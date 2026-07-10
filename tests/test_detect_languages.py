"""Tests for detect_languages and DetectionResult (Issue #109)."""

from __future__ import annotations

# ===================================================================
# _parse_ruff_output tests
# ===================================================================




class TestDetectLanguages:
    """Tests for detect_languages and DetectionResult."""

    def test_detection_result_dataclass(self) -> None:
        from src.sunaba.edit_verify import DetectionResult

        r = DetectionResult(languages={"python"}, scope={"python": "/app"}, reason=None)
        assert r.languages == {"python"}
        assert r.scope == {"python": "/app"}
        assert r.reason is None

        r2 = DetectionResult(languages=set(), scope={}, reason="no markers")
        assert r2.languages == set()
        assert r2.reason == "no markers"

    def test_explicit_language_skips_detection(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        result = detect_languages(mock_container, "/some/path", language="python")
        assert result.languages == {"python"}
        assert result.scope == {"python": "/some/path"}
        assert result.reason is None
        mock_container.exec_run.assert_not_called()

    def test_working_dir_passed_to_exec_run(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/go.mod\n", b""))

        # working_dir should be passed to exec_run as workdir=
        detect_languages(mock_container, ".", working_dir="/app")
        call_kwargs = mock_container.exec_run.call_args[1]
        assert call_kwargs.get("workdir") == "/app"

    def test_working_dir_none_default(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/go.mod\n", b""))

        detect_languages(mock_container, "/app")
        call_kwargs = mock_container.exec_run.call_args[1]
        # workdir should not be set or be None when working_dir is not passed
        assert "workdir" not in call_kwargs or call_kwargs.get("workdir") is None

    def test_file_extension_python(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        result = detect_languages(mock_container, "/app/main.py")
        assert result.languages == {"python"}
        assert result.reason is None

    def test_file_extension_js(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        result = detect_languages(mock_container, "/app/index.js")
        assert result.languages == {"js"}

    def test_file_extension_jsx(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        result = detect_languages(mock_container, "/app/component.jsx")
        assert result.languages == {"js"}

    def test_file_extension_mjs(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        result = detect_languages(mock_container, "/app/module.mjs")
        assert result.languages == {"js"}

    def test_file_extension_cjs(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        result = detect_languages(mock_container, "/app/module.cjs")
        assert result.languages == {"js"}

    def test_file_extension_ts(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        # Mock upward tsconfig search: no tsconfig found
        mock_container.exec_run.return_value = (1, (b"", b""))
        result = detect_languages(mock_container, "/app/src/main.ts")
        assert result.languages == {"ts"}

    def test_file_extension_tsx(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (1, (b"", b""))
        result = detect_languages(mock_container, "/app/src/component.tsx")
        assert result.languages == {"ts"}

    def test_file_extension_go(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        result = detect_languages(mock_container, "/app/main.go")
        assert result.languages == {"go"}

    def test_ts_file_with_tsconfig_upward(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        # Simulate tsconfig.json found in /app (parent of /app/src)
        def exec_side_effect(cmd, **kwargs):
            test_path = cmd[-1]  # e.g. "test -f /app/src/tsconfig.json && echo found || echo notfound"
            if "/app/src/tsconfig.json" in test_path:
                return (0, (b"notfound", b""))
            elif "/app/tsconfig.json" in test_path:
                return (0, (b"found", b""))
            return (1, (b"", b""))
        mock_container.exec_run.side_effect = exec_side_effect

        result = detect_languages(mock_container, "/app/src/main.ts")
        assert result.languages == {"ts"}
        # Scope should point to the directory with tsconfig.json
        assert "/app" in result.scope["ts"]

    def test_unknown_file_extension_returns_unknown(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (1, (b"", b""))
        result = detect_languages(mock_container, "/app/README.md")
        assert result.languages == set()
        assert result.reason is not None

    def test_directory_go_detection(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        # Simulate find output showing go.mod
        mock_container.exec_run.return_value = (0, (b"/app/go.mod\n", b""))

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"go"}
        assert result.scope.get("go") == "/app"

    def test_directory_python_detection(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/pyproject.toml\n", b""))

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"python"}

    def test_directory_js_detection(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/package.json\n", b""))

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"js"}

    def test_directory_ts_detection(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/tsconfig.json\n", b""))

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"ts"}

    def test_requirements_glob_pattern(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/requirements-dev.txt\n", b""))

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"python"}

    def test_multiple_requirements_files_dedup(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/requirements.txt\n/app/requirements-dev.txt\n", b""))

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"python"}

    def test_polyglot_python_and_js(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (
            0,
            (b"/app/pyproject.toml\n/app/frontend/package.json\n", b""),
        )

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"python", "js"}
        assert result.scope.get("python") == "/app"
        assert result.scope.get("js") == "/app/frontend"

    def test_polyglot_ts_and_js(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (
            0,
            (b"/app/package.json\n/app/tsconfig.json\n", b""),
        )

        result = detect_languages(mock_container, "/app")
        # ts and js should coexist (no more discarding)
        assert result.languages == {"js", "ts"}
        assert result.scope.get("js") == "/app"
        assert result.scope.get("ts") == "/app"

    def test_no_markers_returns_unknown(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (1, (b"", b""))

        result = detect_languages(mock_container, "/empty_dir")
        assert result.languages == set()
        assert result.reason is not None
        assert "language=" in result.reason

    def test_exclude_dirs_not_accidentally_detected(self) -> None:
        """node_modules/.venv etc are excluded by -maxdepth 1 and path scope."""
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))

        result = detect_languages(mock_container, "/app/node_modules")
        assert result.languages == set()

    def test_find_cmd_includes_all_marker_patterns(self) -> None:
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import _DETECTION_MARKERS, detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/go.mod\n", b""))

        detect_languages(mock_container, "/app")
        call_args = mock_container.exec_run.call_args[0][0]
        find_cmd = call_args[2]
        # All patterns should be in the find command
        for pattern, _ in _DETECTION_MARKERS:
            assert pattern in find_cmd, f"Pattern {pattern!r} missing from find command"
        assert " -maxdepth 1 " in find_cmd
        assert " -o " in find_cmd

    def test_scope_path_for_subdir_polyglot(self) -> None:
        """Polyglot project where markers are in different subdirectories."""
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (
            0,
            (b"/app/backend/pyproject.toml\n/app/frontend/package.json\n", b""),
        )

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"python", "js"}
        # Scope should point to each marker's directory
        assert result.scope.get("python") == "/app/backend"
        assert result.scope.get("js") == "/app/frontend"

    def test_same_language_multiple_markers_single_scope(self) -> None:
        """Multiple markers for the same language should not duplicate."""
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (
            0,
            (b"/app/pyproject.toml\n/app/setup.py\n/app/tox.ini\n", b""),
        )

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"python"}
        # The last marker's scope wins (dict key dedup)
        assert result.scope.get("python") == "/app"

    def test_ts_file_detects_tsconfig_in_parent(self) -> None:
        """.ts file with tsconfig.json in a parent directory should detect ts with parent scope."""
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        def exec_side_effect(cmd, **kwargs):
            test_path = cmd[-1] if len(cmd) > 1 else ""
            if "tsconfig.json" in test_path and "/app" in test_path and "/app/src" not in test_path:
                return (0, (b"found", b""))
            return (1, (b"", b""))
        mock_container.exec_run.side_effect = exec_side_effect

        # exec_run is called twice: once for tsconfig upward search, once for directory
        # Since path is a .ts file, only upward search happens
        result = detect_languages(mock_container, "/app/src/foo.ts")
        assert result.languages == {"ts"}
        assert "/app" in result.scope.get("ts", "")

    def test_ts_file_without_tsconfig(self) -> None:
        """.ts file without any tsconfig.json should still detect ts with file path scope."""
        from unittest.mock import MagicMock

        from src.sunaba.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (1, (b"", b""))

        result = detect_languages(mock_container, "/app/src/standalone.ts")
        assert result.languages == {"ts"}
        # Scope should be the file path itself when no tsconfig found
        assert result.scope.get("ts") == "/app/src/standalone.ts"


# ===================================================================
# transform_file_in_container tests
# ===================================================================

