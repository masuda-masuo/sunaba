"""Tests for copy_project and copy_file tools."""

from __future__ import annotations

import io
import posixpath
import shlex
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import APIError

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
            (0, b"dir\ndir\n"),  # probe: dest and its parent
            (0, b"999\n"),      # id -u
            (0, b"999\n"),      # id -g
            (0, b""),           # chown
            (0, b"5\n"),        # read-back: the file is there, 5 bytes
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
        # probe + id -u + id -g + chown + read-back
        assert mock_container.exec_run.call_count == 5

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


# ======================================================================
# copy_file destination resolution
# ======================================================================


class _FakeCopyContainer:
    """A container fake with a real (in-memory) filesystem.

    ``copy_file`` asks the container what the destination is and reads
    the destination back afterwards, so a bare ``MagicMock`` cannot
    stand in: the fake has to know which directories exist and where
    ``put_archive`` actually wrote.  It also models docker's silent
    drop of a tar entry with an empty name -- the mechanism behind a
    "Copied ..." message for a file that was never written.
    """

    def __init__(self, dirs: set[str], files: dict[str, bytes] | None = None) -> None:
        self.dirs = set(dirs)
        self.files: dict[str, bytes] = dict(files or {})
        self.put_archive_calls: list[str] = []

    def _kind(self, path: str) -> str:
        if path in self.dirs:
            return "dir"
        if path in self.files:
            return "file"
        return "none"

    def exec_run(self, cmd, **kwargs):  # noqa: ANN001, ANN201
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "id":
            return (0, b"999\n")
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "chown":
            return (0, b"")
        script = cmd[-1] if isinstance(cmd, (list, tuple)) else cmd
        tokens = shlex.split(script)
        if "-d" in tokens:  # the dir/file/none probe
            verdicts = [
                self._kind(tokens[i + 1])
                for i, tok in enumerate(tokens)
                if tok == "-d"
            ]
            return (0, ("\n".join(verdicts) + "\n").encode())
        if "-f" in tokens:  # the post-copy read-back
            path = tokens[tokens.index("-f") + 1]
            # `[ -f dir ]` is false: a directory is not a regular file.
            if path in self.dirs or path not in self.files:
                return (1, b"")
            return (0, f"{len(self.files[path])}\n".encode())
        return (0, b"")

    def put_archive(self, path: str, data: bytes) -> bool:
        self.put_archive_calls.append(path)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for member in tar.getmembers():
                if not member.name:
                    # docker accepts the entry and writes nothing.
                    continue
                dest = posixpath.join(path, member.name)
                if dest in self.dirs:
                    # Untarring a regular file over an existing directory
                    # is refused by the daemon, not silently applied.
                    raise APIError(f"cannot overwrite directory {dest} with file")
                extracted = tar.extractfile(member)
                self.files[dest] = (
                    extracted.read() if extracted is not None else b""
                )
        return True


class _SilentlyLosingContainer(_FakeCopyContainer):
    """A container whose ``put_archive`` writes nothing but succeeds."""

    def put_archive(self, path: str, data: bytes) -> bool:
        self.put_archive_calls.append(path)
        return True


def _fake_client(container: _FakeCopyContainer) -> MagicMock:
    client = MagicMock()
    client.containers.get.return_value = container
    return client


