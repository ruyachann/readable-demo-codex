"""M7 (抽出の一般化) の合成 PDF テスト: 2 段組のガター、ベクタ図の中の文字、1 ページ目の誌名・サイドバー、見出しの誤判定、キャプションの続き。
ユーザーの論文そのものはテストに入れない。"""
from __future__ import annotations

import textwrap
from pathlib import Path

import fitz

from readable.extract import (crosses_gutter, extract_pdf, find_gutters, vector_figure_regions)

SENT = ("Sleep supports memory consolidation and the targeted reactivation of learned material during slow wave sleep "
        "improves recall after one night in healthy young adults according to several recent studies. ")


def two_column_pdf(path: Path, gutter: float = 12.0, size: float = 9.0) -> Path:
    """全幅のタイトル・要旨 + その下は 2 段組の本文 (列間 gutter pt)。左右の行は同じ基線に並ぶ。"""
    doc = fitz.open()
    pg = doc.new_page(width=595, height=790)
    pg.insert_text((40, 60), "A study of memory consolidation during sleep", fontsize=22, fontname="tibo")
    abstract = textwrap.wrap(SENT * 4, 120)
    for k, line in enumerate(abstract[:5]):
        pg.insert_text((40, 100 + k * 11.5), line, fontsize=size, fontname="tiro")
    colw = (595 - 80 - gutter) / 2
    chars = int(colw / (size * 0.47))
    left = textwrap.wrap(SENT * 12, chars)
    right = textwrap.wrap((SENT + "More text follows here. ") * 12, chars)
    for k in range(26):
        y = 200 + k * 11.5
        pg.insert_text((40, y), left[k], fontsize=size, fontname="tiro")
        pg.insert_text((40 + colw + gutter, y), right[k], fontsize=size, fontname="tiro")
    doc.save(path)
    return path


def test_two_column_page_is_split_at_the_gutter_and_full_width_blocks_are_exempt(tmp_path):
    d = extract_pdf(two_column_pdf(tmp_path / "t.pdf"))
    frames = d["pages"][0]["frames"]
    colw = (595 - 80 - 12) / 2
    body = [f for f in frames if f["bbox"][1] > 190 and f["nrows"] >= 3]
    assert body and all((f["bbox"][2] - f["bbox"][0]) < 0.6 * 595 for f in body)           # どの段落も 1 つの列に収まる
    xs = sorted({round(f["bbox"][0]) for f in body})
    assert xs[0] == 40 and any(abs(x - (40 + colw + 12)) < 3 for x in xs)                   # 左右の列が別の frame
    assert not any(f["bbox"][0] < 40 + colw < f["bbox"][2] and f["bbox"][1] > 190 for f in frames)    # ガターをまたぐ frame は無い
    wide = [f for f in frames if f["bbox"][1] < 190 and (f["bbox"][2] - f["bbox"][0]) > 0.7 * 595]
    assert wide                                                                              # 全幅のタイトル・要旨はそのまま (ガターが無い帯)
    assert any(f["role"] == "title" for f in frames)


def test_narrow_gutter_and_helpers(tmp_path):
    d = extract_pdf(two_column_pdf(tmp_path / "t.pdf", gutter=9.0))
    body = [f for f in d["pages"][0]["frames"] if f["bbox"][1] > 190 and f["nrows"] >= 3]
    assert all((f["bbox"][2] - f["bbox"][0]) < 0.6 * 595 for f in body)
    assert crosses_gutter(290, 300, [(289, 301)]) and not crosses_gutter(100, 104, [(289, 301)])


def test_find_gutters_ignores_justified_word_gaps_and_indents():
    # 1 段組の本文 (行ごとに語間が違う・段落の字下げがある) ではガターを検出しない
    lines = []
    for k in range(40):
        gap = 8 + (k * 7) % 11          # 語間 (8〜18pt) が行ごとにばらつく
        sp = [{"bbox": [40, 100 + k * 12, 200, 110 + k * 12], "text": "aaaa", "size": 9, "flags": 0, "origin": (40, 108 + k * 12)},
              {"bbox": [200 + gap, 100 + k * 12, 520, 110 + k * 12], "text": "bbbb", "size": 9, "flags": 0, "origin": (200 + gap, 108 + k * 12)}]
        lines.append({"spans": sp, "base": 108 + k * 12, "size": 9, "stream": k, "x0": 40, "x1": 520})
    assert find_gutters(lines, 790) == []


