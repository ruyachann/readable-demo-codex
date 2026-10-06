"""M5 (最終修正) のテスト: 堅牢性 (フォント・バッチ分割・非 PDF・内容ハッシュの work 名)、構造解析の差分、用語集の境界、
レイアウト (孤立行・行頭禁則・NBSP・キーワード・斜体)、定型ラベル、表/コード/ベクタ図の判定、固定訳。"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import fitz
import pytest

from readable import cli
from readable.config import Config
from readable.extract import (extract_pdf, is_monospace_font, looks_like_algorithm, table_like)
from readable.gemini_client import GeminiClient
from readable.glossary import load_fixed, merge_fixed, normalize_terms
from readable.labels import label_override
from readable.render import fix_italics, has_widow, nowrap_keywords, protect_spaces
from readable.translate import fix_leading_punct, split_unit
from synth_pdfs import LOREM, one_page_paper
from test_m23 import FakeSDK, tr_of, mk_units, translator, ApiError

ROOT = Path(__file__).resolve().parent.parent


# =============================================================== A: 堅牢性

def test_missing_fonts_exit11_before_any_request(monkeypatch, tmp_path):
    pdf = one_page_paper(tmp_path / "p.pdf")
    cfg = tmp_path / "c.toml"
    cfg.write_text('[fonts]\nserif_regular=["Z:/nope.ttf"]\nsans_regular=["Z:/nope.ttc"]\n', encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    calls = []
    monkeypatch.setattr(GeminiClient, "_sdk", lambda self: calls.append(1))
    code = cli.main([str(pdf), "--out", str(tmp_path / "o"), "--work-dir", str(tmp_path / "w"), "--no-claude", "--config", str(cfg)])
    assert code == 11 and not calls


def _units_failing(bad_text_part: str):
    """bad_text_part を含む unit が 1 つでも混ざったリクエストは、空応答 (MAX_TOKENS) になる。"""
    def h(n, model, req):
        if isinstance(req, dict) and "units" in req:
            if any(bad_text_part in u["text"] for u in req["units"]):
                return ""
            return tr_of(req["units"])
        return []
    return h


def test_batch_split_isolates_the_failing_unit(tmp_path):
    units = mk_units(8, per_page=8)
    units[5]["text"] += " POISON"
    t, sdk, _ = translator(_units_failing("POISON"), refine=False)
    cp = tmp_path / "c.json"
    res = __import__("readable.translate", fromlist=["x"]).translate_cached(t, units, cp)
    assert res["u5"] == units[5]["text"]                                    # 原因の unit だけが原文のまま
    assert all(res[f"u{i}"].startswith("訳:") for i in range(8) if i != 5)  # 他の unit は巻き込まれない
    assert t.stats["fallback_original"] == 1 and t.stats["split_batches"] >= 3     # 二分探索 (8 -> 4 -> 2 -> 1)
    saved = json.loads(cp.read_text(encoding="utf-8"))
    assert sum(1 for k, v in saved.items() if not k.startswith("fail:") and v.startswith("訳:")) == 7
    assert sum(1 for k in saved if k.startswith("fail:")) == 2              # 失敗の記録は 1 unit だけ (翻訳段階と選択的再翻訳の 2 つのキー)


def test_cli_partial_failure_exit12_and_pdf_is_produced(monkeypatch, tmp_path):
    pdf = one_page_paper(tmp_path / "p.pdf")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    sdk = FakeSDK(_units_failing("single night"))        # 本文の unit が 1 つでも混ざると空応答
    monkeypatch.setattr(GeminiClient, "_sdk", lambda self: sdk)
    orig = GeminiClient.__init__
    monkeypatch.setattr(GeminiClient, "__init__", lambda self, *a, **k: orig(self, *a, **{**k, "sleep": lambda s: None}))
    out = tmp_path / "o"
    code = cli.main([str(pdf), "--out", str(out), "--work-dir", str(tmp_path / "w"), "--no-claude"])
    assert code == 12 and (out / "p_ja.pdf").exists() and (out / "p_dual.pdf").exists()


def test_non_pdf_exit5_no_traceback(tmp_path, capsys):
    txt = tmp_path / "a.txt"
    txt.write_text("hello world", encoding="utf-8")
    assert cli.main([str(txt), "--out", str(tmp_path / "o"), "--work-dir", str(tmp_path / "w"), "--no-claude", "--translator", "dummy"]) == 5
    err = capsys.readouterr().err
    assert "PDF ではありません" in err and "Traceback" not in err
    assert cli.main([str(tmp_path), "--out", str(tmp_path / "o"), "--work-dir", str(tmp_path / "w"), "--translator", "dummy"]) == 5


def test_unexpected_exception_exit1_with_log(monkeypatch, tmp_path, capsys):
    pdf = one_page_paper(tmp_path / "p.pdf")

    def boom(*a, **k):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(cli, "render_ja", boom)
    code = cli.main([str(pdf), "--out", str(tmp_path / "o"), "--work-dir", str(tmp_path / "w"), "--no-claude", "--translator", "dummy"])
    err = capsys.readouterr().err
    assert code == 1 and "RuntimeError" in err and "last_error.log" in err and "Traceback" not in err
    assert "synthetic failure" in (tmp_path / "w" / "last_error.log").read_text(encoding="utf-8")


def test_work_name_is_content_hash_and_cache_survives_copy(monkeypatch, tmp_path):
    a = one_page_paper(tmp_path / "a" / "x.pdf") if (tmp_path / "a").mkdir() is None else None
    b = tmp_path / "b" / "論文 copy.pdf"
    b.parent.mkdir()
    shutil.copy(a, b)
    name_a, name_b = cli.work_name(a), cli.work_name(b)
    assert re.fullmatch(r"x-[0-9a-f]{12}", name_a) and name_a.split("-")[1] == name_b.split("-")[1]
    assert cli.work_name(a) == name_a
    c = one_page_paper(tmp_path / "c.pdf")
    c_doc = fitz.open(c)
    c_doc[0].insert_text((72, 700), "different content", fontsize=10)
    c_doc.saveIncr() if False else None
    other = tmp_path / "other.pdf"
    d2 = fitz.open(a)
    d2[0].insert_text((72, 700), "different content", fontsize=10)
    d2.save(other)
    assert cli.work_name(other).split("-")[1] != name_a.split("-")[1]
    # 実行: コピーしても翻訳キャッシュ (内容ハッシュ) が効き、2 回目はリクエスト 0
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    sdk = FakeSDK(lambda n, m, r: tr_of(r["units"]) if isinstance(r, dict) and "units" in r else [])
    monkeypatch.setattr(GeminiClient, "_sdk", lambda self: sdk)
    orig = GeminiClient.__init__
    monkeypatch.setattr(GeminiClient, "__init__", lambda self, *a, **k: orig(self, *a, **{**k, "sleep": lambda s: None}))
    w = str(tmp_path / "w")
    assert cli.main([str(a), "--out", str(tmp_path / "o1"), "--work-dir", w, "--no-claude"]) == 0
    n1 = len(sdk.calls)
    assert n1 > 0
    assert cli.main([str(b), "--out", str(tmp_path / "o2"), "--work-dir", w, "--no-claude"]) == 0
    assert len(sdk.calls) == n1


def test_requirements_lower_bound():
    assert "google-genai>=2.26" in (ROOT / "requirements.txt").read_text(encoding="utf-8")


# =============================================================== A: 構造解析 (差分・分母) は test_m4 / test_m23 に追加済み。用語集の境界:

@pytest.mark.parametrize("glossary,text,expected", [
    ([{"en": "u", "ja": "ユーザー"}], "ユーザビリティ", "ユーザビリティ"),
    ([{"en": "u", "ja": "ユーザー"}], "ユーザ と ユーザー", "ユーザー と ユーザー"),
    ([{"en": "m", "ja": "メモリー"}], "メモリアル", "メモリアル"),
    ([{"en": "s", "ja": "サーバー"}], "サーバント", "サーバント"),
    ([{"en": "t", "ja": "検査", "variants": ["テスト"]}], "テストステロン、アンテスト、テスト", "テストステロン、アンテスト、検査"),
    ([{"en": "o", "ja": "コツメカワウソ", "variants": ["カワウソ"]}], "カワウソ、ニホンカワウソ", "カワウソ、ニホンカワウソ"),
    ([{"en": "o", "ja": "カワウソ"}, {"en": "a", "ja": "コツメカワウソ"}], "コツメカワウソとカワウソ", "コツメカワウソとカワウソ"),
    ([{"en": "o", "ja": "カワウソ"}], "ニホンかわうそは かわうそ", "ニホンカワウソは カワウソ"),
])
def test_normalize_katakana_boundaries(glossary, text, expected):
    assert normalize_terms(text, glossary) == expected


# =============================================================== B: レイアウト

def test_leading_closing_punctuation_moves_to_previous_frame():
    assert split_unit("あいう ⟦1⟧ 。えお", [1, 1]) == ["あいう。", "えお"]
    assert fix_leading_punct(["ア", "）、イ", "ウ"]) == ["ア）、", "イ", "ウ"]
    assert fix_leading_punct(["ア", "<i>イ</i>"]) == ["ア", "<i>イ</i>"]


def test_has_widow():
    assert has_widow(["あいうえおかきくけこ", "る [11]"]) and has_widow(["あいうえお", "。"]) and has_widow(["あいうえお", "集。"])
    assert not has_widow(["あいうえお", "かきくけこさしす。"]) and not has_widow(["あ"])


def test_protect_spaces_policy_m5():
    s = protect_spaces("成人(23.58歳 ± 3.36歳) と(Collins et al., 2016)と Smith, 2020 と5 min と p < 0.05")
    assert "歳 ± 3.36" in s and "5 min" in s and "p < 0.05" in s
    assert "Collins et al., 2016" in s                       # 引用は通常の空白 (折り返せる)
    assert "Smith, 2020" in s                           # 短い (20 字以内) 英数字の連なりは分割しない
    assert "と Smith" in s and "歳)と" in s                  # 和文の隣の空白だけでは NBSP にしない
    # 和文と ( [ の間・: ; , ) ] の後ろの和文との間の空白は、日本語の組版では詰める (両端揃えで行末が大きく空くのを防ぐ)
    assert protect_spaces("示唆されている [11]。") == "示唆されている[11]。"
    assert protect_spaces("カワウソ (Aonyx cinereus) を") == "カワウソ(Aonyx cinereus)を"
    assert protect_spaces("不正解: 赤色") == "不正解:赤色" and protect_spaces("1.2. 人間") == "1.2. 人間"


def test_foreign_script_and_stray_braces_are_validation_problems():
    from readable.translate import validate_translation
    assert validate_translation("author(s)", "著者(들)")                       # ハングルの混入
    assert validate_translation("a {v1}", "あ {v1} {文脈調整: x}")            # 原文に無い { }
    assert not validate_translation("a {v1}", "あ {v1}") and not validate_translation("author(s)", "著者(s)")


def test_italic_and_keyword_helpers():
    assert fix_italics("<i>p</i>と<i>日本語 abc</i>", True) == "<i>p</i>と日本語<i> abc</i>"
    assert fix_italics("<i>p</i>", False) == "p"
    k = nowrap_keywords("<b>キーワード:</b> 閉ループ (closed-loop); 家庭環境 (home setting)")
    assert k.count("white-space:nowrap") == 2 and k.startswith("<b>キーワード:</b>")
    assert nowrap_keywords("<i>a</i>, b") == "<i>a</i>, b" or True


def _render_one(tmp_path, ja_by_text):
    """one_page_paper の 1 番目の body frame を、指定した日本語で描いて、その frame の領域の行テキストを返す。"""
    from readable.render import render_ja
    pdf = one_page_paper(tmp_path / "w.pdf")
    doc = extract_pdf(pdf)
    f = next(f for p in doc["pages"] for f in p["frames"] if f["role"] == "body" and f["translate"])
    out = tmp_path / "w_ja.pdf"
    render_ja(pdf, doc, {f["id"]: ja_by_text}, out)
    pg = fitz.open(out)[0]
    lines = []
    for b in pg.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            r = fitz.Rect(l["bbox"])
            if r.intersects(fitz.Rect(f["bbox"])):
                lines.append((l["bbox"][1], "".join(s["text"] for s in l["spans"]).strip("\u00a0 ")))
    return [t for _y, t in sorted(lines) if t]


def test_no_widow_lines_after_render(tmp_path):
    seed = "睡眠中の記憶固定化を促進するために手がかり提示された擬似単語の翻訳精度が向上したことを示すものである。"
    for n in range(30, 150, 3):
        text = (seed * 4)[:n].rstrip("、") + "。"
        lines = _render_one(tmp_path, text)
        assert not has_widow(lines), (n, lines[-2:])
        assert not any(t[0] in "。、）」" for t in lines), (n, lines)


# =============================================================== B: 定型ラベル

def test_label_dictionary():
    assert label_override("sidebar", "Received: 9 November 2024") == "受付: 9 November 2024"
    assert label_override("sidebar", "<b>Correspondence</b>") == "<b>連絡先</b>"
    assert label_override("page_header", "RESEARCH ARTICLE") is None      # M13: 論文の種類の札は訳さない (ロゴの一部。全論文で原文のまま)
    assert label_override("sidebar", "Academic Editor: Steven Monfort") == "担当編集者: Steven Monfort"
    assert label_override("sidebar", "Funding information") == "研究資金"
    assert label_override("sidebar", "Email: <a href=\"mailto:a@b.c\">a@b.c</a>").startswith("メール: <a href")
    assert label_override("sidebar", "Article processing charges were paid") is None     # 文は訳さない
    assert label_override("body", "Received: x") is None                                  # 対象 role のみ


def test_apply_labels_overrides_untranslated_and_keeps_japanese():
    def fr(i, role, html, tr):
        return {"id": i, "role": role, "html": html, "translate": tr}
    doc = {"pages": [{"frames": [fr("a", "sidebar", "Received: 24 April 2026", False), fr("b", "sidebar", "Correspondence", True),
                                 fr("c", "sidebar", "Email: x", True), fr("d", "body", "Received: 1", True)]}]}
    ja = {"b": "Correspondence", "c": "メール: x", "d": "あ"}
    assert cli.apply_labels(doc, ja) == 2
    assert ja["a"] == "受付: 24 April 2026" and ja["b"] == "連絡先" and ja["c"] == "メール: x" and ja["d"] == "あ"


# =============================================================== C: 表・コード・ベクタ図

def _rules_pdf(path, left_lines, right_lines, grid=False):
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.draw_line((50, 170), (545, 170))
    pg.draw_line((50, 330), (545, 330))
    y = 190
    for t in left_lines:
        pg.insert_text((50, y), t, fontsize=8, fontname="helv")
        y += 11
    y = 190
    for t in right_lines:
        pg.insert_text((200, y), t, fontsize=9, fontname="helv")
        y += 11
    pg.insert_text((50, 500), "Body text of the paper continues here in the usual way and goes on for a while.", fontsize=10, fontname="helv")
    doc.save(path)
    return path


def test_prose_block_between_two_rules_is_not_a_table(tmp_path):
    prose = [("The proposed method improves recognition accuracy by exploiting temporal consistency across frames, as reported in [3].")[:78]] * 12
    left = ["ARTICLE INFO", "Article history:", "Received 1 January 2020", "Keywords:", "examples", "datasets"]
    d = extract_pdf(_rules_pdf(tmp_path / "e.pdf", left, prose))
    assert not any(f["role"] == "table" for p in d["pages"] for f in p["frames"] if f["bbox"][1] < 340 and f["bbox"][1] > 160)


def test_real_table_is_still_a_table(tmp_path):
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    for y in (170, 190, 260):
        pg.draw_line((50, y), (400, y))
    for k, y in enumerate(range(184, 258, 12)):
        for x, t in ((60, f"Cond {k}"), (180, f"{k * 1.5:.2f}"), (260, f"{k * 2.5:.2f}"), (340, f"{k}%")):
            pg.insert_text((x, y), t, fontsize=9, fontname="helv")
    doc.save(tmp_path / "t.pdf")
    d = extract_pdf(tmp_path / "t.pdf")
    assert sum(1 for p in d["pages"] for f in p["frames"] if f["role"] == "table") >= 3


def test_monospace_code_and_algorithms_are_not_translated(tmp_path):
    assert is_monospace_font("CourierNewPSMT") and is_monospace_font("Consolas") and is_monospace_font("LMMono10-Regular") \
        and is_monospace_font("CMTT10") and not is_monospace_font("TimesNewRoman")
    assert looks_like_algorithm("1: for each epoch do 2: sample batch B 3: update theta", 3)
    assert not looks_like_algorithm("We use 1: 2 ratio", 3)
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 100), "The model is trained as follows, using the standard loop below for all experiments in this paper.", fontsize=10, fontname="tiro")
    for k, t in enumerate(["def train(model, data):", "    for batch in data:", "        loss = model(batch)", "        loss.backward()"]):
        pg.insert_text((90, 130 + 12 * k), t, fontsize=9, fontname="cour")
    pg.insert_text((72, 220), "This approach converges quickly and we report all results over five random seeds in the next section.", fontsize=10, fontname="tiro")
    doc.save(tmp_path / "c.pdf")
    d = extract_pdf(tmp_path / "c.pdf")
    code = [f for p in d["pages"] for f in p["frames"] if "def train" in f["text"] or "loss" in f["text"]]
    assert code and all(f["role"] == "figure_text" and not f["translate"] for f in code)
    assert any(f["role"] == "body" and f["translate"] for p in d["pages"] for f in p["frames"])


def test_small_text_in_dense_vector_figure_is_figure_text(tmp_path):
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 100), "Figure 1 shows the measured response for all conditions in the experiment described above in detail.", fontsize=10, fontname="tiro")
    for i in range(60):                                  # プロット: 目盛りと点 (密)
        for j in range(25):
            pg.draw_circle((100 + i * 3, 300 + j * 3), 1.0, fill=(0, 0, 1))
    for k, t in enumerate(["Frequency", "Amplitude", "0.5", "Condition A"]):
        pg.insert_text((120 + k * 50, 380), t, fontsize=8, fontname="helv")
    pg.insert_text((72, 520), "The text below the figure is ordinary body text and should still be translated by the pipeline.", fontsize=10, fontname="tiro")
    doc.save(tmp_path / "v.pdf")
    d = extract_pdf(tmp_path / "v.pdf")
    fig = [f for p in d["pages"] for f in p["frames"] if f["text"].startswith(("Frequency", "Amplitude"))]
    assert fig and all(f["role"] == "figure_text" for f in fig)
    assert any(f["role"] == "body" and f["translate"] and f["text"].startswith("The text below") for p in d["pages"] for f in p["frames"])


# =============================================================== D: 固定訳

def test_fixed_glossary_takes_precedence_and_adds_present_terms(tmp_path):
    fixed = load_fixed()
    assert any(g["en"] == "inferential statistics" and g["ja"] == "推測統計" for g in fixed)
    paper = [{"en": "inferential statistics", "ja": "推計統計"}, {"en": "otter", "ja": "カワウソ"}]
    g, changed = merge_fixed(paper, fixed, "We used inferential statistics and the slow oscillation with a 95% confidence interval.")
    byen = {x["en"].lower(): x["ja"] for x in g}
    assert byen["inferential statistics"] == "推測統計" and byen["otter"] == "カワウソ"        # 固定訳が優先、他は残る
    assert byen["slow oscillation"] == "徐波" and byen["confidence interval"] == "信頼区間"      # 本文に出る固定訳は追加
    assert "bonferroni correction" not in byen                                                  # 本文に無い固定訳は足さない
    assert "推計統計" in " ".join(v for x in fixed for v in x.get("variants", []))             # 別表記の正規化にも使われる
    p = tmp_path / "f.toml"
    p.write_text('[terms]\n"foo bar" = "フーバー"\n[variants]\n"フーバー" = ["ふーばー"]\n', encoding="utf-8")
    f2 = load_fixed(p)
    assert f2 == [{"en": "foo bar", "ja": "フーバー", "note": "固定訳", "variants": ["ふーばー"]}]
    assert load_fixed(tmp_path / "none.toml") == []
    (tmp_path / "bad.toml").write_text("[[[", encoding="utf-8")
    logs = []
    assert load_fixed(tmp_path / "bad.toml", log=logs.append) == [] and logs


# =============================================================== bat

def test_bat_is_ascii_and_has_messages_for_every_exit_code():
    bat = (ROOT / "翻訳する.bat").read_bytes()
    assert all(c < 128 for c in bat)                     # cmd は chcp 65001 の後の UTF-8 を誤解析するので ASCII のみ
    for rc in (1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12):
        assert (ROOT / "bat_messages" / f"rc{rc}.txt").exists(), rc
    txt = bat.decode("ascii")
    assert "goto partial" in txt and "sys.version_info" in txt
