"""「翻訳を開始」ボタン制・同じ PDF での再翻訳 (終了コード 7/9/12)・構成の整理 (必須 + 使えないときの確認) のテスト。"""
import http.client
import json
import re
import sys
from pathlib import Path

import pytest

from readable import webapp
from test_webapp_review import apps, multipart, new_app, post, serve, wait_for  # noqa: F401

HTML = (Path(webapp.ROOT) / "readable" / "web_static" / "index.html").read_text(encoding="utf-8")
OPT = {"mode": "both", "claude": False, "retry": False}


def call(app, server, method, path, body=None, ctype=None):
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=60)
    h = {"X-Readable-Token": app.token}
    if ctype:
        h["Content-Type"] = ctype
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, json.loads(data) if data[:1] == b"{" else data


def script(code):
    return [sys.executable, "-c", code]


def finish(app, job):
    wait_for(lambda: job.status in ("done", "failed"))


def real_pdf(tmp_path):
    import fitz
    d = fitz.open()
    for _ in range(3):
        d.new_page().insert_text((72, 72), "hello")
    p = tmp_path / "three.pdf"
    d.save(p)
    return p.read_bytes()


# ---------- 添付だけでは始まらない ----------
def test_inspect_returns_name_pages_size_and_starts_nothing(tmp_path, apps):
    a = new_app(tmp_path)
    apps.append(a)
    server = serve(a, 19400)
    data = real_pdf(tmp_path)
    body, ct = multipart("my paper.pdf", data)
    st, j = call(a, server, "POST", "/api/inspect", body, ct)
    assert st == 200 and j == {"name": "my paper.pdf", "bytes": len(data), "pages": 3, "encrypted": False}
    assert not a.jobs and a.q.empty()  # ジョブは作られず、翻訳も始まらない
    assert call(a, server, "GET", "/api/jobs")[1] == {"jobs": []}
    body, ct = multipart("x.pdf", b"not a pdf")
    assert call(a, server, "POST", "/api/inspect", body, ct)[0] == 400
    server.shutdown()
    server.server_close()


def test_inspect_requires_token_and_origin(tmp_path, apps):
    a = new_app(tmp_path)
    apps.append(a)
    server = serve(a, 19410)
    body, ct = multipart("x.pdf", b"%PDF-1.4")
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
    c.request("POST", "/api/inspect", body=body, headers={"Content-Type": ct})
    assert c.getresponse().status == 401
    c.close()
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
    c.request("POST", "/api/inspect", body=body,
              headers={"Content-Type": ct, "X-Readable-Token": a.token, "Origin": "http://evil.example"})
    assert c.getresponse().status == 403
    c.close()
    server.shutdown()
    server.server_close()


def test_ui_attach_does_not_post_jobs_and_has_start_button():
    pick = HTML[HTML.index("async function pick("):HTML.index('$("#pick-clear")')]
    assert "/api/jobs" not in pick and "/api/inspect" in pick
    assert 'id="start"' in HTML and "翻訳を開始" in HTML and "ファイルを変更" in HTML
    assert 'id="claude"' not in HTML and 'id="retry"' not in HTML and "失敗段落を再試行" not in HTML  # チェックは廃止
    assert "この段落だけ再翻訳する" in HTML and "明日16時以降に押すと続きから" in HTML
    assert "構成の整理なしで続けますか? (品質が下がる場合があります)" in HTML
    assert "structure_message" in HTML


# ---------- 構成の整理: 画面は常に structure を明示する ----------
def test_structure_field_controls_no_claude_flag(tmp_path, apps):
    a = new_app(tmp_path)
    apps.append(a)
    server = serve(a, 19420)
    seen = {}
    a.q.put = lambda jid: seen.setdefault(jid, True)  # ワーカーを動かさず、コマンドだけ見る
    for fields, expect_no_claude in (({"structure": "1"}, False), ({"structure": "0"}, True), ({}, True)):
        body, ct = multipart("a.pdf", b"%PDF-1.4\n" + repr(fields).encode(), dict(fields, replace="1"))
        st, j = call(a, server, "POST", "/api/jobs", body, ct)
        assert st == 201
        cmd = a.command(a.jobs[j["id"]])
        assert ("--no-claude" in cmd) is expect_no_claude, (fields, cmd)
    server.shutdown()
    server.server_close()


