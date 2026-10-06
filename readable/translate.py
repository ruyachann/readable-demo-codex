"""翻訳: Translator インターフェース、DummyTranslator、⟦n⟧ による unit の分割、翻訳キャッシュ。

M1 では Dummy のみ実装。Gemini は M3 で GeminiTranslator を実装する。
翻訳の単位 (unit) = 1 frame、または joins でつながった複数 frame (⟦n⟧ で境界を示す)。
"""
from __future__ import annotations

import hashlib
import html as htmllib
import json
import os
import re
import threading
import unicodedata
from abc import ABC, abstractmethod
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Config, atomic_write_text, load_config
from .numcheck import check_numbers
from .gemini_client import DailyLimitError, GeminiContentError, GeminiError, RateLimitError
from .timing import timed
from .extract import VAR_RE, Vocab, _p, join_pair, plain_html, protect_html, restore_vars

MARK_RE = re.compile(r"\s*⟦(\d+)⟧\s*")
TAG_SPLIT_RE = re.compile(r"(<[^>]+>|⟦\d+⟧|\{v\d+\})")
ALLOWED_TAGS = ("sup", "sub", "i", "b")
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "gemini_translate.md"


def marker(n: int) -> str:
    return f"⟦{n}⟧"


# --------------------------------------------------------------------------
# unit の構築
# --------------------------------------------------------------------------

def build_units(doc: dict, cfg: Config | None = None) -> list[dict]:
    """doc.json から翻訳 unit の一覧を作る。doc は変更しない (結合時のハイフン解除は unit のテキストだけで行う)。

    unit = {"id", "frames": [frame_id...], "role", "page", "text": "A ⟦1⟧ B", "vars": {"v1": html}}。
    - joins は両 frame とも translate=True のときのみ連結する (重複・自己結合は無視)。
    - joins 境界の行末ハイフンは doc["vocab"] で判定して語を前 frame に寄せる (タグをまたぐ場合も)。
      joins を差し替えて呼び直せば、ハイフン処理も最終 joins で再計算される。
    - メール/URL/DOI/助成番号/数式片は {vN} に置換する (vars に元 html)。unit 内で通し番号。
    """
    cfg = cfg or load_config()
    protect = bool(_p(cfg.section("extract"), "protect_inline"))
    vocab = Vocab.from_json(doc.get("vocab"))
    frames = {f["id"]: f for p in doc["pages"] for f in p["frames"]}
    order = [f["id"] for p in doc["pages"] for f in p["frames"]]
    nxt: dict[str, str] = {}
    has_prev: set[str] = set()
    for a, b in doc.get("joins", []):
        if a in frames and b in frames and frames[a]["translate"] and frames[b]["translate"] \
                and a not in nxt and b not in has_prev and a != b:
            nxt[a] = b
            has_prev.add(b)
    units = []
    n = 0
    for fid in order:
        f = frames[fid]
        if not f["translate"] or fid in has_prev:
            continue
        chain = [fid]
        while chain[-1] in nxt and nxt[chain[-1]] not in chain:
            chain.append(nxt[chain[-1]])
        n += 1
        htmls = [frames[cid]["html"] for cid in chain]
        for i in range(len(htmls) - 1):
            r = join_pair(htmls[i], htmls[i + 1], vocab)
            if r is not None:
                htmls[i], htmls[i + 1] = r
        text = ""
        vars_: dict[str, str] = {}
        for i, h in enumerate(htmls):
            if protect:
                h, v = protect_html(h, start=len(vars_) + 1)
                vars_.update(v)
            else:
                h = plain_html(h)
            if i:
                text += f" {marker(i)} "
            text += h
        units.append({"id": f"u{n}", "frames": chain, "role": f["role"], "page": f["page"], "text": text,
                      "vars": vars_})
    return units


# --------------------------------------------------------------------------
# 後処理
# --------------------------------------------------------------------------

_A_OPEN = r'a href="[^"<>]*"'


def build_cell_units(doc: dict, cfg: Config | None = None) -> list[dict]:
    """表のセル (role=table) と図の中の文字 (role=figure_text) の各行を、ホバー注釈用に訳す unit にする (本体の描画は変えない。
    図表の中の文字は翻訳して差し替えず、すべて同じ形の注釈にする)。数値・記号だけのものは除く (OCR ページの図の断片も除く)。
    件数は表が [table_terms] max_cells、図が max_figure_cells (既定 300) まで。unit id は c0, c1 ... で、"cell": [frame_id, 行番号]。"""
    cfg = cfg or load_config()
    tc = cfg.section("table_terms")
    if not tc.get("annotations", True):
        return []
    out: list[dict] = []
    n_table = n_fig = 0
    max_t, max_f = int(tc.get("max_cells", 200)), int(tc.get("max_figure_cells", 300))
    for p in doc["pages"]:
        for f in p["frames"]:
            if f["role"] not in ("table", "figure_text") or (f["role"] == "figure_text" and p.get("scanned_ocr")):
                continue
            rows = f.get("row_texts") or ([f["text"]] if f["role"] == "figure_text" and len(f.get("rows") or []) == 1 else [])
            for i, t in enumerate(rows):
                t = re.sub(r"\s+", " ", t).strip()
                if len(re.findall(r"[A-Za-z]{2,}", t)) == 0:
                    continue
                if f["role"] == "table":
                    if n_table >= max_t:
                        continue
                    n_table += 1
                else:
                    if n_fig >= max_f:
                        continue
                    n_fig += 1
                out.append({"id": f"c{len(out)}", "role": "label", "page": p["number"], "frames": [],
                            "text": htmllib.escape(t, quote=False), "vars": {}, "cell": [f["id"], i]})
    return out


def sanitize_ja(s: str) -> str:
    """翻訳結果を安全な html にする: 許可タグ(sup/sub/i/b/a href)以外の '<' と裸の '&' をエスケープ。"""
    s = re.sub(r"<(?!/?(?:%s)>|/a>|%s>)" % ("|".join(ALLOWED_TAGS), _A_OPEN), "&lt;", s)
    s = re.sub(r"&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)", "&amp;", s)
    return s


def plain_len(h: str) -> int:
    return len(htmllib.unescape(re.sub(r"<[^>]+>", "", h)))


_FOREIGN_SCRIPT_RE = re.compile("[ᄀ-ᇿ가-힯Ѐ-ӿ฀-๿؀-ۿ]")
_TAG_RE = re.compile(r"<(/?)(sup|sub|i|b|a)(?:\s[^>]*)?>")


