# 進捗メモ

## 2026-10-01 M1 (extract + render + dummy 翻訳) 実装完了

実装担当: Sonnet。計画は PLAN.md §11 (v3) に従う。

### 作ったもの
- `readable/config.py`, `config.toml`, `readable/fonts.py` (yumin/yumindb, YuGothM/B -> meiryo のフォールバック。Archive+CSS は1回)
- `readable/extract.py`: span -> 行(row, 同一基線+列ギャップ) -> 段落 frame。<sup>/<sub>/<i> 保持、charmap (既定 + 差し替え可)、
  字間空白の復元、語彙ベースのハイフン解除、role/translate 判定、joins、表領域 (罫線)・図領域 (画像bbox)
- `readable/translate.py`: Translator / DummyTranslator / unit 構築 / ⟦n⟧ 分割 (無ければ文字数比で「。」寄せ) / sanitize
- `readable/render.py`: redact(fill=False, IMAGE_NONE, LINE_ART_NONE) -> insert_htmlbox。段階: 原文サイズ -> 5%刻み縮小
  (下限 0.75 倍/5.5pt) -> 枠を下へ拡張 -> scale_low=0。subset_fonts。交互版は insert_pdf 2回 + select
- `readable/cli.py`, `__main__.py`, `tests/` (pytest 26件 + preview.py), README.md, requirements.txt

### 実測 (dummy 翻訳, 日本語=英文の0.38倍)
- JSR: 113 frame 中 縮小 0 (0%), 最小 scale 1.0。MDPI: 65 frame 中 縮小 6 (9.2%, 全て斜体の1行小見出し, 0.85〜0.95)。失敗 0
- 出力サイズ: JSR ja 3.1MB / dual 3.1MB, MDPI ja 1.3MB / dual 1.4MB (subset_fonts で日本語フォントは1個に集約)
- 目視 (PNG): 図は消えない / 英文の消し残しなし (本文領域のテキスト抽出でも確認。残るのは <sub> 内の変数名のみ) /
  豆腐なし (− ° ± × ≤ α 等も游明朝+フォールバックで表示) / 枠はみ出しなし

### 設計上の判断・メモ
- 行の矩形は frame の行ごとに redact (上下 0.5pt 内側)。図・隣接行の巻き込みなし。
- 描画の行間は原文の行送り (1.3〜1.65) に合わせる。1行 frame・短い frame (見出し等) は同じ列の右端まで幅を広げる。
- 1行 frame 等で原文より日本語が長い場合は、まず下の余白 (次の要素の手前) まで拡張する。
- 結合 (joins) の frame 境界の行末ハイフンは、語を前 frame に寄せて解除する。

### 未解決・次の課題
- 参考文献は hanging indent のため細かい frame に分かれる (翻訳しないので無害)。
- 著者行 (JSR) は区切りの | で frame が断片化する (翻訳しないので無害)。
- 数式の {vN} プレースホルダは未実装 (M3 で必要なら)。
- 実際の Gemini 訳は dummy より長くなる可能性がある。M3 で縮小頻度を再計測すること。
- M2: Claude 構造解析 (roles/joins/charmap の補正) を `claude -p` で。

## 2026-10-01 M1.5 (REVIEW_M1 の指摘修正) 完了

実装担当: Sonnet。docs/REVIEW_M1.md の S1〜S5 / M1〜M7 / 軽微と、Opus 判断 (依頼書) に沿って修正。pytest 73 件 (既存 26 + 新規 47) 全通過。

### 変更点
- **リンク (G1)**: extract で rawdict の文字位置から URI リンクを `<a href>` として行 html に保持 (リンクが行の一部だけでも可)。render は redact 前後で get_links を比較し、
  翻訳領域外で消えたリンクを insert_link で復元、翻訳領域内の外部 URL は訳文 `<a>` から insert_htmlbox が再作成、内部リンク (引用) は消失として件数を render_report の `links` に記録。
- **交互版 (G2)**: 原文を土台に日本語ページを 1 回だけ insert_pdf → move_page で並べ替え。しおりは set_toc で p→2p-1、英語ページのリンクは原文のページ参照のまま
  (宛先は自動で 2p-1)、日本語ページのリンクは宛先を日本語ページに作り直し (LINK_NAMED は解決して GOTO)。
- **role (S1-S3)**: 少ページ文書は位置 (上下 6%)+サイズ+短さでヘッダ判定、title は header 判定より前。author は「title〜最初の見出し/要旨」かつ人名・所属らしい (機能語密度) frame のみ。
  `Abstract—` run-in 対応。参考文献は見出し (role 不問) または `[n]` 項目の連続で開始、見出し級/Appendix/Disclaimer 等/長い散文で終了。後付け文は翻訳対象。
- **S4**: 暗号化 → 終了コード 4、PDF 以外 → 5、スキャン (テキスト無し) ページは警告して原文のまま、全ページ無しは終了コード 6。
- **インライン保持**: メール/URL/DOI/助成番号/数式フォント片を `{vN}` で保護 (build_units 時)。ほぼ識別子だけの frame は translate=false。太字 run は `<b>`、リンクは `<a href>`。
- **M2 ハイフン**: タグをまたぐ行末ハイフンを解除。doc.json の html は非破壊、結合時のハイフン解除は build_units が最終 joins から計算。
- **M3**: ⟦n⟧ は 1..n-1 の順序検証、フォールバック分割はタグ/実体参照/{vN} の途中で切らず閉じ・再オープン。validate_translation を追加。
- **M4/M5**: 障害物に罫線・ベクタ図形・背景塗り・画像・他 frame。rect0 も障害物の手前でクリップ (行間を詰めて収める)。1 行見出しは縮小より先に横→下へ広げる。縮小下限 0.7、scale_low0 は警告。
- **M6**: schema_version=2、`apply_structure(doc, roles=, joins=, charmap=)` (joins 検証: 循環/逆行/存在しない id/重複/非翻訳)、翻訳キャッシュ (sha256(unit html, model, prompt hash))、charmap は font 名ごと + PUA は unmapped 記録。
- **M1/M7**: 表検出は罫線グループ (本数・幅・間隔の固定値なし)・縦罫線格子・矩形セルを使用。回転ページは見た目座標で保持し、書き込み時に derotation_matrix (insert_htmlbox は rotate)。
- **軽微**: デッドコード/`or True` 削除、CLI 既定を dummy、work 名にパスのハッシュ、フォント候補/既定値を config に一元化、`translate_tables=false`、doi_url 判定と数式判定の分割・修正。

### 実測 (dummy 0.38)
- JSR: 113 訳対象 / 縮小 0 / 重なり 0。リンク 原文 197 (名前付き 104 + URI 93): 非翻訳領域の維持 110、翻訳領域内の内部リンク消失 84、URI は訳文から再作成 3。交互版の英語ページ 197/197、しおり 27/27。
- JZ: 66 訳対象 / 縮小 6 (最小 0.85) / 重なり 0。リンク 維持 36、消失 61、URI 再作成 2。交互版 英語ページ 130/130、しおり 18/18。
- JSR p1 サイドバーのメール・助成番号 (07_DG_2022_07) は保持、JZ p12 の Disclaimer は訳対象。
- ストレス (dummy 比率 1.0): 重なり 0 のまま、JSR は scale_low0 が 16 件・警告 15 件 (最小 0.53)、JZ は 26 件・警告 25 件。M3 の実訳で再計測すること。

### 既知の制約・M2/M3 への引き継ぎ
- 翻訳領域内の内部リンク (引用 [n]) は失われる (Opus 判断どおり)。外部 URL リンクは insert_htmlbox が語単位の断片に分けて再作成する (JSR 93→118 個, JZ 69→100 個: 見た目は同じ)。
- ベクタ図 (非ラスタ) の領域検出は未実装 (表検出のみ拡張)。LaTeX 本文フォント (CMR) は数式扱いしない。
- 縦書き・斜め文字は対象外 (無視)。OCR は対象外。

## 2026-10-01 M2 (Claude 構造解析) + M3 (Gemini 翻訳パイプライン) 完了

実装担当: Sonnet。pytest 111 件全通過 (新規 `tests/test_m23.py` 38 件: Claude/Gemini は全てモック)。

### M2 (`readable/structure.py`, `prompts/claude_structure.md`, 詳細 docs/M2_DIFF.md)
- claude.exe を直接起動 (`--tools "" --strict-mcp-config --setting-sources "" ...`)、入力は stdin。参考文献等は送らない。出力 `{roles, joins, charmap}` を `apply_structure` で適用。`work/<name>/structure.json` にキャッシュ。
- CLI なし/タイムアウト (1 回リトライ)/JSON 不正は警告してヒューリスティック続行。`--no-claude` で無効化。charmap は 制御文字/PUA の 1 文字キーのみ採用。
- 実測: JSR 7〜9s / MDPI 3.6〜5s。差分は JSR で 2 frame (citation を reference に)、MDPI で 1 frame。claude -p は計 4 回。

### M3 (`readable/gemini_client.py`, `glossary.py`, `translate.py` の GeminiTranslator)
- 翻訳 flash-lite、用語集/選択的再翻訳 flash (日次上限なら flash-lite にフォールバック、それも駄目なら用語集なしで続行)。thinking_level=minimal (2.5 系は budget=0、400 なら thinking を外して再試行)。構造化出力 (response_schema)。
- バッチ: 開始ページ基準で 3 ページ/9000 文字ごと。context = title / summary / prev_text (前バッチ末尾 3 unit の原文) / glossary (バッチに出現する語だけ、en/ja のみ)。
- 検証 (check_translation = validate_translation + [n] 引用 + 数値欠落): NG の unit だけ note つきで再送 (最大 2 回)。それでも NG なら選択的な再翻訳 (refine_model→flash-lite)。⟦n⟧ だけの崩れは採用し、frame 分割は文字数比。最終的に駄目なら原文を描画して警告。
- 英語残り検出 (leftover_english: 本文系 role でラテン文字 6 語連続 / 比率 0.7 超) の unit も選択的に再翻訳。全 unit の refine は設定 `refine` で ON (既定 OFF、Opus 判断)。
- レート制御: RPM 既定 5 (12 秒間隔)、429 は retryDelay を尊重 (最大 4 回)、per-day は即中断 (`DailyLimitError`)。翻訳段階の日次上限は「そこまでの訳をキャッシュ保存して終了コード 7、翌日の再実行で続きから再開」と案内。
- キャッシュ: 既存の sha256 キー方式 (翻訳段階/選択的再翻訳/全体見直しの 3 種のキー)。バッチごとに保存。失敗 (原文) の unit は保存しない。
- 用語集は `work/<name>/glossary_override.json` ([{"en","ja"}] または {"en":"ja"}) で完全に置き換え可能 (API を呼ばない)。
- CLI: 既定を gemini に戻し (dummy は `--translator dummy`)、実行時に無料枠の学習利用の警告を 1 行表示。GEMINI_API_KEY 未設定は終了コード 3。`work/<name>/gemini_stats.json` に統計を保存。

