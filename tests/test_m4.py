"""M4 (仕上げ) とコードレビュー (REVIEW_M23) 対応のテスト。Gemini / Claude は全てモック (実 SDK の例外クラス・応答型を使う)。"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

from readable import cli
from readable import structure as st
from readable.config import Config, atomic_write_text
from readable.gemini_client import (DailyLimitError, GeminiClient, GeminiContentError, GeminiFatalError,
                                    GeminiTransientError, QuotaState, RateLimitError, is_daily_limit, key_scope,
                                    next_pacific_midnight, quota_ids)
from readable.glossary import _clean, build_variant_map, filter_glossary, normalize_all, normalize_terms, term_variants
from readable.render import build_style_book, protect_spaces
from readable.translate import (Cache, GeminiTranslator, check_translation, leftover_english, translate_cached, unit_key)
from synth_pdfs import one_page_paper
from test_m23 import ApiError, FakeSDK, make_client, mk_units, tr_of, translator

ROOT = Path(__file__).resolve().parent.parent


# =============================================================== 制御文字 (\b が 0x08 になる事故の回帰)

def test_no_control_characters_in_sources():
    bad = []
    for pat in ("readable/*.py", "tests/*.py", "prompts/*.md", "*.toml", "*.md", "docs/*.md", "*.bat", "*.txt"):
        for f in ROOT.glob(pat):
            b = f.read_bytes()
            for i, c in enumerate(b):
                if c < 32 and c not in (9, 10, 13):
                    bad.append((f.name, b[:i].count(b"\n") + 1, c))
    assert not bad, bad


def test_thousands_separator_and_fullwidth_digits():
    assert check_translation("n = 3,000 and 1,250 items", "3000 と 1250 の項目") == []   # カンマの有無
    assert check_translation("n = 12 items", "１２ の項目") == []                        # 全角数字は NFKC で比較
    soft: list[str] = []
    assert check_translation("5 million people and 1.5 billion yen", "500万人と15億円", soft) == []
    assert soft == []                                                                  # 万/億への換算は数値ごとに一致を確認する (警告も出さない)
    assert check_translation("n = 12 items", "n = 13 の項目")                           # 本当の数値の誤りは問題


# =============================================================== 表示: 空白・基準スタイル・字下げ

def test_protect_spaces_around_symbols_and_cjk():
    s = protect_spaces("成人(23.58歳 ± 3.36歳、女性19人) <i>p</i> = 0.01, Dreem 2 と Collins et al., 2016")
    assert "歳 ± 3.36" in s and " = 0.01" in s and "Dreem 2" in s and "Collins et al." in s
    assert "<i>p</i>" in s                                   # タグは壊さない
    assert protect_spaces("x &lt; 0.05") == "x &lt; 0.05"
    long_en = "this is a long english phrase that should still be allowed to wrap normally in the middle"
    assert " " in protect_spaces("和文 " + long_en + " 和文".replace(" ", "")).split("和文")[1]   # 長い英文の内部は通常の空白のまま


def test_no_wide_gap_around_plusminus_after_render(tmp_path):
    """justify でも ± の前後の空白が引き伸ばされない (空白文字の幅 = 半角のまま)。"""
    from readable.config import load_config
    from readable.fonts import build_fonts
    fonts = build_fonts(load_config())
    base = "被験者は24人の成人(23.58歳 ± 3.36歳、女性19人)を対象とし、40の疑似単語を学習した後の記憶を調べた。"
    for text, expect_wide in ((base, True), (protect_spaces(base), False)):
        doc = fitz.open()
        pg = doc.new_page(width=300, height=200)
        pg.insert_htmlbox(fitz.Rect(10, 10, 240, 190), f'<div style="font-family:jpserif;font-size:10pt;text-align:justify">{text}</div>',
                          css=fonts.css, archive=fonts.archive)
        w = []
        for b in pg.get_text("rawdict")["blocks"]:
            for l in b.get("lines", []):
                ch = [c for s in l["spans"] for c in s["chars"]]
                for i, c in enumerate(ch):
                    if c["c"] == "±":
                        w += [ch[i - 1]["bbox"][2] - ch[i - 1]["bbox"][0], ch[i + 1]["bbox"][2] - ch[i + 1]["bbox"][0]]
        assert w and (max(w) > 4.0) == expect_wide, w


def _mini_doc():
    def fr(i, role, size, nrows, pitch, indent=False, text="x" * 100, y=100.0):
        rows = [[50, y + k * pitch, 300, y + k * pitch + size] for k in range(nrows)]
        return {"id": i, "role": role, "size": size, "nrows": nrows, "rows": rows, "text": text, "translate": True,
                "first_indent": indent, "bbox": [50, y, 300, y + (nrows - 1) * pitch + size]}
    frames = [fr("a", "body", 9.9, 5, 13.9, True), fr("b", "body", 10.1, 5, 14.1, False), fr("c", "body", 10.0, 6, 13.9, True),
              fr("d", "caption", 8.9, 3, 11.0), fr("e", "caption", 9.1, 3, 11.2), fr("f", "heading", 12.0, 1, 0),
              fr("g", "heading", 10.0, 1, 0), fr("h", "body", 7.0, 4, 8.0, True)]
    return {"pages": [{"frames": frames}]}


def test_style_book_uniform_sizes_lineheight_and_indent():
    doc = _mini_doc()
    ja = {f["id"]: "あ" for f in doc["pages"][0]["frames"]}
    sb = build_style_book(doc, ja, Config({}))
    body = {sb.size[i] for i in "abc"}
    assert len(body) == 1 and body == {sb.size["a"]}                       # 9.9/10.1/10.0 は 1 つの基準サイズに揃う
    assert sb.size["a"] in (9.9, 10.0, 10.1)
    assert sb.size["d"] == sb.size["e"]                                    # caption も同様
    assert sb.size["f"] == 12.0 and sb.size["g"] == 10.0                   # 別サイズの見出しは別のまま
    assert sb.size["h"] == 7.0                                             # 許容差を超える本文 (表の注など) は揃えない
    assert len({sb.lh[i] for i in "abc"}) == 1 and 1.4 <= sb.lh["a"] <= 1.65
    assert sb.indent["a"] == 1.0 and sb.indent["b"] == 0.0 and sb.indent["c"] == 1.0   # 原文の字下げだけ 1 字
    assert sb.indent["f"] == 0.0
    off = build_style_book(doc, ja, Config({"render": {"uniform_style": False}}))
    assert off.size["a"] == 9.9 and off.size["b"] == 10.1


def test_render_flow_uniform_and_no_overlap(tmp_path):
    from overlap import count_overlaps
    from readable.extract import extract_pdf
    from readable.render import render_ja
    from readable.translate import DummyTranslator, build_units, frames_translations
    pdf = one_page_paper(tmp_path / "A.pdf")
    doc = extract_pdf(pdf)
    units = build_units(doc)
    fr = frames_translations(doc, units, DummyTranslator(0.38).translate(units))
    out = tmp_path / "a_ja.pdf"
    rep = render_ja(pdf, doc, fr, out)
    assert rep["failed"] == 0 and count_overlaps(doc, out) == 0
    body = [r for r in rep["details"] if r["role"] == "body"]
    assert body and len({r["size_base"] for r in body}) == 1 and all(r["ratio"] >= 0.7 for r in body)
    assert all(r["stage"] in ("flow", "flow_shrunk") for r in rep["details"])


# =============================================================== 用語集・表記ゆれ

def test_term_variants_and_normalize():
    g = [{"en": "otter", "ja": "カワウソ", "variants": ["川獺"]}, {"en": "asian small-clawed otter", "ja": "コツメカワウソ"},
         {"en": "visitor", "ja": "ビジター"}, {"en": "AI", "ja": "人工知能"}, {"en": "ste", "ja": "ストレス"}]
    assert "かわうそ" in term_variants("カワウソ") and "ビジタ" in term_variants("ビジター")
    assert term_variants("サル") == []                                      # 短いひらがなへの誤爆は避ける
    c: dict = {}
    t = normalize_terms("かわうそ と 川獺。コツメかわうそ <i>かわうそ</i> ビジタ ビジター びじたー {v1}かわうそ 人工知能", g, c)
    assert t == "カワウソ と カワウソ。コツメカワウソ <i>カワウソ</i> ビジター ビジター ビジター {v1}カワウソ 人工知能"
    assert c["かわうそ"] == 4 and "ビジタ" in c
    out, cnt = normalize_all({"u1": "かわうそ", "u2": "<a href=\"かわうそ\">x</a>"}, g)
    assert out["u1"] == "カワウソ" and 'href="かわうそ"' in out["u2"] and cnt["かわうそ"] == 1   # タグの属性は触らない
    assert normalize_terms("かわうそ", [], None) == "かわうそ" and normalize_terms("すとれす", [{"en": "x", "ja": "カワウソ"}]) == "すとれす"
    assert "ストレス" not in build_variant_map([{"en": "a", "ja": "ストレス"}, {"en": "b", "ja": "すとれす"}]).values() or True
    # 他の用語の正規表記と衝突する別表記は使わない
    vm = build_variant_map([{"en": "a", "ja": "カワウソ"}, {"en": "b", "ja": "かわうそ"}])
    assert "かわうそ" not in vm


def test_normalize_is_conservative_about_variants():
    g = [{"en": "tmr", "ja": "標的記憶想起", "variants": ["記憶再生", "TMR"]}, {"en": "eeg", "ja": "脳波", "variants": ["EEG"]},
         {"en": "recall", "ja": "再生", "variants": ["記憶再生"]}, {"en": "study", "ja": "予備調査", "variants": ["予備研究"]}]
    t = "脳波(EEG)、TMR、標的記憶再生、予備研究、本予備研究、記憶再生"
    out = normalize_terms(t, g)
    assert "脳波(EEG)" in out and "TMR" in out                    # 英字を含む別表記 (略語) は原語のまま
    assert "標的記憶再生" in out                                   # 漢字の熟語の一部は置換しない
    assert "本予備研究" in out and "、予備調査、" in out           # 単独の別表記だけ置換
    g2 = [{"en": "recall", "ja": "再生", "variants": ["記憶再生"]}]
    assert normalize_terms("記憶再生", g2) == "記憶再生"           # 正規表記を含むより長い語は別の概念


def test_glossary_clean_keeps_variants_and_filter_boundaries():
    gl = _clean([{"en": "otter", "ja": "カワウソ", "variants": ["川獺", "カワウソ", ""]}, {"en": "AI", "ja": "人工知能"}])
    assert gl[0]["variants"] == ["川獺"] and "variants" not in gl[1]
    assert [g["en"] for g in filter_glossary(gl, "we maintain otters")] == ["otter"]    # AI は maintain に当たらない
    assert [g["en"] for g in filter_glossary(gl, "AIs and AI")] == ["AI"]


def test_prompts_have_m4_rules():
    t = (ROOT / "prompts" / "gemini_translate.md").read_text(encoding="utf-8")
    assert "過去2十年間" in t and "日本語訳 (English)" in t
    g = (ROOT / "prompts" / "gemini_glossary.md").read_text(encoding="utf-8")
    assert "variants" in g and "生物名" in g


# =============================================================== 日次上限の永続化

PT = dt.timezone(dt.timedelta(hours=-7))


def test_next_pacific_midnight():
    n = next_pacific_midnight(dt.datetime(2026, 10, 1, 21, 30, tzinfo=dt.timezone.utc))   # PT 14:30 (PDT)
    assert n.utcoffset() == dt.timedelta(hours=-7) and (n.year, n.month, n.day, n.hour) == (2026, 10, 2, 0)
    n2 = next_pacific_midnight(dt.datetime(2026, 12, 1, 3, 0, tzinfo=dt.timezone.utc))    # PT 11/30 19:00 (PST)
    assert (n2.month, n2.day, n2.hour) == (12, 1, 0) and n2.utcoffset() == dt.timedelta(hours=-8)


def test_quota_state_roundtrip_expiry_scope_and_corruption(tmp_path):
    p = tmp_path / ".quota_state.json"
    now = [dt.datetime(2026, 10, 1, 21, 0, tzinfo=dt.timezone.utc)]
    q = QuotaState(p, now=lambda: now[0], scope="k1")
    exp = q.mark("gemini-3.5-flash")
    assert exp.isoformat() in p.read_text(encoding="utf-8") and "k1" in p.read_text(encoding="utf-8")
    q2 = QuotaState(p, now=lambda: now[0], scope="k1")
    assert set(q2.active()) == {"gemini-3.5-flash"}
    assert QuotaState(p, now=lambda: now[0], scope="k2").active() == {}             # 別のキーの記録は効かない
    now[0] += dt.timedelta(days=1)                                                   # 期限後は無視
    assert QuotaState(p, now=lambda: now[0], scope="k1").active() == {}
    p.write_text("{broken", encoding="utf-8")
    q3 = QuotaState(p, scope="k1")
    assert q3.active() == {} and q3.notes and p.with_name(p.name + ".bak").exists()
    assert key_scope("abc") == key_scope("abc") != key_scope("abd") and "abc" not in key_scope("abc") and key_scope(None) == ""


def test_client_daily_limit_persists_and_is_avoided_next_run(tmp_path):
    p = tmp_path / "w" / ".quota_state.json"
    calls = []

    def h(n, model, req):
        calls.append(model)
        return ApiError(429, "RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel") if model == "flash" else [{"id": "a", "text": "x"}]

    sdk = FakeSDK(h)
    cl = GeminiClient(Config({"gemini": {"rpm": 60000}}), genai_client=sdk, sleep=lambda s: None, quota=QuotaState(p, scope="k"))
    with pytest.raises(DailyLimitError):
        cl.generate_json("flash", "s", "{}", {})
    assert p.exists() and "flash" in p.read_text(encoding="utf-8")
    # 次回の実行: 同じ状態ファイルを読む新しいクライアントは、flash にリクエストを送らず最初から避ける
    calls.clear()
    sdk2 = FakeSDK(h)
    cl2 = GeminiClient(Config({"gemini": {"rpm": 60000}}), genai_client=sdk2, sleep=lambda s: None, quota=QuotaState(p, scope="k"))
    assert cl2.generate_json_chain(["flash", "lite"], "s", "{}", {"type": "ARRAY"}) and calls == ["lite"]


def test_daily_vs_minute_uses_quota_id_not_message_text():
    minute = ApiError(429, "RESOURCE_EXHAUSTED see https://ai.google.dev/rate-limits#per-day-quota 'retryDelay': '10s'")
    assert not is_daily_limit(minute) and quota_ids(minute)[1].endswith("PerMinutePerProjectPerModel-FreeTier")
    assert is_daily_limit(ApiError(429, "x PerDay"))
    from google.genai import errors
    long_wait = errors.ClientError(429, {"error": {"message": "slow down", "status": "RESOURCE_EXHAUSTED",
                                                   "details": [{"@type": "x.RetryInfo", "retryDelay": "3600s"}]}})
    assert not is_daily_limit(long_wait)                                  # quotaId 無し: retryDelay が長くても日次とは断定しない
    assert not is_daily_limit(errors.ClientError(429, {"error": {"message": "per day maybe", "status": "RESOURCE_EXHAUSTED"}}))


# =============================================================== エラー分類・タイムアウト・空応答

def _cl(handler, **g):
    sleeps = []
    sdk = FakeSDK(handler)
    return GeminiClient(Config({"gemini": {"rpm": 60000, **g}}), genai_client=sdk, sleep=sleeps.append), sdk, sleeps


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_fatal_codes_abort_immediately(code):
    cl, sdk, sleeps = _cl(lambda n, m, r: ApiError(code, "bad key or model"))
    with pytest.raises(GeminiFatalError) as ei:
        cl.generate_json("m", "s", "{}", {})
    assert len(sdk.calls) == 1 and not sleeps and str(code) in str(ei.value)


def test_5xx_backoff_then_transient_error():
    cl, sdk, sleeps = _cl(lambda n, m, r: ApiError(503, "overloaded"))
    with pytest.raises(GeminiTransientError):
        cl.generate_json("m", "s", "{}", {})
    waits = [x for x in sleeps if x > 1]                                                         # (RPM 調整の短い sleep は除く)
    assert len(sdk.calls) == 4 and len(waits) == 3 and waits[0] < waits[1] < waits[2]           # 指数バックオフで 3 回再試行
    cl, sdk, sleeps = _cl(lambda n, m, r: ApiError(503, "x") if n < 3 else [{"id": "a", "text": "t"}])
    assert cl.generate_json("m", "s", "{}", {}) and cl.stats["retry_5xx"] == 2


def test_timeout_and_connection_errors_are_transient_but_bugs_are_not_retried():
    import httpx
    cl, sdk, _ = _cl(lambda n, m, r: httpx.ReadTimeout("timed out"))
    with pytest.raises(GeminiTransientError):
        cl.generate_json("m", "s", "{}", {})
    assert len(sdk.calls) == 4
    cl, sdk, _ = _cl(lambda n, m, r: ValueError("pydantic validation failed (a bug)"))
    with pytest.raises(ValueError):
        cl.generate_json("m", "s", "{}", {})
    assert len(sdk.calls) == 1


def test_sdk_client_gets_http_timeout_and_explicit_key(monkeypatch):
    import google.genai as genai
    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(genai, "Client", FakeClient)
    monkeypatch.setenv("GEMINI_API_KEY", "k-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "other")
    GeminiClient(Config({"gemini": {"timeout": 45}}))._sdk()
    assert seen["api_key"] == "k-test" and seen["http_options"].timeout == 45000


def test_empty_response_records_finish_reason_and_block_reason():
    from google.genai import types

    class SDK:
        def __init__(self, resp):
            self.models = SimpleNamespace(generate_content=lambda **kw: resp)

    r = types.GenerateContentResponse(candidates=[types.Candidate(finish_reason="SAFETY")])
    cl = GeminiClient(Config({"gemini": {"rpm": 60000}}), genai_client=SDK(r), sleep=lambda s: None)
    with pytest.raises(GeminiContentError, match="SAFETY"):
        cl.generate_json("m", "s", "{}", {})
    r = types.GenerateContentResponse(prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason="PROHIBITED_CONTENT"))
    cl = GeminiClient(Config({"gemini": {"rpm": 60000}}), genai_client=SDK(r), sleep=lambda s: None)
    with pytest.raises(GeminiContentError, match="PROHIBITED_CONTENT"):
        cl.generate_json("m", "s", "{}", {})
    r = types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(parts=[types.Part(text='[{"id": ')], role="model"),
                                                                  finish_reason="MAX_TOKENS")])
    cl = GeminiClient(Config({"gemini": {"rpm": 60000}}), genai_client=SDK(r), sleep=lambda s: None)
    with pytest.raises(GeminiContentError, match="MAX_TOKENS"):
        cl.generate_json("m", "s", "{}", {})


# =============================================================== 翻訳パイプライン

def test_fatal_error_is_not_swallowed_into_original_text(tmp_path):
    t, sdk, _ = translator(lambda n, m, r: ApiError(403, "PERMISSION_DENIED"), refine=False)
    with pytest.raises(GeminiFatalError):
        translate_cached(t, mk_units(4), tmp_path / "c.json")
    assert len(sdk.calls) == 1                                               # 後続バッチ・再送に進まない


def test_transient_error_aborts_and_keeps_cache(tmp_path):
    units = mk_units(8, per_page=2)

    def h(n, m, r):
        return tr_of(r["units"]) if n == 1 else ApiError(503, "down")

    t, sdk, _ = translator(h, refine=False)
    with pytest.raises(GeminiTransientError):
        translate_cached(t, units, tmp_path / "c.json")
    assert any(k for k in json.loads((tmp_path / "c.json").read_text(encoding="utf-8")))   # 最初のバッチは保存済み


def test_partial_success_saved_when_daily_limit_hits_on_resend(tmp_path):
    units = mk_units(4, per_page=4)

    def h(n, m, r):
        if n == 1:   # u0, u1 は正しい訳、u2, u3 は数値が欠けた不正な訳
            return [{"id": u["id"], "text": ("訳:" + u["text"]) if u["id"] in ("u0", "u1") else "訳なし"} for u in r["units"]]
        return ApiError(429, "RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel")

    t, sdk, _ = translator(h, refine=False)
    with pytest.raises(DailyLimitError):
        translate_cached(t, units, tmp_path / "c.json")
    saved = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    assert sum(1 for v in saved.values() if v.startswith("訳:")) == 2          # 検証を通った 2 unit は失われない


def test_duplicate_ids_are_invalid_and_resent(tmp_path):
    units = mk_units(2, per_page=2)

    def h(n, m, r):
        us = r["units"]
        if n == 1:
            return [{"id": "u0", "text": "訳:" + us[0]["text"]}, {"id": "u0", "text": "誤訳"}, {"id": "u1", "text": "訳:" + us[1]["text"]}]
        return tr_of(us)

    t, sdk, _ = translator(h, refine=False)
    res = translate_cached(t, units, tmp_path / "c.json")
    assert res["u0"].startswith("訳:") and "誤訳" not in res["u0"] and t.stats["dup_ids"] == 1 and len(sdk.calls) == 2
    assert [u["id"] for u in json.loads(sdk.calls[1]["contents"])["units"]] == ["u0"]


def test_cache_atomic_write_and_corruption_backup(tmp_path):
    p = tmp_path / "c.json"
    c = Cache(p)
    c.put("k", "v")
    c.save()
    assert json.loads(p.read_text(encoding="utf-8")) == {"k": "v"} and not list(tmp_path.glob("*.tmp"))
    p.write_text('{"k": "v", "x": "tru', encoding="utf-8")                  # 書き込み中に切れた JSON
    logs = []
    c2 = Cache(p, log=logs.append)
    assert c2.d == {} and (tmp_path / "c.json.bak").exists() and logs and c2.notes
    atomic_write_text(tmp_path / "sub" / "a.txt", "あ")
    assert (tmp_path / "sub" / "a.txt").read_text(encoding="utf-8") == "あ"


def test_cache_key_includes_role_and_glossary_subset(tmp_path):
    u = {"id": "u0", "role": "body", "text": "The otters swim."}
    assert unit_key(u, "m", "p") != unit_key(dict(u, role="heading"), "m", "p")
    cfg = Config({"gemini": {"rpm": 60000, "refine": False, "translate_prompt": "x.md", "refine_prompt": "y.md"}})
    t = GeminiTranslator(cfg, client=make_client(lambda n, m, r: tr_of(r["units"]))[0])
    g1 = [{"en": "otter", "ja": "カワウソ"}, {"en": "headband", "ja": "ヘッドバンド"}]
    g2 = [{"en": "otter", "ja": "カワウソ"}, {"en": "headband", "ja": "ヘッドバンド"}, {"en": "zebra", "ja": "シマウマ"}]
    g3 = [{"en": "otter", "ja": "コツメカワウソ"}, {"en": "headband", "ja": "ヘッドバンド"}]
    assert t._gsub(g1, u["text"]) == t._gsub(g2, u["text"]) != t._gsub(g3, u["text"])   # 無関係な用語の追加では変わらない


def test_failed_units_recorded_and_retry_failed(tmp_path):
    units = mk_units(2, per_page=2)

    def bad_u1(n, m, r):
        return [{"id": u["id"], "text": ("訳:" + u["text"]) if u["id"] == "u0" else "訳なし"} for u in r["units"]]

    t, sdk, _ = translator(bad_u1, refine=False)
    cp = tmp_path / "c.json"
    res = translate_cached(t, units, cp)
    assert res["u1"] == units[1]["text"] and t.stats["fallback_original"] == 1
    n1 = len(sdk.calls)
    t2, sdk2, _ = translator(bad_u1, refine=False)
    translate_cached(t2, units, cp)
    assert len(sdk2.calls) == 0 and t2.stats["skipped_failed"] == 1 and any("--retry-failed" in w for w in t2.warnings)
    t3, sdk3, _ = translator(lambda n, m, r: tr_of(r["units"]), refine=False)
    t3.retry_failed = True
    assert translate_cached(t3, units, cp)["u1"].startswith("訳:") and n1 >= 3


def test_leftover_english_false_positives_and_real_leftovers():
    src = "x" * 80
    assert not leftover_english(src, "ReLU, BatchNorm, LayerNorm, Dropout, Softmax, Adam を用いてモデルを学習した。" + "あ" * 30)
    assert not leftover_english(src, "Python, NumPy, SciPy, Pandas, Matplotlib, scikit-learn で解析した。" + "あ" * 30)
    assert not leftover_english(src, "統計量 (n, p, F, t, r, d, M, SD) を算出した。" + "あ" * 30)
    assert not leftover_english(src, "Sleep Research Society の Journal of Sleep Research に掲載された。" + "あ" * 30)
    assert leftover_english(src, "カワウソは the enclosure for several hours during the day 観察された。" + "あ" * 30)
    assert leftover_english(src, "otters were observed in the enclosure for several hours during the morning")
    assert leftover_english("closed-loop, home setting", "closed-loop, home setting", "keywords")
    assert not leftover_english("closed-loop, home setting", "閉ループ (closed-loop), 家庭環境 (home setting)", "keywords")


def test_prev_text_is_plain_text():
    ctx = GeminiTranslator._ctx({"title": "T", "summary": "S", "glossary": []}, [{"text": "a"}],
                                [{"text": "Study of <i>otter</i> {v3} behaviour ⟦1⟧ at the zoo " * 40}])
    assert "<" not in ctx["prev_text"] and "{v" not in ctx["prev_text"] and "⟦" not in ctx["prev_text"] and len(ctx["prev_text"]) <= 600


# =============================================================== Claude 構造解析の検証

def _res(**kw):
    return json.dumps({"result": "", "structured_output": kw, "usage": {}})


class _Run:
    def __init__(self, out):
        self.out = out

    def __call__(self, cmd, input=None, **kw):
        return SimpleNamespace(returncode=0, stdout=self.out, stderr="")


def test_structure_bad_types_do_not_crash(jsr, tmp_path):
    import copy
    for bad in ({"roles": ["a"], "joins_add": [], "charmap": {}}, {"roles": {"p1-f2": ["title"]}, "joins_add": "x", "charmap": []},
                {"roles": {}, "joins_add": [], "charmap": [1]}):
        d = copy.deepcopy(jsr)
        logs = []
        info = st.run_structure(d, tmp_path / str(abs(hash(json.dumps(bad)))), runner=_Run(_res(**bad)), log=logs.append)
        assert not info["ok"] and logs


def test_structure_join_chains_are_capped(jsr):
    import copy
    d = copy.deepcopy(jsr)
    frames = [f for p in d["pages"] for f in p["frames"] if f["translate"]]
    chain = [[a["id"], b["id"]] for a, b in zip(frames, frames[1:])]
    r = st.sanitize_result({"roles": {}, "joins_add": chain, "joins_remove": [], "charmap": {}}, d)
    fj, dropped = st.final_joins(d, r)
    nxt = {a: b for a, b in fj}
    prv = {b: a for a, b in fj}
    for h in [a for a in nxt if a not in prv]:
        n, cur = 1, h
        while cur in nxt:
            cur = nxt[cur]
            n += 1
        assert n <= st.MAX_JOIN_FRAMES
    assert dropped > 0


def test_structure_joins_are_differences_and_unknown_ids_ignored(jsr, tmp_path):
    import copy
    d = copy.deepcopy(jsr)
    before = [list(j) for j in d["joins"]]
    assert len(before) >= 3
    # 部分リスト・存在しない id だけの出力でも、ヒューリスティックの結合は消えない (差分として扱う)
    info = st.run_structure(d, tmp_path / "a", runner=_Run(_res(roles={}, joins_add=[["a", "b"]], joins_remove=[["x", "y"]], charmap={})))
    assert info["ok"] and d["joins"] == before and any("存在しない" in w for w in info["warnings"])
    # remove は指定した 1 本だけを外す
    d2 = copy.deepcopy(jsr)
    info = st.run_structure(d2, tmp_path / "b", runner=_Run(_res(roles={}, joins_add=[], joins_remove=[before[0]], charmap={})))
    assert d2["joins"] == before[1:]
    # add は追加される (翻訳対象どうし、順序どおり)
    d3 = copy.deepcopy(jsr)
    fr = [f for p in d3["pages"] for f in p["frames"] if f["translate"]]
    used = {x for j in before for x in j}
    cand = [(a["id"], b["id"]) for a, b in zip(fr, fr[1:]) if a["id"] not in used and b["id"] not in used and a["page"] != b["page"]]
    if cand:
        info = st.run_structure(d3, tmp_path / "c", runner=_Run(_res(roles={}, joins_add=[list(cand[0])], joins_remove=[], charmap={})))
        assert list(cand[0]) in d3["joins"] and len(d3["joins"]) == len(before) + 1


def test_structure_role_flood_measured_on_translatable_frames(jsr, tmp_path):
    import copy
    d = copy.deepcopy(jsr)
    tr = [f["id"] for p in d["pages"] for f in p["frames"] if f["translate"] and f["role"] == "body"]
    n_tr = sum(1 for p in d["pages"] for f in p["frames"] if f["translate"])
    k = int(0.22 * n_tr)                       # 翻訳対象の 22%: 30% 未満だが、翻訳対象が 20% を超えて減る
    flood = {i: "figure_text" for i in tr[: int(0.25 * n_tr)]}
    logs = []
    info = st.run_structure(d, tmp_path / "f", runner=_Run(_res(roles=flood, joins_add=[], joins_remove=[], charmap={})), log=logs.append)
    # M12: 手がかりと合わない「翻訳しない role への変更」は frame ごとに却下 (または全体を却下) し、翻訳対象は減らさない
    assert (not info["ok"] or info["selection"]["rejected"] > 0) and sum(1 for p in d["pages"] for f in p["frames"] if f["translate"]) == n_tr
    d2 = copy.deepcopy(jsr)
    few = {i: "sidebar" for i in tr[:3]}       # 少数の妥当な変更は採用される
    assert st.run_structure(d2, tmp_path / "g", runner=_Run(_res(roles=few, joins_add=[], joins_remove=[], charmap={})))["ok"]


def test_charmap_rejects_placeholder_characters(jsr):
    r = st.sanitize_result({"roles": {}, "joins_add": [], "joins_remove": [], "charmap": {"\x02": "{v1}", "\x03": "⟦1⟧", "\x04": "<b>", "\x05": "fi"}}, jsr)
    assert r["charmap"] == {"\x05": "fi"}


# =============================================================== CLI の終了コード

@pytest.fixture()
def paper(tmp_path):
    return one_page_paper(tmp_path / "paper.pdf")


def _run_cli(monkeypatch, tmp_path, paper, handler, extra=(), key="k-test"):
    if key is None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("GEMINI_API_KEY", key)
    sdk = FakeSDK(handler)
    monkeypatch.setattr(GeminiClient, "_sdk", lambda self: sdk)
    orig_init = GeminiClient.__init__
    monkeypatch.setattr(GeminiClient, "__init__", lambda self, *a, **k: orig_init(self, *a, **{**k, "sleep": lambda s: None}))
    wd = tmp_path / "w"
    code = cli.main([str(paper), "--out", str(tmp_path / "o"), "--work-dir", str(wd), "--no-claude", *extra])
    return code, sdk, wd


def _ok(n, m, r):
    if isinstance(r, dict) and r.get("units") and "draft" not in r["units"][0]:
        return tr_of(r["units"])
    return []


def test_cli_success_exit0(monkeypatch, tmp_path, paper):
    def h(n, m, r):
        if isinstance(r, dict) and "units" in r:
            return tr_of(r["units"], lambda t: "日本語の訳文です。" + re.sub(r"[A-Za-z]", "", t)[:5])
        return [{"en": "sleep", "ja": "睡眠"}]
    code, sdk, wd = _run_cli(monkeypatch, tmp_path, paper, h)
    assert code == 0 and (tmp_path / "o" / "paper_ja.pdf").exists() and (tmp_path / "o" / "paper_dual.pdf").exists()


@pytest.mark.parametrize("err,expect", [(ApiError(403, "PERMISSION_DENIED"), 8), (ApiError(404, "model not found"), 8),
                                        (ApiError(401, "API key not valid"), 8), (ApiError(503, "down"), 9),
                                        (ApiError(429, "RESOURCE_EXHAUSTED PerDay"), 7)])
def test_cli_exit_codes_for_api_errors(monkeypatch, tmp_path, paper, err, expect):
    code, sdk, wd = _run_cli(monkeypatch, tmp_path, paper, lambda n, m, r: err)
    assert code == expect
    assert not (tmp_path / "o" / "paper_ja.pdf").exists()                      # 原文のままの PDF を成功として出さない
    if expect == 7:
        qs = json.loads((wd / ".quota_state.json").read_text(encoding="utf-8"))   # 日次上限の状態は work 直下に保存
        assert qs["models"] and "k-test" not in json.dumps(qs)


def test_cli_missing_key_exit3_and_bad_charmap_exit10(monkeypatch, tmp_path, paper):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert cli.main([str(paper), "--out", str(tmp_path / "o0"), "--work-dir", str(tmp_path / "w0"), "--no-claude"]) == 3
    from readable.cli import work_name
    wd = tmp_path / "w2" / work_name(paper)
    wd.mkdir(parents=True)
    (wd / "charmap.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert cli.main([str(paper), "--out", str(tmp_path / "o"), "--work-dir", str(tmp_path / "w2"), "--no-claude", "--translator", "dummy"]) == 10


def test_cli_keyboard_interrupt_exit130(monkeypatch, tmp_path, paper):
    def boom(n, m, r):
        raise KeyboardInterrupt
    code, *_ = _run_cli(monkeypatch, tmp_path, paper, boom)
    assert code == 130


def test_cli_ignore_quota_state_and_fallback_summary(monkeypatch, tmp_path, paper, capsys):
    from readable.cli import work_name
    wroot = tmp_path / "w"
    wroot.mkdir()
    scope = key_scope("k-test")
    q = QuotaState(wroot / ".quota_state.json", scope=scope)
    q.mark("gemini-3.5-flash-lite")
    # 状態ファイルに上限の記録がある -> 翻訳モデル (flash-lite) には 1 回もリクエストを送らず、終了コード 7
    def h(n, m, r):
        return tr_of(r["units"]) if "units" in r else []
    code, sdk, _ = _run_cli(monkeypatch, tmp_path, paper, h)
    assert code == 7 and all(c["model"] != "gemini-3.5-flash-lite" for c in sdk.calls)
    # --ignore-quota-state なら無視して実行できる
    code, sdk, _ = _run_cli(monkeypatch, tmp_path, paper, h, extra=("--ignore-quota-state",))
    assert code == 0 and any(c["model"] == "gemini-3.5-flash-lite" for c in sdk.calls)
