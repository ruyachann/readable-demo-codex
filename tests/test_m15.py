"""M1.5: REVIEW_M1.md の指摘修正の回帰テスト (合成 PDF + 2 論文)。"""
import copy
import re
from pathlib import Path

import fitz
import pytest
from conftest import JSR, MDPI, frames_of
from overlap import count_overlaps
from synth_pdfs import LOREM, encrypted, ieee_like, links_doc, one_page_paper, para, rotated, scanned, tables

from readable import extract as E
from readable.cli import main as cli_main
from readable.cli import work_name
from readable.config import load_config
from readable.render import PageCtx, _col_right, below_limit, build_dual, count_links_toc, render_ja
from readable.translate import (DummyTranslator, build_units, frames_translations, prompt_hash, sanitize_ja,
                                split_by_ratio, split_unit, translate_cached, unit_key, validate_tags,
                                validate_translation)


def roles_of(doc):
    return {f["id"]: f for p in doc["pages"] for f in p["frames"]}


def run_pipeline(pdf, out_dir, ratio=0.38):
    doc = E.extract_pdf(pdf)
    units = build_units(doc)
    fr = frames_translations(doc, units, DummyTranslator(ratio).translate(units))
    ja = Path(out_dir) / (Path(pdf).stem + "_ja.pdf")
    rep = render_ja(pdf, doc, fr, ja)
    return doc, units, rep, ja


# ---------------------------------------------------------------- S1-S3: role 判定の汎用化

def test_one_page_paper_title_and_body(tmp_path):
    doc = E.extract_pdf(one_page_paper(tmp_path / "A.pdf"))
    F = list(roles_of(doc).values())
    title = next(f for f in F if f["text"].startswith("Closed-loop"))
    assert title["role"] == "title" and title["translate"]          # S1: 1 ページでもタイトルが header 扱いにならない
    authors = [f for f in F if f["text"].startswith(("Jane Doe", "University of"))]
    assert len(authors) == 2 and all(f["role"] == "author" and not f["translate"] for f in authors)
    body = [f for f in F if f["text"].startswith("The quick study")]
    assert len(body) == 2 and all(f["role"] == "body" and f["translate"] for f in body)   # S2: 要旨/本文が author にならない
    ref = next(f for f in F if f["text"].startswith("[1]"))
    assert ref["role"] == "reference" and not ref["translate"]      # S3: 非太字・本文サイズの References 見出し


def test_ieee_like_roles(tmp_path):
    doc = E.extract_pdf(ieee_like(tmp_path / "B.pdf"))
    F = roles_of(doc)

    def by(start):
        return next(f for f in F.values() if f["text"].startswith(start))

    assert by("A Deep Study")["role"] == "title"
    assert by("Alice Author")["role"] == "author"
    assert by("I. INTRODUCTION")["role"] == "heading" and by("I. INTRODUCTION")["translate"]
    intro = [f for f in F.values() if f["page"] == 1 and f["text"].startswith("The quick study")]
    assert intro and all(f["role"] == "body" and f["translate"] for f in intro)     # 導入の本文が author にならない
    refs = [f for f in F.values() if f["text"].startswith(("[1]", "[2]")) or "Journal of Stuff" in f["text"]]
    assert refs and all(f["role"] == "reference" and not f["translate"] for f in refs)
    assert by("REFERENCES")["role"] == "heading"
    p3 = [f for f in F.values() if f["page"] == 3 and f["text"].startswith("The quick study")]
    assert p3 and all(f["role"] == "body" and f["translate"] for f in p3)          # 付録見出しで参考文献が終了する
    assert by("A Proofs")["role"] == "heading"
    assert F["p1-f1"]["role"] == "page_header"                                      # 3 ページ以上は反復で判定


def test_runin_abstract_regex():
    assert E.ABSTRACT_RUNIN_RE.match("Abstract—Targeted memory reactivation is")
    assert E.ABSTRACT_RUNIN_RE.match("Abstract- text") and E.ABSTRACT_RUNIN_RE.match("Summary: text")
    assert not E.ABSTRACT_RUNIN_RE.match("Abstract") and not E.ABSTRACT_RUNIN_RE.match("Abstraction is hard")
    assert E.KEYWORDS_RE.match("Index Terms—sleep") and E.KEYWORDS_RE.match("Keywords: a; b")


