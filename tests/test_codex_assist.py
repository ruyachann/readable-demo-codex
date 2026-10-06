"""Codex assist contract tests: synthetic text, fake CLI, no AI requests."""
import copy
import importlib
import json
from pathlib import Path
import subprocess

import pytest

from readable import assist, codex_provider as cp
from readable.config import Config
from readable.structure import ClaudeError, ClaudeSkipped
from readable.numcheck import check_numbers
from readable.translate import validate_translation


@pytest.fixture(autouse=True)
def clean_api_environment(monkeypatch):
    for name in cp.API_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def call_assist():
    # Import at test time so collection is possible while transport is being built.
    return importlib.import_module("readable.codex_assist").call_assist


def fake_runner(raw=None, *, response=None, events=None, auth="Logged in using ChatGPT", rc=0):
    calls = []
    schemas = []

    def run(command, **options):
        calls.append((list(command), options))
        if command[-2:] == ["login", "status"]:
            return subprocess.CompletedProcess(command, 0, "", auth)
        schemas.append(json.loads(Path(command[command.index("--output-schema") + 1]).read_text(encoding="utf-8")))
        output = Path(command[command.index("-o") + 1])
        output.write_text(response if response is not None else json.dumps(raw), encoding="utf-8")
        return subprocess.CompletedProcess(command, rc,
            events if events is not None else '{"type":"turn.completed","usage":{"input_tokens":12}}',
            "hidden-document-and-token-sentinel")

    return run, calls, schemas


