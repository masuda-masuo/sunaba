# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
The compatibility policy (what counts as a breaking change) is described in
[README.md#compatibility-policy](README.md#compatibility-policy).

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
- The sidecar is still pinned to a pre-rename digest, so `proxy_lifecycle.py`
  passes boundary-crossing variables under both the `SUNABA_*` and legacy
  `CODE_SANDBOX_*` names.  This shim is removed once the sidecar is rebuilt
  under the new package and re-pinned (#534).

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

# 3. Token broker: the keyring entry is looked up by service name.
#    Re-store the credential under "sunaba" or `mcp-token sunaba` finds nothing.
mcp-token --store sunaba          # follow the prompt; verify with: mcp-token sunaba

# 4. Host state: journal + traces. Move it, or past history stops being read.
mv ~/.code-sandbox-mcp ~/.sunaba

# 5. Rename the server key in your MCP client config, and any SUNABA_* env vars
#    you set (mcpServers."code-sandbox-mcp" -> mcpServers."sunaba").
```

The old GHCR package (`ghcr.io/masuda-masuo/code-sandbox-mcp/*`) is left in
place: `image_pins.json` and `proxy_pin.json` still reference it by digest,
because those digests exist only under the old package path.  They are re-pinned
to `ghcr.io/masuda-masuo/sunaba/*` once CI has published there.
