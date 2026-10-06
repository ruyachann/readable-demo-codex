"""M2 (Claude 構造解析) と M3 (Gemini 翻訳パイプライン) のテスト。Claude / Gemini は全てモックする。"""
from __future__ import annotations

import copy
import json
import subprocess
from types import SimpleNamespace

import pytest

from readable import structure as st
from readable.config import Config
from readable.gemini_client import (DailyLimitError, GeminiClient, GeminiError, RateLimitError, file_hash,
                                    fill_placeholders, load_prompt_file, parse_retry_delay)
from readable.glossary import build_glossary, build_glossary_input, filter_glossary
from readable.translate import (Cache, GeminiTranslator, check_translation, make_batches, translate_cached)


# =============================================================== M2

def _result(roles=None, joins_add=None, joins_remove=None, charmap=None, **extra):
    obj = {"result": "", "structured_output": {"roles": roles or {}, "joins_add": joins_add or [], "joins_remove": joins_remove or [],
                                               "charmap": charmap or {}},
           "usage": {"input_tokens": 2, "output_tokens": 50}, "duration_ms": 1000, "total_cost_usd": 0.01, **extra}
    return json.dumps(obj)


class FakeRun:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, cmd, input=None, **kw):
        self.calls.append((cmd, input, kw))
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return SimpleNamespace(returncode=o[0], stdout=o[1], stderr=o[2] if len(o) > 2 else "")


def test_build_input_excludes_reliable_roles(jsr):
    t = st.build_input(jsr)
    ids_ref = [f["id"] for p in jsr["pages"] for f in p["frames"] if f["role"] == "reference"]
    assert ids_ref and not any(i + " [" in t for i in ids_ref)
    assert "heuristic_joins:" in t and "omitted_reliable_frames:" in t
    assert "p1-f2 [title]" in t
    assert len(t) < 25000


def test_build_input_unmapped_context(jsr):
    d = copy.deepcopy(jsr)
    d["unmapped_chars"] = {"AdvPXXXX": {"U+0002": 1}}
    t = st.build_input(d)
    assert "unmapped:" in t and "U+0002" in t and "charmap:" in t


def test_run_structure_applies_and_caches(jsr, tmp_path):
    d = copy.deepcopy(jsr)
    body = next(f["id"] for p in d["pages"] for f in p["frames"] if f["role"] == "body")
    run = FakeRun((0, _result(roles={body: "sidebar", "zzz": "body"}, joins_remove=d["joins"][3:])))
    info = st.run_structure(d, tmp_path, runner=run)
    assert info["ok"] and info["used"] and not info["cached"]
    f = next(f for p in d["pages"] for f in p["frames"] if f["id"] == body)
    assert f["role"] == "sidebar"
    assert len(d["joins"]) <= 3 and info["diff"]["roles_changed"][0]["id"] == body   # joins は差分 (remove) として適用
    assert info["diff"]["joins_removed"]
    cmd = run.calls[0][0]
    for flag in ("--tools", "--strict-mcp-config", "--setting-sources", "--disable-slash-commands",
                 "--no-session-persistence", "--system-prompt", "--json-schema", "--output-format"):
        assert flag in cmd
    assert cmd[cmd.index("--model") + 1] == "sonnet" and "heuristic_joins" in run.calls[0][1]
    assert (tmp_path / "structure.json").exists()
    # 2 回目はキャッシュ (runner 不要)
    d2 = copy.deepcopy(jsr)
    info2 = st.run_structure(d2, tmp_path, runner=FakeRun())
    assert info2["cached"] and info2["ok"]
    assert next(f for p in d2["pages"] for f in p["frames"] if f["id"] == body)["role"] == "sidebar"
    # 入力が変われば再実行
    d3 = copy.deepcopy(jsr)
    d3["pages"][1]["frames"][0]["text"] += " changed"
    run3 = FakeRun((0, _result()))
    assert st.run_structure(d3, tmp_path, runner=run3)["used"]


@pytest.mark.parametrize("outcome", [
    subprocess.TimeoutExpired("claude", 1),
    (1, "", "boom"),
    (0, "not json"),
    (0, json.dumps({"result": "no structured", "usage": {}})),
    (0, json.dumps({"is_error": True, "result": "auth"})),
])
def test_run_structure_failure_continues(jsr, tmp_path, outcome):
    d = copy.deepcopy(jsr)
    before = copy.deepcopy(d["joins"])
    logs = []
    info = st.run_structure(d, tmp_path, runner=FakeRun(outcome, outcome), log=logs.append)
    assert not info["ok"] and logs and "ヒューリスティック" in logs[0]
    assert d["joins"] == before and not (tmp_path / "structure.json").exists()


