"""Claude版の最新フローとCodex版の復旧経路。AI呼出しは全てモック。"""
import json
import sys

import pytest

from readable import codex_provider, webapp
from test_webapp_flow import call
from test_webapp_review import apps, new_app, serve, wait_for  # noqa: F401


@pytest.mark.parametrize("provider,env_name", [("codex", "OPENAI_BASE_URL"), ("codex", "OPENAI_API_KEY"), ("claude", "ANTHROPIC_BASE_URL")])
def test_guard_fix_names_environment_without_disclosing_value(monkeypatch, provider, env_name):
    for key in codex_provider.API_ENV:
        monkeypatch.delenv(key, raising=False)
    marker = "test-value-never-show-this"
    monkeypatch.setenv(env_name, marker)
    result = webapp.structure_status(provider, {provider: {"found": True, "logged_in": False}})
    assert result["usable"] is False
    assert env_name in result["reason"] and marker not in json.dumps(result)
    assert "環境変数を外して" in result["fix"]


@pytest.mark.parametrize("structure", [True, False])
def test_retry_can_recheck_structure_without_reattaching_pdf(tmp_path, apps, monkeypatch, structure):
    app = webapp.App(tmp_path / "jobs", tmp_path / "work/codex", translator="dummy", home=tmp_path / "home", structure_provider="codex")
    apps.append(app)
    app.settings.set_consent()
    monkeypatch.setattr(app, "command", lambda job: [sys.executable, "-c", "import sys; sys.exit(12)"])
    pdf_data = b"%PDF-1.4\nsynthetic"
    job = app.create_job("synthetic.pdf", pdf_data, {"mode": "both", "claude": not structure, "retry": False})
    wait_for(lambda: job.status in ("done", "failed"))
    app.q.put = lambda jid: None  # Inspect the queued command, without an AI subprocess.
    server = serve(app, 0)
    try:
        status, result = call(app, server, "POST", f"/api/jobs/{job.id}/retry", json.dumps({"structure": structure}), "application/json")
        assert status == 200 and result["id"] == job.id
        command = webapp.App.command(app, job)
        assert ("--no-claude" not in command) is structure
        assert command[command.index("--structure-provider") + 1] == "codex"
        assert "--retry-failed" in command
        assert (job.dir / "in/synthetic.pdf").read_bytes() == pdf_data
        assert app.cache_root().name == "codex"
    finally:
        server.shutdown()
        server.server_close()


def test_retry_rejects_non_boolean_structure_before_changing_job(tmp_path, apps):
    app = new_app(tmp_path)
    apps.append(app)
    server = serve(app, 0)
    try:
        status, _ = call(app, server, "POST", "/api/jobs/0000000000000000/retry", '{"structure":"false"}', "application/json")
        assert status == 400 and not app.jobs
    finally:
        server.shutdown()
        server.server_close()
