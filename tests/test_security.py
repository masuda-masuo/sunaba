"""Tests for the static security guardrail module.

Tests cover:
- SecurityProfile defaults
- validate_image_ref
- build_secure_run_kwargs with all guardrails
- Volume/mount validation
- Privileged mode rejection
- Resource limit application
- Network default
"""
from __future__ import annotations

import pytest

from code_sandbox_mcp.security import (
    DEFAULT_SECURITY_PROFILE,
    SecurityProfile,
    _is_allowed_host_path,
    _is_dangerous_socket,
    _validate_volumes,
    build_secure_run_kwargs,
    validate_image_ref,
)


class TestSecurityProfile:
    """Tests for SecurityProfile dataclass defaults."""

    def test_default_profile_non_root_user(self) -> None:
        assert DEFAULT_SECURITY_PROFILE.user == "sandbox"

    def test_default_profile_forbid_privileged(self) -> None:
        assert DEFAULT_SECURITY_PROFILE.forbid_privileged is True

    def test_default_profile_reject_dangerous_sockets(self) -> None:
        assert DEFAULT_SECURITY_PROFILE.reject_dangerous_sockets is True

    def test_default_profile_has_allowed_host_mount_prefixes(self) -> None:
        assert len(DEFAULT_SECURITY_PROFILE.allowed_host_mount_prefixes) > 0

    def test_default_profile_has_mem_limit(self) -> None:
        assert DEFAULT_SECURITY_PROFILE.mem_limit == "512m"

    def test_default_profile_has_memswap_limit(self) -> None:
        assert DEFAULT_SECURITY_PROFILE.memswap_limit == "512m"

    def test_default_profile_has_cpu_period(self) -> None:
        assert DEFAULT_SECURITY_PROFILE.cpu_period == 100000

    def test_default_profile_has_cpu_quota(self) -> None:
        assert DEFAULT_SECURITY_PROFILE.cpu_quota == 50000

    def test_default_profile_has_pids_limit(self) -> None:
        assert DEFAULT_SECURITY_PROFILE.pids_limit == 500

    def test_default_profile_network_mode_none(self) -> None:
        assert DEFAULT_SECURITY_PROFILE.network_mode == "none"

    def test_default_profile_require_digest(self) -> None:
        assert DEFAULT_SECURITY_PROFILE.require_digest is True

    def test_profile_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            DEFAULT_SECURITY_PROFILE.user = "root"  # type: ignore[misc]

    def test_custom_profile(self) -> None:
        profile = SecurityProfile(
            user="testuser",
            mem_limit="1g",
            network_mode="bridge",
            require_digest=False,
        )
        assert profile.user == "testuser"
        assert profile.mem_limit == "1g"
        assert profile.network_mode == "bridge"
        assert profile.require_digest is False

    def test_default_allow_network_false(self) -> None:
        assert DEFAULT_SECURITY_PROFILE.allow_network is False

    def test_allow_network_true_sets_network_mode(self) -> None:
        profile = SecurityProfile(allow_network=True)
        assert profile.allow_network is True
        # allow_network does not change network_mode directly;
        # build_secure_run_kwargs handles the override


class TestValidateImageRef:
    """Tests for validate_image_ref."""

    def test_valid_digest_ref_passes(self) -> None:
        # Should not raise
        validate_image_ref(
            "python@sha256:"
            "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
            "e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
        )

    def test_valid_digest_with_registry_passes(self) -> None:
        validate_image_ref(
            "docker.io/library/python@sha256:"
            "00000000000000000000000000000000"
            "00000000000000000000000000000000"
        )

    def test_tag_ref_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="digest reference"):
            validate_image_ref("python:3.12-slim-bookworm")

    def test_bare_image_name_raises(self) -> None:
        with pytest.raises(ValueError, match="digest reference"):
            validate_image_ref("ubuntu")

    def test_latest_tag_raises(self) -> None:
        with pytest.raises(ValueError, match="digest reference"):
            validate_image_ref("ubuntu:latest")

    def test_invalid_digest_raises(self) -> None:
        with pytest.raises(ValueError, match="digest reference"):
            validate_image_ref(
                "python@sha256:invalid_hex"
            )

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="digest reference"):
            validate_image_ref("")