def test_run_structure_disabled_and_no_cli(jsr, tmp_path, monkeypatch):
    d = copy.deepcopy(jsr)
    assert st.run_structure(d, tmp_path, enabled=False, runner=FakeRun())["reason"] == "disabled"
    monkeypatch.setattr(st, "find_claude", lambda: None)
    logs = []
    info = st.run_structure(d, tmp_path, log=logs.append)
    assert not info["ok"] and "見つかりません" in logs[0]


def test_run_structure_charmap_validated(jsr, tmp_path):
    d = copy.deepcopy(jsr)
    run = FakeRun((0, _result(charmap={"\x02": "ﬁ", "a": "b", "\x06": "toolongvalue"})))
    info = st.run_structure(d, tmp_path, runner=run)
    assert info["result"]["charmap"] == {"\x02": "ﬁ"}
    assert d["charmap"]["*"]["\x02"] == "ﬁ"


def test_sanitize_result_drops_unknown(jsr):
    r = st.sanitize_result({"roles": {"p1-f2": "title", "p1-f3": "bogus", "nope": "body"},
                            "joins_add": [["a", "b"], ["x"], "str"], "joins_remove": [], "charmap": {}}, jsr)
    assert r["roles"] == {"p1-f2": "title"} and r["joins_add"] == [] and r["joins_ignored"] == 3   # 存在しない id の組は無視


# =============================================================== M3: ユーティリティ

def test_check_translation():
    src = "In 2019, 45 of 120 subjects <i>agreed</i> [3, 4] {v1}."
    ok = "2019年に120人中45人が<i>同意</i>した [3,4] {v1}。"
    assert check_translation(src, ok) == []
    assert any("数値" in p for p in check_translation(src, ok.replace("45", "四十五")))
    assert any("引用" in p for p in check_translation(src, ok.replace("[3,4]", "[3]")))
    assert check_translation(src, ok.replace("{v1}", ""))
    assert not check_translation(src, ok.replace("<i>", "").replace("</i>", ""))      # M11: 斜体の欠落は書体だけの情報なので許す
    assert check_translation(src, ok.replace("<i>", "<i><i>"))                       # 開閉が合わなければ拒否
    assert check_translation(src, "")
    assert check_translation("A ⟦1⟧ B", "あ ⟦1⟧ い") == [] and check_translation("A ⟦1⟧ B", "あ い")


def test_make_batches():
    us = [{"id": f"u{i}", "page": 1 + i // 2, "text": "x" * 100} for i in range(10)]
    b = make_batches(us, 3, 10_000)
    assert [len(x) for x in b] == [6, 4] and sum(map(len, b), 0) == 10
    b = make_batches(us, 10, 350)
    assert [len(x) for x in b] == [3, 3, 3, 1]


def test_prompt_loader(tmp_path):
    assert load_prompt_file(tmp_path / "none.md", "FB") == "FB"
    assert load_prompt_file(None, "FB") == "FB"
    p = tmp_path / "p.md"
    p.write_text("# t\n\n## SYSTEM PROMPT\n\n```\nline1 (```) inline\nline2\n```\n\n## other\n```\nx\n```\n", encoding="utf-8")
    assert load_prompt_file(p, "FB") == "line1 (```) inline\nline2"
    p.write_text("plain prompt only", encoding="utf-8")
    assert load_prompt_file(p, "FB") == "plain prompt only"
    assert file_hash(p) != file_hash(tmp_path / "none.md")
    s = fill_placeholders("G:{{GLOSSARY}} C:{{CONTEXT}}", [{"en": "a", "ja": "あ"}], {"title": "T"})
    assert "| a | あ |" in s and "Title: T" in s
    assert fill_placeholders("none", [], {}) == "none"


def test_parse_retry_delay():
    assert parse_retry_delay("... 'retryDelay': '13s' ...") == 13
    assert parse_retry_delay('"retryDelay": "7.5s"') == 7.5
    assert parse_retry_delay("Please retry in 20.2s.") == 20.2
    assert parse_retry_delay("nothing") is None


# =============================================================== M3: フェイク SDK

def ApiError(code, msg):
    """実 SDK の例外 (google.genai.errors.ClientError / ServerError) を作る。429 は msg に応じて QuotaFailure.quotaId
    (PerDay / PerMinute) と RetryInfo.retryDelay を details に入れる (実際の Gemini の応答と同じ形)。"""
    import re as _re
    from google.genai import errors
    status = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND", 429: "RESOURCE_EXHAUSTED",
              503: "UNAVAILABLE"}.get(code, "UNKNOWN")
    details = []
    if code == 429:
        qid = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if "PerDay" in msg else "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaMetric": "m", "quotaId": qid}]})
        m = _re.search(r"retryDelay'?\W+(\d+(?:\.\d+)?)s", msg)
        if m:
            details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{m.group(1)}s"})
    body = {"error": {"code": code, "message": msg, "status": status, "details": details}}
    return (errors.ServerError if code >= 500 else errors.ClientError)(code, body)