def assert_strict_schema(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
        for value in schema.values():
            assert_strict_schema(value)
    elif isinstance(schema, list):
        for value in schema:
            assert_strict_schema(value)


@pytest.mark.parametrize("schema,raw", [
    (assist.GLOSSARY_SCHEMA, {"fixes": [{"en": "density", "ja": "密度", "reason": ""}], "remove": []}),
    (assist.CORRECT_SCHEMA, {"units": [{"id": "u1", "text": "試料は12個です。"}]}),
])
def test_isolated_transport_and_recursive_strict_schema(call_assist, tmp_path, schema, raw):
    original = copy.deepcopy(schema)
    private = tmp_path / "private-document-directory"
    private.mkdir()
    run, calls, schemas = fake_runner(raw)
    data, meta = call_assist("assist-rules", "synthetic-source", Config({"codex": {"model": "gpt-6.1-sol"}}),
                             schema, cwd=private, runner=run)
    assert schema == original
    assert_strict_schema(schemas[0])
    if "fixes" in raw:
        assert data["fixes"][0]["reason"] == ""
        assert data["fixes"][0]["ja"] == "密度"
    else:
        assert data == raw
    command, options = calls[-1]
    assert Path(options["cwd"]) != private
    assert not Path(options["cwd"]).exists()
    assert str(private) not in json.dumps(command)
    assert "synthetic-source" not in command
    assert "synthetic-source" in options["input"]
    assert "assist-rules" in options["input"]
    assert command[-1] == "-"
    for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--json", "--output-schema"):
        assert flag in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert 'forced_login_method="chatgpt"' in command
    assert "features.shell_tool=false" in command
    assert "features.plugins=false" in command
    assert "features.hooks=false" in command
    assert meta["provider"] == "codex"
    assert meta["usage"]["input_tokens"] == 12


@pytest.mark.parametrize("name", cp.API_ENV)
def test_api_environment_rejected_before_cli(call_assist, monkeypatch, name):
    monkeypatch.setenv(name, "synthetic-setting")
    run, calls, _ = fake_runner({"units": []})
    with pytest.raises(ClaudeSkipped):
        call_assist("rules", "source", Config({}), assist.CORRECT_SCHEMA, runner=run)
    assert calls == []


@pytest.mark.parametrize("auth", ["Logged in using an API key", "Not logged in", "unknown"])
def test_subscription_required_before_exec(call_assist, auth):
    run, calls, _ = fake_runner({"units": []}, auth=auth)
    with pytest.raises(ClaudeSkipped):
        call_assist("rules", "source", Config({}), assist.CORRECT_SCHEMA, runner=run)
    assert len(calls) == 1
    assert calls[0][0][-2:] == ["login", "status"]


@pytest.mark.parametrize("events", [
    "", "not-json", '{"type":"turn.started"}', '{"type":"turn.failed"}',
    '{"type":"error","message":"hidden-document-and-token-sentinel"}',
    '{"type":null}', '{"type":"item.completed","item":null}',
    '{"type":"item.completed","item":{"type":"command_execution"}}\n{"type":"turn.completed"}',
    '{"type":"item.completed","item":{"type":"mcp_tool_call"}}\n{"type":"turn.completed"}',
    '{"type":"turn.started"}\n{"type":"item.completed","item":{"type":"error","message":"runtime error"}}\n{"type":"turn.completed"}',
])
def test_tool_error_malformed_and_incomplete_events_rejected(call_assist, events):
    run, _, _ = fake_runner({"units": []}, events=events)
    with pytest.raises(ClaudeError) as error:
        call_assist("rules", "source", Config({}), assist.CORRECT_SCHEMA, runner=run)
    assert "hidden-document-and-token-sentinel" not in str(error.value)


@pytest.mark.parametrize("schema,raw", [
    (assist.CORRECT_SCHEMA, {}),
    (assist.CORRECT_SCHEMA, {"units": {}}),
    (assist.CORRECT_SCHEMA, {"units": [{"id": "u", "text": 12}]}),
    (assist.CORRECT_SCHEMA, {"units": [{"id": 1, "text": "訳"}]}),
    (assist.CORRECT_SCHEMA, {"units": [{"id": "u", "text": "訳", "extra": True}]}),
    (assist.CORRECT_SCHEMA, {"units": [{"id": "u", "text": "訳"}, {"id": "u", "text": "別訳"}]}),
    (assist.CORRECT_SCHEMA, {"units": [], "extra": True}),
    (assist.GLOSSARY_SCHEMA, {"fixes": [], "remove": [1]}),
    (assist.GLOSSARY_SCHEMA, {"fixes": [{"en": "density", "ja": "密度"}], "remove": []}),
    (assist.GLOSSARY_SCHEMA, {"fixes": [{"en": "density", "ja": "密度", "reason": 1}], "remove": []}),
    (assist.GLOSSARY_SCHEMA, {"fixes": [{"en": "density", "ja": "密度", "reason": ""}, {"en": "density", "ja": "密度", "reason": ""}], "remove": []}),
])
def test_invalid_output_rejected(call_assist, schema, raw):
    run, _, _ = fake_runner(raw)
    with pytest.raises(ClaudeError):
        call_assist("rules", "source", Config({}), schema, runner=run)


@pytest.mark.parametrize("response", ["not-json", '```json\n{"units": []}\n```', '{"units":[],"units":[]}'])
def test_malformed_and_duplicate_json_keys_rejected(call_assist, response):
    run, _, _ = fake_runner(response=response)
    with pytest.raises(ClaudeError):
        call_assist("rules", "source", Config({}), assist.CORRECT_SCHEMA, runner=run)


def test_stderr_not_exposed(call_assist, capsys):
    run, _, _ = fake_runner({"units": []}, rc=1)
    with pytest.raises(ClaudeError) as error:
        call_assist("rules", "source", Config({}), assist.CORRECT_SCHEMA, runner=run)
    captured = capsys.readouterr()
    assert "hidden-document-and-token-sentinel" not in str(error.value) + captured.out + captured.err


@pytest.mark.parametrize("stage", ["login", "exec"])
def test_timeouts_safe_and_no_retry(call_assist, stage):
    run, calls, _ = fake_runner({"units": []})
    attempts = []

    def timeout(command, **options):
        attempts.append(command)
        if stage in command:
            raise subprocess.TimeoutExpired(command, options["timeout"], stderr="hidden-document-and-token-sentinel")
        return run(command, **options)

    with pytest.raises(ClaudeError) as error:
        call_assist("rules", "source", Config({"codex": {"timeout": 3}}), assist.CORRECT_SCHEMA, runner=timeout)
    assert len(attempts) == (1 if stage == "login" else 2)
    assert "hidden-document-and-token-sentinel" not in str(error.value)


def make_shared(call_assist, monkeypatch, tmp_path, raw, *, budget=4, enabled=True):
    monkeypatch.setitem(assist.ASSIST_PROVIDERS, "codex", call_assist)
    run, calls, _ = fake_runner(raw)
    instance = assist.Assist(Config({"assist": {"max_calls_per_doc": budget}}), tmp_path,
                             provider="codex", enabled=enabled, used_by_structure=1, runner=run)
    return instance, calls


def test_shared_budget_cache_repeat_and_disabled(call_assist, monkeypatch, tmp_path):
    a, calls = make_shared(call_assist, monkeypatch, tmp_path, {"fixes": [], "remove": []}, budget=3)
    glossary = [{"en": "density", "ja": "密度"}]
    assert a.review_glossary(glossary, "T", "A") == glossary
    assert a.review_glossary(glossary, "T", "A") == glossary
    assert a.calls == 2 and a.info["calls"] == 1
    assert sum("exec" in c for c, _ in calls) == 1
    a.review_glossary(glossary, "different-title", "A")
    a.review_glossary(glossary, "third-title", "A")
    assert a.calls == 3
    assert sum("exec" in c for c, _ in calls) == 2
    disabled, disabled_calls = make_shared(call_assist, monkeypatch, tmp_path / "disabled",
                                          {"units": []}, enabled=False)
    assert disabled.review_glossary(glossary, "T", "A") == glossary
    assert disabled.correct_units([], [], "T", "A", lambda u, ja: []) == {}
    assert disabled_calls == []


def test_shared_correction_rejects_changed_numbers(call_assist, monkeypatch, tmp_path):
    a, calls = make_shared(call_assist, monkeypatch, tmp_path,
                          {"units": [{"id": "u1", "text": "試料は13個です。"}]}, budget=2)
    unit = {"id": "u1", "role": "body", "text": "There are 12 samples."}
    validated = []

    def validate(u, ja):
        validated.append(ja)
        return validate_translation(u["text"], ja) + check_numbers(u["text"], ja)

    assert a.correct_units([{"unit": unit, "current": None, "problems": ["English remains"]}],
                           [], "T", "A", validate) == {}
    assert validated == ["試料は13個です。"]
    assert a.info["correction"]["adopted"] == 0
    assert a.info["correction"]["rejected"]
    assert a.calls == 2
    assert sum("exec" in c for c, _ in calls) == 1


def test_register_installs_callable_and_preserves_existing_provider(monkeypatch):
    from readable import codex_assist

    # A private registry copy prevents startup registration leaking to other tests.
    registry = dict(assist.ASSIST_PROVIDERS)
    registry.pop("codex", None)
    monkeypatch.setattr(assist, "ASSIST_PROVIDERS", registry)
    codex_assist.register()
    assert registry["codex"] is codex_assist.call_assist
    assert callable(registry["codex"])
    codex_assist.register()
    assert registry["codex"] is codex_assist.call_assist

    def existing_provider(*args, **kwargs):
        raise AssertionError("Registration must not invoke the provider")

    registry["codex"] = existing_provider
    codex_assist.register()
    assert registry["codex"] is existing_provider


def test_web_startup_registers_codex_and_setup_reports_supported(monkeypatch, tmp_path):
    from readable import codex_assist, webapp

    registry = dict(assist.ASSIST_PROVIDERS)
    registry.pop("codex", None)
    monkeypatch.setattr(assist, "ASSIST_PROVIDERS", registry)
    monkeypatch.setattr(webapp.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(webapp, "_find_spec", lambda name: object())
    monkeypatch.setattr(webapp.Settings, "get_key", lambda self: (None, ""))
    monkeypatch.setattr(webapp.Settings, "consented", lambda self: False)
    monkeypatch.setattr(cp, "codex_status", lambda: {"found": True, "logged_in": True, "detail": "synthetic"})

    def forbidden_probe(*args, **kwargs):
        raise AssertionError("Fast setup must not run CLI or font probes")

    monkeypatch.setattr(cp, "run_process", forbidden_probe)
    monkeypatch.setattr(webapp, "check_fonts", forbidden_probe)
    app = webapp.App(tmp_path / "jobs", tmp_path / "work", translator="dummy",
                     home=tmp_path / "settings", structure_provider="codex")
    try:
        assert registry["codex"] is codex_assist.call_assist
        status = webapp.setup_status(app.settings, fast=True, provider="codex")
        assert status["assist"] == {"provider": "codex", "supported": True}
        assert not app.jobs and app.q.empty()
    finally:
        app.instance.release()
