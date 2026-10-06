"""Build both site downloads using tools/build_dist.py as the sole ZIP writer."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
import re
import tempfile
import zipfile

SITE = Path(__file__).resolve().parents[1]
ROOT = SITE.parent
DIST = SITE / "dist"
VERSION = "2026.10.06"


def atomic_text(path, text):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def main():
    spec = importlib.util.spec_from_file_location("readable_dist_builder", ROOT / "tools/build_dist.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    template_path = SITE / "scripts/index.template.html"
    template = template_path.read_text(encoding="utf-8-sig")
    sources = {p.as_posix(): (ROOT / p).read_bytes() for p in builder.collect(ROOT)}
    editions = {}
    downloads = DIST / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-", dir=SITE) as temp:
        staged = []
        for variant in ("claude", "codex"):
            path = builder.build(Path(temp), VERSION.replace(".", ""), ROOT, variant=variant)
            builder.verify_zip(path, variant=variant)
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            filename = f"ReadableJP-{variant.title()}-{VERSION}-{digest[:12]}.zip"
            with zipfile.ZipFile(path) as archive:
                entries = [{"path": entry.filename, "bytes": entry.file_size,
                            "sha256": hashlib.sha256(archive.read(entry)).hexdigest()}
                           for entry in archive.infolist() if not entry.is_dir()]
                if archive.testzip():
                    raise RuntimeError("ZIP CRC failed")
            editions[variant] = {"filename": filename, "sha256": digest, "bytes": len(content),
                                 "file_count": len(entries), "files": entries, "structure_provider": variant,
                                 "ai_verification": ("GPT-5.6 Sol CLI synthetic structure response verified; Gemini/document quality not live-evaluated"
                                                     if variant == "codex" else "shared regression and distributed dummy render; no new Claude live evaluation")}
            staged.append((path, downloads / filename))
        changed = [name for name, data in sources.items() if (ROOT / name).read_bytes() != data]
        current_names = {p.as_posix() for p in builder.collect(ROOT)}
        if changed or current_names != set(sources) or template_path.read_text(encoding="utf-8-sig") != template:
            raise RuntimeError("Source or template changed during release build: " + ", ".join(changed))
        release = {"snapshot": VERSION, "captured_at": datetime.now(timezone.utc).isoformat(),
                   "builder": "tools/build_dist.py", "editions": editions,
                   "shared_pdf_pipeline": True, "sample": "fictional document with manual translation",
                   "published": False}
        release_text = json.dumps(release, ensure_ascii=False, indent=2)
        release_name = "release-" + hashlib.sha256(release_text.encode()).hexdigest()[:12] + ".json"
        replacements = {"__VERSION__": VERSION, "__RELEASE_NAME__": release_name}
        for name, edition in editions.items():
            prefix = "__" + name.upper()
            replacements[prefix + "_ZIP__"] = edition["filename"]
            replacements[prefix + "_HASH__"] = edition["sha256"]
            replacements[prefix + "_SIZE__"] = f"{edition['bytes'] / 1024:.0f} KB"
        html = template
        for token, value in replacements.items():
            html = html.replace(token, value)
        if re.search(r"__[A-Z_]+__", html):
            raise RuntimeError("Unresolved template token")
        if 'name="robots" content="noindex, nofollow, nosnippet"' not in html:
            # Preserve search exclusion even when template formatting changes.
            if not re.search(r'name="robots"[^>]*noindex', html):
                raise RuntimeError("Search exclusion missing")
        for path, destination in staged:
            os.replace(path, destination)
        atomic_text(downloads / release_name, release_text)
        atomic_text(downloads / "release.json", release_text)
        atomic_text(downloads / "START_HERE.md", "# はじめに\n\nClaude版またはCodex版を選び、ZIPを展開してsetup.batを実行します。"
                    "翻訳アプリ.batでローカル画面を開き、ご自身のGeminiキーを登録します。"
                    "構造補正は各版のCLIにご自身の契約でログインして利用します。\n\n"
                    "詳しい手順はZIP内のREADME.md、docs/INSTALL.md、Codex版はdocs/CODEX.mdをご覧ください。"
                    "このサイト自体に翻訳機能やキー入力欄はありません。\n")
        # Last write switches the page only after both immutable downloads are ready.
        atomic_text(DIST / "index.html", html)
    print(json.dumps({"snapshot": VERSION, "editions": {k: {n: v[n] for n in ("filename", "bytes", "sha256", "file_count")}
                                                       for k, v in editions.items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
