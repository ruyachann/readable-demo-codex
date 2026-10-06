---
name: coordinate-with-codex
description: Codex と共有しているファイル (readable/structure.py, cli.py, config.py, webapp.py, web_static/, tools/build_dist.py, codex_provider.py, website/scripts/) を編集するときの手順。共有ファイルの変更や、Codex 版にも関わる変更のときに使う。
---
# Codex と共有ファイルを編集する手順

1. `docs/CODEX_CLAUDE_COORDINATION.md` の末尾を読み、Codex が今どのファイルを担当中か確認する
2. 同じファイルを Codex が担当中なら、編集せずにメモへ依頼を書き、Opus に報告する
3. 編集してよい場合、メモの末尾に追記する: 日時 / 担当 (Codex 側のどの係か) / 内容 / 担当ファイル / 共有ファイルへの変更の範囲
4. **編集の直前に** 対象ファイルの最新版を読み直す (Codex が数時間前に更新していることがある)
5. 全体の書き換えはしない。最小の差分で変更する。Codex 版と Codex 版の両方で動くようにする (provider 固有の処理は provider 側へ)
6. 全体の pytest を流す。Codex のテスト (test_codex_provider, test_provider_integration, test_dist_variants など) が落ちたら、自分の変更が原因か、元から落ちていたかを切り分けて報告する
7. 終わったらメモに「完了」と変更ファイルを追記する
