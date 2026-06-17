# Code Sandbox MCP — 設計方針・機能ロードマップ

> 位置づけ: これは「Dockerを操作するMCP」ではなく、**AIが最小限のコンテキストで安全にテスト・検証・提出を実行できる基盤**。
> 機能を増やすことが目的ではなく、トークン消費を抑え・推論精度を保ち・人間の最終制御を残すことを優先する。

---

## 0. 基本思想

- **AI-first**: 全部見せない / 生データではなく構造を返す / 段階的に詳細化する。
- **サンドボックス境界による安全 + 事後監査**: 守るべき線は「危険な操作か?」ではなく **「サンドボックスの外に出る操作か?」**。コンテナ内は使い捨てなので中で何が起きても消えるだけ（中の任意コマンドはゲートしない）。境界を越える操作（永続ボリューム/ホストマウント/ネットワーク/外部VCS書き込み）だけを §2 の静的ガードレールで構造的に塞ぐ。それでも防ぎきれない部分は §8 の append-only 実行ジャーナルで**事後に追える**ことを安全網の主役にする。
- **クリーン環境優先**: 状態共有による高速化より、再現性・安全性・デバッグ容易性を優先。原則は使い捨て環境。
- **スコープの線引き**: このMCPは「テスト・検証・提出（submit）まで回すサンドボックス」。**コード理解レイヤーの自前実装は引き続き §1 で除外**。外部VCSへの提出（push/PR/issue取得）は、コンテナ内クローンに対する**境界越え操作**として §2.2 の条件下でのみ許可。ユーザのローカル作業ツリーは §5 の通り対象外のまま。
- **組織化プリミティブ = セッション**: すべてのツールは「Docker操作」ではなく **ライブなセッション（コンテナ＋FS＋run履歴を持つ作業空間）に対する操作** として設計する。機能の所属は常に **「それはセッション状態に対する操作か?」** で判定する（Yes＝入れる / 横断的なコード理解はNo＝§1で切る）。
- **リスク階層**: AI にホストを直接操作させるリスクは最大（`rm -rf ~` / `git push --force` / SSH鍵流出の経路が無制限）。コンテナに隔離すれば、何をされても使い捨てで消える。この MCP の価値の半分は **「できないことの保証」** にある — ネットワーク off 既定 / 非 root 強制 / VCSトークン opt-in の三重ロックで、AI の暴走が境界を越える経路を構造的に塞いでいる。
- **affordance**: インストール済み ≠ 使われる。LLM はツールリストに**動詞として載っている能力**しか常用しない（Claude Code が bash を持ちつつ Grep/Edit/Glob を別ツールとして出しているのが傍証）。「使わせたい操作」は first-class ツールにする。**ただし §1/§5 の非増殖と両立**させ、first-class 化は「常用させたい動詞」に限る（残りはイメージ同梱＋exec）。

---

## 1. スコープ判断（あえてやらないこと）

線引きを明確にして発散を防ぐ。判定基準は§0の通り **「ライブなセッションに対する操作か?」**、および **「インデックス/埋め込みストア/コードグラフを所有・永続化するか?」**。

- **コード理解レイヤーの自前実装** → **このMCPに入れない。**
  基準は能力ではなく「**インデックス/埋め込みストア/コードグラフを所有・永続化するか**」。
  - **入れない（所有・永続化が必要）**: 埋め込みベースのセマンティック検索 / code-RAG ストア / 永続コードグラフ。既存 code-RAG MCP と丸かぶりし保守が膨張する。必要なら**別リポジトリ**。
  - **入れてよい（使い捨てコンテナ内で完結する CLI）**: ripgrep / ast-grep（構造検索＋書換）/ ctags 等。MCP は状態を持たず、生成物はコンテナ破棄で消えるため「自前で抱える」に当たらない。セマンティック検索は CLI 形が無いので自動的に外側へ落ちる。
- **`snapshot_container` / `restore_from_snapshot`** → 後回し（クリーン環境思想と矛盾）。
- **`create_network` / `expose_port_temporarily` / `estimate_cost`** → 後回し。Composeで足りる領域はComposeに寄せる。
- **コンテナ内任意コマンドの「危険度判定」ゲート** → **入れない。** 自己申告に依存して構造的に強制できない。守るのは境界越え操作のみ（§2）。
- **Tool Auto Discovery / Intelligent Test Runner** → 北極星（ビジョン）として保持。
- **外部VCS の広域運用** → **入れない。** issue 一覧管理・レビュー運用・projects 連携等は GitHub MCP の領分。このMCPは入口（`issue_view`）と出口（`submit`）に限定。