def validate_translation(src: str, ja: str) -> list[str]:
    """原文 unit テキストと訳文のタグ列・{vN}・⟦n⟧ の整合を検査し、問題のリストを返す (空なら OK)。

    タグ (sup/sub/i/b/a) は種別ごとの個数と開閉の入れ子、<a href> は href の多重集合、
    {vN} は個数まで一致、⟦n⟧ は出現順まで一致していること。"""
    problems: list[str] = []
    ts = [(m.group(1), m.group(2)) for m in _TAG_RE.finditer(src)]
    tj = [(m.group(1), m.group(2)) for m in _TAG_RE.finditer(ja)]
    # 太字・斜体 (b/i) は書体だけの情報なので個数は問わない (<b><i>…</i></b> の重ね方・片方だけの省略を許す)。
    # sup/sub/a は意味を持つので個数まで一致させる。開閉の入れ子が正しいことは下で全タグについて検査する
    keep = ("sup", "sub", "a")
    if Counter(t for t in ts if t[1] in keep) != Counter(t for t in tj if t[1] in keep):
        problems.append("タグの種類/個数が原文と一致しない")
    depth: list[str] = []
    for close, name in tj:
        if not close:
            depth.append(name)
        elif depth and depth[-1] == name:
            depth.pop()
        else:
            problems.append("タグの開閉が不正")
            break
    else:
        if depth:
            problems.append("閉じられていないタグがある")
    if Counter(re.findall(r'<a href="([^"]*)">', src)) != Counter(re.findall(r'<a href="([^"]*)">', ja)):
        problems.append("リンク href が一致しない")
    if Counter(VAR_RE.findall(src)) != Counter(VAR_RE.findall(ja)):
        problems.append("{vN} の個数/種類が一致しない")
    if re.findall(r"⟦(\d+)⟧", src) != re.findall(r"⟦(\d+)⟧", ja):
        problems.append("⟦n⟧ の順序/個数が一致しない")
    rest_j = re.sub(r"⟦\d+⟧", "", ja)
    if re.search(r"[⟦⟧]", rest_j) and not re.search(r"[⟦⟧]", re.sub(r"⟦\d+⟧", "", src)):
        problems.append("原文に無いマーカー記号 ⟦ ⟧ が混入している")
    if _FOREIGN_SCRIPT_RE.search(ja) and not _FOREIGN_SCRIPT_RE.search(src):
        problems.append("日本語・英語以外の文字 (ハングル・キリル文字など) が混入している")
    # 英語以外のアクセント付きの語 (「metodología」など。他言語の混入) が、原文に無いのに訳文にあるとき
    acc = re.compile(r"[A-Za-z]*[áéíóúñãõçàèìòù][A-Za-záéíóúñãõçàèìòù]*", re.I)
    src_acc = {w.lower() for w in acc.findall(src)}
    foreign = [w for w in acc.findall(_STRIP_RE.sub(" ", ja)) if w.lower() not in src_acc and len(w) >= 4]
    if foreign:
        problems.append("原文に無い他言語の語が混入している: " + ", ".join(sorted(set(foreign))[:3]))
    # 数式の文字化け: 原文に無い ƒ・Latin 拡張 B の字 (「{」が「ƒ」になる、など) が訳文に出たとき
    weird = [c for c in set(ja) if (c == "ƒ" or "ƀ" <= c <= "ɏ") and c not in src]
    if weird:
        problems.append("原文に無い記号 (" + "".join(sorted(weird)) + ") が混入している (数式の文字化けの疑い)")
    stray = lambda t: re.sub(r"\{v\d+\}", "", t)
    if ("{" in stray(ja) or "}" in stray(ja)) and not ("{" in stray(src) or "}" in stray(src)):
        problems.append("原文に無い波括弧 { } が混入している")
    return problems


def validate_tags(src: str, ja: str) -> bool:
    return not validate_translation(src, ja)


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}\b)")
_CITE_RE = re.compile(r"\[\s*\d+(?:\s*[,–\-‐]\s*\d+)*\s*\]")
_STRIP_RE = re.compile(r"<[^>]+>|⟦\d+⟧|\{v\d+\}|&#?\w+;")


def _plain_for_check(s: str) -> str:
    return _STRIP_RE.sub(" ", s)


_UNIT_WORDS_RE = re.compile("[万億兆千百]")


def repair_style_tags(src: str, ja: str) -> str:
    """原文に無い太字・斜体・上付き/下付きのタグを訳文が付け足したときは、タグだけ外す (図表ラベルを太字にする・I_B に添え字を付ける癖。これだけで検証 NG にして原文に戻さない)。"""
    for tag in ("b", "i", "sub", "sup", "u"):
        if not re.search(r"<%s>" % tag, src, re.I) and re.search(r"</?%s>" % tag, ja, re.I):
            ja = re.sub(r"</?%s>" % tag, "", ja, flags=re.I)
    # 訳文が ⟦~⟧ のように、原文に無い記号を ⟦ ⟧ で囲んだとき (マーカーの真似) は囲みだけ外す (⟦数字⟧ は本物のマーカーなので残す)
    if not re.search(r"[⟦⟧]", re.sub(r"⟦\d+⟧", "", src)):
        ja = re.sub(r"⟦(?!\d+⟧)([^⟦⟧]{0,12})⟧", r"\1", ja)
    # 太字・斜体の入れ子の順序が崩れているとき (<b><i>…</b></i>) は、b/i のタグだけ全て外す
    depth: list[str] = []
    for m in re.finditer(r"<(/?)(b|i)>", ja):
        if not m.group(1):
            depth.append(m.group(2))
        elif depth and depth[-1] == m.group(2):
            depth.pop()
        else:
            return re.sub(r"</?(?:b|i)>", "", ja)
    if depth:
        ja = re.sub(r"</?(?:b|i)>", "", ja)
    return ja


def check_translation(src: str, ja: str, soft: list[str] | None = None, ocr: bool = False) -> list[str]:
    """validate_translation (タグ・{vN}・⟦n⟧) に加え、空訳・[n] 引用・数値の欠落を検査する。

    数値は全角数字を NFKC で半角にそろえて比べる。訳文に 万/億/兆/千/百 があり数値が合わないときは、数量の言い換え
    (5 million -> 500万 など) とみなして問題にせず、soft (リスト) があれば警告文を追加する (再送しない)。"""
    if not ja or not ja.strip():
        return ["訳文が空"]
    problems = validate_translation(src, ja)
    ps, pj = _plain_for_check(src), _plain_for_check(ja)
    cs = Counter(re.sub(r"\s+", "", c).replace("‐", "-") for c in _CITE_RE.findall(ps))
    cj = Counter(re.sub(r"\s+", "", c).replace("‐", "-") for c in _CITE_RE.findall(pj))
    if cs != cj:
        problems.append("[n] 引用が原文と一致しない")
    problems += check_numbers(src, ja, ocr=ocr)
    return problems


_CJK_RE = re.compile("[\u3040-\u30ff\u3400-\u9fff]")
LEFTOVER_ROLES = {"body", "abstract", "caption", "footnote", "keywords", "sidebar"}
_EN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’\-]*")
_LOWER_WORD_RE = re.compile(r"[a-z][a-z'’\-]+")
_LOWER_RUN_RE = re.compile(r"(?:[a-z][a-z'’\-]+[ ,;]+){3,}[a-z][a-z'’\-]+")


def _is_name_token(tok: str) -> bool:
    """固有名詞・ソフトウェア名・略語・統計記号らしいトークン: 大文字を含む (Python, NumPy, EEG, SD, pH)、または 1 文字、数字を含む。"""
    return any(c.isupper() for c in tok) or len(tok) < 2 or any(c.isdigit() for c in tok)


def check_keep_en(src: str, ja: str, entries: list[dict]) -> list[str]:
    """表の用語 (keep_en) が原文に出てくるのに、訳文に英語の併記が無いものを返す (大文字小文字は区別しない)。"""
    probs = []
    plain_src = _STRIP_RE.sub(" ", src)
    plain_ja = _STRIP_RE.sub(" ", ja)
    for g in entries:
        if not g.get("keep_en"):
            continue
        en = g["en"]
        if not (g.get("abbr") or len(en.split()) >= 2):
            continue   # 一般語と重なる 1 語 (Other, Social など) は検査しない (プロンプトの規則だけ。再送の無駄を避ける)
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(en) + r"(?:s|es)?(?![A-Za-z0-9])", plain_src, re.I) \
                and not re.search(re.escape(en), plain_ja, re.I):
            probs.append(f"表の用語「{en}」の英語が併記されていない (日本語訳 (English) の形にする)")
    return probs