def test_runin_abstract_role(tmp_path):
    doc = fitz.open()
    pg = doc.new_page(width=612, height=792)
    pg.insert_text((72, 80), "A Deep Study of Things", fontsize=20, fontname="tibo")
    pg.insert_text((72, 110), "Alice Author and Bob Writer", fontsize=11)
    y = para(pg, 72, 150, "Abstract-" + LOREM, 9, width=95)
    para(pg, 72, y + 10, "Index Terms-sleep, memory", 9)
    para(pg, 72, y + 40, LOREM, 10, width=90)
    doc.save(str(tmp_path / "R.pdf"))
    F = list(roles_of(E.extract_pdf(tmp_path / "R.pdf")).values())
    ab = next(f for f in F if f["text"].startswith("Abstract"))
    assert ab["role"] == "abstract" and ab["translate"]
    assert next(f for f in F if f["text"].startswith("Index Terms"))["role"] == "keywords"
    assert [f["role"] for f in F if f["text"].startswith("The quick") and f is not ab][-1] == "body"


def test_entry_like_and_doi_url():
    assert E.is_entry_like("[1] A. Author, B. Author. A paper title. Journal of Stuff, 2020, 12(3): 1-10.")
    assert E.is_entry_like("Groch, S., Schreiner, T. (2017). Prior knowledge is essential. Nature, 1(2), 3-4.")
    assert not E.is_entry_like("We recruited 40 participants in 2020 and tested them.")
    assert E.is_doi_url("https://doi.org/10.1111/jsr.70000", 1, 750, 842)
    assert E.is_doi_url("DOI: 10.1111/jsr.70000", 1, 44, 842)
    # URL を含むだけの本文寄りの脚注は doi_url にしない
    assert not E.is_doi_url("See https://example.org for details of the experimental protocol used in this study", 2, 800, 842)
    assert E.is_doi_url("1 https://github.com/foo/bar", 1, 800, 842)


def test_equation_detection():
    assert E.looks_like_equation("Response ~ 1 + stimulus type + session", 1)
    assert E.looks_like_equation("y = a x + b", 1)
    assert not E.looks_like_equation("where x = 3 in this case", 1)


def test_prose_density():
    assert E.prose_density("the study of the effects of sleep on memory") > 0.3
    assert E.prose_density("Federico Salfi, Domenico Corigliano") == 0.0


def test_mdpi_disclaimer_is_translated(mdpi):
    F = frames_of(mdpi)
    disc = [f for f in F.values() if f["page"] == 12 and f["text"].startswith("Disclaimer")]
    assert disc and all(f["role"] == "body" and f["translate"] for f in disc)
    assert any(f["role"] == "reference" for f in F.values() if f["page"] == 11)


def test_after_reference_sections_translated(jsr):
    F = frames_of(jsr)
    assert F["p10-f53"]["translate"]                                                # Supporting Information 本文


# ---------------------------------------------------------------- S4: 暗号化・スキャン

def test_encrypted_pdf(tmp_path, capsys):
    p = encrypted(tmp_path / "D.pdf")
    with pytest.raises(E.EncryptedPDFError):
        E.extract_pdf(p)
    assert cli_main([str(p), "--out", str(tmp_path / "o"), "--work-dir", str(tmp_path / "w"), "--translator", "dummy", "--no-claude"]) == 4
    assert "パスワード" in capsys.readouterr().err
    # オーナーパスワードのみ (開ける) は正常
    d2 = E.extract_pdf(encrypted(tmp_path / "D2.pdf", user_pw=""))
    assert sum(len(p["frames"]) for p in d2["pages"]) >= 1


def test_not_a_pdf(tmp_path, capsys):
    p = tmp_path / "x.pdf"
    p.write_text("not a pdf")
    assert cli_main([str(p), "--out", str(tmp_path / "o"), "--work-dir", str(tmp_path / "w"), "--translator", "dummy", "--no-claude"]) == 5
    assert "開けません" in capsys.readouterr().err