def _st(provider, **kw):
    return {"claude": kw.get("claude", {"found": True, "logged_in": True}), "codex": kw.get("codex", {"found": True, "logged_in": True})}


def test_structure_status_reasons(monkeypatch):
    monkeypatch.setattr("readable.structure.detect_api_env", lambda environ=None: [])
    ok = webapp.structure_status("claude", _st("claude"))
    assert ok["usable"] and ok["enabled"] and ok["name"] == "Claude Code"
    nf = webapp.structure_status("claude", _st("claude", claude={"found": False, "logged_in": None}))
    assert not nf["usable"] and "インストール" in nf["reason"] and "/login" in nf["fix"]
    li = webapp.structure_status("claude", _st("claude", claude={"found": True, "logged_in": False}))
    assert not li["usable"] and "ログイン" in li["reason"] and "/login" in li["fix"]
    unknown = webapp.structure_status("claude", _st("claude", claude={"found": True, "logged_in": None}))
    assert unknown["usable"]  # 判定できないときは止めない (ジョブ中にスキップされたら結果欄に出る)
    monkeypatch.setattr("readable.structure.detect_api_env", lambda environ=None: ["ANTHROPIC_API_KEY"])
    api = webapp.structure_status("claude", _st("claude"))
    assert not api["usable"] and "ANTHROPIC_API_KEY" in api["reason"] and "課金" in api["reason"]
    cx = webapp.structure_status("codex", _st("codex", codex={"found": True, "logged_in": False, "detail": "未ログイン"}))
    assert not cx["usable"] and cx["name"] == "Codex" and "codex login" in cx["fix"]
    cn = webapp.structure_status("none", _st("none"))
    assert cn["enabled"] is False and cn["usable"]


def test_setup_endpoint_has_structure(tmp_path, apps, monkeypatch):
    monkeypatch.setattr(webapp, "_cli_status", lambda n, a: {"found": False, "logged_in": None})
    a = new_app(tmp_path)
    apps.append(a)
    server = serve(a, 19430)
    st, j = call(a, server, "GET", "/api/setup")
    assert st == 200 and j["structure"]["provider"] == a.structure_provider
    server.shutdown()
    server.server_close()


# ---------- 構成の整理がジョブ途中でスキップされた ----------
def test_structure_skip_is_captured(tmp_path, apps, monkeypatch):
    a = new_app(tmp_path)
    apps.append(a)
    monkeypatch.setattr(a, "command", lambda job: script(
        "print('[1/4] extract: x'); print('[警告] Claude 構造解析をスキップしヒューリスティックで続行します: claude にログインしていない')"))
    job = a.create_job("a.pdf", b"%PDF-1.4\n", OPT)
    finish(a, job)
    d = job.to_dict()
    assert d["structure_note"] == "Claude 構造解析をスキップしヒューリスティックで続行します: claude にログインしていない"
    assert d["structure_message"] == "構成の整理は行われませんでした: claude にログインしていない"
    ok = a.create_job("b.pdf", b"%PDF-1.4\nb", OPT)
    a.q.put  # noqa: B018
    assert ok.to_dict()["structure_note"] is None


# ---------- 同じ PDF で再翻訳 ----------
PARTIAL = ("import pathlib, sys\n"
           "out = [x for x in sys.argv[1:] if True]\n"
           "print('[警告] 翻訳できず原文のまま出力した段落が 3 件あります (上の警告を参照)。PDF は出力しました。')\n"
           "p = pathlib.Path(sys.argv[1]); p.mkdir(parents=True, exist_ok=True); (p / 'a_ja.pdf').write_bytes(b'%PDF')\n"
           "sys.exit(12)\n")


