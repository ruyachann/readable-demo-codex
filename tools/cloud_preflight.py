"""Read-only readiness check. No model requests; no credential values in output."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def readiness(config: Path, context: str) -> dict:
    from readable.config import load_config

    packages = {}
    for name in ("PyMuPDF", "fonttools", "google-genai"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    cfg = load_config(config)
    try:
        from readable.fonts import build_fonts
        fonts = build_fonts(cfg)
        font_report = {"ok": True, "files": fonts.files}
    except Exception:
        # Do not dump exceptions, environment, user config, or credential files.
        font_report = {"ok": False, "reason": "Japanese font configuration could not be loaded/embedded"}
    if all(packages.values()):
        from readable.codex_provider import codex_status
        codex = codex_status()  # codex login status only; never codex exec
    else:
        codex = {"found": False, "logged_in": False, "detail": "Install requirements.txt before checking Codex"}
    api_key_present = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    report = {
        "execution_context": context,
        "platform": platform.system(),
        "python": platform.python_version(),
        "python_supported": sys.version_info >= (3, 11),
        "packages": packages,
        "config": config.name,
        "provider": cfg.get("structure", "provider"),
        "gemini_key_present": api_key_present,
        "codex": codex,
        "fonts": font_report,
        "gemini_requests": 0,
        "codex_model_requests": 0,
        "note": "Readiness is not an end-to-end real-model test; context is caller supplied.",
    }
    report["ready_for_pilot"] = all((
        report["python_supported"], all(packages.values()), font_report["ok"],
        api_key_present, codex.get("logged_in"), report["provider"] == "codex",
    ))
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.codex.cloud.toml")
    parser.add_argument("--context", choices=("local-preparation", "cloud-task"), default="local-preparation")
    parser.add_argument("--out", type=Path, default=ROOT / "work/cloud-preflight.json")
    args = parser.parse_args(argv)
    if not args.config.is_file():
        parser.error("configuration file is missing")
    report = readiness(args.config, args.context)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready_for_pilot"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