def test_scanned_pdf(tmp_path, capsys):
    p = scanned(tmp_path / "C.pdf")
    d = E.extract_pdf(p)
    assert d["pages"][0]["scanned"] and any("スキャン" in w for w in d["warnings"])
    assert cli_main([str(p), "--out", str(tmp_path / "o"), "--work-dir", str(tmp_path / "w"), "--translator", "dummy", "--no-claude"]) == 6
    assert "OCR" in capsys.readouterr().err


# ---------------------------------------------------------------- M1 表 / M7 回転

def test_table_detection_general(tmp_path):
    d = E.extract_pdf(tables(tmp_path / "F.pdf"))
    t = [p["tables"] for p in d["pages"]]
    assert len(t[0]) == 1 and t[0][0][1] < 210 and t[0][0][3] > 510          # 縦に長い 3 罫線の表 (間隔 150 超)
    assert len(t[1]) == 1 and t[1][0][2] - t[1][0][0] < 80                     # 幅 68pt の細い表
    assert len(t[2]) == 1                                                       # 縦罫線のある格子表
    assert t[3] == []                                                           # 段落を挟む罫線は表にしない
    F = roles_of(d)
    assert all(f["role"] == "body" for f in F.values() if f["page"] == 4)
    tab = [f for f in F.values() if f["page"] == 1 and f["role"] == "table"]
    assert len(tab) >= 20 and not any(f["translate"] for f in tab)


def test_translate_tables_option(tmp_path):
    cfg = load_config()
    cfg.data["extract"]["translate_tables"] = True
    d = E.extract_pdf(tables(tmp_path / "F.pdf"), cfg=cfg)
    cells = [f for f in roles_of(d).values() if f["role"] == "table" and E._alpha(f["text"]) >= 2]
    assert cells and all(f["translate"] for f in cells)


@pytest.mark.parametrize("rot", [90, 180, 270])
def test_rotated_pages(tmp_path, rot):
    pdf = rotated(tmp_path / f"E{rot}.pdf", rot)
    doc, units, rep, ja = run_pipeline(pdf, tmp_path)
    pm = doc["pages"][0]
    assert pm["rotation"] == rot
    fr = pm["frames"]
    assert len(fr) == 1 and fr[0]["bbox"][0] == pytest.approx(72, abs=2) and fr[0]["bbox"][1] < 100   # 見た目の座標
    assert rep["failed"] == 0
    out = fitz.open(ja)
    txt = out[0].get_text()
    assert "【訳】" in txt and not re.search(r"[A-Za-z]{4,}", txt)
    # 日本語が見た目で水平に出ている (回転行列を適用した行方向が (1,0))
    pg = out[0]
    rm = pg.rotation_matrix
    M = fitz.Matrix(rm.a, rm.b, rm.c, rm.d, 0, 0)
    dirs = [fitz.Point(*l["dir"]) * M for b in pg.get_text("dict")["blocks"] if b.get("type") == 0 for l in b["lines"]]
    assert dirs and all(abs(d.y) < 0.2 and d.x > 0.8 for d in dirs)
    assert count_overlaps(doc, ja) == 0


# ---------------------------------------------------------------- G1 リンク / G2 交互版

def test_links_restored_and_inline_uri(tmp_path):
    pdf = links_doc(tmp_path / "G.pdf")
    doc, units, rep, ja = run_pipeline(pdf, tmp_path)
    lk = rep["links"]
    assert lk["orig"] == 4 and lk["lost_internal"] == 1 and lk["inside_uri"] == 1   # [12] は消失、URL は <a> で再作成
    out = fitz.open(ja)
    links = out[0].get_links()
    uris = sorted(l["uri"] for l in links if l["kind"] == fitz.LINK_URI)
    assert "http://example.org/data" in uris                                       # 翻訳対象外の URL リンクは維持
    assert "https://example.org/protocol" in uris                                  # 翻訳領域内の外部リンクは訳文の <a> で再作成
    assert any(l["kind"] == fitz.LINK_GOTO for l in links)                         # ページ番号リンク (非翻訳領域) が維持/復元
    assert len(out.get_toc()) == 3


