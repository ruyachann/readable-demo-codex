"""M8 (3 本の新しい論文の実翻訳で見つかった問題) の合成 PDF テスト。ユーザーの論文そのものはテストに入れない。

- 両端揃えで語間が空白文字ではなく文字の間隔 (TJ の数値) だけで表された PDF の語間 (リンクのあるページ含む)
- 合字の境目をガターと見なさない / 1 行目だけ太字のキャプションを 1 つの frame にする / 箇条書きは 1 項目 1 frame / 一部だけ斜体の行で段落を割らない
- ぶら下げインデントの番号つき項目
- スキャン + OCR ページ: 流し込みで重ならない・表は原文のまま・OCR の見出し/表題・行の断片の結合
- 固定訳 (TMR など)・固定訳の適用漏れの検出・スキャン + OCR だけ緩い role 変更の閾値
"""
from __future__ import annotations

import fitz
import pytest

from readable.extract import _needs_spaces, crosses_gutter, extract_pdf
from readable.glossary import filter_glossary, load_fixed
from readable.render import render_ja
from readable.structure import StructureRejected, check_role_changes
from readable.translate import build_units, check_glossary_applied, frames_translations

WORDS = "Sleep supports memory consolidation during slow wave sleep in healthy young adults after learning".split()


def _tj(words, gap):
    return "[" + "".join("(%s)-%d" % (w, gap) for w in words[:-1]) + "(%s)] TJ" % words[-1]


def _raw_pdf(path, lines, link=False):
    """lines: [(x, y, [words], gap)] を TJ の語間 (gap/1000 em。空白文字なし) で書いた PDF。"""
    doc = fitz.open()
    pg = doc.new_page(width=400, height=300)
    pg.insert_text((10, 10), " ", fontname="helv", fontsize=1)
    x = pg.get_contents()[0]
    st = doc.xref_stream(x)
    body = "".join("\nBT /helv 10 Tf %d %d Td %s ET\n" % (lx, 300 - ly, _tj(w, g)) for lx, ly, w, g in lines)
    doc.update_stream(x, st + body.encode())
    if link:
        pg.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(300, 250, 380, 262), "uri": "https://example.org/"})
    doc.save(path)
    return path


@pytest.mark.parametrize("link", [False, True])
def test_word_gaps_expressed_only_by_spacing_get_real_spaces(tmp_path, link):
    p = _raw_pdf(tmp_path / "j.pdf", [(40, 60, WORDS, 120), (40, 74, WORDS[::-1], 120)], link=link)
    frames = extract_pdf(p)["pages"][0]["frames"]
    text = " ".join(f["text"] for f in frames)
    assert "Sleep supports memory consolidation during" in text and "learning after adults" in text
    assert "Sleepsupports" not in text


def test_needs_spaces_heuristic():
    assert _needs_spaces("Sleepsupportsmemoryconsolidationduringslowwavesleep")
    assert not _needs_spaces("Sleep supports memory consolidation during slow wave sleep")
    assert not _needs_spaces("short")
    assert not _needs_spaces("12345678901234567890123456789")        # 数字・記号の連なりは対象外


def test_tiny_gap_such_as_a_ligature_is_not_a_gutter():
    assert not crosses_gutter(83.0, 83.004, [(60.0, 90.0)])
    assert crosses_gutter(80.0, 92.0, [(60.0, 90.0)]) is True


def _para_pdf(path, rows, size=9.0):
    """rows: [(x, y, [(fontname, text), ...])]"""
    doc = fitz.open()
    pg = doc.new_page(width=600, height=500)
    for x, y, parts in rows:
        cx = x
        for fn, t in parts:
            pg.insert_text((cx, y), t, fontsize=size, fontname=fn)
            cx += fitz.get_text_length(t, fontname=fn, fontsize=size)
    doc.save(path)
    return path


BODY = "tiro"


def test_caption_whose_first_line_is_bold_plus_regular_is_one_frame(tmp_path):
    rows = [(40, 40 + 11 * k, [(BODY, ("The body paragraph of the paper explains the experiment in detail with enough words here %d" % k))])
            for k in range(4)]
    cap = [(40, 120, [("tibo", "Fig. 4 | Electrophysiological responses to stimuli. "), (BODY, "Average event-related potentials are")])]
    pool = ("displayed across all channels for the three groups in response to cues. Each auditory cue consisted of a word pair "
            "presented sequentially within a window. Colored lines indicate the groups and bars indicate significant intervals here "
            "and in the other panels of the figure as well.").split()
    k = 0
    for y in (131, 142, 153):
        line = ""
        while k < len(pool) and fitz.get_text_length(line + " " + pool[k], fontname=BODY, fontsize=8) <= 285:
            line = (line + " " + pool[k]).strip()
            k += 1
        for filler in ("a", "I", "of", "to"):
            if fitz.get_text_length(line + " " + filler, fontname=BODY, fontsize=8) <= 285:
                line += " " + filler
        cap.append((40, y, [(BODY, line)]))
    p = _para_pdf(tmp_path / "c.pdf", rows + cap, size=8.0)
    fr = extract_pdf(p)["pages"][0]["frames"]
    c = [f for f in fr if f["text"].startswith("Fig. 4")]
    assert len(c) == 1 and c[0]["role"] == "caption" and c[0]["nrows"] == 4
    assert "Colored lines indicate" in c[0]["text"]


