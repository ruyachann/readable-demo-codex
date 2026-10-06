"""① extract: PDF -> 段落 frame (doc.json)。

PLAN.md §11 の 1,3,4,5,6 + M1.5 (docs/REVIEW_M1.md の指摘修正) に対応:
  行(span) -> 行(row) -> 段落 frame。上付き/下付き/斜体/太字/リンクを <sup>/<sub>/<i>/<b>/<a href> で保持。
  charmap (font 名ごと)、字間空白の正規化、語彙に基づくハイフン解除 (タグをまたぐ行末ハイフンも解除)、
  ヒューリスティックの role / translate / joins。座標は常に「見た目の向き」(回転適用後) で保持する。
  メール/URL/DOI/助成番号/数式片は build_units 時に {vN} で保護する (protect_html)。
"""
from __future__ import annotations

import collections
import html as htmllib
import json
import re
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from pathlib import Path
from typing import Any

import fitz

from .config import DEFAULTS, Config, load_config
from .timing import timed

SCHEMA_VERSION = 2


class PDFOpenError(Exception):
    """PDF として開けない (壊れている/PDF ではない)。"""


class EncryptedPDFError(PDFOpenError):
    """ユーザーパスワード付き PDF。"""


def _p(ex: dict, key: str) -> Any:
    """extract 設定の取得 (既定値は config.DEFAULTS に一元化)。"""
    return ex.get(key, DEFAULTS["extract"][key])


# --------------------------------------------------------------------------
# charmap (font ごと)
# --------------------------------------------------------------------------

#: JSR (Wiley/Acrobat) の記号フォント AdvP* の制御文字。
DEFAULT_CHARMAP: dict[str, str] = {
    "\x01": "−",  # (\x014.6%) = (−4.6%)
    "\x03": "°",  # 45\x03 ± 52\x03 = 45° ± 52°
    "\x04": "~",  # Response \x04 1þstimulus type = Response ~ 1 + stimulus type
    "\x05": "×",  # session \x05 stimulus type interaction
}
#: font 名 (先頭一致, 大文字小文字無視) -> 表。"*" は全 font。
DEFAULT_FONT_CHARMAP: dict[str, dict[str, str]] = {"AdvP": DEFAULT_CHARMAP,
                                                     "AdvPSMPi": {"d": "•"},
                                                     # TeX の大きな演算子の記号フォント (CMEX): 総和 ∑ が「P」、総乗 ∏ が「Q」、積分 ∫ が「R」として抽出される
                                                     "CMEX": {"P": "∑", "Q": "∏", "R": "∫", "X": "∑", "Y": "∏", "Z": "∫"}}   # Elsevier (Cell Press) の記号フォント: Highlights の箇条書き記号が「d」で出る
#: 記号フォント(下記 SYMBOL_FONT_RE)の span にだけ適用する表
#: ð 1 j subject Þ (lme4 式 "(1|subject)") のように þ=+, ð=(, Þ=), j=|
SYMBOL_FONT_CHARMAP: dict[str, str] = {"þ": "+", "ð": "(", "Þ": ")"}
SYMBOL_FONT_WHOLE_SPAN: dict[str, str] = {"j": "|"}
SYMBOL_FONT_RE = re.compile(r"^(AdvP|Symbol|CMSY|CMMI|CMEX|MathematicalPi|Wingdings|MT-?Extra)", re.I)
#: 数式フォント (CMR/CMBX/CMTI は LaTeX 本文なので含めない。AdvP は JSR の本文記号用なので含めない)
MATH_FONT_RE = re.compile(r"^(CMSY|CMMI|CMEX|MSAM|MSBM|MathematicalPi|Cambria.?Math|STIX.?Math|LMMath|Symbol)|.*(?:Mth|Math|MTSY|MTMI|MTEX|MTExtra|Euclid)", re.I)
TEXMATH_RE = re.compile(r"^(CMSY|CMMI|CMEX|MSAM|MSBM|MathematicalPi|Cambria.?Math|STIX.?Math|LMMath)", re.I)
PUA_RANGE = (0xE000, 0xF8FF)

BOLD_RE = re.compile(r"(bold|black|heavy|demi|semibold|\.B$|-B$|,B$)", re.I)
ITALIC_RE = re.compile(r"(italic|oblique|slant|[-.,]ital\w*$|\.I$|-It$|,I$)", re.I)
_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")


def _fname(font: str) -> str:
    return _SUBSET_PREFIX.sub("", font or "")


def resolve_charmap(font: str, charmap: dict | None) -> dict[str, str]:
    """font に適用する {char: str} を返す。charmap は (a) None=既定, (b) 旧形式 {char: str} (全 font),
    (c) 新形式 {font_prefix: {char: str}} のいずれか。"""
    fn = _fname(font)
    if charmap is None:
        return DEFAULT_CHARMAP if (not fn or fn.lower().startswith("advp")) else {}
    if charmap and all(isinstance(v, dict) for v in charmap.values()):
        out: dict[str, str] = {}
        for key in sorted(charmap, key=lambda k: (k != "*", len(k))):
            if key == "*" or (fn and fn.lower().startswith(key.lower())):
                out.update(charmap[key])
        return out
    return charmap  # type: ignore[return-value]


def merge_charmap(base: dict | None, extra: dict | None) -> dict[str, dict[str, str]]:
    """base (新形式) に extra (旧/新形式) を上書きマージして新形式で返す。"""
    out = {k: dict(v) for k, v in (base if base is not None else DEFAULT_FONT_CHARMAP).items()}
    if not extra:
        return out
    if all(isinstance(v, dict) for v in extra.values()):
        for k, v in extra.items():
            out.setdefault(k, {}).update(v)
    else:
        out.setdefault("*", {}).update(extra)
    return out


def apply_charmap(text: str, font: str = "", charmap: dict | None = None,
                  unmapped: dict[str, int] | None = None) -> str:
    """制御文字/記号フォント文字を Unicode に置換する。

    未知の制御文字は除去、PUA (U+E000-F8FF) は残して、どちらも unmapped に記録する。"""
    cm = resolve_charmap(font, charmap)
    is_sym = bool(SYMBOL_FONT_RE.match(_fname(font)))
    out = []
    if is_sym and text.strip() in SYMBOL_FONT_WHOLE_SPAN and text.strip() not in cm:
        return text.replace(text.strip(), SYMBOL_FONT_WHOLE_SPAN[text.strip()])
    for ch in text:
        if ch in cm:
            out.append(cm[ch])
        elif is_sym and ch in SYMBOL_FONT_CHARMAP:
            out.append(SYMBOL_FONT_CHARMAP[ch])
        elif ord(ch) < 32 and ch not in "\t\n":
            if unmapped is not None:
                unmapped[ch] = unmapped.get(ch, 0) + 1
        elif PUA_RANGE[0] <= ord(ch) <= PUA_RANGE[1]:
            if unmapped is not None:
                unmapped[ch] = unmapped.get(ch, 0) + 1
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def style_of(font: str, flags: int) -> tuple[bool, bool, bool]:
    """(bold, italic, serif)"""
    bold = bool(flags & 16) or bool(BOLD_RE.search(font or ""))
    italic = bool(flags & 2) or bool(ITALIC_RE.search(font or ""))
    serif = bool(flags & 4)
    return bold, italic, serif


# --------------------------------------------------------------------------
# 上付き/下付き判定
# --------------------------------------------------------------------------

def classify_script(size: float, flags: int, origin_y: float, dom_size: float, base_y: float,
                    sub_ratio: float = 0.85) -> str | None:
    """行内の相対位置で 'sup' / 'sub' / None を返す。

    上付き = flags&1。下付き = サイズ<ratio×行の主サイズ かつ 基線より下(origin.y が大きい)。
    小型大文字 (サイズ小・基線同一) や † は誤検出しない。
    """
    if flags & 1:
        return "sup"
    if size < sub_ratio * dom_size:
        if origin_y > base_y + 0.1 * size:
            return "sub"
        if origin_y < base_y - 0.2 * size:
            return "sup"
    return None


INLINE_TAGS = "sup|sub|i|b|x|a"
_TAG_STRIP_RE = re.compile(r"</?(?:%s)(?:\s[^>]*)?>" % INLINE_TAGS)


def esc(t: str) -> str:
    return htmllib.escape(t, quote=False)


def esc_attr(t: str) -> str:
    return htmllib.escape(t, quote=True)


def strip_tags(h: str) -> str:
    return htmllib.unescape(_TAG_STRIP_RE.sub("", h))


# --------------------------------------------------------------------------
# 字間空白の正規化
# --------------------------------------------------------------------------

LABELS = {
    "RESEARCHARTICLE": "RESEARCH ARTICLE", "KEYWORDS": "KEYWORDS", "ORIGINALARTICLE": "ORIGINAL ARTICLE",
    "REVIEWARTICLE": "REVIEW ARTICLE", "SHORTCOMMUNICATION": "SHORT COMMUNICATION", "ABSTRACT": "ABSTRACT",
    "SUMMARY": "SUMMARY", "REFERENCES": "REFERENCES", "INTRODUCTION": "INTRODUCTION",
}


def despace_letters(s: str) -> str:
    """'R E S E A R C H A R T I C L E' -> 'RESEARCH ARTICLE'。字間が空白の見出しを復元する。"""
    t = s.strip()
    if re.fullmatch(r"(?:[A-Za-z]\s+){3,}[A-Za-z]", t):
        joined = re.sub(r"\s+", "", t)
        return LABELS.get(joined.upper(), joined)
    return s


# --------------------------------------------------------------------------
# ハイフン解除 (文書内の語彙)。タグをまたぐ行末ハイフンも扱う
# --------------------------------------------------------------------------

WORD_RE = re.compile(r"[A-Za-zÀ-ÿ]+(?:-[A-Za-zÀ-ÿ]+)*")
HYPHENS = "-‐­"


@dataclass
class Vocab:
    words: set[str] = field(default_factory=set)
    compounds: set[str] = field(default_factory=set)

    @classmethod
    def from_texts(cls, texts: list[str]) -> "Vocab":
        v = cls()
        for t in texts:
            toks = WORD_RE.findall(t)
            # 行末ハイフンで切れた断片は語彙に入れない
            if toks and t.rstrip()[-1:] in HYPHENS:
                toks = toks[:-1]
            for tok in toks:
                low = tok.lower()
                if "-" in low:
                    v.compounds.add(low)
                    for p in low.split("-"):
                        if p:
                            v.words.add(p)
                else:
                    v.words.add(low)
        return v

    def to_json(self) -> dict:
        return {"words": sorted(self.words), "compounds": sorted(self.compounds)}

    @classmethod
    def from_json(cls, d: dict | None) -> "Vocab":
        d = d or {}
        return cls(set(d.get("words", [])), set(d.get("compounds", [])))


def hyphen_decision(prev_frag: str, next_word: str, vocab: Vocab) -> str:
    """'join' (ハイフン除去して連結) か 'keep' (ハイフンを残す) を返す。"""
    if not prev_frag or not next_word:
        return "keep"
    joined = (prev_frag + next_word).lower()
    if joined in vocab.words:
        return "join"
    if f"{prev_frag}-{next_word}".lower() in vocab.compounds:
        return "keep"
    if next_word[0].islower() and prev_frag[-1].isalpha():
        return "join"
    return "keep"


_CLOSE = r"(?:</(?:i|b|x|a|sup|sub)>)*"
_OPEN = r"(?:<(?:i|b|x|sup|sub)>)*"
_TAIL_RE = re.compile(r"([A-Za-zÀ-ÿ]+)[-‐­](" + _CLOSE + r")$")
_HEAD_RE = re.compile(r"^(" + _OPEN + r")([A-Za-zÀ-ÿ]+)(" + _CLOSE + r")")
_TAGS_IN = re.compile(r"<(/?)(\w+)>")


def _tags(s: str) -> list[str]:
    return [m.group(2) for m in _TAGS_IN.finditer(s)]


def _cancel(closers: list[str], openers: list[str]) -> tuple[list[str], list[str]]:
    """末尾の閉じタグと先頭の開きタグを (i/b/x/sup/sub が一致する限り) 相殺する。"""
    c, o = list(closers), list(openers)
    while c and o and c[-1] == o[0]:
        c.pop()
        o.pop(0)
    return c, o


def _mk(tags: list[str], close: bool) -> str:
    return "".join(f"</{t}>" if close else f"<{t}>" for t in tags)


def join_pair(a: str, b: str, vocab: Vocab) -> tuple[str, str] | None:
    """行 a (html) の末尾が語+ハイフンのとき、b の先頭語を a に寄せて (a', b') を返す。該当しなければ None。

    a' は語を完結させた html (ハイフンは語彙判定で除去/保持)、b' は残り。b 側の単語の書式 (<i> など) は
    a 側の書式に吸収する (a の語が <i>pseudo-</i> なら <i>pseudoword</i>)。"""
    mt, mh = _TAIL_RE.search(a), _HEAD_RE.match(b)
    if not (mt and mh):
        return None
    w1, w2 = mt.group(1), mh.group(2)
    hy = "" if hyphen_decision(w1, w2, vocab) == "join" else "-"
    openers, post = _tags(mh.group(1)), _tags(mh.group(3))
    na = a[: mt.start(1)] + w1 + hy + w2 + mt.group(2)
    rest = b[mh.end():]
    nb = (_mk(openers, False) + rest) if (openers and not post) else rest
    return na, nb.lstrip()


def join_lines(htmls: list[str], vocab: Vocab) -> str:
    """行 html を連結する。行末ハイフンは語彙で判定し、それ以外は空白1個で連結。タグをまたぐハイフンも解除する。"""
    out = ""
    for h in htmls:
        h = h.strip()
        if not h:
            continue
        if not out:
            out = h
            continue
        mt, mh = _TAIL_RE.search(out), _HEAD_RE.match(h)
        if mt and mh:
            w1, w2 = mt.group(1), mh.group(2)
            dec = hyphen_decision(w1, w2, vocab)
            closers = _tags(mt.group(2))
            openers = _tags(mh.group(1))
            c2, o2 = _cancel(closers, openers)
            hy = "" if dec == "join" else "-"
            # out = prefix + w1 + '-' + closers ; h = openers + w2 + ...
            tail_len = len(mt.group(0)) - len(mt.group(1))
            out = out[: len(out) - tail_len] + hy + _mk(c2, True) + _mk(o2, False) + h[len(mh.group(1)):]
        else:
            out = out + " " + h
    return re.sub(r"[ \t ]+", " ", out).strip()


# --------------------------------------------------------------------------
# インライン保護 ({vN} プレースホルダ)
# --------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL_RE = re.compile(r"(?:https?://|ftp://|www\.)[^\s<>\"']*[^\s<>\"'.,;:)\]]")
DOI_INLINE_RE = re.compile(r"(?:\bdoi:\s*)?\b10\.\d{4,9}/[^\s<>\"']*[^\s<>\"'.,;:)\]]", re.I)
_ID_TOKEN = r"((?=[^\s]*\d)[A-Za-z0-9][\w./-]{2,}[A-Za-z0-9])"
GRANT_CTX_RE = re.compile(
    r"\b(?:grants?|awards?|projects?|contracts?|fellowships?)(?:\s*/\s*(?:grants?|awards?))?"
    r"\s*(?:nos?\.?|numbers?|#|id)\s*[:#]?\s*" + _ID_TOKEN, re.I)