def test_link_restore_when_redact_removes(tmp_path):
    """翻訳領域に隣接する (中心が外の) リンクが redact で消えたら insert_link で復元される。"""
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 60), "Adjacent link paper", fontsize=18, fontname="tibo")
    for i in range(3):
        pg.insert_text((72, 140 + i * 12.5), "The quick study of sleep and memory consolidation shows that reactivation improves recall", fontsize=10)
    pg.insert_text((72, 120), "Corresponding: x@y.org", fontsize=8)
    r = pg.search_for("Corresponding")[0]
    pg.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(r.x0, r.y0, r.x1, r.y1 + 12), "uri": "https://corr.example.org"})
    doc.save(str(tmp_path / "H.pdf"))
    d, units, rep, ja = run_pipeline(tmp_path / "H.pdf", tmp_path)
    out = fitz.open(ja)
    assert "https://corr.example.org" in [l.get("uri") for l in out[0].get_links()]


def test_dual_links_and_toc(tmp_path):
    pdf = links_doc(tmp_path / "G.pdf")
    doc, units, rep, ja = run_pipeline(pdf, tmp_path)
    dual = tmp_path / "G_dual.pdf"
    n = build_dual(pdf, ja, dual)
    assert n == 4
    ol, ot = count_links_toc(pdf)
    dl, dt = count_links_toc(dual, [0, 2])
    assert (dl, dt) == (ol, ot)                                                     # 英語ページのリンク数/しおり数が原文と一致
    d = fitz.open(dual)
    assert [t[2] for t in d.get_toc()] == [1, 3, 3]                                 # しおり p -> 2p-1
    # 英語ページ (p1=index0) の GOTO 宛先は英語ページ、日本語ページ (index1) の GOTO は日本語ページ
    en_goto = [l for l in d[0].get_links() if l["kind"] == fitz.LINK_GOTO]
    ja_goto = [l for l in d[1].get_links() if l["kind"] == fitz.LINK_GOTO]
    assert en_goto and all(l["page"] % 2 == 0 for l in en_goto)
    assert ja_goto and all(l["page"] % 2 == 1 for l in ja_goto)


def test_dual_named_destinations(tmp_path):
    """名前付き宛先 (LINK_NAMED) のリンクが原文ページで保持され、日本語ページでは GOTO に解決される。"""
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    p1, p2 = doc[0], doc[1]
    p1.insert_text((72, 100), "See the appendix for the details of the study participants here", fontsize=10)
    p2.insert_text((72, 100), "Second page of the document with enough text to translate it ok", fontsize=10)
    p1.insert_link({"kind": fitz.LINK_NAMED, "from": fitz.Rect(72, 300, 200, 320), "name": "sec2"})
    doc.xref_set_key(doc.pdf_catalog(), "Dests", f"<< /sec2 [{p2.xref} 0 R /XYZ 0 700 0] >>")
    doc.save(str(tmp_path / "N.pdf"))
    pdf = tmp_path / "N.pdf"
    _, _, rep, ja = run_pipeline(pdf, tmp_path)
    dual = tmp_path / "N_dual.pdf"
    build_dual(pdf, ja, dual)
    dd = fitz.open(dual)
    assert len(dd[0].get_links()) == len(fitz.open(pdf)[0].get_links()) == 1
    ja_links = dd[1].get_links()
    assert ja_links and all(l["kind"] == fitz.LINK_GOTO and l["page"] % 2 == 1 for l in ja_links)


# ---------------------------------------------------------------- 2 論文の統合確認

@pytest.fixture(scope="module")
def jsr_run(tmp_path_factory):
    if not JSR.exists():
        pytest.skip("sample PDF missing")
    d = tmp_path_factory.mktemp("jsr")
    return (d,) + run_pipeline(JSR, d)


@pytest.fixture(scope="module")
def mdpi_run(tmp_path_factory):
    if not MDPI.exists():
        pytest.skip("sample PDF missing")
    d = tmp_path_factory.mktemp("mdpi")
    return (d,) + run_pipeline(MDPI, d)