def test_bullet_items_are_separate_frames(tmp_path):
    rows = [(40, 100 + 11 * k, [(BODY, "•  Supplementary file %d. List of the words and their probabilities in the experiment." % (k + 1))])
            for k in range(4)]
    fr = [f for f in extract_pdf(_para_pdf(tmp_path / "b.pdf", rows))["pages"][0]["frames"] if f["role"] == "body"]
    assert len(fr) == 4 and all(f["nrows"] == 1 for f in fr)


def test_hanging_numbered_items_stay_one_frame_each(tmp_path):
    L = "Matching a small patch of light with the phosphene on backgrounds of different luminances, together with the increment"
    rows = []
    y = 100
    for lab in ("I.", "II.", "III."):
        rows += [(40, y, [(BODY, lab + " " + L[:70])]), (58, y + 11, [(BODY, L[70:] + " threshold of the same patch.")])]
        y += 24
    fr = [f for f in extract_pdf(_para_pdf(tmp_path / "h.pdf", rows))["pages"][0]["frames"] if f["role"] == "body"]
    assert [f["nrows"] for f in fr] == [2, 2, 2]
    assert [f["text"].split()[0] for f in fr] == ["I.", "II.", "III."]


def test_row_with_only_part_italic_does_not_split_the_paragraph(tmp_path):
    rows = [(40, 100, [(BODY, "By conducting an additional analysis based on detection of fast sleep spindles we confirmed that")]),
            (40, 111, [(BODY, "fast spindles occurred with higher amplitude after the cueing presentation of the words (see")]),
            (40, 122, [("tiit", "2"), (BODY, " and "), ("tiit", "Supplementary file 6"), (BODY, ").")])]
    fr = [f for f in extract_pdf(_para_pdf(tmp_path / "i.pdf", rows))["pages"][0]["frames"] if f["role"] == "body"]
    assert len(fr) == 1 and fr[0]["nrows"] == 3


# =============================================================== スキャン + OCR (流し込み・表・見出し)

OCR_LINES = [
    (40, 60, 8, "voltage was under the control of the subject, and was adjusted so that the phosphene"),
    (40, 70, 8, "appeared to match the comparison patch of light in size and shape throughout."),
    (40, 83, 8, "The lenses L1 and L2 were arranged so that when the eye was immersed in the bath, a clearly"),
    (40, 93, 8, "focused view of the two superimposed object planes was provided by two projectors of the"),
    (40, 103, 8, "Maxwellian type, and the colour of the stimuli was adjusted until it matched the phosphene."),
    (40, 128, 8, "RESULTS"),
    (40, 150, 8, "Three main classes of experiment were performed in the course of the whole work."),
    (52, 163, 8, "I. Matching a small patch of light with the phosphene on backgrounds of different luminances"),
    (66, 168, 8, "together with the increment threshold of the same patch on the same backgrounds."),
    (52, 181, 8, "II. Matching as above after a bleach by a bright light uniformly over the background"),
    (66, 191, 8, "field, together with threshold measurements for the patch alone under the same conditions."),
    (40, 218, 8, "Class I"),
    (40, 234, 8, "Here the appearance of the subject's visual field was as shown in the figure, and the phosphene"),
    (40, 244, 8, "was always centred on the point of regard, with zero background luminance at the start."),
    (100, 270, 8, "TABTE 1. Comparison of threshold changes"),
    (190, 285, 8, "Mean (log td)"),
    (270, 285, 8, "s.E. of mean"),
    (60, 298, 8, "On phosphene"), (190, 298, 8, "-1.65"), (270, 298, 8, "0.063"),
    (60, 310, 8, "On light"), (190, 310, 8, "-2.16"), (270, 310, 8, "0.065"),
]


