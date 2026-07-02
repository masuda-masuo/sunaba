"""Tests for the egress-proxy sidecar entrypoint (#358).

Exercise the pure argv assembly and the thin ``main`` launch flow without a
container or mitmproxy installed, mirroring how the entrypoint runs in the
sidecar image.
"""
from __future__ import annotations

import pytest

from code_sandbox_mcp.proxy_entrypoint import (
    ADDON_ENV,
    CONFDIR_ENV,
    DEFAULT_ADDON,
    DEFAULT_CONFDIR,
    DEFAULT_LISTEN_HOST,
    DEFAULT_LISTEN_PORT,
    LISTEN_HOST_ENV,
    LISTEN_PORT_ENV,
    ProxyEntrypointError,
    build_mitmdump_argv,
    main,
)


def _kv(argv: list[str]) -> dict[str, str]:
    """Collapse an ``--opt value`` argv into a dict for order-free assertions."""
    out: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("-") and i + 1 < len(argv):
            out[argv[i]] = argv[i + 1]
            i += 2
        else:
            i += 1
    return out


class TestBuildArgv:
    def test_defaults_when_env_empty(self) -> None:
        argv = build_mitmdump_argv({})
        assert argv[0] == "mitmdump"
        kv = _kv(argv)
        assert kv["-s"] == DEFAULT_ADDON
        assert kv["--listen-host"] == DEFAULT_LISTEN_HOST
        assert kv["--listen-port"] == str(DEFAULT_LISTEN_PORT)
        assert kv["--set"] == f"confdir={DEFAULT_CONFDIR}"

    def test_overrides_from_env(self) -> None:
        kv = _kv(
            build_mitmdump_argv(
                {
                    LISTEN_HOST_ENV: "127.0.0.1",
                    LISTEN_PORT_ENV: "9090",
                    CONFDIR_ENV: "/var/certs",
                    ADDON_ENV: "/custom/proxy.py",
                }
            )
        )
        assert kv["--listen-host"] == "127.0.0.1"
        assert kv["--listen-port"] == "9090"
        assert kv["--set"] == "confdir=/var/certs"
        assert kv["-s"] == "/custom/proxy.py"

    def test_blank_values_fall_back_to_defaults(self) -> None:
        kv = _kv(
            build_mitmdump_argv(
                {LISTEN_HOST_ENV: "   ", LISTEN_PORT_ENV: "", CONFDIR_ENV: "  "}
            )
        )
        assert kv["--listen-host"] == DEFAULT_LISTEN_HOST
        assert kv["--listen-port"] == str(DEFAULT_LISTEN_PORT)
        assert kv["--set"] == f"confdir={DEFAULT_CONFDIR}"

    def test_non_numeric_port_raises(self) -> None:
        with pytest.raises(ProxyEntrypointError, match="integer"):
            build_mitmdump_argv({LISTEN_PORT_ENV: "http"})

    @pytest.mark.parametrize("port", ["0", "-1", "70000"])
    def test_out_of_range_port_raises(self, port: str) -> None:
        with pytest.raises(ProxyEntrypointError, match="1..65535"):
            build_mitmdump_argv({LISTEN_PORT_ENV: port})


class TestMain:
    def test_execs_assembled_argv(self) -> None:
        calls: list[tuple[str, list[str]]] = []

        def fake_exec(file: str, argv) -> None:
            calls.append((file, list(argv)))

        rc = main(env={LISTEN_PORT_ENV: "8081"}, exec_fn=fake_exec)
        # exec_fn returning is treated as a launch failure.
        assert rc == 1
        assert len(calls) == 1
        file, argv = calls[0]
        assert file == "mitmdump"
        assert argv == build_mitmdump_argv({LISTEN_PORT_ENV: "8081"})

    def test_config_error_returns_2_without_exec(self) -> None:
        def fake_exec(file: str, argv) -> None:  # pragma: no cover - must not run
            raise AssertionError("exec must not be attempted on bad config")

        rc = main(env={LISTEN_PORT_ENV: "nope"}, exec_fn=fake_exec)
        assert rc == 2
