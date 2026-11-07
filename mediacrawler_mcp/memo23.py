"""Xiaohongshu backend for the local MediaCrawler MCP tools."""
from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ACTOR = "memo23/xiaohongshu-all-in-one-scraper"
TOKEN_FILE = Path.home() / ".config/mediacrawler/apify-token.txt"
API = "https://api.apify.com/v2/"
TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


def token():
    value = os.environ.get("APIFY_TOKEN", "").strip()
    if not value and TOKEN_FILE.is_file():
        value = TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
    if not value or not value.isascii() or any(c.isspace() or not c.isprintable() for c in value):
        raise ValueError(f"Set APIFY_TOKEN or save the token alone in {TOKEN_FILE}.")
    return value


def status():
    try:
        token()
    except (OSError, ValueError):
        configured = False
    else:
        configured = True
    return {"actor": ACTOR, "token_configured": configured, "token_file": str(TOKEN_FILE)}


def target(value, creator=False):
    """Drop share tracking tokens for canonical IDs; keep the short-link path."""
    value = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{24}", value):
        return value.lower()
    url = urlsplit(value)
    if url.scheme in {"http", "https"}:
        if url.hostname in {"xhslink.com", "www.xhslink.com"} and url.path.strip("/"):
            return "https://xhslink.com" + url.path
        if url.hostname in {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.cn", "www.rednote.com"}:
            prefix = "user/profile" if creator else "(?:explore|discovery/item)"
            match = re.fullmatch(rf"/{prefix}/([0-9a-fA-F]{{24}})/?", url.path)
            if match:
                return match[1].lower()
    raise ValueError("Expected a Xiaohongshu ID, note/profile URL, or xhslink.com share link.")


def build_input(mode, values, count, with_comments, sort=None):
    payload = {"maxItems": count, "includeComments": with_comments, "includeReplies": with_comments}
    if with_comments:
        payload.update(maxCommentsPerNote=10, maxRepliesPerComment=5)
    if mode == "search":
        words = list(dict.fromkeys(w.strip() for w in values if w.strip()))
        if not words:
            raise ValueError("Provide at least one search keyword.")
        payload.update(keywords=words, maxResultsPerKeyword=max(5, count))
        if sort in {"general", "latest", "mostLiked", "mostCommented", "mostSaved"}:
            payload["searchSort"] = sort
    else:
        ids = list(dict.fromkeys(target(v, creator=mode == "creator") for v in values))
        if mode == "detail":
            payload.update(noteUrls=ids)
        else:
            payload.update(creators=ids, maxNotesPerCreator=max(5, count), includeCreatorNotes=True,
                           maxItems=max(5, count) * len(ids))
    return payload


def normalise(rows, with_comments):
    notes = {}
    threads = defaultdict(dict)
    for row in rows:
        nid = row.get("noteId")
        if not nid:
            continue
        if row.get("type") == "note":
            notes[nid] = {
                "id": nid, "author": row.get("authorName"), "author_id": row.get("authorId"),
                "title": row.get("title"), "content": row.get("text") or row.get("title"),
                "like_count": row.get("likes"), "comment_count": row.get("comments"),
                "publish_time": row.get("publishedAt"), "url": row.get("url") or
                f"https://www.xiaohongshu.com/explore/{nid}", "raw": row,
            }
        elif with_comments and row.get("type") in {"comment", "reply"} and row.get("commentId"):
            threads[nid][row["commentId"]] = {
                **row, "note_id": nid, "comment_id": row["commentId"], "content": row.get("text"),
                "nickname": row.get("userName"), "like_count": row.get("likes"),
                "create_time": row.get("postedAt"), "parent_comment_id": row.get("parentCommentId"),
            }
    if with_comments:
        for nid, note in notes.items():
            comments = list(threads[nid].values())
            counts = Counter(c["parent_comment_id"] for c in comments if c["type"] == "reply")
            for c in comments:
                if c["type"] == "comment":
                    c["replies_returned"] = counts[c["comment_id"]]
                    expected = c.get("replyCount")
                    c["replies_incomplete"] = (counts[c["comment_id"]] < expected
                                               if isinstance(expected, int) else None)
            note["comments"] = comments
    return list(notes.values())


def _read_rows(client, dataset, rows):
    while True:
        page = client.get(f"datasets/{dataset}/items",
                          params={"format": "json", "offset": len(rows), "limit": 250}).raise_for_status().json()
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise ValueError("Unexpected Apify dataset response.")
        rows.extend(page)
        if len(page) < 250:
            return


def collect(mode, values, count, with_comments, data_dir, *, sort=None):
    result = {"ok": False, "platform": "xhs", "backend": ACTOR, "items": [], "count": 0,
              "warnings": [], "raw_files": []}
    try:
        payload = build_input(mode, values, count, with_comments, sort)
    except ValueError as exc:
        return {**result, "error": "bad_args", "details": [str(exc)]}
    try:
        secret = token()
        budget = float(os.environ.get("MEDIACRAWLER_APIFY_MAX_USD", "2"))
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("MEDIACRAWLER_APIFY_MAX_USD must be a finite positive amount.")
    except (ValueError, OSError) as exc:
        return {**result, "error": "configuration_error", "details": [str(exc)]}

    rows = []
    issue = None
    try:
        # Only submit once. Read-only polls of the same run cannot create a second charge.
        with httpx.Client(base_url=API, headers={"Authorization": f"Bearer {secret}"},
                          timeout=65) as client:
            run = client.post(f"acts/{ACTOR.replace('/', '~')}/runs", json=payload,
                              params={"maxTotalChargeUsd": budget, "timeout": 300}
                              ).raise_for_status().json()["data"]
            rid = run["id"]
            result["run_id"] = rid
            deadline = time.monotonic() + 365
            while run["status"] not in TERMINAL:
                if time.monotonic() >= deadline:
                    raise ValueError(f"Run {rid} has not reached a terminal state; inspect it in Apify.")
                time.sleep(1)
                run = client.get(f"actor-runs/{rid}", params={"waitForFinish": 30}
                                 ).raise_for_status().json()["data"]
            result["run_status"] = run["status"]
            for name in ("chargedEventCounts", "usageTotalUsd"):
                if name in run:
                    result[name] = run[name]
            if run["status"] != "SUCCEEDED":
                issue = f"Apify run ended with {run['status']}."
            _read_rows(client, run["defaultDatasetId"], rows)
    except httpx.HTTPStatusError as exc:
        issue = f"Apify returned HTTP {exc.response.status_code}."
        if exc.response.status_code in (401, 403):
            issue += " Check the Apify token and its Actor/run/dataset permissions."
    except (httpx.RequestError, ValueError, KeyError, TypeError) as exc:
        issue = f"Apify request or response failed ({type(exc).__name__})."
    items = normalise(rows, with_comments)
    if rows:
        try:
            folder = data_dir / "xhs" / "apify" / result["run_id"]
            folder.mkdir(parents=True, exist_ok=True)
            out = folder / "rows.json"
            tmp = folder / "rows.tmp"
            tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            tmp.replace(out)
            result["raw_files"].append(str(out))
        except OSError as exc:
            result["warnings"].append(f"Could not save local raw rows ({type(exc).__name__}).")
    # The Actor's per-creator minimum is five; retain the MCP's smaller per-creator cap.
    if mode == "creator":
        kept = Counter()
        selected = []
        for item in items:
            owner = item["raw"].get("creatorId") or item["author_id"]
            if kept[owner] < count:
                selected.append(item)
                kept[owner] += 1
        items = selected
    else:
        items = items[:count]
    if mode == "detail" and len(items) < len(payload["noteUrls"]):
        issue = issue or "One or more requested notes were missing from the dataset."
    if rows and not items and mode != "creator":
        issue = issue or "Dataset contained rows but no usable note."
    if issue and not items:
        return {**result, "error": "crawl_failed", "details": [issue]}
    if issue:
        result["partial"] = True
        result["warnings"].append(issue)
    if any(c.get("replies_incomplete") for n in items for c in n.get("comments", [])):
        result["warnings"].append("Some comment threads contain fewer replies than the reported reply count.")
    if not items:
        result["warnings"].append("Apify returned no notes for this request.")
    return {**result, "ok": True, "items": items, "count": len(items)}
