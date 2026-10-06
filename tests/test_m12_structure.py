"""M12 structure states and cache/log regressions; synthetic data, no AI calls."""
import copy
import json

import fitz
import pytest

from readable import cli, structure as st
from readable.config import Config
from readable.gemini_client import GeminiError


def document(numbered=False):
    frames = []
    for i in range(5):
        text = (f"[{i + 1}] Example publication by Smith, published in 2024."
                if numbered else f"The animals in group {i} were observed during the study and the results were recorded.")
        frames.append({"id": f"f{i}", "page": 1, "role": "body", "translate": True,
                       "text": text, "html": text, "flags": [], "size": 10, "nrows": 1,
                       "bbox": [20, 100 + 30 * i, 500, 115 + 30 * i]})
    return {"num_pages": 1, "body_size": 10, "joins": [], "charmap": {},
            "pages": [{"number": 1, "height": 800, "width": 600, "frames": frames}]}


def corrections(kind):
    roles = {"f0": "heading", "f1": "reference"}
    if kind == "whole":
        roles = {f"f{i}": "reference" for i in range(5)}
    elif kind == "whole_partial":
        roles = {f"f{i}": "reference" for i in range(4)} | {"f4": "table"}
    elif kind in ("all_rejected", "join_partial"):
        roles = {"f0": "reference"}
    joins = [["f1", "f2"]] if kind == "join_partial" else []
    return {"roles": roles, "joins_add": joins, "joins_remove": [], "charmap": {}}


@pytest.fixture(params=["claude", "codex"])
def provider(request):
    return request.param


def mock_provider(monkeypatch, provider, raw):
    calls = []

    def invoke(system, text, cfg, **kwargs):
        calls.append(provider)
        return copy.deepcopy(raw), {"output_tokens": 10}

    monkeypatch.setitem(st.PROVIDERS, provider, invoke)
    return calls


def config(provider):
    return Config({"structure": {"provider": provider}})


def test_partial_counts_and_audit_survive_cache(tmp_path, monkeypatch, provider):
    raw = corrections("partial")
    calls = mock_provider(monkeypatch, provider, raw)
    for cached in (False, True):
        doc, logs = document(), []
        info = st.run_structure(doc, tmp_path, config(provider), log=logs.append)
        assert info["ok"] and info["used"] and info["status"] == "partial"
        assert info["cached"] is cached
        assert (info["selection"]["accepted"], info["selection"]["rejected"]) == (1, 1)
        assert [f["role"] for f in doc["pages"][0]["frames"]] == ["heading", "body", "body", "body", "body"]
        assert any(line.startswith("[構造] 反映 1 件 / 不採用 1 件") for line in logs)
        assert logs.count("[構造状態] partial") == 1
        audit = json.loads((tmp_path / "structure_rejected.json").read_text(encoding="utf-8"))
        assert audit["raw"] == raw and audit["rejected"][0]["id"] == "f1"
    assert calls == [provider]
    cache = json.loads((tmp_path / "structure.json").read_text(encoding="utf-8"))
    assert cache["raw"] == raw and cache["selection"]["rejected"] == 1


@pytest.mark.parametrize("kind,candidates", [("whole", (5, 0)), ("whole_partial", (4, 1))])
def test_whole_rejection_counts_only_actual_application(tmp_path, monkeypatch, provider, kind, candidates):
    raw = corrections(kind)
    calls = mock_provider(monkeypatch, provider, raw)
    original = document(numbered=True)
    for cached in (False, True):
        doc, logs = copy.deepcopy(original), []
        info = st.run_structure(doc, tmp_path, config(provider), log=logs.append)
        assert doc == original
        assert not info["ok"] and not info["used"] and info["status"] == "rejected"
        assert info["cached"] is cached
        assert info["selection"]["accepted"] == 0 and info["selection"]["rejected"] == 5
        assert not info["selection"]["accepted_items"]
        candidate = info["candidate_selection"]
        assert (candidate["accepted"], candidate["rejected"]) == candidates
        assert any(line.startswith("[構造] 反映 0 件 / 不採用 5 件") for line in logs)
        assert logs.count("[構造状態] rejected") == 1
        audit = json.loads((tmp_path / "structure_rejected.json").read_text(encoding="utf-8"))
        assert audit["raw"] == raw and audit["candidate_selection"] == candidate
    assert calls == [provider]


def test_no_adopted_changes_is_not_used(tmp_path, monkeypatch, provider):
    calls = mock_provider(monkeypatch, provider, corrections("all_rejected"))
    for cached in (False, True):
        info = st.run_structure(document(), tmp_path, config(provider))
        assert not info["used"] and info["status"] == "rejected"
        assert info["cached"] is cached and info["selection"]["rejected"] == 1
    assert calls == [provider]


def test_accepted_join_with_all_roles_rejected_logs_partial(tmp_path, monkeypatch, provider):
    calls = mock_provider(monkeypatch, provider, corrections("join_partial"))
    for cached in (False, True):
        doc, logs = document(), []
        info = st.run_structure(doc, tmp_path, config(provider), log=logs.append)
        assert info["ok"] and info["used"] and info["status"] == "partial"
        assert info["cached"] is cached
        assert (info["selection"]["accepted"], info["selection"]["rejected"]) == (0, 1)
        assert doc["joins"] == [["f1", "f2"]] and not info["diff"]["roles_changed"]
        assert any(line.startswith("[構造] 反映 0 件 / 不採用 1 件") for line in logs)
        assert logs.count("[構造状態] partial") == 1
    assert calls == [provider]


