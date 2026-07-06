import code_sandbox_mcp.security as security
        security._effective_default_profile = None

    def teardown_method(self) -> None:
        # Reset the module-level effective profile so tests don't leak state.
        import code_sandbox_mcp.security as security
        security._effective_default_profile = None