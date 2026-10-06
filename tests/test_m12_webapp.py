"""M12の中止競合と構造警告。合成入力とローカル子プロセスのみ。"""
import json
import sys
import threading

import pytest

from readable import webapp
from test_webapp_review import wait_for


@pytest.fixture(params=["claude", "codex"])
def app(tmp_path, request, monkeypatch):
    a = webapp.App(tmp_path / "jobs", tmp_path / "work", translator="dummy",
                   home=tmp_path / "home", structure_provider=request.param)
    monkeypatch.setattr(a.settings, "get_key", lambda: ("", "none"))
    yield a
    a.stop_all()
    a.instance.release()


OPT = {"mode": "both", "claude": False, "retry": False}
DATA = b"%PDF-1.4\nsynthetic cancellation input"


def test_cancel_before_spawn_starts_nothing(app, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    started = []

    def key():
        entered.set()
        assert release.wait(10)
        return "", "none"

    monkeypatch.setattr(app.settings, "get_key", key)
    monkeypatch.setattr(app, "command", lambda j: started.append(j.id) or [sys.executable, "-c", "pass"])
    j = app.create_job("synthetic.pdf", DATA, OPT)
    try:
        assert entered.wait(10)
        assert j.status == "running" and j.proc is None
        assert app.cancel_job(j)
    finally:
        release.set()
    assert wait_for(lambda: j.status == "failed")
    assert j.exit_code == 130 and j.proc is None and not started
    assert (j.dir / "in/synthetic.pdf").read_bytes() == DATA and j.can_retry()


def test_cancel_during_process_registration_stops_child(app, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real_popen = webapp.subprocess.Popen

    def gated_popen(*args, **kw):
        p = real_popen(*args, **kw)
        entered.set()
        assert release.wait(10)
        return p

    monkeypatch.setattr(app, "command", lambda j: [sys.executable, "-c", "import time; time.sleep(120)"])
    monkeypatch.setattr(webapp.subprocess, "Popen", gated_popen)
    j = app.create_job("synthetic.pdf", DATA, OPT)
    results = []
    try:
        assert entered.wait(10)
        t = threading.Thread(target=lambda: results.append(app.cancel_job(j)))
        t.start()
    finally:
        release.set()
    t.join(15)
    assert not t.is_alive() and results == [True]
    assert wait_for(lambda: j.status == "failed")
    assert j.exit_code == 130 and j.proc.poll() is not None


def test_queued_cancel_retry_does_not_run_stale_queue_item(app, monkeypatch):
    tokens = []
    put = app.q.put
    monkeypatch.setattr(app.q, "put", tokens.append)
    j = app.create_job("synthetic.pdf", DATA, OPT)
    assert app.cancel_job(j) and app.retry_job(j)
    assert len(tokens) == 2
    runs = []
    command = lambda job: runs.append(job.id) or [sys.executable, "-c", "import sys; sys.exit(7)"]
    monkeypatch.setattr(app, "command", command)
    drained = threading.Event()
    get = app.q.get

    def observed_get():
        token = get()
        if token[1] == -1:
            drained.set()
        return token

    monkeypatch.setattr(app.q, "get", observed_get)
    put(tokens[0])
    put(tokens[1])
    put(tokens[0])
    put((j.id, -1))
    assert drained.wait(10)
    assert runs == [j.id] and j.exit_code == 7


@pytest.mark.parametrize("counts", [(7, 0), (5, 2)])
def test_whole_rejection_stays_visible(app, tmp_path, counts):
    j = webapp.Job("0000000000000001", "synthetic", OPT, tmp_path)
    j.work_dir = tmp_path / "report"
    j.work_dir.mkdir()
    j.structure_kind = "rejected"
    j.structure_reason = "翻訳対象が減るため不採用"
    report = {"structure": {"used": False, "accepted": counts[0], "rejected": counts[1], "reason": j.structure_reason}}
    (j.work_dir / "render_report.json").write_text(json.dumps(report), encoding="utf-8")
    app.read_structure_report(j)
    message = j.to_dict()["structure_message"]
    assert "全部使われませんでした" in message and "0件を反映" in message


def test_report_used_false_without_log_is_not_success(app, tmp_path):
    j = webapp.Job("0000000000000001", "synthetic", OPT, tmp_path)
    j.work_dir = tmp_path / "report"
    j.work_dir.mkdir()
    report = {"structure": {"status": "rejected", "used": False, "accepted": 0, "rejected": 3, "reason": "全体検証"}}
    (j.work_dir / "render_report.json").write_text(json.dumps(report), encoding="utf-8")
    app.read_structure_report(j)
    assert "全部使われませんでした" in j.to_dict()["structure_message"]


def test_previous_report_is_not_used_for_current_failure(app, tmp_path):
    j = webapp.Job("0000000000000001", "synthetic", OPT, tmp_path)
    j.work_dir = tmp_path / "report"
    j.work_dir.mkdir()
    path = j.work_dir / "render_report.json"
    path.write_text(json.dumps({"structure": {"accepted": 2, "rejected": 5}}), encoding="utf-8")
    stat = path.stat()
    j.structure_report_stamp = (stat.st_mtime_ns, stat.st_size)
    app.read_structure_report(j)
    assert j.structure_counts is None and j.to_dict()["structure_message"] is None


def test_join_adoption_is_partial_even_when_all_role_changes_rejected(app, tmp_path):
    j = webapp.Job("0000000000000001", "synthetic", OPT, tmp_path)
    j.work_dir = tmp_path / "report"
    j.work_dir.mkdir()
    j.structure_counts = (0, 1)  # ログのrole件数を先に取得している
    path = j.work_dir / "render_report.json"
    path.write_text(json.dumps({"structure": {"used": True, "status": "partial", "accepted": 0, "rejected": 1}}), encoding="utf-8")
    app.read_structure_report(j)
    assert "一部使われませんでした" in j.to_dict()["structure_message"]


def test_join_adoption_status_survives_failure_before_render(app, monkeypatch):
    code = "print('[構造状態] partial'); print('[構造] 反映 0 件 / 不採用 1 件'); import sys; sys.exit(7)"
    monkeypatch.setattr(app, "command", lambda j: [sys.executable, "-c", code])
    j = app.create_job("synthetic.pdf", DATA, OPT)
    assert wait_for(lambda: j.status == "failed")
    assert "一部使われませんでした" in j.to_dict()["structure_message"]


def test_delayed_cancel_cannot_stop_new_retry(app, tmp_path, monkeypatch):
    finish_old = tmp_path / "finish-old"
    started = tmp_path / "started"
    old = ("from pathlib import Path; import time; "
           f"Path({str(started)!r}).write_text('started'); "
           f"\nwhile not Path({str(finish_old)!r}).exists(): time.sleep(.02)")
    monkeypatch.setattr(app, "command", lambda j: [sys.executable, "-c", old])
    j = app.create_job("synthetic.pdf", DATA, OPT)
    assert wait_for(started.exists)
    entered, release, retried = threading.Event(), threading.Event(), threading.Event()
    stop = app.stop_job

    def delayed_stop(job):
        entered.set()
        assert release.wait(10)
        return stop(job)

    monkeypatch.setattr(app, "stop_job", delayed_stop)
    cancelled = threading.Thread(target=lambda: app.cancel_job(j))
    cancelled.start()
    try:
        assert entered.wait(10)
        finish_old.write_text("finish", encoding="utf-8")
        assert wait_for(lambda: j.status == "failed")
        assert j.exit_code == 130
        monkeypatch.setattr(app, "command", lambda j: [sys.executable, "-c", "print('resumed')"])
        result = []

        def retry():
            result.append(app.retry_job(j))
            retried.set()

        retrying = threading.Thread(target=retry)
        retrying.start()
        assert not retried.wait(.1)  # 旧試行の停止が終わるまで新試行を登録しない
    finally:
        release.set()
    cancelled.join(10)
    retrying.join(10)
    assert not cancelled.is_alive() and not retrying.is_alive() and result == [True]
    assert wait_for(lambda: j.status == "failed" and j.exit_code == 0)
    assert not j.cancelled and "resumed" in j.log


@pytest.mark.parametrize("line", [
    "[構造] 反映 7 件 / 不採用 3 件",
    "[info] structure: role の変更 採用 7 件 / 却下 3 件 (翻訳する/しないを変える変更は手がかりと照合)",
])
def test_partial_rejection_visible_before_render_failure(app, monkeypatch, line):
    code = f"import sys; print({line!r}); sys.exit(7)"
    monkeypatch.setattr(app, "command", lambda j: [sys.executable, "-c", code])
    j = app.create_job("synthetic.pdf", DATA, OPT)
    assert wait_for(lambda: j.status == "failed")
    assert j.exit_code == 7
    message = j.to_dict()["structure_message"]
    assert "一部使われませんでした" in message and "7件を反映、3件は不採用" in message
