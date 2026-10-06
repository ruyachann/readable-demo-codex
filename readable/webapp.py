"""ローカル Web アプリ: python -m readable.webapp [--port 8765] [--no-browser]

標準ライブラリのみ。127.0.0.1 だけに bind し、起動ごとのトークンで API を保護する。
ジョブは `python -m readable` を subprocess で 1 本ずつ直列に実行する。仕様は docs/API.md。
"""
from __future__ import annotations

import argparse
import contextlib
import email.parser
import email.policy
import hashlib
import importlib.util
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "web_static"
MAX_UPLOAD = 200 * 1024 * 1024
KEEP_DOCS = 3  # work/ に残す翻訳キャッシュ (文書) の数
RESUME_MARK = ".resume_after_exit7"
LOG_KEEP = 20
COOKIE = "readable_token"
EXIT_ALREADY_RUNNING = 3

# 終了コード -> 日本語メッセージ (cli.py の表に対応)
EXIT_MESSAGES = {
    0: "変換が完了しました。",
    1: "想定外のエラーが起きました。ログ末尾を確認してください。ここまでの翻訳はキャッシュに保存されているので、再実行すれば続きから再開します。",
    2: "入力ファイルが見つかりませんでした。",
    3: "翻訳器を使えません。環境変数 GEMINI_API_KEY が設定されていないか、ライブラリが不足しています。",
    4: "パスワード付き PDF です。パスワードを解除した PDF を指定してください。",
    5: "PDF として開けませんでした。ファイルが壊れているか、PDF ではない可能性があります。",
    6: "翻訳対象のテキストがありません。スキャン PDF は OCR 文字層が必要です。画像だけの PDF に文字認識を行う機能はありません。",
    7: "Gemini の日次上限です。上限が解除された後 (翌日) に「この段落だけ再翻訳する」ボタンを押すと、同じファイルで続きから再開します。",
    8: "設定・権限の誤りです (API キーが不正、モデル名の誤り、利用地域など)。再試行しても直りません。API キーと config.toml を確認してください。",
    9: "Gemini API が回復しませんでした (サーバーエラー・タイムアウト・分あたり上限)。時間をおいて「この段落だけ再翻訳する」ボタンを押すと、同じファイルで続きから再開します。",
    10: "入力データの誤りです (charmap.json が壊れている等)。ログを確認してください。",
    11: "日本語フォントが見つかりません。config.toml の [fonts] に日本語フォントのパスを指定してください。",
    12: "一部の段落が翻訳できず原文のまま出力されました。PDF は出力されています。下の「この段落だけ再翻訳する」ボタンで、その段落だけ再翻訳します (ファイルを選び直す必要はありません)。",
    130: "中止しました。アップロードした PDF とここまでの翻訳は残してあります。「この段落だけ再翻訳する」ボタンを押すと続きから再開します。",
}
STAGES = [("extract", "[1/4]"), ("structure", "[2/4]"), ("translate", "[3/4]"), ("render", "[4/4]")]


def exit_message(code: int | None) -> str:
    if code is None:
        return ""
    return EXIT_MESSAGES.get(code, f"終了コード {code} で終了しました。ログ末尾を確認してください。")


def sanitize_stem(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", stem).strip(" .")
    stem = re.sub(r"\s+", " ", stem)[:80].strip(" .")
    if not stem or stem.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(COM|LPT)\d", stem.upper()):
        stem = "document"
    return stem


# ---------------------------------------------------------------------------
# 設定 (API キー・同意) と初回セットアップの診断
# ---------------------------------------------------------------------------
CONSENT_VERSION = 3  # 3: 翻訳の補助 (assist) の送信内容を同意文に追加 (再同意)
KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{20,128}$")


def default_home() -> Path:
    """設定の置き場。環境変数 READABLEJP_HOME (テスト用) > %APPDATA%/ReadableJP > ~/.readablejp。環境変数やレジストリは変更しない。"""
    if os.environ.get("READABLEJP_HOME"):
        return Path(os.environ["READABLEJP_HOME"])
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "ReadableJP" if appdata else Path.home() / ".readablejp"


def restrict_permissions(path: Path) -> None:
    """現在のユーザーだけが読めるようにする (できる範囲で。失敗しても動作は続ける)。"""
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME")
            if user:
                grant = f"{user}:(OI)(CI)F" if path.is_dir() else f"{user}:F"
                subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant], shell=False,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        else:
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
    except (OSError, subprocess.SubprocessError):
        pass


def mask_key(key: str) -> str:
    return "\u2022" * 8 + key[-4:] if len(key) >= 8 else "\u2022" * 8


@contextlib.contextmanager
def settings_write_lock(home: Path):
    """Serialize read/modify/replace across separately running editions."""
    new_dir = not home.exists()
    home.mkdir(parents=True, exist_ok=True)
    if new_dir:
        restrict_permissions(home)
    fh = open(home / ".settings.lock", "a+b")
    acquired = False
    try:
        deadline = time.monotonic() + 15
        while not acquired:
            try:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("設定の保存が混み合っています。もう一度お試しください。")
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