def test_retry_uses_same_pdf_and_retry_failed_flag(tmp_path, apps, monkeypatch):
    a = new_app(tmp_path)
    apps.append(a)
    server = serve(a, 19440)
    monkeypatch.setattr(a, "command", lambda job: [sys.executable, "-c", PARTIAL, str(job.dir / "out")])
    data = b"%PDF-1.4\nthe-same-file"
    job = a.create_job("a.pdf", data, OPT)
    finish(a, job)
    d = job.to_dict()
    assert d["status"] == "done" and d["exit_code"] == 12 and d["warning"] and d["failed_paragraphs"] == 3 and d["can_retry"]
    pdf = job.dir / "in" / "a.pdf"
    assert pdf.read_bytes() == data
    runs = []
    monkeypatch.setattr(a, "command", lambda j: runs.append(webapp.App.command(a, j)) or script("print('retry ran')"))
    st, j = call(a, server, "POST", f"/api/jobs/{job.id}/retry", b"{}", "application/json")
    assert st == 200 and j["id"] == job.id  # 同じジョブ・同じ PDF
    wait_for(lambda: job.status in ("done", "failed") and runs)
    assert "--retry-failed" in runs[0] and str(pdf) in runs[0]
    assert pdf.read_bytes() == data  # 再翻訳の後も元の PDF を保持
    assert job.exit_code == 0 and job.failed_paragraphs is None and not job.to_dict()["can_retry"]
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("code", [7, 9])
def test_retry_button_available_for_limit_and_api_errors(tmp_path, apps, monkeypatch, code):
    a = new_app(tmp_path)
    apps.append(a)
    monkeypatch.setattr(a, "command", lambda job: script(f"import sys; sys.exit({code})"))
    job = a.create_job("a.pdf", b"%PDF-1.4\nx", OPT)
    finish(a, job)
    assert job.status == "failed" and job.exit_code == code and job.to_dict()["can_retry"]
    assert a.retry_job(job) is True


@pytest.mark.parametrize("code", [0, 4, 5, 11])
def test_retry_refused_for_other_exit_codes(tmp_path, apps, monkeypatch, code):
    a = new_app(tmp_path)
    apps.append(a)
    pdf_out = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.mkdir(parents=True,exist_ok=True); (p/'a_ja.pdf').write_bytes(b'%PDF'); "
    monkeypatch.setattr(a, "command", lambda job: [sys.executable, "-c", pdf_out + f"sys.exit({code})", str(job.dir / "out")])
    job = a.create_job("a.pdf", b"%PDF-1.4\ny", OPT)
    finish(a, job)
    assert not job.to_dict()["can_retry"] and a.retry_job(job) is False


def test_retry_needs_kept_pdf_and_idle_job(tmp_path, apps, monkeypatch):
    a = new_app(tmp_path)
    apps.append(a)
    monkeypatch.setattr(a, "command", lambda job: script("import sys; sys.exit(7)"))
    job = a.create_job("a.pdf", b"%PDF-1.4\nz", OPT)
    finish(a, job)
    (job.dir / "in" / "a.pdf").unlink()
    assert not job.can_retry()  # 元の PDF が無ければ再翻訳できない


def test_retry_endpoint_rejects_unknown_and_unauthed(tmp_path, apps):
    a = new_app(tmp_path)
    apps.append(a)
    server = serve(a, 19450)
    assert call(a, server, "POST", "/api/jobs/" + "0" * 16 + "/retry", b"{}", "application/json")[0] == 404
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
    c.request("POST", "/api/jobs/" + "0" * 16 + "/retry", body=b"{}", headers={"Content-Type": "application/json"})
    assert c.getresponse().status == 401
    c.close()
    server.shutdown()
    server.server_close()


# ---------- 構成の整理の結果 (全部/一部不採用) の表示 ----------
def _run_lines(tmp_path, apps, monkeypatch, lines):
    a = new_app(tmp_path)
    apps.append(a)
    code = "\n".join(f"print({ln!r})" for ln in lines)
    monkeypatch.setattr(a, "command", lambda job: script(code))
    job = a.create_job("a.pdf", b"%PDF-1.4\n" + repr(lines).encode(), OPT)
    finish(a, job)
    return a, job


