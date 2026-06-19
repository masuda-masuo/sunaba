# Code Sandbox MCP — 多言語サポート設計（verify ディスパッチ化 / イメージ分割）

> 位置づけ: 既存 `docs/design.md` の §4（構造化テスト）・§5（Edit/Verify）・§12（ベースイメージ）を、
> 設計意図（pytest / jest / go test の3言語）に実装を追従させるための補助設計。
> 新機能の追加ではなく、**「言語サポートを単一イメージから切り離し、検証の取りこぼし（無音失敗）を無くす」** ことが目的。

---

## 1. 背景・問題

`docs/design.md` §4 は v1 で **pytest / jest / go test の3つを"ちゃんと"対応**すると明記している。
しかし実装は Python に固まっており、設計意図に追いついていない。

- **パーサは多言語、実行系が Python 固定**: `test_report.py` は `PytestAdapter` / `JestAdapter` / `GoTestAdapter` を
  実装済み（3言語対応）。一方 `edit_verify.py` の `run_verify` は言語判定を一切持たず、
  `ruff` / `pyright` / `python3 -m pytest` / `semgrep --config p/python` を無条件で呼ぶ。
  パーサが宙に浮いている状態。
- **ベースイメージに node / go が無い**: `docker/Dockerfile.sandbox` は `python:3.12-slim` ベースで、
  node ランタイムも go ツールチェーンも入っていない。jest / go test を回す実行環境が存在しない。
- **pyright の潜在バグ**: pyright は `uv tool install pyright`（PyPI ラッパー）で導入されており、実体は node アプリ。
  node が無い場合は初回実行時に取得を試みるが、サンドボックスは §2 でネットワーク off が既定のため、
  Python の型検査自体が不安定になりうる。`HEALTHCHECK` も `rg` / `sg` / `semgrep` しか確認していないため、
  pyright / ruff / pytest が壊れても検知できない。
- **無音失敗（気づかないエラー）**: ここが最も危険。
  - 全 runner が `... 2>/dev/null || true` で stderr を捨て exit を 0 に潰すため、
    「クリーンで所見ゼロ」と「ツールが落ちて出力ゼロ」が区別できない。空 → 所見 0 → 緑。
  - `_run_pytest_verify` は出力空でも parse 例外でも `status: skipped` を返す。
    skipped は `gate_on_test_fail` の対象外（gate は `status == "failed"` のみ判定）。
    **テストが一度も走っていないのに submit ゲートが通る。**
  - 127（ツール未導入）は `no-linter` / `no-typechecker` / `no-scanner`（severity `info`）になり、
    gate が明示的に除外する。Python 固定と相まって、JS / Go プロジェクトは全ツールが空振りし、
    **検証していないのに全部緑で通る。**

---

## 2. 方針

1. **node / js はほぼベース層、backend が変数**。js はどの backend（py / go / java / ruby …）とも同居する
   横断レイヤであり、「3つのうちの1つ」ではない。
2. **ディスパッチ表は全言語一様、イメージは部分集合を提供する**。検出は py / js / ts / go を一様に知り、
   イメージはそのうち実際に同梱しているツールだけを持つ。欠けは一級の `not_available` として必ず可視化する。
3. **肥大の歯止めルール**: 「横断インフラが依存するランタイムは base へ。言語固有の開発ツール群は backend レイヤへ。」
   これにより base が再び monolith に膨れるのを防ぐ。
4. **無音失敗の根絶**: 各検証層は所見配列ではなく status を必ず返す。未検証 / エラーは決して緑にしない。

---

## 3. 言語検出ルール

verify は `path`（ファイル or ディレクトリ）を受け、それを「言語（複数可）→ ツールセット」へ写す。
**検出**と「**そのイメージで実行可能か**」は別軸として扱う。

検出の優先順位（primary は先勝ち。ただし polyglot 用に全マッチを集合で集める）:

1. **明示 `language=` 指定** → 検出スキップ。手動エスケープハッチで最優先。
2. **path がファイル** → 拡張子マップ。
   `.py`→python / `.js,.jsx,.mjs,.cjs`→js / `.ts,.tsx`→ts / `.go`→go。
   ts 系は `tsconfig.json` を上方探索する（tsc はプロジェクト前提）。
3. **path がディレクトリ** → マーカーファイルを走査し集合で返す。
   - `go.mod` → go（確定）
   - `package.json` → js。`tsconfig.json` があれば ts。中身を見て jest / vitest を判定。
   - `pyproject.toml` / `setup.py` / `requirements*.txt` / `Pipfile` / `tox.ini` → python
   - 走査からは `node_modules` / `.venv` / `vendor` / `dist` / `build` を除外。
4. **複数マーカー** → polyglot。集合を返し、各ツールチェーンを**マーカーのあるサブツリーにスコープ**して回す
   （root の `pyproject.toml` と `frontend/package.json` を別々に扱う）。
5. **どれにも当たらない** → unknown。verify は「認識できるプロジェクトマーカー無し。`language=` で強制可」と返して
   **スキップ**する。Python へのフォールバックはしない。

---

## 4. verify status モデル

各検証層（lint / type / test / scan）の返りを、所見配列ではなく **status 封筒**にする。

```
{
  "tool": "ruff",
  "status": "ok" | "findings" | "not_available" | "error" | "skipped",
  "findings": [ { "file", "line", "rule", "severity", "message" }, ... ],
  "detail": "...",      # error の理由 / skipped の理由
  "exit_code": <int>
}
```

| status | 意味 |
|--------|------|
| `ok` | 走って exit 0、所見なし |
| `findings` | 走って所見あり（gate するかは severity 次第） |
| `not_available` | イメージにツール無し（exit 127）。緑に混ぜず独立表示 |
| `error` | 走ったが解釈不能（想定外 exit / stderr あり / parse 失敗）。**必ず表に出す** |
| `skipped` | 意図的に未実行（go に型層が無い、テスト 0 件など）。理由必須 |

