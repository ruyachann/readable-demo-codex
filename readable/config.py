"""config.toml の読み込み。既定値は DEFAULTS に一元化し、config.toml は上書き分だけを書く。"""
from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.toml"


def atomic_write_text(path: str | Path, text: str) -> None:
    """一時ファイルに書いてから os.replace で置き換える (書き込み中の中断でファイルが壊れない)。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)

DEFAULTS: dict[str, dict[str, Any]] = {
    "paths": {"work_dir": "work"},        # 中間成果物の置き場 (相対パスはプロジェクトのルート基準)。日次上限の状態 (.quota_state.json) もここ
    "gemini": {"translate_model": "gemini-3.5-flash-lite", "refine_model": "gemini-3.5-flash", "rpm": 5,
               "refine": False, "thinking_level": "minimal", "temperature": 0.2, "max_retries": 4,
               "glossary": True,
               "fixed_glossary": "glossary_fixed.toml",   # 固定訳 (論文ごとの自動用語集より優先)
               "timeout": 120,               # 1 リクエストのタイムアウト (秒)
               "retries_5xx": 3,             # 5xx/タイムアウト/接続エラーの再試行回数 (指数バックオフ。尽きたら終了コード 9)
               "pages_per_batch": 6,         # 1 リクエストにまとめるページ数 (unit の開始ページ基準。M9: 3 -> 6 でリクエスト数を減らす)
               "max_chars": 18000,           # 1 リクエストの原文文字数の上限 (翻訳)
               "workers": 3,                 # 独立なバッチを同時に送る数 (送信の間隔は rpm で守る)
               "refine_max_chars": 6000,     # 〃 見直し (原文+訳文を送るので小さめ)
               "validate_retries": 1,        # 検証 NG の unit のバッチ内での再送回数 (残りは選択的再翻訳で全バッチ分をまとめて 1 回)
               "prev_units": 3,              # 前バッチ末尾から prev_text に渡す unit 数
               "translate_prompt": "prompts/gemini_translate.md",
               "refine_prompt": "prompts/gemini_refine.md",
               "glossary_prompt": "prompts/gemini_glossary.md"},
    "claude": {"model": "sonnet", "timeout": 120,
               "allow_gateway": False},   # 開発用: API ゲートウェイ設定 (ANTHROPIC_BASE_URL) があっても Claude を使う。既定 OFF (検出したらスキップ)
    "assist": {"max_calls_per_doc": 4},        # 翻訳の補助 (用語集の点検・問題段落の補正) の Claude/Codex 呼び出しの上限 (構造解析 1 + 用語集 1 + 補正 2)
    "codex": {"model": "gpt-5.6-sol", "timeout": 180},
    "structure": {"provider": "claude"},      # claude | codex | none (ヒューリスティックのみ)
    # 表の用語と本文の対応づけ (M6): 表・図キャプションの英語の用語を、本文で「日本語 (English)」の形に保つ
    "table_terms": {"keep_en": True,          # 本文に表の用語の英語を毎回併記する (用語集の keep_en)
                    "color": "",              # 本文中の (English) 部分の色。既定は色なし・下線なし ("#1a3d8f" などで色と下線)
                    "annotations": False,     # 表のセル・図の中の文字の和訳注釈 (ホバー表示) を付けるか。既定は付けない (M12。付けないときは表・図の語を訳さず、Gemini の要求も増えない)
                    "annotation_style": "invisible",   # "invisible" (完全に透明。見える印なし) | "highlight" (不透明度 1%) | "text" (小さな付箋アイコン)
                    "max_cells": 200,        # 注釈用に訳す表のセルの上限
                    "max_figure_cells": 300,  # 注釈用に訳す図の中の文字の上限
                    "gloss_first_only": True},   # 表・図の用語の英語併記は各ページの初出だけ
    "fonts": {
        # 先頭から順に試し、埋め込めなかったものは次の候補にする。可変フォントは使わない。
        "serif_regular": ["C:/Windows/Fonts/yumin.ttf", "C:/Windows/Fonts/msmincho.ttc",
                          "/System/Library/Fonts/ヒラギノ明朝 ProN.ttc", "/usr/share/fonts/opentype/ipafont-mincho/ipam.ttf"],
        "serif_bold": ["C:/Windows/Fonts/yumindb.ttf"],
        "sans_regular": ["C:/Windows/Fonts/YuGothM.ttc", "C:/Windows/Fonts/meiryo.ttc",
                         "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc", "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"],
        "sans_bold": ["C:/Windows/Fonts/YuGothB.ttc", "C:/Windows/Fonts/meiryob.ttc"],
        # ラテン文字の斜体 (任意)。統計記号などの <i> に使う
        "serif_italic": ["C:/Windows/Fonts/timesi.ttf", "/Library/Fonts/Times New Roman Italic.ttf"],
        "sans_italic": ["C:/Windows/Fonts/ariali.ttf", "/Library/Fonts/Arial Italic.ttf"],
    },
    "extract": {
        "row_gap_min": 14.0,          # 同一行内で別の段とみなす最小の空き(pt)
        "row_gap_factor": 1.8,        # 〃 サイズ倍率
        "para_max_pitch": 1.9,        # 行送り上限 (サイズ倍)
        "para_indent_factor": 0.8,    # これ以上字下げされた行は新段落 (サイズ倍)
        "short_line_factor": 2.5,     # 前行がこれ以上短ければ段落末 (サイズ倍)
        "size_tol_abs": 0.6,
        "size_tol_rel": 0.06,
        "sub_size_ratio": 0.85,       # 下付き判定
        "repeat_min_pages": 3,        # ヘッダ/フッタ判定: 同一文字列の最小出現ページ数 (これ未満の文書は位置+サイズで判定)
        "header_zone": 0.06,          # ページ上下この割合以内 = ヘッダ/フッタ帯 (位置判定)
        "header_zone_repeat": 0.10,   # 反復判定の帯
        "scan_min_chars": 20,         # これ未満の文字しか無く画像があるページはスキャンとみなす
        "table_min_width_frac": 0.08, # 表の罫線とみなす最小幅 (ページ幅比)
        "table_margin_frac": 0.07,    # ページ上下この割合内の罫線は表から除外
        "table_tabular_ratio": 0.25,  # 罫線2本/枠のみの領域を表とみなす「同一基線に複数セルがある行」の割合
        "translate_tables": False,    # True なら表本体も翻訳対象にする
        "vector_figures": True,       # ベクタ図の領域 (cluster_drawings) の中の短い文字を翻訳しない
        "gutter_split": True,         # 段間 (ガター) を検出し、左右の列の行を結合しない (2 段組の論文向け)
        "table_terms_max": 60,        # 表・キャプションから集める用語の上限
        "vector_dense_min_paths": 60,  # ベクタ図とみなす密度: 36pt 四方のセルあたりの小さな図形の数 (その中の小さな文字は figure_text)
        "protect_inline": True,       # メール/URL/DOI/助成番号/数式片を {vN} で保護する
    },
    "render": {
        "line_height": 1.5, "shrink_step": 0.05, "min_scale": 0.7, "min_font": 6.5, "expand_margin": 4.0,
        "bottom_margin": 24.0, "redact_inset": 0.5, "justify": True, "subset_fonts": True,
        "obstacle_min_size": 1.0,     # この大きさ未満の図形は障害物にしない (pt)
        "wide_heading": True,         # (互換用。M4 以降は常に列幅まで広げて折り返す)
        "uniform_style": True,        # role ごとに文書全体で基準サイズ/行間を1つに揃える (M4)
        "line_height_min": 1.4,       # 行間 (サイズ倍) の下限/上限: 原文の行送りの中央値をこの範囲に収める
        "line_height_max": 1.65,
        "indent_em": 1.0,             # 原文で字下げされた本文段落の字下げ (和文の慣習どおり 1 字)
    },
}


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        merged = copy.deepcopy(DEFAULTS)
        for sec, vals in (self.data or {}).items():
            if isinstance(vals, dict):
                merged.setdefault(sec, {}).update(vals)
            else:
                merged[sec] = vals
        self.data = merged

    def section(self, name: str) -> dict[str, Any]:
        return self.data.get(name, {})

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.data.get(section, {}).get(key, default)


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if not p.exists():
        return Config({})
    with open(p, "rb") as f:
        return Config(tomllib.load(f))