def check_glossary_applied(src: str, ja: str, entries: list[dict]) -> list[str]:
    """固定訳 (glossary_fixed.toml。note が「固定訳」) の複数語の用語が原文にあるのに、訳文に訳語が無く、その語の一部が英語のまま日本語の中に残っているものを返す
    (例: correct rejection -> 「CR (正 rejection)」)。表の用語 (keep_en。英語併記が意図) と、訳語に英字を含む項目は対象にしない。"""
    probs = []
    plain_src = _STRIP_RE.sub(" ", src).lower()
    plain_ja = _STRIP_RE.sub(" ", ja)
    toks = {t.lower() for t in _EN_TOKEN_RE.findall(plain_ja)}
    for g in entries:
        en, jt = g.get("en", ""), g.get("ja", "")
        words = [w for w in re.split(r"[\s\-]+", en.lower()) if len(w) >= 4]
        if g.get("note") != "固定訳" or g.get("keep_en") or len(en.split()) < 2 or not words or not jt or re.search(r"[A-Za-z]", jt):
            continue
        if not re.search(r"(?<![a-z0-9])" + re.escape(en.lower()) + r"(?:s|es)?(?![a-z0-9])", plain_src) or jt in plain_ja:
            continue
        left = [w for w in words if w in toks]
        if left:
            probs.append(f"用語集の訳語「{jt}」({en}) が使われず、英語 ({', '.join(left)}) が日本語の中に残っている")
    return probs


def leftover_english(src: str, ja: str, role: str = "body") -> bool:
    """訳文に普通の英単語が連続して残っているか。固有名詞・ソフトウェア名・略語・統計記号 (大文字を含む語、1 文字、数字入りの語)・
    著者年引用・タグ・{vN}・URL・メールは数えない。小文字の英単語が 4 語以上連続する、または (記号類を除いた) 英字の比率が高い訳を英語残りとする。
    本文系の role のみ判定する。keywords は和文が全く無ければ英語のまま返されたとみなす。"""
    if role in ("heading", "title"):
        # 見出し・題名は短いので英字の比率では判定できない: 和文が 1 字も無く、普通の英単語が 3 語以上あれば英語のまま返された
        tj = _STRIP_RE.sub(" ", ja)
        return not _CJK_RE.search(tj) and len(re.findall(r"[A-Za-z]{3,}", tj)) >= 3 and len(re.findall(r"[a-z]{3,}", tj)) >= 2
    if role not in LEFTOVER_ROLES:
        return False
    t = _STRIP_RE.sub(" ", ja)
    t = re.sub(r"https?://\S+|\S+@\S+\.\S+", " ", t)
    t = re.sub(r"\([^()]*\d{4}[a-z]?[^()]*\)|\[[^\]]*\]", " ", t)
    plain_src = _STRIP_RE.sub(" ", src)
    if role == "keywords" and not _CJK_RE.search(t):
        return True  # キーワード一覧が英語のまま返された
    if len(plain_src.strip()) < 40:
        return False
    # 固有名詞らしいトークンを取り除いてから、普通の英単語だけを見る
    t2 = _EN_TOKEN_RE.sub(lambda m: " " if _is_name_token(m.group(0)) else m.group(0), t)
    if _LOWER_RUN_RE.search(t2):
        return True
    latin = sum(len(w) for w in _LOWER_WORD_RE.findall(t2))
    cjk = len(_CJK_RE.findall(t2))
    return latin + cjk > 60 and latin / (latin + cjk) > 0.7


_TOK = re.compile(r"<[^>]+>|&#?\w+;|\{v\d+\}|⟦\d+⟧|.", re.S)


def _boundaries(ja: str) -> list[tuple[int, list[str]]]:
    """分割可能位置 (トークン境界: タグ・実体参照・{vN}・⟦n⟧ の途中を除く) と、その位置で開いているタグ。"""
    out: list[tuple[int, list[str]]] = [(0, [])]
    stack: list[str] = []
    for m in _TOK.finditer(ja):
        t = m.group(0)
        if t.startswith("</"):
            name = t[2:-1]
            for i in range(len(stack) - 1, -1, -1):
                if re.match(r"<%s[\s>]" % name, stack[i]):
                    del stack[i]
                    break
        elif t.startswith("<") and len(t) > 1 and t.endswith(">"):
            stack.append(t)
        out.append((m.end(), list(stack)))
    return out


def split_by_ratio(ja: str, weights: list[float]) -> list[str]:
    """マーカーが無い場合のフォールバック: 重み (原文文字数) 比で '。' に寄せて分割する。
    タグ・実体参照・{vN} の途中では切らず、タグ内で切る場合は閉じ/再オープンで補完する。"""
    n = len(weights)
    if n <= 1:
        return [ja]
    total = float(sum(weights)) or 1.0
    L = len(ja)
    pos_state = {p: st for p, st in _boundaries(ja)}
    positions = sorted(pos_state)
    cuts: list[int] = []
    acc = 0.0
    prev = 0
    for k in range(n - 1):
        acc += weights[k]
        target = int(round(L * acc / total))
        hi = L - (n - 1 - k)
        span = max(int(L * weights[k] / total * 0.3), 3)
        cand = [p for p in positions if prev < p <= max(hi, prev + 1)] or [p for p in positions if p > prev] or [L]
        best = None
        for m in re.finditer(r"[。．！？]", ja):
            p = m.end()
            if p in cand and not pos_state.get(p) and abs(p - target) <= span \
                    and (best is None or abs(p - target) < abs(best - target)):
                best = p
        if best is None:
            flat = [p for p in cand if not pos_state.get(p)] or cand
            best = min(flat, key=lambda p: abs(p - target))
        cuts.append(best)
        prev = best
    parts, s, reopen = [], 0, []
    for c in cuts + [L]:
        seg = "".join(reopen) + ja[s:c]
        st = pos_state.get(c, [])
        seg += "".join("</%s>" % re.match(r"<(\w+)", t).group(1) for t in reversed(st))
        parts.append(seg.strip())
        reopen = list(st)
        s = c
    return parts


_LEADING_CLOSE_RE = re.compile(r"^\s*([。、，．）」』】〉》］｝！？)\]!?,.]+)")


def fix_leading_punct(parts: list[str]) -> list[str]:
    """frame の先頭に来た閉じ括弧・句読点 (。、）」 など) を、前の frame の末尾へ移す (行頭禁則)。"""
    out = list(parts)
    for i in range(1, len(out)):
        m = _LEADING_CLOSE_RE.match(out[i])
        if m and out[i - 1].strip():
            out[i - 1] = out[i - 1].rstrip() + m.group(1)
            out[i] = out[i][m.end():].lstrip()
    return out


def split_unit(ja: str, weights: list[float]) -> list[str]:
    return fix_leading_punct(_split_unit(ja, weights))


def _split_unit(ja: str, weights: list[float]) -> list[str]:
    """unit の訳文を frame ごとに分割する。⟦n⟧ が 1..n-1 の順に揃っていればそれを使い、そうでなければ文字数比。"""
    n = len(weights)
    ja = sanitize_ja(ja)
    if n == 1:
        return [MARK_RE.sub(" ", ja).strip()]
    parts = MARK_RE.split(ja)  # [t0, '1', t1, '2', t2 ...]
    texts, nums = parts[0::2], parts[1::2]
    if nums == [str(i) for i in range(1, n)] and len(texts) == n and all(t.strip() for t in texts):
        return [t.strip() for t in texts]
    flat = MARK_RE.sub("", ja)
    return split_by_ratio(flat, weights)


def frames_translations(doc: dict, units: list[dict], ja_by_unit: dict[str, str],
                        warnings: list[str] | None = None) -> dict[str, str]:
    """unit ごとの訳文 -> frame_id ごとの訳文 (html)。{vN} は元の html に戻す。
    訳文から落ちた {vN} (メール/助成番号など) は末尾に補う (警告に記録)。"""
    frames = {f["id"]: f for p in doc["pages"] for f in p["frames"]}
    out: dict[str, str] = {}
    for u in units:
        ja = ja_by_unit.get(u["id"])
        if ja is None:
            continue
        weights = [max(plain_len(frames[fid]["html"]), 1) for fid in u["frames"]]
        parts = split_unit(ja, weights)
        vars_ = u.get("vars", {})
        parts = [restore_vars(p, vars_) for p in parts]
        missing = [k for k in vars_ if "{%s}" % k not in ja]
        if missing:
            parts[-1] += " " + " ".join(vars_[k] for k in missing)
            if warnings is not None:
                warnings.append(f"{u['id']}: 訳文から {','.join('{%s}' % k for k in missing)} が欠落したため末尾に補いました")
        for fid, part in zip(u["frames"], parts):
            out[fid] = part
    for fid, part in list(out.items()):
        clean = sanitize_leftovers(part)
        if clean != part:
            out[fid] = clean
            if warnings is not None:
                warnings.append(f"{fid}: 訳文に残っていた内部記号 (⟦ ⟧ / {{vN}}) を取り除きました")
    return out


