"""Cloud preparation checks: fake fonts/auth only, zero model requests."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from readable import codex_provider, fonts

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cloud_preflight", ROOT / "tools/cloud_preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def prepare(monkeypatch):
    monkeypatch.setattr(preflight.importlib.metadata, "version", lambda name: "test-version")
    monkeypatch.setattr(fonts, "build_fonts", lambda cfg: SimpleNamespace(files={"serif-regular": "fake.ttf"}))
    monkeypatch.setattr(codex_provider, "codex_status", lambda: {"found": True, "logged_in": True, "detail": "mock ChatGPT login"})


def test_cloud_readiness_never_discloses_key_or_claims_real_model_test(monkeypatch):
    prepare(monkeypatch)
    key = "test-key-must-not-appear-in-report"
    monkeypatch.setenv("GEMINI_API_KEY", key)
    monkeypatch.setattr(codex_provider, "run_process", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no model process")))
    result = preflight.readiness(ROOT / "config.codex.cloud.toml", "cloud-task")
    assert result["ready_for_pilot"] is True
    assert result["gemini_requests"] == result["codex_model_requests"] == 0
    assert key not in json.dumps(result)
    assert "not an end-to-end" in result["note"]


def test_missing_key_stops_pilot_without_network(monkeypatch):
    prepare(monkeypatch)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = preflight.readiness(ROOT / "config.codex.cloud.toml", "cloud-task")
    assert result["ready_for_pilot"] is False and result["gemini_key_present"] is False


def test_missing_fonts_stops_pilot_and_redacts_exception(monkeypatch):
    prepare(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-value")
    def fail(cfg):
        raise ValueError("private-value-do-not-log")
    monkeypatch.setattr(fonts, "build_fonts", fail)
    result = preflight.readiness(ROOT / "config.codex.cloud.toml", "cloud-task")
    assert result["ready_for_pilot"] is False
    assert "private-value-do-not-log" not in json.dumps(result)


def test_no_dependency_report_does_not_launch_cli(monkeypatch):
    prepare(monkeypatch)
    def missing(name):
        raise preflight.importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(preflight.importlib.metadata, "version", missing)
    monkeypatch.setattr(codex_provider, "codex_status", lambda: (_ for _ in ()).throw(AssertionError("CLI must not start")))
    assert preflight.readiness(ROOT / "config.codex.cloud.toml", "cloud-task")["ready_for_pilot"] is False
