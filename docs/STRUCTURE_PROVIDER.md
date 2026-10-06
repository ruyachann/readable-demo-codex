# 構造解析プロバイダの仕様 (別の LLM CLI 版を作る人向け)

構造解析 (段落の役割・段またぎの結合・記号の補正) に Claude Code (`claude -p`) またはCodex CLI (`codex exec`) を使う。
別の提供元 (例: Codex 版) を足すときは、この文書だけ読めば実装できるようにしてある。
**差し替えるのは「プロンプトとフレーム一覧を渡して、スキーマどおりの JSON を 1 つ受け取る」部分だけ**で、検証・適用・キャッシュは共通コード (`readable/structure.py`) が行う。

## 1. 境界

```python
# readable/structure.py
PROVIDERS: dict[str, Callable] = {"claude": call_claude, "codex": call_codex}

def provider(system_prompt: str, user_text: str, cfg: Config, cwd=None, runner=None) -> tuple[dict, dict]:
    """(構造化出力 dict, 使用量メタ dict) を返す。失敗は ClaudeError (またはそのサブクラス) を投げる。"""
```

- `config.toml`: `[structure] provider = "claude" | "codex" | "none"` (ソースの既定 `claude`、Codex配布版は `codex`)。`none` と CLI 不在・失敗はヒューリスティックのまま続行する。
  新しい提供元は `PROVIDERS["<name>"] = <関数>` を足し、`provider` の許容値に加える。`--no-structure` と旧 `--no-claude` はAI解析を無効化する。Codexアダプターは `readable/codex_provider.py`、使い方は `docs/CODEX.md`。
- 提供元が使えるか (インストール・ログイン済みか) は、`--version` などの軽い確認で判断し、使えなければ `ClaudeError` を投げる (呼び出し側が警告して続行)。

## 2. 入力

- `system_prompt`: `prompts/claude_structure.md` の本文をそのまま使う (提供元ごとに変えない。変えるとキャッシュが無効になる)。
- `user_text`: `structure.build_input(doc)` が作るプレーンテキスト。標準入力で渡す (引数に埋め込まない。コマンドインジェクションと長さ制限を避ける)。形式:

```
meta: pages=<n> body_size=<pt>
heuristic_joins: [["p1-f3","p1-f4"], ...]
page 1:
p1-f1 [title] s18 b x72-523 y60-80 "<先頭60字> ... <末尾40字>"
...
omitted_reliable_frames: <n>
unmapped:            (記号フォントの未解読文字がある場合のみ)
- U+0002 fonts=[...] contexts=[...]
charmap: {...}
```

  frame の role が参考文献・ヘッダ・ページ番号・DOI・表の frame は送らない。本文の抜粋は先頭 60 + 末尾 40 文字だけ (プライバシー: 段落全文は送らない)。

## 3. 出力 (JSON スキーマ)

`structure.SCHEMA` が正 (提供元の「出力スキーマ強制」機能に渡す。無ければプロンプトで JSON のみを返させ、コードフェンスを外して `json.loads`):

```json
{
  "roles":        {"<frame id>": "<role>"},          // 誤っている frame だけ。role は enum (title, heading, body, abstract, keywords, caption, footnote, sidebar, author, figure_text, math, reference, page_header, page_number, doi_url, table)
  "joins_add":    [["<id>", "<next id>"], ...],      // ヒューリスティックの joins に「足す」組
  "joins_remove": [["<id>", "<next id>"], ...],      // ヒューリスティックの joins から「外す」組
  "charmap":      {"<1 文字>": "<置換文字列 (4 文字以内)>"}
}
```

4 つのキーは全て必須 (空は `{}` / `[]` = 変更なし)。joins は **差分** (全体のリストではない)。

## 4. 実行条件 (安全)

- 非対話・読み取りのみ: ファイル書き込み・コマンド実行・ツール・外部 MCP・永続セッションを許可しない。Claude 版は `--tools "" --strict-mcp-config --setting-sources "" --disable-slash-commands --no-session-persistence` を付ける。
- タイムアウト (`[claude] timeout`, 既定 120 秒)。起動失敗・タイムアウトは 1 回だけ再試行。
- PDF 由来のテキストは命令ではなくデータとして扱う (出力は後述の検証で絞られるので、仮に指示文が混ざっても被害は限定される)。