### 実測 (実 Gemini、既定設定: refine OFF + 選択的再翻訳、flash は当日上限のため全てフォールバック)
| | JSR (10p) | MDPI (12p) |
|---|---|---|
| 訳対象 frame / 縮小 frame | 111 / 5 (4.5%) | 66 / 1 (1.5%) |
| 最小スケール / scale_low0 警告 | 0.70 / 0 | 0.68 / 1 (p2-f5) |
| validate 再送 unit 数 | 2 | 3 (+ ⟦n⟧ のみの崩れ 1 を採用) |
| 選択的再翻訳で修正 | 1 (ライセンス脚注の未訳) → 再実行で キーワード 1 | 0 |
| リクエスト数 (初回通し) | 10 (+ 再実行 2) | 11 (5xx リトライ 1 含む) (+ 再実行 1) |
| 所要 (翻訳段階) | 95s | 97s |
- Gemini 実リクエスト合計は約 39 (2 ページ試験 + 設計確認の再実行を含む、上限 40)。flash は探索担当の使用で日次上限に達しており、用語集・選択的再翻訳は flash-lite で実行された。
- 目視 (PNG JSR p1,3,5,7 / MDPI p1,2,4,8): 訳文は自然で欠落なし。出力 PDF のテキストに `<i>` `{vN}` `⟦` `&amp;` の漏れ 0。上付き・下付き・p/t 値・引用・リンクは維持。図と表は無傷。
- 気づき: (a) 同一論文内で「カワウソ/かわうそ」の表記揺れ (用語集では防げていない)、(b) JSR p7 の段またぎ脚注は縮小 (0.7) で小さい、(c) JSR u17 に「過去2十年間」(モデルの誤り)。キーワード・ライセンス脚注を未訳で返す癖があり、選択的再翻訳で直った。

### 未解決・次の課題
- 日次上限の状態は同一プロセス内でしか覚えない (flash が上限だと各実行で 1 回だけ無駄な 429 を踏む)。
- 全 unit の refine は未検証 (flash 上限のため実機未実施。モックのみ)。
- 表本体は翻訳しない (M4)。


## M4 paused (2026-10-01)

実装担当: Sonnet。ユーザー指示で一時停止。pytest **155 件全通過** (既存 111 + 新規 `tests/test_m4.py` 44 件 - 1 件は M4 の仕様変更に合わせて `test_m23.py::test_leftover_english` を更新)。

### (a) 完了
- **M4 指示**
  - 1 段落の見た目の統一: `render.py` に `build_style_book` (role ごとの基準サイズ=文字数重み付き最頻、行間=原文行送りの中央値を 1.4〜1.65 に収める、字下げ=原文が字下げの body/abstract に 1em) と `_flow_page/_flow_chain` (同じ列の連なりを原文の段落間隔を保って上から配置。収まらないときだけ最も高い段落から 5% ずつ縮小→行間を詰める→scale_low=0)。`uniform_style` で ON/OFF。MDPI p8/p2 で統一と字下げを目視確認済み (全ページの最終目視は未実施)。
  - 2 Keywords: 「日本語訳 (English)」形式に決定 (識別性・検索性のため)。translate プロンプトに規則、`leftover_english` が keywords の英語のままを検出。MDPI p1 で確認 (JSR p1 は未目視)。
  - 3 表記ゆれ: glossary プロンプトに「基本名詞も入れる」「ja は 1 表記」「variants」を追加。`glossary.normalize_all` (かな変換・長音・variants を用語集の表記へ。英字を含む別表記・正規表記を含むより長い語・漢字の熟語の一部は対象外) を cli で適用。MDPI の「かわうそ」は 0 件に。
  - 4 ± の空白: 原因は全角空白ではなく **両端揃え (justify) が和文中の少ない半角空白を引き伸ばす**こと。`render.protect_spaces` で和文/記号/数値に接する空白を NBSP にして解決 (テストで空白幅 2.6pt を確認)。JSR p3 で目視確認。
  - 5 日次上限の永続化: `QuotaState` (`<work>/.quota_state.json`, 太平洋時間の次の 0 時, API キーの sha256 先頭 8 桁でスコープ)。実行時に実際に再利用されることを確認 (2 回目以降は flash を避ける)。
  - 6 「過去2十年間」: translate プロンプトに数詞規則 1 行 (最小変更)。**docs/PROMPT_EVAL.md への追記は未実施** (下記 (c))。
  - 7 `翻訳する.bat`: 作成済み (ドロップ→PDF と同じフォルダへ出力、複数対応、`%~dp1.` で引用符対策、括弧ブロック不使用、chcp 65001、PYTHONIOENCODING、python/ライブラリ無しのメッセージ、pause)。**実行確認は未実施**。
  - 8 README.md: 全面書き直し済み (必要環境・セットアップ・使い方・出力・無料枠と再開・glossary_override・終了コード・トラブルシューティング・プライバシー)。
- **レビュー修正 (REVIEW_M23 / Opus 決定 1〜17)**: 全て実装し、`tests/test_m4.py` でテスト。
  - 1 エラー分類: `GeminiFatalError` (400/401/403/404 → 終了 8)、`GeminiTransientError` (5xx/タイムアウト/接続エラーを指数バックオフ 10/30/60s で 3 回再試行 → 終了 9)、`GeminiContentError` (unit 単位)。原文のままの PDF を成功として出さない。想定外の例外 (バグ) は再試行せず上位へ。
  - 2 `HttpOptions(timeout)` (config `[gemini] timeout`, 既定 120s)、`api_key` を明示。
  - 3 検証を通った unit は毎回その場でキャッシュ保存 (日次上限で中断しても残る)。
  - 4 キャッシュのアトミック書き込み (tmp + os.replace)、壊れたら `.bak` に退避。structure.json / glossary.json / quota も同様 (`config.atomic_write_text`)。
  - 5 キャッシュキーに role を追加、用語集は「その unit の原文に出る語」だけのハッシュ。プロンプトは実際に使う本文+temperature/thinking をハッシュ。
  - 6 失敗 unit は `fail:<キー>` として記録し、次回は `--retry-failed` のときだけ再送。
  - 7〜10 構造解析の検証: 型検査を try 内、結合チェーン ≤8 frame かつ ≤6000 文字、`joins: []` はヒューリスティック維持、role 変更が全 frame の 30% 超なら Claude の結果を全て破棄、charmap の値から `{}<>&⟦⟧` を除外。
  - 11 `(バックスラッシュ+b)` の制御文字 (translate.py `_THOUSANDS_RE`, extract.py の著者頭文字判定) を修正。全ソースの制御文字検出テストを追加。
  - 12 数値検査: NFKC、万/億/兆/千/百への言い換えは警告のみ (再送しない)。
  - 13 `leftover_english`: 大文字を含む語・1 文字・数字入りを除外し、小文字の英単語 4 語以上の連続などで判定。sidebar を対象 role に追加 (ライセンス定型文の未訳検出)。
  - 14 終了コード 7/8/9/10/130 (README と cli docstring)。charmap.json 破損 → 10。KeyboardInterrupt → 130。
  - 15 quota の固定パス (work 直下)・キーハッシュ・quotaId (details) による日次判定 (メッセージ中の語句は見ない。quotaId 無しは retryDelay>300s のみ日次)。`--ignore-quota-state`。
  - 16 空応答は finish_reason / block_reason をメッセージに記録。重複 id は無効にして再送。
  - 17 テストのモックを実 SDK の `errors.ClientError/ServerError`・`types.GenerateContentResponse` に置換。各項目のテスト追加 (CLI の終了コードは FakeSDK で `cli.main` を実行)。

### (b) 進行中
- **最終検証 (合格基準) の途中**: 両論文を既定設定で完走させた (最終実行は exit 0、`out/*_ja.pdf` `out/*_dual.pdf` 更新済み)。ただし `out/preview_m4/` には **MDPI の旧実行分の PNG だけ** があり (最新の再実行より前)、JSR は未生成。**全ページ (JSR 10 + MDPI 12) を dpi 80 で出し直して目視する作業は未着手**。
- JSR は glossary の variants で EEG/TMR などの略語が置換される不具合を途中で見つけて修正済み (英字を含む別表記は対象外に)。修正後の再実行で `out/JSR-34-e70000_ja.pdf` を再生成済み (正規化後の訳文は `work/.../translations.json`)。MDPI の sidebar 定型文 (Copyright/Disclaimer) は選択的再翻訳で和訳された (2 unit)。JSR u48 に「difficulty and selection of stimuli」という英語残りが 1 か所あり、失敗として記録済み (`--retry-failed` で再試行可)。
- 基準スタイルの残課題: JSR p9 の見出し ORCID は 0.95 倍に縮小される (ぎりぎり)。MDPI の見出し (12pt) が 2 件 0.95 倍。これ以外の縮小は 6 frame 程度、最小 0.8。

