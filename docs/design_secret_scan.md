# Secret scan — design

This document is authoritative. The code implements it; an implementation that drifts from
this document is a bug in the code, not a reason to rewrite the document.

It exists because the rationale behind this subsystem was previously recoverable only by
reading five issue threads (#676, #696, #699, #701, #703, #704, #708, #842) and two long
comment blocks. Several decisions here look wrong at first glance and are not, and two of
them have already been "fixed" back into bugs by someone who did not know the history.

The document has two layers, deliberately separated:

- **Part 1** is about the scanner itself (gitleaks since #842). It is transferable — none of
  it is specific to sunaba, and all of it is invisible on first contact with the tool.
- **Part 2** is sunaba's contract. When the scanner was replaced, Part 1 was rewritten and
  Part 2 survived almost unchanged — which is the split working as intended.

---

## Threat model

State this first, because it sets the bar in both directions.

The guard is **not** defending against an agent trying to exfiltrate a secret. Inside a
sandbox container the agent has arbitrary code execution by design; defending against a
determined adversary there is not a coherent goal.

It is defending against an agent that hits a block and **retries**. The block response
contains the `hashed_secret` value. "The tool told me exactly what to suppress" is one short
step from an ordinary recovery loop, and no malice is required to get there — retrying after
a failure is normal agent behaviour.

The requirement is therefore narrower and achievable:

> An agent must not be able to suppress its own finding without a human in the loop —
> by accident or by ordinary persistence.

Without this stated, the design invites over-engineering (defending the undefendable) and
equally invites under-engineering (dismissing a real bypass as "well, the agent could always
misbehave"). #708 was found and fixed because the bar was set here.

---

## Part 1 — Driving gitleaks

Written so that someone who has never used the binary does not step on the same things.
Nothing here is sunaba-specific.

The scanner was `detect-secrets` until #842. That history is not deleted below — the two
fail-opens it produced (#701, #704) are the reason several decisions here look
over-careful — but the operative subject is now gitleaks 8.30.1, a static Go binary baked
into the base image and installed in CI by a checksum-pinned workflow step.

### `gitleaks dir` scans a path, and a single file is a valid path

`gitleaks git` walks history; `gitleaks dir` scans the filesystem. sunaba scans manifest
files only, so `dir` is the mode, and it accepts one file as readily as a directory.

Invoked with a **relative** path from the repo root, the report's `File` field is that same
relative path — which is what makes findings line up with the manifest, the baseline and the
publish response without any path rewriting. Invoking with absolute paths would change the
`File` values and quietly break that correspondence.

sunaba runs one invocation per file and aggregates. A single invocation over several paths
also works, but per-file keeps one file's fatal error from being read as another file's
clean result.

### The exit code is ambiguous unless you set it

Measured on 8.30.1:

```
$ gitleaks dir --no-banner --report-format json --report-path - has_secret.txt ; echo $?
1                     # findings
$ gitleaks dir --no-banner --report-format json --report-path - missing.txt   ; echo $?
1                     # fatal: stat missing.txt: no such file or directory
```

Findings and "the scan could not run" share exit 1. Reading 1 as "findings" would report a
scan that never happened as an ordinary block — recoverable via the override tool, which is
exactly the authority a broken scan must not get.

`--exit-code 99` removes the ambiguity, and this is why sunaba passes it:

| exit | meaning | sunaba state |
|---|---|---|
| 0 | scanned, nothing found | `clean` |
| 99 | scanned, findings | `findings` |
| anything else | scan failed | `error` (publish blocked) |

### The report is a JSON array, and a clean scan still writes one

`--report-format json --report-path -` writes the report to stdout; `--report-path FILE`
writes it to a file. Each finding object carries `RuleID`, `Description`, `StartLine`,
`EndLine`, `StartColumn`, `EndColumn`, `Match`, `Secret`, `File`, `Fingerprint`, `Entropy`,
`Tags`, plus git-only fields (`Commit`, `Author`, …) that are empty in `dir` mode.

A clean scan writes `[]` — **not** nothing. So empty stdout means the report never arrived,
which is an error, not a clean result. (One invocation per file means the aggregated stdout
is a *sequence* of arrays, so the parser decodes array by array rather than with a single
`json.loads`.)

### Finding identity: `sha1(Secret)`, deliberately

detect-secrets identified a finding by `hashed_secret`, which is `sha1(secret)`. gitleaks
has its own identity (`Fingerprint`: `file:rule:line`), and adopting it would have been the
natural-looking choice — and would have invalidated every `.secrets.baseline` already
committed on a base branch, plus made suppressions positional: move the line, lose the
suppression.

So sunaba computes `sha1(finding["Secret"])` and keeps `hashed_secret` as the identity. The
suppression architecture (baseline format, host-side fetch, override registry) is untouched
by the scanner swap.

This is an *identity*, not a password hash. SHA-1's collision weakness buys nothing here:
producing a colliding string still requires already knowing the secret.

### No verification, so no verification fail-open

This is the property that motivated the swap. detect-secrets' `AWSKeyDetector.verify()`
called AWS STS and read HTTP 403 as "not a secret" — and sunaba's egress proxy answers
non-allowlisted hosts with **403**. A real key pair came back clean (#701, upstream
[Yelp/detect-secrets#976](https://github.com/Yelp/detect-secrets/issues/976)); the mitigation
was a permanent `--no-verify` that could never be probed for, because falling back without
it restored the fail-open.

gitleaks makes no network calls at all. There is no successor flag to `--no-verify`, and the
whole class is structurally gone rather than suppressed by a flag.

### Do not hand the scanner the suppression list

gitleaks can take `--baseline-path` or a `.gitleaksignore`. sunaba passes neither, for the
same reason it never passed detect-secrets' `--baseline`: both live **inside the container**,
where the agent can write them, and in detect-secrets' case running the scan with `--baseline`
also *appended* new findings to it, so a blocked publish passed on retry.

The suppression list is applied host-side, after the scan, from sources the container cannot
reach (Part 2). Note the reason carefully: not "the file format is inconvenient", but "the
verdict must not depend on state the sandbox can edit".

**Not passing gitleaks' suppression inputs is not enough — the binary honours two of them
by default, and both are agent-writable (#842 [high]).** Measured against the real 8.30.1
binary:

- An inline `gitleaks:allow` comment on the secret's own line returns clean. Denied with
  `--ignore-gitleaks-allow`.
- A `.gitleaksignore` at the scan cwd (the repo root — the lookup default is `.`) listing
  the finding's `Fingerprint` returns clean. Denied by pointing `--gitleaks-ignore-path` at
  a scanner-owned empty temp dir created fresh per scan.
- A repo-root `.gitleaks.toml` is **not** picked up in this invocation shape (config
  resolution is flag/env only — measured, not assumed), so no `--config` pin is needed. If
  the invocation shape ever changes, re-measure this before shipping.

This is denial, not adoption: the non-goal "no `.gitleaksignore` / gitleaks-native baseline"
means sunaba never *reads* them; it must also mean the binary never *obeys* them.
`tests/test_secret_scan.py::TestRealGitleaks` proves both denials black-box against the real
binary — a planted correct fingerprint and a planted `gitleaks:allow` must both still yield
`findings`.

### Well-known example credentials are allowlisted

gitleaks ships allowlists for documentation keys: `AKIA…EXAMPLE` from the AWS docs produces
**no** finding. A smoke test built on one reads as "the guard is broken" — the same wrong
conclusion #699 reached about detect-secrets, for a different underlying reason.

Use a **randomly generated** value of the right shape instead: `AKIA` followed by 16 random
uppercase alphanumerics is detected (rule `aws-access-token`, entropy ≈4.1). The value is
left out of this document on purpose — writing one here would make this file a finding, and
`tests/test_secret_scan.py::TestRealGitleaks` already assembles one at runtime.

(The predecessor of this section quoted the AWS example key in full, and this document's own
publish was blocked by it. Left redacted rather than overridden: suppressing an
AWS-Access-Key-shaped hash on the base branch, permanently, to document a warning about AWS
keys is a bad trade.)

### The baseline is self-referential

`.secrets.baseline` stores `hashed_secret` values: 40-character SHA-1 hex, next to keys whose
names contain `secret`. Generic entropy and keyword rules fire on exactly that. Scanned as
ordinary source, a baseline reports much of itself as secrets — which is why sunaba excludes
the repo-root baseline path from the scanned file list at both invocation sites (Part 2),
rather than relying on the scanner to recognise its own artefact.

### stdout and exit code are not a stable API

Empty output, unparseable output, an exit code that means neither clean nor findings, and
"exit says findings but the report is empty" each mean something different, and none of them
mean "clean". Treating "not literally the findings case" as success is how #704 happened; the
same rule survives the scanner swap because it was never about detect-secrets.

---

## Part 2 — sunaba's contract

### Where the scan runs, and when

`publish` scans before pushing. In manifest mode the scan runs **before the commit**, so a
blocked secret never enters local git history either. In legacy mode the commit has already
happened and the scan reads the HEAD commit's files.

The scan itself executes inside the container (that is where the files are). Everything that
*decides* runs host-side.

### Guard states

`run_secret_scan` reports exactly one `secret_scan_state`:

| state | meaning | publish |
|---|---|---|
| `clean` | scan ran, nothing found | proceeds |
| `findings` | scan ran, secrets found | **blocked** |
| `error` | scan could not complete (unexpected exit code, empty or unparseable output, or an exit that signals findings with an empty report) | **blocked** |
| `skipped` | `gitleaks` not present in the image | proceeds |

The publish decision is written as an allowlist of **safe** states, never a blocklist of
failures:

```python
if scan_state not in ("clean", "skipped"):
    ...block...
```

This inversion is load-bearing. An unrecognised state, or a missing key, blocks. A failure
mode added later fails closed without anyone remembering to update the caller. Writing it the
other way round — `if scan_state in ("findings", "error")` — is the same bug as #704 with a
new coat of paint, and was caught in review of the #704 fix itself.

`skipped` proceeding is a deliberate, named exception, not a string that happens not to match.
See *Known gaps*.

### Suppressions: two mechanisms, two authorities

`secret_scan_override` conflates two things that must not be conflated. They are distinct:

| | authority | scope | gate |
|---|---|---|---|
| **immediate** | host-held one-time flag, keyed by container | this publish only | MCP tool-approval prompt (human) |
| **durable** | `.secrets.baseline` **as committed on the base branch** | every future publish | PR review (human) |

Both have a human in them. That is the whole design.

The container's `.secrets.baseline` is a **proposal** — a file the override tool writes so a
human has something to commit. It carries no authority. The baseline that the scan subtracts
is fetched from the base branch via the GitHub API **in the host process**.

This is #708. Before it, the scan `cat`-ed the baseline out of the container, so the
suppression list was agent-writable: take the hash from the block response, write it to the
file, publish again, pass — with the permission gate never firing. Verified as a working
bypass against the deployed server, and verified fixed the same way.

Do not "simplify" this by resolving the baseline with `git show origin/HEAD:.secrets.baseline`
inside the container. Container git objects and refs are as writable as any other file there;
that is not a host-side resolution, it just looks like one.

When the fetch fails — absent on the base branch, network error, anything — there are **no
suppressions**, and there is no fallback to the container copy. More findings is recoverable;
trusting the container is not.

`check_override` peeks and `consume_override` runs only after a **successful push**. Consuming
on check would burn the authorization when the push then fails, forcing the human to
re-authorize for a failure that was not theirs.

### Baseline path exclusion

The repo-root `.secrets.baseline` is excluded from the scanned file list, at **both**
invocation sites: the publish path and the override path. Missing the override path means each
override appends the baseline's own hashes back into it — a ratchet where every suppression
makes the next one bigger.

The match is **exact path** (`f != ".secrets.baseline"`), never a suffix, glob or basename
match. `notes/.secrets.baseline.bak`, `sub/dir/.secrets.baseline.txt` and a nested
`sub/dir/.secrets.baseline` are all still scanned. Matching by name is how this exclusion would
turn into a hiding place — verified live: a real key pair in `notes/.secrets.baseline.bak` is
still blocked.

Note upstream #912: the equivalent upstream exclusion is sensitive to path normalisation. Any
change here should compare normalised paths rather than raw strings.

---

## Known gaps

Stated as gaps, not omitted.

### An image without `gitleaks` publishes unguarded

`skipped` proceeds. The guard assumes the sandbox images sunaba provides, which include
`gitleaks`. A custom or broken image silently has no secret scanning.

Accepted rather than fixed: making `skipped` block would require `secret_scan_override` on
every publish from a minimal image, and the operating assumption is that users work with the
provided containers. Recorded here so nobody mistakes it for an oversight.

### False positives on the scan's own source recur until suppressed on the base branch

Keyword-style rules fire on an identifier containing `secret` next to a quoted value. The
file that *implements* the secret scan cannot avoid the word: `"secret_scan_state":
"skipped"` and friends are candidates. Under detect-secrets the count grew from 8 to 11 to 12
as the subsystem grew; gitleaks' rule set is narrower, so the same sources produce fewer, but
the phenomenon is a property of scanning the scanner, not of one tool.

This is self-limiting — the suppressions are committed on the base branch and inherited —
but only because #703 made the baseline committable at all.

The identity (`sha1(secret)`) is unchanged across the swap, so baseline entries approved
under the old scanner still suppress. Entries whose secret the new scanner no longer flags
simply stop matching anything: dead weight, not a hole.

---

## Considered and rejected

### A root-owned, unreadable directory inside the container

Keep the baseline (or anything else the agent must not touch) in a `chmod 700` root-owned
directory.

Rejected. The container grants arbitrary command execution by design, so file permissions turn
one hole into a search for privilege-escalation routes, and the search only has to succeed
once. More fundamentally it does not address the defect: #708 was not "a file was writable", it
was "the verdict trusted state that lives inside the container". As long as the scan reads the
baseline from inside, `PATH`, shell functions and file replacement all remain reachable.

Host-side resolution differs in kind, not degree: `publish` runs in a host process whose only
channel into the container is "run a command, read the result". There is no path from the
container to the reference value, so there is no set of bypasses to enumerate.

Worth answering the fair objection — "an agent that knows the value lives host-side might find
mischief anyway". That would hold if the defence were the agent's ignorance. It is not: the
path is absent whether or not the agent knows. **A design that breaks upon being documented is
not one this document could describe.**

The idea is not wrong in general. If something must genuinely live inside the container (a
token, say), an unreadable location is a sensible layer. The baseline simply does not need to
be there.

### Handing the suppression list to the scanner

detect-secrets' `--baseline`, and gitleaks' `--baseline-path` / `.gitleaksignore`. See
Part 1: the list would live inside the container, and with detect-secrets running the scan
also grew it, so a retry passed. Rejected on the threat model.

### Adopting gitleaks' native `Fingerprint` as the finding identity

`file:rule:line` is what gitleaks itself uses, and it is right there in the report. Rejected:
it invalidates every `.secrets.baseline` already committed on a base branch (a human-reviewed
artefact sunaba does not get to expire unilaterally), and it makes suppressions positional —
moving the line loses the suppression, adding a line above it silently un-suppresses.

---

## History

Four fail-opens were found in this subsystem within about a day. They are listed together
because the pattern is the point — each was a *different* door, and each looked fine in review:

| | failure | cause |
|---|---|---|
| #696 | the scan never ran at all | `exec_in_container` called `APIClient` methods on a `Container`; a broad `except` turned the `AttributeError` into exit 127, read as "detect-secrets not installed". The #676 guard had **never once fired** in production. The broken wiring had been chosen to keep a publish test's mock queue intact. |
| #701 | the scan ran and lost its findings | verification 403 collision (Part 1) |
| #704 | the scan could not run, and "could not determine" collapsed into "clean" | publish blocked only on the literal string `"findings"` |
| #842 | (not a failure — the migration) | #701 was mitigated by a flag that could never be probed for; gitleaks removes the verification path entirely, so the class cannot recur |
| #708 | the scan ran, found it, and the verdict was discarded on the agent's own say-so | suppression list read from inside the container |

The first three are failures to detect. The fourth is a working detection being thrown away,
which is why it is the most severe despite being the least visible.

Two recurring lessons, both earned:

- **Mock convenience selected the production wiring** (#696), and the layer that got skipped
  was the one carrying the guarantee. When a test's shape dictates a production code path, the
  guarantee is untested by construction.
- **A test can pin a bug as intended behaviour.** Removing the container-trusting path in #708
  broke exactly one existing test — one that asserted the container's baseline suppresses a
  finding. It was defending the bypass.