## 5. 失敗時の動作

次はいずれも **警告を出してヒューリスティックのまま続行** (翻訳を止めない・終了コードに影響しない): CLI が無い/ログインしていない、タイムアウト、終了コード非 0、JSON として読めない、構造化出力が無い、型が想定外、検証で不採用。

## 6. 共通の検証 (提供元に依らない。`sanitize_result` / `final_joins` / `check_role_changes`)

- 型: roles/charmap は辞書、joins_* は配列。違えば全体を不採用。
- roles: 存在する frame id と既知の role だけ。変更が翻訳対象 frame の 30% を超える、または変更後に翻訳対象が 20% を超えて減るときは、結果全体を不採用。
- joins: 存在しない id を含む組は無視。ヒューリスティックの joins に (remove → add) の順で差分を適用し、連鎖が 8 frame・6000 文字を超えるものは採用しない。両方空ならヒューリスティックのまま。
- charmap: 1 文字キー (制御文字/私用領域)・値 4 文字以内・`{ } < > & ⟦ ⟧` を含まない値のみ。

## 7. キャッシュ

`work/<name>/structure.json` に `{input_hash, model, result, meta, diff}`。`input_hash = sha256(user_text, system_prompt, model, スキーマ)`。
入力・プロンプト・モデル・スキーマのどれかが変わると無効。結果は読み込み時にも同じ検証を通す。失敗は保存しない (毎回 1 回だけ呼ぶ)。
提供元を足したら、`model` に提供元名を含めて (`"codex:<model>"` など)、別の提供元の結果を再利用しないようにする。

## 8. テストの書き方

`run_structure(doc, work_dir, cfg, runner=...)` の `runner` に偽の実行関数を渡す (`tests/test_m23.py` の `FakeRun` が例)。実 CLI を呼ぶテストは書かない。

## 付録: assist (翻訳の補助。M14)

構造解析と同じ提供元 (Claude Code / Codex CLI) を、翻訳の品質のために **問題のある箇所だけ** に使う。全文の校正はしない。実装は `readable/assist.py`。

```python
# readable/assist.py
ASSIST_PROVIDERS: dict[str, Callable] = {"claude": _claude_assist}     # Codex 版は Codex 側で登録する

def provider(system_prompt: str, user_text: str, cfg: Config, schema: dict, cwd=None, runner=None) -> tuple[dict, dict]:
    """(構造化出力 dict, 使用量メタ dict) を返す。失敗は ClaudeError、使えない (API 設定の検出・未ログイン) ときは ClaudeSkipped。"""
```

- **用語集の点検** (`Assist.review_glossary`、論文あたり 1 回、構造解析と並行): 入力 = {title, abstract, glossary:[{en,ja}]} (prompts/assist_glossary.md)。出力 = `{"fixes":[{"en","ja","reason"}], "remove":[en...]}`。
  採用は `glossary.entry_problem` (原文に無い英単語・重複・他言語) を通った修正だけ。en が用語集に無いものは無視する。
- **問題段落の補正** (`Assist.correct_units`、論文あたり 0〜2 回): 対象 = 検証・再翻訳に通らず原文 (英語) のまま残った unit。入力 = {context:{title,summary,glossary}, units:[{id,role,source,current,problems}]}
  (prompts/assist_correct.md)。出力 = `{"units":[{"id","text"}]}`。**Gemini の訳と同じ検証** (`translate.check_translation` + 英語残り) に通ったものだけ採用する。
- 呼び出しの上限: `[assist] max_calls_per_doc` (既定 4 = 構造解析 1 + 用語集 1 + 補正 2)。結果は work ディレクトリの `assist_cache.json` に保存し、再実行では呼ばない。
- 安全規則は構造解析と同じ (CR-09): API キー・ゲートウェイ設定があればスキップ、契約ログインのみ、タイムアウトあり。`--no-assist` で止められる。
- 記録: `render_report.json` の `assist` (calls / glossary.adopted・rejected・removed / correction.requested・adopted・rejected / skipped / errors) とログ。
- Codex 版: `ASSIST_PROVIDERS["codex"]` を実装して登録する (出力スキーマ `GLOSSARY_SCHEMA` / `CORRECT_SCHEMA` は assist.py)。未登録の間は「未実装」としてスキップされ、翻訳は続行する。