@pytest.mark.parametrize("who", ["Claude", "Codex"])
def test_structure_rejected_whole_is_shown(tmp_path, apps, monkeypatch, who):
    _, job = _run_lines(tmp_path, apps, monkeypatch, [
        f"[警告] {who} 構造解析の結果を採用せずヒューリスティックで続行します: 翻訳対象の frame が 100 -> 10 に減るため採用しません"])
    assert job.to_dict()["structure_message"] == "構成の整理の結果は全部使われませんでした: 翻訳対象の frame が 100 -> 10 に減るため採用しません"


def test_structure_partial_counts_are_shown(tmp_path, apps, monkeypatch):
    _, job = _run_lines(tmp_path, apps, monkeypatch, ["[構造] 反映 7 件 / 不採用 3 件 : 重複する結合"])
    m = job.to_dict()["structure_message"]
    assert "一部使われませんでした" in m and "7件を反映、3件は不採用" in m and "重複する結合" in m


def test_structure_all_applied_shows_nothing(tmp_path, apps, monkeypatch):
    _, job = _run_lines(tmp_path, apps, monkeypatch, ["[構造] 反映 5 件 / 不採用 0 件"])
    assert job.to_dict()["structure_message"] is None


def test_structure_counts_from_render_report(tmp_path, apps, monkeypatch):
    a = new_app(tmp_path)
    apps.append(a)
    monkeypatch.setattr(a, "command", lambda job: script("import time; time.sleep(2)"))
    job = a.create_job("rr.pdf", b"%PDF-1.4\nrr", OPT)
    wait_for(lambda: job.work_dir is not None)
    job.work_dir.mkdir(parents=True, exist_ok=True)
    (job.work_dir / "render_report.json").write_text(
        json.dumps({"structure": {"accepted": 2, "rejected": 5, "reason": "検証に失敗"}}), encoding="utf-8")
    finish(a, job)
    m = job.to_dict()["structure_message"]
    assert m and "2件を反映、5件は不採用" in m and "検証に失敗" in m