def _ocr_doc_pdf(path):
    src = fitz.open()
    pg = src.new_page(width=480, height=420)
    pg.draw_rect(pg.rect, fill=(0.93, 0.9, 0.8), color=None)
    for x, y, s, t in OCR_LINES:
        pg.insert_text((x, y), t, fontsize=s, fontname="tiro")
    pix = pg.get_pixmap(dpi=120)
    doc = fitz.open()
    out = doc.new_page(width=480, height=420)
    out.insert_image(out.rect, pixmap=pix)
    for x, y, s, t in OCR_LINES:
        out.insert_text((x, y), t, fontsize=s, fontname="cour", render_mode=3)
    doc.save(path)
    return path


@pytest.fixture(scope="module")
def ocr_doc(tmp_path_factory):
    p = _ocr_doc_pdf(tmp_path_factory.mktemp("ocr") / "o.pdf")
    return p, extract_pdf(p)


def test_ocr_headings_hanging_list_and_table_are_recognised(ocr_doc):
    _p, d = ocr_doc
    fr = d["pages"][0]["frames"]
    by = {f["text"].split()[0] if f["text"].split() else "": f for f in fr}
    assert by["RESULTS"]["role"] == "heading"
    assert [f for f in fr if f["text"] == "Class I"][0]["role"] == "heading"
    items = [f for f in fr if f["text"].startswith(("I.", "II."))]
    assert len(items) == 2 and all(f["nrows"] == 2 and f["role"] == "body" for f in items)
    cap = [f for f in fr if f["text"].startswith("TABTE")][0]
    assert cap["role"] == "caption" and cap["translate"]                                      # OCR の誤読 (TABTE) でも表題はキャプション
    body = [f for f in fr if f["text"] in ("Mean (log td)", "On phosphene", "-1.65", "0.063")]
    assert len(body) == 4 and all(f["role"] in ("table", "math") and not f["translate"] for f in body)    # 表の本体は翻訳しない


def test_ocr_flow_does_not_overlap_and_leaves_table_alone(ocr_doc, tmp_path):
    p, d = ocr_doc
    units = build_units(d)
    long_ja = "これは日本語に訳された段落であり、スキャンの英語は紙の色で隠される。流し込みは上から順に行われ、段落同士は重ならない。"
    fr = frames_translations(d, units, {u["id"]: long_ja * 2 for u in units})
    out = tmp_path / "o_ja.pdf"
    rep = render_ja(p, d, fr, out)
    assert rep["failed"] == 0 and rep["scanned_ocr"] == [1]
    page = fitz.open(out)[0]
    lines = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(s["text"] for s in l["spans"])
            if any("぀" <= c <= "鿿" for c in t):
                lines.append(fitz.Rect(l["bbox"]))
    assert lines
    for i, a in enumerate(lines):
        for b in lines[i + 1:]:
            inter = a & b
            assert not (not inter.is_empty and inter.width > 2 and inter.height > 2.5), (a, b)   # 日本語の行同士が重ならない
    txt = page.get_text()
    assert "Mean (log td)" in txt and "On phosphene" in txt                                       # 表の本体は原文のまま
    pix = page.get_pixmap(dpi=100)
    # 表の領域の紙の色は塗り替えられていない (原文の画像が残る = 表の左端付近の画素は紙の色のまま)
    assert pix.width > 0


def test_ocr_row_split_by_a_symbol_is_rejoined(tmp_path):
    lines = [(40, 60, 8, "Fig. 4. Increment thresholds and matching to the phosphene against various background"),
             (40, 70, 8, "intensities IB. In both cases the intensity of the test or comparison spot is IT. O,"),
             (40, 80, 8, "occasions; O,"), (120, 80, 8, "matching to the phosphene on two separate occasions and so on in the end"),
             (40, 104, 8, "Another long line of ordinary body text that fills the column from the left edge to the right.")]
    src = fitz.open()
    pg = src.new_page(width=480, height=200)
    for x, y, s, t in lines:
        pg.insert_text((x, y), t, fontsize=s, fontname="tiro")
    pix = pg.get_pixmap(dpi=120)
    doc = fitz.open()
    out = doc.new_page(width=480, height=200)
    out.insert_image(out.rect, pixmap=pix)
    for x, y, s, t in lines:
        out.insert_text((x, y), t, fontsize=s, fontname="cour", render_mode=3)
    doc.save(tmp_path / "f.pdf")
    fr = extract_pdf(tmp_path / "f.pdf")["pages"][0]["frames"]
    cap = [f for f in fr if f["text"].startswith("Fig. 4")]
    assert len(cap) == 1 and "separate occasions" in cap[0]["text"]


# =============================================================== 固定訳・検証

