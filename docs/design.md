# Code Sandbox MCP — 設計方針・機能ロードマップ

> 位置づけ: これは「Dockerを操作するMCP」ではなく、**AIが最小限のコンテキストで安全にテスト・検証・提出を実行できる基盤**。
> 機能を増やすことが目的ではなく、トークン消費を抑え・推論精度を保ち・人間の最終制御を残すことを優先する。

## 設計の根本目標

**セキュリティを高めながら、可能な限りローカル相当の利便性を実現する。**

通常、セキュリティと利便性はトレードオフになる。権限を広げれば便利になり、絞れば不便になる。このMCPはサンドボックス（Dockerコンテナ）を中間層として置くことでこのトレードオフを回避する設計思想を持つ。

- AIから見た利便性: ローカル作業と変わらない操作感・速度
- ホストへの影響: 構造的に遮断、ホスト側シェル権限をオフにできる

「sandboxで完結できないならツールの問題」という姿勢はここから来ている。不便を感じた時点でそれはMCPの改善issueであり、ローカル直接作業への逃げは設計の失敗を隠蔽する。この目標は機能追加・変更の意思決定において常に判断軸となる。

---

## 0. 基本思想

- **AI-first**: 全部見せない / 生データではなく構造を返す / 段階的に詳細化する。
- **サンドボックス境界による安全 + 事後監査**: 守るべき線は「危険な操作か?」ではなく **「サンドボックスの外に出る操作か?」**。コンテナ内は使い捨てなので中で何が起きても消えるだけ（中の任意コマンドはゲートしない）。境界を越える操作（永続ボリューム/ホストマウント/ネットワーク/外部VCS書き込み）だけを §2 の静的ガードレールで構造的に塞ぐ。それでも防ぎきれない部分は §8 の append-only 実行ジャーナルで**事後に追える**ことを安全網の主役にする。
- **クリーン環境優先**: 状態共有による高速化より、再現性・安全性・デバッグ容易性を優先。原則は使い捨て環境。
- **リスク階層**: AI にホストを直接操作させるリスクは最大（`rm -rf ~` / `git push --force` / SSH鍵流出の経路が無制限）。コンテナに隔離すれば、何をされても使い捨てで消える。この MCP の価値の半分は **「できないことの保証」** にある — ネットワーク off 既定 / 非 root 強制 / VCSトークン opt-in の三重ロックで、AI の暴走が境界を越える経路を構造的に塞いでいる。
- **ホスト権限の最小化（承認疲れ防止）**: サンドボックスなしで AI がホスト上で作業すると、Bash/PowerShell の承認プロンプトが頻発する。ユーザーは承認疲れで全許可にしてしまい、結果として AI がホスト上で何でもできる状態になる。この MCP を使うことで実作業をコンテナ内に集約し、ホスト側のシェル権限をオフのまま維持できる。**MCP があることでホスト権限を絞れる**、というのが本来の設計意図であり、ローカル直接作業はその恩恵を自ら壊す。
- **スコープの線引き**: このMCPは「テスト・検証・提出（publish）まで回すサンドボックス」。**コード理解レイヤーの自前実装は引き続き §1 で除外**。外部VCSへの提出（push/PR/issue取得）は、コンテナ内クローンに対する**境界越え操作**として §2.2 の条件下でのみ許可。ユーザのローカル作業ツリーは §5 の通り対象外のまま。
- **組織化プリミティブ = セッション**: すべてのツールは「Docker操作」ではなく **ライブなセッション（コンテナ＋FS＋run履歴を持つ作業空間）に対する操作** として設計する。機能の所属は常に **「それはセッション状態に対する操作か?」** で判定する（Yes＝入れる / 横断的なコード理解はNo＝§1で切る）。
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
- **外部VCS の広域運用** → **入れない。** issue 一覧管理・レビュー運用・projects 連携等は GitHub MCP の領分。このMCPは入口（`issue_view`）と出口（`publish`）に限定。

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
- **外部VCS への書き込み**: `git push` / PR作成 / PRコメント / リモートブランチ削除。
- 方式: 該当ツールは一発実行（V1.0 で `dry_run`＋確認トークンの二段階は廃止）。人間ゲートは MCP クライアント自身のツール承認に一本化し、構造的防御は egress proxy（allowlist＋短命の認可ウィンドウ）が担う。二段階トークン／`sandbox_approve`・`sandbox_reject`・`sandbox_approval_status` は前提（HITL が唯一の防御）が egress proxy 成熟で失効したため撤去した。

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
- [x] **失敗のフィンガープリント＋重複圧縮**: 同型失敗は `×N` に畳む（`compress_failures`）。
- ~~コンテンツアドレスな結果キャッシュ~~: V1.0 の棚卸し（#457 / #459）で削除。default-deny 運用下ではヒット率が低く、鮮度バグの温床だった（#329 / #382）。再導入するなら `package_install` 内部などツール局所で。
- **`publish` の差分も非通過**: diff はコンテナ内で完結し、LLM には差分サマリ＋ハンドルのみ返す。

