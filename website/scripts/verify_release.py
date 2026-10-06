"""Verify the exact two downloadable editions and render without external AI."""
from pathlib import Path
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
import fitz

SITE = Path(__file__).resolve().parents[1]
DIST = SITE / "dist"
ROOT = SITE.parent


def main():
    meta = json.loads((DIST / "downloads/release.json").read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("dist_builder", ROOT / "tools/build_dist.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    html = (DIST / "index.html").read_text(encoding="utf-8")
    env = dict(os.environ, PYTHONUTF8="1")
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY"):
        env.pop(key, None)
    report = {"editions": {}, "external_ai_calls": 0, "published": False}
    for variant in ("claude", "codex"):
        edition = meta["editions"][variant]
        path = DIST / "downloads" / edition["filename"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == edition["sha256"]
        builder.verify_zip(path, variant=variant)
        assert edition["filename"] in html and edition["sha256"] in html
        with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory(prefix="edition-smoke-", dir=SITE) as td:
            target = Path(td).resolve()
            names = set(archive.namelist())
            assert archive.testzip() is None
            for entry in edition["files"]:
                payload = archive.read(entry["path"])
                assert len(payload) == entry["bytes"]
                assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
            for name in names:
                assert (target / name).resolve().is_relative_to(target)
            for bat in ["setup.bat", "翻訳する.bat", "翻訳アプリ.bat", "翻訳アプリCodex.bat"]:
                bat_path = builder.TOP + "/" + bat
                assert bat_path in names
                code = archive.read(bat_path).decode("utf-8-sig")
                references = re.findall(r"%MSG%\\([A-Za-z0-9_]+\.txt)", code)
                if "rc%%C.txt" in code:
                    references += [f"rc{i}.txt" for i in (1, 3, 4, 5, 6, 7, 8, 9, 10, 11)]
                assert all(builder.TOP + "/bat_messages/" + name in names for name in references)
            archive.extractall(target)
            root = target / builder.TOP
            config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8-sig"))
            assert config["structure"]["provider"] == variant
            checks = []
            for module in ("readable", "readable.webapp"):
                executed = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", module, "--help"],
                                          cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
                assert executed.returncode == 0, executed.stderr[-1200:]
                checks.append(module + " --help: OK")
            executed = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "readable",
                                       str(DIST / "assets/sample-en.pdf"), "--translator", "dummy", "--no-structure",
                                       "--out", str(target / "render"), "--work-dir", str(target / "work")],
                                      cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)
            assert executed.returncode in (0, 12), (executed.returncode, executed.stderr[-1500:])
            with fitz.open(target / "render/sample-en_ja.pdf") as ja, fitz.open(target / "render/sample-en_dual.pdf") as dual:
                assert len(ja) == 1 and len(dual) == 2
                assert "【訳】" in ja[0].get_text()
            checks.append("exact distributed source dummy PDF render: OK")
            report["editions"][variant] = {"zip_bytes": edition["bytes"], "zip_files": edition["file_count"],
                                           "sha256": edition["sha256"], "provider_default": variant,
                                           "builder_verification": "OK", "checks": checks}
    with fitz.open(DIST / "assets/sample-en.pdf") as en, fitz.open(DIST / "assets/sample-ja.pdf") as ja, fitz.open(DIST / "assets/sample-dual.pdf") as dual:
        assert len(en) == len(ja) == 1 and len(dual) == 2
        assert dual[0].get_text() == en[0].get_text() and dual[1].get_text() == ja[0].get_text()
    assert not re.search(r"__[A-Z_]+__", html)
    assert re.search(r'name="robots"[^>]*noindex', html)
    links = re.findall(r'(?:src|href)="([^"#]+)"', html)
    for link in links:
        if not link.startswith(("https:", "http:", "mailto:")):
            assert (DIST / link.split("#")[0].split("?")[0]).is_file(), link
    report["sample"] = "fictional; manual translation; EN=1 JA=1 dual=2"
    report["local_links"] = "OK"
    (SITE / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
