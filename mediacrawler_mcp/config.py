"""Configuration for the MediaCrawler MCP server.

All settings are read from environment variables so the server can be
configured entirely through the MCP client's `env` block. See .env.example.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Supported platform codes -> human-readable names.
# Source of truth: MediaCrawler CrawlerFactory.CRAWLERS in main.py.
SUPPORTED_PLATFORMS: dict[str, str] = {
    "xhs": "小红书 (Xiaohongshu / RedNote)",
    "dy": "抖音 (Douyin)",
    "ks": "快手 (Kuaishou)",
    "bili": "B站 (Bilibili)",
    "wb": "微博 (Weibo)",
    "tieba": "百度贴吧 (Tieba)",
    "zhihu": "知乎 (Zhihu)",
}

# Crawl types accepted by MediaCrawler's --type argument.
SUPPORTED_TYPES = ("search", "detail", "creator")

# Login types accepted by MediaCrawler's --lt argument.
SUPPORTED_LOGIN_TYPES = ("qrcode", "phone", "cookie")

# Storage options accepted by --save_data_option. We force json for easy parsing.
SUPPORTED_SAVE_OPTIONS = ("json", "csv", "sqlite", "db")


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _get_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    """Runtime settings, populated from environment variables."""

    # Absolute path to the cloned MediaCrawler repository.
    mediacrawler_home: Path = field(
        default_factory=lambda: Path(
            os.environ.get("MEDIACRAWLER_HOME", "").strip()
            or str(Path.home() / "MediaCrawler")
        ).expanduser()
    )

    # Command used to invoke MediaCrawler. Default assumes `uv` is installed.
    # Override with e.g. "python" if you use a plain venv.
    python_runner: str = field(
        default_factory=lambda: os.environ.get("MEDIACRAWLER_RUNNER", "uv").strip()
    )

    # Login type passed to --lt.
    login_type: str = field(
        default_factory=lambda: os.environ.get("MEDIACRAWLER_LOGIN_TYPE", "qrcode").strip()
    )

    # Storage backend for MediaCrawler output. json is strongly recommended.
    save_data_option: str = field(
        default_factory=lambda: os.environ.get("MEDIACRAWLER_SAVE_OPTION", "json").strip()
    )

    # ---- Anti-ban / safety guardrails -------------------------------------
    # Hard ceiling on how many items a single call may request.
    max_count_hard_limit: int = field(default_factory=lambda: _get_int("MEDIACRAWLER_MAX_HARD_LIMIT", 50))
    # Default number of items when the caller does not specify.
    default_max_count: int = field(default_factory=lambda: _get_int("MEDIACRAWLER_DEFAULT_MAX", 20))
    # Random delay window (seconds) between crawl requests, written into config.
    delay_min_seconds: float = field(default_factory=lambda: _get_float("MEDIACRAWLER_DELAY_MIN", 2.0))
    delay_max_seconds: float = field(default_factory=lambda: _get_float("MEDIACRAWLER_DELAY_MAX", 5.0))
    # Local rate limiter: max tool invocations allowed per rolling window.
    rate_limit_calls: int = field(default_factory=lambda: _get_int("MEDIACRAWLER_RATE_CALLS", 10))
    rate_limit_window_seconds: int = field(default_factory=lambda: _get_int("MEDIACRAWLER_RATE_WINDOW", 600))

    # Whether to enable comment scraping by default. Off: every note's comments are an extra
    # logged-in request, the scarcest budget (as counted by the optional risk guard).
    enable_comments: bool = field(default_factory=lambda: _get_bool("MEDIACRAWLER_ENABLE_COMMENTS", False))
    # Use CDP mode (connect to user's existing logged-in Chrome) to reduce risk.
    enable_cdp: bool = field(default_factory=lambda: _get_bool("MEDIACRAWLER_ENABLE_CDP", True))
    # Optional proxy pool toggle (passed through to MediaCrawler config).
    enable_proxy: bool = field(default_factory=lambda: _get_bool("MEDIACRAWLER_ENABLE_PROXY", False))

    # subprocess timeout (seconds) for a single crawl run. MediaCrawler's risk guard now
    # paces requests 10-30 s apart, so a 20-note crawl takes several minutes.
    run_timeout_seconds: int = field(default_factory=lambda: _get_int("MEDIACRAWLER_RUN_TIMEOUT", 1800))
    # How long a crawl may wait for the ones ahead of it. Crawls run one at a time on this
    # machine, and each can take up to run_timeout_seconds.
    queue_timeout_seconds: int = field(default_factory=lambda: _get_int("MEDIACRAWLER_QUEUE_TIMEOUT", 3600))

    def data_dir(self) -> Path:
        return self.mediacrawler_home / "data"

    def guard_dir(self) -> Path:
        """Where MediaCrawler's cross-process risk guard keeps its state."""
        return Path(os.environ.get("MEDIACRAWLER_GUARD_DIR") or self.mediacrawler_home / ".risk_guard")

    def validate(self) -> list[str]:
        """Return a list of human-readable configuration problems (empty = ok)."""
        problems: list[str] = []
        if not self.mediacrawler_home.exists():
            problems.append(
                f"MEDIACRAWLER_HOME does not exist: {self.mediacrawler_home}. "
                "Clone https://github.com/NanmiCoder/MediaCrawler and point "
                "MEDIACRAWLER_HOME at it."
            )
        elif not (self.mediacrawler_home / "main.py").exists():
            problems.append(
                f"main.py not found under MEDIACRAWLER_HOME ({self.mediacrawler_home}). "
                "Is this really the MediaCrawler repo root?"
            )
        if self.save_data_option not in SUPPORTED_SAVE_OPTIONS:
            problems.append(
                f"MEDIACRAWLER_SAVE_OPTION={self.save_data_option!r} invalid; "
                f"choose one of {SUPPORTED_SAVE_OPTIONS}."
            )
        if self.login_type not in SUPPORTED_LOGIN_TYPES:
            problems.append(
                f"MEDIACRAWLER_LOGIN_TYPE={self.login_type!r} invalid; "
                f"choose one of {SUPPORTED_LOGIN_TYPES}."
            )
        return problems


# Singleton-style accessor.
_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings
