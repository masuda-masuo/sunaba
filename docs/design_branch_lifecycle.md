# Branch and base lifecycle — design

This document is authoritative. The code implements it; an implementation that drifts from
this document is a bug in the code, not a reason to rewrite the document.

It covers one question asked at four different moments in a container's life:

> **Which commit is "where my work started"?**

Clone, diff, merge and publish each need an answer, they each resolved it separately, and
they did not agree. #748 was the visible consequence: `diff_in_container` answered with
`HEAD~1`, which on a fresh clone is the change set of whatever landed upstream most
recently — someone else's merged PR, presented as your pending review.

Scope boundary: what happens *during* a publish once the base is known — the reset, the
manifest, auto-include of base-advance files — is
[design_merge_auto_include.md](design_merge_auto_include.md). That document owns the
threat model and the push contract. This one owns **how the base is chosen** and what
refs exist to choose from.

## Implementation status

Every section below is implemented.

| Section | Status |
|---|---|
| What a clone actually gives you | implemented |
| Container metadata | implemented |
| The default-branch ladder | implemented (#758) |
| Choosing the diff base | implemented (#748) |
| Publish | implemented |
| Checkpoints | implemented |

---

## Why this is easy to get wrong

In solo development the merge target rarely moves while you work, so "one commit back",
"the branch point", and "the tip of main" are usually the same commit. Every wrong answer
looks right.

They diverge as soon as anything else lands first — an orchestrator running several issues
at once, or team development of any kind. Measured on this deployment over 2026-07-11 →
07-29: **85% of containers (440/512) overlapped in lifetime with another container**, with
up to 53 alive simultaneously. Concurrency is the normal case here, not the exception.

`design_merge_auto_include.md` records the same observation from the #675 side. That is not
a coincidence; both bugs are the same mistake made at different moments.

---

## What a clone actually gives you

`sandbox_initialize(clone_repo=...)` performs a **full clone**:

- No `--depth`, no `--single-branch`, no partial-clone filter.
- The fetch refspec is the standard `+refs/heads/*:refs/remotes/origin/*`, so **every
  remote branch is present locally** as `origin/<name>` from the moment the container
  exists.
- `git clone` sets `origin/HEAD`, so the remote's default branch is recoverable without
  network access.
- With `branch=`, git additionally checks that branch out. With `pr=N`, the PR branch is
  checked out and the PR's base branch is recorded (below).

Two consequences that are easy to assume wrongly:

1. **A base branch that existed at clone time needs no fetch.** `base="origin/v2"` works
   without any network round trip.
2. **A branch created after the container started is invisible.** Nothing re-fetches during
   a container's life, so a merge target that appears later is not reachable. This is a
   known gap, deliberately out of scope, not an oversight.

`origin/HEAD` is normally present, but it is not guaranteed — some initialization paths can
leave it unset, and that is precisely when the fallback ladder below stops being academic.

---

## Container metadata

`.sandbox-meta.json` lives in the home directory, not the workspace, so it never appears in
`git status`. It records:

| Key | Written when | Meaning |
|---|---|---|
| `clone_path` | always | where the repo was cloned |
| `base_branch` | `pr=N` only | the base branch of the checked-out PR |

**The initial HEAD is not recorded.** A design that wants "everything this container
produced since it started" would need that; the design below deliberately does not, because
it anchors on the merge target instead. If a future change needs the container's own start
point, adding the key is the honest way to get it — deriving it from `HEAD~1`, reflog, or
container creation time is not.

---

## The default-branch ladder

Resolving "the repository's default branch" is layered, and the layers exist because each
one can legitimately be unavailable. Since #758 the order is:

1. An explicit `base_branch` argument, when the caller supplied one.
2. `base_branch` from container metadata (a `pr=N` checkout).
3. `git symbolic-ref refs/remotes/origin/HEAD` — set by `git clone`, no network.
4. `git ls-remote --symref origin HEAD` — asks the remote. Bounded by a timeout; any
   failure means "this layer did not resolve" and falls through. Never fatal.
5. `main`, then `master` — the offline last resort.

Layer 5 is a guess and is documented as one. It exists so an offline container with a
conventional repository still works. **It must never be the only thing standing between
the caller and a wrong answer**, which is what layer 4 was added to prevent: before #756,
a repository whose default branch is neither `main` nor `master` had no escape.
`masuda-masuo/opencode` defaults to `dev` and has neither.

When every layer fails, the error says the default branch may be something other than
`main`/`master`. That sentence is load-bearing: the failure is otherwise indistinguishable
from a broken clone.

**PR creation does not use this ladder.** The base of a newly opened PR comes from
`GET /repos/{repo}`'s `default_branch`, host-side. That is authoritative and was never
affected by the bugs above.

---

## Choosing the diff base

The base for review is **the branch the work is intended to merge into**. This is the
standard meaning of a review diff — it is what a pull request shows.

### Compare from the divergence point, not from the tip

Comparing against the current tip of the merge target is wrong. When the target advances
while you work, the commits other people landed appear *inverted* inside your diff, as
though you had deleted them. Git's answer is to compare from the commit where the two
branches diverged — the merge base. `main...HEAD` is that, spelled with three dots.

The property that matters: **your diff does not change when the merge target moves.**

### The base must be the remote-tracking ref, not the branch name

A resolved base is a plain branch name such as `main`, and git reads that as the **local**
branch. That is the wrong commit here, and the reason is specific to how sunaba works: a
container checks out the cloned default branch and edits it directly, rather than creating
a feature branch. `publish` is what eventually moves the work onto a branch.

So the moment a `checkpoint` commits, the local `main` advances with HEAD, and
`merge-base(main, HEAD)` collapses to HEAD itself. Every committed change disappears from
the diff while the call still succeeds.

`origin/main` does not move — nothing re-fetches during a container's life — so it remains
the commit the work started from. An auto-resolved base (from the ladder or from container
metadata) is therefore rewritten to `origin/<branch>` when that ref exists. A base the
caller passed explicitly is used verbatim; the response reports whichever was used.

This is invisible to a test that mocks `git merge-base`, because the mock answers the same
regardless of which ref it was asked about. It was found by running the real tool against a
real container with a real checkpoint in it.

### End at the working tree, not at HEAD

Ordinary git workflow commits before opening a PR, so "your branch" is a settled thing. In
sunaba `publish` performs the commit, so at review time the work is typically **not
committed at all**. Measured: of 364 calls that hit the old default, **364 were in
containers that had performed no committing operation whatsoever**.

A review diff that ends at `HEAD` therefore shows nothing in the normal case. It must end
at the working tree.

### The two modes

No inspection of container state selects between these. Auto-detection was considered and
rejected: guessing wrong is exactly the failure #748 describes, and a cleverer guess would
reproduce it in a subtler form.

| Mode | Meaning | Range |
|---|---|---|
| `worktree=True` | what I have not committed | `HEAD` → working tree |
| default | what would land if I published now | `merge-base(base, HEAD)` → working tree |

`base` resolves through the ladder above. `HEAD~1` is still available, but only when the
caller asks for it explicitly — it answers "what did that one commit do", which is a real
question and a poor default.

### Untracked files

`git diff` does not report untracked files. This is standard git behaviour and a genuine
hazard for pre-publish review, because a worker's output is very often a **new file** and
`publish` will commit it. In #748 the real work was a modified `schema.py` plus a newly
created test file; the new file belonged to a category that never appears in a diff.

Untracked paths are therefore reported separately, mirroring `verify_in_container`'s
`diff_summary` — which is what made the original diagnosis possible.

**The index is not mutated to achieve this.** `git add -N` would make untracked files
visible to `git diff`, but a read-only review call must not change repository state, and
`publish` builds its commit from the index.

### Say what was used

Every response reports the base actually used and the mode that ran. The #748 incident was
survivable only because the returned diff was obviously unrelated; a wrong-but-plausible
one would have been accepted. Silence about the basis of an answer is the root defect,
independent of which default is chosen.

---

## Publish

The push path resolves its own base, because it needs a ref that exists **locally** to
reset onto:

1. `origin/<branch>` when the feature branch already exists on the remote — this is what
   preserves earlier commits when adding to an open PR.
2. Otherwise `origin/HEAD`.
3. Otherwise `git ls-remote --symref origin HEAD`, fetching the named branch if it is not
   already a local remote-tracking ref (#758).
4. Otherwise the `main`/`master` guess.
5. Otherwise **fail**. An unresolved base is an error, never a silent skip — skipping the
   reset is the manifest-leak bypass that `design_merge_auto_include.md` exists to prevent.

### Two traps, both with real damage behind them

**The remote base is the one fetched at container initialization.** Nothing refreshes it.
Publish one PR, merge it, then open a second from the same container, and the second is
still built on the pre-merge base: the commit reads as a fresh file addition and the PR
goes `CONFLICTING`. **After publishing one PR, start a new container** (#727).

Measured: 80 containers published more than once, so this is reached in practice, not
theoretical.

**Reusing an existing remote branch name can discard your worktree edits.** Working around
the first trap by editing in a fresh container and force-pushing to the same branch name
made publish switch to the existing remote branch and produce a commit containing only a
deletion — the PR ended up empty. **Always retry under a new branch name** and close the
old PR (#727).

---

## Checkpoints

`checkpoint` is `git add -A && git commit --allow-empty`. Two consequences:

- It sweeps **everything**, including files the caller never declared. That is why publish
  rebuilds the commit from an explicit manifest against the remote base rather than
  trusting `HEAD` — see `design_merge_auto_include.md`.
- After a checkpoint the working tree is clean, so a `worktree=True` diff shows nothing
  even though the container holds real work. This is why the default review mode starts
  from the merge base rather than from `HEAD`: it covers committed and uncommitted work in
  one answer.

---

## Invariants

1. The base for review is the merge target, never a positional offset from `HEAD`.
2. Diffs are computed from the divergence point, so an advancing target does not alter
   them.
3. A review answer states the base it used.
4. Nothing that reads state mutates it — no index changes, no fetches into the working
   branch.
5. A base that cannot be resolved is an error, never a silent fallback to "no base".
6. Layer 5 of the ladder is a guess and is labelled as one wherever it surfaces.

---

## History

| Issue | What it established |
|---|---|
| #675, #711, #712, #715, #716 | the publish reset, manifest and auto-include contract — see `design_merge_auto_include.md` |
| #727 | container reuse and branch-name reuse traps |
| #756 / #758 | the default branch is not always `main`/`master`; ask the remote before guessing |
| #748 | the review base is the merge target, not `HEAD~1` |
