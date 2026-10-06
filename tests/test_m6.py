"""M6 (表の用語と本文の対応づけ: keep_en・色づけ・ホバー注釈) と、構造解析プロバイダの境界のテスト。"""
from __future__ import annotations

import json
from pathlib import Path

import fitz
import pytest

from readable import cli
from readable import structure as st
from readable.config import Config
from readable.extract import collect_table_terms, extract_pdf, _term_from_text
from readable.glossary import _clean, build_glossary_input, filter_glossary, merge_fixed
from readable.render import add_cell_notes, mark_table_terms, render_ja
from readable.translate import (KEEP_EN_RULE, GeminiTranslator, build_cell_units, check_keep_en, translate_cached)
from synth_pdfs import one_page_paper
from test_m23 import mk_units, tr_of, translator

ROOT = Path(__file__).resolve().parent.parent
MDPI = ROOT / "english_paper" / "jzbg-07-00024.pdf"


# ---------------------------------------------------------------- A: 用語の収集

@pytest.mark.parametrize("text,expect", [
    ("Locomotion (Lo)", {"en": "Locomotion", "abbr": "Lo"}), ("Eating/Drinking (ED)", {"en": "Eating/Drinking", "abbr": "ED"}),
    ("Active", {"en": "Active", "abbr": ""}), ("Behavioral Class and Behaviors", {"en": "Behavioral Class and Behaviors", "abbr": ""}),
])
def test_term_from_text_accepts_terms(text, expect):
    assert _term_from_text(text) == expect


@pytest.mark.parametrize("text", ["12.5", "0.05%", "24 April 2026", "Directed non-repetitive movement.", "(Abbreviated)", "min", "Hz",
                                  "Any part of the body submerged in water and held there for a while", "±1.62", "p = 0.05"])
def test_term_from_text_rejects_numbers_dates_units_and_sentences(text):
    assert _term_from_text(text) is None


@pytest.mark.skipif(not MDPI.exists(), reason="sample PDF missing")
def test_mdpi_table_terms_are_collected():
    d = extract_pdf(MDPI)
    terms = {t["en"]: t["abbr"] for t in d["table_terms"]}
    assert terms["Locomotion"] == "Lo" and terms["Social"] == "So" and terms["Other"] == "Ot" and "Active" in terms
    assert not any(any(ch.isdigit() for ch in t["en"]) for t in d["table_terms"])          # 数値・日付は含まない
    assert not any(t["en"].endswith(".") for t in d["table_terms"])                         # 定義文は含まない
    cells = build_cell_units(d, Config({"table_terms": {"annotations": True}}))
    assert cells and all(u["id"].startswith("c") and u["cell"] for u in cells) and len(cells) <= 200
    assert build_cell_units(d, Config({"table_terms": {"annotations": False}})) == []
    gi = build_glossary_input(d)
    assert "Locomotion (Lo)" in gi["table_terms"]


def test_caption_terms_only_capitalized():
    class F:
        def __init__(s, role, text, html, page=1): s.role, s.text, s.html, s.page, s.row_texts = role, text, html, page, []
    fr = [F("caption", "test and Remembered and “Not Remembered”", "<i>test</i> and <i>Remembered</i> and “Not Remembered”")]
    got = [t["en"] for t in collect_table_terms(fr)]
    assert "Remembered" in got and "Not Remembered" in got and "test" not in got


# ---------------------------------------------------------------- A: 用語集・翻訳

def test_glossary_keeps_keep_en_flags_and_fixed_merge_preserves_them():
    gl = _clean([{"en": "Locomotion", "ja": "移動", "keep_en": True, "abbr": "Lo"}, {"en": "otter", "ja": "カワウソ", "keep_en": False}])
    assert gl[0]["keep_en"] is True and gl[0]["abbr"] == "Lo" and "keep_en" not in gl[1]
    fixed = [{"en": "locomotion", "ja": "運動", "note": "固定訳"}]
    g, _ = merge_fixed(gl, fixed, "Locomotion was frequent")
    assert g[0]["ja"] == "運動" and g[0]["keep_en"] is True and g[0]["abbr"] == "Lo"       # 固定訳が優先でも keep_en は残る
    assert [x["en"] for x in filter_glossary([{"en": "Active", "ja": "活動", "keep_en": True}], "inactive and Active")] == ["Active"]
    assert filter_glossary([{"en": "Active", "ja": "活動", "keep_en": True}], "interactive") == []     # 単語境界