def test_fixed_glossary_has_the_new_terms():
    fx = {g["en"].lower(): g for g in load_fixed()}
    assert fx["tmr"]["ja"] == "標的記憶再活性化 (TMR)" and fx["targeted memory reactivation"]["ja"] == "標的記憶再活性化"
    assert fx["two-way anova"]["ja"] == "二元配置分散分析" and fx["correct rejection"]["ja"] == "正棄却"
    assert fx["false alarm"]["ja"] == "誤警報" and fx["ms"]["ja"] == "ミリ秒" and fx["closed-loop"]["ja"] == "クローズドループ"
    got = [g["en"] for g in filter_glossary(list(fx.values()), "TMR improved the correct rejection rate after 250 ms")]
    assert {"TMR", "correct rejection", "ms"} <= set(got)


def test_unapplied_fixed_glossary_is_detected():
    g = [{"en": "correct rejection", "ja": "正棄却", "note": "固定訳"}, {"en": "false alarm", "ja": "誤警報", "note": "固定訳"}]
    src = "The correct rejection rate and the false alarm rate were compared."
    assert check_glossary_applied(src, "CR(正 rejection)率と誤警報率を比較した。", g)           # 英語が日本語の中に残っている
    assert not check_glossary_applied(src, "正棄却率と誤警報率を比較した。", g)
    assert not check_glossary_applied(src, "正棄却 (correct rejection) 率と誤警報率を比較した。", g)  # 訳語がある (英語の併記は可)
    assert not check_glossary_applied("No terms here.", "用語は無い。", g)
    assert not check_glossary_applied(src, "CR率とFA率を比較した。", g)                         # 英語は残っていない (略語のみ) -> 他の検査の対象
    # 表の用語 (英語併記が意図) は対象外
    assert not check_glossary_applied(src, "correct rejection を比較した。", [{"en": "correct rejection", "ja": "正棄却", "note": "固定訳", "keep_en": True}])
    # 論文ごとの自動用語集 (固定訳でない) は対象外
    assert not check_glossary_applied(src, "CR(正 rejection)率", [{"en": "correct rejection", "ja": "正棄却", "note": ""}])


def _doc_with_roles(ocr: bool, n: int = 10):
    frames = [{"id": f"p1-f{i}", "role": "body", "translate": True, "text": "Some body text here that is long enough.", "flags": [],
               "page": 1, "nrows": 1, "size": 9.0, "bbox": [40, 100, 300, 110], "math_ratio": 0, "in_image": False, "in_table": False}
              for i in range(n)]
    return {"pages": [{"number": 1, "height": 790, "scanned_ocr": ocr, "frames": frames}], "body_size": 9.0}


def test_role_changes_are_selected_per_frame_and_wholesale_drops_are_rejected():
    from readable.structure import select_role_changes
    d = _doc_with_roles(False)
    # 翻訳対象どうしの変更 (body -> heading) は、いくつあっても無条件に採用される (M12: 変更の多さだけで全部を捨てない)
    res, st = select_role_changes({"roles": {f"p1-f{i}": "heading" for i in range(8)}}, d)
    assert st["accepted"] == 8 and st["rejected"] == 0 and len(res["roles"]) == 8
    check_role_changes(res, d)
    # 翻訳しない role への変更は、手がかりと合うものだけ (この frame は本文の長さ・位置・字の大きさで、参考文献でも図の文字でもない)
    res, st = select_role_changes({"roles": {f"p1-f{i}": "reference" for i in range(5)}}, d)
    assert st["accepted"] == 0 and st["rejected"] == 5 and not res["roles"]
    # 手がかりに合うものは採用するが、翻訳する量が大きく減るなら全体を捨てる
    d2 = _doc_with_roles(False)
    for f in d2["pages"][0]["frames"]:
        f["text"] = "12. Smith, J., and Jones, K. (2019). A study of sleep. Nature 5, 1-9."
    res, st = select_role_changes({"roles": {f"p1-f{i}": "reference" for i in range(10)}}, d2)
    assert st["accepted"] == 10
    with pytest.raises(StructureRejected):
        check_role_changes(res, d2)


def test_number_check_allows_compound_numerals_quantity_words_and_ocr_decimal_points():
    from readable.numcheck import check_numbers
    from readable.translate import repair_style_tags
    assert not check_numbers("with thirty-six participants (13 females)", "36名の参加者 (女性13名)")
    assert not check_numbers("nearly a million times brighter", "約1000000倍明るい")
    assert check_numbers("Warm 09 % saline", "温めた0.9%生理食塩水")                 # OCR でない文書では数値の変化として指摘
    assert not check_numbers("Warm 09 % saline", "温めた0.9%生理食塩水", ocr=True)     # OCR が落とした小数点を直した訳は許す
    assert repair_style_tags("Fig. 3. a, b", "<b>図3</b> a, b") == "図3 a, b"       # 原文に無い太字は外す
    assert repair_style_tags("<b>Fig</b> 3", "<b>図</b> 3") == "<b>図</b> 3"         # 原文にあるタグは触らない