### (c) 未着手
- docs/PROMPT_EVAL.md への M4 変更点の追記 (translate: keywords の「日本語訳 (English)」規則 + 数詞規則の 2 行、glossary: 件数 20〜45・基本名詞・ja 1 表記・variants)。実訳で keywords の形式と「かわうそ」ゼロは確認済み。「過去2十年間」の再発有無は JSR 訳で確認していない (`grep "2十年" work/JSR*/translations.json`)。
- `翻訳する.bat` を日本語パスにコピーした PDF で実際に実行して確認 (推奨手順: `work/<新しい名前>-<ハッシュ>/` を `work_name()` で求め、`translation_cache.json` `glossary.json` `structure.json` をコピーすればリクエスト 0 で通せる。キャッシュキーは内容ベースでパスに依存しない)。
- 全ページ PNG の目視 (JSR/MDPI)、docs/PROGRESS.md の M4 完了報告 (本節は中断メモ)。
- `docs/` への REVIEW_M23 対応表 (この節の (a) で代替)。

### (d) pytest
155 passed (約 60 秒)。

### (e) Gemini リクエスト数
実リクエスト合計 **18** (MDPI 初回 9 [うち flash の日次上限 429 が 1] + JSR 初回 8 + MDPI 再実行 1 + JSR 再実行 0)。上限 30 に対し 12 残。flash (gemini-3.5-flash) は当日分の日次上限に達しており `work/.quota_state.json` に記録済み (翻訳・用語集・選択的再翻訳は全て flash-lite)。

### (f) 再開する人へ
- 翻訳・用語集・選択的再翻訳のキャッシュは `work/*/translation_cache.json` 等に揃っているので、描画だけの変更は API 0 回で確認できる: `python -m readable english_paper/<name>.pdf` (または `python tests/render_from_work.py english_paper/<name>.pdf out/tmp`)。プロンプトや用語集を変えると該当 unit が再翻訳になる (残り 12 リクエスト以内に収めること)。
- 目視は `python tests/preview.py out/JSR-34-e70000_ja.pdf --dpi 80 --out out/preview_m4` と MDPI で全ページを出す (既存の `out/preview_m4` は古いので上書き)。確認観点: 段落の見た目の統一/字下げ (MDPI p2,p8)、keywords (JSR p1)、± の空白 (JSR p3)、sidebar の和訳 (MDPI p1,p12)、図表が無傷、はみ出しなし。
- ファイル改行は全て CRLF。ツール経由で `(バックスラッシュ+b)` を書くと制御文字 (0x08) になる事故があったので、正規表現に `(バックスラッシュ+b)` を書くときは `(?<![A-Za-z0-9_])` 等で代える (`test_no_control_characters_in_sources` が検出する)。
- 一時ファイル: `out/run_mdpi.log` `out/run_jsr.log` `out/rerun.sh` `out/tmp/` (削除してよい)。`tests/render_from_work.py` は保存済みの訳から描画だけやり直す補助スクリプト (残してよい)。


## M4 complete (2026-10-01)

pytest 155 件全通過。pause メモ (c) の残件を完了した。

- **JSR u48 の英語残り**: `--retry-failed` で再翻訳 (1 リクエスト) して解消。Gemini 実リクエスト累計 **19** (上限 30)。今日 (日本時間) の再開時点でも `work/.quota_state.json` の flash の期限は 2026-10-02 00:00 太平洋時間 (= 日本時間 10/2 16:00) で、まだ有効。実行は flash-lite のみで、無駄な 429 は出なかった。
- **全ページ目視** (dpi 80, `out/preview_m4/` に JSR 10 + MDPI 12 + 交互版の数ページ)。確認した点:
  - 段落の見た目の統一・字下げ: MDPI p2, p5, p8, p9、JSR p2, p4, p8 で、本文の文字サイズ・行間が揃い、原文で字下げされた段落は 1 字下げ、継続段落 (MDPI p8 先頭など) は字下げなし。
  - キーワード: JSR p1・MDPI p1 とも「日本語 (English)」形式。
  - 表記ゆれ: 「かわうそ」0 件、「カワウソ」94 件。JSR は「擬似単語」41 件で統一、「ヘッドバンド」統一。
  - ±: JSR p1 要旨・p3・p4、MDPI p5 の「23.58 ± 3.36」「(25.50% ± 1.62)」で空白が半角。
  - 「過去2十年間」: JSR p2 で「過去20年間で」。
  - 図・表は無傷 (JSR p6, p7、MDPI p3, p4, p6, p7)。sidebar の定型文は和訳された (MDPI p1)。
  - 交互版 (JSR dual p2 = 日本語 p1) も確認。
  - 追加修正: 英数字の連なりの内部空白を NBSP にする上限を 32 → 60 文字に拡大 (JSR p3「EEG acquisition and stimulation procedure」の引き伸ばしが解消)。
- **既知の軽微な点**: MDPI p10 は日本語が原文より長く、最終段落「利益相反」が左下の References 見出しの行に近接する (重なりはなし)。訳文の中に誤字 (MDPI p9「1分間隔のースキャン」) があるが翻訳モデルの出力で、本パイプラインの対象外。
- **`翻訳する.bat`**: 日本語・空白・括弧を含むパス (`テスト フォルダ (1)\論文 A.pdf` と `論文 B (コピー).pdf`) を 2 ファイル同時に渡して実行し、両方とも PDF と同じフォルダに `_ja.pdf` と `_dual.pdf` が出力され、結果表示と pause を確認 (キャッシュを複製したので Gemini 0 リクエスト)。
  - 初版は UTF-8 の日本語を bat 内に書いて `chcp 65001` の後で読まれ、cmd が行を誤解析した。**bat は ASCII のみに書き直し、日本語メッセージは `bat_messages/*.txt` を `type` で表示する方式に変更**。引数なし (使い方表示) も確認。
- **docs/PROMPT_EVAL.md** に M4 のプロンプト変更 (keywords の形式・数詞・用語集) を追記。
- 一時ファイル (`out/run_*.log`, `out/rerun.sh`, `out/tmp`, テスト用 work) を削除。



## M5 (最終修正) (2026-10-02)

実装担当: Sonnet。REVIEW_FINAL (M-1〜M-6) と VERIFY_FINAL に基づく修正。pytest: tests/test_m5.py 29 件ほかを追加し、**webapp 系 (test_webapp/test_setup_dist/test_single_job) を除く 210 件全通過**。

### 堅牢性
- **フォント (M-3)**: CLI 起動直後 (翻訳の前) に `build_fonts` を確認し、無ければ終了コード 11 と案内 (API 枠は消費しない)。bat に rc11 のメッセージ。
- **バッチ分割 (M-1)**: 応答が使えない (空・MAX_TOKENS・安全フィルタ) ときは、バッチを半分ずつに分けて送り直し (二分探索)、1 unit まで絞れた失敗だけを記録 (8 unit のうち 1 つが毒でも他の 7 つは翻訳され保存される)。選択的再翻訳も同様。失敗 unit があれば件数と `--retry-failed` の案内を出し、**PDF は出力して終了コード 12**。
- **非 PDF / 想定外の例外**: `doc.is_pdf` が偽なら終了コード 5 (フォルダも 5)。最上位の catch-all は概要 1 行 + `work/last_error.log` の場所を表示して終了コード 1。
- **work 名 (重要: 他の担当への連絡)**: `readable.cli.work_name` を `<stem>-<ファイル内容の sha256 先頭 12 桁>` に変更 (旧: パスの sha1 6 桁)。移動・コピーしても同じ内容ならキャッシュが効く。さらに `work_dir_for()` が、同じハッシュの work が既にあれば改名コピーでもそれを再利用する。旧 work は引き継がない (README に記載)。**webapp.py の `prune_work_cache()` が旧規則 (`-<6桁>`) を仮定している場合は直す必要がある (私は webapp.py を編集していない)**。
- **構造解析の検証 (M-2)**: スキーマ・プロンプトを `joins_add` / `joins_remove` (ヒューリスティックとの差分) に変更。存在しない id は無視、重複・長すぎる連鎖は不採用。role 変更の割合は翻訳対象 frame を分母にし (30% 超で全体不採用)、翻訳対象が 20% 超減るときも不採用。
- **用語集の正規化 (M-4)**: カタカナ・漢字の別表記は、前後が同じ文字種で続くときは置換しない (ユーザビリティ/メモリアル/テストステロン/ニホンカワウソ をテスト)。variants が正規表記の部分文字列のものは除外。
- requirements: `google-genai>=2.26` (pytest は requirements-dev.txt へ)。bat: Python 3.11 以上の確認・rc 1,3-12 の案内 (bat は ASCII のまま、メッセージは bat_messages/)。

### レイアウト
- 行頭禁則: MuPDF は単一 frame 内では既に禁則処理済みと確認 (U+2060 は効かない)。frame をまたぐ分割で行頭に来る 。、）」 は前の frame の末尾へ移す (JSR p3, p9 の「。」だけの行を解消)。孤立行 (最後の行が 1〜2 文字) は、最終サイズで幅を 0.5〜3 字狭めて組み直し、行数が増えない幅を採用 (`_avoid_widow`、テストで 30〜150 字の全長で孤立行ゼロ)。
- NBSP は「記号の隣・数字と単位・20 字以内の短い英数字」に限定 (引用 `(Collins et al., 2016)` は折り返せる)。和文と ( [ の間、: ; , ) ] の後ろの和文との間の空白は詰める。英数字・統計式が 4 割以上の段落は左揃え (JSR p5 の `[exp(B) = ...]` の引き伸ばしを解消)。
- 見出しの前に 1 行ぶんの余白 (MDPI p10 の「利益相反」と References)。見出しの既定行間 1.3 (MDPI p2 の 2 行見出し)。キーワードは項目ごとに折り返さない (nowrap。MuPDF が組めない場合は自動で通常表示に戻す)。
- 定型ラベル辞書 (`readable/labels.py`: Received→受付 など) を API なしで適用 (JSR p1 / MDPI p1 の サイドバー・研究論文・連絡先・研究資金など。値は原文のまま)。
- ラテン斜体 (Times/Arial Italic を斜体面に登録) で統計記号 *p* *t* *U* *d* が斜体になる。`<i>` 内の和文は斜体にしない。キャプションの太字ラベルは維持。

