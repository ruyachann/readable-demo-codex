"""M2: Claude (claude -p, Sonnet) による構造解析。ヒューリスティックの role / joins / charmap を補正する。

- 入力は frame 一覧のテキスト (確度の高い参考文献・ヘッダ等は除外)。stdin で渡す。
- 出力は {roles:{id:role}, joins_add:[[a,b]], joins_remove:[[a,b]], charmap:{char:str}} (--json-schema で強制)。
  joins はヒューリスティックの結合との「差分」(追加・削除) として解釈する。
- 結果は work/<name>/structure.json にキャッシュする (入力 + プロンプト + モデルのハッシュつき)。
- CLI 不在・タイムアウト・JSON 不正のときは警告してヒューリスティックのまま続行する。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from .config import ROOT, Config, atomic_write_text, load_config
from .extract import ALL_ROLES, apply_structure, translate_flag
from .timing import timed

PROMPT_PATH = ROOT / "prompts" / "claude_structure.md"
FALLBACK_PROMPT = ("You review the heuristic roles/joins of paper-PDF frames. Return only corrections as structured "
                   "output: roles {id: role} for wrong frames, joins_add / joins_remove (differences from heuristic_joins, [id, next_id] pairs), charmap {char: str}.")
#: Claude に送らない (確度の高い) role
EXCLUDED_ROLES = {"reference", "page_header", "page_number", "doi_url", "table"}
SCHEMA = {
    "type": "object",
    "properties": {
        "roles": {"type": "object", "additionalProperties": {"type": "string", "enum": sorted(ALL_ROLES)}},
        "joins_add": {"type": "array", "items": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2}},
        "joins_remove": {"type": "array", "items": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2}},
        "charmap": {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "required": ["roles", "joins_add", "joins_remove", "charmap"],
    "additionalProperties": False,
}


def load_prompt(path: str | Path | None = None) -> str:
    p = Path(path) if path else PROMPT_PATH
    try:
        t = p.read_text(encoding="utf-8").strip()
        return t or FALLBACK_PROMPT
    except OSError:
        return FALLBACK_PROMPT


# --------------------------------------------------------------------------
# 入力の構築
# --------------------------------------------------------------------------

def _snip(text: str, head: int = 60, tail: int = 40) -> str:
    t = re.sub(r"\s+", " ", text).strip().replace('"', "'")
    if len(t) <= head + tail + 5:
        return t
    return f"{t[:head]} ... {t[-tail:]}"


def raw_contexts(pdf_path: str | Path, chars: set[str], per_char: int = 3, width: int = 25) -> dict[str, list[str]]:
    """未マップ文字の前後の生テキストを集める (charmap 推定の手掛かり)。"""
    import fitz
    out: dict[str, list[str]] = {c: [] for c in chars}
    try:
        d = fitz.open(str(pdf_path))
    except Exception:
        return out
    try:
        for page in d:
            t = page.get_text("text")
            for c in chars:
                if len(out[c]) >= per_char:
                    continue
                for m in re.finditer(re.escape(c), t):
                    s = t[max(0, m.start() - width):m.end() + width].replace("\n", " ")
                    out[c].append(s.replace(c, "<U+%04X>" % ord(c)))
                    if len(out[c]) >= per_char:
                        break
    finally:
        d.close()
    return out


def build_input(doc: dict) -> str:
    """frame 一覧を Claude 用のテキストにする。"""
    lines = [f"meta: pages={doc['num_pages']} body_size={doc.get('body_size')}"]
    lines.append("heuristic_joins: " + json.dumps(doc.get("joins", []), ensure_ascii=False))
    omitted = 0
    for p in doc["pages"]:
        lines.append(f"page {p['number']}:")
        for f in p["frames"]:
            if f["role"] in EXCLUDED_ROLES or p.get("scanned"):
                omitted += 1
                continue
            b = f["bbox"]
            fl = ("b" if f.get("bold") else "") + ("i" if f.get("italic") else "") + ("" if f.get("serif") else "S")
            lines.append(f'{f["id"]} [{f["role"]}] s{f["size"]:g} {fl or "-"} x{b[0]:.0f}-{b[2]:.0f} y{b[1]:.0f}-{b[3]:.0f} '
                         f'"{_snip(f["text"])}"')
    lines.append(f"omitted_reliable_frames: {omitted}")
    unm: dict[str, set[str]] = {}
    for font, d in (doc.get("unmapped_chars") or {}).items():
        for k in d:
            unm.setdefault(k, set()).add(font)
    if unm:
        chars = {chr(int(k[2:], 16)) for k in unm}
        ctx = raw_contexts(doc["source"], chars) if doc.get("source") else {}
        lines.append("unmapped:")
        for k, fonts in sorted(unm.items()):
            c = chr(int(k[2:], 16))
            lines.append(f"- {k} fonts={sorted(fonts)} contexts={json.dumps(ctx.get(c, []), ensure_ascii=False)}")
        lines.append("charmap: " + json.dumps(doc.get("charmap") or {}, ensure_ascii=False))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# claude -p の呼び出し
# --------------------------------------------------------------------------

class ClaudeError(Exception):
    pass


class ClaudeSkipped(ClaudeError):
    """API キー・ゲートウェイ設定・API 認証が検出されたため、課金を避けて呼ばずにスキップした。"""


class StructureConfigError(ValueError):
    """[structure] provider の値が未対応 (設定の誤り。CLI は終了コード 10)。"""


# この版は Claude の「契約 (サブスクリプション) のログイン」だけを使い、Claude API (従量課金) は使わない。
# 次の環境変数があると、claude -p はログインより API 認証を優先する/別の接続先へ送るので、検出したら呼ばない。
API_ENV_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_UNIX_SOCKET", "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_ANTHROPIC_AWS",
)
#: allow_gateway (開発用の明示的な許可) のときでも、必ず子プロセスから外す変数 (API キー・API 認証・外部プロバイダ指定)。接続先 (BASE_URL) だけは残す
ALWAYS_STRIP = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_FOUNDRY_API_KEY", "AWS_BEARER_TOKEN_BEDROCK",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_ANTHROPIC_AWS")


def detect_api_env(environ: dict | None = None) -> list[str]:
    """設定されている (空でない) API 認証・接続先・外部プロバイダ指定の環境変数名。"""
    env = os.environ if environ is None else environ
    return [k for k in API_ENV_VARS if str(env.get(k, "")).strip()]


def prepare_claude_env(environ: dict | None = None, allow_gateway: bool = False) -> tuple[dict | None, str | None]:
    """claude 子プロセス用の環境 (親の環境のコピー) と、スキップ理由を返す。実ユーザーの環境変数は変更しない。

    既定 (allow_gateway=False): API キー・認証トークン・接続先・外部プロバイダの指定が 1 つでもあれば (None, 警告) を返す
    (外して呼ぶより、スキップする方が安全)。allow_gateway=True (開発用): ALWAYS_STRIP だけ外して、接続先は残す。"""
    env = dict(os.environ if environ is None else environ)
    found = detect_api_env(env)
    if found and not allow_gateway:
        return None, (f"API キー / ゲートウェイ設定 ({', '.join(found)}) を検出したため、API 課金を避けて Claude による構造解析は使いませんでした"
                      " (この版は Claude の契約ログインだけを使います。使いたい場合はこれらの環境変数を外してください)")
    for k in ALWAYS_STRIP:
        env.pop(k, None)
    if not allow_gateway:
        for k in API_ENV_VARS:
            env.pop(k, None)
    return env, None


def check_claude_auth(base: list[str], env: dict, timeout: float = 30.0, run: Callable | None = None) -> str | None:
    """`claude auth status` (JSON) で認証方法を確認する。契約 (claude.ai) のログインなら None、そうでなければスキップ理由を返す。"""
    run = run or subprocess.run
    try:
        r = run(base + ["auth", "status"], capture_output=True, text=True, encoding="utf-8", timeout=timeout, env=env)
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"claude の認証状態を確認できませんでした ({type(e).__name__}) ため、構造解析をスキップしました"
    try:
        j = json.loads(r.stdout)
    except ValueError:
        return "claude の認証状態を判定できなかった (出力が JSON ではない) ため、API 課金を避けて構造解析をスキップしました"
    if not isinstance(j, dict) or not j.get("loggedIn"):
        return "claude にログインしていない (claude auth login が必要) ため、構造解析をスキップしました"
    method, prov = str(j.get("authMethod", "")), str(j.get("apiProvider", ""))
    if method != "claude.ai" or prov not in ("firstParty", ""):
        return (f"claude の認証方法が契約ログインではない (authMethod={method!r}, apiProvider={prov!r}) ため、"
                "API 課金を避けて構造解析をスキップしました")
    return None


def find_claude() -> list[str] | None:
    """claude の起動コマンド。npm の .cmd シムの隣に実体 (claude.exe) があればそれを直接呼ぶ (引数の引用符問題を避ける)。"""
    w = shutil.which("claude")
    if not w:
        return None
    base = Path(w).resolve().parent
    exe = base / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    if exe.exists():
        return [str(exe)]
    return [w]


def _parse_output(stdout: str) -> tuple[dict, dict]:
    try:
        obj = json.loads(stdout)
    except ValueError as e:
        raise ClaudeError(f"claude の出力が JSON ではありません: {e}")
    if isinstance(obj, list):  # stream 形式の保険
        obj = next((o for o in reversed(obj) if isinstance(o, dict) and o.get("type") == "result"), {})
    if not isinstance(obj, dict):
        raise ClaudeError("claude の出力が想定外の形式です")
    if obj.get("is_error"):
        raise ClaudeError(f"claude がエラーを返しました: {str(obj.get('result'))[:200]}")
    data = obj.get("structured_output")
    if not isinstance(data, dict):
        r = obj.get("result")
        if isinstance(r, str):
            r = re.sub(r"^```(?:json)?\s*|\s*```$", "", r.strip())
            try:
                data = json.loads(r)
            except ValueError:
                data = None
    if not isinstance(data, dict):
        raise ClaudeError("claude の応答に構造化出力がありません")
    u = obj.get("usage") or {}
    meta = {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
            "cache_creation_input_tokens": u.get("cache_creation_input_tokens"),
            "cache_read_input_tokens": u.get("cache_read_input_tokens"),
            "duration_ms": obj.get("duration_ms"), "duration_api_ms": obj.get("duration_api_ms"),
            "total_cost_usd": obj.get("total_cost_usd")}
    return data, meta


def call_claude(system_prompt: str, user_text: str, cfg: Config, cwd: str | Path | None = None,
                runner: Callable | None = None, schema: dict | None = None) -> tuple[dict, dict]:
    """claude -p を呼んで (構造化出力, 使用量メタ) を返す。失敗は ClaudeError。runner はテスト用の差し替え。"""
    c = cfg.section("claude")
    model = c.get("model", "sonnet")
    timeout = float(c.get("timeout", 120))
    base = find_claude() if runner is None else ["claude"]
    if base is None:
        raise ClaudeError("claude CLI が見つかりません")
    env, why = prepare_claude_env(None, bool(c.get("allow_gateway", False)))
    if env is None:
        raise ClaudeSkipped(why)
    if runner is None:      # 実際に起動するときだけ、認証方法 (契約ログインか) を確認する
        why = check_claude_auth(base, env, min(timeout, 30.0))
        if why:
            raise ClaudeSkipped(why)
    cmd = base + ["-p", "--model", model, "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                  "--disable-slash-commands", "--no-session-persistence", "--system-prompt", system_prompt,
                  "--json-schema", json.dumps(schema or SCHEMA), "--output-format", "json"]
    run = runner or subprocess.run
    last: Exception | None = None
    for attempt in range(2):  # タイムアウト/起動失敗は 1 回だけリトライ
        t0 = time.time()
        try:
            r = run(cmd, input=user_text, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                    cwd=str(cwd) if cwd else None, env=env)
        except subprocess.TimeoutExpired:
            last = ClaudeError(f"claude がタイムアウトしました ({timeout:.0f}s)")
            continue
        except OSError as e:
            raise ClaudeError(f"claude を起動できません: {e}")
        if r.returncode != 0:
            last = ClaudeError(f"claude が終了コード {r.returncode} で失敗: {(r.stderr or r.stdout or '')[:200]}")
            continue
        data, meta = _parse_output(r.stdout)
        meta["wall_s"] = round(time.time() - t0, 1)
        meta["attempts"] = attempt + 1
        return data, meta
    raise last or ClaudeError("claude の呼び出しに失敗しました")


#: 構造解析の提供元。関数 provider(system_prompt, user_text, cfg, cwd=None, runner=None) -> (出力 dict, 使用量メタ dict) を登録する。
#: 失敗は ClaudeError (または継承) を投げる。出力の検証 (sanitize_result など) は提供元に依らず共通。仕様: docs/STRUCTURE_PROVIDER.md
def call_codex(system_prompt: str, user_text: str, cfg: Config, cwd=None, runner=None):
    """Load the Codex adapter only when selected; keep PDF processing shared."""
    from .codex_provider import call_codex as execute
    return execute(system_prompt, user_text, cfg, cwd=cwd, runner=runner)


PROVIDERS: dict[str, Callable] = {"claude": call_claude, "codex": call_codex}
STRUCTURE_CACHE_VERSION = "3"


# --------------------------------------------------------------------------
# 結果の検証・適用・差分
# --------------------------------------------------------------------------

MAX_JOIN_FRAMES = 8        # 1 つの結合 (unit) に含める frame 数の上限
MAX_JOIN_CHARS = 6000      # 1 つの結合の原文文字数の上限
MAX_ROLE_CHANGE_RATIO = 0.30   # roles を変えてよい「翻訳対象 frame」の割合の上限 (超えたら Claude の結果を全て捨てる)
MAX_ROLE_CHANGE_RATIO_OCR = 0.60   # スキャン + OCR 文書 (OCR の frame 分けが粗く role の直しが多くなる) だけ緩める。翻訳対象の減少の上限はそのまま
MAX_TRANSLATABLE_DROP = 0.20   # 翻訳対象 frame の数がこの割合より減るなら、Claude の結果を全て捨てる
BAD_CHARMAP_CHARS = set("{}<>&\u27e6\u27e7")


class StructureRejected(Exception):
    """Claude の出力が不審で採用できない (ヒューリスティックのまま続行する)。"""


def _limit_join_chains(joins: list[list[str]], doc: dict) -> tuple[list[list[str]], int]:
    """joins を前から順に採用し、連鎖の frame 数が MAX_JOIN_FRAMES、原文文字数が MAX_JOIN_CHARS を超えるものは採用しない。
    (採用した joins, 捨てた数) を返す。"""
    text = {f["id"]: len(f["text"]) for p in doc["pages"] for f in p["frames"]}
    nxt: dict[str, str] = {}
    prv: dict[str, str] = {}
    kept, dropped = [], 0
    for a, b in joins:
        if a in nxt or b in prv or a == b:
            dropped += 1          # 重複 (同じ frame が 2 回結合される) ・自己結合は採用しない
            continue
        # a の連鎖の先頭と b の連鎖の末尾までの長さを測る
        head = a
        while head in prv:
            head = prv[head]
        chain, cur = [head], head
        while cur in nxt:
            cur = nxt[cur]
            chain.append(cur)
        tail, cur = [b], b
        while cur in nxt:
            cur = nxt[cur]
            tail.append(cur)
        allf = chain + tail
        if len(allf) > MAX_JOIN_FRAMES or sum(text.get(x, 0) for x in allf) > MAX_JOIN_CHARS:
            dropped += 1
            continue
        nxt[a], prv[b] = b, a
        kept.append([a, b])
    return kept, dropped


def _pairs(raw, ids: set[str]) -> tuple[list[list[str]], int]:
    """[[a, b], ...] のうち、型が正しく両方の id が存在するものだけ。(採用, 無視した数)"""
    out, bad = [], 0
    for j in raw:
        if isinstance(j, list) and len(j) == 2 and isinstance(j[0], str) and isinstance(j[1], str) and j[0] in ids and j[1] in ids:
            out.append([j[0], j[1]])
        else:
            bad += 1
    return out, bad


def sanitize_result(data: dict, doc: dict) -> dict:
    """Claude の出力から安全に使える部分だけを取り出す。型が想定外なら StructureRejected。

    - roles: {frame id: role}。存在する id と既知の role のみ。
    - joins_add / joins_remove: ヒューリスティックの結合との差分 ([[id, id]])。存在しない id を含む組は無視する。
    - charmap: 1 文字キー (制御文字/PUA) -> 4 文字以内の値 (プレースホルダ・タグ用の文字 { } < > & ⟦ ⟧ を含むものは捨てる)。"""
    if not isinstance(data, dict):
        raise StructureRejected("出力が辞書ではありません")
    raw_roles, raw_cm = data.get("roles") or {}, data.get("charmap") or {}
    raw_add, raw_rem = data.get("joins_add") or [], data.get("joins_remove") or []
    if not isinstance(raw_roles, dict) or not isinstance(raw_add, list) or not isinstance(raw_rem, list) or not isinstance(raw_cm, dict):
        raise StructureRejected("roles/joins_add/joins_remove/charmap の型が想定外です")
    ids = {f["id"] for p in doc["pages"] for f in p["frames"]}
    roles = {k: v for k, v in raw_roles.items()
             if isinstance(k, str) and k in ids and isinstance(v, str) and v in ALL_ROLES}
    add, bad_a = _pairs(raw_add, ids)
    rem, bad_r = _pairs(raw_rem, ids)
    cm = {}
    for k, v in raw_cm.items():
        if isinstance(k, str) and len(k) == 1 and (ord(k) < 32 or 0xE000 <= ord(k) <= 0xF8FF) \
                and isinstance(v, str) and 0 < len(v) <= 4 and not (set(v) & BAD_CHARMAP_CHARS):
            cm[k] = v
    out = {"roles": roles, "joins_add": add, "joins_remove": rem, "charmap": cm}
    if bad_a + bad_r:
        out["joins_ignored"] = bad_a + bad_r
    return out


def final_joins(doc: dict, result: dict) -> tuple[list[list[str]] | None, int]:
    """ヒューリスティックの joins に差分 (remove -> add) を適用し、長さの上限を守らせた最終リストと、上限で捨てた数を返す。
    差分が空なら None (ヒューリスティックのまま)。"""
    add, rem = result.get("joins_add") or [], result.get("joins_remove") or []
    if not add and not rem:
        return None, 0
    heur = [list(j) for j in doc.get("joins", [])]
    rset = {tuple(j) for j in rem}
    cur = [j for j in heur if tuple(j) not in rset]
    have = {tuple(j) for j in cur}
    for j in add:
        if tuple(j) not in have:
            cur.append(list(j))
            have.add(tuple(j))
    return _limit_join_chains(cur, doc)


TRANSLATABLE_ROLES = ("title", "heading", "body", "abstract", "caption", "footnote", "sidebar", "keywords")


def _frame_signals(f: dict, role: str, doc: dict, page: dict) -> bool:
    """Claude が frame を「翻訳しない role」(translate true -> false) に変えるとき、ヒューリスティックの手がかり (位置・字の大きさ・
    文字列の型) と矛盾しないか。矛盾しなければ True (採用)。"""
    from .extract import _NUMBERED_START, _alpha, is_entry_like, looks_like_equation, prose_density

    text = f["text"].strip()
    h = page["height"]
    body = float(doc.get("body_size") or 10.0)
    short = len(text) <= 60 and f["nrows"] <= 3
    if role == "reference":
        return is_entry_like(text) or bool(_NUMBERED_START.match(text)) or (re.match(r"^\d{1,3}[.)]?$", text) is not None)
    if role == "figure_text":
        return bool(f.get("in_image")) or f["size"] <= 0.85 * body or (short and prose_density(text) < 0.25)
    if role == "math":
        return f.get("math_ratio", 0) >= 0.2 or looks_like_equation(text, f["nrows"]) or (short and _alpha(text) < 0.5 * max(len(text), 1))
    if role == "author":
        return page["number"] <= 3 and f["nrows"] <= 12 and len(text) <= 700 and prose_density(text) < 0.3
    if role in ("page_header", "page_number", "doi_url"):
        in_zone = f["bbox"][3] < 0.12 * h or f["bbox"][1] > 0.88 * h
        return (in_zone and len(text) <= 120) or bool(re.match(r"^(https?://\S+|doi:\s*\S+)$", text, re.I))
    if role == "table":
        return bool(f.get("in_table"))
    return False


def select_role_changes(result: dict, doc: dict) -> tuple[dict, dict]:
    """Claude の roles を frame ごとに選んで採用する (全部採用か全部却下かにしない)。

    - 翻訳対象の role 同士の変更 (body/heading/caption/abstract/footnote/sidebar/title/keywords) は無条件に採用 (翻訳量が変わらない)。
    - 翻訳する -> しない (reference/figure_text/author/math/table/page_header など) への変更は、その frame が手がかり (位置・字の大きさ・
      参考文献の番号づけなど) と矛盾しないときだけ採用し、他はヒューリスティックの role のまま。
    - 翻訳しない -> する への変更は、文章らしい (40 字以上で小文字の語が多い) ときだけ採用。
    採用しなかった変更は stats["rejected"] に記録する。戻り値は (roles を絞った result の複製, stats)。"""
    from .extract import prose_density

    frames = {f["id"]: (f, p) for p in doc["pages"] for f in p["frames"]}
    roles_in = result.get("roles") or {}
    keep: dict[str, str] = {}
    acc: list[dict] = []
    rej: list[dict] = []
    for k, new in roles_in.items():
        if k not in frames:
            continue
        f, page = frames[k]
        old = f["role"]
        if new == old:
            keep[k] = new
            continue
        flags = [x for x in f.get("flags", []) if x != "identifier"]
        new_tr = translate_flag(new, f["text"], flags)
        item = {"id": k, "page": f["page"], "from": old, "to": new, "text": f["text"][:60]}
        if f["translate"] and not new_tr:
            ok = _frame_signals(f, new, doc, page)
        elif not f["translate"] and new_tr:
            ok = (len(f["text"].strip()) >= 40 and prose_density(f["text"]) >= 0.3) or (new in ("heading", "title") and len(f["text"].strip()) <= 80)
        else:
            ok = True
        if ok:
            keep[k] = new
            acc.append(item)
        else:
            rej.append(item)
    out = dict(result)
    out["roles"] = keep
    stats = {"accepted": len(acc), "rejected": len(rej), "rejected_items": rej, "accepted_items": acc}
    return out, stats


def check_role_changes(result: dict, doc: dict) -> None:
    """採用する roles を適用した結果、翻訳対象の frame が MAX_TRANSLATABLE_DROP を超えて減るなら StructureRejected (結果全体を捨てる)。
    (frame ごとの選択は select_role_changes が先に行う。M12 から、変更の多さだけで全体を捨てることはしない。)"""
    frames = {f["id"]: f for p in doc["pages"] for f in p["frames"]}
    n_tr = sum(1 for f in frames.values() if f["translate"])
    c_tr = sum(len(f["text"]) for f in frames.values() if f["translate"])
    if not n_tr or not result.get("roles"):
        return
    after = after_c = 0
    for k, f in frames.items():
        r = result["roles"].get(k, f["role"])
        if r != f["role"]:
            ok = translate_flag(r, f["text"], [x for x in f.get("flags", []) if x != "identifier"])
        else:
            ok = f["translate"]
        after += 1 if ok else 0
        after_c += len(f["text"]) if ok else 0
    # 翻訳する量が大きく減るなら全体を捨てる。frame 数の減りは MAX_TRANSLATABLE_DROP (20%)、
    # ただし参考文献・著者欄・ヘッダ (frame は多いが文字数は少ない) を正しく外す変更で落ちないよう、文字数で見る (frame 数は 40% まで)
    if (c_tr - after_c) / max(c_tr, 1) > MAX_TRANSLATABLE_DROP or (n_tr - after) / n_tr > 2 * MAX_TRANSLATABLE_DROP:
        raise StructureRejected(f"翻訳対象が frame {n_tr} -> {after}、文字数 {c_tr} -> {after_c} に減るため採用しません")


def snapshot(doc: dict) -> dict:
    return {"roles": {f["id"]: f["role"] for p in doc["pages"] for f in p["frames"]},
            "joins": [list(j) for j in doc.get("joins", [])]}


def diff_structure(before: dict, after: dict, doc: dict, charmap: dict | None = None) -> dict:
    """snapshot 同士の差分。"""
    text = {f["id"]: f["text"] for p in doc["pages"] for f in p["frames"]}
    changed = [{"id": k, "old": before["roles"].get(k), "new": v, "text": _snip(text.get(k, ""), 50, 20)}
               for k, v in after["roles"].items() if before["roles"].get(k) != v]
    bj = {tuple(j) for j in before["joins"]}
    aj = {tuple(j) for j in after["joins"]}
    return {"roles_changed": changed,
            "joins_added": [list(j) for j in sorted(aj - bj)], "joins_removed": [list(j) for j in sorted(bj - aj)],
            "charmap_added": charmap or {}}


@timed("structure")
def run_structure(doc: dict, work_dir: str | Path, cfg: Config | None = None, enabled: bool = True,
                  log: Callable[[str], None] | None = None, runner: Callable | None = None,
                  prompt_path: str | Path | None = None, force: bool = False) -> dict:
    """構造解析を実行して doc に適用する (in-place)。info (使用量・差分・警告・cached 等) を返す。

    失敗時は警告を log して doc を変更せず続行する (info['ok']=False)。"""
    cfg = cfg or load_config()
    log = log or (lambda s: None)
    info: dict = {"ok": False, "used": False, "status": "skipped", "cached": False, "warnings": []}
    prov = str(cfg.get("structure", "provider", "claude"))
    if prov != "none" and prov not in PROVIDERS:
        raise StructureConfigError(f"structure.provider={prov!r} は未対応です (claude | codex | none)")
    if not enabled or prov == "none":
        info["reason"] = "disabled"      # キャッシュも読まない・何も呼ばない
        log("[構造状態] skipped")
        return info
    if prov not in PROVIDERS:
        raise StructureConfigError(f"structure.provider={prov!r} は未対応です (claude | codex | none)")
    wd = Path(work_dir)
    wd.mkdir(parents=True, exist_ok=True)
    system = load_prompt(prompt_path)
    text = build_input(doc)
    model = cfg.get(prov, "model", "gpt-6.1-sol" if prov == "codex" else "sonnet")
    policy = {"selection": "frame-signals-v1", "max_translatable_drop": MAX_TRANSLATABLE_DROP,
              "max_join_frames": MAX_JOIN_FRAMES, "max_join_chars": MAX_JOIN_CHARS}
    ihash = hashlib.sha256("\0".join((text, system, model, prov, STRUCTURE_CACHE_VERSION,
                                    json.dumps(SCHEMA, sort_keys=True), json.dumps(policy, sort_keys=True))).encode("utf-8")).hexdigest()
    cache_path = wd / "structure.json"
    raw = meta = None
    if cache_path.exists() and not force:
        try:
            cj = json.loads(cache_path.read_text(encoding="utf-8"))
            if (cj.get("input_hash") == ihash and cj.get("cache_version") == STRUCTURE_CACHE_VERSION
                    and cj.get("policy") == policy and cj.get("provider") == prov and cj.get("model") == model
                    and isinstance(cj.get("raw"), dict)):
                # Revalidate the complete candidate, not the filtered result: otherwise
                # frame rejections disappear on the next run.
                sanitize_result(cj["raw"], doc)
                raw, meta = cj["raw"], cj.get("meta", {})
                info["cached"] = True
        except (OSError, ValueError, StructureRejected, TypeError, AttributeError, KeyError):
            raw = meta = None
            info["cached"] = False
    if raw is None:
        try:
            raw, meta = PROVIDERS[prov](system, text, cfg, cwd=wd, runner=runner)
        except ClaudeError as e:
            w = f"{prov.title()} 構造解析をスキップしヒューリスティックで続行します: {e}"
            info["warnings"].append(w)
            log(f"[警告] {w}")
            info["reason"] = str(e)
            log("[構造状態] skipped")
            return info

    def save_cache(result: dict) -> None:
        atomic_write_text(cache_path, json.dumps({"input_hash": ihash, "provider": prov,
                          "cache_version": STRUCTURE_CACHE_VERSION, "policy": policy, "model": model,
                          "raw": raw, "result": result, "selection": info.get("selection"),
                          "status": info["status"], "used": info["used"], "meta": meta,
                          "diff": info.get("diff"), "input_chars": len(text)}, ensure_ascii=False, indent=1))

    result = None
    try:
        result = sanitize_result(raw, doc)
        result, sel = select_role_changes(result, doc)
        info["selection"] = sel
        check_role_changes(result, doc)
    except (StructureRejected, TypeError, AttributeError, ValueError, KeyError) as e:
        w = f"{prov.title()} 構造解析の結果を採用せずヒューリスティックで続行します: {e}"
        info["warnings"].append(w)
        log(f"[警告] {w}")
        info.update(status="rejected", reason=str(e))
        # Candidate acceptance is audit data; nothing was actually applied.
        sel = info.get("selection") or {}
        info["candidate_selection"] = sel
        items = sel.get("rejected_items", []) + sel.get("accepted_items", [])
        info["selection"] = {"accepted": 0, "rejected": sel.get("accepted", 0) + sel.get("rejected", 0),
                             "accepted_items": [], "rejected_items": items}
        log(f"[構造] 反映 0 件 / 不採用 {info['selection']['rejected']} 件 : {e}")
        log("[構造状態] rejected")
        try:
            atomic_write_text(wd / "structure_rejected.json", json.dumps({"reason": str(e), "provider": prov,
                              "raw": raw, "candidate_selection": sel}, ensure_ascii=False, indent=1))
            if result is not None:
                save_cache(result)
        except (OSError, TypeError, ValueError):
            pass
        return info
    sel = info["selection"]
    if sel["rejected"]:
        info["reason"] = "role の変更の一部を却下 (ヒューリスティックの手がかりと矛盾)"
        try:
            atomic_write_text(wd / "structure_rejected.json", json.dumps({"reason": info["reason"],
                              "provider": prov, "rejected": sel["rejected_items"], "raw": raw}, ensure_ascii=False, indent=1))
        except (OSError, TypeError, ValueError):
            pass
    if result.get("joins_ignored"):
        w = f"存在しない frame id を含む joins_add/joins_remove を {result['joins_ignored']} 件無視しました"
        info["warnings"].append(w)
        log(f"[警告] structure: {w}")
    before = snapshot(doc)
    cur = [m for m in (doc.get("charmap") or {}).values() if isinstance(m, dict)]
    new_cm = {k: v for k, v in result["charmap"].items() if not any(m.get(k) == v for m in cur)}
    fj, dropped = final_joins(doc, result)
    if dropped:
        w = f"長すぎる結合 (>{MAX_JOIN_FRAMES} frame または >{MAX_JOIN_CHARS} 文字) を {dropped} 件捨てました"
        info["warnings"].append(w)
        log(f"[警告] structure: {w}")
    warns = apply_structure(doc, roles=result["roles"] or None, joins=fj, charmap=new_cm or None, cfg=cfg)
    for w in warns:
        info["warnings"].append(w)
        log(f"[警告] structure: {w}")
    after = snapshot(doc)
    info.update(ok=True, meta=meta or {}, result=result,
                diff=diff_structure(before, after, doc, new_cm), input_chars=len(text), input_hash=ihash)
    has_changes = any(info["diff"].values())
    info["used"] = bool(has_changes or not sel["rejected"])
    info["status"] = ("partial" if info["used"] else "rejected") if sel["rejected"] else "accepted"
    log(f"[構造] 反映 {sel['accepted']} 件 / 不採用 {sel['rejected']} 件"
        + (f" : {info['reason']}" if info.get("reason") else ""))
    # Role counts cannot express accepted joins/charmap changes. Emit the
    # complete state before translation, even when no render report is produced.
    log(f"[構造状態] {info['status']}")
    save_cache(result)
    return info
