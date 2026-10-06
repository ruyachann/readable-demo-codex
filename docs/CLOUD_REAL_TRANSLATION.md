# クラウドでの実翻訳・PDF確認（準備中、未実行）

## 現在の状態

利用者のブラウザログイン後、既存GitHub接続から対象リポジトリを選び、Cloud環境のセットアップを開始した。
セットアップチャット: `01a1103f-d56c-77a7-b26b-87de6a1e80ad`（host: durable）。依存関係・子CLI認証・Geminiキー有無の確認のみを依頼済み。
実Gemini/Codex要求は0回。ローカルの認証・APIキーをCloudへ自動転送しない。実翻訳とPDF受入は未実施。
読取preflightの単体検証: `tests/test_cloud_preflight.py`、4 passed（モックのみ、実モデル要求0）。

## 論文候補（Webで原文を確認）

| ID | 論文・原文PDF | 頁 | 形式・重点 |
|---|---|---:|---|
| attention | [Attention Is All You Need](https://arxiv.org/pdf/1706.03762) | 15 | NeurIPS形式、1段組、数式、表、図 |
| resnet | [Deep Residual Learning for Image Recognition](https://arxiv.org/pdf/1512.03385) | 12 | CVPR形式のarXiv版、2段組、数式、図表、補足 |
| plos | [Assessing a behavioral nudge on healthcare leaders’ intentions to implement evidence-based practices](https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0311442&type=printable) | 16 | PLOS形式、余白の書誌情報、図表、統計表記 |

CVFのResNet PDFはWeb取得が403だったため、同論文のarXiv版を選んだ。全て原文リンクを記録し、PDFをGitに収録しない。実翻訳は全頁を対象にする。OCR形式は今回の3論文で評価したと主張しない。

## Cloud環境の準備

対象リポジトリ: `ruyachann/readable-demo-codex`。
Python 3.11以上、Node/Codex CLI、`requirements.txt`、`fonts-ipafont`、`fonts-dejavu-core`、PDFレンダリング用のPopplerを環境に用意する。
設定には `config.codex.cloud.toml` を使う。Windows向けの通常設定は変えない。

通信先は論文取得先の `arxiv.org` / `journals.plos.org`、Geminiの `generativelanguage.googleapis.com`、必要なCodex契約認証/推論先をCloud環境で許可する。認証先の必要範囲は実際の公式CLIで確認し、無制限の通信許可へ変更しない。
Geminiは環境の秘密情報設定で `GEMINI_API_KEY` を供給する。値をチャット、Git、ログへ出さない。
アプリが起動するCodex CLIのChatGPT契約ログインが必要。作業担当Codexの認証だけで、このCLIもログイン済みとは判断しない。OpenAI/Codex APIキーへは切り替えない。

準備完了後の読取診断:

```bash
python tools/cloud_preflight.py --context cloud-task --out work/cloud-preflight.json
```

診断はGeminiキーの有無だけを記録し、Codexは `login status` のみ。実モデル要求は0回。
`ready_for_pilot = true` は本番モデル疎通の成功ではない。最初の1論文の実翻訳で実際の疎通を確認する。

## 予算と実行

初期概算は各論文 `ceil(頁/6)+用語集1+再送余裕3` で、7+6+7 = Gemini20要求。これは抽出した文字量/用語集のバッチ/キャッシュ確認前の仮見積もり。
実行前に再見積もりして画面へ報告する。初回attentionの上限は12要求、全論文と再試行の累計上限は30要求。Codex実execは構造/補助/再試行合算で最大12回。
再試行は退出12の場合に限り1回。退出7で止める。Gemini統計が得られない中断は使用量不明として次の実行を止める。構造補正がスキップ/不採用なら成功にしない。

**上限に0を使わない。現在のGeminiClientは0を上限なしとして扱う。** 上限を正の整数で指定する。第2論文以降/再試行では、累計30から実績を引いた残額を設定し、残額0なら起動しない。

最初の1論文:

```bash
READABLE_GEMINI_MAX_REQUESTS=12 PYTHONIOENCODING=utf-8 python -m readable \
  work/cloud-inputs/attention.pdf --config config.codex.cloud.toml \
  --structure-provider codex --translator gemini --mode both \
  --work-dir work/cloud-cache --out out/cloud-check/attention
```

このコマンドはCloudの認証確認と、予定回数の報告が完了するまで実行しない。`--no-structure` や `--no-assist` で要求された実Codexを代替しない。
Cloud作業担当はCodex実execの累計も記録し、12回を超える補助/再試行を起動しない。

## PDF確認と成果物

各論文の原文/_ja.pdfを全頁60〜70dpiでPNG化し、原文と並べて目視。疑わしい箇所は110dpi以上。
英文残り、文字/図表の重なり、段の混在、図表や罫線の消失、英語併記の重複、タグ/数式プレースホルダ/豆腐を確認する。
数値・統計は原文と5段落以上照合。交互版は英→日、頁数2倍、リンク/しおりを確認する。
`render_report.json` と `gemini_stats.json`、実Codexのログ/採用状況を参照し、docs/ACCEPTANCE.md A1〜A14を論文ごとに合格/不合格/未確認で埋める。見ていない項目は合格にしない。
結果のPDF/PNG/キャッシュはCloud成果物として保存し、Gitへ入れない。文書には原文URL、終了コード、実AI回数、時間、検査結果、残件を記録する。

公式の環境設定: https://learn.chatgpt.com/docs/environments/cloud-environments
契約CLIの認証: https://learn.chatgpt.com/docs/auth#login-on-headless-devices
