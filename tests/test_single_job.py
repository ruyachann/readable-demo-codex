"""最新 1 件だけ保持・実行中の置き換え確認・翻訳キャッシュ 3 文書上限・exit 7 の保持のテスト。"""
import http.client
import json
import os
import sys
import threading
import time
import uuid

import pytest

from readable import webapp


def multipart(files, fields=None):
    b = "----t" + uuid.uuid4().hex
    out = b""
    for k, v in (fields or {}).items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for fn, data in files:
        out += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{fn}"\r\n'
                "Content-Type: application/pdf\r\n\r\n").encode() + data + b"\r\n"
    return out + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


def make(tmp_path, monkeypatch, cmd=None):
    app = webapp.App(tmp_path / "jobs", tmp_path / "work", translator="dummy", home=tmp_path / "home")
    app.settings.set_consent()
    if cmd:
        monkeypatch.setattr(app, "command", lambda job: cmd)
    server = webapp.make_server(app, 19100)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return app, server


def call(app, server, method, path, body=None, ctype=None):
    port = server.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    h = {"X-Readable-Token": app.token}
    if ctype:
        h["Content-Type"] = ctype
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, json.loads(data) if data[:1] == b"{" else data


def post(app, server, files=None, **fields):
    body, ct = multipart(files or [("a.pdf", b"%PDF-1.4\n" + uuid.uuid4().bytes)], fields)
    return call(app, server, "POST", "/api/jobs", body, ct)


SLEEP = [sys.executable, "-c", "import time; time.sleep(120)"]
QUICK = [sys.executable, "-c", "print('[1/4] extract: x')"]


def wait(app, jid, cond, t=30):
    for _ in range(int(t * 10)):
        j = app.jobs.get(jid)
        if j is None or cond(j):
            return j
        time.sleep(0.1)
    raise AssertionError("timeout")


@pytest.fixture()
def stop():
    servers = []
    yield servers
    for app, server in servers:
        for j in list(app.jobs.values()):
            app.delete_job(j)
        server.shutdown()
        server.server_close()
        app.instance.release()


def test_only_latest_job_kept_and_files_deleted(tmp_path, monkeypatch, stop):
    app, server = make(tmp_path, monkeypatch, QUICK)
    stop.append((app, server))
    st, j1 = post(app, server)
    assert st == 201
    wait(app, j1["id"], lambda j: j.status in ("done", "failed"))
    d1 = app.jobs_dir / j1["id"]
    assert d1.exists()
    st, j2 = post(app, server)  # 完了後の次の翻訳は確認なしで前回を削除
    assert st == 201 and j2["id"] != j1["id"]
    assert not d1.exists()
    ids = [j["id"] for j in call(app, server, "GET", "/api/jobs")[1]["jobs"]]
    assert ids == [j2["id"]]
    assert call(app, server, "GET", f"/api/jobs/{j1['id']}")[0] == 404


def test_multiple_files_rejected(tmp_path, monkeypatch, stop):
    app, server = make(tmp_path, monkeypatch, QUICK)
    stop.append((app, server))
    st, j = post(app, server, files=[("a.pdf", b"%PDF-1"), ("b.pdf", b"%PDF-2")])
    assert st == 400 and not app.jobs


def test_busy_requires_replace_then_cancels_and_deletes(tmp_path, monkeypatch, stop):
    app, server = make(tmp_path, monkeypatch, SLEEP)
    stop.append((app, server))
    st, j1 = post(app, server)
    assert st == 201
    wait(app, j1["id"], lambda j: j.status == "running" and j.proc is not None)
    job1, d1 = app.jobs[j1["id"]], app.jobs_dir / j1["id"]
    st, j = post(app, server)  # 確認 (replace) なし
    assert st == 409 and j.get("busy") is True
    assert j1["id"] in app.jobs and d1.exists()
    monkeypatch.setattr(app, "command", lambda job: QUICK)
    st, j2 = post(app, server, replace="1")  # 「中止して新しく始める」
    assert st == 201
    assert j1["id"] not in app.jobs
    job1.proc.wait(timeout=20)  # 実行中だった処理は止められた
    for _ in range(100):
        if not d1.exists():
            break
        time.sleep(0.1)
    assert not d1.exists()
    wait(app, j2["id"], lambda j: j.status in ("done", "failed"))