class TestBuildSecureRunKwargs:
    """Tests for build_secure_run_kwargs."""

    def test_non_root_user_applied(self) -> None:
        result = build_secure_run_kwargs(DEFAULT_SECURITY_PROFILE)
        assert result["user"] == "sandbox"

    def test_non_root_user_overrides_existing(self) -> None:
        """Even if someone passes user='root', it gets overwritten."""
        result = build_secure_run_kwargs(
            DEFAULT_SECURITY_PROFILE, user="root"
        )
        assert result["user"] == "sandbox"

    def test_privileged_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="Privileged mode"):
            build_secure_run_kwargs(
                DEFAULT_SECURITY_PROFILE, privileged=True
            )

    def test_privileged_false_allowed(self) -> None:
        """Explicit privileged=False should not raise."""
        result = build_secure_run_kwargs(
            DEFAULT_SECURITY_PROFILE, privileged=False
        )
        # Should not raise, but user should still be set
        assert result["user"] == "sandbox"

    @pytest.mark.parametrize(
        "volumes",
        [
            {"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}},
            {"/var/run/docker.sock": "/var/run/docker.sock"},
            {"/var/run/docker.sock:/var/run/docker.sock": {}},
            {"/run/docker.sock": {"bind": "/run/docker.sock", "mode": "rw"}},
        ],
    )
    def test_dangerous_socket_mount_rejected(
        self, volumes: dict
    ) -> None:
        with pytest.raises(ValueError, match="forbidden by security policy"):
            build_secure_run_kwargs(
                DEFAULT_SECURITY_PROFILE, volumes=volumes
            )

    def test_host_mount_not_in_whitelist_rejected(self) -> None:
        volumes = {"/etc/passwd": {"bind": "/host/etc/passwd", "mode": "ro"}}
        with pytest.raises(ValueError, match="not in the allowed mount"):
            build_secure_run_kwargs(
                DEFAULT_SECURITY_PROFILE, volumes=volumes
            )

    def test_tmp_mount_allowed(self) -> None:
        volumes = {"/tmp/myproject": {"bind": "/workspace", "mode": "rw"}}
        result = build_secure_run_kwargs(
            DEFAULT_SECURITY_PROFILE, volumes=volumes
        )
        assert result["volumes"] == volumes

    def test_home_mount_allowed(self) -> None:
        volumes = {"/home/user/project": {"bind": "/project", "mode": "ro"}}
        result = build_secure_run_kwargs(
            DEFAULT_SECURITY_PROFILE, volumes=volumes
        )
        assert result["volumes"] == volumes

    def test_mem_limit_applied(self) -> None:
        result = build_secure_run_kwargs(DEFAULT_SECURITY_PROFILE)
        assert result["mem_limit"] == "512m"

    def test_mem_limit_does_not_override_explicit(self) -> None:
        """Explicit mem_limit should be preserved (setdefault)."""
        result = build_secure_run_kwargs(
            DEFAULT_SECURITY_PROFILE, mem_limit="1g"
        )
        assert result["mem_limit"] == "1g"

    def test_memswap_limit_applied(self) -> None:
        result = build_secure_run_kwargs(DEFAULT_SECURITY_PROFILE)
        assert result["memswap_limit"] == "512m"

    def test_cpu_period_applied(self) -> None:
        result = build_secure_run_kwargs(DEFAULT_SECURITY_PROFILE)
        assert result["cpu_period"] == 100000

    def test_cpu_quota_applied(self) -> None:
        result = build_secure_run_kwargs(DEFAULT_SECURITY_PROFILE)
        assert result["cpu_quota"] == 50000

    def test_pids_limit_applied(self) -> None:
        result = build_secure_run_kwargs(DEFAULT_SECURITY_PROFILE)
        assert result["pids_limit"] == 500

    def test_network_mode_none_by_default(self) -> None:
        result = build_secure_run_kwargs(DEFAULT_SECURITY_PROFILE)
        assert result["network_mode"] == "none"

    def test_explicit_network_mode_preserved(self) -> None:
        """Explicit network_mode should be preserved (setdefault)."""
        result = build_secure_run_kwargs(
            DEFAULT_SECURITY_PROFILE, network_mode="bridge"
        )
        assert result["network_mode"] == "bridge"

    def test_original_kwargs_not_mutated(self) -> None:
        original = {"command": "sleep infinity", "detach": True}
        build_secure_run_kwargs(DEFAULT_SECURITY_PROFILE, **original)
        # Original should not have been modified
        assert "user" not in original
        assert "mem_limit" not in original

    def test_allow_network_false_keeps_network_mode_none(self) -> None:
        profile = SecurityProfile(allow_network=False)
        result = build_secure_run_kwargs(profile)
        assert result["network_mode"] == "none"

    def test_allow_network_true_sets_bridge(self) -> None:
        profile = SecurityProfile(allow_network=True)
        result = build_secure_run_kwargs(profile)
        assert result["network_mode"] == "bridge"

    def test_allow_network_overrides_explicit_network_mode(self) -> None:
        """allow_network=True should override any network_mode setting."""
        profile = SecurityProfile(allow_network=True, network_mode="none")
        result = build_secure_run_kwargs(profile)
        assert result["network_mode"] == "bridge"

    def test_allow_network_false_preserves_explicit_network_mode(self) -> None:
        """allow_network=False should preserve an explicit network_mode."""
        profile = SecurityProfile(allow_network=False, network_mode="bridge")
        result = build_secure_run_kwargs(profile)
        assert result["network_mode"] == "bridge"


class TestDangerousSocketDetection:
    """Tests for the _is_dangerous_socket helper."""

    @pytest.mark.parametrize(
        "path",
        [
            "/var/run/docker.sock",
            "/var/run/docker.sock:/var/run/docker.sock",
            "/run/docker.sock",
        ],
    )
    def test_dangerous_socket_detected(self, path: str) -> None:
        assert _is_dangerous_socket(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/tmp/test.sock",
            "/var/run/mysocket.sock",
            "",
            "/nonexistent/path",
        ],
    )
    def test_safe_socket_not_detected(self, path: str) -> None:
        assert _is_dangerous_socket(path) is False