def test_jsr_sidebar_email_and_grant_preserved(jsr_run):
    d, doc, units, rep, ja = jsr_run
    txt = fitz.open(ja)[0].get_text()
    assert "aurora.datri@univaq.it" in txt
    assert "07DG202207" in re.sub(r"[̲\s_]", "", txt)           # 助成番号 (抽出時に _ が結合下線+空白になる)
    assert any("aurora.datri@univaq.it" in " ".join(u["vars"].values()) for u in units)


def test_jsr_mdpi_links_dual_overlap(jsr_run, mdpi_run):
    for d, doc, units, rep, ja in (jsr_run, mdpi_run):
        src = doc["source"]
        dual = d / "dual.pdf"
        n = build_dual(src, ja, dual)
        ol, ot = count_links_toc(src)
        dl, dt = count_links_toc(dual, list(range(0, n, 2)))
        assert (dl, dt) == (ol, ot), (dl, ol, dt, ot)
        lk = rep["links"]
        assert lk["kept"] + lk["restored"] > 0 and lk["orig"] >= lk["kept"] + lk["restored"]
        assert count_overlaps(doc, ja) == 0
        assert rep["failed"] == 0


@pytest.mark.parametrize("ratio", [0.7, 1.0])
def test_stress_ratio_no_overlap(tmp_path, ratio):
    if not JSR.exists():
        pytest.skip("sample PDF missing")
    doc, units, rep, ja = run_pipeline(JSR, tmp_path, ratio)
    assert count_overlaps(doc, ja) == 0 and rep["failed"] == 0
    # 縮小下限割れ (scale_low0) は警告として出る
    assert len(rep["warnings"]) >= sum(1 for r in rep["details"] if r["stage"] == "scale_low0" and r["ratio"] < 0.7)


def test_rect0_does_not_intrude(jsr):
    """rect0 / 拡張矩形が他の frame に食い込まない (M4)。"""
    cfg = load_config()
    page = fitz.open(JSR)[1]
    pd = jsr["pages"][1]
    ctx = PageCtx(page=page, pdata=pd, cfg=cfg, fonts=None,
                  obstacles=[(f["id"], f["bbox"]) for f in pd["frames"]] + [("img", b) for b in pd["images"]],
                  containers=[], M=page.rotation_matrix, D=page.derotation_matrix, rotation=0,
                  page_w=pd["width"], page_h=pd["height"])
    for f in pd["frames"]:
        if not f["translate"]:
            continue
        x1 = _col_right(f, ctx)
        lim = max(below_limit(f, f["bbox"][0], x1, f["bbox"][3], ctx, 0.5), f["bbox"][3])
        for oid, ob in ctx.obstacles:
            if oid == f["id"]:
                continue
            if ob[1] >= f["bbox"][3] - 1 and min(x1, ob[2]) - max(f["bbox"][0], ob[0]) > 2:
                assert lim <= max(ob[1] - 0.4, f["bbox"][3]), (f["id"], oid)


# ---------------------------------------------------------------- M2 ハイフン (タグをまたぐ / 非破壊)

def test_hyphen_across_tags():
    v = E.Vocab.from_texts(["pseudo word", "newtime", "retest", "the closed-loop CL-TMR"])
    assert E.join_lines(["see <i>pseudo-</i>", "<i>word</i> here"], v) == "see <i>pseudoword</i> here"
    assert E.join_lines(["a <i>newti-</i>", "<i>mef</i> x"], v) == "a <i>newtimef</i> x"     # タグをまたいでもハイフン+空白を残さない
    assert E.join_lines(["x <b><i>CL-</i></b>", "<b><i>TMR</i></b> y"], v) == "x <b><i>CL-TMR</i></b> y"
    assert E.join_lines(["were partici-", "pants who"], v) == "were participants who"
    assert E.join_lines(["one", "two"], v) == "one two"


def test_hyphen_nondestructive_and_recomputable(jsr):
    F = frames_of(jsr)
    assert F["p2-f8"]["html"].rstrip().endswith("CL-")                              # doc.json の html は元のまま
    u_joined = next(u for u in build_units(jsr) if u["frames"][:2] == ["p2-f8", "p2-f9"])
    assert "In CL-TMR ⟦1⟧ experiments" in u_joined["text"]
    d2 = copy.deepcopy(jsr)
    d2["joins"] = [j for j in d2["joins"] if j != ["p2-f8", "p2-f9"]]                # joins を後から差し替え
    u = {tuple(u["frames"]): u for u in build_units(d2)}
    assert ("p2-f8",) in u and u[("p2-f8",)]["text"].rstrip().endswith("CL-")        # 元に戻って再計算される


