"""Codex transport tests; synthetic data and fake subprocess only."""
import json
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from readable import codex_provider as cp
from readable.config import Config
from readable.structure import ClaudeError, ClaudeSkipped


@pytest.fixture(autouse=True)
def no_api_environment(monkeypatch):
    for name in cp.API_ENV:
        monkeypatch.delenv(name, raising=False)


EMPTY = {"roles": [], "joins_add": [], "joins_remove": [], "charmap": []}


def fake_runner(raw=None, events=None, auth="Logged in using ChatGPT", rc=0):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-2:] == ["login", "status"]:
            return subprocess.CompletedProcess(command, 0, "", auth)
        output = Path(command[command.index("-o") + 1])
        output.write_text(json.dumps(EMPTY if raw is None else raw), encoding="utf-8")
        event_text = events if events is not None else json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12}})
        return subprocess.CompletedProcess(command, rc, event_text, "sensitive excerpt")
    return run, calls


def test_transport_isolated_and_normalized(tmp_path):
    run, calls = fake_runner({"roles": [{"id": "p0f0", "role": "heading"}], "joins_add": [["a", "b"]],
                              "joins_remove": [], "charmap": [{"char": "\u0003", "replacement": "°"}]})
    result, meta = cp.call_codex("rules", "synthetic", Config({"codex": {"model": "gpt-6.1-sol"}}), cwd=tmp_path, runner=run)
    assert result["roles"] == {"p0f0": "heading"} and result["charmap"] == {"\u0003": "°"}
    command, options = calls[-1]
    assert options["cwd"] != str(tmp_path) and not Path(options["cwd"]).exists()
    assert command[-1] == "-" and "synthetic" not in command
    for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--output-schema", "--json"):
        assert flag in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert 'forced_login_method="chatgpt"' in command and "features.shell_tool=false" in command
    assert "features.plugins=false" in command and "features.hooks=false" in command
    assert meta["usage"]["input_tokens"] == 12 and meta["provider"] == "codex"


@pytest.mark.parametrize("variable", cp.API_ENV)
def test_api_and_gateway_environment_never_invokes_cli(monkeypatch, variable):
    monkeypatch.setenv(variable, "synthetic-setting")
    run, calls = fake_runner()
    with pytest.raises(ClaudeSkipped):
        cp.call_codex("rules", "data", Config({}), runner=run)
    assert calls == []


@pytest.mark.parametrize("auth", ["Logged in using an API key", "Not logged in", "unknown status"])
def test_unconfirmed_subscription_never_calls_model(auth):
    run, calls = fake_runner(auth=auth)
    with pytest.raises(ClaudeSkipped):
        cp.call_codex("rules", "data", Config({}), runner=run)
    assert len(calls) == 1


@pytest.mark.parametrize("events", ["not json", '{"type":"turn.failed"}',
    '{"type":null}', '{"type":"item.completed","item":null}', '{"type":"item.completed","item":[]}',
    '{"type":"item.completed","item":{"type":"command_execution"}}\n{"type":"turn.completed"}',
    '{"type":"item.completed","item":{"type":"mcp_tool_call"}}\n{"type":"turn.completed"}', ""])
def test_invalid_events_or_tools_rejected(events):
    run, _ = fake_runner(events=events)
    with pytest.raises(ClaudeError):
        cp.call_codex("rules", "data", Config({}), runner=run)


def test_known_startup_warning_allowed_but_runtime_error_rejected():
    warning = json.dumps({"type": "item.completed", "item": {"type": "error", "message": "Code Mode is unavailable because code-mode host is disabled."}})
    run, _ = fake_runner(events=warning + '\n{"type":"turn.started"}\n{"type":"turn.completed"}')
    _, meta = cp.call_codex("rules", "data", Config({}), runner=run)
    assert meta["startup_warning_count"] == 1
    run, _ = fake_runner(events='{"type":"turn.started"}\n' + warning + '\n{"type":"turn.completed"}')
    with pytest.raises(ClaudeError):
        cp.call_codex("rules", "data", Config({}), runner=run)


@pytest.mark.parametrize("raw", [{}, {**EMPTY, "roles": {}}, {**EMPTY, "roles": [{"id": "a", "role": "unknown"}]},
    {**EMPTY, "roles": [{"id": "a", "role": "body"}, {"id": "a", "role": "title"}]},
    {**EMPTY, "charmap": [{"char": "ab", "replacement": "x"}]}, {**EMPTY, "joins_add": [["a"]]}])
def test_invalid_structured_output_rejected(raw):
    run, _ = fake_runner(raw=raw)
    with pytest.raises(ClaudeError):
        cp.call_codex("rules", "data", Config({}), runner=run)


def test_nonzero_exit_does_not_echo_document_or_token():
    run, _ = fake_runner(rc=1)
    with pytest.raises(ClaudeError) as error:
        cp.call_codex("rules", "data", Config({}), runner=run)
    assert "sensitive excerpt" not in str(error.value)


def test_timeout_and_missing_output():
    run, _ = fake_runner()
    def timeout(command, **options):
        if "exec" in command:
            raise subprocess.TimeoutExpired(command, 1)
        return run(command, **options)
    with pytest.raises(ClaudeError, match="タイムアウト"):
        cp.call_codex("rules", "data", Config({}), runner=timeout)
    def missing(command, **options):
        if "exec" in command:
            return subprocess.CompletedProcess(command, 0, '{"type":"turn.completed"}', "")
        return run(command, **options)
    with pytest.raises(ClaudeError):
        cp.call_codex("rules", "data", Config({}), runner=missing)


def test_npm_launcher_without_shell(tmp_path):
    entry = tmp_path / "node_modules/@openai/codex/bin/codex.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// fixture")
    def which(name):
        return {"codex": str(tmp_path / "codex.cmd"), "node": "node.exe"}.get(name)
    with patch.object(cp.shutil, "which", which):
        assert cp.find_codex() == ["node.exe", str(entry)]


def test_status_reports_skipped_auth_without_leaking_output():
    with patch.object(cp, "find_codex", return_value=["codex"]), patch.object(cp, "check_auth", side_effect=ClaudeSkipped("not authenticated")):
        assert cp.codex_status() == {"found": True, "logged_in": False, "detail": "not authenticated"}


def test_timeout_stops_actual_spawned_child():
    from readable.webapp import pid_alive
    parent = ("import subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
              "print(child.pid,flush=True); time.sleep(30)")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as error:
        cp.run_process([sys.executable, "-c", parent], timeout=1.5, text=True, encoding="utf-8")
    assert time.monotonic() - started < 8
    child_pid = int(error.value.output.strip())
    assert not pid_alive(child_pid)
