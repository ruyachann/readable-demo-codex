"""初回セットアップ (キー保存・マスク・優先順位・同意・トークン保護) と配布 zip の検査のテスト。"""
import http.client
import json
import threading
import zipfile

import pytest

from readable import webapp

FAKE_KEY = "AIzaSyTESTKEYtestkey0123456789abcdefgh"


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    app = webapp.App(tmp_path / "jobs", tmp_path / "work", translator="dummy", home=tmp_path / "home", structure_provider="claude")
    server = webapp.make_server(app, 18900)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield app, server.server_address[1]
    server.shutdown()
    server.server_close()
    app.instance.release()


def call(srv, method, path, obj=None, token=True, extra=None):
    app, port = srv
    body = json.dumps(obj).encode() if obj is not None else None
    h = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"}
    if token:
        h["X-Readable-Token"] = app.token
    h.update(extra or {})
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in h.items():
        c.putheader(k, v)
    c.putheader("Content-Length", str(len(body or b"")))
    c.endheaders(body)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def test_setup_status_shape(srv):
    st, data = call(srv, "GET", "/api/setup")
    j = json.loads(data)
    assert st == 200
    assert j["python"]["ok"] is True and j["python"]["version"].count(".") == 2
    assert {"packages", "fonts", "gemini", "claude", "consent", "ready"} <= set(j)
    assert "codex" not in j
    assert j["gemini"]["configured"] is False and j["consent"] is False and j["ready"] is False
    assert j["packages"]["ok"] is True  # このテスト環境には導入済み


def test_missing_packages_detected(monkeypatch, tmp_path):
    monkeypatch.setattr(webapp, "_find_spec", lambda m: None if m == "fitz" else object())
    st = webapp.setup_status(webapp.Settings(tmp_path), fast=True)
    assert st["packages"] == {"ok": False, "missing": ["PyMuPDF"]}
    assert st["ready"] is False


def test_key_save_mask_delete(srv):
    app, _ = srv
    assert call(srv, "POST", "/api/setup/key", {"key": "short"})[0] == 400
    assert call(srv, "POST", "/api/setup/key", {"key": "bad key with spaces and symbols!!!!!!"})[0] == 400
    st, data = call(srv, "POST", "/api/setup/key", {"key": FAKE_KEY})
    assert st == 200 and FAKE_KEY.encode() not in data
    g = json.loads(data)["gemini"]
    assert g["configured"] and g["source"] == "file" and g["masked"].endswith(FAKE_KEY[-4:]) and FAKE_KEY[:8] not in g["masked"]
    stored = json.loads((app.settings.home / "secrets.json").read_text(encoding="utf-8"))
    assert stored["gemini_api_key"] == FAKE_KEY
    for path in ("/api/setup", "/api/jobs"):  # どの API にもキー全体は出ない
        assert FAKE_KEY.encode() not in call(srv, "GET", path)[1]
    st, data = call(srv, "DELETE", "/api/setup/key")
    assert st == 200 and json.loads(data)["gemini"]["configured"] is False


def test_env_key_takes_precedence(srv, monkeypatch):
    app, _ = srv
    app.settings.set_key(FAKE_KEY)
    assert app.settings.get_key() == (FAKE_KEY, "file")
    monkeypatch.setenv("GEMINI_API_KEY", "ENVKEY_" + "x" * 30)
    key, src = app.settings.get_key()
    assert src == "env" and key.startswith("ENVKEY_")
    g = json.loads(call(srv, "GET", "/api/setup")[1])["gemini"]
    assert g["source"] == "env" and "ENVKEY" not in json.dumps(g)


def test_key_test_endpoint_uses_stub(srv, monkeypatch):
    calls = []
    monkeypatch.setattr(webapp, "check_gemini_key", lambda k: (calls.append(k) or (True, "ok")))
    assert call(srv, "POST", "/api/setup/test-key", {})[0] == 400  # 未設定
    srv[0].settings.set_key(FAKE_KEY)
    st, data = call(srv, "POST", "/api/setup/test-key", {})
    assert st == 200 and json.loads(data)["ok"] is True and calls == [FAKE_KEY]


def test_consent_required_for_jobs(srv):
    app, _ = srv
    st, _ = call(srv, "POST", "/api/jobs", {})  # 同意前
    assert st == 403
    assert call(srv, "POST", "/api/setup/consent", {"agree": False})[0] == 400
    assert call(srv, "POST", "/api/setup/consent", {"agree": True})[0] == 200
    assert app.settings.consented()


