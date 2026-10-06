"""Distribution-only tests: synthetic files, no AI, installs, or PDF processing."""
from __future__ import annotations

import os
import struct
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_dist as dist


@pytest.fixture
def source(tmp_path, monkeypatch):
    for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY",
                "OPENAI_ACCESS_TOKEN", "CODEX_ACCESS_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "source"
    for name in dist.required_files("codex"):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# fixture\n", encoding="utf-8")
    (root / "config.toml").write_text('[gemini]\nrpm = 5\n[fonts]\nserif = ["keep"]\n', encoding="utf-8")
    (root / "config.codex.toml").write_text('[structure]\nprovider = "codex"\n', encoding="utf-8")
    (root / "翻訳アプリ_Codex.bat").write_text(
        '@echo off\npython -m readable.webapp --structure-provider codex %*\n', encoding="utf-8")
    return root


def payload(path):
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        return {name: z.read(name) for name in z.namelist()}


def make_zip(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.filename = name  # Preserve invalid backslashes on Windows for the negative fixture.
            z.writestr(info, data)
    return path


@pytest.mark.parametrize("config", [
    '[gemini]\nrpm = 5\n',
    '[structure]\nprovider = "none" # keep comment\ntimeout = 120\n[gemini]\nrpm = 5\n',
    '[structure]\ntimeout = 120\n',
    '["structure"]\n"provider" = \'none\'\n[structure.nested]\nx = 2\n',
    '[structure.nested]\nx = 2\n',
    '[structure]',
])
def test_two_variants_same_sources_only_default_changes(source, tmp_path, config):
    config_path = source / "config.toml"
    config_path.write_bytes(config.replace("\n", "\r\n").encode("utf-8"))
    original = config_path.read_bytes()
    claude = dist.build(tmp_path / "out", "20000101", source, variant="claude")
    codex = dist.build(tmp_path / "out", "20000101", root=source, variant="codex")
    assert claude.name == "ReadableJP-Claude-20000101.zip"
    assert codex.name == "ReadableJP-Codex-20000101.zip"
    a, b = payload(claude), payload(codex)
    assert a.keys() == b.keys()
    assert all(name.startswith("ReadableJP/") for name in a)
    assert {name for name in a if a[name] != b[name]} == {"ReadableJP/config.toml"}
    for variant, files in (("claude", a), ("codex", b)):
        parsed = tomllib.loads(files["ReadableJP/config.toml"].decode())
        expected = tomllib.loads(original.decode())
        expected["structure"] = {**expected.get("structure", {}), "provider": variant}
        assert parsed == expected
        assert "ReadableJP/readable/codex_provider.py" in files
        dist.verify_zip(claude if variant == "claude" else codex, variant=variant)
    assert config_path.read_bytes() == original


def test_legacy_build_signature_and_config_unchanged(source, tmp_path):
    original = (source / "config.toml").read_bytes()
    for name in dist.VARIANT_FILES:
        (source / name).unlink()
    (source / "翻訳アプリ_Codex.bat").unlink()
    result = dist.build(tmp_path / "out", "20000101", source)
    assert result.name == "ReadableJP-20000101.zip"
    assert payload(result)["ReadableJP/config.toml"] == original


@pytest.mark.parametrize("missing", [
    "readable/cli.py", "readable/web_static/index.html", "prompts/gemini_translate.md",
    "bat_messages/rc12.txt", "docs/INSTALL.md", "readable/codex_provider.py", "readable/codex_assist.py", "config.codex.toml", "docs/CODEX.md",
])
def test_missing_required_file_refused(source, tmp_path, missing):
    (source / missing).unlink()
    with pytest.raises(dist.DistError, match="missing required files"):
        dist.build(tmp_path / "out", "20000101", root=source, variant="codex")
    assert not list((tmp_path / "out").glob("*"))


@pytest.mark.parametrize("entry", [None, 'rem --structure-provider codex\npython -m readable.webapp\n'])
def test_missing_or_comment_only_codex_launcher_refused(source, tmp_path, entry):
    bat = source / "翻訳アプリ_Codex.bat"
    if entry is None:
        bat.unlink()
    else:
        bat.write_text(entry, encoding="utf-8")
    with pytest.raises(dist.DistError, match="Codex launcher"):
        dist.build(tmp_path / "out", "20000101", source, "codex")
    assert not list((tmp_path / "out").glob("*"))


@pytest.mark.parametrize("action", ["modify", "add", "delete", "same_stat"])
def test_source_change_refused_and_previous_release_preserved(source, tmp_path, monkeypatch, action):
    out = tmp_path / "out"
    out.mkdir()
    previous = out / "ReadableJP-Codex-20000101.zip"
    previous.write_bytes(b"previous release")
    real_verify = dist.verify_zip

    def edit_during_verification(path, *args, **kwargs):
        real_verify(path, *args, **kwargs)
        target = source / "readable/cli.py"
        if action == "add":
            (source / "readable/new_provider.py").write_text("# new\n", encoding="utf-8")
        elif action == "delete":
            target.unlink()
        else:
            stamp = target.stat()
            target.write_bytes(b"X" + target.read_bytes()[1:])
            if action == "same_stat":
                os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    monkeypatch.setattr(dist, "verify_zip", edit_during_verification)
    with pytest.raises(dist.DistError, match="source .*changed"):
        dist.build(out, "20000101", source, "codex")
    assert previous.read_bytes() == b"previous release"
    assert list(out.iterdir()) == [previous]


@pytest.mark.parametrize("name", [
    "readable/_patch_m6c.py", "readable/scratch_check.py", ".codex/auth.json", "readable/auth.json",
    "readable/.codex/cache.py", "readable/cache/stale.py", "readable/Codex_auth.py", "private.pdf",
])
def test_work_scripts_auth_cache_pdf_excluded(source, tmp_path, name):
    p = source / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("private fixture", encoding="utf-8")
    result = dist.build(tmp_path / "out", "20000101", source, "codex")
    assert f"ReadableJP/{name}" not in payload(result)
    with pytest.raises(dist.DistError):
        dist.verify_zip(make_zip(tmp_path / "forbidden.zip", {f"ReadableJP/{name}": b"fixture"}))


@pytest.mark.parametrize("secret", [
    "sk-proj-" + "a" * 48, "sk-svcacct-" + "b" * 48, "sk-" + "c" * 48,
    "sk-ant-" + "d" * 48, "AIza" + "e" * 40, 'openai_api_key = "literal_key_0123456789"',
])
def test_secret_literals_rejected(tmp_path, secret):
    p = make_zip(tmp_path / "secret.zip", {"ReadableJP/readable/a.py": secret})
    with pytest.raises(dist.DistError, match="秘密情報"):
        dist.verify_zip(p)


def test_regex_source_not_a_secret_and_registered_openai_key_checked(tmp_path, monkeypatch):
    # Include the actual patterns to guard against accidentally scanning their syntax as keys.
    p = make_zip(tmp_path / "patterns.zip", {"ReadableJP/readable/patterns.py": Path(dist.__file__).read_bytes()})
    dist.verify_zip(p)
    monkeypatch.setenv("OPENAI_API_KEY", "opaque_key_0123456789")
    p = make_zip(tmp_path / "known.zip", {"ReadableJP/readable/a.py": b"opaque_key_0123456789"})
    with pytest.raises(dist.DistError, match="秘密情報"):
        dist.verify_zip(p)


def test_crc_corruption_rejected(tmp_path):
    p = make_zip(tmp_path / "crc.zip", {"ReadableJP/readable/a.py": b"print(1)"})
    raw = bytearray(p.read_bytes())
    name_len, extra_len = struct.unpack_from("<HH", raw, 26)
    raw[30 + name_len + extra_len] ^= 1
    p.write_bytes(raw)
    with pytest.raises(dist.DistError, match="CRC"):
        dist.verify_zip(p)


@pytest.mark.parametrize("name", ["ReadableJP/../readable/a.py", "ReadableJP/readable/../../a.py", "ReadableJP\\readable\\a.py"])
def test_unsafe_zip_paths_rejected(tmp_path, name):
    p = make_zip(tmp_path / "paths.zip", {name: b"fixture"})
    with pytest.raises(dist.DistError, match="unsafe ZIP path"):
        dist.verify_zip(p)


def test_invalid_variant_and_config_do_not_publish(source, tmp_path):
    with pytest.raises(dist.DistError, match="unknown variant"):
        dist.build(tmp_path / "out", "20000101", source, "other")
    (source / "config.toml").write_text('[structure]\nprovider="none"\n[structure]\n', encoding="utf-8")
    with pytest.raises(dist.DistError, match="invalid TOML"):
        dist.build(tmp_path / "out", "20000101", source, "codex")
    assert not list((tmp_path / "out").glob("*"))


def test_cli_variant_passed_to_builder(source, tmp_path, monkeypatch):
    real_build = dist.build
    seen = []

    def build_fixture(out, date, root=dist.ROOT, variant=None):
        seen.append(variant)
        return real_build(out, date, source, variant)

    monkeypatch.setattr(dist, "build", build_fixture)
    assert dist.main(["--out", str(tmp_path / "out"), "--date", "20000101", "--variant", "claude"]) == 0
    assert seen == ["claude"]
