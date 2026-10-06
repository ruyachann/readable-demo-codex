"""M14: 数値の換算・用語集の検査・数式の記号・英語併記・描画後の重なり・assist (用語集の点検・問題段落の補正)。合成データのみ。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import fitz

from readable.assist import Assist
from readable.config import Config
from readable.numcheck import check_numbers


# =============================================================== 数値

def test_unit_rewording_and_shared_multiplier_are_accepted():
    assert not check_numbers("50k training images and 10k testing images", "5万枚の学習画像と1万枚のテスト画像")
    assert not check_numbers("(15.3/19.6 billion FLOPs)", "(153億/196億 FLOPs)")
    assert not check_numbers("1.28 million images, 10-50 thousand", "128万枚、1万〜5万")
    assert check_numbers("50k images", "6万枚")                         # 値が違えば拒否


# =============================================================== 用語集・他言語・数式の文字化け

def test_glossary_entry_problems_are_detected():
    from readable.glossary import entry_problem
    assert entry_problem("excessive daytime sleepiness", "過度の Bethany 日中傾眠")        # 原文に無い英単語 (幻覚)
    assert entry_problem("x", "過度の過度の傾眠")                                         # 重複
    assert entry_problem("method", "metodología")                                        # 他言語
    for en, ja in (("Transformer (big)", "Transformer（ビッグ）"), ("fMRI", "fMRI"), ("sleep spindle", "睡眠紡錘波"),
                   ("top-1 err.", "top-1エラー率")):
        assert entry_problem(en, ja) is None


def test_foreign_language_words_and_math_garbling_are_validation_errors():
    from readable.translate import validate_translation
    assert any("他言語" in p for p in validate_translation("The methodology of the study", "研究のmetodología"))
    assert not validate_translation("The methodology of Göldi", "Göldi の方法論")           # 原文にある語は可
    assert any("数式" in p for p in validate_translation("a {x} b", "a ƒx} b"))


def test_cmex_summation_is_mapped():
    from readable.extract import apply_charmap, merge_charmap
    cm = merge_charmap(None, None)
    assert apply_charmap("P", "CMEX10", cm) == "∑" and apply_charmap("P", "CMMI10", cm) == "P"


# =============================================================== 英語併記

def test_gloss_is_merged_per_whole_term_and_removed_in_captions():
    from readable.render import strip_repeated_gloss
    out = strip_repeated_gloss(["データ(Data)収集(collection)を行った。", "表1 パーソナライズTMR (Personalized TMR)",
                                "TMR (TMR) と TMR (TMR)"], ["Data", "collection", "Personalized TMR", "TMR"], ["body", "caption", "body"])
    assert out[0] == "データ収集 (Data collection)を行った。"
    assert "Personalized" not in out[1]
    assert out[2].count("(TMR)") == 1


def test_links_are_not_drawn_blue_with_underline():
    from readable.config import load_config
    from readable.fonts import build_fonts
    assert "a{color:inherit;text-decoration:none}" in build_fonts(load_config()).css


# =============================================================== 英語のまま + 括弧の併記だけの訳は原文に戻す (併記を足さない)

def test_english_with_added_glosses_is_not_adopted():
    from test_m23 import translator
    from readable.translate import Cache
    src = "We evaluate our method on the ImageNet dataset that consists of one thousand classes and the models are trained."
    units = [{"id": "u0", "role": "body", "page": 1, "frames": ["f0"], "text": src, "vars": {}}]
    glossary = [{"en": "method", "ja": "手法", "note": "", "keep_en": True}]

    def h(n, m, req):
        return [{"id": u["id"], "text": u["text"].replace("method", "method (method)").replace("dataset", "dataset (dataset)")} for u in req["units"]]

    t, sdk, _ = translator(h, refine=False)
    res = t.translate(units, context={"glossary": glossary}, cache=Cache("nul"))
    assert res["u0"] == src                           # 併記だけを足した英語の訳を採用せず、原文のまま (括弧を足さない)


# =============================================================== 描画後の重なりの検査

def test_drawn_overlaps_are_detected_and_reported(tmp_path):
    from readable.extract import extract_pdf
    from readable.render import render_ja
    from readable.translate import build_units, frames_translations
    doc = fitz.open()
    pg = doc.new_page(width=300, height=300)
    for k in range(3):
        pg.insert_text((40, 60 + k * 11.5), "The quick brown fox jumps over the lazy dog while reading a paper %d." % k, fontsize=9.5, fontname="tiro")
    pg.draw_rect(fitz.Rect(40, 120, 260, 220), fill=(0.8, 0.8, 1))
    for k in range(4):
        pg.insert_text((50, 140 + k * 14), "Figure label number %d" % k, fontsize=9, fontname="tiro")          # 図の中の文字 (翻訳しない)
    src = tmp_path / "s.pdf"
    doc.save(src)
    d = extract_pdf(src)
    units = build_units(d)
    long_ja = "これは非常に長い訳文であり、下の図の領域まではみ出して描かれてしまうほどの長さがある。" * 8
    rep = render_ja(src, d, frames_translations(d, units, {u["id"]: long_ja for u in units}), tmp_path / "o.pdf")
    assert "overlaps" in rep and isinstance(rep["overlaps"], list)
    ok = render_ja(src, d, frames_translations(d, units, {u["id"]: "短い訳。" for u in units}), tmp_path / "o2.pdf")
    assert ok["overlaps"] == []


# =============================================================== assist (用語集の点検・問題段落の補正)

class FakeRun:
    """claude -p の子プロセスの代わり。payload に応じた出力を JSON で返す。"""

    def __init__(self, fn):
        self.fn = fn
        self.calls = []

    def __call__(self, cmd, input=None, **kw):
        self.calls.append(json.loads(input))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"structured_output": self.fn(json.loads(input))}), stderr="")


def _assist(tmp_path, fn, max_calls=4, **kw):
    tmp_path.mkdir(parents=True, exist_ok=True)
    run = FakeRun(fn)
    a = Assist(Config({"assist": {"max_calls_per_doc": max_calls}}), tmp_path, runner=run, used_by_structure=1, **kw)
    return a, run


GL = [{"en": "excessive daytime sleepiness", "ja": "過度の日中傾眠"}, {"en": "layer", "ja": "レイヤー"}, {"en": "BLEU", "ja": "BLEU"}]


def test_assist_glossary_review_adopts_only_clean_fixes(tmp_path):
    def fn(p):
        return {"fixes": [{"en": "layer", "ja": "層", "reason": "表記を統一"},
                          {"en": "excessive daytime sleepiness", "ja": "過度の Bethany 日中傾眠", "reason": "?"},        # 幻覚: 却下
                          {"en": "not in glossary", "ja": "無い"}], "remove": []}
    a, run = _assist(tmp_path / "a", fn)
    out = a.review_glossary(GL, "T", "A")
    assert {g["en"]: g["ja"] for g in out}["layer"] == "層" and out[0]["ja"] == "過度の日中傾眠"
    assert len(a.info["glossary"]["adopted"]) == 1 and len(a.info["glossary"]["rejected"]) == 1 and a.info["calls"] == 1
    assert len(run.calls) == 1 and "abstract" in run.calls[0] and "glossary" in run.calls[0]          # 本文は送らない (題名・要旨・用語集だけ)


def test_assist_correction_adopts_only_validated_units_and_respects_the_budget(tmp_path):
    from readable.translate import check_translation
    units = [{"id": "u1", "role": "body", "page": 1, "text": "We train the network with 50k images [3] {v1}.", "vars": {}},
             {"id": "u2", "role": "body", "page": 1, "text": "The error is 3.5 percent on the test set of the benchmark.", "vars": {}}]

    def fn(p):
        return {"units": [{"id": "u1", "text": "5万枚の画像でネットワークを学習した [3] {v1}。"},
                          {"id": "u2", "text": "テスト集合での誤差は 9.9 パーセントである。"}]}        # u2 は数値が違う: 却下

    a, run = _assist(tmp_path / "b", fn)
    got = a.correct_units([{"unit": u, "current": None, "problems": ["x"]} for u in units], [], "T", "A",
                          lambda u, ja: check_translation(u["text"], ja))
    assert list(got) == ["u1"] and a.info["correction"]["adopted"] == 1
    assert {r["id"] for r in a.info["correction"]["rejected"]} == {"u2"}
    # M15 retries rejected units once, sending the previous validation failure.
    assert a.info["correction"]["calls"] == len(run.calls) == 2
    assert [u["id"] for u in run.calls[1]["units"]] == ["u2"]
    assert "前回の補正が検証に落ちた" in run.calls[1]["units"][0]["problems"][0]
    # 予算: 構造解析 1 + 用語集 1 + 補正 2 = 4 まで。これを超える呼び出しは送らない
    a2, run2 = _assist(tmp_path / "c", fn, max_calls=2)
    a2.review_glossary(GL, "T", "A")
    a2.correct_units([{"unit": units[0], "current": None, "problems": []}], [], "T", "A", lambda u, ja: [])
    assert len(run2.calls) == 1                                   # 構造解析 1 + 用語集 1 = 2 で上限。補正は送らない


def test_assist_results_are_cached_and_skipped_for_api_env(tmp_path, monkeypatch):
    a, run = _assist(tmp_path / "d", lambda p: {"fixes": [], "remove": []})
    a.review_glossary(GL, "T", "A")
    b, run_b = _assist(tmp_path / "d", lambda p: {"fixes": [], "remove": []})
    b.review_glossary(GL, "T", "A")
    assert len(run.calls) == 1 and len(run_b.calls) == 0         # 2 回目はキャッシュ (呼び出しなし)
    # CR-09: API キー/ゲートウェイの環境変数があれば、claude を呼ばずにスキップ (構造解析と同じ規則)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    (tmp_path / "e").mkdir()
    c = Assist(Config({}), tmp_path / "e", used_by_structure=0)
    assert c.review_glossary(GL, "T", "A") == GL and not c.enabled and c.info["skipped"]


def test_no_assist_flag_and_unsupported_provider(tmp_path):
    from readable.cli import build_parser
    assert build_parser().parse_args(["x.pdf", "--no-assist"]).no_assist is True
    a = Assist(Config({}), tmp_path, provider="unregistered")   # 未登録 provider は安全にスキップする
    assert not a.enabled and "未実装" in a.info["skipped"]
    assert a.review_glossary(GL, "T", "A") == GL