_LEFTOVER_RE = re.compile(r"⟦\d+⟧|⟦|⟧|\{v\d+\}")


def _added_glosses(src: str, ja: str) -> bool:
    """訳文が、原文に無い「(English)」の併記を足しているか (英語のまま + 括弧の併記だけの訳の判定)。"""
    pat = re.compile(r"\([A-Za-z][A-Za-z \-]{1,30}\)")
    return len(pat.findall(_STRIP_RE.sub(" ", ja))) > len(pat.findall(_STRIP_RE.sub(" ", src)))


def sanitize_leftovers(html: str) -> str:
    """描画の直前の安全網: frame ごとの訳文に ⟦ ⟧ や展開されなかった {vN} が残っていたら取り除く (⟦~⟧ なら中身の ~ は残す)。"""
    html = re.sub(r"⟦(?!\d+⟧)([^⟦⟧]{1,12})⟧", r"\1", html)
    return _LEFTOVER_RE.sub("", html)


# --------------------------------------------------------------------------
# 翻訳キャッシュ
# --------------------------------------------------------------------------

def prompt_hash(path: str | Path | None = None) -> str:
    p = Path(path) if path else PROMPT_PATH
    return hashlib.sha256(p.read_bytes() if p.exists() else b"").hexdigest()


def unit_key(unit: dict, model: str, phash: str) -> str:
    """キャッシュキー = sha256(role, unit の html, モデル, プロンプト等のハッシュ)。unit id (連番) には依存しない。
    role は訳し方 (体言止め/常体など) を変えるのでキーに入れる。"""
    return hashlib.sha256("\0".join((unit.get("role", ""), unit["text"], model, phash)).encode("utf-8")).hexdigest()


FAIL_PREFIX = "fail:"


class Cache:
    """sha256 キー -> 訳文 の JSON キャッシュ。保存は一時ファイル + os.replace (書き込み中の中断で壊れない)。

    読み込めない (壊れた) ファイルは .bak に退避して空から始める。失敗した unit は "fail:<キー>" -> {"problems", "count"} (JSON 文字列) で記録し、
    次回以降は再送しない (retry_failed のときだけ再送)。"""

    def __init__(self, path: str | Path, log=None):
        self.path = Path(path)
        self.log = log
        self.notes: list[str] = []
        self.d: dict[str, str] = {}
        if str(self.path) != os.devnull and self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(d, dict):
                    raise ValueError("辞書ではありません")
                self.d = {str(k): v for k, v in d.items() if isinstance(v, str)}
            except (OSError, ValueError) as e:
                self.d = {}
                bak = self.path.with_name(self.path.name + ".bak")
                try:
                    self.path.replace(bak)
                except OSError:
                    pass
                msg = f"翻訳キャッシュ {self.path} を読めないため {bak.name} に退避して空から始めます ({e})"
                self.notes.append(msg)
                if log:
                    log(f"[警告] {msg}")

    def get(self, key: str) -> str | None:
        return self.d.get(key)

    def put(self, key: str, val: str) -> None:
        self.d[key] = val
        self.d.pop(FAIL_PREFIX + key, None)

    def failure(self, key: str) -> dict | None:
        v = self.d.get(FAIL_PREFIX + key)
        if not v:
            return None
        try:
            r = json.loads(v)
            return r if isinstance(r, dict) else {"problems": [str(r)], "count": 1}
        except ValueError:
            return {"problems": [v], "count": 1}

    def mark_failed(self, key: str, problems: list[str]) -> None:
        prev = self.failure(key) or {}
        self.d[FAIL_PREFIX + key] = json.dumps({"problems": [str(x)[:200] for x in problems][:5],
                                                "count": int(prev.get("count", 0)) + 1}, ensure_ascii=False)

    def clear_failed(self, key: str) -> None:
        self.d.pop(FAIL_PREFIX + key, None)

    def save(self) -> None:
        if str(self.path) == os.devnull:
            return
        atomic_write_text(self.path, json.dumps(self.d, ensure_ascii=False, indent=0))


def translate_cached(translator: "Translator", units: list[dict], cache_path: str | Path,
                     phash: str | None = None, context: dict | None = None, log=None) -> dict[str, str]:
    """キャッシュに無い unit だけ翻訳し、{unit_id: 訳文} を返す。キャッシュは cache_path に書き戻す。

    translator.uses_cache = True の翻訳器 (Gemini) は、段階ごと (翻訳/見直し) にバッチ単位で Cache へ書き込みながら進み、
    日次上限などで中断してもそこまでの結果が残る (翌日の再実行は続きから再開)。"""
    cache = Cache(cache_path, log=log)
    if getattr(translator, "uses_cache", False):
        try:
            return translator.translate(units, context=context, cache=cache)
        finally:
            cache.save()
    phash = phash if phash is not None else prompt_hash()
    model = getattr(translator, "model", translator.name)
    keys = {u["id"]: unit_key(u, model, phash) for u in units}
    todo = [u for u in units if cache.get(keys[u["id"]]) is None]
    if todo:
        res = translator.translate(todo, context=context)
        for u in todo:
            if u["id"] in res:
                cache.put(keys[u["id"]], res[u["id"]])
        cache.save()
    return {u["id"]: cache.get(keys[u["id"]]) for u in units if cache.get(keys[u["id"]]) is not None}


# --------------------------------------------------------------------------
# Translator
# --------------------------------------------------------------------------

class Translator(ABC):
    """翻訳器のインターフェース。units を受け取り {unit_id: 訳文} を返す。

    訳文は <sup>/<sub>/<i>/<b>/<a href> と {vN} と ⟦n⟧ を保持すること (PLAN §11-9)。
    """
    name = "base"
    model = "base"

    @abstractmethod
    def translate(self, units: list[dict], context: dict | None = None) -> dict[str, str]:
        ...


DUMMY_SAMPLE = ("これはレイアウト確認用のダミー訳文であり、原文の意味とは無関係です。"
                "文章の長さを日本語の平均的な密度に合わせて調整しています。")


class DummyTranslator(Translator):
    """frame ごとに「【訳】」+ 英文長×ratio 程度の日本語ダミー文。タグ・{vN}・⟦n⟧ は保持する。"""
    name = "dummy"

    def __init__(self, ratio: float = 0.38):
        self.ratio = ratio
        self.model = f"dummy-{ratio}"

    def _segment(self, seg: str) -> str:
        toks = TAG_SPLIT_RE.split(seg)
        out: list[str] = []
        depth = 0
        first = True
        pos = 0
        last_plain_idx = None
        for t in toks:
            if not t:
                continue
            if re.fullmatch(r"\{v\d+\}", t):
                out.append(t)  # 保護された識別子/数式片はそのまま
                continue
            m = re.match(r"<(/?)(\w+)", t)
            if m:
                if m.group(2) in ("sup", "sub"):
                    depth += -1 if m.group(1) else 1
                out.append(t)
                continue
            if depth > 0:
                out.append(t)  # 上付き/下付きの中身は原文のまま
                continue
            n = len(htmllib.unescape(t).strip())
            if n == 0:
                out.append(t)
                continue
            k = max(1, round(n * self.ratio))
            piece = ""
            if first:
                piece = "【訳】"
                k = max(k - 3, 0)
                first = False
            for _ in range(k):
                piece += DUMMY_SAMPLE[pos % len(DUMMY_SAMPLE)]
                pos += 1
            out.append(piece)
            last_plain_idx = len(out) - 1
        if last_plain_idx is not None and not out[last_plain_idx].rstrip().endswith("。"):
            out[last_plain_idx] = out[last_plain_idx].rstrip() + "。"
        return "".join(out)

    def translate(self, units: list[dict], context: dict | None = None) -> dict[str, str]:
        res = {}
        for u in units:
            parts = re.split(r"(⟦\d+⟧)", u["text"])
            res[u["id"]] = "".join(p if re.fullmatch(r"⟦\d+⟧", p) else self._segment(p) for p in parts)
        return res


