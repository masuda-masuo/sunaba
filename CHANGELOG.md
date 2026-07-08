# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
The compatibility policy (what counts as a breaking change) is described in
[README.md#compatibility-policy](README.md#compatibility-policy).

## [1.0.0] - 2026-07-08

Initial versioned release. For the full current tool surface, see
[README.md#available-tools](README.md#available-tools).

### Changed

- **Error return shape unified** across all tools to
  `{"status": "error", "error": "<message>"}` (tool-specific extra fields,
  e.g. `verify_in_container`'s `gate_passed`, are still allowed) (#467).
- **`search_in_container` return shape changed** from a bare array to
  `{"matches": [...], "shown": ..., "total": ..., "truncated": ..., "next_offset": ...}`;
  default search path now resolves to the repository root instead of `/`;
  added `glob`, `ignore_case`, `context`, `output_mode`, `offset` parameters
  (#469).
- **Environment variables unified** under the `CODE_SANDBOX_*` prefix. Old
  `CSB_*` names and `SHIORI_REPOS_PATH` are read as fallbacks and log a
  deprecation warning; they will be removed in a future major release (#468).

### Removed

- Unused tools with no real-world usage: `sandbox_exec_diff`,
  `rerun_failed`, `run_test_environment`, `stop_test_environment`,
  `wait_for_condition` (#458).
- The `result_cache` mechanism, including `sandbox_cache_invalidate` and
  `sandbox_cache_stats` (#459).
- The `dry_run` / confirmation-token two-step on `publish` and
  `sandbox_issue_write` (both are now one-shot); `sandbox_approve` /
  `sandbox_reject` / `sandbox_approval_status`; `sandbox_create_pr`
  (superseded by `publish`) (#438).
- `inject_vcs_token` — VCS tokens are never injected into the container; the
  host resolves and uses them directly for `publish` / `sandbox_issue_write`
  (#441).

None of the removals above shipped with a compatibility shim: all predate
this first versioned release, so there were no external consumers to break.
