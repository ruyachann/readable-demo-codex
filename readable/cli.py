"""python -m readable <in.pdf> [--out DIR] [--mode ja|dual|both] [--translator gemini|dummy] [--no-claude] [--work-dir DIR]
                       [--retry-failed] [--ignore-quota-state]

終了コード: 0 成功 / 2 入力が見つからない / 3 翻訳器を使えない (GEMINI_API_KEY 未設定など) / 4 パスワード付き PDF /
5 PDF として開けない (PDF 以外のファイルを含む) / 6 翻訳対象のテキストが無い (スキャン PDF など) /
7 Gemini の日次上限 (翌日再実行で続きから再開) / 8 設定・権限の誤り (API キー不正・モデル名誤りなど。再試行しても直らない) /
9 API の回復不能なエラー (5xx・タイムアウトが再試行しても直らない、分あたり上限が続く) / 10 入力データの誤り (charmap.json が壊れている等) /
11 日本語フォントが見つからない / 12 一部の段落が翻訳できず原文のまま (PDF は出力される。--retry-failed で再試行) /
1 想定外のエラー (概要と traceback の保存先を表示) / 130 Ctrl-C。7〜9 と 130 では、そこまでの翻訳はキャッシュに保存済みで、再実行すれば続きから再開する。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from .config import ROOT, load_config
from .extract import EncryptedPDFError, PDFOpenError, extract_pdf
from .gemini_client import (DailyLimitError, GeminiError, GeminiFatalError, GeminiTransientError, GeminiUnavailable,
                            QuotaState, RateLimitError, key_scope)
from .glossary import build_glossary, load_fixed, merge_fixed, normalize_all
from .labels import label_override
from .render import build_dual, count_links_toc, render_ja, save_report
from .structure import PROVIDERS, run_structure
from .translate import build_cell_units, build_units, frames_translations, make_translator, translate_cached

PRIVACY_WARNING = ("[注意] Gemini API の無料枠では、入力・出力が Google のモデル改善 (学習) に使われる可能性があります。"
                   "未公開論文・機密文書は送信しないでください。")
CLAUDE_PRIVACY_NOTE = ("[注意] 構造解析のため、各段落の抜粋 (先頭・末尾の数十文字) を Claude (Anthropic) に送ります。"
                       "翻訳の補助 (用語集の点検・失敗した段落の補正) では、題名・要旨・用語集と失敗した段落の原文も送ります。"
                       "送りたくない場合は --no-claude (全て止める) または --no-assist (補助だけ止める) を付けてください。")

EXIT_DAILY_LIMIT, EXIT_FATAL_CONFIG, EXIT_API_ERROR, EXIT_BAD_INPUT, EXIT_INTERRUPTED = 7, 8, 9, 10, 130
EXIT_NO_FONT, EXIT_PARTIAL = 11, 12
_LAST_WORK_ROOT: list[Path] = []


def work_name(pdf: Path) -> str:
    """work ディレクトリ名: <stem>-<ファイル内容の sha256 先頭 12 桁>。
    内容で決まるので、PDF を移動・コピー・改名しても同じ work (翻訳キャッシュ) を再利用できる。"""
    h = hashlib.sha256()
    with open(pdf, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return f"{pdf.stem}-{h.hexdigest()[:12]}"


def work_dir_for(wroot: Path, pdf: Path) -> Path:
    """この PDF の work ディレクトリ。同じ内容 (ハッシュ) の work が既にあれば、PDF の名前が違っても (改名コピー) それを再利用する。"""
    name = work_name(pdf)
    d = wroot / name
    if not d.exists():
        h = name.rsplit("-", 1)[1]
        for c in sorted(wroot.glob(f"*-{h}")) if wroot.exists() else []:
            if c.is_dir():
                return c
    return d


def work_root(cfg, work_dir: str | None) -> Path:
    """中間成果物の置き場。--work-dir 指定が無ければ config の [paths] work_dir (相対ならプロジェクトのルート基準)。"""
    if work_dir:
        return Path(work_dir)
    p = Path(cfg.get("paths", "work_dir", "work"))
    return p if p.is_absolute() else ROOT / p


def apply_labels(doc: dict, ja_frames: dict[str, str]) -> int:
    """サイドバー等の定型ラベル (Received: など) を辞書で訳す (値は原文のまま)。ja_frames を更新し、変えた frame 数を返す。
    Gemini が既にラベルを訳している frame は触らない (英語のまま返っていたものだけ上書きする)。"""
    import re
    n = 0
    for p in doc["pages"]:
        for f in p["frames"]:
            ov = label_override(f["role"], f["html"])
            if ov is None:
                continue
            cur = ja_frames.get(f["id"])
            if cur is None and f["translate"]:
                continue
            if cur is not None:
                plain_cur = re.sub(r"<[^>]+>", "", cur).strip().lower()
                plain_src = re.sub(r"<[^>]+>", "", f["html"]).strip().lower()
                if plain_cur != plain_src and not re.match(r"[a-z]", plain_cur):
                    continue          # 既に日本語に訳されている
            ja_frames[f["id"]] = ov
            n += 1
    return n


def _assist_correct(assist, units: list[dict], ja_units: dict[str, str], context: dict, ocr: bool, failures: dict | None = None) -> None:
    """検証・再翻訳に通らず原文 (英語) のまま残った段落だけを、assist (Claude/Codex) に訳し直してもらう。同じ検証に通ったものだけ採用する。"""
    import re

    from .translate import check_translation, leftover_english, repair_style_tags

    probs = []
    for u in units:
        ja = ja_units.get(u["id"])
        if ja is not None and ja == u["text"] and u["role"] in ("body", "abstract", "caption", "footnote", "sidebar")                 and len(re.findall(r"[A-Za-z]{3,}", re.sub(r"<[^>]+>|\{v\d+\}", " ", u["text"]))) >= 6:
            probs.append({"unit": u, "current": None,
                          "problems": (failures or {}).get(u["id"]) or ["検証・再翻訳に通らず、原文 (英語) のまま残った"]})
    if not probs:
        return

    def _validate(u: dict, ja: str) -> list[str]:
        j = repair_style_tags(u["text"], ja)
        return check_translation(u["text"], j, ocr=ocr) + (["英語が残っている"] if leftover_english(u["text"], j, u["role"]) else [])

    got = assist.correct_units(probs, context.get("glossary") or [], context.get("title", ""), context.get("summary", ""), _validate)
    by_id = {u["id"]: u for u in units}
    for i, ja in got.items():
        ja_units[i] = repair_style_tags(by_id[i]["text"], ja)


def build_annotations(doc: dict, cell_units: list[dict], cell_ja: dict[str, str]) -> list[dict]:
    """表のセルの訳 (タグ・実体参照を外した平文) を、セルの矩形つきの注釈データにする。訳が原文と同じセルは付けない。"""
    import html as _h
    import re
    frames = {f["id"]: f for p in doc["pages"] for f in p["frames"]}
    out = []
    for u in cell_units:
        ja = cell_ja.get(u["id"])
        if not ja:
            continue
        txt = _h.unescape(re.sub(r"<[^>]+>", "", ja)).replace("\u00a0", " ").strip()
        if not txt or txt == _h.unescape(u["text"]).strip():
            continue
        f = frames[u["cell"][0]]
        out.append({"page": f["page"], "rect": f["rows"][u["cell"][1]], "text": txt})
    return out


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m readable", description="英語PDF -> 日本語PDF")
    ap.add_argument("pdf", help="入力PDF")
    ap.add_argument("--out", default="out", help="出力ディレクトリ (既定: out)")
    ap.add_argument("--mode", choices=["ja", "dual", "both"], default="both")
    ap.add_argument("--translator", choices=["dummy", "gemini"], default="gemini",
                    help="既定は gemini (環境変数 GEMINI_API_KEY が必要)。dummy はレイアウト確認用のダミー訳")
    ap.add_argument("--no-structure", "--no-claude", dest="no_claude", action="store_true",
                    help="AIによる構造解析を使わない (ヒューリスティックのみ。旧 --no-claude も利用可)")
    ap.add_argument("--no-assist", dest="no_assist", action="store_true",
                    help="翻訳の補助 (用語集の点検・問題段落の補正に Claude/Codex を使う) を使わない。このとき段落の抜粋は構造解析の分しか送らない")
    ap.add_argument("--structure-provider", choices=["claude", "codex", "none"], default=None,
                    help="構造解析の提供元 (未指定はconfig.tomlの設定)")
    ap.add_argument("--work-dir", default=None, help="中間成果物の置き場 (既定: プロジェクト直下の work)")
    ap.add_argument("--config", default=None, help="config.toml のパス")
    ap.add_argument("--retry-failed", action="store_true", help="前回の実行で翻訳に失敗した unit も再送する (既定は再送しない)")
    ap.add_argument("--allow-claude-gateway", action="store_true",
                    help="開発用: API ゲートウェイ (ANTHROPIC_BASE_URL) 経由の Claude を許可する (既定は検出したらスキップ)。API キーは常に外す")
    ap.add_argument("--ignore-quota-state", action="store_true", help="保存された日次上限の状態 (.quota_state.json) を無視する")
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("\n中断しました。ここまでの翻訳はキャッシュに保存済みです。同じコマンドを再実行すれば続きから再開します。", file=sys.stderr)
        return EXIT_INTERRUPTED
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - 想定外のエラーは概要 1 行 + traceback の保存先を出して終了コード 1
        import traceback
        root = _LAST_WORK_ROOT[0] if _LAST_WORK_ROOT else ROOT / "work"
        log = root / "last_error.log"
        try:
            root.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding="utf-8")
            where = f"詳細は {log} を参照してください"
        except OSError:
            where = "詳細を保存できませんでした"
        print(f"エラー: 想定外の問題が起きました ({type(e).__name__}: {str(e)[:120]})。{where}。"
              "ここまでの翻訳はキャッシュに保存済みです。", file=sys.stderr)
        return 1


def _main(argv: list[str] | None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.structure_provider is not None:
        cfg.data.setdefault("structure", {})["provider"] = args.structure_provider
    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"入力が見つかりません: {pdf}", file=sys.stderr)
        return 2
    if not pdf.is_file():
        print(f"エラー: ファイルではありません (フォルダ?): {pdf}", file=sys.stderr)
        return 5
    if args.allow_claude_gateway:
        cfg.data.setdefault("claude", {})["allow_gateway"] = True
    prov = str(cfg.get("structure", "provider", "claude"))
    if prov not in (*PROVIDERS, "none"):
        print(f"エラー: config.toml の [structure] provider = {prov!r} は未対応です (claude | codex | none)。", file=sys.stderr)
        return EXIT_BAD_INPUT
    wroot = work_root(cfg, args.work_dir)
    if prov == "codex" and not args.work_dir:
        wroot = wroot / "codex"
    _LAST_WORK_ROOT[:] = [wroot]
    # 日本語フォントの確認は、翻訳 (API 枠の消費) より前に行う
    from .fonts import build_fonts
    try:
        build_fonts(cfg)
    except RuntimeError as e:
        print("エラー: 日本語フォント (游明朝/游ゴシック/メイリオ) が見つかりません。\n"
              "config.toml の [fonts] に、お使いの PC にある日本語フォントのパス (.ttf/.ttc。可変フォントは不可) を指定してください。\n"
              f"({str(e)[:200]})", file=sys.stderr)
        return EXIT_NO_FONT
    try:
        work_name(pdf)
    except OSError as e:
        print(f"エラー: ファイルを読めません: {pdf} ({e})", file=sys.stderr)
        return 5
    wd = work_dir_for(wroot, pdf)
    out = Path(args.out)

    def warn(msg: str) -> None:
        print(msg if msg.startswith("[") else f"[警告] {msg}", file=sys.stderr)

    quota_path = wroot / ".quota_state.json"
    if args.translator == "gemini":
        print(PRIVACY_WARNING, file=sys.stderr)
    if not args.no_claude and prov != "none":
        if prov == "claude":
            print(CLAUDE_PRIVACY_NOTE, file=sys.stderr)
        else:
            print("[注意] 構造解析のため、各段落の抜粋を Codex (OpenAI) に送ります。"
                  "送りたくない場合は --no-structure を付けてください。", file=sys.stderr)
    if args.translator == "gemini":
        try:
            quota = QuotaState(None if args.ignore_quota_state else quota_path, scope=key_scope(os.environ.get("GEMINI_API_KEY")))
            for n in quota.notes:
                warn(n)
            translator = make_translator("gemini", cfg, log=warn, quota=quota, retry_failed=args.retry_failed)
            translator.check_ready()  # API キー・SDK の有無をここで確認 (キーの有効性は最初のリクエストで分かる)
        except (GeminiUnavailable, GeminiError) as e:
            print(f"エラー: {e}\n(レイアウト確認だけなら --translator dummy を使えます)", file=sys.stderr)
            return 3
    else:
        translator = make_translator(args.translator, cfg)

    charmap = None
    cm_path = wd / "charmap.json"  # 手書きの追加 charmap ({font: {char: str}} または {char: str}) の入口。既定の charmap に上書きマージされる
    if cm_path.exists():
        try:
            charmap = json.loads(cm_path.read_text(encoding="utf-8"))
            if not isinstance(charmap, dict):
                raise ValueError("JSON の辞書ではありません")
        except (OSError, ValueError) as e:
            print(f"エラー: {cm_path} を読めません ({e})。直すか削除してください。", file=sys.stderr)
            return EXIT_BAD_INPUT
    print(f"[1/4] extract: {pdf}")
    try:
        doc = extract_pdf(pdf, wd, charmap=charmap, cfg=cfg)
    except EncryptedPDFError as e:
        print(f"エラー: {e}\nパスワードを解除した PDF を指定してください。", file=sys.stderr)
        return 4
    except PDFOpenError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 5
    for w in doc.get("warnings", []):
        print(f"[警告] {w}", file=sys.stderr)
    nf = sum(len(p["frames"]) for p in doc["pages"])
    ntr = sum(1 for p in doc["pages"] for f in p["frames"] if f["translate"])
    print(f"      pages={doc['num_pages']} frames={nf} translate={ntr} joins={len(doc['joins'])}")
    if ntr == 0:
        print("エラー: 翻訳対象のテキストがありません (テキスト層の無いスキャン PDF? OCR は対象外です)。", file=sys.stderr)
        return 6
    from .assist import Assist
    assist = Assist(cfg, wd, provider=prov, log=warn,
                    enabled=not (getattr(args, "no_assist", False) or args.no_claude or prov == "none" or args.translator != "gemini"),
                    used_by_structure=1)       # 構造解析の分を 1 回と数える ([assist] max_calls_per_doc の内訳: 構造解析 1 + 用語集 1 + 補正 2)
    gl_job = None
    if args.translator == "gemini" and not (wd / "glossary_override.json").exists():
        # 用語集の作成 (Gemini 1 リクエスト) を、構造解析 (Claude/Codex の別プロセス) と同時に走らせて待ち時間を隠す。
        # 用語集の入力はヒューリスティックの role で決まる (構造解析の前の doc の複製を使う。結果は provider によらず同じ)
        import copy
        import threading
        gl_job = {}

        def _glossary_job(d=copy.deepcopy(doc)):
            try:
                g0 = build_glossary(d, translator.client, cfg, wd / "glossary.json", log=warn,
                                    override_path=wd / "glossary_override.json")
                if assist.enabled:          # 用語集の点検 (構造解析と並行して Claude/Codex に 1 回)
                    fr0 = [f for p in d["pages"] for f in p["frames"]]
                    g0 = assist.review_glossary(g0, " ".join(f["text"] for f in fr0 if f["role"] == "title"),
                                                " ".join(f["text"] for f in fr0 if f["role"] == "abstract"))
                gl_job["gl"] = g0
            except BaseException as e:  # noqa: BLE001 - 本体の except 群へそのまま渡す (join のあとで投げ直す)
                gl_job["err"] = e
        gl_thread = threading.Thread(target=_glossary_job, daemon=True)
        gl_thread.start()
    print(f"[2/4] structure ({prov.title()})" if not args.no_claude and prov != "none"
          else "[2/4] structure (ヒューリスティックのみ)")
    sinfo = run_structure(doc, wd, cfg, enabled=not args.no_claude, log=warn)
    if sinfo["ok"]:
        d = sinfo["diff"]
        m = sinfo.get("meta") or {}
        print(f"      {'cache' if sinfo['cached'] else prov}: roles 変更 {len(d['roles_changed'])} / joins +{len(d['joins_added'])} "
              f"-{len(d['joins_removed'])} / charmap +{len(d['charmap_added'])}"
              + ("" if sinfo["cached"] else f" ({m.get('wall_s')}s, out {m.get('output_tokens')} tok)"))
    out.mkdir(parents=True, exist_ok=True)

    print(f"[3/4] translate ({translator.name})")
    units = build_units(doc, cfg)
    cell_units = build_cell_units(doc, cfg)   # 表のセル (ホバー注釈用)。表本体の描画は変えない
    context = None
    gl: list[dict] = []
    t0 = time.time()
    try:
        if args.translator == "gemini":
            print(f"      用語集を差し替えるには {wd / 'glossary_override.json'} を置いてください")
            if gl_job is not None:
                gl_thread.join()
                if "err" in gl_job:
                    raise gl_job["err"]
                gl = gl_job["gl"]
            else:
                gl = build_glossary(doc, translator.client, cfg, wd / "glossary.json", log=warn,
                                    override_path=wd / "glossary_override.json")
            fr = [f for p in doc["pages"] for f in p["frames"]]
            if not (wd / "glossary_override.json").exists():   # 論文ごとの手書き上書きがあるときは、それを最終とする
                fixed = load_fixed(cfg.get("gemini", "fixed_glossary", "glossary_fixed.toml"), log=warn)
                gl, fx_changed = merge_fixed(gl, fixed, " ".join(f["text"] for f in fr if f["translate"]))
                if fx_changed:
                    print(f"      固定訳 (glossary_fixed.toml) を適用: {len(fx_changed)} 語 ({', '.join(fx_changed[:5])}{' ...' if len(fx_changed) > 5 else ''})")
            context = {"ocr": any(p.get("scanned_ocr") for p in doc["pages"]),
                       "title": " ".join(f["text"] for f in fr if f["role"] == "title")[:300],
                       "summary": " ".join(f["text"] for f in fr if f["role"] == "abstract")[:600], "glossary": gl}
            print(f"      glossary: {len(gl)} 語")
        ja_units = translate_cached(translator, units + cell_units, wd / "translation_cache.json", context=context, log=warn)
        if assist.enabled and context is not None:
            _assist_correct(assist, units, ja_units, context, bool(context.get("ocr")), getattr(translator, "failures", None))
    except DailyLimitError as e:
        print(f"エラー: Gemini の日次上限に達しました ({e.model})。ここまでの翻訳は保存済みです。"
              "翌日 (太平洋時間 0 時にリセット) に同じコマンドを再実行すれば続きから再開します。"
              f"(上限の状態は {quota_path} に保存し、期限までそのモデルを避けます)", file=sys.stderr)
        return EXIT_DAILY_LIMIT
    except GeminiFatalError as e:
        print(f"エラー: Gemini の設定・権限に問題があります (再試行しても直りません): {e}\n"
              "API キー (環境変数 GEMINI_API_KEY)・モデル名 (config.toml の [gemini])・利用地域を確認してください。"
              "ここまでの翻訳は保存済みです。", file=sys.stderr)
        return EXIT_FATAL_CONFIG
    except (GeminiTransientError, RateLimitError) as e:
        print(f"エラー: Gemini の呼び出しが回復しませんでした: {e}\nここまでの翻訳は保存済みです。時間をおいて再実行すれば続きから再開します。",
              file=sys.stderr)
        return EXIT_API_ERROR
    except GeminiError as e:
        print(f"エラー: Gemini の呼び出しに失敗しました: {e}\nここまでの翻訳は保存済みです。再実行すれば続きから再開します。", file=sys.stderr)
        return EXIT_API_ERROR
    fallback = 0
    if args.translator == "gemini":
        st = {**translator.stats, "client": translator.client.stats, "elapsed_s": round(time.time() - t0, 1)}
        (wd / "gemini_stats.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        for w in translator.warnings:
            warn(w)
        cs = translator.client.stats
        fallback = translator.stats.get("fallback_original", 0)
        print(f"      gemini: requests={cs['requests']} (429 再試行 {cs['retry_429']}, 5xx 再試行 {cs['retry_5xx']}) "
              f"validate 再送={translator.stats['validate_resend']} 検証失敗(原文)={translator.stats['validate_failed']} "
              f"選択的見直し={translator.stats['selective_fixed']} 全体見直し変更={translator.stats['refine_changed']} {st['elapsed_s']}s")
    cell_ja = {u["id"]: ja_units.pop(u["id"]) for u in cell_units if u["id"] in ja_units}
    if gl:
        ja_units, nrep = normalize_all(ja_units, gl)
        if nrep:
            print(f"      表記ゆれを用語集の表記へ統一: {sum(nrep.values())} 箇所 ({', '.join(f'{k}×{v}' for k, v in list(nrep.items())[:6])})")
    tw: list[str] = []
    ja_frames = frames_translations(doc, units, ja_units, tw)
    for w in tw:
        print(f"[警告] {w}", file=sys.stderr)
    nlab = apply_labels(doc, ja_frames)
    if nlab:
        print(f"      定型ラベルを辞書で訳した frame: {nlab}")
    (wd / "translations.json").write_text(json.dumps({"units": units, "ja": ja_units}, ensure_ascii=False, indent=1),
                                          encoding="utf-8")

    print("[4/4] render")
    ja_pdf = out / f"{pdf.stem}_ja.pdf"
    annotations = build_annotations(doc, cell_units, cell_ja)
    rep = render_ja(pdf, doc, ja_frames, ja_pdf, cfg=cfg, annotations=annotations)
    _sel = sinfo.get("selection") or {}
    rep["structure"] = {"used": bool(sinfo.get("used", sinfo.get("ok"))),
                        "status": sinfo.get("status", "accepted" if sinfo.get("ok") else "skipped"),
                        "accepted": _sel.get("accepted", 0), "rejected": _sel.get("rejected", 0),
                        "reason": sinfo.get("reason", "")}      # 構造解析の role の変更を採用/却下した件数 (M12)
    _fl = getattr(translator, "failures", None) or {}
    rep["failures"] = [{"id": k, "reasons": v} for k, v in _fl.items()]      # 原文のまま残った段落の原因 (M15)
    for k, v in list(_fl.items())[:20]:
        print(f"      原文のまま: {k} ← {'; '.join(v)[:120]}")
    rep["assist"] = assist.info          # 補助の呼び出し回数・採用/却下 (用語集の点検・問題段落の補正)
    save_report(rep, wd / "render_report.json")
    if assist.info.get("calls") or assist.info.get("skipped"):
        c_ = assist.info.get("correction") or {}
        g_ = assist.info.get("glossary") or {}
        print(f"      assist ({prov}): 呼び出し {assist.info['calls']} 回 / 用語集の修正 {len(g_.get('adopted', []))} 件採用 {len(g_.get('rejected', []))} 件却下 / "
              f"問題段落の補正 {c_.get('adopted', 0)}/{c_.get('requested', 0)} 件採用" + (f" / スキップ: {assist.info['skipped']}" if assist.info.get("skipped") else ""))
    lk = rep["links"]
    print(f"      {ja_pdf}  frames={rep['frames']} shrunk={rep['shrunk']} ({rep['shrunk_ratio']:.1%}) "
          f"min_scale={rep['min_scale']} expanded={rep['expanded']} failed={rep['failed']}")
    print(f"      links: 原文={lk['orig']} 維持={lk['kept']} 復元={lk['restored']} "
          f"翻訳領域内の外部URL(訳文<a>で再作成)={lk['inside_uri']} 翻訳領域内の内部リンク(消失)={lk['lost_internal']}")
    for w in rep["warnings"]:
        print(f"[警告] {w}", file=sys.stderr)
    if args.mode in ("dual", "both"):
        dual_pdf = out / f"{pdf.stem}_dual.pdf"
        n = build_dual(pdf, ja_pdf, dual_pdf)
        ol, ot = count_links_toc(pdf)
        dl, dt = count_links_toc(dual_pdf, list(range(0, n, 2)))
        print(f"      {dual_pdf}  pages={n}  英語ページのリンク {dl}/{ol} しおり {dt}/{ot}")
    if args.mode == "dual":
        ja_pdf.unlink(missing_ok=True)
    if fallback:
        print(f"[警告] 翻訳できず原文のまま出力した段落が {fallback} 件あります (上の警告を参照)。PDF は出力しました。"
              "--retry-failed を付けて再実行すると、その段落だけ再翻訳を試みます。", file=sys.stderr)
        return EXIT_PARTIAL
    return 0