def test_jsr_italic_hyphen_in_real_doc(jsr):
    txt = " ".join(f["text"] for f in frames_of(jsr).values())
    assert "pseudo- word" not in txt and "newti- mef" not in txt and "ret- est" not in txt


# ---------------------------------------------------------------- インライン保護

def test_protect_html():
    src = ("Email: aurora.datri@univaq.it, see https://doi.org/10.1111/jsr.70000. "
           "Grant/Award Number: 07_DG_2022_07 and H2020 and PRIN2022ABC")
    h, v = E.protect_html(src)
    assert "{v1}" in h and "aurora" not in h and "doi.org" not in h and "07_DG_2022_07" not in h
    assert v["v1"] == "aurora.datri@univaq.it" and any("doi.org" in x for x in v.values())
    assert E.restore_vars(h, v) == src
    # リンク (アンカー) 全体を保護。通常語のアンカーは <a> のまま
    h, v = E.protect_html('see <a href="mailto:a@b.co">a@b.co</a> and <a href="https://x.org/y">the protocol</a>.')
    assert h.count("{v") == 1 and '<a href="https://x.org/y">the protocol</a>' in h
    # 数式片
    h, v = E.protect_html("where <x>x<sub>i</sub></x> is the value")
    assert h == "where {v1} is the value" and v["v1"] == "x<sub>i</sub>"


def test_identifier_frames_not_translated():
    assert E.is_identifier_frame("aurora.datri@univaq.it")
    assert E.is_identifier_frame("https://doi.org/10.1111/jsr.70000")
    assert not E.is_identifier_frame("Correspondence: aurora.datri@univaq.it")
    assert not E.is_identifier_frame("Plain text only")


def test_bold_runin_preserved(mdpi):
    assert any("<b>" in f["html"] for f in frames_of(mdpi).values())
    assert not any(f["bold"] and "<b>" in f["html"] for f in frames_of(mdpi).values())  # 全体太字の frame には付けない


def test_dummy_preserves_vars_and_tags(jsr):
    units = build_units(jsr)
    ja = DummyTranslator().translate(units)
    for u in units:
        for k in u["vars"]:
            assert "{%s}" % k in ja[u["id"]]
        assert "{v" not in u["text"] or all("{%s}" % k in u["text"] for k in u["vars"])


# ---------------------------------------------------------------- M3 分割 / validate

def test_split_marker_order_validated():
    ja = "前半。 ⟦2⟧ 中間。 ⟦1⟧ 後半。"          # 番号の順序が入れ替わっている -> 採用しない
    parts = split_unit(ja, [10, 10, 10])
    assert len(parts) == 3 and "⟦" not in "".join(parts)
    assert split_unit("前半。 ⟦1⟧ 中間。 ⟦2⟧ 後半。", [1, 1, 1]) == ["前半。", "中間。", "後半。"]
    dup = "前半。 ⟦1⟧ 中間。 ⟦1⟧ 後半。"                                                 # 番号の重複 -> 比率分割
    assert split_unit(dup, [1, 8, 1]) == split_by_ratio("前半。中間。後半。", [1, 8, 1])


def test_split_fallback_keeps_tags_entities_vars():
    ja = "これは<i>斜体の長い長い長い長い長い文章がここにあって、まだ続く</i>。次の文です。A &amp; B と {v1} と <sup>12</sup> の続き。さらに続く文章がある。"
    for w in ([10, 10], [3, 1], [1, 3], [1, 1, 1]):
        parts = split_by_ratio(ja, w)
        assert len(parts) == len(w)
        for p in parts:
            assert p.count("<i>") == p.count("</i>") and p.count("<sup>") == p.count("</sup>")
            assert not re.search(r"&\w*$", p) and not re.search(r"^\w*;", p)
            assert p.count("{") == p.count("}")


