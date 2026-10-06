"""合成 PDF (docs/REVIEW_M1.md の再現ケースを固定化したもの)。実 PDF に依存しない汎用ケースの回帰テスト用。"""
from __future__ import annotations

import textwrap
from pathlib import Path

import fitz

LOREM = ("The quick study of sleep and memory consolidation shows that targeted reactivation improves recall after a single night. "
         "Participants were recruited from the local community and gave written informed consent before taking part in any session. ") * 3


def para(page, x, y, text, size=10, font="tiro", width=75, lead=1.25, indent=0):
    ls = textwrap.wrap(text, width)
    for i, l in enumerate(ls):
        page.insert_text((x + (indent if i == 0 else 0), y), l, fontsize=size, fontname=font)
        y += size * lead
    return y


def one_page_paper(path: Path) -> Path:
    """A: 1 ページ論文。y=60 に 18pt のタイトル、見出し無しで要旨が始まる。参考文献は本文サイズ非太字の見出し。"""
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 60), "Closed-loop reactivation of vocabulary during sleep", fontsize=18, fontname="tibo")
    pg.insert_text((72, 85), "Jane Doe, John Smith", fontsize=11, fontname="tiro")
    pg.insert_text((72, 100), "University of Somewhere, Somewhere, Country", fontsize=9, fontname="tiit")
    y = para(pg, 72, 140, LOREM, 10)
    y = para(pg, 72, y + 8, LOREM, 10, indent=14)
    pg.insert_text((72, y + 20), "References", fontsize=10, fontname="tiro")
    para(pg, 72, y + 36, "[1] A. Author, B. Author. A paper title about things. Journal of Stuff, 2020, 12(3): 1-10.", 9)
    doc.save(str(path))
    return path


def ieee_like(path: Path) -> Path:
    """B: IEEE 風 3 ページ。Abstract-- (run-in)、非太字 REFERENCES、付録 'A Proofs ...' (キーワード無し)。"""
    doc = fitz.open()
    for i in range(3):
        pg = doc.new_page(width=612, height=792)
        pg.insert_text((72, 30), "IEEE TRANSACTIONS ON FOO, VOL. 1", fontsize=8, fontname="tiro")
        if i == 0:
            pg.insert_text((72, 80), "A Deep Study of Things", fontsize=20, fontname="tibo")
            pg.insert_text((72, 110), "Alice Author and Bob Writer", fontsize=11, fontname="tiro")
            y = para(pg, 72, 150, "Abstract" + chr(8212) + LOREM, 9, width=95)
            y = para(pg, 72, y + 10, "I. INTRODUCTION", 10, font="tibo")
            para(pg, 72, y + 4, LOREM, 10, width=90)
        elif i == 1:
            y = para(pg, 72, 80, LOREM, 10, width=90)
            pg.insert_text((72, y + 20), "REFERENCES", fontsize=10, fontname="tiro")
            y = para(pg, 72, y + 40, "[1] A. Author, B. Author. A paper title about things and more things for the references list. Journal of Stuff, 2020.", 9, width=100)
            para(pg, 72, y + 4, "[2] C. Author, D. Author. Another paper title about things and more things for the references list. Journal of Stuff, 2021.", 9, width=100)
        else:
            pg.insert_text((72, 80), "A Proofs of the Main Theorems", fontsize=12, fontname="tibo")
            para(pg, 72, 110, LOREM, 10, width=90)
    doc.save(str(path))
    return path


def scanned(path: Path) -> Path:
    """C: 画像だけ (スキャン風)。"""
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), False)
    pix.set_rect(pix.irect, (250, 250, 250))
    pg.insert_image(fitz.Rect(0, 0, 595, 842), pixmap=pix)
    doc.save(str(path))
    return path


def encrypted(path: Path, user_pw: str = "u") -> Path:
    doc = fitz.open()
    para(doc.new_page(), 72, 100, LOREM, 10)
    doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw=user_pw)
    return path


def rotated(path: Path, rot: int) -> Path:
    """E: /Rotate=rot のページ。見た目が水平になるように文字を (回転して) 描く。"""
    doc = fitz.open()
    landscape = rot in (90, 270)
    pg = doc.new_page(width=842 if landscape else 595, height=595 if landscape else 842)
    pg.set_rotation(rot)
    ls = textwrap.wrap(LOREM, 75)[:12]
    for i, line in enumerate(ls):
        raw = fitz.Point(72, 100 + i * 12.5) * pg.derotation_matrix
        pg.insert_text(raw, line, fontsize=10, fontname="tiro", rotate=pg.rotation)
    doc.save(str(path))
    return path


