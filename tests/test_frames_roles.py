"""段落分割 / role / joins (2論文の具体例を固定値として使う)"""
from conftest import frames_of
from readable.extract import Row, group_rows


def _row(y, x0=46.0, x1=288.0, text="word " * 10, size=8.0, page=1, first_script=False):
    return Row(page=page, bbox=[x0, y - 7, x1, y + 1], base=y, size=size, html=text, text=text, bold=False,
               italic=False, serif=True, color=0, font="F", stream=int(y), nchars=len(text), first_script=first_script)


def test_group_rows_indent_and_short_last_line():
    rows = [_row(100), _row(113), _row(126, x1=150), _row(139, x0=61), _row(152), _row(165, x1=200)]
    groups = group_rows(rows, 8.0, {})
    assert [len(g) for g in groups] == [3, 3]  # 最終行が短い/字下げで段落が切れる


def test_group_rows_size_and_gap_split():
    rows = [_row(100, size=10.0), _row(113, size=8.0), _row(126, size=8.0), _row(200, size=8.0)]
    groups = group_rows(rows, 8.0, {})
    assert [len(g) for g in groups] == [1, 2, 1]  # サイズ違い、行送り過大で分割


def test_group_rows_two_columns_do_not_merge():
    rows = [_row(100), _row(100, x0=307, x1=550), _row(113), _row(113, x0=307, x1=550)]
    groups = group_rows(rows, 8.0, {})
    assert sorted(len(g) for g in groups) == [2, 2]
    assert all(len({round(r.x0) for r in g}) == 1 for g in groups)


def test_jsr_paragraphs(jsr):
    F = frames_of(jsr)
    # p2 左段: 1行=1ブロックのPDFから段落が復元される
    assert F["p2-f2"]["text"].startswith("Since its first experimental evidence") and F["p2-f2"]["nrows"] == 4
    assert F["p2-f3"]["text"].startswith("Current models suggest") and F["p2-f3"]["nrows"] == 12
    assert F["p2-f4"]["text"].startswith("In the last two decades")
    body_p2 = [f for f in F.values() if f["page"] == 2 and f["role"] == "body"]
    assert len(body_p2) == 14
    assert F["p2-f2"]["align"] == "justify"
    # 1語=1行 (均等割付) の行が1つの段落に結合される
    assert "forms the neural basis for the gradual transformation" in F["p2-f3"]["text"]
    # 行末ハイフンの解除
    assert "consolidation (Brodt et al., 2023" in F["p2-f2"]["text"]
    assert "oscillations (SOs; < 1 Hz)" in F["p2-f3"]["text"]


def test_mdpi_paragraphs(mdpi):
    F = frames_of(mdpi)
    assert F["p1-f12"]["role"] == "abstract" and F["p1-f12"]["nrows"] == 21
    assert F["p1-f16"]["text"].startswith("The Asian small-clawed otter") and F["p1-f16"]["nrows"] == 6
    # 題名ブロックは "Article" (10pt) と題名 (17.9pt) が別 frame になる
    assert F["p1-f7"]["text"] == "Article" and F["p1-f8"]["role"] == "title"
    assert F["p1-f8"]["text"].startswith("You Otter Not Talk")


