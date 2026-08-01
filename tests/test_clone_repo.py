"""Tests for the sandbox_initialize clone path: validation and network clone."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sunaba.tools.container import (
    _clone_repo_via_network,
    _install_repo_deps,
    _manifest_probe_cmd,
    _npm_install_cmd,
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

    @patch("sunaba.tools.container.clone.record_boundary_crossing")
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

    @patch("sunaba.tools.container.clone.record_boundary_crossing")
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

    @patch("sunaba.tools.container.clone.record_boundary_crossing")
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


class TestNpmInstallCmd:
    """Tests for _npm_install_cmd (#798)."""

    def test_ci_with_lockfile(self) -> None:
        assert _npm_install_cmd("/tmp/repo", lockfile=True) == "cd /tmp/repo && npm ci"

    def test_install_without_lockfile(self) -> None:
        assert _npm_install_cmd("/tmp/repo", lockfile=False) == "cd /tmp/repo && npm install"


class TestInstallRepoDeps:
    """Unit tests for the manifest-aware deps dispatcher (#798)."""

    @staticmethod
    def _container(exec_returns):
        container = MagicMock()
        container.exec_run.side_effect = exec_returns
        return container

    def test_allow_network_false_skips_everything(self) -> None:
        """allow_network=False: no exec at all, no note (like the pip path)."""
        container = self._container([])
        deps = _install_repo_deps(
            container, "owner/repo", "/tmp/repo", "[dev]", allow_network=False
        )
        assert deps == ("", False)
        container.exec_run.assert_not_called()

    def test_pip_extras_none_skips_only_python(self) -> None:
        """pip_extras=None skips the pip step but npm still runs."""
        container = self._container([
            (0, (b"pyproject.toml\npackage.json\n", b"")),  # probe
            (0, (b"added 3 packages", b"")),  # npm install
        ])
        deps = _install_repo_deps(container, "owner/repo", "/tmp/repo", None)
        assert deps.failed is False
        assert deps.note == "deps: pip skipped (pip_extras=None); npm install ok"
        cmds = [c.args[0][-1] for c in container.exec_run.call_args_list]
        assert cmds == [
            "cd /tmp/repo && for f in pyproject.toml setup.py package.json "
            "package-lock.json go.mod Cargo.toml; do [ -e \"$f\" ] && printf "
            "'%s\\n' \"$f\" || true; done",
            "cd /tmp/repo && npm install",
        ]

    def test_multi_language_runs_both_installers(self) -> None:
        """pyproject.toml + package.json + lockfile: pip and npm ci both run."""
        container = self._container([
            (0, (b"pyproject.toml\npackage.json\npackage-lock.json\n", b"")),
            (0, (b"Installed", b"")),  # pip
            (0, (b"added 9 packages", b"")),  # npm ci
        ])
        deps = _install_repo_deps(container, "owner/repo", "/tmp/repo", "[dev]")
        assert deps.failed is False
        assert deps.note == "deps: pip install ok; npm ci ok"
        cmds = [c.args[0][-1] for c in container.exec_run.call_args_list]
        assert any("pip install" in cmd for cmd in cmds)
        assert cmds[2] == "cd /tmp/repo && npm ci"

    def test_npm_failure_is_reported_and_non_fatal(self) -> None:
        container = self._container([
            (0, (b"package.json\n", b"")),
            (1, (b"", b"npm ERR! code E404")),
        ])
        deps = _install_repo_deps(container, "owner/repo", "/tmp/repo", "[dev]")
        assert deps.failed is True
        assert deps.note == "deps: npm install failed (exit 1): npm ERR! code E404"

    def test_probe_failure_treated_as_no_manifest(self) -> None:
        """A failed probe degrades to the skip note, not an error."""
        container = self._container([
            (1, (b"", b"probe failed")),
        ])
        deps = _install_repo_deps(container, "owner/repo", "/tmp/repo", "[dev]")
        assert deps.failed is False
        assert deps.note == "deps: no manifest detected — skipped"
        # No installer exec followed the failed probe.
        assert container.exec_run.call_count == 1


class TestManifestProbeShellContract:
    """The probe command is executed through a real shell in production, so
    its exit-status contract is pinned against /bin/sh (dash), not against a
    mocked exec_run (#798 review).

    Regression: the original probe body ``[ -e "$f" ] && printf ...`` made
    the ``for`` loop exit with the status of its last executed command --
    ``[ -e Cargo.toml ]`` failing for every repo without a root Cargo.toml
    (the last probed entry).  The probe then exited 1 even though it had
    printed the manifests, and _detect_manifests discarded them, so no
    installer ever ran on a real repo.
    """

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            (["package.json", "package-lock.json"], {"package.json", "package-lock.json"}),
            (["package.json"], {"package.json"}),
            (["pyproject.toml"], {"pyproject.toml"}),
            (["setup.py", "pyproject.toml"], {"setup.py", "pyproject.toml"}),
            (["pyproject.toml", "package.json"], {"pyproject.toml", "package.json"}),
            (["go.mod"], {"go.mod"}),
            (["Cargo.toml"], {"Cargo.toml"}),
            ([], set()),
        ],
    )
    def test_exits_zero_and_lists_present_manifests(self, tmp_path, files, expected):
        """Exit 0 and the exact manifest set, whatever is (not) present."""
        import subprocess

        dest = tmp_path / ("repo_" + "_".join(files) if files else "repo_empty")
        dest.mkdir()
        for name in files:
            (dest / name).write_text("{}")
        cp = subprocess.run(
            ["/bin/sh", "-c", _manifest_probe_cmd(str(dest))],
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, (
            f"probe exited {cp.returncode} for {files}: {cp.stderr!r}"
        )
        found = {line.strip() for line in cp.stdout.splitlines() if line.strip()}
        assert found == expected, f"for {files}: got {found!r}"

    def test_exits_zero_when_cargo_toml_is_the_only_absent_entry(self, tmp_path):
        """The exact regression: package.json present, Cargo.toml (last
        probed entry) absent -- must exit 0 and report the JS manifests."""
        import subprocess

        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "package-lock.json").write_text("{}")
        cp = subprocess.run(
            ["/bin/sh", "-c", _manifest_probe_cmd(str(tmp_path))],
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, f"probe exited {cp.returncode}: {cp.stderr!r}"
        found = {line.strip() for line in cp.stdout.splitlines() if line.strip()}
        assert found == {"package.json", "package-lock.json"}


class TestInstallRepoDepsRealShell:
    """End-to-end dispatcher cases: the probe exec runs through a real
    /bin/sh against a fixture repo, so detection is proven on the real shell
    (installer steps are intercepted, not executed) (#798 review)."""

    @staticmethod
    def _container_with_real_probe(tmp_path):
        import subprocess

        calls: list[list[str]] = []

        def exec_run(cmd, **kwargs):
            calls.append(cmd)
            shell = cmd[2] if isinstance(cmd, list) and cmd[:2] == ["/bin/sh", "-c"] else ""
            if "npm ci" in shell or "npm install" in shell or "pip install" in shell:
                # Installer steps: canned success, never actually run.
                return (0, (b"Installed", b""))
            cp = subprocess.run(cmd, capture_output=True, text=True)
            return cp.returncode, (cp.stdout.encode(), cp.stderr.encode())

        container = MagicMock()
        container.exec_run.side_effect = exec_run
        return container, calls

    def test_js_only_repo_triggers_npm_ci(self, tmp_path):
        """A JS-only repo (package.json + lockfile) yields `npm ci` through
        the real probe -- the issue's headline outcome."""
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "package-lock.json").write_text("{}")
        container, calls = self._container_with_real_probe(tmp_path)

        deps = _install_repo_deps(container, "owner/repo", str(tmp_path), "[dev]")
        assert deps.failed is False
        assert deps.note == "deps: npm ci ok"
        install_cmds = [
            c for c in calls if isinstance(c, list) and "npm ci" in c[2]
        ]
        assert install_cmds == [["/bin/sh", "-c", f"cd {str(tmp_path)} && npm ci"]]
        assert not any("pip install" in (c[2] if len(c) > 2 else "") for c in calls)

    def test_js_only_without_lockfile_triggers_npm_install(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        container, calls = self._container_with_real_probe(tmp_path)

        deps = _install_repo_deps(container, "owner/repo", str(tmp_path), "[dev]")
        assert deps.note == "deps: npm install ok"
        install_cmds = [
            c for c in calls if isinstance(c, list) and "npm install" in c[2]
        ]
        assert install_cmds == [["/bin/sh", "-c", f"cd {str(tmp_path)} && npm install"]]

    def test_python_only_repo_triggers_pip_install(self, tmp_path):
        """A python-only repo still gets the pip install (criterion 2)."""
        (tmp_path / "pyproject.toml").write_text("{}")
        container, calls = self._container_with_real_probe(tmp_path)

        deps = _install_repo_deps(container, "owner/repo", str(tmp_path), "[dev]")
        assert deps.failed is False
        assert deps.note == "deps: pip install ok"
        pip_cmds = [
            c for c in calls if isinstance(c, list) and "pip install" in c[2]
        ]
        assert len(pip_cmds) == 1
        assert "pip install -e '.[dev]' -q" in pip_cmds[0][2]
        assert not any("npm" in (c[2] if len(c) > 2 else "") for c in calls)
