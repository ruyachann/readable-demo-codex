# Readable代替ツール 計画書 v2 (Opus 5.5)

## 0. 要件 (ユーザー合意済み)
- **PDFを入れたらPDFが返る。手作業の往復なし。**
- 出力: `<name>_ja.pdf` (日本語のみ) と `<name>_dual.pdf` (**英p1→日p1→英p2→日p2… の交互**)
- 翻訳は Gemini API 無料枠。日本語品質はできるだけ Gemini 側で出す (プロンプト探索を行う)
- **構成 (PDF構造の整理) は claude.ai の使用量で Claude (Sonnet) に任せる。Claude API は使わない** → `claude -p --model sonnet` (Claude Code ヘッドレス。claude.ai サブスクリプション認証で動き、API課金なし)
- 本番での claude.ai 使用量は最小に。1論文につき構造解析で Sonnet 1〜数回、テキストのみ (画像は送らない)
- 図・画像はそのまま。図内の文字は英語のまま。キャプションと本文は日本語化
- 文字サイズは原文通りでなくてよい (読みやすさと枠内収容を優先)
- UI: CLI + ドラッグ&ドロップ用 `翻訳する.bat`
- テスト用PDF: `english_paper/JSR-34-e70000.pdf` (Wiley, 10p, 2段組, サイド欄, 下付き/上付き多数), `english_paper/jzbg-07-00024.pdf` (MDPI, 12p, 左サイドバー+1段本文, ®・下付き)

## 1. パイプライン
```
in.pdf
 ① extract     (PyMuPDF)   行/スパン単位で抽出 → ヒューリスティックで段落ブロック化・役割推定
 ② structure   (Claude Sonnet, claude -p) ブロック一覧(ID・座標・フォント・先頭/末尾数十字)を渡し
                読み順・段落の結合(段/ページまたぎ)・役割・翻訳要否を JSON で返させる
                ※ CLI が無い/失敗時は ① のヒューリスティック結果で続行
 ③ glossary    (Gemini)    タイトル+要旨+見出し+本文抜粋 → 用語集 (英→日) を作成
 ④ translate   (Gemini flash-lite) 3〜4ページ分の段落を1リクエストで。用語集・論文概要・直前段落を文脈に
 ⑤ refine      (Gemini flash) EN/JA対を見直し (誤訳・訳抜け→自然さ→用語統一)。修正のみ返させる
 ⑥ validate    [n]・URL・数値・<sup>/<sub>・プレースホルダの個数一致チェック → 不一致のみ再送
 ⑦ render      日本語版 / 交互対訳版
```
全中間成果物は `work/<name>/` に保存 (doc.json, structure.json, glossary.json, cache/)。再実行時はキャッシュ済み工程をスキップ。

## 2. データモデル (doc.json)
```
pages[]: {number, width, height,
  blocks[]: {id:"p3-b12", bbox, lines[], text, html(sup/sub/italic保持), font_size, color,
             bold, italic, serif, align, role, translate, group_id, order}}
groups[]: {id:"g15", block_ids:[...], role, text_en, text_ja}   # 段/ページまたぎ段落を1単位に
```
- 翻訳の単位は **group** (論理段落)。描画は **block** (物理的な枠)。
- group を複数 block に戻す方法: Gemini に `⟦1⟧` 区切りマーカーを訳文中に入れさせる (英文側のブロック境界に対応)。マーカー欠落時は文字数比で文境界(。)に近い位置で分割。
- インライン装飾: 上付き(flags&1)・下付き(基線/サイズ差)・斜体は `<sup>`,`<sub>`,`<i>` として保持し、プロンプトで「タグは保持」と指示。数式記号・ギリシャ文字はそのまま。

