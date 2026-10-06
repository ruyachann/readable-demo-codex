"""M9 の合成 PDF テスト: 左=本文 / 右=ぶら下げインデントの参考文献、の列混在。ユーザーの論文そのものは入れない。"""
from __future__ import annotations

import fitz
import pytest

from readable.extract import extract_pdf

WORDS = ("sleep memory consolidation reactivation targeted cueing spindle oscillation hippocampal neocortical performance "
         "accuracy difficulty participants stimulation session analysis group").split()


def _fit(words, k, width, size=9.0, font="tiro"):
    line = ""
    while fitz.get_text_length(line + " " + words[k % len(words)], fontname=font, fontsize=size) <= width:
        line = (line + " " + words[k % len(words)]).strip()
        k += 1
    return line, k


def body_left_refs_right_pdf(path):
    doc = fitz.open()
    pg = doc.new_page(width=595, height=790)
    k = 0
    for i in range(30):                                    # 左の列: 本文 (両端がほぼ揃う)
        line, k = _fit(WORDS, k, 252)
        pg.insert_text((40, 60 + i * 10.6), line, fontsize=9, fontname="tiro")
    for r in range(9):                                     # 右の列: 参考文献 (番号は 306、本文と続きは 323 から)
        y = 60 + r * 31.8
        pg.insert_text((306, y), "%d." % (9 + r), fontsize=9, fontname="tiro")
        for j in range(3):
            line, k = _fit(WORDS, k, 230 if j < 2 else 150)
            pg.insert_text((323, y + j * 10.6), line, fontsize=9, fontname="tiro")
    for r in range(8):                                     # 下の左の列: 参考文献 (番号は 40、本文は 57 から) = 偽のガターになりやすい
        y = 420 + r * 21.2
        pg.insert_text((40, y), "%d." % (r + 1), fontsize=9, fontname="tiro")
        for j in range(2):
            line, k = _fit(WORDS, k, 225 if j == 0 else 120)
            pg.insert_text((57, y + j * 10.6), line, fontsize=9, fontname="tiro")
    doc.save(path)
    return path


def test_two_columns_with_hanging_references_are_not_merged(tmp_path):
    d = extract_pdf(body_left_refs_right_pdf(tmp_path / "p.pdf"))
    frames = d["pages"][0]["frames"]
    assert not any(f["bbox"][0] < 296 and f["bbox"][2] > 305 and f["bbox"][1] < 380 for f in frames)      # 上の帯で列をまたぐ frame は無い
    right = [f for f in frames if f["bbox"][0] >= 300]
    assert len(right) == 9 and all(f["nrows"] == 3 and f["text"].split()[0].rstrip(".").isdigit() for f in right)   # 1 文献 = 1 frame (番号つき)
    left = [f for f in frames if f["bbox"][2] < 300 and f["bbox"][1] < 380]
    assert left and all(f["nrows"] >= 3 for f in left)


# =============================================================== 英語併記は各ページ初出のみ・注釈・図の文字

def test_gloss_is_kept_only_at_first_occurrence_on_a_page():
    from readable.render import strip_repeated_gloss
    h = ["パーソナライズTMR (Personalized TMR) は効く。また パーソナライズTMR (Personalized TMR) と (p < 0.05) 。",
         "<b>パーソナライズTMR (Personalized TMR)</b> と標準 (CNT) と標準 (CNT)"]
    out = strip_repeated_gloss(h, ["Personalized TMR", "CNT"])
    assert out[0].count("Personalized TMR") == 1 and "(p < 0.05)" in out[0]                 # 初出だけ残す。表の用語でない括弧は触らない
    assert "Personalized TMR" not in out[1] and out[1].count("CNT") == 1 and "<b>パーソナライズTMR</b>" in out[1]


