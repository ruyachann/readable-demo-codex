"""Codex レビュー (CR-01〜04, 08) とビルド allowlist の再現テスト。Gemini・Claude は呼ばない。"""
import hashlib
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path

import pytest

from readable import webapp

ROOT = Path(webapp.ROOT)
TREE_SCRIPT = (
    "import os, subprocess, sys, time, pathlib\n"
    "d = pathlib.Path(sys.argv[1]); depth = int(sys.argv[2])\n"
    "(d / f'pid{depth}').write_text(str(os.getpid()))\n"
    "if depth < 2:\n"
    "    subprocess.Popen([sys.executable, __file__, str(d), str(depth + 1)])\n"
    "time.sleep(120)\n")


def multipart(fn, data, fields=None):
    b = "----t" + uuid.uuid4().hex
    out = b""
    for k, v in (fields or {}).items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    out += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{fn}"\r\n'
            "Content-Type: application/pdf\r\n\r\n").encode() + data + b"\r\n" + f"--{b}--\r\n".encode()
    return out, f"multipart/form-data; boundary={b}"


def new_app(tmp_path, name="a", **kw):
    app = webapp.App(tmp_path / "jobs", tmp_path / "work", translator="dummy", home=tmp_path / f"home-{name}", **kw)
    app.settings.set_consent()
    return app


@pytest.fixture()
def apps():
    made = []
    yield made
    for app in made:
        app.stop_all()
        app.instance.release()


def serve(app, port=19300):
    server = webapp.make_server(app, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def post(app, server, data, fn="a.pdf", **fields):
    body, ct = multipart(fn, data, fields)
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=60)
    c.request("POST", "/api/jobs", body=body, headers={"X-Readable-Token": app.token, "Content-Type": ct})
    r = c.getresponse()
    out = json.loads(r.read())
    c.close()
    return r.status, out


def wait_for(cond, t=30):
    for _ in range(int(t * 20)):
        if cond():
            return True
        time.sleep(0.05)
    raise AssertionError("timeout")


# ---------------- CR-01 ----------------
def test_cr01_second_instance_keeps_first_files(tmp_path, apps):
    a = new_app(tmp_path)
    apps.append(a)
    job = a.create_job("active.pdf", b"%PDF-x", {"mode": "both", "claude": False, "retry": False})
    (job.dir / "out").mkdir()
    out = job.dir / "out" / "active_ja.pdf"
    out.write_bytes(b"%PDF-out")
    a.instance.publish(4321, "http://127.0.0.1:4321/?token=abc")
    with pytest.raises(webapp.AlreadyRunning) as e:
        webapp.App(tmp_path / "jobs", tmp_path / "work", "dummy", tmp_path / "home-b")
    assert e.value.info["url"] == "http://127.0.0.1:4321/?token=abc"
    assert (job.dir / "in" / "active.pdf").exists() and out.exists() and job.id in a.jobs


def test_cr01_leftovers_removed_only_after_owner_gone(tmp_path, apps):
    a = new_app(tmp_path)
    leftover = a.jobs_dir / "deadbeefdeadbeef" / "in"
    leftover.mkdir(parents=True)
    (leftover / "x.pdf").write_bytes(b"%PDF")
    a.instance.release()  # 先行アプリが終了した
    b = webapp.App(tmp_path / "jobs", tmp_path / "work", "dummy", tmp_path / "home-b")
    apps.append(b)
    assert not leftover.exists()


def test_cr01_dead_pid_in_info_does_not_block(tmp_path, apps):
    d = tmp_path / "jobs"
    d.mkdir()
    (d / webapp.INFO_NAME).write_text(json.dumps({"pid": 2 ** 22 + 12345, "port": 1, "url": "http://127.0.0.1:1/"}))
    b = webapp.App(d, tmp_path / "work", "dummy", tmp_path / "h")
    apps.append(b)