class TestAllowedHostPath:
    """Tests for the _is_allowed_host_path helper."""

    ALLOWED = ("/tmp/", "/home/", "/Users/", "/mnt/")

    @pytest.mark.parametrize(
        "path",
        [
            "/tmp/myproject",
            "/home/user/code",
            "/Users/john/project",
            "/mnt/data",
            "/tmp/",
            "/home/",
        ],
    )
    def test_allowed_path(self, path: str) -> None:
        assert _is_allowed_host_path(path, self.ALLOWED) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/etc/passwd",
            "/var/run/docker.sock",
            "/root",
            "/usr/bin",
            "/opt",
        ],
    )
    def test_disallowed_path(self, path: str) -> None:
        assert _is_allowed_host_path(path, self.ALLOWED) is False

    def test_empty_allowed_prefixes(self) -> None:
        """Empty prefix tuple means no paths are allowed."""
        assert _is_allowed_host_path("/tmp/test", ()) is False


class TestValidateVolumes:
    """Tests for the _validate_volumes helper."""

    def test_dangerous_socket_rejected(self) -> None:
        volumes = {"/var/run/docker.sock": {"bind": "/var/run/docker.sock"}}
        with pytest.raises(ValueError, match="forbidden by security policy"):
            _validate_volumes(
                volumes,
                allowed_prefixes=("/tmp/",),
                reject_dangerous_sockets=True,
            )

    def test_disallowed_host_path_rejected(self) -> None:
        volumes = {"/etc/secret": {"bind": "/secret", "mode": "ro"}}
        with pytest.raises(ValueError, match="not in the allowed mount"):
            _validate_volumes(
                volumes,
                allowed_prefixes=("/tmp/",),
                reject_dangerous_sockets=True,
            )

    def test_allowed_volume_passes(self) -> None:
        volumes = {"/tmp/project": {"bind": "/project", "mode": "rw"}}
        # Should not raise
        _validate_volumes(
            volumes,
            allowed_prefixes=("/tmp/", "/home/"),
            reject_dangerous_sockets=True,
        )

    def test_none_allowed_prefixes_allows_all(self) -> None:
        """When allowed_prefixes is None, all paths are allowed."""
        volumes = {"/etc/passwd": {"bind": "/host/etc/passwd", "mode": "ro"}}
        # Should not raise
        _validate_volumes(
            volumes,
            allowed_prefixes=None,
            reject_dangerous_sockets=False,
        )

    def test_list_mount_config_format(self) -> None:
        """Volumes can also be specified as {path: [bind, mode]}."""
        volumes = {"/tmp/project": ["/project", "rw"]}
        # Should not raise
        _validate_volumes(
            volumes,
            allowed_prefixes=("/tmp/",),
            reject_dangerous_sockets=True,
        )

    def test_string_mount_config(self) -> None:
        """Volumes can be specified as {path: bind_target} string."""
        volumes = {"/var/run/docker.sock": "/var/run/docker.sock"}
        with pytest.raises(ValueError, match="forbidden by security policy"):
            _validate_volumes(
                volumes,
                allowed_prefixes=("/tmp/",),
                reject_dangerous_sockets=True,
            )