def test_invisible_annotation_style_draws_nothing_but_keeps_the_text():
    from readable.render import add_cell_notes
    d = fitz.open()
    pg = d.new_page(width=300, height=100)
    pg.insert_text((20, 40), "Correct-Correct", fontsize=14)
    before = pg.get_pixmap(dpi=100).samples
    assert add_cell_notes(d, [{"page": 1, "rect": [18, 26, 140, 46], "text": "正解-正解"}], "invisible") == 1
    assert pg.get_pixmap(dpi=100, annots=True).samples == before                                # 見た目は変わらない
    a = list(pg.annots())[0]
    assert a.info["content"] == "正解-正解" and a.opacity == 0.0 and not (a.flags & fitz.PDF_ANNOT_IS_HIDDEN)


def test_figure_text_is_annotated_not_translated(tmp_path):
    from readable.config import Config
    from readable.translate import build_cell_units
    doc = {"pages": [{"number": 1, "frames": [
        {"id": "p1-f1", "role": "figure_text", "text": "Time (sec)", "rows": [[0, 0, 1, 1]], "row_texts": ["Time (sec)"]},
        {"id": "p1-f2", "role": "figure_text", "text": "0.5", "rows": [[0, 0, 1, 1]], "row_texts": ["0.5"]},
        {"id": "p1-f3", "role": "table", "text": "Group A", "rows": [[0, 0, 1, 1]], "row_texts": ["Group A"]},
        {"id": "p1-f4", "role": "body", "text": "Body text", "rows": [[0, 0, 1, 1]]}]}]}
    cells = build_cell_units(doc, Config({"table_terms": {"annotations": True}}))
    assert sorted(c["text"] for c in cells) == ["Group A", "Time (sec)"]       # 数字だけは除く。本文は含めない


def test_identical_figure_labels_are_translated_once_and_units_are_bundled_by_page():
    from test_m23 import translator, tr_of
    from readable.translate import Cache
    units = []
    for p in (1, 2, 3):
        units.append({"id": f"u{p}", "role": "body", "page": p, "frames": [], "text": f"Body text of page {p}.", "vars": {}})
    cells = [{"id": f"c{i}", "role": "footnote", "page": 1 + i % 3, "frames": [], "text": "Time (sec)", "vars": {}, "cell": ["f", i]}
             for i in range(6)]
    t, sdk, _ = translator(lambda n, m, req: tr_of(req["units"]), refine=False, pages_per_batch=6)
    res = t.translate(units + cells, context={}, cache=Cache("nul"))
    assert len(sdk.calls) == 1                                                     # 本文と注釈用の unit は 1 リクエスト
    import json
    sent = json.loads(sdk.calls[0]["contents"])["units"]
    assert sum(1 for u in sent if u["text"] == "Time (sec)") == 1                   # 同じ原文は 1 つだけ送る
    assert all(res[f"c{i}"] == res["c0"] for i in range(6)) and all(f"u{p}" in res for p in (1, 2, 3))


def test_parallel_batches_give_the_same_result_as_sequential():
    from test_m23 import translator, tr_of, mk_units
    from readable.translate import Cache
    units = mk_units(12, per_page=2)
    t1, s1, _ = translator(lambda n, m, req: tr_of(req["units"]), refine=False, pages_per_batch=2, workers=1)
    t3, s3, _ = translator(lambda n, m, req: tr_of(req["units"]), refine=False, pages_per_batch=2, workers=3)
    assert t1.translate(units, context={}, cache=Cache("nul")) == t3.translate(units, context={}, cache=Cache("nul"))
    assert len(s1.calls) == len(s3.calls) == 3


# =============================================================== 開発用の予算上限 (READABLE_GEMINI_MAX_REQUESTS)

