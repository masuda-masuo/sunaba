# Code Sandbox MCP — #131 MCPツールUX改善 方針

> 位置づけ: [issue #131](https://github.com/masuda-masuo/code-sandbox-mcp/issues/131)
> （`read_file_range` エラー / `sandbox_exec` の日本語parse・timeout / `clone_repo` の既存dest）
> に対する対応方針。issue #120 の修正作業中にヒットした3つの UX 問題を整理し、
> 「実バグ」「機能追加」「クライアント側の制約（docで明記）」に切り分けて確定したもの。

---

## 1. 背景

issue #120 の作業中、MCPツールの利用で3つの問題にヒットした。
ただし現 main のコードを精査した結果、3問題は性質が異なる:

- **①は現 main で既に解消済み**（古いビルドの残骸）。
- **③が唯一の実バグ**（コマンドと報告 `clone_path` の矛盾）。
- **②は2つに分かれ**、片方はサーバ側で直せないクライアント制約、片方は機能不足。

---

## 2. 問題ごとの実態と方針

### ① `read_file_range` の `name 'container' is not defined`

**実態**: 現 main では解消済み。`read_file_range`
（`src/code_sandbox_mcp/server.py` の `read_file_range`）は
`client.containers.get()` の戻り値を `read_file_lines()` に渡しており、
`container` 未定義参照はコード上どこにも存在しない。
issue は #120 作業時の古いサーバビルドでヒットした残骸と判断。

**方針**: コード修正は不要。**回帰テストを1本追加**し、
`name 'container' is not defined` が再発しないことを担保したうえで「修正済み」としてクローズ参照する。

### ② `sandbox_exec` のマルチバイト+改行で JSON parse エラー / `timeout` 拒否

2つの別問題が混在している。

**②-a 「`JSON Parse error: Unterminated string`」**
→ **サーバ側では直せない**。原因は MCP クライアント（LLM）が
ツール呼び出しの JSON 文字列引数に**生の改行**を埋め込んだことによるシリアライズ崩れ。
サーバ側の `sandbox_exec` は受け取ったコマンドを base64 エンコードしてから
コンテナに渡しており、コマンド本体が正しく届けばマルチバイトは問題なく通る。
→ **doc に注意書き＋回避策を明記するのみ**。
回避策: 生改行を避ける／コマンドを base64 で渡す／`-F file` でファイル経由にする。

**②-b 「`timeout` が `Unexpected keyword argument`」**
→ `sandbox_exec` に `timeout` 引数が存在しないため。
→ **`timeout` パラメータを追加**する（妥当な機能追加）。

### ③ `clone_repo` が dest_dir 存在時に失敗 — 唯一の実バグ

**実態**: `clone_repo` は `gh repo clone {repo} {dest_dir}` を実行する。
`gh repo clone` は第2引数を「クローン先ディレクトリそのもの」として扱うため、
デフォルト `dest_dir=/home/sandbox`（既存・非空）に直接展開しようとして
`destination path '/home/sandbox' already exists and is not an empty directory.` で失敗する。
一方コードは `clone_path = {dest_dir}/{repo_name}` と**サブディレクトリ前提で報告**しており、
コマンドと報告が矛盾している。

**方針**:
- クローン先を `{dest_dir}/{repo_name}` に統一する。コマンドを
  `gh repo clone {repo} {dest_dir}/{repo_name}` とし、報告 `clone_path` と一致させる。
- クローン先が既存・非空のときは git の生メッセージではなく、
  「`<path>` already exists — 別の dest_dir を指定してください」相当のヒント付きエラーに整形する。

---

## 3. PR分割

バグ系と feature/doc 系で2分割する（責務を分けレビューしやすくするため）。

### PR-A: `clone_repo` バグ修正（バグ系）

- ③の修正: クローン先を `{dest_dir}/{repo_name}` に統一し、コマンドと報告を一致させる。
- ③のエラー改善: 既存・非空時のヒント付きエラー整形。
- ①の回帰テストを1本追加（`name 'container' is not defined` の再発防止）。
- issue①は「修正済み」、③は「本PRで修正」としてクローズ参照。

### PR-B: `sandbox_exec` timeout 追加 + doc（feature/doc系）

- ②-b: `sandbox_exec` に `timeout` パラメータを実装。
- ②-a: docstring / README に「マルチバイト＋生改行は MCP クライアント側の制約で
  サーバでは直せない」旨と回避策（生改行回避 / base64 / `-F` ファイル渡し）を明記。

---

## 4. スコープ外

- ②-a のクライアント側 JSON シリアライズ問題そのものの修正
  （MCP クライアント／LLM 側の責務であり本リポジトリでは対処不能）。
