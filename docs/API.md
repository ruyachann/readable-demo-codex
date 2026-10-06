# HTTP API 仕様 (readable.webapp)

`python -m readable.webapp` (`翻訳アプリ.bat`) が提供する、**自分の PC の中だけで動くローカル用** の JSON API。
このアプリは公開サーバとして使うことを想定していません (サーバやサーバ側の保存領域は用意しません)。

## 方針

- 各ユーザーが自分のアカウント (自分の Gemini API キー、任意で自分の Claude Code のログイン) を使い、自分の PC で実行する。
- 保持するジョブは**最新の 1 件だけ**。新しい翻訳を始めると、前回のジョブ (アップロードした PDF と出力) は削除される。起動時には `work/web_jobs/` を空にする。
- 1 度に 1 ファイル。ジョブは 1 本ずつ実行する。

## 認証・共通ルール

- 起動時にランダムなトークンを作る。`http://127.0.0.1:<port>/?token=<token>` を開くと Cookie (`readable_token`, HttpOnly, SameSite=Strict) が設定される。
- API は Cookie または ヘッダ `X-Readable-Token: <token>` で認証する。不一致は 401。
- `Host` が `127.0.0.1:<port>` / `localhost:<port>` 以外なら 403 (DNS rebinding 対策)。
- POST / DELETE で `Origin` ヘッダがあり、自分自身でなければ 403。
- エラーは `{"error": "日本語メッセージ"}`。

## エンドポイント

| メソッド | パス | 内容 |
|---|---|---|
| POST | `/api/jobs` | multipart/form-data。`file` (PDF 1 つ)、`mode` (`ja`/`dual`/`both`、既定 both)、`structure` (`1` で構成の整理を使う。画面は常に明示する。省略時は従来の `claude` 欄を見て、それも省略ならオフ)、`retry` (`1` で --retry-failed)、`no_assist` (`1` で翻訳の補助を止める = `--no-assist`)、`replace` (`1`)。201 で `{"id": "..."}`。前回のジョブは削除される。実行中のジョブがあり `replace=1` が無いと 409 (`busy: true`)。`replace=1` なら実行中のジョブを中止して削除する |
| POST | `/api/inspect` | multipart で `file` を 1 つ。保存も翻訳もせず `{name, bytes, pages, encrypted}` だけ返す (画面の添付直後の表示用) |
| POST | `/api/jobs/<id>/retry` | 終了コード 7・9・12 のジョブを、保存してある同じ PDF で `--retry-failed` として再実行 (それ以外は 409)。JSON の `structure` (true / false) で今回の構造補正を指定。画面は利用可否を再確認して明示する。省略時は従来の設定を保持し、真偽値以外は 400 |
| POST | `/api/jobs/<id>/cancel` | 実行中・待機中のジョブを止める。ジョブ・アップロードした PDF・できていた出力は残し、終了コード 130 で終わる (`can_retry` が true になり、`/retry` で続きから再開できる)。それ以外の状態は 409 |
| GET | `/api/jobs` | `{"jobs": [Job]}` (0 または 1 件) |
| GET | `/api/jobs/<id>` | Job |
| GET | `/api/jobs/<id>/download/<ja\|dual>` | PDF。`?inline=1` でブラウザ内表示 |
| DELETE | `/api/jobs/<id>` | ジョブと保存ファイルを削除 (実行中なら中止) |

Job:

```json
{"id": "16 桁 hex", "name": "paper.pdf", "status": "queued|running|done|failed",
 "stage": "extract|structure|translate|render|finished", "log_tail": ["最後の 20 行"],
 "exit_code": 0, "message_ja": "終了コードに対応する日本語メッセージ", "warning": false, "pages": 12, "bytes": 123456, "failed_paragraphs": null, "structure_note": null, "can_retry": false, "structure_message": null, "assist_message": null,
 "options": {"mode": "both", "claude": false, "retry": false}, "created": 1700000000,
 "outputs": {"ja": "/api/jobs/<id>/download/ja", "dual": "..."}}
```

