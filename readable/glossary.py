"""用語集 (英→日) の作成: タイトル + 要旨 + 見出し + 本文抜粋を Gemini (refine_model) に渡して 1 リクエストで作る。

入力 {"title","abstract","headings","sample"} -> 出力 [{"en","ja","note"}]。結果は work/<name>/glossary.json にキャッシュする。
失敗時は空の用語集で続行する (翻訳は止めない)。日次上限は DailyLimitError をそのまま投げる。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable

from .config import ROOT, Config, atomic_write_text, load_config
from .timing import timed
from .gemini_client import (DailyLimitError, GeminiClient, GeminiContentError, RateLimitError, file_hash, load_prompt_file)

FALLBACK_PROMPT = (
    "あなたは英日翻訳の用語担当である。学術論文のタイトル・要旨・見出し・本文抜粋から、翻訳の一貫性が必要な専門用語・固有名詞・略語を"
    "最大 40 個選び、日本語の定訳を付けて JSON 配列 [{\"en\":\"...\",\"ja\":\"...\",\"note\":\"...\"}] のみで返す。"
    "一般的な語は含めない。略語は en に略語そのものを入れる。note は不要なら空文字。")

SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT",
                                     "properties": {"en": {"type": "STRING"}, "ja": {"type": "STRING"},
                                                    "note": {"type": "STRING"},
                                                    "variants": {"type": "ARRAY", "items": {"type": "STRING"}},
                                                    "keep_en": {"type": "BOOLEAN"}, "abbr": {"type": "STRING"}},
                                     "required": ["en", "ja"]}}


def build_glossary_input(doc: dict, max_sample: int = 3000) -> dict:
    """doc.json からタイトル・要旨・見出し・本文抜粋を集める。"""
    frames = [f for p in doc["pages"] for f in p["frames"]]
    title = " ".join(f["text"] for f in frames if f["role"] == "title")[:300]
    abstract = " ".join(f["text"] for f in frames if f["role"] == "abstract")[:1800]
    headings = [re.sub(r"\s+", " ", f["text"]).strip() for f in frames if f["role"] == "heading"][:50]
    body = [f["text"] for f in frames if f["role"] == "body" and f["translate"] and len(f["text"]) > 200]
    sample = ""
    if body:
        k = min(len(body), 8)
        step = max(len(body) // k, 1)
        per = max(max_sample // k, 200)
        sample = "\n".join(body[i][:per] for i in list(range(0, len(body), step))[:k])[:max_sample]
    out = {"title": title, "abstract": abstract, "headings": headings, "sample": sample}
    tt = doc.get("table_terms") or []
    if tt:   # 表・キャプションの用語: 用語集に必ず入れ、本文では英語を併記する (keep_en)
        out["table_terms"] = [t["en"] + (f" ({t['abbr']})" if t.get("abbr") else "") for t in tt]
    return out


_FOREIGN_RE = re.compile("[ᄀ-ᇿ가-힯Ѐ-ӿ฀-๿؀-ۿ]")
_ACCENT_RE = re.compile("[áéíóúñãõçàèìòùâêîôûäëïöü]", re.I)


def entry_problem(en: str, ja: str) -> str | None:
    """用語集の項目 (en -> ja) の明らかな誤りを返す (問題なければ None): 原文に無い英単語 (幻覚: 「過度の Bethany 日中傾眠」)・
    同じ語句の重複 (「過度の過度の」)・日本語・英語以外の文字や、英語以外のアクセント付きの語 (他言語の混入)。
    項目ごと捨てても本文の翻訳は用語集なしで訳せるので、疑わしいものは採用しない。"""
    en_words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9'’]*", en)}
    en_norm = re.sub(r"[^a-z0-9]", "", en.lower())
    for w in re.findall(r"[A-Za-z][A-Za-z0-9'’]{2,}", ja):
        lw = w.lower()
        if lw in en_words or re.sub(r"[^a-z0-9]", "", lw) in en_norm:
            continue
        return f"原文に無い英単語「{w}」"
    if re.search(r"([぀-ヿ㐀-鿿]{2,6})\1", ja):
        return "同じ語句の重複"
    if _FOREIGN_RE.search(ja) or (_ACCENT_RE.search(ja) and not _ACCENT_RE.search(en)):
        return "日本語・英語以外の言語の混入"
    return None


def _clean(entries) -> list[dict]:
    out, seen = [], set()
    if not isinstance(entries, list):
        return out
    for e in entries:
        if not isinstance(e, dict):
            continue
        en, ja = str(e.get("en", "")).strip(), str(e.get("ja", "")).strip()
        if not en or not ja or en.lower() in seen:
            continue
        if entry_problem(en, ja):
            continue           # 幻覚・重複・他言語の混入のある項目は採用しない (M14)
        seen.add(en.lower())
        item = {"en": en, "ja": ja, "note": str(e.get("note", "") or "").strip()}
        if e.get("keep_en") is True:
            item["keep_en"] = True
            if str(e.get("abbr") or "").strip():
                item["abbr"] = str(e["abbr"]).strip()
        vs = e.get("variants")
        if isinstance(vs, list):
            item["variants"] = [str(v).strip() for v in vs if str(v).strip() and str(v).strip() != ja]
        out.append(item)
    return out


def load_override(path: str | Path | None, log: Callable[[str], None] | None = None) -> list[dict] | None:
    """glossary_override.json ([{"en","ja"[,"note"]}] または {"en": "ja"})。あれば用語集をこれに完全に置き換える (API は呼ばない)。"""
    if not path or not Path(path).exists():
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        if log:
            log(f"[警告] {path} を読めません ({e})。無視します")
        return None
    if isinstance(data, dict):
        data = [{"en": k, "ja": v} for k, v in data.items()]
    return _clean(data)


@timed("glossary")
def build_glossary(doc: dict, client: GeminiClient, cfg: Config | None, cache_path: str | Path,
                   log: Callable[[str], None] | None = None, override_path: str | Path | None = None) -> list[dict]:
    cfg = cfg or load_config()
    log = log or (lambda s: None)
    g = cfg.section("gemini")
    ov = load_override(override_path, log)
    if ov is not None:
        log(f"[info] 用語集は {override_path} で上書きされています ({len(ov)} 語)")
        return ov
    if not g.get("glossary", True):
        return []
    inp = build_glossary_input(doc)
    if not (inp["title"] or inp["abstract"] or inp["sample"]):
        return []
    model = g.get("refine_model", "gemini-3.5-flash")
    ppath = g.get("glossary_prompt")
    system = load_prompt_file(ppath, FALLBACK_PROMPT)
    user = json.dumps(inp, ensure_ascii=False)
    key = hashlib.sha256("\0".join((user, model, system)).encode("utf-8")).hexdigest()
    cp = Path(cache_path)
    if cp.exists():
        try:
            cj = json.loads(cp.read_text(encoding="utf-8"))
            if not isinstance(cj, dict) or ("glossary" in cj and not isinstance(cj["glossary"], list)):
                raise ValueError("用語集キャッシュの型が不正です")
            if cj.get("key") == key:
                return _clean(cj.get("glossary"))
        except (OSError, ValueError, AttributeError, TypeError) as e:   # 壊れた/型の違うキャッシュは退避して再生成する
            try:
                cp.replace(cp.with_name(cp.name + ".bak"))
            except OSError:
                pass
            log(f"[警告] 用語集キャッシュ {cp.name} を読めないため退避して再生成します ({e})")
    try:
        res = client.generate_json_chain([model, g.get("translate_model", "gemini-3.5-flash-lite")], system, user, SCHEMA)
    except DailyLimitError as e:
        log(f"[警告] 用語集の作成をスキップしました (日次上限: {e.model})。翻訳は用語集なしで続行します")
        return []
    except (GeminiContentError, RateLimitError) as e:   # 設定・権限の誤り/回復しない 5xx は呼び出し側 (cli) で中断する
        log(f"[警告] 用語集の作成に失敗しました。用語集なしで続行します: {e}")
        return []
    gl = _clean(res)
    atomic_write_text(cp, json.dumps({"key": key, "model": model, "prompt": file_hash(ppath), "glossary": gl},
                                     ensure_ascii=False, indent=1))
    return gl


def filter_glossary(glossary: list[dict], text: str) -> list[dict]:
    """バッチ/unit の原文に出現する用語だけに絞る (大文字小文字無視)。短い語 (4 文字以下の略語など) は単語境界で判定し
    (AI が maintain に当たらない。複数形の s/es は許す)、それ以外は部分文字列で判定する (複数形・語形変化を拾う)。"""
    low = text.lower()
    out = []
    for g in glossary:
        en = g["en"].lower()
        if len(en) <= 4 or g.get("keep_en"):   # 短い語・表の用語 (Active など一般語と重なる) は単語境界で判定する
            if re.search(r"(?<![a-z0-9])" + re.escape(en) + r"(?:s|es)?(?![a-z0-9])", low):
                out.append(g)
        elif en in low:
            out.append(g)
    return out


# --------------------------------------------------------------------------
# 表記ゆれの正規化: 用語集の訳語 (ja) と異なる表記 (ひらがな⇔カタカナ、長音の有無、variants に書かれた別表記) を用語集の表記へ寄せる。
# 対象は用語集に載っている語だけ (過剰な正規化を避ける)。タグ・{vN}・⟦n⟧・実体参照の内側は触らない。
# --------------------------------------------------------------------------

_KATA_RE = re.compile("[ァ-ヶ]")
_HIRA_RE = re.compile("[ぁ-ゖ]")
_KANJI_RE = re.compile("[㐀-鿿々〆]")
_LATIN_RE = re.compile(r"[A-Za-z0-9]")
_SKIP_RE = re.compile(r"(<[^>]+>|\{v\d+\}|⟦\d+⟧|&#?\w+;)")


def _char_class(ch: str) -> str:
    """kata (カタカナ・長音・中黒) / kanji / hira / other"""
    if "ァ" <= ch <= "ヺ" or ch in "ー・ｰ":
        return "kata"
    if _KANJI_RE.match(ch):
        return "kanji"
    if "ぁ" <= ch <= "ゖ":
        return "hira"
    return "other"


def to_hiragana(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def to_katakana(s: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


def term_variants(ja: str, extra: list[str] | None = None) -> list[str]:
    """用語集の訳語 ja に対して、揺れとして現れうる別表記を返す (ja 自身は含まない)。
    自動: カタカナ→ひらがな (カタカナ語)、ひらがな→カタカナ (かな語のみ)、カタカナ語末尾の長音の脱落 (ユーザー→ユーザ)。
    extra (用語集の variants) はそのまま加える。短すぎる語 (かな 3 字未満) は誤爆を避けて除く。"""
    out: list[str] = []
    if not _LATIN_RE.search(ja):
        if _KATA_RE.search(ja):
            h = to_hiragana(ja)
            if h != ja and (_KANJI_RE.search(h) or len(h) >= 3):
                out.append(h)
        if _HIRA_RE.search(ja) and not _KANJI_RE.search(ja):
            k = to_katakana(ja)
            if k != ja and len(k) >= 3:
                out.append(k)
        if re.fullmatch("[ァ-ヶー・]+", ja) and ja.endswith("ー") and len(ja) >= 4:
            out.append(ja[:-1])
    for v in extra or []:
        v = v.strip()
        if len(v) >= 2 and v != ja and v not in out:
            out.append(v)
    return out


def build_variant_map(glossary: list[dict]) -> dict[str, str]:
    """{別表記: 用語集の表記}。別表記が他の用語の正規表記と同じ、または複数の用語に割り当たるものは除く。"""
    canon = {g["ja"] for g in glossary if g.get("ja")}
    m: dict[str, str] = {}
    bad: set[str] = set()
    for g in glossary:
        ja = g.get("ja", "")
        if not ja or _LATIN_RE.search(ja):
            continue
        for v in term_variants(ja, g.get("variants")):
            if v in canon or _LATIN_RE.search(v):   # 英字を含む別表記 (EEG, TMR など。略語は原語のまま残す) は対象にしない
                continue
            if ja in v or (v in ja and v in (g.get("variants") or [])):   # 正規表記を含むより長い語 (再生 に対する 記憶再生) ・正規表記の一部 (コツメカワウソ に対する カワウソ) は統一しない
                continue
            if v in m and m[v] != ja:
                bad.add(v)
            m[v] = ja
    for v in bad:
        m.pop(v, None)
    return m


def normalize_terms(text: str, glossary: list[dict], counter: dict | None = None) -> str:
    """text (html 断片) 中の用語集にある語の別表記を、用語集の表記へ置き換える。
    正規表記自身も選択肢に含めて最長一致にするので、「ビジタ」が「ビジター」の一部に誤って当たることはない。
    counter があれば {別表記: 置換回数} を加算する。"""
    vm = build_variant_map(glossary)
    if not vm:
        return text
    alts = set(vm) | {g["ja"] for g in glossary if g.get("ja") and not _LATIN_RE.search(g["ja"])}
    pat = re.compile("|".join(re.escape(a) for a in sorted(alts, key=len, reverse=True)))

    def make_sub(seg: str):
        def sub(m: re.Match) -> str:
            v = m.group(0)
            if v not in vm:
                return v
            # 別表記の前後が同じ文字種 (カタカナ・長音 / 漢字) で続くときは、より長い語の一部なので置換しない
            # (ユーザ in ユーザビリティ, カワウソ in ニホンカワウソ, テスト in テストステロン, 予備研究 in 本予備研究)。
            # ひらがなは助詞が隣に来るのが普通なので境界を要求しない。
            a, b = m.start(), m.end()
            for edge, nb in ((v[0], seg[a - 1] if a > 0 else ""), (v[-1], seg[b] if b < len(seg) else "")):
                c = _char_class(edge)
                if nb and c in ("kata", "kanji") and _char_class(nb) == c:
                    return v
            if counter is not None:
                counter[v] = counter.get(v, 0) + 1
            return vm[v]
        return sub

    parts = _SKIP_RE.split(text)
    return "".join(p if i % 2 else pat.sub(make_sub(p), p) for i, p in enumerate(parts))


def normalize_all(ja_by_unit: dict[str, str], glossary: list[dict]) -> tuple[dict[str, str], dict[str, int]]:
    counter: dict[str, int] = {}
    return {k: normalize_terms(v, glossary, counter) for k, v in ja_by_unit.items()}, counter


# --------------------------------------------------------------------------
# 固定訳 (glossary_fixed.toml): どの論文でも優先される訳語。論文ごとの自動用語集より優先する。
# --------------------------------------------------------------------------

def load_fixed(path: str | Path | None = None, log: Callable[[str], None] | None = None) -> list[dict]:
    """glossary_fixed.toml ([terms] 英語 = 日本語, [variants] 日本語 = [別表記...]) を用語集の形式 [{"en","ja","variants"}] で返す。
    ファイルが無い/壊れているときは空 (警告のみ)。"""
    import tomllib
    p = Path(path) if path else ROOT / "glossary_fixed.toml"
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return []
    try:
        with open(p, "rb") as f:
            d = tomllib.load(f)
    except (OSError, ValueError) as e:
        if log:
            log(f"[警告] {p} を読めません ({e})。固定訳なしで続行します")
        return []
    terms = d.get("terms", {})
    variants = d.get("variants", {})
    out = []
    for en, ja in terms.items():
        if isinstance(en, str) and isinstance(ja, str) and en.strip() and ja.strip():
            vs = variants.get(ja)
            item = {"en": en.strip(), "ja": ja.strip(), "note": "固定訳"}
            if isinstance(vs, list):
                item["variants"] = [str(v) for v in vs if str(v).strip() and str(v) != ja]
            out.append(item)
    return out


def merge_fixed(glossary: list[dict], fixed: list[dict], text: str) -> tuple[list[dict], list[str]]:
    """論文の用語集に固定訳を重ねる。固定訳が優先され (同じ en の項目は固定訳で置き換え)、論文の本文 text に出てくる固定訳は
    (自動用語集に無くても) 追加する。変えた/足した en の一覧も返す。"""
    present = {g["en"].lower() for g in filter_glossary(fixed, text)}
    fx = {g["en"].lower(): g for g in fixed}
    out, changed, seen = [], [], set()
    for g in glossary:
        k = g["en"].lower()
        if k in fx:
            if fx[k]["ja"] != g["ja"]:
                changed.append(g["en"])
            item = dict(fx[k])
            for key in ("keep_en", "abbr"):   # 表の用語 (英語を併記する) の指定は固定訳に置き換えても引き継ぐ
                if g.get(key):
                    item[key] = g[key]
            out.append(item)
            seen.add(k)
        else:
            out.append(g)
    for g in fixed:            # 固定訳ファイルの順 (集合の順序に依存しない = キャッシュキーが安定する)
        k = g["en"].lower()
        if k in present and k not in seen:
            out.append(g)
            changed.append(g["en"])
            seen.add(k)
    return out, changed