class TestCopyFileDestination:
    """Where copy_file says it put the file is where the file is.

    Measured 2026-08-09: a dest ending in ``/`` reported success while
    no file existed anywhere in the container, and a dest whose
    basename differed from the source failed with a bare 404.
    """

    @staticmethod
    def _src(tmp_path: Path) -> Path:
        src = tmp_path / "brief-xxx.md"
        src.write_text("brief body")
        return src

    @patch("sunaba.tools.file.record_copy")
    @patch("sunaba.tools.file._docker")
    def test_trailing_slash_directory_takes_the_source_name(
        self, mock_docker: MagicMock, mock_record: MagicMock, tmp_path: Path,
    ) -> None:
        src = self._src(tmp_path)
        container = _FakeCopyContainer({"/workspace", "/workspace/chain-id"})
        mock_docker.return_value = _fake_client(container)

        result = copy_file("abc123", str(src), "/workspace/chain-id/")

        assert "Error" not in result, result
        assert "/workspace/chain-id/brief-xxx.md" in result
        assert container.files["/workspace/chain-id/brief-xxx.md"] == b"brief body"

    @patch("sunaba.tools.file.record_copy")
    @patch("sunaba.tools.file._docker")
    def test_existing_directory_without_slash_is_still_a_directory(
        self, mock_docker: MagicMock, mock_record: MagicMock, tmp_path: Path,
    ) -> None:
        src = self._src(tmp_path)
        container = _FakeCopyContainer({"/workspace", "/workspace/chain-id"})
        mock_docker.return_value = _fake_client(container)

        result = copy_file("abc123", str(src), "/workspace/chain-id")

        assert "Error" not in result, result
        assert container.files["/workspace/chain-id/brief-xxx.md"] == b"brief body"

    @patch("sunaba.tools.file.record_copy")
    @patch("sunaba.tools.file._docker")
    def test_file_path_with_a_different_basename_renames(
        self, mock_docker: MagicMock, mock_record: MagicMock, tmp_path: Path,
    ) -> None:
        src = self._src(tmp_path)
        container = _FakeCopyContainer({"/workspace", "/workspace/chain-id"})
        mock_docker.return_value = _fake_client(container)

        result = copy_file("abc123", str(src), "/workspace/chain-id/brief.md")

        assert "Error" not in result, result
        assert "/workspace/chain-id/brief.md" in result
        assert container.files["/workspace/chain-id/brief.md"] == b"brief body"
        assert "/workspace/chain-id/brief-xxx.md" not in container.files

    @patch("sunaba.tools.file.record_copy")
    @patch("sunaba.tools.file._docker")
    def test_missing_parent_directory_is_named_in_the_error(
        self, mock_docker: MagicMock, mock_record: MagicMock, tmp_path: Path,
    ) -> None:
        src = self._src(tmp_path)
        container = _FakeCopyContainer({"/workspace"})
        mock_docker.return_value = _fake_client(container)

        result = copy_file("abc123", str(src), "/workspace/absent/brief.md")

        assert "Error" in result, result
        assert "/workspace/absent" in result
        assert "Copied" not in result
        assert container.files == {}
        assert container.put_archive_calls == []
        mock_record.assert_not_called()

    @patch("sunaba.tools.file.record_copy")
    @patch("sunaba.tools.file._docker")
    def test_missing_directory_with_trailing_slash_is_named_in_the_error(
        self, mock_docker: MagicMock, mock_record: MagicMock, tmp_path: Path,
    ) -> None:
        src = self._src(tmp_path)
        container = _FakeCopyContainer({"/workspace"})
        mock_docker.return_value = _fake_client(container)

        result = copy_file("abc123", str(src), "/workspace/absent/")

        assert "Error" in result, result
        assert "/workspace/absent" in result
        assert "Copied" not in result
        assert container.files == {}

    @patch("sunaba.tools.file.record_copy")
    @patch("sunaba.tools.file._docker")
    def test_existing_file_destination_is_overwritten_in_place(
        self, mock_docker: MagicMock, mock_record: MagicMock, tmp_path: Path,
    ) -> None:
        src = self._src(tmp_path)
        container = _FakeCopyContainer(
            {"/workspace"}, {"/workspace/brief.md": b"stale"},
        )
        mock_docker.return_value = _fake_client(container)

        result = copy_file("abc123", str(src), "/workspace/brief.md")

        assert "Error" not in result, result
        assert container.files["/workspace/brief.md"] == b"brief body"

    @patch("sunaba.tools.file.record_copy")
    @patch("sunaba.tools.file._docker")
    def test_a_write_that_lands_nowhere_is_not_reported_as_success(
        self, mock_docker: MagicMock, mock_record: MagicMock, tmp_path: Path,
    ) -> None:
        """The read-back is what makes the success message trustworthy."""
        src = self._src(tmp_path)
        container = _SilentlyLosingContainer({"/workspace", "/workspace/chain-id"})
        mock_docker.return_value = _fake_client(container)

        result = copy_file("abc123", str(src), "/workspace/chain-id/")

        assert "Error" in result, result
        assert "nothing was written" in result
        assert "Copied" not in result
        mock_record.assert_not_called()


class _UnprobableContainer(_FakeCopyContainer):
    """A container that cannot answer the dir/file probe.

    ``_probe_paths`` swallows the failure and returns ``None``, which is
    the fallback path -- the one branch of ``copy_file`` no test used to
    reach.
    """

    def exec_run(self, cmd, **kwargs):  # noqa: ANN001, ANN201
        script = cmd[-1] if isinstance(cmd, (list, tuple)) else cmd
        if isinstance(script, str) and "-d" in shlex.split(script):
            raise RuntimeError("docker exec unavailable")
        return super().exec_run(cmd, **kwargs)


class _UnprobableLosingContainer(_UnprobableContainer):
    """Cannot be probed, and its put_archive writes nothing but succeeds."""

    def put_archive(self, path: str, data: bytes) -> bool:
        self.put_archive_calls.append(path)
        return True


