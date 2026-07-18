# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
The compatibility policy (what counts as a breaking change) is described in
[README.md#compatibility-policy](README.md#compatibility-policy).

## [Unreleased]

## [0.10.2] - 2026-07-18

### Changed

- **`edit_verify.py` 解体と `search.py` 抽出（EPIC #663 完遂）**（#664–#668）: 肥大化した `edit_verify.py`（3203行）を13モジュールへ分割し、`search.py` を独立モジュールとして抽出。**挙動不変の内部リファクタリング。**

## [0.10.1] - 2026-07-18

### Added

- **publish/checkpoint の未追跡ファイル可視化**（#658）: publish と checkpoint が `git add -A` で巻き込む未追跡ファイルを、結果JSONの `swept_untracked`（相対パス配列）として列挙するようになった。自動除外はしない（可視化のみ）。push挙動は不変。`typings/*.pyi` のような意図しないゴミの混入をpush前に目視できる。
- **js/ts verify が `package.json` の `scripts.test` を尊重**（#643）: js/ts の verify dispatch が、プロジェクトの `package.json` に定義された `scripts.test` を優先して実行するようになった。

### Removed

- **auto-checkpoint (#586) を撤去**（#657）: `write_file`/`transform_file`/明示 `checkpoint` 完了時に裏で `git add -A && git commit` を自動実行する機構を撤去した。2つの害（(1) 編集ごとの自動commitで `git status` が常にクリーンになりレビューでdiffが見えない、(2) `git add -A` が未追跡ゴミを巻き込みpublishのsquashを通ってpushされる）を解消。編集単位のロールバックは既存の `undo_file_edit` がカバーするため機能損失はない。明示 `checkpoint` ツールは維持。

### Fixed

- **新規ファイルが root 所有になる不具合**（#642）: 新規作成ファイルの所有者判定が root 所有の `/proc/self` symlink を辿ってしまい、コンテナ内で作られたファイルが root 所有になっていた問題を修正（`id -u`/`id -g` ベースに変更）。

### Changed

- **`tools/` のモジュール構造整理（EPIC #646）**（#647–#651）: 肥大化した `tools/*.py`（container.py / vcs.py / file.py）を責務別のサブパッケージ・純粋モジュールへ分割（edit_engine / github_api / container パッケージ / publish_planner / publish_ops / vcs サブパッケージ）。**挙動不変の内部リファクタ。**

## [0.10.0] - 2026-07-17

### Added

- **js/ts toolchain baked in; verify's js dispatch now actually runs**
  (#588). `edit_verify` had eslint/tsc/jest adapters since #493, but no
  image shipped the binaries, so js/ts verify always returned
  `not_available`. `docker/install-js-tools.sh` (mirroring
  `install-python-tools.sh` / `install-go.sh`, #584) bakes eslint,
  typescript, and jest into `sandbox:full` (the runtime default) and the
  new lean `sandbox:js` image; both `HEALTHCHECK`s assert the three
  tools, and a new test (`tests/test_image_pins.py`) checks the
  dispatch-matrix-⊆-healthcheck contract statically so a bake omission
  fails CI instead of a user's first verify. **Repo-pinned tools win
  over the baked global**: unlike Python's `pip install -e .[dev]`
  (which writes into the same venv already on `PATH`), node has no such
  mechanism, so a globally baked eslint 9 hitting a repo pinned to
  eslint 8 would silently lint with the wrong version. The eslint/tsc/jest
  runners now resolve `node_modules/.bin/<tool>` first, falling back to
  the image-baked global only when absent, and always record which one
  ran in the `VerifyResult.detail` field (as JSON fields for jest, since
  its `detail` carries a machine-parsed test report; as a text prefix
  for eslint/tsc). Jest vs Vitest is discriminated by reading
  `package.json` before invoking anything; a vitest-only project reports
  a clear `skipped` status instead of being forced through the jest CLI
  (no `VitestAdapter` yet — tracked as a follow-up).
- **Per-edit undo: `undo_file_edit` tool** — every `write_file_sandbox` /
  `transform_file` edit now snapshots the pre-edit file automatically
  (host-side under `~/.sunaba/undo/`, bounded ring of 10 versions per file,
  files over 5 MB skipped, history cleared on container stop). The new
  `undo_file_edit(container_id, file_path, steps=1)` tool restores the state
  `steps` edits back; the replaced content is snapshotted too, so an undo is
  redoable. Rationale: when an LLM breaks a file, it tends to keep "fixing"
  the broken text forward and spirals; a guaranteed way back to the pre-edit
  state breaks that loop. The `.py` parse-regression warning now points at
  `undo_file_edit` as the first recovery step. (#599)
- **`write_file_sandbox` anti-loop guards** (the callers are LLMs — a mistake
  must come back as an actionable message, and dead ends must offer an exit):
  a failed `old_str` match whose `file_contents` already exists in the file now
  says "this edit may have already been applied" (the most common retry loop:
  repeating an edit that already landed); a `.py` edit that leaves the file
  unparseable gets a warning in the success echo pointing at escaping artifacts
  (instead of surfacing later at verify time); multi-match and near-miss errors
  suggest `transform_file` as the pattern-based escape hatch. (#599)
- **`edit_file`: explicit `ast` override, plus `preserve` / `line`** — the
  implicit rule (a `.py` `old_str` that looks like a `def`/`class` signature
  triggers AST resolution and replaces the whole definition) silently
  rewrote the entire function even when the caller only meant to change its
  docstring (hit for real against shiori#287). `ast=True` forces AST
  resolution and errors instead of falling back when it fails; `ast=False`
  disables it and forces a plain string match even for a definition-shaped
  `old_str`; `ast=None` (default) keeps the old implicit behaviour. AST
  replacements also gained `preserve` (`"decorators+docstring"` (default),
  `"decorators"`, `"docstring"`, or `"none"`) to keep parts of the old
  definition that a full-body replacement would otherwise clobber, and
  `line` to disambiguate same-named definitions. Both parameters, and the
  AST resolution path itself, were carried over from the short-lived
  `edit_symbol` tool, which is retired — this is the same consolidation
  described under *Changed* below. (#632, #598)
- **`diff_in_container`: `worktree` parameter** — the tool only ever
  diffed base↔HEAD (committed history), so uncommitted working-tree
  changes (modified or untracked) were invisible; on a checkpoint-less
  session an agent's own uncommitted edits showed up as "no changes" and
  were misread as not having happened (hit for real while running
  opencode-plugin-cc #17: a sub-agent's 3 edited files were invisible
  before its first checkpoint). `worktree=True` ignores `base` and diffs
  HEAD against the working tree instead (untracked files are hunk-diffed
  via `git diff --no-index` in file mode, and listed with
  `status: "untracked"` in summary mode); the existing base↔HEAD behaviour
  is unchanged when `worktree` is omitted. (#633)
- **Auto-checkpoint on edit operations** — a successful `write_file` /
  `edit_file` / `transform_file` now creates a process-local `[auto]`
  checkpoint automatically, so uncommitted work survives a failed
  `publish` instead of being lost to a network hiccup or a forgotten
  manual `checkpoint`. Unpushed `[auto]` commits are squashed by
  `publish`'s existing `git reset --soft @{u}`, so they never surface in
  the repo's real history. Motivated by #550's finding that LLM callers
  read past checkpoint recommendations rather than act on them — this
  replaces the recommendation with a mechanism. (#586)
- **`issue_view`: `include_comments` to fetch the discussion thread**
  (default `False`, backward compatible) — the tool previously saved only
  the issue body, but the settled spec, design decisions, and rejection
  rationale usually live in the comments (this repo's own #580/#581 are
  an example), so body-only reads risk mistaking an early proposal for
  the agreed plan. Comments are fetched host-side (works for private
  repos), auto-paginated, and appended as a `## Comments` section with
  author and timestamp; `max_comments` (default 30) keeps only the newest
  when a thread is long. (#585)
- **`issue_view`: PR review comments and reviews** — when the target is a
  PR, `include_comments` now also fetches inline code-review comments
  (`/pulls/{n}/comments`) and review summaries (`/pulls/{n}/reviews`), not
  just the general issue comments. A PR with 17 GitHub-UI comments
  previously surfaced only the 1 general comment; the other 16 (inline
  review feedback) were invisible. Fetched items are merged and shown in
  chronological order with review state (`APPROVED` /
  `CHANGES_REQUESTED`) and file location where applicable; a PR API
  failure falls back silently (non-fatal). (#611)
- **`verify_in_container`: `recommended_next_action: "publish"` on gate
  success** — now that `publish` hard-blocks without a recorded verify
  pass (#615 below), a passing gate nudges the agent toward `publish`
  next so the pass isn't forgotten and hit as a block in a later session.
  Advisory only, per the nudge pattern from #550. (#619)

### Changed

- **BREAKING: `write_file_sandbox` split into `write_file` + `edit_file`**
  (issue #630). The tools are partitioned by *intent*, not mechanism:
  `write_file` creates a file or overwrites it wholesale (no partial-update
  parameters at all); `edit_file` modifies an existing file with exactly one
  edit mode per call — `old_str` replacement (with the `.py` AST resolution),
  `start_line`/`end_line` range, or `append=True`. `edit_file` rejects calls
  on missing files (pointing at `write_file`) and calls with no mode
  (pointing at the mode list). Rationale: the pre-consolidation surface
  (`write_file` with `old_str` + `edit_symbol`) starved the specific tool
  because two tools shared the intent "modify existing code"; splitting by
  intent (create vs modify) matches the Write/Edit shape LLMs are trained
  on. Both tools journal their use (`write_file` logs an
  `overwrote_existing` flag) so full-overwrite-of-existing-file rates can be
  measured before deciding on a guard. Hard cut, no alias (pre-1.0, #438
  precedent).
- **`sandbox:full` is the default sandbox image; host-side language
  auto-detection is removed** — `sandbox_initialize` used to probe the
  GitHub contents API to guess python/go/base before starting the
  container, but the image is immutable once the container exists, so a
  wrong guess (or a failed probe) silently produced a container missing
  the toolchain the project actually needed. `sandbox_initialize` now
  always starts `sandbox:full` (Python + Go + Node — every toolchain
  `verify` can dispatch to) unless `image=` is explicit; language
  detection still happens, but inside the container at verify time, where
  it reads the real files and can be safely re-run if it guesses wrong.
  Each image's `HEALTHCHECK` now asserts the tools it owes `verify`, so a
  missing tool fails a CI build instead of a user's first verify
  (`sandbox:full` also ships the `pytest-json-report` plugin needed for
  `verify_in_container`'s own JSON parsing, which a bare `pip_extras=[dev]`
  install didn't provide). (#584)
- **`/workspace` is now actually the repo root and the container's working
  directory** — the design doc always said so, but the code cloned into
  `/tmp/repo/{name}`, ran `copy_project` into `/home/sandbox`, and set the
  images' `WORKDIR` to `/home/sandbox`; every exec had to rediscover the
  real root, and a call that forgot `working_dir=` silently ran outside
  the repo. Containers are now created with `working_dir=` set to the
  clone destination (default `/workspace`) and the repo is cloned directly
  into it, so an exec that names no working directory is already at the
  repo root; `resolve_git_root` reads the container's own `WorkingDir`
  instead of probing. Containers created before this upgrade keep working
  via the old metadata/probe fallback. (#600)
- **`publish` hard-blocks without a recorded `verify_in_container` pass**,
  with a `skip_verify_gate=True` bypass — previously a missing or failing
  verify only attached a warning and `publish` proceeded anyway (literally
  commented `# Never blocks`), so a `verify` gate-fail could still reach a
  pushed branch and only surface as a CI failure. The bypass is meant to
  be exercised through the MCP client's own tool-approval prompt (a human
  in the loop), not decided unilaterally by the calling LLM. (#615)
- Vendored `mcp-token` broker pin (both `token_broker.py`'s digest-verified
  assets and `scripts/setup.sh`'s tag) bumped to v1.3.2, matching the
  mint-socket version already rolled out elsewhere; the sunaba-vendored
  broker path (`GITHUB_TOKEN_BROKER_SERVICE=sunaba`) had been left on
  v1.2.0. (#609)
- The MCP server's `instructions` block now states the file-transfer
  security boundary explicitly (one-way host→container only, no
  container-to-container path) instead of leaving it to be discovered by
  hitting it. (#578)
- Language detection for image selection now always uses the GitHub API
  instead of probing a Shiori pre-clone directory first. (#575)

### Removed

- **`clone_repo` tool**: the standalone MCP tool that cloned an extra repository
  into an already-running container is gone. `sandbox_initialize(clone_repo=...)`
  (and `run_container_and_exec`) clone and install in one call, so the tool was a
  redundant second implementation whose different `dest_dir` default was a source
  of confusion (#230, #600). The `clone_repo` *parameter* on `sandbox_initialize`
  / `run_container_and_exec` is unchanged. (#602)
- **Shiori pre-clone copy path**: `clone_repo` now always clones via the network
  (`gh repo clone` / `git clone`), eliminating the shiori pre-clone copy route
  that was faster in theory but slower in practice and had freshness bugs.
  Removed `_clone_shiori_repo_to_container`, `_shiori_preclone_root`,
  `_shiori_preclone_exists`, `warn_if_shiori_root_unusable` functions.
  Removed `--shiori-repos-path` / `SUNABA_SHIORI_REPOS_PATH` CLI argument.
  Removed `preclone_root` parameter from `resolve_initial_image`.
  `clone_repo` now always auto-enables `allow_network`. (#575)

### Fixed

- **`write_file_sandbox` AST-fallthrough corruption**: when `old_str` was a bare
  definition signature (`def foo():`) and AST resolution failed (ambiguous
  symbol) or reported no change, the silent fallback to exact-string matching
  replaced only the signature line and spliced the new body in front of the old
  one, leaving the old body orphaned in the file — reported as success. A no-op
  AST edit now returns "No changes" without writing; an AST failure with a
  bare-signature `old_str` and complete-definition `file_contents` surfaces the
  AST error (with `line=` guidance) instead of corrupting the file.
  Signature-to-signature renames and full-definition `old_str` blocks keep the
  string fallback. Near-miss errors now note the preceding AST failure. (#599)
- **`edit_symbol` docstring preservation**: the preserved docstring was inserted
  right after the first `def` line, which broke multi-line signatures and
  one-liner replacements (both rejected valid `new_code` with a spurious syntax
  error), and multi-line docstrings were flattened to a single indent level.
  Insertion now uses the new definition's AST body position, one-liners skip
  preservation, and docstring blocks shift as a whole keeping relative
  indentation. (#599)
- **`edit_file` old_str mismatch diagnostics, and a post-edit success echo**:
  a failed match used to report an "indentation mismatch" hint with indent
  numbers that didn't correspond to what was visibly on screen, making the
  actual diff hard to find; it's replaced with a "first mismatch" report
  that pinpoints the first diverging line via `repr()` (so stray whitespace
  is visible) and a dynamic unified-diff cap (full diff up to 50 lines, a
  30-line cap with a "truncated" note beyond that, up from a flat 6-line
  cap). Successful `old_str` edits now also echo the post-edit region with
  line numbers, so a caller can confirm the result without a follow-up
  read. (#580)
- **`sandbox_initialize` swallowed dev-install failures**: a bad
  `pip_extras` (e.g. `"dev"` instead of `"[dev]"`) made `pip install`
  fail silently, `sandbox_initialize` still reported success, and the
  failure only surfaced later as a wall of `Import "X" could not be
  resolved` errors from `verify_in_container`'s type gate — which reads
  as broken code, not a missing dependency. Pip failures are now surfaced
  on every init path (`sandbox_initialize`, `run_container_and_exec`,
  PR-branch setup), and a bare `"dev"` is normalized to `"[dev]"` instead
  of being passed through to a command that silently no-ops on it. (#595)
- **Idle reaper (`SUNABA_CONTAINER_TTL_SECONDS`) had two defects that only
  showed up once the TTL was actually turned on**: it ran only from
  `sandbox_list_containers`, which agents rarely call (they create
  containers, not list them), so a configured TTL would almost never
  fire — it's now also invoked from `sandbox_initialize`, the one hook
  every agent goes through. It also selected purely on the
  container-managed label, which also matches the egress-proxy sidecar;
  reaping the sidecar would have broken networked init for every other
  container. The reaper is now scoped to sandbox containers explicitly.
  TTL stays opt-in, default 0. (#594)
- **`publish(create_pr=True)` silently created PRs with an empty body**
  when `pr_body` was omitted — `pr_title` was required but `pr_body`
  defaulted to `""` with no validation, and several of the project's own
  PRs shipped with a zero-length body as a result. `create_pr=True` with
  an empty `pr_body` is now a validation error instead of a silent no-op. (#608)
- **PR creation via `publish` was not idempotent on a dropped transport**:
  a long `publish(create_pr=True)` call whose MCP transport dropped
  mid-call left the caller unable to tell whether the PR had actually
  been created — and it usually had been, discoverable only via a manual
  `gh pr list`. A 422 (PR already exists) on retry is now resolved by
  fetching and returning the existing PR instead of failing. (#593)
- **`sandbox_pr_review_write` rejected `APPROVE` / `REQUEST_CHANGES` on a
  PR the bot itself owns** — GitHub's API 422s on self-approval/self-changes,
  which previously had to be caught and retried as `COMMENT` by the
  caller. The tool now retries with `COMMENT` automatically on that
  specific 422 and reports both `original_event` and `downgraded_to` in
  the response. (#614)
- **`run_container_and_exec` dropped `open_read_grant` on PR checkout**:
  unlike `sandbox_initialize`, it computed `proxied` and
  `container_has_token` but never derived `open_read_grant` from them
  before passing to `_setup_pr_branch` / `_try_clone_into_container`, so
  checking out a private-repo PR under the egress proxy fell back to an
  anonymous fetch and failed authentication. (#624)
- **`publish` push failures now carry deterministic hints**: a push
  failure's error message gave no direct signal for two of its most
  common causes, so callers had to guess from error text. The response
  now checks the container's actual `allow_network` state and whether a
  VCS token was resolved on the host, and appends a plain-language hint
  when either explains the failure (container started offline; no VCS
  token configured on the host). `sandbox_initialize`'s return string and
  `sandbox_attach`'s orientation summary now also surface `allow_network`
  directly instead of leaving it to be inferred. (#577)

## [0.9.0] - 2026-07-12

### Added

- Tool results can carry an advisory `recommended_next_action` nudge, emitted
  only when the state warrants it -- e.g. a call against a container that no
  longer exists points at `sandbox_initialize` (#550).
- Dashboard: the container list is backed by Docker labels and split out into
  its own `/containers` page (#527), with a per-container Stop button (#528).
- `sandbox_attach` is recorded in the journal, so a session hand-off leaves a
  trace instead of appearing as two unrelated runs of operations (#554).
- `publish-pypi.yml`: releases publish `sunaba` to PyPI automatically, via
  Trusted Publishing (OIDC -- no API token is stored on the GitHub side),
  triggered by `release: published` (#534).  The workflow merged after v0.8.0
  was tagged, so 0.8.0 was pushed to PyPI by a manual `workflow_dispatch`;
  0.9.0 is the first release published by its own tag.

### Changed

- The MCP server now ships an `instructions` block describing the sandbox
  workflow, and tool docstrings state their interface contract instead of
  restating that workflow (#550).  Total tool-description weight drops from
  ~34KB to ~16KB.
- Image pins moved to `ghcr.io/masuda-masuo/sunaba/*` for both the sandbox
  variants (#313) and the proxy sidecar (#432).  This closes the loose end
  0.8.0 left open: the pins still pointed at the pre-rename GHCR package path.
- The egress-proxy sidecar is recreated when its baked-in config goes stale,
  instead of being reused with an outdated allowlist (#551).
- shiori pre-clone resolution is consolidated behind one code path: a flat
  `owner__repo` layout, an EACCES fallback, conditional unshallow, and a
  startup sanity check (#532).

### Fixed

- `sandbox_initialize` did not resolve the variant aliases `python` / `go` /
  `neutral` to their pinned digests (#545).
- `write_file_sandbox` dropped the file's trailing newline on line-range
  replacement and on append (#570).
- The shiori pre-clone copy path: `clone_dest` was not created before
  `put_archive`, the copied tree stayed root-owned rather than being chowned to
  the default exec user (#532), and the copy filter stripped `.env` templates
  (#561).
- `sandbox_pr_review_write` swallowed the GitHub 422 response body, hiding why
  a review was rejected (#537).
- `sunaba.service` listened on a port other than the documented default 8750
  (#544).

### Internal

- Docs: the README is restructured with detail delegated to sub-docs (#563),
  the Japanese design docs are translated to English (#565, #566), design.md
  reflects the egress-proxy default-on posture and a contradicting README claim
  is removed (#553), and usecases.md is refreshed (#530).
- Integration tests for the search pipeline (#548).

Upgrading from 0.8.0 needs no migration steps: no state directory, env var, or
Docker label changes name in this release.

## [0.8.0] - 2026-07-10

The project is renamed **code-sandbox-mcp -> sunaba** and versioning restarts
at `0.8.0`.

A `1.0.0` entry previously headed this file, dated 2026-07-08.  It was never
released: no git tag, no GHCR version tag, no package on any index.  Declaring
`1.0.0` committed the project to a stable external contract before the
operational side was stable enough to keep that promise, and the commits that
followed broke it twice (see *Changed* below).  Rather than launder those
breaks into a `1.1.0`, the release is withdrawn: `0.8.0` restates the same
contract under a `0.x` version, where a minor bump is the honest way to reverse
a default.  See `docs/design.md` §15 for the decision, and #531 / #534.

### Changed

- **BREAKING: renamed to `sunaba`** (#534).  The distribution, the import
  package, the console script, and every runtime identity move together:
  - Package `code-sandbox-mcp` -> `sunaba`; import `code_sandbox_mcp` -> `sunaba`
  - Console script / MCP server key `code-sandbox-mcp` -> `sunaba`
  - Env var prefix `CODE_SANDBOX_*` -> `SUNABA_*` (all 20 variables)
  - Host state directory `~/.code-sandbox-mcp/` -> `~/.sunaba/`
  - Docker labels `com.code-sandbox-mcp.*` -> `com.sunaba.*`
  - Docker network / sidecar / volume `code-sandbox-egress*` -> `sunaba-egress*`
  - systemd unit `code-sandbox-mcp.service` -> `sunaba.service`, and
    `GITHUB_TOKEN_BROKER_SERVICE=sunaba`
  - GHCR images move to `ghcr.io/masuda-masuo/sunaba/{sandbox,proxy}`

  No compatibility shims are provided: the old names are gone, not deprecated.
  See **Migration** below -- several of these are *runtime* identities, so an
  upgrade that skips the migration steps silently loses track of existing
  containers or breaks the token chain.
- **BREAKING**: legacy `CSB_*` / `SHIORI_REPOS_PATH` env-var fallbacks
  removed — the rename already breaks every env var, so the
  two-generations-old aliases go with it.
- **BREAKING: egress proxy is on by default** (#509).  Opt out with
  `SUNABA_ENABLE_EGRESS_PROXY=false`.
- **BREAKING: destination hosts are default-deny** (#506).  With the proxy on,
  egress to anything outside `DEFAULT_EGRESS_HOSTS` is blocked.

### Added

- `SUNABA_ALLOWED_EGRESS_HOSTS`: destination-host allowlist (#506).
- `scripts/setup.sh`, `scripts/install-systemd.sh`, `scripts/sunaba.service`:
  three-phase resident setup (#517).
- `proxy.golang.org` / `sum.golang.org` added to `DEFAULT_EGRESS_HOSTS` (#522).

### Fixed

- `sandbox_stop` failed to detect unpushed checkpoints, which also blocked
  `working_dir` auto-detection (#503).
- `SUNABA_ALLOWED_EGRESS_HOSTS` was not forwarded to the proxy sidecar (#519).

### Internal

- Pinned `mcp-token` broker bumped to v1.2.0 (#525).
- The rename initially shipped with a shim in `proxy_lifecycle.py` passing
  boundary-crossing variables under both the `SUNABA_*` and legacy
  `CODE_SANDBOX_*` names, because `proxy_pin.json` still pinned a pre-rename
  sidecar that only read the old names.  #538 re-pinned the sidecar to an
  image built from the renamed source, so the shim was removed (#534).

### Migration

Run once, with the server stopped.  Steps 2-4 are not optional: each renames a
key the server uses to *find* existing state.

```bash
# 1. Reinstall under the new name
pip uninstall code-sandbox-mcp
pip install git+https://github.com/masuda-masuo/sunaba@v0.8.0

# 2. Docker objects: the managed-container label, the sidecar, the network and
#    the CA volume all changed name.  Old containers are invisible to the new
#    server (they carry com.code-sandbox-mcp.managed), so remove them here.
docker ps -aq --filter label=com.code-sandbox-mcp.managed | xargs -r docker rm -f
docker network rm code-sandbox-egress   2>/dev/null || true
docker volume  rm code-sandbox-egress-certs 2>/dev/null || true   # CA is regenerated

# 3. Token broker: mcp-token resolves the service via launcher.json, not by
#    keyring service name.  Duplicate (or rename) the "code-sandbox-mcp"
#    service entry to "sunaba" in launcher.json (next to the mcp-token
#    binary, or $MCP_LAUNCHER_CONFIG).  The keystore entries are referenced
#    by absolute key via env_keys and need no change.
#    Verify with: mcp-token sunaba

# 4. Host state: journal + traces. Move it, or past history stops being read.
#    (Guarded: if the new server already ran once, ~/.sunaba exists and an
#    unconditional mv would nest the old directory inside it.)
[ -e ~/.sunaba ] || mv ~/.code-sandbox-mcp ~/.sunaba

# 5. Rename the server key in your MCP client config
#    (mcpServers."code-sandbox-mcp" -> mcpServers."sunaba"), and rename any
#    CODE_SANDBOX_* env vars you set to SUNABA_*.
```

The old GHCR package (`ghcr.io/masuda-masuo/code-sandbox-mcp/*`) is left in
place: `image_pins.json` and `proxy_pin.json` still reference it by digest,
because those digests exist only under the old package path.  They are re-pinned
to `ghcr.io/masuda-masuo/sunaba/*` once CI has published there.
