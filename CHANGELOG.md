# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
The compatibility policy (what counts as a breaking change) is described in
[README.md#compatibility-policy](README.md#compatibility-policy).

## [Unreleased]

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

### Removed

- **`clone_repo` tool**: the standalone MCP tool that cloned an extra repository
  into an already-running container is gone. `sandbox_initialize(clone_repo=...)`
  (and `run_container_and_exec`) clone and install in one call, so the tool was a
  redundant second implementation whose different `dest_dir` default was a source
  of confusion (#230, #600). The `clone_repo` *parameter* on `sandbox_initialize`
  / `run_container_and_exec` is unchanged. (#602)

## [0.10.0] - 2026-07-13

### Removed

- **Shiori pre-clone copy path**: `clone_repo` now always clones via the network
  (`gh repo clone` / `git clone`), eliminating the shiori pre-clone copy route
  that was faster in theory but slower in practice and had freshness bugs.
  Removed `_clone_shiori_repo_to_container`, `_shiori_preclone_root`,
  `_shiori_preclone_exists`, `warn_if_shiori_root_unusable` functions.
  Removed `--shiori-repos-path` / `SUNABA_SHIORI_REPOS_PATH` CLI argument.
  Removed `preclone_root` parameter from `resolve_initial_image`.
  `clone_repo` now always auto-enables `allow_network`. (#575)

### Changed

- Language detection for image selection now always uses the GitHub API
  instead of probing a Shiori pre-clone directory first. (#575)

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
