"""Tests for state-conditioned nudges (Issue #550) and verify gate (Issue #615).

Nudge fields (``warning`` / ``recommended_next_action`` / ``note``) are
advisory hints attached to tool results only when the recorded
server-session state contradicts the action.  The verify gate in
``publish`` (Issue #615) does block with ``status=error`` when no
verify_in_container is recorded and ``skip_verify_gate=False``.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import NotFound

from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.common import (
    CONTAINER_NOT_FOUND_NEXT_ACTION,
    container_not_found_error,
)
from sunaba.tools.vcs import publish
from sunaba.tools.verify import verify_in_container
from sunaba.verify_state import has_verify_success, record_verify_success
from tests.conftest import _decode, _make_client_mock, _make_container_mock


@pytest.fixture(autouse=True)
def _fresh_verify_state():
    """Isolate the module-level verify map between tests."""
    with patch("sunaba.verify_state._verify_map", {}):
        yield


@pytest.fixture(autouse=True)
def _disable_egress_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep publish off the egress-proxy path (matches test_publish.py)."""
    monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")


# Standard exec sequence for a successful one-shot push (see test_publish.py).
_PUSH_SEQUENCE = [
    (0, b"", b""),  # git checkout -b
    (0, b"", b""),  # git add
    (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
    (0, b"[fix/x abc1234] Fix\n1 file changed", b""),  # git commit
    (0, b"pushed", b""),  # git push
    (0, b"abc1234def5678", b""),  # git rev-parse HEAD
]


def _run_publish(**kwargs):
    """Call publish with the standard test arguments, merged with overrides."""
    defaults = dict(
        container_id="abc123def456",
        repo="owner/repo",
        branch="fix/x",
        message="Fix issue",
        working_dir="/root/repo",
    )
    defaults.update(kwargs)
    return _decode(publish(**defaults))


class TestVerifyStateMap:
    """Round-trip behaviour of the in-memory verify-success map."""

    def test_unknown_container_has_no_record(self) -> None:
        assert has_verify_success("abc123def456") is False

    def test_record_normalizes_to_12_char_prefix(self) -> None:
        record_verify_success("abc123def456" + "0" * 52)
        assert has_verify_success("abc123def456") is True
        assert has_verify_success("abc123def456" + "f" * 52) is True


class TestVerifyNudges:
    """verify_in_container nudges toward publish on gate success (Issue #619)."""

    def test_full_pass_nudges_publish(self) -> None:
        """When gate_passed=True, recommended_next_action must be "publish"."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        with patch("sunaba.tools.verify._docker", return_value=mock_client):
            import json as j
            gate_ok = {
                "gate_passed": True, "incomplete": False,
                "lint": [], "types": [], "gate_fail_reasons": [],
            }
            json_report = j.dumps({
                "summary": {"collected": 1, "total": 1, "passed": 1, "failed": 0},
                "duration": 0.1, "tests": [],
            })
            nl = "\n"
            mock_container.exec_run.side_effect = [
                (0, (b"", b"")),  # git diff HEAD --numstat
                (0, (b"", b"")),  # git diff --cached --numstat
                (0, (b"", b"")),  # src/tests dir probe
                (0, (f"{json_report}{nl}---PYTEST-RAW---{nl}".encode(), b"")),  # pytest
            ]
            from sunaba.edit_verify import DetectionResult
            det = DetectionResult(languages={"python"}, scope={"python": "."}, reason=None)
            with (
                patch("sunaba.edit_verify.detect_languages", return_value=det),
                patch("sunaba.edit_verify.run_lint_type_gate", return_value=gate_ok),
            ):
                from sunaba.tools.verify import verify_in_container
                raw = verify_in_container(
                    container_id="abc123def456", path="tests/",
                )
        parsed = j.loads(raw)
        assert parsed["gate_passed"] is True
        assert parsed.get("recommended_next_action") == "publish"


class TestPublishVerifyGate:
    """publish blocks with error without a recorded verify success, unless skip_verify_gate=True."""

    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_block_when_no_verify_recorded(
        self, mock_record: MagicMock, mock_docker: MagicMock
    ) -> None:
        container = _make_container_mock(list(_PUSH_SEQUENCE))
        mock_docker.return_value = _make_client_mock(container)

        result = _run_publish()

        assert result["status"] == "error"
        assert result["step"] == "verify_gate"
        assert "no successful verify_in_container" in result["error"]
        assert "skip_verify_gate=True" in result["error"]
        assert result["recommended_next_action"] == "verify_in_container"

    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_skip_verify_gate_bypasses_block(
        self, mock_record: MagicMock, mock_docker: MagicMock
    ) -> None:
        container = _make_container_mock(list(_PUSH_SEQUENCE))
        mock_docker.return_value = _make_client_mock(container)

        result = _run_publish(skip_verify_gate=True)

        assert result["status"] == "pushed"
        assert "warning" in result
        assert "no successful verify_in_container" in result["warning"]
        assert result["recommended_next_action"] == "verify_in_container"

    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_no_warning_after_verify_success(
        self, mock_record: MagicMock, mock_docker: MagicMock
    ) -> None:
        record_verify_success("abc123def456")
        container = _make_container_mock(list(_PUSH_SEQUENCE))
        mock_docker.return_value = _make_client_mock(container)

        result = _run_publish()

        assert result["status"] == "pushed"
        assert "warning" not in result
        assert "recommended_next_action" not in result

    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_skip_verify_gate_no_warning_after_verify(
        self, mock_record: MagicMock, mock_docker: MagicMock
    ) -> None:
        record_verify_success("abc123def456")
        container = _make_container_mock(list(_PUSH_SEQUENCE))
        mock_docker.return_value = _make_client_mock(container)

        result = _run_publish(skip_verify_gate=True)

        assert result["status"] == "pushed"
        assert "warning" not in result
        assert "recommended_next_action" not in result

    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_warning_attached_to_other_errors_too(
        self, mock_record: MagicMock, mock_docker: MagicMock
    ) -> None:
        record_verify_success("abc123def456")
        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (1, b"", b"add failed"),  # git add -> error return
        ])
        mock_docker.return_value = _make_client_mock(container)

        result = _run_publish()

        assert result["status"] == "error"
        assert result["step"] == "git_add"
        assert "recommended_next_action" not in result


class TestPublishPushOnlyNote:
    """create_pr=False success carries a push-only note."""

    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_note_when_create_pr_false(
        self, mock_record: MagicMock, mock_docker: MagicMock
    ) -> None:
        record_verify_success("abc123def456")
        container = _make_container_mock(list(_PUSH_SEQUENCE))
        mock_docker.return_value = _make_client_mock(container)

        result = _run_publish()

        assert result["status"] == "pushed"
        assert "no PR was created" in result["note"]
        assert "create_pr=True" in result["note"]

    @patch(
        "sunaba.tools.vcs._create_pr_via_api",
        return_value="https://github.com/owner/repo/pull/1",
    )
    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="tok")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_no_note_when_pr_created(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_token: MagicMock,
        mock_pr: MagicMock,
    ) -> None:
        record_verify_success("abc123def456")
        container = _make_container_mock(list(_PUSH_SEQUENCE))
        mock_docker.return_value = _make_client_mock(container)

        result = _run_publish(create_pr=True, pr_title="t", pr_body="PR body")

        assert result["status"] == "pushed"
        assert result["pr_url"] == "https://github.com/owner/repo/pull/1"
        assert "note" not in result


class TestContainerNotFoundNudge:
    """Container-not-found errors carry a recommended first move."""

    def test_helper_payload(self) -> None:
        payload = json.loads(container_not_found_error("abc123def456789"))
        assert payload == {
            "status": "error",
            "error": "Container abc123def456 not found",
            "recommended_next_action": CONTAINER_NOT_FOUND_NEXT_ACTION,
        }

    def test_helper_merges_extra_fields(self) -> None:
        payload = json.loads(container_not_found_error("abc123", gate_passed=False))
        assert payload["gate_passed"] is False
        assert "sandbox_list_containers" in payload["recommended_next_action"]

    @patch("sunaba.tools.vcs._docker")
    def test_publish_not_found_carries_nudge(self, mock_docker: MagicMock) -> None:
        client = MagicMock()
        client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = client

        result = _run_publish()

        assert result["status"] == "error"
        assert "sandbox_list_containers" in result["recommended_next_action"]

    @patch("sunaba.tools.verify._docker")
    def test_verify_not_found_carries_nudge_and_gate_flag(
        self, mock_docker: MagicMock
    ) -> None:
        client = MagicMock()
        client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = client

        result = json.loads(verify_in_container(container_id="abc123", path="tests/"))

        assert result["status"] == "error"
        assert result["gate_passed"] is False
        assert "sandbox_initialize" in result["recommended_next_action"]


class TestVerifyRecordsSuccess:
    """verify_in_container records full-gate success for later nudges."""

    _GATE_OK = {
        "gate_passed": True, "incomplete": False,
        "lint": [], "types": [], "gate_fail_reasons": [],
    }

    @staticmethod
    def _detection(langs: set[str]):
        from sunaba.edit_verify import DetectionResult

        return DetectionResult(
            languages=langs, scope={lang: "." for lang in langs}, reason=None
        )

    @patch("sunaba.tools.verify._docker")
    def test_full_pass_records_success(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        json_report = json.dumps({
            "summary": {"collected": 3, "total": 3, "passed": 3, "failed": 0, "errors": 0},
            "duration": 0.1,
            "tests": [],
        })
        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),  # git diff HEAD --numstat
            (0, (b"", b"")),  # git diff --cached --numstat
            (0, (b"", b"")),  # src/tests dir probe
            (0, (f"{json_report}\n---PYTEST-RAW---\n".encode(), b"")),  # pytest
        ]

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=self._detection({"python"}),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=dict(self._GATE_OK),
        ):
            result = json.loads(
                verify_in_container(container_id="abc123def456", path="tests/")
            )

        assert result["gate_passed"] is True
        assert has_verify_success("abc123def456") is True

    @patch("sunaba.tools.verify._docker")
    def test_gate_fail_records_nothing(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container.exec_run.return_value = (0, (b"", b""))

        gate_fail = {
            "gate_passed": False, "incomplete": False,
            "lint": [{"file": "f.py"}], "types": [],
            "gate_fail_reasons": ["lint failed"],
        }
        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=self._detection({"python"}),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_fail,
        ):
            result = json.loads(
                verify_in_container(container_id="abc123def456", path="tests/")
            )

        assert result["gate_passed"] is False
        assert has_verify_success("abc123def456") is False
