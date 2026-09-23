"""Workflow-guide documentation contract for the foreground deadline.

Decision 2 of the brief: the foreground exec deadline is *a documented
environment setting* -- ``0`` disables it, a positive value overrides
the 270s default, invalid/negative fall back.  The project pins its
operational documentation: ``test_workflow_guide.py`` reads
``workflow_guide.md``, and the #910 verify deadline is documented there
(the ``SUNABA_VERIFY_TIMEOUT`` section).  The foreground deadline must
be documented in the same guide, or an operator cannot discover or
disable it.  These tests are red until the guide documents the setting.
"""

from __future__ import annotations

import pathlib

from sunaba import workflow_guide

#: The documented environment setting (same name the resolver reads;
#: pinned by tests/test_foreground_deadline_config.py).
_FOREGROUND_TIMEOUT_ENV = "SUNABA_FOREGROUND_TIMEOUT"


def _guide_text() -> str:
    """Read the workflow guide markdown directly from the source tree."""
    return (
        pathlib.Path(workflow_guide.__file__).resolve().parent / "workflow_guide.md"
    ).read_text("utf-8")


class TestForegroundDeadlineDocumented:
    """The workflow guide names the setting and its semantics."""

    def test_workflow_guide_names_the_env_setting(self) -> None:
        """The guide must name ``SUNABA_FOREGROUND_TIMEOUT`` so operators
        can find and override/disable the deadline."""
        text = _guide_text()
        assert _FOREGROUND_TIMEOUT_ENV in text, (
            f"workflow_guide.md must document {_FOREGROUND_TIMEOUT_ENV} "
            "(the foreground exec deadline environment setting)"
        )

    def test_workflow_guide_pins_default_override_and_disable(self) -> None:
        """The guide must state the 270s default and that ``0`` disables
        the deadline (mirroring the ``SUNABA_VERIFY_TIMEOUT`` section)."""
        text = _guide_text()
        lines = [line for line in text.splitlines() if _FOREGROUND_TIMEOUT_ENV in line]
        assert lines, f"workflow_guide.md does not mention {_FOREGROUND_TIMEOUT_ENV}"
        assert any("270" in line for line in lines), (
            "workflow_guide.md must state the 270s default for the foreground deadline"
        )
        assert any("disabl" in line for line in lines), (
            "workflow_guide.md must state that 0 disables the foreground deadline"
        )
