# Codex版のリポジトリ移行（2026-10-06）

利用者の依頼により、共有readable_demoフォルダの保存済みソースを、このcloneへ移行する。
リモート: https://github.com/ruyachann/readable-demo-codex.git
ブランチ: main。移行前のリモートにはコミットなし。

## 移行範囲

- 共通PDF処理とCodex provider/assist、ローカルWeb UI、プロンプト、設定、固定用語集、起動bat、依存ライブラリ一覧。
- 合成/モックテストと配布・サイト生成スクリプト。個人PDFを必要とする旧テストは対象PDFを含めず、存在しない場合はスキップする。
- Codex/共通の利用・開発・連携文書、受入条件、関連ローカルskills。過去の文書は当時の記録として保持する。
- 119ファイルをハッシュ確認してコピー。config.tomlのstructure.providerだけをcodexへ変更。共有Pythonコードは原本と同一。
- cloneのREADME/AGENTSと本移行記録を追加・調整。.gitignoreで個人PDF・認証情報・作業結果・キャッシュを除外する。
- Claude専用の2テストはClaudeを明示し、共通テストhelperがprovider指定を受け取るよう3テストファイルを最小変更。検査の期待値は変更しない。

個人PDF、english_paper、tests/evalの実論文データ、work/out、フォント、認証ファイル、レビューの画像/生ログ、生成済みZIP/サイトは移行しない。元の共有フォルダは保持する。サイトの外部デプロイは行わない。

`MIGRATION_MANIFEST.json` はコピー時点の原本ハッシュとcloneハッシュを記録する。README/AGENTS/進捗などの移行後の文書調整はGitの履歴で管理する。

## 検証

全体pytestは **618 passed / 59 skipped**（74.39秒）。59件は個人PDFがないためスキップ。CLI/Web help、Codex既定設定・assist登録、合成PDFのdummy日本語1頁/交互2頁（英→日）、Codex ZIP生成・整合性検査が通過。共有Pythonコードとプロンプトのハッシュはコピー時点の原本と一致。実Gemini/Codex要求・個人PDF利用は0回。根拠は `MIGRATION_VALIDATION.json`。mainのpush状況はdocs/PROGRESS.mdに記録する。

## 引き継いだ既知の残件

この依頼は移行であり、実論文での製品受入を再判定したものではない。
コピー時点のM13/M14/M15の残件（読み順、図ラベル判定、数値換算、英語併記、補助キャッシュなど）は `codex-implementation/2026-10-06-codex-assist/ACCEPTANCE_UPDATE.md` と `CODEX_CLAUDE_COORDINATION.md` を参照する。元フォルダの後続編集は自動反映されない。
