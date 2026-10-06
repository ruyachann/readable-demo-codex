# readable_demo — 英語論文PDF → 日本語PDF (Readable 代替)

毎回知っておくべき事実と決まりだけを書く (200 行未満)。経緯と詳細は docs/RESUME.md → docs/PROGRESS.md の末尾。

このcloneはCodex版の開発先。移行は docs/MIGRATION.md、コピー元の記録は docs/MIGRATION_MANIFEST.json。config.tomlの既定providerはcodex。過去の文書にある「担当中」は共有フォルダ時点の記録なので、編集前に最新の連携メモで確認する。

## 何を作っているか
- `python -m readable in.pdf` → `<name>_ja.pdf` (日本語版) と `<name>_dual.pdf` (英p1→日p1→… 交互)
- 翻訳 = Gemini API (利用者自身のキー・無料枠)。構造補正 = Codex CLI (`codex exec`、利用者の契約ログイン)
- **Codex API は使わない**。API キーやゲートウェイ設定を検出したら構造補正をスキップ (CR-09)
- 配布 = ローカル Web アプリ (`翻訳アプリ.bat`、127.0.0.1 のみ) + 紹介・配布サイト (`website/`、Codex 作成、URL 共有のみ・未公開)

## ユーザーの決定 (覆さない)
- PDF を入れたら PDF が返る。手作業の往復なし
- 図はそのまま。図・表の中の文字は英語のまま。和訳ホバー注釈は既定オフ（2026-10-06利用者決定、annotations=trueで有効）。本文の英語併記は各ページ初出のみ、色・下線なし
- 文字サイズは原文通りでなくてよい
- 構造補正は必須。使えないときは理由を出して「続けますか?」と確認 (方針 B)
- 翻訳は「翻訳を開始」ボタンで開始。失敗段落はボタン 1 つで再試行
- 主翻訳は Gemini (プロンプト C3 + 検証 + 選択的再翻訳)。Claude版に合わせ、Codexも用語集点検・失敗段落だけの翻訳補助に対応する（2026-10-06版更新依頼）。全文校正は行わず、補助は --no-assist / Web設定でオフにできる
- サーバーは立てない。結果は最新 1 件のみ保持

## 触ってはいけないもの (なぜ)
- `C:\Users\kota_\OneDrive\Documents\maedalab\paper\` と `english_paper/` の PDF: ユーザーの論文。読み取りのみ。リポジトリにコピーしない、テストに含めない (合成 PDF で再現する)
- `%APPDATA%\ReadableJP\secrets.json` と環境変数の API キーの値: 表示・記録しない
- `docs/codex-review/`: Codex のレビュー証拠 (固定版)。編集しない
- `website/dist/`: 生成物。`website/scripts/index.template.html` を編集して `build_release.py` で再生成する
- Codex と共有中のファイル (`readable/structure.py`, `cli.py`, `config.py`, `webapp.py`, `web_static/`, `tools/build_dist.py`, `codex_provider.py`): 編集前に `docs/CODEX_CLAUDE_COORDINATION.md` に担当範囲を追記し、**最新版を読み直してから最小差分で**。手順は Skill `coordinate-with-codex`

## 実行・テスト
- Windows。日本語出力は `PYTHONIOENCODING=utf-8`
- テスト: `python -m pytest -q` (pytest.ini で tests/ のみ収集。Gemini/Codex はモック)
- レイアウト確認は `--translator dummy` (API 0 回)。実翻訳の確認手順は Skill `real-translation-check`
- 正規表現をツール経由で書くとき `\b` が制御文字 0x08 になる事故があった → `(?<![A-Za-z0-9_])` 等で代用
- この開発セッションには `ANTHROPIC_BASE_URL` があるため Codex 構造補正は既定でスキップされる。試すときだけ `--allow-Codex-gateway`

## Gemini 無料枠の予算 (過去に 3 回超過した)
- 実リクエストを使う作業は、**始める前に予定回数を数えて**報告し、上限を守る。キャッシュ (work/ の内容ハッシュ) を最大限使う
- Web アプリは work/ を直近 3 文書に間引く → 連続で多数の論文を処理するとキャッシュが消える
- 日次上限は太平洋時間 0 時 = 日本時間 16 時 (夏時間) に解除
- **実機の実行は必ず `READABLE_GEMINI_MAX_REQUESTS=<上限>` を付けて行う** (超えそうなら送らずに止まり、終了コード 9。キャッシュは保存される)。本番 (利用者の実行) では設定しない。詳細は README の「開発」

## 分業 (サブエージェント, .Codex/agents/)
- `implementer` (書く係) / `reviewer` (確かめる係・書き換え不可) / `verifier` (実行と目視・書き換え不可) / `researcher` (外部調査) / `scribe` (進捗メモ)
- Opus は計画・判断・統合・詰まったときの交代。サブエージェントは同じ問題で 2 回失敗したら `BLOCKED:` で返す
- 「終わりました」は自己申告にしない。完了は docs/ACCEPTANCE.md の表を画面に出して判定する
- 進み具合は会話ではなくファイル (docs/PROGRESS.md) に書く。利用上限で止まっても再開できるように