class TestCopyFileProbeFallback:
    """What copy_file does when the container cannot be asked.

    The fallback used to re-derive the very defect the probe fixes: a
    destination with a differing basename and no trailing slash was
    classified as a directory, so a rename landed under the wrong path.
    """

    @staticmethod
    def _src(tmp_path: Path) -> Path:
        src = tmp_path / "brief-xxx.md"
        src.write_text("brief body")
        return src

    @patch("sunaba.tools.file.record_copy")
    @patch("sunaba.tools.file._docker")
    def test_fallback_treats_a_differing_basename_as_a_rename(
        self, mock_docker: MagicMock, mock_record: MagicMock, tmp_path: Path,
    ) -> None:
        src = self._src(tmp_path)
        container = _UnprobableContainer({"/workspace", "/workspace/chain-id"})
        mock_docker.return_value = _fake_client(container)

        result = copy_file("abc123", str(src), "/workspace/chain-id/brief.md")

        assert "Error" not in result, result
        assert "/workspace/chain-id/brief.md" in result
        assert container.files["/workspace/chain-id/brief.md"] == b"brief body"
        assert "/workspace/chain-id/brief.md/brief-xxx.md" not in container.files

    @patch("sunaba.tools.file.record_copy")
    @patch("sunaba.tools.file._docker")
    def test_fallback_trailing_slash_still_takes_the_source_name(
        self, mock_docker: MagicMock, mock_record: MagicMock, tmp_path: Path,
    ) -> None:
        """A trailing slash is the one thing the string does say."""
        src = self._src(tmp_path)
        container = _UnprobableContainer({"/workspace", "/workspace/chain-id"})
        mock_docker.return_value = _fake_client(container)

        result = copy_file("abc123", str(src), "/workspace/chain-id/")

        assert "Error" not in result, result
        assert container.files["/workspace/chain-id/brief-xxx.md"] == b"brief body"

    @patch("sunaba.tools.file.record_copy")
    @patch("sunaba.tools.file._docker")
    def test_fallback_guessing_wrong_is_reported_not_claimed(
        self, mock_docker: MagicMock, mock_record: MagicMock, tmp_path: Path,
    ) -> None:
        """A bare name that is really a directory: the guess fails loudly."""
        src = self._src(tmp_path)
        container = _UnprobableContainer({"/workspace", "/workspace/chain-id"})
        mock_docker.return_value = _fake_client(container)

        result = copy_file("abc123", str(src), "/workspace/chain-id")

        assert "Error" in result, result
        assert "Copied" not in result
        assert "/workspace/chain-id" in result
        # The message has to say what the caller can do about it.
        assert "trailing slash" in result
        assert container.files == {}
        mock_record.assert_not_called()

    @patch("sunaba.tools.file.record_copy")
    @patch("sunaba.tools.file._docker")
    def test_fallback_still_reaches_the_read_back(
        self, mock_docker: MagicMock, mock_record: MagicMock, tmp_path: Path,
    ) -> None:
        """No probe, no evidence -- so the read-back has to run anyway."""
        src = self._src(tmp_path)
        container = _UnprobableLosingContainer({"/workspace"})
        mock_docker.return_value = _fake_client(container)

        result = copy_file("abc123", str(src), "/workspace/brief.md")

        assert "Error" in result, result
        assert "nothing was written" in result
        assert "Copied" not in result
        mock_record.assert_not_called()

    @patch("sunaba.tools.file.record_copy")
    @patch("sunaba.tools.file._docker")
    def test_probe_exit_code_failure_also_falls_back(
        self, mock_docker: MagicMock, mock_record: MagicMock, tmp_path: Path,
    ) -> None:
        """_probe_paths returns None on a non-zero exit, not just on raise."""
        src = self._src(tmp_path)

        class _ProbeExitsNonZero(_FakeCopyContainer):
            def exec_run(self, cmd, **kwargs):  # noqa: ANN001, ANN202
                script = cmd[-1] if isinstance(cmd, (list, tuple)) else cmd
                if isinstance(script, str) and "-d" in shlex.split(script):
                    return (127, b"")
                return super().exec_run(cmd, **kwargs)

        container = _ProbeExitsNonZero({"/workspace", "/workspace/chain-id"})
        mock_docker.return_value = _fake_client(container)

        result = copy_file("abc123", str(src), "/workspace/chain-id/brief.md")

        assert "Error" not in result, result
        assert container.files["/workspace/chain-id/brief.md"] == b"brief body"
