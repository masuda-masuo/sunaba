"""Mock-free tests for github_api.py pure functions.

Tests ``_link_next_url`` (Link-header parsing) and ``_fetch_all_pages``
(pagination-loop extraction) with zero mocks -- no Docker, no network.
"""

from __future__ import annotations

from sunaba.tools.github_api import _fetch_all_pages, _link_next_url

# ---------------------------------------------------------------------------
# _link_next_url -- pure function, no mock needed
# ---------------------------------------------------------------------------


class TestLinkNextUrl:
    def test_none_returns_none(self) -> None:
        assert _link_next_url(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert _link_next_url("") is None

    def test_no_rel_next_returns_none(self) -> None:
        header = '<https://api.github.com/resource?page=1>; rel="prev"'
        assert _link_next_url(header) is None

    def test_simple_next_link(self) -> None:
        header = '<https://api.github.com/resource?page=2>; rel="next"'
        assert _link_next_url(header) == "https://api.github.com/resource?page=2"

    def test_multiple_links_extracts_next(self) -> None:
        header = (
            '<https://api.github.com/resource?page=1>; rel="prev",'
            '<https://api.github.com/resource?page=3>; rel="next",'
            '<https://api.github.com/resource?page=5>; rel="last"'
        )
        assert _link_next_url(header) == "https://api.github.com/resource?page=3"

    def test_next_with_extra_whitespace(self) -> None:
        header = '<https://api.github.com/resource?page=2>  ;  rel="next"'
        assert _link_next_url(header) == "https://api.github.com/resource?page=2"

    def test_next_with_url_params(self) -> None:
        header = (
            '<https://api.github.com/resource?per_page=50&page=2>; rel="next"'
        )
        assert (
            _link_next_url(header)
            == "https://api.github.com/resource?per_page=50&page=2"
        )

    def test_next_url_with_scheme(self) -> None:
        header = '<http://api.github.com/resource?page=2>; rel="next"'
        assert _link_next_url(header) == "http://api.github.com/resource?page=2"

    def test_missing_angle_brackets_returns_none(self) -> None:
        header = 'https://api.github.com/resource?page=2; rel="next"'
        assert _link_next_url(header) is None


# ---------------------------------------------------------------------------
# _fetch_all_pages -- injectable fetch function, no mock needed
# ---------------------------------------------------------------------------


class TestFetchAllPages:
    def test_single_page(self) -> None:
        """A single page with no next link returns that page."""
        def fetch_page(path: str) -> tuple[list[dict[str, str]], str | None]:
            return [{"item": "a"}, {"item": "b"}], None

        result = _fetch_all_pages(fetch_page, "/initial")
        assert result == [{"item": "a"}, {"item": "b"}]

    def test_two_pages(self) -> None:
        """Two pages linked via next Link header."""
        def fetch_page(path: str) -> tuple[list[dict[str, str]], str | None]:
            if path == "/first":
                return [{"page": 1, "item": "a"}], '<http://x/second>; rel="next"'
            # The next_path passed by _fetch_all_pages is the URL extracted
            # from the Link header, i.e. "http://x/second".
            return [{"page": 2, "item": "b"}], None

        result = _fetch_all_pages(fetch_page, "/first")
        assert result == [
            {"page": 1, "item": "a"},
            {"page": 2, "item": "b"},
        ]

    def test_three_pages(self) -> None:
        """Three pages, order preserved."""
        def fetch_page(path: str) -> tuple[list[dict[str, str]], str | None]:
            if path == "/p1":
                return [{"n": 1}], '<http://x/p2>; rel="next"'
            elif path == "http://x/p2":
                return [{"n": 2}], '<http://x/p3>; rel="next"'
            return [{"n": 3}], None

    def test_no_link_header_stops_after_first_page(self) -> None:
        """No Link header: fetch once and stop."""
        call_count = 0

        def fetch_page(path: str) -> tuple[list[dict[str, str]], str | None]:
            nonlocal call_count
            call_count += 1
            return [{"x": call_count}], None

        result = _fetch_all_pages(fetch_page, "/start")
        assert result == [{"x": 1}]
        assert call_count == 1

    def test_empty_first_page(self) -> None:
        """First page is empty but has a next link."""
        def fetch_page(path: str) -> tuple[list[dict[str, str]], str | None]:
            if path == "/first":
                return [], '<http://x/second>; rel="next"'
            return [{"done": True}], None

    def test_initial_path_is_full_url(self) -> None:
        """Initial path can be a full URL, not just a path."""
        def fetch_page(path: str) -> tuple[list[dict[str, str]], str | None]:
            return [{"from": path}], None

        result = _fetch_all_pages(fetch_page, "https://api.github.com/foo")
        assert result == [{"from": "https://api.github.com/foo"}]
