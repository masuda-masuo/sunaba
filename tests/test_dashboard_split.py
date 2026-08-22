"""Structural guards for the ``sunaba.dashboard`` split.

The 2,490-line dashboard module was split three ways: the markup moved to
``sunaba.dashboard_templates``, the pure renderers to
``sunaba.dashboard_render``, and everything that reads the journal, inspects
containers or holds process state stayed behind in ``sunaba.dashboard``.

Nothing here checks rendered HTML -- the existing dashboard tests do that, and
they are the real proof the split changed no behaviour.  These tests pin the
*shape* of the split instead, because the way it can quietly break is
invisible to a rendering test.  ``mock.patch`` rebinds a name in exactly one
module; the rest of the suite patches the data-gathering names on
``sunaba.dashboard``.  If a moved function called one of them from its new
home, it would resolve the real implementation there, the patch would never
bite, and those tests would keep passing while testing nothing at all.
"""
from __future__ import annotations

import inspect

import sunaba.dashboard
import sunaba.dashboard_render
import sunaba.dashboard_templates

#: Names the suite patches on ``sunaba.dashboard``, plus the mutable module
#: state the server owns.  Every use of these has to stay in that one module.
FORBIDDEN = (
    "read_journal",
    "read_journal_snapshot",
    "get_runs",
    "list_managed_containers",
    "sandbox_stop",
    "cached_disk_usage",
    "_cached_",
    "_dashboard_host",
    "_dashboard_port",
    "_journal_agg_",
    "threading",
    "_CSRF_TOKEN",
)

#: Names other modules and tests reach for as ``sunaba.dashboard.<name>``.  The
#: moved ones are re-exported, so this also pins the re-export list.
PUBLIC_SURFACE = (
    "start_dashboard",
    "stop_dashboard",
    "get_dashboard_url",
    "_DashboardHandler",
    "_host_allowed",
    "_render_containers_page",
    "_containers_fragments",
    "_cached_agg_state",
    "_run_summaries_from_state",
    "_render_insights_page",
    "_escape",
)

SPLIT_MODULES = (sunaba.dashboard_render, sunaba.dashboard_templates)


class TestSplitModulesReachNoState:
    """The two new modules must not touch anything the tests patch."""

    def test_no_forbidden_name_appears(self) -> None:
        """A forbidden name in either module means a patch somewhere is dead.

        A plain substring check, deliberately: it catches the name in a
        comment or a docstring too, which is noise, but the alternative --
        only flagging calls -- would miss an alias or a ``getattr``.
        """
        for module in SPLIT_MODULES:
            source = inspect.getsource(module)
            found = [name for name in FORBIDDEN if name in source]
            assert found == [], (
                f"{module.__name__} refers to {found}; patches applied to "
                f"sunaba.dashboard would not reach it"
            )


class TestDashboardKeepsItsSurface:
    """Moving a definition must not move where callers find it."""

    def test_expected_names_are_importable(self) -> None:
        """Every name the rest of the tree imports is still on the module."""
        missing = [name for name in PUBLIC_SURFACE if not hasattr(sunaba.dashboard, name)]
        assert missing == [], f"sunaba.dashboard no longer exposes {missing}"


class TestTemplatesModuleIsOnlyStrings:
    """The template module holds markup and nothing else."""

    def test_every_attribute_is_a_string(self) -> None:
        """No functions, no imports, no state -- only ``str`` constants."""
        offenders = {
            name: type(value).__name__
            for name, value in vars(sunaba.dashboard_templates).items()
            if not name.startswith("__") and not isinstance(value, str)
        }
        assert offenders == {}, f"non-string attributes in dashboard_templates: {offenders}"