---

## 2. セキュリティ（土台 / Phase 0・非機能）

機能ではなく**最初のコミットから入っている前提**。実行時の承認UIではなく「静的ガードレール」。**守る対象はサンドボックスの境界を越える操作に限定する**。

### 2.1 静的ガードレール（マージ済み）
- 非root実行（`--user`）を既定強制。
- `--privileged` 禁止。
- `/var/run/docker.sock` 等の危険ソケットのマウント拒否。
- ホストマウントはホワイトリスト制限。
- memory / CPU / pids 制限、ネットワークは既定 off。
- 再現性のため **imageはタグでなくダイジェスト固定**（`image@sha256:...`）。

### 2.2 境界越え操作のトークン必須（Phase 0 拡張）
**「危険操作の承認」という発散した枠組みは捨てる。** 残すのは、サーバが専用ツールとして所有していて構造的に強制できるものだけ。

**対象（write 系・トークン必須）**
- 永続ボリュームの削除 / 永続リソースの削除 / ホストマウント変更 / ネットワーク変更。
- **外部VCS への書き込み**: `git push` / PR作成 / PRコメント / リモートブランチ削除。verify ゲート（§5）未通過の push はトークン発行段階で拒否。
- 方式: 該当ツールは `dry_run` で実行予定＋確認トークンを返し、本実行はトークン無しでは無条件拒否（二段階トークン）。elicitation は対応クライアント向けの確認UI糖衣として任意で返す。

**対象（read 系・ネットワーク許可＋記録のみ）**
- **外部VCS からの読み取り**: `gh issue view` / `gh pr view` 等は破壊的でないため二段階トークン不要。ただし外部ネットワークなので §8 ジャーナルに必ず記録し、ネットワーク既定 off に対する明示許可フラグ配下で実行。

**原則**: 外に書くものはトークン必須、外から読むものは記録のみ。どちらも §8 ジャーナルに残す。

**非対象**: コンテナ内の任意コマンド（使い捨てサンドボックス内で完結するためゲートしない。§8 ジャーナルで事後追跡）。

---

## 3. トークン削減（LLM視点）

**反復ループ全体での削減**を主眼にする。系統は **(1)状態をサーバに置く (2)全文でなく差分を返す (3)返す前にノイズを削る**。

### 3.1 状態はサーバ側、LLMは「ハンドル」を持つ
- **`run_id` を全結果の起点にする。** 「続きのログ」「失敗したテストだけ再実行」を巨大な再投入なしに参照できる。
- **大きな成果物はインライン展開しない。** カバレッジ・生成物・大JSONは `resource://run/123/coverage (1.2MB)` のようにサイズ付きハンドルで返す（MCP resource）。
- **`issue_view` もハンドル方式**: issue 本文をコンテナ内ファイルへ落とし、LLM には要約＋ハンドルのみ返す。全文は `read_file_range` で必要時に。

### 3.2 全文ではなく「差分」を返す（反復ループの本命）
- **run間diff**: 前回比で変化点だけ返す。
- **失敗のフィンガープリント＋重複圧縮**: 同型失敗は `×N` に畳む。
- **`rerun_failed(run_id)` / 影響範囲の絞り込み再実行**: 失敗分・変更ファイルが影響するテストだけ実行。
- **コンテンツアドレスな結果キャッシュ**: image＋コマンド＋入力ハッシュが不変なら `cached: true`。
- **`submit` の差分も非通過**: diff はコンテナ内で完結し、LLM には差分サマリ＋ハンドルのみ返す。

### 3.3 返す前にデノイズ
- ANSIカラー・タイムスタンプ・`\r` 進捗バーを除去。`CI=true` / `--no-color` 等を強制。
- **ライブラリフレームの剪定**: `site-packages` / pytest内部を落とし、ユーザコードのフレームに圧縮。
- **成功はほぼ無料に**: all-passなら `{status: ok, passed: 120, duration: 4.2s}` の一行だけ。

### 3.4 補助
- **トークン予算パラメータ**: `max_output_tokens` を渡すと枠に収まるよう要約し「続きはこのハンドル」。
- **コマンドのバッチ実行**: `[cmd1, cmd2, cmd3]` を1コールで。