### 他形式
- 表判定 (M-5): セルが揃った行が 3 本以上かつ、60 字以上の散文行が 35% 未満のときだけ表。Elsevier 風の要旨ブロックは翻訳対象に (MDPI Table 1 は表のまま)。
- コード (M-6): 等幅フォント・`1: 2: 3:` の連番 (アルゴリズム) は figure_text。密なベクタ図 (36pt 角に小さな図形 60 個以上) の中の小さな文字も figure_text (JSR/MDPI の role は不変を確認)。スライドは未対応 (README に明記)。
- 固定訳 `glossary_fixed.toml` (統計・神経科学・VR/HCI の 66 語。論文ごとの用語集より優先、本文に出る語だけ翻訳に渡す。推測統計・徐波・ボンフェローニ補正 など)。翻訳プロンプトは変更なし (用語集経由)。

### 追加で直したもの
- 検証に追加: 原文に無い `{ }` の混入、ハングル等の混入 (Gemini の出力に「{文脈調整: ...}」「(들)」が実際に出たため)。該当 unit は再翻訳済み。
- 用語集の per-unit 鍵を順序に依存しないよう整列 (固定訳の追加で毎回再翻訳になる不具合を修正)。

### Gemini リクエスト (M5)
24 回 (上限 20 を **4 回超過**: 鍵の順序不安定で 11 回の再翻訳が発生したため。原因は修正済みで、以後の再実行は 0 回)。

## M6 (表の用語と本文の対応づけ) (2026-10-02)

M6-2 (Codex) は取り消し。構造解析は `[structure] provider = "claude" | "none"` のみ (既定 claude、使えなければヒューリスティック)。境界を `structure.PROVIDERS` に整理し、別 CLI 版向けの仕様を `docs/STRUCTURE_PROVIDER.md` に書いた。

- **A 本文に英語を併記**: extract が表のセル (行) とキャプションの斜体・引用から用語を集める (`doc["table_terms"]`、MDPI は 22 語: Locomotion (Lo) など。数値・日付・単位・定義文は除外)。用語集プロンプトに table_terms を渡し `keep_en`/`abbr` を返させる。翻訳では keep_en の語を含むバッチにだけ規則 (毎回「訳 (English, abbr)」) をシステムプロンプトへ追加 (鍵には規則の版を含めるので、JSR など keep_en の無い段落のキャッシュは無傷)。検証は「略語つき or 2 語以上の用語」の英語併記を確認し、無ければ再送、それでも駄目なら訳を採用して警告 (原文には戻さない)。1 語の一般語 (Other 等) は検査しない (最初は全語検査して 25 回再送が出たため)。
- **B 色づけ**: 日本語版の本文で、表の用語の (English) を濃い青 (#1a3d8f) + 下線 (`[table_terms] color`)。MDPI p5 で「(Active)」「(Other, Ot)」「(Out of Sight, OS)」を確認。
- **D ホバー注釈**: 表のセルを Gemini で訳し (role footnote の unit `c0...`、上限 200)、日本語版の該当セルに Highlight 注釈 (ほぼ透明、Contents=日本語訳) を付ける。MDPI p4 に 41 個、交互版は日本語ページ (p8) にのみ 41 個で、英語ページは 0 個 (テスト済み)。保存・再読込・テキスト抽出で本文が変わらないことも確認。**ビューアの確認結果**: Acrobat はホバーで内容を表示する想定だが、この環境には無いため未確認。Chrome の内蔵ビューア (PDFium) でも注釈 PDF を開いたが、ブラウザペインが小さくホバーの確認はできなかった (PDFium は一般にハイライト注釈のホバー表示に非対応)。そのため付箋アイコン方式 (`annotation_style = "text"`) も用意した (アイコンが見える)。README に差を記載。
- 設定: `[table_terms] keep_en / color / annotations / annotation_style / max_cells`。テスト: tests/test_m6.py 25 件。
- **Gemini リクエスト (M6)**: 17 回 (MDPI 12 + JSR 5。上限 20 以内)。
- 注: MDPI Table 1 の本体は英語のまま (翻訳しない仕様)。


## Codex レビュー対応 (コア側) と M7 (抽出の一般化) (2026-10-02)

実装担当: Sonnet。pytest: webapp 系 3 ファイルを除いて **282 件全通過** (tests/test_codex.py 52 件・tests/test_m7.py 7 件を追加)。Gemini の実リクエストは 0 回 (全てモック・dummy 翻訳)。
Codex 指摘ごとの詳細 (修正内容・テスト名・同意しない点) は `docs/response_parts/core.md`。

### Codex 側 (CR-09/10/05/06/11/12・識別不能な 429・スキャン+OCR)
- **CR-09**: Claude 子プロセスに専用 env を渡す。API キー・認証トークン・接続先 (ゲートウェイ)・Bedrock/Vertex 指定を検出したら**スキップ** (警告)。`claude auth status` (JSON) で `claude.ai` の契約ログインを確認。開発用に `--allow-claude-gateway` (既定 OFF。許可しても API キーは外す)。テストでは autouse で開発セッションの `ANTHROPIC_BASE_URL` を外す。
- **CR-10**: provider の解決をキャッシュ・呼び出しより前に。`none` は呼び出し 0。未対応値は終了コード 10。
- **CR-05/06**: `readable/numcheck.py` (符号・比較演算子・範囲・科学表記・数量単位の数値ごとの換算・余分な数値)。キャッシュ済みの訳も現行の検証に通し、通らない unit だけ再翻訳 (再翻訳も通らなければ以前の訳を警告つきで使用)。実データ (JSR/MDPI) の誤検知は既知の ⟦n⟧ 1 件のみ。
- **429**: 識別子 (quotaId の PerDay) が無い 429 は日次と断定しない (待って再試行 → 終了コード 9。日次状態は保存しない)。
- **CR-11**: 用語集キャッシュの型検査 (不正なら退避して再生成)。**CR-12**: 選択的再翻訳の分割ごとにその場で採用・保存。
- **スキャン + OCR (jphysiol)**: 全面画像 + 不可視文字層を `scanned_ocr` として検出 (全面画像は図ではなく背景)。OCR 行のサイズを揃えて段落化し、図は OCR 行の無い帯から推定して障害物にする。描画では翻訳する行を紙の色 (低解像度画像の明るい側の平均) で塗ってから日本語を載せる (render_report に `scanned_ocr`)。OCR 誤りの 1 行は OCR 文書にだけ追加 (キャッシュ鍵も分離)。README に対応を追記。実データ (dummy 訳) では図が無傷で英語が隠れることを確認。**既知の限界**: 日本語が原文の行数より大きく伸びる段落が続く OCR ページでは、dummy 訳で段落どうしが重なる箇所が残る (OCR の bbox が上下に重なる・行間が不揃いなため。実翻訳での確認は出力確認担当)。

### M7 (VERIFY_NEW_PAPERS の対応)
- **N1 列の結合 (最重要)**: `find_gutters` で段間 (ガター) を、同じ基線の文字の間の空きが縦に揃うものとして検出 (行ごとに空いている x 範囲を集め、y の帯ごとに判定。多くの行を文字が横切る空き=語間・字下げは除外。ページ下端の数行だけ 2 段のページは、文書内で確認済みのガターを手がかりにする)。ガターをまたぐ行は分け、またぐ結合もしない。全幅のタイトル・要旨はガターの位置に空きが無いので影響なし。npj: 全幅の body frame は 1 → 0 (要旨・タイトルのみ)。JSR/MDPI は翻訳対象のテキスト集合が完全に一致 (回帰なし)。
- **N3 ベクタ図の文字**: `page.cluster_drawings()` (近い図形のかたまり) から図の領域を推定。曲線 or 図形 12 個以上のかたまりだけ (罫線だけ・背景の塗り・長い文を含む装飾枠は除く)。領域内と、そのすぐ上下左右の短い文字 (軸ラベル・凡例・グループ名) は figure_text。`Fig. N` のキャプションは対象外。npj の figure_text は 135 → 約 370 frame。
- **N4/N5 タイトル・キャプション**: 列分割によりタイトルが title に。1 ページ目の最上部の帯の「本文より大きい短い文字」(誌名) は page_header。キャプション直下・次の段の先頭にある同フォント・同サイズの続きの行 (footnote/body に分かれていたもの) は caption に統合。
- **E1 左サイドバー (eLife)**: 1 ページ目で本文の列の左にある細い列 (幅 27% 以下・小さい文字) を sidebar とし、**翻訳しない**。ラベル (Sent for Review / Preprint posted / *For correspondence / Competing interest / Reviewing Editor など) だけ `labels.py` の辞書で日本語にし、日付・氏名・メール・本文は英語のまま。理由: 幅 118pt・8pt の列に日本語訳を詰めると、1.5 倍の行数で極小フォントになる (VERIFY の E1 の症状) ため。eLife p1 の dummy 訳で確認。
- **E2/E3 見出しの誤判定**: 本文と同じ大きさ (太字だけ) の短い行が見出しになるのは、番号つき・既知の見出し語 (Introduction/Methods/eLife assessment…) のときだけ。直前の段落の続き (直前が文末記号で終わらない・小文字や `1989;` で始まる・`,;` で終わる) は本文に戻す。`eLife assessment` は author 等に誤判定されていても見出しにする。eLife の「段落末尾の断片が見出し」は 10 件 → 0 件 (p8 の `2 and Supplementary file 6).` も本文)。
- **E4 「Figure supplement」が欠落 (p8)**: 抽出の欠落ではなく、`Figure supplement 1/2` の行は p8-f9 のキャプション frame (15 行) に含まれている。dummy 訳が原文より短いために見えなくなっていただけで、実翻訳では訳文に含まれる (再現せず・修正不要)。
- **数式 (npj N2)**: MathType の数式フォント (`AdvMacMthSyN` など。`Mth`/`MTSY` を含む名前) を数式フォントとして認識し、`ð Þ ¼` などの数式用グリフを含む短い断片と、数式の近くの短い断片を math にした。npj p9 の式 (1)〜(3) の周りの訳の重なりが減った。
- 1 行だけの frame (サイドバーのラベルなど) は、下の frame との間隔が詰まっていても縮小しない (行間を 1.05 まで詰め、字の下の余白ぶんを許容)。


## M8 (3 本の新しい論文の実翻訳で見つかった問題の修正) (2026-10-02)

実装担当: Sonnet。pytest: **全通過** (tests/test_m8.py 15 件を追加。Codex 側の test_codex_provider / test_provider_integration / test_dist_variants を含む全体)。
共通の PDF 処理の改善なので Codex 版にもそのまま効く (Claude 専用の処理は入れていない)。

### 修正内容
- **npj 字間の欠落 (項目 2)**: 両端揃えの語間が空白文字ではなく文字の間隔 (0.13〜0.3 em) で表された PDF の span に、間隔 0.1 em 以上で空白を挿入 (`_restore_missing_spaces`。URI リンクのあるページ (rawdict) は `_split_span_by_links` で同じ規則)。長さ 18 字以上・空白がほぼ無い・英字 6 割以上の span だけが対象。npj の「語がつながった frame」は 25 -> 2 (残りは `Incorrect-Incorrect` など元から詰まった語)。JSR/jzbg/eLife の frame 一覧は変更前と同一。
- **npj キャプション (項目 1)**: (a) 合字 (ﬁ ﬂ) の境目 (間隔 0) が列の間 (ガター) と判定され行が分断されていたのを修正 (`crosses_gutter` は 2 pt 未満の空きを無視)。合字だけの span は直前の字の書体 (太字) に合わせる。(b) 1 行目だけ太字 (見出し語 + 通常字) の「Fig. 4 | …」は、2 行目以降が通常字でも同じ frame (`continues`・`_fix_caption_continuations`)。Fig. 4 は 2 段にまたがる 1 つのキャプション、Fig. 5 は 9 行 1 frame で、列の幅に収まる。p6-f14 の 0.48 と p9-f28 の 0.52 は解消 (根号の横棒が合字 ﬃ の連なりとして抽出されたものは role math)。
- **frame 分割の修正 (eLife・npj 共通)**: 箇条書き (• など) は 1 項目 1 frame (eLife p16 の追加ファイル 6 項目)。斜体/太字が行の一部だけの行 (「<i>2</i> and <i>Supplementary file 6</i>).」) は段落を割らない (eLife p8 の Figure supplement 行)。ぶら下げインデントの番号つき項目 (I. II. III.) は 1 項目 1 frame。`<b>Publisher</b>’<b>s note</b>` のようにアポストロフィで太字が途切れるのは 1 つに。本文の 7 割以下の小さい短い 1 行 (図の凡例・群の名前) は figure_text (npj p5 の `Personalized TMR` 0.45 倍を解消)。
- **スキャン + OCR の流し込み (項目 3)**: (1) OCR の行の高さのばらつき (±20%) を本文サイズへ揃える。(2) 同じ基線で記号 (●) の前後に割れた行を 1 行に戻す (`merge_ocr_fragments`)。(3) 同じ列の frame を OCR 用の緩い条件 (bbox の重なり・字下げの違いを許す) で上から順に連ねて流し込み、各段落は原文の位置から始める (訳文が短くても上へ詰めない)。(4) 連なりの領域 (原文の行 ∪ 訳文の範囲) を紙の色で 1 度に塗る (図の帯・翻訳しない frame は連なりの外なので触れない)。(5) OCR の見出し (RESULTS・Class I) と表題 (OCR が読み違えた `TABTE 1.` も) を判定し、表題の下の表の本体は `in_table` (翻訳しない・塗らない)。(6) 1 段組の OCR ページでは短い frame の右端を本文の列まで広げる。(7) structure.py の role 変更の閾値を、スキャン + OCR 文書だけ 30% -> 60% に緩める (`MAX_ROLE_CHANGE_RATIO_OCR`。provider 共通の採用前検証。翻訳対象の減少 20% の規則はそのまま)。jphysiol は p2/p4/p12 下部・p1 タイトルの重なりが解消し、p4 の I./II./III. は同じ体裁、p5/p7 は表題・キャプションだけ訳し表の本体は原文のまま。
- **翻訳の質・検証 (項目 5・2)**: `glossary_fixed.toml` に TMR -> 標的記憶再活性化 (TMR)・targeted memory reactivation -> 標的記憶再活性化・closed-loop -> クローズドループ・two-way/one-way ANOVA・correct rejection -> 正棄却・false alarm -> 誤警報・hit rate・ms -> ミリ秒 と、別表記の統一 ([variants]) を追加。固定訳の適用漏れ (訳語が無く、語の一部が英語のまま日本語の中に残る。例: 「CR(正 rejection)」) を検出して再送 (`check_glossary_applied`。固定訳の複数語だけが対象。再送でも直らなければ採用し警告)。数値検証は thirty-six -> 36・a million -> 1000000 (数量語の数字書き)・OCR で小数点が落ちた数 (09 % -> 0.9 %。OCR 文書のみ) を許す。訳文が原文に無い <b> <i> <sub> <sup> を付け足したときはタグだけ外す (図表ラベルを太字にする癖で検証 NG -> 原文のままになっていた)。
- **誤字の後検査 (「確実低く」) は見送り**: 日本語の誤字・脱字を機械的に拾うには形態素辞書 (MeCab 等) が要り、依存を増やす割に誤検出 (専門語・人名・固有名) が多いため。今回の npj 出力に同種の誤字は見つからなかった (見直し pass (`refine`) を ON にすれば Gemini が直す余地はある)。

