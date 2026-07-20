"""Tests for publish advanced features: squash, force push, API fallback, token routing."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.vcs import publish
from tests.conftest import _decode, _make_client_mock, _make_container_mock


class TestPublishSquashCheckpoints:
    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    """Tests for publish with automatic checkpoint squash."""

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_execute_squash_checkpoints(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Publish should squash unpushed checkpoints with reset --soft."""
        container = _make_container_mock([
            (0, b"", b""),  # git ls-files --others --exclude-standard
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b
            (0, b"", b""),  # git add
            (0, b"main\n", b""),  # rev-parse @{u}
            (0, b"abc1234 First checkpoint\n", b""),  # log oneline
            (0, b"", b""),  # reset --soft
            (0, b"", b""),  # readd
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"pushed", b""),  # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
        ))

        assert result["status"] == "pushed"
        reset_calls = [
            c[0][0][2] for c in container.exec_run.call_args_list
            if "reset --soft" in c[0][0][2]
        ]
        assert len(reset_calls) == 1

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_execute_squash_checkpoints_no_tracking(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Publish with no tracking branch should skip squash."""
        container = _make_container_mock([
            (0, b"", b""),  # git ls-files --others --exclude-standard
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),
            (0, b"[fix/x abc1234] Fix", b""),
            (0, b"pushed", b""),
            (0, b"abc1234def5678", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
        ))

        assert result["status"] == "pushed"


class TestPublishAllowForcePush:
    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    """Tests for publish with allow_force_push=True."""

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_execute_allow_force_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """allow_force_push=True should include --force in push command."""
        container = _make_container_mock([
            (0, b"", b""),  # git ls-files --others --exclude-standard
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),
            (0, b"[fix/x abc1234] Fix", b""),
            (0, b"pushed", b""),
            (0, b"abc1234def5678", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            allow_force_push=True,
        ))

        assert result["status"] == "pushed"
        push_calls = [
            c[0][0][2] for c in container.exec_run.call_args_list
            if "push origin" in c[0][0][2]
        ]
        assert len(push_calls) == 1
        assert "--force" in push_calls[0]


class TestPublishApiPushFallback:
    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    """Tests for publish when git push fails and falls back to _try_api_push."""

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_execute_api_push_fallback_succeeds(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When git push fails, _try_api_push should be used as fallback."""
        push_json = json.dumps({"sha": "b" * 40}).encode()
        container = _make_container_mock([
            (0, b"", b""),  # git ls-files --others --exclude-standard
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),
            (0, b"[fix/x abc1234] Fix", b""),
            (1, b"", b"remote rejected: permission denied"),
            (0, b"abc1234def5678", b""),
            (0, b"", b""),
            (0, push_json, b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
        ))

        assert result["status"] == "pushed"
        assert result["sha"] == "bbbbbbb"

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_execute_api_push_fallback_fails(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When both git push and API push fail, return error."""
        container = _make_container_mock([
            (0, b"", b""),  # git ls-files --others --exclude-standard
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),
            (0, b"[fix/x abc1234] Fix", b""),
            (1, b"", b"remote rejected"),
            (0, b"abc1234def5678", b""),
            (0, b"", b""),
            (1, b"", b"API push failed too"),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
        ))

        assert result["status"] == "error"
        assert result["step"] == "git_push"


class TestPublishLazyTokenInjection:
    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    """Tests for lazy VCS-token injection at push time (Issue #347).

    The token is resolved host-side and handed only to the push / PR
    execs, so a container that carries no VCS token of its own can still
    publish, while read-only git execs never see a credential.
    """

    @staticmethod
    def _simple_push_returns() -> list[tuple[int, bytes, bytes]]:
        # checkout, ls-files, git add, no-upstream (skip squash), commit, push, HEAD
        return [
            (0, b"", b""),  # git ls-files --others --exclude-standard
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),
            (0, b"[fix/x abc1234] Fix", b""),
            (0, b"", b""),
            (0, b"abc1234def5678", b""),
        ]

    @staticmethod
    def _env_of(call) -> dict | None:
        # exec_run is called as exec_run([...], stdout=, stderr=, environment=)
        return call.kwargs.get("environment")

    @patch("sunaba.tools.vcs.publishing._resolve_vcs_token")
    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_token_injected_into_push_exec_only(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """A host-resolved token reaches the push exec but not read-only execs."""
        mock_resolve.return_value = "ghs_lazytoken"

        container = _make_container_mock(self._simple_push_returns())
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
        ))
        assert result["status"] == "pushed"

        calls = container.exec_run.call_args_list
        push_calls = [c for c in calls if "push origin" in c.args[0][2]]
        assert len(push_calls) == 1
        push_env = self._env_of(push_calls[0])
        assert push_env == {
            "GITHUB_TOKEN": "ghs_lazytoken",
            "GH_TOKEN": "ghs_lazytoken",
        }

        # Least-privilege: read-only git execs carry no credential.
        readonly_calls = [
            c for c in calls if "push origin" not in c.args[0][2]
        ]
        assert readonly_calls  # sanity
        assert all(self._env_of(c) is None for c in readonly_calls)

    @patch("sunaba.tools.vcs.publishing._resolve_vcs_token")
    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_no_host_token_leaves_push_env_unset(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """With no host token, the push exec env is None.

        The container carries no VCS token of its own (#356/#439), so there
        is no credential to fall back on -- the push proceeds without one.
        """
        mock_resolve.return_value = ""

        container = _make_container_mock(self._simple_push_returns())
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
        ))
        assert result["status"] == "pushed"

        calls = container.exec_run.call_args_list
        assert all(self._env_of(c) is None for c in calls)

    @patch("sunaba.tools.vcs.publishing._resolve_vcs_token")
    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_token_injected_into_api_push_fallback(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """When git push fails, the API-push script exec carries the token."""
        mock_resolve.return_value = "ghs_lazytoken"

        push_json = json.dumps({"sha": "b" * 40}).encode()
        container = _make_container_mock([
            (0, b"", b""),              # git ls-files --others --exclude-standard
            (0, b"none\n", b""),              # MERGE_HEAD check
            (0, b"", b""),              # checkout -b
            (0, b"", b""),              # git add
            (1, b"", b"no upstream"),  # skip squash
            (0, b"[fix/x abc] Fix", b""),  # commit
            (1, b"", b"permission denied"),  # git push fails
            (0, b"abc1234def5678", b""),     # rev-parse HEAD
            (0, b"", b""),              # api-push: write script
            (0, push_json, b""),        # api-push: run script
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
        ))
        assert result["status"] == "pushed"
        assert result["sha"] == "bbbbbbb"

        calls = container.exec_run.call_args_list
        script_calls = [
            c for c in calls if "_sandbox_create_pr.py" in c.args[0][-1]
            and "python3" in c.args[0][-1]
        ]
        assert len(script_calls) == 1
        assert script_calls[0].kwargs.get("environment") == {
            "GITHUB_TOKEN": "ghs_lazytoken",
            "GH_TOKEN": "ghs_lazytoken",
        }


class TestPublishProxiedCredentialRouting:
    """With the egress proxy configured the credential goes to the proxy (#356).

    The push exec and the API-push fallback must stay token-free (the proxy
    injects ``Authorization`` into the authorized push itself, and a token in
    the fallback would let the Objects API bypass the proxy gate); only the
    gh-pr-create exec still carries it, because api.github.com writes are not
    proxy-gated yet (#360).
    """

    @staticmethod
    def _env_of(call) -> dict | None:
        return call.kwargs.get("environment")

    @patch("sunaba.tools.vcs.publishing.proxy_lifecycle.ensure_egress_proxy")
    @patch("sunaba.tools.vcs.publishing.authorized_push_grant")
    @patch("sunaba.tools.vcs.publishing.proxy_configured", return_value=True)
    @patch("sunaba.tools.vcs.publishing._resolve_vcs_token")
    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_push_exec_token_free_and_grant_carries_credential(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
        mock_proxied: MagicMock,
        mock_grant: MagicMock,
        mock_ensure: MagicMock,
    ) -> None:
        mock_resolve.return_value = "ghs_lazytoken"

        container = _make_container_mock(
            TestPublishLazyTokenInjection._simple_push_returns()
        )
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
        ))
        assert result["status"] == "pushed"

        # The credential rode the authorization grant to the proxy...
        mock_grant.assert_called_once_with("owner/repo", token="ghs_lazytoken")
        # ...and no exec in the container ever saw it.
        calls = container.exec_run.call_args_list
        assert calls  # sanity
        assert all(self._env_of(c) is None for c in calls)

    @patch("sunaba.tools.vcs.publishing.proxy_lifecycle.ensure_egress_proxy")
    @patch("sunaba.tools.vcs.publishing.authorized_push_grant")
    @patch("sunaba.tools.vcs.publishing.proxy_configured", return_value=True)
    @patch("sunaba.tools.vcs.publishing._resolve_vcs_token")
    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_pr_create_runs_host_side_no_exec_token(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
        mock_proxied: MagicMock,
        mock_grant: MagicMock,
        mock_ensure: MagicMock,
    ) -> None:
        """PR creation is host-side (#360): no exec ever carries a token."""
        mock_resolve.return_value = "ghs_lazytoken"

        returns = TestPublishLazyTokenInjection._simple_push_returns()
        container = _make_container_mock(returns)
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with patch(
            "sunaba.tools.vcs.publishing._create_pr_via_api",
            return_value="https://github.com/owner/repo/pull/9",
        ) as mock_create_pr:
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                create_pr=True,
                pr_title="Fix",
                pr_body="PR body",
            ))
        assert result["status"] == "pushed"
        assert result["pr_url"] == "https://github.com/owner/repo/pull/9"

        # The PR was created host-side with the host-resolved token...
        mock_create_pr.assert_called_once_with(
            "owner/repo", "fix/x", "Fix", "PR body", "", "ghs_lazytoken"
        )
        calls = container.exec_run.call_args_list
        # ...no gh exec ran in the container, and no exec carried a token —
        # the container stays credential-free end to end under the proxy.
        assert not [c for c in calls if "gh pr create" in c.args[0][2]]
        assert all(self._env_of(c) is None for c in calls)
        push_calls = [c for c in calls if "push origin" in c.args[0][2]]
        assert len(push_calls) == 1


class TestResolvePushToken:
    """Unit tests for the host-side token resolver (Issue #347)."""

    @patch("sunaba.tools.github_api.token_broker.mint_token")
    def test_prefers_minted_broker_token(self, mock_mint: MagicMock) -> None:
        from sunaba.tools.github_api import _resolve_vcs_token

        mock_mint.return_value = "ghs_minted"
        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghs_static"}):
            assert _resolve_vcs_token() == "ghs_minted"

    @patch("sunaba.tools.github_api.token_broker.mint_token")
    def test_falls_back_to_static_env(self, mock_mint: MagicMock) -> None:
        from sunaba.tools.github_api import _resolve_vcs_token

        mock_mint.return_value = None
        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghs_static"}, clear=True):
            assert _resolve_vcs_token() == "ghs_static"

    @patch("sunaba.tools.github_api.token_broker.mint_token")
    @patch("sunaba.tools.github_api.github_auth.get_global_provider")
    def test_prefers_global_provider_over_static_env(
        self, mock_get_provider: MagicMock, mock_mint: MagicMock
    ) -> None:
        from sunaba.tools.github_api import _resolve_vcs_token

        mock_mint.return_value = None
        mock_provider = MagicMock()
        mock_provider.get_token.return_value = "ghs_provider_tok"
        mock_get_provider.return_value = mock_provider
        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghs_static"}, clear=True):
            assert _resolve_vcs_token() == "ghs_provider_tok"

    @patch("sunaba.tools.github_api.token_broker.mint_token")
    def test_empty_when_no_token_available(self, mock_mint: MagicMock) -> None:
        from sunaba.tools.github_api import _resolve_vcs_token

        mock_mint.return_value = None
        with patch.dict("os.environ", {}, clear=True):
            assert _resolve_vcs_token() == ""


class TestApiPushBlobReading:
    """Tests for the blob-content reading logic in the API-push fallback.

    The embedded script _SANDBOX_CREATE_PR_SCRIPT reads each file's
    content via ``_read_blob(path)`` (binary-safe call to
    ``git cat-file blob HEAD:<path>``), so that symlinks (mode 120000)
    upload the link target path string — not the target file's content —
    matching what git committed.

    This test extracts the actual ``_read_blob`` function from the
    embedded script and executes it against a real (ephemeral) git
    repository via subprocess, so the production code path is validated
    rather than re-implemented.
    """

    def test_read_blob_respects_symlinks(self, tmp_path) -> None:
        """The _read_blob function from the API-push script must return
        the link target path string for symlinks, not the target file
        content."""
        import subprocess
        import sys
        from pathlib import Path

        from sunaba.tools.vcs.publishing import _SANDBOX_CREATE_PR_SCRIPT

        # --- Extract the real _read_blob function from the script ---
        fn_marker = "def _read_blob(path):"
        fn_start = _SANDBOX_CREATE_PR_SCRIPT.index(fn_marker)
        end_marker = "\n\nrepo, branch, working_dir"
        fn_end = _SANDBOX_CREATE_PR_SCRIPT.index(end_marker, fn_start)
        read_blob_code = _SANDBOX_CREATE_PR_SCRIPT[fn_start:fn_end]

        # --- Build a test harness that calls _read_blob ---
        harness = (
            "import subprocess\n"
            "import sys\n"
            + read_blob_code +
            "\n"
            "path = sys.argv[1]\n"
            "try:\n"
            "    result = _read_blob(path)\n"
            "except OSError as e:\n"
            "    sys.stderr.write(str(e))\n"
            "    sys.exit(1)\n"
            "sys.stdout.buffer.write(result)\n"
        )
        harness_path = tmp_path / "_test_read_blob.py"
        harness_path.write_text(harness)

        # --- Create a git repo with test files ---
        repo = tmp_path / "repo"
        repo.mkdir()

        def _git(*args):
            subprocess.run(
                ["git"] + list(args),
                capture_output=True,
                cwd=repo,
                check=True,
            )

        _git("init")
        _git("config", "user.email", "test@test")
        _git("config", "user.name", "Test")

        # Regular text file
        regular_file = Path(repo) / "hello.txt"
        regular_file.write_text("Hello, world!\n")

        # Binary file (contains NUL byte)
        binary_file = Path(repo) / "binary.bin"
        binary_file.write_bytes(
            b"\x00\x01\x02\xff\xfeHello\x00binary\x00content\n"
        )

        # Symlink pointing outside the repo
        outside_target = "/etc/os-release"  # guaranteed to exist on Linux
        symlink_path = Path(repo) / "link_to_etc"
        symlink_path.symlink_to(outside_target)

        _git("add", "-A")
        _git("commit", "-m", "test commit")

        # --- Helper: call the real _read_blob via subprocess ---
        def call_read_blob(filepath: str) -> bytes:
            r = subprocess.run(
                [sys.executable, str(harness_path), filepath],
                capture_output=True,
                cwd=repo,
            )
            assert r.returncode == 0, (
                f"_read_blob({filepath!r}) failed (exit {r.returncode}): "
                f"{r.stderr.decode(errors='replace')}"
            )
            return r.stdout

        # --- Assertions ---

        # Regular file: content matches
        regular_blob = call_read_blob("hello.txt")
        assert regular_blob == b"Hello, world!\n", (
            f"Regular file blob mismatch: {regular_blob!r}"
        )

        # Binary file: byte-identical (including NUL bytes)
        binary_blob = call_read_blob("binary.bin")
        expected_binary = (
            b"\x00\x01\x02\xff\xfeHello\x00binary\x00content\n"
        )
        assert binary_blob == expected_binary, (
            f"Binary file blob mismatch (length {len(binary_blob)}): "
            f"{binary_blob!r}"
        )

        # Symlink: blob content is the target path string, NOT target content
        symlink_blob = call_read_blob("link_to_etc")
        assert symlink_blob == outside_target.encode(), (
            f"Symlink blob should be {outside_target!r} but got "
            f"{symlink_blob!r}"
        )
        # Sanity: the symlink blob is short (path string), not file content
        assert len(symlink_blob) < 100, (
            "Symlink blob is unreasonably large -- it likely contains the "
            "target file content rather than the link target path"
        )

        # --- Verify git modes are preserved ---
        r = subprocess.run(
            ["git", "ls-tree", "HEAD"],
            capture_output=True, text=True, cwd=repo,
        )
        mode_map: dict[str, str] = {}
        for line in r.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                mode_map[parts[-1]] = parts[0]

        assert mode_map.get("hello.txt") == "100644", (
            f"Expected mode 100644 for hello.txt, got "
            f"{mode_map.get('hello.txt')}"
        )
        assert mode_map.get("binary.bin") == "100644", (
            f"Expected mode 100644 for binary.bin, got "
            f"{mode_map.get('binary.bin')}"
        )
        assert mode_map.get("link_to_etc") == "120000", (
            f"Expected mode 120000 for link_to_etc, got "
            f"{mode_map.get('link_to_etc')}"
        )