### 3.3 返す前にデノイズ
- ANSIカラー・タイムスタンプ・`\r` 進捗バーを除去。`CI=true` / `--no-color` 等を強制。
- **ライブラリフレームの剪定**: `site-packages` / pytest内部を落とし、ユーザコードのフレームに圧縮。
- **成功はほぼ無料に**: all-passなら `{status: ok, passed: 120, duration: 4.2s}` の一行だけ。

### 3.4 補助
- [x] **トークン予算パラメータ**: `max_output_tokens` を渡すと枠に収まるよう要約し「続きはこのハンドル」（`truncate_by_tokens`）。
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
- **`read_file_range`**: `offset` / `limit` で該当箇所だけ読む。
- **`write_file_sandbox`（編集の主役）**: 既知の新テキストを渡す宣言的編集。`overwrite` / 行範囲 / `append` / `old_str`（文字列置換）の各モードを束ねる。LLM 著作編集の既定経路。
- **`lint_in_container` / `type_check_in_container`**: 編集ループ中の単体（単一ファイル）確認用。ruff / pyright。`lint_in_container` は `fix=True` で `ruff check --fix` / `eslint --fix` を対象ファイルに適用し、修正後に残った findings を返す（#284。autofix は単一ファイル限定で、プロジェクト全体の scope フェーズは読み取り専用のまま）。
- **`verify_in_container`**: 公開前の品質ゲート。pytest の前に lint（プロジェクトの `src/` を素の `ruff check` で。CI と一致）と型チェックを**前提条件**として実行し、どちらかが落ちればテストを走らせず `gate_passed=false` と findings を返す（#293。lint 忘れが CI まで漏れない）。ツール不在（例: `:minimal` イメージ）は `lint_type_incomplete` 扱いでゲートは落とさない。`test_filter`（pytest `-k`）で特定テストだけ実行可能。フィルタ合格時は自動で全件テストを実行し、gate は常に全件ベースで判定。結果に `diff_summary`（`git diff --stat`）を含める。

**編集の2モダリティ（宣言的 / 命令的の直交2本）**

編集は「新バイト列を*渡す*」か「*計算するコードを渡す*」かの直交2本に集約する（§0 affordance の非増殖原則）。

- **宣言的 = `write_file_sandbox`**（上記コア）: 新テキストが既知のとき。点編集の主役。
- **命令的 = `transform_file`**: 新バイト列をコードで計算するとき（regex 一括 / 構造書換 / 計算的編集 / 機械生成 diff の `git apply`）。`code` は**単一トップレベル文字列**で受け内部 base64 化（multibyte/改行のエスケープ問題を構造的に回避）、**結果 diff を返す**ことで read-modify-write-verify を一体化（サイレント破壊の可視化）。#268 で `verify.py` から `file.py` へ移管、ファイル編集ツール群との統合を反映。
- **`apply_patch` は削除済み**: かつて「主役」と位置づけたが誤り。LLM 手書き unified diff は失敗率が高く、失敗時の往復で**トークン経済が反転する**。「フルファイル送信比で1〜2桁削減」は baseline が誤りで、真の競合は `old_str`（そこに対しペイロード優位はほぼ無く、コンテキスト行ぶん出力が増えることすらある）。`apply_patch` は `mcp.tool()` 登録を削除済み（#259）。機械生成 diff の適用は `transform_file` の `git apply` 経路を使用する。