def test_split_inside_tag_closes_and_reopens():
    ja = "<i>" + "あいうえお" * 10 + "</i>"
    a, b = split_by_ratio(ja, [1, 1])
    assert a.startswith("<i>") and a.endswith("</i>") and b.startswith("<i>") and b.endswith("</i>")


def test_validate_translation():
    src = 'A <i>b</i> {v1} ⟦1⟧ C <a href="https://x.org">d</a>'
    assert validate_translation(src, 'あ <i>い</i> {v1} ⟦1⟧ う <a href="https://x.org">え</a>') == []
    assert validate_translation(src, 'あ <i>い</i> {v1} ⟦1⟧ う え')            # <a> 欠落 (M11: 太字・斜体 <i> <b> の欠落は書体だけの情報なので許す)
    assert validate_translation(src, 'あ <i>い</i> ⟦1⟧ う <a href="https://x.org">え</a>')          # {v1} 欠落
    assert validate_translation(src, 'あ <i>い</i> {v1} う <a href="https://x.org">え</a>')          # ⟦1⟧ 欠落
    assert not validate_tags("a <i>b</i>", "<i>x</i><i>")


def test_sanitize_allows_anchor():
    assert sanitize_ja('a <a href="https://x.org/?a=1&b=2">t</a> < b') == 'a <a href="https://x.org/?a=1&amp;b=2">t</a> &lt; b'


def test_frames_translations_restores_vars_and_reports_missing(jsr):
    units = [u for u in build_units(jsr) if u["vars"]][:5]
    ja = {u["id"]: "あ" for u in units}
    warns: list[str] = []
    out = frames_translations(jsr, units, ja, warns)
    assert warns and all(v in "".join(out.values()) for v in units[0]["vars"].values())


# ---------------------------------------------------------------- M6 インターフェース

def test_schema_version_and_doc_fields(jsr):
    assert jsr["schema_version"] == E.SCHEMA_VERSION == 2
    assert "vocab" in jsr and jsr["joins_source"] == "heuristic"
    f = next(iter(frames_of(jsr).values()))
    assert {"math_ratio", "in_table", "in_image"} <= set(f)
    assert isinstance(jsr["charmap"]["AdvP"], dict)                                   # font ごと
    assert all(isinstance(v, dict) for v in jsr["unmapped_chars"].values())


def test_charmap_per_font_and_pua():
    cm = {"AdvP": {"\x02": "Ö"}, "Times": {"\x02": "Ü"}, "*": {"\x07": "§"}}
    assert E.apply_charmap("\x02a\x07", "AdvP4C4E59", cm) == "Öa§"
    assert E.apply_charmap("\x02a", "ABCDEF+Times-Roman", cm) == "Üa"
    assert E.apply_charmap("\x02a", "Other", cm) == "a"                                # 他 font には適用しない
    assert E.apply_charmap("a\x03b", "Arial") == "ab"                                   # 既定表は AdvP 専用 (誤変換しない)
    un: dict = {}
    assert E.apply_charmap("ab", "Symbol", None, un) == "ab" and un == {"": 1}   # PUA は unmapped に記録
    assert E.merge_charmap(None, {"\x09": "X"})["*"] == {"\x09": "X"}
    assert E.merge_charmap(None, {"Foo": {"\x09": "X"}})["Foo"] == {"\x09": "X"}


def test_validate_joins_and_apply_structure(jsr):
    doc = copy.deepcopy(jsr)
    F = frames_of(doc)
    order = [f["id"] for p in doc["pages"] for f in p["frames"]]
    tf = [f["id"] for p in doc["pages"] for f in p["frames"] if f["translate"]]
    a, b, c = tf[3], tf[4], tf[5]
    bad = [[a, a], ["zz", b], [tf[-1], a], [a, b], [a, c], [c, b]]          # 自己/存在しない/逆行 (M13: 同じ列で隣り合う逆順は採用するので、離れた frame を使う)/正常/重複(a)/重複(b)
    ok, warns = E.validate_joins(F, order, bad)
    assert ok == [[a, b]] and len(warns) == 5
    w = E.apply_structure(doc, joins=[[a, b], [b, c], [c, a]])         # 循環 (c->a は逆行)
    assert doc["joins"] == [[a, b], [b, c]] and w and doc["joins_source"] == "override"
    nonbody = next(f for f in F.values() if f["role"] == "reference")
    w = E.apply_structure(doc, roles={nonbody["id"]: "body", "p99-f1": "body", tf[0]: "bogus"})
    assert len(w) == 2 and F[nonbody["id"]]["translate"] and F[nonbody["id"]]["role"] == "body"
    E.apply_structure(doc, roles={nonbody["id"]: "reference"})
    assert not F[nonbody["id"]]["translate"]
    w = E.apply_structure(doc, joins=[[nonbody["id"], b]])             # translate=False への join は拒否
    assert doc["joins"] == [] and w


