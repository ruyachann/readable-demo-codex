"""段階ごとの処理時間 (extract / structure / glossary / translate / render) を集計する小さなモジュール。
各段階の関数に @timed("名前") を付けると、呼び出しの経過秒が snapshot() に積まれる (render_report.json の stage_times に入る)。"""
from __future__ import annotations

import functools
import threading
import time

_T: dict[str, float] = {}
_LOCK = threading.Lock()


def add(name: str, seconds: float) -> None:
    with _LOCK:
        _T[name] = _T.get(name, 0.0) + seconds


def timed(name: str):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            if name == "extract":
                reset()          # extract は 1 回の実行の最初の段階なので、ここで前回 (同じプロセスの前のジョブ) の集計を捨てる
            t0 = time.monotonic()
            try:
                return fn(*a, **kw)
            finally:
                add(name, time.monotonic() - t0)
        return wrapper
    return deco


def snapshot() -> dict[str, float]:
    with _LOCK:
        return {k: round(v, 1) for k, v in _T.items()}


def reset() -> None:
    with _LOCK:
        _T.clear()
