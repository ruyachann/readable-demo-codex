"""ローカル Web アプリ (readable.webapp) のテスト。dummy 翻訳器で全工程を通す。"""
import http.client
import json
import threading
import time
import uuid

import pytest

from conftest import JSR, ROOT
from readable import webapp


def multipart(files, fields=None):
    b = "----t" + uuid.uuid4().hex
    out = b""
    for k, v in (fields or {}).items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for fn, data in files:
        out += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{fn}"\r\n'
                "Content-Type: application/pdf\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{b}--\r\n".encode()
    return out, f"multipart/form-data; boundary={b}"


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    base = tmp_path_factory.mktemp("web")
    app = webapp.App(base / "jobs", base / "work", translator="dummy", home=base / "home")
    app.settings.set_consent()
    server = webapp.make_server(app, 18765)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield app, server.server_address[1]
    server.shutdown()
    server.server_close()
    app.instance.release()


def req(srv, method, path, body=None, headers=None, token=True, host=None):
    app, port = srv
    h = {"Host": host or f"127.0.0.1:{port}"}
    if token:
        h["X-Readable-Token"] = app.token
    h.update(headers or {})
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in h.items():
        c.putheader(k, v)
    if body is not None:
        c.putheader("Content-Length", str(len(body)))
    c.endheaders(body)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data, r


def post_pdf(srv, name="a.pdf", data=b"%PDF-1.4\n", **kw):
    body, ct = multipart([(name, data)], {"mode": "both"})
    return req(srv, "POST", "/api/jobs", body, {"Content-Type": ct}, **kw)


def test_full_job_and_download(srv):
    if not JSR.exists():
        pytest.skip("sample PDF missing")
    st, data, _ = post_pdf(srv, "paper one.pdf", JSR.read_bytes())
    assert st == 201
    jid = json.loads(data)["id"]
    for _ in range(300):
        st, data, _ = req(srv, "GET", f"/api/jobs/{jid}")
        j = json.loads(data)
        if j["status"] in ("done", "failed"):
            break
        time.sleep(1)
    assert j["status"] == "done", j["log_tail"]
    assert j["exit_code"] == 0 and j["message_ja"]
    assert set(j["outputs"]) == {"ja", "dual"}
    for k in ("ja", "dual"):
        st, pdf, r = req(srv, "GET", f"/api/jobs/{jid}/download/{k}")
        assert st == 200 and pdf.startswith(b"%PDF")
        assert "attachment" in r.getheader("Content-Disposition")
    assert any(x["id"] == jid for x in json.loads(req(srv, "GET", "/api/jobs")[1])["jobs"])
    st, _, _ = req(srv, "DELETE", f"/api/jobs/{jid}")
    assert st == 200
    assert req(srv, "GET", f"/api/jobs/{jid}")[0] == 404
    assert not (srv[0].jobs_dir / jid).exists()


def test_no_token_rejected(srv):
    assert req(srv, "GET", "/api/jobs", token=False)[0] == 401
    assert post_pdf(srv, token=False)[0] == 401
    assert req(srv, "GET", "/", token=False)[0] == 401
    assert req(srv, "GET", "/api/jobs", token=False, headers={"X-Readable-Token": "wrong"})[0] == 401


def test_token_url_sets_cookie_and_serves_ui(srv):
    app, _ = srv
    st, _, r = req(srv, "GET", f"/?token={app.token}", token=False)
    assert st == 303 and "HttpOnly" in r.getheader("Set-Cookie")
    cookie = r.getheader("Set-Cookie").split(";")[0]
    st, html, _ = req(srv, "GET", "/", token=False, headers={"Cookie": cookie})
    assert st == 200 and "<html" in html.decode("utf-8")
    assert app.token not in html.decode("utf-8")


def test_bad_host_rejected(srv):
    assert req(srv, "GET", "/api/jobs", host="evil.example.com")[0] == 403
    assert req(srv, "GET", "/api/jobs", host="127.0.0.1:1")[0] == 403
    assert req(srv, "GET", "/", host="evil.example.com", token=False)[0] == 403


def test_bad_origin_rejected(srv):
    _, port = srv
    body, ct = multipart([("a.pdf", b"%PDF-1.4")])
    st, _, _ = req(srv, "POST", "/api/jobs", body, {"Content-Type": ct, "Origin": "http://evil.example.com"})
    assert st == 403
    st, _, _ = req(srv, "DELETE", "/api/jobs/" + "0" * 16, headers={"Origin": "null"})
    assert st == 403


def test_non_pdf_rejected(srv):
    st, data, _ = post_pdf(srv, "x.pdf", b"hello, not a pdf")
    assert st == 400 and "PDF" in json.loads(data)["error"]


def test_too_large_rejected(srv, monkeypatch):
    monkeypatch.setattr(webapp, "MAX_UPLOAD", 1000)
    st, _, _ = post_pdf(srv, "big.pdf", b"%PDF" + b"0" * 5000)  # 上限内の外枠 (+1MB) だが本体は超過
    assert st == 413
    monkeypatch.setattr(webapp, "MAX_UPLOAD", 10)
    body = b"x" * (2 * 1024 * 1024)  # Content-Length 自体が上限超過
    try:
        st, _, _ = req(srv, "POST", "/api/jobs", body, {"Content-Type": "multipart/form-data; boundary=x"})
    except OSError:  # 本体を読まずに閉じるため、送信中に切断されることがある (拒否された)
        st = 413
    assert st == 413


def test_path_traversal(srv):
    jobs_before = set(p.name for p in srv[0].jobs_dir.iterdir())
    st, data, _ = post_pdf(srv, "..\\..\\..\\evil.pdf", b"%PDF-1.4\n")
    assert st == 201
    jid = json.loads(data)["id"]
    d = srv[0].jobs_dir / jid
    assert (d / "in" / "evil.pdf").exists()
    assert not (srv[0].jobs_dir.parent / "evil.pdf").exists()
    for bad in ("../x", "..%2F..%2Fx", "%2e%2e/%2e%2e", "0" * 15 + "/../..", "download"):
        assert req(srv, "GET", f"/api/jobs/{bad}")[0] == 404
        assert req(srv, "GET", f"/api/jobs/{jid}/download/{bad}")[0] == 404
    assert req(srv, "GET", "/../../etc/passwd")[0] in (404, 401)
    req(srv, "DELETE", f"/api/jobs/{jid}")
    assert jobs_before <= set(p.name for p in srv[0].jobs_dir.iterdir()) | {jid}


def test_sanitize_and_messages():
    assert webapp.sanitize_stem("a/b\\c<>:d.pdf") == "c___d"
    assert webapp.sanitize_stem("..") == "document"
    assert webapp.sanitize_stem("CON.pdf") == "document"
    for code in (4, 7, 11, 12):
        assert webapp.exit_message(code)
    assert "日次上限" in webapp.exit_message(7)
    assert "16" not in webapp.exit_message(7)
    assert "99" in webapp.exit_message(99)
