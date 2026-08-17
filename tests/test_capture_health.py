"""Tests for the capture-health guard (issue #852).

The guard converts a silent fail-open -- every output-bearing tool returning
``status: ok`` with empty output while the command still executed -- into a
loud, actionable error.  Guard state is **per container** (issue #852
review): one container's empty decodes accumulate on its own counter, the
canary always probes the container whose counter tripped, and other
containers' non-empty traffic can neither mask nor clear that state.

Unit tests drive :func:`sunaba.capture_health.check_capture` directly; wire
tests drive ``sandbox_exec`` / ``read_file_range`` with a mocked docker
client that returns empty output for everything (the incident shape).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

from sunaba import capture_health
from sunaba.tools.diff import diff_in_container
from sunaba.tools.exec import sandbox_exec, sandbox_exec_check
from sunaba.tools.file import read_file_range
from sunaba.tools.verify import search_in_container, verify_in_container

_CID = "abc123def456"

#: A well-formed job id for ``sandbox_exec_check`` (``<cid>-<epoch>-<hex>``).
_JOB_ID = f"{_CID}-1700000000-a1b2c3"


class _FakeContainer:
    """A container whose ``exec_run`` answers the canary (and only the canary).

    Real commands return empty output -- the incident shape.  The canary
    (``[\"echo\", nonce]``) echoes the nonce back through the demuxed capture
    path, or returns empty when ``canary_empty`` is set.
    """

    def __init__(self, canary_empty: bool = False) -> None:
        self.canary_empty = canary_empty
        self.exec_calls: list[tuple[list[str], dict[str, Any]]] = []

    def exec_run(self, argv: list[str], **kwargs: Any) -> tuple[int, object]:
        self.exec_calls.append((argv, kwargs))
        if argv and argv[0] == "echo":
            nonce = argv[1]
            if self.canary_empty:
                return (0, (b"", b""))
            return (0, (nonce.encode("utf-8") + b"\n", b""))
        return (0, (b"", b""))


class _IdContainer(_FakeContainer):
    """A canary-answering container that carries a docker id (issue #869)."""

    def __init__(self, container_id: str, canary_empty: bool = False) -> None:
        super().__init__(canary_empty=canary_empty)
        self.id = container_id


class _RaisingContainer:
    """A container whose ``exec_run`` raises (container/channel-level failure)."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.exec_calls: list[tuple[list[str], dict[str, Any]]] = []

    def exec_run(self, argv: list[str], **kwargs: Any) -> tuple[int, object]:
        self.exec_calls.append((argv, kwargs))
        raise self.exc


class TestCaptureHealthUnit:
    """Direct unit tests of the guard's decision logic."""

    def test_n_minus_1_empties_no_canary_no_error(self) -> None:
        container = _FakeContainer()
        for _ in range(capture_health.EMPTY_TRIGGER - 1):
            err = capture_health.check_capture(
                container, decoded_empty=True, container_id=_CID
            )
            assert err is None
        assert container.exec_calls == []  # no canary before the threshold
        assert not capture_health.is_capture_broken(_CID)
        assert (
            capture_health.consecutive_empty(_CID)
            == capture_health.EMPTY_TRIGGER - 1
        )

    def test_n_empties_canary_succeeds_resets_counter(self) -> None:
        container = _FakeContainer()
        err = None
        for _ in range(capture_health.EMPTY_TRIGGER):
            err = capture_health.check_capture(
                container, decoded_empty=True, container_id=_CID
            )
        assert err is None  # legitimate empties: served normally
        assert not capture_health.is_capture_broken(_CID)
        assert capture_health.consecutive_empty(_CID) == 0
        # exactly one canary ran, through the same demuxed exec_run path
        assert len(container.exec_calls) == 1
        argv, kwargs = container.exec_calls[0]
        assert argv[0] == "echo"
        assert kwargs.get("stdout") is True
        assert kwargs.get("stderr") is True
        assert kwargs.get("demux") is True

    def test_n_empties_canary_empty_loud_error(self) -> None:
        container = _FakeContainer(canary_empty=True)
        result = None
        for _ in range(capture_health.EMPTY_TRIGGER):
            result = capture_health.check_capture(
                container, decoded_empty=True, container_id=_CID
            )
        assert result is not None
        payload = json.loads(result)
        assert payload["status"] == "error"
        assert capture_health.RESTART_REMEDY in payload["error"]
        assert capture_health.is_capture_broken(_CID)

    def test_subsequent_call_errors_without_waiting_for_n(self) -> None:
        container = _FakeContainer(canary_empty=True)
        for _ in range(capture_health.EMPTY_TRIGGER):
            capture_health.check_capture(
                container, decoded_empty=True, container_id=_CID
            )
        assert capture_health.is_capture_broken(_CID)
        container.exec_calls.clear()
        result = capture_health.check_capture(
            container, decoded_empty=True, container_id=_CID
        )
        assert result is not None
        assert capture_health.is_capture_broken(_CID)
        # one immediate re-probe, not N more empties
        assert len(container.exec_calls) == 1

    def test_recovery_clears_and_serves_normally(self) -> None:
        container = _FakeContainer(canary_empty=True)
        for _ in range(capture_health.EMPTY_TRIGGER):
            capture_health.check_capture(
                container, decoded_empty=True, container_id=_CID
            )
        assert capture_health.is_capture_broken(_CID)
        # capture heals: the canary now returns its nonce
        container.canary_empty = False
        result = capture_health.check_capture(
            container, decoded_empty=True, container_id=_CID
        )
        assert result is None
        assert not capture_health.is_capture_broken(_CID)
        assert capture_health.consecutive_empty(_CID) == 0

    def test_legit_empty_workflow_never_errors_while_healthy(self) -> None:
        container = _FakeContainer()  # canary always healthy
        for _ in range(10 * capture_health.EMPTY_TRIGGER):
            err = capture_health.check_capture(
                container, decoded_empty=True, container_id=_CID
            )
            assert err is None
        assert not capture_health.is_capture_broken(_CID)

    def test_nonempty_output_resets_counter(self) -> None:
        container = _FakeContainer()
        for _ in range(capture_health.EMPTY_TRIGGER - 1):
            capture_health.check_capture(
                container, decoded_empty=True, container_id=_CID
            )
        assert capture_health.consecutive_empty(_CID) == capture_health.EMPTY_TRIGGER - 1
        capture_health.check_capture(
            container, decoded_empty=False, container_id=_CID
        )
        assert capture_health.consecutive_empty(_CID) == 0
        assert container.exec_calls == []  # non-empty never probes

    def test_trip_and_recovery_are_journaled(self) -> None:
        container = _FakeContainer(canary_empty=True)
        with patch("sunaba.capture_health.record_capture_health") as rec:
            for _ in range(capture_health.EMPTY_TRIGGER):
                capture_health.check_capture(
                    container, decoded_empty=True, container_id=_CID
                )
            broken_call = rec.call_args_list[-1]
            assert broken_call.args[0] == _CID
            assert broken_call.args[1] == "broken"
            assert broken_call.kwargs["consecutive_empty"] == capture_health.EMPTY_TRIGGER
            assert broken_call.kwargs["canary_nonce_found"] is False

            container.canary_empty = False
            capture_health.check_capture(
                container, decoded_empty=True, container_id=_CID
            )
            recovered_call = rec.call_args_list[-1]
            assert recovered_call.args[1] == "recovered"
            assert recovered_call.kwargs["canary_nonce_found"] is True

    def test_failed_reprobes_do_not_rejournal_broken(self) -> None:
        """'broken' is journaled on the False->True trip only, not per re-probe.

        While already broken, every empty decode re-probes; a failing canary
        must keep returning the loud error but must not write another
        'broken' entry with a meaningless ``consecutive_empty=0`` -- the
        trip entry carries the real counter value, and re-probe noise would
        bury it (issue #852 review).
        """
        container = _FakeContainer(canary_empty=True)
        with patch("sunaba.capture_health.record_capture_health") as rec:
            for _ in range(capture_health.EMPTY_TRIGGER):
                capture_health.check_capture(
                    container, decoded_empty=True, container_id=_CID
                )
            # exactly one trip entry, carrying the real counter value
            assert rec.call_count == 1
            trip_call = rec.call_args_list[0]
            assert trip_call.args[1] == "broken"
            assert trip_call.kwargs["consecutive_empty"] == capture_health.EMPTY_TRIGGER
            assert trip_call.kwargs["canary_nonce_found"] is False

            # failed re-probes while broken keep erroring but add no journal noise
            for _ in range(5):
                err = capture_health.check_capture(
                    container, decoded_empty=True, container_id=_CID
                )
                assert err is not None
                assert capture_health.is_capture_broken(_CID)
            assert rec.call_count == 1


class TestCaptureHealthPerContainer:
    """Per-container state: the round's structural fix (issue #852 review).

    A global counter+flag masked single-container breakage: any non-empty
    decode from a healthy container reset the counter, and the canary could
    land on a healthy container, so the guard never tripped for the broken
    one.  These tests pin the per-container behaviour that dissolves the
    finding: empties accumulate per container, the canary probes the counter
    owner, and one container's traffic cannot clear another's flag.
    """

    def test_single_container_breakage_not_masked_by_other_traffic(self) -> None:
        broken = _FakeContainer(canary_empty=True)
        healthy = _FakeContainer()
        cid_a, cid_b = "aaaa11111111", "bbbb22222222"
        for _ in range(capture_health.EMPTY_TRIGGER - 1):
            assert (
                capture_health.check_capture(
                    broken, decoded_empty=True, container_id=cid_a
                )
                is None
            )
            # interleaved non-empty traffic from a healthy container
            assert (
                capture_health.check_capture(
                    healthy, decoded_empty=False, container_id=cid_b
                )
                is None
            )
        # A's counter climbed despite B's non-empty decodes
        assert capture_health.consecutive_empty(cid_a) == capture_health.EMPTY_TRIGGER - 1
        assert capture_health.consecutive_empty(cid_b) == 0
        # the Nth empty on A trips the canary against A itself
        result = capture_health.check_capture(
            broken, decoded_empty=True, container_id=cid_a
        )
        assert result is not None
        assert json.loads(result)["status"] == "error"
        assert capture_health.is_capture_broken(cid_a)
        assert not capture_health.is_capture_broken(cid_b)
        assert len(broken.exec_calls) == 1  # canary ran against the broken container
        assert healthy.exec_calls == []

    def test_other_container_nonempty_does_not_clear_broken_flag(self) -> None:
        broken = _FakeContainer(canary_empty=True)
        healthy = _FakeContainer()
        cid_a, cid_b = "aaaa11111111", "bbbb22222222"
        for _ in range(capture_health.EMPTY_TRIGGER):
            capture_health.check_capture(
                broken, decoded_empty=True, container_id=cid_a
            )
        assert capture_health.is_capture_broken(cid_a)
        # B's non-empty decode is direct evidence only for B
        assert (
            capture_health.check_capture(
                healthy, decoded_empty=False, container_id=cid_b
            )
            is None
        )
        assert capture_health.is_capture_broken(cid_a)  # no cross-container flap
        assert not capture_health.is_capture_broken(cid_b)
        # A still errors on its next empty decode
        result = capture_health.check_capture(
            broken, decoded_empty=True, container_id=cid_a
        )
        assert result is not None
        assert json.loads(result)["status"] == "error"

    def test_server_wide_breakage_trips_each_container(self) -> None:
        a = _FakeContainer(canary_empty=True)
        b = _FakeContainer(canary_empty=True)
        cid_a, cid_b = "aaaa11111111", "bbbb22222222"
        for _ in range(capture_health.EMPTY_TRIGGER):
            capture_health.check_capture(
                a, decoded_empty=True, container_id=cid_a
            )
            capture_health.check_capture(
                b, decoded_empty=True, container_id=cid_b
            )
        assert capture_health.is_capture_broken(cid_a)
        assert capture_health.is_capture_broken(cid_b)
        assert capture_health.consecutive_empty(cid_a) == 0
        assert capture_health.consecutive_empty(cid_b) == 0

    def test_prune_forgets_container_state(self) -> None:
        container = _FakeContainer(canary_empty=True)
        for _ in range(capture_health.EMPTY_TRIGGER):
            capture_health.check_capture(
                container, decoded_empty=True, container_id=_CID
            )
        assert capture_health.is_capture_broken(_CID)
        capture_health.prune(_CID)  # container stopped/removed
        assert not capture_health.is_capture_broken(_CID)
        assert capture_health.consecutive_empty(_CID) == 0
        # a reused id starts fresh: N-1 empties do not trip
        err = None
        for _ in range(capture_health.EMPTY_TRIGGER - 1):
            err = capture_health.check_capture(
                container, decoded_empty=True, container_id=_CID
            )
        assert err is None


class TestCaptureHealthResolvedId:
    """Issue #869: state is keyed by the container object's docker id.

    ``sandbox_stop`` and the reaper prune by the resolved id prefix
    (``container.id[:12]``), so guard state keyed by the caller-supplied
    string would linger forever when the client passed a container NAME
    (prune never matches), and distinct names can collide where ids
    cannot.  These tests pin keying by the object's id prefix -- and the
    journal carrying the same resolved id.  The caller string remains
    only as a fallback for stubs without ``.id``; the other classes in
    this file still exercise that path.
    """

    def test_state_stored_and_pruned_under_resolved_id(self) -> None:
        container = _IdContainer(
            container_id="abcdef1234567890", canary_empty=True
        )
        for _ in range(capture_health.EMPTY_TRIGGER):
            capture_health.check_capture(
                container, decoded_empty=True, container_id="my-container-name"
            )
        # keyed by the resolved id prefix, not the caller-supplied name
        assert capture_health.is_capture_broken("abcdef123456")
        assert not capture_health.is_capture_broken("my-container-name")
        assert capture_health.consecutive_empty("my-container-name") == 0
        # prune by the resolved prefix (sandbox_stop / reaper shape) removes it
        capture_health.prune("abcdef123456")
        assert not capture_health.is_capture_broken("abcdef123456")
        assert capture_health.consecutive_empty("abcdef123456") == 0

    def test_names_sharing_a_prefix_keep_state_independent(self) -> None:
        broken = _IdContainer(container_id="aaaa11111111aaaa", canary_empty=True)
        healthy = _IdContainer(container_id="bbbb22222222bbbb")
        name_a = "aaaa11111111-worker-a"
        name_b = "aaaa11111111-worker-b"  # shares name_a's 12-char prefix
        for _ in range(capture_health.EMPTY_TRIGGER):
            capture_health.check_capture(
                broken, decoded_empty=True, container_id=name_a
            )
        assert capture_health.is_capture_broken("aaaa11111111")
        # the healthy container's non-empty decode is direct evidence only
        # for its own id; it must not clear the broken flag
        assert (
            capture_health.check_capture(
                healthy, decoded_empty=False, container_id=name_b
            )
            is None
        )
        assert capture_health.is_capture_broken("aaaa11111111")
        assert not capture_health.is_capture_broken("bbbb22222222")
        assert capture_health.consecutive_empty("bbbb22222222") == 0
        # and the broken container still errors on its next empty decode
        result = capture_health.check_capture(
            broken, decoded_empty=True, container_id=name_a
        )
        assert result is not None
        assert json.loads(result)["status"] == "error"

    def test_journal_carries_resolved_id(self) -> None:
        container = _IdContainer(
            container_id="abcdef1234567890", canary_empty=True
        )
        with patch("sunaba.capture_health.record_capture_health") as rec:
            for _ in range(capture_health.EMPTY_TRIGGER):
                capture_health.check_capture(
                    container, decoded_empty=True, container_id="my-container-name"
                )
        assert rec.call_count == 1
        trip_call = rec.call_args_list[0]
        assert trip_call.args[1] == "broken"
        assert trip_call.args[0] == "abcdef123456"  # the resolved id, not the name

    def test_stub_without_id_falls_back_to_caller_string(self) -> None:
        # _FakeContainer has no .id: the caller string must keep keying
        # state, so stubs (and defensive paths) stay meaningful.
        container = _FakeContainer(canary_empty=True)
        for _ in range(capture_health.EMPTY_TRIGGER):
            capture_health.check_capture(
                container, decoded_empty=True, container_id="stub-key"
            )
        assert capture_health.is_capture_broken("stub-key")
        assert not capture_health.is_capture_broken("stub-ke")
        capture_health.prune("stub-key")
        assert not capture_health.is_capture_broken("stub-key")


class TestCaptureHealthCanaryError:
    """Canary-probe-exception class (issue #852 review, finding 2 home).

    When the probe itself raises -- container stopped/removed, docker
    channel failure -- the loud error must name the container and the
    exception and offer a container-level remedy, NOT the server-restart
    command: a dead container cannot be fixed by restarting the server, and
    the journal must be able to tell this class from the #852 silent-empty
    break (via ``canary_error``).
    """

    def test_exception_yields_container_level_message(self) -> None:
        container = _RaisingContainer(RuntimeError("exec socket gone"))
        result = None
        with patch("sunaba.capture_health.record_capture_health") as rec:
            for _ in range(capture_health.EMPTY_TRIGGER):
                result = capture_health.check_capture(
                    container, decoded_empty=True, container_id=_CID
                )
            # the trip is journaled with the exception repr for forensics
            assert rec.call_count == 1
            trip_call = rec.call_args_list[0]
            assert trip_call.args[1] == "broken"
            assert trip_call.kwargs["canary_nonce_found"] is False
            assert trip_call.kwargs["canary_error"] is not None
            assert "RuntimeError" in trip_call.kwargs["canary_error"]
        assert result is not None
        payload = json.loads(result)
        assert payload["status"] == "error"
        assert _CID in payload["error"]
        assert "RuntimeError" in payload["error"]
        assert capture_health.RESTART_REMEDY not in payload["error"]

    def test_exception_class_reprobes_while_broken(self) -> None:
        container = _RaisingContainer(RuntimeError("exec socket gone"))
        for _ in range(capture_health.EMPTY_TRIGGER):
            capture_health.check_capture(
                container, decoded_empty=True, container_id=_CID
            )
        assert capture_health.is_capture_broken(_CID)
        result = capture_health.check_capture(
            container, decoded_empty=True, container_id=_CID
        )
        assert result is not None
        payload = json.loads(result)
        assert capture_health.RESTART_REMEDY not in payload["error"]
        assert "RuntimeError" in payload["error"]

    def test_exception_class_recovers_when_probe_heals(self) -> None:
        container = _RaisingContainer(RuntimeError("exec socket gone"))
        for _ in range(capture_health.EMPTY_TRIGGER):
            capture_health.check_capture(
                container, decoded_empty=True, container_id=_CID
            )
        assert capture_health.is_capture_broken(_CID)
        # the container comes back (e.g. a new sandbox): the probe answers again
        healed = _FakeContainer()
        result = capture_health.check_capture(
            healed, decoded_empty=True, container_id=_CID
        )
        assert result is None
        assert not capture_health.is_capture_broken(_CID)


class TestSandboxExecCaptureGuard:
    """Wire tests: sandbox_exec with a mock docker client in the incident shape."""

    def _wire(self, canary_empty: bool) -> tuple[MagicMock, dict[str, bool]]:
        state = {"canary_empty": canary_empty}
        container = MagicMock()

        def exec_run(argv: list[str], **kwargs: Any) -> tuple[int, object]:
            if argv and argv[0] == "echo":
                nonce = argv[1]
                if state["canary_empty"]:
                    return (0, (b"", b""))
                return (0, (nonce.encode("utf-8") + b"\n", b""))
            return (0, (b"", b""))  # incident shape: real output empty

        container.exec_run.side_effect = exec_run
        client = MagicMock()
        client.containers.get.return_value = container
        return container, state

    @patch("sunaba.tools.exec._docker")
    def test_n_minus_1_empties_still_ok(self, mock_docker: MagicMock) -> None:
        container, _state = self._wire(canary_empty=False)
        mock_docker.return_value.containers.get.return_value = container
        for _ in range(capture_health.EMPTY_TRIGGER - 1):
            result = json.loads(sandbox_exec(container_id=_CID, commands=["true"]))
            assert result["status"] == "ok"

    @patch("sunaba.tools.exec._docker")
    def test_incident_shape_trips_loud_error(self, mock_docker: MagicMock) -> None:
        container, _state = self._wire(canary_empty=True)
        mock_docker.return_value.containers.get.return_value = container
        for _ in range(capture_health.EMPTY_TRIGGER - 1):
            result = json.loads(sandbox_exec(container_id=_CID, commands=["true"]))
            assert result["status"] == "ok"
        result = json.loads(sandbox_exec(container_id=_CID, commands=["true"]))
        assert result["status"] == "error"
        assert capture_health.RESTART_REMEDY in result["error"]

    @patch("sunaba.tools.exec._docker")
    def test_subsequent_call_errors_immediately(self, mock_docker: MagicMock) -> None:
        container, _state = self._wire(canary_empty=True)
        mock_docker.return_value.containers.get.return_value = container
        for _ in range(capture_health.EMPTY_TRIGGER):
            sandbox_exec(container_id=_CID, commands=["true"])
        assert capture_health.is_capture_broken(_CID)
        result = json.loads(sandbox_exec(container_id=_CID, commands=["true"]))
        assert result["status"] == "error"
        assert capture_health.RESTART_REMEDY in result["error"]

    @patch("sunaba.tools.exec._docker")
    def test_recovery_serves_ok(self, mock_docker: MagicMock) -> None:
        container, state = self._wire(canary_empty=True)
        mock_docker.return_value.containers.get.return_value = container
        for _ in range(capture_health.EMPTY_TRIGGER):
            sandbox_exec(container_id=_CID, commands=["true"])
        assert capture_health.is_capture_broken(_CID)
        state["canary_empty"] = False  # capture heals
        result = json.loads(sandbox_exec(container_id=_CID, commands=["true"]))
        assert result["status"] == "ok"
        assert not capture_health.is_capture_broken(_CID)

    @patch("sunaba.tools.exec._docker")
    def test_healthy_canary_never_errors(self, mock_docker: MagicMock) -> None:
        container, _state = self._wire(canary_empty=False)
        mock_docker.return_value.containers.get.return_value = container
        for _ in range(10 * capture_health.EMPTY_TRIGGER):
            result = json.loads(sandbox_exec(container_id=_CID, commands=["true"]))
            assert result["status"] == "ok"
        assert not capture_health.is_capture_broken(_CID)

    @patch("sunaba.tools.exec._docker")
    def test_canary_uses_exec_run_demux_path(self, mock_docker: MagicMock) -> None:
        container, _state = self._wire(canary_empty=False)
        mock_docker.return_value.containers.get.return_value = container
        for _ in range(capture_health.EMPTY_TRIGGER):
            sandbox_exec(container_id=_CID, commands=["true"])
        canary_calls = [
            c for c in container.exec_run.call_args_list
            if c.args and c.args[0] and c.args[0][0] == "echo"
        ]
        assert canary_calls, "the canary must run through container.exec_run"
        assert canary_calls[-1].kwargs.get("demux") is True

    @patch("sunaba.tools.exec._docker")
    def test_canary_exception_yields_container_level_error(
        self, mock_docker: MagicMock
    ) -> None:
        container = MagicMock()

        def exec_run(argv: list[str], **kwargs: Any) -> tuple[int, object]:
            if argv and argv[0] == "echo":
                raise RuntimeError("exec socket gone")
            return (0, (b"", b""))  # incident shape: real output empty

        container.exec_run.side_effect = exec_run
        mock_docker.return_value.containers.get.return_value = container
        for _ in range(capture_health.EMPTY_TRIGGER - 1):
            result = json.loads(sandbox_exec(container_id=_CID, commands=["true"]))
            assert result["status"] == "ok"
        result = json.loads(sandbox_exec(container_id=_CID, commands=["true"]))
        assert result["status"] == "error"
        assert _CID in result["error"]
        assert "RuntimeError" in result["error"]
        assert capture_health.RESTART_REMEDY not in result["error"]


class TestReadFileRangeCaptureGuard:
    """Wire test: read_file_range with a mock docker client in the incident shape."""

    @patch("sunaba.tools.file._docker")
    def test_incident_shape_trips_loud_error(self, mock_docker: MagicMock) -> None:
        state = {"canary_empty": True}
        container = MagicMock()

        def exec_run(argv: list[str], **kwargs: Any) -> tuple[int, object]:
            if argv and argv[0] == "echo":
                nonce = argv[1]
                if state["canary_empty"]:
                    return (0, (b"", b""))
                return (0, (nonce.encode("utf-8") + b"\n", b""))
            return (0, (b"", b""))  # cat returns empty despite bytes on disk

        container.exec_run.side_effect = exec_run
        mock_docker.return_value.containers.get.return_value = container

        for _ in range(capture_health.EMPTY_TRIGGER - 1):
            result = json.loads(read_file_range(_CID, "/f.txt"))
            assert "status" not in result  # normal read envelope
        result = json.loads(read_file_range(_CID, "/f.txt"))
        assert result["status"] == "error"
        assert capture_health.RESTART_REMEDY in result["error"]

    @patch("sunaba.tools.file._docker")
    def test_healthy_canary_never_errors(self, mock_docker: MagicMock) -> None:
        state = {"canary_empty": False}
        container = MagicMock()

        def exec_run(argv: list[str], **kwargs: Any) -> tuple[int, object]:
            if argv and argv[0] == "echo":
                nonce = argv[1]
                if state["canary_empty"]:
                    return (0, (b"", b""))
                return (0, (nonce.encode("utf-8") + b"\n", b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_run
        mock_docker.return_value.containers.get.return_value = container

        for _ in range(10 * capture_health.EMPTY_TRIGGER):
            result = json.loads(read_file_range(_CID, "/f.txt"))
            assert "status" not in result
        assert not capture_health.is_capture_broken(_CID)


# ---------------------------------------------------------------------------
# Issue #870: the four output-bearing tools PR #868 left fail-open
# ---------------------------------------------------------------------------
#
# #868 wired the guard into sandbox_exec and read_file_range only, so
# search_in_container, diff_in_container, verify_in_container and
# sandbox_exec_check neither fed the counter (a search-only session never
# tripped the canary) nor consulted the flag (they kept serving empty
# results after another tool had already found capture broken).  The wire
# tests below drive each tool in the incident shape -- every real command
# captures nothing -- with the same fixture style as
# ``TestSandboxExecCaptureGuard`` above.


def _incident_exec_run(state: dict[str, bool]):
    """Return an ``exec_run`` stand-in in the #852 incident shape.

    Real commands "run" but capture nothing; the canary (``echo <nonce>``,
    always ``demux=True``) answers according to ``state["canary_empty"]``.
    Non-canary calls answer in the shape docker-py itself would use:
    demuxed calls get a ``(stdout, stderr)`` tuple, plain ones get bytes.
    """

    def exec_run(argv: list[str], **kwargs: Any) -> tuple[int, object]:
        if argv and argv[0] == "echo":
            nonce = argv[1]
            if state["canary_empty"]:
                return (0, (b"", b""))
            return (0, (nonce.encode("utf-8") + b"\n", b""))
        if kwargs.get("demux"):
            return (0, (b"", b""))
        return (0, b"")

    return exec_run


def _incident_container(canary_empty: bool) -> tuple[MagicMock, dict[str, bool]]:
    """A mock container in the incident shape, plus its mutable state."""
    state = {"canary_empty": canary_empty}
    container = MagicMock()
    container.exec_run.side_effect = _incident_exec_run(state)
    container.attrs = {"Config": {"WorkingDir": "/workspace"}}
    return container, state


def _break_capture_via_sandbox_exec(container: MagicMock) -> None:
    """Trip the guard for ``_CID`` through a *different* tool.

    The point of the "consult" half of #870: once ``capture_broken`` is
    set for a container, the four tools must stop serving empty results
    even though they were not the ones that tripped it.
    """
    with patch("sunaba.tools.exec._docker") as mock_docker:
        mock_docker.return_value.containers.get.return_value = container
        for _ in range(capture_health.EMPTY_TRIGGER):
            sandbox_exec(container_id=_CID, commands=["true"])
    assert capture_health.is_capture_broken(_CID)


class TestSearchInContainerCaptureGuard:
    """search_in_container: "no hits" must not mean "capture is broken"."""

    def test_incident_shape_trips_loud_error(self) -> None:
        container, _state = _incident_container(canary_empty=True)
        with patch("sunaba.tools.verify._docker") as mock_docker:
            mock_docker.return_value.containers.get.return_value = container
            for _ in range(capture_health.EMPTY_TRIGGER - 1):
                result = json.loads(
                    search_in_container(_CID, "pattern", path="/workspace")
                )
                assert result["matches"] == []  # still fail-open, pre-threshold
            result = json.loads(
                search_in_container(_CID, "pattern", path="/workspace")
            )
        assert result["status"] == "error"
        assert capture_health.RESTART_REMEDY in result["error"]

    def test_healthy_canary_still_serves_no_match(self) -> None:
        container, _state = _incident_container(canary_empty=False)
        with patch("sunaba.tools.verify._docker") as mock_docker:
            mock_docker.return_value.containers.get.return_value = container
            for _ in range(10 * capture_health.EMPTY_TRIGGER):
                result = json.loads(
                    search_in_container(_CID, "pattern", path="/workspace")
                )
                assert result["matches"] == []
                assert "status" not in result
        assert not capture_health.is_capture_broken(_CID)

    def test_flag_set_by_another_tool_blocks_search(self) -> None:
        container, _state = _incident_container(canary_empty=True)
        _break_capture_via_sandbox_exec(container)
        with patch("sunaba.tools.verify._docker") as mock_docker:
            mock_docker.return_value.containers.get.return_value = container
            result = json.loads(
                search_in_container(_CID, "pattern", path="/workspace")
            )
        assert result["status"] == "error"
        assert capture_health.RESTART_REMEDY in result["error"]


class TestDiffInContainerCaptureGuard:
    """diff_in_container: the first tool observed failing in #852."""

    def test_incident_shape_trips_loud_error(self) -> None:
        container, _state = _incident_container(canary_empty=True)
        with patch("sunaba.tools.diff._docker") as mock_docker, patch(
            "sunaba.tools.diff.resolve_git_root", return_value="/repo"
        ):
            mock_docker.return_value.containers.get.return_value = container
            for _ in range(capture_health.EMPTY_TRIGGER - 1):
                result = json.loads(diff_in_container(_CID, worktree=True))
                assert result["total_files"] == 0  # fail-open empty diff
            result = json.loads(diff_in_container(_CID, worktree=True))
        assert result["status"] == "error"
        assert capture_health.RESTART_REMEDY in result["error"]

    def test_no_merge_base_is_replaced_by_the_loud_error(self) -> None:
        """The incident's first symptom, now attributed to capture.

        An empty merge-base decode reads as "git found no merge base",
        which sent the operator looking for a branch problem that did not
        exist.  Once the guard trips, the answer names the real cause.
        """
        container, _state = _incident_container(canary_empty=True)
        with patch("sunaba.tools.diff._docker") as mock_docker, patch(
            "sunaba.tools.diff.resolve_git_root", return_value="/repo"
        ), patch(
            "sunaba.tools.diff._resolve_base_branch",
            return_value=("main", "mocked"),
        ):
            mock_docker.return_value.containers.get.return_value = container
            for _ in range(capture_health.EMPTY_TRIGGER - 1):
                result = json.loads(diff_in_container(_CID))
                assert result["step"] == "merge_base"  # the bogus symptom
            result = json.loads(diff_in_container(_CID))
        assert result["status"] == "error"
        assert "merge base" not in result["error"]
        assert capture_health.RESTART_REMEDY in result["error"]

    def test_healthy_canary_still_serves_empty_diff(self) -> None:
        container, _state = _incident_container(canary_empty=False)
        with patch("sunaba.tools.diff._docker") as mock_docker, patch(
            "sunaba.tools.diff.resolve_git_root", return_value="/repo"
        ):
            mock_docker.return_value.containers.get.return_value = container
            for _ in range(10 * capture_health.EMPTY_TRIGGER):
                result = json.loads(diff_in_container(_CID, worktree=True))
                assert result["total_files"] == 0
                assert result["untracked"] == []
        assert not capture_health.is_capture_broken(_CID)

    def test_flag_set_by_another_tool_blocks_diff(self) -> None:
        container, _state = _incident_container(canary_empty=True)
        _break_capture_via_sandbox_exec(container)
        with patch("sunaba.tools.diff._docker") as mock_docker, patch(
            "sunaba.tools.diff.resolve_git_root", return_value="/repo"
        ):
            mock_docker.return_value.containers.get.return_value = container
            result = json.loads(diff_in_container(_CID, worktree=True))
        assert result["status"] == "error"
        assert capture_health.RESTART_REMEDY in result["error"]


class TestVerifyInContainerCaptureGuard:
    """verify_in_container: a green gate must not be built from empties."""

    def test_incident_shape_trips_loud_error(self) -> None:
        container, _state = _incident_container(canary_empty=True)
        with patch("sunaba.tools.verify._docker") as mock_docker:
            mock_docker.return_value.containers.get.return_value = container
            for _ in range(capture_health.EMPTY_TRIGGER - 1):
                result = json.loads(verify_in_container(_CID, "tests/"))
                assert "gate_passed" in result  # fail-open verdict
            result = json.loads(verify_in_container(_CID, "tests/"))
        assert result["status"] == "error"
        assert capture_health.RESTART_REMEDY in result["error"]

    def test_refused_verify_records_no_success(self) -> None:
        """A refused verify must not leave a publish-unblocking record."""
        container, _state = _incident_container(canary_empty=True)
        with patch("sunaba.tools.verify._docker") as mock_docker, patch(
            "sunaba.tools.verify.record_verify_success"
        ) as rec:
            mock_docker.return_value.containers.get.return_value = container
            for _ in range(capture_health.EMPTY_TRIGGER):
                result = json.loads(verify_in_container(_CID, "tests/"))
        assert result["status"] == "error"
        # The pre-threshold calls still recorded (they were served); the
        # refused one did not.
        assert rec.call_count == capture_health.EMPTY_TRIGGER - 1

    def test_healthy_canary_still_serves_the_verdict(self) -> None:
        container, _state = _incident_container(canary_empty=False)
        with patch("sunaba.tools.verify._docker") as mock_docker:
            mock_docker.return_value.containers.get.return_value = container
            for _ in range(10 * capture_health.EMPTY_TRIGGER):
                result = json.loads(verify_in_container(_CID, "tests/"))
                assert "gate_passed" in result
                assert result.get("status") != "error"
        assert not capture_health.is_capture_broken(_CID)

    def test_flag_set_by_another_tool_blocks_verify(self) -> None:
        container, _state = _incident_container(canary_empty=True)
        _break_capture_via_sandbox_exec(container)
        with patch("sunaba.tools.verify._docker") as mock_docker:
            mock_docker.return_value.containers.get.return_value = container
            result = json.loads(verify_in_container(_CID, "tests/"))
        assert result["status"] == "error"
        assert capture_health.RESTART_REMEDY in result["error"]


class TestSandboxExecCheckCaptureGuard:
    """sandbox_exec_check: a poll must not report "running" forever."""

    def test_incident_shape_trips_loud_error(self) -> None:
        container, _state = _incident_container(canary_empty=True)
        with patch("sunaba.tools.exec._docker") as mock_docker:
            mock_docker.return_value.containers.get.return_value = container
            for _ in range(capture_health.EMPTY_TRIGGER - 1):
                result = json.loads(sandbox_exec_check(_CID, _JOB_ID))
                assert result["status"] == "running"  # fail-open poll
            result = json.loads(sandbox_exec_check(_CID, _JOB_ID))
        assert result["status"] == "error"
        assert capture_health.RESTART_REMEDY in result["error"]

    def test_healthy_empty_job_output_still_served(self) -> None:
        """A job that printed nothing is a legitimate empty, not a break."""
        container = MagicMock()

        def exec_run(argv: list[str], **kwargs: Any) -> tuple[int, object]:
            if argv and argv[0] == "echo":
                nonce = argv[1]
                return (0, (nonce.encode("utf-8") + b"\n", b""))
            cmd = argv[-1]
            if "EXIT=" in cmd:  # the status block always prints
                return (0, b"NOW=100\nSTART=90\nOUT_MTIME=0\nERR_MTIME=0\nEXIT=0")
            return (0, b"")  # the job itself printed nothing

        container.exec_run.side_effect = exec_run
        with patch("sunaba.tools.exec._docker") as mock_docker:
            mock_docker.return_value.containers.get.return_value = container
            for _ in range(10 * capture_health.EMPTY_TRIGGER):
                result = json.loads(sandbox_exec_check(_CID, _JOB_ID))
                assert result["status"] == "completed"
                assert result["output"] == ""
        assert not capture_health.is_capture_broken(_CID)

    def test_invalid_job_id_is_not_fed_to_the_guard(self) -> None:
        """A structured error the tool already returns is not an empty decode."""
        container, _state = _incident_container(canary_empty=True)
        with patch("sunaba.tools.exec._docker") as mock_docker:
            mock_docker.return_value.containers.get.return_value = container
            for _ in range(10 * capture_health.EMPTY_TRIGGER):
                result = json.loads(sandbox_exec_check(_CID, "not a job id"))
                assert result["status"] == "error"
                assert "invalid job_id" in result["error"]
        assert capture_health.consecutive_empty(_CID) == 0

    def test_flag_set_by_another_tool_blocks_exec_check(self) -> None:
        container, _state = _incident_container(canary_empty=True)
        _break_capture_via_sandbox_exec(container)
        with patch("sunaba.tools.exec._docker") as mock_docker:
            mock_docker.return_value.containers.get.return_value = container
            result = json.loads(sandbox_exec_check(_CID, _JOB_ID))
        assert result["status"] == "error"
        assert capture_health.RESTART_REMEDY in result["error"]


class TestCanaryTimeout:
    """The canary must not block on a wedged container (issue #870, [low]).

    The guard fires precisely when a container may be wedged, and docker-py
    has no per-exec timeout: an unbounded ``echo`` probe turns a fail-open
    empty result into a tool call that never returns.  Before #868 the same
    call returned an empty ``ok`` immediately.
    """

    def test_bound_is_the_existing_recovery_docker_timeout(self) -> None:
        from sunaba.tools.common import RECOVERY_DOCKER_TIMEOUT

        assert capture_health._canary_timeout() == RECOVERY_DOCKER_TIMEOUT

    def test_hung_canary_returns_container_level_error(self) -> None:
        release = threading.Event()

        class _HungContainer:
            def exec_run(self, argv: list[str], **kwargs: Any) -> tuple[int, object]:
                if argv and argv[0] == "echo":
                    release.wait(30)  # never answers within the bound
                return (0, (b"", b""))

        container = _HungContainer()
        try:
            started = time.monotonic()
            with patch.object(
                capture_health, "_canary_timeout", return_value=0.2
            ):
                result = None
                for _ in range(capture_health.EMPTY_TRIGGER):
                    result = capture_health.check_capture(
                        container, decoded_empty=True, container_id=_CID
                    )
            elapsed = time.monotonic() - started
        finally:
            release.set()

        assert result is not None
        payload = json.loads(result)
        assert payload["status"] == "error"
        # The container-level class, not the #852 empty-nonce class: a
        # wedged container is not fixed by restarting the server.
        assert capture_health.RESTART_REMEDY not in payload["error"]
        assert _CID in payload["error"]
        assert "did not return" in payload["error"]
        assert capture_health.is_capture_broken(_CID)
        assert elapsed < 5, "the canary blocked instead of timing out"

    def test_timeout_is_journaled_as_the_canary_error(self) -> None:
        release = threading.Event()

        class _HungContainer:
            def exec_run(self, argv: list[str], **kwargs: Any) -> tuple[int, object]:
                if argv and argv[0] == "echo":
                    release.wait(30)
                return (0, (b"", b""))

        container = _HungContainer()
        try:
            with patch(
                "sunaba.capture_health.record_capture_health"
            ) as rec, patch.object(
                capture_health, "_canary_timeout", return_value=0.2
            ):
                for _ in range(capture_health.EMPTY_TRIGGER):
                    capture_health.check_capture(
                        container, decoded_empty=True, container_id=_CID
                    )
            trip_call = rec.call_args_list[0]
        finally:
            release.set()

        assert trip_call.args[1] == "broken"
        assert trip_call.kwargs["canary_nonce_found"] is False
        assert "did not return" in trip_call.kwargs["canary_error"]
