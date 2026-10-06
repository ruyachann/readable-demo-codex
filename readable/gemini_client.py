"""Gemini API の薄いラッパー: レート制御 (RPM)、429 の retryDelay 尊重、日次上限の検出、thinking 設定のフォールバック、
構造化出力 (JSON) の取得、プロンプトファイルの読み込み。API キーは環境変数 GEMINI_API_KEY のみ (値は表示・保存しない)。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import ROOT, Config, load_config

# SDK は generate_content のたびに「automatic function calling は推奨されない」という (無関係な) 警告を出すので、警告は出さない
logging.getLogger("google_genai.models").setLevel(logging.ERROR)


class GeminiError(Exception):
    """Gemini 呼び出しの失敗 (一般)。"""


class GeminiUnavailable(GeminiError):
    """API キー未設定・SDK 未インストールなど、使い始められない。"""


class GeminiFatalError(GeminiError):
    """設定・権限の誤り (API キー不正 401/403、モデル名誤り 404、不正なリクエスト 400 など)。再試行しても直らないので即中断する。"""


class GeminiTransientError(GeminiError):
    """5xx・タイムアウト・接続エラーが、決められた回数の再試行でも直らなかった。"""


class GeminiContentError(GeminiError):
    """応答の内容が使えない (空応答・安全フィルタ・MAX_TOKENS による途切れ・JSON 不正)。その unit/バッチ単位の失敗として扱う。"""


class DailyLimitError(GeminiError):
    """日次上限 (per-day の RESOURCE_EXHAUSTED)。再実行は翌日 (太平洋時間 0 時リセット)。"""

    def __init__(self, model: str, msg: str = ""):
        super().__init__(f"{model}: 日次上限に達しました {msg}".strip())
        self.model = model


class BudgetExceeded(GeminiError):
    """開発・検証用の予算上限 (環境変数 READABLE_GEMINI_MAX_REQUESTS) に達した。次のリクエストは送らない (cli は終了コード 9)。"""

    def __init__(self, limit: int, sent: int):
        self.limit, self.sent = limit, sent
        super().__init__(f"開発用の予算上限 (READABLE_GEMINI_MAX_REQUESTS={limit}) に達したため、これ以上リクエストを送りません "
                         f"(このプロセスで送信済み: {sent} 回)。翻訳済みの分はキャッシュに保存済みです。再実行すれば続きから再開します")


class RateLimitError(GeminiError):
    """分あたり上限などで、待っても通らなかった。"""


# --------------------------------------------------------------------------
# 日次上限の状態 (.quota_state.json): 上限に達したモデルと、太平洋時間の次の 0 時 (リセット) を保存する
# --------------------------------------------------------------------------

def _pacific():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Los_Angeles")
    except Exception:  # noqa: BLE001 - tzdata が無い環境: 夏時間 (UTC-7) 固定で近似する (冬は 1 時間早く再試行するだけ)
        return dt.timezone(dt.timedelta(hours=-7))


def next_pacific_midnight(now: dt.datetime | None = None) -> dt.datetime:
    """now (tz 付き。省略で現在) の次の太平洋時間 0 時を、太平洋時間の tz 付き datetime で返す。"""
    tz = _pacific()
    n = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    return dt.datetime.combine(n.date() + dt.timedelta(days=1), dt.time(0, 0), tzinfo=tz)


def key_scope(api_key: str | None) -> str:
    """API キー (プロジェクト) ごとに状態を分けるための sha256 先頭 8 桁。キー本体は保存しない。"""
    import hashlib
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:8] if api_key else ""


class QuotaState:
    """{"models": {"<スコープ>:<モデル名>": 期限 (ISO8601, tz 付き)}} を JSON で保存する。期限を過ぎたものは無視する。

    scope = key_scope(API キー): キー (プロジェクト・課金設定) を変えれば、前のキーの上限状態は効かない。
    path が None なら保存しない (テスト用)。壊れたファイルは .bak に退避して空から始める。"""

    def __init__(self, path: str | Path | None, now: Callable[[], dt.datetime] | None = None, scope: str = ""):
        self.path = Path(path) if path else None
        self.scope = scope
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.models: dict[str, str] = {}
        self.notes: list[str] = []
        if self.path is not None and self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                m = d.get("models", {}) if isinstance(d, dict) else {}
                if not isinstance(m, dict):
                    raise ValueError("models が辞書ではありません")
                self.models = {str(k): str(v) for k, v in m.items()}
            except (OSError, ValueError, AttributeError) as e:
                self.models = {}
                try:
                    self.path.replace(self.path.with_name(self.path.name + ".bak"))
                except OSError:
                    pass
                self.notes.append(f"{self.path} を読めないため退避して空から始めます ({e})")

    def _k(self, model: str) -> str:
        return f"{self.scope}:{model}" if self.scope else model

    def _expiry(self, key: str) -> dt.datetime | None:
        v = self.models.get(key)
        if not v:
            return None
        try:
            e = dt.datetime.fromisoformat(v)
        except ValueError:
            return None
        return e if e.tzinfo else e.replace(tzinfo=_pacific())

    def active(self) -> dict[str, dt.datetime]:
        """この scope の、期限内のモデル -> 期限。"""
        now = self._now()
        pre = f"{self.scope}:" if self.scope else ""
        out = {}
        for k in list(self.models):
            if pre and not k.startswith(pre):
                continue
            if not pre and ":" in k:   # scope なしの利用では、scope 付きのエントリ (別のキーの記録) は見ない
                continue
            e = self._expiry(k)
            if e is not None and e > now:
                out[k[len(pre):]] = e
        return out

    def mark(self, model: str) -> dt.datetime:
        """model が日次上限に達したと記録する (期限 = 太平洋時間の次の 0 時)。"""
        e = next_pacific_midnight(self._now())
        self.models[self._k(model)] = e.isoformat()
        self.save()
        return e

    def save(self) -> None:
        if self.path is None:
            return
        now = self._now()
        live = {k: v for k, v in self.models.items() if (self._expiry(k) or now) > now}  # 他の scope の期限内エントリも残す
        self.models = live
        try:
            from .config import atomic_write_text
            atomic_write_text(self.path, json.dumps({"models": live}, ensure_ascii=False, indent=1))
        except OSError:  # pragma: no cover - 書けなくても実行は続ける
            pass


# --------------------------------------------------------------------------
# プロンプトの読み込み
# --------------------------------------------------------------------------

def load_prompt_file(path: str | Path | None, fallback: str) -> str:
    """プロンプトファイルを読む。'## SYSTEM PROMPT' 見出しの後に ``` だけの行で囲まれた本文があればそれを、
    無ければファイル全体を返す。ファイルが無い/空なら内蔵のフォールバック。"""
    if not path:
        return fallback
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return fallback
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if re.match(r"^#{1,3}\s*SYSTEM PROMPT\b", l.strip(), re.I):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            k = j + 1
            while k < len(lines) and lines[k].strip() != "```":
                k += 1
            if j < len(lines) and k <= len(lines):
                body = "\n".join(lines[j + 1:k]).strip()
                if body:
                    return body
    return text.strip() or fallback


