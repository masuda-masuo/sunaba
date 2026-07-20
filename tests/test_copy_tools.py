"""Tests for copy_project and copy_file tools."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sunaba.tools.file import copy_file, copy_project


def _git_result(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    """Build one ``subprocess.run`` result for a mocked git call.

    Each git call needs its own result -- reusing a single ``return_value``
    across the ``--cached`` and ``--others`` calls makes the second one echo
    the first, which silently fabricates untracked entries.
    """
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


class TestCopyProject:
    """Tests for copy_project tool."""

    @patch("sunaba.tools.file._docker")
    def test_copy_project_with_dot(
        self,
        mock_docker: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """local_src_dir="." should resolve to the actual directory name as arcname."""
        src_dir = tmp_path / "myproject"
        src_dir.mkdir()
        (src_dir / "hello.txt").write_text("hello")
        (src_dir / "subdir").mkdir()
        (src_dir / "subdir" / "nested.txt").write_text("nested")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.side_effect = [
            (0, b"999\n"),  # id -u
            (0, b"999\n"),  # id -g
            (0, b""),       # chown
        ]
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        with monkeypatch.context() as m:
            m.chdir(str(src_dir))
            result = copy_project(
                container_id="abc123",
                local_src_dir=".",
                dest_dir="/root/shiori",
                include_untracked=True,
            )

        assert "Error" not in result
        assert "/root/shiori" in result
        assert "/root/shiori/." not in result

        mock_container.put_archive.assert_called_once()
        call_args = mock_container.put_archive.call_args
        assert call_args[0][0] == "/root/shiori"

        # exec_run called 3 times: id -u, id -g, chown as root
        assert mock_container.exec_run.call_count == 3
        chown_call = mock_container.exec_run.call_args_list[2]
        assert chown_call[0][0] == ["chown", "-R", "999:999", "/root/shiori"]
        assert chown_call[1] == {"user": "root"}

        tar_data = call_args[0][1]
        tar_data.seek(0)
        import tarfile
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            names = tar.getnames()
        assert all(
            name == "." or name.startswith("./")
            for name in names
        ), f"Entries should be rooted at the dest dir, got: {names}"
        assert "./hello.txt" in names
        assert "./subdir/nested.txt" in names

    @patch("sunaba.tools.file._docker")
    def test_copy_project_with_absolute_path(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The directory's contents land in dest_dir, not a subdir named after it."""
        src_dir = tmp_path / "myapp"
        src_dir.mkdir()
        (src_dir / "app.py").write_text("print('hello')")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.side_effect = [
            (0, b"999\n"),  # id -u
            (0, b"999\n"),  # id -g
            (0, b""),       # chown
        ]
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/opt",
            include_untracked=True,
        )

        assert "Error" not in result
        assert "to /opt " in result

        call_args = mock_container.put_archive.call_args
        assert call_args[0][0] == "/opt"

        tar_data = call_args[0][1]
        tar_data.seek(0)
        import tarfile
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            names = tar.getnames()
        assert "./app.py" in names

        assert mock_container.exec_run.call_count == 3
        chown_call = mock_container.exec_run.call_args_list[2]
        assert chown_call[0][0] == ["chown", "-R", "999:999", "/opt"]
        assert chown_call[1] == {"user": "root"}

    @patch("sunaba.tools.file._docker")
    def test_copy_project_container_not_found(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Should return error when container is not found."""
        from docker.errors import NotFound
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=".",
            dest_dir="/root",
        )
        assert "Error" in result
        assert "not found" in result

    @patch("sunaba.tools.file._docker")
    def test_copy_project_src_not_a_directory(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Should return error when local_src_dir is not a directory."""
        src_file = tmp_path / "file.txt"
        src_file.write_text("not a directory")
        mock_client = MagicMock()
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_file),
            dest_dir="/root",
        )
        assert "Error" in result
        assert "not a directory" in result

    @patch("sunaba.tools.file._docker")
    def test_copy_project_put_archive_fails(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Should return error when put_archive raises an APIError."""
        from unittest.mock import Mock

        from docker.errors import APIError
        src_dir = tmp_path / "testproj"
        src_dir.mkdir()
        (src_dir / "f.txt").write_text("data")

        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.reason = "Not Found"

        mock_container = MagicMock()
        mock_container.put_archive.side_effect = APIError(
            "404 Client Error: Not Found",
            mock_response,
            explanation="No such directory",
        )
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/nonexistent",
            include_untracked=True,
        )
        assert "Error" in result

    @patch("sunaba.tools.file._docker")
    def test_copy_project_exec_run_fails(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """exec_run failure during ownership normalisation must return an error."""
        src_dir = tmp_path / "chownfail"
        src_dir.mkdir()
        (src_dir / "f.txt").write_text("data")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.side_effect = RuntimeError("exec failed")
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/tmp",
            include_untracked=True,
        )

        assert "Error" in result
        assert "exec failed" in result

    @patch("sunaba.tools.file._docker")
    def test_copy_project_chown_raises_error(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Ownership normalisation failure must return an error, never log-and-swallow."""
        src_dir = tmp_path / "logtest"
        src_dir.mkdir()
        (src_dir / "f.txt").write_text("data")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.side_effect = PermissionError("permission denied")
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/tmp",
            include_untracked=True,
        )

        assert "Error" in result
        assert "Failed to determine container user id" in result

    @patch("sunaba.tools.file._docker")
    def test_copy_project_special_chars_in_path(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Paths with special characters should be properly shell-escaped."""
        src_dir = tmp_path / "my project (1)"
        src_dir.mkdir()
        (src_dir / "file.txt").write_text("data")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.side_effect = [
            (0, b"999\n"),  # id -u
            (0, b"999\n"),  # id -g
            (0, b""),       # chown
        ]
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/home/sandbox/my project (1)",
            include_untracked=True,
        )

        assert "Error" not in result
        assert mock_container.exec_run.call_count == 3
        chown_call = mock_container.exec_run.call_args_list[2]
        assert chown_call[0][0] == [
            "chown", "-R", "999:999", "/home/sandbox/my project (1)"
        ]
        assert chown_call[1] == {"user": "root"}

    @patch("sunaba.tools.file._docker")
    @patch("sunaba.tools.file.record_copy")
    def test_copy_file_default_dest_path(
        self,
        mock_record_copy: MagicMock,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Default dest_path is /home/sandbox."""
        src_file = tmp_path / "hello.txt"
        src_file.write_text("hello")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.side_effect = [
            (0, b"999\n"),  # id -u
            (0, b"999\n"),  # id -g
            (0, b""),       # chown
        ]
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_file(
            container_id="abc123",
            local_src_file=str(src_file),
        )

        assert "Error" not in result
        assert "/workspace" in result
        assert mock_container.exec_run.call_count == 3

    # ------------------------------------------------------------------
    # New tests for #678: tracked-only copy, ownership fix, submodules, etc.
    # ------------------------------------------------------------------

    @patch("sunaba.tools.file._docker")
    @patch("sunaba.tools.file.subprocess.run")
    def test_tracked_only_skips_untracked_and_gitignored(
        self,
        mock_run: MagicMock,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Only tracked files should appear in the tar; untracked/gitignored skipped."""
        src_dir = tmp_path / "repo"
        src_dir.mkdir()
        (src_dir / ".git").mkdir()
        (src_dir / "tracked.py").write_text("# tracked")
        (src_dir / "untracked.log").write_text("secret")
        (src_dir / ".gitignore").write_text(".gitignore\n")
        (src_dir / "node_modules").mkdir()
        (src_dir / "node_modules" / "lib.js").write_text("// lib")

        # The two git calls must return *different* things: sharing one
        # return_value made the --others call echo the --cached output, so the
        # untracked count was populated by a malformed entry and the
        # "untracked skipped" assertion passed for the wrong reason.
        mock_run.side_effect = [
            # git ls-files --cached --stage
            _git_result("100644 abc123 0\ttracked.py\n"),
            # git ls-files --others --exclude-standard
            _git_result("untracked.log\n"),
        ]

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.side_effect = [
            (0, b"999\n"),
            (0, b"999\n"),
            (0, b""),
        ]
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/workspace",
        )

        assert "Error" not in result
        assert "1 untracked skipped" in result

        call_args = mock_container.put_archive.call_args
        tar_data = call_args[0][1]
        tar_data.seek(0)
        import tarfile
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            names = tar.getnames()
        assert "./tracked.py" in names or ".tracked.py" in names
        assert "./untracked.log" not in names
        assert "./node_modules/lib.js" not in names

    @patch("sunaba.tools.file._docker")
    @patch("sunaba.tools.file.subprocess.run")
    def test_include_untracked_copies_all(
        self,
        mock_run: MagicMock,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """include_untracked=True copies tracked + untracked; result reports breakdown."""
        src_dir = tmp_path / "repo"
        src_dir.mkdir()
        (src_dir / ".git").mkdir()
        (src_dir / "tracked.py").write_text("# tracked")
        (src_dir / "untracked.log").write_text("log")

        # First call (ls-files --cached --stage): tracked.py
        # Second call (ls-files --others): untracked.log
        mock_run.side_effect = [
            MagicMock(stdout="100644 abc123 0\ttracked.py\n", stderr="", returncode=0),
            MagicMock(stdout="untracked.log\n", stderr="", returncode=0),
        ]

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.side_effect = [
            (0, b"999\n"),
            (0, b"999\n"),
            (0, b""),
        ]
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/workspace",
            include_untracked=True,
        )

        assert "Error" not in result
        assert "1 tracked files" in result
        assert "1 untracked files" in result

        call_args = mock_container.put_archive.call_args
        tar_data = call_args[0][1]
        tar_data.seek(0)
        import tarfile
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            names = tar.getnames()
        assert any("tracked.py" in n for n in names)
        assert any("untracked.log" in n for n in names)

    @patch("sunaba.tools.file._docker")
    @patch("sunaba.tools.file.subprocess.run")
    def test_submodule_entries_are_skipped(
        self,
        mock_run: MagicMock,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Submodule gitlinks (mode 160000) must not be recursed into."""
        src_dir = tmp_path / "repo"
        src_dir.mkdir()
        (src_dir / ".git").mkdir()
        (src_dir / "tracked.py").write_text("# tracked")
        (src_dir / "mysub").mkdir()
        (src_dir / "mysub" / "internal.txt").write_text("submodule file")

        mock_run.return_value.stdout = (
            "100644 abc123 0\ttracked.py\n"
            "160000 def456 0\tmysub\n"
        )
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.side_effect = [
            (0, b"999\n"),
            (0, b"999\n"),
            (0, b""),
        ]
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/workspace",
        )

        assert "Error" not in result

        call_args = mock_container.put_archive.call_args
        tar_data = call_args[0][1]
        tar_data.seek(0)
        import tarfile
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            names = tar.getnames()
        assert any("tracked.py" in n for n in names)
        assert not any("mysub/internal.txt" in n for n in names)
        assert not any("internal.txt" in n for n in names)

    @patch("sunaba.tools.file._docker")
    def test_file_based_git_is_refused(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A .git plain file (worktree pointer) must be refused."""
        src_dir = tmp_path / "worktree"
        src_dir.mkdir()
        (src_dir / "src.py").write_text("print('ok')")
        git_file = src_dir / ".git"
        git_file.write_text("gitdir: /some/real/path/.git\n")

        mock_client = MagicMock()
        mock_client.containers.get.return_value = MagicMock()
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/workspace",
        )

        assert "Error" in result
        assert "file-based .git" in result.lower()

    @patch("sunaba.tools.file._docker")
    @patch("sunaba.tools.file.subprocess.run")
    def test_chown_nonzero_exit_is_error(
        self,
        mock_run: MagicMock,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Non-zero exit from chown (as root) must be surfaced as error."""
        src_dir = tmp_path / "repo"
        src_dir.mkdir()
        (src_dir / ".git").mkdir()
        (src_dir / "f.txt").write_text("data")

        mock_run.side_effect = [
            _git_result("100644 abc123 0\tf.txt\n"),  # ls-files --cached --stage
            _git_result(""),  # ls-files --others --exclude-standard
        ]

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.side_effect = [
            (0, b"999\n"),    # id -u
            (0, b"999\n"),    # id -g
            (1, b"chown: Operation not permitted\n"),  # chown fails
        ]
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/workspace",
        )

        assert "Error" in result
        assert "Failed to set ownership" in result

    @patch("sunaba.tools.file._docker")
    def test_copy_file_ownership_failure_is_error(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """copy_file ownership failure must return an error."""
        src_file = tmp_path / "hello.txt"
        src_file.write_text("hello")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.side_effect = RuntimeError("exec failed")
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_file(
            container_id="abc123",
            local_src_file=str(src_file),
        )

        assert "Error" in result
        assert "Failed to determine container user id" in result

    @patch("sunaba.tools.file._docker")
    def test_non_git_directory_is_refused_by_default(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A plain directory has no 'tracked' notion, so refuse rather than copy all.

        Falling back to copying everything would reinstate exactly the leak
        #678 exists to prevent, just for a different class of source.
        """
        src_dir = tmp_path / "plain"
        src_dir.mkdir()
        (src_dir / "project.py").write_text("# code")
        (src_dir / "my_private_notes.txt").write_text("personal")

        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/workspace",
        )

        assert result.startswith("Error")
        assert "not a git repository" in result
        assert "include_untracked=True" in result
        # Nothing may reach the container.
        mock_container.put_archive.assert_not_called()

    @patch("sunaba.tools.file._docker")
    def test_non_git_directory_copies_all_when_opted_in(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """include_untracked=True is the explicit opt-in for a non-git source."""
        src_dir = tmp_path / "plain"
        src_dir.mkdir()
        (src_dir / "project.py").write_text("# code")
        (src_dir / "nested").mkdir()
        (src_dir / "nested" / "data.json").write_text("{}")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.side_effect = [
            (0, b"999\n"),
            (0, b"999\n"),
            (0, b""),
        ]
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/workspace",
            include_untracked=True,
        )

        assert "Error" not in result
        assert "not a git repository" in result

        tar_data = mock_container.put_archive.call_args[0][1]
        tar_data.seek(0)
        import tarfile

        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            names = tar.getnames()
        assert any("project.py" in n for n in names)
        assert any("data.json" in n for n in names)
