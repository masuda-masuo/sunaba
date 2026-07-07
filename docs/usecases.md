# Code Sandbox MCP — ユースケース整理と機能・情報の充足度評価

> 位置づけ: LLM にこの MCP を使わせて開発させる場合の**想定ユースケースの棚卸し**と、
> それに対する**機能カバレッジ**および**初見ユーザー向け情報の充足度**の評価。
> 評価基準は `docs/design.md` の設計原則（最小コンテキスト / 境界防御 / 事後監査）と、
> 2026-07 時点の実装（`src/code_sandbox_mcp/`）・README の突き合わせによる。

---

## 1. 想定ユースケース一覧

凡例: ◎ = 主要フローとして完全対応 / ○ = 対応（併用・注意点あり） / △ = 部分対応（ギャップあり） / × = 非対応（意図的スコープ外を含む）

| # | ユースケース | 典型フロー | 対応 |
|---|---|---|---|
| UC-1 | **GitHub issue 駆動のバグ修正** | `issue_view` → `clone_repo`（または `sandbox_initialize(clone_repo=…)`）→ `search_in_container` → `read_file_range` → `write_file_sandbox` → `verify_in_container` → `checkpoint` → `publish` | ◎ |
| UC-2 | **GitHub リポジトリへの機能追加 / TDD** | UC-1 と同一ループ。テスト先行なら `write_file_sandbox`（テスト）→ `verify_in_container(test_filter=…)` → 実装 → 全件 verify → `publish` | ◎ |
| UC-3 | **既存 PR のチェックアウト・修正** | `sandbox_initialize(repo=…, pr=N)` → 編集ループ → `publish` | ○（後述 3.4） |
| UC-4 | **ローカルプロジェクトの修正** | `copy_project` → 編集ループ → `verify_in_container` | △（後述 3.2） |
| UC-5 | **使い捨てコードの実行・検証**（スニペット検証、再現実験、データ処理） | `run_container_and_exec` または `sandbox_initialize` → `sandbox_exec` | ◎ |
| UC-6 | **依存パッケージの導入・アップグレード検証** | `package_install`（Python）→ `verify_in_container` | ○（pip のみ。後述 3.3） |
| UC-7 | **JS / TS プロジェクトの開発** | 編集ループ（search / eslint / tsc は対応）→ テストは `sandbox_exec` で生 jest | △（後述 3.1） |
| UC-8 | **Go プロジェクトの開発** | `sandbox:go` イメージで編集ループ → テストは `sandbox_exec` で生 `go test` | △（後述 3.1） |
| UC-9 | **長時間ジョブ**（ビルド、大規模テスト、依存解決） | `sandbox_exec_background` → `sandbox_exec_check`（+ SSE/HTTP transport） | ○（ジョブ状態はメモリ内 — Known limitations 記載済み） |
| UC-10 | **Web サーバ / 複数サービスの動作確認** | コンテナ内でサーバ起動 → 同一コンテナ内から `curl` | △（後述 3.5） |
| UC-11 | **人間による事後監査・レビュー** | journal / trace / dashboard / 通知 | ◎ |
| UC-12 | **調査結果の issue / コメント起票** | `sandbox_issue_write` | ○ |
| UC-13 | **GitHub 以外の VCS（GitLab / Bitbucket）** | — | ×（`gh` / GitHub API 前提。スコープ外だが明文化なし） |
| UC-14 | **issue 一覧管理・レビュー運用・projects 連携** | GitHub MCP の領分 | ×（design.md §1 で意図的に除外・明文化済み） |

**総評**: 本 MCP の主戦場である「issue → fix → verify → publish」（UC-1/2）は入口から出口まで
first-class ツールで閉じており、payload 非通過・構造化出力・checkpoint による巻き戻しまで揃っている。
ギャップは主戦場の外側 — 多言語テスト（UC-7/8）、非 GitHub プロジェクトの成果回収（UC-4）、
サービス動作確認（UC-10）— に集中している。