def test_budget_limit_stops_before_sending_and_keeps_the_cache(tmp_path, monkeypatch):
    import json
    from test_m23 import translator, tr_of, mk_units
    from readable.gemini_client import BudgetExceeded, GeminiError
    from readable.translate import translate_cached
    monkeypatch.setenv("READABLE_GEMINI_MAX_REQUESTS", "1")
    t, sdk, _ = translator(lambda n, m, req: tr_of(req["units"]), refine=False, pages_per_batch=1, workers=1)
    units = mk_units(4, per_page=2)                       # 2 ページ = 2 バッチ
    cp = tmp_path / "c.json"
    with pytest.raises(BudgetExceeded) as e:
        translate_cached(t, units, cp, context={})
    assert isinstance(e.value, GeminiError) and e.value.limit == 1 and e.value.sent == 1
    assert "開発用の予算上限" in str(e.value) and "READABLE_GEMINI_MAX_REQUESTS=1" in str(e.value)
    assert len(sdk.calls) == 1                            # 2 つ目は送らない
    assert len(json.loads(cp.read_text(encoding="utf-8"))) >= 1       # 送れた分はキャッシュに保存されている
    # 上限を外して再実行すると、残りだけを送って完了する (続きから再開)
    monkeypatch.delenv("READABLE_GEMINI_MAX_REQUESTS")
    t2, sdk2, _ = translator(lambda n, m, req: tr_of(req["units"]), refine=False, pages_per_batch=1, workers=1)
    res = translate_cached(t2, units, cp, context={})
    assert len(sdk2.calls) == 1 and len(res) == 4


def test_budget_limit_exit_code_and_message_in_cli(tmp_path, monkeypatch, capsys):
    from test_m23 import translator, tr_of
    from synth_pdfs import one_page_paper
    from readable import cli
    monkeypatch.setenv("READABLE_GEMINI_MAX_REQUESTS", "1")
    t, sdk, _ = translator(lambda n, m, req: tr_of(req["units"]) if "units" in req else [], refine=False, glossary=False)
    monkeypatch.setattr(cli, "make_translator", lambda *a, **k: t)
    cfg = tmp_path / "c.toml"
    cfg.write_text('[gemini]\nglossary = false\npages_per_batch = 1\nrpm = 60000\n[structure]\nprovider = "none"\n', encoding="utf-8")
    pdf = one_page_paper(tmp_path / "p.pdf")
    t.client.max_requests = 0 or 1
    t.client.stats["requests"] = 1                         # すでに上限まで送った状態から始める
    rc = cli.main([str(pdf), "--out", str(tmp_path / "o"), "--work-dir", str(tmp_path / "w"), "--no-structure", "--config", str(cfg)])
    err = capsys.readouterr().err
    assert rc == 9 and "開発用の予算上限" in err and not sdk.calls


def test_gutter_made_by_figure_labels_does_not_split_body_lines_below_the_figure(tmp_path):
    doc = fitz.open()
    pg = doc.new_page(width=595, height=500)
    for r in range(9):                                     # 図の中の軸ラベル 3 列 (間が縦に揃う = 見かけのガター)
        y = 100 + r * 20
        for x in (100, 260, 420):
            pg.insert_text((x, y), "Label%d" % r, fontsize=9, fontname="tiro")
    # 図の下の本文の行: 語どうしを別々に書き、(140, 200) の位置に 8pt の空き (見かけのガターと重なる位置)
    words = ["Sleep", "supports", "memory", "consolidation", "during", "slow", "wave", "sleep"]
    x = 40.0
    for w in words:
        pg.insert_text((x, 340), w, fontsize=9, fontname="tiro")
        x += fitz.get_text_length(w, fontname="tiro", fontsize=9) + 8
    pg.insert_text((40, 352), "Next line of the paragraph continues here with more words to fill the row of text.", fontsize=9, fontname="tiro")
    doc.save(tmp_path / "g.pdf")
    frames = extract_pdf(tmp_path / "g.pdf")["pages"][0]["frames"]
    body = [f for f in frames if "Sleep supports" in f["text"]]
    assert len(body) == 1 and "slow wave sleep" in body[0]["text"]                 # 1 行のまま (単語ごとに割れない)


# =============================================================== M11: マーカー記号の漏れ・タグの入れ子・比較演算子の言い換え

