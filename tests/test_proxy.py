    def test_builtin_registries_pass(self) -> None:
        guard = EgressGuard()
        assert guard.decide_host("pypi.org").allow is True
        assert guard.decide_host("files.pythonhosted.org").allow is True
        assert guard.decide_host("registry.npmjs.org").allow is True
        assert guard.decide_host("proxy.golang.org").allow is True
        assert guard.decide_host("sum.golang.org").allow is True

    def test_unknown_host_denied_by_default(self) -> None:
        guard = EgressGuard()
        d = guard.decide_host("attacker.com")
        assert d.allow is False
        assert "allowlist" in d.reason

    def test_empty_host_denied(self) -> None:
        # A request with no resolvable host must not slip through.
        assert EgressGuard().decide_host("").allow is False

    def test_host_match_is_case_insensitive(self) -> None:
        assert EgressGuard().decide_host("GitHub.com").allow is True

    def test_subdomain_wildcard_entry_matches_subdomains(self) -> None:
        # ``.githubusercontent.com`` is a built-in dotted entry.
        guard = EgressGuard()
        assert guard.decide_host("raw.githubusercontent.com").allow is True
        assert guard.decide_host("objects.githubusercontent.com").allow is True
        # The bare apex is matched too, but a lookalike suffix is not.
        assert guard.decide_host("githubusercontent.com").allow is True
        assert guard.decide_host("evilgithubusercontent.com.attacker.com").allow is False

    def test_operator_added_host_passes(self) -> None:
        guard = EgressGuard(allowed_egress_hosts={"internal.example.com"})
        assert guard.decide_host("internal.example.com").allow is True
        assert guard.decide_host("other.example.com").allow is False

    def test_operator_added_dotted_host_matches_subdomains(self) -> None:
        guard = EgressGuard(allowed_egress_hosts={".example.com"})
        assert guard.decide_host("a.example.com").allow is True
        assert guard.decide_host("example.com").allow is True

    def test_builtins_cannot_be_configured_away(self) -> None:
        # Passing an unrelated host does not drop the built-in registries.
        guard = EgressGuard(allowed_egress_hosts={"internal.example.com"})
        assert guard.decide_host("pypi.org").allow is True

    def test_wildcard_disables_containment(self) -> None:
        guard = EgressGuard(allowed_egress_hosts={EGRESS_HOST_WILDCARD})
        assert guard.decide_host("attacker.com").allow is True
        assert guard.decide_host("anything.example").allow is True


class TestAllowedEgressHostsFromEnv:
    """Parsing CODE_SANDBOX_ALLOWED_EGRESS_HOSTS (built-ins added by the guard)."""

    def test_comma_separated_lowercased(self) -> None:
        env = {"CODE_SANDBOX_ALLOWED_EGRESS_HOSTS": "A.com, B.org ,.internal"}
        assert allowed_egress_hosts_from_env(env) == {"a.com", "b.org", ".internal"}

    def test_empty_is_empty_set(self) -> None:
        assert allowed_egress_hosts_from_env({}) == set()
        assert allowed_egress_hosts_from_env({"CODE_SANDBOX_ALLOWED_EGRESS_HOSTS": ""}) == set()

    def test_wildcard_passes_through(self) -> None:
        env = {"CODE_SANDBOX_ALLOWED_EGRESS_HOSTS": "*"}
        assert allowed_egress_hosts_from_env(env) == {EGRESS_HOST_WILDCARD}

    def test_env_wired_into_guard(self) -> None:
        hosts = allowed_egress_hosts_from_env(
            {"CODE_SANDBOX_ALLOWED_EGRESS_HOSTS": "internal.example.com"}
        )
        guard = EgressGuard(allowed_egress_hosts=hosts)
        assert guard.decide_host("internal.example.com").allow is True
        # Built-ins still present.
        assert "github.com" in DEFAULT_EGRESS_HOSTS
        assert guard.decide_host("github.com").allow is True
        assert "proxy.golang.org" in DEFAULT_EGRESS_HOSTS
        assert guard.decide_host("proxy.golang.org").allow is True
        assert "sum.golang.org" in DEFAULT_EGRESS_HOSTS
        assert guard.decide_host("sum.golang.org").allow is True
        # And the block hint names the right env var.
        assert "CODE_SANDBOX_ALLOWED_EGRESS_HOSTS" in EGRESS_HOST_BLOCK_HINT