def file_hash(path: str | Path | None) -> str:
    import hashlib
    if not path:
        return ""
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except OSError:
        return "missing"


def render_glossary(glossary: list[dict]) -> str:
    if not glossary:
        return "(なし)"
    rows = ["| English | 日本語 | note |", "|---|---|---|"]
    for g in glossary:
        rows.append(f"| {g.get('en', '')} | {g.get('ja', '')} | {g.get('note', '')} |")
    return "\n".join(rows)


def fill_placeholders(system: str, glossary: list[dict] | None, context: dict | None) -> str:
    """プロンプト中の {{GLOSSARY}} / {{CONTEXT}} を置換する (あれば)。"""
    if "{{GLOSSARY}}" in system:
        system = system.replace("{{GLOSSARY}}", render_glossary(glossary or []))
    if "{{CONTEXT}}" in system:
        c = context or {}
        ctx = f"Title: {c.get('title', '')}\nSummary: {c.get('summary', '')}\nPrevious text: {c.get('prev_text', '')}"
        system = system.replace("{{CONTEXT}}", ctx)
    return system


# --------------------------------------------------------------------------
# エラー解析
# --------------------------------------------------------------------------

_DELAY_RES = (re.compile(r"retryDelay\W+(\d+(?:\.\d+)?)\s*s", re.I), re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.I))
_QUOTA_ID_RE = re.compile(r"""['"]quota(?:Id|Metric)['"]\s*:\s*['"]([^'"]+)['"]""")
_TRANSPORT_NAMES = {"TransportError", "TimeoutException", "TimeoutError", "ConnectError", "ReadError", "WriteError",
                    "ConnectionError", "RemoteProtocolError", "NetworkError", "ProtocolError", "ConnectTimeout", "ReadTimeout"}


