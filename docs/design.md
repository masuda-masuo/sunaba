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
- [x] **run間diff**: 前回比で変化点だけ返す（`sandbox_exec_diff`）。
- [x] **失敗のフィンガープリント＋重複圧縮**: 同型失敗は `×N` に畳む（`compress_failures`）。
- [x] **`rerun_failed(run_id)` / 影響範囲の絞り込み再実行**: 失敗分・変更ファイルが影響するテストだけ実行。
- [x] **コンテンツアドレスな結果キャッシュ**: image＋コマンド＋入力ハッシュが不変なら `cached: true`（`result_cache.py`）。
  - **キャッシュ管理ツール**: `sandbox_cache_stats`（ヒット率・エントリ数の統計）/ `sandbox_cache_invalidate`（`key` 指定 or 全件無効化）。キャッシュが古い結果を返すときの手動リセット経路。
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
- **プッシュ通知（OS通知 / Webhook）**（`notify.py`）: 境界越え操作・失敗閾値超え（既定5回）・長時間実行（既定300秒）のときだけ。OS デスクトップ通知は Linux `notify-send` / macOS `osascript` / Windows PowerShell の3OS対応、Webhook は設定 URL への HTTP POST。閾値・宛先は CLI 引数 `--webhook-url` / `--failure-threshold` / `--long-run-seconds` で調整する。
- **実行前の人間向けプラン表示**: `publish` で対象ブランチ・差分サマリ・上書き有無を提示。

> 注意: ダッシュボードは localhost 限定＋必要なら認証。

---

## 10. テスト環境クイックパス

- `run_test_environment`: Compose相当の環境を一括起動。ネットワーク作成・ヘルスチェック待機・後片付けを自動化し、各サービスのアクセス先を返す。
- `wait_for_condition`: TCPポート開放 / ログ内文字列 / healthy を条件に待機（タイムアウト付き）。AIによる `sleep 30` 乱用を排除。

---

## 11. 外部VCS連携（issue→fix→verify→publish の自己完結）

> 位置づけ: edit/verify ループの**入口（課題取得）と出口（提出）**だけを足す。GitHub MCP を介さず payload をコンテキストに通さないことが唯一の狙い。

**ツール**

- **`issue_view`**（read）: issue 本文をコンテナ内ファイルへ落とし、LLM には**要約＋ハンドル**だけ返す（§3.1）。§2.2 read 扱い（ジャーナル記録・ネットワーク明示許可）。
- **`clone_repo`**（read / 入口）: `gh repo clone` で対象リポジトリをコンテナ内へクローン（`repo` / `dest_dir` / `branch`）。issue_view と並ぶ作業の起点。`sandbox_initialize(clone_repo=…)` / `run_container_and_exec(clone_repo=…)` でも起動と同時にクローンできる。§2.2 read 扱い（ネットワーク明示許可・ジャーナル記録）。
- **`publish`**（write / 境界越え）: コミット済みの状態を push し、任意で PR を作成する唯一の出口。verify は内蔵せず、LLM が `verify_in_container` で事前に行う。§2.2 の二段階トークン必須、§8 ジャーナルにプランと結果を記録。

**認証（opt-in トークン注入）**

VCS トークンは `inject_vcs_token=True` を指定したコンテナにのみ `GITHUB_TOKEN` / `GITHUB_TOKEN_SOURCE` / `GH_TOKEN` として注入される。既定はトークン無し。

- 最小権限: VCS 不要のコンテナにトークンが渡らない。
- 漏洩低減: 実行ログは `sanitize_output` の `mask_tokens` で `KEY=***` に自動マスクされる。
- read 用途と write 用途で別コンテナに分けられる。

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
| 出口 | `publish` | 必須 | 無し（LLM が事前に verify_in_container で担保） | GitHub への push |

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

### 11.2 トークン供給の3経路（直交）

`inject_vcs_token=True` でコンテナに渡る `GITHUB_TOKEN` の**供給元**は3経路あり、いずれも opt-in・直交。常駐 HTTP（§7）で mcp-launcher 管理外でも認証を維持するために導入された。

| 経路 | 実装 | 仕組み | 用途 |
|---|---|---|---|
| 静的トークン | env 直指定 | `GITHUB_TOKEN` をそのまま注入 | 既定・最小構成 |
| GitHub App 自己管理 | `github_auth.py` | `AppTokenProvider` が Installation Token を発行・キャッシュし、daemon スレッドで定期リフレッシュ（`setup_github_app_token()`） | 秘密鍵をホストに置き、長時間常駐でも短期トークンを切らさない（PR #223） |
| トークンブローカー | `token_broker.py` | `GITHUB_TOKEN_COMMAND` / `GITHUB_TOKEN_BROKER_SERVICE` 指定時に pin 済み keystore-broker CLI で新鮮な短期トークンを mint（バイナリ pin＋SHA-256 検証＋platformdirs キャッシュ） | 秘密鍵をホストに置かない第3経路（PR #235） |

**優先順位**: ブローカー mint 成功時は静的 `GITHUB_TOKEN` より優先し、失敗時は従来の静的トークンへ fallback する（`_container_env()`）。GitHub App env / ブローカー env のいずれも未設定なら完全 no-op で、既存の静的注入の挙動を変えない（後方互換）。

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
- `sandbox_exec_diff` / `rerun_failed`: 前 run 比の差分、失敗・影響範囲の絞り込み再実行（§3.2）。
- `run_test_environment` / `stop_test_environment` / `wait_for_condition`: Compose 相当環境の起動・停止・条件待機（§10）。
- `sandbox_stop`: コンテナ停止・削除（未 push の checkpoint があれば警告）。

> 旧構想の `exec_in_container` / `inspect_container` / `build_image` は名前のみで未実装。exec の実体は `sandbox_exec`、ワンショットは `run_container_and_exec` が担う。生成 Dockerfile の即時検証（旧 `build_image`）は現状スコープ外。

**Dockerfile 管理**（リポジトリ内 `docker/` で管理・CI でダイジェスト固定タグをビルド）:
- `docker/Dockerfile.sandbox` — §11 の全部入りイメージ
- `docker/Dockerfile.sandbox.minimal` — git + python のみ（軽量・高速起動優先）

---

## 14. 推奨実装順

| Phase | 内容 | 狙い |
|------|------|------|
| **0** | セキュリティ土台 §2.1（マージ済み）＋ 境界越え操作のトークン必須 §2.2（外部VCS含む） | 機能ではなく前提 |
| **1** | 出力制御（§6）＋ `run_container_and_exec` | トークン削減ROI最大 |
| **2** | 構造化テスト結果 pytest/jest/go（§4） | AI-firstの本丸 |
| **2+** | Edit/Verify コア（§5）: `search_in_container`（lexical/structural）＋ `verify`（束ね・強制ゲート）＋ stdout バグ修正（#52） | 失敗→修正→再検証ループを閉じる |
| **4** | `run_test_environment`＋`wait_for_condition`（§10） | `sleep 30`撲滅 |
| **5** | 外部VCS連携（§11）: `issue_view` + `publish` | issue→push をコンテキスト非通過で自己完結 |
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
