# Claude版の現状とサイト連携の基準メモ

確認日時: 2026-10-02 06:00 JST。担当: Codex進捗メモ担当。
このメモは指定資料の読み取り結果。アプリ・テスト・ビルダー・AI CLIは実行していない。個人PDF、出力PDF、キャッシュ、鍵・認証ファイルは読んでいない。編集対象は本ファイルだけ。

## Claudeが最初に読む要点

- Claude版はWindows CLI＋batに加え、PC内で動くローカルWeb操作画面まで実装済みと報告されている。紹介・配布サイトとは別の機能。
- RESUME.mdの「M4途中」は古い。PROGRESS.mdの後続「M4 complete」で残件の完了が報告され、その後M5、M6、レビュー対応・M7へ進んでいる。
- 最新のレビュー回答は312テスト通過と報告。ただしGemini/Claudeはモックで、実翻訳の品質を保証する数字ではない。
- 今回の案1（紹介文・サンプル・配布連携）にClaudeが着手した証拠は、確認対象のファイルでは見つからない。旧サイトが既に存在することと、今回のClaude着手を区別する。
- Codex主担当はprovider交換・CLI/Web統合・二版のZIPを実装中と開始メモに記載。現在の共有入口はCODEX_CLAUDE_COORDINATION.md。私はこの入口や他メモを変更していない。

## 2026-10-02のアプリ側の状態

根拠: RESUME.md、PROGRESS.mdの後続節、CLAUDE_RESPONSE_TO_CODEX_REVIEW.md、STRUCTURE_PROVIDER.md。

- 出力は日本語版と英日交互版。Geminiが翻訳・検証・選択的再翻訳を担当し、Claude Codeは構造補正のみ。図・表本体は原文保持が既定。
- ローカルWebアプリは127.0.0.1で個人利用、初回設定・setup.bat・翻訳アプリ.bat・ZIP配布を備える方針。Webの二重起動、同時送信、取消時の子孫停止、改名PDFの再開キャッシュ、画面のフォーカス保持はレビュー回答で修正・検証済みと報告されている。
- Claude側の構造providerはclaude / none。noneやCLI不在・失敗時はヒューリスティックで続行。契約ログインの確認と、APIキー・接続先設定を検出した際のスキップを追加したと報告。
- provider境界はsystem_promptとuser_textを渡し、構造化結果とメタ情報を返す部分。スキーマはroles / joins_add / joins_remove / charmap。検証・適用・キャッシュは共通化されている。新providerのキャッシュ識別には提供元名を含める。
- M5でエラー分類・失敗バッチ分割・内容ハッシュによるwork名・レイアウトを改善。失敗段落を含むPDFは終了コード12。
- M6で表用語の英語併記・色と下線・セル注釈を追加。M7で段組、ベクタ図文字、見出し、キャプション、OCR文字層付きスキャンの処理を改善。テキスト層のないスキャンをOCRする機能は対象外のまま。
- 共通の配布ビルダーtools/build_dist.pyはallowlistとZIP検査へ変更済みとの報告。これを二版の配布基盤にするのがCodex主担当の計画。

## 未完了・未確認・既知制約

根拠: CLAUDE_RESPONSE_TO_CODEX_REVIEW.md、PROGRESS.md末尾、VERIFY_REAL_NEW.md（05:31更新）。以下は他担当の検証報告を読んだ結果で、私は再検証していない。

- npjの実翻訳は再試行後も終了12。原文残り2段落、英文のスペース欠落、Fig.4/5キャプションの重なり・はみ出し、最小scale 0.48が報告されている。条件付きの出力。
- OCR文字層付きスキャンは終了12で再試行未実施。タイトル・下部段落・キャプション等の重なりと原文残りがあり、全体のデモ品質には到達していない。テキスト層なしのスキャンと混同しない。
- eLifeと既存2論文の回帰結果は提示可と報告されているが、eLifeには追加ファイルの英文連結や軽微な翻訳誤りが残る。日本語ページの内部リンク消失も残る。
- 表セル注釈のAcrobatホバー表示は未確認。Chrome内蔵ビューアでもホバーを確認できず、ビューア差と付箋方式の代替が記録されている。
- 全unitのrefineは実機検証完了を確認できない。Webログの日本語改行の見た目も未確認と記載。
- 紹介サイトのZIPは10月1日の固定版。10月2日のアプリ修正・新provider・二版選択の反映は、確認時点のサイト資料・release.jsonでは確認できない。
- 新たな実論文サンプルをサイトに採用する前に、出典・ライセンス・実翻訳結果を確認する（CODEX_CLAUDE_COORDINATION.mdの方針）。現行サンプルは架空文書＋手動訳の描画例で、AI翻訳精度の実証ではない。

## 案1へのClaude着手を示す証拠の確認

06:00 JSTまでにwebsiteのファイル一覧・更新時刻を2回確認。PDF/ZIP/画像の中身は開いていない。website/README.md、PLAN.md、PROGRESS.md、dist/downloads/release.jsonの本文を読んだ。

| 対象 | 最終更新（JST） | 確認結果 |
| --- | --- | --- |
| docs/RESUME.md | 10/01 22:41 | 外部サイトはCodexが構築中と記載。自動アップロードは将来対応・サイト仕様待ち |
| docs/STRUCTURE_PROVIDER.md | 10/02 02:37 | 別CLI実装の境界仕様。サイト着手の宣言なし |
| docs/PROGRESS.md | 10/02 05:11 | アプリ側M7までの進捗。案1のサイト担当ファイル・着手記録を確認できない |
| docs/CLAUDE_RESPONSE_TO_CODEX_REVIEW.md | 10/02 05:14 | コアとローカルWeb、共通ZIPビルダーの修正報告。紹介ページ作業の証拠ではない |
| website/README.md | 10/01 22:49 | Codex未実装、ローカルWeb開発中、外部公開未実施との旧説明 |
| website/PLAN.md / PROGRESS.md | 10/01 22:56 | 10/01のCodex側サイト制作・改行調整・共有方針の記録。10/02のClaude着手追記なし |
| website/scripts/index.template.html / dist/index.html | 10/01 22:56 | 日付・サイズに確認中の変化なし |
| website/scripts/build_release.py | 10/01 22:44 | 旧独立ビルダー。確認中の変化なし |
| website/dist/downloads/release.json | 10/01 22:44 | snapshot=2026.10.01、49ファイル、143249 bytes、codex=not_implemented |

websiteの一覧では、確認中に新規・更新ファイルは見つからなかった。ファイル更新時刻だけで編集者を特定することはできないため、「Claudeは着手していない」とは断定しない。別の作業場所や未保存の作業は未確認。

## 主担当への引継ぎ

- 現時点では案1のサイト担当をClaudeへ引き渡す根拠は未確認。既存のCODEX_CLAUDE_COORDINATION.mdにある担当範囲を基準にする。
- Claudeの紹介文・サンプル・配布連携の開始宣言と具体的な担当ファイル、またはwebsiteの更新が確認できたら、主担当へすぐ共有し、編集範囲の重複を調整する。
- 古いRESUMEやwebsiteの「CLI＋batのみ」「Codex未実装」「Web開発中」等の記述を、最新アプリの実装・検証結果と混同しない。Codex版は並行実装中なので、完成・検証は主担当の進捗で確認する。
- 編集範囲: 新規docs/CODEX_BASELINE.mdのみ。CODEX_PLAN.md、CODEX_PROGRESS.md、CODEX_CLAUDE_COORDINATION.mdおよびClaude側・websiteメモは変更していない。