def test_no_corrections_logs_accepted(tmp_path, monkeypatch, provider):
    calls = mock_provider(monkeypatch, provider, corrections("partial") | {"roles": {}})
    for cached in (False, True):
        logs = []
        info = st.run_structure(document(), tmp_path, config(provider), log=logs.append)
        assert info["ok"] and info["used"] and info["status"] == "accepted"
        assert info["cached"] is cached and logs.count("[構造状態] accepted") == 1
    assert calls == [provider]


@pytest.mark.parametrize("damage", ["version", "policy", "filtered_only"])
def test_incompatible_cache_is_not_reused(tmp_path, monkeypatch, provider, damage):
    calls = mock_provider(monkeypatch, provider, corrections("partial"))
    st.run_structure(document(), tmp_path, config(provider))
    path = tmp_path / "structure.json"
    cache = json.loads(path.read_text(encoding="utf-8"))
    if damage == "version":
        cache["cache_version"] = "2"
    elif damage == "policy":
        cache["policy"]["selection"] = "old-policy"
    else:
        del cache["raw"]
    path.write_text(json.dumps(cache), encoding="utf-8")
    info = st.run_structure(document(), tmp_path, config(provider))
    assert not info["cached"] and info["selection"]["rejected"] == 1
    assert calls == [provider, provider]


def test_changed_validation_policy_invalidates_cache(tmp_path, monkeypatch, provider):
    calls = mock_provider(monkeypatch, provider, corrections("partial"))
    st.run_structure(document(), tmp_path, config(provider))
    monkeypatch.setattr(st, "MAX_TRANSLATABLE_DROP", 0.10)
    assert not st.run_structure(document(), tmp_path, config(provider))["cached"]
    assert calls == [provider, provider]


def test_provider_failure_and_disable_return_skipped(tmp_path, monkeypatch, provider):
    def fail(*args, **kwargs):
        raise st.ClaudeError("mock provider unavailable")

    monkeypatch.setitem(st.PROVIDERS, provider, fail)
    for enabled in (True, False):
        doc = document()
        original = copy.deepcopy(doc)
        logs = []
        info = st.run_structure(doc, tmp_path, config(provider), enabled=enabled, log=logs.append)
        assert not info["ok"] and not info["used"] and info["status"] == "skipped"
        assert doc == original
        assert logs.count("[構造状態] skipped") == 1


def cli_setup(tmp_path, monkeypatch, provider, kind):
    # A generated PDF establishes the CLI's file/hash path; all content and
    # rendering are supplied in memory to keep these checks independent of fonts.
    pdf = tmp_path / "synthetic.pdf"
    with fitz.open() as generated:
        generated.new_page().insert_text((20, 40), "Synthetic test paper")
        generated.save(pdf)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'[structure]\nprovider = "{provider}"\n', encoding="utf-8")
    mock_provider(monkeypatch, provider, corrections(kind))
    monkeypatch.setattr("readable.fonts.build_fonts", lambda cfg: None)
    monkeypatch.setattr(cli, "extract_pdf", lambda *args, **kwargs: document(numbered=kind == "whole"))
    monkeypatch.setattr(cli, "build_units", lambda *args: [])
    monkeypatch.setattr(cli, "build_cell_units", lambda *args: [])
    monkeypatch.setattr(cli, "frames_translations", lambda *args: {})
    monkeypatch.setattr(cli, "translate_cached", lambda *args, **kwargs: {})
    monkeypatch.setattr(cli, "render_ja", lambda *args, **kwargs: {
        "frames": 5, "shrunk": 0, "shrunk_ratio": 0, "min_scale": 1, "expanded": 0, "failed": 0,
        "warnings": [], "links": dict.fromkeys(["orig", "kept", "restored", "inside_uri", "lost_internal"], 0)})
    args = [str(pdf), "--translator", "dummy", "--mode", "ja", "--config", str(cfg_path),
            "--out", str(tmp_path / "out"), "--work-dir", str(tmp_path / "work")]
    return args, cli.work_dir_for(tmp_path / "work", pdf)


@pytest.mark.parametrize("kind,state,used,counts", [
    ("partial", "partial", True, (1, 1)),
    ("whole", "rejected", False, (0, 5)),
    ("all_rejected", "rejected", False, (0, 1)),
    ("join_partial", "partial", True, (0, 1)),
])
def test_cli_report_exposes_used_and_status(tmp_path, monkeypatch, provider, kind, state, used, counts):
    args, wd = cli_setup(tmp_path, monkeypatch, provider, kind)
    for _ in range(2):
        assert cli.main(args) == 0
        report = json.loads((wd / "render_report.json").read_text(encoding="utf-8"))["structure"]
        assert report["status"] == state and report["used"] is used
        assert (report["accepted"], report["rejected"]) == counts
        assert report["reason"]


@pytest.mark.parametrize("kind,state,counts", [
    ("partial", "partial", (1, 1)), ("whole", "rejected", (0, 5)),
    ("join_partial", "partial", (0, 1)),
])
def test_canonical_log_survives_translation_failure(tmp_path, monkeypatch, capsys, provider, kind, state, counts):
    args, wd = cli_setup(tmp_path, monkeypatch, provider, kind)

    def fail(*args, **kwargs):
        # Check at entry to translation: the state must already be available
        # while Gemini is running, before either failure or rendering occurs.
        output = capsys.readouterr()
        assert f"[構造] 反映 {counts[0]} 件 / 不採用 {counts[1]} 件" in output.err
        assert output.err.count(f"[構造状態] {state}") == 1
        raise GeminiError("mock failure before render")

    monkeypatch.setattr(cli, "translate_cached", fail)
    for _ in range(2):
        assert cli.main(args) == cli.EXIT_API_ERROR
        assert "mock failure before render" in capsys.readouterr().err
        assert not (wd / "render_report.json").exists()