**二次（需要が出てから）**
- `sd`（テキスト/設定/Markdown の正規表現置換）/ `ast-grep`(rewrite)（コード構造書換）。`transform_file` の内部実装に吸収可。§1改訂の通りコンテナ内 CLI なので可。
- `format_in_container` / `tree_in_container` / `git_diff_in_container`。

**ファイル操作（`tools/file.py`）**

コア4点とは別に、コンテナ内ファイルの一覧とホスト→コンテナの転送を担うツール群。`read_file_range` / `write_file_sandbox` / `transform_file` と同じ `tools/file.py` に同居する。

- `list_files`: `find` ベースでコンテナ内ファイルを一覧。`path` / `max_depth` / `pattern` フィルタ付き。
- `copy_file`: 単一ローカルファイルをコンテナへ転送（`local_src_file` → `dest_path`）。
- `copy_project`: ローカルディレクトリ（またはファイル）を tar アーカイブでコンテナへ転送（`local_src_dir` → `dest_dir`）。

転送はホスト→コンテナの片方向のみ。コンテナ→ホストの逆流は持たない（§5 スコープ境界・§0 リスク階層）。

**スコープ境界**: 対象は使い捨てサンドボックス内のファイルのみ。ユーザの実リポジトリ作業ツリーは対象外。**コア4点が主役**、二次は後追い。

**理想ループ（最小コンテキスト）**
```
search_in_container → read_file_range → write_file_sandbox(old_str) | transform_file → lint_in_container → type_check_in_container → verify_in_container → publish
```

`search_in_container` はデフォルトでリポジトリルートを検索対象とし（従来の `path="/"` からの変更）、戻り値には切り詰めメタデータを含む（§6b）。

---

## 6. 出力制御

- `verbose`: `error_only` / `summary` / `full`（既定 `summary`）。
- トランケート＋メタデータ: `{ "shown": 20, "total_lines": 5000, "truncated": true }`。
- ページング: `offset` / `limit`、レスポンスに `next_offset` / `has_more`。
- 終了コード≠0 / stderrありの場合はエラー周辺を多めに含める。

### 6a. エラー契約（Issue #467）

全ツールのエラー戻り値は以下の単一形状に統一する:

```json
{"status": "error", "error": "<人間可読なメッセージ>"}
```

ツール固有の追加フィールド（例: `verify_in_container` の `gate_passed`）は許容する。
エラー時は `status` と `error` の2フィールドを常に含むこと。

**該当ツール一覧:**

| ツール | 変更内容 |
|--------|---------|
| `search_in_container` | `[{"error":...}]` → `{"status":"error","error":"..."}` |
| `lint_in_container` | 偽 finding (`rule:"error"`) → `{"status":"error","error":"..."}` |
| `type_check_in_container` | 同上 |
| `verify_in_container` | `gate_passed` は維持、`status` ＋ `error` を追加 |
| `checkpoint` / `checkpoint_list` / `checkpoint_restore` | `{"error":...}` → `{"status":"error","error":"..."}` |
| `clone_repo` | 同上 |
| `issue_view` / `sandbox_issue_write` | 同上 |
| `list_files` / `read_file_range` | 同上 |
| `publish` | 同上（status ありと混在していたものを統一）|
| `sandbox_exec` / `sandbox_exec_background` / `sandbox_exec_check` | 元から `{"status":"error","error":"..."}` 準拠。変更不要 |
| `package_install` | 同上。変更不要 |

### 6b. 検索結果の出力制御（Issue #469）

`search_in_container` は切り詰めを明示するメタデータを返す:

```json
{"matches": [...], "shown": 20, "total": 150, "truncated": true, "next_offset": 20}
```

| フィールド | 意味 |
|-----------|------|
| `matches` | 該当行の配列（各要素: `file`, `line`, `text`） |
| `shown` | 実際に含めた件数（`max_results` 以下） |
| `total` | 見つかった件数（`max_results` 超過時は推定値を含む） |
| `truncated` | 切り詰めが発生したか |
| `next_offset` | 次のページを取得するためのオフセット（`truncated=true` 時のみ） |

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
  100MB に達すると `journal.log.1` へ自動退避し、ディスク使用量は最大約 200MB に抑制される。
  退避後も同一ファイルへの追記は続かず、新しい `journal.log` が作られる。
  読み取りは両ファイル (`journal.log.1` → `journal.log`) を透過的に結合する。退避より前の履歴も消えない。