---

## 2. ユースケース × ツールのカバレッジマトリクス

ループの各フェーズに first-class ツールが存在するかの確認。

| フェーズ | ツール | Python | JS/TS | Go |
|---|---|---|---|---|
| 環境起動 | `sandbox_initialize`（言語検出でイメージ自動選択） | ✅ | ✅ | ✅ |
| 入口（課題取得） | `issue_view` / `clone_repo` / `pr=N` | ✅ | ✅ | ✅ |
| 検索 | `search_in_container`（ripgrep / ast-grep） | ✅ | ✅ | ✅ |
| 読取 | `read_file_range` / `list_files` | ✅ | ✅ | ✅ |
| 編集（宣言的） | `write_file_sandbox` | ✅ | ✅ | ✅ |
| 編集（命令的） | `transform_file` | ✅ | ✅ | ✅ |
| lint | `lint_in_container`（ruff / eslint、`fix=True` 対応） | ✅ | ✅ | —（未対応） |
| 型検査 | `type_check_in_container`（pyright / tsc） | ✅ | ✅ | —（go vet 未配線） |
| **テスト（構造化）** | `verify_in_container` | ✅ pytest | ⚠️ **未配線** | ⚠️ **未配線** |
| パッケージ導入 | `package_install` | ✅ pip/uv | —（`sandbox_exec` 経由） | —（同左） |
| 保存/巻き戻し | `checkpoint` / `checkpoint_list` / `checkpoint_restore` | ✅ | ✅ | ✅ |
| 出口 | `publish` / `sandbox_issue_write` | ✅ | ✅ | ✅ |
| 監査 | journal / trace / dashboard | ✅ | ✅ | ✅ |

---

## 3. 機能ギャップ（優先度順）

### 3.1 【最重要】jest / go test の構造化 verify が公開ツールに未配線

design.md §4 は「v1 は pytest / jest / go test の3つを"ちゃんと"対応」と明記し、
`docs/design-multilang-support.md` が設計を詳述している。実装状況:

- パーサ: `test_report.py` に `JestAdapter` / `GoTestAdapter` 実装済み。
- ランナー: `edit_verify.py` に `_run_jest_verify` / `_run_go_verify` とディスパッチ表実装済み。
- **しかし公開ツール `verify_in_container`（`tools/verify.py`）のテスト実行フェーズは pytest 固定。**
  言語検出（`detect_languages`）と lint/型ゲートは言語対応済みなのに、テスト層だけが
  ディスパッチに接続されていない。

結果: JS / Go プロジェクトでは「lint と型は構造化ゲートが効くが、テストだけ `sandbox_exec` で
生ログに落ちる」という中途半端な状態になる。構造化テスト結果（design.md §4「本丸」）の
恩恵が Python 以外で受けられない。

**推奨**: `tools/verify.py` のテストフェーズを `edit_verify.py` のディスパッチ表に接続する。
それまでの間は README のツール表に「テスト実行は現状 pytest のみ（JS/Go は `sandbox_exec` を使用）」と注記する。

### 3.2 非 GitHub プロジェクトの成果回収経路がない（意図的だが明文化不足）

ファイル転送はホスト→コンテナの片方向のみ（design.md §5）。成果の出口は `publish`
（= GitHub への push）だけ。したがって **GitHub に置いていないローカルプロジェクトは
「copy_project で入れて検証まではできるが、修正結果を取り出せない」**。

設計として一貫している（コンテナ→ホスト逆流はリスク階層上禁止）が、README にこの帰結が
書かれていない。UC-4 を期待して使い始めた初見ユーザーが最後に詰む形になる。

**推奨**: README「Known limitations」に「成果の出口は publish（GitHub）のみ。
ローカル専用プロジェクトのラウンドトリップは対象外」と明記。

### 3.3 package_install は pip のみ

