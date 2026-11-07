"""Parse MediaCrawler output files from the ``data/`` directory.

MediaCrawler writes results under ``<HOME>/data/`` in the format chosen by
``--save_data_option`` (json / csv / ...). Different platforms use slightly
different field names, so we normalise the most common fields into a stable
schema while always preserving the raw record.

We deliberately keep this tolerant: MediaCrawler's exact filenames and columns
change across versions, so we locate the newest relevant files and best-effort
map fields, never crashing on unknown shapes.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any

# Candidate field names per normalised key, tried in order.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("note_id", "aweme_id", "video_id", "id", "post_id", "content_id", "question_id", "answer_id"),
    "author": ("nickname", "author", "user_name", "username", "author_name"),
    "author_id": ("user_id", "author_id", "uid", "sec_uid"),
    "content": ("title", "desc", "content", "text", "note_desc"),
    "like_count": ("liked_count", "like_count", "digg_count", "likes", "voteup_count"),
    "comment_count": ("comment_count", "comments_count", "total_comment", "sub_comment_count"),
    "publish_time": ("time", "create_time", "publish_time", "created_at", "last_update_time"),
    "url": ("note_url", "url", "video_url", "post_url", "link"),
}


def _first(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in record and record[k] not in (None, ""):
            return record[k]
    return None


def normalise_record(record: dict[str, Any]) -> dict[str, Any]:
    """Map a raw MediaCrawler record into the stable output schema."""
    normalised = {key: _first(record, aliases) for key, aliases in _FIELD_ALIASES.items()}
    normalised["raw"] = record
    return normalised


def _read_json_file(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return []
    if not text:
        return []
    # Support both a JSON array and JSON-Lines.
    if text[0] == "[":
        try:
            data = json.loads(text)
            return [r for r in data if isinstance(r, dict)]
        except json.JSONDecodeError:
            return []
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                records.append(obj)
        except json.JSONDecodeError:
            continue
    return records


def _read_csv_file(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def _looks_like_comment(path: Path) -> bool:
    name = path.name.lower()
    return "comment" in name


def _output_files(data_dir: Path, platform: str, save_option: str) -> list[tuple[Path, float]]:
    """Every result file for ``platform``, with its modification time.

    Files are matched loosely by platform code appearing in the path: MediaCrawler nests
    output under a platform folder or encodes it in the filename.
    """
    if not data_dir.exists():
        return []
    ext = ".json" if save_option == "json" else ".csv" if save_option == "csv" else ""
    found: list[tuple[Path, float]] = []
    for path in data_dir.rglob("*"):
        if not path.is_file():
            continue
        if ext and path.suffix.lower() != ext:
            continue
        if ext == "" and path.suffix.lower() not in (".json", ".csv"):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if platform in str(path).lower():
            found.append((path, mtime))
    return found


def find_output_files(
    data_dir: Path,
    platform: str,
    since_ts: float,
    save_option: str,
) -> tuple[list[Path], list[Path]]:
    """Return (content_files, comment_files) modified after ``since_ts``."""
    content_files: list[Path] = []
    comment_files: list[Path] = []
    for path, mtime in _output_files(data_dir, platform, save_option):
        # No tolerance: crawls queue, so the previous one's last write can come just before
        # this one starts. A crawl's own files are written well after it starts.
        if mtime < since_ts:
            continue
        if _looks_like_comment(path):
            comment_files.append(path)
        else:
            content_files.append(path)
    content_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    comment_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return content_files, comment_files


def load_records(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        return _read_json_file(path)
    if path.suffix.lower() == ".csv":
        return _read_csv_file(path)
    return []


def count_records(data_dir: Path, platform: str, save_option: str) -> dict[str, int]:
    """How many records each recent result file holds. Take this before a crawl.

    MediaCrawler keeps one file per platform, crawl type and day, and adds every crawl of that
    day to the end of it, so these counts are what tells this crawl's records from the rest.
    Files untouched for two days are left out: new records only go into the current day's file.
    """
    recent = time.time() - 2 * 86400
    return {
        str(path): len(load_records(path))
        for path, mtime in _output_files(data_dir, platform, save_option)
        if mtime >= recent
    }


def _added_since(records: list[dict[str, Any]], earlier_count: int) -> list[dict[str, Any]]:
    # Fewer than before means MediaCrawler started the file over, so all of it is new.
    return records[earlier_count:] if len(records) >= earlier_count else records


def collect_results(
    data_dir: Path,
    platform: str,
    since_ts: float,
    save_option: str,
    with_comments: bool,
    earlier_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build the result dict from what this crawl added to the output files.

    ``earlier_counts`` comes from ``count_records``, taken before the crawl. ``raw_files`` lists
    the files read; they also hold every earlier crawl of the same day.
    """
    earlier_counts = earlier_counts or {}
    content_files, comment_files = find_output_files(data_dir, platform, since_ts, save_option)

    items: list[dict[str, Any]] = []
    raw_files: list[str] = []
    unreadable: list[str] = []
    for cf in content_files:
        raw_files.append(str(cf))
        records = load_records(cf)
        if not records and cf.stat().st_size > 0:
            unreadable.append(str(cf))
        for rec in _added_since(records, earlier_counts.get(str(cf), 0)):
            items.append(normalise_record(rec))

    comments_by_post: dict[str, list[dict[str, Any]]] = {}
    if with_comments:
        for cf in comment_files:
            raw_files.append(str(cf))
            for rec in _added_since(load_records(cf), earlier_counts.get(str(cf), 0)):
                key = str(
                    rec.get("note_id")
                    or rec.get("aweme_id")
                    or rec.get("video_id")
                    or rec.get("content_id")
                    or rec.get("post_id")
                    or ""
                )
                comments_by_post.setdefault(key, []).append(rec)

    if with_comments and comments_by_post:
        for item in items:
            key = str(item.get("id") or "")
            if key and key in comments_by_post:
                item["comments"] = comments_by_post[key]

    warnings: list[str] = []
    if not content_files:
        warnings.append(
            "No content output files were found for this run. The crawl may have "
            "failed, returned nothing, or the login session expired (run "
            "login_status)."
        )
    if unreadable:
        warnings.append(
            "Could not parse output files, so their items are missing from this result; "
            "something may have been writing them: " + ", ".join(unreadable)
        )

    return {
        "count": len(items),
        "items": items,
        "raw_files": raw_files,
        "warnings": warnings,
    }