## 3. 役割と翻訳対象
| role | 翻訳 | 備考 |
|---|---|---|
| title / heading / body / abstract / caption / footnote / table_note / sidebar(所属・受付日など) | する | caption は「図1」「表2」形式 |
| figure_text (図内ラベル) | しない | 図はそのまま |
| table_cell | 初期はしない (M4で検討) | 表の構造崩れを避ける |
| reference (参考文献) / author / doi / url / page_header / page_number / math | しない | |

## 4. 描画
- 日本語版: 元ページ複製 → 翻訳対象 block を `add_redact_annot(fill=False)` + `apply_redactions(images=NONE, graphics=LINE_ART_NONE)` → `insert_htmlbox` で流し込み。
- フォントサイズ: 原文サイズ×0.9 程度から開始し、収まらなければ段階縮小 (下限 原文×0.6 かつ 5pt)。それでも収まらなければ枠を下方の空白まで拡張 → 最後に scale_low=0。
- フォント: serif → 游明朝 (yumin.ttf / 太字 yumindb.ttf)、sans → 游ゴシック or メイリオ (埋め込み確認の上決定)。可変フォント(Noto*-VF)は使わない。Archive/CSS は文書で1回生成。
- 行間 1.5 前後、禁則は MuPDF 任せ (要確認)、左右揃えは原文の align。
- 交互対訳版: 新規PDFに 原文p → 日本語p を交互に `insert_pdf`。
- サブセット化 (`subset_fonts`) でファイルサイズ削減。

## 5. Gemini 設定
- 翻訳 `gemini-3.5-flash-lite` / 見直し・用語集 `gemini-3.5-flash` (モデル別に無料枠が分かれる)。`config.toml` で変更可。
- thinking_level="minimal" (3.x)。structured output (response_schema)。
- レート: 既定 RPM 5、429 は retryDelay 尊重。1論文(10p)あたり ≈ 用語集1 + 翻訳3 + 見直し3 + 再送数回 ≈ 10リクエスト。
- APIキーは環境変数 GEMINI_API_KEY のみ。ログに出さない。

## 6. プロンプト探索 (M3)
- 2論文から代表 20 段落 (本文/見出し/キャプション/要旨/下付き含む/段またぎ) を評価セットに固定。
- 3〜4 案 (現行 prompts/gemini_translate.md, 用語集あり/なし, 2段階 vs 1段階, few-shot あり) を比較。
- 採点: Gemini flash を審査員 (正確性・自然さ・用語統一・タグ保持の4観点, 1〜5点) + 自動検査。最後に Opus が上位案のサンプルを目視確認して決定。

## 7. モジュール構成
```
readable/
  __main__.py / cli.py   python -m readable in.pdf [--out DIR] [--mode ja|dual|both] [--no-claude] [--translator gemini|dummy]
  config.py              config.toml 読み込み (モデル名, RPM, フォント, 閾値)
  extract.py             ①
  structure.py           ② Claude CLI 呼び出し + ヒューリスティック フォールバック
  glossary.py            ③
  translate.py           ④⑤⑥ (GeminiClient, DummyTranslator, キャッシュ, レート制御, 検証)
  render.py              ⑦
  fonts.py               フォント解決・Archive/CSS 生成
prompts/  gemini_translate.md, gemini_refine.md, gemini_glossary.md, claude_structure.md
tests/    単体テスト (pytest) + 評価セット + プレビューPNG生成スクリプト
翻訳する.bat, README.md, requirements.txt, config.toml
```

## 8. マイルストーン
- **M1** extract + structure(ヒューリスティック) + render を DummyTranslator で。2論文で日本語版/交互版を生成し PNG で確認 (図が消えない・枠からはみ出さない・空枠なし)。
- **M2** Claude 構造解析 (claude -p) を組み込み、ヒューリスティックとの差分を確認。
- **M3** Gemini 用語集/翻訳/見直し/検証 + プロンプト探索。
- **M4** 仕上げ (表セル・細部配置・bat・README・最終レビュー)。
各マイルストーン末に: コードレビュー担当 → 成果物確認担当 → 進捗メモ担当 (docs/PROGRESS.md)。

