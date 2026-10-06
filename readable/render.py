"""⑦ render: 日本語版 PDF と交互対訳版 PDF の生成。

座標は doc.json と同じ「見た目の向き」(回転適用後) で扱い、PDF への書き込み (redact / insert_htmlbox /
insert_link) の直前にだけ page.derotation_matrix で元の座標系へ戻す。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from . import timing
from .config import Config, load_config
from .fonts import FontSet, build_fonts


def _x_overlap(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0]))


def _y_overlap(a, b) -> float:
    return max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def _frame_css(f: dict, size: float, lh: float, cfg: Config, fonts: FontSet, indent: float = 0.0) -> str:
    fam = fonts.family(bool(f.get("serif", True)))
    weight = "bold" if f.get("bold") else "normal"
    if weight == "bold" and f"{fam}-bold" not in fonts.files:
        weight = "normal"
    c = int(f.get("color", 0))
    color = "#%06x" % (c & 0xFFFFFF)
    align = f.get("align", "left")
    if align == "justify" and not cfg.get("render", "justify", True):
        align = "left"
    ti = f"text-indent:{indent:g}em;" if indent else ""
    return (f"font-family:{fam};font-size:{size:.2f}pt;line-height:{lh:.2f};font-weight:{weight};"
            f"color:{color};text-align:{align};margin:0;padding:0;{ti}")


_HTML_SPLIT_RE = re.compile(r"(<[^>]+>)")
_CJK_RE = re.compile("[、-〿぀-ヿ㐀-鿿＀-￯]")
_SYM_CH = "±=<>≤≥×−–%°~∼+/"
_NBSP = " "


def protect_spaces(ja: str) -> str:
    """表示用: 数値・記号まわりの半角空白を NBSP (U+00A0) にして、両端揃え (justify) で引き伸ばされないようにする。

    insert_htmlbox の両端揃えは通常の空白だけを引き伸ばすため、和文中に少ない空白 (「23.58歳 ± 3.36歳」の ± の前後など) が
    行の余りを一手に引き受けて広く空いて見える (全角空白が入っているわけではない)。NBSP は引き伸ばされず、幅は半角のまま。
    ただし NBSP は折り返しも禁じるので、対象は最小限にする:
      - ± = < > ≤ ≥ × − – % ° ~ + / (&lt; &gt; 含む) に接する空白
      - 数字と単位の境 (「5 min」「10.5 Hz」) と、英字と数字の境 (「Dreem 2」)
      - 和文に挟まれた短い (20 字以内) 英数字の連なりの内部 (「Smith, 2020」)。それより長いもの
        (「(Collins et al., 2016)」のような引用など) は通常の空白のまま折り返せる
    タグの内側・属性は触らない (空白の左右の文字はタグをまたいで見る)。"""
    parts = _HTML_SPLIT_RE.split(ja)
    # 和文と ASCII の括弧・句読点の間の空白は、日本語の組版では入れない (両端揃えで行末に大きな空きができる原因になる):
    # 和文 + 空白 + ( [ → 詰める、: ; , . ) ] + 空白 + 和文 → 詰める
    parts = [p if i % 2 else _TIGHT_RE1.sub("", _TIGHT_RE2.sub("", p)) for i, p in enumerate(parts)]
    flat = "".join(p for i, p in enumerate(parts) if i % 2 == 0)
    n = len(flat)
    res = []
    for i, ch in enumerate(flat):
        if ch != " " or i == 0 or i + 1 >= n:
            res.append(ch)
            continue
        l, r = flat[i - 1], flat[i + 1]
        if l == " " or r == " ":
            res.append(ch)
            continue
        hit = (l in _SYM_CH or r in _SYM_CH
               or flat[max(0, i - 4):i].endswith(("&lt;", "&gt;")) or flat[i + 1:i + 5].startswith(("&lt;", "&gt;"))
               or (l.isdigit() and r.isascii() and r.isalpha()) or (l.isascii() and l.isalpha() and r.isdigit()))
        res.append(_NBSP if hit else " ")
    for m in re.finditer(r"[^\u3001-\u303f\u3040-\u30ff\u3400-\u9fff\uff00-\uffef]+", flat):
        r0 = m.group(0)
        if len(r0.strip()) <= 20:
            lead, trail = len(r0) - len(r0.lstrip()), len(r0) - len(r0.rstrip())
            for j in range(m.start() + lead, m.end() - trail):
                if flat[j] == " " and 0 < j < n - 1 and flat[j - 1] != " " and flat[j + 1] != " ":
                    res[j] = _NBSP
    flat2 = "".join(res)
    out, k = [], 0
    for i, p in enumerate(parts):
        if i % 2:
            out.append(p)
        else:
            out.append(flat2[k:k + len(p)])
            k += len(p)
    return "".join(out)


_TIGHT_RE1 = re.compile(r"(?<=[\u3001-\u303f\u3040-\u30ff\u3400-\u9fff\uff00-\uffef]) +(?=[(\[])")
_TIGHT_RE2 = re.compile(r"(?<=[:;,)\]]) +(?=[\u3001-\u303f\u3040-\u30ff\u3400-\u9fff\uff00-\uffef])")
_CJK_ANY = re.compile("[\u3001-\u303f\u3040-\u30ff\u3400-\u9fff\uff00-\uffef]")
_NON_CJK_RUN = re.compile("[^\u3001-\u303f\u3040-\u30ff\u3400-\u9fff\uff00-\uffef]+")
_ITALIC_RE = re.compile(r"<i>(.*?)</i>", re.S)
_KW_SPLIT_RE = re.compile(r"([;\uff1b,\uff0c\u3001]\s*)")


def fix_italics(html: str, has_italic_face: bool) -> str:
    """<i> の中の和文は斜体にしない (日本語フォントに斜体面が無く、別のフォントに化けるため)。ラテン文字の連なりだけを <i> に残す。
    斜体面が登録されていなければ <i> は全て外す (擬似斜体にならず立体のまま)。"""
    def sub(m: re.Match) -> str:
        inner = m.group(1)
        if not has_italic_face:
            return inner
        if "<" in inner or not _CJK_ANY.search(inner):
            return m.group(0)
        out, pos = [], 0
        for r in _NON_CJK_RUN.finditer(inner):
            out.append(inner[pos:r.start()])
            seg = r.group(0)
            out.append(f"<i>{seg}</i>" if re.search(r"[A-Za-z]", seg) else seg)
            pos = r.end()
        out.append(inner[pos:])
        return "".join(out)
    return _ITALIC_RE.sub(sub, html)


def _est_width(t: str, size: float) -> float:
    """文字列の組み幅の概算 (全角 = 1 字、半角 = 0.5 字)。"""
    return sum(size if _CJK_ANY.match(c) else 0.5 * size for c in re.sub(r"<[^>]+>", "", t))


_PAREN_RE = re.compile(r"\(([^()<>]{2,60})\)")


def mark_table_terms(html: str, terms: list[str], color: str) -> str:
    """本文中の「日本語 (English)」の (English) 部分のうち、表の用語 (英語・略語) を含むものに色と下線を付ける。
    括弧の中身を ", " で分けた要素のどれかが表の用語 (大文字小文字を区別しない) と一致したときだけ。タグの内側は触らない。"""
    tset = {t.lower() for t in terms}

    def sub(m: re.Match) -> str:
        parts = [x.strip().lower() for x in re.split(r"[,、]", m.group(1))]
        if any(x in tset for x in parts):
            return f'<span style="color:{color};text-decoration:underline">{m.group(0)}</span>'
        return m.group(0)

    segs = _HTML_SPLIT_RE.split(html)
    return "".join(p if i % 2 else _PAREN_RE.sub(sub, p) for i, p in enumerate(segs))


_GLOSS_RE = re.compile(r"(?:[  ]|&nbsp;)*\(([^()<>]{2,60})\)")


_ADJ_GLOSS_RE = re.compile(
    r"([぀-ヿ㐀-鿿]{1,8})[  ]?\(([A-Za-z][A-Za-z \-]{1,30})\)([぀-ヿ㐀-鿿]{1,8})[  ]?\(([A-Za-z][A-Za-z \-]{1,30})\)")
GLOSS_FREE_ROLES = ("caption", "table", "footnote", "figure_text", "heading", "title", "keywords")


def strip_repeated_gloss(htmls: list[str], terms: list[str], roles: list[str] | None = None) -> list[str]:
    """表・図の用語の英語併記 (「パーソナライズTMR (Personalized TMR)」の括弧部分) の後処理。
    (1) 語ごとの併記 (「データ(Data)収集(collection)」) は、用語全体の 1 つの併記 (「データ収集 (Data collection)」) にまとめる。
    (2) キャプション・表・脚注・見出しなど (GLOSS_FREE_ROLES) の併記は全て外す。
    (3) 本文は、同じページで同じ用語の 2 回目以降の括弧を (直前の空白ごと) 外す。
    htmls は 1 ページ分の訳文を読み順に並べたもの。括弧の中身を ", " で分けた要素のどれかが用語 (大文字小文字を区別しない) と一致するものだけが対象。
    タグの内側は触らない。"""
    tset = {t.lower() for t in terms}
    seen: set[tuple[str, ...]] = set()

    def in_terms(x: str) -> bool:
        return any(p.strip().lower() in tset for p in re.split(r"[,、]", x))

    def merge(m: re.Match) -> str:
        a, e1, b, e2 = m.groups()
        if in_terms(e1) and in_terms(e2) and len(e1.split()) == 1 and len(e2.split()) == 1:
            return f"{a}{b} ({e1} {e2.lower() if e2[:1].isupper() and not e2.isupper() else e2})"
        return m.group(0)

    def sub_seen(m: re.Match) -> str:
        parts = tuple(x.strip().lower() for x in re.split(r"[,、]", m.group(1)))
        if not any(x in tset for x in parts):
            return m.group(0)
        if parts in seen:
            return ""
        seen.add(parts)
        return m.group(0)

    def sub_all(m: re.Match) -> str:
        parts = tuple(x.strip().lower() for x in re.split(r"[,、]", m.group(1)))
        return "" if any(x in tset for x in parts) else m.group(0)

    out = []
    for k, h in enumerate(htmls):
        role = roles[k] if roles and k < len(roles) else "body"
        segs = _HTML_SPLIT_RE.split(h)
        fn = sub_all if role in GLOSS_FREE_ROLES else sub_seen
        res = []
        for i, p in enumerate(segs):
            if i % 2:
                res.append(p)
            else:
                p = _ADJ_GLOSS_RE.sub(merge, p)
                res.append(_GLOSS_RE.sub(fn, p))
        out.append("".join(res))
    return out


def add_cell_notes(src: fitz.Document, annotations: list[dict], style: str = "highlight") -> int:
    """表のセルごとに、日本語訳を PDF 注釈 (Contents) として付ける。表の見た目は変えない。
    style="invisible" (既定): セル全体を覆う完全に透明なハイライト (見える印は一切なし。Acrobat のホバー・コメント一覧で読める)。
    style="highlight": セル全体を覆うほぼ透明 (不透明度 1%) なハイライト (Acrobat ではホバーで内容が出る)。
    style="text": セルの右上に小さな付箋アイコン (アイコンは見える。Acrobat/Edge/Chrome でクリックまたはホバーで表示)。
    annotations: [{"page": 1 始まり, "rect": 見た目の座標, "text": 訳}]。付けた数を返す。"""
    n = 0
    for a in annotations:
        page = src[a["page"] - 1]
        D = page.derotation_matrix
        r = (fitz.Rect(a["rect"]) * D).normalize()
        try:
            if style == "text":
                an = page.add_text_annot(fitz.Point(r.x1 - 2, r.y0), a["text"], icon="Note")
                an.set_rect(fitz.Rect(r.x1 - 10, r.y0, r.x1, r.y0 + 10))
            else:
                an = page.add_highlight_annot(r)
                an.set_colors(stroke=(1, 1, 1))
                an.set_opacity(0.0 if style == "invisible" else 0.01)      # invisible: 完全に透明 (目に見える印なし。NoView は付けないので、Acrobat のホバー/コメント一覧で読める)
                an.set_flags(fitz.PDF_ANNOT_IS_PRINT)
                an.set_info(content=a["text"], title="訳")
            an.set_info(content=a["text"], title="訳")
            an.update()
            n += 1
        except Exception:  # pragma: no cover - 注釈を付けられないセルは諦める
            continue
    return n


def nowrap_keywords(html: str, width: float | None = None, size: float = 10.0) -> str:
    """キーワード一覧は、区切り (; , 、) の間の 1 語 (「標的記憶想起 (targeted memory reactivation)」) を途中で折り返さない。
    先頭のラベル (<b>キーワード:</b>) はそのまま。タグをまたぐ項目がある場合は何もしない。"""
    m = re.match(r"^((?:<b>.*?</b>|<i>.*?</i>)?\s*)(.*)$", html, re.S)
    head, body = m.group(1), m.group(2)
    if "<" in body:
        return html
    items = _KW_SPLIT_RE.split(body)
    if width is not None and max((_est_width(t, size) for i, t in enumerate(items) if i % 2 == 0), default=0.0) > 0.9 * width:
        return html            # 1 項目が行幅に収まらないときは、折り返せるままにする (縮小を避ける)
    out = []
    for i, t in enumerate(items):
        out.append(t if i % 2 else f'<span style="white-space:nowrap">{t.rstrip()}</span>' + (t[len(t.rstrip()):] if t.strip() else ""))
    return head + "".join(out)


def estimate_paper_color(page: fitz.Page) -> tuple[float, float, float]:
    """スキャンページの紙の色を、低解像度に描いた画像の「明るい側 25% の画素」の平均から推定する (文字・図の暗い画素の影響を避ける)。"""
    try:
        pix = page.get_pixmap(dpi=30, alpha=False)
    except Exception:
        return (1.0, 1.0, 1.0)
    n, s = pix.n, pix.samples
    px = [(s[i] * 299 + s[i + 1] * 587 + s[i + 2] * 114, s[i], s[i + 1], s[i + 2]) for i in range(0, len(s) - n + 1, n)]
    if not px:
        return (1.0, 1.0, 1.0)
    px.sort()
    top = px[int(len(px) * 0.75):]
    k = len(top)
    return (sum(p[1] for p in top) / k / 255.0, sum(p[2] for p in top) / k / 255.0, sum(p[3] for p in top) / k / 255.0)


def _row_pitch(f: dict) -> float:
    rows = f.get("rows", [])
    if len(rows) < 2:
        return 0.0
    ys = [r[1] for r in rows]
    return (ys[-1] - ys[0]) / (len(rows) - 1)


PEER_ROLES = {
    "heading": {"body", "heading", "abstract"},
    "body": {"body", "abstract"},
    "abstract": {"body", "abstract", "keywords"},
    "keywords": {"abstract", "keywords"},
    "caption": {"caption", "body"},
    "sidebar": {"sidebar"},
    "footnote": {"footnote"},
}


# --------------------------------------------------------------------------
# 障害物 (他 frame / 画像 / 罫線・ベクタ図形・背景塗り)
# --------------------------------------------------------------------------

@dataclass
class PageCtx:
    """1 ページ分の描画文脈 (座標は見た目の向き)。"""
    page: fitz.Page
    pdata: dict
    cfg: Config
    fonts: FontSet
    obstacles: list[tuple[str, list[float]]]       # (id, bbox): 他 frame・画像・図形
    containers: list[list[float]]                  # frame を内包する塗り/枠 (背景ボックス)
    M: fitz.Matrix
    D: fitz.Matrix
    rotation: int
    page_w: float
    page_h: float
    drawings: list[list[float]] = field(default_factory=list)
    ocr_fill: tuple | None = None                          # スキャン + OCR ページ: 英語を隠す背景色 (紙の色)
    term_marks: list[str] = field(default_factory=list)   # 表の用語 (英語・略語) の小文字。本文の (English) 部分を色づけする
    mark_color: str = ""
    size_scale: float = 1.0                                # このページの文字サイズの倍率 (描画後に重なりが見つかったときの描き直し)
    no_expand: bool = False                                # True: 枠を広げない (幅も下方向も原文の枠まで。重なりの描き直しの 1 回目)


def page_drawing_rects(page: fitz.Page, min_size: float = 1.0) -> list[list[float]]:
    """ページの図形 (罫線・矩形・ベクタ図) の bbox を見た目の座標で返す。ページ全面の背景や点は除く。"""
    M = page.rotation_matrix
    area = page.rect.width * page.rect.height
    out = []
    for dr in page.get_drawings():
        r = (fitz.Rect(dr["rect"]) * M).normalize()
        if r.width < min_size and r.height < min_size:
            continue
        if r.width * r.height > 0.6 * area:
            continue
        out.append([r.x0, r.y0, r.x1, r.y1])
    return out


def _contains(outer, inner, tol: float = 1.5) -> bool:
    return (outer[0] <= inner[0] + tol and outer[1] <= inner[1] + tol
            and outer[2] >= inner[2] - tol and outer[3] >= inner[3] - tol)


def _container_of(bbox, ctx: PageCtx) -> list[float] | None:
    best = None
    for c in ctx.containers:
        if _contains(c, bbox) and (c[2] - c[0]) * (c[3] - c[1]) > 1.0:
            if best is None or (c[2] - c[0]) * (c[3] - c[1]) < (best[2] - best[0]) * (best[3] - best[1]):
                best = c
    return best


def _col_right(f: dict, ctx: PageCtx) -> float:
    """短い frame (見出し等) の右端を、同じ列の仲間 (PEER_ROLES) の右端まで広げる。
    右に同じ高さ帯で隣接する要素 (frame/画像/図形) があれば、その手前で止める。frame を内包する背景ボックスの内側に収める。"""
    r = f["bbox"]
    w = r[2] - r[0]
    peers = PEER_ROLES.get(f["role"], set())
    right = r[2]
    for o in ctx.pdata["frames"]:
        if o["id"] == f["id"]:
            continue
        ob = o["bbox"]
        if o["role"] in peers and _x_overlap(r, ob) >= 0.5 * min(w, ob[2] - ob[0]) \
                and ((ob[2] - ob[0]) <= 0.6 * ctx.page_w or ctx.ocr_fill is not None):
            right = max(right, ob[2])
    limit = 1e9
    for oid, ob in ctx.obstacles:
        if oid == f["id"]:
            continue
        if ob[0] >= r[2] - 1.0 and ob[1] < r[3] - 1.0 and ob[3] > r[1] + 1.0:
            limit = min(limit, ob[0] - 4.0)
    cont = _container_of(r, ctx)
    if cont is not None:
        limit = min(limit, cont[2] - 2.0)
    for g in ctx.pdata.get("gutters") or []:      # 段間 (ガター): 列の右端 (ガターの左端) を越えて広げない。段をまたぐ全幅の frame は対象外
        if g[0] > r[0] + 30 and r[2] <= g[1] + 6.0 and r[2] >= g[0] - 40.0:
            limit = min(limit, g[0] + 1.0)
    if ctx.no_expand:
        limit = min(limit, r[2])                  # 描き直し: 広げずに原文の枠の幅で組む
    return max(min(right, limit), r[2])


def size_ladder(s0: float, step: float, min_scale: float, min_font: float) -> list[float]:
    floor = max(min_font, s0 * min_scale)
    sizes = [s0]
    k = 1
    while True:
        s = s0 * (1 - step * k)
        if s < floor - 1e-6:
            break
        sizes.append(round(s, 2))
        k += 1
    return sizes


def below_limit(f: dict, x0: float, x1: float, y_from: float, ctx: PageCtx, margin: float) -> float:
    """frame の下方向に伸ばせる限界の y。他 frame・画像・罫線/図形 (x 範囲 [x0,x1] に重なるもの) の手前、
    frame を内包する背景ボックスの下端、ページ下余白でクリップする。"""
    limit = ctx.page_h - ctx.cfg.get("render", "bottom_margin", 24.0)
    span = [x0, 0, x1, 0]
    for oid, ob in ctx.obstacles:
        if oid == f["id"]:
            continue
        # 図形は、直下の下線 (リンク下線など) を障害物にしないよう少し離れたものだけ対象にする
        thr = y_from + 2.5 if oid == "draw" else y_from - 1.0
        if ob[1] >= thr and _x_overlap(span, ob) > 2.0:
            limit = min(limit, ob[1] - margin)
    cont = _container_of(f["bbox"], ctx)
    if cont is not None:
        limit = min(limit, cont[3] - 2.0)
    return limit


def expanded_rect(f: dict, page_obstacles: list[tuple[str, list[float]]], page_h: float, cfg: Config,
                  x1: float | None = None, ctx: PageCtx | None = None) -> fitz.Rect:
    """枠を下の空白 (次の障害物の手前) まで拡張した矩形。x1 を渡すと広げた後の幅で障害物を判定する。"""
    r = f["bbox"]
    x1 = r[2] if x1 is None else max(x1, r[2])
    margin = cfg.get("render", "expand_margin", 4.0)
    if ctx is None:
        limit = page_h - cfg.get("render", "bottom_margin", 24.0)
        for oid, ob in page_obstacles:
            if oid != f["id"] and ob[1] >= r[3] - 1.0 and _x_overlap([r[0], 0, x1, 0], ob) > 2.0:
                limit = min(limit, ob[1] - margin)  # (ctx 無しの簡易版: テストと互換のため)
    else:
        limit = below_limit(f, r[0], x1, r[3], ctx, margin)
    return fitz.Rect(r[0], r[1], x1, max(limit, r[3]))


# --------------------------------------------------------------------------
# リンク
# --------------------------------------------------------------------------

def _link_sig(l: dict) -> tuple:
    r = l["from"]
    tgt = l.get("uri") or l.get("nameddest") or l.get("file") or l.get("page")
    return (l["kind"], round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1), str(tgt))


def _clean_link(l: dict, page_map: dict[int, int] | None = None) -> dict:
    """get_links() の dict から insert_link 用の dict を作る (page_map があれば GOTO の宛先ページを写像)。"""
    d = {"kind": l["kind"], "from": fitz.Rect(l["from"])}
    for k in ("uri", "nameddest", "name", "file", "page", "to", "zoom"):
        if k in l and l[k] is not None:
            d[k] = l[k]
    if page_map is not None and d["kind"] == fitz.LINK_GOTO and d.get("page", -1) in page_map:
        d["page"] = page_map[d["page"]]
    return d


def _center_in(r, rects) -> bool:
    cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
    return any(a.x0 - 0.5 <= cx <= a.x1 + 0.5 and a.y0 - 0.5 <= cy <= a.y1 + 0.5 for a in rects)


# --------------------------------------------------------------------------
# 日本語版
# --------------------------------------------------------------------------

def check_drawn_overlaps(src: fitz.Document, doc: dict, pages: set[int], min_w: float = 2.0, min_h: float = 2.0,
                         ja_by_frame: dict[str, str] | None = None) -> list[dict]:
    """描画した結果 (insert_htmlbox のあとの実際の文字の位置) で、日本語の行どうし、および日本語の行と翻訳しない要素
    (図・表・罫線・サイドバーなど。frame の bbox と画像) の重なりを調べる。見つかったものを警告として返す (描き直しはしない)。"""
    out: list[dict] = []
    for pdata in doc["pages"]:
        if pdata["number"] not in pages:
            continue
        pg = src[pdata["number"] - 1]
        M = pg.rotation_matrix
        fallback = [f["bbox"] for f in pdata["frames"] if ja_by_frame and f["id"] in ja_by_frame and ja_by_frame[f["id"]] == f["html"]]
        lines = []
        for b in pg.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                t = "".join(sp["text"] for sp in l["spans"])
                r = (fitz.Rect(l["bbox"]) * M).normalize()
                if _CJK_RE.search(t):
                    lines.append(([r.x0, r.y0, r.x1, r.y1], t))
                elif fallback and len(t.strip()) >= 8 and any(fb[0] <= (r.x0 + r.x1) / 2 <= fb[2] and fb[1] <= (r.y0 + r.y1) / 2 <= fb[3] for fb in fallback):
                    lines.append(([r.x0, r.y0, r.x1, r.y1], t))      # 英語のまま残った段落の行 (日本語の行との重なりも調べる)
        fixed = [(f["id"], f["bbox"]) for f in pdata["frames"] if not f["translate"] and f["role"] not in ("page_header", "page_number", "doi_url")]
        fixed += [("img", i) for i in pdata.get("images", []) if (i[2] - i[0]) < 0.9 * pdata["width"]]
        for bb, t in lines:
            for oid, ob in fixed:
                w, h = min(bb[2], ob[2]) - max(bb[0], ob[0]), min(bb[3], ob[3]) - max(bb[1], ob[1])
                if w > min_w and h > min_h - 0.5:
                    out.append({"page": pdata["number"], "kind": f"日本語と {oid}", "w": round(w), "h": round(h, 1), "text": t[:16]})
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                a, b = lines[i][0], lines[j][0]
                w, h = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
                if w > min_w and h > min_h:
                    out.append({"page": pdata["number"], "kind": "日本語どうし", "w": round(w), "h": round(h, 1), "text": lines[i][1][:10] + " / " + lines[j][1][:10]})
    return out


def render_ja(src_pdf: str | Path, doc: dict, ja_by_frame: dict[str, str], out_pdf: str | Path,
              cfg: Config | None = None, fonts: FontSet | None = None, subset: bool | None = None,
              annotations: list[dict] | None = None) -> dict:
    """日本語版を生成し、描画レポートを返す (_render_ja_pass)。描画後に実際の文字の位置で重なりを調べ、重なりのあるページだけ
    文字を 8% ずつ縮めて描き直す (最大 2 回。[render] overlap_retry = false で無効)。直らなかったものは警告として残す。"""
    cfg = cfg or load_config()
    scale: dict[int, float] = {}
    noexp: set[int] = set()
    summ = _render_ja_pass(src_pdf, doc, ja_by_frame, out_pdf, cfg, fonts, subset, annotations, scale, noexp)
    retries = 0
    while cfg.get("render", "overlap_retry", True) and summ["overlaps"] and retries < 3:
        for p in {o["page"] for o in summ["overlaps"]}:
            if p not in noexp:
                noexp.add(p)                              # 1 回目: まず広げた枠を原文の枠に戻す (縮小はしない)
            else:
                scale[p] = scale.get(p, 1.0) * 0.92       # 2 回目以降: さらに文字を縮める
        retries += 1
        prev = summ
        summ = _render_ja_pass(src_pdf, doc, ja_by_frame, out_pdf, cfg, fonts, subset, annotations, scale, noexp)
        summ["overlap_retries"] = retries
        summ["overlaps_before_retry"] = len(prev["overlaps"])
    return summ


def _render_ja_pass(src_pdf: str | Path, doc: dict, ja_by_frame: dict[str, str], out_pdf: str | Path,
                    cfg: Config, fonts: FontSet | None, subset: bool | None, annotations: list[dict] | None,
                    page_scale: dict[int, float], no_expand_pages: set[int] | None = None) -> dict:
    """日本語版を 1 回描く。page_scale = {ページ: 文字サイズの倍率} (重なりの描き直し用)。

    - 翻訳した行だけを redact し、日本語を流し込む。他の frame・図・罫線・背景は触らない。
    - redact で消えるリンクのうち、翻訳領域の外にあるものは insert_link で復元する。
      翻訳領域内の外部 URL リンクは訳文の <a href> から insert_htmlbox が作る。内部リンク (引用 [n] 等) は失われ、件数を記録する。
    """
    t_render = time.monotonic()
    no_expand_pages = no_expand_pages or set()
    cfg = cfg or load_config()
    fonts = fonts or build_fonts(cfg)
    rc = cfg.section("render")
    inset = rc.get("redact_inset", 0.5)
    src = fitz.open(str(src_pdf))
    report: list[dict] = []
    link_stats = {"orig": 0, "kept": 0, "restored": 0, "inside_uri": 0, "lost_internal": 0}
    scanned_ocr_pages: list[int] = []
    style = build_style_book(doc, ja_by_frame, cfg)
    tc = cfg.section("table_terms")
    mark_color = tc.get("color", "") if tc.get("keep_en", True) else ""
    term_marks = [x for t in (doc.get("table_terms") or []) for x in (t.get("en", ""), t.get("abbr", "")) if x]
    gloss_first_only = bool(tc.get("gloss_first_only", True))
    for pdata in doc["pages"]:
        page = src[pdata["number"] - 1]
        targets = [f for f in pdata["frames"] if f["id"] in ja_by_frame]
        if not targets:
            continue
        M, D = page.rotation_matrix, page.derotation_matrix
        draw_rects = page_drawing_rects(page, rc.get("obstacle_min_size", 1.0))
        orig_links = page.get_links()
        # 1) redact: 行ごとの矩形 (図・隣接行を巻き込まないよう上下を少し内側に)。矩形は元の座標系 (derotate) で
        redact_rects: list[fitz.Rect] = []
        for f in targets:
            for rb in f["rows"]:
                rr = fitz.Rect(rb[0] + 0.3, rb[1] + inset, rb[2] - 0.3, rb[3] - inset)
                if rr.width > 0 and rr.height > 0:
                    raw = (rr * D).normalize()
                    redact_rects.append(raw)
                    page.add_redact_annot(raw, fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
        ocr_fill = None
        if pdata.get("scanned_ocr"):
            ocr_fill = estimate_paper_color(page)
            scanned_ocr_pages.append(pdata["number"])
            # 画像の中の英語を隠す塗りつぶしは、連なりごとに _flow_chain が行う (訳文を流し込む範囲を含めて 1 度に塗る)
        # 2) リンク: redact に巻き込まれて消えた翻訳領域の外のリンクを復元
        after = {_link_sig(l) for l in page.get_links()}
        link_stats["orig"] += len(orig_links)
        for l in orig_links:
            if _link_sig(l) in after:
                link_stats["kept"] += 1
            elif _center_in(fitz.Rect(l["from"]), redact_rects):
                link_stats["inside_uri" if l["kind"] == fitz.LINK_URI else "lost_internal"] += 1
            else:
                try:
                    page.insert_link(_clean_link(l))
                    link_stats["restored"] += 1
                except Exception:  # pragma: no cover - 壊れたリンク注釈は諦める
                    link_stats["lost_internal"] += 1
        # 3) 流し込み
        ja_pg = ja_by_frame
        if gloss_first_only and term_marks:       # 表・図の用語の英語併記は、そのページでの初出だけ (2 回目以降は日本語だけ)
            ids = [f["id"] for f in targets]
            new = strip_repeated_gloss([ja_by_frame[i] for i in ids], term_marks, [f["role"] for f in targets])
            ja_pg = {**ja_by_frame, **dict(zip(ids, new))}
        ctx = PageCtx(
            page=page, pdata=pdata, cfg=cfg, fonts=fonts,
            obstacles=[(f["id"], f["bbox"]) for f in pdata["frames"]] + [("img", b) for b in pdata["images"]]
                      + [("draw", d) for d in draw_rects],
            containers=[d for d in draw_rects if (d[2] - d[0]) > 20 and (d[3] - d[1]) > 10],
            M=M, D=D, rotation=page.rotation, page_w=pdata["width"], page_h=pdata["height"], drawings=draw_rects,
            term_marks=term_marks, mark_color=mark_color, ocr_fill=ocr_fill, size_scale=page_scale.get(pdata["number"], 1.0),
            no_expand=(pdata["number"] in no_expand_pages))
        for rep in _flow_page(ctx, targets, ja_pg, style):
            rep["page"] = pdata["number"]
            report.append(rep)
    overlaps = check_drawn_overlaps(src, doc, {r["page"] for r in report}, ja_by_frame=ja_by_frame)
    n_ann = 0
    if annotations:
        n_ann = add_cell_notes(src, annotations, cfg.get("table_terms", "annotation_style", "highlight"))
    if (subset if subset is not None else rc.get("subset_fonts", True)):
        src.subset_fonts()
    Path(out_pdf).parent.mkdir(parents=True, exist_ok=True)
    src.save(str(out_pdf), garbage=4, deflate=True)
    src.close()
    summ = summarize(report)
    summ["links"] = link_stats
    summ["annotations"] = n_ann
    summ["overlaps"] = overlaps          # 描画後に実際の文字の位置で調べた重なり (日本語の行どうし・日本語の行と翻訳しない要素)
    for o in overlaps[:10]:
        summ["warnings"].append(f"p{o['page']}: 描画後の重なり ({o['kind']} {o['w']}x{o['h']}pt) {o['text']}")
    summ["stage_times"] = {**timing.snapshot(), "render": round(time.monotonic() - t_render, 1)}     # 段階別の処理時間 (秒)
    summ["scanned_ocr"] = scanned_ocr_pages        # スキャン + OCR として描いたページ (英語を紙の色で隠した)
    return summ


# --------------------------------------------------------------------------
# 文書全体で揃えるスタイル (role ごとの基準サイズ・行間)
# --------------------------------------------------------------------------

@dataclass
class StyleBook:
    """frame id -> (基準サイズ pt, 行間 (サイズ倍), 字下げ em)。"""
    size: dict[str, float] = field(default_factory=dict)
    lh: dict[str, float] = field(default_factory=dict)
    indent: dict[str, float] = field(default_factory=dict)
    modes: dict[str, list[float]] = field(default_factory=dict)  # role -> 基準サイズ (レポート用)
    role_lh: dict[str, float] = field(default_factory=dict)


def _size_modes(weights: dict[float, int], tol_abs: float, tol_rel: float) -> dict[float, float]:
    """サイズ (0.1pt 丸め) -> その塊の基準サイズ (重み = 文字数が最大のサイズ)。許容差内のサイズは同じ塊にまとめる。"""
    out: dict[float, float] = {}
    for sz in sorted(weights, key=lambda x: -weights[x]):
        if sz in out:
            continue
        mode = sz
        for o in weights:
            if o not in out and abs(o - mode) <= max(tol_abs, tol_rel * mode):
                out[o] = mode
    return out


def build_style_book(doc: dict, ja_by_frame: dict[str, str], cfg: Config) -> StyleBook:
    """翻訳する frame について、role ごとに原文の最頻サイズ (文字数で重み付け) を基準サイズ、原文の行送りの中央値を行間にする。

    サイズが基準と許容差内の frame は基準サイズに揃える (9.9/10.0/10.1pt -> 10.1pt)。許容差を超える frame (別サイズの見出しなど) は
    その frame のサイズのまま。行間は role ごとに [line_height_min, line_height_max] に収めた中央値 (複数行 frame が無い role は line_height)。
    原文で字下げされた本文・要旨の段落には indent_em の字下げを付ける。"""
    rc = cfg.section("render")
    ec = cfg.section("extract")
    tol_abs, tol_rel = ec.get("size_tol_abs", 0.6), ec.get("size_tol_rel", 0.06)
    lo, hi, dflt = rc.get("line_height_min", 1.4), rc.get("line_height_max", 1.65), rc.get("line_height", 1.5)
    uniform = bool(rc.get("uniform_style", True))
    frames = [f for p in doc["pages"] for f in p["frames"] if f["id"] in ja_by_frame]
    sb = StyleBook()
    by_role: dict[str, list[dict]] = {}
    for f in frames:
        by_role.setdefault(f["role"], []).append(f)
    for role, fs in by_role.items():
        weights: dict[float, int] = {}
        for f in fs:
            k = round(float(f["size"]), 1)
            weights[k] = weights.get(k, 0) + max(len(f["text"]), 1)
        modes = _size_modes(weights, tol_abs, tol_rel) if uniform else {}
        ratios = sorted(_row_pitch(f) / f["size"] for f in fs if f.get("nrows", 1) >= 3 and _row_pitch(f) > 0)
        med = ratios[len(ratios) // 2] if ratios else (1.3 if role in ("heading", "title") else dflt)
        L = round(min(max(med, lo if ratios else 1.2), hi), 2)
        sb.role_lh[role] = L if uniform else dflt
        sb.modes[role] = sorted(set(modes.values()))
        for f in fs:
            k = round(float(f["size"]), 1)
            sb.size[f["id"]] = float(modes.get(k, f["size"])) if uniform else float(f["size"])
            if uniform:
                sb.lh[f["id"]] = L
            else:
                pitch = _row_pitch(f)
                sb.lh[f["id"]] = min(max(pitch / f["size"], 1.3), 1.65) if pitch > 0 else dflt
            sb.indent[f["id"]] = float(rc.get("indent_em", 1.0)) if (f.get("first_indent") and role in ("body", "abstract")) else 0.0
    return sb


# --------------------------------------------------------------------------
# 段落の流し込み (同じ列で隣り合う frame の連なりを、原文の段落間隔を保って上から詰めて配置する)
# --------------------------------------------------------------------------

@dataclass
class _Member:
    f: dict
    ja: str
    x0: float
    x1: float
    base: float
    lh: float
    indent: float
    ladder: list[float]
    k: int = 0                       # 縮小の段 (0 = 基準サイズ)
    heights: dict = field(default_factory=dict)
    narrow: dict = field(default_factory=dict)   # (size, lh) -> 孤立行 (widow) を避けるために狭めた幅 (pt)
    nowrap: bool | None = None       # キーワードを項目ごとに折り返さない表示を使うか (None = 未判定)
    spacing: float = 0.0             # 直前の frame の末尾からの追加の空き (pt)
    anchored: bool = False           # 位置を原文の位置より上へ動かさない (すぐ下に別の要素がある末尾の frame)
    soft_k: int = 10 ** 6            # 読みやすさの下限 (min_font) に達する段。これを超える縮小は、収まらないときの最終手段 (警告つき)

    @property
    def size(self) -> float:
        return self.ladder[self.k]


def _member_html(ctx: PageCtx, m: _Member, size: float, lh: float) -> str:
    ja = m.ja
    if m.f.get("role") == "keywords" and m.nowrap is not False:
        ja_nw = nowrap_keywords(ja, m.x1 - m.x0, size)
        if m.nowrap is None and ja_nw != ja:
            # MuPDF は nowrap の連なりを (まれに) 組めなくなるので、実際に組めるか使い捨てのページで確かめてから使う
            doc = fitz.open()
            try:
                pg = doc.new_page(width=m.x1 - m.x0 + 4, height=400)
                css = _frame_css(m.f, size, lh, ctx.cfg, ctx.fonts, m.indent)
                sp, _ = pg.insert_htmlbox(fitz.Rect(1, 1, m.x1 - m.x0 + 1, 390), f'<div style="{css}">{ja_nw}</div>',
                                          css=ctx.fonts.css, archive=ctx.fonts.archive, scale_low=1)
                m.nowrap = sp >= 0
            finally:
                doc.close()
        if m.nowrap:
            ja = ja_nw
    ja = fix_italics(protect_spaces(ja), "jpserif-italic" in ctx.fonts.files or "jpsans-italic" in ctx.fonts.files)
    if ctx.term_marks and ctx.mark_color:
        ja = mark_table_terms(ja, ctx.term_marks, ctx.mark_color)
    css = _frame_css(m.f, size, lh, ctx.cfg, ctx.fonts, m.indent)
    plain = re.sub(r"<[^>]+>|&\w+;", "", ja)
    if len(plain) >= 20 and sum(1 for c in plain if not _CJK_ANY.match(c)) / len(plain) >= 0.4:
        css = css.replace("text-align:justify", "text-align:left")   # 英数字・統計式が多い段落は、両端揃えで空白が引き伸ばされるより左揃えの方が読みやすい
    return f'<div style="{css}">{ja}</div>'


def _story_height(ctx: PageCtx, html: str, width: float) -> float:
    story = fitz.Story(html=html, user_css="body {margin:1px;}" + ctx.fonts.css, archive=ctx.fonts.archive)
    _more, filled = story.place(fitz.Rect(0, 0, width, 6000))
    return float(filled[3]) + 1.0


def _line_texts(ctx: PageCtx, html: str, width: float, height: float) -> list[str]:
    """html を width の箱に組んだときの、各行の文字列 (上から順)。使い捨ての空ページに描いて調べる。"""
    doc = fitz.open()
    try:
        pg = doc.new_page(width=width + 4, height=height + 20)
        pg.insert_htmlbox(fitz.Rect(1, 1, width + 1, height + 15), html, css=ctx.fonts.css, archive=ctx.fonts.archive, scale_low=1)
        rows = []
        for b in pg.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                rows.append((l["bbox"][1], "".join(sp["text"] for sp in l["spans"]).strip("\u00a0 ")))
        rows.sort()
        return [t for _y, t in rows if t]
    finally:
        doc.close()


def has_widow(lines: list[str]) -> bool:
    """最後の行が 1〜2 文字 (句読点・閉じ括弧だけ・「る [11]」のような 1 文字+引用) の孤立行か。"""
    if len(lines) < 2:
        return False
    last = lines[-1]
    core = re.sub(r"[。、，．）」』】\s]", "", last)
    if len(core) <= 1 or (len(core) <= 2 and len(last) <= 3):
        return True
    cjk = len(re.findall("[\u3040-\u30ff\u3400-\u9fff]", last))
    return cjk <= 1 and len(last) <= 8


def _measure(ctx: PageCtx, m: _Member, size: float, lh: float) -> float:
    """幅に size/lh で組んだ訳文の高さ (pt)。Story で測るだけで描かない。"""
    key = (round(size, 2), round(lh, 3))
    if key in m.heights:
        return m.heights[key]
    h = _story_height(ctx, _member_html(ctx, m, size, lh), m.x1 - m.x0)
    m.heights[key] = h
    return h


def _avoid_widow(ctx: PageCtx, m: _Member) -> None:
    """最終サイズで、複数行の最後の行が 1〜2 文字だけの孤立行 (行頭の 。 や 1 文字の行) になるときは、幅を 0.5〜2 字ぶん狭めて組み直し、
    孤立行が消えて行数が増えない最初の幅を m.narrow に記録する (描画はその幅で行う)。行数が増える狭め方は採用しない
    (配置の再計算を避け、枠からはみ出さないため)。見つからなければそのまま。"""
    size, lh = m.size, m.lh
    key = (round(size, 2), round(lh, 3))
    m.narrow.setdefault(key, 0.0)
    if not ctx.cfg.get("render", "widow_control", True):
        return
    h = _measure(ctx, m, size, lh)
    if h <= lh * size * 1.5:
        return
    html = _member_html(ctx, m, size, lh)
    width = m.x1 - m.x0
    if not has_widow(_line_texts(ctx, html, width, h)):
        return
    for k in (1.0, 2.0, 3.0, 0.5, 1.5, 2.5):
        w2 = width - k * size
        if w2 < 4 * size:
            continue
        h2 = _story_height(ctx, html, w2)
        if h2 <= h + 0.5 and not has_widow(_line_texts(ctx, html, w2, h2)):
            m.narrow[key] = k * size
            return


def _build_chains(targets: list[dict], ctx: PageCtx) -> list[list[dict]]:
    """同じ列で上下に隣り合う (間に図・罫線・他の frame が無く、間隔が 2.2 行以内の) frame を連ねる。"""
    order = sorted(targets, key=lambda f: (f["bbox"][1], f["bbox"][0]))
    pred: dict[str, dict] = {}
    succ: dict[str, dict] = {}

    ocr = ctx.ocr_fill is not None

    def compatible(f: dict, g: dict) -> bool:
        fb, gb = f["bbox"], g["bbox"]
        if ocr:
            # スキャン + OCR: OCR の bbox は上下の frame が数 pt 重なり、箇条は字下げ (ぶら下げ) も違う。同じ列で上から下へ順に流し込む
            if gb[1] < fb[1] + 0.8 * f["size"]:
                return False
            w = min(fb[2] - fb[0], gb[2] - gb[0])
            if _x_overlap(fb, gb) < 0.6 * w or abs(gb[0] - fb[0]) > 40:
                return False
            if gb[1] - fb[3] > 4.0 * f["size"] + 6.0:
                return False
        else:
            if gb[1] < fb[3] - max(1.0, 0.45 * f["size"]):    # OCR の bbox は上下の frame が数 pt 重なることがある
                return False
            w = min(fb[2] - fb[0], gb[2] - gb[0])
            if _x_overlap(fb, gb) < 0.6 * w or abs(gb[0] - fb[0]) > 20:
                return False
            if gb[1] - fb[3] > 2.2 * f["size"] + 6.0:
                return False
        band = [min(fb[0], gb[0]), fb[3] + 0.5, max(fb[2], gb[2]), gb[1] - 0.5]
        if band[3] <= band[1]:
            return True
        for oid, ob in ctx.obstacles:
            if oid in (f["id"], g["id"]):
                continue
            if oid == "draw" and (_contains(ob, fb) or _contains(ob, gb)):
                continue  # 背景ボックス
            if _x_overlap(band, ob) > 2.0 and _y_overlap(band, ob) > 0.0:
                return False
        return True

    for f in order:
        cands = [g for g in order if g is not f and compatible(f, g)]
        if cands:
            succ[f["id"]] = min(cands, key=lambda g: g["bbox"][1])
    for f in order:
        g = succ.get(f["id"])
        if g is not None:
            ps = [h for h in order if succ.get(h["id"]) is g]
            if max(ps, key=lambda h: h["bbox"][3]) is f:
                pred[g["id"]] = f
    chains, seen = [], set()
    for f in order:
        if f["id"] in pred or f["id"] in seen:
            continue
        chain = [f]
        seen.add(f["id"])
        while True:
            g = succ.get(chain[-1]["id"])
            if g is None or pred.get(g["id"]) is not chain[-1] or g["id"] in seen:
                break
            chain.append(g)
            seen.add(g["id"])
        chains.append(chain)
    for f in order:  # 取りこぼし (循環など) は単独
        if f["id"] not in seen:
            chains.append([f])
            seen.add(f["id"])
    return chains


def _flow_page(ctx: PageCtx, targets: list[dict], ja_by_frame: dict[str, str], style: StyleBook) -> list[dict]:
    reps: list[dict] = []
    for chain in _build_chains(targets, ctx):
        reps.extend(_flow_chain(ctx, chain, ja_by_frame, style))
    return reps


def _half_lead(m: _Member) -> float:
    return min((m.lh - 1.0) * m.base / 2.0, 3.0)


def _flow_chain(ctx: PageCtx, chain: list[dict], ja_by_frame: dict[str, str], style: StyleBook,
                lim_override: float | None = None) -> list[dict]:
    """連なりの各 frame を、基準サイズ・共通の行間で、原文の段落間隔を保ちながら上から順に配置する。

    - 訳文が原文より短ければ後ろの frame は上へ詰まり、長ければ下へ送られる (連なりの下限 = 次の障害物の手前/ページ下余白)。
    - 収まらないときだけ、最も高い段落から 1 段 (5%) ずつ縮小する (縮小の下限は min_scale/min_font)。それでも駄目なら行間を詰め、
      最後は scale_low=0。"""
    cfg = ctx.cfg
    rc = cfg.section("render")
    step, min_scale, min_font = rc.get("shrink_step", 0.05), rc.get("min_scale", 0.7), rc.get("min_font", 6.5)
    hard_font = rc.get("min_font_hard", 5.5)        # min_font (6.5pt) は読みやすさの下限。収まらないときだけ、ここ (5.5pt) まで縮める (警告つき)
    margin = rc.get("expand_margin", 4.0)
    ms: list[_Member] = []
    for f in chain:
        base = style.size.get(f["id"], float(f["size"])) * ctx.size_scale
        ms.append(_Member(f=f, ja=ja_by_frame[f["id"]], x0=f["bbox"][0], x1=(_col_right(f, ctx) if f["role"] != "sidebar" else min(_col_right(f, ctx), f["bbox"][2] + 4.0)), base=base,
                          lh=style.lh.get(f["id"], rc.get("line_height", 1.5)), indent=style.indent.get(f["id"], 0.0),
                          ladder=size_ladder(base, step, min_scale, min(min_font, hard_font))))
    for m in ms:
        m.soft_k = max((k for k, v in enumerate(m.ladder) if v >= min_font - 1e-6), default=0)
    lh_base = [m.lh for m in ms]
    if lim_override is None and len(ms) > 1:
        # 末尾の frame のすぐ下 (2.5 行以内) に別の要素があるなら、その frame は原文の位置から上へ動かさず、残りの連なりと分けて解く
        # (表のキャプションなど。末尾だけが原因の溢れで連なり全体が縮小されるのを防ぐ)
        lb0 = ms[-1].f["bbox"]
        lim0 = max(below_limit(ms[-1].f, min(m.x0 for m in ms), max(m.x1 for m in ms), lb0[3], ctx, margin), lb0[3])
        if (lim0 - lb0[3]) < 2.5 * ms[-1].base:
            a, b = ms[-2], ms[-1]
            gap = b.f["bbox"][1] - a.f["bbox"][3]
            sp = min(max(gap - (a.lh - 1.0) * a.base, 0.0), 2.0 * a.base)
            top_last = b.f["bbox"][1] - _half_lead(b)
            head = _flow_chain(ctx, chain[:-1], ja_by_frame, style, lim_override=top_last - sp)
            return head + _flow_chain(ctx, chain[-1:], ja_by_frame, style)
    # 段落間の空き: 原文の (次の frame の上端 - 前の frame の下端) から、描画側の行間 ((lh-1) * size) を引いた残り
    for i in range(1, len(ms)):
        a, b = ms[i - 1], ms[i]
        gap = b.f["bbox"][1] - a.f["bbox"][3]
        b.spacing = min(max(gap - (a.lh - 1.0) * a.base, 0.0), 2.0 * a.base)
    last = ms[-1]
    x0, x1 = min(m.x0 for m in ms), max(m.x1 for m in ms)
    lb = last.f["bbox"]
    lim = max(below_limit(last.f, x0, x1, lb[3], ctx, margin), lb[3])
    if ctx.no_expand and lim_override is None:
        lim = min(lim, lb[3] + 1.0)                  # 描き直し: 下へも広げない (原文の枠の下端まで)
    if last.f["role"] in ("sidebar", "footnote") and lim_override is None:
        lim = min(lim, lb[3] + (2.0 if last.f["role"] == "sidebar" else 6.0))     # M14: サイドバー・脚注は原文の枠より下へ広げない (隣の要素・本文と重なるため。収まらなければ縮小)
    if lim_override is None:
        chain_ids = {m.f["id"] for m in ms}
        for oid, ob in ctx.obstacles:
            if oid in chain_ids or oid in ("img", "draw"):
                continue
            of = next((f for f in ctx.pdata["frames"] if f["id"] == oid), None)
            if of is not None and of["role"] == "heading" and 0 <= ob[1] - lb[3] <= 3.0 * last.base \
                    and _x_overlap([x0, 0, x1, 0], ob) < 2.0:
                lim = min(lim, ob[1] - last.lh * last.base)   # 見出しの手前に最低 1 行ぶんの余白
    if lim_override is not None:
        lim = lim_override
    elif len(ms) == 1:
        last.anchored = True
    top0 = ms[0].f["bbox"][1] - _half_lead(ms[0])
    if ms[0].f["role"] == "footnote":                  # 脚注の上の罫線 (短い横線): 罫線より下から始める (罫線に取り消し線のように重ならない)
        fb = ms[0].f["bbox"]
        for d in ctx.drawings:
            if (d[3] - d[1]) <= 2.0 and (d[2] - d[0]) >= 30.0 and fb[1] - 16.0 <= d[3] <= fb[1] + 1.0 and d[0] <= fb[2] and d[2] >= fb[0]:
                top0 = max(top0, d[3] + 1.5)  # 先頭の行の字面が原文の位置に来るよう、行間の半分だけ上げる

    def layout() -> tuple[list[float], list[float]]:
        tops, ends = [], []
        y = top0
        for i, m in enumerate(ms):
            t = y if i == 0 else y + m.spacing
            if (m.anchored or ctx.ocr_fill is not None) and i > 0:
                t = max(t, m.f["bbox"][1] - _half_lead(m))   # スキャン: 訳文が短くても、各段落は原文の位置から始める (上へ詰めない)
            tops.append(t)
            y = t + _measure(ctx, m, m.size, m.lh)
            ends.append(y)
        return tops, ends

    tol = 5.0 if max(int(last.f.get("nrows", 1)), 1) == 1 else 2.5   # 1 行だけの frame は、字の下の余白 (行間) ぶん多めに許す
    hl = _half_lead(last)   # 最後の行の下の半行ぶん (字の無い行間) は限界を越えてよい
    tops, ends = layout()
    # 収まらないときは、文字を縮める前に (a) 行間を詰める ([render] line_height_floor。既定 1.3: 1.2 以下に下げると、次の frame の行と bbox が 3pt 以上重なる場合がある。1 行だけの frame (ラベルなど) は 1.05 まで)、
    # (b) 段落間の空きと字下げを詰める、の順に試す。それでも収まらないときだけ (c) 文字を縮める (下限は min_font / min_scale)。
    reflow_first = bool(rc.get("reflow_first", True))      # False = M12 までの順序 (縮小 -> 行間。比較用)
    lh_cut = 0.0
    single = all(max(int(m.f.get("nrows", 1)), 1) == 1 for m in ms)
    lh_floor = float(rc.get("line_height_floor", 1.3))
    lh_max_cut = max(0.0, max(lh_base) - lh_floor)
    while reflow_first and ends[-1] - hl > lim - 4.0 and lh_cut < lh_max_cut:      # 行間を詰めるのは、余裕をもって収まるところまで (tol は使わない)
        lh_cut += 0.04
        for m, lb0 in zip(ms, lh_base):
            m.lh = max(lh_floor, lb0 - lh_cut)
        tops, ends = layout()
    for fac in ((0.5, 0.0) if (reflow_first and rc.get("reflow_spacing", True)) else ()):
        if ends[-1] - hl > lim + tol and any(m.spacing > 0 or m.indent > 0 for m in ms):
            for m in ms:
                m.spacing *= fac
                m.indent *= fac if fac else 0.0
            for m in ms:
                m.heights.clear()
                m.narrow.clear()
            tops, ends = layout()
    below_floor = False
    while ends[-1] - hl > lim + tol:
        cand = [i for i, m in enumerate(ms) if m.k + 1 <= m.soft_k and m.k + 1 < len(m.ladder)]
        if not cand:
            cand = [i for i, m in enumerate(ms) if m.k + 1 < len(m.ladder)]        # 読みやすさの下限でも収まらない: 最終手段として下限より小さくする
            below_floor = bool(cand) or below_floor
        if not cand:
            break
        i = max(cand, key=lambda j: (_measure(ctx, ms[j], ms[j].size, ms[j].lh) / (1 + ms[j].k), j))
        ms[i].k += 1
        tops, ends = layout()
    if True:      # 縮小のあとも収まらないときの最終手段としての行間詰め (下限 1.2。1 行の frame は 1.05)
        lh_floor = 1.05 if single else 1.2
        lh_max_cut = max(0.2, max(lh_base) - lh_floor)
        while ends[-1] - hl > lim + tol and lh_cut < lh_max_cut:
            lh_cut += 0.04
            for m, lb0 in zip(ms, lh_base):
                m.lh = max(lh_floor, lb0 - lh_cut)
            tops, ends = layout()
    overflow = ends[-1] - hl > lim + tol
    for m in ms:
        _avoid_widow(ctx, m)
    if ctx.ocr_fill is not None:
        # 連なりの領域 (原文の行 ∪ 訳文を流し込む範囲) を、紙の色で 1 度に塗って英語を隠す。連なりの間には他の frame・図が無い
        # (連なりの条件) ので、図や翻訳しない frame には触れない。原文の行の上下に余裕を持たせる (OCR の bbox は画像の文字とずれる)
        fy0 = min(min(m.f["bbox"][1] for m in ms) - 2.2, tops[0])
        fy1 = max(max(m.f["bbox"][3] for m in ms) + 2.6, ends[-1] + 0.5)
        fx0 = min(m.x0 for m in ms) - 3.0
        fx1 = max(m.x1 for m in ms) + 3.0
        ctx.page.draw_rect((fitz.Rect(fx0, fy0, fx1, fy1) * ctx.D).normalize(), color=None, fill=ctx.ocr_fill, width=0)
    reps = []
    for i, m in enumerate(ms):
        f = m.f
        top = tops[i]
        bottom = top + _measure(ctx, m, m.size, m.lh)
        is_last = i == len(ms) - 1
        base_rep = {"id": f["id"], "role": f["role"], "size_orig": float(f["size"]), "size_base": m.base,
                    "expanded": bottom > f["bbox"][3] + 1.0, "shift": round(top - f["bbox"][1], 1)}
        raw_kw = {"rotate": ctx.rotation} if ctx.rotation else {}

        def put(rect: fitz.Rect, size: float, scale_low: int):
            if ctx.ocr_fill is not None and rect.y1 > fy1:
                ctx.page.draw_rect((fitz.Rect(rect.x0 - 1, fy1, rect.x1 + 1, rect.y1 + 0.5) * ctx.D).normalize(),
                                   color=None, fill=ctx.ocr_fill, width=0)    # 最終手段で枠を広げたときのはみ出し分
            return ctx.page.insert_htmlbox((rect * ctx.D).normalize(), _member_html(ctx, m, size, m.lh),
                                           css=ctx.fonts.css, archive=ctx.fonts.archive, scale_low=scale_low, **raw_kw)

        spare = -1.0
        x1d = m.x1 - m.narrow.get((round(m.size, 2), round(m.lh, 3)), 0.0)
        if not (is_last and overflow):
            for extra in (0.0, 1.5, 3.0, 6.0):
                spare, _sc = put(fitz.Rect(m.x0, top, x1d, bottom + extra), m.size, 1)
                if spare >= 0:
                    break
        if spare >= 0:
            rr = {**base_rep, "stage": "flow" if m.k == 0 else "flow_shrunk", "size_used": m.size,
                  "ratio": round(m.size / m.base, 3), "spare": round(spare, 1), "line_height": round(m.lh, 2)}
            if m.size < min_font - 1e-6:
                rr["warning"] = f"読みやすさの下限 {min_font}pt を下回る縮小 ({m.size}pt) で描画 (枠に収まらない)"
            reps.append(rr)
            continue
        # 最終手段 2: 残りの枠いっぱいまで scale_low=0 で縮小
        rect = fitz.Rect(m.x0, top, x1d, max(lim, top + 4.0) if is_last else bottom + 6.0)
        spare, scale = put(rect, m.base, 0)
        if spare < 0:
            reps.append({**base_rep, "stage": "failed", "size_used": 0, "ratio": 0.0, "spare": -1,
                         "warning": "描画に失敗 (枠が小さすぎる)", "line_height": round(m.lh, 2)})
            continue
        r = {**base_rep, "stage": "scale_low0", "size_used": round(m.base * scale, 2), "ratio": round(scale, 3),
             "spare": round(spare, 1), "line_height": round(m.lh, 2)}
        if scale < min_scale:
            r["warning"] = f"縮小下限 {min_scale} を下回る縮小 ({scale:.2f}) で描画 (訳文が枠に対して長い)"
        reps.append(r)
    return reps


def summarize(report: list[dict]) -> dict:
    n = len(report)
    shrunk = [r for r in report if r["ratio"] < 0.999]
    failed = [r for r in report if r["stage"] == "failed"]
    ratios = [r["ratio"] for r in report if r["ratio"] > 0]
    return {
        "frames": n,
        "shrunk": len(shrunk),
        "shrunk_ratio": round(len(shrunk) / n, 4) if n else 0.0,
        "min_scale": min(ratios) if ratios else 1.0,
        "failed": len(failed),
        "expanded": sum(1 for r in report if r["expanded"]),
        "stages": {s: sum(1 for r in report if r["stage"] == s) for s in sorted({r["stage"] for r in report})},
        "warnings": [f"p{r['page']} {r['id']}: {r['warning']}" for r in report if r.get("warning")],
        "details": report,
    }


# --------------------------------------------------------------------------
# 交互版
# --------------------------------------------------------------------------

def _resolved_links(doc: fitz.Document, page: fitz.Page, names: dict) -> list[dict]:
    """ページのリンクを、名前付き宛先を解決した形 (GOTO) で返す。"""
    out = []
    for l in page.get_links():
        if l["kind"] == fitz.LINK_NAMED:
            tgt = names.get(l.get("nameddest") or l.get("name"))
            if tgt is None:
                continue
            out.append({"kind": fitz.LINK_GOTO, "from": fitz.Rect(l["from"]), "page": tgt.get("page", 0),
                        "to": fitz.Point(tgt.get("to", (0, 0))), "zoom": tgt.get("zoom", 0.0)})
        else:
            out.append(_clean_link(l))
    return out


def build_dual(orig_pdf: str | Path, ja_pdf: str | Path, out_pdf: str | Path, subset: bool = True) -> int:
    """交互版: 英p1, 日p1, 英p2, 日p2 ...。

    原文を土台にして (しおり・名前付き宛先・リンクを保持) 日本語ページを 1 回だけ insert_pdf で末尾に足し、
    move_page で各英語ページの直後に並べ替える (フォントの重複コピーを避ける)。
    - 英語ページのリンク/しおりは原文のページ参照のまま (宛先は 2p-1 ページ目に自動で追従)。しおりは set_toc で明示的に写像。
    - 日本語ページのリンクは、宛先を日本語ページ (2p) に向けて作り直す。名前付き宛先は解決して GOTO にする。
    """
    dual = fitz.open(str(orig_pdf))
    ja = fitz.open(str(ja_pdf))
    n = len(dual)
    toc = dual.get_toc(simple=False)
    ja_names = ja.resolve_names() if hasattr(ja, "resolve_names") else {}
    ja_links = [_resolved_links(ja, ja[p], ja_names) for p in range(n)]
    dual.insert_pdf(ja, links=False)
    for p in range(n):
        dual.move_page(n + p, 2 * p + 1)
    # 日本語ページ (index 2p+1) のリンク: 宛先は日本語ページ (2q+1)
    for p in range(n):
        page = dual[2 * p + 1]
        for l in ja_links[p]:
            d = dict(l)
            if d["kind"] == fitz.LINK_GOTO:
                d["page"] = 2 * min(max(int(d.get("page", 0)), 0), n - 1) + 1
            try:
                page.insert_link(d)
            except Exception:  # pragma: no cover
                pass
    # しおり: 原文ページ p -> 2p-1 (1 始まり)
    if toc:
        new_toc = []
        for ent in toc:
            lvl, title, pg = ent[0], ent[1], ent[2]
            npg = 2 * (pg - 1) + 1 if pg > 0 else pg
            dest = {k: v for k, v in (ent[3] if len(ent) > 3 else {}).items() if k in ("kind", "to", "zoom", "collapse", "color", "bold", "italic")}
            if dest.get("kind") not in (fitz.LINK_GOTO, None) and pg > 0:
                dest = {}
            new_toc.append([lvl, title, npg, dest] if dest else [lvl, title, npg])
        try:
            dual.set_toc(new_toc)
        except Exception:  # pragma: no cover
            dual.set_toc([[e[0], e[1], e[2]] for e in new_toc])
    if subset:
        dual.subset_fonts()
    Path(out_pdf).parent.mkdir(parents=True, exist_ok=True)
    dual.save(str(out_pdf), garbage=4, deflate=True)
    dual.close()
    ja.close()
    return 2 * n


def count_links_toc(pdf: str | Path, pages: list[int] | None = None) -> tuple[int, int]:
    """(リンク数, しおり数)。pages (0 始まり) を指定するとそのページのリンクだけ数える。"""
    d = fitz.open(str(pdf))
    idx = range(len(d)) if pages is None else pages
    nl = sum(len(d[i].get_links()) for i in idx)
    nt = len(d.get_toc())
    d.close()
    return nl, nt


def save_report(rep: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
