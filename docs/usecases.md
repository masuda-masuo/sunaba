# Code Sandbox MCP — ユースケース整理と機能・情報の充足度評価

> 位置づけ: LLM にこの MCP を使わせて開発させる場合の**想定ユースケースの棚卸し**と、
> それに対する**機能カバレッジ**および**初見ユーザー向け情報の充足度**の評価。
> 評価基準は `docs/design.md` の設計原則（最小コンテキスト / 境界防御 / 事後監査）と、
> 2026-07-09 時点の実装（`src/sunaba/`）・README の突き合わせによるスナップショット評価。
> 本ドキュメント発のギャップ指摘（P1/P2）は #493〜#496 で対応済み。
> 本改訂（#530, 2026-07-11）では解消済み項目を棚卸し更新し、UC 一覧にレビュー実行を追加する。

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
| UC-7 | **JS / TS プロジェクトの開発** | 編集ループ（search / eslint / tsc / jest）→ `verify_in_container` で構造化テスト | ◎（#493 で解消済み） |
| UC-8 | **Go プロジェクトの開発** | `sandbox:go` イメージで編集ループ → `verify_in_container` で構造化テスト（`go test -json`） | ◎（#493 で解消済み） |
| UC-9 | **長時間ジョブ**（ビルド、大規模テスト、依存解決） | `sandbox_exec_background` → `sandbox_exec_check`（+ SSE/HTTP transport） | ○（ジョブ状態はメモリ内 — Known limitations 記載済み） |
| UC-10 | **Web サーバ / 複数サービスの動作確認** | コンテナ内でサーバ起動 → 同一コンテナ内から `curl` | △（後述 3.5） |
| UC-11 | **人間による事後監査・レビュー** | journal / trace / dashboard / 通知 | ◎ |
| UC-12 | **調査結果の issue / コメント起票** | `sandbox_issue_write` | ○ |
| UC-13 | **GitHub 以外の VCS（GitLab / Bitbucket）** | — | ×（`gh` / GitHub API 前提。スコープ外だが明文化なし） |
| UC-14 | **issue 一覧管理・projects 連携** | GitHub MCP の領分 | ×（design.md §1 で意図的に除外・明文化済み） |
| UC-15 | **PR レビューの実行**（#475 でスコープ化） | `sandbox_initialize(pr=N)` → 編集ループ → `diff_in_container` → `sandbox_pr_review_write` | ◎（`diff_in_container` / `sandbox_pr_review_write` が新設済み） |

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
| **テスト（構造化）** | `verify_in_container` | ✅ pytest | ✅ jest（#493 で配線済み） | ✅ go test（#493 で配線済み） |
| パッケージ導入 | `package_install` | ✅ pip/uv | —（`sandbox_exec` 経由） | —（同左） |
| 保存/巻き戻し | `checkpoint` / `checkpoint_list` / `checkpoint_restore` | ✅ | ✅ | ✅ |
| 出口 | `publish` / `sandbox_issue_write` | ✅ | ✅ | ✅ |
| 監査 | journal / trace / dashboard | ✅ | ✅ | ✅ |

---

## 3. 機能ギャップ（優先度順）

### 3.1 【解消済み】jest / go test の構造化 verify 未配線

#493 で `tools/verify.py` のテストフェーズが `edit_verify.py` のディスパッチ表に接続された。
現在は `verify_in_container` が pytest / jest / go test の3言語を構造化結果で返す。
UC-7/8 およびカバレッジマトリクスの該当セルは本改訂で ◎ / ✅ に更新済み。

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

### 3.5 サービス動作確認の限界

- ホストへのポート公開は意図的に後回し（design.md §1）。コンテナ内起動＋コンテナ内 `curl` で
  API の動作確認は可能だが、人間がブラウザで UI を確認する経路はない。
- **記述矛盾（#496 で解消済み）**: design.md §10 にあった docker compose の記述は #496 で
  §2.1（/var/run/docker.sock 非マウント）と整合する形に修正済み。

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

**P1 — 最初の 10 分で詰むポイント（#494 で解消済み）**

以下の指摘は #494 で README に対応済み:
- 「Prerequisites & first-run pitfalls」節で Docker デーモン・初回 pull タイムアウト・
  `allow_network` 要否を説明
- 「Troubleshooting」節で Docker 接続不可・permission denied・pull 失敗・
  stdio タイムアウト・`BLOCKED by egress proxy` エラーの対処を一覧表に

**P2 — セットアップの段階性（#495/#506/#509 で一部解消）**

3. **egress proxy の有効条件（#495/#506/#509 で解消済み）**: proxy は既定 on に変更され、
   README「Security model」節に proxy off 時／on 時の保証比較表が追加された。
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
8. **環境変数リファレンスの一元化**。`SUNABA_*` が design.md 内の表・README・
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

## 5. 推奨アクションまとめ（残課題のみ）

凡例: 本ドキュメント発の P1/P2 は #493〜#496 で対応済み。以下は現時点で未対応のもの。

| 優先度 | アクション | 種別 |
|---|---|---|
| P3 | `pr_view`（レビューコメントの入口ツール）の検討（§3.4） | 検討 |
| P3 | クライアント別設定例・ホスト権限オフ手順・環境変数一覧（§4.2-5,6,8） | ドキュメント |
| — | Go の lint（golangci-lint 等）・`go vet` が `lint_in_container` / `type_check_in_container` に未配線（§3.6） | 実装 |
| — | 非 GitHub プロジェクトの成果回収経路がないことの Known limitations 明記（§3.2） | ドキュメント |
| — | 最小→安全のセットアップラダー（§4.2-4） | ドキュメント |