def test_check_keep_en_only_for_distinctive_terms():
    e = [{"en": "Locomotion", "ja": "移動", "keep_en": True, "abbr": "Lo"}, {"en": "Other", "ja": "その他", "keep_en": True},
         {"en": "Manipulating Object", "ja": "物体の操作", "keep_en": True}]
    assert check_keep_en("Locomotion was frequent", "移動が多かった", e)
    assert not check_keep_en("Locomotion was frequent", "移動 (Locomotion, Lo) が多かった", e)
    assert not check_keep_en("other otters", "他のカワウソ", e)                       # 一般語と重なる 1 語は検査しない
    assert check_keep_en("Manipulating Object occurred", "物体の操作が起きた", e)


def test_keep_en_rule_is_added_only_for_batches_with_such_terms():
    t, sdk, _ = translator(lambda n, m, r: tr_of(r["units"]), refine=False)
    plain = t._system("translate", [], {"glossary": [{"en": "a", "ja": "あ"}]})
    with_rule = t._system("translate", [], {"glossary": [{"en": "a", "ja": "あ", "keep_en": True}]})
    assert KEEP_EN_RULE.strip() in with_rule and KEEP_EN_RULE.strip() not in plain
    ctx = GeminiTranslator._ctx({"glossary": [{"en": "Locomotion", "ja": "移動", "keep_en": True, "abbr": "Lo", "note": "x"}]},
                                [{"text": "Locomotion"}], [])
    assert ctx["glossary"] == [{"en": "Locomotion", "ja": "移動", "keep_en": True, "abbr": "Lo"}]


def test_missing_english_is_resent_then_accepted_with_warning(tmp_path):
    units = mk_units(1, text="Locomotion (Lo) occurred often")
    ctx = {"glossary": [{"en": "Locomotion", "ja": "移動", "keep_en": True, "abbr": "Lo"}], "title": "", "summary": ""}
    calls = []

    def h(n, m, r):
        calls.append(r["units"][0].get("note"))
        txt = "移動が多かった [1] {v1}"
        if n == 2:
            txt = "移動 (Locomotion, Lo) が多かった [1] {v1}"
        return [{"id": u["id"], "text": txt} for u in r["units"]]

    # 2 回目で併記された訳が来る
    t, sdk, _ = translator(h, refine=False)
    u = dict(units[0], text="Locomotion occurred often [1] {v1}")
    got, bad = t._translate_batch([u], dict(t._ctx({"glossary": ctx["glossary"]}, [u], [])), ctx["glossary"])
    assert not bad and "(Locomotion, Lo)" in got[u["id"]] and calls[1] and "表の用語" in calls[1]
    # 何度送っても併記されない: 原文に戻さず、訳を採用して警告
    t2, sdk2, _ = translator(lambda n, m, r: [{"id": x["id"], "text": "移動が多かった [1] {v1}"} for x in r["units"]], refine=False)
    got2, bad2 = t2._translate_batch([u], dict(t2._ctx({"glossary": ctx["glossary"]}, [u], [])), ctx["glossary"])
    assert not bad2 and got2[u["id"]].startswith("移動") and any("併記" in w for w in t2.warnings)


# ---------------------------------------------------------------- B: 色づけ

def test_mark_table_terms_colors_only_matching_parentheses():
    html = "活動 (Active) と移動 (Locomotion, Lo)、統計(Statistics)、<a href=\"x(Active)\">リンク</a>"
    out = mark_table_terms(html, ["Active", "Locomotion", "Lo"], "#1a3d8f")
    assert out.count("color:#1a3d8f") == 2 and "text-decoration:underline" in out
    assert "(Statistics)" in out and 'color' not in out.split("(Statistics)")[1].split("<a")[0]
    assert 'href="x(Active)"' in out                                     # タグの属性は触らない