FALLBACK_TRANSLATE = (
    "あなたは学術論文の英日翻訳者である。入力 JSON の units を、常体(である調)の自然な日本語に翻訳し、"
    "[{\"id\":..., \"text\":...}] の JSON 配列のみを返す。id と順序を変えず、要素を増減しない。"
    "<i> <b> <sup> <sub> <a href=...> タグ、{v1} 形式のプレースホルダ、⟦1⟧ 形式のマーカー、[3] 形式の引用、数値、URL は原文のまま保持する。"
    "context.glossary の訳語を使い、context.prev_text は直前の文脈(翻訳しない)である。")
RETRY_NOTE_VALIDATE = ("前回の訳は検証に失敗した ({})。原文のタグ・{{vN}}・⟦n⟧・[n] 引用・数値を同じ個数・同じ順序で保ち、訳し直すこと。")
RETRY_NOTE_ENGLISH = "前回の訳は英語が多く残っていた。固有名詞・略語・ソフトウェア名以外は全て日本語に訳し直すこと(定型文も訳す)。"
FALLBACK_REFINE = (
    "あなたは英日翻訳の校閲者である。入力 JSON の各 unit は source(原文)と draft(訳文)を持つ。誤訳・訳抜け・不自然な日本語・"
    "用語の不統一だけを直し、[{\"id\":..., \"text\":..., \"changed\":true/false}] の JSON 配列を返す。変更が無い unit は changed=false と"
    "し text に draft を入れる。タグ、{vN}、⟦n⟧、[n]、数値は変えない。")

# スキャン + OCR の文書にだけ足す 1 行 (他の文書のキャッシュ鍵を変えないよう、プロンプトファイルではなく文書ごとの追加にしている)
OCR_RULE = "\n- 原文は OCR (文字認識) の誤りを含むことがある (例: Phyriol, text-figurem)。文脈から意図された語を推定して訳す。\n"
# role "label" (図表の中の語句。ホバー注釈用) の扱い。プロンプトファイルに書くと全 unit のキャッシュ鍵が変わるので、コードで足す
LABEL_RULE = """
# role: label (図・表の中の短い語句。軸ラベル・凡例・群の名前・セルの語。原文は図表にそのまま残り、これは注釈で読まれる)
- 名詞句として自然に和訳する (例: Pre-sleep → 睡眠前, Time (sec) → 時間 (秒), Correct-Correct → 正解-正解, Accuracy (%) → 正答率 (%))。
- 英語を括弧で併記しない。略語・記号・単位・数値・固有名詞・「Check for updates」のような操作表示は原文のまま返してよい。
- glossary に載っている語は必ずその訳語に従う (本文と揃える)。
"""
KEEP_EN_RULE_VERSION = 2
KEEP_EN_RULE = """
# 表の用語 (keep_en)
- glossary のうち keep_en: true の語は、この論文の表・図に英語のまま載っている用語である。本文では、その語が出るたびに必ず「日本語訳 (English)」の形にし、英語を省略しない (2 回目以降も同じ)。abbr がある語は「日本語訳 (English, abbr)」とする (例: 移動 (Locomotion, Lo))。
- 原文がすでに英語の用語をそのまま使っている箇所 (例: "Active behaviour") も同じ規則に従う (例: 「活動的 (Active) 行動」)。
- 括弧内の English は glossary の en の綴り (大文字小文字を含む) のまま、半角括弧に入れる。表の用語でない語には付けない。
- 英語は用語の全体に 1 回だけ付ける。複数の語から成る用語は、語ごとに括弧を付けない (良い: 「データ収集 (data collection)」、悪い: 「データ(Data)収集(collection)」)。
- 文章の全体を英語のまま残して括弧だけを足す訳は誤り。必ず日本語の文に訳し、用語のところだけ括弧で英語を付ける。
- 役割が caption (図表の説明) の unit と、表の中の語句には、英語を併記しない (日本語訳だけにする)。
"""

TRANSLATE_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "id": {"type": "STRING"}, "text": {"type": "STRING"}}, "required": ["id", "text"]}}
REFINE_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "id": {"type": "STRING"}, "text": {"type": "STRING"}, "changed": {"type": "BOOLEAN"}},
    "required": ["id", "text", "changed"]}}


def make_batches(units: list[dict], max_pages: int, max_chars: int, size=lambda u: len(u["text"])) -> list[list[dict]]:
    """unit を 開始ページ基準で最大 max_pages ページ・max_chars 文字ごとのバッチにまとめる。"""
    batches: list[list[dict]] = []
    cur: list[dict] = []
    pages: set[int] = set()
    chars = 0
    for u in units:
        sz = size(u)
        newp = pages | {u["page"]}
        if cur and (len(newp) > max_pages or chars + sz > max_chars):
            batches.append(cur)
            cur, pages, chars = [], set(), 0
            newp = {u["page"]}
        cur.append(u)
        pages = newp
        chars += sz
    if cur:
        batches.append(cur)
    return batches