def test_startup_clears_web_jobs(tmp_path):
    jobs = tmp_path / "jobs"
    (jobs / "old1" / "in").mkdir(parents=True)
    (jobs / "old1" / "in" / "a.pdf").write_bytes(b"%PDF")
    (jobs / "stray.txt").write_text("x")
    app = webapp.App(jobs, tmp_path / "work", home=tmp_path / "h")
    app.instance.release()
    assert [p.name for p in jobs.iterdir() if p.name not in (webapp.LOCK_NAME, webapp.INFO_NAME)] == []


def _mkdoc(root, name, age):
    d = root / name
    d.mkdir(parents=True)
    f = d / "translation_cache.json"
    f.write_text("{}")
    t = time.time() - age
    os.utime(f, (t, t))
    os.utime(d, (t, t))
    return d


def test_cache_cap_keeps_three_most_recent(tmp_path):
    w = tmp_path / "work"
    names = [f"doc{i}-{i:012x}" for i in range(5)]
    for i, n in enumerate(names):
        _mkdoc(w, n, age=1000 - i * 100)  # doc4 が最新
    (w / "web_jobs").mkdir()
    (w / ".quota_state.json").write_text("{}")
    (w / "notes").mkdir()
    removed = webapp.prune_work_cache(w, 3)
    assert sorted(removed) == sorted(names[:2])
    assert {p.name for p in w.iterdir() if p.is_dir()} == {names[2], names[3], names[4], "web_jobs", "notes"}
    assert (w / ".quota_state.json").exists()


def test_cache_cap_spares_resume_doc(tmp_path):
    w = tmp_path / "work"
    old = _mkdoc(w, "stuck-" + "a" * 12, age=9000)
    (old / webapp.RESUME_MARK).write_text("exit 7")
    for i in range(3):
        _mkdoc(w, f"new{i}-{i:012x}", age=10 + i)
    assert webapp.prune_work_cache(w, 3) == []
    assert old.exists()


def test_doc_cache_name_matches_cli(tmp_path):
    from readable.cli import work_name
    p = tmp_path / "paper x.pdf"
    p.write_bytes(b"%PDF-1.4 hello")
    assert webapp.doc_cache_name(p) == work_name(p)


def test_exit7_keeps_cache_and_success_clears_mark(tmp_path, monkeypatch, stop):
    app, server = make(tmp_path, monkeypatch, [sys.executable, "-c", "import sys; sys.exit(7)"])
    stop.append((app, server))
    pdf = b"%PDF-1.4\nsame"
    tmp_pdf = tmp_path / "p.pdf"
    tmp_pdf.write_bytes(pdf)
    doc = _mkdoc(app.work_dir, webapp.doc_cache_name(tmp_pdf), age=99999)  # 最も古い = 通常なら消される
    for i in range(4):
        _mkdoc(app.work_dir, f"n{i}-{i:012x}", age=10 + i)
    body, ct = multipart([("p.pdf", pdf)])
    st, j = call(app, server, "POST", "/api/jobs", body, ct)
    assert st == 201
    job = app.jobs[j["id"]]
    wait(app, j["id"], lambda x: x.status in ("done", "failed"))
    assert job.exit_code == 7 and "日次上限" in app.jobs[j["id"]].to_dict()["message_ja"]
    assert doc.exists() and (doc / webapp.RESUME_MARK).exists()
    # 同じファイルで成功 -> 印が外れ、通常の整理対象に戻る
    monkeypatch.setattr(app, "command", lambda job: [sys.executable, "-c", "import pathlib,sys;"
                        f"pathlib.Path(sys.argv[1]).mkdir(parents=True,exist_ok=True);"
                        "pathlib.Path(sys.argv[1],'x_ja.pdf').write_bytes(b'%PDF')", str(job.dir / "out")])
    st, j2 = call(app, server, "POST", "/api/jobs", *multipart([("p.pdf", pdf)]))
    wait(app, j2["id"], lambda x: x.status in ("done", "failed"))
    assert not (doc / webapp.RESUME_MARK).exists() or not doc.exists()