### 原則（必須）
**既定は差分・要約、ただしフルは常にハンドルで取得可能（offset/limitで逃げ道を必ず残す）。**

---

## 4. 構造化テスト結果（本丸）

5000行のログではなく結果を返す。AI-firstの核心。

```json
{
  "status": "failed",
  "duration": 12.3,
  "passed": 120,
  "failed": 2,
  "failures": [
    { "test": "test_login", "error": "AssertionError", "file": "auth/login.py", "line": 42 }
  ]
}
```

- **v1は pytest / jest / go test の3つを"ちゃんと"対応。**
  - pytest: `pytest-json-report` + `--json-report`
  - jest: `jest --json`
  - go test: `go test -json`

---

## 5. Edit/Verify サブシステム（最小編集ループ）

> 位置づけ: **テストが失敗したときに、その場で少しだけ効率よく直して再検証するためだけ**のツール群。§4の構造化失敗（`file:line`付き）を入力に、最小の修正→再検証を回すことだけが目的。

**独立性の設計**: §0のセッションプリミティブ上に乗る**独立サブシステム**。ただし **同一サーバ内に同居**（別サーバにするとコンテナハンドルを失う）。

**コア（これだけでループが閉じる）**
- **`search_in_container`**: `mode: lexical|structural` で ripgrep / ast-grep を切り替え。`{file, line, text}` 配列を返す。`max_results` 上限付き。理想ループの起点。
- **`apply_patch`**: unified diff を当てる（**主役**）。ファイル全文の送受信を避けトークンを1〜2桁削減。
- **`read_file_range`**: `offset` / `limit` で該当箇所だけ読む。
- **`verify`**: lint（ruff）＋ type_check（pyright/mypy）＋ test（pytest/jest/go test）＋ scan（semgrep）を **1コールで強制実行**。各ツールの出力を `{file, line, rule, severity, message}` に統一。`submit` の内部ゲートでも必ず再実行（構造的強制）。ゲート閾値: lint error / pytest fail / semgrep ERROR で push 拒否。型エラーと semgrep WARNING は設定で変更可能。

**二次（需要が出てから）**
- `sd`（テキスト/設定/Markdown の正規表現置換）/ `ast-grep`(rewrite)（コード構造書換）。`apply_patch` で届かない場面用。§1改訂の通りコンテナ内 CLI なので可。
- `format_in_container` / `tree_in_container` / `git_diff_in_container`。

**スコープ境界**: 対象は使い捨てサンドボックス内のファイルのみ。ユーザの実リポジトリ作業ツリーは対象外。**コア4点が主役**、二次は後追い。

**理想ループ（最小コンテキスト）**
```
search_in_container → read_file_range → apply_patch → verify → submit
```

---

## 6. 出力制御

- `verbose`: `error_only` / `summary` / `full`（既定 `summary`）。
- トランケート＋メタデータ: `{ "shown": 20, "total_lines": 5000, "truncated": true }`。
- ページング: `offset` / `limit`、レスポンスに `next_offset` / `has_more`。
- 終了コード≠0 / stderrありの場合はエラー周辺を多めに含める。

---

## 7. トランスポート（タイムアウト対策）

> **問題**: stdio ベース MCP トランスポートにはクライアント側で ~60秒のタイムアウトがかかる。Docker の `pull`, `build`, 大規模 `put_archive` などがこの制限を超える。

**解決**: FastMCP がサポートする HTTP ベースのトランスポート（SSE / HTTP / streamable-HTTP）に切り替えることでクライアント側のタイムアウト制約を回避する。

### 選択肢

| トランスポート | タイムアウト | 特徴 |
|---------------|-------------|------|
| `stdio`（既定） | ~60秒 | シンプル、全クライアント対応、launcher が stdio プロキシ |
| `sse`（推奨） | なし | Server-Sent Events、HTTP 永続接続、クライアントが SSE をサポートする必要あり |
| `http` | なし | 標準 HTTP、ステートレスに近い |
| `streamable-http` | なし | MCP spec の Streamable HTTP、双方向ストリーミング |

### アーキテクチャ

`stdio` モードでは launcher が子プロセスの stdin/stdout/stderr をプロキシする2プロセス構成。`sse`/`http` モードではサーバが TCP ポートにバインドし、launcher はプロセスライフサイクル管理のみを行う。

