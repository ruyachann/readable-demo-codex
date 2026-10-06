"""Integration checks for edition selection and shared structure caches."""
import copy
import json
from unittest.mock import patch

from readable.config import Config
from readable import structure, webapp, cli


def synthetic_doc():
    return {"num_pages": 1, "body_size": 10, "joins": [], "pages": [{"number": 1, "frames": [
        {"id": "f1", "text": "Study title", "html": "Study title", "role": "title", "translate": True,
         "size": 18, "bbox": [10, 10, 100, 30]}]}]}


def test_structure_cache_is_separated_by_provider_and_model(tmp_path):
    calls = []
    def invoke(system, text, cfg, **kwargs):
        calls.append(cfg.get("structure", "provider"))
        return {"roles": {}, "joins_add": [], "joins_remove": [], "charmap": {}}, {}
    with patch.dict(structure.PROVIDERS, {"claude": invoke, "codex": invoke}):
        for provider in ["claude", "claude", "codex", "codex"]:
            structure.run_structure(copy.deepcopy(synthetic_doc()), tmp_path, Config({"structure": {"provider": provider}}))
        structure.run_structure(copy.deepcopy(synthetic_doc()), tmp_path,
                                Config({"structure": {"provider": "codex"}, "codex": {"model": "different-model"}}))
    assert calls == ["claude", "codex", "codex"]
    saved = json.loads((tmp_path / "structure.json").read_text())
    assert saved["provider"] == "codex" and saved["model"] == "different-model"


def test_cli_generic_disable_keeps_legacy_alias():
    assert cli.build_parser().parse_args(["x.pdf", "--no-structure"]).no_claude
    assert cli.build_parser().parse_args(["x.pdf", "--no-claude"]).no_claude
    assert cli.build_parser().parse_args(["x.pdf", "--structure-provider", "codex"]).structure_provider == "codex"


def test_web_command_selects_edition_and_optional_disable(tmp_path):
    with patch.object(webapp.App, "worker", lambda self: None):
        app = webapp.App(tmp_path / "jobs", tmp_path / "work", "dummy", tmp_path / "home", "codex")
        try:
            job = app.create_job("a.pdf", b"%PDF-synthetic", {"mode": "both", "claude": True, "retry": False})
            command = app.command(job)
            assert command[command.index("--structure-provider") + 1] == "codex"
            assert "--no-claude" not in command
            job.options["claude"] = False
            assert "--no-claude" in app.command(job)
        finally:
            app.instance.release()


def test_consent_is_checked_again_when_ai_destination_changes(tmp_path):
    claude = webapp.Settings(tmp_path, "claude")
    codex = webapp.Settings(tmp_path, "codex")
    claude.set_consent()
    assert claude.consented() and not codex.consented()
    codex.set_consent()
    assert codex.consented() and claude.consented()


def test_two_settings_instances_preserve_both_consents_on_concurrent_save(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    claude = webapp.Settings(tmp_path, "claude")
    codex = webapp.Settings(tmp_path, "codex")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda settings: settings.set_consent(), [claude, codex]))
    assert claude.consented() and codex.consented()
    assert not list(tmp_path.glob("*.tmp"))


def test_two_editions_use_independent_cookies_in_same_browser(tmp_path):
    import http.client
    import threading
    servers = []
    with patch.object(webapp.App, "worker", lambda self: None):
        try:
            for provider in ["claude", "codex"]:
                app = webapp.App(tmp_path / provider / "jobs", tmp_path / provider / "work", "dummy",
                                 tmp_path / "home", provider)
                server = webapp.make_server(app, 0)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                servers.append((app, server))
            cookies = []
            for app, server in servers:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
                conn.request("GET", "/?token=" + app.token)
                response = conn.getresponse()
                assert response.status == 303
                cookies.append(response.getheader("Set-Cookie").split(";")[0])
                response.read(); conn.close()
            assert cookies[0].split("=")[0] != cookies[1].split("=")[0]
            for app, server in servers:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
                conn.request("GET", "/api/jobs", headers={"Cookie": "; ".join(cookies)})
                response = conn.getresponse()
                assert response.status == 200
                response.read(); conn.close()
        finally:
            for app, server in servers:
                server.shutdown(); server.server_close(); app.instance.release()
