"""MediaCrawler MCP server entry point.

Exposes MediaCrawler as MCP tools. Built on the official `mcp` Python SDK
(FastMCP). Run with:  python -m mediacrawler_mcp.server

Tools:
    search_posts        keyword search on a platform
    get_post_detail     fetch specific posts by id
    get_creator_posts   fetch a creator's posts
    login_status        check which platforms have a valid cached session
    risk_status         request budget used and whether crawling is paused
    list_platforms      capability matrix

SAFETY: personal / low-frequency / research use only. Respect platform ToS.
Do not use for commercial or large-scale scraping, and do not collect private
personal data.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import memo23
from .config import (
    SUPPORTED_LOGIN_TYPES,
    SUPPORTED_PLATFORMS,
    get_settings,
)
from .parser import collect_results, count_records
from .rate_limiter import RateLimiter
from .risk_state import blocked_payload, read_state, risk_warnings
from .runner import MediaCrawlerError, QueueTimeout, crawl_lock, probe_help, run_crawl

mcp = FastMCP("mediacrawler-mcp")

_settings = get_settings()
_rate_limiter = RateLimiter(
    max_calls=_settings.rate_limit_calls,
    window_seconds=_settings.rate_limit_window_seconds,
)


def _validate_platform(platform: str) -> str | None:
    if platform not in SUPPORTED_PLATFORMS:
        return (
            f"Unsupported platform {platform!r}. "
            f"Supported: {', '.join(SUPPORTED_PLATFORMS)}."
        )
    return None


def _clamp_max_count(max_count: int | None) -> tuple[int, list[str]]:
    warnings: list[str] = []
    if max_count is None:
        return _settings.default_max_count, warnings
    if max_count > _settings.max_count_hard_limit:
        warnings.append(
            f"max_count {max_count} exceeds hard limit "
            f"{_settings.max_count_hard_limit}; clamped. This limit protects "
            "your account from risk-control triggers."
        )
        return _settings.max_count_hard_limit, warnings
    if max_count < 1:
        return 1, warnings
    return max_count, warnings


def _guard(platform: str) -> dict[str, Any] | None:
    """Return an error payload if configuration, the risk guard or rate limit blocks the call."""
    if platform != "xhs":
        problems = _settings.validate()
        if problems:
            return {"ok": False, "error": "configuration_error", "details": problems}
        # MediaCrawler's cross-process risk guard: refuse while its breaker is open.
        blocked = blocked_payload(read_state(_settings.guard_dir(), platform))
        if blocked:
            return blocked
    allowed, retry_after = _rate_limiter.acquire()
    if not allowed:
        return {
            "ok": False,
            "error": "rate_limited",
            "details": [
                f"Local rate limit reached ({_settings.rate_limit_calls} calls / "
                f"{_settings.rate_limit_window_seconds}s). Retry in "
                f"{retry_after:.0f}s. This guard reduces ban risk."
            ],
        }
    return None


def _run_and_collect(
    *,
    platform: str,
    crawl_type: str,
    keywords: str | None = None,
    specified_ids: list[str] | None = None,
    creator_ids: list[str] | None = None,
    max_count: int = 20,
    with_comments: bool = True,
    sort: str | None = None,
) -> dict[str, Any]:
    if platform == "xhs":
        if crawl_type == "search":
            values = (keywords or "").replace("，", ",").split(",")
        elif crawl_type == "detail":
            values = specified_ids or []
        else:
            values = creator_ids or []
        return memo23.collect(crawl_type, values, max_count, with_comments, _settings.data_dir(), sort=sort)
    try:
        with crawl_lock(_settings, f"{platform} {crawl_type}") as slot:
            # The crawl ahead may have tripped the breaker during the wait.
            blocked = blocked_payload(read_state(_settings.guard_dir(), platform))
            if blocked:
                return blocked
            earlier_counts = count_records(_settings.data_dir(), platform, _settings.save_data_option)
            run_meta = run_crawl(
                _settings,
                slot=slot,
                platform=platform,
                crawl_type=crawl_type,
                keywords=keywords,
                specified_ids=specified_ids,
                creator_ids=creator_ids,
                max_count=max_count,
                with_comments=with_comments,
            )
            # Still inside the lock: the next crawl may rewrite these same per-day files.
            results = collect_results(
                data_dir=_settings.data_dir(),
                platform=platform,
                since_ts=run_meta["started_ts"],
                save_option=_settings.save_data_option,
                with_comments=with_comments,
                earlier_counts=earlier_counts,
            )
    except QueueTimeout as exc:
        return {"ok": False, "error": "queue_timeout", "details": [str(exc)]}
    except MediaCrawlerError as exc:
        return {"ok": False, "error": "run_error", "details": [str(exc)]}

    notices = "\n".join(run_meta.get("notices", []))
    warnings = risk_warnings(f"{notices}\n{run_meta['stdout_tail']}", run_meta["stderr_tail"])
    warnings += list(results.get("warnings", []))

    failure = None
    if run_meta["timed_out"]:
        failure = f"Crawl timed out after {_settings.run_timeout_seconds}s."
    elif run_meta["returncode"] != 0:
        failure = (
            f"MediaCrawler exited with code {run_meta['returncode']}. "
            f"stderr tail: {run_meta['stderr_tail'][-400:]}"
        )
    # A run that stopped midway (e.g. by the risk guard) keeps what it saved. One that saved
    # nothing must not look like a search with no results.
    partial = bool(failure and results["count"])
    if partial:
        warnings.append(f"{failure} Results may be partial.")

    result: dict[str, Any] = {
        "ok": not failure or partial,
        "platform": platform,
        "platform_name": SUPPORTED_PLATFORMS.get(platform, platform),
        "crawl_type": crawl_type,
        "count": results["count"],
        "items": results["items"],
        "raw_files": results["raw_files"],
        "command": run_meta["command"],
        "queue_wait_seconds": round(slot.waited_seconds),
        "warnings": warnings,
        "risk_status": read_state(_settings.guard_dir(), platform),
    }
    if partial:
        result["partial"] = True
    elif failure:
        result["error"] = "crawl_failed"
        result["details"] = [failure]
    return result


@mcp.tool()
def list_platforms() -> dict[str, Any]:
    """List supported platforms and the capability matrix.

    Returns platform codes, human-readable names, and which crawl types each
    supports. All platforms support search / detail / creator per MediaCrawler.
    """
    return {
        "ok": True,
        "platforms": [
            {
                "code": code,
                "name": name,
                "backend": memo23.ACTOR if code == "xhs" else "mediacrawler-cli",
                "supports": ["search", "detail", "creator"],
            }
            for code, name in SUPPORTED_PLATFORMS.items()
        ],
        "notes": [
            "Personal, low-frequency, research use only.",
            "For browser-backed platforms, CDP (reuse your logged-in Chrome) is recommended to "
            "reduce risk-control detection.",
        ],
    }


@mcp.tool()
def search_posts(
    platform: str,
    keywords: str,
    max_count: int | None = None,
    with_comments: bool | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    """Keyword-search a platform and return matching public posts (+comments).

    Args:
        platform: one of xhs, dy, ks, bili, wb, tieba, zhihu.
        keywords: search query. Multiple keywords may be comma-separated.
        max_count: max posts to fetch (default from config, hard-capped).
        with_comments: include comments (default from config).
        sort: optional sort hint (platform-dependent; may be ignored).

    Returns a dict: {ok, platform, count, items:[{id, author, content,
    like_count, comment_count, publish_time, url, comments?}], raw_files,
    warnings}. items holds only what this call crawled; raw_files are MediaCrawler's
    per-day files, which also hold every earlier crawl of that day.

    Browser crawls run one at a time across every project on this machine, so this call first
    waits for any crawl ahead of it (queue_wait_seconds says how long). If that wait
    outlasts MEDIACRAWLER_QUEUE_TIMEOUT, ok is false and error is "queue_timeout".

    If the crawl fails and saves nothing, ok is false, error is "crawl_failed" and
    details gives the reason. If it stops after saving some results, ok stays true
    with partial: true and the reason in warnings. ok: true with count 0 means the
    crawl finished without finding anything; check warnings.
    """
    perr = _validate_platform(platform)
    if perr:
        return {"ok": False, "error": "bad_platform", "details": [perr]}
    guard = _guard(platform)
    if guard:
        return guard

    count, warnings = _clamp_max_count(max_count)
    wc = _settings.enable_comments if with_comments is None else with_comments
    result = _run_and_collect(
        platform=platform,
        crawl_type="search",
        keywords=keywords,
        sort=sort,
        max_count=count,
        with_comments=wc,
    )
    if result.get("ok"):
        result["query"] = keywords
        result["warnings"] = warnings + result.get("warnings", [])
    return result


@mcp.tool()
def get_post_detail(
    platform: str,
    post_ids: list[str],
    with_comments: bool | None = None,
) -> dict[str, Any]:
    """Fetch specific posts by their platform id(s), with optional comments.

    Args:
        platform: one of xhs, dy, ks, bili, wb, tieba, zhihu.
        post_ids: platform-native IDs. XHS accepts note IDs, ordinary note URLs and xhslink.com links.
        with_comments: include comments (default from config).

    Browser platforms queue behind other local browser crawls. XHS uses Apify.
    """
    perr = _validate_platform(platform)
    if perr:
        return {"ok": False, "error": "bad_platform", "details": [perr]}
    if not post_ids:
        return {"ok": False, "error": "bad_args", "details": ["post_ids is empty."]}
    guard = _guard(platform)
    if guard:
        return guard

    wc = _settings.enable_comments if with_comments is None else with_comments
    result = _run_and_collect(
        platform=platform,
        crawl_type="detail",
        specified_ids=list(post_ids),
        max_count=max(len(post_ids), _settings.default_max_count),
        with_comments=wc,
    )
    if result.get("ok"):
        result["requested_ids"] = list(post_ids)
    return result


@mcp.tool()
def get_creator_posts(
    platform: str,
    creator_ids: list[str],
    max_count: int | None = None,
) -> dict[str, Any]:
    """Fetch posts from one or more creators' home pages.

    Args:
        platform: one of xhs, dy, ks, bili, wb, tieba, zhihu.
        creator_ids: list of creator ids (or profile URLs for tieba/zhihu).
        max_count: max posts per creator (default from config, hard-capped).

    Browser platforms queue behind other local browser crawls. XHS uses Apify.
    """
    perr = _validate_platform(platform)
    if perr:
        return {"ok": False, "error": "bad_platform", "details": [perr]}
    if not creator_ids:
        return {"ok": False, "error": "bad_args", "details": ["creator_ids is empty."]}
    guard = _guard(platform)
    if guard:
        return guard

    count, warnings = _clamp_max_count(max_count)
    result = _run_and_collect(
        platform=platform,
        crawl_type="creator",
        creator_ids=list(creator_ids),
        max_count=count,
        with_comments=False,
    )
    if result.get("ok"):
        result["requested_creators"] = list(creator_ids)
        result["warnings"] = warnings + result.get("warnings", [])
    return result


@mcp.tool()
def risk_status(platform: str = "xhs") -> dict[str, Any]:
    """Show MediaCrawler's risk guard for a platform: request budget used and whether
    crawling is paused after a captcha / risk-control signal. Check this before
    planning a large crawl; the guard is shared by every process using the same MediaCrawler checkout.
    """
    perr = _validate_platform(platform)
    if perr:
        return {"ok": False, "error": "bad_platform", "details": [perr]}
    if platform == "xhs":
        return {"ok": True, "platform": "xhs", **memo23.status(),
                "notes": ["Apify handles XHS requests. The local browser risk guard applies to CLI crawls."]}
    status = read_state(_settings.guard_dir(), platform)
    return {
        "ok": True,
        **status,
        "notes": [
            "Budgets and pacing are set by the risk guard installed in MediaCrawler (optional; see README).",
            "Only the user should reset a pause, using that guard's own reset command.",
        ],
    }


@mcp.tool()
def login_status() -> dict[str, Any]:
    """Report configuration health and login guidance.

    MediaCrawler caches login sessions in a browser context. This tool cannot
    silently re-login; instead it validates configuration and tells you when a
    manual QR-code scan is likely required.
    """
    problems = _settings.validate()
    browser_data = _settings.mediacrawler_home / "browser_data"
    cache_exists = browser_data.exists() and any(browser_data.iterdir()) if browser_data.exists() else False
    return {
        "ok": not problems,
        "config_problems": problems,
        "login_type": _settings.login_type,
        "supported_login_types": list(SUPPORTED_LOGIN_TYPES),
        "cdp_mode": _settings.enable_cdp,
        "cached_session_detected": cache_exists,
        "xhs": memo23.status(),
        "risk_status": {**read_state(_settings.guard_dir(), "xhs"), "scope": "local-browser-only"},
        "guidance": [
            "If a crawl returns no items and cached_session_detected is false, "
            "run a crawl once interactively so you can scan the QR code, or "
            "enable CDP mode and log in via your own Chrome.",
            "Never share your cookies/session files.",
        ],
    }


@mcp.tool()
def diagnostics() -> dict[str, Any]:
    """Return MediaCrawler `--help` output and effective settings for debugging.

    ok is false, with error "startup_failed", when MediaCrawler cannot start.
    """
    started, help_text = probe_help(_settings)
    result: dict[str, Any] = {
        "ok": started,
        "mediacrawler_home": str(_settings.mediacrawler_home),
        "runner": _settings.python_runner,
        "save_data_option": _settings.save_data_option,
        "help": help_text,
    }
    if not started:
        result["error"] = "startup_failed"
    return result


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
