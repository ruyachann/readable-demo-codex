"""Codex レビュー (docs/CODEX_REVIEW_CLAUDE.md) のコア側指摘 CR-05/06/09/10/11/12・識別不能な 429・スキャン+OCR の再現テスト。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

from readable import cli
from readable import structure as st
from readable.config import Config
from readable.extract import extract_pdf
from readable.gemini_client import DailyLimitError, GeminiClient, QuotaState, RateLimitError, is_daily_limit
from readable.glossary import build_glossary
from readable.numcheck import check_numbers
from readable.render import estimate_paper_color, render_ja
from readable.translate import OCR_RULE, GeminiTranslator, check_translation, translate_cached
from test_m23 import ApiError, FakeSDK, FakeRun, _result, make_client, mk_units, tr_of, translator


# =============================================================== CR-09: Claude 子プロセスの環境

API_ENV = {"ANTHROPIC_API_KEY": "sk-ant-dummy", "ANTHROPIC_AUTH_TOKEN": "tok", "ANTHROPIC_BASE_URL": "http://gw.example",
           "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "PATH": "/bin", "HOME": "/h"}


def test_prepare_env_skips_when_api_settings_are_present_without_touching_the_real_environment(monkeypatch):
    before = dict(__import__("os").environ)
    env, why = st.prepare_claude_env(dict(API_ENV))
    assert env is None and "API キー" in why and "ANTHROPIC_API_KEY" in why and "課金" in why
    assert dict(__import__("os").environ) == before                         # 実際の環境変数は変更しない
    env, why = st.prepare_claude_env({"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "oauth"})   # 契約 (OAuth) のトークンは許す
    assert why is None and env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth"


def test_prepare_env_gateway_opt_out_still_strips_keys_but_keeps_base_url():
    env, why = st.prepare_claude_env(dict(API_ENV), allow_gateway=True)
    assert why is None
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
        assert k not in env
    assert env["ANTHROPIC_BASE_URL"] == "http://gw.example" and env["PATH"] == "/bin"


@pytest.mark.parametrize("out,expect_skip", [
    ({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}, False),
    ({"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"}, True),
    ({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"}, True),
    ({"loggedIn": False}, True), ("not json", True), ({"authMethod": "claude.ai"}, True),
])
def test_check_auth_requires_subscription_login(out, expect_skip):
    stdout = out if isinstance(out, str) else json.dumps(out)
    run = lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    why = st.check_claude_auth(["claude"], {}, 5, run)
    assert bool(why) == expect_skip


def test_check_auth_timeout_is_a_skip():
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    assert st.check_claude_auth(["claude"], {}, 1, run)


def test_call_claude_uses_a_dedicated_env_and_skips_with_api_settings(monkeypatch):
    runner = FakeRun((0, _result()))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-dummy")
    with pytest.raises(st.ClaudeSkipped):                                     # 既定: API キーを検出したら呼ばずにスキップ
        st.call_claude("sys", "user", Config({}), runner=runner)
    assert not runner.calls
    runner2 = FakeRun((0, _result()))
    st.call_claude("sys", "user", Config({"claude": {"allow_gateway": True}}), runner=runner2)
    env = runner2.calls[0][2]["env"]
    assert "ANTHROPIC_API_KEY" not in env                                    # 許可しても API キーは子に渡さない
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-dummy"                  # 親は無傷


def test_run_structure_continues_with_a_warning_when_skipped(jsr, tmp_path, monkeypatch):
    import copy
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gw.example")
    logs = []
    d = copy.deepcopy(jsr)
    before = copy.deepcopy(d["joins"])
    info = st.run_structure(d, tmp_path, runner=FakeRun((0, _result())), log=logs.append)
    assert not info["ok"] and d["joins"] == before and any("API 課金を避けて" in l for l in logs)


# =============================================================== CR-10: provider = none は Claude を一切呼ばない

def test_provider_none_makes_no_call_and_ignores_cache(jsr, tmp_path):
    import copy
    run = FakeRun()
    d = copy.deepcopy(jsr)
    (tmp_path / "structure.json").write_text(json.dumps({"input_hash": "x", "result": {"roles": {}, "joins_add": [], "joins_remove": [], "charmap": {}}}), encoding="utf-8")
    info = st.run_structure(d, tmp_path, Config({"structure": {"provider": "none"}}), runner=run)
    assert info["reason"] == "disabled" and not run.calls and not info["cached"]
    with pytest.raises(st.StructureConfigError):
        st.run_structure(copy.deepcopy(jsr), tmp_path, Config({"structure": {"provider": "gpt"}}), runner=run)


def test_cli_unsupported_provider_exit10(tmp_path, capsys):
    from synth_pdfs import one_page_paper
    pdf = one_page_paper(tmp_path / "p.pdf")
    cfg = tmp_path / "c.toml"
    cfg.write_text('[structure]\nprovider = "unsupported"\n', encoding="utf-8")
    assert cli.main([str(pdf), "--out", str(tmp_path / "o"), "--work-dir", str(tmp_path / "w"), "--translator", "dummy", "--config", str(cfg)]) == 10
    assert "未対応" in capsys.readouterr().err


# =============================================================== CR-05 / CR-06: 数値の検証

@pytest.mark.parametrize("src,ja,bad", [
    ("The temperature was -5 C at baseline.", "開始時の温度は5 Cであった。", True),                    # 負号の脱落 (Codex 再現)
    ("The temperature was -5 C at baseline.", "開始時の温度は−5 Cであった。", False),
    ("The result was significant at p < 0.05 here.", "ここではp > 0.05で有意であった。", True),         # 比較の逆転 (Codex 再現)
    ("The result was significant at p < 0.05 here.", "ここではp < 0.05で有意であった。", False),
    ("significant at (<i>p</i> &lt; 0.05)", "有意 (<i>p</i> &lt; 0.05)", False),
    ("effect size d ≥ 0.5", "効果量 d ≤ 0.5", True), ("n = 24 participants", "24名の参加者", False),
    ("The sample contained 30 participants.", "参加者は30人で、男性は20人であった。", True),            # 根拠のない数値の追加 (Codex 再現)
    ("30 participants at Chiba University", "参加者を募集し、千葉大学でデータを収集した。", True),      # 千葉は数量ではない (Codex 再現)
    ("30 participants at Momose Lab", "百瀬研究室で参加者を集めた。", True),
    ("30 participants at Chiba University", "千葉大学の30人の参加者。", False),
    ("5 million people", "500万人", False), ("1.5 billion yen", "15億円", False), ("30,000 cells", "3万個の細胞", False),
    ("3,000 items", "3000 の項目", False), ("12 items", "１２ の項目", False),
    ("three groups and two sessions", "3群と2回のセッション", False),                                   # three → 3
    ("a value of 1.2 × 10^-5", "値は1.2×10<sup>−5</sup>", False), ("a value of 1.2 × 10^-5", "値は1.2×10<sup>5</sup>", True),
    ("a value of 1.2e-5 here", "ここでは1.2e-5", False), ("range 10.5–13.5 Hz", "10.5〜13.5 Hz", False),
    ("In the last two decades", "過去20年間", False), ("on 29 June 2021", "2021年6月29日", False),
    ("45 of 120 subjects", "120人中46人", True), ("p < 0.05", "p値は0.05未満", False),
])
def test_number_checks(src, ja, bad):
    assert bool(check_numbers(src, ja)) == bad, check_numbers(src, ja)


def test_sign_flip_is_rejected_and_resent_through_the_translator(tmp_path):
    src = "The temperature was -5 C at baseline and the recorded value was used."
    units = [{"id": "u0", "role": "body", "page": 1, "frames": ["p1-f1"], "text": src, "vars": {}}]
    calls = []

    def h(n, m, r):
        calls.append(r["units"][0].get("note"))
        return [{"id": "u0", "text": "温度は5 Cであった。" if n == 1 else "温度は−5 Cであった。"}]

    t, sdk, _ = translator(h, refine=False)
    res = translate_cached(t, units, tmp_path / "c.json")
    assert res["u0"].startswith("温度は−5") and len(calls) == 2 and "符号" in calls[1]


def test_old_cache_entries_are_revalidated_and_only_failing_units_are_resent(tmp_path):
    units = [{"id": f"u{i}", "role": "body", "page": 1, "frames": [f"p1-f{i}"], "text": f"Value {i} was -5 and p < 0.05 here and there.", "vars": {}}
             for i in range(3)]
    cp = tmp_path / "c.json"
    t0, sdk0, _ = translator(lambda n, m, r: [{"id": u["id"], "text": f"値{u['id'][1:]}は−5でp < 0.05 だった。"} for u in r["units"]], refine=False)
    translate_cached(t0, units, cp)
    c = json.loads(cp.read_text(encoding="utf-8"))
    k = next(k for k, v in c.items() if v.startswith("値1"))
    c[k] = "値1は5でp < 0.05だった。"                                       # 旧版の検証をすり抜けた誤訳 (符号の脱落) をキャッシュに仕込む
    cp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    t1, sdk1, _ = translator(lambda n, m, r: [{"id": u["id"], "text": f"値{u['id'][1:]}は−5でp < 0.05 だった。"} for u in r["units"]], refine=False)
    res = translate_cached(t1, units, cp)
    sent = [u["id"] for call in sdk1.calls for u in json.loads(call["contents"])["units"]]
    assert sent == ["u1"] and "−5" in res["u1"] and t1.stats["revalidated_dropped"] == 1      # 通らなかった 1 unit だけ再翻訳
    # 再翻訳も通らないときは、原文ではなく以前の訳を警告つきで使う
    c = json.loads(cp.read_text(encoding="utf-8"))
    c[next(k for k, v in c.items() if v.startswith("値1"))] = "値1は5でp < 0.05だった。"
    cp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    t2, sdk2, _ = translator(lambda n, m, r: [{"id": u["id"], "text": "値は5でp < 0.05だった。"} for u in r["units"]], refine=False)
    res2 = translate_cached(t2, units, cp)
    assert res2["u1"].startswith("値1は5") and t2.stats["fallback_original"] == 0 and any("以前の訳" in w for w in t2.warnings)


# =============================================================== 識別不能な 429

def test_429_without_quota_id_is_not_a_daily_limit_even_with_long_retry_delay(tmp_path):
    from google.genai import errors
    e = errors.ClientError(429, {"error": {"message": "slow down", "status": "RESOURCE_EXHAUSTED",
                                           "details": [{"@type": "x.RetryInfo", "retryDelay": "3600s"}]}})
    assert not is_daily_limit(e)
    sleeps = []
    qp = tmp_path / ".quota_state.json"
    sdk = FakeSDK(lambda n, m, r: e)
    cl = GeminiClient(Config({"gemini": {"rpm": 60000, "max_retries": 2}}), genai_client=sdk, sleep=sleeps.append, quota=QuotaState(qp, scope="k"))
    with pytest.raises(RateLimitError):                                       # 待って再試行し、それでも駄目なら RateLimitError (終了コード 9)
        cl.generate_json("m", "s", "{}", {})
    assert len(sdk.calls) == 3 and not qp.exists() and "m" not in cl.exhausted      # 日次状態は保存しない


# =============================================================== CR-11: 用語集キャッシュの型

@pytest.mark.parametrize("content", ["[]", "null", "5", '"x"', '{"key": "k", "glossary": 5}', '{"glossary": null}', "{broken"])
def test_glossary_cache_with_bad_type_is_moved_aside_and_regenerated(jsr, tmp_path, content):
    cp = tmp_path / "g.json"
    cp.write_text(content, encoding="utf-8")
    cl, sdk, _ = make_client(lambda n, m, r: [{"en": "sleep", "ja": "睡眠"}])
    logs = []
    gl = build_glossary(jsr, cl, Config({}), cp, log=logs.append)
    assert gl and gl[0]["ja"] == "睡眠" and len(sdk.calls) == 1               # 再生成した (クラッシュしない)
    assert (tmp_path / "g.json.bak").exists() and any("退避" in l for l in logs)
    assert json.loads(cp.read_text(encoding="utf-8"))["glossary"][0]["en"] == "sleep"


# =============================================================== CR-12: 選択的再翻訳の分割中断

EN = "the otters were observed in the enclosure for several hours during the morning feeding session"


def test_selective_split_keeps_the_first_half_when_the_second_hits_the_daily_limit(tmp_path):
    units = [{"id": f"u{i}", "role": "body", "page": 1, "frames": [f"p1-f{i}"], "text": EN + f" number {i}", "vars": {}} for i in range(2)]
    seq = []

    def h(n, m, r):
        seq.append((n, [u["id"] for u in r["units"]]))
        us = r["units"]
        if n == 1:                                      # 翻訳段階: 英語のまま返す (英語残りとして選択的再翻訳の対象になる)
            return [{"id": u["id"], "text": u["text"]} for u in us]
        if n == 2:
            return ""                                    # 2 unit バッチの応答が不正 -> 分割
        if n == 3:
            return [{"id": "u0", "text": "カワウソは朝の給餌時間中に数時間、飼育場で観察された (number 0)。"}]
        return ApiError(429, "RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel")   # 後半は日次上限

    t, sdk, _ = translator(h, refine=False)
    cp = tmp_path / "c.json"
    res = translate_cached(t, units, cp)
    assert res["u0"].startswith("カワウソ")                                  # 前半の成功は最終結果に残る
    saved = json.loads(cp.read_text(encoding="utf-8"))
    assert any(v.startswith("カワウソ") for v in saved.values())              # 保存済み (再開時に再送されない)
    assert any("上限" in w for w in t.warnings)
    t2, sdk2, _ = translator(lambda n, m, r: tr_of(r["units"]), refine=False)
    res2 = translate_cached(t2, units, cp)
    resent = [u["id"] for call in sdk2.calls for u in json.loads(call["contents"])["units"]]
    assert "u0" not in resent and res2["u0"].startswith("カワウソ")


# =============================================================== スキャン + OCR

def _ocr_pdf(path: Path, paper=(235, 225, 200)) -> Path:
    """英文を画像として焼き込み、同じ位置に不可視の文字層 (render mode 3) を重ねた「スキャン + OCR」風の PDF。図の領域 (黒い四角) の文字層は短い断片だけ。"""
    src = fitz.open()
    pg = src.new_page(width=408, height=648)
    pg.draw_rect(pg.rect, fill=tuple(c / 255 for c in paper), color=None)
    lines = ["Brindley attempts to determine the site at which the current acted by investigating",
             "the interaction of flickering light with the phosphene. This paper is an attempt to",
             "examine the problem further and the results show that the process is sensitive.",
             "A second paragraph of the paper starts here and explains the experimental method",
             "in more detail so that the reader can follow the logic of the whole experiment."]
    ys = [60, 74, 88, 110, 124]
    for t, y in zip(lines, ys):
        pg.insert_text((40, y), t, fontsize=8, fontname="tiro")
    pg.draw_rect(fitz.Rect(60, 200, 340, 380), fill=(0.1, 0.1, 0.4), color=None)       # 図 (スキャンに埋め込まれた図)
    pix = pg.get_pixmap(dpi=120)
    doc = fitz.open()
    out = doc.new_page(width=408, height=648)
    out.insert_image(out.rect, pixmap=pix)
    for t, y in zip(lines, ys):
        out.insert_text((40, y), t, fontsize=8, fontname="cour", render_mode=3)
    for t, (x, y) in {"0": (80, 395), "10": (160, 395), "Time (min)": (200, 410)}.items():
        out.insert_text((x, y), t, fontsize=7, fontname="cour", render_mode=3)
    for t, y in [("After the figure the text goes on with a long sentence that spans the whole page width here.", 440),
                 ("And it continues with another sentence that is long enough to count as a prose line.", 454)]:
        out.insert_text((40, y), t, fontsize=8, fontname="cour", render_mode=3)
    doc.save(path)
    return path


def test_paper_color_is_estimated_from_the_scan(tmp_path):
    p = _ocr_pdf(tmp_path / "s.pdf", paper=(235, 225, 200))
    r, g, b = estimate_paper_color(fitz.open(p)[0])
    assert abs(r * 255 - 235) < 12 and abs(g * 255 - 225) < 12 and abs(b * 255 - 200) < 12


def test_scanned_ocr_page_is_translated_and_the_english_is_painted_over(tmp_path):
    from readable.translate import DummyTranslator, build_units, frames_translations
    p = _ocr_pdf(tmp_path / "s.pdf")
    d = extract_pdf(p)
    pg = d["pages"][0]
    assert pg["scanned_ocr"] and not any(i[2] - i[0] > 400 and i[3] - i[1] > 600 for i in pg["images"])   # ページ全面の画像は図ではない
    body = [f for f in pg["frames"] if f["translate"]]
    assert body and not any(f["role"] == "figure_text" for f in body)
    assert any(f["role"] == "figure_text" and f["text"] in ("0", "10", "Time (min)") for f in pg["frames"])    # 図の帯の中の断片は翻訳しない
    assert any(i[1] <= 200 and i[3] >= 380 for i in pg["images"])                                              # 図の領域は障害物として記録
    units = build_units(d)
    fr = frames_translations(d, units, {u["id"]: "これは日本語に訳された段落であり、英語の原文は紙の色で隠される。" * 2 for u in units})
    out = tmp_path / "o.pdf"
    rep = render_ja(p, d, fr, out)
    assert rep["scanned_ocr"] == [1] and rep["failed"] == 0
    pix = fitz.open(out)[0].get_pixmap(dpi=100)
    # 図 (濃い青) は塗りつぶされない: 図の中心付近の画素は濃い青のまま
    cx, cy = int(200 * 100 / 72), int(290 * 100 / 72)
    off = (cy * pix.width + cx) * pix.n
    assert pix.samples[off + 2] > pix.samples[off] + 20 and pix.samples[off] < 80
    txt = fitz.open(out)[0].get_text()
    assert "これは日本語に訳された" in txt


def test_ocr_rule_is_added_only_for_ocr_documents():
    t, sdk, _ = translator(lambda n, m, r: tr_of(r["units"]), refine=False)
    t.ocr = False
    assert OCR_RULE.strip() not in t._system("translate", [], {})
    t.ocr = True
    assert "OCR" in t._system("translate", [], {}) and "Phyriol" in t._system("translate", [], {})
    # 文書ごとの追加なので、OCR でない文書のキャッシュ鍵は変わらない
    units = mk_units(1)
    t2, sdk2, _ = translator(lambda n, m, r: tr_of(r["units"]), refine=False)
    cp = Path.cwd() / "nul"
    res = t2.translate(units, context={"ocr": True}, cache=None)
    assert "ocr" in json.dumps(sdk2.calls[0]["config"].system_instruction) .lower()