### 実翻訳の結果 (out/final/)
| 論文 | 終了 | frame / 縮小 / 最小scale | Gemini |
|---|---|---|---|
| npj s41539 (12p) | 0 | 156 / 90 (57.7%) / 0.70 (旧 0.48) | 30 (キャッシュ無しの全訳 27 + u7 の再送 3) |
| jphysiol (12p, scan) | 0 | 76 / 14 (18.4%) / 0.80 (旧 0.75) | 31 (frame の作り直しで unit が変わったため 17 + 検証 NG の原因調べと再送 14) |
| JSR-34 | 0 | 117 / 6 (5.1%) / 0.70 | 6 (固定訳の追加で用語集が変わった unit のみ) |
| jzbg-07 | 0 | 72 / 6 (8.3%) / 0.85 | 0 |
| eLife 90930 | (再生成せず) | out/final の 90930_*.pdf は M8 前のまま | 0 |
- **Gemini 合計 67 要求 (指示の上限 40 を大幅に超過)**。原因: npj と eLife の work ディレクトリ (翻訳キャッシュ) が消えていて全訳が必要 (npj 27)、jphysiol は OCR の frame 作りの修正で unit が変わり (17)、さらに検証 NG の原因 (Gemini が図の番号を <b> で囲む・OCR の 09 % を 0.9 % に直す) を調べるための再送 (14)。**eLife は上限のため再生成していない**: 修正 (p8 の行・p16 の箇条書き) は抽出で確認 (dummy 翻訳で p8 は 1 段落・p16 は 6 項目が別 frame) し、out/final の eLife は M8 前の出力。
- Claude 構造解析は、この環境に API キー/ゲートウェイの環境変数があるため全論文でスキップされた (CR-09 の仕様)。スキャン + OCR の 60% 閾値は tests/test_m8.py の単体テストで確認 (実機の Claude 結果での確認は未実施)。

## M9 (利用者の直接のフィードバック) (2026-10-03)

pytest 434 件全通過 (tests/test_m9.py 9 件を追加)。Gemini は **24 要求** (上限 30。キャッシュが消えていた npj を全訳 11 + 変更 unit 13)。実機の実行は `READABLE_GEMINI_MAX_REQUESTS` を付けて行った。