```python
# server.py (抜粋)
transport = args.transport  # "stdio" | "sse" | "http" | "streamable-http"
if transport == "stdio":
    mcp.run(transport=transport)
else:
    mcp.run(transport=transport, host=args.host, port=args.port)
```

### 設定

```json
"--transport", "sse", "--host", "127.0.0.1", "--port", "8765"
```

`launcher` モードでも `--transport sse` を指定可能。launcher は stdio プロキシを省略し、サーバプロセスのみ管理する。

### 補足

- `sandbox_exec_background` + `sandbox_exec_check` のバックグラウンド実行パターンは引き続き利用可能（SSE 下でも有用）。
- `_ensure_image()` の `docker pull` がタイムアウトする問題は、SSE 移行により解消される（pull 中にクライアントがタイムアウトしなくなる）。

---

## 8. 事後監査が安全網の主役（旧 HITL）

> 旧 §7「実効性のある承認」は廃止。コンテナ内の任意コマンドは構造的に強制できない。だが**サンドボックス内は使い捨てなのでゲート不要**（§0）。本当に強制すべき境界越え操作は §2.2 に統合済み。

人間の最終制御は「実行前の承認」から **「事後に追える監査」** に重心を移す。

---

## 9. 人間が把握しやすい仕組み（可観測性 / 監査）

§8の通り安全網の主役はここ。**実装の最優先はジャーナル**。

- **人間可読の append-only 実行ジャーナル（最優先）**: `tail -f ~/.code-sandbox-mcp/journal.log` で「いつ・どのimageで・何を・実行結果サマリ・境界越え操作なら承認の有無・外部VCS操作の内容」が自然文で流れる。全実行を漏れなく記録。改竄しにくい append-only を厳守。
- **run_id 単位のリプレイ可能トレース出力（HTML / JSON）**: 事後に「なぜそう動いたか」を共有・レビュー。
- **ローカルWebダッシュボード（localhost限定 / read-mostly / 自動更新）**: 稼働コンテナ・run履歴・pass/fail・リソース使用量・承認待ちを一目で。
- **承認キュー＋ワンクリック Approve/Reject（縮小）**: §2.2 の境界越え操作トークンと連動。ダッシュボードの一機能。
- **プッシュ通知（OS通知 / Webhook）**: 境界越え操作・失敗閾値超え・長時間実行のときだけ。
- **実行前の人間向けプラン表示**: `submit` で対象ブランチ・差分サマリ・上書き有無を提示。

> 注意: ダッシュボードは localhost 限定＋必要なら認証。

---

## 10. テスト環境クイックパス

- `run_test_environment`: Compose相当の環境を一括起動。ネットワーク作成・ヘルスチェック待機・後片付けを自動化し、各サービスのアクセス先を返す。
- `wait_for_condition`: TCPポート開放 / ログ内文字列 / healthy を条件に待機（タイムアウト付き）。AIによる `sleep 30` 乱用を排除。

---

## 11. 外部VCS連携（issue→fix→verify→submit の自己完結）

> 位置づけ: edit/verify ループの**入口（課題取得）と出口（提出）**だけを足す。GitHub MCP を介さず payload をコンテキストに通さないことが唯一の狙い。

**ツール（新規はこの2つだけ）**
- **`issue_view`**（`gh issue view` / read）: issue 本文をコンテナ内ファイルへ落とし、LLM には**要約＋ハンドル**だけ返す（§3.1）。§2.2 read 扱い（ジャーナル記録・ネットワーク明示許可）。
- **`submit`**（write / 境界越え）: `branch → commit → push →（任意）PR作成` を1コール。**内部で `verify` を必ず再実行**し、失敗なら push 拒否。§2.2 の dry_run＋トークン必須、§8 ジャーナルにプランと結果を記録。

**認証**: VCS トークンの注入は **opt-in** 化（Issue #57）。`sandbox_initialize` / `run_container_and_exec` の `inject_vcs_token=True` を指定したコンテナにのみ `GITHUB_TOKEN` / `GITHUB_TOKEN_SOURCE` / `GH_TOKEN` が注入される。既定は `False`（トークン無し）。これにより:
- 最小権限の原則を遵守（VCS 不要のコンテナにトークンが渡らない）
- 実行ログからのトークン漏洩リスク低減（`sanitize_output` 内の `mask_tokens` で `KEY=***` に自動マスク）
- トークンのスコープに応じた細かい制御が可能（read 用途と write 用途で別コンテナを使い分け）

