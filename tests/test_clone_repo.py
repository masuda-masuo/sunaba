"""Tests for the sandbox_initialize clone path: validation and network clone."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sunaba.tools.container import (
    _clone_repo_via_network,
    _validate_clone_repo,
)


class TestValidateCloneRepo:
    """Tests for _validate_clone_repo."""

    def test_valid_owner_name(self) -> None:
        owner, name = _validate_clone_repo("masuda-masuo/shiori")
        assert owner == "masuda-masuo"
        assert name == "shiori"

    def test_valid_with_dots(self) -> None:
        owner, name = _validate_clone_repo("my.org/my-repo_v2")
        assert owner == "my.org"
        assert name == "my-repo_v2"

    def test_empty_string(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            _validate_clone_repo("")

    def test_no_slash(self) -> None:
        with pytest.raises(ValueError, match="owner/name"):
            _validate_clone_repo("masuda-masuo")

    def test_too_many_slashes(self) -> None:
        with pytest.raises(ValueError, match="owner/name"):
            _validate_clone_repo("a/b/c")

    def test_empty_owner(self) -> None:
        with pytest.raises(ValueError, match="owner/name"):
            _validate_clone_repo("/shiori")

    def test_empty_name(self) -> None:
        with pytest.raises(ValueError, match="owner/name"):
            _validate_clone_repo("masuda-masuo/")

    def test_invalid_characters(self) -> None:
        with pytest.raises(ValueError, match="alphanumeric"):
            _validate_clone_repo("bad@owner/repo")

    def test_invalid_characters_in_name(self) -> None:
        with pytest.raises(ValueError, match="alphanumeric"):
            _validate_clone_repo("owner/rep o")


class TestCloneRepoViaNetwork:
    """Tests for _clone_repo_via_network (Issue #146, PR #170 review)."""

    def _container(self, exit_code: int, output: bytes) -> MagicMock:
        c = MagicMock()
        c.id = "abc123def456"
        c.exec_run.return_value = (exit_code, output)
        return c

    def test_success_returns_message(self) -> None:
        c = self._container(0, b"")
        msg = _clone_repo_via_network(c, "abc123def456", "owner/repo", "/workspace")
        assert "owner/repo" in msg
        assert "/workspace" in msg

    def test_failure_without_token_hints_read_grant(self) -> None:
        c = self._container(1, b"gh: Could not resolve to a Repository")
        with pytest.raises(RuntimeError) as exc:
            _clone_repo_via_network(c, "abc123def456", "owner/private", "/tmp/repo")
        assert "read grant" in str(exc.value)

    def test_failure_with_token_omits_hint(self) -> None:
        c = self._container(1, b"some other gh error")
        with pytest.raises(RuntimeError) as exc:
            _clone_repo_via_network(
                c, "abc123def456", "owner/private", "/tmp/repo",
                authenticated=True,
            )
        assert "read grant" not in str(exc.value)

    def test_anonymous_git_clone_without_token(self) -> None:
        """Issue #333: no token -> anonymous git clone (public works)."""
        c = self._container(0, b"")
        _clone_repo_via_network(c, "abc123def456", "owner/repo", "/tmp/repo")
        cmd = c.exec_run.call_args_list[0][0][0][-1]
        assert "git clone" in cmd
        assert "https://github.com/owner/repo.git" in cmd
        assert "GIT_TERMINAL_PROMPT=0" in cmd
        assert "gh repo clone" not in cmd

    def test_gh_clone_with_token(self) -> None:
        """Issue #333: an authenticated container keeps gh repo clone (private)."""
        c = self._container(0, b"")
        _clone_repo_via_network(
            c, "abc123def456", "owner/repo", "/tmp/repo",
            authenticated=True,
        )
        cmd = c.exec_run.call_args_list[0][0][0][-1]
        assert "gh repo clone owner/repo" in cmd

    @patch("sunaba.tools.container.record_boundary_crossing")
    def test_read_grant_success_is_journaled(self, mock_record) -> None:
        """#421: a proxy-read-grant-authorized clone records approved=True."""
        c = self._container(0, b"")
        _clone_repo_via_network(
            c, "abc123def456", "owner/repo", "/tmp/repo", open_read_grant=True,
        )
        mock_record.assert_called_once_with(
            "abc123def456",
            "clone_repo",
            "repo=owner/repo dest=/tmp/repo proxy_read_grant=True",
            approved=True,
        )

    @patch("sunaba.tools.container.record_boundary_crossing")
    def test_read_grant_failure_is_journaled(self, mock_record) -> None:
        """#421: a denied/failed proxy read grant must show up too, not just success."""
        c = self._container(1, b"fatal: could not read Username")
        with pytest.raises(RuntimeError):
            _clone_repo_via_network(
                c, "abc123def456", "owner/repo", "/tmp/repo",
                open_read_grant=True,
            )
        mock_record.assert_called_once_with(
            "abc123def456",
            "clone_repo",
            "repo=owner/repo dest=/tmp/repo proxy_read_grant=True",
            approved=False,
        )

    @patch("sunaba.tools.container.record_boundary_crossing")
    def test_no_read_grant_is_not_journaled(self, mock_record) -> None:
        """A plain (non-proxied) clone is unaffected -- no new journal entry."""
        c = self._container(0, b"")
        _clone_repo_via_network(c, "abc123def456", "owner/repo", "/tmp/repo")
        mock_record.assert_not_called()


class TestCloneWarnsWithoutToken:
    """Issue #333 follow-up: warn at clone time when no token (push fails)."""

    def test_network_clone_without_token_warns(self) -> None:
        from sunaba.tools.container import _try_clone_into_container
        c = MagicMock()
        c.exec_run.return_value = (0, b"")
        res = _try_clone_into_container(
            c, "abc123def456", "owner/repo", "/tmp/repo"
        )
        assert res.error is None
        assert "WARNING" in res.msg
        # Issue #347: warning flags the anonymous clone, not a re-init demand.
        assert "anonymous clone" in res.msg

    def test_network_clone_with_token_no_warning(self) -> None:
        from sunaba.tools.container import _try_clone_into_container
        c = MagicMock()
        c.exec_run.return_value = (0, b"")
        res = _try_clone_into_container(
            c, "abc123def456", "owner/repo", "/tmp/repo",
            authenticated=True,
        )
        assert res.error is None
        assert "WARNING" not in res.msg


class TestEditableInstallCmd:
    """Tests for _editable_install_cmd."""

    def test_runtime_installer_selection(self) -> None:
        # #390: uv when $VIRTUAL_ENV is set (venv-baked images, PR #388),
        # plain pip otherwise (venv-less images, the #380 constraint).
        from sunaba.tools.container import _editable_install_cmd

        cmd = _editable_install_cmd('".[dev]"')

        assert cmd.startswith('if [ -n "$VIRTUAL_ENV" ]')
        assert "command -v uv" in cmd
        assert "then uv pip install -q -e" in cmd

    def test_pip_fallback_branch(self) -> None:
        # The pip branch must stay byte-identical to the pre-#390 command so
        # venv-less images keep the user-site fallback behaviour.
        from sunaba.tools.container import _editable_install_cmd

        cmd = _editable_install_cmd('".[dev]"')

        assert "else pip install -e '\".[dev]\"' -q; fi" in cmd

    def test_no_temp_venv(self) -> None:
        # Regression for #383: the former uv path installed into a mktemp
        # venv and deleted it right away, discarding the install.
        from sunaba.tools.container import _editable_install_cmd

        cmd = _editable_install_cmd('".[dev]"')

        assert "mktemp" not in cmd
        assert "rm -rf" not in cmd

    def test_quotes_target(self) -> None:
        from sunaba.tools.container import _editable_install_cmd

        cmd = _editable_install_cmd("foo[bar]")

        assert "'foo[bar]'" in cmd, "shlex.quote should wrap target in single quotes"

    def test_pip_args_appended(self) -> None:
        from sunaba.tools.container import _editable_install_cmd

        cmd = _editable_install_cmd('".[dev]"', pip_args="--index-url https://example.com")

        assert "--index-url" in cmd
        assert "https://example.com" in cmd
        assert cmd.count("--index-url") == 2, "both uv and pip branches should have the arg"

    def test_pip_args_empty_string(self) -> None:
        from sunaba.tools.container import _editable_install_cmd

        cmd_with_empty = _editable_install_cmd('".[dev]"', pip_args="")
        cmd_with_none = _editable_install_cmd('".[dev]"')

        # Verify no extra whitespace when pip_args is empty/missing
        assert "  -q" not in cmd_with_empty
        assert "  -q" not in cmd_with_none
        assert cmd_with_empty == cmd_with_none

    def test_pip_args_multiword(self) -> None:
        from sunaba.tools.container import _editable_install_cmd

        cmd = _editable_install_cmd(
            '".[dev]"',
            pip_args="--extra-index-url https://example.com --no-build-isolation",
        )

        assert "--extra-index-url" in cmd
        assert "https://example.com" in cmd
        assert "--no-build-isolation" in cmd

    def test_pip_args_shell_injection_prevented(self) -> None:
        from sunaba.tools.container import _editable_install_cmd

        cmd = _editable_install_cmd('".[dev]"', pip_args='; rm -rf /')

        # The semicolon must be quoted so the shell treats it as a literal
        # pip argument rather than a command separator.
        assert "\\';\\'" in cmd or "';'" in cmd
        # The raw injection string should NOT appear unquoted as-is.
        assert "'; rm -rf /'" not in cmd
