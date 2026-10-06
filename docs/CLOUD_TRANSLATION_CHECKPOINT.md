# Cloud実翻訳のチェックポイント（2026-10-06）

依頼: Webから形式の異なる英語論文を探索し、クラウドで実Gemini＋実Codex CLIによる翻訳と全ページPDF確認を行う。
対象repo: ruyachann/readable-demo-codex、移行main 070e1cc。

現状:
- Codex Cloud用の直接実行/環境管理toolはこのローカルチャットにはないため、ブラウザで公式cloud画面を開いた。
- 利用者のログイン後、既存GitHub接続から対象repoを選択し、Cloud環境セットアップを開始した。
- セットアップチャット: 01a1103f-d56c-77a7-b26b-87de6a1e80ad、host durable。Linux /workspace/readable-demo-codex の読取が始まった。
- Cloudには環境準備のみ、実モデル要求は送信しない、秘密値は表示/転送しないという条件を送った。認証・秘密設定の結果待ち。
- Linuxフォント設定、キーを出力しない読取preflight、候補論文と検証指示を準備済み。preflight単体テスト4 passed（モック）。

予算案: 3論文（15+12+16頁）の初期見積もりGemini20要求、試験1論文の上限12要求。3論文の累計上限は30要求、Codexの実exec累計上限12回（再試行も含む）。実行前に抽出した文字量・キャッシュ・用語集バッチを数えて再見積もり。上限は正の整数のみ。
実績: 実Gemini0、実Codex0。論文PDFはGitへ収録しない。
次: Cloud準備結果を確認し、必要なGemini秘密設定・契約CLI認証を利用者へ引き継ぐ。preflightが通ってから1論文の実翻訳へ進む。ローカル実翻訳へ勝手に切り替えない。
