# 再開メモ (Opus 5.5, 2026-10-01 一時停止時点)

ユーザー指示により一時停止。再開時はこのファイル → docs/PROGRESS.md 末尾 → docs/PLAN.md の順に読む。

## 確定事項 (ユーザー合意済み)
- PDFを入れたらPDFが返る (手作業なし)。出力は `_ja.pdf` と `_dual.pdf` (英p1→日p1→英p2… 交互)
- 翻訳: Gemini 無料枠。`gemini-3.5-flash-lite` 単段 + プロンプト C3 (prompts/gemini_translate.md)。refine は既定OFF (検証失敗/英語残り unit だけ選択的に再翻訳)。用語集は flash (上限時は flash-lite → 用語集なし)
- 構成整理: Claude Sonnet を `claude -p` (サブスク認証, API不使用) で 1論文1回。失敗時はヒューリスティック
- 図はそのまま、キャプション・本文は日本語。表本体・所属・日付・参考文献は英語のまま (config で表は切替可)
- 文字サイズは原文通りでなくてよい
- UI: CLI + `翻訳する.bat` (D&D)
- クラウド移行はしない (ユーザー判断: 現状のまま手元で)
- 実装中の claude.ai 使用量は気にしない。本番は最小に

## 追加決定 (2026-10-01 夜)
- 表本体は英語のまま。表の用語と本文の対応は **A+B+D** (本文に「訳 (English)」を毎回残す / 色・下線で目印 / 表セルにホバーで和訳注釈)。図キャプション中の英語用語も同じ扱い → M6-1
- 配布形態: **ローカル Web アプリ** (標準ライブラリのみ・127.0.0.1・`翻訳アプリ.bat`)。ユーザー各自が自分の Gemini キー / Claude Code or Codex ログインを使う。初回セットアップ画面・setup.bat (venv)・配布 zip
- 構造解析は **Claude Code のみ** (本バージョンは Claude 利用者向け)。Codex 版はユーザーが後で Codex に別途依頼する → 境界仕様を docs/STRUCTURE_PROVIDER.md に残す。M6-2 (Codex provider) は取り消し。Web UI の Codex 表示も削除する
- Web ページは利用者個人ごと。サーバー構築・サーバー側保存は不要。翻訳は1件ずつで、新しい翻訳を始めたら前回の PDF は破棄する (翻訳キャッシュは日次上限からの再開用に直近3文書だけ保持)
- 外部サイト (ユーザーが Codex で構築中の配布サイト) とは「出来た PDF をサイトで送る」を推奨。自動アップロードは post_actions フックとして将来対応 (サイト仕様待ち)

## マイルストーン状況
| | 状態 |
|---|---|
| M1 抽出+描画 | 完了 (REVIEW_M1 / VERIFY_M1 → M1.5 で修正済み, pytest 73) |
| M2 Claude構造解析 | 完了 (docs/M2_DIFF.md) |
| M3 Gemini翻訳 + プロンプト探索 | 完了 (docs/PROMPT_EVAL.md, pytest 111)。両論文で実訳PDF生成済み (out/) |
| M4 仕上げ | **途中で停止**。進捗は docs/PROGRESS.md 末尾「M4 一時停止」を参照 |
| M2/M3 レビュー修正 | docs/REVIEW_M23.md の指摘 (重大3/中9) を Opus が判断し M4 担当に送付済み。着手状況は PROGRESS.md 参照 |

## M4 の作業項目 (Opus 指示)
1. role ごとに文書全体で基準サイズを統一 + 本文1字下げ + 段落間隔
2. Keywords の翻訳
3. 表記ゆれ (カワウソ/かわうそ) → 用語集ベースの正規化
4. 「23.58歳 ± 3.36歳」の広い空白
5. 日次上限状態の永続化 (QuotaState)
6. 数詞の誤訳 (過去2十年間)
7. 翻訳する.bat
8. README 最終化

## レビュー修正 (REVIEW_M23 に対する Opus 判断, 番号は M4 担当への指示と同じ)
1 APIエラー分類 (4xx→終了8 / 5xx・timeout→再試行3回→終了9、原文PDFを exit 0 で出さない) / 2 HttpOptions timeout / 3 部分成功の即時キャッシュ / 4 アトミック書き込み / 5 キーに role・用語集は unit 内出現分 / 6 失敗 unit は --retry-failed 時のみ再送 / 7-10 構造解析の型検査・join 上限 (8 frame / 6000字)・空 joins=維持・role 変更30%超で破棄 / 11 `\b` 制御文字バグ2箇所 / 12 全角数字・万億 / 13 英語残り誤検出 / 14 終了コード整理 (7/8/9/10) / 15 QuotaState の保存先・キー・quotaId / 16 finish_reason・重複id / 17 実SDKに合わせたモック

## 停止時点の状態 (M4 担当の報告より)
- M4 項目 1〜8 とレビュー修正 1〜17 は実装済み。pytest 155 件全通過。両論文とも既定設定で完走 (exit 0)
- 未了: 全22ページ PNG の再出力と目視 (out/preview_m4 は MDPI の旧分のみ)、bat の日本語パスでの実行確認、JSR の keywords と「過去2十年間」の確認、PROMPT_EVAL.md への M4 変更点の追記、PROGRESS.md の M4 完了報告
- JSR u48 に英語残り1件 (失敗記録済み → `--retry-failed` で再試行可)
- Gemini 使用: M4 で18回。flash は当日上限の記録が `<work>/.quota_state.json` にある
- 注意: ツール経由で正規表現に `\b` を書くと制御文字 0x08 になる事故あり → `(?<![A-Za-z0-9_])` 等で代用 (tests/test_m4.py に検出テストあり)
- 削除可: out/run_*.log, out/rerun.sh, out/tmp

## 残りの手順
1. M4 の未了分を完了 (実装担当 Sonnet)。描画確認はキャッシュで API 0 回
2. 最終コードレビュー (Sonnet) → 指摘修正
3. 成果物確認 (Sonnet): 両論文の全ページ・交互版・bat 実行
4. Opus が最終確認しユーザーへ報告

## 注意
- Gemini flash は 2026-10-01 にプロンプト探索で日次上限到達 (太平洋時間0時=日本時間16〜17時にリセット)
- Windows では PYTHONIOENCODING=utf-8 が必要
- 一時ファイルは scratchpad (セッション固有) にあり、再開後のセッションでは消えている可能性がある。review_m23/e1-e7.py 等の再現スクリプトは必要なら作り直す

## 2026-10-06：Codex専用cloneへの移行
Codex版の開発先はこのclone（共有readable_demo配下のcodex/readable-demo-codex）。config.tomlのproviderはcodex。docs/MIGRATION.mdとMIGRATION_MANIFEST.json/VALIDATION.jsonを最初に参照する。元フォルダは保持され、後続編集は自動反映されない。全体618 passed / 59 skipped、合成dummyとCodex ZIP通過。mainへのpushを確認して作業を引き継ぐ。
