"""Read the state of an optional cross-process risk guard running inside MediaCrawler.

The guard inside MediaCrawler paces every request and opens a circuit breaker
on captcha / risk-control responses. This module only reads the guard's state
file, so the MCP server can refuse a crawl while the breaker is open instead of
launching MediaCrawler (and asking the user to allow a browser connection) just
to fail.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

HOUR, DAY = 3600, 86400

# Log/traceback markers MediaCrawler prints when the guard stops a run.
RISK_MARKERS = ("RiskControlTripped", "BudgetExhausted", "LoginRequired", "[RiskGuard]")
# Logged by a patched MediaCrawler (if present) when xhs rejects the browser session
# and a detail run goes on without the account.
ANON_ONLY_MARKER = "anonymous note pages only"
# Logged by MediaCrawler when it cannot attach to the debug Chrome and opens its own browser.
# Platforms word it as "falling back to" or "fallback to".
CDP_FALLBACK_MARKER = "back to standard mode"


def read_state(guard_dir: Path, platform: str) -> dict[str, Any]:
    """Summarise the guard state for ``platform`` (empty summary if none yet)."""
    path = Path(guard_dir) / f"{platform}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"platform": platform, "known": False, "tripped": False}
    now = time.time()
    until = float(state.get("tripped_until") or 0)
    tripped = until > now
    requests = state.get("requests") or {}
    return {
        "platform": platform,
        "known": True,
        "tripped": tripped,
        "tripped_until": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until)) if tripped else None,
        "retry_after_seconds": int(until - now) if tripped else 0,
        "trip_reason": state.get("trip_reason") if tripped else None,
        "requests_last_hour": {k: sum(1 for t in v if t > now - HOUR) for k, v in requests.items()},
        "requests_last_24h": {k: sum(1 for t in v if t > now - DAY) for k, v in requests.items()},
        "recent_trips": (state.get("history") or [])[-5:],
    }


def blocked_payload(status: dict[str, Any]) -> dict[str, Any] | None:
    """Error payload to return instead of crawling while the breaker is open."""
    if not status.get("tripped"):
        return None
    minutes = max(1, round(status["retry_after_seconds"] / 60))
    return {
        "ok": False,
        "error": "risk_guard_tripped",
        "details": [
            f"{status['platform']} showed a risk-control signal ({status['trip_reason']}); "
            f"crawling is paused until {status['tripped_until']} (about {minutes} min).",
            "Do not retry before then. If a captcha is showing in the browser, the user should "
            "solve it there; the pause also ends early if the user resets the risk guard.",
        ],
        "retry_after_seconds": status["retry_after_seconds"],
        "risk_status": status,
    }


def risk_warnings(stdout_tail: str, stderr_tail: str) -> list[str]:
    """Warnings for a finished run that was stopped by the guard or ran without the account."""
    text = f"{stdout_tail}\n{stderr_tail}"
    warnings = []
    if ANON_ONLY_MARKER in text:
        warnings.append(
            "Xiaohongshu did not accept the browser session for API calls, so this run read "
            "public note pages only: no comments and no logged-in fallback."
        )
    fallback = next((ln.strip()[-300:] for ln in text.splitlines() if CDP_FALLBACK_MARKER in ln), None)
    if fallback:
        warnings.append(
            "MediaCrawler could not attach to the debug Chrome and opened its own browser, "
            "without that Chrome's login: " + fallback
        )
    lines = [ln.strip() for ln in text.splitlines() if any(m in ln for m in RISK_MARKERS)]
    if lines:
        last = lines[-1][-300:]
        if "LoginRequired" in last:
            warnings.append("MediaCrawler stopped because the login session was not accepted: " + last)
        else:
            warnings.append("MediaCrawler stopped early because of its risk guard: " + last)
    return warnings