- **run_id 単位のリプレイ可能トレース出力（HTML / JSON）**: 事後に「なぜそう動いたか」を共有・レビュー。
  最大 100 ファイルまで保持し、超過時は古いものから削除される。
- **ローカルWebダッシュボード（localhost限定 / read-mostly / 自動更新）**: 稼働コンテナ・run履歴・pass/fail・リソース使用量・承認待ちを一目で。
- **承認キュー＋ワンクリック Approve/Reject（縮小）**: §2.2 の境界越え操作トークンと連動。ダッシュボードの一機能。
- **プッシュ通知（OS通知 / Webhook）**（`notify.py`）: 境界越え操作・失敗閾値超え（既定5回）・長時間実行（既定300秒）のときだけ。OS デスクトップ通知は Linux `notify-send` / macOS `osascript` / Windows PowerShell の3OS対応、Webhook は設定 URL への HTTP POST。閾値・宛先は CLI 引数 `--webhook-url` / `--failure-threshold` / `--long-run-seconds` で調整する。
- **実行前の人間向けプラン表示**: `publish` で対象ブランチ・差分サマリ・上書き有無を提示。

> 注意: ダッシュボードは localhost 限定＋必要なら認証。

### 9.1 ツール別記録マトリクス（Issue #359 対応）

すべてのツールはジャーナルにエントリを記録**しなければならない**。
記録がないツールは監査ギャップ（trace に現れない）と #229 ダッシュボードの
集計不整合（`bypass_rate_pct` の過大評価）の原因となる。

| ツール | 記録種別 | operation | 補足 |
|--------|----------|-----------|------|
| `sandbox_initialize` | `record_initialize` / `record_initialize_complete` | `initialize` / `initialize_complete` | — |
| `sandbox_exec` | `record_exec` | `exec` | — |
| `sandbox_exec_background` | `record_exec` (exit_code=-1) | `exec` | #361 で追加 |
| `sandbox_exec_check` | `record_tool_use` | `tool_use` | #454 で追加。ポーリング自体を可視化 |
| `run_container_and_exec` | `record_initialize` + （内部で `sandbox_exec` 利用）+ `record_stop` | initialize/exec/stop | — |
| `sandbox_stop` | `record_stop` | `stop` | — |
| `write_file_sandbox` | `record_file_write`（内部で `record_file_write` 呼び出し）| `write_file` | — |
| `transform_file` | `record_tool_use`（変更時は加えて `write_file`） | `tool_use` | #454 で追加。従来は変更時しか痕跡が残らなかった |
| `copy_project` | `record_copy` | `copy_project` | — |
| `copy_file` | `record_copy` | `copy_file` | — |
| `read_file_range` | `record_tool_use` | `tool_use` | #359 Tier 3 |
| `list_files` | `record_tool_use` | `tool_use` | #359 Tier 3 |
| `search_in_container` | `record_tool_use` | `tool_use` | #359 Tier 3 |
| `lint_in_container` | `record_tool_use` | `tool_use` | #359 Tier 3 |
| `type_check_in_container` | `record_tool_use` | `tool_use` | #359 Tier 3 |
| `verify_in_container` | `record_tool_use` | `tool_use` | #359 Tier 3 |
| `package_install` | `record_exec` | `exec` | #361 で追加 |
| `issue_view` | `record_boundary_crossing`（approved=None）+ `record_exec`（内部で `gh` 呼び出し）| `boundary_crossing` | 読取専用 VCS |
| `clone_repo` | `record_boundary_crossing`（approved=None）+ `record_exec`（内部の gh clone）| `boundary_crossing` | 読取専用 VCS |
| `checkpoint` | `record_boundary_crossing`（approved=None） | `boundary_crossing` | 表を実装に合わせ修正（#454） |
| `checkpoint_list` | `record_tool_use` | `tool_use` | #454 で追加。読取専用 |
| `checkpoint_restore` | `record_boundary_crossing`（approved=None） | `boundary_crossing` | 表を実装に合わせ修正（#454） |
| `publish` | `record_boundary_crossing` | `boundary_crossing` | 境界越え（write、一発実行） |
| `sandbox_read_journal` | （なし） | — | 読取専用・opt-in（#460） |
| `sandbox_trace` | （なし） | — | 読取専用・opt-in（#460） |
| `sandbox_list_runs` | （なし） | — | 読取専用・opt-in（#460） |
| `sandbox_journal_path` | （なし） | — | 読取専用・opt-in（#460） |
| `sandbox_trace_dir` | （なし） | — | 読取専用・opt-in（#460） |
| `sandbox_issue_write` | `record_boundary_crossing` | `boundary_crossing` | 境界越え（write、一発実行） |

