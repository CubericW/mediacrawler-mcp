"""Minimal example: call the crawl pipeline directly (bypassing MCP transport).

This shows the shape of a search_posts call. It WILL actually run MediaCrawler,
so only run it after you have:
  1. cloned MediaCrawler and set MEDIACRAWLER_HOME,
  2. installed its deps (uv sync),
  3. logged in once (QR scan) or enabled CDP mode.

Usage:
    export MEDIACRAWLER_HOME=/path/to/MediaCrawler
    python examples/quickstart.py
"""

import json

from mediacrawler_mcp.server import search_posts


def main() -> None:
    result = search_posts(
        platform="xhs",
        keywords="城市露营",
        max_count=5,
        with_comments=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])


if __name__ == "__main__":
    main()