GRANT_BARE_RE = re.compile(r"\b[A-Z][A-Z0-9]*[-/]?\d{4,}[A-Za-z0-9/_-]*\b")
# 数値の羅列 (5e-5, 3e-5, 2e-5 / 16, 32, 64)・k 付きの数 (32k)・比較演算子つきの数 (≥30, &gt;0.05) は {vN} で保護して、訳さず・換算せず原文のまま残す
NUM_LIST_RE = re.compile(r"(?<![\w{.])\d+(?:\.\d+)?(?:[eE]-?\d+)?[kK]?(?:\s*[,/、]\s*\d+(?:\.\d+)?(?:[eE]-?\d+)?[kK]?){2,}(?![\w}])")
NUM_K_RE = re.compile(r"(?<![\w{.])\d+(?:\.\d+)?[kK](?![A-Za-z\d}])")
NUM_CMP_RE = re.compile(r"(?:\u2265|\u2264|\u2a7e|\u2a7d|&gt;=|&lt;=|&gt;|&lt;|>=|<=)\s*\d+(?:\.\d+)?(?:[eE]-?\d+)?%?")
_PROTECT_RES = (URL_RE, EMAIL_RE, DOI_INLINE_RE, GRANT_CTX_RE, GRANT_BARE_RE, NUM_LIST_RE, NUM_K_RE, NUM_CMP_RE)
VAR_RE = re.compile(r"\{v(\d+)\}")
_ANCHOR_RE = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.S)
_X_RE = re.compile(r"<x>(.*?)</x>", re.S)
_SPLIT_TAG = re.compile(r"(<[^>]+>)")


def _is_ident_text(t: str) -> bool:
    t = t.strip()
    return bool(EMAIL_RE.fullmatch(t) or URL_RE.fullmatch(t) or DOI_INLINE_RE.fullmatch(t)
                or re.fullmatch(r"(?:https?://)?[\w.-]+\.[a-z]{2,}(?:/\S*)?", t, re.I))


def protect_html(h: str, start: int = 1) -> tuple[str, dict[str, str]]:
    """メール/URL/DOI/助成番号/数式片 (<x>) とそのリンクを {vN} に置換する。(保護後 html, {vN: 元 html 断片}) を返す。"""
    vars_: dict[str, str] = {}
    n = [start - 1]

    def put(frag: str) -> str:
        n[0] += 1
        vars_[f"v{n[0]}"] = frag
        return "{v%d}" % n[0]

    h = _X_RE.sub(lambda m: put(m.group(1)), h)

    def anchor(m: re.Match) -> str:
        inner = strip_tags(m.group(2))
        if _is_ident_text(inner) or m.group(1).startswith("mailto:"):
            return put(m.group(0))
        return m.group(0)

    h = _ANCHOR_RE.sub(anchor, h)
    out = []
    in_a = 0
    for tok in _SPLIT_TAG.split(h):
        if not tok:
            continue
        if tok.startswith("<"):
            if tok.startswith("<a "):
                in_a += 1
            elif tok == "</a>":
                in_a = max(in_a - 1, 0)
            out.append(tok)
            continue
        out.append(_protect_text(tok, put))
    return "".join(out), vars_


def _protect_text(t: str, put) -> str:
    spans: list[tuple[int, int]] = []
    for rx in _PROTECT_RES:
        for m in rx.finditer(t):
            g = 1 if rx is GRANT_CTX_RE else 0
            s, e = m.span(g)
            if "{v" in t[s:e]:
                continue
            if any(s < b and a < e for a, b in spans):
                continue
            spans.append((s, e))
    if not spans:
        return t
    spans.sort()
    res, pos = [], 0
    for s, e in spans:
        res.append(t[pos:s])
        res.append(put(t[s:e]))
        pos = e
    res.append(t[pos:])
    return "".join(res)


def restore_vars(h: str, vars_: dict[str, str]) -> str:
    return VAR_RE.sub(lambda m: vars_.get("v" + m.group(1), m.group(0)), h)


def plain_html(h: str) -> str:
    """<x> (数式片の目印) を取り除く。"""
    return re.sub(r"</?x>", "", h)


# --------------------------------------------------------------------------
# Row / Frame
# --------------------------------------------------------------------------

@dataclass
class Row:
    page: int
    bbox: list[float]
    base: float
    size: float
    html: str
    text: str
    bold: bool
    italic: bool
    serif: bool
    color: int
    font: str
    stream: int
    first_script: bool = False
    math_ratio: float = 0.0
    in_image: bool = False
    in_table: bool = False
    nchars: int = 0

    @property
    def x0(self): return self.bbox[0]
    @property
    def x1(self): return self.bbox[2]
    @property
    def y0(self): return self.bbox[1]
    @property
    def y1(self): return self.bbox[3]


def _dominant(items: list[tuple[Any, int]]):
    c: collections.Counter = collections.Counter()
    for k, w in items:
        c[k] += w
    return c.most_common(1)[0][0] if c else None


@dataclass
class _Part:
    k: str          # 'n' | 'sup' | 'sub'
    it: bool
    bd: bool
    uri: str
    m: bool         # 数式フォントの run
    t: str

    def style(self):
        return (self.k, self.it, self.bd, self.uri, self.m)


def _emit_part(p: _Part, inner_only: bool = False) -> str:
    t = p.t
    if not t.strip():
        return esc(t)
    lead, trail = t[: len(t) - len(t.lstrip())], t[len(t.rstrip()):]
    core = esc(t.strip())
    if p.k == "sup":
        return f"{lead}<sup>{core}</sup>{trail}"
    if p.k == "sub":
        return f"{lead}<sub>{core}</sub>{trail}"
    if p.it:
        core = f"<i>{core}</i>"
    if inner_only:
        return lead + core + trail
    if p.bd:
        core = f"<b>{core}</b>"
    if p.uri:
        core = f'<a href="{esc_attr(p.uri)}">{core}</a>'
    return lead + core + trail