class GeminiTranslator(Translator):
    """Gemini による翻訳 (+ 見直し)。

    translate(units, context, cache): バッチごとに ① 翻訳 (translate_model) -> 検証 NG の unit だけ再送 (最大 validate_retries 回) ->
    ② 見直し (refine_model, 設定で OFF 可) -> ③ (見直し OFF のとき) 検証 NG/英語残りの unit だけ選択的に再翻訳 (refine_model)。
    検証を通った unit は、その場でキャッシュに保存する (日次上限などで中断してもそこまでが残り、翌日の再実行は続きから再開)。
    エラーの扱い: 日次上限 (DailyLimitError)・設定/権限の誤り (GeminiFatalError)・再試行しても直らない 5xx/タイムアウト
    (GeminiTransientError)・分あたり上限 (RateLimitError) は中断して上位へ投げる (原文のまま出力にしない)。
    応答の内容が使えない (GeminiContentError)・検証に通らない unit だけが unit 単位の失敗: 原文を返し、warnings と stats に記録する。
    失敗した unit はキャッシュに失敗として記録し、次回は retry_failed=True のときだけ再送する。
    """
    name = "gemini"
    uses_cache = True

    def __init__(self, cfg: Config | None = None, client=None, log=None, quota=None, retry_failed: bool = False):
        from .gemini_client import GeminiClient
        self.cfg = cfg or load_config()
        g = self.cfg.section("gemini")
        self.g = g
        self.log = log or (lambda s: None)
        self.client = client or GeminiClient(self.cfg, log=self.log, quota=quota)
        self.translate_model = g["translate_model"]
        self.refine_model = g["refine_model"]
        self.do_refine = bool(g.get("refine", False))
        self.retry_failed = retry_failed
        self._lock = threading.RLock()
        self.failures: dict[str, list[str]] = {}        # unit id -> 原文のまま残った原因 (検証の問題点)
        self.model = self.translate_model + ("+" + self.refine_model if self.do_refine else "")
        self.warnings: list[str] = []
        self.stats = {"batches": 0, "refine_batches": 0, "validate_resend": 0, "validate_failed": 0,
                      "refine_changed": 0, "refine_rejected": 0, "refine_skipped": False, "units": 0,
                      "selective_batches": 0, "selective_fixed": 0, "skipped_failed": 0, "fallback_original": 0,
                      "dup_ids": 0}
        self._last_problems: dict[str, list[str]] = {}

    def check_ready(self) -> None:
        self.client.check_ready()

    # -- プロンプト --------------------------------------------------------
    def _system(self, kind: str, glossary: list[dict], context: dict) -> str:
        from .gemini_client import fill_placeholders, load_prompt_file
        fb = FALLBACK_TRANSLATE if kind == "translate" else FALLBACK_REFINE
        sysm = fill_placeholders(load_prompt_file(self.g.get(f"{kind}_prompt"), fb), glossary, context)
        if kind == "translate" and any(g.get("keep_en") for g in (context.get("glossary") or [])):
            sysm += "\n" + KEEP_EN_RULE
        if kind == "translate" and getattr(self, "ocr", False):
            sysm += OCR_RULE
        if kind == "translate":
            sysm += LABEL_RULE
        return sysm

    def _digests(self) -> tuple[str, str]:
        """(翻訳, 見直し) のキャッシュキー用ハッシュ: 実際に使うプロンプト本文 (ファイルが無ければ内蔵の文面) と生成設定。"""
        sysT, sysR = self._system("translate", [], {}), self._system("refine", [], {})
        gen = f"{self.client.temperature}|{self.client.thinking_level}"
        pt = hashlib.sha256(f"{sysT}|{gen}".encode()).hexdigest()
        pr = hashlib.sha256(f"{pt}|{sysR}".encode()).hexdigest()
        return pt, pr

    @staticmethod
    def _gsub(glossary: list[dict], text: str) -> str:
        """その unit の原文に出現する用語 (en/ja のみ) のハッシュ。用語集全体ではなく、関係する語の変更だけがキーに効く。"""
        from .glossary import filter_glossary
        sub = sorted(({k: g[k] for k in ("en", "ja", "keep_en", "abbr") if k in g and g[k]} for g in filter_glossary(glossary, text)),
                     key=lambda x: (x["en"].lower(), x["ja"]))
        if any(x.get("keep_en") for x in sub):
            sub.append({"keep_en_rule": KEEP_EN_RULE_VERSION})
        return hashlib.sha256(json.dumps(sub, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _ctx(context: dict, batch: list[dict], prev_units: list[dict]) -> dict:
        from .glossary import filter_glossary
        text = "\n".join(u["text"] for u in batch)
        # 前の文脈は、タグ・{vN}・⟦n⟧ を除いた平文の末尾 (語やタグの途中から始まらない・このリクエストの vars と食い違わない)
        prev = re.sub(r"\s+", " ", _STRIP_RE.sub(" ", " ".join(u["text"] for u in prev_units))).strip()[-600:]
        gl = [{k: g[k] for k in ("en", "ja", "keep_en", "abbr") if k in g and g[k]} for g in filter_glossary(context.get("glossary") or [], text)]
        return {"title": context.get("title", ""), "summary": context.get("summary", ""), "prev_text": prev,
                "glossary": gl}

    # -- 1 バッチの翻訳 ----------------------------------------------------
    @staticmethod
    def _as_list(res) -> list:
        if isinstance(res, dict):  # 配列を {"units": [...]} などで包んだ場合の保険
            res = next((v for v in res.values() if isinstance(v, list)), [])
        return res if isinstance(res, list) else []

    def _call_translate(self, models: list[str], batch: list[dict], ctx: dict, glossary: list[dict],
                        notes: dict[str, str] | None = None) -> dict[str, str]:
        """translate リクエスト。notes = {unit_id: 再送の理由} は unit の "note" として渡す (再翻訳時のヒント)。
        応答に同じ id が複数あるときは、どれを採るか決められないので無効 (応答に含まれない扱いで再送) にする。"""
        def uj(u):
            d = {"id": u["id"], "role": u["role"], "text": u["text"]}
            if notes and u["id"] in notes:
                d["note"] = notes[u["id"]]
            return d
        user = json.dumps({"context": ctx, "units": [uj(u) for u in batch]}, ensure_ascii=False)
        res = self.client.generate_json_chain(models, self._system("translate", glossary, ctx), user, TRANSLATE_SCHEMA)
        out: dict[str, str] = {}
        seen: Counter = Counter()
        for r in self._as_list(res):
            if isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("text"), str):
                out[r["id"]] = r["text"]
                seen[r["id"]] += 1
        for k, n in seen.items():
            if n > 1:
                out.pop(k, None)
                self.stats["dup_ids"] += 1
                self.warnings.append(f"{k}: 応答に同じ id が {n} 個あったため無効にして再送します")
        return out

    def _call_split(self, models: list[str], batch: list[dict], ctx: dict, glossary: list[dict],
                    notes: dict[str, str] | None, accept=None) -> tuple[dict[str, str], dict[str, str]]:
        """_call_translate。応答が使えない (GeminiContentError) とき、2 unit 以上なら半分に分けて別々に送り直す。
        (得られた訳, {単独でも失敗した unit id: 理由}) を返す。日次上限などの他のエラーはそのまま投げる。"""
        try:
            got = self._call_translate(models, batch, ctx, glossary, notes)
        except GeminiContentError as e:
            if len(batch) < 2:
                return {}, {batch[0]["id"]: str(e)[:160]}
            h = len(batch) // 2
            g1, b1 = self._call_split(models, batch[:h], ctx, glossary, notes, accept)
            g2, b2 = self._call_split(models, batch[h:], ctx, glossary, notes, accept)
            return {**g1, **g2}, {**b1, **b2}
        if accept is not None:
            accept(batch, got)    # 応答が得られた分割ごとにその場で検証・採用・保存する (後半が上限などで例外になっても前半は残る)
        return got, {}

    def _translate_batch(self, batch: list[dict], ctx: dict, glossary: list[dict],
                         on_good=None) -> tuple[dict[str, str], set[str]]:
        """検証つきで翻訳。(訳文, 失敗 unit id の集合) を返す。失敗 unit の訳文は原文。

        on_good(dict) は、検証を通った unit が出るたびに呼ばれる (呼び出し側がキャッシュへ保存する)。
        日次上限などで途中で例外が出ても、それまでに検証を通った unit は保存済みになる。"""
        good: dict[str, str] = {}
        soft_ok: dict[str, str] = {}      # 検証は通ったが表の用語の英語が併記されていない訳 (再送が駄目なら、これを採用する)
        pending = list(batch)
        retries = int(self.g.get("validate_retries", 2))
        for attempt in range(retries + 1):
            notes = None
            if attempt >= 1:
                self.stats["validate_resend"] += len(pending)
                notes = {u["id"]: RETRY_NOTE_VALIDATE.format("; ".join(self._last_problems.get(u["id"], []))) for u in pending}
            try:
                got = self._call_translate([self.translate_model], pending, ctx, glossary, notes)
            except GeminiContentError as e:  # 空応答・不正 JSON (MAX_TOKENS/安全フィルタなど)
                if len(pending) >= 2:
                    # 原因の unit を特定するため、半分ずつに分けて別々に送り直す (他の unit を巻き込まない)。二分探索で 1 unit まで絞れる
                    self.stats["split_batches"] = self.stats.get("split_batches", 0) + 1
                    self.log(f"[info] 応答が使えないため、{len(pending)} unit のバッチを 2 つに分けて再送します ({str(e)[:80]})")
                    h = len(pending) // 2
                    g1, f1 = self._translate_batch(pending[:h], ctx, glossary, on_good=on_good)
                    g2, f2 = self._translate_batch(pending[h:], ctx, glossary, on_good=on_good)
                    good.update(g1)
                    good.update(g2)
                    return good, f1 | f2
                for u in pending:
                    self._last_problems[u["id"]] = [str(e)[:160]]
                if attempt >= retries:
                    self.warnings.append(f"{pending[0]['id']} (p{pending[0]['page']}): 応答が使えませんでした (単独で送っても): {e}")
                continue
            nxt = []
            new_good: dict[str, str] = {}
            for u in pending:
                ja = got.get(u["id"])
                if ja is not None:
                    ja = repair_style_tags(u["text"], ja)
                soft: list[str] = []
                probs = check_translation(u["text"], ja, soft, ocr=getattr(self, "ocr", False)) if ja is not None else ["応答に含まれない"]
                for w in soft:
                    self.warnings.append(f"{u['id']} (p{u['page']}): {w}")
                if ja is not None and not probs and u["role"] != "label":
                    kp = check_keep_en(u["text"], ja, ctx.get("glossary") or []) + check_glossary_applied(u["text"], ja, ctx.get("glossary") or [])
                    if kp and attempt < retries:
                        soft_ok[u["id"]] = ja
                        self._last_problems[u["id"]] = kp
                        nxt.append(u)
                        continue
                    if kp:   # 再送しても併記されなかった: 訳は採用し、警告だけ出す (原文に戻さない)
                        self.warnings.append(f"{u['id']} (p{u['page']}): {'; '.join(kp)}")
                        self.stats["keep_en_missing"] = self.stats.get("keep_en_missing", 0) + 1
                if ja is not None and probs and all("⟦n⟧" in x for x in probs):
                    # ⟦n⟧ だけが崩れた訳は採用する (frame への分割は文字数比のフォールバックが担う)
                    self.warnings.append(f"{u['id']} (p{u['page']}): ⟦n⟧ が崩れたため、frame への分割は文字数比で行います")
                    self.stats["marker_soft"] = self.stats.get("marker_soft", 0) + 1
                    new_good[u["id"]] = ja
                elif probs:
                    nxt.append(u)
                    self._last_problems[u["id"]] = probs
                else:
                    new_good[u["id"]] = ja
            good.update(new_good)
            if new_good and on_good is not None:
                on_good(new_good)
            pending = nxt
            if not pending:
                break
        for u in list(pending):
            if u["id"] in soft_ok:           # 再送でかえって検証 NG になったときは、先に得た (検証は通った) 訳を使う
                good[u["id"]] = soft_ok[u["id"]]
                self.warnings.append(f"{u['id']} (p{u['page']}): 表の用語の英語の併記または用語集の訳語の適用が不足していますが、再送の訳が検証に通らなかったため先の訳を使います")
                if on_good is not None:
                    on_good({u["id"]: soft_ok[u["id"]]})
                pending.remove(u)
        failed = {u["id"] for u in pending}
        for u in pending:
            self.failures[u["id"]] = list(self._last_problems.get(u["id"], ["検証に通らなかった"]))
            self.stats["validate_failed"] += 1
            self.warnings.append(f"{u['id']} (p{u['page']}): 検証に通りませんでした (選択的な見直しを試し、だめなら原文を使います): "
                                 + "; ".join(self._last_problems.get(u["id"], ["応答なし"])))
            good[u["id"]] = u["text"]
        return good, failed

    # -- 見直し ------------------------------------------------------------
    def _refine_batch(self, batch: list[dict], drafts: dict[str, str], ctx: dict, glossary: list[dict],
                      models: list[str] | None = None, selective: bool = False) -> dict[str, str]:
        """見直しを 1 リクエストで行い、採用する {id: 訳文} を返す。selective=True は draft が検証 NG/英語残りの unit 用で、
        採用条件に「英語が残っていない」を加える。"""
        user = json.dumps({"context": ctx, "units": [{"id": u["id"], "role": u["role"], "source": u["text"],
                                                       "draft": drafts[u["id"]]} for u in batch]}, ensure_ascii=False)
        res = self.client.generate_json_chain(models or [self.refine_model], self._system("refine", glossary, ctx), user,
                                              REFINE_SCHEMA)
        out: dict[str, str] = {}
        by_id = {u["id"]: u for u in batch}
        for r in self._as_list(res):
            if not (isinstance(r, dict) and r.get("id") in by_id and isinstance(r.get("text"), str)):
                continue
            u = by_id[r["id"]]
            if r["text"] == drafts[r["id"]] or not r.get("changed", True):
                continue
            if check_translation(u["text"], r["text"]) or (selective and leftover_english(u["text"], r["text"], u["role"])):
                self.stats["refine_rejected"] += 1
                continue
            out[r["id"]] = r["text"]
        return out

    # -- 本体 --------------------------------------------------------------
    @timed("translate")
    def translate(self, units: list[dict], context: dict | None = None, cache: Cache | None = None) -> dict[str, str]:
        """units をページ順に並べ (表・図の注釈用の unit を同じページの本文と同じリクエストにまとめる)、注釈用の unit のうち
        原文が同じもの (図の軸ラベル「Time (sec)」など) は 1 つだけ訳して、残りへ訳を写す。"""
        units = sorted(units, key=lambda u: u["page"])      # 安定ソート: 同じページ内の順序は保つ
        first: dict[str, str] = {}
        dup: dict[str, str] = {}
        for u in units:
            if "cell" in u:
                if u["text"] in first:
                    dup[u["id"]] = first[u["text"]]
                else:
                    first[u["text"]] = u["id"]
        res = self._translate_core([u for u in units if u["id"] not in dup], context, cache)
        for i, rep_id in dup.items():
            res[i] = res[rep_id]
        return res

    def _translate_core(self, units: list[dict], context: dict | None = None, cache: Cache | None = None) -> dict[str, str]:
        context = context or {}
        glossary = context.get("glossary") or []
        self.ocr = bool(context.get("ocr"))     # スキャン + OCR の文書: OCR 誤りの注意を足し、鍵も分ける
        cache = cache if cache is not None else Cache(os.devnull)
        for n in getattr(cache, "notes", []):
            self.warnings.append(n)
        pt, pr = self._digests()
        if self.ocr:
            pt, pr = pt + "|ocr", pr + "|ocr"
        gsub = {u["id"]: self._gsub(glossary, u["text"]) for u in units}
        dkey = {u["id"]: unit_key(u, self.translate_model, f"{pt}|{gsub[u['id']]}") for u in units}
        fkey = {u["id"]: unit_key(u, self.model, f"{pr}|{gsub[u['id']]}") for u in units}
        skey = {u["id"]: unit_key(u, f"{self.translate_model}|sel:{self.refine_model}", f"{pt}|{gsub[u['id']]}") for u in units}
        self.stats["units"] += len(units)
        g = self.g
        pages, mchars = int(g.get("pages_per_batch", 3)), int(g.get("max_chars", 9000))
        final: dict[str, str] = {}
        drafts: dict[str, str] = {}
        failed: set[str] = set()
        skipped: set[str] = set()
        stale: dict[str, str] = {}

        def cached(key: str, u: dict) -> str | None:
            """キャッシュの訳。現行の検証 (数値の符号・比較・余分な数値など) に通らない古い訳は捨てて、その unit だけ再翻訳する
            (再翻訳も通らなかったときの代わりとして stale に取っておく)。"""
            v = cache.get(key)
            if v is None:
                return None
            v = repair_style_tags(u["text"], v)       # 以前の訳に ⟦~⟧ や余分な太字タグがあっても、直せるものは直して使う (再翻訳しない)
            probs = check_translation(u["text"], v, ocr=self.ocr)
            if probs and not all("⟦n⟧" in x for x in probs):
                stale[u["id"]] = v
                cache.d.pop(key, None)
                self.stats["revalidated_dropped"] = self.stats.get("revalidated_dropped", 0) + 1
                return None
            return v

        for u in units:  # キャッシュ済みの段階はスキップ (再実行で続きから再開)
            i = u["id"]
            if self.do_refine and (v := cached(fkey[i], u)) is not None:
                final[i] = v
            elif not self.do_refine and (v := cached(skey[i], u)) is not None:
                final[i] = v
            elif (v := cached(dkey[i], u)) is not None:
                drafts[i] = v
            elif cache.failure(dkey[i]) is not None and not self.retry_failed:
                failed.add(i)            # 前回失敗: 再送しない (--retry-failed で再送)
                skipped.add(i)
                drafts[i] = u["text"]
                self._last_problems[i] = list(cache.failure(dkey[i]).get("problems", []))
        if skipped:
            self.stats["skipped_failed"] = len(skipped)
            self.warnings.append(f"前回の実行で翻訳に失敗した {len(skipped)} unit は再送しません (原文のまま)。再試行するには --retry-failed を付けてください")
        todo = [u for u in units if u["id"] not in final and u["id"] not in drafts]
        n_prev = int(g.get("prev_units", 3))
        order = {u["id"]: i for i, u in enumerate(units)}

        def on_good(new: dict[str, str]) -> None:
            with self._lock:
                for k, v in new.items():
                    cache.put(dkey[k], v)
                cache.save()

        def prevs(i0: int) -> list[dict]:
            """前後関係の手がかり (直前の本文 unit)。表・図の注釈用の unit は除く。"""
            return [x for x in units[max(i0 - 4 * n_prev, 0):i0] if "cell" not in x][-n_prev:] if n_prev else []

        # ① 翻訳 (バッチどうしは独立なので、workers > 1 なら同時に送る。送信の間隔は GeminiClient が rpm で守る)
        def run_batch(batch: list[dict]):
            i0 = order[batch[0]["id"]]
            ctx = self._ctx(context, batch, prevs(i0))
            with self._lock:
                self.stats["batches"] += 1
            return batch, self._translate_batch(batch, ctx, glossary, on_good=on_good)

        batches1 = make_batches(todo, pages, mchars)
        workers = max(1, min(int(g.get("workers", 1)), len(batches1)))
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results1 = list(ex.map(run_batch, batches1))
        else:
            results1 = (run_batch(b) for b in batches1)
        for batch, (got, bad) in results1:
            for u in batch:
                drafts[u["id"]] = got[u["id"]]
                if u["id"] in bad:
                    failed.add(u["id"])
            cache.save()
        # ② 見直し
        if self.do_refine:
            rtodo = [u for u in units if u["id"] not in final and u["id"] not in failed]
            for batch in make_batches(rtodo, pages, int(g.get("refine_max_chars", 6000)),
                                      size=lambda u: len(u["text"]) + len(drafts[u["id"]])):
                i0 = order[batch[0]["id"]]
                ctx = self._ctx(context, batch, prevs(i0))
                self.stats["refine_batches"] += 1
                try:
                    ref = self._refine_batch(batch, drafts, ctx, glossary)
                except (DailyLimitError, RateLimitError) as e:
                    self.stats["refine_skipped"] = True
                    self.warnings.append(f"見直し ({self.refine_model}) の上限に達したため、残りは見直しなし (翻訳結果のまま) で続行します ({e})")
                    break
                except GeminiContentError as e:
                    self.warnings.append(f"見直しの応答が使えませんでした (翻訳結果のまま続行): {e}")
                    continue
                self.stats["refine_changed"] += len(ref)
                for u in batch:
                    final[u["id"]] = ref.get(u["id"], drafts[u["id"]])
                    cache.put(fkey[u["id"]], final[u["id"]])
                cache.save()
        # ③ 選択的な再翻訳: 検証 NG が残った unit と、英語が多く残った unit だけを refine_model (日次上限なら translate_model) で
        #    note つきで訳し直す。検証に通り英語が残っていなければ採用。直らなかった unit は失敗として記録し、次回は再送しない。
        #    (見直し ON のときは、検証 NG の unit は原文のまま出力され、警告に記録される。)
        if not self.do_refine:
            cand = []
            for u in units:
                i = u["id"]
                if i in final or i in skipped:
                    continue
                if (i in failed or leftover_english(u["text"], drafts[i], u["role"])) \
                        and (self.retry_failed or cache.failure(skey[i]) is None):
                    cand.append(u)
            notes = {u["id"]: (RETRY_NOTE_VALIDATE.format("; ".join(self._last_problems.get(u["id"], [])))
                               if u["id"] in failed else RETRY_NOTE_ENGLISH) for u in cand}
            models = [self.refine_model, self.translate_model]
            for batch in make_batches(cand, pages, mchars):
                i0 = order[batch[0]["id"]]
                ctx = self._ctx(context, batch, prevs(i0))
                self.stats["selective_batches"] += 1
                def accept(sub: list[dict], got: dict[str, str]) -> None:
                    for u in sub:
                        ja = got.get(u["id"])
                        if ja is not None:
                            ja = repair_style_tags(u["text"], ja)
                        why = ["応答に含まれない"] if ja is None else \
                            [x for x in check_translation(u["text"], ja, ocr=self.ocr) if "⟦n⟧" not in x]
                        if ja is not None and not why and leftover_english(u["text"], ja, u["role"]):
                            why = ["英語が残っている (日本語に訳されていない)"]
                        if why:
                            self.stats["refine_rejected"] += 1
                            self.failures[u["id"]] = list(why)      # 原文のまま残る原因 (どの検査に落ちたか) を記録する (M15)
                            cache.mark_failed(skey[u["id"]], why)
                            continue
                        final[u["id"]] = ja
                        cache.put(skey[u["id"]], ja)
                        failed.discard(u["id"])
                        self.stats["selective_fixed"] += 1
                    cache.save()

                try:
                    got, broken = self._call_split(models, batch, ctx, glossary, notes, accept)
                except (DailyLimitError, RateLimitError):
                    self.warnings.append("選択的な再翻訳のモデルが上限に達したため、検証 NG/英語残りの unit はそのまま続行します")
                    break
                for u in batch:   # 単独でも応答が使えなかった unit だけを失敗として記録する
                    if u["id"] in broken:
                        cache.mark_failed(skey[u["id"]], [broken[u["id"]]])
                        self.warnings.append(f"{u['id']} (p{u['page']}): 選択的な再翻訳の応答が使えませんでした (そのまま続行): {broken[u['id']]}")
                cache.save()
        # 検証に通らず直らなかった unit は失敗として記録する (次回は再送しない)
        for u in units:
            i = u["id"]
            if i in failed and i not in final and i not in skipped:
                cache.mark_failed(dkey[i], self._last_problems.get(i, ["検証に通らなかった"]))
        cache.save()
        out: dict[str, str] = {}
        for u in units:
            i = u["id"]
            if i in failed and i not in final and i in stale:
                out[i] = stale[i]      # 再翻訳も検証に通らなかった: 以前に採用していた訳を (警告つきで) 使う。原文には戻さない
                self.warnings.append(f"{i} (p{u['page']}): 現行の検証 (数値表現) に通らない以前の訳を、再翻訳も通らなかったためそのまま使います")
            elif i in failed and i not in final:
                out[i] = u["text"]
                self.stats["fallback_original"] += 1
            else:
                d_ = final.get(i) or drafts.get(i) or u["text"]
                if i not in final and d_ != u["text"] and leftover_english(u["text"], d_, u["role"]) and _added_glosses(u["text"], d_):
                    d_ = u["text"]      # M14: 選択的な再翻訳でも日本語にならなかった「英語 + 括弧の併記」の訳は採用せず、原文のまま (併記を足さない)
                    self.stats["fallback_original"] += 1
                out[i] = d_
        return out


def make_translator(name: str, cfg: Config | None = None, **kw) -> Translator:
    if name == "dummy":
        return DummyTranslator()
    if name == "gemini":
        return GeminiTranslator(cfg, **kw)
    raise ValueError(f"unknown translator: {name}")