**gate との接続**:

- **submit ゲート = strict**: 検出した言語に必要な層のどれかが `not_available` か `error` なら
  `gate_passed = false`、理由 `"verification incomplete: <tool> <status>"`。**未検証コードは push させない。**
- **対話的 verify = lenient + warn**: 通してよいが `incomplete: true` を目立つ形で必ず返す（不可視にしない）。

「4列（lint / type / test / scan）をすべての言語でそろえる」ことはしない。
go のように層が対応しない場合は、欠落させず `skipped`（理由: build/vet が兼ねる）で**明示的に埋める**。

---

## 5. 中途半端の整理（クリーンアップ）

1. `|| true` / `2>/dev/null` の一律握り潰しを廃止。exit と stderr を捕って status を決める。
   lint の exit 1（= 所見あり）は正常として区別する。
2. **runner の二系統を統合**。`lint_file` 系（拡張子 dispatch + fallback あり）と
   `_run_*_verify` 系（dispatch なし・Python 固定・fallback なし）が別物になっている。
   1 つの dispatch する runner + status 封筒に統合し、verify も単体ツールも同じものを呼ぶ。
3. **検出を `run_verify` の前段に挿入**し、言語ごとに層を選ぶ（§3・§4）。
4. **semgrep の `p/python` 固定を解消**。検出言語に合わせて `p/python` / `p/javascript` / `p/go` … に振る。
5. **unknown 言語の Python フォールバックを廃止**。`skipped`（unrecognized）とし、
   submit は `language=` 明示が無ければ通さない。
6. `HEALTHCHECK` を各イメージの保有ツールに合わせる（壊れたツールを隠さない）。

---

## 6. イメージ / タグ設計

### レイヤ構成（`FROM` チェーン）

- **base** = 言語非依存 CLI（rg, sg, fd, sd, ctags, git, gh, jq, uv）＋ **python-runtime ＋ node-runtime** ＋ semgrep。
  semgrep（多言語スキャナ＝横断インフラ）は python 実装、pyright（py 層のツール）は node アプリのため、
  両ランタイムは「横断インフラの依存」として正当に base 入り。base への **node 追加**が pyright バグ（§1）も解消する。
- **backend レイヤ**（base の上に `FROM` で重ねる。開発ツール群のみ）:
  - python: ruff, pyright, pytest + pytest-json-report
  - js/ts: eslint, typescript(tsc), jest
  - go: go ツールチェーン

### 公開タグ（すべて `image@sha256` で digest 固定）

| タグ | 構成 | 用途 |
|------|------|------|
| `sandbox:base` | base | 共有親（直接使わず `FROM` 元） |
| `sandbox:python` | base + python + js | `sandbox_initialize` の既定。py 単体 / py+js モノレポ |
| `sandbox:go` | base + go (+ js) | go 単体 / go+js |
| `sandbox:full` | base + python + go + js | 混在モノレポ用。opt-in（既定にしない） |
| `sandbox:minimal` | 既存 git+python | 高速起動枠（温存） |

js を各 backend タグに同梱するのは意図的。「backend + js フロント」というポリグロットこそ
1 イメージが勝つケースだから。純 go 等で js が不要なら後で `sandbox:go-slim` を追加する（既定はバンドル）。

### 検出 ↔ イメージの結線（loud-failure 契約）

検出 = go、起動イメージ = `sandbox:python`（go なし）の場合:
go ツールが 127 → `not_available` → verify `incomplete: true` → submit ゲートは strict なので fail、
理由「detected go / このイメージ（`sandbox:python@sha…`）に go ツールチェーン無し → `sandbox:go` で再初期化」。
緑にはならない。

（任意）`sandbox_initialize` 時に先回り検出し、`go.mod` があるのに `sandbox:python` を指定したら
警告 or 適合タグを提案する。verify より早くミスマッチを可視化できる。

---

## 7. 既存ファイルとの対応・移行

- `docker/Dockerfile.sandbox`（Python 固定の全部入り）→ `Dockerfile.base` ＋ `Dockerfile.python` に分割。
  ruff / pyright / pytest は python 層へ、非依存 CLI と両ランタイムは base へ、**base に node-runtime を追加**。
- `docker/Dockerfile.sandbox.minimal` → `sandbox:minimal` として温存。
- CI: base を先にビルド → digest 確定 → 子イメージは固定 base に対してビルド → 全部 `@sha256` で publish。
  server の既定参照も `:latest` ではなく digest で（Dockerfile ヘッダの方針どおり）。

---

## 8. 実装順

1. **イメージ分割 + node 追加**: `Dockerfile.base` / `Dockerfile.python` / `Dockerfile.go`、CI のタグ別ビルド。
2. **`edit_verify` の refactor**: status 封筒（§4）＋ 言語 dispatch（§3）。runner 二系統の統合（§5-2）。
3. **検出モジュール新設**: §3 の検出ルールを独立モジュールに。
4. semgrep の言語別 config（§5-4）、`HEALTHCHECK` 修正（§5-6）。

依存的には 1 → 2/3 → 4 の順が素直。

---

## 9. 非目標（スコープ外）

- Java / Ruby / Rust 等の追加対応（本設計は py / js / go の3言語で凍結。将来は backend レイヤ追加で拡張）。
- 実行時のオンデマンド導入（`apt` / `npm install` 等）。§2 のネットワーク off 既定・§7 のタイムアウト・再現性と
  衝突するため採らない。言語追加は**バリアントイメージ**で行う。
- スナップショット系・ネットワーク系の拡張（既存 `docs/design.md` の方針を踏襲）。