# ---------------------------------------------------------------- D: 注釈

def _pdf_with_cell(tmp_path):
    doc = fitz.open()
    pg = doc.new_page(width=300, height=200)
    pg.insert_text((40, 60), "Locomotion (Lo)", fontsize=10, fontname="helv")
    p = tmp_path / "t.pdf"
    doc.save(p)
    return p


def test_cell_notes_are_added_with_contents_and_do_not_change_the_page_text(tmp_path):
    p = _pdf_with_cell(tmp_path)
    src = fitz.open(p)
    before = src[0].get_text()
    n = add_cell_notes(src, [{"page": 1, "rect": [38, 50, 120, 64], "text": "移動 (Locomotion, Lo)"}])
    assert n == 1 and src[0].get_text() == before
    an = [(a.type[1], a.info["content"]) for a in src[0].annots()]
    assert an == [("Highlight", "移動 (Locomotion, Lo)")]
    src2 = fitz.open(p)
    add_cell_notes(src2, [{"page": 1, "rect": [38, 50, 120, 64], "text": "訳"}], style="text")
    assert [a.type[1] for a in src2[0].annots()] == ["Text"]
    out = tmp_path / "o.pdf"
    src.save(out)
    assert [a.info["content"] for a in fitz.open(out)[0].annots()][0].startswith("移動")      # 保存・再読込しても残る


def test_build_annotations_and_dual_keep_english_pages_untouched(tmp_path):
    doc = {"pages": [{"frames": [{"id": "p1-f1", "page": 1, "rows": [[38, 50, 120, 64]], "role": "table"}]}]}
    cells = [{"id": "c0", "text": "Locomotion (Lo)", "cell": ["p1-f1", 0]}, {"id": "c1", "text": "12", "cell": ["p1-f1", 0]}]
    ann = cli.build_annotations(doc, cells, {"c0": "移動 (Locomotion, Lo)", "c1": "12"})
    assert ann == [{"page": 1, "rect": [38, 50, 120, 64], "text": "移動 (Locomotion, Lo)"}]      # 訳が原文と同じセルは付けない
    # render_ja -> build_dual: 日本語ページだけに注釈、英語ページは無傷
    from readable.render import build_dual
    pdf = one_page_paper(tmp_path / "p.pdf")
    d = extract_pdf(pdf)
    f = next(f for p in d["pages"] for f in p["frames"] if f["translate"])
    ja = tmp_path / "ja.pdf"
    rep = render_ja(pdf, d, {f["id"]: "日本語"}, ja, annotations=[{"page": 1, "rect": f["bbox"], "text": "訳"}])
    assert rep["annotations"] == 1
    dual = tmp_path / "dual.pdf"
    build_dual(pdf, ja, dual)
    dd = fitz.open(dual)
    assert sum(1 for _ in (dd[0].annots() or [])) == 0 and sum(1 for _ in (dd[1].annots() or [])) == 1


# ---------------------------------------------------------------- 構造解析プロバイダの境界

def test_structure_provider_none_and_unknown(jsr, tmp_path):
    import copy
    d = copy.deepcopy(jsr)
    logs = []
    info = st.run_structure(d, tmp_path / "a", Config({"structure": {"provider": "none"}}), runner=object())
    assert info["reason"] == "disabled"
    with pytest.raises(st.StructureConfigError):
        st.run_structure(copy.deepcopy(jsr), tmp_path / "b", Config({"structure": {"provider": "unsupported"}}), log=logs.append, runner=object())
    assert st.PROVIDERS["claude"] is st.call_claude


def test_structure_provider_doc_exists_and_mentions_schema_keys():
    t = (ROOT / "docs" / "STRUCTURE_PROVIDER.md").read_text(encoding="utf-8")
    for k in ("joins_add", "joins_remove", "charmap", "roles", "PROVIDERS", "claude_structure.md"):
        assert k in t