class FakeSDK:
    """models.generate_content を差し替える。handlers: [(model, contents) -> str(text) | Exception] を順に使う。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        r = self.handler(len(self.calls), model, json.loads(contents) if contents.startswith("{") else contents)
        if isinstance(r, Exception):
            raise r
        from google.genai import types
        text = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(parts=[types.Part(text=text)], role="model"), finish_reason="STOP")],
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=10, candidates_token_count=5))


def tr_of(units, fn=lambda t: "訳:" + t):
    return [{"id": u["id"], "text": fn(u["text"])} for u in units]


def make_client(handler, rpm=60000, cfg=None):
    sleeps = []
    sdk = FakeSDK(handler)
    cl = GeminiClient(cfg or Config({"gemini": {"rpm": rpm}}), genai_client=sdk, sleep=sleeps.append)
    return cl, sdk, sleeps


def mk_units(n, per_page=2, text="Some text {v1} here [1]."):
    return [{"id": f"u{i}", "role": "body", "page": 1 + i // per_page, "frames": [f"p1-f{i}"], "text": f"{text} #{i}",
             "vars": {}} for i in range(n)]


def translator(handler, refine=True, **g):
    cfg = Config({"gemini": {"rpm": 60000, "refine": refine, "translate_prompt": "nonexistent_t.md", "pages_per_batch": 3, "max_chars": 9000,
                             "workers": 1, "validate_retries": 2,   # 既定 (M9) は 6 ページ・並列・再送 1 回。テストは従来の値に固定する
                            
                             "refine_prompt": "nonexistent_r.md", **g}})
    cl, sdk, sleeps = make_client(handler, cfg=cfg)
    return GeminiTranslator(cfg, client=cl), sdk, sleeps


def handler_ok(n, model, req):
    if "source" in json.dumps(req.get("units", [{}])[0] if isinstance(req, dict) else {}):
        return [{"id": u["id"], "text": u["draft"] + "(校)" if u["id"].endswith("0") else u["draft"],
                 "changed": u["id"].endswith("0")} for u in req["units"]]
    return tr_of(req["units"])


# =============================================================== M3: クライアント

def test_client_models_thinking_and_schema():
    seen = []

    def h(n, model, req):
        seen.append(model)
        return tr_of(req["units"])

    cl, sdk, _ = make_client(h)
    cl.generate_json("gemini-3.5-flash-lite", "sys", json.dumps({"units": [{"id": "a", "text": "t"}]}), {"type": "ARRAY"})
    cfg = sdk.calls[0]["config"]
    assert cfg.response_mime_type == "application/json" and cfg.thinking_config.thinking_level.value.lower() == "minimal"
    cl.generate_json("gemini-2.5-flash", "sys", json.dumps({"units": []}), {"type": "ARRAY"})
    assert sdk.calls[1]["config"].thinking_config.thinking_budget == 0
    assert cl.stats["requests"] == 2 and cl.stats["by_model"]["gemini-2.5-flash"] == 1


def test_client_thinking_fallback():
    def h(n, model, req):
        return ApiError(400, "Thinking level is not supported for this model") if n == 1 else [{"id": "a", "text": "x"}]

    cl, sdk, _ = make_client(h)
    out = cl.generate_json("m", "s", json.dumps({"units": []}), {})
    assert out and sdk.calls[1]["config"].thinking_config is None and cl.stats["thinking_fallback"] == 1
    cl.generate_json("m", "s", json.dumps({"units": []}), {})
    assert sdk.calls[2]["config"].thinking_config is None  # 以後も外したまま


def test_client_429_retry_delay_and_daily():
    def h(n, model, req):
        if n == 1:
            return ApiError(429, "RESOURCE_EXHAUSTED quota per minute. 'retryDelay': '13s'")
        return [{"id": "a", "text": "x"}]

    cl, sdk, sleeps = make_client(h)
    assert cl.generate_json("m", "s", json.dumps({"units": []}), {})
    assert 14.0 in sleeps and cl.stats["retry_429"] == 1 and cl.stats["requests"] == 2

    def h2(n, model, req):
        return ApiError(429, "RESOURCE_EXHAUSTED: Quota exceeded for metric GenerateRequestsPerDayPerProjectPerModel-FreeTier")

    cl, sdk, sleeps = make_client(h2)
    with pytest.raises(DailyLimitError):
        cl.generate_json("m", "s", json.dumps({"units": []}), {})
    assert len(sdk.calls) == 1 and not sleeps

    cl, sdk, sleeps = make_client(lambda n, m, r: ApiError(429, "RESOURCE_EXHAUSTED per minute"))
    with pytest.raises(RateLimitError):
        cl.generate_json("m", "s", json.dumps({"units": []}), {})
    assert len(sdk.calls) == 5  # 初回 + max_retries(4)


def test_client_5xx_retry_and_errors():
    def h(n, model, req):
        return ApiError(503, "unavailable") if n == 1 else [{"id": "a", "text": "x"}]

    cl, sdk, sleeps = make_client(h)
    assert cl.generate_json("m", "s", json.dumps({"units": []}), {}) and cl.stats["retry_5xx"] == 1
    cl, *_ = make_client(lambda n, m, r: ApiError(403, "forbidden"))
    with pytest.raises(GeminiError):
        cl.generate_json("m", "s", json.dumps({"units": []}), {})
    cl, *_ = make_client(lambda n, m, r: "not json")
    with pytest.raises(GeminiError):
        cl.generate_json("m", "s", json.dumps({"units": []}), {})


def test_client_rate_limit_spacing():
    t = [0.0]
    sleeps = []
    sdk = FakeSDK(lambda n, m, r: [])
    cl = GeminiClient(Config({"gemini": {"rpm": 5}}), genai_client=sdk, sleep=lambda s: (sleeps.append(s), t.__setitem__(0, t[0] + s)),
                      clock=lambda: t[0])
    for _ in range(3):
        cl.generate_json("m", "s", json.dumps({"units": []}), {})
    assert sleeps == [12.0, 12.0]


def test_client_requires_key(monkeypatch):
    from readable.gemini_client import GeminiUnavailable
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(GeminiUnavailable):
        GeminiClient(Config({}))._sdk()


# =============================================================== M3: 翻訳パイプライン

def test_translate_batches_refine_and_cache(tmp_path):
    units = mk_units(8, per_page=2)  # 4 ページ -> pages_per_batch=3 で 2 バッチ
    t, sdk, _ = translator(handler_ok)
    cp = tmp_path / "c.json"
    ctx = {"title": "T", "summary": "S", "glossary": [{"en": "Some text", "ja": "テキスト", "note": ""},
                                                       {"en": "absent", "ja": "なし", "note": ""}]}
    res = translate_cached(t, units, cp, context=ctx)
    assert len(sdk.calls) == 4  # 翻訳 2 + 見直し 2
    assert t.stats["batches"] == 2 and t.stats["refine_batches"] == 2
    assert res["u0"].endswith("(校)") and not res["u1"].endswith("(校)") and res["u1"].startswith("訳:")
    first = json.loads(sdk.calls[0]["contents"])
    assert first["context"]["title"] == "T" and first["context"]["prev_text"] == ""
    assert first["context"]["glossary"] == [{"en": "Some text", "ja": "テキスト"}]  # 出現語のみ・en/ja のみ
    assert set(first["units"][0]) == {"id", "role", "text"}
    # 2 バッチ目の prev_text は前バッチ末尾 unit
    models = [c["model"] for c in sdk.calls]
    assert models == ["gemini-3.5-flash-lite"] * 2 + ["gemini-3.5-flash"] * 2
    second = json.loads(sdk.calls[1]["contents"])
    assert "#5" in second["context"]["prev_text"]
    # 再実行はキャッシュのみ
    t2, sdk2, _ = translator(handler_ok)
    assert translate_cached(t2, units, cp, context=ctx) == res and not sdk2.calls
    # refine を OFF にした翻訳器は下書きキャッシュを再利用して見直しなし
    t3, sdk3, _ = translator(handler_ok, refine=False)
    r3 = translate_cached(t3, units, cp, context=ctx)
    assert not sdk3.calls and not r3["u0"].endswith("(校)")


def test_translate_validate_resend_and_promotion(tmp_path):
    units = mk_units(3, per_page=3)
    log = []

    def h(n, model, req):
        out = tr_of(req["units"])
        if n == 1:  # u1 の {v1} を落とし、u2 の数値を落とす
            for o in out:
                if o["id"] == "u1":
                    o["text"] = o["text"].replace("{v1}", "")
                if o["id"] == "u2":
                    o["text"] = o["text"].replace("2", "")
        log.append((n, model, [u["id"] for u in req["units"]]))
        return out

    t, sdk, _ = translator(h, refine=False)
    res = translate_cached(t, units, tmp_path / "c.json")
    assert log[0][2] == ["u0", "u1", "u2"] and log[1][2] == ["u1", "u2"] and len(log) == 2
    assert res["u1"].startswith("訳:") and "{v1}" in res["u1"]
    assert t.stats["validate_resend"] == 2 and t.stats["validate_failed"] == 0
    # u2 の数値 "2" は #2 の 2。再送後は正しい
    assert "2" in res["u2"]


def _bad_u1(refine_fix, from_call=4):
    """u1 は通常の翻訳 (と再送) では壊れた訳を返す。選択的な再翻訳 (4 回目以降のリクエスト) で refine_fix なら直す。"""
    def h(n, model, req):
        out = tr_of(req["units"])
        for o, u in zip(out, req["units"]):
            if u["id"] == "u1":
                o["text"] = ("訳(校):" + u["text"]) if (refine_fix and n >= from_call) else "壊れた <b>訳"
        return out
    return h


def test_translate_persistent_failure_selective_refine_fixes(tmp_path):
    units = mk_units(2, per_page=2)
    t, sdk, _ = translator(_bad_u1(True), refine=False)
    cp = tmp_path / "c.json"
    res = translate_cached(t, units, cp)
    assert res["u1"].startswith("訳(校):") and res["u0"].startswith("訳:")
    models = [c["model"] for c in sdk.calls]
    assert models == ["gemini-3.5-flash-lite"] * 3 + ["gemini-3.5-flash"]  # 再送 2 回 (lite) + 選択的な再翻訳 1 回 (flash)
    assert t.stats["validate_failed"] == 1 and t.stats["selective_fixed"] == 1
    sel = json.loads(sdk.calls[3]["contents"])["units"]
    assert [u["id"] for u in sel] == ["u1"] and "検証に失敗" in sel[0]["note"]
    resend = json.loads(sdk.calls[1]["contents"])["units"]
    assert [u["id"] for u in resend] == ["u1"] and "note" in resend[0] and "note" not in json.loads(sdk.calls[0]["contents"])["units"][0]
    t2, sdk2, _ = translator(_bad_u1(True), refine=False)  # 再実行はキャッシュのみ
    assert translate_cached(t2, units, cp) == res and not sdk2.calls


def test_marker_only_problem_is_accepted(tmp_path):
    units = [{"id": "u0", "role": "body", "page": 1, "frames": ["a", "b"], "text": "First half ⟦1⟧ second half 12.", "vars": {}}]
    t, sdk, _ = translator(lambda n, m, r: [{"id": "u0", "text": "前半と後半 12。"}], refine=False)
    res = translate_cached(t, units, tmp_path / "c.json")
    assert res["u0"] == "前半と後半 12。" and len(sdk.calls) == 1 and t.stats["validate_failed"] == 0
    assert any("⟦n⟧" in w for w in t.warnings)
    # 他の問題 (数値欠落) も一緒にあれば再送対象
    t2, sdk2, _ = translator(lambda n, m, r: [{"id": "u0", "text": "前半と後半。"}], refine=False)
    translate_cached(t2, units, tmp_path / "c2.json")
    assert t2.stats["validate_failed"] == 1


def test_translate_persistent_failure_falls_back_to_source(tmp_path):
    units = mk_units(2, per_page=2)
    t, sdk, _ = translator(_bad_u1(False), refine=False)
    cp = tmp_path / "c.json"
    res = translate_cached(t, units, cp)
    assert res["u1"] == units[1]["text"] and res["u0"].startswith("訳:")
    assert t.stats["validate_failed"] == 1 and any("u1" in w for w in t.warnings) and t.stats["selective_fixed"] == 0
    assert len(sdk.calls) == 4
    # 失敗 unit は失敗として記録され、再実行では再送されない (--retry-failed のときだけ再送)
    t2, sdk2, _ = translator(lambda n, m, r: tr_of(r["units"]), refine=False)
    res2 = translate_cached(t2, units, cp)
    assert len(sdk2.calls) == 0 and res2["u1"] == units[1]["text"] and t2.stats["skipped_failed"] == 1
    t3, sdk3, _ = translator(lambda n, m, r: tr_of(r["units"]), refine=False)
    t3.retry_failed = True
    res3 = translate_cached(t3, units, cp)
    assert len(sdk3.calls) == 1 and json.loads(sdk3.calls[0]["contents"])["units"][0]["id"] == "u1"
    assert res3["u1"].startswith("訳:")


EN = "The otters were observed in the enclosure for several hours during the morning feeding session each day"


def test_leftover_english():
    from readable.translate import leftover_english
    ja_ok = "カワウソは毎日、午前の給餌時間中に数時間、飼育場で観察された (Smith et al., 2020)。EEG と <i>p</i> 値を {v1} で調べた。"
    assert not leftover_english(EN, ja_ok, "body")
    assert leftover_english(EN, "カワウソは the enclosure for several hours during the morning feeding session だった。", "body")
    assert leftover_english(EN, EN, "body")                       # 未訳
    assert leftover_english(EN, EN, "sidebar")                    # M4: 著作権・ライセンスの定型文 (sidebar) の未訳も検出する
    assert leftover_english(EN, EN, "heading")                    # M10: 見出し・題名も、和文が無く英単語 3 語以上なら未訳
    assert not leftover_english("Results", "Results", "heading") and not leftover_english("EEG", "EEG", "heading")
    assert not leftover_english("Comparison on key EEG features", "主要な EEG 特徴量の比較", "heading")
    assert not leftover_english("Short title", "Short title", "body")
    assert leftover_english("closed-loop, home setting, spindle activity, targeted memory reactivation, vocabulary learning",
                            "closed-loop, home setting, spindle activity, targeted memory reactivation, vocabulary learning", "keywords")
    assert not leftover_english(EN, "<i>Lutra lutra</i> と Aonyx cinereus の Asian small-clawed 研究 [3, 4] (Smith 2020)。", "body")


def test_selective_refine_for_leftover_english(tmp_path):
    units = [{"id": "u0", "role": "body", "page": 1, "frames": ["f0"], "text": EN, "vars": {}},
             {"id": "u1", "role": "body", "page": 1, "frames": ["f1"], "text": EN + " too", "vars": {}}]

    def h(n, model, req):
        if "note" in req["units"][0]:
            assert "英語" in req["units"][0]["note"]
            return [{"id": u["id"], "text": "カワウソは飼育場で観察された。"} for u in req["units"]]
        return [{"id": "u0", "text": "カワウソは飼育場で観察された。"}, {"id": "u1", "text": "the enclosure for several hours during the morning feeding"}]

    t, sdk, _ = translator(h, refine=False)
    res = translate_cached(t, units, tmp_path / "c.json")
    assert res["u0"].startswith("カワウソ") and res["u1"].startswith("カワウソ")
    sel = json.loads(sdk.calls[-1]["contents"])["units"]
    assert [u["id"] for u in sel] == ["u1"] and t.stats["selective_fixed"] == 1 and not t.warnings


def test_selective_refine_falls_back_to_flash_lite_on_daily_limit(tmp_path):
    units = mk_units(2, per_page=2)

    def h(n, model, req):
        if n >= 4 and model == "gemini-3.5-flash":
            return ApiError(429, "RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel")
        out = tr_of(req["units"])
        for o, u in zip(out, req["units"]):
            if u["id"] == "u1":
                o["text"] = "訳(lite再):" + u["text"] if n >= 4 else "壊れた <b>訳"
        return out

    t, sdk, _ = translator(h, refine=False)
    res = translate_cached(t, units, tmp_path / "c.json")
    assert res["u1"].startswith("訳(lite再):")
    assert [c["model"] for c in sdk.calls][-2:] == ["gemini-3.5-flash", "gemini-3.5-flash-lite"]
    assert "gemini-3.5-flash" in t.client.exhausted


def test_client_exhausted_model_is_not_requested_again():
    cl, sdk, _ = make_client(lambda n, m, r: ApiError(429, "RESOURCE_EXHAUSTED PerDay"))
    for _ in range(2):
        with pytest.raises(DailyLimitError):
            cl.generate_json("m", "s", json.dumps({"units": []}), {})
    assert len(sdk.calls) == 1
    cl, sdk, _ = make_client(lambda n, m, r: ApiError(429, "RESOURCE_EXHAUSTED PerDay") if m == "big" else [{"id": "a"}])
    assert cl.generate_json_chain(["big", "small"], "s", json.dumps({"units": []}), {}) == [{"id": "a"}]
    assert [c["model"] for c in sdk.calls] == ["big", "small"]
    with pytest.raises(DailyLimitError):
        cl.generate_json_chain(["big"], "s", json.dumps({"units": []}), {})


def test_translate_daily_limit_saves_partial_and_resumes(tmp_path):
    units = mk_units(8, per_page=1)  # 8 ページ -> 3 バッチ (3,3,2)
    cp = tmp_path / "c.json"

    def h(n, model, req):
        if n == 2:
            return ApiError(429, "RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel")
        return tr_of(req["units"])

    t, sdk, _ = translator(h, refine=False)
    with pytest.raises(DailyLimitError):
        translate_cached(t, units, cp)
    saved = json.loads(cp.read_text(encoding="utf-8"))
    assert len(saved) == 3  # 1 バッチ目だけ保存
    t2, sdk2, _ = translator(lambda n, m, r: tr_of(r["units"]), refine=False)
    res = translate_cached(t2, units, cp)
    assert len(sdk2.calls) == 2 and len(res) == 8  # 残り 2 バッチのみ


def test_refine_daily_limit_skips_refine(tmp_path):
    units = mk_units(8, per_page=1)

    def h(n, model, req):
        if model == "gemini-3.5-flash":
            return ApiError(429, "RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel")
        return tr_of(req["units"])

    t, sdk, _ = translator(h)
    cp = tmp_path / "c.json"
    res = translate_cached(t, units, cp)
    assert all(v.startswith("訳:") for v in res.values()) and len(res) == 8
    assert t.stats["refine_skipped"] and any("見直し" in w for w in t.warnings)
    assert sum(1 for c in sdk.calls if c["model"] == "gemini-3.5-flash") == 1  # 1 回で打ち切り
    # 見直しなしの結果は最終版としてキャッシュされない (翌日の再実行で見直される)
    t2, sdk2, _ = translator(handler_ok)
    translate_cached(t2, units, cp)
    assert all(c["model"] == "gemini-3.5-flash" for c in sdk2.calls) and t2.stats["batches"] == 0


def test_refine_rejects_invalid_edits(tmp_path):
    units = mk_units(2, per_page=2)

    def h(n, model, req):
        if "units" in req and "draft" in req["units"][0]:
            return [{"id": u["id"], "text": "壊した訳 (タグ{v1}欠落)".replace("{v1}", ""), "changed": True} for u in req["units"]]
        return tr_of(req["units"])

    t, sdk, _ = translator(h)
    res = translate_cached(t, units, tmp_path / "c.json")
    assert all(v.startswith("訳:") for v in res.values()) and t.stats["refine_rejected"] == 2


def test_translate_response_missing_ids_and_wrapped_array(tmp_path):
    units = mk_units(2, per_page=2)
    calls = []

    def h(n, model, req):
        calls.append(n)
        us = req["units"] if n > 1 else req["units"][:1]
        return {"units": tr_of(us)}  # 配列を dict で包み、1 件欠落

    t, sdk, _ = translator(h, refine=False)
    res = translate_cached(t, units, tmp_path / "c.json")
    assert all(v.startswith("訳:") for v in res.values()) and len(calls) == 2


def test_prompts_missing_use_fallback_and_placeholders(tmp_path):
    t, sdk, _ = translator(lambda n, m, r: tr_of(r["units"]), refine=False)
    translate_cached(t, mk_units(1), tmp_path / "c.json")
    sys_inst = sdk.calls[0]["config"].system_instruction
    assert "翻訳" in sys_inst


# =============================================================== M3: 用語集

def test_glossary_build_cache_and_filter(jsr, tmp_path):
    inp = build_glossary_input(jsr)
    assert inp["title"] and inp["abstract"] and inp["headings"] and inp["sample"]
    assert len(inp["sample"]) <= 3000 and set(inp) == {"title", "abstract", "headings", "sample"}
    entries = [{"en": "Closed-loop", "ja": "クローズドループ", "note": ""}, {"en": "", "ja": "x"},
               {"en": "closed-loop", "ja": "dup"}, "bad"]
    cl, sdk, _ = make_client(lambda n, m, r: entries if False else json.dumps(entries))
    cfg = Config({"gemini": {"glossary_prompt": "nope.md"}})
    logs = []
    g = build_glossary(jsr, cl, cfg, tmp_path / "g.json", log=logs.append)
    assert g == [{"en": "Closed-loop", "ja": "クローズドループ", "note": ""}]
    assert sdk.calls[0]["model"] == "gemini-3.5-flash"
    g2 = build_glossary(jsr, cl, cfg, tmp_path / "g.json")
    assert g2 == g and len(sdk.calls) == 1  # キャッシュ
    assert filter_glossary(g, "a closed-LOOP system") == g and filter_glossary(g, "nothing") == []
    off = build_glossary(jsr, cl, Config({"gemini": {"glossary": False}}), tmp_path / "g2.json")
    assert off == [] and len(sdk.calls) == 1


def test_glossary_falls_back_to_lite_and_override(jsr, tmp_path):
    entries = [{"en": "spindle", "ja": "紡錘波"}]

    def h(n, model, req):
        return ApiError(429, "RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel") if model == "gemini-3.5-flash" else entries

    cl, sdk, _ = make_client(h)
    g = build_glossary(jsr, cl, Config({}), tmp_path / "g.json")
    assert g == [{"en": "spindle", "ja": "紡錘波", "note": ""}]
    assert [c["model"] for c in sdk.calls] == ["gemini-3.5-flash", "gemini-3.5-flash-lite"]
    ov = tmp_path / "glossary_override.json"
    ov.write_text(json.dumps([{"en": "cue", "ja": "手がかり"}, {"en": "bad"}], ensure_ascii=False), encoding="utf-8")
    cl2, sdk2, _ = make_client(h)
    assert build_glossary(jsr, cl2, Config({}), tmp_path / "g2.json", override_path=ov) == [{"en": "cue", "ja": "手がかり", "note": ""}]
    assert not sdk2.calls
    ov.write_text(json.dumps({"cue": "キュー"}), encoding="utf-8")
    assert build_glossary(jsr, cl2, Config({}), tmp_path / "g2.json", override_path=ov)[0]["ja"] == "キュー"
    ov.write_text("{broken", encoding="utf-8")
    logs = []
    assert build_glossary(jsr, make_client(h)[0], Config({}), tmp_path / "g3.json", override_path=ov, log=logs.append)
    assert any("読めません" in l for l in logs)


def test_glossary_failure_is_nonfatal(jsr, tmp_path):
    logs = []
    cl, *_ = make_client(lambda n, m, r: "not json")      # 応答が使えない -> 用語集なしで続行
    assert build_glossary(jsr, cl, Config({}), tmp_path / "g.json", log=logs.append) == [] and logs
    from readable.gemini_client import GeminiFatalError
    cl, *_ = make_client(lambda n, m, r: ApiError(403, "forbidden"))   # 設定・権限の誤りは中断する (cli が終了コード 8)
    with pytest.raises(GeminiFatalError):
        build_glossary(jsr, cl, Config({}), tmp_path / "g2.json", log=logs.append)
    cl, *_ = make_client(lambda n, m, r: ApiError(429, "RESOURCE_EXHAUSTED PerDay"))
    logs.clear()
    assert build_glossary(jsr, cl, Config({}), tmp_path / "g.json", log=logs.append) == [] and "日次" in logs[0]