**payload 非通過フロー**:
```
issue_view →(要約)→ search_in_container → read_file_range → apply_patch → verify → submit
```
issue 本文も diff もコンテナ内に留まり、LLM は run_id / ハンドル / 構造化サマリだけ運ぶ。

**スコープ境界**: 触るのはコンテナ内クローンのみ（§5 維持）。issue 一覧管理・レビュー運用・projects は入れない。

---

## 12. ベースイメージ（全部入り）

ツール同梱はイメージの責務。MCP のツール数は増やさない。`docker/Dockerfile.sandbox` で管理（→ §12）。

| カテゴリ | ツール | 用途 |
|---------|--------|------|
| 検索（字句） | `ripgrep` | `search_in_container` lexical モード |
| 検索・書換（構造） | `ast-grep` | `search_in_container` structural モード / rewrite |
| テキスト置換 | `sd` | 設定・Markdown 含む全テキスト（二次） |
| ファイル検索 | `fd` | find 代替 |
| シンボル | `ctags` | 任意（需要次第） |
| lint | `ruff` | Python lint + autofix |
| 型検査 | `pyright` | Python 型検査（mypy も可） |
| セキュリティ | `semgrep` | `verify` の scan 層 |
| VCS | `git` / `gh` | clone / push / issue_view |
| 高速インストール | `uv` | pip 代替（タイムアウト対策） |
| JSON処理 | `jq` | semgrep --json 等のパース補助 |

---

## 13. Dockerライフサイクル（必要最低限）

- `run_container_and_exec`（最重要 / ワンショット）
- `exec_in_container`
- `inspect_container`
- `build_image`（生成Dockerfileの即時検証）

**Dockerfile 管理**（リポジトリ内 `docker/` で管理・CI でダイジェスト固定タグをビルド）:
- `docker/Dockerfile.sandbox` — §11 の全部入りイメージ
- `docker/Dockerfile.sandbox.minimal` — git + python のみ（軽量・高速起動優先）

---

## 13. 推奨実装順

| Phase | 内容 | 狙い |
|------|------|------|
| **0** | セキュリティ土台 §2.1（マージ済み）＋ 境界越え操作のトークン必須 §2.2（外部VCS含む） | 機能ではなく前提 |
| **1** | 出力制御（§6）＋ `run_container_and_exec` | トークン削減ROI最大 |
| **2** | 構造化テスト結果 pytest/jest/go（§4） | AI-firstの本丸 |
| **2+** | Edit/Verify コア（§5）: `search_in_container`（lexical/structural）＋ `verify`（束ね・強制ゲート）＋ stdout バグ修正（#52） | 失敗→修正→再検証ループを閉じる |
| **4** | `run_test_environment`＋`wait_for_condition`（§10） | `sleep 30`撲滅 |
| **5** | 外部VCS連携（§11）: `issue_view` + `submit`（verify ゲート内蔵） | issue→push をコンテキスト非通過で自己完結 |
| 横断 | トークン削減（§3） | 各Phaseに織り込む |
| 並行 | 可観測性 §9 | ジャーナル→リプレイ→ダッシュボード→通知 |
| 基盤 | `docker/Dockerfile.sandbox`（§12・§13） | Phase 1 と並行して整備 |

**切る / 別リポジトリ**: 埋め込みセマンティック検索・code-RAG・永続コードグラフ、snapshot系、ネットワーク系の大半、コンテナ内コマンドの危険度判定ゲート、GitHub 広域運用。

---

## まとめ

基本思想（全部見せない / 構造を返す / 人間の最終制御 / できないことの保証）を採用し、コード理解レイヤーの**自前実装（ストア・インデックスの所有）**だけを切る（CLI は可）。
人間の最終制御は、**静的ガードレール（§2）＋ 境界越え操作のトークン必須（§2.2、外部VCS含む）＋ 全実行の事後監査ジャーナル（§9）** で担保する。
payload はコンテナ内に留め、LLM は制御だけ運ぶ。
AI が暴走しても被害は使い捨てコンテナ内に留まり、ホストは常に安全。
目標は一貫して——**AIが最小限のコンテキストで最大限の判断を行える、安全なテスト・検証・提出の基盤**。