npm / cargo / go get は `sandbox_exec` 経由（`docs/designpackageinstall.md` でスコープ外と明記済み）。
JS/Go サポート（3.1）を進めるなら、依存解決ログのコンテキスト汚染は言語を問わないため、
将来的には同じ構造化を npm 等にも広げる価値がある。優先度は 3.1 の後。

### 3.4 PR レビューコメントの取得手段がない

`pr=N` でチェックアウトはできるが、レビューコメント・レビュー指摘の読取ツールはない
（issue は `issue_view` があるのに対し非対称）。「レビュー運用は GitHub MCP の領分」
（design.md §1）との線引きは理解できるが、「PR チェックアウト → 指摘対応 → push」という
ループを回すなら、入口の `issue_view` に相当する `pr_view`（コメント本文をコンテナ内ファイルへ、
LLM には要約＋ハンドル）は §11 の入口/出口原則と整合する。要検討。

### 3.5 サービス動作確認の限界と design.md の記述矛盾

- ホストへのポート公開は意図的に後回し（design.md §1）。コンテナ内起動＋コンテナ内 `curl` で
  API の動作確認は可能だが、人間がブラウザで UI を確認する経路はない。
- **記述矛盾**: design.md §10 は「多サービス環境が必要な場合は `sandbox_exec` から
  docker compose を直接使う」とするが、§2.1 は `/var/run/docker.sock` のマウントを拒否している。
  コンテナ内に Docker デーモンは無いため、この案内は現行ガードレール下では成立しない。
  §10 の記述を実態に合わせて修正するか、compose の位置づけを再定義する必要がある。

### 3.6 その他（既知・低優先）

- バックグラウンドジョブ状態がメモリ内（README Known limitations 記載済み）。
- GitHub 以外の VCS 非対応（明文化推奨、対応自体はスコープ外で妥当）。
- Go の lint（golangci-lint 等）・`go vet` が `lint_in_container` / `type_check_in_container` に未配線。

---

## 4. 初見ユーザー向け情報の充足度

### 4.1 揃っているもの

| 項目 | 場所 | 評価 |
|---|---|---|
| コンセプト・設計思想 | README 冒頭 / design.md | ◎ 「何を守り、何をしないか」まで明快 |
| クイックスタート・インストール | README | ○ |
| 典型ワークフロー（5ステップ） | README | ◎ ツール 30 個を読まずに全体像が掴める |
| ツール一覧（1行説明） | README | ○ |
| トランスポート選択と理由 | README / design.md §7 | ○ |
| セキュリティモデル | README / design.md §2 | ◎ |
| 本番デプロイ（3プラットフォーム） | README | ◎ gotcha まで記載 |
| 既知の制限 | README | ○（3.2 の追記が必要） |

### 4.2 不足しているもの（優先度順）

**P1 — 最初の 10 分で詰むポイントが書かれていない**

1. **前提と初回の落とし穴の節がない**。具体的には:
   - Docker デーモンが起動済みであること（要件行に「Docker」とあるだけ）。
   - 初回 `sandbox_initialize` は GHCR からのイメージ pull が走り数分かかりうること。
     stdio の ~60 秒タイムアウトと相互作用して**初回がタイムアウトで失敗する**のが
     最も典型的な初見の躓きだが、SSE 推奨の記述は「Configuration」まで読まないと出てこない。
   - **ネットワークは既定 off** のため、`clone_repo` / `package_install` / pip を使う作業には
     `sandbox_initialize(allow_network=True)` が必要なこと。workflow example には書かれているが
     「なぜ必要か・忘れるとどう失敗するか」の説明がどこにもない。
2. **トラブルシューティング節がない**。最低限: Docker 接続不可 / permission denied
   (docker group) / イメージ pull 失敗 / stdio タイムアウトの症状 / `BLOCKED by egress proxy`
   エラーの意味と `CODE_SANDBOX_ALLOWED_REPOS` の設定。

