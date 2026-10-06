# Codex版の進捗

## 2026-10-02 開始

- 現在のClaude版とレビュー回答、provider境界、Windows起動入口、旧サイト配布ビルダーを確認。
- Claude側で追加された `numcheck.py`、プロセスツリー停止、provider無効化、キャッシュ回復などを共通基盤として利用する。
- Codex CLI連携、版別配布、共有メモを並行実装する。計画と担当は `CODEX_PLAN.md`、連携入口は `CODEX_CLAUDE_COORDINATION.md`。
- 検証と実AI使用の有無は、各マイルストーンで追記する。

## 共通処理と配布連携

- `structure.py` にCodex providerの遅延登録を追加。構造キャッシュをprovider・モデル・処理バージョンで区別。
- CLIに `--structure-provider` と `--no-structure` を追加。旧 `--no-claude` は互換を維持。
- ローカルWeb画面は起動版に合わせてラベル・認証案内・送信先を表示。送信先が変わった場合は同意を確認し直す。
- Codex版の既定workを `work/codex/` に分け、Claude版と同時に起動しても書込みが衝突しない構成。
- Codex起動bat、最小設定、導入説明 `docs/CODEX.md` を追加。
- provider/キャッシュ/同意の新規テストとClaude側回帰テスト：**56 passed**。実AI呼出しなし。
- ローカル `codex-cli 0.152.0` のhelpと、ChatGPTログイン方式を確認。認証値やアカウント情報は記録していない。実モデルの構造補正は未検証。
- サイトにClaude版/Codex版の選択を追加中。旧ZIP実装を削除し、共通 `tools/build_dist.py` 呼出しへ変更。公開HTMLの切替は両ZIP作成・検証後。
- 定期レビューを1時間ごとのheartbeatとして登録（ID `readable-claude-codex`）。変更なしは通知しない。Claudeがサイト担当を開始したら対象ファイルの編集を避けてレビューへ移る。

現時点で全体ビルド・実ブラウザー・配布物の実行確認は継続中。完成扱いにはしていない。

## 2026-10-02 統合確認・Claudeへの引継ぎ

- 実CLIで `gpt-6.1-sol` を指定すると、ChatGPT認証では未対応というHTTP 400で拒否された。開発サブエージェントの指定モデルとは別に、アプリ既定はCLIで確認できた `gpt-5.6-sol` へ変更。明示モデルの失敗でAPI認証や別モデルへ自動変更しない。
- 架空2段落でCodex構造補正の実JSON応答を確認。本文相当の大見出しをtitleへ補正。最新のプロセスツリー管理を含む確認も成功。Gemini実呼出し0、利用者PDFの外部送信0。証拠は `codex-implementation/2026-10-02/codex-live-probe.json`、モデル拒否は `codex-model-rejection.json`。
- 独立レビュー：同時起動Cookieの衝突、設定保存競合、サイト導入手順・送信先説明を修正。追加のnpm子プロセス停止と不正イベント型の指摘も修正し、実際のPython子プロセス停止を確認。
- Codexアダプター担当の応答が止まったため、担当を終了して主担当が引き取った。配布担当の保存済みビルダーとテストは採用し、WindowsのZIP名正規化で元のバックスラッシュ名を見落とす検査を修正。
- Claudeの案1担当開始とM8完了を共有メモで確認。サイト文面とサンプルはClaude側へ引継ぎ、Codexは編集を止めてレビュー担当へ移行。Claudeの最新共通PDF修正を両版へ取り込んだ。
- 06:42の全回帰テストは390件成功、実行中のソース変更なし。M8と最後のCodex修正を含む全体確認を再実行中。
- ブラウザでCodex版ラベル・構造補正チェック・OpenAI送信説明を確認。ファイル選択操作は送信失敗したため、画面からのPDF投入は確認済みとはしない。隔離したダミーjobと配布ZIP展開後の実行で処理を確認する。
- 両版ZIPを再生成：Claude `a91cbfd3c027`、Codex `708166858d82`。各55件。最終の展開実行・manifest検証を継続中。サイトの外部公開は行っていない。

## 最終確認（2026-10-02）

- **409 passed**、実行中のソース変更なし。Claude M8を含む共通処理とCodex修正を確認。全体ログ・ソースハッシュは `codex-implementation/2026-10-02/test-results.json` と `pytest.log`。
- 両配布ZIPのCRC・SHA-256・収録manifest・秘密情報除外・既定provider・batメッセージ・リンクを検証。展開した両版のCLI/Web helpとdummy PDF生成を実行。日本語版1ページ、英日交互版2ページ。結果は `website/verification.json`。
- Codex版のダミーjob完了・PDFダウンロードをブラウザで確認。ダウンロードされた日本語PDFも開き、1ページとdummy印を確認。画面証拠は `codex-implementation/2026-10-02/codex-preview.jpg`。ブラウザのファイル選択からの投入は未確認のまま。
- 独立コードレビューの追加2指摘は修正後に再確認され、新しい重大指摘なし。独立担当の実行は承認制限で未実施。テスト・実CLI・画面・配布物の実行検証は主担当が行った。
- 実AIによる文書翻訳やCodexによる全文校正は今回検証していない。Claude側の実論文評価は `docs/PROGRESS.md` のM8を参照し、予算・未再生成箇所・OCR由来の誤字などの制約を引き継ぐ。
- 紹介サイトの最新文面・サンプル担当はClaude。Codexは引継ぎ済みでレビューを継続。外部公開なし。1時間ごとの確認は変更がない場合に通知しない。

## 2026-10-03：Claude最新仕様との同期を実施

- Claude M9・Web変更・M10の保存済みソースを読み直し、config.py/cli.py の担当を重ねずにCodex版を最新共通処理へ同期。
- 再試行時に構造補正を再確認。CLI復旧後は有効に戻し、使用不可なら理由・直し方・続行承認を再表示。APIはBoolean structure指定、省略は互換保持。
- Codex API設定検出時の案内をログイン一律から「該当環境変数を外して再起動」へ。名前だけ表示し値は非表示。
- 独立GPT-6.1 Solレビューの送信中の選択競合を修正。PDF選択/クリアを送信中に抑止し、送信開始時の入力を保持。修正後レビューに新規指摘なし。
- 初期検証は追加テストの明示work指定ミスで2件失敗し修正。最終は **381 passed / 59 skipped**、テスト中のソース変化なし。個人PDF関連59件はAGENTSに沿って除外。実AI0回、私有PDF読取なし。
- 実ブラウザ: 再試行→構造確認→取消で保持→承認→同じPDFでdummy終了0、両PDFリンク。CLI未導入/終了12は模擬。実翻訳や全文校正の検証とは区別。
- docs/CODEX.md・API.md・WEBAPP.md・INSTALL.md、README初回案内、紹介テンプレート/FAQの古い説明を同期。M9の初出のみ・色なし・透明注釈を両版へ引継ぎ。timing.pyを必須収録。
- tools/build_dist.pyから2026.10.03両ZIPを再生成。各56ファイル。Claude e02eba157f85 / Codex a617097e2119。CRC・ハッシュ・manifest・除外検査・展開後helpとdummy PDF生成・現行ソース一致を確認。website/verification.jsonを更新。
- 受入表: codex-implementation/2026-10-03/ACCEPTANCE_SYNC.md。B2/B3/C1/C3合格、実論文再評価・ホバー・ブラウザ添付開始全工程・新規PC setupなど未確認。外部公開なし。
- Claudeへの共有: CODEX_CLAUDE_COORDINATION.md。M10の保存済み並列化を収録したが、Claude側の完了メモは未追記。今後の更新は定期レビューで再確認。

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