def tables(path: Path) -> Path:
    """F: 表検出ケース。1 ページ目: 縦に長い 3 罫線の表 (y=200,222,520)、2 ページ目: 幅 68pt の細い表、
    3 ページ目: 縦罫線のある格子表、4 ページ目: 本文を挟んで同じ幅の罫線が並ぶ (表ではない)。"""
    doc = fitz.open()
    # p1: 縦に長い booktabs
    pg = doc.new_page(width=595, height=842)
    para(pg, 72, 80, LOREM, 10, width=90)
    for y in (200, 222, 520):
        pg.draw_line((72, y), (520, y), width=0.8)
    pg.insert_text((80, 214), "Item", fontsize=9)
    pg.insert_text((300, 214), "Value", fontsize=9)
    for i in range(20):
        pg.insert_text((80, 240 + i * 14), f"row{i}", fontsize=9)
        pg.insert_text((300, 240 + i * 14), f"{i * 1.5:.1f}", fontsize=9)
    # p2: 細い表 (68pt)
    pg = doc.new_page(width=595, height=842)
    para(pg, 72, 80, LOREM, 10, width=90)
    for y in (300, 314, 380):
        pg.draw_line((100, y), (168, y), width=0.8)
    pg.insert_text((102, 311), "A", fontsize=8)
    pg.insert_text((140, 311), "B", fontsize=8)
    for i in range(4):
        pg.insert_text((102, 327 + i * 14), str(i), fontsize=8)
        pg.insert_text((140, 327 + i * 14), str(i * 2), fontsize=8)
    # p3: 格子 (縦罫線あり)
    pg = doc.new_page(width=595, height=842)
    para(pg, 72, 80, LOREM, 10, width=90)
    x0, y0, cw, ch = 100, 300, 90, 16
    for r in range(5):
        for c in range(3):
            pg.draw_rect(fitz.Rect(x0 + c * cw, y0 + r * ch, x0 + (c + 1) * cw, y0 + (r + 1) * ch), width=0.6)
            pg.insert_text((x0 + c * cw + 4, y0 + r * ch + 11), f"c{r}{c}", fontsize=8)
    # p4: 表ではない (本文の段落を挟んだ罫線)
    pg = doc.new_page(width=595, height=842)
    pg.draw_line((72, 150), (520, 150), width=0.8)
    para(pg, 72, 170, LOREM, 10, width=90)
    pg.draw_line((72, 400), (520, 400), width=0.8)
    para(pg, 72, 420, LOREM, 10, width=90)
    doc.save(str(path))
    return path


def links_doc(path: Path) -> Path:
    """G: 本文中に引用 (内部リンク)・外部 URL リンクがあり、翻訳対象外の場所にもリンクがある。しおり・名前付き宛先つき。"""
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    doc.new_page(width=595, height=842)
    p1, p2 = doc[0], doc[1]   # new_page() は前のページ参照を無効にするので、2 ページ作ってから取り直す
    p1.insert_text((72, 60), "Links paper", fontsize=18, fontname="tibo")
    # 段落1 (翻訳される): 内部リンク (引用) + 外部 URL リンク
    y = para(p1, 72, 140, "The quick study of sleep shows that reactivation improves recall [12] after a night. "
             "See the protocol at the project site for details of the procedure and the experiment design used here. " * 2, 10)
    for needle, link in (("[12]", {"kind": fitz.LINK_GOTO, "page": 1, "to": fitz.Point(72, 100)}),
                         ("project site", {"kind": fitz.LINK_URI, "uri": "https://example.org/protocol"})):
        r = p1.search_for(needle)[0]
        link["from"] = r
        p1.insert_link(link)
    # 翻訳対象外 (ヘッダ帯のページ番号付近 / 図キャプションの外) のリンク
    p1.insert_text((72, 800), "1", fontsize=8)
    p1.insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(70, 790, 90, 805), "page": 1, "to": fitz.Point(0, 0)})
    p1.insert_text((72, 400), "http://example.org/data", fontsize=10)
    p1.insert_link({"kind": fitz.LINK_URI, "from": p1.search_for("http://example.org/data")[0], "uri": "http://example.org/data"})
    p2.insert_text((72, 60), "Second page heading", fontsize=14, fontname="tibo")
    para(p2, 72, 120, LOREM, 10)
    doc.set_toc([[1, "First", 1], [1, "Second", 2], [2, "Second sub", 2]])
    doc.save(str(path))
    return path