def test_vector_figure_text_is_not_translated_but_caption_is(tmp_path):
    doc = fitz.open()
    pg = doc.new_page(width=595, height=790)
    pg.insert_text((40, 60), "The measured response is shown in Figure 1 below for all conditions in the experiment as described.", fontsize=10, fontname="tiro")
    # ベクタのプロット: 曲線・折れ線・目盛り
    pg.draw_rect(fitz.Rect(100, 150, 480, 400), color=(0, 0, 0))
    for i in range(14):
        pg.draw_line((100 + i * 27, 400), (100 + i * 27, 406))
        pg.draw_line((94, 150 + i * 18), (100, 150 + i * 18))
    for j in range(3):
        pg.draw_bezier((100, 380 - j * 30), (200, 150 + j * 30), (300, 420 - j * 20), (480, 200 + j * 30), color=(0.2 * j, 0, 1))
    for t, (x, y) in {"Amplitude": (58, 260), "Frequency (Hz)": (250, 425), "Condition A": (120, 140), "0.5": (80, 400)}.items():
        pg.insert_text((x, y), t, fontsize=7, fontname="helv")
    pg.insert_text((40, 470), "Fig. 1 | Measured response. The curves show the amplitude of the response for each condition studied.", fontsize=8, fontname="helv")
    pg.insert_text((40, 520), "The text after the figure is ordinary body text that must still be translated by the pipeline.", fontsize=10, fontname="tiro")
    path = tmp_path / "v.pdf"
    doc.save(path)
    assert vector_figure_regions(fitz.open(path)[0])
    d = extract_pdf(path)
    fr = d["pages"][0]["frames"]
    labels = [f for f in fr if f["text"] in ("Amplitude", "Frequency (Hz)", "Condition A", "0.5")]
    assert labels and all(f["role"] == "figure_text" and not f["translate"] for f in labels)
    cap = next(f for f in fr if f["text"].startswith("Fig. 1"))
    assert cap["role"] == "caption" and cap["translate"]                                      # キャプションは図の領域の近くでも翻訳する
    assert any(f["role"] == "body" and f["translate"] and f["text"].startswith("The text after") for f in fr)


def test_table_rules_alone_are_not_a_vector_figure(tmp_path):
    doc = fitz.open()
    pg = doc.new_page(width=595, height=790)
    for y in (150, 170, 260):
        pg.draw_line((50, y), (500, y))
    doc.save(tmp_path / "r.pdf")
    assert vector_figure_regions(fitz.open(tmp_path / "r.pdf")[0]) == []


def test_first_page_masthead_left_sidebar_headings_and_caption_continuation(tmp_path):
    doc = fitz.open()
    pg = doc.new_page(width=612, height=792)
    pg.insert_text((169, 36), "Journal of Examples | science", fontsize=15, fontname="tibo")           # 誌名 (最上部)
    pg.insert_text((169, 105), "Difficulty in artificial word learning impacts targeted memory", fontsize=20, fontname="tibo")
    pg.insert_text((169, 125), "reactivation during sleep", fontsize=20, fontname="tibo")
    # 左の細い列 (サイドバー): 小さい文字
    for k, t in enumerate(["*For correspondence:", "a.b@example.org (AB)", "Sent for Review", "21 August 2023", "Reviewing Editor: Hong"]):
        pg.insert_text((37, 380 + k * 22), t, fontsize=8, fontname="tiro")
    # 本文 (本文の列は x=169 から)
    y = 160
    for blk in range(3):
        for line in textwrap.wrap(SENT * 3, 85)[:6]:
            pg.insert_text((169, y), line, fontsize=9, fontname="tiro")
            y += 11
        y += 8
    # 見出しの誤判定の元: 段落の途中で折り返した、太字の短い断片
    pg.insert_text((169, y), "complementary system account by Genzel et al., 2014; Diekelmann and Born, 2010;", fontsize=9, fontname="tiro")
    y += 11
    pg.insert_text((169, y), "1989; Girardeau and Zugaro, 2011; Singer et al., 2013).", fontsize=9, fontname="tibo")
    y += 11
    for line in textwrap.wrap(SENT * 2, 85)[:4]:
        pg.insert_text((169, y), line, fontsize=9, fontname="tiro")
        y += 11
    y += 14
    pg.insert_text((169, y), "Introduction", fontsize=14, fontname="tibo")                                 # 本物の見出し
    y += 22
    for line in textwrap.wrap(SENT * 2, 85)[:4]:
        pg.insert_text((169, y), line, fontsize=9, fontname="tiro")
        y += 11
    y += 20
    pg.insert_text((169, y), "Figure 1. Experimental design of the study and the main outcome measures that", fontsize=8, fontname="tiro")
    pg.insert_text((169, y + 10), "were recorded in each session of the experiment described in the text.", fontsize=8, fontname="tiro")
    doc.save(tmp_path / "e.pdf")
    d = extract_pdf(tmp_path / "e.pdf")
    fr = d["pages"][0]["frames"]
    role = lambda s: next(f["role"] for f in fr if f["text"].startswith(s))
    assert role("Journal of Examples") == "page_header"                                                  # 誌名はタイトルにしない
    assert role("Difficulty") == "title"
    side = [f for f in fr if f["bbox"][2] < 160]
    assert side and all(f["role"] == "sidebar" and not f["translate"] for f in side)                        # 左の細い列は翻訳しない (ラベルだけ辞書で訳す)
    assert role("1989;") == "body"                                                                       # 段落の途中の太字の断片は見出しではない
    assert role("Introduction") == "heading" and next(f for f in fr if f["text"] == "Introduction")["translate"]
    cap = [f for f in fr if f["text"].startswith(("Figure 1", "were recorded"))]
    assert cap and all(f["role"] == "caption" for f in cap)


def test_known_heading_word_beats_author_role():
    from readable.extract import KNOWN_HEADING_RE, NUMBERED_HEAD_RE
    assert KNOWN_HEADING_RE.match("eLife assessment") and KNOWN_HEADING_RE.match("Results and discussion")
    assert NUMBERED_HEAD_RE.match("2.3 Methods") and not NUMBERED_HEAD_RE.match("2 and Supplementary file 6).")