## 9. 体制
- Opus: 計画・判断・レビュー結果のトリアージ・詰まった時の交代
- Sonnet: 実装 / コードレビュー / 成果物確認 (実行+スクショ) / 進捗メモ / 調査
- エスカレーション: 同じ問題で2回失敗 → `BLOCKED:` 報告 → Opus が相談または実装交代

## 11. v3: 計画レビュー (docs/PLAN_REVIEW.md) の反映 — §1〜§5 より優先
1. **翻訳・描画の単位は「段落 frame」**。extract で行を集め、段内の連続行を字下げ・最終行の短さ・行間・フォント変化で段落に分割し、行 bbox の和集合を frame とする (JSRは1ブロック=1行、MDPIは1ブロック=1段落で粒度が違うため、ブロックに依存しない)。`⟦n⟧` マーカーは **段/ページまたぎの結合 (joins) のみ** に使う。
2. **Claude 構造解析** (`claude -p`):
   - 入力は frame 一覧 (id, page, bbox を丸めた値, size, font種別, 先頭/末尾40字, ヒューリスティック role)。参考文献のように確度の高いものは事前に確定して送らない。
   - 出力は `{roles:{id:role}, joins:[[id,id]], charmap:{"\u0003":"°"}}` だけにする。order は返させない (stream順 ≒ 読み順)。
   - フラグ: `--model sonnet --tools "" --strict-mcp-config --setting-sources "" --disable-slash-commands --no-session-persistence --system-prompt <file> --json-schema <schema> --output-format json` (オーバーヘッドは約31k → 約700 tokens)。stdin で入力。Windows は shell 経由。結果は work/ にキャッシュ。失敗時はヒューリスティックで続行。
3. **記号フォントの文字化け** (JSR: \x03=°, \x01=−, \x04=~, \x05=×, þ/ð=+/( ) は charmap で置換する。charmap は Claude が推定し、既知の表を既定値とする。数式片は `{vN}` プレースホルダで保護する。
4. 上付き = flags&1。下付き = サイズ<0.85×行の主サイズ かつ 基線より下。小型大文字や † の誤検出を避けるため、行内の相対位置で判定する。
5. 表 (MDPI): drawings の罫線で表領域を推定し、その中の frame は table として翻訳しない (M4で再検討)。図は全てラスタ。画像に重なる・隣接する小さい文字 ("(a)") は figure_text とする。
6. ハイフン解除は文書内の語彙で判定する (結合形が文書内に存在すれば結合、そうでなければハイフンを残す)。字間空白 ("R E S E A R C H") は正規化する。
7. **フォントサイズは原文サイズで開始** し、収まらない時だけ縮小する (実測で日本語は英語の0.34〜0.40倍の文字数で、原文サイズで収まる)。
8. ~~見直し既定ON~~ → **(v4, PROMPT_EVAL 反映) 翻訳は flash-lite 単段 + プロンプト C3。refine は既定 OFF とし、validate 失敗/英語残りの unit だけ選択的に見直す。** 用語集 (flash, 1論文1回) は維持。flash の日次上限時は flash-lite → 用語集なしの順にフォールバックする。
9. プロンプトを計画に揃える: ⟦n⟧ を保持すること、<sup>/<sub>/<i> と {vN} を保持すること、type を role 一覧に一致させること。
10. フォントは subset_fonts で軽量化する。交互版は日本語版PDFのページを再利用する。README と CLI 実行時に「無料枠の入出力は学習に使われうる」旨を警告する。

## 10. 調査結果 (docs/RESEARCH.md より)
- 可変フォント(NotoJP-VF)は極細で描画 → 不使用。meiryo.ttc / msmincho.ttc / yumin(db).ttf 埋め込み確認済み、YuGoth 未確認。
- `insert_htmlbox` は scale_low を下回ると何も描かず (-1, scale) を返す → フォールバック必須。
- Windows コンソールは `PYTHONIOENCODING=utf-8`。