- `status` が `done` で `exit_code` が 12 のとき (一部段落が原文のまま) は `warning: true`。PDF は出力される。
- 検査: 先頭 `%PDF`、200MB まで (超過は 413)、非 PDF は 400、複数ファイルは 400。ファイル名は無害化される。

## 翻訳キャッシュ

各文書の翻訳キャッシュ (`work/<名前>-<ハッシュ>/`) はジョブとは別で、日次上限 (終了コード 7) の後に同じファイルで続きから再開するために使う。**最近使った 3 文書ぶんだけ**残し、古いものは自動削除する (終了コード 7 で止まった文書は、再開できるよう削除しない。成功すると通常の対象に戻る)。

## 初回設定用エンドポイント

すべてトークン必須 (未認証は 401)、POST/DELETE は Origin 検査あり。API キーを返すことはない (`masked` は末尾 4 文字のみ)。

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/api/setup` | 診断: `python`, `packages`, `fonts`, `gemini` (`configured`/`source`=env\|file\|none/`masked`), `claude` (`found`/`logged_in`), `consent`, `ready` |
| POST | `/api/setup/key` | `{"key": "..."}` を `%APPDATA%\ReadableJP\secrets.json` に保存 (環境変数 `GEMINI_API_KEY` があればそちらが優先) |
| DELETE | `/api/setup/key` | 保存したキーを削除 |
| POST | `/api/setup/test-key` | モデル一覧を 1 回取得して接続を確認 → `{ok, message_ja}` |
| POST | `/api/setup/consent` | `{"agree": true}` プライバシーへの同意を保存。同意前は `POST /api/jobs` が 403 |

ジョブ実行時、キーは子プロセスへ環境変数 `GEMINI_API_KEY` でのみ渡し、ログ出力にキーが現れた場合は `***` に置換する。

## 外部サイトとの連携

推奨は「ローカルで翻訳した PDF を、ユーザー自身がサイトにアップロードする」方式で、アプリ側の変更は不要。
将来、翻訳後の自動アップロードが必要になれば、config.toml に完了時のフック `post_actions` を足す (未実装。サイトの仕様が決まってから)。その場合も、既定は無効、送信前に宛先とファイル名を画面で確認、トークンは設定ファイルに書かない。

## 公開サーバとしての利用について

公開サーバとしての利用は想定していません。

補足: `structure_message` は構成の整理が (一部でも) 使われなかったときの 1 行 (例: 「構成の整理は行われませんでした: 理由」「構成の整理の結果は一部使われませんでした: N件を反映、M件は不採用」)。ログ全文は `work/web_jobs/<id>/job.log` (API キーは `***`、ジョブと一緒に削除)。DELETE はジョブとファイルを削除し、cancel は止めるだけで残す。

全体不採用の警告は、採用候補の件数にかかわらず「全部使われませんでした」と表示します。部分不採用の件数はキャッシュ再利用時や描画前の停止時にも保持し、前回の描画レポートを今回の結果として読みません。中止と子プロセス起動を同期し、待機中の中止後に再試行した場合も古いキュー項目から二重実行しません。

構造レポートの `structure` は `{used, status, accepted, rejected, reason}` です。`status` は `accepted / partial / rejected / skipped`。件数は役割変更の件数で、段落結合や記号対応の採用は `used/status` で判定します。コアは `[構造状態] <status>` と件数ログを出すため、役割変更が0件でも結合が採用された場合は部分採用と区別できます。中止処理が完了するまで次の再試行を登録しません。

補足: `assist_message` は翻訳の補助 (用語集の点検・問題段落の補正) の結果の 1 行 (例: 「Claudeの補助: 用語集の修正 16件、問題段落の補正 3/4件採用」「Codexの補助: 用語集の修正 2件」)。`/api/setup` の `assist.supported` は、この版で補助が実装されているか。Codex版もCLI/Web起動時に補助を登録する。