読取専用の journal/trace 5ツールは `CODE_SANDBOX_OBSERVABILITY_TOOLS=1` のときだけ登録される（#460）。記録側（`record_*`）は無条件で動く基盤であり、集計はホスト側で journal.log を直読みすれば足りる。この5ツールは意図的に非計装（#454）: デフォルト無効の観測用デバッグ面であり、journal の読み取りを journal に書くのは自己言及ノイズになる（`sandbox_journal_path` / `sandbox_trace_dir` は container_id 引数自体を持たない）。

テストファイル: `tests/test_journal.py` に対応する単体テストを追加済み（#359 用の
`TestRecordToolUse` クラス）。新しいツールを追加するときは必ずテストも追加すること。

---

## 10. テスト環境クイックパス

V1.0 の棚卸し（#457 / #458）で削除。`run_test_environment` / `stop_test_environment` / `wait_for_condition` は実利用が無いまま休眠していたため、互換シム無しで撤去した（#438 と同方針）。多サービス環境が必要な場合は `sandbox_exec` から docker compose を直接使う。

---

### Environment variables

プロジェクト固有の環境変数は `CODE_SANDBOX_*` prefix に統一する。
`GITHUB_*` / `GH_TOKEN` は GitHub エコシステム標準のため対象外。

旧名（`CSB_*`、`SHIORI_REPOS_PATH`）は V1.0 リリース後に削除予定。
各旧名はフォールバックとして読み取り、使用時は deprecation warning をログ出力する。

| 新名 | 旧名（deprecated） | 用途 |
|---|---|---|
| `CODE_SANDBOX_OBSERVABILITY_TOOLS` | `CSB_OBSERVABILITY_TOOLS` | Observability ツール登録 |
| `CODE_SANDBOX_TOKEN_BROKER_CACHE_DIR` | `CSB_TOKEN_BROKER_CACHE_DIR` | トークンブローカのキャッシュディレクトリ |
| `CODE_SANDBOX_TOKEN_BROKER_NO_DOWNLOAD` | `CSB_TOKEN_BROKER_NO_DOWNLOAD` | トークンブローカのダウンロード抑止 |
| `CODE_SANDBOX_SHIORI_REPOS_PATH` | `SHIORI_REPOS_PATH` | Shiori リポジトリルートへのホストパス |

---

## 11. 外部VCS連携（issue→fix→verify→publish の自己完結）

> 位置づけ: edit/verify ループの**入口（課題取得）と出口（提出）**だけを足す。GitHub MCP を介さず payload をコンテキストに通さないことが唯一の狙い。

**ツール**

- **`issue_view`**（read）: issue 本文をコンテナ内ファイルへ落とし、LLM には**要約＋ハンドル**だけ返す（§3.1）。§2.2 read 扱い（ジャーナル記録・ネットワーク明示許可）。
- **`clone_repo`**（read / 入口）: 対象リポジトリをコンテナ内へ匿名 `git clone`（`repo` / `dest_dir` / `branch`；private は proxy の read 認可ウィンドウで認証、#419）。issue_view と並ぶ作業の起点。`sandbox_initialize(clone_repo=…)` / `run_container_and_exec(clone_repo=…)` でも起動と同時にクローンできる。§2.2 read 扱い（ネットワーク明示許可・ジャーナル記録）。
- **`publish`**（write / 境界越え）: コミット済みの状態を push し、任意で PR を作成する唯一の出口。verify は内蔵せず、LLM が `verify_in_container` で事前に行う。人間ゲートは MCP クライアントのツール承認、構造ガードは egress proxy（§2.2、二段階トークンは #438 で廃止）。§8 ジャーナルに結果を記録。

  **egress proxy 遮断時は Objects API にフォールバックしない（#401）**: git push のエラー出力に `"BLOCKED by egress proxy"` が含まれている場合、`publish` は Objects API（blob→tree→commit→ref）による代替 push を行わず、そのままエラーを返す。これは意図的な設計判断である。API フォールバックはホスト側から api.github.com を直接叩くため proxy をバイパスする — もし proxy 遮断時にフォールバックが発動すると、allowlist 未設定などの構成ミスが隠蔽され、「なぜか API 経由でのみ push される」状態で運用が続くリスクがある。エラーメッセージには `CODE_SANDBOX_ALLOWED_REPOS` の設定が必要である旨のヒントを含める。