# ---------- job.log (全文をファイルにも保存。API キーは伏せる) ----------
def test_job_log_file_saved_masked_and_removed_with_job(tmp_path, apps, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    a = new_app(tmp_path)
    apps.append(a)
    key = "AIzaSyTESTKEYtestkey0123456789abcdefgh"
    a.settings.set_key(key)
    code = ("import os, sys\n" + "\n".join(f"print('line {i}')" for i in range(40))
            + "\nprint('key=' + os.environ['GEMINI_API_KEY'])\nsys.exit(7)")
    monkeypatch.setattr(a, "command", lambda job: script(code))
    job = a.create_job("a.pdf", b"%PDF-1.4\nlog", OPT)
    finish(a, job)
    text = (job.dir / "job.log").read_text(encoding="utf-8")
    assert "line 0" in text and "line 39" in text and len(job.log) == webapp.LOG_KEEP  # 画面は末尾だけ、ファイルは全文
    assert key not in text and "key=***" in text
    assert key not in json.dumps(job.to_dict(), ensure_ascii=False)
    monkeypatch.setattr(a, "command", lambda j: script("print('retry line')"))
    assert a.retry_job(job)
    wait_for(lambda: job.status in ("done", "failed") and "retry line" in (job.dir / "job.log").read_text(encoding="utf-8"))
    text2 = (job.dir / "job.log").read_text(encoding="utf-8")
    assert "line 0" in text2 and "再翻訳" in text2  # 再翻訳は追記
    d = job.dir
    a.delete_job(job)
    assert not d.exists()  # ジョブと一緒に削除


# ---------- 中止 (処理だけ止めて、アップロードした PDF・できていた出力・キャッシュは残す) ----------
def test_cancel_running_keeps_pdf_outputs_and_allows_retry(tmp_path, apps, monkeypatch):
    a = new_app(tmp_path)
    apps.append(a)
    server = serve(a, 19460)
    monkeypatch.setattr(a, "command", lambda job: script("import time; print('[1/4] extract: x', flush=True); time.sleep(120)"))
    data = b"%PDF-1.4\ncancel-me"
    job = a.create_job("c.pdf", data, OPT)
    wait_for(lambda: job.proc is not None and job.stage == "extract")
    (job.dir / "out").mkdir(exist_ok=True)
    (job.dir / "out" / "c_ja.pdf").write_bytes(b"%PDF-old")  # 以前の結果が残っている想定
    st, j = call(a, server, "POST", f"/api/jobs/{job.id}/cancel", b"{}", "application/json")
    assert st == 200
    wait_for(lambda: job.status == "failed")
    assert job.proc.poll() is not None  # 処理は止まった
    assert job.exit_code == 130 and "中止" in job.to_dict()["message_ja"]
    assert (job.dir / "in" / "c.pdf").read_bytes() == data  # アップロードした PDF は残る
    assert (job.dir / "out" / "c_ja.pdf").exists() and "ja" in job.to_dict()["outputs"]  # 出力も残る
    assert job.id in a.jobs and job.to_dict()["can_retry"]
    monkeypatch.setattr(a, "command", lambda j2: script("print('resumed')"))
    assert a.retry_job(job)  # 添付し直さずに続きから
    wait_for(lambda: job.status in ("done", "failed") and job.exit_code == 0)
    server.shutdown()
    server.server_close()


def test_cancel_queued_job_never_starts(tmp_path, apps, monkeypatch):
    a = new_app(tmp_path)
    apps.append(a)
    monkeypatch.setattr(a.q, "put", lambda jid: None)  # ワーカーに渡さず「待機中」のまま
    job = a.create_job("q.pdf", b"%PDF-1.4\nq", OPT)
    assert job.status == "queued" and a.cancel_job(job) is True
    assert job.status == "failed" and job.exit_code == 130 and job.proc is None
    assert (job.dir / "in" / "q.pdf").exists()


def test_cancel_refused_when_not_running_and_needs_token(tmp_path, apps, monkeypatch):
    a = new_app(tmp_path)
    apps.append(a)
    server = serve(a, 19470)
    monkeypatch.setattr(a, "command", lambda job: script("print('x')"))
    job = a.create_job("d.pdf", b"%PDF-1.4\nd", OPT)
    finish(a, job)
    assert call(a, server, "POST", f"/api/jobs/{job.id}/cancel", b"{}", "application/json")[0] == 409
    assert call(a, server, "POST", "/api/jobs/" + "0" * 16 + "/cancel", b"{}", "application/json")[0] == 404
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
    c.request("POST", f"/api/jobs/{job.id}/cancel", body=b"{}", headers={"Content-Type": "application/json"})
    assert c.getresponse().status == 401
    c.close()
    server.shutdown()
    server.server_close()


def test_ui_cancel_button_uses_cancel_endpoint_not_delete():
    assert '"/cancel"' in HTML and 'data-act=' in HTML


# ---------- M14: 翻訳の補助 (assist) の同意文・オフ・結果の 1 行 ----------
def test_assist_message_summaries():
    info = {"enabled": True, "calls": 3, "glossary": {"adopted": [{}] * 16, "rejected": []},
            "correction": {"requested": 4, "adopted": 3}, "skipped": ""}
    assert webapp.assist_message(info, "claude", True) == "Claudeの補助: 用語集の修正 16件、問題段落の補正 3/4件採用"
    info2 = dict(info, skipped="呼び出し回数の上限に達しました")
    assert "スキップ: 呼び出し回数の上限に達しました" in webapp.assist_message(info2, "claude", True)
    assert webapp.assist_message({"enabled": True, "calls": 1, "glossary": {"adopted": []}}, "claude", True) == "Claudeの補助: 用語集の修正 0件"
    assert "未対応" in webapp.assist_message({"enabled": False, "skipped": "codex 版の assist は未実装です"}, "codex", True)
    assert "使いませんでした" in webapp.assist_message(None, "claude", False) and "オフ" in webapp.assist_message(None, "claude", False)
    assert "使いませんでした" in webapp.assist_message({"enabled": False, "skipped": ""}, "claude", True)
    assert webapp.assist_message(None, "claude", True) is None and webapp.assist_message({}, "none", True) is None


def test_assist_summary_read_from_render_report(tmp_path, apps, monkeypatch):
    a = new_app(tmp_path, structure_provider="claude")
    apps.append(a)
    monkeypatch.setattr(a, "command", lambda job: script("import time; time.sleep(2)"))
    job = a.create_job("as.pdf", b"%PDF-1.4\nas", OPT)
    wait_for(lambda: job.work_dir is not None)
    job.work_dir.mkdir(parents=True, exist_ok=True)
    (job.work_dir / "render_report.json").write_text(json.dumps({"assist": {
        "enabled": True, "provider": "claude", "calls": 3, "glossary": {"adopted": ["a"] * 16, "rejected": []},
        "correction": {"requested": 4, "adopted": 3}, "skipped": ""}}), encoding="utf-8")
    finish(a, job)
    assert job.to_dict()["assist_message"] == "Claudeの補助: 用語集の修正 16件、問題段落の補正 3/4件採用"


def test_codex_edition_shows_assist_unsupported(tmp_path, apps, monkeypatch):
    monkeypatch.setattr("readable.assist.ASSIST_PROVIDERS", {"claude": object()})
    monkeypatch.setattr("readable.codex_assist.register", lambda: None)  # optional provider unavailable
    a = webapp.App(tmp_path / "jobs", tmp_path / "work", translator="dummy", home=tmp_path / "h", structure_provider="codex")
    apps.append(a)
    monkeypatch.setattr(a, "command", lambda job: script("import time; time.sleep(2)"))
    job = a.create_job("cx.pdf", b"%PDF-1.4\ncx", OPT)
    wait_for(lambda: job.work_dir is not None)
    job.work_dir.mkdir(parents=True, exist_ok=True)
    (job.work_dir / "render_report.json").write_text(json.dumps({"assist": {
        "enabled": False, "provider": "codex", "calls": 0, "skipped": "codex 版の assist は未実装です"}}), encoding="utf-8")
    finish(a, job)
    assert job.to_dict()["assist_message"] == "Codexの補助: 未対応 (この版ではまだ使えません)"
    assert webapp.setup_status(a.settings, fast=True, provider="codex")["assist"] == {"provider": "codex", "supported": False}
    assert webapp.setup_status(a.settings, fast=True, provider="claude")["assist"]["supported"] is True


def test_no_assist_option_passed_to_cli(tmp_path, apps):
    a = new_app(tmp_path)
    apps.append(a)
    server = serve(a, 19480)
    a.q.put = lambda item: None  # ワーカーを動かさず、コマンドだけ見る
    for fields, expect in (({"no_assist": "1"}, True), ({"no_assist": "0"}, False), ({}, False)):
        body, ct = multipart("a.pdf", b"%PDF-1.4\n" + repr(fields).encode(), dict(fields, structure="1", replace="1"))
        st, j = call(a, server, "POST", "/api/jobs", body, ct)
        assert st == 201
        job = a.jobs[j["id"]]
        cmd = a.command(job)
        assert ("--no-assist" in cmd) is expect and "--no-claude" not in cmd, (fields, cmd)  # 構成の整理は別 (常にオン)
        assert job.options["assist"] is (not expect)
    server.shutdown()
    server.server_close()


def test_consent_is_asked_again_after_wording_change(tmp_path):
    assert webapp.CONSENT_VERSION >= 3
    home = tmp_path / "h"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"consent_version": 2, "consent_provider": "claude",
                                                   "consent_providers": {"claude": 2, "codex": 2}}), encoding="utf-8")
    for scope in ("claude", "codex"):
        st = webapp.Settings(home, consent_scope=scope)
        assert st.consented() is False  # 旧文面の同意は無効 (どちらの版でも再同意)
        st.set_consent()
        assert st.consented() is True


def test_ui_has_assist_consent_text_and_off_switch():
    assert "題名・要旨 (最大 900 文字)" in HTML and "用語集" in HTML and "原文と訳文" in HTML
    assert 'id="no-assist"' in HTML and "翻訳の補助をさせない" in HTML and '"no_assist"' in HTML
    assert "assist_message" in HTML