def test_stray_marker_symbols_are_rejected_repaired_and_swept_before_render():
    from readable.translate import frames_translations, repair_style_tags, sanitize_leftovers, validate_translation
    src, bad = "mounted ~2 cm below the right eye", "右目の約 ⟦~⟧ 2 cm 下に装着"
    assert any("マーカー記号" in p for p in validate_translation(src, bad))                  # 検証で検出する
    assert repair_style_tags(src, bad) == "右目の約 ~ 2 cm 下に装着"                          # ⟦~⟧ の囲みだけ外して使う
    assert not validate_translation("a ⟦1⟧ b", "あ ⟦1⟧ い")                                    # 本物のマーカーは正常
    assert sanitize_leftovers("約 ⟦~⟧ 2 cm ⟦1⟧ {v3}") == "約 ~ 2 cm  "                         # 描画直前の安全網
    doc = {"pages": [{"number": 1, "frames": [{"id": "p1-f1", "html": "x"}]}]}
    unit = {"id": "u0", "frames": ["p1-f1"], "text": "x", "vars": {}}
    warns: list[str] = []
    assert frames_translations(doc, [unit], {"u0": "約 ⟦~⟧ 2 cm"}, warns) == {"p1-f1": "約 ~ 2 cm"} and warns


def test_bold_italic_nesting_is_tolerated_but_sup_sub_are_not():
    from readable.translate import repair_style_tags, validate_translation
    src = "see <b><i>Iber et al., 2007</i></b> and p&lt;0.05"
    assert not validate_translation(src, "<i>Iber et al., 2007</i> を参照。p&lt;0.05")                 # 二重の太字斜体 -> 片方だけ
    assert not validate_translation(src, "<b>Iber et al., 2007</b> を参照。p&lt;0.05")
    assert not validate_translation(src, "Iber et al., 2007 を参照。p&lt;0.05")                         # 書体の省略
    assert repair_style_tags("<b><i>a</i></b>", "<b><i>あ</b></i>") == "あ"                           # 入れ子の順序が崩れたら b/i を外す
    assert validate_translation("x <sup>2</sup> y", "x 2 y")                                          # sup は個数まで一致させる


def test_comparison_operator_paraphrases_are_allowed_but_reversals_are_not():
    from readable.numcheck import check_numbers
    src = "(p-values &gt;0.05) of sleep"
    assert not check_numbers(src, "p値が0.05より大きい")
    assert not check_numbers(src, "p値が0.05を上回る")
    assert check_numbers(src, "p値が0.05より小さい") and check_numbers(src, "p値が0.05を下回る")        # 向きが逆なら拒否


# =============================================================== M12: 構造解析の選択採用・Elsevier の箇条書き記号・注釈の既定

def test_elsevier_highlight_bullet_font_is_mapped_and_each_item_is_one_frame(tmp_path):
    from readable.extract import apply_charmap, merge_charmap
    cm = merge_charmap(None, None)
    assert apply_charmap("d", "AdvPSMPi6", cm) == "•"            # 記号フォントの 'd' は箇条書きの記号
    assert apply_charmap("d", "AdvPSHN-M", cm) == "d"                  # 本文のフォントの d は触らない
    assert apply_charmap("\x01", "AdvPSMPi6", cm) == "−"          # AdvP の既存の表も効く


def test_annotations_are_off_by_default_and_no_cells_are_translated():
    from readable.config import Config
    from readable.translate import build_cell_units
    cfg = Config({})
    assert cfg.get("table_terms", "annotations") is False and cfg.get("table_terms", "color") == ""
    doc = {"pages": [{"number": 1, "frames": [{"id": "p1-f1", "role": "table", "text": "Group A", "rows": [[0, 0, 1, 1]], "row_texts": ["Group A"]}]}]}
    assert build_cell_units(doc, cfg) == []                            # 注釈なしなら表のセル・図の語を訳さない (Gemini の要求を使わない)


# =============================================================== M13: ドロップキャップ・join の読み順・誌名ラベル・縮小の順序