**認証（トークンはホスト側に留まる）**

VCS トークンはコンテナに一切注入されない（#439）。read（clone / PR チェックアウト）は egress proxy の read 認可ウィンドウ（#419）でネットワーク層認証し、write（push / PR / issue）は `publish` / `sandbox_issue_write` がホスト側でトークンを解決する。コンテナ自身の `git`/`gh` は常に無認証。

- 最小権限: コンテナにトークンが存在しないので、はぐれた in-container `git push` が漏らす credential がない。
- 漏洩低減: 実行ログは `sanitize_output` の `mask_tokens` で `KEY=***` に自動マスクされる。
- 構造ガード: read / push の認可はホストが proxy に per-grant でトークンを渡す。コンテナは credential を保持しない。

**payload 非通過フロー**

```
issue_view →(要約)→ search_in_container → read_file_range → write_file_sandbox(old_str) | transform_file → lint_in_container → type_check_in_container → verify_in_container → publish
```

issue 本文も差分もコンテナ内に留まり、LLM は run_id / ハンドル / 構造化サマリだけ運ぶ。

**スコープ境界**: 触るのはコンテナ内クローンのみ（§5 維持）。issue 一覧管理・レビュー運用・projects は入れない。

### 11.1 コミット/プッシュの3層モデル

サンドボックス内の git 操作を3層に分ける。各ツールの docstring と挙動判断はこの節を唯一の真実源とする。

| 層 | ツール | token | verify ゲート | 境界 |
|---|---|---|---|---|
| 保存 | `checkpoint` / `checkpoint_list` | 不要 | 無し | コンテナ内 |
| 巻き戻し | `checkpoint_restore` | 不要 | 無し | コンテナ内 |
| 出口 | `publish` | 不要（ホスト側解決） | 無し（LLM が事前に verify_in_container で担保） | GitHub への push |

コンテナ内は使い捨て（§0）。保存・巻き戻し層は token もゲートも要らず、edit/verify ループ中のセーブポイントと巻き戻しに使う。ゲートの対象は境界を越える push だけ（§2.2）。

**出口（`publish`）と transport**

提出ツールは `publish` のみ。`publish` は2つの push transport を内部に持ち、外からは透過:

- 既定: `git push`（credential helper 経由）。
- フォールバック: GitHub Objects API（blob→tree→commit→ref）に `Authorization` ヘッダを直接載せて push。helper を介さないため、helper にトークンを渡せない環境でも成功する。

`git push` が認証配管の都合で失敗したとき、`publish` は自動で API push に切り替える。transport の選択は LLM に露出しない。

**transport 非依存の不変条件**

- force-push はオプトイン。フォールバック時も暗黙には force-push しない。

**checkpoint の squash**

`publish` は常に未 push の checkpoint コミットを1コミットに畳んでから push する（`squash_checkpoints` パラメータは削除済み）。squash ベースは clone/branch 時に記録した分岐点 ref を使い、デフォルトブランチ名に依存せず push を常に fast-forward に保つ。API push 経路は HEAD ツリーを単一コミット化するため、checkpoint は元から残らない。

### 11.2 ホスト側トークン解決の3経路（直交）

ホストがトークンを解決する**供給元**は3経路あり、いずれも直交。解決したトークンはコンテナには入らず、proxy の read / push ウィンドウと `publish` / `sandbox_issue_write` がホスト側で使う。常駐 HTTP（§7）で mcp-launcher 管理外でも認証を維持するために導入された。