class TestBuildSecureRunKwargsAllowAll:
    """Tests with a profile that allows all mounts."""

    PERMISSIVE_PROFILE = SecurityProfile(
        allowed_host_mount_prefixes=None,  # Allow all
        reject_dangerous_sockets=False,
    )

    def test_any_host_path_allowed(self) -> None:
        volumes = {"/etc/passwd": {"bind": "/host/etc/passwd", "mode": "ro"}}
        result = build_secure_run_kwargs(
            self.PERMISSIVE_PROFILE, volumes=volumes
        )
        assert result["volumes"] == volumes

    def test_docker_socket_allowed_when_reject_false(self) -> None:
        volumes = {"/var/run/docker.sock": {"bind": "/var/run/docker.sock"}}
        result = build_secure_run_kwargs(
            self.PERMISSIVE_PROFILE, volumes=volumes
        )
        assert result["volumes"] == volumes

    def test_no_user_override_when_user_empty(self) -> None:
        profile = SecurityProfile(user="")
        result = build_secure_run_kwargs(profile)
        assert "user" not in result


class TestParseMemToMb:
    """Tests for _parse_mem_to_mb."""

    def test_parse_m(self) -> None:
        from code_sandbox_mcp.security import _parse_mem_to_mb
        assert _parse_mem_to_mb("512m") == 512
        assert _parse_mem_to_mb("0m") == 0

    def test_parse_g(self) -> None:
        from code_sandbox_mcp.security import _parse_mem_to_mb
        assert _parse_mem_to_mb("2g") == 2048
        assert _parse_mem_to_mb("1g") == 1024

    def test_parse_k(self) -> None:
        from code_sandbox_mcp.security import _parse_mem_to_mb
        assert _parse_mem_to_mb("1024k") == 1
        assert _parse_mem_to_mb("2048k") == 2

    def test_parse_plain_number(self) -> None:
        from code_sandbox_mcp.security import _parse_mem_to_mb
        assert _parse_mem_to_mb("2048") == 2048
        assert _parse_mem_to_mb("512") == 512

    def test_parse_case_insensitive(self) -> None:
        from code_sandbox_mcp.security import _parse_mem_to_mb
        assert _parse_mem_to_mb("2G") == 2048
        assert _parse_mem_to_mb("512M") == 512

    def test_parse_empty_raises(self) -> None:
        from code_sandbox_mcp.security import _parse_mem_to_mb
        with pytest.raises(ValueError, match="Empty memory string"):
            _parse_mem_to_mb("")

    def test_parse_invalid_raises(self) -> None:
        from code_sandbox_mcp.security import _parse_mem_to_mb
        with pytest.raises(ValueError):
            _parse_mem_to_mb("not-a-number")