def test_jsr_roles(jsr):
    F = frames_of(jsr)
    assert F["p1-f1"]["role"] == "page_header" and F["p1-f1"]["text"] == "RESEARCH ARTICLE"   # 字間空白の復元
    assert F["p1-f2"]["role"] == "title" and F["p1-f2"]["translate"]
    assert F["p1-f3"]["role"] == "author" and not F["p1-f3"]["translate"]
    assert F["p1-f9"]["role"] == "sidebar" and F["p1-f9"]["translate"]
    assert F["p1-f16"]["role"] == "heading" and F["p1-f17"]["role"] == "abstract"
    assert F["p1-f18"]["text"] == "KEYWORDS" and F["p1-f19"]["role"] == "keywords"
    assert F["p1-f20"]["role"] == "sidebar" and not F["p1-f20"]["translate"]       # Received: 日付は非翻訳
    assert F["p1-f23"]["role"] == "doi_url"
    assert F["p2-f1"]["role"] == "heading" and F["p2-f1"]["text"] == "1 | INTRODUCTION"
    assert F["p2-f15"]["role"] == "heading" and F["p2-f15"]["text"] == "2.1 | Participants"
    assert {F["p2-f18"]["role"], F["p2-f19"]["role"]} == {"page_number", "page_header"}
    assert F["p6-f1"]["role"] == "caption" and F["p6-f1"]["text"].startswith("FIGURE 1")
    assert F["p5-f3"]["role"] == "math" and not F["p5-f3"]["translate"]
    refs = [f for f in F.values() if f["page"] in (9, 10) and f["role"] == "reference"]
    assert len(refs) >= 50 and all(not f["translate"] for f in refs)
    assert F["p10-f52"]["text"] == "SUPPORTING INFORMATION" and F["p10-f52"]["role"] == "heading"
    assert F["p10-f53"]["role"] != "reference"


def test_mdpi_roles(mdpi):
    F = frames_of(mdpi)
    assert F["p1-f2"]["role"] == "sidebar" and not F["p1-f2"]["translate"]       # Received 日付
    assert F["p1-f6"]["role"] == "sidebar" and F["p1-f6"]["translate"]            # Copyright (複数行が1段落)
    assert F["p1-f9"]["role"] == "author" and F["p1-f10"]["role"] == "author"
    assert F["p1-f13"]["role"] == "keywords"
    assert F["p1-f15"]["role"] == "heading" and F["p1-f15"]["text"].startswith("1.1. Otter")
    assert F["p4-f4"]["role"] == "caption" and F["p4-f4"]["translate"]            # 表キャプションは翻訳
    tbl = [f for f in F.values() if f["page"] == 4 and f["role"] == "table"]
    assert len(tbl) >= 40 and not any(f["translate"] for f in tbl)               # 表本体は翻訳しない/結合しない
    assert all(f["nrows"] == 1 for f in tbl)
    assert F["p6-f3"]["role"] == "figure_text" and F["p6-f3"]["text"] == "(b)"    # パネルラベル
    assert F["p6-f4"]["role"] == "figure_text" and not F["p6-f4"]["translate"]
    assert F["p6-f5"]["role"] == "caption"
    refs = [f for f in F.values() if f["role"] == "reference"]
    assert len(refs) >= 3 and all(not f["translate"] for f in refs)


def test_joins(jsr, mdpi):
    assert ["p2-f8", "p2-f9"] in jsr["joins"]            # 左段末 -> 右段先頭 ("In CL-TMR" / "experiments")
    assert ["p2-f17", "p3-f1"] in jsr["joins"]           # ページまたぎ
    assert ["p5-f23", "p7-f1"] in jsr["joins"]           # 図ページ (p6) を挟んだまたぎ
    assert ["p3-f11", "p4-f3"] in mdpi["joins"]
    jf = frames_of(jsr)
    # doc.json の html は非破壊 (元のまま行末ハイフンが残る)。unit では frame 境界のハイフンが解除される
    assert jf["p2-f8"]["text"].rstrip().endswith("CL-")
    from readable.translate import build_units
    u = next(u for u in build_units(jsr) if u["frames"][:2] == ["p2-f8", "p2-f9"])
    head = u["text"].split(" ⟦1⟧ ")[0]
    assert head.rstrip().endswith("In CL-TMR") and "⟦1⟧ experiments" in u["text"]
    # 結合は同じ role の本文同士だけ
    for doc in (jsr, mdpi):
        F = frames_of(doc)
        for a, b in doc["joins"]:
            assert F[a]["role"] == F[b]["role"] in ("body", "abstract")