class Settings:
    """home/secrets.json (API キー) と home/settings.json (同意)。キーは API・ログに出さない。"""

    def __init__(self, home: Path, consent_scope: str = "claude"):
        self.home = home
        self.consent_scope = consent_scope
        self.lock = threading.Lock()

    def _read(self, name: str) -> dict:
        try:
            d = json.loads((self.home / name).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, name: str, data: dict) -> None:
        new_dir = not self.home.exists()
        self.home.mkdir(parents=True, exist_ok=True)
        if new_dir:
            restrict_permissions(self.home)
        tmp = self.home / (name + "." + secrets.token_hex(8) + ".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            if name == "secrets.json":
                restrict_permissions(tmp)
            os.replace(tmp, self.home / name)
        finally:
            tmp.unlink(missing_ok=True)

    def get_key(self) -> tuple[str, str]:
        """(キー, 出どころ env|file|none)。環境変数 GEMINI_API_KEY が優先。"""
        env = (os.environ.get("GEMINI_API_KEY") or "").strip()
        if env:
            return env, "env"
        k = str(self._read("secrets.json").get("gemini_api_key") or "").strip()
        return (k, "file") if k else ("", "none")

    def set_key(self, key: str) -> None:
        with self.lock, settings_write_lock(self.home):
            d = self._read("secrets.json")
            d["gemini_api_key"] = key
            self._write("secrets.json", d)

    def delete_key(self) -> None:
        with self.lock, settings_write_lock(self.home):
            d = self._read("secrets.json")
            d.pop("gemini_api_key", None)
            self._write("secrets.json", d)

    def consented(self) -> bool:
        data = self._read("settings.json")
        scopes = data.get("consent_providers")
        if isinstance(scopes, dict):
            return scopes.get(self.consent_scope) == CONSENT_VERSION
        return data.get("consent_version") == CONSENT_VERSION and data.get("consent_provider") == self.consent_scope

    def set_consent(self) -> None:
        with self.lock, settings_write_lock(self.home):
            d = self._read("settings.json")
            d["consent_version"] = CONSENT_VERSION
            d["consent_provider"] = self.consent_scope
            scopes = d.get("consent_providers")
            scopes = dict(scopes) if isinstance(scopes, dict) else {}
            scopes[self.consent_scope] = CONSENT_VERSION
            d["consent_providers"] = scopes
            d["consent_at"] = int(time.time())
            self._write("settings.json", d)


def check_gemini_key(key: str) -> tuple[bool, str]:
    """モデル一覧を 1 回だけ取得して接続を確認する。-> (成功, 日本語メッセージ)。メッセージにキーは含めない。"""
    req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                                 headers={"x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read(2000)
        return True, "接続できました。API キーは有効です。"
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            return False, "API キーが無効、または権限がありません。キーをコピーし直してください。"
        if e.code == 429:
            return True, "キーは有効です (現在は利用上限に達しています。時間をおいてください)。"
        return False, f"Google 側のエラーです (HTTP {e.code})。時間をおいて再試行してください。"
    except (urllib.error.URLError, OSError, TimeoutError):
        return False, "Google に接続できませんでした。インターネット接続を確認してください。"


def _cli_status(name: str, args: list[str]) -> dict:
    exe = shutil.which(name)
    if not exe:
        return {"found": False, "logged_in": None}
    try:
        r = subprocess.run([exe, *args], shell=False, stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                           encoding="utf-8", errors="replace")
        text = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0 and not re.search(r"not logged|logged out|no active|please log", text, re.I):
            return {"found": True, "logged_in": True}
        return {"found": True, "logged_in": False}
    except (OSError, subprocess.SubprocessError):
        return {"found": True, "logged_in": None}  # 判定できない


_FONT_CACHE: list[dict] = []


def check_fonts() -> dict:
    if _FONT_CACHE:
        return _FONT_CACHE[0]
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from readable.config import load_config
        from readable.fonts import build_fonts
        fs = build_fonts(load_config(None))
        res = {"ok": True, "detail": ", ".join(sorted({Path(v).name for v in fs.files.values()}))[:120]}
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "detail": "日本語フォント (游明朝/游ゴシック/メイリオ) が見つかりません。config.toml の [fonts] にパスを指定してください。"
               if isinstance(e, RuntimeError) else f"確認できませんでした ({type(e).__name__})"}
    if res["ok"]:
        _FONT_CACHE.append(res)
    return res


def _find_spec(mod: str):
    try:
        return importlib.util.find_spec(mod)
    except (ImportError, ValueError):
        return None


def structure_status(provider: str, st: dict) -> dict:
    """構成の整理 (構造補正) を、いま使えるか。使えない場合は理由と直し方 (画面で確認してから簡易解析で続ける)。"""
    name = {"claude": "Claude Code", "codex": "Codex"}.get(provider, provider)
    r = {"provider": provider, "name": name, "enabled": provider != "none", "usable": True, "reason": "", "fix": ""}
    if provider == "none":
        return r
    if provider == "claude":
        try:
            from .structure import detect_api_env
            found = detect_api_env()
        except Exception:  # noqa: BLE001
            found = []
        c = st.get("claude") or {}
        if found:
            r.update(usable=False, reason=f"API キー/ゲートウェイの設定 ({', '.join(found)}) を検出したため (API 課金を避けて使いません)",
                     fix="これらの環境変数を外してから、アプリを起動し直してください。")
        elif not c.get("found"):
            r.update(usable=False, reason="Claude Code がインストールされていません",
                     fix="Claude Code を導入し、コマンドプロンプトで claude を起動して /login でログインしてください。")
        elif c.get("logged_in") is False:
            r.update(usable=False, reason="Claude Code にログインしていません",
                     fix="コマンドプロンプトで claude を起動し、/login でログインしてください。")
        return r
    from .codex_provider import API_ENV
    found = [key for key in API_ENV if os.environ.get(key, "").strip()]
    c = st.get("codex") or {}
    if found:
        r.update(usable=False, reason=f"API キー/ゲートウェイの設定 ({', '.join(found)}) を検出したため (API 課金を避けて使いません)",
                 fix="これらの環境変数を外してから、アプリを起動し直してください。")
    elif not c.get("found"):
        r.update(usable=False, reason="Codex CLI がインストールされていません",
                 fix="Codex CLI を導入し、codex login でログインしてください。")
    elif c.get("logged_in") is False:
        r.update(usable=False, reason=str(c.get("detail") or "Codex にログインしていません"), fix="ターミナルで codex login を実行してください。")
    return r


def setup_status(settings: Settings, fast: bool = False, provider: str = "claude") -> dict:
    py_ok = sys.version_info >= (3, 11)
    missing = [m for m, mod in (("PyMuPDF", "fitz"), ("fonttools", "fontTools"), ("google-genai", "google.genai"))
               if _find_spec(mod) is None]
    key, source = settings.get_key()
    fonts = {"ok": None, "detail": ""} if fast else (check_fonts() if not missing else {"ok": False, "detail": "ライブラリ導入後に確認します"})
    st = {
        "python": {"ok": py_ok, "version": "%d.%d.%d" % sys.version_info[:3]},
        "packages": {"ok": not missing, "missing": missing},
        "fonts": fonts,
        "gemini": {"configured": bool(key), "source": source, "masked": mask_key(key) if key else ""},
        "claude": _cli_status("claude", ["auth", "status"]) if provider == "claude" else {"found": False, "logged_in": None},
        "structure_provider": provider,
        "consent": settings.consented(),
    }
    if provider == "codex":
        from .codex_provider import codex_status
        st["codex"] = codex_status()
    st["structure"] = structure_status(provider, st) if not fast else {"provider": provider}
    st["assist"] = {"provider": provider, "supported": assist_supported(provider) and provider != "none"}
    st["ready"] = bool(py_ok and not missing and fonts["ok"] and st["gemini"]["configured"] and st["consent"])
    return st


DOC_DIR_RE = re.compile(r"^.+-[0-9a-f]{12}$")


def doc_cache_name(pdf: Path) -> str:
    """work/ 内の文書ディレクトリ名。readable.cli.work_name と同じ規則 (<stem>-<内容 sha256 先頭 12 桁>)。"""
    h = hashlib.sha256()
    with open(pdf, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return f"{pdf.stem}-{h.hexdigest()[:12]}"


def _last_used(d: Path) -> float:
    """文書ディレクトリを最後に更新した時刻 (再開用の印ファイルは除く)。"""
    ts = []
    for p in d.iterdir():
        if p.name != RESUME_MARK:
            try:
                ts.append(p.stat().st_mtime)
            except OSError:
                pass
    return max(ts) if ts else d.stat().st_mtime


def prune_work_cache(work_root: Path, keep: int = KEEP_DOCS) -> list[str]:
    """work/<stem>-<hash>/ (翻訳キャッシュ) を、最近使った順に keep 件だけ残して削除する。
    日次上限 (exit 7) で止まった文書 (RESUME_MARK 付き) は削除しない。work/web_jobs などの他のものには触れない。-> 削除した名前"""
    if not work_root.is_dir():
        return []
    docs = []
    for d in work_root.iterdir():
        try:
            if d.is_dir() and DOC_DIR_RE.match(d.name) and d.name != "web_jobs":
                docs.append((_last_used(d), d))
        except OSError:
            pass
    docs.sort(key=lambda x: x[0], reverse=True)
    removed = []
    for _, d in docs[keep:]:
        if (d / RESUME_MARK).exists():
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    return removed


# ---------------------------------------------------------------------------
# 二重起動の防止 (データディレクトリ単位のプロセス間ロック)
# ---------------------------------------------------------------------------
LOCK_NAME, INFO_NAME = ".app.lock", ".app.json"


class AlreadyRunning(Exception):
    """同じデータディレクトリを使うアプリが既に動いている。info は先行アプリの {pid, port, url} (読めなければ空)。"""

    def __init__(self, info: dict):
        super().__init__("already running")
        self.info = info


def pid_alive(pid: int) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            k.OpenProcess.restype = ctypes.c_void_p
            k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            h = k.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, OverflowError):
        return False


class InstanceLock:
    """OS のファイルロック (Windows: msvcrt.locking、他: flock)。プロセスが死ねば OS が解放するので、残骸の掃除は
    「ロックを取れた = 先行アプリは生きていない」ことを確認してからだけ行える。"""

    def __init__(self, directory: Path):
        self.dir = directory
        self.fh = None

    @property
    def info_path(self) -> Path:
        return self.dir / INFO_NAME

    def read_info(self) -> dict:
        try:
            d = json.loads(self.info_path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def acquire(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.dir / LOCK_NAME, "a+b")
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise AlreadyRunning(self.read_info()) from None
        old = self.read_info()
        if old.get("pid") and old["pid"] != os.getpid() and pid_alive(old["pid"]):  # ロックが効かない環境への念のため
            fh.close()
            raise AlreadyRunning(old)
        self.fh = fh
        self.info_path.unlink(missing_ok=True)

    def publish(self, port: int, url: str) -> None:
        tmp = self.dir / (INFO_NAME + ".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "port": port, "url": url}), encoding="utf-8")
        restrict_permissions(tmp)
        os.replace(tmp, self.info_path)

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            self.info_path.unlink(missing_ok=True)
            self.fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            self.fh.close()
        except (OSError, ValueError):
            pass
        self.fh = None


# ---------------------------------------------------------------------------
# プロセスツリーの所有と停止 (Windows Job Object。使えなければ taskkill /T /F)
# ---------------------------------------------------------------------------
_K32 = None


def _kernel32():
    global _K32
    if _K32 is None:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = ctypes.c_void_p
        k.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k.TerminateJobObject.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        k.CloseHandle.restype = wintypes.BOOL
        _K32 = k
    return _K32


def create_job_object():
    """KILL_ON_JOB_CLOSE 付きの Job Object を作り、ハンドル (int) を返す。Windows 以外・失敗時は None。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        k = _kernel32()

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]

        class IOC(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("a", "b", "c", "d", "e", "f")]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        h = k.CreateJobObjectW(None, None)
        if not h:
            return None
        info = EXT()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):  # JobObjectExtendedLimitInformation
            k.CloseHandle(h)
            return None
        return int(h)
    except (OSError, AttributeError, ValueError):
        return None


def assign_to_job_object(handle, proc) -> bool:
    try:
        return bool(_kernel32().AssignProcessToJobObject(handle, int(proc._handle)))  # noqa: SLF001
    except (OSError, AttributeError, ValueError):
        return False


def close_job_object(handle) -> None:
    if handle:
        try:
            _kernel32().CloseHandle(handle)
        except (OSError, AttributeError):
            pass


def kill_tree_fallback(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], shell=False, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        pass


def stop_process_tree(proc, jobobj=None, timeout: float = 10.0) -> bool:
    """取消・置換・アプリ終了で共通の停止処理。プロセスツリーごと止め、終了を確認する (確認できれば True)。"""
    if proc is None:
        return True
    if jobobj:
        try:
            _kernel32().TerminateJobObject(jobobj, 1)
        except (OSError, AttributeError):
            kill_tree_fallback(proc.pid)
    elif proc.poll() is None:
        kill_tree_fallback(proc.pid)
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        kill_tree_fallback(proc.pid)
        try:
            proc.terminate()
            proc.wait(timeout)
        except (subprocess.TimeoutExpired, OSError):
            pass
    return proc.poll() is not None


# ---------------------------------------------------------------------------
# work ディレクトリの解決 (CLI と同じ規則)
# ---------------------------------------------------------------------------
def resolve_cache_root(work_dir: Path | None) -> Path:
    if work_dir:
        return Path(work_dir)
    try:
        from .cli import work_root
        from .config import load_config
        return work_root(load_config(None), None)
    except Exception:  # noqa: BLE001 - ライブラリ未導入など
        return ROOT / "work"


def resolve_doc_dir(wroot: Path, pdf: Path) -> Path:
    """CLI が実際に使う文書 work ディレクトリ (同じ内容の改名コピーは旧名のディレクトリを再利用する)。"""
    try:
        from .cli import work_dir_for
        return work_dir_for(wroot, pdf)
    except ImportError:
        d = wroot / doc_cache_name(pdf)
        if not d.exists():
            h = d.name.rsplit("-", 1)[1]
            for c in sorted(wroot.glob(f"*-{h}")) if wroot.exists() else []:
                if c.is_dir():
                    return c
        return d


class Job:
    def __init__(self, jid: str, stem: str, options: dict, directory: Path):
        self.id, self.stem, self.options, self.dir = jid, stem, options, directory
        self.name = stem + ".pdf"
        self.status = "queued"
        self.stage = ""
        self.exit_code: int | None = None
        self.log: deque[str] = deque(maxlen=LOG_KEEP)
        self.created = time.time()
        self.proc: subprocess.Popen | None = None
        self.deleted = False
        self.jobobj = None
        self.work_dir: Path | None = None
        self.pages: int | None = None
        self.bytes = 0
        self.failed_paragraphs: int | None = None
        self.structure_note: str | None = None  # ログの該当行 (タグなし)
        self.structure_kind: str | None = None  # skipped | rejected | partial
        self.structure_reason = ""
        self.structure_counts: tuple[int, int] | None = None  # (反映, 不採用)
        self.cancelled = False
        self.attempt = 0  # 再試行前に残ったキュー項目を識別する
        self.structure_report_stamp: tuple[int, int] | None = None
        self.provider = "claude"
        self.assist_info: dict | None = None
        self.lock = threading.Lock()

    def _structure_message(self) -> str | None:
        kind, acc, rej = self.structure_kind, None, None
        if kind in ("skipped", "rejected", "partial"):
            if self.structure_counts:
                acc, rej = self.structure_counts
                if kind == "rejected":
                    acc = 0
            return structure_message(kind, self.structure_reason, acc, rej)
        if self.structure_counts:
            acc, rej = self.structure_counts
            if rej > 0:
                kind = kind if kind == "skipped" else ("partial" if acc > 0 else "rejected")
            elif kind != "skipped":
                kind = None  # すべて反映された
        return structure_message(kind, self.structure_reason, acc, rej)

    def _assist_message(self) -> str | None:
        if self.status not in ("done", "failed") or self.exit_code in (None, 130):
            return None
        return assist_message(self.assist_info, self.provider, bool(self.options.get("assist", True)))

    def can_retry(self) -> bool:
        """終了コード 7 (日次上限)・9 (API の回復不能)・12 (一部失敗)・130 (中止) のとき、同じ PDF で再実行できる。"""
        return (self.status in ("done", "failed") and self.exit_code in (7, 9, 12, 130) and not self.deleted
                and (self.dir / "in" / f"{self.stem}.pdf").is_file())

    def outputs(self) -> dict:
        out = {}
        if self.status == "done" or self.exit_code == 130:  # 中止しても、すでにできていた出力は残す
            for k in ("ja", "dual"):
                if (self.dir / "out" / f"{self.stem}_{k}.pdf").is_file():
                    out[k] = f"/api/jobs/{self.id}/download/{k}"
        return out

    def to_dict(self) -> dict:
        with self.lock:
            return {"id": self.id, "name": self.name, "status": self.status, "stage": self.stage,
                    "log_tail": list(self.log), "exit_code": self.exit_code,
                    "message_ja": exit_message(self.exit_code), "warning": self.exit_code == 12,
                    "options": self.options, "created": int(self.created), "outputs": self.outputs(),
                    "pages": self.pages, "bytes": self.bytes, "failed_paragraphs": self.failed_paragraphs,
                    "structure_note": self.structure_note, "structure_message": self._structure_message(),
                    "assist_message": self._assist_message(),
                    "can_retry": self.can_retry()}


class App:
    def __init__(self, data_dir: Path, work_dir: Path | None, translator: str = "gemini", home: Path | None = None,
                 structure_provider: str | None = None):
        from .codex_assist import register
        register()
        self.jobs_dir = data_dir
        self.work_dir = work_dir
        self.translator = translator
        from .config import load_config
        self.structure_provider = structure_provider or str(load_config().get("structure", "provider", "claude"))
        if self.structure_provider not in ("claude", "codex", "none"):
            raise ValueError("構造解析の設定は claude | codex | none で指定してください")
        self.settings = Settings(home or default_home(), consent_scope=self.structure_provider)
        self.jobs: dict[str, Job] = {}
        self.q: queue.Queue[tuple[str, int]] = queue.Queue()
        self.token = secrets.token_urlsafe(24)
        self.cookie_name = COOKIE + "_" + secrets.token_hex(8)
        self.host_ok: set[str] = set()
        self.lock = threading.Lock()  # self.jobs の出し入れだけを短く保護する
        self.job_lock = threading.Lock()  # 「確認 → 削除 → 登録」をまとめて保護する (非再入。内部メソッドは取らない)
        self._cache_root = resolve_cache_root(work_dir)
        self.instance = InstanceLock(self.jobs_dir)
        self.instance.acquire()  # 清掃より前。取れなければ AlreadyRunning
        self.clear_jobs_dir()
        self.prune_cache()
        threading.Thread(target=self.worker, daemon=True).start()

    def clear_jobs_dir(self) -> None:
        """起動時に work/web_jobs/ の中身をすべて削除する (最新 1 件しか保持しない方針)。"""
        for d in self.jobs_dir.iterdir():
            if d.name in (LOCK_NAME, INFO_NAME):
                continue
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
            else:
                d.unlink(missing_ok=True)

    def cache_root(self) -> Path:
        return self._cache_root

    def prune_cache(self) -> list[str]:
        return prune_work_cache(self.cache_root(), KEEP_DOCS)

    def read_structure_report(self, job: "Job") -> None:
        """コア側が render_report.json の `structure` ({accepted, rejected, reason}) に書いた件数を読む (ログに無いときの補い)。"""
        if not job.work_dir:
            return
        try:
            report = job.work_dir / "render_report.json"
            stamp = report.stat()
            if (stamp.st_mtime_ns, stamp.st_size) == job.structure_report_stamp:
                return  # 描画前に停止した今回の実行へ、前回の結果を混ぜない
            data = json.loads(report.read_text(encoding="utf-8"))
            if isinstance(data.get("assist"), dict):
                with job.lock:
                    job.assist_info = data["assist"]
            st = data.get("structure")
            if isinstance(st, dict) and isinstance(st.get("accepted"), int) and isinstance(st.get("rejected"), int):
                with job.lock:
                    job.structure_counts = (st["accepted"], st["rejected"])
                    if st.get("status") in ("accepted", "partial", "skipped", "rejected"):
                        job.structure_kind = st["status"]
                    elif st.get("used") is False and job.structure_kind != "skipped":
                        job.structure_kind = "rejected" if any(job.structure_counts) else (job.structure_kind or "skipped")
                    if st.get("reason") and not job.structure_reason:
                        job.structure_reason = str(st["reason"])[:300]
        except (OSError, ValueError, AttributeError):
            pass

    def after_job(self, job: "Job", rc: int) -> None:
        """終了後: exit 7 の文書は再開用に印を付けて消さない。成功したら印を外す。古い文書のキャッシュを整理。"""
        try:
            d = job.work_dir or resolve_doc_dir(self.cache_root(), job.dir / "in" / f"{job.stem}.pdf")
            if rc == 7 and d.is_dir():
                (d / RESUME_MARK).write_text("exit 7", encoding="utf-8")
            elif rc in (0, 12):
                (d / RESUME_MARK).unlink(missing_ok=True)
            self.prune_cache()
        except OSError:
            pass

    def create_job(self, filename: str, data: bytes, options: dict) -> Job:
        jid = secrets.token_hex(8)
        d = self.jobs_dir / jid
        stem = sanitize_stem(filename)
        (d / "in").mkdir(parents=True)
        (d / "in" / f"{stem}.pdf").write_bytes(data)
        job = Job(jid, stem, options, d)
        job.provider = self.structure_provider
        job.bytes = len(data)
        job.pages = count_pages(d / "in" / f"{stem}.pdf")
        with self.lock:
            self.jobs[jid] = job
        self.q.put((jid, job.attempt))
        return job

    def retry_job(self, job: Job, structure: bool | None = None) -> bool:
        """保存してある同じ PDF を --retry-failed で再実行する (終了コード 7/9/12 のとき)。再添付は不要。"""
        with self.job_lock:
            if not job.can_retry() or job.id not in self.jobs:
                return False
            with job.lock:
                job.options = {**job.options, "retry": True}
                if structure is not None:
                    job.options["claude"] = structure
                job.status, job.stage, job.exit_code = "queued", "", None
                job.cancelled = False
                job.assist_info = None
                job.attempt += 1
                job.proc = None
                job.structure_report_stamp = None
                job.failed_paragraphs = job.structure_note = job.structure_kind = job.structure_counts = None
                job.structure_reason = ""
                job.log.clear()
            self.q.put((job.id, job.attempt))
            return True

    def submit(self, filename: str, data: bytes, options: dict, replace: bool) -> "Job | None":
        """実行中の確認 → 前回のジョブの削除 → 登録 を 1 つのロックで行う。実行中で replace が無ければ None (何も変えない)。"""
        with self.job_lock:
            if not replace and any(j.status in ("queued", "running") for j in list(self.jobs.values())):
                return None
            for old in list(self.jobs.values()):
                self._delete_job(old)
            return self.create_job(filename, data, options)

    def cancel_job(self, job: Job) -> bool:
        """「中止」: 処理を止める。ジョブ (アップロードした PDF・できていた出力・キャッシュ) は消さず、終了コード 130 で残す。"""
        with self.job_lock:
            if job.deleted or job.id not in self.jobs or job.status not in ("queued", "running"):
                return False
            with job.lock:
                job.cancelled = True
                if job.status == "queued":  # まだ始まっていない
                    job.status, job.exit_code = "failed", 130
            if job.status == "failed":
                return True
            # 停止が完了するまで再試行を登録させず、旧試行の取消を新試行へ持ち越さない。
            self.stop_job(job)
            return True

    def stop_job(self, job: Job) -> bool:
        """このジョブのプロセスツリーを止めて終了を確認する (取消・置換・アプリ終了で共通)。"""
        with job.lock:
            h, job.jobobj = job.jobobj, None
            proc = job.proc
        ok = stop_process_tree(proc, h)
        close_job_object(h)
        return ok

    def _delete_job(self, job: Job) -> None:
        with job.lock:
            job.deleted = True
            p = job.proc
        running = bool(p and p.poll() is None)
        if running or job.jobobj:
            self.stop_job(job)
        with self.lock:
            self.jobs.pop(job.id, None)
        if not running:
            shutil.rmtree(job.dir, ignore_errors=True)

    def delete_job(self, job: Job) -> None:
        with self.job_lock:
            self._delete_job(job)

    def stop_all(self) -> None:
        for j in list(self.jobs.values()):
            with j.lock:
                j.cancelled = True
            self.stop_job(j)

    def worker(self) -> None:
        while True:
            jid, attempt = self.q.get()
            with self.job_lock:
                job = self.jobs.get(jid)
                if job is None:
                    continue
                with job.lock:
                    if job.deleted or job.cancelled or job.status != "queued" or job.attempt != attempt:
                        continue
                    job.status = "running"
            try:
                self.run_job(job)
            except Exception as e:  # noqa: BLE001 - UI サーバを巻き込まない
                with job.lock:
                    job.status, job.exit_code = "failed", 130 if job.cancelled else 1
                    job.log.append(f"内部エラー: {type(e).__name__}")
            if job.deleted:
                shutil.rmtree(job.dir, ignore_errors=True)

    def command(self, job: Job) -> list[str]:
        o = job.options
        cmd = [sys.executable, "-m", "readable", str(job.dir / "in" / f"{job.stem}.pdf"), "--out", str(job.dir / "out"),
               "--mode", o["mode"], "--translator", self.translator,
               "--structure-provider", self.structure_provider]
        if not o["claude"]:
            cmd.append("--no-claude")
        if o["retry"]:
            cmd.append("--retry-failed")
        if not o.get("assist", True):
            cmd.append("--no-assist")  # 翻訳の補助 (用語集の点検・問題段落の補正) だけ止める。構成の整理は別
        if self.work_dir:
            cmd += ["--work-dir", str(self.work_dir)]
        return cmd

    def run_job(self, job: Job) -> None:
        with job.lock:
            if job.cancelled or job.deleted:
                job.status, job.exit_code = "failed", 130
                return
            job.status, job.stage = "running", "extract"
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        secret, _ = self.settings.get_key()
        if secret:
            env["GEMINI_API_KEY"] = secret  # 子プロセスには環境変数でのみ渡す
        try:
            job.work_dir = resolve_doc_dir(self.cache_root(), job.dir / "in" / f"{job.stem}.pdf")  # CLI が使う場所を先に確定
        except OSError:
            job.work_dir = None
        if job.work_dir:
            try:
                stamp = (job.work_dir / "render_report.json").stat()
                job.structure_report_stamp = (stamp.st_mtime_ns, stamp.st_size)
            except OSError:
                job.structure_report_stamp = None
        with job.lock:
            # 中止フラグの設定と、プロセス/Job Objectの登録を同じロックで同期する。
            if job.cancelled or job.deleted:
                job.status, job.exit_code = "failed", 130
                return
            job.proc = subprocess.Popen(self.command(job), cwd=str(ROOT), env=env, shell=False, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                                        start_new_session=(os.name != "nt"))
            jo = create_job_object()
            if jo and not assign_to_job_object(jo, job.proc):
                close_job_object(jo)
                jo = None
            job.jobobj = jo
        if job.deleted:  # 起動の直前に削除された
            self.stop_job(job)
        logf = None
        try:  # 画面のログは末尾だけなので、全文をジョブのフォルダにも書く (ジョブと一緒に削除される)
            logf = open(job.dir / "job.log", "a", encoding="utf-8", errors="replace", buffering=1)
            if job.options.get("retry"):
                logf.write("--- 再翻訳 (--retry-failed) ---\n")
        except OSError:
            logf = None
        for line in job.proc.stdout:
            line = line.rstrip()
            if secret and secret in line:
                line = line.replace(secret, "***")  # 画面にも job.log にも API キーを出さない
            if not line:
                continue
            if logf:
                try:
                    logf.write(line + "\n")
                except (OSError, ValueError):
                    logf = None
            with job.lock:
                job.log.append(line[:300])
                mc = STRUCT_COUNTS_RE.search(line)
                if not mc:
                    mc = STRUCT_LEGACY_COUNTS_RE.search(line)
                if mc:
                    job.structure_counts = (int(mc.group(1)), int(mc.group(2)))
                    if len(mc.groups()) >= 3 and mc.group(3) and not job.structure_reason:
                        job.structure_reason = mc.group(3).strip()[:300]
                ms = STRUCT_STATUS_RE.search(line)
                if ms:
                    job.structure_kind = ms.group(1)
                m = FAILED_PARAS_RE.search(line)
                if m:
                    job.failed_paragraphs = int(m.group(1))
                if job.structure_note is None and STRUCT_SKIP_RE.search(line):
                    job.structure_note = re.sub(r"^\[[^\]]*\]\s*", "", line)[:300]
                    job.structure_kind = "rejected" if STRUCT_REJECT_RE.search(line) else "skipped"
                    job.structure_reason = structure_reason(line)
                for name, tag in STAGES:
                    if line.startswith(tag):
                        job.stage = name
        rc = job.proc.wait()
        if logf:
            try:
                logf.close()
            except OSError:
                pass
        self.read_structure_report(job)
        with job.lock:
            h, job.jobobj = job.jobobj, None
        close_job_object(h)  # 残った子孫があれば KILL_ON_JOB_CLOSE で終了する
        with job.lock:
            if job.cancelled:
                rc = 130
        self.after_job(job, rc)
        with job.lock:
            if job.cancelled:
                rc = 130
            job.exit_code = rc
            ok = rc in (0, 12) and any((job.dir / "out").glob("*.pdf"))
            job.status = "done" if ok else "failed"
            if ok:
                job.stage = "finished"


def count_pages(path: Path) -> int | None:
    """アップロードした PDF のページ数 (読めなければ None)。"""
    try:
        import fitz
        with fitz.open(path) as d:
            return None if d.needs_pass else d.page_count
    except Exception:  # noqa: BLE001
        return None


STRUCT_COUNTS_RE = re.compile(r"(?:構造|構成)[^\n]*?反映\s*(\d+)\s*件[^\n]*?不採用\s*(\d+)\s*件(?:\s*[:：]\s*(.+))?$")
STRUCT_LEGACY_COUNTS_RE = re.compile(r"structure:.*?採用\s*(\d+)\s*件\s*/\s*却下\s*(\d+)\s*件")
STRUCT_STATUS_RE = re.compile(r"\[構造状態\]\s*(accepted|partial|rejected|skipped)\s*$")
STRUCT_REJECT_RE = re.compile(r"構造解析の結果を採用せず")


def structure_reason(line: str) -> str:
    """ログ行 (`[警告] Claude 構造解析の結果を採用せずヒューリスティックで続行します: 理由` など) から理由だけを取り出す。"""
    text = re.sub(r"^\[[^\]]*\]\s*", "", line).strip()
    m = re.search(r"続行します\s*[:：]\s*(.+)$", text)
    return (m.group(1) if m else text)[:300]


def structure_message(kind: str | None, reason: str, accepted: int | None, rejected: int | None) -> str | None:
    """結果欄に出す 1 行。構成の整理が (一部でも) 使われなかったときだけ。"""
    counts = f"{accepted}件を反映、{rejected}件は不採用" if accepted is not None and rejected is not None else ""
    tail = f" ({reason})" if reason else ""
    if kind == "skipped":
        return "構成の整理は行われませんでした: " + (reason or "理由は不明です")
    if kind == "rejected":
        return "構成の整理の結果は全部使われませんでした: " + (counts + tail if counts else reason or "理由は不明です")
    if kind == "partial":
        return "構成の整理の結果は一部使われませんでした: " + counts + tail
    return None


def assist_supported(provider: str) -> bool:
    """この版 (provider) の assist (用語集の点検・問題段落の補正) が実装されているか。"""
    try:
        from .assist import ASSIST_PROVIDERS
        return provider in ASSIST_PROVIDERS
    except Exception:  # noqa: BLE001
        return False


def _n(v) -> int:
    return len(v) if isinstance(v, (list, tuple, dict)) else int(v or 0)


def assist_message(info: dict | None, provider: str, assist_on: bool) -> str | None:
    """結果欄に出す「補助」の 1 行 (render_report.json の assist から)。"""
    name = {"claude": "Claude", "codex": "Codex"}.get(provider, provider)
    if provider == "none":
        return None
    if not assist_on:
        return f"{name}の補助: 使いませんでした (オフにした設定)"
    if not isinstance(info, dict):
        return None
    skipped = str(info.get("skipped") or "")
    if "未実装" in skipped:
        return f"{name}の補助: 未対応 (この版ではまだ使えません)"
    if info.get("enabled") is False:
        return f"{name}の補助: 使いませんでした" + (f" ({skipped})" if skipped else " (構成の整理なしで実行したため)")
    parts = []
    g, c = info.get("glossary") or {}, info.get("correction") or {}
    if g:
        parts.append(f"用語集の修正 {_n(g.get('adopted'))}件")
    if c and _n(c.get("requested")):
        parts.append(f"問題段落の補正 {_n(c.get('adopted'))}/{_n(c.get('requested'))}件採用")
    msg = f"{name}の補助: " + ("、".join(parts) if parts else ("修正の対象はありませんでした" if info.get("calls") else "呼び出しはありませんでした"))
    if skipped:
        msg += f" (スキップ: {skipped})"
    return msg


FAILED_PARAS_RE = re.compile(r"原文のまま出力した段落が\s*(\d+)\s*件")
STRUCT_SKIP_RE = re.compile(r"構造解析.*(スキップ|採用せず|使いませんでした)|(スキップ|使いませんでした).*構造解析")


def parse_multipart(content_type: str, body: bytes):
    """-> (fields: dict[str,str], files: list[(filename, bytes)])"""
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1", "replace") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    if not msg.is_multipart():
        raise ValueError("multipart ではありません")
    fields, files = {}, []
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        fn = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if fn is not None:
            files.append((fn, payload))
        elif name:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


def truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "on", "yes")


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ReadableLocal"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 静かに
            pass

        # ---- 共通 ----
        def handle_one_request(self):
            self._body_read = False  # keep-alive 接続では 1 リクエストごとに戻す
            super().handle_one_request()

        def drain(self):
            """応答前に、未読の小さな本文を読み捨てる (読まずに応答・切断すると、クライアントには接続エラーに見える)。"""
            if self.command not in ("POST", "PUT", "PATCH") or getattr(self, "_body_read", False):
                return
            self._body_read = True
            try:
                n = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                n = 0
            if 0 < n <= (1 << 20):
                self.rfile.read(n)
            elif n > (1 << 20):
                self.close_connection = True

        def send_json(self, obj, status=200):
            self.drain()
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def err(self, status, msg):
            self.send_json({"error": msg}, status)

        def cookie_token(self) -> str:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == app.cookie_name:
                    return v
            return ""

        def check_host(self) -> bool:
            return (self.headers.get("Host") or "") in app.host_ok

        def authed(self) -> bool:
            t = self.headers.get("X-Readable-Token") or self.cookie_token()
            return secrets.compare_digest(t.encode("utf-8", "replace"), app.token.encode())

        def guard(self, mutating: bool) -> bool:
            if not self.check_host():
                self.err(403, "不正な Host です")
                return False
            if mutating:
                origin = self.headers.get("Origin")
                if origin is not None and origin not in {f"http://{h}" for h in app.host_ok}:
                    self.err(403, "不正な Origin です")
                    return False
            if not self.authed():
                self.err(401, "トークンが無効です。起動時に開いた URL からアクセスしてください")
                return False
            return True

        def find_job(self, jid):
            return app.jobs.get(jid) if re.fullmatch(r"[0-9a-f]{16}", jid or "") else None

        # ---- GET ----
        def do_GET(self):
            u = urlparse(self.path)
            if not self.check_host():
                return self.err(403, "不正な Host です")
            if u.path == "/":
                tok = parse_qs(u.query).get("token", [""])[0]
                if tok and secrets.compare_digest(tok.encode("utf-8", "replace"), app.token.encode()):
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", f"{app.cookie_name}={app.token}; Path=/; HttpOnly; SameSite=Strict")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not self.authed():
                    return self.err(401, "トークンが無効です。起動時に開いた URL (または 翻訳アプリ.bat) から開いてください")
                data = (STATIC / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(data)
                return
            if not self.guard(False):
                return
            parts = u.path.strip("/").split("/")
            if parts == ["api", "setup"]:
                return self.send_json(setup_status(app.settings, provider=app.structure_provider))
            if parts[:2] == ["api", "jobs"]:
                if len(parts) == 2:
                    jobs = sorted(list(app.jobs.values()), key=lambda j: j.created, reverse=True)
                    return self.send_json({"jobs": [j.to_dict() for j in jobs]})
                job = self.find_job(parts[2])
                if job is None:
                    return self.err(404, "ジョブが見つかりません")
                if len(parts) == 3:
                    return self.send_json(job.to_dict())
                if len(parts) == 5 and parts[3] == "download" and parts[4] in ("ja", "dual"):
                    path = job.dir / "out" / f"{job.stem}_{parts[4]}.pdf"
                    if job.status != "done" or not path.is_file():
                        return self.err(404, "出力がありません")
                    inline = parse_qs(u.query).get("inline", ["0"])[0] == "1"
                    data = path.read_bytes()
                    fname = f"{job.stem}_{parts[4]}.pdf"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition", f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(fname)}")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(data)
                    return
            self.err(404, "見つかりません")

        # ---- POST / DELETE ----
        def do_POST(self):
            if not self.guard(True):
                return
            path = urlparse(self.path).path
            if path.startswith("/api/setup/"):
                return self.post_setup(path)
            m = re.fullmatch(r"/api/jobs/([0-9a-f]{16})/retry", path)
            if m:
                return self.post_retry(m.group(1))
            m = re.fullmatch(r"/api/jobs/([0-9a-f]{16})/cancel", path)
            if m:
                return self.post_cancel(m.group(1))
            if path == "/api/inspect":
                return self.post_inspect()
            if path != "/api/jobs":
                return self.err(404, "見つかりません")
            if self.precheck():
                return
            ctype = self.headers.get("Content-Type") or ""
            if not ctype.lower().startswith("multipart/form-data"):
                return self.err(400, "multipart/form-data で送ってください")
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                return self.err(411, "Content-Length が必要です")
            if length < 0 or length > MAX_UPLOAD + 1024 * 1024:
                self.close_connection = True
                return self.err(413, f"ファイルが大きすぎます (上限 {MAX_UPLOAD // 1024 // 1024} MB)")
            self._body_read = True
            body = self.rfile.read(length)
            try:
                fields, files = parse_multipart(ctype, body)
            except Exception:  # noqa: BLE001
                return self.err(400, "リクエストを解釈できません")
            del body
            if not files:
                return self.err(400, "ファイルがありません")
            if len(files) > 1:
                return self.err(400, "一度に変換できるのは 1 ファイルだけです")
            mode = fields.get("mode", "both")
            if mode not in ("ja", "dual", "both"):
                return self.err(400, "mode が不正です")
            # structure (構成の整理): 画面は常に明示する。省略時は従来の claude 欄 (省略=オフ) を見る (API 互換)
            structure = truthy(fields["structure"]) if "structure" in fields else truthy(fields.get("claude"))
            options = {"mode": mode, "claude": structure, "retry": truthy(fields.get("retry")),
                       "assist": not truthy(fields.get("no_assist"))}
            for fn, data in files:
                if len(data) > MAX_UPLOAD:
                    return self.err(413, f"ファイルが大きすぎます (上限 {MAX_UPLOAD // 1024 // 1024} MB)")
                if not data.startswith(b"%PDF"):
                    return self.err(400, f"PDF ではありません: {sanitize_stem(fn)}")
            job = app.submit(files[0][0], files[0][1], options, truthy(fields.get("replace")))
            if job is None:
                return self.send_json({"error": "翻訳の実行中です。中止して新しく始めるには replace=1 を付けてください", "busy": True}, 409)
            self.send_json({"id": job.id, "ids": [job.id], "status": job.status, "pages": job.pages, "bytes": job.bytes}, 201)

        def precheck(self) -> bool:
            """同意・キーの確認。問題があれば応答して True。"""
            if not app.settings.consented():
                self.err(403, "初回設定でプライバシーの注意への同意が必要です")
                return True
            if app.translator == "gemini" and not app.settings.get_key()[0]:
                self.err(400, "Gemini API キーが未設定です。初回設定で登録してください")
                return True
            return False

        def post_retry(self, jid):
            """保存してある同じ PDF を --retry-failed で再実行する (再添付は不要)。"""
            data = self.read_json()
            if "structure" in data and not isinstance(data["structure"], bool):
                return self.err(400, "structure は true / false で指定してください")
            job = self.find_job(jid)
            if job is None:
                return self.err(404, "ジョブが見つかりません")
            if self.precheck():
                return
            if not app.retry_job(job, structure=data.get("structure")):
                return self.err(409, "再翻訳できる状態ではありません (終了コード 7・9・12 のときだけ使えます)")
            self.send_json(job.to_dict())

        def post_cancel(self, jid):
            self.read_json()
            job = self.find_job(jid)
            if job is None:
                return self.err(404, "ジョブが見つかりません")
            if not app.cancel_job(job):
                return self.err(409, "中止できる状態ではありません")
            self.send_json(job.to_dict())

        def post_inspect(self):
            """添付直後の表示用: ファイル名・ページ数・サイズだけを返す (保存せず、翻訳も始めない)。"""
            ctype = self.headers.get("Content-Type") or ""
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                return self.err(411, "Content-Length が必要です")
            if not ctype.lower().startswith("multipart/form-data"):
                return self.err(400, "multipart/form-data で送ってください")
            if not 0 < length <= MAX_UPLOAD + 1024 * 1024:
                self.close_connection = True
                return self.err(413, f"ファイルが大きすぎます (上限 {MAX_UPLOAD // 1024 // 1024} MB)")
            self._body_read = True
            try:
                _, files = parse_multipart(ctype, self.rfile.read(length))
            except Exception:  # noqa: BLE001
                return self.err(400, "リクエストを解釈できません")
            if len(files) != 1:
                return self.err(400, "ファイルは 1 つだけ選んでください")
            fn, data = files[0]
            if len(data) > MAX_UPLOAD:
                return self.err(413, f"ファイルが大きすぎます (上限 {MAX_UPLOAD // 1024 // 1024} MB)")
            if not data.startswith(b"%PDF"):
                return self.err(400, f"PDF ではありません: {sanitize_stem(fn)}")
            pages, encrypted = None, False
            try:
                import fitz
                with fitz.open(stream=data, filetype="pdf") as d:
                    encrypted = bool(d.needs_pass)
                    pages = None if encrypted else d.page_count
            except Exception:  # noqa: BLE001
                pass
            self.send_json({"name": sanitize_stem(fn) + ".pdf", "bytes": len(data), "pages": pages, "encrypted": encrypted})

        def read_json(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or "0")
                if not 0 <= n <= 4096:
                    return {}
                self._body_read = True
                d = json.loads(self.rfile.read(n) or b"{}")
                return d if isinstance(d, dict) else {}
            except (ValueError, OSError):
                return {}

        def post_setup(self, path):
            body = self.read_json()
            if path == "/api/setup/consent":
                if body.get("agree") is not True:
                    return self.err(400, "同意のチェックが必要です")
                app.settings.set_consent()
                return self.send_json({"consent": True})
            if path == "/api/setup/key":
                key = str(body.get("key") or "").strip()
                if not KEY_RE.match(key):
                    return self.err(400, "API キーの形式が正しくありません (英数字・- _ のみ、20 文字以上)")
                app.settings.set_key(key)
                return self.send_json({"gemini": setup_status(app.settings, fast=True, provider=app.structure_provider)["gemini"]})
            if path == "/api/setup/test-key":
                key, _ = app.settings.get_key()
                if not key:
                    return self.err(400, "API キーが未設定です")
                ok, msg = check_gemini_key(key)
                return self.send_json({"ok": ok, "message_ja": msg})
            self.err(404, "見つかりません")

        def do_DELETE(self):
            if not self.guard(True):
                return
            parts = urlparse(self.path).path.strip("/").split("/")
            if parts == ["api", "setup", "key"]:
                app.settings.delete_key()
                return self.send_json({"gemini": setup_status(app.settings, fast=True, provider=app.structure_provider)["gemini"]})
            if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                job = self.find_job(parts[2])
                if job is None:
                    return self.err(404, "ジョブが見つかりません")
                app.delete_job(job)
                return self.send_json({"deleted": job.id})
            self.err(404, "見つかりません")

        def do_PUT(self):
            self.close_connection = True
            self.err(405, "許可されていないメソッドです")

        do_PATCH = do_PUT

    return Handler


def make_server(app: App, port: int) -> ThreadingHTTPServer:
    """port が使用中なら +1 ずつ最大 50 個、それでもだめなら OS 任せ。127.0.0.1 のみ。"""
    ThreadingHTTPServer.daemon_threads = True
    last: OSError | None = None
    srv = None
    for p in list(range(port, port + 50)) + [0]:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), make_handler(app))
            break
        except OSError as e:
            last = e
    if srv is None:
        raise last  # type: ignore[misc]
    pnum = srv.server_address[1]
    app.cookie_name = f"{COOKIE}_{pnum}"
    app.host_ok = {f"127.0.0.1:{pnum}", f"localhost:{pnum}"}
    return srv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m readable.webapp", description="ローカル Web アプリ (127.0.0.1 のみ)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--translator", choices=["gemini", "dummy"], default="gemini", help=argparse.SUPPRESS)
    ap.add_argument("--data-dir", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--home", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--work-dir", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--structure-provider", choices=["claude", "codex", "none"], default=None)
    a = ap.parse_args(argv)
    from .config import load_config
    provider = a.structure_provider or str(load_config().get("structure", "provider", "claude"))
    work_dir = Path(a.work_dir) if a.work_dir else None
    if provider == "codex" and work_dir is None:
        work_dir = resolve_cache_root(None) / "codex"
    data_dir = Path(a.data_dir) if a.data_dir else resolve_cache_root(work_dir) / "web_jobs"
    try:
        app = App(data_dir, work_dir, a.translator, Path(a.home) if a.home else None,
                  structure_provider=provider)
    except AlreadyRunning as e:
        url = e.info.get("url")
        print("ALREADY RUNNING: 翻訳アプリは既に起動しています。", flush=True)
        if url:
            print(f"URL: {url}", flush=True)
            if not a.no_browser:
                webbrowser.open(url)
        else:
            print("開いているブラウザのタブを使うか、起動中のウィンドウを閉じてからもう一度起動してください。", flush=True)
        return EXIT_ALREADY_RUNNING
    srv = make_server(app, a.port)
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={app.token}"
    app.instance.publish(port, url)
    print(f"Started: http://127.0.0.1:{port}/", flush=True)
    print("Close this window (or press Ctrl+C) to quit.", flush=True)
    if a.no_browser:
        print(f"URL: {url}", flush=True)
    else:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        app.stop_all()
        app.instance.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
