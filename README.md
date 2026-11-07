# MediaCrawler MCP Server

An **MCP (Model Context Protocol) server** that wraps the open-source
[MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) project so that
Claude Code / Codex / Cursor and any other MCP-compatible agent can collect
**public** data from Chinese social platforms through simple tool calls:

> 小红书 (Xiaohongshu) · 微博 (Weibo) · 抖音 (Douyin) · B站 (Bilibili) ·
> 快手 (Kuaishou) · 贴吧 (Tieba) · 知乎 (Zhihu)

Xiaohongshu uses Apify. Other platforms use MediaCrawler as a **black-box CLI**: it
patches config just-in-time, runs the crawler, reads the `data/` output, and
returns normalised JSON. Anti-ban guardrails are built in (rate limiting, a
machine-wide crawl queue, and optional integration with a shared
[risk guard](#optional-risk-guard-integration)). It works with an unmodified
upstream MediaCrawler checkout.

> ⚠️ **For personal, low-frequency, research use only.** MediaCrawler is
> released under a non-commercial learning license and explicitly forbids
> commercial use and large-scale scraping. Respect each platform's Terms of
> Service and robots.txt, keep request rates low, use a secondary account, and
> never collect private personal data.

---

## Architecture

```
agent (Claude Code / Codex)
        │  MCP tool call
        ▼
mediacrawler_mcp.server   ← this package (guardrails + parsing)
        │  patch config/base_config.py  +  subprocess
        ▼
MediaCrawler CLI (uv run main.py --platform ... --type ...)
        │  writes
        ▼
    <MEDIACRAWLER_HOME>/data/*.json   → parsed → normalised JSON back to agent
```

## Xiaohongshu via Apify

The three content tools route `platform="xhs"` to `memo23/xiaohongshu-all-in-one-scraper`.
Set `APIFY_TOKEN` in the MCP environment or save the token alone in
`~/.config/mediacrawler/apify-token.txt` (mode 0600). The file is read on each call.
`login_status().xhs` reports whether a token is present. This backend runs without CDP.
The default per-run charge limit is USD 2, configurable via `MEDIACRAWLER_APIFY_MAX_USD`.
With comments enabled it requests up to 10 root comments per note and 5 replies per root.
Read `replies_incomplete` on root comments for identified gaps. Search returns the text from
the search row. Detail opens the note. Raw dataset rows are saved under `data/xhs/apify/<run_id>/`.

## Exposed MCP tools

| Tool | Purpose | Key args |
|---|---|---|
| `search_posts` | Keyword search on a platform | `platform`, `keywords`, `max_count`, `with_comments` |
| `get_post_detail` | Fetch specific posts by id (for xhs: note IDs, URLs or xhslink.com links) | `platform`, `post_ids[]`, `with_comments` |
| `get_creator_posts` | Fetch a creator's posts | `platform`, `creator_ids[]`, `max_count` |
| `login_status` | Check config + cached login session (+ xhs risk guard) | – |
| `risk_status` | Request budget used and whether crawling is paused after a captcha | `platform` |
| `list_platforms` | Capability matrix | – |
| `diagnostics` | `main.py --help` + effective settings | – |

All content tools return:

```json
{
  "ok": true,
  "platform": "xhs",
  "count": 12,
  "items": [
    {
      "id": "...", "author": "...", "author_id": "...",
      "content": "...", "like_count": 42, "comment_count": 3,
      "publish_time": "...", "url": "...",
      "comments": [ { "...": "raw comment record" } ],
      "raw": { "...": "original MediaCrawler record" }
    }
  ],
  "raw_files": ["/path/to/data/xhs/....json"],
  "warnings": []
}
```

How to read `ok` after a crawl:

| Result | Meaning |
|---|---|
| `ok: false`, `error: "crawl_failed"` | The crawl exited with an error or timed out and saved nothing. The reason is in `details`. |
| `ok: true`, `partial: true` | The crawl stopped the same way but saved some results, for example when the risk guard ended it midway. The items are returned and the reason is in `warnings`. |
| `ok: true`, `count: 0` | The crawl finished without finding anything. Check `warnings`, since an expired login can look the same. |

`diagnostics` returns `ok: false` and `error: "startup_failed"` when
`main.py --help` fails. `login_status` only checks configuration, so its `ok`
does not show whether MediaCrawler can start.

---

## Prerequisites

1. **Clone MediaCrawler** and install its dependencies (it requires Python 3.11,
   `uv`, and Node.js ≥ 16 for some platforms):

   ```bash
   git clone https://github.com/NanmiCoder/MediaCrawler
   cd MediaCrawler
   uv sync
   # optional (only for standard Playwright mode):
   uv run playwright install
   ```

2. **This server** needs the official `mcp` SDK:

   ```bash
   pip install --user "mcp>=1.2.0,<2"
   # or, from this folder:
   pip install --user -e .
   ```

---

## CDP mode (strongly recommended — lowers ban risk)

MediaCrawler's default **CDP mode** connects to *your own* already-logged-in
Chrome, reusing its cookies and session, which greatly reduces risk-control
detection:

1. Install the latest Chrome (≥ 144).
2. Visit `chrome://inspect/#remote-debugging` and enable
   **"Allow remote debugging for this browser instance"** (should report
   `Server running at: 127.0.0.1:9222`).
3. Keep `MEDIACRAWLER_ENABLE_CDP=true` (default).
4. On first crawl, Chrome shows a confirmation dialog — click "accept" within 60s.

If you prefer standard mode, set `MEDIACRAWLER_ENABLE_CDP=false`; you will then
log in via QR code on first run.

---

## Configuration

All settings come from environment variables (see [`.env.example`](.env.example)).
The only required one is `MEDIACRAWLER_HOME`.

| Var | Default | Meaning |
|---|---|---|
| `MEDIACRAWLER_HOME` | `~/MediaCrawler` | Path to the cloned repo (must contain `main.py`) |
| `MEDIACRAWLER_RUNNER` | `uv` | `uv` → `uv run main.py`; or `python` |
| `MEDIACRAWLER_LOGIN_TYPE` | `qrcode` | `--lt` value: qrcode / phone / cookie |
| `MEDIACRAWLER_SAVE_OPTION` | `json` | Output format (json recommended) |
| `MEDIACRAWLER_DEFAULT_MAX` | `20` | Items per call when unspecified |
| `MEDIACRAWLER_MAX_HARD_LIMIT` | `50` | Absolute cap; larger requests clamped |
| `MEDIACRAWLER_DELAY_MIN/MAX` | `2` / `5` | Random delay window (s) |
| `MEDIACRAWLER_RATE_CALLS` | `10` | Max tool calls per window |
| `MEDIACRAWLER_RATE_WINDOW` | `600` | Rate-limit window (s) |
| `MEDIACRAWLER_ENABLE_COMMENTS` | `false` | Collect comments by default (each note's comments cost logged-in requests) |
| `MEDIACRAWLER_ENABLE_CDP` | `true` | Reuse your logged-in Chrome |
| `MEDIACRAWLER_ENABLE_PROXY` | `false` | Enable MediaCrawler's proxy pool |
| `MEDIACRAWLER_RUN_TIMEOUT` | `1800` | Per-run subprocess timeout (s); the risk guard paces requests 10–30 s apart |
| `MEDIACRAWLER_QUEUE_TIMEOUT` | `3600` | How long a crawl may wait for the ones ahead of it (s) |
| `MEDIACRAWLER_GUARD_DIR` | `<HOME>/.risk_guard` | Where the optional risk guard keeps its shared state |

---

## Register with an MCP client

### Claude Code (CLI)

```bash
claude mcp add mediacrawler \
  --env MEDIACRAWLER_HOME=/path/to/MediaCrawler \
  --env PYTHONPATH=/path/to/mediacrawler-mcp \
  -- python -m mediacrawler_mcp.server
```

### Claude Desktop / Cursor (JSON)

See [`examples/claude_desktop_config.snippet.json`](examples/claude_desktop_config.snippet.json)
and paste it into your client's MCP config, fixing the two absolute paths.

---

## Quick test

```bash
# 1. unit tests (no network, no CLI)
pip install --user pytest
PYTHONPATH=. pytest -q

# 2. end-to-end (actually runs MediaCrawler — log in first!)
export MEDIACRAWLER_HOME=/path/to/MediaCrawler
python examples/quickstart.py
```

Ask your agent, e.g.:

> "用 mediacrawler 搜索小红书上关于『城市露营』的 10 条笔记并带上评论"

---

## How it passes parameters (important)

MediaCrawler reads **keywords / specified post-ids / creator-ids from
`config/base_config.py`**, not reliably from CLI flags. So before each run this
server **temporarily patches `base_config.py`** (setting `KEYWORDS`,
`*_SPECIFIED_ID_LIST`, `*_CREATOR_ID_LIST`, `CRAWLER_MAX_NOTES_COUNT`,
`ENABLE_GET_COMMENTS`, `ENABLE_CDP_MODE`, …) and **restores the original file
afterwards**, byte for byte. This keeps the server working across MediaCrawler
versions without forking it.

If MediaCrawler changes its argument names, run the `diagnostics` tool to see the
current `main.py --help`, then adjust `mediacrawler_mcp/runner.py`.

---

## One crawl at a time

Every MCP server process on this machine shares one MediaCrawler checkout: one
`config/base_config.py`, one debug Chrome, and one result file per platform, type
and day, which MediaCrawler rewrites whole for each item. So crawls queue on a
machine-wide lock, `<MEDIACRAWLER_GUARD_DIR>/crawl.lock`, and a tool call simply
takes longer while it waits. `queue_wait_seconds` in the result says how long.

- **Scope**: the lock covers `search_posts`, `get_post_detail` and
  `get_creator_posts`. `uv run main.py` by hand and other tools that drive the same
  Chrome do not take it.
- **Breaker**: the risk guard is checked again once a crawl reaches the front, since
  the crawl ahead may have tripped it.
- **Server killed mid-crawl**: the crawler inherits the lock, so the queue waits for
  it to finish. The original config stays in
  `<MEDIACRAWLER_GUARD_DIR>/base_config.py.running` for the length of a run, and the
  next crawl restores from it if the server never got to.
- **Timeout**: the whole process group is stopped, because `uv run` keeps Python as
  a child and killing `uv` alone would leave the crawl running.
- **Results**: `items` holds only what this call crawled. The record counts of the
  per-day files are taken before the crawl and compared after it, inside the lock.
  `raw_files` lists those per-day files, which also hold every earlier crawl of the day.
- **Logs**: the crawler's output goes to temporary files. Playwright's Node driver
  makes the stderr it inherits non-blocking, which on a pipe cuts long log lines
  mid-character and drops later ones. Any invalid UTF-8 left in the output is shown
  as U+FFFD.

Code changes reach a server only when that process restarts, and a server still on
code from before this lock neither queues nor respects it.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `configuration_error` | `MEDIACRAWLER_HOME` wrong or missing `main.py` |
| `run_error: Failed to launch ...` | `uv` not installed → set `MEDIACRAWLER_RUNNER=python` and activate a venv |
| `count: 0` + warning | Login session expired → run `login_status`, then re-login (QR / CDP) |
| `rate_limited` | You hit the local guard; wait, or raise `MEDIACRAWLER_RATE_CALLS` (at your own risk) |
| `queue_timeout` | Other crawls held the queue past `MEDIACRAWLER_QUEUE_TIMEOUT`. `details` names the holder and its pid; retry later, and stop that process only if it is stuck |
| warning "could not attach to the debug Chrome" | MediaCrawler fell back to its own browser profile, without the debug Chrome's login. The warning quotes the reason; check that Chrome is running with `--remote-debugging-port=9222` |
| `risk_guard_tripped` | The platform answered with a captcha / risk code; every project is paused until `retry_after_seconds`. Solve any captcha in the browser; do not retry early |
| warning "stopped early because of its risk guard" | The budget ran out (`BudgetExhausted`) or a captcha appeared mid-run; partial results are returned |
| warning "login session was not accepted" (xhs) | The platform rejected the browser session. Check that Chrome is logged in, or use the Apify backend for xhs |

---

## Optional risk guard integration

This server can cooperate with a cross-process **risk guard** running inside
MediaCrawler, i.e. a component that paces every request, counts them against
shared hourly/daily budgets, and opens a circuit breaker when the platform
answers with a captcha or risk-control code. **No such guard ships with
upstream MediaCrawler or with this repository**; if you add one, have it write
its state to `<MEDIACRAWLER_GUARD_DIR>/<platform>.json`:

```json
{ "tripped_until": 1767225600, "trip_reason": "captcha HTTP 461",
  "requests": { "account": [1767220000.0], "anon": [], "media": [] },
  "history": [] }
```

When that file exists, the server:

- checks the breaker before launching MediaCrawler and returns
  `risk_guard_tripped` with `retry_after_seconds` instead of crawling;
- reports the request budget used in `risk_status` / `login_status`;
- recognises the guard's log markers (`RiskControlTripped`, `BudgetExhausted`,
  `LoginRequired`, `[RiskGuard]`) and returns partial results with a warning.

Without the file, these checks are no-ops and everything else works as usual.

## Safety & compliance

- Personal, low-frequency, **non-commercial research** only.
- Use a **secondary account**; prefer **CDP mode** + proxies; keep counts small.
- Do **not** collect private personal data or anything behind auth you don't own.
- You are responsible for complying with platform ToS and applicable law.
  See MediaCrawler's disclaimer and
  [记录爬虫违法违规案例](https://github.com/HiddenStrawberry/Crawler_Illegal_Cases_In_China).

## License

The code in this repository is released under the [MIT License](LICENSE).
It does **not** include any MediaCrawler code; it runs MediaCrawler as an
external program. MediaCrawler itself is distributed under its own
[NON-COMMERCIAL LEARNING LICENSE 1.1](https://github.com/NanmiCoder/MediaCrawler/blob/main/LICENSE),
which you must accept and comply with when you install and run it.