class TestComputeDefaultLimits:
    """Tests for compute_default_limits."""

    def test_computes_from_host_resources(self, monkeypatch) -> None:
        from code_sandbox_mcp.security import compute_default_limits
        # Mock 16GB host / 8 CPUs
        monkeypatch.setattr(
            "code_sandbox_mcp.security._detect_host_resources",
            lambda: (16384, 8),
        )
        mem_str, cpus = compute_default_limits(0.25, 0.25)
        # 16384 * 0.25 = 4096
        assert mem_str == "4096m"
        # 8 * 0.25 = 2.0
        assert cpus == 2.0

    def test_floor_mem_512(self, monkeypatch) -> None:
        from code_sandbox_mcp.security import compute_default_limits
        # Very small host (e.g. 512MB)
        monkeypatch.setattr(
            "code_sandbox_mcp.security._detect_host_resources",
            lambda: (512, 1),
        )
        mem_str, cpus = compute_default_limits(0.25, 0.25)
        # 512 * 0.25 = 128, floor is 512
        assert mem_str == "512m"

    def test_floor_cpu_0_5(self, monkeypatch) -> None:
        from code_sandbox_mcp.security import compute_default_limits
        # Single-core host
        monkeypatch.setattr(
            "code_sandbox_mcp.security._detect_host_resources",
            lambda: (8192, 1),
        )
        mem_str, cpus = compute_default_limits(0.25, 0.25)
        # 1 * 0.25 = 0.25, floor is 0.5
        assert cpus == 0.5

    def test_fallback_on_detection_failure(self, monkeypatch) -> None:
        from code_sandbox_mcp.security import compute_default_limits
        monkeypatch.setattr(
            "code_sandbox_mcp.security._detect_host_resources",
            lambda: (0, 4),
        )
        mem_str, cpus = compute_default_limits(0.25, 0.25)
        # Fallback to hard-coded 512m / 0.5
        assert mem_str == "512m"
        assert cpus == 0.5

    def test_custom_ratios(self, monkeypatch) -> None:
        from code_sandbox_mcp.security import compute_default_limits
        monkeypatch.setattr(
            "code_sandbox_mcp.security._detect_host_resources",
            lambda: (32768, 16),
        )
        mem_str, cpus = compute_default_limits(0.5, 0.5)
        # 32768 * 0.5 = 16384
        assert mem_str == "16384m"
        # 16 * 0.5 = 8.0
        assert cpus == 8.0


class TestGetSetDefaultProfile:
    """Tests for get_default_profile / set_default_profile."""

    def test_returns_static_default_by_default(self) -> None:
        from code_sandbox_mcp.security import (
            DEFAULT_SECURITY_PROFILE,
            _effective_default_profile,
            get_default_profile,
        )
        # Ensure no override is set
        saved = _effective_default_profile
        try:
            from code_sandbox_mcp.security import set_default_profile
            set_default_profile(None)  # noqa: type error ok for test
        except TypeError:
            pass
        # After clearing, should return DEFAULT
        result = get_default_profile()
        assert result is DEFAULT_SECURITY_PROFILE

    def test_set_profile_returned_by_get(self) -> None:
        from code_sandbox_mcp.security import (
            SecurityProfile,
            get_default_profile,
            set_default_profile,
        )
        custom = SecurityProfile(mem_limit="1g")
        set_default_profile(custom)
        result = get_default_profile()
        assert result is custom
        # Reset
        from code_sandbox_mcp.security import DEFAULT_SECURITY_PROFILE
        set_default_profile(DEFAULT_SECURITY_PROFILE)


class TestDetectHostResources:
    """Tests for _detect_host_resources."""

    def test_returns_positive_values(self) -> None:
        from code_sandbox_mcp.security import _detect_host_resources
        mem_mb, cpus = _detect_host_resources()
        assert cpus >= 1
        # mem_mb may be 0 on platforms without sysconf support
        assert mem_mb >= 0