| 経路 | 実装 | 仕組み | 用途 |
|---|---|---|---|
| 静的トークン | env 直指定 | `GITHUB_TOKEN` をそのまま注入 | 既定・最小構成 |
| GitHub App 自己管理 | `github_auth.py` | `AppTokenProvider` が Installation Token を発行・キャッシュし、daemon スレッドで定期リフレッシュ（`setup_github_app_token()`） | 秘密鍵をホストに置き、長時間常駐でも短期トークンを切らさない（PR #223） |
| トークンブローカー | `token_broker.py` | `GITHUB_TOKEN_COMMAND` / `GITHUB_TOKEN_BROKER_SERVICE` 指定時に pin 済み keystore-broker CLI で新鮮な短期トークンを mint（バイナリ pin＋SHA-256 検証＋platformdirs キャッシュ） | 秘密鍵をホストに置かない第3経路（PR #235） |

**優先順位**: ブローカー mint 成功時は静的 `GITHUB_TOKEN` より優先し、失敗時は静的トークンへ fallback する（`token_broker.mint_token()` → `_resolve_vcs_token()`）。いずれも未設定ならトークン無しで、read は匿名クローン、push は credential 無しでクリーンに失敗する。

---

## 12. ベースイメージ（全部入り）

> **更新（#104 / #313）**: 単一の全部入り `docker/Dockerfile.sandbox` は廃止し、
> `docker/Dockerfile.{base,python,go}` に分割した（`docs/design-multilang-support.md` §6/§7）。
> 既定イメージは python 固定ではなく**検出ベースで選択**し、不明時は中立 `sandbox:base` に fallback する。
> 以下の表は base + 各 backend が同梱するツールの総体を示す。

ツール同梱はイメージの責務。MCP のツール数は増やさない。`docker/Dockerfile.{base,python,go}` で管理。

| カテゴリ | ツール | 用途 |
|---------|--------|------|
| 検索（字句） | `ripgrep` | `search_in_container` lexical モード |
| 検索・書換（構造） | `ast-grep` | `search_in_container` structural モード / rewrite |
| テキスト置換 | `sd` | 設定・Markdown 含む全テキスト（二次） |
| ファイル検索 | `fd` | find 代替 |
| シンボル | `ctags` | 任意（需要次第） |
| lint | `ruff` | Python lint + autofix |
| 型検査 | `pyright` | Python 型検査（mypy も可） |
| VCS | `git` / `gh` | clone / push / issue_view |
| 高速インストール | `uv` | pip 代替（タイムアウト対策） |
| JSON処理 | `jq` | テスト結果JSON等のパース補助 |

---

## 13. Dockerライフサイクル（必要最低限）

セッション（コンテナ＋FS＋run履歴）のライフサイクル操作。実装済みのツールは以下。

- `sandbox_initialize`（最重要 / 起動）: コンテナ起動。**async ラッパー**で、image pull / clone / pip install / PR checkout など低速セットアップ中に MCP progress notification を発行し、HTTP・クライアントタイムアウトでコンテナが孤立するのを防ぐ（#298）。
- `run_container_and_exec`: 起動＋実行のワンショット。
- `sandbox_exec`: 稼働コンテナでコマンド実行（`commands` / `argv` の2モード）。
- `sandbox_exec_background` / `sandbox_exec_check`: 長時間コマンドの非同期実行と完了確認。
- `sandbox_stop`: コンテナ停止・削除（未 push の checkpoint があれば警告）。

**Dockerfile 管理**: `docker/Dockerfile.{base,python,go}` （§12 参照）。CI でダイジェスト固定タグをビルド・管理。


## まとめ

基本思想（全部見せない / 構造を返す / 人間の最終制御 / できないことの保証）を採用し、コード理解レイヤーの**自前実装（ストア・インデックスの所有）**だけを切る（CLI は可）。
人間の最終制御は、**静的ガードレール（§2）＋ 境界越え操作のトークン必須（§2.2、外部VCS含む）＋ 全実行の事後監査ジャーナル（§9）** で担保する。
payload はコンテナ内に留め、LLM は制御だけ運ぶ。
AI が暴走しても被害は使い捨てコンテナ内に留まり、ホストは常に安全。
目標は一貫して——**AIが最小限のコンテキストで最大限の判断を行える、安全なテスト・検証・提出の基盤**。