def error_text(e: Exception) -> str:
    parts = [str(e)]
    for attr in ("details", "message", "status"):
        v = getattr(e, attr, None)
        if v:
            try:
                parts.append(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str))
            except Exception:
                parts.append(str(v))
    return "\n".join(parts)


def error_code(e: Exception) -> int | None:
    c = getattr(e, "code", None)
    if isinstance(c, int):
        return c
    m = re.match(r"\s*(\d{3})\b", str(e))
    return int(m.group(1)) if m else None


def parse_retry_delay(text: str) -> float | None:
    for r in _DELAY_RES:
        m = r.search(text)
        if m:
            return float(m.group(1))
    return None


def quota_ids(e: Exception) -> list[str]:
    """429 の details (QuotaFailure.violations[].quotaId / quotaMetric) から、上限の種類を示す文字列を集める。"""
    ids: list[str] = []

    def walk(o) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("quotaId", "quotaMetric") and isinstance(v, str):
                    ids.append(v)
                else:
                    walk(v)
        elif isinstance(o, (list, tuple)):
            for x in o:
                walk(x)

    walk(getattr(e, "details", None))
    if not ids:
        ids = _QUOTA_ID_RE.findall(error_text(e))
    return ids


def is_daily_limit(e_or_text) -> bool:
    """429 が日次上限か。根拠は quotaId (details の QuotaFailure) だけ: PerDay を含めば日次、他の quotaId (PerMinute 等) なら分あたり。
    quotaId が取れない 429 は日次と断定しない (未知の上限として、待って再試行し、それでも駄目なら終了コード 9)。メッセージ中の語句・retryDelay の長さは見ない。"""
    if isinstance(e_or_text, str):
        ids = _QUOTA_ID_RE.findall(e_or_text)
        txt = e_or_text
    else:
        ids = quota_ids(e_or_text)
        txt = error_text(e_or_text)
    if ids:
        return any(re.search(r"per.?day", i, re.I) for i in ids)
    return False


def is_transport_error(e: BaseException) -> bool:
    """タイムアウト・接続エラー (httpx / 標準ライブラリ)。"""
    if isinstance(e, (TimeoutError, ConnectionError)):
        return True
    return any(c.__name__ in _TRANSPORT_NAMES for c in type(e).__mro__)


def _finish_info(r) -> str:
    """応答の finish_reason / block_reason を文字列にする (空応答・途切れの原因表示用)。"""
    parts = []
    try:
        cands = getattr(r, "candidates", None) or []
        if cands:
            fr = getattr(cands[0], "finish_reason", None)
            if fr is not None:
                parts.append(f"finish_reason={getattr(fr, 'name', fr)}")
        pf = getattr(r, "prompt_feedback", None)
        br = getattr(pf, "block_reason", None) if pf is not None else None
        if br is not None:
            parts.append(f"block_reason={getattr(br, 'name', br)}")
    except Exception:  # noqa: BLE001
        pass
    return ", ".join(parts)


# --------------------------------------------------------------------------
# クライアント
# --------------------------------------------------------------------------