def build_row(spans: list[dict], page: int, stream: int, charmap: dict | None,
              unmapped: dict[str, dict[str, int]], sub_ratio: float = 0.85,
              uri_links: list[tuple[list[float], str]] | None = None) -> Row | None:
    """span 群(同一視覚行)から Row を作る。unmapped は {font: {char: count}}。"""
    spans = sorted(spans, key=lambda s: s["bbox"][0])
    items = []
    for s in spans:
        um: dict[str, int] = {}
        txt = apply_charmap(s["text"], s["font"], charmap, um)
        for ch, c in um.items():
            d = unmapped.setdefault(_fname(s["font"]), {})
            d[ch] = d.get(ch, 0) + c
        if txt == "":
            continue
        items.append((s, txt))
    if not items:
        return None
    weighted = [(round(s["size"], 1), max(len(t.strip()), 0)) for s, t in items if not (s["flags"] & 1)]
    if not any(w for _, w in weighted):
        weighted = [(round(s["size"], 1), len(t.strip())) for s, t in items]
    dom = _dominant(weighted) or items[0][0]["size"]
    near = [s["origin"][1] for s, t in items if abs(s["size"] - dom) < 0.15 * dom + 0.2 and t.strip()]
    base = sorted(near)[len(near) // 2] if near else items[0][0]["origin"][1]

    parts: list[_Part] = []
    prev = None
    bold_w, ital_w, serif_w, math_w = 0, 0, 0, 0
    colors, fonts = [], []
    total = 0
    first_script = False
    x0 = y0 = 1e9
    x1 = y1 = -1e9
    for idx, (s, txt) in enumerate(items):
        b = s["bbox"]
        x0, y0, x1, y1 = min(x0, b[0]), min(y0, b[1]), max(x1, b[2]), max(y1, b[3])
        kind = classify_script(s["size"], s["flags"], s["origin"][1], dom, base, sub_ratio) if txt.strip() else None
        if kind and txt.strip() in ("°",):
            kind = None
        if idx == 0 and kind == "sup" and txt.strip():
            first_script = True
        bold, italic, serif = style_of(s["font"], s["flags"])
        w = len(txt.strip())
        total += w
        if kind is None:
            bold_w += w * bold
            ital_w += w * italic
        serif_w += w * serif
        math_w += w * bool(MATH_FONT_RE.match(_fname(s["font"])))
        colors.append((s["color"], w))
        fonts.append((s["font"], w))
        uri = s.get("uri", "") if txt.strip() else ""
        is_math = bool(TEXMATH_RE.match(_fname(s["font"]))) and bool(txt.strip())
        if prev is not None:
            gap = b[0] - prev["bbox"][2]
            ptxt = parts[-1].t if parts else ""
            if gap > 0.12 * max(s["size"], 1) and not ptxt.endswith(" ") and not txt.startswith(" ") and kind is None:
                parts.append(_Part("n", False, False, "", False, " "))
        parts.append(_Part(kind or "n", bool(italic) and kind is None, bool(bold) and kind is None, uri, is_math, txt))
        prev = s
    h = _render_parts(parts)
    h = re.sub(r"[ \t ]+", " ", h).strip()
    dsp = despace_letters(strip_tags(h))
    if dsp != strip_tags(h):
        h = esc(dsp)
    text = strip_tags(h)
    if not text.strip():
        return None
    return Row(
        page=page, bbox=[x0, y0, x1, y1], base=base, size=float(dom), html=h, text=text,
        bold=bold_w >= 0.6 * max(total, 1), italic=ital_w >= 0.6 * max(total, 1),
        serif=serif_w >= 0.5 * max(total, 1), color=_dominant(colors) or 0,
        font=_dominant(fonts) or "", stream=stream, first_script=first_script,
        math_ratio=math_w / max(total, 1), nchars=total,
    )


def _render_parts(parts: list[_Part]) -> str:
    for p in parts:        # 添え字・上付きの位置にある文章 (∑ の下の「, has mean 0 and variance」など) は数式ではなく通常の文字
        if p.k in ("sup", "sub") and len(re.findall(r"[A-Za-z]{3,}", p.t)) >= 2 and " " in p.t.strip():
            p.k = "n"
    # 同スタイルの run を結合し、同スタイルの run に挟まれた空白だけの part も両隣と結合する
    same: list[_Part] = []
    for p in parts:
        if same and same[-1].style() == p.style():
            same[-1].t += p.t
        else:
            same.append(_Part(p.k, p.it, p.bd, p.uri, p.m, p.t))
    merged: list[_Part] = []
    i = 0
    while i < len(same):
        p = same[i]
        if ((not p.t.strip() or p.t.strip() in ("’", "'", "‘", "‐", "-")) and merged and i + 1 < len(same) and merged[-1].k == "n"
                and merged[-1].style() == same[i + 1].style()):
            merged[-1].t += p.t + same[i + 1].t
            i += 2
            continue
        merged.append(p)
        i += 1
    # 数式フォントの run (+ 直後/直前の上下付き) を <x>..</x> にまとめる
    n = len(merged)
    in_x = [p.m for p in merged]
    for j in range(n):
        if merged[j].k in ("sup", "sub") and ((j > 0 and merged[j - 1].m) or (j + 1 < n and merged[j + 1].m)):
            in_x[j] = True
    for j in range(1, n - 1):
        if not merged[j].t.strip() and in_x[j - 1] and in_x[j + 1]:
            in_x[j] = True
    out: list[str] = []
    j = 0
    while j < n:
        if in_x[j]:
            k = j
            while k < n and in_x[k]:
                k += 1
            inner = "".join(_emit_part(p, inner_only=True) for p in merged[j:k])
            lead = inner[: len(inner) - len(inner.lstrip())]
            trail = inner[len(inner.rstrip()):]
            out.append(f"{lead}<x>{inner.strip()}</x>{trail}")
            j = k
        else:
            out.append(_emit_part(merged[j]))
            j += 1
    return "".join(out)


# --------------------------------------------------------------------------
# 回転ページ: 座標は常に見た目の向き (page.rotation_matrix を適用) で扱う
# --------------------------------------------------------------------------

def _vrect(r, M: fitz.Matrix) -> list[float]:
    rr = (fitz.Rect(r) * M).normalize()
    return [rr.x0, rr.y0, rr.x1, rr.y1]


def _uri_of(bbox, uri_links) -> str:
    """bbox の中心を含む URI リンクの uri (無ければ "")。"""
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    for r, uri in uri_links:
        if r[0] - 0.3 <= cx <= r[2] + 0.3 and r[1] - 0.3 <= cy <= r[3] + 0.3:
            return uri
    return ""


def _split_span_by_links(s: dict, uri_links) -> list[dict]:
    """rawdict の span (chars あり) を、URI リンクの境界で分割し、各片に "uri" を付ける。
    1 つの span が行全体 (リンク部分だけ別 span になっていない PDF) でも、リンク文字列だけにリンクを付けられる。"""
    groups: list[list] = []   # [uri, chars]
    chars = s.get("chars") or []
    if _needs_spaces("".join(c["c"] for c in chars)):
        size = max(s.get("size", 1.0), 1.0)
        spaced = []
        for c in chars:
            if spaced and c["c"].strip() and spaced[-1]["c"].strip() and c["bbox"][0] - spaced[-1]["bbox"][2] >= 0.1 * size:
                pb = spaced[-1]["bbox"]
                spaced.append({"c": " ", "bbox": (pb[2], pb[1], c["bbox"][0], pb[3]), "origin": tuple(spaced[-1]["origin"])})
            spaced.append(c)
        chars = spaced
    for ch in chars:
        ws = not ch["c"].strip()
        u = "" if ws else _uri_of(ch["bbox"], uri_links)
        if not groups:
            groups.append([u, [ch]])
            continue
        cur = groups[-1]
        if ws or cur[0] == u:
            cur[1].append(ch)                      # 空白は直前のグループに付ける
        elif cur[0] == "" and not any(c["c"].strip() for c in cur[1]):
            cur[0] = u                             # ここまで空白だけのグループはこの文字のグループに合流
            cur[1].append(ch)
        else:
            groups.append([u, [ch]])
    out = []
    for u, chs in groups:
        if not chs:
            continue
        t = "".join(c["c"] for c in chs)
        s2 = {k: v for k, v in s.items() if k != "chars"}
        xs = [c["bbox"] for c in chs]
        s2["text"] = t
        s2["bbox"] = (min(b[0] for b in xs), min(b[1] for b in xs), max(b[2] for b in xs), max(b[3] for b in xs))
        s2["origin"] = tuple(chs[0]["origin"])
        s2["uri"] = u
        out.append(s2)
    return out


def _needs_spaces(text: str) -> bool:
    """空白が 1 つも無いのに長い (語がつながっている) span か。両端揃えで語間を文字間隔 (Tc) で表した PDF に多い。"""
    t = text.strip()
    return len(t) >= 18 and t.count(" ") < len(t) / 25 and sum(c.isalpha() for c in t) >= 0.6 * len(t)


def _restore_missing_spaces(page: fitz.Page, d: dict, em: float = 0.1) -> int:
    """語間が空白文字ではなく文字の間隔で表されている span の文字間の空き (em の 10% 以上。字間は 0 か負で、語間は 0.13〜0.3 em になる) に空白を入れる。直した span の数を返す。
    (d は page.get_text("dict") の結果で、その場で書き換える。回転ページは対象外。)"""
    cand = [sp for b in d["blocks"] if b.get("type") == 0 for ln in b["lines"] for sp in ln["spans"] if _needs_spaces(sp["text"])]
    if not cand or page.rotation:
        return 0
    raw = page.get_text("rawdict")
    by_bbox = {}
    for b in raw["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            for sp in ln["spans"]:
                by_bbox[tuple(round(v, 1) for v in sp["bbox"])] = sp
    n = 0
    for sp in cand:
        r = by_bbox.get(tuple(round(v, 1) for v in sp["bbox"]))
        if not r or not r.get("chars"):
            continue
        out, prev = [], None
        size = max(sp["size"], 1.0)
        for ch in r["chars"]:
            c = ch["c"]
            if prev is not None and c.strip() and prev["c"].strip() and ch["bbox"][0] - prev["bbox"][2] >= em * size:
                out.append(" ")
            out.append(c)
            prev = ch
        txt = "".join(out)
        if txt != sp["text"]:
            sp["text"] = txt
            n += 1
    return n


def _visual_lines(page: fitz.Page, uri_links: list | None = None) -> list[dict]:
    """get_text の行を見た目の座標へ変換して返す (回転 0 は無変換)。URI リンクのあるページは rawdict で文字単位に
    リンク境界を判定し、リンク部分の span に "uri" を付ける。"""
    raw = bool(uri_links)
    d = page.get_text("rawdict" if raw else "dict")
    if not raw:
        _restore_missing_spaces(page, d)
    M = page.rotation_matrix
    rot = page.rotation % 360
    mdir = fitz.Matrix(M.a, M.b, M.c, M.d, 0, 0)
    out = []
    for b in d["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            dx, dy = ln.get("dir", (1, 0))
            p = fitz.Point(dx, dy) * mdir if rot else fitz.Point(dx, dy)
            spans = []
            for s in ln["spans"]:
                if rot:
                    s = dict(s)
                    s["bbox"] = _vrect(s["bbox"], M)
                    o = fitz.Point(s["origin"]) * M
                    s["origin"] = (o.x, o.y)
                    if raw:
                        s["chars"] = [dict(c, bbox=_vrect(c["bbox"], M), origin=tuple(fitz.Point(c["origin"]) * M))
                                      for c in s.get("chars", [])]
                if raw:
                    if "chars" in s and not s.get("text"):
                        spans.extend(_split_span_by_links(s, uri_links))
                    else:
                        spans.append(s)
                else:
                    spans.append(s)
            for i in range(1, len(spans)):          # 合字 (ﬁ ﬂ など) だけの span は別フォントなので、直前の字の書体に合わせる (太字が途切れない)
                if spans[i]["text"].strip() and all(ch in "ﬀﬁﬂﬃﬄﬅﬆ" for ch in spans[i]["text"].strip()):
                    spans[i] = dict(spans[i], font=spans[i - 1]["font"], flags=spans[i - 1]["flags"])
            out.append({"dir": (p.x, p.y), "spans": spans})
    return out


def _mk_line(sp: list[dict], stream: int) -> dict:
    weights = [(round(s["size"], 1), len(s["text"].strip())) for s in sp if not (s["flags"] & 1)]
    dom = _dominant(weights) if any(w for _, w in weights) else sp[0]["size"]
    near = [s["origin"][1] for s in sp if abs(s["size"] - dom) < 0.15 * dom + 0.2]
    base = sorted(near)[len(near) // 2] if near else sp[0]["origin"][1]
    return {"spans": sp, "base": base, "size": dom, "stream": stream,
            "x0": min(s["bbox"][0] for s in sp), "x1": max(s["bbox"][2] for s in sp)}


def _same_baseline_clusters(lines: list[dict]) -> list[list[dict]]:
    ls = sorted(lines, key=lambda l: (l["base"], l["x0"]))
    clusters: list[list[dict]] = []
    for l in ls:
        if clusters and abs(l["base"] - clusters[-1][0]["base"]) <= 0.3 * min(l["size"], clusters[-1][0]["size"]):
            clusters[-1].append(l)
        else:
            clusters.append([l])
    return clusters


def find_gutters(lines: list[dict], page_h: float, min_members: int = 6, min_extent: float = 0.12,
                 known: list[tuple[float, float]] | None = None) -> list[tuple[float, float]]:
    """段間 (ガター) を、ページ内の「同じ基線の文字の連なりの間の空き」が縦方向に揃っているものとして検出する。

    各基線クラスタ内の連続する span の間の空き (幅 >= max(1 字, 7pt)) を集め、x 範囲が揃うものをまとめて、
    min_members 本以上の行にまたがり、縦の広がりが ページ高さの min_extent 以上のものをガターとする。
    全幅の要素 (タイトル・要旨・全幅のキャプション) はガターの位置に空きが無いので、その帯では何も検出されない (y の帯ごとの判定になる)。"""
    gaps: list[tuple[float, float, float, float, float]] = []
    for cl in _same_baseline_clusters(lines):
        spans = sorted((s for l in cl for s in l["spans"]), key=lambda s: s["bbox"][0])
        if len(spans) < 2:
            continue
        size = cl[0]["size"]
        end = spans[0]["bbox"][2]
        y = (spans[0]["bbox"][1] + spans[0]["bbox"][3]) / 2
        for s in spans[1:]:
            a, b = end, s["bbox"][0]
            if b - a >= max(1.0 * size, 7.0):
                gaps.append((a, b, y, a - spans[0]["bbox"][0], max(s2["bbox"][2] for s2 in spans) - b))   # (空きの左端, 右端, y, 左の文字の幅, 右の文字の幅)
            end = max(end, s["bbox"][2])
    gaps.sort()
    groups: list[list[tuple]] = []
    for g in gaps:
        for grp in groups:
            ga = sorted(x[0] for x in grp)[len(grp) // 2]
            gb = sorted(x[1] for x in grp)[len(grp) // 2]
            ov = min(g[1], gb) - max(g[0], ga)
            if ov >= 0.6 * min(g[1] - g[0], gb - ga):
                grp.append(g)
                break
        else:
            groups.append([g])
    clusters = _same_baseline_clusters(lines)
    cl_size = sorted(l["size"] for l in lines)[len(lines) // 2] if lines else 10.0
    # 縦に途切れた (45pt 以上空いた) ところで分けて、y の帯ごとに判定する (全幅の要素を挟んで上下で段数が違う場合)
    runs: list[list[tuple]] = []
    for grp in groups:
        cur: list[tuple] = []
        for g in sorted(grp, key=lambda x: x[2]):
            if cur and g[2] - cur[-1][2] > 45:
                runs.append(cur)
                cur = []
            cur.append(g)
        runs.append(cur)
    cand = []
    for grp in runs:
        ys = [x[2] for x in grp]
        a = sorted(x[0] for x in grp)[min(len(grp) - 1, 3 * len(grp) // 4)]     # 左の列が右ぞろえでない (ragged) ときは、最も右まで届く行に合わせる
        b = sorted(x[1] for x in grp)[len(grp) // 2]
        # 空きの両側に十分な幅の文字があること (参考文献の番号 "1." と本文の間の空きはガターではない)
        lw = sorted(x[3] for x in grp)[len(grp) // 2]
        rw = sorted(x[4] for x in grp)[len(grp) // 2]
        side_ok = lw >= 4.0 * max(1.0, cl_size) and rw >= 4.0 * max(1.0, cl_size)
        # 本物のガターは、その帯の行のほとんどで空いている。多くの行で文字が横切る空き (両端揃えの語間・段落の字下げなど) は除く
        inband = [cl for cl in clusters if min(ys) - 4 <= cl[0]["base"] <= max(ys) + 8]
        cross = sum(1 for cl in inband if any(s["bbox"][0] < b - 1 and s["bbox"][2] > a + 1 and (s["bbox"][2] - s["bbox"][0]) > 2.5 * cl[0]["size"]
                                              for l in cl for s in l["spans"]))     # 短い span (参考文献の番号 "10." など) が空きの中に入っていても、文字が横切っているとは数えない
        ok = bool(inband) and cross <= 0.25 * len(inband) and side_ok and b - a >= 2.0
        b_ret = min(b, sorted(x[1] for x in grp)[len(grp) // 4])      # 返すガターの右端は狭い側に合わせる (参考文献の番号と本文の間の空きをガターと誤認して行を割らない)
        cand.append((a, b_ret, len(grp), max(ys) - min(ys), ok, min(ys), max(ys)))
    out = [(a, b, y0, y1) for a, b, n, ext, ok, y0, y1 in cand if ok and n >= min_members and ext >= min_extent * page_h]    # (空きの左端, 右端, 帯の上端, 下端)
    # 図などで途切れて短くなった帯 (ページ下端の数行など) は、確認済みのガターと同じ位置なら 2 行以上で採用する
    ref = [(g[0], g[1]) for g in out] + [(g[0], g[1]) for g in (known or [])]      # 同じ文書の他のページで確認済みのガターも手がかりにする
    for a, b, n, ext, ok, y0, y1 in cand:
        if ok and n >= 2 and not any((g[0], g[1], g[2], g[3]) == (a, b, y0, y1) for g in out) \
                and any(min(b, gb) - max(a, ga) >= 0.8 * min(b - a, gb - ga) for ga, gb in ref):
            out.append((a, b, y0, y1))
    return out


def crosses_gutter(a: float, b: float, gutters: list[tuple], y: float | None = None) -> bool:
    """x 範囲 [a, b] の空きが、検出したガターと半分以上重なるか。"""
    if b - a < 2.0:               # 字間・合字との境目 (空きがほぼ 0) はガターではない
        return False
    w = max(b - a, 1e-6)
    # gutters の要素は (a, b) または (a, b, 帯の上端, 下端)。帯つきのものは、y (行の基線) が帯の中にあるときだけ有効
    # (図の中の軸ラベルなどが作る見かけのガターで、図の下の本文の行を割らない)
    return any(min(b, g[1]) - max(a, g[0]) >= 0.5 * min(w, g[1] - g[0])
               and (y is None or len(g) < 4 or g[2] - 6.0 <= y <= g[3] + 14.0) for g in gutters)


def split_lines_at_gutters(lines: list[dict], gutters: list[tuple[float, float]]) -> list[dict]:
    """1 本の視覚行に左右の列の span が混ざっている場合 (PyMuPDF が同じ基線の列をまとめることがある) を、ガターの位置で分ける。"""
    if not gutters:
        return lines
    out = []
    for l in lines:
        sp = sorted(l["spans"], key=lambda s: s["bbox"][0])
        parts, cur = [], [sp[0]]
        end = sp[0]["bbox"][2]
        for s in sp[1:]:
            if s["bbox"][0] - end > 0 and crosses_gutter(end, s["bbox"][0], gutters, l["base"]):
                parts.append(cur)
                cur = [s]
            else:
                cur.append(s)
            end = max(end, s["bbox"][2])
        parts.append(cur)
        if len(parts) == 1:
            out.append(l)
        else:
            out.extend(_mk_line(p, l["stream"]) for p in parts)
    return out


def collect_rows(page: fitz.Page, pno: int, cfg: Config, charmap: dict | None,
                 unmapped: dict[str, dict[str, int]],
                 uri_links: list[tuple[list[float], str]] | None = None,
                 known_gutters: list[tuple[float, float]] | None = None,
                 out_gutters: list | None = None) -> list[Row]:
    ex = cfg.section("extract")
    gap_min = _p(ex, "row_gap_min")
    gap_fac = _p(ex, "row_gap_factor")
    sub_ratio = _p(ex, "sub_size_ratio")
    lines = []
    stream = 0
    for ln in _visual_lines(page, uri_links):
        dx, dy = ln.get("dir", (1, 0))
        if abs(dy) > 0.2 or dx < 0.8:  # 縦書き・斜め文字は対象外
            stream += 1
            continue
        sp = [s for s in ln["spans"] if s["text"] != "" and s["size"] > 1]
        if not sp:
            stream += 1
            continue
        lines.append(_mk_line(sp, stream))
        stream += 1
    lines = merge_drop_cap_lines(lines)
    gutters = find_gutters(lines, page.rect.height, known=known_gutters) if _p(ex, "gutter_split") else []
    if known_gutters is not None:
        for g in gutters:
            if not any(min(g[1], kb) - max(g[0], ka) >= 0.8 * min(g[1] - g[0], kb - ka) for ka, kb in known_gutters):
                known_gutters.append((g[0], g[1]))
    if out_gutters is not None:
        out_gutters.extend([list(g) for g in gutters])      # (空きの左端, 右端, 帯の上端, 下端): 描画で、列の右端を越えて広げないために使う
    lines = split_lines_at_gutters(lines, gutters)     # 左右の列が同じ基線で 1 行にまとまらないようにする
    # 同一基線でクラスタ
    lines.sort(key=lambda l: (l["base"], l["x0"]))
    clusters: list[list[dict]] = []
    for l in lines:
        if clusters:
            ref = clusters[-1][0]
            if abs(l["base"] - ref["base"]) <= 0.3 * min(l["size"], ref["size"]):
                clusters[-1].append(l)
                continue
        clusters.append([l])
    rows: list[Row] = []
    for cl in clusters:
        cl.sort(key=lambda l: l["x0"])
        cur = [cl[0]]
        groups = []
        for l in cl[1:]:
            prev = cur[-1]
            gap = l["x0"] - max(x["x1"] for x in cur)
            thr = max(gap_min, gap_fac * min(l["size"], prev["size"]))
            if gap > thr or gap < -2 or crosses_gutter(max(x["x1"] for x in cur), l["x0"], gutters, l["base"]):
                groups.append(cur)
                cur = [l]
            else:
                cur.append(l)
        groups.append(cur)
        for g in groups:
            spans = [s for l in g for s in l["spans"]]
            r = build_row(spans, pno, min(l["stream"] for l in g), charmap, unmapped, sub_ratio, uri_links)
            if r is not None:
                rows.append(r)
    rows.sort(key=lambda r: r.stream)
    return rows


# --------------------------------------------------------------------------
# 画像 / 表領域
# --------------------------------------------------------------------------

def page_images(page: fitz.Page) -> list[list[float]]:
    M = page.rotation_matrix
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") == 1:
            out.append(_vrect(b["bbox"], M))
    return out


def is_ocr_page(page: fitz.Page, imgs: list[list[float]]) -> bool:
    """スキャン画像 + 不可視の OCR テキスト層のページか: 文字の 8 割以上が不可視 (render mode 3) で、ページの 85% 以上を覆う画像がある。"""
    try:
        tr = page.get_texttrace()
    except Exception:
        return False
    total = sum(len(t.get("chars") or ()) for t in tr)
    inv = sum(len(t.get("chars") or ()) for t in tr if t.get("type") == 3)
    if total < 20 or inv < 0.8 * total:
        return False
    area = page.rect.width * page.rect.height
    return any((i[2] - i[0]) * (i[3] - i[1]) >= 0.85 * area for i in imgs)


def ocr_figure_bands(rows: list["Row"], page_w: float, page_h: float, min_gap: float = 70.0) -> list[list[float]]:
    """OCR ページ内の図 (スキャンに埋め込まれた図) の領域を、OCR の行が無い帯から推定する。
    幅の 40% 以上ある行 (本文の行) の間が min_gap pt 以上空いている帯を図とみなし、その中の行 (軸ラベルなど) は翻訳しない。
    ページ上下の余白 (本文の行の外側) は図にしない。"""
    prose = sorted((r for r in rows if (r.x1 - r.x0) >= 0.4 * page_w), key=lambda r: r.y0)
    bands = []
    for a, b in zip(prose, prose[1:]):
        if b.y0 - a.y1 >= min_gap:
            bands.append([0.0, a.y1 + 1.0, page_w, b.y0 - 1.0])
    return bands


def normalize_ocr_rows(rows: list["Row"], bands: list[list[float]], page_w: float) -> None:
    """OCR ページの行を整える: (1) 図の帯の中の小さな断片は figure_text (in_image) にする。ただし本文の段落の最後の短い行
    (帯の直前の行に連なる、左端が揃った行) は本文のまま。(2) OCR は行ごとに文字サイズがばらつくので、本文の行 (幅 40% 以上) の
    サイズの中央値に揃える (段落の結合・基準サイズの判定が行ごとに割れないように)。"""
    prose = [r for r in rows if (r.x1 - r.x0) >= 0.4 * page_w]
    if prose:
        sizes = sorted(r.size for r in prose)
        med = sizes[len(sizes) // 2]
        left = sorted(r.x0 for r in prose)[len(prose) // 2]
        for r in rows:
            if 0.85 * med <= r.size <= 1.12 * med or (r.nchars >= 8 and 0.78 * med <= r.size <= 1.2 * med):
                r.size = med       # OCR の行の高さは ±15〜20% ばらつく (箇条の行など)。ある程度の長さの行は本文サイズへ揃える
    else:
        med, left = 10.0, 0.0
    for b in bands:
        prev_bottom = b[1]
        for r in sorted((r for r in rows if b[1] <= (r.y0 + r.y1) / 2 <= b[3]), key=lambda r: r.y0):
            if (r.x1 - r.x0) >= 0.4 * page_w:
                continue
            if r.y0 - prev_bottom <= 0.5 * med and abs(r.x0 - left) <= 0.8 * med + 8 and r.nchars >= 6:
                prev_bottom = r.y1          # 段落の最後の短い行 (本文)
                continue
            r.in_image = True


OCR_TABLE_TITLE_RE = re.compile(r"^TA[A-Z]{2,3}E?\s*[\dIl]+\s*[.:]?", re.I)


def _label_of_image(b: list[float], img: list[float], page_w: float) -> bool:
    """行 b が、小さな画像 img (幅がページの 45% 以下) の真上 32pt 以内/真下 22pt 以内にあり、画像の中央に揃っているか。"""
    w = img[2] - img[0]
    if w <= 0 or w > 0.45 * page_w or (b[2] - b[0]) > 1.05 * w:
        return False
    if abs((b[0] + b[2]) / 2 - (img[0] + img[2]) / 2) > 0.25 * w:
        return False
    return (img[1] - 32 <= b[3] <= img[1] + 2) or (img[3] - 2 <= b[1] <= img[3] + 22)


def mark_ocr_tables(rows: list["Row"], page_w: float) -> int:
    """OCR ページの表: 表題 (TABLE 1. … 。OCR の誤読も許す) の下から、本文の行 (列の幅の 93% 以上・60 字以上。両端揃えの本文は行がほぼ全幅になる) が現れる、
    または行間が大きく空くところまでの行を、表の本体 (in_table: 翻訳せず原文のまま残す) にする。表題の行はキャプションとして翻訳する。
    表の本体の数に加えた行数を返す。"""
    prose = [r for r in rows if (r.x1 - r.x0) >= 0.4 * page_w]
    if not prose:
        return 0
    col_w = max(r.x1 for r in prose) - min(r.x0 for r in prose)
    n = 0
    order = sorted(rows, key=lambda r: (r.y0, r.x0))
    for t in [r for r in order if OCR_TABLE_TITLE_RE.match(r.text) and not r.in_image]:
        last = t.y1
        for r in order:
            if r is t or r.y0 < t.y1 - 3.0:
                continue
            if r.in_image:
                break
            if ((r.x1 - r.x0) >= 0.93 * col_w and r.nchars >= 60) or r.y0 - last > 3.0 * r.size:
                break
            r.in_table = True
            n += 1
            last = max(last, r.y1)
    return n


def merge_ocr_fragments(rows: list["Row"], page_w: float, max_gap_em: float = 3.2) -> list["Row"]:
    """OCR は行の途中の記号 (● など) の前後で 1 行を 2 つに分けることがある。同じ基線で、間が max_gap_em 字以内で、右の断片が
    本文の列の右端まで届く (行の続き) ものは 1 行に戻す。表の列 (右端が揃わない) には使わない。"""
    prose = sorted(r.x1 for r in rows if (r.x1 - r.x0) >= 0.4 * page_w)
    if not prose:
        return rows
    right = prose[int(0.9 * (len(prose) - 1))]
    out: list[Row] = []
    drop: set[int] = set()
    order = sorted(range(len(rows)), key=lambda i: (rows[i].base, rows[i].x0))
    for ia, ib in zip(order, order[1:]):
        a, b = rows[ia], rows[ib]
        if ia in drop or abs(a.base - b.base) > 0.3 * min(a.size, b.size):
            continue
        gap = b.x0 - a.x1
        near = [r.x1 for r in rows if r is not a and r is not b and (r.x1 - r.x0) >= 0.4 * page_w
                and b.y0 - 45 <= r.y0 <= b.y0 + 1 and abs(r.x0 - a.x0) <= 12]      # 同じ段落の直前の行の右端 (キャプションなど、本文より狭い段落)
        edge = max(near) if near else right
        if 0 <= gap <= max_gap_em * a.size and b.x1 >= edge - 0.6 * b.size and b.nchars >= 8 and a.nchars >= 2                 and not a.in_image and not b.in_image:
            rows[ia] = dc_replace(
                a, bbox=[a.x0, min(a.y0, b.y0), b.x1, max(a.y1, b.y1)], html=a.html + " " + b.html, text=a.text + " " + b.text,
                nchars=a.nchars + b.nchars, stream=min(a.stream, b.stream))
            drop.add(ib)
    return [r for i, r in enumerate(rows) if i not in drop]


def merge_rows_across_inline_images(rows: list["Row"], imgs: list[list[float]]) -> list["Row"]:
    """行の途中に小さな画像 (凡例の破線・記号など) が挟まると、行が画像の前後で 2 つに割れる。同じ基線で、間に小さな画像 (高さが行程度) が
    あり、間隔が 220pt 以内なら 1 行に戻す (割れたままだと、行の断片が別の frame になって 1 語だけの枠に訳文が押し込まれる)。"""
    small = [i for i in imgs if (i[3] - i[1]) <= 24 and (i[2] - i[0]) <= 140]
    if not small:
        return rows
    drop: set[int] = set()
    order = sorted(range(len(rows)), key=lambda i: (rows[i].base, rows[i].x0))
    for ia, ib in zip(order, order[1:]):
        a, b = rows[ia], rows[ib]
        if ia in drop or abs(a.base - b.base) > 0.3 * min(a.size, b.size):
            continue
        gap = b.x0 - a.x1
        if 0 <= gap <= 220 and any(i[0] >= a.x1 - 3 and i[2] <= b.x0 + 3 and i[1] < b.y1 and i[3] > b.y0 for i in small):
            rows[ia] = dc_replace(a, bbox=[a.x0, min(a.y0, b.y0), b.x1, max(a.y1, b.y1)], html=a.html + " " + b.html,
                                  text=a.text + " " + b.text, nchars=a.nchars + b.nchars, stream=min(a.stream, b.stream))
            drop.add(ib)
    return [r for i, r in enumerate(rows) if i not in drop]


def merge_drop_cap_lines(lines: list[dict]) -> list[dict]:
    """ドロップキャップ (段落の頭の大きな装飾文字 1 字。本文の 2 倍以上の大きさで、右隣の数行が字下げされている) を、
    右隣の最初の行の先頭に付けて 1 つの段落にする (視覚行の段階。行にまとめる前なので、装飾文字が基線の近い別の行に張り付かない)。
    右隣の字下げされた行は左端を装飾文字の左端までそろえ (字下げが無い段落として続く)、装飾文字の領域は最初の行の bbox に含める。"""
    if len(lines) < 4:
        return lines
    wt: collections.Counter = collections.Counter()
    for l in lines:
        wt[round(l["size"], 1)] += sum(len(sp["text"].strip()) for sp in l["spans"])
    body = wt.most_common(1)[0][0]
    drop: set[int] = set()
    for ci, cap in enumerate(lines):
        t = "".join(sp["text"] for sp in cap["spans"]).strip()
        if len(t) != 1 or not t.isalpha() or cap["size"] < 2.0 * body:
            continue
        cb = [min(sp["bbox"][0] for sp in cap["spans"]), min(sp["bbox"][1] for sp in cap["spans"]),
              max(sp["bbox"][2] for sp in cap["spans"]), max(sp["bbox"][3] for sp in cap["spans"])]
        beside = [i for i, l in enumerate(lines) if i != ci and cb[2] - 3 <= l["x0"] <= cb[2] + 9
                  and min(sp["bbox"][1] for sp in l["spans"]) >= cb[1] - 8 and max(sp["bbox"][3] for sp in l["spans"]) <= cb[3] + 8
                  and l["size"] <= 1.25 * body and len(l["spans"]) >= 1]
        if not beside:
            continue
        beside.sort(key=lambda i: min(sp["bbox"][1] for sp in lines[i]["spans"]))
        top = min(sp["bbox"][1] for sp in lines[beside[0]]["spans"])
        if top > cb[1] + 0.6 * cap["size"]:
            continue
        for k, i in enumerate(beside):
            l = lines[i]
            sp = sorted(l["spans"], key=lambda x: x["bbox"][0])
            first = dict(sp[0])
            first["bbox"] = (cb[0], min(first["bbox"][1], cb[1]) if k == 0 else first["bbox"][1], first["bbox"][2], first["bbox"][3])
            if k == 0:
                first["text"] = t + first["text"]
                first["origin"] = (cb[0], first["origin"][1])
            nl_ = _mk_line([first] + sp[1:], min(l["stream"], cap["stream"]) if k == 0 else l["stream"])
            lines[i] = nl_
        drop.add(ci)
    return [l for i, l in enumerate(lines) if i not in drop]


def page_uri_links(page: fitz.Page) -> list[tuple[list[float], str]]:
    M = page.rotation_matrix
    out = []
    for l in page.get_links():
        if l.get("kind") == fitz.LINK_URI and l.get("uri"):
            out.append((_vrect(l["from"], M), l["uri"]))
    return out


def _inside(b: list[float], region: list[float], pad: float = 0.0) -> bool:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return region[0] - pad <= cx <= region[2] + pad and region[1] - pad <= cy <= region[3] + pad


def x_overlap(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _drawing_lines(page: fitz.Page, W: float, H: float):
    """drawings を見た目の座標で (水平罫線, 垂直罫線, 枠矩形) に分ける。"""
    M = page.rotation_matrix
    hs: list[tuple[float, float, float]] = []   # (y, x0, x1)
    vs: list[tuple[float, float, float]] = []   # (x, y0, y1)
    boxes: list[list[float]] = []
    for dr in page.get_drawings():
        for it in dr["items"]:
            op = it[0]
            if op == "l":
                p1, p2 = fitz.Point(it[1]) * M, fitz.Point(it[2]) * M
                if abs(p1.y - p2.y) <= 1.0 and abs(p1.x - p2.x) >= 4:
                    hs.append(((p1.y + p2.y) / 2, min(p1.x, p2.x), max(p1.x, p2.x)))
                elif abs(p1.x - p2.x) <= 1.0 and abs(p1.y - p2.y) >= 4:
                    vs.append(((p1.x + p2.x) / 2, min(p1.y, p2.y), max(p1.y, p2.y)))
            elif op == "re":
                r = (fitz.Rect(it[1]) * M).normalize()
                if r.height <= 2.5 and r.width >= 4:
                    hs.append(((r.y0 + r.y1) / 2, r.x0, r.x1))
                elif r.width <= 2.5 and r.height >= 4:
                    vs.append(((r.x0 + r.x1) / 2, r.y0, r.y1))
                elif r.width >= 12 and r.height >= 6 and r.get_area() < 0.85 * W * H:
                    boxes.append([r.x0, r.y0, r.x1, r.y1])
    return hs, vs, boxes


def _distinct(vals: list[float], tol: float = 2.0) -> list[float]:
    out: list[float] = []
    for v in sorted(vals):
        if not out or v - out[-1] > tol:
            out.append(v)
    return out


def _tabular_ratio(rows: list[Row]) -> float:
    """同一基線に複数セル (行) がある基線の割合。"""
    if not rows:
        return 0.0
    cl: list[list[Row]] = []
    for r in sorted(rows, key=lambda r: r.base):
        if cl and abs(r.base - cl[-1][0].base) <= 0.3 * min(r.size, cl[-1][0].size):
            cl[-1].append(r)
        else:
            cl.append([r])
    return sum(1 for c in cl if len(c) >= 2) / len(cl)


def _aligned_lines(rows: list[Row]) -> int:
    """同一基線に複数セル (行) がある基線の数。"""
    cl: list[list[Row]] = []
    for r in sorted(rows, key=lambda r: r.base):
        if cl and abs(r.base - cl[-1][0].base) <= 0.3 * min(r.size, cl[-1][0].size):
            cl[-1].append(r)
        else:
            cl.append([r])
    return sum(1 for c in cl if len(c) >= 2)


def _prose_rows(rows: list[Row]) -> int:
    """散文の長さの行 (60 字以上) の数。表のセルには少ない (定義列のような長いセルがあっても全体の 2 割程度)。"""
    return sum(1 for r in rows if r.nchars >= 60)


def table_like(rows: list[Row], min_aligned: int = 3) -> bool:
    """領域内の文字行が表らしいか: セルが揃った行 (同一基線に複数セル) が min_aligned 本以上あり、散文の長さの行が多くない。
    要旨ブロック (Elsevier 風の 2 カラム: Article info | Abstract) のように、散文の行が 4 本以上かつ全体の 1/4 以上なら表にしない。"""
    if _aligned_lines(rows) < min_aligned:
        return False
    pr = _prose_rows(rows)
    return not (pr >= 4 and pr >= 0.35 * len(rows))


MONO_FONT_RE = re.compile(r"(courier|consolas|mono|menlo|monaco|inconsolata|lucidaconsole|typewriter|cmtt|cursor|fixed|code)", re.I)
ALGO_LINE_RE = re.compile(r"(?:^|\s)(\d{1,2}):\s")


def is_monospace_font(name: str) -> bool:
    """等幅フォント (Courier / Consolas / ...Mono / CMTT 等) の名前か。コード・疑似コードは翻訳しない。"""
    return bool(MONO_FONT_RE.search(name or ""))


def looks_like_algorithm(text: str, nrows: int) -> bool:
    """行頭が 1: 2: 3: の連番になっている (アルゴリズム・疑似コード)。"""
    if nrows < 3:
        return False
    nums = [int(x) for x in ALGO_LINE_RE.findall(text)]
    return len(nums) >= 3 and nums[:3] == list(range(nums[0], nums[0] + 3)) and nums[0] <= 2


LIST_LABEL_RE = re.compile(r"^\s*(\(?\d{1,2}[.)]|\(?[ivxIVX]{1,4}[.)]|\(?[a-z][.)]|[•◦▪▸●■‣∙·])\s+\S")
BULLET_START_RE = re.compile(r"^\s*[•◦▪▸●■‣∙·]\s*\S")
CAPTION_START_RE = re.compile(r"^\s*(Fig(?:ure)?s?\.?|Extended Data Fig\.?|Supplementary Fig\.?|Table)\s*S?\d", re.I)


def vector_figure_regions(page: fitz.Page, tol: float = 14.0) -> list[list[float]]:
    """ベクタ図 (プロット・フローチャート・ダイアグラム) の領域を、page.cluster_drawings() (近い図形のかたまり) から推定する。
    曲線を含む、または図形が 12 個以上あるかたまりだけを図とする。罫線だけ (表) や背景の塗りだけのかたまりは除く。見た目の座標。"""
    try:
        drs = page.get_drawings()
        if len(drs) < 12:
            return []
        clusters = page.cluster_drawings(drawings=drs, x_tolerance=tol, y_tolerance=tol)
    except Exception:
        return []
    M = page.rotation_matrix
    out = []
    for r in clusters:
        if r.width < 40 or r.height < 25:
            continue
        big = fitz.Rect(r.x0 - 1, r.y0 - 1, r.x1 + 1, r.y1 + 1)
        inside = [d for d in drs if big.contains(fitz.Rect(d["rect"]))]
        # 背景の塗り (かたまりの面積の半分以上を占める塗りつぶし) は図形に数えない。残りが図
        area = max(r.width * r.height, 1.0)
        inside = [d for d in inside if not (d.get("fill") is not None and fitz.Rect(d["rect"]).get_area() >= 0.5 * area)]
        n = len(inside)
        curves = sum(1 for d in inside for it in d["items"] if it[0] == "c")
        if n < 12 and curves < 3:
            continue
        thin = sum(1 for d in inside if d["rect"].height < 1.6 or d["rect"].width < 1.6)
        if curves == 0 and n < 40 and thin >= 0.9 * n:
            continue                       # 水平・垂直の罫線だけ = 表
        u = fitz.Rect(inside[0]["rect"])
        for d in inside[1:]:
            u |= fitz.Rect(d["rect"])
        if u.width < 40 or u.height < 25:
            continue
        v = (u * M).normalize()
        out.append([v.x0, v.y0, v.x1, v.y1])
    return out


def dense_vector_cells(page: fitz.Page, cell: float = 36.0, min_paths: int = 14) -> set[tuple[int, int]]:
    """ベクタ図 (matplotlib/TikZ/PowerPoint 由来のプロット・図) が密なセルの集合。小さな図形 (目盛り・マーカー・棒・線分) の数を、
    cell pt 四方のセルごとに数え、min_paths 以上のセルとその隣を返す。罫線表 (長い水平罫線) や下線は小さな図形に数えない。"""
    M = page.rotation_matrix
    cnt: dict[tuple[int, int], int] = collections.defaultdict(int)
    try:
        drawings = page.get_drawings()
    except Exception:
        return set()
    for dr in drawings:
        r = (fitz.Rect(dr["rect"]) * M).normalize()
        if r.width > 80 or r.height > 80 or (r.width < 0.5 and r.height < 0.5):
            continue
        if r.height < 1.5 and r.width > 12:    # 下線・罫線の断片
            continue
        cnt[(int((r.x0 + r.x1) / 2 // cell), int((r.y0 + r.y1) / 2 // cell))] += 1
    dense = {k for k, v in cnt.items() if v >= min_paths}
    out = set(dense)
    for (i, j) in dense:
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                out.add((i + di, j + dj))
    return out


def doc_body_size_hint(rows: list[Row]) -> float:
    """ページ内の最頻サイズ (文字数で重み付け) = 本文サイズの目安。"""
    c: collections.Counter = collections.Counter()
    for r in rows:
        c[round(r.size, 1)] += r.nchars
    return c.most_common(1)[0][0] if c else 10.0


def _rows_in(region: list[float], rows: list[Row]) -> list[Row]:
    return [r for r in rows if _inside(r.bbox, region)]


def find_table_regions(page: fitz.Page, rows: list[Row] | None = None, ex: dict | None = None) -> list[list[float]]:
    """drawings (水平罫線のグループ・縦罫線を含む格子・枠矩形) から表領域を推定する。

    固定の本数/幅/間隔に依存しない: (a) x 範囲が揃った水平罫線のグループ (3 本以上、または 2 本で表らしい文字配置)、
    (b) 罫線・矩形が連結した格子、(c) 矩形セルの集合。いずれも領域内の文字行が「表らしい」(複数セルの基線) か確認する。
    ヘッダ/フッタ帯の罫線は除外。段落 (列幅いっぱいの長文行) を挟む罫線は別の表として分ける。"""
    ex = ex or {}
    rows = rows or []
    W, H = page.rect.width, page.rect.height
    minw = max(_p(ex, "table_min_width_frac") * W, 20.0)
    mg = _p(ex, "table_margin_frac")
    tab_ratio = _p(ex, "table_tabular_ratio")
    hs, vs, boxes = _drawing_lines(page, W, H)
    regions: list[list[float]] = []

    # (a) 水平罫線グループ
    rules = sorted({(round(y, 1), x0, x1) for y, x0, x1 in hs if x1 - x0 >= minw and mg * H < y < (1 - mg) * H})
    groups: list[dict] = []
    for y, x0, x1 in rules:
        w = x1 - x0
        for g in groups:
            gw = g["x1"] - g["x0"]
            if x_overlap(x0, x1, g["x0"], g["x1"]) >= 0.7 * min(w, gw) and 0.5 <= w / gw <= 2.0:
                g["rules"].append((y, x0, x1))
                g["x0"], g["x1"] = min(g["x0"], x0), max(g["x1"], x1)
                break
        else:
            groups.append({"x0": x0, "x1": x1, "rules": [(y, x0, x1)]})
    for g in groups:
        ys: list[float] = []
        for y, _, _ in sorted(g["rules"]):
            if not ys or abs(y - ys[-1]) > 2.0:
                ys.append(y)
        gw = g["x1"] - g["x0"]
        pieces: list[list[float]] = [[ys[0]]]
        for a, b in zip(ys, ys[1:]):
            para = [r for r in rows if r.y0 >= a - 1 and r.y1 <= b + 1 and x_overlap(r.x0, r.x1, g["x0"], g["x1"]) > 0
                    and (r.x1 - r.x0) >= 0.85 * gw and r.nchars >= 80]
            if para:
                pieces.append([b])
            else:
                pieces[-1].append(b)
        for pc in pieces:
            if len(pc) < 2:
                continue
            reg = [g["x0"], pc[0] - 1, g["x1"], pc[-1] + 1]
            inside = _rows_in(reg, rows)
            if len(pc) >= 3 and inside and (_prose_rows(inside) < 4 or table_like(inside)):
                regions.append(reg)
            elif len(pc) == 2 and len(inside) >= 2 and _tabular_ratio(inside) >= tab_ratio and table_like(inside):
                regions.append(reg)

    # (b)(c) 罫線+矩形の連結成分
    segs: list[tuple[str, list[float]]] = []
    for y, x0, x1 in hs:
        if x1 - x0 >= 8 and mg * H < y < (1 - mg) * H:
            segs.append(("h", [x0, y, x1, y]))
    for x, y0, y1 in vs:
        if y1 - y0 >= 8 and mg * H < y0 and y1 < (1 - mg) * H:
            segs.append(("v", [x, y0, x, y1]))
    for b in boxes:
        segs.append(("b", b))
    if 0 < len(segs) <= 4000:
        parent = list(range(len(segs)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        pad = 1.5
        order = sorted(range(len(segs)), key=lambda i: segs[i][1][0])
        for ai, i in enumerate(order):
            bi = segs[i][1]
            for j in order[ai + 1:]:
                bj = segs[j][1]
                if bj[0] > bi[2] + pad:
                    break
                if bj[1] <= bi[3] + pad and bi[1] <= bj[3] + pad:
                    parent[find(i)] = find(j)
        comps: dict[int, list[int]] = collections.defaultdict(list)
        for i in range(len(segs)):
            comps[find(i)].append(i)
        for idxs in comps.values():
            nh = len(_distinct([segs[i][1][1] for i in idxs if segs[i][0] == "h"]))
            nv = len(_distinct([segs[i][1][0] for i in idxs if segs[i][0] == "v"]))
            nb = sum(1 for i in idxs if segs[i][0] == "b")
            if nh + nv + nb < 2:
                continue
            reg = [min(segs[i][1][0] for i in idxs), min(segs[i][1][1] for i in idxs),
                   max(segs[i][1][2] for i in idxs), max(segs[i][1][3] for i in idxs)]
            if (reg[2] - reg[0]) * (reg[3] - reg[1]) > 0.7 * W * H or reg[3] - reg[1] < 10 or reg[2] - reg[0] < 20:
                continue
            inside = _rows_in(reg, rows)
            if len(inside) < 2:
                continue
            gridlike = (nh >= 3 and nv >= 2) or (nv >= 3 and nh >= 2) or nb >= 4
            if gridlike or (len(inside) >= 4 and (nv or nb >= 2) and _tabular_ratio(inside) >= tab_ratio and table_like(inside)):
                regions.append(reg)

    # 重なる領域を統合
    merged: list[list[float]] = []
    for r in sorted(regions, key=lambda r: (r[1], r[0])):
        for m in merged:
            ov = x_overlap(r[0], r[2], m[0], m[2]) * x_overlap(r[1], r[3], m[1], m[3])
            small = min((r[2] - r[0]) * (r[3] - r[1]), (m[2] - m[0]) * (m[3] - m[1]))
            if small > 0 and ov >= 0.5 * small:
                m[0], m[1], m[2], m[3] = min(m[0], r[0]), min(m[1], r[1]), max(m[2], r[2]), max(m[3], r[3])
                break
        else:
            merged.append(list(r))
    return merged


# --------------------------------------------------------------------------
# 段落 frame 化
# --------------------------------------------------------------------------

CAPTION_START = re.compile(r"^(Figure|Fig\.|FIGURE|FIG\.|Table|TABLE|Scheme|Chart|Plate)\s*[A-Z]?\d+[A-Za-z]?\s*([.:|]|\s{0,2}[A-Z])", re.U)
CAPTION_STRICT = re.compile(
    r"^((Figure|Fig\.|Table|Scheme)\s*[A-Z]?\d+[A-Za-z]?\s*[.:|]|(FIGURE|FIG\.|TABLE|SCHEME)\s*[A-Z]?\d+[A-Za-z]?\b)")
LABEL_START = re.compile(r"^[A-Z][A-Za-z ]{2,24}:\s")


def size_match(a: float, b: float, ex: dict) -> bool:
    return abs(a - b) <= max(_p(ex, "size_tol_abs"), _p(ex, "size_tol_rel") * max(a, b))


class _Frame:
    def __init__(self, row: Row):
        self.rows = [row]

    @property
    def left(self): return min(r.x0 for r in self.rows)
    @property
    def right(self): return max(r.x1 for r in self.rows)


def _colmax(row: Row, rows: list[Row], ex: dict) -> tuple[float, bool]:
    """行の属する列の右端と、その列が ragged (右端不揃い) かどうか。"""
    peers = [r for r in rows
             if r.page == row.page and size_match(r.size, row.size, ex)
             and x_overlap(r.x0, r.x1, row.x0, row.x1) >= 0.8 * min(r.x1 - r.x0, row.x1 - row.x0)
             and (r.x1 - r.x0) > 1.5 * r.size]
    if not peers:
        return row.x1, True
    cm = max(r.x1 for r in peers)
    reach = sum(1 for r in peers if r.x1 >= cm - 0.6 * row.size)
    ragged = len(peers) >= 4 and reach / len(peers) < 0.4
    return cm, ragged


def _styled_partly(r: Row, tag: str) -> bool:
    """tag (i/b) で囲まれていない文字が 3 字以上ある行 (太字/斜体が一部だけの行。例: 「<i>2</i> and <i>Supplementary file 6</i>).」)。"""
    rest = re.sub(r"<%s>.*?</%s>" % (tag, tag), "", r.html, flags=re.S)
    return sum(c.isalnum() for c in strip_tags(rest)) >= 3


def continues(fr: _Frame, r: Row, rows: list[Row], body_size: float, ex: dict, cache: dict) -> bool:
    p = fr.rows[-1]
    if p.in_table or r.in_table or p.in_image != r.in_image:
        return False
    if not size_match(p.size, r.size, ex):
        return False
    if p.italic != r.italic:
        # 斜体が行の一部だけ (相互参照などが斜体で、残りは通常字) の行は、斜体の優勢/非優勢で段落を割らない
        mixed = _styled_partly(r if r.italic else p, "i")
        if not (mixed and len(r.text) <= 80 and x_overlap(p.x0, p.x1, r.x0, r.x1) > 0):
            return False
    if p.bold != r.bold:
        # 「Fig. 4 | タイトル. a 本文…」のように 1 行目だけ太字の見出し語 + 通常字の混在行で、2 行目以降が通常字のとき
        cap_first = len(fr.rows) == 1 and p.bold and not r.bold and CAPTION_START_RE.match(p.text)
        part = _styled_partly(r if r.bold else p, "b") and min(len(r.text), len(p.text)) <= 80 and x_overlap(p.x0, p.x1, r.x0, r.x1) > 0 and r.bold != p.bold and not (r.bold and not p.bold and len(r.text) > 80)
        if not (cap_first or part):
            return False
    size = r.size
    dy = r.base - p.base
    if not (0.55 * size <= dy <= _p(ex, "para_max_pitch") * size):
        return False
    if x_overlap(p.x0, p.x1, r.x0, r.x1) < 0.5 * min(p.x1 - p.x0, r.x1 - r.x0):
        return False
    left = fr.left
    indent_thr = _p(ex, "para_indent_factor") * size
    first = fr.rows[0]
    hanging = False
    if LIST_LABEL_RE.match(first.text) and r.x0 > left + indent_thr:
        # 番号・記号つきの項目: ラベルが外に出て、2 行目以降が本文の位置まで下がる (ぶら下げインデント)
        hang_x = fr.rows[1].x0 if len(fr.rows) >= 2 else None
        hanging = (0.8 * size <= r.x0 - first.x0 <= 3.5 * size) if hang_x is None else abs(r.x0 - hang_x) <= 0.6 * size + 1.0
    if LIST_LABEL_RE.match(first.text) and LIST_LABEL_RE.match(r.text):
        return False          # 次の項目の先頭 (ラベルが外に出た行): 新しい項目
    if r.x0 > left + indent_thr and not hanging:
        return False  # 字下げ -> 新段落
    if r.x0 < left - 0.5 * size:
        # 1行目が字下げされていて2行目が左マージンへ戻る場合のみ
        if not (len(fr.rows) == 1 and 0.8 * size <= left - r.x0 <= 3.5 * size):
            return False
    if r.first_script and fr.rows[0].first_script:
        return False
    if CAPTION_STRICT.match(r.text):
        return False
    if BULLET_START_RE.match(r.text):
        return False          # 箇条書きの項目は 1 つずつ別の frame (段落に連結しない)
    if LABEL_START.match(r.text) and size <= 0.92 * body_size:
        return False
    # 前行が短い -> 段落末 (見出し等の大きい字・ragged な列では判定しない)
    if size < 1.15 * body_size:
        key = id(p)
        if key not in cache:
            cache[key] = _colmax(p, rows, ex)
        cm, ragged = cache[key]
        right = max(fr.right, cm)
        if not ragged and p.x1 < right - _p(ex, "short_line_factor") * size:
            if not (p.text.rstrip()[-1:] in HYPHENS) and not r.text[:1].islower():
                return False
    return True


def group_rows(rows: list[Row], body_size: float, ex: dict) -> list[list[Row]]:
    frames: list[_Frame] = []
    cache: dict = {}
    by_page: dict[int, list[Row]] = collections.defaultdict(list)
    for r in rows:
        by_page[r.page].append(r)
    for r in rows:
        best, best_dy = None, 1e9
        for fr in frames:      # 枠の中に収まる行 (∑ の上下の添え字の行など。基線が前の行と近く、別の frame になってしまう) は、その frame の続き
            if fr.rows[0].page == r.page and len(fr.rows) >= 2 and r.size <= fr.rows[0].size * 1.05 \
                    and r.y0 >= min(x.y0 for x in fr.rows) - 1.0 and r.y1 <= max(x.y1 for x in fr.rows) + 1.0 \
                    and r.x0 >= min(x.x0 for x in fr.rows) - 1.0 and r.x1 <= max(x.x1 for x in fr.rows) + 1.0 \
                    and not r.in_table and not r.in_image and fr.rows[-1].base < r.base + 6.0 \
                    and len(re.findall(r"[A-Za-z]{3,}", r.text)) >= 3:      # 文章を含む行だけ (純粋な数式の断片は別のまま)
                best, best_dy = fr, 0.0
                break
        if best is not None:
            best.rows.append(r)
            continue
        for fr in frames:
            p = fr.rows[-1]
            if p.page != r.page:
                continue
            dy = r.base - p.base
            if dy <= 0 or dy >= best_dy:
                continue
            if continues(fr, r, by_page[r.page], body_size, ex, cache):
                best, best_dy = fr, dy
        if best is not None:
            best.rows.append(r)
        else:
            frames.append(_Frame(r))
    return [fr.rows for fr in frames]


# --------------------------------------------------------------------------
# role 判定
# --------------------------------------------------------------------------

TRANSLATE_ROLES = {"title", "heading", "body", "abstract", "caption", "footnote", "sidebar", "keywords"}
ALL_ROLES = TRANSLATE_ROLES | {"author", "table", "figure_text", "math", "reference", "page_header",
                               "page_number", "doi_url"}

DOI_RE = re.compile(r"^(https?://\S+|doi:\s*\S+|www\.\S+)$", re.I)
PAGENUM_RE = re.compile(r"^\s*(\d+|\d+\s+of\s+\d+|page\s+\d+(\s+of\s+\d+)?)\s*$", re.I)
ARTICLE_TYPE_RE = re.compile(
    r"^(research\s*article|article|review(\s*article)?|short\s*communication|original\s*(research|article|paper)|"
    r"letter|perspective|case\s*report|brief\s*report|open(?:\s*access)?|articles?\s+open\s+access)$", re.I)
ABSTRACT_HEAD_RE = re.compile(r"^(abstract|summary)\s*[.:]?$", re.I)
ABSTRACT_RUNIN_RE = re.compile(r"^(abstract|summary)\s*[—–:.\-]+\s*\S", re.I)
KEYWORDS_RE = re.compile(r"^(k\s*e\s*y\s*w\s*o\s*r\s*d\s*s|index\s+terms)\b\s*[:—–\-]?", re.I)
REF_HEAD_RE = re.compile(r"^\s*(\d+[.\s]*)?(references?|bibliography|literature\s+cited|works\s+cited)\s*:?\s*$", re.I)
AFTER_REF_HEAD_RE = re.compile(
    r"^\s*(supporting\s+information|supplementary|supplemental|appendix|appendices|acknowledg|funding|conflicts?\s+of|"
    r"competing\s+interests?|data\s+availability|author\s+contributions?|ethics|peer\s+review|orcid|disclaimer|"
    r"publisher.?s\s+note|declaration)", re.I)
DATE_META_RE = re.compile(r"^(Received|Revised|Accepted|Published|Academic\s+Editor)\b", re.I)
HEADING_NUM_RE = re.compile(r"^\d+(\.\d+)*\.?\s")
SECTION_START_RE = re.compile(r"^(\d+(\.\d+)*\.?\s*\|?\s*|[IVX]+\.\s+)[A-Z][A-Za-z ,:-]{2,}")
AFFIL_RE = re.compile(r"\b(University|Universit[éày]|Institute|Department|School|Laborator(?:y|ies)|College|Hospital|"
                      r"Center|Centre|Faculty|Academy|Corporation|Inc\.|Ltd\.|GmbH)\b")
YEAR_RE = re.compile(r"\b(1[89]|20)\d\d[a-z]?\b")
_JOURNALISH = re.compile(r"(\bpp?\.|\bvol\.?|\bdoi\b|\bet al\.|\bJ\.|\bProc\.|\bTrans\.|\bRev\.|\d+\(\d+\)|\d+:\d+|arXiv)", re.I)
_NUMBERED_START = re.compile(r"^(\[\d+\]|\d{1,3}[.)]\s)")
_AUTHOR_START = re.compile(r"^[A-Z][A-Za-z'’-]+,\s+(?:[A-Z]\.|[A-Z][a-z]+)")


@dataclass
class Frame:
    id: str
    page: int
    bbox: list[float]
    rows: list[list[float]]
    text: str
    html: str
    size: float
    bold: bool
    italic: bool
    serif: bool
    color: int
    font: str
    align: str
    nrows: int
    first_indent: bool = False
    role: str = "body"
    translate: bool = True
    flags: list[str] = field(default_factory=list)
    math_ratio: float = 0.0
    in_table: bool = False
    in_image: bool = False
    row_texts: list[str] = field(default_factory=list)   # 表のセル・図の中の文字の frame のみ: 行ごとの文字列 (hover 注釈・表の用語の収集に使う)

    def to_json(self) -> dict:
        d = {k: getattr(self, k) for k in (
            "id", "page", "bbox", "rows", "text", "html", "size", "bold", "italic", "serif", "color", "font",
            "align", "nrows", "first_indent", "role", "translate", "flags", "math_ratio", "in_table", "in_image")}
        d["bbox"] = [round(v, 2) for v in d["bbox"]]
        d["rows"] = [[round(v, 2) for v in r] for r in d["rows"]]
        d["size"] = round(d["size"], 2)
        d["math_ratio"] = round(d["math_ratio"], 3)
        if self.row_texts:
            d["row_texts"] = list(self.row_texts)
        return d


def _align(rows: list[Row]) -> str:
    if len(rows) >= 3:
        right = max(r.x1 for r in rows)
        full = sum(1 for r in rows[:-1] if r.x1 >= right - 2.0)
        if full >= 0.8 * (len(rows) - 1):
            return "justify"
    if len(rows) >= 2:
        cs = [(r.x0 + r.x1) / 2 for r in rows]
        if max(cs) - min(cs) < 3 and max(r.x0 for r in rows) - min(r.x0 for r in rows) > 6:
            return "center"
    return "left"


def make_frame(fid: str, rows: list[Row], vocab: Vocab) -> Frame:
    bbox = [min(r.x0 for r in rows), min(r.y0 for r in rows), max(r.x1 for r in rows), max(r.y1 for r in rows)]
    w = lambda r: max(r.nchars, 1)  # noqa: E731
    size = _dominant([(round(r.size, 1), w(r)) for r in rows])
    html = join_lines([r.html for r in rows], vocab)
    bold = sum(w(r) for r in rows if r.bold) >= 0.6 * sum(w(r) for r in rows)
    if bold:  # frame 全体が太字なら <b> は冗長 (CSS の font-weight で出る)
        html = re.sub(r"</?b>", "", html)
    text = strip_tags(html)
    left = min(r.x0 for r in rows)
    return Frame(
        id=fid, page=rows[0].page, bbox=bbox, rows=[list(r.bbox) for r in rows], text=text, html=html,
        size=float(size), bold=bold,
        italic=sum(w(r) for r in rows if r.italic) >= 0.6 * sum(w(r) for r in rows),
        serif=sum(w(r) for r in rows if r.serif) >= 0.5 * sum(w(r) for r in rows),
        color=_dominant([(r.color, w(r)) for r in rows]) or 0, font=_dominant([(r.font, w(r)) for r in rows]) or "",
        align=_align(rows), nrows=len(rows),
        first_indent=(rows[0].x0 - left) > 0.8 * rows[0].size and len(rows) > 1,
        math_ratio=sum(r.math_ratio * w(r) for r in rows) / sum(w(r) for r in rows),
        in_table=all(r.in_table for r in rows), in_image=all(r.in_image for r in rows),
        row_texts=[r.text for r in rows] if all(r.in_table or r.in_image for r in rows) else [],
    )


_MONTH_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)
_ABBR_TAIL_RE = re.compile(r"^(.*?)\s*\(([A-Za-z][A-Za-z0-9/\-]{0,7})\)$")
_NUMERICISH_RE = re.compile(r"^[\d\s.,%±+\-\u2212\u2013:/()=<>]+$")


def _term_from_text(text: str) -> dict | None:
    """表のセル/キャプションの文字列から、本文でも英語を残したい「用語」(カテゴリ名・項目名・略語つきの語・列見出し) を取り出す。
    数値・日付・単位・文 (文末ピリオド) は用語にしない。"""
    t = re.sub(r"\s+", " ", text).strip().strip(":;,")
    if not t or t[0] == "(" or len(t) > 48 or len(t) < 2 or _NUMERICISH_RE.match(t):
        return None
    if sum(c.isalpha() for c in t) < 2 or sum(c.isdigit() for c in t) >= 2 or _MONTH_RE.search(t):
        return None
    words = t.split()
    if len(words) > 5 or (t[-1] in ".。" and len(words) >= 3):
        return None
    m = _ABBR_TAIL_RE.match(t)
    en, abbr = (m.group(1).strip(), m.group(2)) if m and m.group(1).strip() else (t, "")
    if not en or not re.search(r"[A-Za-z]{2}", en) or (len(en.split()) == 1 and len(en) <= 3 and not abbr):
        return None     # 単位・記号 (min, Hz, ms など)
    return {"en": en, "abbr": abbr}


def collect_table_terms(frames: list[Frame], max_terms: int = 60) -> list[dict]:
    """表のセル (role=table の frame の各行) と、図表キャプション中の斜体・引用の英語から、用語の一覧を作る。
    [{"en", "abbr", "src": "table"|"caption", "page"}]。数値・日付・単位・文は含めない。"""
    out: list[dict] = []
    seen: set[str] = set()

    def add(text: str, src: str, page: int) -> None:
        tm = _term_from_text(text)
        if not tm:
            return
        if src == "caption" and not tm["en"][:1].isupper():
            return          # キャプション中の小文字の語 (test, retest など) は頻出の一般語なので対象にしない
        k = tm["en"].lower()
        if k in seen:
            if tm["abbr"]:   # 「Social」(グループ見出し) の後に「Social (So)」が来たら略語を足す
                for o in out:
                    if o["en"].lower() == k and not o["abbr"]:
                        o["abbr"] = tm["abbr"]
            return
        if len(out) < max_terms:
            seen.add(k)
            out.append({**tm, "src": src, "page": page})

    for f in frames:
        if f.role == "table":
            for t in f.row_texts or [f.text]:
                add(t, "table", f.page)
        elif f.role == "caption":
            for m in re.finditer(r"<i>([^<]{3,40})</i>", f.html):
                if re.search(r"[A-Za-z]{3}", m.group(1)) and not re.search(r"et al", m.group(1)):
                    add(m.group(1), "caption", f.page)
            for m in re.finditer(r"[\u201c\"]([A-Za-z][^\u201d\"]{2,38})[\u201d\"]", f.text):
                add(m.group(1), "caption", f.page)
    return out


def _norm_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text.lower())).strip()


def _alpha(s: str) -> int:
    return sum(c.isalpha() for c in s)


def compact_up(s: str) -> str:
    return re.sub(r"\s+", "", s).upper()


def body_size_of(rows: list[Row]) -> float:
    c: collections.Counter = collections.Counter()
    for r in rows:
        c[round(r.size * 2) / 2] += r.nchars
    return c.most_common(1)[0][0] if c else 10.0


def is_doi_url(text: str, nrows: int, y0: float, h: float) -> bool:
    """DOI/URL だけの行 (ヘッダ/フッタ・リンク欄)。URL を含むだけの脚注文は含めない。"""
    text = text.strip()
    if DOI_RE.match(text) or "orcid.org" in text:
        return True
    if URL_RE.search(text) and len(text) < 120 and nrows <= 3 and y0 > 0.85 * h:
        rest = URL_RE.sub("", text)
        if len(re.findall(r"[A-Za-z]{3,}", rest)) <= 3:
            return True
    if re.match(r"^[\w.-]+\.[a-z]{2,}(/\S*)?$", text, re.I) and y0 > 0.85 * h:
        return True
    tail = text.split(":", 1)[-1].strip()
    if ("doi.org" in text.lower() or text.upper().startswith("DOI:")) and len(text) < 90 and nrows <= 2 \
            and " " not in tail:
        return True
    return False


def looks_like_equation(text: str, nrows: int) -> bool:
    """独立した数式行。"where x = 3 in this case" のような短い本文は含めない。"""
    if nrows > 2 or len(text) > 140:
        return False
    if re.search(r"\s[~∼]\s", text):
        return True
    if nrows == 1 and len(text) < 60 and re.search(r"\s[=≈]\s", text):
        words = re.findall(r"\b[a-z]{4,}\b", text)
        return len(words) <= 2 and not re.search(r"\b(the|and|where|is|are|in|of|for|with)\b", text)
    return False


def is_entry_like(text: str) -> bool:
    """参考文献の項目らしさ ([n]/n. で始まり年・雑誌名/巻号、または 著者, 名. + 年)。"""
    t = text.strip()
    has_year = bool(YEAR_RE.search(t))
    journal = bool(_JOURNALISH.search(t))
    if _NUMBERED_START.match(t) and (has_year or journal):
        return True
    if _AUTHOR_START.match(t) and has_year:
        return True
    return has_year and journal and len(t) > 40


_FUNC_WORDS = frozenset("the and of in to we is are was were that this with for as by on from which be it an at or not "
                        "have has had can may these those their our results showed shows using used".split())


def prose_density(text: str) -> float:
    """機能語 (the, and, of ...) の割合。文章 (要旨・本文) は高く、著者名・所属・参考文献は低い。"""
    words = re.findall(r"[A-Za-z']+", text.lower())
    return sum(1 for w in words if w in _FUNC_WORDS) / len(words) if words else 0.0


def _authorish(f: Frame) -> bool:
    """著者・所属行らしさ: 機能語の多い長い文章、文末ピリオドで終わる長文は除外。"""
    t = f.text.strip()
    if f.nrows > 10 or len(t) > 600:
        return False
    if len(t.split()) >= 25 and t.endswith(".") and prose_density(t) >= 0.15:
        return False
    if len(t) >= 100 and prose_density(t) >= 0.2:
        return False
    return True


def _intro_stop_y(cands: list[Frame], body_size: float) -> float | None:
    for f in sorted(cands, key=lambda f: f.bbox[1]):
        t = f.text.strip()
        if ABSTRACT_HEAD_RE.match(t) or ABSTRACT_RUNIN_RE.match(t) or KEYWORDS_RE.match(t):
            return f.bbox[1]
        if SECTION_START_RE.match(t) and f.nrows <= 2 and (f.bold or f.size >= 1.0 * body_size) and len(t) <= 100:
            return f.bbox[1]
        if f.nrows >= 4 and len(t) >= 300 and not AFFIL_RE.search(t[:200]):
            return f.bbox[1]
    return None


def find_authors(p1: list[Frame], title: Frame | None, body_size: float, page_h: float) -> set[str]:
    """タイトル直後〜最初の見出し/要旨までの、人名・所属らしい短い frame の id。"""
    if title is None:
        return set()
    cands = [f for f in p1 if f is not title and f.bbox[1] >= title.bbox[3] - 2 and not f.in_table and not f.in_image]
    stop = _intro_stop_y(cands, body_size)
    limit = stop if stop is not None else 0.40 * page_h
    out = set()
    for f in cands:
        t = f.text.strip()
        if f.bbox[1] >= limit:
            continue
        if CAPTION_START.match(t) or DATE_META_RE.match(t) or ABSTRACT_HEAD_RE.match(t) or KEYWORDS_RE.match(t):
            continue
        if f.size > title.size or not _authorish(f):
            continue
        out.add(f.id)
    return out


def assign_roles(frames: list[Frame], pages: list[dict], body_size: float, ex: dict) -> None:
    npages = len(pages)
    H = {p["number"]: p["height"] for p in pages}
    min_rep = _p(ex, "repeat_min_pages")
    zone = _p(ex, "header_zone")
    seen: dict[str, set] = collections.defaultdict(set)
    for f in frames:
        seen[_norm_key(f.text)].add(f.page)
    p1 = [f for f in frames if f.page == 1]
    cand = [f for f in p1 if f.size >= 1.3 * body_size and not f.in_table and _alpha(f.text) >= 3
            and len(seen[_norm_key(f.text)]) < max(min_rep, 2)]
    title = max(cand, key=lambda f: (f.size, -f.bbox[1])) if cand else None
    abs_head = next((f for f in p1 if ABSTRACT_HEAD_RE.match(f.text.strip())), None)
    abs_left = abs_head.bbox[0] if abs_head else None
    author_ids = find_authors(p1, title, body_size, H.get(1, 842.0))
    W1 = pages[0]["width"] if pages else 595.0
    side_right = _left_sidebar_edge(p1, body_size, W1)

    for f in frames:
        text = f.text.strip()
        h = H[f.page]
        in_zone10 = f.bbox[3] < 0.1 * h or f.bbox[1] > 0.9 * h
        in_zone = f.bbox[3] < zone * h or f.bbox[1] > (1 - zone) * h
        if f.in_table:
            role = "table"
        elif f.in_image:
            role = "figure_text"
        elif (is_monospace_font(f.font) and not pages[f.page - 1].get("scanned_ocr")) or looks_like_algorithm(text, f.nrows):
            role = "figure_text"     # コード・疑似コードは翻訳しない
        elif title is not None and f is title:
            role = "title"
        elif is_doi_url(text, f.nrows, f.bbox[1], h):
            role = "doi_url"
        elif len(seen[_norm_key(text)]) >= min_rep and in_zone10:
            role = "page_number" if PAGENUM_RE.match(text) else "page_header"
        elif npages < min_rep and in_zone and f.size <= 1.1 * body_size and f.nrows <= 2 and len(text) <= 120:
            # 少ページ文書: 反復が使えないので 位置 (上下 6%) + サイズ (本文以下) + 短さ で判定
            role = "page_number" if PAGENUM_RE.match(text) else "page_header"
        elif PAGENUM_RE.match(text) and in_zone10:
            role = "page_number"
        elif f.page == 1 and ARTICLE_TYPE_RE.match(re.sub(r"\s+", " ", text)) and f.bbox[1] < 0.2 * h:
            role = "page_header"
        elif f.page == 1 and f is not title and f.bbox[3] < 0.08 * h and f.nrows <= 2 and len(text) <= 60 \
                and not re.search(r"[.!?]\s*$", text) and f.size > body_size:
            role = "page_header"    # 誌名・マストヘッド (1 ページ目の最上部の帯の、本文より大きい短い文字。タイトルは除く)
        elif DATE_META_RE.match(text) and f.size <= body_size:
            role = "sidebar"
            f.flags.append("date")
        elif f.page == 1 and abs_left is not None and f.bbox[2] <= abs_left - 5 and f.size <= body_size + 0.6 \
                and title is not None and f.bbox[1] > title.bbox[3]:
            role = "sidebar"
        elif abs_left is None and side_right is not None and f.page == 1 and f.bbox[2] <= side_right and f.size <= body_size + 0.6 \
                and f is not title and (f.bbox[2] - f.bbox[0]) <= 0.27 * W1:
            role = "sidebar"        # 1 ページ目の左の細い列 (eLife のメタ情報欄): ラベルだけ辞書で訳し、本文は英語のまま
            f.flags.append("sidebar_col")
        elif f.id in author_ids:
            role = "author"
        elif f.page == 1 and ABSTRACT_RUNIN_RE.match(text):
            role = "abstract"
        elif ABSTRACT_HEAD_RE.match(text) or compact_up(text) == "KEYWORDS":
            role = "heading"
        elif KEYWORDS_RE.match(text):
            role = "keywords"
        elif CAPTION_STRICT.match(text):
            role = "caption"
        elif (f.size >= 1.12 * body_size or (f.bold and f.nrows <= 2 and len(text) <= 120 and f.size >= 0.95 * body_size)
              or (f.italic and f.nrows <= 2 and len(text) <= 100 and HEADING_NUM_RE.match(text))) and len(text) <= 160:
            role = "heading"
        elif (f.math_ratio >= 0.5 and len(text) <= 80) or (f.math_ratio >= 0.3 and len(text) <= 40 and f.nrows <= 2)                 or (len(text) <= 60 and f.nrows <= 2 and re.search("[ðÞ¼½]", text) and f.math_ratio > 0):
            role = "math"
        elif re.search("[ﬀ-ﬆ]{6,}", text):
            role = "math"  # 根号の横棒などが合字 (ﬃ) の連なりとして抽出されたもの (数式の一部)
        elif looks_like_equation(text, f.nrows):
            role = "math"  # 独立した数式行 (例: Response ~ 1 + stimulus type + ...)
        elif _alpha(text) < max(3, 0.3 * len(text)) and len(text) <= 60:
            role = "math"
        elif f.size <= 0.72 * body_size and f.nrows <= 1 and len(text) <= 40 and f.bbox[1] <= 0.82 * h \
                and not pages[f.page - 1].get("scanned_ocr"):
            role = "figure_text"     # 本文よりずっと小さい (7 割以下) 短い 1 行は、図の凡例・軸ラベル・群の名前 (本文の中には現れない大きさ)
        elif f.size <= 0.92 * body_size and f.bbox[1] > 0.82 * h and f.nrows <= 8:
            role = "footnote"
        else:
            role = "body"
        f.role = role

    # --- 数式行の bbox に収まる断片 (括弧など) も math にする
    maths = [m for m in frames if m.role == "math" and m.nrows >= 1 and len(m.text) > 20]
    for f in frames:
        if f.role in ("body", "math"):
            cx, cy = (f.bbox[0] + f.bbox[2]) / 2, (f.bbox[1] + f.bbox[3]) / 2
            for m in maths:
                if (m is not f and m.page == f.page and m.bbox[0] - 3 <= cx <= m.bbox[2] + 3
                        and m.bbox[1] - 3 <= cy <= m.bbox[3] + 3 and f.nrows <= 2):
                    f.role = "math"
                elif (m is not f and m.page == f.page and f.nrows <= 2 and len(f.text) <= 40
                        and (f.math_ratio >= 0.1 or _alpha(f.text) <= 8)
                        and m.bbox[0] - 40 <= cx <= m.bbox[2] + 40 and m.bbox[1] - 8 <= cy <= m.bbox[3] + 8):
                    f.role = "math"      # 数式の上下左右にある、短い断片 (添え字・上下の項・括弧など)
    _assign_states(frames, abs_head, body_size)
    _fix_headings(frames, body_size)
    _fix_ocr_headings(frames, pages)
    _fix_caption_continuations(frames)


KNOWN_HEADING_RE = re.compile(
    r"^(?:\d+\.?\s*)?(abstract|summary|introduction|background|methods?|materials? and methods?|results?|results and discussion|"
    r"discussion|conclusions?|limitations?|acknowledg(?:e)?ments?|references|funding|appendix|data availability(?: statement)?|"
    r"author contributions?|competing interests?|keywords|e?life assessment|supplementary (?:information|materials?)|"
    r"eLife assessment)\s*[.:]?$", re.I)
NUMBERED_HEAD_RE = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[A-Z]\.|[IVX]+\.)\s+[A-Z]")


SHORT_HEAD_RE = re.compile(r"^(class|experiment|series|group|case|section|part|stage|phase|type|mode|method)\s+([IVX]+|\d+|[A-Z])\s*[.:]?$", re.I)


def _fix_ocr_headings(frames: list["Frame"], pages: list[dict]) -> None:
    """スキャン + OCR ページには太字・サイズの手がかりが無い (OCR の文字は本文と同じ見かけ) ので、既知の見出し語 (RESULTS など) と
    「Class I」のような短い見出しを 1 行だけの frame から見出しにする。"""
    for f in frames:
        if not pages[f.page - 1].get("scanned_ocr") or f.role != "body":
            continue
        t = re.sub(r"\s+", " ", f.text.strip())
        if f.nrows <= 1 and len(t) <= 30 and (KNOWN_HEADING_RE.match(t) or SHORT_HEAD_RE.match(t)):
            f.role = "heading"
        elif OCR_TABLE_TITLE_RE.match(t) and not f.in_table:
            f.role = "caption"        # OCR が TABLE を読み違えた表題 (TABTE 1. など)


def _left_sidebar_edge(p1: list["Frame"], body_size: float, W: float) -> float | None:
    """1 ページ目で、本文の列の左に細い (幅 25% 以下の) 列があれば、その右端 (本文の列の左端 - 5)。無ければ None。"""
    main = [f for f in p1 if f.nrows >= 4 and abs(f.size - body_size) <= 0.6 and (f.bbox[2] - f.bbox[0]) >= 0.4 * W]
    if len(main) < 2:
        return None
    left = min(f.bbox[0] for f in main)
    return left - 5.0 if left >= 0.18 * W else None


def _fix_headings(frames: list["Frame"], body_size: float) -> None:
    """見出しの誤判定を直す。本文と同じ大きさ (太字だけ) の短い行が見出しになるのは、番号つき・既知の見出し語のときだけ。
    直前の段落の続き (直前の行が文末記号で終わっていない・小文字や ; で始まる断片) は本文に戻す。
    既知の見出し語 (eLife assessment など) は、author / body などに誤判定されていても見出しにする。"""
    by_page: dict[int, list[Frame]] = collections.defaultdict(list)
    for f in frames:
        by_page[f.page].append(f)
    for f in frames:
        t = f.text.strip()
        if f.role == "heading" and BULLET_START_RE.match(t) and len(t) > 50:
            f.role = "body"          # 箇条書きの項目 (Highlights など。太字でも見出しではない)
            continue
        if f.role in ("author", "body", "sidebar", "abstract") and f.nrows <= 1 and KNOWN_HEADING_RE.match(t) \
                and (f.bold or f.size >= 1.12 * body_size):
            f.role = "heading"
            continue
        if f.role != "heading" or f.size >= 1.12 * body_size:
            continue
        if NUMBERED_HEAD_RE.match(t) or KNOWN_HEADING_RE.match(t):
            continue
        prev = None
        for g in reversed(by_page[f.page][:by_page[f.page].index(f)]):
            if g.role in ("page_header", "page_number", "figure_text", "doi_url"):
                continue
            prev = g
            break
        cont = t[:1].islower() or bool(re.match(r"^\d{4}[;,)]", t)) or t.endswith((",", ";"))
        if prev is not None and prev.role in ("body", "abstract", "caption", "footnote") and abs(prev.bbox[0] - f.bbox[0]) <= 8 \
                and 0 <= f.bbox[1] - prev.bbox[3] <= 1.6 * f.size and not re.search(r"[.?!:\u3002][\"')\]]*\s*$", prev.text.strip()):
            cont = True
        if cont:
            f.role = "body"


def _fix_caption_continuations(frames: list["Frame"]) -> None:
    """キャプションの続きの行 (同じフォント・サイズで、キャプションの直下、または次の段の先頭) が footnote/body に分かれたものを caption にする。"""
    for i, c in enumerate(frames):
        if c.role != "caption":
            continue
        cur = c
        for g in frames[i + 1:i + 6]:
            if g.page != c.page or g.role not in ("footnote", "body", "sidebar"):
                break
            same = abs(g.size - c.size) <= 0.3 and (g.font == c.font or (c.bold and not g.bold and CAPTION_START_RE.match(c.text)))
            below = abs(g.bbox[0] - cur.bbox[0]) <= 6 and 0 <= g.bbox[1] - cur.bbox[3] <= 1.3 * g.size
            nextcol = g.bbox[0] >= c.bbox[2] and abs(g.bbox[1] - c.bbox[1]) <= 4
            if same and (below or nextcol):
                g.role = "caption"
                cur = g
            else:
                break


_NON_REF_ROLES = {"page_header", "page_number", "doi_url", "figure_text", "table"}
_REF_CONVERT = {"body", "footnote", "caption", "math", "sidebar"}


def _is_prose(text: str, nrows: int) -> bool:
    return (nrows >= 4 and len(text) >= 250 and prose_density(text) >= 0.3
            and len(re.findall(r"\b[A-Z]\.", text)) < 3 and not YEAR_RE.search(text))


def _ref_start_by_pattern(frames: list[Frame]) -> int | None:
    """見出しが無い場合: [n]/n. で始まる項目らしい frame が 3 つ連続する先頭の index。"""
    run = []
    for i, f in enumerate(frames):
        if f.role in _NON_REF_ROLES:
            continue
        t = f.text.strip()
        if f.role in ("body", "footnote", "math") and _NUMBERED_START.match(t) and is_entry_like(t):
            run.append(i)
            if len(run) >= 3:
                return run[0]
        else:
            run = []
    return None


def _assign_states(frames: list[Frame], abs_head: Frame | None, body_size: float) -> None:
    """abstract / keywords 本文 / reference の状態遷移。"""
    has_ref_head = any(f.nrows <= 2 and len(f.text) <= 40 and REF_HEAD_RE.match(f.text.strip())
                       and f.role not in _NON_REF_ROLES for f in frames)
    pat_start = None if has_ref_head else _ref_start_by_pattern(frames)
    abstract_page = abs_head.page if abs_head else None
    after_ref = False
    in_abstract = False
    for i, f in enumerate(frames):
        text = f.text.strip()
        if f.role in _NON_REF_ROLES:
            continue
        is_refhead = f.nrows <= 2 and len(text) <= 40 and bool(REF_HEAD_RE.match(text))
        if is_refhead:
            f.role = "heading"
            after_ref = True
            in_abstract = False
            continue
        if pat_start is not None and i == pat_start:
            after_ref = True
        if f.role == "heading" and not after_ref:
            in_abstract = bool(ABSTRACT_HEAD_RE.match(text))
            if compact_up(text) == "KEYWORDS":
                nxt = next((g for g in frames[i + 1:] if g.page == f.page), None)
                if nxt is not None and nxt.role == "body":
                    nxt.role = "keywords"
            continue
        if after_ref:
            if is_entry_like(text):
                f.role = "reference"
                continue
            if f.role == "heading" or AFTER_REF_HEAD_RE.match(text):
                after_ref = False  # 付録/謝辞/Disclaimer など参考文献の後ろの節
            elif _is_prose(text, f.nrows):
                after_ref = False  # 項目らしくない長い文章 (機能語が多く、著者のイニシャルが少ない)
            elif f.role in _REF_CONVERT:
                f.role = "reference"
                continue
            else:
                continue
        if f.role == "heading":
            in_abstract = bool(ABSTRACT_HEAD_RE.match(text))
            continue
        if f.role == "keywords":
            in_abstract = False
        if in_abstract and f.page == abstract_page and f.role == "body":
            f.role = "abstract"


def translate_flag(role: str, text: str, flags: list[str], translate_tables: bool = False) -> bool:
    ok = role in TRANSLATE_ROLES or (translate_tables and role == "table")
    if not ok or _alpha(text) < 2 or "date" in flags or "identifier" in flags or "sidebar_col" in flags:
        return False
    if role == "sidebar" and re.match(r"^Academic\s+Editor", text):
        return False
    return True


def is_identifier_frame(html: str) -> bool:
    """保護後にほぼ識別子 (メール/URL/DOI/助成番号/数式片) しか残らない frame。"""
    ph, vars_ = protect_html(html)
    if not vars_:
        return False
    core = VAR_RE.sub("", strip_tags(ph))
    return _alpha(core) < 3


def finalize_translate(frames: list[Frame], cfg: Config) -> None:
    ex = cfg.section("extract")
    tt = bool(_p(ex, "translate_tables"))
    prot = bool(_p(ex, "protect_inline"))
    for f in frames:
        f.flags = [x for x in f.flags if x != "identifier"]
        if prot and f.role in TRANSLATE_ROLES and is_identifier_frame(f.html):
            f.flags.append("identifier")
        f.translate = translate_flag(f.role, f.text, f.flags, tt)


# --------------------------------------------------------------------------
# joins (段/ページまたぎの結合候補) と検証
# --------------------------------------------------------------------------
JOIN_ROLES = {"body", "abstract"}
PASS_ROLES = {"caption", "figure_text", "page_header", "page_number", "doi_url", "footnote", "table", "math", "sidebar"}
TERMINATORS = ".?!:;"


def ends_sentence(text: str) -> bool:
    t = text.rstrip().rstrip(")]\"'”’»")
    return t[-1:] in TERMINATORS if t else True


def find_joins(frames: list[Frame], ex: dict) -> list[list[str]]:
    joins = []
    last: Frame | None = None
    for f in frames:
        if f.role in PASS_ROLES:
            continue
        if f.role not in JOIN_ROLES:
            last = None
            continue
        if last is not None and last.role == f.role and size_match(last.size, f.size, ex):
            lowered = f.text[:1].islower()
            unfinished = not ends_sentence(last.text) or last.text.rstrip()[-1:] in HYPHENS
            indented = f.rows and (f.rows[0][0] - min(r[0] for r in f.rows)) > 0.8 * f.size and f.nrows > 1
            if (lowered or unfinished) and not indented:
                joins.append([last.id, f.id])
        last = f
    return joins


def _adjacent_same_column(a: dict, b: dict) -> bool:
    """同じページ・同じ列で、縦に隣接 (行間 1.5 行以内) または重なっている 2 つの frame か。
    ドロップキャップなどで抽出の順序が逆になっていても、Claude の結合 (読み順の誤りの訂正) を採用してよい条件。"""
    if a["page"] != b["page"]:
        return False
    ab, bb = a["bbox"], b["bbox"]
    w = min(ab[2] - ab[0], bb[2] - bb[0])
    if min(ab[2], bb[2]) - max(ab[0], bb[0]) < 0.5 * max(w, 1.0):
        return False
    gap = max(ab[1], bb[1]) - min(ab[3], bb[3])
    return gap <= 1.5 * max(a.get("size", 10.0), b.get("size", 10.0))


def validate_joins(frames: dict[str, dict], order: list[str], joins: list[list[str]]) -> tuple[list[list[str]], list[str]]:
    """joins を検証し、(有効な joins, 警告) を返す。
    拒否: 存在しない id / 自己結合 / 非翻訳 frame / ページ逆行・読み順逆行 / 重複 (a が 2 回、b が 2 回) / 循環。"""
    pos = {fid: i for i, fid in enumerate(order)}
    ok: list[list[str]] = []
    warns: list[str] = []
    nxt: dict[str, str] = {}
    prv: dict[str, str] = {}
    for j in joins:
        if not (isinstance(j, (list, tuple)) and len(j) == 2):
            warns.append(f"join {j!r}: 形式が不正")
            continue
        a, b = j
        if a not in frames or b not in frames:
            warns.append(f"join {a}->{b}: 存在しない frame id")
        elif a == b:
            warns.append(f"join {a}->{b}: 自己結合")
        elif not (frames[a]["translate"] and frames[b]["translate"]):
            warns.append(f"join {a}->{b}: 翻訳対象でない frame を含む")
        elif frames[b]["page"] < frames[a]["page"] or (pos[b] < pos[a] and not _adjacent_same_column(frames[a], frames[b])):
            warns.append(f"join {a}->{b}: ページ/読み順が逆行")
        elif a in nxt or b in prv:
            warns.append(f"join {a}->{b}: 重複 (a か b が既に結合済み)")
        else:
            # 循環検査 (読み順の単調性で実際は起きないが念のため)
            c, cyc = b, False
            while c in nxt:
                c = nxt[c]
                if c == a:
                    cyc = True
                    break
            if cyc:
                warns.append(f"join {a}->{b}: 循環")
                continue
            nxt[a], prv[b] = b, a
            ok.append([a, b])
    return ok, warns


def apply_structure(doc: dict, roles: dict[str, str] | None = None, joins: list[list[str]] | None = None,
                    charmap: dict | None = None, cfg: Config | None = None) -> list[str]:
    """構造の上書き (M2: Claude の補正) を doc に適用する。警告のリストを返す。

    roles:   {frame_id: role}。不正な role / id は警告して無視。role 更新後に translate フラグを再計算する。
    joins:   [[a, b], ...]。validate_joins で検証し、有効なものだけを採用 (doc['joins'] を置換)。
    charmap: {font_prefix: {char: str}} または {char: str}。文字が変わるため doc['source'] から再抽出し、
             同じ id の roles/joins を引き継ぐ (doc を in-place で更新)。
    """
    cfg = cfg or load_config()
    warns: list[str] = []
    if charmap:
        prev_over = dict(doc.get("roles_override", {}))
        prev_joins = doc.get("joins") if doc.get("joins_source") == "override" else None
        new = extract_pdf(doc["source"], None, charmap=merge_charmap(doc.get("charmap"), charmap), cfg=cfg)
        doc.clear()
        doc.update(new)
        roles = {**prev_over, **(roles or {})}
        if joins is None:
            joins = prev_joins
    frames = {f["id"]: f for p in doc["pages"] for f in p["frames"]}
    order = [f["id"] for p in doc["pages"] for f in p["frames"]]
    if roles:
        for fid, r in roles.items():
            if fid not in frames:
                warns.append(f"role {fid}: 存在しない frame id")
            elif r not in ALL_ROLES:
                warns.append(f"role {fid}={r!r}: 未知の role")
            else:
                frames[fid]["role"] = r
                if "roles_override" not in doc:
                    doc["roles_override"] = {}
                doc["roles_override"][fid] = r
    _recompute_translate(doc, cfg)
    if joins is not None:
        ok, w = validate_joins(frames, order, joins)
        warns += w
        doc["joins"] = ok
        doc["joins_source"] = "override"
    else:
        ok, w = validate_joins(frames, order, doc.get("joins", []))  # roles 変更で無効化された joins を除く
        warns += w
        doc["joins"] = ok
    return warns


def _recompute_translate(doc: dict, cfg: Config) -> None:
    ex = cfg.section("extract")
    tt = bool(_p(ex, "translate_tables"))
    prot = bool(_p(ex, "protect_inline"))
    for p in doc["pages"]:
        for f in p["frames"]:
            flags = [x for x in f.get("flags", []) if x != "identifier"]
            if prot and f["role"] in TRANSLATE_ROLES and is_identifier_frame(f["html"]):
                flags.append("identifier")
            f["flags"] = flags
            f["translate"] = translate_flag(f["role"], f["text"], flags, tt)


# --------------------------------------------------------------------------
# メイン
# --------------------------------------------------------------------------

def open_pdf(pdf_path: str | Path) -> fitz.Document:
    """PDF を開く。壊れている/PDF でない -> PDFOpenError、パスワード付き -> EncryptedPDFError。"""
    try:
        doc = fitz.open(str(pdf_path))
    except FileNotFoundError:
        raise
    except Exception as e:  # fitz.FileDataError など
        raise PDFOpenError(f"PDF として開けません: {pdf_path} ({e})") from e
    if not doc.is_pdf:   # PyMuPDF は txt/epub/xps なども開けるが、対象は PDF だけ
        doc.close()
        raise PDFOpenError(f"PDF ではありません (PDF 以外のファイル形式): {pdf_path}")
    if not doc.is_pdf:   # PyMuPDF は txt/epub/xps なども開けるが、対象は PDF だけ
        doc.close()
        raise PDFOpenError(f"PDF ではありません (PDF 以外のファイル形式): {pdf_path}")
    if doc.needs_pass:
        doc.close()
        raise EncryptedPDFError(f"パスワードで保護された PDF です: {pdf_path}")
    return doc


@timed("extract")
def extract_pdf(pdf_path: str | Path, work_dir: str | Path | None = None, charmap: dict | None = None,
                cfg: Config | None = None) -> dict:
    """PDF を抽出して doc dict を返す。work_dir を与えると doc.json に保存する。

    charmap: 既定表に上書きマージされる追加表 ({font_prefix: {char: str}} または旧形式 {char: str})。
    """
    cfg = cfg or load_config()
    ex = cfg.section("extract")
    pdf_path = Path(pdf_path)
    eff_charmap = merge_charmap(None, charmap)
    unmapped: dict[str, dict[str, int]] = {}
    doc = open_pdf(pdf_path)
    all_rows: list[Row] = []
    pages_meta: list[dict] = []
    warnings: list[str] = []
    scan_min = _p(ex, "scan_min_chars")
    known_gutters: list[tuple[float, float]] = []
    for pno, page in enumerate(doc, start=1):
        imgs = page_images(page)
        ocr = is_ocr_page(page, imgs)
        bands: list[list[float]] = []
        if ocr:   # ページ全面のスキャン画像は「図」ではなく背景。図の領域は OCR の行が無い帯から推定する
            parea = page.rect.width * page.rect.height
            imgs = [i for i in imgs if (i[2] - i[0]) * (i[3] - i[1]) < 0.85 * parea]
        uris = page_uri_links(page)
        page_gutters: list = []
        rows = collect_rows(page, pno, cfg, eff_charmap, unmapped, uris, known_gutters, page_gutters)
        if not ocr:
            rows = merge_rows_across_inline_images(rows, imgs)
        if ocr:
            bands = ocr_figure_bands(rows, page.rect.width, page.rect.height)
            normalize_ocr_rows(rows, bands, page.rect.width)
            rows = merge_ocr_fragments(rows, page.rect.width)
            mark_ocr_tables(rows, page.rect.width)
        tables = find_table_regions(page, rows, ex)
        vcells = dense_vector_cells(page, min_paths=int(_p(ex, "vector_dense_min_paths")))
        vfigs = vector_figure_regions(page) if _p(ex, "vector_figures") else []
        # 本文の長い行 (60 字以上) を 3 本以上含む領域は、図ではなく装飾つきのテキスト枠 (要旨の囲み・ライセンス枠など)
        vfigs = [f for f in vfigs if sum(1 for r in rows if r.nchars >= 60 and f[0] <= (r.x0 + r.x1) / 2 <= f[2]
                                         and f[1] <= (r.y0 + r.y1) / 2 <= f[3]) < 3]
        for r in rows:
            b = r.bbox
            if vfigs and r.nchars <= 50 and not CAPTION_START_RE.match(r.text) and not any(_inside(b, t) for t in tables)                     and any(f[0] - 2 <= (b[0] + b[2]) / 2 <= f[2] + 2 and f[1] - 2 <= (b[1] + b[3]) / 2 <= f[3] + 2 for f in vfigs):
                r.in_image = True      # ベクタ図の中の短い文字 (軸ラベル・凡例など) は図の一部 (キャプションは除く)
            elif vfigs and r.nchars <= 30 and r.size <= 1.12 * doc_body_size_hint(rows) and not CAPTION_START_RE.match(r.text)                     and not any(_inside(b, t) for t in tables)                     and any(f[0] - 28 <= (b[0] + b[2]) / 2 <= f[2] + 8 and f[1] - 28 <= (b[1] + b[3]) / 2 <= f[3] + 26 for f in vfigs):
                r.in_image = True      # 図のすぐ上下にある短い軸ラベル・グループ名 (図形の外側に置かれることが多い)
            if vcells and r.size <= 1.05 * (doc_body_size_hint(rows)) and not any(_inside(b, t) for t in tables) \
                    and (int((b[0] + b[2]) / 2 // 36.0), int((b[1] + b[3]) / 2 // 36.0)) in vcells \
                    and r.nchars <= 80:
                r.in_image = True
            if any(_inside(b, [i[0] - 3, i[1] - 3, i[2] + 3, i[3] + 3]) for i in imgs):
                r.in_image = True
            elif not ocr and r.nchars <= 30 and not CAPTION_START_RE.match(r.text) and r.size <= 1.15 * doc_body_size_hint(rows) \
                    and any(_label_of_image(b, i, page.rect.width) for i in imgs):
                r.in_image = True      # 小さな画像 (サブプロット) の真上/真下に中央ぞろえで置かれた短い題 (「Correct-Correct」など) は図の一部
            if any(_inside(b, t) for t in tables):
                r.in_table = True
        nchars = sum(r.nchars for r in rows)
        scanned = nchars < scan_min and bool(imgs or page.get_images())
        if scanned:
            warnings.append(f"p{pno}: テキストがほぼありません (スキャン画像?)。OCR は対象外のため原文のままにします")
        links = page.get_links()
        pages_meta.append({"number": pno, "width": page.rect.width, "height": page.rect.height,
                           "rotation": page.rotation, "scanned": scanned, "scanned_ocr": ocr,
                           "gutters": [[round(v, 1) for v in g] for g in page_gutters],
                           "images": [[round(v, 2) for v in i] for i in imgs + bands + vfigs],   # 障害物 (OCR ページでは推定した図の帯も含む)
                           "tables": [[round(v, 2) for v in t] for t in tables],
                           "n_links": {"uri": sum(1 for l in links if l["kind"] == fitz.LINK_URI),
                                       "internal": sum(1 for l in links if l["kind"] in (fitz.LINK_GOTO, fitz.LINK_NAMED))},
                           "frames": []})
        all_rows.extend(rows)
    doc.close()

    body_size = body_size_of(all_rows)
    vocab = Vocab.from_texts([r.text for r in all_rows])
    frames: list[Frame] = []
    groups = group_rows(all_rows, body_size, ex)
    counters: dict[int, int] = collections.defaultdict(int)
    for g in groups:
        pno = g[0].page
        counters[pno] += 1
        frames.append(make_frame(f"p{pno}-f{counters[pno]}", g, vocab))
    frames.sort(key=lambda f: (f.page,))  # 読み順: ページ順 → 最初の行の stream 順
    assign_roles(frames, pages_meta, body_size, ex)
    finalize_translate(frames, cfg)
    joins = find_joins([f for f in frames], ex)
    for f in frames:
        pages_meta[f.page - 1]["frames"].append(f.to_json())
    out = {
        "schema_version": SCHEMA_VERSION,
        "name": pdf_path.stem, "source": str(pdf_path), "num_pages": len(pages_meta), "body_size": body_size,
        "charmap": eff_charmap,
        "unmapped_chars": {fn: {f"U+{ord(ch):04X}": n for ch, n in d.items()} for fn, d in unmapped.items()},
        "warnings": warnings,
        "pages": pages_meta, "joins": joins, "joins_source": "heuristic", "joins_heuristic": [list(j) for j in joins],
        "vocab": vocab.to_json(),
        "table_terms": collect_table_terms(frames, int(_p(ex, "table_terms_max"))),
    }
    ok, w = validate_joins({f["id"]: f for p in pages_meta for f in p["frames"]},
                           [f["id"] for p in pages_meta for f in p["frames"]], joins)
    out["joins"] = ok
    out["warnings"] += w
    if work_dir is not None:
        wd = Path(work_dir)
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "doc.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def load_doc(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def iter_frames(doc: dict):
    for p in doc["pages"]:
        for f in p["frames"]:
            yield f