**P2 — セットアップの段階性が示されていない**

3. **egress proxy の有効条件が読み取れない**。「VCS token safety」節は proxy を常時の構造ガード
   のように書くが、「Configuring the egress proxy」節は `CODE_SANDBOX_ENABLE_EGRESS_PROXY=true`
   設定時のみ有効（実装既定は off、`proxy_lifecycle.py` の staged rollout）。
   **既定構成で何が有効で何が守られるのか**が初見では判別できない。
   「最小構成 / proxy 有効 / broker 併用」の3列で保証内容を並べた表が必要。
4. **最小→安全の段階的ラダーがない**。(a) トークンなし（公開 repo 読取のみ）→
   (b) `GITHUB_TOKEN` 直指定 → (c) keystore + broker、の3段で案内すれば迷わないが、
   現状は Quick start の直後にいきなり mcp-launcher / systemd の本格運用が来る。
   「とりあえず自分の公開 repo で issue → publish を一周する」最短経路が示されていない。

**P3 — あれば強い**

5. **クライアント別設定例が Claude Desktop のみ**。Claude Code（`claude mcp add`）、
   opencode、Cursor 等の例がない（WSL 節に断片的にあるのみ）。
6. **「ホスト側シェル権限をオフにする」具体手順がない**。本 MCP の核心的推奨
   （README「Reducing host permissions」）なのに、各クライアントでどう設定するか
   （例: Claude Code の permissions deny）の案内がない。思想だけあって操作がない状態。
7. **ドキュメント言語の分裂**。README は英語、design.md は日本語。どちらかしか読めない
   ユーザーには半分が届かない。少なくとも README から design.md への参照に言語注記を。
8. **環境変数リファレンスの一元化**。`CODE_SANDBOX_*` が design.md 内の表・README・
   各節に分散している。一覧表を README に一つ。

### 4.3 LLM（クライアント側モデル）にとっての充足度

初見の「ユーザー」には LLM 自身も含まれる。こちらは概ね良好:

- ツール docstring は「Use when / 使い分け / 注意点」まで含み厚い（例: `verify_in_container` の
  「単一ファイルなら `sandbox_exec` + pytest を使え」）。MCP スキーマ経由で LLM に届く。
- エラー契約が `{status: "error", error: …}` に統一済み（design.md §6a）で、LLM が
  分岐しやすい。
- 改善余地: 「このサーバの使い方」を1ツールで返す規約的な入口（もしくは MCP の
  server instructions）に「典型5ステップ + allow_network の要否 + verify が pytest のみ」を
  載せると、クライアント側のシステムプロンプト整備なしで正しいループに乗りやすい。

---

## 5. 推奨アクションまとめ

| 優先度 | アクション | 種別 |
|---|---|---|
| P1 | `verify_in_container` のテスト層を jest / go test ディスパッチに接続（§3.1） | 実装 |
| P1 | README に「前提・初回の落とし穴」（Docker 起動 / 初回 pull / allow_network / stdio→SSE）節を追加（§4.2-1） | ドキュメント |
| P1 | README ツール表に「テスト実行は現状 pytest のみ」の注記（3.1 実装までの暫定） | ドキュメント |
| P2 | egress proxy の既定 on/off と「構成別の保証内容」表（§4.2-3） | ドキュメント |
| P2 | 最小→安全のセットアップラダー + トラブルシューティング節（§4.2-2,4） | ドキュメント |
| P2 | Known limitations に「成果の出口は publish のみ」（§3.2）と GitHub 限定（§3.6）を追記 | ドキュメント |
| P2 | design.md §10 の docker compose 記述を §2.1 と整合させる（§3.5） | ドキュメント |
| P3 | `pr_view`（レビューコメントの入口ツール）の検討（§3.4） | 検討 |
| P3 | クライアント別設定例・ホスト権限オフ手順・環境変数一覧（§4.2-5,6,8） | ドキュメント |