class GeminiClient:
    """generate_json(model, system, user, schema) -> parsed JSON。stats にリクエスト数などを集計する。

    genai_client / sleep / clock はテスト用の差し替え。"""

    def __init__(self, cfg: Config | None = None, genai_client: Any = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] | None = None, quota: QuotaState | None = None):
        self.cfg = cfg or load_config()
        g = self.cfg.section("gemini")
        self.rpm = max(float(g.get("rpm", 5)), 0.1)
        self.thinking_level = g.get("thinking_level", "minimal")
        self.temperature = float(g.get("temperature", 0.2))
        self.max_retries = int(g.get("max_retries", 4))
        self.timeout_s = float(g.get("timeout", 120))
        self.retries_5xx = int(g.get("retries_5xx", 3))
        self.sleep = sleep
        self.clock = clock
        self.log = log or (lambda s: None)
        self._last = None
        self._lock = threading.Lock()      # 並列 (複数バッチ同時) 送信のときの、間隔の予約・統計・予算の更新を守る
        try:      # 開発・検証専用: このプロセスで実際に送るリクエスト数の上限。本番 (利用者の実行) では設定しない
            self.max_requests = int(os.environ.get("READABLE_GEMINI_MAX_REQUESTS", "") or 0) or None
        except ValueError:
            self.max_requests = None
        self._no_thinking: set[str] = set()
        self.quota = quota if quota is not None else QuotaState(None)
        # 日次上限に達したモデル (以後はリクエストを送らず即 DailyLimitError)。前回までの保存状態 (期限内) も引き継ぐ
        self.exhausted: set[str] = set(self.quota.active())
        for m, e in self.quota.active().items():
            self.log(f"[info] {m} は日次上限のため {e.strftime('%Y-%m-%d %H:%M %Z')} まで使いません (前回の実行で上限に達した記録)")
        self.stats: dict[str, Any] = {"requests": 0, "by_model": {}, "retry_429": 0, "retry_5xx": 0,
                                      "thinking_fallback": 0, "wait_s": 0.0, "input_tokens": 0, "output_tokens": 0}
        self.pending_5xx_sleep = [10.0, 30.0, 60.0]
        self._client = genai_client

    # -- SDK ---------------------------------------------------------------
    def _sdk(self):
        if self._client is None:
            key = os.environ.get("GEMINI_API_KEY")
            if not key:
                raise GeminiUnavailable("環境変数 GEMINI_API_KEY が設定されていません")
            try:
                from google import genai
                from google.genai import types
            except ImportError as e:  # pragma: no cover
                raise GeminiUnavailable(f"google-genai がインストールされていません: {e}")
            # GOOGLE_API_KEY が別にあっても GEMINI_API_KEY を使う。タイムアウトを必ず設定する (未設定だと応答が止まると永久に待つ)
            self._client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(self.timeout_s * 1000)))
        return self._client

    def check_ready(self) -> None:
        """API キーの有無・SDK の有無を確認する (リクエストは送らない)。使えなければ GeminiUnavailable。"""
        self._sdk()

    def _config(self, model: str, system: str, schema: dict):
        from google.genai import types
        kw: dict[str, Any] = dict(system_instruction=system, response_mime_type="application/json",
                                  response_schema=schema, temperature=self.temperature)
        if model not in self._no_thinking:
            if "2.5" in model:
                kw["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            else:
                kw["thinking_config"] = types.ThinkingConfig(thinking_level=self.thinking_level)
        return types.GenerateContentConfig(**kw)

    def _throttle(self) -> None:
        """リクエストの開始時刻を 60/rpm 秒間隔で予約する (予約は排他、待つのは排他の外: 複数スレッドから呼んでも rpm を超えない)。"""
        with self._lock:
            now = self.clock()
            slot = now if self._last is None else max(now, self._last + 60.0 / self.rpm)
            self._last = slot
            wait = slot - now
            if wait > 0:
                self.stats["wait_s"] += wait
        if wait > 0:
            self.sleep(wait)

    def _count_request(self, model: str) -> None:
        with self._lock:
            if self.max_requests is not None and self.stats["requests"] >= self.max_requests:
                raise BudgetExceeded(self.max_requests, self.stats["requests"])
            self.stats["requests"] += 1
            self.stats["by_model"][model] = self.stats["by_model"].get(model, 0) + 1

    # -- 呼び出し ----------------------------------------------------------
    def generate_json(self, model: str, system: str, user: str, schema: dict) -> Any:
        """1 リクエスト (リトライ込み) して JSON をパースして返す。

        DailyLimitError: 日次上限。RateLimitError: 分あたり上限で待っても通らない。GeminiFatalError: 設定・権限の誤り (再試行しない)。
        GeminiTransientError: 5xx/タイムアウト/接続エラーが再試行しても直らない。GeminiContentError: 応答が使えない (空・不正 JSON)。"""
        if model in self.exhausted:
            raise DailyLimitError(model, "(上限に達した記録があります)")
        sdk = self._sdk()
        tries_429 = 0
        tries_5xx = 0
        while True:
            self._count_request(model)       # 予算を超えるときはここで送らずに止める (BudgetExceeded)
            self._throttle()
            try:
                r = sdk.models.generate_content(model=model, contents=user, config=self._config(model, system, schema))
            except GeminiError:
                raise
            except Exception as e:  # noqa: BLE001 - SDK の例外型は多岐にわたる。種類ごとに分類して投げ直す
                code = error_code(e)
                txt = error_text(e)
                if code == 429 or "RESOURCE_EXHAUSTED" in txt:
                    if is_daily_limit(e):
                        self.exhausted.add(model)
                        self.quota.mark(model)
                        raise DailyLimitError(model, "(per-day)") from e
                    tries_429 += 1
                    self.stats["retry_429"] += 1
                    if tries_429 > self.max_retries:
                        raise RateLimitError(f"{model}: 429 が続いたため中断しました") from e
                    d = parse_retry_delay(txt)
                    d = min((d if d is not None else 30.0) + 1.0, 120.0)
                    self.log(f"[info] {model}: 429 レート制限。{d:.0f} 秒待って再試行します")
                    self.stats["wait_s"] += d
                    self.sleep(d)
                    continue
                if code == 400 and "thinking" in txt.lower() and model not in self._no_thinking:
                    self._no_thinking.add(model)
                    self.stats["thinking_fallback"] += 1
                    self.log(f"[info] {model}: thinking 設定が非対応のため外して再試行します")
                    continue
                transient = (code is not None and (code >= 500 or code == 408)) or is_transport_error(e) \
                    or (code is None and type(e).__name__ in ("ServerError", "APIError"))
                if transient:
                    tries_5xx += 1
                    self.stats["retry_5xx"] += 1
                    if tries_5xx > self.retries_5xx:
                        raise GeminiTransientError(f"{model}: {type(e).__name__} {str(e)[:160]} ({self.retries_5xx} 回再試行しても直りません)") from e
                    base = self.pending_5xx_sleep[min(tries_5xx - 1, len(self.pending_5xx_sleep) - 1)]
                    wait = base * (0.9 + 0.2 * (time.time() % 1.0))
                    self.log(f"[info] {model}: {type(e).__name__} ({code})。{wait:.0f} 秒待って再試行します ({tries_5xx}/{self.retries_5xx})")
                    self.stats["wait_s"] += wait
                    self.sleep(wait)
                    continue
                if code is not None and 400 <= code < 500:
                    hint = {400: "リクエストが不正です", 401: "API キーが無効です", 403: "API キーの権限がありません (または対象外の地域)",
                            404: "モデル名が存在しません"}.get(code, "リクエストが拒否されました")
                    raise GeminiFatalError(f"{model}: HTTP {code} {hint}: {str(e)[:200]}") from e
                raise  # 想定外 (プログラムのバグなど): 5xx 扱いで再試行せず、そのまま上位へ
            um = getattr(r, "usage_metadata", None)
            if um is not None:
                self.stats["input_tokens"] += getattr(um, "prompt_token_count", 0) or 0
                self.stats["output_tokens"] += getattr(um, "candidates_token_count", 0) or 0
            try:
                txt = getattr(r, "text", None)
            except Exception:  # noqa: BLE001
                txt = None
            info = _finish_info(r)
            if not txt:
                raise GeminiContentError(f"{model}: 空の応答 ({info or '理由不明'})")
            try:
                return json.loads(txt)
            except ValueError as e:
                raise GeminiContentError(f"{model}: 応答が JSON として不正です ({info or 'finish_reason 不明'}): {e}") from e

    def generate_json_chain(self, models: list[str], system: str, user: str, schema: dict) -> Any:
        """models を順に試す。DailyLimitError のモデルは飛ばして次へ。全て上限なら最後の DailyLimitError を投げる。"""
        last: DailyLimitError | None = None
        seen: list[str] = []
        for m in models:
            if m in seen:
                continue
            seen.append(m)
            try:
                return self.generate_json(m, system, user, schema)
            except DailyLimitError as e:
                last = e
                self.log(f"[info] {m} が日次上限のため別のモデルにフォールバックします")
        raise last or DailyLimitError(models[0] if models else "")
