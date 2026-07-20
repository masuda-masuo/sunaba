# Secret scan — design

This document is authoritative. The code implements it; an implementation that drifts from
this document is a bug in the code, not a reason to rewrite the document.

It exists because the rationale behind this subsystem was previously recoverable only by
reading five issue threads (#676, #696, #699, #701, #703, #704, #708) and two long comment
blocks. Several decisions here look wrong at first glance and are not, and two of them have
already been "fixed" back into bugs by someone who did not know the history.

The document has two layers, deliberately separated:

- **Part 1** is about `detect-secrets` itself. It is transferable — none of it is specific to
  sunaba, and all of it is invisible on first contact with the library.
- **Part 2** is sunaba's contract. If the scanner is ever replaced, Part 1 dies with it and
  most of Part 2 survives.

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

## Part 1 — Driving detect-secrets

Written so that someone who has never used the library does not step on the same things.
Nothing here is sunaba-specific.

### `scan` and `scan --baseline` are two different modes

They are not one command with an option. `detect-secrets scan FILE` reports findings on
stdout. `detect-secrets scan --baseline B FILE` **updates B in place and prints nothing**.

Measured:

```
$ detect-secrets scan --no-verify app.py          # JSON on stdout, results populated
$ detect-secrets scan --no-verify --baseline b.json app.py
exit=0
stdout bytes: 0
b.json md5: changed
```

Worse, and this is the part that matters: `--baseline` **adds newly discovered secrets to the
baseline**. Appending a brand-new secret to the scanned file and re-running grew the baseline
from 2 entries to 3, silently, with no output.

So in `--baseline` mode, *running the scan is the act of suppressing*. Using it as a guard
would mean snapshotting the baseline and diffing it afterwards — and a second run would report
nothing new, meaning **a blocked publish would pass on retry**. Against the threat model above
that is the worst possible property.

This is why sunaba does not pass `--baseline`. Note the reason carefully: not "stdout is empty
so results are inconvenient to read" (that is merely awkward and invites the reasonable-looking
suggestion of reading the file instead), but "the scan mutates the suppression list, so
scanning twice is self-approval".

### Verification plugins make outbound network calls, and fail open behind a proxy

`AWSKeyDetector.verify()` calls AWS STS. The relevant code, at the end of
`verify_aws_secret_access_key`:

```python
response = requests.post('https://sts.amazonaws.com', headers=headers, data=body)

if response.status_code == 403:
    return False
return True
```

`403` is the **only** status that means "not a secret" — it is how STS reports invalid
credentials. Every other status returns `True` and the finding survives.

sunaba's egress proxy answers a request to a non-allowlisted host with **403**:

```
STATUS 403
BODY BLOCKED by egress proxy: egress to sts.amazonaws.com is not in the allowlist. ...
```

The proxy's "I refuse to forward this" and AWS's "these credentials are invalid" are the same
status code, and the plugin cannot tell them apart. A real key pair comes back clean.

Note the asymmetry: a proxy returning 407 or 502 would not have caused this. **It depends on
which code your egress layer picked.** A reader who sets out to fix "non-200 handling" has not
found the hazard.

`--no-verify` is therefore mandatory, not merely preferable. Reported upstream as
[Yelp/detect-secrets#976](https://github.com/Yelp/detect-secrets/issues/976). This is not
covered by upstream #306 (catching `RequestException`): nothing raises here — a well-formed
HTTP response comes back, it just did not come from AWS.

Do not probe for `--no-verify` support and fall back without it. Falling back restores exactly
this fail-open, and a warning in a container log is not a control. The flag has existed since
the verification feature landed upstream, and the image pins detect-secrets, so an image
lacking it is a broken image: let the scan fail loudly instead.

### The baseline is self-referential

`.secrets.baseline` stores `hashed_secret` values: 40-character SHA-1 hex. That is exactly
what `HexHighEntropyString` and `KeywordDetector` look for. Scanned as ordinary source, a
baseline reports every line of itself as a secret.

Upstream ships `detect_secrets/filters/common.py::is_baseline_file` for precisely this, and
learns the path from `--baseline` — which sunaba does not pass, so sunaba excludes the path
itself (Part 2). Upstream has known about the phenomenon since 2018 (#55), and the
path-normalisation edge of it is still open (#912).

### stdout and exit code are not a stable API

Empty output, unparseable output and a non-zero exit each mean something different, and none
of them mean "clean". Treating "not literally the findings case" as success is how #704
happened.

### A credential can go undetected because of what is *next to* it

`AWSKeyDetector` treats an `AKIA…` match as a key ID, looks for a 40-character secret key
nearby, and calls STS when it finds one. **A key ID alone is reported; a complete, usable pair
was not** — the more dangerous of the two.

This is the mechanism behind the fail-open above, and it is unintuitive enough that a
plausible-sounding wrong explanation ("upstream filters well-known example credentials")
survived on #699 for a while before being corrected.

### Operational note: how to smoke-test this guard

Use a **randomly generated** value of the right shape. Well-known example credentials — the
`AKIA…EXAMPLE` key from the AWS documentation and its relatives — produce misleading results
and read as "the guard is broken". This already caused one incorrect conclusion that had to be
retracted on #699.

(That sentence originally quoted the example key in full, and this document's own publish was
blocked by it. Left redacted rather than overridden: suppressing an AWS-Access-Key-shaped
hash on the base branch, permanently, to document a warning about AWS keys is a bad trade.)

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
| `error` | scan could not complete (non-zero exit, empty or unparseable output) | **blocked** |
| `skipped` | `detect-secrets` not present in the image | proceeds |

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

### An image without `detect-secrets` publishes unguarded

`skipped` proceeds. The guard assumes the sandbox images sunaba provides, which include
`detect-secrets`. A custom or broken image silently has no secret scanning.

Accepted rather than fixed: making `skipped` block would require `secret_scan_override` on
every publish from a minimal image, and the operating assumption is that users work with the
provided containers. Recorded here so nobody mistakes it for an oversight.

### False positives on the scan's own source recur until suppressed on the base branch

`KeywordDetector` fires on an identifier containing `secret` next to a quoted value. The file
that *implements* the secret scan cannot avoid the word: `"secret_scan_state": "skipped"` and
friends are all reported. The count grew from 8 to 11 to 12 as the subsystem grew.

This is now self-limiting — the suppressions are committed on the base branch and inherited —
but only because #703 made the baseline committable at all.

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

### Passing `--baseline` and reading results from the file

See Part 1. It makes the scan mutate the suppression list, so a retry passes. Rejected on the
threat model.

---

## History

Four fail-opens were found in this subsystem within about a day. They are listed together
because the pattern is the point — each was a *different* door, and each looked fine in review:

| | failure | cause |
|---|---|---|
| #696 | the scan never ran at all | `exec_in_container` called `APIClient` methods on a `Container`; a broad `except` turned the `AttributeError` into exit 127, read as "detect-secrets not installed". The #676 guard had **never once fired** in production. The broken wiring had been chosen to keep a publish test's mock queue intact. |
| #701 | the scan ran and lost its findings | verification 403 collision (Part 1) |
| #704 | the scan could not run, and "could not determine" collapsed into "clean" | publish blocked only on the literal string `"findings"` |
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
