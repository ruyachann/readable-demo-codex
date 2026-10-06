"""⟦n⟧ 分割 / Dummy 翻訳 / レンダリング (結合テスト)"""
import re

import fitz
import pytest
from conftest import MDPI, frames_of

from readable.translate import (DummyTranslator, build_units, frames_translations, marker, sanitize_ja,
                                split_by_ratio, split_unit)


def test_split_unit_with_markers():
    ja = "前半の文。 ⟦1⟧ 中間の文。 ⟦2⟧ 後半の文。"
    assert split_unit(ja, [10, 10, 10]) == ["前半の文。", "中間の文。", "後半の文。"]


def test_split_unit_missing_markers_ratio():
    ja = "あいうえお。かきくけこ。さしすせそ。たちつてと。"
    a, b = split_unit(ja, [20, 20])
    assert a + b == ja and a.endswith("。") and b.endswith("。") and len(a) == 12
    a, b = split_by_ratio(ja, [30, 10])
    assert a == "あいうえお。かきくけこ。さしすせそ。" and b == "たちつてと。"


def test_split_unit_wrong_marker_count_falls_back():
    ja = "あいうえお。かきくけこ。 ⟦1⟧ さしすせそ。たちつてと。 ⟦2⟧ なにぬねの。"
    parts = split_unit(ja, [10, 10])  # 2 frame なのにマーカーが2個 -> 比率分割
    assert len(parts) == 2 and all(parts)
    assert "⟦" not in "".join(parts)


def test_split_does_not_cut_inside_tag():
    ja = "あいう<sup>12</sup>えおかき<sub>ab</sub>くけこ。"
    for w in ([1, 1], [3, 1], [1, 3]):
        a, b = split_by_ratio(ja, w)
        assert a.count("<") == a.count(">") and b.count("<") == b.count(">")


def test_sanitize():
    assert sanitize_ja("a < b & c <sup>2</sup>") == "a &lt; b &amp; c <sup>2</sup>"


def test_dummy_preserves_tags_and_markers(mdpi):
    units = build_units(mdpi)
    assert units and all(u["frames"] for u in units)
    ja = DummyTranslator().translate(units)
    for u in units:
        t = ja[u["id"]]
        assert "【訳】" in t[:12]
        assert len(re.findall(r"⟦\d+⟧", t)) == len(re.findall(r"⟦\d+⟧", u["text"])) == len(u["frames"]) - 1
        for tag in ("<sup>", "<sub>", "<i>"):
            assert t.count(tag) == u["text"].count(tag)
    # 長さは英文の 0.38 倍程度
    u = max(units, key=lambda x: len(x["text"]))
    plain = re.sub(r"<[^>]+>", "", u["text"])
    jl = len(re.sub(r"<[^>]+>|⟦\d+⟧", "", ja[u["id"]]))
    assert 0.25 * len(plain) < jl < 0.55 * len(plain)
    # frame への配分
    fr = frames_translations(mdpi, units, ja)
    assert set(fr) == {f["id"] for f in frames_of(mdpi).values() if f["translate"]}


def test_units_follow_joins(mdpi):
    units = {tuple(u["frames"]): u for u in build_units(mdpi)}
    assert ("p3-f11", "p4-f3") in units
    assert units[("p3-f11", "p4-f3")]["text"].count(marker(1)) == 1


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    if not MDPI.exists():
        pytest.skip("sample PDF missing")
    from readable.extract import extract_pdf
    from readable.render import build_dual, render_ja
    d = tmp_path_factory.mktemp("out")
    doc = extract_pdf(MDPI)
    units = build_units(doc)
    fr = frames_translations(doc, units, DummyTranslator().translate(units))
    rep = render_ja(MDPI, doc, fr, d / "ja.pdf")
    n = build_dual(MDPI, d / "ja.pdf", d / "dual.pdf")
    return doc, rep, d, n


def test_render_report_and_pages(rendered):
    doc, rep, d, n = rendered
    assert rep["failed"] == 0 and rep["frames"] == sum(1 for p in doc["pages"] for f in p["frames"] if f["translate"])
    assert rep["min_scale"] >= 0.75 and rep["shrunk_ratio"] < 0.25
    assert len(fitz.open(d / "ja.pdf")) == 12 and n == 24
    assert (d / "ja.pdf").stat().st_size < 4_000_000


def test_render_removes_english_keeps_images(rendered):
    doc, rep, d, n = rendered
    out = fitz.open(d / "ja.pdf")
    src = fitz.open(MDPI)
    assert len(out[5].get_images()) == len(src[5].get_images()) >= 2   # 図が消えない
    F = frames_of(doc)
    for fid in ("p3-f5", "p3-f7", "p1-f12"):
        f = F[fid]
        txt = out[f["page"] - 1].get_text("text", clip=fitz.Rect(f["bbox"]))
        assert not re.search(r"[A-Za-z]{4,}", txt), (fid, txt[:80])           # 英文の消し残しなし
        assert "【訳】" in txt
    # 翻訳しない frame (表) は原文のまま
    t = out[3].get_text("text", clip=fitz.Rect(F["p4-f9"]["bbox"]))
    assert "Locomotion" in t


def test_dual_order(rendered):
    doc, rep, d, n = rendered
    dual = fitz.open(d / "dual.pdf")
    assert "Otters spend" in dual[0].get_text() and "【訳】" not in dual[0].get_text()
    assert "【訳】" in dual[1].get_text() and "Academic Editor" in dual[1].get_text()
    assert "Otters spend" not in dual[1].get_text()
