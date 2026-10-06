# PDF Translate 紹介・配布ページ

Windows PC向けのPDF翻訳ツールを紹介し、ソースコードZIPを配布する静的サイトです。表示名「PDF Translate」は仮称です。

## 構成

- dist/: 公開用HTML/CSS/JavaScript、架空のPDF描画例、配布ZIP。
- scripts/index.template.html: ページの編集元。生成済みindex.htmlへ直接変更せず、このテンプレートを編集します。
- scripts/build_release.py: `tools/build_dist.py` を使い、Claude版・Codex版のZIPと配布情報を生成。生成HTMLを最後に切り替えます。
- scripts/create_samples.py: 手動訳と既存のPDF抽出・描画処理でサンプルを生成。外部AIは呼びません。
- scripts/verify_release.py: ZIPのハッシュ、起動用batの参照、展開後コード、PDFの日本語とページ順を検査。
- scripts/preview_server.py: dist/だけをlocalhostで配信します。
- PLAN.md / PROGRESS.md: 計画・判断事項・確認結果。

## 再生成・確認

プロジェクトルートでPython 3.11以上を使います。PDFサンプルの生成と検査にはアプリ側のrequirements.txtの依存ライブラリが必要です。

```powershell
python website/scripts/create_samples.py
python website/scripts/build_release.py
python website/scripts/verify_release.py
python website/scripts/preview_server.py
```

プレビュー: http://127.0.0.1:4173/

通常の文章変更にはbuild_release.pyだけを再実行します。公開するのはdist/一式です。サイトにビルドツールやAPIキーは不要です。

## 配布ZIP

翻訳コード、ローカルWeb画面、翻訳プロンプト、設定・固定用語集、依存ライブラリ一覧、README、起動用bat、setup.batとメッセージ、導入ガイドを収録。ZIPの収録と秘密情報検査は `tools/build_dist.py` に集約。ZIP外の配布情報JSONに、各版と収録ファイルのSHA-256を記録します。アプリの変更と競合した場合は作成を中止します。

個人PDF、出力・キャッシュ、AI認証情報、ユーザーログ、.claude/・.codex/、フォントは同梱しません。利用者自身のGemini APIキーと、各版のClaude CodeまたはCodex CLI認証を使います。

PCで起動する操作画面を収録し、初回設定からPDFダウンロードまで行えます。実AI・配置の検証状況はアプリの進捗メモと配布情報で確認してください。紹介・配布サイト自体に翻訳機能やPDFアップロードはありません。サンプルは架空文書と手動訳による配置例です。

## 公開前の決定事項

ローカルでレビューできる状態です。外部公開は未実施です。名称、公開先・公開範囲、ソースコードのライセンスを所有者が確定します。依存ライブラリの再配布条件も確認します。
