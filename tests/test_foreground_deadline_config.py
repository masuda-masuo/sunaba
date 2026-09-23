"""Foreground exec deadline configuration contract.

Every synchronous foreground command path (``sandbox_exec``,
``run_python``, ``package_install``) arms a server-owned wall-clock
deadline so a call that outlives the MCP client's ~300s tool-call wait
answers with a terminal ``status: "timeout"`` result instead of leaving
the in-container command tree running and exhausting the container's PID
capacity.  This file pins the *configuration* contract of that deadline
(decision 2 of the brief):

* default 270s -- below the normal ~300s client boundary;
* a numeric override via ``SUNABA_FOREGROUND_TIMEOUT``;
* exactly ``0`` disables the deadline;
* invalid or negative values fall back to the default (only ``0``
  disables -- a misconfiguration must never silently turn the deadline
  off).

The default and the disabled case are not observable through the tools'
public behaviour within a seconds-fast test (they would require a 270s
hang), so they are pinned through the resolver itself, following the
``SUNABA_VERIFY_TIMEOUT`` precedent (issue #910,
``tests/test_verify_tools.py::TestVerifyDeadlineConfig``).

The file also pins decision 7: the generalization of the #910 machinery
must leave the verify deadline surface unchanged (its own resolver and
environment variable keep working), so the verify timeout/reap suite
stays green without semantic changes.
"""

from __future__ import annotations

import importlib
from typing import Callable, cast

import pytest

#: Environment variable resolving the per-call foreground deadline
#: (seconds).  Pinned here as the public contract: the implementer must
#: read exactly this name, ``0`` disables, invalid/negative fall back.
_FOREGROUND_TIMEOUT_ENV = "SUNABA_FOREGROUND_TIMEOUT"

#: Environment variable carrying the per-call private marker into every
#: foreground exec (the reaper matches it against ``/proc/<pid>/environ``).
#: Verify keeps its own ``SUNABA_VERIFY_MARKER`` untouched (decision 7).
_FOREGROUND_MARKER_ENV = "SUNABA_FOREGROUND_MARKER"

_VERIFY_TIMEOUT_ENV = "SUNABA_VERIFY_TIMEOUT"


def _foreground_resolver() -> Callable[[], float]:
    """The foreground deadline resolver, or a descriptive failure.

    The symbol does not exist on pristine code (the feature is not
    implemented yet); the import is resolved dynamically so the base
    failure is the missing behavior rather than a bare ImportError.
    """
    try:
        module = importlib.import_module("sunaba.edit_verify.shell")
    except ImportError:
        pytest.fail(
            "foreground deadline missing: sunaba.edit_verify.shell is not importable"
        )
    resolver = getattr(module, "resolve_foreground_deadline", None)
    if not callable(resolver):
        pytest.fail(
            "foreground deadline missing: resolve_foreground_deadline() is not "
            f"implemented (the {_FOREGROUND_TIMEOUT_ENV} deadline configuration "
            "contract for sandbox_exec / run_python / package_install)"
        )
    return cast(Callable[[], float], resolver)


class TestForegroundDeadlineConfig:
    """Deadline resolution contract (default/override/disabled/invalid/negative)."""

    def test_default_is_270_below_client_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset env resolves to 270.0s, under the ~300s MCP client boundary."""
        monkeypatch.delenv(_FOREGROUND_TIMEOUT_ENV, raising=False)
        deadline = _foreground_resolver()()
        assert deadline == 270.0
        # The whole point: the server must answer before the MCP client's
        # ~300s tool-call wait.
        assert deadline < 300.0

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A positive numeric override is used as-is."""
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, "3.5")
        assert _foreground_resolver()() == 3.5

    def test_zero_disables_the_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exactly ``0`` disables the deadline (a deliberate opt-out)."""
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, "0")
        assert _foreground_resolver()() == 0.0

    def test_invalid_values_fall_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Garbage values can never silently disable the deadline."""
        for bad in ("abc", "", "   ", "12x"):
            monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, bad)
            assert _foreground_resolver()() == 270.0, f"invalid value {bad!r}"

    def test_negative_values_fall_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Negative values fall back to the default, never disable it."""
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, "-5")
        assert _foreground_resolver()() == 270.0


class TestVerifyDeadlineSurfaceUnchanged:
    """Decision 7: verify behaviour and tests remain unchanged.

    These pins would go red if the shared-machinery generalization
    removed or renamed the verify-specific resolver or its environment
    variable.  The full verify timeout/reap behaviour itself stays pinned
    by ``tests/test_verify_tools.py`` and
    ``tests/test_verify_timeout_wrapper.py`` (unchanged, still green).
    """

    def test_verify_deadline_resolver_and_env_unchanged(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``resolve_verify_deadline`` still resolves ``SUNABA_VERIFY_TIMEOUT``."""
        try:
            module = importlib.import_module("sunaba.tools.verify")
        except ImportError:
            pytest.fail("verify surface missing: sunaba.tools.verify is not importable")
        resolver = getattr(module, "resolve_verify_deadline", None)
        if not callable(resolver):
            pytest.fail("verify surface missing: resolve_verify_deadline() no longer exists")
        monkeypatch.delenv(_VERIFY_TIMEOUT_ENV, raising=False)
        assert cast(Callable[[], float], resolver)() == 270.0
        # The verify-specific env name is still the one the resolver reads.
        assert getattr(module, "_VERIFY_TIMEOUT_ENV", None) == _VERIFY_TIMEOUT_ENV
