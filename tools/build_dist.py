"""配布用 zip を作る: python tools/build_dist.py [--out dist] [--date YYYYMMDD]

dist/ReadableJP-<日付>.zip を作り、秘密情報・テスト PDF が混ざっていないか検査する。
含める: コード・prompts・設定・bat・README・docs/API.md・WEBAPP.md・INSTALL.md
除外: work/ out/ english_paper/ tests/ 開発用 docs .venv キャッシュ secrets
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import re
import stat
import sys
import tempfile
import tomllib
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP = "ReadableJP"

# ---- allowlist: ここに書いたものだけを配布する (新しい開発用ファイルが自動で混ざらない) ----
ALLOW_FILES = {
    "README.md", "config.toml", "glossary_fixed.toml", "requirements.txt",
    "setup.bat", "翻訳アプリ.bat", "翻訳する.bat",
    "docs/API.md", "docs/WEBAPP.md", "docs/INSTALL.md",
    "config.codex.toml", "docs/CODEX.md",
}
# Accept the intended Codex entry points, without allowing arbitrary root scripts.
CODEX_BAT_RE = re.compile(r"^(?:翻訳アプリ|翻訳する|起動)[_-]?codex\.bat$", re.I)
# ディレクトリ -> 許可する拡張子
ALLOW_DIRS = {
    "readable": {".py"},
    "readable/web_static": {".html"},
    "prompts": {".md"},
    "bat_messages": {".txt"},
}
# allowlist に合っていても除外する名前 (多重防御。以前の事故の再発防止)
DENY_NAME_RE = re.compile(r"(^_patch.*\.py$|^scratch.*\.py$|^pytest\.ini$|^CODEX_REVIEW.*|^secrets\.json$|^settings\.json$|^\.env$|"
                          r"^last_error\.log$|^\.app\.(lock|json)$|^auth\.json$|^\.credentials.*$|"
                          r"^(?:codex[_-]?)?(?:auth|credentials|tokens?)[_.-].*$)", re.I)
DENY_DIR_NAMES = {"__pycache__", ".venv", "venv", "work", "out", "english_paper", "tests", ".git", "dist", ".pytest_cache",
                  "website", "codex-review", "response_parts", "eval", ".codex", ".claude", ".cache",
                  "cache", "codex-cache", "codex_cache", "sessions", "logs"}

SECRET_PATTERNS = [
    re.compile(rb"AIza[0-9A-Za-z_\-]{30,}"),
    # Match literal token values, not regex definitions or environment lookups.
    re.compile(rb"(?:gemini_api_key|openai_api_key|codex_api_key)[\"']?\s*[:=]\s*[\"'][0-9A-Za-z_\-]{8,}[\"']", re.I),
    re.compile(rb"sk-(?:(?:ant|proj|svcacct)-)?[0-9A-Za-z_\-]{20,}"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

REQUIRED_FILES = {
    "README.md", "config.toml", "glossary_fixed.toml", "requirements.txt", "setup.bat",
    "翻訳アプリ.bat", "翻訳する.bat", "docs/API.md", "docs/WEBAPP.md", "docs/INSTALL.md",
    "readable/web_static/index.html",
    *(f"readable/{m}.py" for m in (
        "__init__", "__main__", "cli", "config", "extract", "fonts", "gemini_client", "glossary",
        "labels", "numcheck", "render", "structure", "timing", "translate", "webapp", "assist",
        "codex_provider", "codex_assist")),
    *(f"prompts/{p}.md" for p in ("claude_structure", "gemini_translate", "gemini_refine", "gemini_glossary",
                                "assist_glossary", "assist_correct")),
    *(f"bat_messages/{m}.txt" for m in (
        "nopython", "nopackages", "usage", "done", "setup_start", "setup_done", "setup_fail",
        "setup_oldpython", "webapp_start", "webapp_stopped", "webapp_running")),
    *(f"bat_messages/rc{n}.txt" for n in (1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
}
# Runtime modules are required even for the legacy build entry point because
# both application entry points register providers before starting.
VARIANT_FILES = {"config.codex.toml", "docs/CODEX.md"}


def _check_variant(variant: str | None) -> None:
    if variant not in (None, "claude", "codex"):
        raise DistError(f"unknown variant: {variant!r} (claude | codex)")


def required_files(variant: str | None = None) -> set[str]:
    _check_variant(variant)
    # Both variants ship the same source and entry points.
    return REQUIRED_FILES | (VARIANT_FILES if variant is not None else set())


def _provider_config(data: bytes, provider: str) -> bytes:
    """Change only structure.provider; retain comments and reject unsupported TOML forms."""
    try:
        text = data.decode("utf-8-sig")
        before = tomllib.loads(text)
        structure = before.get("structure", {})
        if not isinstance(structure, dict):
            raise DistError("config.toml: structure must be a table")
        lines = text.splitlines(keepends=True)
        newline = "\r\n" if "\r\n" in text else "\n"
        header = re.compile(r"^\s*\[\s*(?:structure|\"structure\"|'structure')\s*\]\s*(?:#.*)?$")
        start = next((i for i, line in enumerate(lines) if header.fullmatch(line.rstrip("\r\n"))), None)
        key = re.compile(r"^(\s*(?:provider|\"provider\"|'provider')\s*=\s*).*$")
        if start is None:
            if "provider" in structure or any(re.match(r"^\s*structure\s*=", line) for line in lines):
                raise DistError("config.toml: use a [structure] table for variant injection")
            text = text.rstrip("\r\n") + f'{newline}{newline}[structure]{newline}provider = "{provider}"{newline}'
        else:
            end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[", lines[i])), len(lines))
            index = next((i for i in range(start + 1, end) if key.fullmatch(lines[i].rstrip("\r\n"))), None)
            if index is None:
                if not lines[start].endswith(("\n", "\r")):
                    lines[start] += newline
                lines.insert(start + 1, f'provider = "{provider}"{newline}')
            else:
                old = lines[index].rstrip("\r\n")
                prefix = key.fullmatch(old).group(1)
                tail = re.search(r'(?:"[^"\r\n]*"|\x27[^\x27\r\n]*\x27)(\s*(?:#.*)?)$', old)
                comment = tail.group(1) if tail else ""
                lines[index] = f'{prefix}"{provider}"{comment}{newline}'
            text = "".join(lines)
        expected = dict(before)
        expected["structure"] = {**structure, "provider": provider}
        if tomllib.loads(text) != expected:
            raise DistError("config.toml: injection changed other settings")
        return text.encode("utf-8")
    except (UnicodeError, tomllib.TOMLDecodeError) as e:
        raise DistError("config.toml: invalid TOML for variant injection") from e


class DistError(Exception):
    pass


def is_allowed(rel: Path) -> bool:
    """zip に入れてよいファイルか (allowlist かつ denylist に当たらない)。rel は配布フォルダ内の相対パス。"""
    rel = Path(*rel.parts)
    if rel.is_absolute() or any(p in (".", "..") or ":" in p or "\\" in p for p in rel.parts):
        return False
    if any(p.lower() in DENY_DIR_NAMES for p in rel.parts[:-1]) or DENY_NAME_RE.match(rel.name):
        return False
    key = rel.as_posix()
    if key in ALLOW_FILES or (len(rel.parts) == 1 and CODEX_BAT_RE.fullmatch(rel.name)):
        return True
    parent = rel.parent.as_posix()
    return parent in ALLOW_DIRS and rel.suffix.lower() in ALLOW_DIRS[parent] and rel.name != "__init__.py.bak"


def collect(root: Path = ROOT) -> list[Path]:
    files: list[Path] = []
    for d in ALLOW_DIRS:
        base = root / d
        if base.is_dir():
            files += [p.relative_to(root) for p in sorted(base.iterdir()) if p.is_file() and is_allowed(p.relative_to(root))]
    files += [Path(f) for f in sorted(ALLOW_FILES) if (root / f).is_file()]
    files += [p.relative_to(root) for p in root.glob("*.bat") if p.is_file() and is_allowed(p.relative_to(root))]
    return sorted(set(files))


def verify_zip(path: Path, extra_secrets: list[str] | None = None, *,
               variant: str | None = None, required: set[str] | None = None) -> None:
    """秘密情報・PDF・開発用ディレクトリが zip に入っていれば DistError。"""
    extra = [s.encode("utf-8") for s in (extra_secrets or []) if s and len(s) >= 8]
    _check_variant(variant)
    if variant is not None and required is None:
        required = required_files(variant)
    for name in ("GEMINI_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY",
                 "OPENAI_ACCESS_TOKEN", "CODEX_ACCESS_TOKEN"):
        value = os.environ.get(name, "")
        if len(value) >= 8:
            extra.append(value.encode("utf-8"))
    problems = []
    payload = {}
    seen = set()
    try:
        with zipfile.ZipFile(path) as z:
            bad_crc = z.testzip()
            if bad_crc is not None:
                raise DistError(f"ZIP CRC mismatch: {bad_crc}")
            for info in z.infolist():
                name = info.orig_filename
                parts = name.rstrip("/").split("/")
                if (not parts or parts[0] != TOP or "\\" in name
                        or any(not p or p in (".", "..") or any(c in p for c in '<>:"|?*') for p in parts)):
                    problems.append(f"unsafe ZIP path: {name}")
                    continue
                if name.casefold() in seen:
                    problems.append(f"duplicate ZIP path: {name}")
                    continue
                seen.add(name.casefold())
                if stat.S_ISLNK(info.external_attr >> 16):
                    problems.append(f"symlink in ZIP: {name}")
                    continue
                rel = Path(*parts[1:])
                if info.is_dir():
                    if any(p.lower() in DENY_DIR_NAMES for p in parts[1:]):
                        problems.append(f"forbidden ZIP directory: {name}")
                    continue
                if len(parts) < 2 or not is_allowed(rel):
                    problems.append(f"allowlist に無いファイル: {name}")
                    continue
                data = z.read(info)
                payload[rel.as_posix()] = data
                if any(pat.search(data) for pat in SECRET_PATTERNS):
                    problems.append(f"秘密情報らしき文字列: {name}")
                elif any(s in data for s in extra):
                    problems.append(f"秘密情報 (登録済みキー等) を含む: {name}")
    except (OSError, zipfile.BadZipFile, RuntimeError, zlib.error, EOFError, NotImplementedError) as e:
        raise DistError("ZIP read/CRC verification failed") from e
    if required is not None:
        missing = required - payload.keys()
        if missing:
            problems.append("missing required files: " + ", ".join(sorted(missing)))
    if variant is not None and not problems:
        try:
            cfg = tomllib.loads(payload["config.toml"].decode("utf-8-sig"))
            codex_cfg = tomllib.loads(payload["config.codex.toml"].decode("utf-8-sig"))
            if cfg.get("structure", {}).get("provider") != variant:
                problems.append(f"config.toml provider must be {variant}")
            if codex_cfg.get("structure", {}).get("provider") != "codex":
                problems.append("config.codex.toml provider must be codex")
        except (KeyError, AttributeError, UnicodeError, tomllib.TOMLDecodeError) as e:
            raise DistError("invalid variant configuration") from e
        launchers = {n: data for n, data in payload.items() if CODEX_BAT_RE.fullmatch(n)}
        if not launchers:
            problems.append("missing Codex launcher (*.bat)")
        for name, data in launchers.items():
            commands = b"\n".join(line for line in data.splitlines()
                                  if not re.match(rb"\s*(?:@?rem\b|::)", line, re.I))
            if not re.search(rb"--structure-provider(?:\s+|=)[\"']?codex(?:[\"']?(?:\s|$))", commands, re.I):
                problems.append(f"Codex launcher lacks --structure-provider codex: {name}")
    if problems:
        raise DistError("\n".join(problems))


def _stamp(path: Path) -> tuple[int, ...]:
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _safe_source(root: Path, rel: Path) -> Path:
    path = root / rel
    if (any(root.joinpath(*rel.parts[:i]).is_symlink() for i in range(1, len(rel.parts) + 1))
            or not path.resolve().is_relative_to(root.resolve())):
        raise DistError(f"symlink or outside source root: {rel.as_posix()}")
    return path


def _assert_snapshot(root: Path, snapshot: dict[Path, tuple[tuple[int, ...], bytes]]) -> None:
    if set(collect(root)) != snapshot.keys():
        raise DistError("source files changed during build (added/deleted)")
    for rel, (stamp, data) in snapshot.items():
        path = _safe_source(root, rel)
        if (_stamp(path) != stamp or hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(data).digest()
                or _stamp(path) != stamp):
            raise DistError(f"source changed during build: {rel.as_posix()}")


def build(out_dir: Path, date: str, root: Path = ROOT, variant: str | None = None) -> Path:
    """Build from one checked source snapshot; None preserves the legacy name/config."""
    required = required_files(variant)
    if not re.fullmatch(r"[0-9]{8}", date):
        raise DistError("date must be YYYYMMDD")
    try:
        day = datetime.datetime.strptime(date, "%Y%m%d")
    except ValueError as e:
        raise DistError("date must be a valid YYYYMMDD") from e
    out_dir, root = Path(out_dir), Path(root)
    suffix = "" if variant is None else "-" + variant.title()
    zpath = out_dir / f"ReadableJP{suffix}-{date}.zip"
    temporary = None
    try:
        files = collect(root)
        missing = required - {f.as_posix() for f in files}
        if missing:
            raise DistError("missing required files: " + ", ".join(sorted(missing)))
        stamps = {rel: _stamp(_safe_source(root, rel)) for rel in files}
        snapshot = {}
        for rel in files:
            data = _safe_source(root, rel).read_bytes()
            if _stamp(root / rel) != stamps[rel]:
                raise DistError(f"source changed during build: {rel.as_posix()}")
            snapshot[rel] = (stamps[rel], data)
        _assert_snapshot(root, snapshot)
        out_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{zpath.stem}-", suffix=".tmp", dir=out_dir)
        os.close(fd)
        temporary = Path(name)
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as z:
            for rel, (_, data) in snapshot.items():
                if variant is not None and rel.as_posix() == "config.toml":
                    data = _provider_config(data, variant)
                info = zipfile.ZipInfo(f"{TOP}/{rel.as_posix()}", (max(day.year, 1980), day.month, day.day, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                z.writestr(info, data)
        verify_zip(temporary, variant=variant, required=required)
        _assert_snapshot(root, snapshot)
        os.replace(temporary, zpath)
        return zpath
    except (OSError, zipfile.BadZipFile) as e:
        raise DistError("source read or archive write failed") from e
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--date", default=datetime.date.today().strftime("%Y%m%d"))
    ap.add_argument("--variant", choices=("claude", "codex"), default=None)
    a = ap.parse_args(argv)
    try:
        z = build(Path(a.out), a.date, variant=a.variant)
    except DistError as e:
        print("配布 zip の検査に失敗しました:\n" + str(e), file=sys.stderr)
        return 1
    with zipfile.ZipFile(z) as zf:
        n = len(zf.namelist())
    print(f"{z}  ({n} files, {z.stat().st_size // 1024} KB) 検査 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
