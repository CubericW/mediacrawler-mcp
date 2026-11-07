"""Tests for config patching and the rate limiter (no network / no CLI)."""

from mediacrawler_mcp.config import Settings
from mediacrawler_mcp.rate_limiter import RateLimiter
from mediacrawler_mcp.runner import _set_py_assignment, build_config_patch


def test_set_py_assignment_replaces_existing():
    src = "KEYWORDS = 'old'\nOTHER = 1\n"
    out = _set_py_assignment(src, "KEYWORDS", "'new'")
    assert "KEYWORDS = 'new'" in out
    assert "OTHER = 1" in out
    assert "old" not in out


def test_set_py_assignment_appends_missing():
    src = "OTHER = 1\n"
    out = _set_py_assignment(src, "KEYWORDS", "'x'")
    assert "KEYWORDS = 'x'" in out
    assert "OTHER = 1" in out


def test_build_config_patch_search():
    s = Settings()
    patch = build_config_patch(
        s,
        keywords="python,爬虫",
        specified_ids=None,
        creator_ids=None,
        max_count=15,
        with_comments=True,
    )
    assert patch["CRAWLER_MAX_NOTES_COUNT"] == "15"
    assert patch["ENABLE_GET_COMMENTS"] == "True"
    assert "python,爬虫" in patch["KEYWORDS"]


def test_rate_limiter_blocks_after_max():
    rl = RateLimiter(max_calls=2, window_seconds=100)
    assert rl.acquire()[0] is True
    assert rl.acquire()[0] is True
    allowed, retry_after = rl.acquire()
    assert allowed is False
    assert retry_after > 0
