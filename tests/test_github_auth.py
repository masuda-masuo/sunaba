"""Unit tests for code_sandbox_mcp.github_auth (issue #203, PR-A).

httpx is mocked to verify installation-token issue / cache / refresh.
The RS256 key is generated in-test. Backward-compat (no GitHub App env ->
no-op) is verified explicitly.
"""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from code_sandbox_mcp import github_auth
from code_sandbox_mcp.github_auth import (
    AppTokenProvider,
    build_app_token_provider,
    setup_github_app_token,
)

_ENV_KEYS = [
    "GITHUB_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_PRIVATE_KEY_PATH",
]


@pytest.fixture
def rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


# --- builder / backward-compat ------------------------------------------------


def test_no_app_env_is_noop(clean_env):
    # No GitHub App env -> builder returns None (fall back to existing GITHUB_TOKEN).
    assert build_app_token_provider() is None


def test_setup_noop_preserves_existing_token(clean_env):
    clean_env.setenv("GITHUB_TOKEN", "ghp_existing")
    provider = setup_github_app_token()
    assert provider is None
    # existing token left untouched -> zero impact on existing deployments
    import os

    assert os.environ["GITHUB_TOKEN"] == "ghp_existing"


def test_build_app_provider(clean_env, rsa_pem):
    clean_env.setenv("GITHUB_APP_ID", "123")
    clean_env.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    clean_env.setenv("GITHUB_APP_PRIVATE_KEY", rsa_pem)
    provider = build_app_token_provider()
    assert isinstance(provider, AppTokenProvider)


def test_build_app_provider_from_key_path(clean_env, rsa_pem, tmp_path):
    key_file = tmp_path / "key.pem"
    key_file.write_text(rsa_pem, encoding="utf-8")
    clean_env.setenv("GITHUB_APP_ID", "123")
    clean_env.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    clean_env.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(key_file))
    provider = build_app_token_provider()
    assert isinstance(provider, AppTokenProvider)
    assert provider._key == rsa_pem


def test_incomplete_app_env_raises(clean_env):
    clean_env.setenv("GITHUB_APP_ID", "123")  # installation/key missing
    with pytest.raises(ValueError):
        build_app_token_provider()


# --- JWT claims ---------------------------------------------------------------


def test_app_jwt_claims(rsa_pem):
    key = serialization.load_pem_private_key(rsa_pem.encode(), password=None)
    prov = AppTokenProvider("123456", rsa_pem, "789")
    token = prov._app_jwt()
    decoded = jwt.decode(token, key.public_key(), algorithms=["RS256"])
    now = int(time.time())
    assert decoded["iss"] == "123456"
    assert abs(decoded["iat"] - (now - 60)) < 5
    assert abs(decoded["exp"] - (now + 540)) < 5


# --- refresh / cache / errors -------------------------------------------------


def _mock_post(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def fake_post(url, **kw):
        allowed = {k: v for k, v in kw.items() if k in ("headers", "timeout", "json")}
        with httpx.Client(transport=transport) as c:
            return c.post(url, **allowed)

    monkeypatch.setattr(github_auth.httpx, "post", fake_post)


def test_refresh_and_cache(monkeypatch, rsa_pem):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path == "/app/installations/789/access_tokens"
        assert request.headers["Authorization"].startswith("Bearer ")
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        return httpx.Response(201, json={"token": "ghs_tok", "expires_at": exp})

    _mock_post(monkeypatch, handler)
    prov = AppTokenProvider("123456", rsa_pem, "789")

    assert prov.get_token() == "ghs_tok"
    assert calls["n"] == 1
    # cached while valid -> no re-issue
    assert prov.get_token() == "ghs_tok"
    assert calls["n"] == 1
    # within REFRESH_BEFORE of expiry -> re-issue
    prov._expires_at = time.time() + 100
    prov.get_token()
    assert calls["n"] == 2


def test_setup_sets_github_token(monkeypatch, clean_env, rsa_pem):
    clean_env.setenv("GITHUB_APP_ID", "123456")
    clean_env.setenv("GITHUB_APP_INSTALLATION_ID", "789")
    clean_env.setenv("GITHUB_APP_PRIVATE_KEY", rsa_pem)

    def handler(request: httpx.Request) -> httpx.Response:
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        return httpx.Response(201, json={"token": "ghs_app_tok", "expires_at": exp})

    _mock_post(monkeypatch, handler)
    provider = setup_github_app_token()
    assert isinstance(provider, AppTokenProvider)
    import os

    assert os.environ["GITHUB_TOKEN"] == "ghs_app_tok"


@pytest.mark.parametrize(
    "code,fragment",
    [
        (401, "JWT rejected"),
        (404, "Installation not found"),
        (403, "Insufficient permissions"),
    ],
)
def test_refresh_error_messages(monkeypatch, rsa_pem, code, fragment):
    _mock_post(monkeypatch, lambda req: httpx.Response(code, json={"message": "x"}))
    prov = AppTokenProvider("1", rsa_pem, "2")
    with pytest.raises(RuntimeError) as ei:
        prov.get_token()
    assert fragment in str(ei.value)


# --- global provider singleton (issue #474) -----------------------------------


def test_global_provider_default_none(clean_env):
    assert github_auth.get_global_provider() is None


def test_set_global_provider(clean_env, rsa_pem):
    provider = AppTokenProvider("123", rsa_pem, "456")
    github_auth.set_global_provider(provider)
    assert github_auth.get_global_provider() is provider
    github_auth.set_global_provider(None)
    assert github_auth.get_global_provider() is None


def test_resolve_vcs_token_uses_global_provider(monkeypatch, clean_env, rsa_pem):
    from code_sandbox_mcp.tools import vcs

    clean_env.setenv("GITHUB_TOKEN", "ghp_static")
    provider = AppTokenProvider("123", rsa_pem, "456")
    provider._token = "ghs_provider_tok"
    provider._expires_at = time.time() + 3600
    github_auth.set_global_provider(provider)
    try:
        # provider should take precedence over static env
        assert vcs._resolve_vcs_token() == "ghs_provider_tok"
    finally:
        github_auth.set_global_provider(None)


def test_resolve_vcs_token_falls_back_to_env_without_provider(clean_env):
    from code_sandbox_mcp.tools import vcs

    clean_env.setenv("GITHUB_TOKEN", "ghp_static")
    assert github_auth.get_global_provider() is None
    assert vcs._resolve_vcs_token() == "ghp_static"


def test_refresh_network_error_reuses_cached_token(monkeypatch, rsa_pem):
    prov = AppTokenProvider("1", rsa_pem, "2")
    prov._token = "ghs_cached"
    prov._expires_at = time.time() + 1000  # still valid

    def boom(url, **kw):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(github_auth.httpx, "post", boom)
    # force a refresh attempt; cached-but-valid token should be reused
    prov._expires_at = time.time() + 100  # inside REFRESH_BEFORE window
    assert prov.get_token() == "ghs_cached"