def test_cr01_second_process_prints_url_and_exits_3(tmp_path, apps):
    a = new_app(tmp_path)
    apps.append(a)
    a.instance.publish(5555, "http://127.0.0.1:5555/?token=zzz")
    r = subprocess.run([sys.executable, "-m", "readable.webapp", "--no-browser", "--translator", "dummy", "--data-dir",
                        str(tmp_path / "jobs"), "--work-dir", str(tmp_path / "work"), "--home", str(tmp_path / "h2")],
                       cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=60,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert r.returncode == webapp.EXIT_ALREADY_RUNNING == 3
    assert "http://127.0.0.1:5555/?token=zzz" in r.stdout


# ---------------- CR-02 ----------------
def test_cr02_concurrent_posts_register_one_and_never_cancel(tmp_path, apps, monkeypatch):
    a = new_app(tmp_path)
    apps.append(a)
    monkeypatch.setattr(a, "command", lambda job: [sys.executable, "-c", "import time; time.sleep(60)"])
    orig = a.create_job
    started = threading.Event()

    def slow_create(*args, **kw):
        started.set()
        time.sleep(0.6)  # 確認 → 登録 の間に他の要求が割り込める隙間
        return orig(*args, **kw)

    monkeypatch.setattr(a, "create_job", slow_create)
    server = serve(a)
    results = []

    def go(n):
        results.append(post(a, server, b"%PDF-1.4\n" + bytes([n]), fn=f"f{n}.pdf"))  # replace なし

    ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    ts[0].start()
    started.wait(5)
    ts[1].start()
    for t in ts:
        t.join(30)
    codes = sorted(r[0] for r in results)
    assert codes == [201, 409]
    assert len(a.jobs) == 1
    winner = next(r[1]["id"] for r in results if r[0] == 201)
    assert winner in a.jobs and not a.jobs[winner].deleted  # 後続が replace なしで先行を取り消していない
    wait_for(lambda: a.jobs[winner].proc is not None)
    assert a.jobs[winner].proc.poll() is None
    server.shutdown()
    server.server_close()


# ---------------- CR-03 ----------------
def _alive(pid):
    return webapp.pid_alive(pid)


def _run_tree(tmp_path, apps, monkeypatch, use_job_object):
    a = new_app(tmp_path)
    apps.append(a)
    script = tmp_path / "tree.py"
    script.write_text(TREE_SCRIPT, encoding="utf-8")
    pdir = tmp_path / "pids"
    pdir.mkdir()
    monkeypatch.setattr(a, "command", lambda job: [sys.executable, str(script), str(pdir), "0"])
    if not use_job_object:
        monkeypatch.setattr(webapp, "create_job_object", lambda: None)
    job = a.create_job("t.pdf", b"%PDF-x", {"mode": "both", "claude": False, "retry": False})
    wait_for(lambda: all((pdir / f"pid{i}").exists() for i in range(3)), 40)
    pids = [int((pdir / f"pid{i}").read_text()) for i in range(3)]
    assert all(_alive(p) for p in pids)
    assert (job.jobobj is not None) == (use_job_object and os.name == "nt")
    t0 = time.time()
    a.delete_job(job)
    wait_for(lambda: not any(_alive(p) for p in pids), 15)
    assert time.time() - t0 < 15


def test_cr03_cancel_kills_parent_child_grandchild_job_object(tmp_path, apps, monkeypatch):
    _run_tree(tmp_path, apps, monkeypatch, True)


def test_cr03_cancel_fallback_taskkill_tree(tmp_path, apps, monkeypatch):
    _run_tree(tmp_path, apps, monkeypatch, False)


def test_cr03_shutdown_stops_tree(tmp_path, apps, monkeypatch):
    a = new_app(tmp_path)
    apps.append(a)
    pdir = tmp_path / "pids"
    pdir.mkdir()
    script = tmp_path / "tree.py"
    script.write_text(TREE_SCRIPT, encoding="utf-8")
    monkeypatch.setattr(a, "command", lambda job: [sys.executable, str(script), str(pdir), "0"])
    a.create_job("t.pdf", b"%PDF-x", {"mode": "both", "claude": False, "retry": False})
    wait_for(lambda: all((pdir / f"pid{i}").exists() for i in range(3)), 40)
    pids = [int((pdir / f"pid{i}").read_text()) for i in range(3)]
    a.stop_all()
    wait_for(lambda: not any(_alive(p) for p in pids), 15)


# ---------------- CR-04 ----------------
def test_cr04_renamed_copy_protects_actual_cache_on_exit7(tmp_path, apps, monkeypatch):
    a = new_app(tmp_path)
    apps.append(a)
    content = b"%PDF-same-synthetic-content"
    suffix = hashlib.sha256(content).hexdigest()[:12]
    wroot = a.cache_root()
    old = wroot / ("old-" + suffix)
    old.mkdir(parents=True)
    (old / "translation_cache.json").write_text("{}")
    os.utime(old / "translation_cache.json", (1, 1))
    os.utime(old, (1, 1))
    for i in range(3):
        r = wroot / (f"recent{i}-" + str(i) * 12)
        r.mkdir()
        (r / "translation_cache.json").write_text("{}")
    monkeypatch.setattr(a, "command", lambda job: [sys.executable, "-c", "import sys; sys.exit(7)"])
    job = a.create_job("new.pdf", content, {"mode": "both", "claude": False, "retry": False})
    wait_for(lambda: job.status in ("done", "failed"))
    assert job.exit_code == 7
    assert job.work_dir == old  # CLI が実際に使う場所をジョブが保持
    assert old.exists() and (old / webapp.RESUME_MARK).exists()


def test_cr04_cache_root_follows_cli_rules(tmp_path):
    from readable.cli import work_root
    from readable.config import load_config
    assert webapp.resolve_cache_root(None) == work_root(load_config(None), None)
    assert webapp.resolve_cache_root(tmp_path) == tmp_path


# ---------------- CR-08 (静的確認。実画面は手動確認) ----------------
def test_cr08_poll_does_not_replace_whole_job_area():
    html = (ROOT / "readable" / "web_static" / "index.html").read_text(encoding="utf-8")
    assert "jobs.map" not in html or "box.innerHTML=jobs.map" not in html
    assert "clearTimeout" in html and "scheduleNext" in html  # 完了後はポーリングを止める


# ---------------- build_dist allowlist ----------------
def _bd():
    sys.path.insert(0, str(ROOT / "tools"))
    import build_dist
    return build_dist


def test_build_allowlist_excludes_dev_files(tmp_path):
    bd = _bd()
    for rel in ("_patch1.py", "pytest.ini", "CODEX_REVIEW_CLAUDE.md", "docs/codex-review/x.json", "website/index.html",
                "scratch_dbg.py", "docs/PROGRESS.md", "docs/response_parts/webapp.md", "readable/__pycache__/a.pyc",
                "readable/_patch2.py", "tests/test_x.py", "work/a.json", "requirements-dev.txt"):
        assert not bd.is_allowed(Path(rel)), rel
    for rel in ("readable/webapp.py", "readable/web_static/index.html", "prompts/claude_structure.md", "setup.bat",
                "config.toml", "docs/INSTALL.md", "docs/API.md", "README.md", "bat_messages/setup_done.txt"):
        assert bd.is_allowed(Path(rel)), rel
    z = bd.build(tmp_path, "20000101")
    names = [n[len("ReadableJP/"):] for n in zipfile.ZipFile(z).namelist()]
    assert names and all(bd.is_allowed(Path(n)) for n in names)


def test_verify_rejects_non_allowlisted_entries(tmp_path):
    bd = _bd()
    p = tmp_path / "x.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("ReadableJP/readable/ok.py", "x=1")
        zf.writestr("ReadableJP/_patch1.py", "x=1")
    with pytest.raises(bd.DistError, match="_patch1"):
        bd.verify_zip(p)