def test_gemini_translator_needs_key(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    app = webapp.App(tmp_path / "jobs", None, translator="gemini", home=tmp_path / "h")
    app.settings.set_consent()
    server = webapp.make_server(app, 18950)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        st, data = call((app, server.server_address[1]), "POST", "/api/jobs", {})
        assert st == 400 and "キー" in json.loads(data)["error"]
    finally:
        server.shutdown()
        server.server_close()
        app.stop_all()
        app.instance.release()


def test_job_env_gets_key_only_via_env(srv, monkeypatch):
    app, _ = srv
    app.settings.set_key(FAKE_KEY)
    cmd = app.command(webapp.Job("a" * 16, "x", {"mode": "both", "claude": False, "retry": False}, app.jobs_dir / "x"))
    assert FAKE_KEY not in " ".join(cmd)


def test_setup_endpoints_need_token_host_origin(srv):
    for method, path, body in [("GET", "/api/setup", None), ("POST", "/api/setup/key", {"key": FAKE_KEY}),
                               ("POST", "/api/setup/consent", {"agree": True}), ("POST", "/api/setup/test-key", {}),
                               ("DELETE", "/api/setup/key", None)]:
        assert call(srv, method, path, body, token=False)[0] == 401, path
        assert call(srv, method, path, body, extra={"Host": "evil.example.com"})[0] == 403, path
    assert call(srv, "POST", "/api/setup/key", {"key": FAKE_KEY}, extra={"Origin": "http://evil.example.com"})[0] == 403
    assert not (srv[0].settings.home / "secrets.json").exists()
    assert not srv[0].settings.consented()


def test_secrets_file_location_default(monkeypatch, tmp_path):
    monkeypatch.delenv("READABLEJP_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert webapp.default_home() == tmp_path / "ReadableJP"


# ---- 配布 zip ----
def test_build_dist_contents(tmp_path):
    import sys
    sys.path.insert(0, str(webapp.ROOT / "tools"))
    import build_dist
    z = build_dist.build(tmp_path, "20000101")
    names = zipfile.ZipFile(z).namelist()
    assert all(n.startswith("ReadableJP/") for n in names)
    assert "ReadableJP/docs/INSTALL.md" in names and "ReadableJP/readable/webapp.py" in names
    assert "ReadableJP/setup.bat" in names and "ReadableJP/requirements.txt" in names
    bad = ("/work/", "/out/", "/english_paper/", "/tests/", "/.venv/", "__pycache__", ".pdf", "secrets.json", "PROGRESS")
    assert not [n for n in names if any(b in n for b in bad)], names


def test_verify_zip_detects_secrets_and_pdfs(tmp_path):
    import sys
    sys.path.insert(0, str(webapp.ROOT / "tools"))
    import build_dist

    def make(name, files):
        p = tmp_path / name
        with zipfile.ZipFile(p, "w") as z:
            for k, v in files.items():
                z.writestr(k, v)
        return p

    build_dist.verify_zip(make("ok.zip", {"ReadableJP/readable/a.py": "print(1)"}))
    with pytest.raises(build_dist.DistError):
        build_dist.verify_zip(make("k.zip", {"ReadableJP/readable/a.py": f'KEY = "{FAKE_KEY}"'}))
    with pytest.raises(build_dist.DistError):
        build_dist.verify_zip(make("s.zip", {"ReadableJP/secrets.json": "{}"}))
    with pytest.raises(build_dist.DistError):
        build_dist.verify_zip(make("p.zip", {"ReadableJP/english_paper/x.pdf": "%PDF"}))
    with pytest.raises(build_dist.DistError):
        build_dist.verify_zip(make("e.zip", {"ReadableJP/readable/a.py": "mykey-1234567890"}), extra_secrets=["mykey-1234567890"])


def test_build_refuses_when_secret_in_tree(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(webapp.ROOT / "tools"))
    import build_dist
    root = tmp_path / "proj"
    (root / "readable").mkdir(parents=True)
    (root / "prompts").mkdir()
    (root / "bat_messages").mkdir()
    (root / "docs").mkdir()
    (root / "docs" / "INSTALL.md").write_text("x", encoding="utf-8")
    (root / "readable" / "leak.py").write_text(f'K = "{FAKE_KEY}"', encoding="utf-8")
    with pytest.raises(build_dist.DistError):
        build_dist.build(tmp_path / "dist", "20000101", root=root)
    assert not list((tmp_path / "dist").glob("*.zip"))