def drop_cap_pdf(path):
    doc = fitz.open()
    pg = doc.new_page(width=300, height=400)
    pg.insert_text((40, 90), "I", fontsize=40, fontname="tiro")                    # 装飾の頭文字 (本文の 4 倍)
    body = ["s the visual imagery of dreams more like perception or imagination?",
            "This question has been asked at least since Aristotle but until now has",
            "lacked empirical data. Here we perform an objective test of this question",
            "using smooth pursuit eye movements in lucid dreams and in waking life.",
            "The results show that tracking in dreams resembles perception closely.",
            "A second sentence follows to fill the paragraph to a sensible length."]
    x = [56, 56, 40, 40, 40, 40]
    for k, (t, xx) in enumerate(zip(body, x)):
        pg.insert_text((xx, 60 + k * 11.5 + 11), t, fontsize=9.5, fontname="tiro")
    for k in range(14):                                                            # 後ろの別の段落
        pg.insert_text((40, 200 + k * 11.5), "Since the discovery of REM sleep in the 1950s dreams have been believed vivid.", fontsize=9.5, fontname="tiro")
    doc.save(path)
    return path


def test_drop_cap_is_merged_into_one_paragraph(tmp_path):
    d = extract_pdf(drop_cap_pdf(tmp_path / "dc.pdf"))
    fr = [f for f in d["pages"][0]["frames"] if f["bbox"][1] < 150]
    assert len(fr) == 1 and fr[0]["text"].startswith("Is the visual imagery") and fr[0]["nrows"] == 6      # 頭文字つきの 1 段落
    assert fr[0]["bbox"][0] <= 41 and "Aristotle" in fr[0]["text"] and "tracking in dreams" in fr[0]["text"]


def test_join_with_reversed_stream_order_is_accepted_when_adjacent_in_the_same_column():
    from readable.extract import validate_joins
    fr = {"a": {"translate": True, "page": 1, "bbox": [40, 60, 290, 90], "size": 9.5},
          "b": {"translate": True, "page": 1, "bbox": [40, 40, 290, 100], "size": 9.5},
          "c": {"translate": True, "page": 1, "bbox": [300, 40, 550, 100], "size": 9.5}}
    ok, w = validate_joins(fr, ["b", "a", "c"], [["a", "b"]])         # 順序が逆でも、同じ列で重なる (ドロップキャップ) なら採用
    assert ok == [["a", "b"]]
    ok, w = validate_joins(fr, ["b", "a", "c"], [["c", "a"]])         # 別の列は従来どおり拒否
    assert not ok and "逆行" in w[0]


def test_article_type_labels_are_not_translated_and_open_is_a_page_header():
    from readable.labels import label_override
    for t in ("ARTICLE", "OPEN", "RESEARCH ARTICLE", "Short communication"):
        assert label_override("page_header", t) is None
    from readable.extract import ARTICLE_TYPE_RE
    assert ARTICLE_TYPE_RE.match("OPEN") and ARTICLE_TYPE_RE.match("ARTICLE")


def test_reflow_tightens_line_spacing_before_shrinking(tmp_path):
    from readable.config import Config
    from readable.render import render_ja
    from readable.translate import build_units, frames_translations
    src = tmp_path / "s.pdf"
    doc = fitz.open()
    pg = doc.new_page(width=300, height=300)
    for k in range(4):
        pg.insert_text((40, 60 + k * 11.5), "The quick brown fox jumps over the lazy dog while reading a long paper %d." % k, fontsize=9.5, fontname="tiro")
    pg.draw_rect(fitz.Rect(30, 118, 270, 122), fill=(0, 0, 0))                       # すぐ下に図形 (広げられない)
    doc.save(src)
    d = extract_pdf(src)
    units = build_units(d)
    ja = {u["id"]: "これはとても長い訳文であり、原文より少しだけ長いので、行間を詰めれば文字を縮めずに収まるはずである。" * 2 for u in units}
    fr = frames_translations(d, units, ja)
    new = render_ja(src, d, fr, tmp_path / "n.pdf", cfg=Config({"render": {"reflow_first": True}}))
    old = render_ja(src, d, fr, tmp_path / "o.pdf", cfg=Config({"render": {"reflow_first": False}}))
    assert new["min_scale"] >= old["min_scale"] and new["failed"] == 0