def test_apply_structure_charmap_reextract(jsr):
    doc = copy.deepcopy(jsr)
    nf = sum(len(p["frames"]) for p in doc["pages"])
    E.apply_structure(doc, roles={"p2-f1": "body"}, charmap={"AdvP": {"\x02": "Ö"}})
    assert sum(len(p["frames"]) for p in doc["pages"]) == nf
    assert doc["charmap"]["AdvP"]["\x02"] == "Ö" and doc["roles_override"]["p2-f1"] == "body"
    assert frames_of(doc)["p2-f1"]["role"] == "body"


def test_translation_cache_key(tmp_path, jsr):
    units = build_units(jsr)[:6]
    ph = prompt_hash()
    assert len(ph) == 64
    k1, k2 = unit_key(units[0], "m1", ph), unit_key(units[0], "m2", ph)
    assert k1 != k2 and k1 == unit_key(dict(units[0], id="u99"), "m1", ph)           # id (連番) に依存しない
    assert unit_key(units[0], "m1", ph) != unit_key(units[0], "m1", "x")
    calls = []

    class T(DummyTranslator):
        def translate(self, units, context=None):
            calls.append(len(units))
            return super().translate(units, context)

    cp = tmp_path / "c.json"
    r1 = translate_cached(T(), units, cp)
    r2 = translate_cached(T(), units, cp)
    assert r1 == r2 and calls == [6]                                                    # 2 回目は全てキャッシュ
    rev = [dict(u, id=f"x{i}") for i, u in enumerate(reversed(units))]                  # id が振り直されても再利用
    r3 = translate_cached(T(), rev, cp)
    assert calls == [6] and r3["x0"] == r1[units[-1]["id"]]
    translate_cached(T(0.5), units, cp)                                                 # モデル (ratio) が違えば再翻訳
    assert calls == [6, 6]


# ---------------------------------------------------------------- 軽微 (CLI / work 名 / config)

def test_cli_defaults_and_work_name(tmp_path):
    from readable.cli import build_parser
    assert build_parser().parse_args(["x.pdf"]).translator == "gemini"
    a, b = tmp_path / "d1" / "paper.pdf", tmp_path / "d2" / "paper.pdf"
    a.parent.mkdir()
    b.parent.mkdir()
    a.write_bytes(b"AAA")
    b.write_bytes(b"BBB")
    assert work_name(a) != work_name(b) and work_name(a).startswith("paper-")      # M5: 内容のハッシュ (場所ではなく)
    c = tmp_path / "paper2.pdf"
    c.write_bytes(b"AAA")
    assert work_name(a).split("-")[1] == work_name(c).split("-")[1]                  # 同じ内容なら場所・名前が違っても同じ


def test_config_defaults_single_source():
    cfg = load_config(Path("nonexistent.toml"))
    assert cfg.get("render", "min_scale") == 0.7 and cfg.get("extract", "translate_tables") is False
    assert cfg.section("fonts")["serif_regular"]
    assert load_config().get("render", "min_scale") == 0.7


def test_cli_end_to_end_small(tmp_path, capsys):
    pdf = links_doc(tmp_path / "G.pdf")
    rc = cli_main([str(pdf), "--out", str(tmp_path / "out"), "--work-dir", str(tmp_path / "w"), "--no-claude", "--translator", "dummy"])
    assert rc == 0
    assert (tmp_path / "out" / "G_ja.pdf").exists() and (tmp_path / "out" / "G_dual.pdf").exists()
    out = capsys.readouterr().out
    assert "links:" in out and "しおり 3/3" in out
