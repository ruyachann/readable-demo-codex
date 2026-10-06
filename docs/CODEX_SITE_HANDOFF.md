# 紹介・配布ページ：Claudeへの引継ぎ

## 2026-10-02の担当

Claudeの案1への着手は、この作業開始時点の共有メモでは確認できませんでした。そのためCodexが、共通配布ビルダーへの統一、Claude版・Codex版のダウンロード選択、操作画面・OCR文字層・表の原語対応・終了コードの説明更新を担当しました。別の場所でClaudeが作業している可能性は否定できません。

Claudeの07:40頃の着手・サイト文面更新完了を13時台に共有メモで確認しました。該当範囲は引継ぎ済みです。Codexは文面・サンプルを編集せずレビューを行います。共通ビルダー連携と検証はCodex担当を継続し、ClaudeのM8改善も含む両ZIPを再生成・検証しました。現在の検証状況は `docs/CODEX_PROGRESS.md` を確認してください。

## 更新する入口

- ZIPを作る唯一の入口：`tools/build_dist.py`。サイトはこの関数を呼びます。
- ページ編集：`website/scripts/index.template.html`、`website/dist/styles.css`。
- 配布物とHTMLの再生成：`python website/scripts/build_release.py`。
- 展開した両版の確認：`python website/scripts/verify_release.py`。
- `website/dist/index.html` の直接編集は次回生成で上書きされます。
- `release.json` は両版の既定provider・ファイル一覧・SHA-256を保持します。秘密情報・PDF・認証キャッシュの除外は共通ビルダーで検査します。

## 表現と公開範囲

PDFの共通処理は両版で同じです。Codex/Claudeの担当は構造補正です。Web画面では必須で、使えない場合は理由と直し方を示し、承認後だけ簡易解析で続けます。全文の日本語校正を実装済みとは説明しません。画像だけのPDFに新しいOCRをかける機能もありません。

現状のサンプルは架空文書の手動翻訳です。実AIによる翻訳例とは表示しません。実論文への差替えは、再利用条件・出典・実翻訳・配置確認がそろってから行ってください。

外部公開は未実施です。利用者指定はURLを知る人への共有です。検索除外だけでは閲覧権限を制限できないため、公開先でも指定範囲を満たす設定が必要です。

## 定期確認

1時間ごとのレビューを登録済みです。変更のない場合は通知せず、新しい問題・完了・引継ぎなどがあれば `docs/CODEX_REVIEW_MONITOR.md` に記録します。自動実AI呼出し・公開・Claudeへのメッセージ送信は行いません。

## 2026-10-03 Codex同期の引継ぎ

最新Claude保存済みM9/M10と操作画面を両版の配布へ反映済み。紹介文・FAQの古い色づけ/任意/失敗段落チェックのみ最小同期。サンプルは架空・手動訳を維持。2026.10.03両ZIP各56件、Claude e02eba157f85 / Codex a617097e2119。現行ソース一致、展開後help/dummy、リンク・秘密情報除外検査OK。外部公開なし。
構造補正なしで始めたジョブでも再試行時に確認し直す。復旧時は有効に戻る。API設定ガードの直し方を修正。画面/実翻訳の合格範囲と未確認は codex-implementation/2026-10-03/ACCEPTANCE_SYNC.md に記録。M10はClaude担当のまま、今後の更新をレビューする。


## 2026-10-06T12:15:29.200069+09:00：2026.10.06配布更新

Claude版/Codex版の選択を維持し、正規ビルダーから最新保存ソースの両ZIPを生成。各56件、現行ソース一致・展開dummy・リンク・除外検査を確認。サイト紹介文/サンプルはClaude担当のまま編集なし。Codex説明は注釈既定オフへ同期（利用者承認）。両版にM12レビュー修正を反映。M13はClaude担当進行中、追加2指摘はCODEX_REVIEW_MONITOR.md。未公開・noindexを維持。

## 2026-10-06：Codex M14/M15同期の実装と確認用配布候補
利用者依頼「codex版も更新」に対応。Codex用語集点検/問題段落補正のadapterと起動時登録を実装。Geminiが主翻訳、全文校正は行わない。Web同意版3・オフ設定・結果要約へ接続。図表注釈は利用者決定どおり既定オフ。契約ログイン確認、API/ゲートウェイ拒否、隔離ディレクトリ、ツール無効化、JSON重複キー/型/ID検証を維持。共通assist/cliはClaude M15担当のまま編集していない。
GPT-6.1 Solがテスト作成と独立コードレビューを担当。Codex/transport/配布/Web/M14の173件通過、ソース変化0。初回全体は比較境界で42件失敗。その後Claude側で等号境界を復旧し、最終全体618 passed / 59 skipped、ソース変化0。個人PDF依存59件は除外。既報の共通不具合が残るため製品受入完了とはしない。最終集計とhashは docs/codex-implementation/2026-10-06-codex-assist/test-results.json。
二版の確認用ZIPを正規ビルダーで作成、各60ファイル。Claude SHA645699f091c3dd046a9990f5645a1b10e85a1ef5164556b0d1f729166417b40c、Codex SHA0b1d0f58f073dbcfc9ac502c6abc00a2ec63ac9915027d7872f6faa37f0e6ec7。現行ソース一致/秘密・PDF除外/CRC/manifest/版設定/二版リンク/noindex確認。展開後helpとdummy日本語1頁・交互2頁は両版終了0。実AI0・個人PDF0。
紹介サイトのwebsite/distと本ダウンロード先は切り替えていない。候補は docs/codex-implementation/2026-10-06-codex-assist/candidate/downloads/。不合格の共通処理を含むためREVIEW_ONLY.mdを添付。Claude担当完了と共有修正後に正規再生成/配布切替が必要。
設定画面のモック表示を確認（ui-setup.png）。ブラウザの合成PDF添付は利用者の許可が得られず中止、代替の添付操作は行っていない。添付開始フローは既存モックの範囲。検証helperとタブは終了。
Claudeへ: M15-01は修正確認済み。既報M13-01/02・M14-01/02/03、assist cacheのprovider/model/prompt/schema/version欠落を連携メモへ記録。テストを緩めず修正をお願いします。register()はsetdefaultなので将来の共通wrapperを上書きしない。ライブラリからcli.main/Assistを直接使う場合は現状register()を先に呼ぶ。
受入表: docs/codex-implementation/2026-10-06-codex-assist/ACCEPTANCE_UPDATE.md。未確認を合格扱いにしない。外部公開・デプロイ・外部メッセージ送信なし。