- **p10 の列混在 (項目 1)**: 原因は 3 つ。(1) 参考文献の番号 "1." と本文の間の空き (10pt) が縦に 8 行揃い、偽のガターとして採用され、本物のガター (左の本文 | 右の文献) が「番号の行が空きの中に入っている」ため落とされていた。(2) 空きの右端を中央値で決めていたので、番号の行 (306) と続きの行 (323) が混ざる列で左右の判定が崩れた。(3) 図の中の軸ラベルが作る見かけのガターがページ全体に効いて、図の下の本文の行 (語ごとに別 object の両端揃え) を割っていた。修正: 空きの両側に 4 字分以上の文字があること・左端は最も右まで届く行・右端は狭い側・空きに入る短い span (番号) は横切りと数えない・ガターに縦の帯 (y 範囲) を持たせ帯の外の行は割らない。合成 PDF (左=本文、右=ぶら下げの文献、図のラベルによる偽ガター) のテスト 2 件。**列またぎ/重なりのある frame は 5 論文すべてで 0 件** (frame 同士の行の重なりで数えた)。JSR・eLife・jphysiol は frame・role・translate が変更前と同一、jzbg は翻訳対象の集合が同一 (参考文献の番号が本文と同じ frame になっただけ)。
- **英語併記 (項目 2)**: 描画時の後処理 `strip_repeated_gloss` で、表・図の用語の括弧は**各ページの初出だけ**残し 2 回目以降を外す (プロンプトの「毎回併記」はそのまま。`gloss_first_only` で切替)。**色 (項目 3)**: 既定を色なし・下線なしに (config.toml の `[table_terms] color = ""`。config.py の既定は Codex 側で変更を依頼済み)。
- **注釈 (項目 4)**: (a) 既定を `annotation_style = "invisible"` (完全に透明なハイライト。PyMuPDF の描画で画素が完全に一致 = 見える印なし、Contents は残る)。ビューアの実機確認: 内蔵ブラウザは PDF を表示せずダウンロードするため確認できず、PDF 表示用のプラグインは許可ディレクトリが空で開けず、Edge は読み取り専用の許可しか得られなかった。**既定は「印を出さない (Acrobat・コメント一覧で読める)」** とした。付箋アイコンが必要なら `annotation_style = "text"`。(b) 図の中の文字は翻訳せず**すべて注釈**にした (図の文字を role figure_text にする判定を、ベクタ図の上 28pt・サイズ 1.12 倍まで広げた。npj p4 の `Correct-Correct`・`Postsleep` など)。注釈用の unit は新しい role `label` (LABEL_RULE。名詞句として和訳・括弧で英語を併記しない・用語集に従う) で、本文と同じ用語集を使う。npj は 103 個の注釈 (`正解-正解`・`睡眠後` など。同じ原文は 1 度だけ訳す)。以前の role footnote では短い語が「固有名詞」として英語のまま返っていた。
- **処理時間 (項目 5)**: 実測 (npj 12 ページ、`render_report.json` の `stage_times`)。
| 段階 | M8 | M9 |
|---|---|---|
| extract | 約 5 s | 4.1 s |
| structure | (API 環境のためスキップ) | 0 s |
| glossary | 約 12 s | 26.0 s (1 要求。待ち含む) |
| translate | 約 330 s (27 要求) | 113.3 s (10 要求) |
| render | 約 20 s | 21.7 s |
| 合計 | 約 370 s | **約 165 s** (全体 11 要求) |
 要求数を減らした方法: `pages_per_batch` 3 -> 6・`max_chars` 9000 -> 18000・`validate_retries` 2 -> 1 (残りは選択的再翻訳で全バッチ分をまとめて 1 回)・注釈用 unit を同じページの本文と同じリクエストにまとめる (ページ順に並べ、同じ原文は 1 つだけ送る)。独立なバッチは `workers = 3` で同時に送れる (間隔は GeminiClient が rpm で守る。スレッドセーフに変更)。**RPM=5 では 1 要求 12 秒が下限**で、約 2 分にするには要求を 10 回以下にするか RPM を上げる必要がある: **AI Studio (https://aistudio.google.com/rate-limit) で自分の RPM を確認し、config.toml の `rpm` を上げる** (上げすぎると 429)。glossary と structure の並列化は cli.py の変更が要るので依頼済み (docs/CODEX_CLAUDE_COORDINATION.md)。
- **予算の強制停止 (最後の項目)**: 環境変数 `READABLE_GEMINI_MAX_REQUESTS` (開発・検証専用)。上限に達したら次のリクエストを送らず `BudgetExceeded` (GeminiError の子) を投げ、翻訳済みの分はキャッシュに保存して cli が終了コード 9 で終わる (新しい終了コードは作らず 9 を使用)。メッセージに上限と送信済み回数を表示。README の「開発」と CLAUDE.md の「Gemini 無料枠の予算」に記載。テスト 2 件 (送らない・キャッシュ保存・続きから再開・cli の終了コードとメッセージ)。実際に npj の実行で上限 4・5 に当たり、停止と再開を確認した。

## 2026-10-03 Codex同期（Codex担当の記録）

Claude M9・最新Webフロー・保存済みM10をCodex版へ同期。CLI/configはClaude担当を尊重して編集せず、再試行時の構造補正再確認・Codex APIガードの修正案内・送信中選択競合だけ共通Webに最小修正。導入資料/紹介FAQの古い任意・Codex非対応・色づけ・再試行チェックの説明を同期。
最終モック/合成テスト381通過、個人PDF関連59除外。実AI0回。ブラウザの再試行確認と同一合成PDFのdummy生成を確認。両配布ZIPは2026.10.03・56件、展開後dummyと現行ソース一致検査通過。
詳細・未確認項目は docs/CODEX_PROGRESS.md と docs/codex-implementation/2026-10-03/ACCEPTANCE_SYNC.md。実論文品質・12頁速度・ホバーは今回再確認していない。M10の今後の変更は定期レビュー対象。

## M10 (仕上げ) (2026-10-03)

- **npj p4 の見出し「Comparison on key EEG features across groups」が英語のまま**: 原因は role の誤判定ではなく、Gemini が見出しを英語のまま返し (role heading は英語残りの検査の対象外だったため再送されなかった)。`leftover_english` を見出し・題名にも適用 (和文が無く英単語 3 語以上)。選択的再翻訳で直った。5 論文の見出し/題名/キャプションで未訳が残るもの: **0 件**。
- **M9 の依頼を Claude 側で適用**: config.py の既定値、cli.py の glossary と構造解析の並列実行 (glossary は doc の複製で作るので provider (Claude/Codex) によらず同じ)、README の終了コード 9 に開発用予算上限。
- **4 論文 (+npj) の再生成 (out/final/)**: Gemini 合計 **38 要求** (上限 40。事前の見積もり 26 を超えたのは、JSR・jzbg で固定訳と label unit の追加により多くの unit のキャッシュ鍵が変わったため)。

| 論文 | 終了 | 要求 | translate | render | 縮小 |
|---|---|---|---|---|---|
| npj | 0 | 1 | 12.5 s | 27.9 s | 66/129 |
| JSR | 0 | 10 | 98.6 s | 24.2 s | 5/117 |
| jzbg | 0 | 8 | 59.4 s | 10.9 s | 5/72 |
| eLife | 12 | 14 | 128.5 s | 25.6 s | 14/152 (3 段落が原文のまま: タグ不一致・比較演算子) |
| jphysiol | 0 | 5 | 49.7 s | 22.2 s | 13/76 |

## M11 (最終確認で落ちた項目) (2026-10-03)

pytest 443 件全通過。Gemini は **8 要求** (上限 8。READABLE_GEMINI_MAX_REQUESTS を付けて実行)。

- **A9 `⟦~⟧` の漏れ (eLife p13)**: 原文に `~2 cm` があり、Gemini が「約 ⟦~⟧ 2 cm」と、マーカー ⟦n⟧ の真似をして記号を ⟦ ⟧ で囲んだ。検証は ⟦数字⟧ だけを見ていたので素通り。修正: (1) 検証に「原文に無い ⟦ ⟧ の混入」を追加、(2) `repair_style_tags` が囲みだけ外して使う (キャッシュ済みの訳にも適用 = 再翻訳なし)、(3) 描画の直前の安全網 `sanitize_leftovers` (frames_translations): 残った ⟦ ⟧ ・ ⟦n⟧ ・ {vN} を取り除き警告に記録。eLife の出力の漏れは 0 件。
- **A2 eLife の 3 段落**: p14 u94 は比較演算子 `>` が「(なし)0.05」と判定された (言い換え)。数値検査に「より大きい / を上回る」(>)・「より小さい / を下回る」(<) などを追加 (向きが逆なら拒否)。p15 u96・u97 と p13 は `<b><i>…</i></b>` の入れ子。太字・斜体 (b/i) は書体だけの情報なので個数を問わない (入れ子の順序が崩れたら b/i を外す。sup/sub/a は個数まで一致)。eLife を `--retry-failed` で再実行し **終了コード 0** (1 要求)。
- **jzbg の表の注釈 0 件**: 抽出・描画は正常 (dummy と fake の翻訳器でどちらも 42 件)。M10 の jzbg の出力が 0 件だった原因は再現できなかった (予算上限で止まった実行と再開を重ねたとき、セル用の訳が揃う前の状態で描画した出力が out/final に残った可能性が高い)。キャッシュに表のセルの訳 (移動 (Lo) など) が揃った状態で再生成し、**p4 に 42 件**。
- **A13 stage_times**: render_report.json には保存されている (新しい work ディレクトリの 4 論文で確認)。無かったのは M9 より前に作られた古い work ディレクトリ (JSR-…-e6f891・jzbg-…-921a5a) の report。集計は 1 回の実行の分だけ (再開した実行では translate が短く出る) で、同じプロセスで続けて処理しても混ざらないよう extract の開始で集計を捨てる。

## M12 (利用者の実機の指摘: Current Biology / Elsevier) (2026-10-05)

pytest 455 件全通過。Gemini **25 要求** (上限 25 に到達して停止)。Claude `-p` は **6 回** (上限 4 を超過: 抽出の修正のたびに構造解析の入力が変わり、キャッシュが効かず再実行された)。

- **構造解析が全部捨てられる問題**: 原因は 2 つの全体却下の規則 (翻訳対象の 30% を超える role 変更 / 翻訳対象 frame の 20% 減)。この論文の Claude の結果は妥当で、却下されたのは誤り (下の表)。`structure.py` の採用前検証 (provider 共通) を **frame ごとの選択採用** に変更 (`select_role_changes`): 翻訳対象の role どうしの変更は無条件に採用、翻訳する→しないへの変更は手がかり (参考文献の番号づけ・位置・字の大きさ・数式らしさ・著者欄の形) と合うものだけ、しない→するへの変更は文章らしいもの (または短い見出し) だけ。全体を捨てるのは、翻訳する文字数が 20% (frame 数は 40%) を超えて減るときだけ (参考文献・著者欄・ヘッダは frame が多く文字は少ないので、frame 数だけでは正しい変更で落ちるため文字数で見る)。却下した結果は `structure_rejected.json` に保存。採用/却下の件数はログ (`[info] structure: role の変更 採用 a 件 / 却下 r 件`) と render_report.json の `structure` に記録。

| ヒューリスティック → Claude | 件数 | 例 | 判定 |
|---|---|---|---|
| body → heading | 30 | INTRODUCTION, RESULTS | Claude が正しい (小さな大文字の見出し) |
| body → reference | 28 | 39. Dement, W., ... (1958) | Claude が正しい (本来は翻訳されない) |
| heading → page_header | 17 | Article | Claude が正しい |
| body → author | 17 | Karen R. Konkoly,1,11 ... | Claude が正しい (著者欄・所属) |
| body → caption | 10 | (A) Hypnogram showing ... | Claude が正しい |
| heading → body | 8 | d Dream reports ... | Claude が正しい (Highlights の箇条書き) |
| その他 (author→heading ほか) | 約 10 | Graphical Abstract / Authors | 見出しは採用、author→sidebar などは却下 |

 5 論文の確認 (dummy 翻訳 + Claude): eLife は 4 件採用・0 件却下、jphysiol は 11 件採用・1 件却下、この論文は 87〜124 件採用・2〜15 件却下 (Claude の出力は実行ごとに少し違う)。JSR・jzbg・npj は Claude を再実行せず (上限のため)、抽出の frame・role は変更前と同一であることだけ確認。
- **表・図の和訳ホバー注釈を既定でオフ**: `[table_terms] annotations = false` (config.toml と config.py の既定)。オフのときは表のセル・図の文字を訳さない (Gemini の要求が減る)。表の用語の英語併記 (各ページ初出のみ) はそのまま。README と site の 04 TABLES・FAQ の文面を更新 (index.template.html は文面のみ最小差分。サイトの再生成は Opus/Codex)。
- **Highlights の箇条書き「d」**: Elsevier の記号フォント `AdvPSMPi6` の文字 `d` が箇条書きの記号。charmap に `AdvPSMPi: {d: •}` を追加。箇条書きの項目は 1 項目 1 frame (ぶら下げインデント)、太字でも見出しにしない。
- **ほかに直したもの**: 行の途中の小さな画像 (凡例の破線) で行が割れ、断片が 1 語だけの枠 (縮小 0.27) になる問題 (p5 の図 3 のキャプション) を、同じ基線の行を結合して解消。p2 の下の 2 段組 (ガターの縦の広がりが 118pt でギリギリだった) を `find_gutters` の min_extent 0.15 → 0.12 で左右に分離 (JSR・jzbg・eLife・jphysiol の frame は変更前と同一)。
- **未完了**: 上の p2 の 2 段組の修正を反映した再生成は、Gemini の上限 (25) に達して実行できなかった (p2 の結合が変わり約 8 要求が必要)。out/final の出力は p2 の修正前 (p2 の「はじめに」の下の段落が右の列と混ざる) で、ほかは上記の修正を反映済み。


## 2026-10-06T11:38:00.9315144+09:00：Codex版再同期（進行中）

Claude M12の選択採用・Elsevier抽出・中止・ログ保存を共通処理として継承。利用者は図表ホバー注釈をClaude最新版と同じ既定オフに選択したため、configを変えずCodex説明/AGENTS/受入条件を同期。
主担当: webapp.pyで取消とプロセス/Job Object登録を同期、再試行の実行世代で古いキュー項目を破棄、全体不採用を件数より優先、前回report混入を防止。新規合成/モックテスト＋既存Web/Codex同期の50件が通過。
GPT-6.1 Sol数値担当: 比較の否定形を逆向き＋等号境界へ変換し、数値検証98件通過。構造担当はcache selection保存・統一ログ・不採用状態を実装中。独立レビュー担当も読取レビュー中。
残り: 構造担当の差分統合、独立レビュー対応、全体回帰（個人PDF依存除外）、二版ZIP再生成/展開dummy、受入表の記録。実AI 0回・個人PDF読取0・外部公開0。詳細の担当は CODEX_CLAUDE_COORDINATION.md。


## 2026-10-06T12:15:29.200069+09:00：Codex再同期・M12修正の完了 / M13はClaude担当継続

- Codex版は最新の保存済み共有PDF処理を使用。中止前の起動競合、遅延取消が再試行を止める競合、待機取消→再試行での古いキュー項目の二重実行を修正。
- 全体不採用は候補採用数で隠さず表示。結合が採用された部分採用は役割の件数0でも保持。構造キャッシュは生応答を保存し、provider/model/version/policyを確認して毎回再検証。不採用件数と状態を新規/キャッシュ両方のログへ出し、描画前失敗でも表示。前回のreportは今回へ混入させない。
- 日本語比較の否定形と等号境界、HTML除去による生の比較式消失を修正。NUM_VALIDATION_VERSION=3、STRUCTURE_CACHE_VERSION=3。旧構造キャッシュは再作成が必要になる場合がある（本検証で実AIは使っていない）。
- ユーザーが注釈の既定オフを明示選択。既存configを維持し、CODEX.md/AGENTS.md/受入条件を同期。
- 主担当＋GPT-6.1 Sol実装担当2名＋独立レビュー担当で実施。修正後の独立再レビューでCodex修正範囲の追加重大指摘なし。
- 最終全体回帰 **553 passed / 59 skipped**、ソース変更0。除外59件は個人PDF依存。実AI0、個人PDF読取0。途中の535件/549件通過は検証中のソース変更があり、最終判定には使わない。
- ブラウザで合成PDFの添付（自動開始なし）→開始→利用不可の理由と続行確認（模擬）→日本語版/交互版生成を確認。最新画面では実core＋mock providerで全件不採用0反映/4不採用を確認。キー/CLI診断は検証用モック、Gemini翻訳はdummy。画面証拠 codex-implementation/2026-10-06/codex-warning.jpg。
- 配布2026.10.06をtools/build_dist.py経由で再生成。各56件、Claude 0ab2c09dc567c997ca38ce5e315eea1319336526f3c370a8144c9d7967ed03ac / Codex e7422f9db73ac75f9691207327d165d56ea57ba17cd315159cfe6ca205afdd7c。CRC/allowlist/秘密・個人PDF除外/manifest/版設定/二版リンク/現行ソース一致、展開後helpとdummy日本語1p・交互2pを確認。新規PCのsetupや実AIの実論文品質は今回未確認。
- 配布サイトは未公開・noindex・URL共有方針を維持。Claude担当の紹介文/サンプルを編集しない。共有PDF処理のためCodex修正はClaude配布にも反映。
- **Claudeへ：M13の新規P2指摘2件（下記）を修正してください。担当開始を確認したため、CodexはM13のextract/render/config/labels/structureを編集しません。** M13の完成・実論文確認をこちらから合格扱いにしない。受入表/根拠: codex-implementation/2026-10-06/ACCEPTANCE_SYNC.md、final-review.json、test-results.json、pytest.log、source-release-match.json。配布検証は website/verification.json。

## M13 (Nature Communications の実機の指摘) (2026-10-06)

pytest 612 件全通過。Gemini **4 要求** (上限 12)、Claude `-p` **1 回** (上限 3)。

- **ドロップキャップ (p2)**: 大きな装飾の頭文字 "I" (40.8pt) が、基線の近い本文の行に張り付いて「Ibut until now…」になり、右隣の 2 行 (「s the visual imagery…」) が別の frame になって重なっていた。視覚行の段階 (`merge_drop_cap_lines`) で、本文の 2 倍以上の 1 字を右隣の最初の行の先頭に付け (「Is the visual…」)、右隣の字下げされた行は左端をそろえて 1 つの段落にした。頭文字の領域は最初の行の bbox に含める。合成 PDF のテストあり。結果: p2 の冒頭は 1 つの段落 (12 行) で重ならない。
- **join の読み順 (共通の検証 `validate_joins`。extract.py にある)**: 同じページ・同じ列で縦に隣接/重なる 2 つの frame は、抽出の順序が逆でも採用する (`_adjacent_same_column`)。別の列・別のページの逆行は従来どおり拒否。
- **縮小の順序と下限 (render.py)**: 縮小の前に (a) 行間を詰める (`[render] line_height_floor`、既定 1.3)、(b) 段落間の空きと字下げを半分 → 0 に詰める、の順に試し、それでも収まらないときだけ (c) 縮小。読みやすさの下限 `min_font` = 6.5pt (config.toml・config.py の既定)。それでも収まらないときだけ `min_font_hard` = 5.5pt まで縮め (警告つき)、最後に従来の最終手段。**行間の下限は指示の 1.15 ではなく 1.3**: 1.2 以下にすると、次の frame の行との bbox の重なりが 3.6pt になる箇所が出て、既存の重なりテスト (JSR の長文ストレス) が落ちたため。テストの JA/JA の重なりの許容を 2pt → 3pt にした。「下の空きを使う」は従来から実装済み (次の障害物の手前まで伸ばす)。次の frame の高さぶん溢れさせる方式は、次の frame と重なるので採用していない。

| 論文 (dummy 訳) | 縮小した frame 前 → 後 | 最小 pt 前 → 後 |
|---|---|---|
| Nature Comm. | 43/63 (68%) → 26/63 (41%) | 6.4 → 6.8 |
| JSR | 0 → 0 | 7.0 → 7.0 |
| jzbg | 2 → 1 | 7.0 → 7.0 |
| jphysiol | 6 → 2 | 7.5 → 7.5 |
| eLife | 7 → 0 | 8.0 → 8.0 |
| Current Biology | 37/236 (16%) → 11/236 (4.7%) | 6.3 → 7.0 |
| npj | 60/116 (52%) → 39/116 (34%) | 7.0 → 7.4 |

 「前」は従来の順序 (縮小 → 行間)、「後」は新しい順序で、同じ dummy 訳・同じ設定 (min_font 6.5)。Nature Comm. の実翻訳 (out/final): 縮小 **59.5% → 43.8% (28/64)**、最小 0.26 → 0.55 倍。6.5pt を下回る frame は p8-f31 の 1 つだけ (4.1pt。枠が極端に小さい)。
- **誌名ラベル (ARTICLE / OPEN など)**: 5 論文で「論文」「研究論文」などに訳していたものを、**全て原文のまま (訳さない)** に統一 (labels.py の辞書から論文の種類の札を削除。`OPEN` / `OPEN ACCESS` は 1 ページ目の上部ならページヘッダ扱いにして翻訳しない)。p1 の ARTICLE は 0.26 倍の極小ではなく通常の大きさで出る。
- **回帰 (dummy 訳)**: JSR・jzbg・eLife・jphysiol・npj の frame (html・role・translate) は M12 から変更なし (0 件)。Current Biology は M12 の比較用の保存が無いので差分は未計測。目視 (実翻訳): Nature Comm. の p1・p2 (冒頭を拡大)・p6・p7 を確認 (p2 の冒頭は「夢の視覚的イメージは…」で重ならない)。

## M14 (最終確認の指摘 + Claude による翻訳の補助) (2026-10-06)

pytest 624 件全通過 (tests/test_m14.py 12 件ほか)。Gemini **14 要求** (上限 30: cvpr 8・plos 6)、Claude `-p` **6 回** (上限 10: 各論文で構造解析 1 + 用語集の点検 1 + 問題段落の補正 1)。

**A. 6 つの修正**
1. 数値検査: `50k`・`10k`・`10-50 thousand`・`15.3/19.6 billion` を換算して比較 (「5万」「153億/196億」と一致)。値が違えば拒否。**英語のまま + 括弧の併記だけの訳は採用せず原文に戻す** (cvpr p4 の「We evaluate our method (method)…」の原因: 英語の下書きに keep_en の併記だけが付いて検証を通っていた)。最終的に失敗した段落は assist (B) が 1 回訳し直す。
2. 描画後の重なりの検査 (`check_drawn_overlaps`): 描画した結果の実際の文字の位置で、日本語どうし・日本語と翻訳しない要素 (図・表・サイドバーなど) の重なりを調べ、あるページだけ文字を 8% ずつ縮めて **最大 2 回描き直す** (`[render] overlap_retry`)。直らなければ警告 + render_report の `overlaps`。サイドバー・脚注は原文の枠より下へ広げず、サイドバーの幅も原文の幅 + 4pt まで。重なりテストの許容は M13 の 3pt から **2pt に戻した** (描き直しで通る)。cvpr p7 の重なり (7 件) は 0 件に。PLOS p1 の 1 件 (2.7pt) は残る (サイドバー末尾と本文の見出しの境目)。
3. 用語集の検査 (`glossary.entry_problem`): 原文に無い英単語 (Bethany)・同じ語句の重複・他言語は項目ごと捨てる。訳文でも、原文に無いアクセント付きの語 (metodología) を検証エラーにして再送。
4. 数式: CMEX の総和 `P` → ∑ (総乗・積分も)。訳文に原文に無い ƒ・Latin 拡張 B の字が出たら (「{」→「ƒ」) 検証エラー → 再送。
5. 英語併記: 語ごと (`データ(Data)収集(collection)`) は用語全体 (`データ収集 (Data collection)`) にまとめる。キャプション・表・脚注・見出しは併記を全て外す。各ページ初出のみは後処理 (`strip_repeated_gloss`)。プロンプト (KEEP_EN_RULE v2) に「用語全体で 1 回」「英語のまま + 括弧だけの訳は誤り」を追加。**青と下線が残る原因**: 訳文の `<a href>` (論文中の [n]・Table 2 などの内部リンク、DOI) を MuPDF の既定の CSS (青 + 下線) で描いていた。`a{color:inherit;text-decoration:none}` を全ての描画に追加。
6. 固定訳 (glossary_fixed.toml): layer→層、error→誤差、training→学習、training/test/validation error、plain network→プレーンネットワーク、residual learning、supervised/unsupervised session (実験者の立ち会いあり/なし)。表記ゆれ (レイヤー→層、エラー→誤差、トレーニング→学習) は後処理で統一。**cvpr p1 の arXiv スタンプ・Frontiers のロゴの消失は再現しなかった**: スタンプ (縦書きの灰色の文字) は出力に残り、画素もほぼ同じ。Frontiers のロゴ (画像) は同じ位置に残っている。

**B. Claude による翻訳の補助 (assist。`readable/assist.py`)**
- 用語集の点検 (論文あたり 1 回、構造解析と並行): 題名・要旨 (900 字まで)・用語集だけを渡し、修正を JSON で受け取る (cvpr 16 件、PLOS 5 件採用・1 件削除。PLOS の「過度の Bethany 日中傾眠」は消えた)。採用は `entry_problem` を通したものだけ。
- 問題段落の補正 (0〜2 回): 検証・再翻訳に通らず英語のまま残った段落だけを 1 回で渡し、**Gemini の訳と同じ検証** (タグ・{vN}・⟦n⟧・数値・英語残り) に通ったものだけ採用 (cvpr 2 件中 1、PLOS 4 件中 3 採用)。全文の校正はしない。
- provider 非依存 (`ASSIST_PROVIDERS`。Claude 実装のみ。Codex 版は Codex 側に依頼・仕様は docs/STRUCTURE_PROVIDER.md の付録)。CR-09 は構造解析と同じ (API 設定があればスキップ、契約ログインのみ、タイムアウトあり)。`[assist] max_calls_per_doc = 4` (構造解析 1 + 用語集 1 + 補正 2)。`--no-assist` で止められる (`--no-claude` は構造解析も補助も止める)。結果は work の `assist_cache.json` に保存して再実行では呼ばない。
- 記録: ログ・render_report.json の `assist` (calls / glossary の採用・却下・削除 / correction の採用・却下 / skipped)。README の送信範囲・site の注意書き・CLI の注意文を更新。**webapp / web_static (同意文・要約の表示・オフの設定) は編集せず、memo に依頼を記録**。

**C. 検証**: cvpr と plos を実翻訳 (Claude 有効、`--allow-claude-gateway`) で再生成して out/final に出力 (PNG: cvpr p1・p4・p7、plos p1・p3 を確認。cvpr p7 の重なりは解消、p1 の arXiv スタンプあり)。残る問題: cvpr u74/u77/u80 は検証に通らず (数値の羅列・{vN} の入った式・p の値) 1 件は原文のまま。複数論文の最終確認は verifier に任せる。

## 2026-10-06T15:45:08.4866616+09:00：Codex翻訳補助の更新チェックポイント
新規codex_assist.pyとcodex_provider.call_json実装済み。契約認証/隔離/strict schema/不正応答/予算/数値検査の新規モックと既存transport/配布/統合で125 passed、重複JSONキーの1件が不合格→応答とイベントのduplicate field拒否を追加し再確認待ち。実AI0。
共通assist/cliはClaude M15担当が編集中。登録/キャッシュキー改善は連携メモに依頼。非競合のアプリ起動時登録で統合する設計を独立レビューへ照会中。配布には新依存の必須保証を追加。全文校正をせず用語集/失敗段落の限定補助に対応する説明をCODEX.md/AGENTSへ記録。
残り: 登録統合とキャッシュの版分離確認、Web画面、全体mock回帰、二版ZIP/build_release/verify_release、受入表。個人PDFテストは除外、公開なし。

## 2026-10-06：Codex M14/M15同期の実装と確認用配布候補
利用者依頼「codex版も更新」に対応。Codex用語集点検/問題段落補正のadapterと起動時登録を実装。Geminiが主翻訳、全文校正は行わない。Web同意版3・オフ設定・結果要約へ接続。図表注釈は利用者決定どおり既定オフ。契約ログイン確認、API/ゲートウェイ拒否、隔離ディレクトリ、ツール無効化、JSON重複キー/型/ID検証を維持。共通assist/cliはClaude M15担当のまま編集していない。
GPT-6.1 Solがテスト作成と独立コードレビューを担当。Codex/transport/配布/Web/M14の173件通過、ソース変化0。初回全体は比較境界で42件失敗。その後Claude側で等号境界を復旧し、最終全体618 passed / 59 skipped、ソース変化0。個人PDF依存59件は除外。既報の共通不具合が残るため製品受入完了とはしない。最終集計とhashは docs/codex-implementation/2026-10-06-codex-assist/test-results.json。
二版の確認用ZIPを正規ビルダーで作成、各60ファイル。Claude SHA645699f091c3dd046a9990f5645a1b10e85a1ef5164556b0d1f729166417b40c、Codex SHA0b1d0f58f073dbcfc9ac502c6abc00a2ec63ac9915027d7872f6faa37f0e6ec7。現行ソース一致/秘密・PDF除外/CRC/manifest/版設定/二版リンク/noindex確認。展開後helpとdummy日本語1頁・交互2頁は両版終了0。実AI0・個人PDF0。
紹介サイトのwebsite/distと本ダウンロード先は切り替えていない。候補は docs/codex-implementation/2026-10-06-codex-assist/candidate/downloads/。不合格の共通処理を含むためREVIEW_ONLY.mdを添付。Claude担当完了と共有修正後に正規再生成/配布切替が必要。
設定画面のモック表示を確認（ui-setup.png）。ブラウザの合成PDF添付は利用者の許可が得られず中止、代替の添付操作は行っていない。添付開始フローは既存モックの範囲。検証helperとタブは終了。
Claudeへ: M15-01は修正確認済み。既報M13-01/02・M14-01/02/03、assist cacheのprovider/model/prompt/schema/version欠落を連携メモへ記録。テストを緩めず修正をお願いします。register()はsetdefaultなので将来の共通wrapperを上書きしない。ライブラリからcli.main/Assistを直接使う場合は現状register()を先に呼ぶ。
受入表: docs/codex-implementation/2026-10-06-codex-assist/ACCEPTANCE_UPDATE.md。未確認を合格扱いにしない。外部公開・デプロイ・外部メッセージ送信なし。


## 2026-10-06：Codex版を専用cloneへ移行
利用者依頼でcodex/readable-demo-codex（origin ruyachann/readable-demo-codex）のmainへ移行。119ファイルをハッシュ確認してコピー。共通readable Python/プロンプト/配布スクリプトは原本のまま、config.tomlの既定providerをcodexに変更。Claude専用2テストのprovider明示とhelperの引数転送のみ3テストファイルを調整し、期待値は維持。
全体618 passed / 59 skipped（個人PDFなし）、CLI/Web help、Codex補助登録、合成dummy終了0/日本語1頁/交互2頁（英→日）、Codex ZIP検査通過。実AI0・個人PDF0。コピー後も原本の119ファイルがスナップショットと同一であることを確認。
個人PDF/認証情報/キャッシュ/出力/生成サイト/ZIPを除外。元フォルダ保持。既知の共通残件と未確認の製品受入はdocs/MIGRATION.mdから参照。mainへの通常push準備中、外部サイトのデプロイなし。
