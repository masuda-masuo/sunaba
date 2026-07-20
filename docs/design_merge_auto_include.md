# Merge / auto-include — design

This document is authoritative. The code implements it; an implementation that drifts from
this document is a bug in the code, not a reason to rewrite the document.

It exists because the rationale behind this subsystem was previously recoverable only by
reading five issue threads (#675, #712, #715, #716, #711) and three long comment blocks.
Several decisions here look wrong at first glance and are not, and one of them was nearly
"fixed" back into a bug by someone who did not know the history (Candidate B, which would
have caused permanent silent loss of base-advance files).

The document covers three layers, deliberately separated:

- **The threat** — the forgeable-skip-the-reset bypass and the principle it exposed
  (`publish_ops.py`'s merge-preservation logic, Issue #712).
- **The candidates** — three designs, two rejected with concrete reproductions, one
  adopted. The reasoning for each rejection matters, because both rejected candidates
  are the natural first answer anyone would reach for.
- **sunaba's contract** — what the current code actually does, where the reset happens,
  the three-way split of auto-included/skipped/unexplained paths, and the response
  contract (#711).

---

## Threat model

State this first, because it sets the bar in both directions.

The guard is **not** defending against a malicious container trying to push arbitrary
content. Inside a sandbox container the agent has arbitrary code execution by design;
defending against a determined adversary there is not a coherent goal.

It is defending against an agent that publishes after a merge with a manifest that
declares only its own feature files, accidentally leaking undeclared container-local
changes — checkpoint artifacts, work-in-progress, or secrets — into the pushed commit.
No malice is required: a worker that runs `checkpoint()`, then `merge_base()`, then
`publish(files=["declared.txt"])` is following instructions.

The requirement is therefore narrower and achievable:

> **Inputs to a security decision must be resolvable independently of the container;
> inputs to a diagnostic/reporting value need not be.**

This phrase is the crux of the entire design. It appears verbatim across the
corresponding issues (#712, #711). Everything that decides *what gets pushed* —
the reset target, the auto-included file content — is resolved host-side via the GitHub
REST API. Everything that is *reported* — the merge-touched paths, the SHA of the
discarded merge commit — is computed from container `git diff` output, because none of
it changes what gets pushed. Conflating the two is the mistake Candidate A made
(host-side SHA verification of what the container said about parent SHAs, while still
trusting the container about what the parents *were*); the document returns to this
below.

Why this matters in practice: solo development rarely has main advance while a PR branch
is open, so this class of bug was rarely triggered. Concurrent development — kusabi
running multiple workers on separate issues, or any team development — makes it common.
The original #675 friction came from exactly this: a PR falling behind main while other
work landed first, and the sandbox having no fetch/merge path.

---

## The bypass, reproduced

The original #675 design added a merge-preservation branch to `git_prepare_commit`
(`publish_ops.py`): when `HEAD` was a merge commit whose first parent matched the
remote tracking ref (`origin/<branch>`), the reset was skipped and the merge commit's
tree was preserved. The rationale was defensible — a clean merge with no local
checkpoints should survive publish. But the decision was made from **purely
container-supplied inputs**.

Every input to that decision came from inside the container:

| Input | Source | Forgeable? |
|---|---|---|
| `HEAD^2` exists (is this a merge?) | `git rev-parse --verify HEAD^2` | Yes — refs are in `.git/`, owned by the sandbox user |
| `HEAD^1` SHA | `git rev-parse --verify HEAD^1` | Yes |
| `origin/<branch>` SHA | `git rev-parse --verify origin/<branch>` | Yes — `refs/remotes/origin/*` are local files |

The bypass is not theoretical. It was demonstrated live against
`masuda-masuo/sunaba-lab` (a real GitHub remote) on 2026-07-20. The exploit uses only
ordinary git operations that any agent — malicious or simply mis-scripted — can perform:

```bash
# 1. Create a branch, push it
git checkout -b feat/x
echo "declared content" > declared.txt
git add declared.txt && git commit -m "feature"
git push origin feat/x
# → origin/feat/x = de4d040

# 2. main advances (simulate: other worker merged first)
echo "main-only change" > moved.txt
git add moved.txt && git commit -m "main advance"
git push origin main
# → origin/main = 2130f8d

# 3. Create a secret in a local checkpoint
echo "AWS_SECRET=totally-fake-not-real-0000" > .env
git add .env && git commit -m "secret checkpoint"
# → eb2b055

# 4. Merge main (clean, no conflicts)
git merge origin/main --no-edit
# → merge commit d117ecd, P1=eb2b055(.env), P2=2130f8d(main)
# → tree includes .env

# 5. Forge the remote-tracking ref to match HEAD^1
git update-ref refs/remotes/origin/feat/x eb2b055...

# 6. Publish with a manifest that doesn't declare .env
publish(files=["declared.txt"], branch="feat/x")
```

The publish succeeded with `"status": "pushed"`. GitHub API confirmation:

```
$ gh api repos/masuda-masuo/sunaba-lab/commits/feat/x --jq '{parents:[.parents[].sha]}'
{"parents":["eb2b055f...","2130f8df..."]}

$ gh api repos/masuda-masuo/sunaba-lab/contents/.env?ref=feat/x --jq '.content' | base64 -d
AWS_SECRET=totally-fake-not-real-0000
```

`.env` was pushed to the real remote despite `files=["declared.txt"]`. The `secret_scan`
reported `"clean"` because it scanned only declared files. The leak required no elevated
privileges, no conflict resolution, and no special knowledge of the remote — just the
ability to write a ref in the container's own `.git/`.

The attack is indistinguishably an accident: a worker that checkpoint-then-merges is
following the documented flow, and `git update-ref` can just as easily be a misapplied
`git fetch` or a stale remote-tracking ref from an earlier clone. The tool silently
enabled the leak; the principle violated is the one that matters.

---

## Candidate A / B / C, and why A and B were rejected

Three designs were evaluated in the #712 comment thread. Each is described here with
what it closes, what it does not, and the concrete reason for rejection.

### Candidate A: host-side SHA verification only

**What it does:** Instead of trusting the container's `git rev-parse` output, the host
verifies the parent SHAs with `git ls-remote` (or the GitHub API). If both parents exist
on the remote, the merge is "real" and the reset is skipped.

**What it closes:** The simplest forgery — writing a fake `origin/<branch>` ref that
doesn't exist on the remote.

**What it does not close — two remaining bypasses:**

1. **The "lying container" bypass:** The container can report *different* SHA values to
   the host than what `HEAD` actually has. For instance, report `HEAD^1` as `origin/main`'s
   real SHA (which passes host-side verification) while the actual `HEAD` tree contains
   the secret. The host has no path to inspect the container's *actual* `HEAD` content
   independently.

2. **The "reversed merge" bypass:** The container can construct a merge where the
   first parent IS a truly remote SHA, but contains the secret:

```bash
# .env is on a genuinely remote branch origin/evil
git fetch origin evil
git checkout -b tmp origin/evil
git merge origin/main --no-edit
# Now HEAD is a merge: P1=origin/evil(has .env), P2=origin/main
# P1 passes host-side verification — it's really on the remote
# But the merged tree includes .env
```

Candidate A alone cannot distinguish this from a legitimate merge. **Rejected.**

### Candidate B: always-reset + host-resolved two-parent commit

**What it does:** Remove the skip-the-reset branch entirely — always run
`git reset --mixed <base_ref>`. Then, when a merge was detected, construct a two-parent
commit using host-resolved SHAs (from `git ls-remote`): `<base_sha>` as parent 1,
`<branch_sha>` as parent 2, via `git commit-tree`.

**What it closes:** All container-forged-parent bypasses. Both parent SHAs come from
`ls-remote`, so the container cannot inject a forged SHA. The reset is unconditional.

**What it does not close — the permanent-silent-loss:**

Candidate B resets to the *feature branch's own previous remote tip*
(`origin/<branch>`). That tree does not contain any files that `main` advanced since the
feature branch was last pushed. If those files are not declared in the manifest, they
are silently dropped — and, worse, their loss becomes invisible to future `git merge`
operations.

This was measured in a throwaway git repository, not reasoned about abstractly:

```bash
# Setup: two repos simulating main and feature
mkdir test-candidate-b && cd test-candidate-b && git init

# Simulate main advancing with a new file
echo "main-only content" > moved.txt
git add moved.txt && git commit -m "main: add moved.txt"
MAIN_SHA=$(git rev-parse HEAD)

# Simulate feature branch (behind main, has its own file)
git checkout -b feature $MAIN_SHA~1 2>/dev/null || git checkout -b feature
echo "feature content" > feature.txt
git add feature.txt && git commit -m "feature: add feature.txt"
FEATURE_SHA=$(git rev-parse HEAD)

# A real merge would bring moved.txt into feature
git merge $MAIN_SHA --no-edit
# Merge commit M: tree = feature.txt + moved.txt
# But Candidate B resets to FEATURE_SHA, not M

# Simulate Candidate B's fabricated commit:
# tree = feature's previous tip (moved.txt NOT present)
# parents = [FEATURE_SHA, MAIN_SHA] — both real remote SHAs
TREE=$(git rev-parse $FEATURE_SHA^{tree})
FAKE=$(git commit-tree $TREE -p $FEATURE_SHA -p $MAIN_SHA -m "fake merge")
git update-ref HEAD $FAKE

# Now push this — looks like a merge, but moved.txt is gone

# Main advances further
echo "more main content" > other.txt
git add other.txt && git commit -m "main: add other.txt"
MAIN_SHA2=$(git rev-parse HEAD)

# Feature tries a real merge again
git merge $MAIN_SHA2 --no-edit

# Result:
#   Merge made by the 'ort' strategy.
#    other.txt | 1 +
#    1 file changed, 1 insertion(+)
# moved.txt is NOT mentioned. git status is clean.
# moved.txt is permanently gone with no warning.
```

The mechanism: git's three-way merge treats "present at merge-base, absent in ours,
unchanged in theirs" as an **intentional deletion**. The fabricated commit has
`MAIN_SHA` (the tip at the time `moved.txt` was added) as a parent, so git considers
`moved.txt` to have been present at the common ancestor and deliberately removed in
the feature branch. No amount of subsequent real merging brings it back.

This is the inverse of #679's leak prevention: instead of "undeclared container content
silently leaking out," it is "legitimately public base-branch content silently and
permanently disappearing." **Rejected** — the cost is higher than the benefit.

Note: conflict-resolution data (merge conflict markers resolved by the user) is also
lost, but that is recoverable (re-merge, re-resolve). The permanent loss of base-only
files is not.

### Candidate C (adopted): reset-always + host-side base-diff auto-include

**What it does:**

1. **Always reset** to the remote base (`git reset --mixed <base_ref>`). No skip-the-reset
   branch exists. The merge commit's tree is never trusted.

2. **Host-side auto-include**: For files that the base branch advanced since the feature
   branch's last push — detected via the GitHub Compare API
   (`GET /repos/{owner}/{repo}/compare/{feature_sha}...{base_sha}`) — the host fetches
   the file content directly from GitHub (Contents API) and writes it into the working
   tree before the commit. These files enter the pushed commit even though they were not
   declared in the manifest.

3. **Declared files take priority**: files in `files=[...]` are staged from the
   container's working tree *after* auto-include, so a declared path overrides any
   auto-included path with the same name.

**Why it is safe:** The auto-included value's provenance is **always** a host-side read
of real, already-public remote content. The GitHub Compare API tells the host what
changed; the GitHub Contents API gives the host the exact file content from the base
branch tip. The container's working tree, git refs, and command output are not consulted
for any value that enters what gets pushed. A lying container can at most choose *which*
real branch gets diffed against — it cannot inject content that is not already public.
And the branch it picks is the one `publish` is trying to push to; lying about it would
push to the wrong branch, which is a different failure mode with its own guard.

**Why it is correct (not just safe):** Candidate C's behavior is deliberately designed
to match what a normal `git merge && git push` outside sunaba would produce. In ordinary
git usage, merging main brings main's changes into the branch, and pushing sends them.
sunaba's manifest mode is meant to prevent **container-only** undeclared content from
leaking — checkpoint artifacts, secrets, work-in-progress — not to prevent legitimately
public base-branch content from arriving the way it always would. Candidate C restores
that behavior without reintroducing the leak.

The implementation lives in two places:

- **Host-side fetch**: `_fetch_base_auto_include()` in
  `src/sunaba/tools/vcs/publishing.py`. Uses the GitHub REST API (Compare + Contents)
  to determine what the base branch advanced and fetch the content. Returns an
  `AutoIncludeResult(included, skipped)`.
- **Container-side apply**: `git_prepare_commit()` in `src/sunaba/tools/publish_ops.py`.
  Receives the `base_auto_include` dict and writes each file via base64-encoded echo
  (shell-safe binary passthrough), then stages it. Deleted files (`None` sentinel) are
  removed with `git rm`.

The pattern is directly modeled on `_fetch_baseline_from_base_branch` in the secret scan
subsystem (#708 / `design_secret_scan.md`): a host-side API call that resolves a value
the security decision depends on, independently of anything in the container.

---

## sunaba's contract

### Where the reset happens, and when

The reset (`git reset --mixed <base_ref>`) **always** runs in manifest mode (when
`files=[...]` is non-empty). There is no skip-the-reset branch — the entire
merge-preservation logic that existed in #675/PR#695 was removed by PR#713.

Legacy mode (no manifest, `include_untracked=True` or `git add -A` fallback) is
unaffected. Merge detection and auto-include only operate in manifest mode.

The base ref is resolved in this order:
1. `origin/<branch>` — if the branch already exists on the remote (follow-up push to an
   open PR preserves earlier commits)
2. `origin/HEAD` — the remote default branch (set by `git clone`)
3. `origin/main`, `origin/master` — last-resort fallback

If none resolve, manifest mode fails with a clear error — it will not silently skip the
reset, which would re-open the manifest leak.

### The three-way split of merge-touched paths

When `HEAD` is a merge commit (detected before the reset by checking `HEAD^2` exists),
every path the merge touched falls into one of three buckets:

| Bucket | Definition | Source | Effect |
|---|---|---|---|
| **Auto-included** | In the base-diff, successfully fetched host-side | GitHub API (host) | Written into working tree, staged, committed |
| **Skipped** | In the base-diff, but could not be fetched (rename, fetch failure, non-base64 encoding) | GitHub API (host) — but fetch failed | Reported in `auto_include_skipped` |
| **Unexplained** | Touched by the merge, but neither declared in the manifest nor auto-included | Container `git diff` | Reported in `merge_discarded_undeclared` |

The "unexplained" bucket is the AC-4 "real accident" set from #711:
`merge_touched_paths - declared - auto_include.keys()`. These are paths that the merge
brought in but that are not explained by either the manifest or the base's advance —
typically checkpoint artifacts buried beneath the merge commit. #679's leak prevention
is working as intended for these; #711 makes them visible instead of silent.

### The response contract

When a merge was detected at `HEAD` before `git_prepare_commit` reset it, the publish
response carries these fields:

| Field | Type | Meaning |
|---|---|---|
| `merge_discarded_sha` | `string` | Abbreviated SHA (7 chars) of the discarded merge commit |
| `merge_parents` | `[string, string]` | Abbreviated SHAs of the merge's two parents |
| `auto_include_applied` | `[string]` | Paths the host-side auto-include successfully restored |
| `auto_include_skipped` | `[string]` | Paths in the base diff that could not be auto-included |
| `merge_discarded_undeclared` | `[string]` | AC-4 set: merge-touched paths explained by neither manifest nor auto-include |
| `push_transport` | `"native"` or `"api"` | Which push path succeeded |

These fields are **present only when a merge was detected** — they are absent (not
present-but-empty) for ordinary pushes. This is deliberate: the merge-specific fields
carry meaning only in the merge context, and their absence is the signal that nothing
was discarded.

A non-empty `merge_discarded_undeclared` set is a **warning**, not a failure.
`status` stays `"pushed"`. This was an explicit, deliberate decision recorded in #711's
issue body ("検討事項"): after Candidate C, the vast majority of merge-touched paths are
auto-included, so a non-empty AC-4 set is almost always the checkpoint-beneath-merge
case that #679 intentionally discards. Making it a failure would block the common case
and force the user to take action on a non-issue. The information is surfaced so the
user can inspect and decide, not so the tool can refuse.

### Push transport reporting

`push_transport` reports `"native"` when `git push` succeeded directly, and `"api"`
when the GitHub Objects API fallback was used (blob → tree → commit → ref). The API
fallback creates only single-parent commits — a pre-existing limitation from before this
entire chain. #711 only makes it visible via `push_transport`; it does not fix it.

The transport matters for merge handling because the API fallback's single-parent
constraint means a merge commit with two parents cannot be represented through that
path. When the API fallback is triggered for a merge-detected publish, the pushed commit
will have a single parent (the resolved base SHA), and the merge lineage is lost. The
caller sees `push_transport: "api"` and can understand that the merge commit was not
preserved as a merge on the remote. This is separately tracked, not fixed here.

### Safety fallbacks

Every step of the auto-include pipeline fails safe:

- **GitHub API unreachable**: `_fetch_base_auto_include` returns `None`. The publish
  proceeds with only declared files — no auto-include at all. This is the recoverable
  direction: missing auto-included files is better than trusting the container.
- **Branch doesn't exist on remote yet**: Returns `AutoIncludeResult(included={},
  skipped=[])` — empty, not an error. A fresh branch has no base diff.
- **Default branch can't be resolved**: Returns `None`.
- **Compare API fails**: Returns `None`.
- **Individual file fetch fails** (network error, 404, non-base64 encoding): The file is
  added to `skipped`, not silently dropped. The caller sees it in `auto_include_skipped`.

---

## Known gaps

Stated as gaps, not omitted.

### Renamed files are not auto-included

When the base branch renames a file, the GitHub Compare API reports `status: "renamed"`.
`_fetch_base_auto_include` skips these (they are neither `"added"` nor `"modified"` nor
`"removed"`) and reports them in `skipped`. A real `git merge` would follow the rename;
this mechanism does not. The gap is visible in the response (the path appears in
`auto_include_skipped`), so the caller knows it happened, but the file is not restored.
Tracked as a future improvement, not resolved here.

### The AC-4 "real accident" set is diagnostic only, computed from the container

`merge_discarded_undeclared` is computed from the container's own `git diff --name-only
HEAD^1 HEAD` output. This is safe — nothing about what gets pushed depends on it — but
it does mean the *reported* set could in principle be wrong if the container lies about
it. This is a deliberate, bounded exception to the "no container-supplied security
decisions" principle, because it is not a security decision: it only affects what a
human reads afterward, never what gets pushed.

### API-fallback push path still creates single-parent commits

The GitHub Objects API fallback (`_try_api_push` in `publishing.py`) creates commits
with a single parent. When the native `git push` fails and the fallback is used for a
merge-detected publish, the pushed commit will have one parent (the resolved base SHA)
rather than two. The merge lineage is lost. This is a pre-existing limitation from
before this whole chain; #711 only makes it visible via `push_transport`, does not fix
it.

### Non-UTF-8 binary files in the base diff are handled, but inadequately reported

Binary and non-UTF-8 files (images, compiled assets) that appear in the base diff are
successfully auto-included as `bytes` (#716). However, the `auto_include_applied` field
lists only the path — the caller cannot distinguish a binary auto-included file from a
text one from the response alone. The content is correct (GitHub API → base64 decode →
write raw bytes), but the reporting is coarse.

### Deleted files are signaled but not verified against the container's state

When the base branch deletes a file (`status: "removed"` in the Compare API),
`_fetch_base_auto_include` signals a `None` sentinel, and `git_prepare_commit` runs
`git rm` on the path if tracked (#715). If the path was not tracked in the feature
branch's index (edge case), the `git rm` is silently a no-op. The deletion signal is
correct (it reflects the base branch state), but the gap between "base deleted it" and
"feature branch actually had it" is not surfaced.

---

## Considered and rejected

### Candidate A: host-side SHA verification only

**What it was:** Let the container report the merge's parent SHAs; verify them host-side
with `git ls-remote` / GitHub API. If verified, skip the reset (preserve the merge).

**Why it looked reasonable:** It closely mirrors the pattern that `design_secret_scan.md`
describes for the baseline: "the value that decides is resolved host-side." The parent
SHAs *are* resolved host-side. A naive reading of the principle says this is enough.

**Why it failed:** The value that decides is not whether the SHAs exist on the remote
— it is whether the *actual tree content* in the container matches what those SHAs
imply. The host can verify the SHA names, but it cannot verify that the container's
`HEAD` actually points to a merge with those parents. The container can:
1. Report `HEAD^1` as a verifiable remote SHA while the actual tree contains a secret
   (lying about what `HEAD` is).
2. Construct a merge whose first parent genuinely IS a remote SHA that contains the
   secret (the "reversed merge" pattern — `origin/evil` + `origin/main`).

The principle demands that **what gets pushed** be resolvable independently. Candidate A
resolves only the *names* of the parents, not the *content* of the commit. That is a
half-application of the principle, and half-applications are holes.

### Candidate B: always-reset + host-resolved two-parent commit

**What it was:** Always reset to `origin/<branch>`. Construct a two-parent commit using
host-resolved SHAs (`ls-remote` for both `main` and `branch` tips) via `git commit-tree`.

**Why it looked reasonable:** It fully applies the principle — both parents come from
`ls-remote`, so the container cannot inject a forged SHA. The reset is unconditional, so
no container-supplied input controls it. It is simpler than Candidate C (no auto-include
logic, no Compare API, no Contents API).

**Why it failed:** The reset target is `origin/<branch>` — the feature branch's own
previous remote tip. That tree does not contain any files that `main` advanced since the
feature branch was last pushed. If those files are not in the manifest, they are dropped.
And, critically, the two-parent commit records `main`'s tip as a parent, which makes
git's three-way merge treat the dropped files as *intentionally deleted*. Subsequent real
merges of main will not restore them — the loss is permanent and silent. See the
`moved.txt` reproduction in the Candidate B section above for the measured commands and
output.

The cost is higher than Candidate A's bypass, because Candidate A's bypass requires an
adversarial (or seriously mistaken) container to trigger, while Candidate B's loss
triggers from an ordinary, correct workflow: merge main, publish with a manifest that
doesn't list main's new files. The files are public, the merge was clean, and the user
did nothing wrong — but the files are permanently gone.

---

## History

Four changes built this subsystem. They are listed together because the pattern is the
point — each was a gap found in the previous step, and the first step (#712) was
rewriting the original #675 design from scratch:

| Issue | What was wrong | Root cause |
|---|---|---|
| #712 | `publish` skipped the reset when `HEAD^1 == origin/<branch>`, trusting container git refs | The reset-skip decision used only container-supplied inputs (`HEAD^1`, `origin/<branch>`), both forgeable via `git update-ref` |
| #715 | `_fetch_base_auto_include` ignored `status: "removed"` files (base branch deletions) | The implementation only handled `"added"` / `"modified"` statuses from the Compare API; `"removed"` was a gap in the initial scope |
| #716 | `_fetch_base_auto_include` silently skipped binary/non-UTF-8 files with `except Exception: continue` | `base64.b64decode(...).decode("utf-8")` threw `UnicodeDecodeError` for binary content; the broad `except` caught it and dropped the file with no warning |
| #711 | After the merge was discarded, the user had no visibility into what was discarded, what was saved, or what might be a real accident | The publish response gave no merge-specific information; the user saw `"status": "pushed"` with no indication that a merge commit was discarded, no information about auto-included paths, and no way to know if undeclared changes had been silently dropped |

#712 is the foundation: it removed the forgeable branch and established the
host-side-resolution pattern. #715 and #716 filled gaps in the auto-include
implementation. #711 added the response layer so the user can see what happened.

The original #675 friction (PR falling behind main with no sandbox fetch/merge path) was
solved by PR#697 (`sandbox_initialize(branch=...)`), which put both the PR branch and
main into the container at clone time. The merge tools (`merge_base`/`merge_complete`/
`merge_abort`, PR#695) then let the user resolve conflicts inside the sandbox. The
publish-side changes documented here (#712→#711) ensure that what gets pushed after all
that work is both safe and correct.
