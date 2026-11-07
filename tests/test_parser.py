"""Unit tests for the output parser. These use mocked data files and do NOT
touch any real platform or the MediaCrawler CLI."""

import json
import time
from pathlib import Path

from mediacrawler_mcp.parser import collect_results, count_records, normalise_record


def test_normalise_xhs_like_record():
    raw = {
        "note_id": "abc123",
        "nickname": "tester",
        "user_id": "u1",
        "title": "hello world",
        "liked_count": "42",
        "comment_count": 3,
        "time": "2026-07-19",
        "note_url": "https://xhs/abc123",
    }
    n = normalise_record(raw)
    assert n["id"] == "abc123"
    assert n["author"] == "tester"
    assert n["author_id"] == "u1"
    assert n["content"] == "hello world"
    assert n["like_count"] == "42"
    assert n["comment_count"] == 3
    assert n["url"] == "https://xhs/abc123"
    assert n["raw"] == raw


def test_collect_results_reads_json(tmp_path: Path):
    data_dir = tmp_path / "data" / "xhs"
    data_dir.mkdir(parents=True)
    content = [
        {"note_id": "n1", "nickname": "a", "title": "post one", "liked_count": 10},
        {"note_id": "n2", "nickname": "b", "title": "post two", "liked_count": 20},
    ]
    cfile = data_dir / "xhs_search_contents.json"
    cfile.write_text(json.dumps(content), encoding="utf-8")

    comments = [
        {"note_id": "n1", "content": "nice"},
        {"note_id": "n1", "content": "cool"},
    ]
    (data_dir / "xhs_search_comments.json").write_text(
        json.dumps(comments), encoding="utf-8"
    )

    res = collect_results(
        data_dir=tmp_path / "data",
        platform="xhs",
        since_ts=time.time() - 10,
        save_option="json",
        with_comments=True,
    )
    assert res["count"] == 2
    ids = {item["id"] for item in res["items"]}
    assert ids == {"n1", "n2"}
    n1 = next(i for i in res["items"] if i["id"] == "n1")
    assert len(n1.get("comments", [])) == 2
    assert not res["warnings"]


def test_collect_results_warns_when_empty(tmp_path: Path):
    (tmp_path / "data").mkdir()
    res = collect_results(
        data_dir=tmp_path / "data",
        platform="wb",
        since_ts=time.time(),
        save_option="json",
        with_comments=True,
    )
    assert res["count"] == 0
    assert res["warnings"]


def test_output_file_cut_mid_character_warns_instead_of_raising(tmp_path: Path):
    data_dir = tmp_path / "data" / "zhihu"
    data_dir.mkdir(parents=True)
    whole = json.dumps([{"content_id": "1", "title": "知乎回答"}], ensure_ascii=False).encode("utf-8")
    cut = whole[: whole.index("回".encode("utf-8")) + 1]
    (data_dir / "search_contents.json").write_bytes(cut)

    res = collect_results(
        data_dir=tmp_path / "data",
        platform="zhihu",
        since_ts=time.time() - 10,
        save_option="json",
        with_comments=False,
    )
    assert res["count"] == 0
    assert any("Could not parse" in w and "search_contents.json" in w for w in res["warnings"])


def test_previous_crawls_last_write_is_not_this_crawls_output(tmp_path: Path):
    data_dir = tmp_path / "data" / "zhihu"
    data_dir.mkdir(parents=True)
    path = data_dir / "search_contents.json"
    path.write_text(json.dumps([{"content_id": "earlier"}]), encoding="utf-8")
    # Crawls queue, so the next one can start within a second of the last write.
    res = collect_results(
        data_dir=tmp_path / "data",
        platform="zhihu",
        since_ts=path.stat().st_mtime + 0.5,
        save_option="json",
        with_comments=False,
    )
    assert res["count"] == 0


def _collect(tmp_path: Path, platform="zhihu", save_option="json", with_comments=True, **kw):
    return collect_results(
        data_dir=tmp_path / "data",
        platform=platform,
        since_ts=kw.pop("since_ts", time.time() - 10),
        save_option=save_option,
        with_comments=with_comments,
        **kw,
    )


def _day_file(tmp_path: Path, name: str, records: list) -> Path:
    path = tmp_path / "data" / "zhihu" / "json" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return path


def test_only_records_added_after_the_count_are_returned(tmp_path: Path):
    earlier = [{"content_id": "别的任务", "title": "a"}]
    _day_file(tmp_path, "search_contents_2026-11-05.json", earlier)
    _day_file(tmp_path, "search_comments_2026-11-05.json", [{"content_id": "x", "content": "旧评论"}])
    counts = count_records(tmp_path / "data", "zhihu", "json")

    _day_file(tmp_path, "search_contents_2026-11-05.json", earlier + [{"content_id": "这次", "title": "b"}])
    _day_file(
        tmp_path,
        "search_comments_2026-11-05.json",
        [{"content_id": "x", "content": "旧评论"}, {"content_id": "这次", "content": "新评论"}],
    )
    res = _collect(tmp_path, earlier_counts=counts)
    assert [i["id"] for i in res["items"]] == ["这次"]
    assert [c["content"] for c in res["items"][0]["comments"]] == ["新评论"]
    assert not res["warnings"]


def test_a_file_new_since_the_count_is_returned_whole(tmp_path: Path):
    (tmp_path / "data").mkdir()
    counts = count_records(tmp_path / "data", "zhihu", "json")
    assert counts == {}
    _day_file(tmp_path, "search_contents_2026-11-05.json", [{"content_id": "1"}, {"content_id": "2"}])
    assert _collect(tmp_path, earlier_counts=counts)["count"] == 2


def test_a_file_started_over_is_all_new(tmp_path: Path):
    # MediaCrawler drops what it cannot parse and writes the file afresh.
    path = _day_file(tmp_path, "search_contents_2026-11-05.json", [{"content_id": "1"}, {"content_id": "2"}])
    counts = count_records(tmp_path / "data", "zhihu", "json")
    assert counts == {str(path): 2}
    _day_file(tmp_path, "search_contents_2026-11-05.json", [{"content_id": "3"}])
    assert [i["id"] for i in _collect(tmp_path, earlier_counts=counts)["items"]] == ["3"]


def test_counting_works_for_csv(tmp_path: Path):
    path = tmp_path / "data" / "zhihu" / "csv" / "search_contents_2026-11-05.csv"
    path.parent.mkdir(parents=True)
    path.write_text("content_id,title\n1,a\n", encoding="utf-8-sig")
    counts = count_records(tmp_path / "data", "zhihu", "csv")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("2,b\n")
    res = _collect(tmp_path, save_option="csv", earlier_counts=counts)
    assert [i["id"] for i in res["items"]] == ["2"]


def test_counting_skips_old_files_and_other_platforms(tmp_path: Path):
    import os

    old = _day_file(tmp_path, "search_contents_2026-10-01.json", [{"content_id": "1"}])
    os.utime(old, (time.time() - 3 * 86400, time.time() - 3 * 86400))
    other = tmp_path / "data" / "bili" / "json" / "search_contents_2026-11-05.json"
    other.parent.mkdir(parents=True)
    other.write_text("[]", encoding="utf-8")
    assert count_records(tmp_path / "data", "zhihu", "json") == {}
