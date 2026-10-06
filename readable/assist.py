"""翻訳品質の補助 (assist): 構造解析と同じ「契約ログインの LLM CLI」を、翻訳の問題のある箇所だけに使う (M14)。

- 用語集の点検 (論文あたり 1 回): Gemini が作った用語集を題名・要旨と一緒に渡し、誤訳・幻覚・表記の不統一の修正を JSON で受け取る。
- 問題段落の補正 (論文あたり 0〜2 回): 検証に通らず原文のまま残った段落 (英語のまま) だけをまとめて渡し、訳し直しを受け取る。
  補正は Gemini の訳と同じ検証 (タグ・{vN}・⟦n⟧・数値・英語残り) に通ったものだけ採用する。

全文の校正はしない (本文は渡さない)。CR-09 の安全規則は構造解析と同じ (API キー・ゲートウェイ設定があればスキップ、契約ログインのみ)。
提供元 (provider) は ASSIST_PROVIDERS に登録する。Claude 版はここ、Codex 版は Codex 側で実装する (仕様: docs/STRUCTURE_PROVIDER.md の「assist」)。
呼び出し回数は [assist] max_calls_per_doc (既定 4。構造解析 1 + 用語集 1 + 補正 2) まで。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from .config import Config, atomic_write_text
from .glossary import entry_problem
from .structure import ClaudeError, ClaudeSkipped, call_claude, load_prompt

GLOSSARY_SCHEMA = {
    "type": "object",
    "properties": {
        "fixes": {"type": "array", "items": {"type": "object", "properties": {
            "en": {"type": "string"}, "ja": {"type": "string"}, "reason": {"type": "string"}}, "required": ["en", "ja"]}},
        "remove": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["fixes", "remove"],
}
CORRECT_SCHEMA = {
    "type": "object",
    "properties": {"units": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "text": {"type": "string"}}, "required": ["id", "text"]}}},
    "required": ["units"],
}

MAX_UNITS_PER_CALL = 20
MAX_CHARS_PER_CALL = 14000


def _claude_assist(system_prompt: str, user_text: str, cfg: Config, schema: dict, cwd=None, runner=None) -> tuple[dict, dict]:
    return call_claude(system_prompt, user_text, cfg, cwd=cwd, runner=runner, schema=schema)


#: 提供元 -> 関数 (system_prompt, user_text, cfg, schema, cwd=None, runner=None) -> (出力 dict, 使用量メタ dict)。失敗は ClaudeError。
ASSIST_PROVIDERS: dict[str, Callable] = {"claude": _claude_assist}


class Assist:
    """1 文書あたりの呼び出し回数を数え、結果を work ディレクトリにキャッシュする。"""

    def __init__(self, cfg: Config, work_dir: str | Path, provider: str = "claude", enabled: bool = True,
                 used_by_structure: int = 0, runner: Callable | None = None, log: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.wd = Path(work_dir)
        self.provider = provider
        self.fn = ASSIST_PROVIDERS.get(provider)
        self.enabled = bool(enabled) and self.fn is not None and provider != "none"
        self.max_calls = int(cfg.get("assist", "max_calls_per_doc", 4))
        self.calls = int(used_by_structure)
        self.runner = runner
        self.log = log or (lambda s: None)
        self.info: dict = {"enabled": self.enabled, "provider": provider, "calls": 0, "glossary": {}, "correction": {}, "skipped": ""}
        if enabled and self.fn is None and provider != "none":
            self.info["skipped"] = f"{provider} 版の assist は未実装です (Codex 側で ASSIST_PROVIDERS に登録してください)"
        self._cache_path = self.wd / "assist_cache.json"
        try:
            self._cache = json.loads(self._cache_path.read_text(encoding="utf-8")) if self._cache_path.exists() else {}
        except (OSError, ValueError):
            self._cache = {}

    # -- 共通 -----------------------------------------------------------
    def _budget_left(self) -> bool:
        return self.enabled and self.calls < self.max_calls

    def _call(self, prompt_file: str, payload: dict, schema: dict) -> dict | None:
        key = hashlib.sha256((prompt_file + json.dumps(payload, ensure_ascii=False, sort_keys=True)).encode("utf-8")).hexdigest()
        if key in self._cache:
            self.info.setdefault("cached", 0)
            self.info["cached"] += 1
            return self._cache[key]
        if not self._budget_left():
            return None
        system = load_prompt(Path(__file__).resolve().parent.parent / "prompts" / prompt_file)
        self.calls += 1
        self.info["calls"] += 1
        try:
            data, meta = self.fn(system, json.dumps(payload, ensure_ascii=False), self.cfg, schema, cwd=self.wd, runner=self.runner)
        except ClaudeSkipped as e:
            self.enabled = False
            self.info["skipped"] = str(e)
            self.log(f"[警告] assist をスキップします: {e}")
            return None
        except ClaudeError as e:
            self.info.setdefault("errors", []).append(str(e)[:200])
            self.log(f"[警告] assist の呼び出しに失敗しました (翻訳は続行します): {str(e)[:160]}")
            return None
        self._cache[key] = data
        try:
            atomic_write_text(self._cache_path, json.dumps(self._cache, ensure_ascii=False))
        except OSError:
            pass
        return data

    # -- 用語集の点検 ---------------------------------------------------
    def review_glossary(self, glossary: list[dict], title: str, abstract: str) -> list[dict]:
        """用語集を点検して返す。修正は entry_problem を通ったものだけ採用 (幻覚を入れない)。失敗・スキップなら入力のまま。"""
        if not glossary or not self._budget_left():
            return glossary
        data = self._call("assist_glossary.md",
                          {"title": title[:300], "abstract": abstract[:900], "glossary": [{"en": g["en"], "ja": g["ja"]} for g in glossary]},
                          GLOSSARY_SCHEMA)
        if not isinstance(data, dict):
            return glossary
        by_en = {g["en"].lower(): i for i, g in enumerate(glossary)}
        out = [dict(g) for g in glossary]
        adopted: list[dict] = []
        rejected: list[dict] = []
        for fx in data.get("fixes") or []:
            if not isinstance(fx, dict):
                continue
            en, ja = str(fx.get("en", "")).strip(), str(fx.get("ja", "")).strip()
            i = by_en.get(en.lower())
            if i is None or not ja or ja == out[i]["ja"]:
                continue
            why = entry_problem(out[i]["en"], ja)
            if why:
                rejected.append({"en": en, "ja": ja, "why": why})
                continue
            adopted.append({"en": out[i]["en"], "from": out[i]["ja"], "to": ja, "reason": str(fx.get("reason", ""))[:80]})
            out[i]["ja"] = ja
        drop = {str(x).strip().lower() for x in (data.get("remove") or []) if isinstance(x, str)}
        removed = [g["en"] for g in out if g["en"].lower() in drop]
        out = [g for g in out if g["en"].lower() not in drop]
        self.info["glossary"] = {"adopted": adopted, "rejected": rejected, "removed": removed}
        self.log(f"[info] assist: 用語集の点検 修正 {len(adopted)} 件採用 / {len(rejected)} 件却下 / 削除 {len(removed)} 件")
        return out

    # -- 問題段落の補正 -------------------------------------------------
    def correct_units(self, problems: list[dict], glossary: list[dict], title: str, abstract: str,
                      validate: Callable[[dict, str], list[str]]) -> dict[str, str]:
        """problems: [{"unit": unit, "current": 現在の訳 or None, "problems": [理由...]}]。
        補正を受け取り、validate(unit, 訳) が [] を返したものだけ {unit_id: 訳} で返す (最大 2 回の呼び出し)。"""
        adopted: dict[str, str] = {}
        rejected: list[dict] = []
        rej_ja: dict[str, tuple[dict, str, str]] = {}      # 却下された補正: id -> (problem, 補正の訳, 却下の理由)。2 回目の呼び出しに理由を渡す
        todo = list(problems)
        n_calls = 0
        while (todo or (rej_ja and n_calls == 1)) and self._budget_left() and n_calls < 2:
            if not todo:                                   # 1 回目で却下されたものを、却下の理由つきでもう 1 回だけ依頼する
                todo = [{"unit": p["unit"], "current": ja, "problems": ["前回の補正が検証に落ちた: " + why] + p.get("problems", [])}
                        for p, ja, why in rej_ja.values()]
                rej_ja = {}
                self.info["correction_second_pass"] = len(todo)
            batch, chars = [], 0
            while todo and len(batch) < MAX_UNITS_PER_CALL and chars + len(todo[0]["unit"]["text"]) <= MAX_CHARS_PER_CALL:
                p = todo.pop(0)
                batch.append(p)
                chars += len(p["unit"]["text"])
            if not batch:
                batch.append(todo.pop(0))
            terms = " ".join(p["unit"]["text"] for p in batch).lower()
            gl = [{"en": g["en"], "ja": g["ja"]} for g in glossary if g["en"].lower() in terms][:40]
            payload = {"context": {"title": title[:300], "summary": abstract[:600], "glossary": gl},
                       "units": [{"id": p["unit"]["id"], "role": p["unit"]["role"], "source": p["unit"]["text"],
                                  "current": p.get("current"), "problems": p.get("problems", [])[:3]} for p in batch]}
            data = self._call("assist_correct.md", payload, CORRECT_SCHEMA)
            n_calls += 1
            if not isinstance(data, dict):
                continue
            got = {str(u.get("id")): str(u.get("text", "")) for u in (data.get("units") or []) if isinstance(u, dict)}
            for p in batch:
                u = p["unit"]
                ja = got.get(u["id"])
                if not ja or ja.strip() == u["text"].strip():
                    rejected.append({"id": u["id"], "why": "訳が返らない/原文のまま"})
                    continue
                why = validate(u, ja)
                if why:
                    rejected.append({"id": u["id"], "why": "; ".join(why)[:120]})
                    if n_calls == 1:
                        rej_ja[u["id"]] = (p, ja, "; ".join(why)[:200])
                else:
                    adopted[u["id"]] = ja
        self.info["correction"] = {"requested": len(problems), "adopted": len(adopted), "rejected": rejected[:20], "calls": n_calls}
        if problems:
            self.log(f"[info] assist: 問題段落の補正 {len(problems)} 件中 {len(adopted)} 件採用 / {len(rejected)} 件却下 (呼び出し {n_calls} 回)")
        return adopted
