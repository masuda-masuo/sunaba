# Issue #181 フォローアップ: unhealthy コンテナによるセッション凍結の診断

PR #199 で recovery/poll 系に短い Docker タイムアウト・`sandbox_stop` の
force-kill・リソース override を入れたが、#181 の本丸である **セッション全体の
凍結** の真因は未診断のまま残っていた。本ドキュメントは追加診断と、#199 が
カバーする範囲 / しない範囲を整理する。

## 観測された事実

issue #181 では、unhealthy コンテナ発生後に **Docker を一切呼ばないツールまで**
`-32001 Request timed out` になった:

- `sandbox_list_runs` → `get_runs()`（ジャーナルのファイル読み、Docker 非依存）
- `sandbox_approval_status` → `get_pending_tokens()`（メモリ上の状態、Docker 非依存）

もし「個々の Docker 呼び出しがブロックするだけ」が真因なら、これらは即答する
はず。固まったという事実は、wedge が **個々の呼び出しではなくセッション /
トランスポート層** で起きていることを示す。

## 真因の仮説（未実証）

stdio トランスポート + MCP クライアントの制約による複合:

1. クライアントは 1 ツール呼び出しずつ in-flight にする（直列）。
2. unhealthy コンテナへの poll（`sandbox_exec_check`）等が docker-py の
   デフォルト 60s タイムアウト近くまでブロック。
3. ~60s のクライアントタイムアウトに達して `-32001`。この時点で stdio の
   JSON-RPC ストリームが desync、もしくは以降のリクエストを送れず、
   **非 Docker 系を含む全ツールが応答不能** に見える。

> 注: これは観測事実と整合する仮説であり、決定的な再現実証は未完。実証には
> 実 MCP クライアント + 実 wedge コンテナの再現が要る（残課題）。

## #199 がカバーする範囲 / しない範囲

カバー:

- recovery/poll 系（`sandbox_stop` / `sandbox_exec_check`）を `RECOVERY_DOCKER_TIMEOUT`
  で打ち切り、~60s のクライアントタイムアウト **前に** 返す → desync の誘発を
  避け、recovery を応答可能に保つ。
- `sandbox_stop` の force-kill により、wedge コンテナでも停止処理自体がハングしない。

しない:

- recovery 系 **以外** の長時間 Docker 呼び出し（verify/pytest 等）は依然
  セッションを wedge させ得る。
- stdio トランスポートの ~60s 天井という構造的脆弱性そのもの。

## 構造的な対策（推奨）

issue 本文も指摘する通り、stdio の ~60s 天井を構造的に外すのは
`--transport sse` / `--transport http`。重いインストールや長時間操作を伴う
ワークロードではこちらを推奨。`sandbox_exec_background` + `sandbox_exec_check`
（短タイムアウト poll）との併用で、stdio でも実務上は回避できる。

## 残課題

- [ ] 実 MCP クライアントでの wedge 再現と、本仮説（desync）の実証。
- [ ] mem_limit/cpus リソース override（#199 同梱）のセキュリティ評価 → #201 へ分離。
- [x] recovery timeout の環境変数化（本 PR で対応: `CODE_SANDBOX_RECOVERY_DOCKER_TIMEOUT`）。
