"""Crawls queue across processes and leave the shared config as they found it.

These run a fake MediaCrawler (a small main.py in a temporary folder), so they touch no
platform, no browser and nothing in the real MediaCrawler checkout.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import mediacrawler_mcp
from mediacrawler_mcp import runner, server
from mediacrawler_mcp.config import Settings
from mediacrawler_mcp.risk_state import risk_warnings
from mediacrawler_mcp.runner import QueueTimeout, crawl_lock, patched_config, run_crawl

ORIGINAL_CONFIG = (
    '# -*- coding: utf-8 -*-\r\n'
    'KEYWORDS = "原始关键词"  # 注释\n'
    'CRAWLER_MAX_NOTES_COUNT = 15\n'
    'ENABLE_CDP_MODE = True\n'
).encode("utf-8")

FAKE_MAIN = """
import json, os, runpy, sys, time
cfg = runpy.run_path("config/base_config.py")
started = time.time()
{before}
time.sleep(float(os.environ.get("FAKE_CRAWL_SECONDS", "0.4")))
folder = "data/zhihu/json"
os.makedirs(folder, exist_ok=True)
day_file = f"{{folder}}/search_contents_2026-11-05.json"
records = json.load(open(day_file, encoding="utf-8")) if os.path.exists(day_file) else []
records.append({{"content_id": cfg["KEYWORDS"], "title": cfg["KEYWORDS"]}})
with open(day_file, "w", encoding="utf-8") as f:
    json.dump(records, f, ensure_ascii=False)
with open("runs.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({{"keywords": cfg["KEYWORDS"], "start": started, "end": time.time()}}) + "\\n")
{after}
"""


def make_home(tmp_path: Path, *, before: str = "", after: str = "", exit_code: int = 0) -> Path:
    home = tmp_path / "MediaCrawler"
    (home / "config").mkdir(parents=True)
    (home / "config" / "base_config.py").write_bytes(ORIGINAL_CONFIG)
    (home / "main.py").write_text(
        FAKE_MAIN.format(before=before, after=after + f"\nsys.exit({exit_code})"), encoding="utf-8"
    )
    return home


def settings_for(home: Path, **overrides) -> Settings:
    return Settings(mediacrawler_home=home, python_runner=sys.executable, **overrides)


def runs(home: Path) -> list[dict]:
    path = home / "runs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def config_bytes(home: Path) -> bytes:
    return (home / "config" / "base_config.py").read_bytes()


def wait_until(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def crawl(settings: Settings, keywords: str = "kw") -> dict:
    with crawl_lock(settings, "test") as slot:
        return run_crawl(settings, slot=slot, platform="zhihu", crawl_type="search", keywords=keywords)


def spawn_server_call(home: Path, keywords: str, **env) -> subprocess.Popen:
    """Call search_posts from its own process, the way a separate MCP server would."""
    code = (
        "import json, sys\n"
        "from mediacrawler_mcp import server\n"
        "print(json.dumps(server.search_posts('zhihu', sys.argv[1]), ensure_ascii=False))\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code, keywords],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(mediacrawler_mcp.__file__).parent.parent),
            "MEDIACRAWLER_HOME": str(home),
            "MEDIACRAWLER_RUNNER": sys.executable,
            **env,
        },
    )


# --- queueing ---------------------------------------------------------------


def test_crawls_from_separate_server_processes_run_one_at_a_time(tmp_path):
    home = make_home(tmp_path)
    procs = {kw: spawn_server_call(home, kw) for kw in ("甲", "乙", "丙")}
    results = {}
    for kw, proc in procs.items():
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err.decode("utf-8", "replace")
        results[kw] = json.loads(out)

    order = sorted(runs(home), key=lambda r: r["start"])
    assert len(order) == 3
    for earlier, later in zip(order, order[1:]):
        assert earlier["end"] <= later["start"], "two crawls overlapped"

    # Each crawl ran with its own keywords, and got back only what it saved itself.
    assert {r["keywords"] for r in order} == {"甲", "乙", "丙"}
    for kw, result in results.items():
        assert result["ok"] is True and result["count"] == 1, result
        assert result["items"][0]["id"] == kw
    assert max(r["queue_wait_seconds"] for r in results.values()) >= 1

    assert config_bytes(home) == ORIGINAL_CONFIG


def test_threads_in_one_process_queue_too(tmp_path):
    settings = settings_for(make_home(tmp_path))
    inside, overlaps = [], []

    def hold():
        with crawl_lock(settings, "thread"):
            inside.append(1)
            overlaps.append(len(inside))
            time.sleep(0.1)
            inside.pop()

    threads = [threading.Thread(target=hold) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlaps == [1, 1, 1]


def test_giving_up_on_the_queue_names_who_holds_it(tmp_path):
    settings = settings_for(make_home(tmp_path), queue_timeout_seconds=0)
    with crawl_lock(settings, "zhihu search"):
        with pytest.raises(QueueTimeout) as exc:
            with crawl_lock(settings, "bili search"):
                pass
    assert "zhihu search" in str(exc.value) and f"pid {os.getpid()}" in str(exc.value)


def test_server_reports_a_queue_timeout(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    settings = settings_for(home, queue_timeout_seconds=0)
    monkeypatch.setattr(server, "_settings", settings)
    with crawl_lock(settings, "zhihu search"):
        result = server.search_posts("zhihu", "kw")
    assert result["ok"] is False and result["error"] == "queue_timeout"
    assert runs(home) == []


def test_waiting_crawl_does_not_start_if_the_breaker_tripped_meanwhile(tmp_path, monkeypatch, isolated_guard_dir):
    home = make_home(tmp_path)
    monkeypatch.setattr(server, "_settings", settings_for(home))
    # _guard saw a closed breaker when the call came in; it opened during the wait.
    monkeypatch.setattr(server, "_guard", lambda platform: None)
    isolated_guard_dir.mkdir()
    (isolated_guard_dir / "zhihu.json").write_text(
        json.dumps({"tripped_until": time.time() + 600, "trip_reason": "captcha"}), encoding="utf-8"
    )
    result = server.search_posts("zhihu", "kw")
    assert result["ok"] is False and result["error"] == "risk_guard_tripped"
    assert runs(home) == []
    assert config_bytes(home) == ORIGINAL_CONFIG


def test_lock_is_free_again_after_its_holder_dies(tmp_path):
    settings = settings_for(make_home(tmp_path), queue_timeout_seconds=0)
    code = (
        "import fcntl, sys, time\n"
        "f = open(sys.argv[1], 'a')\n"
        "fcntl.flock(f, fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    settings.guard_dir().mkdir(parents=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", code, str(settings.guard_dir() / runner._LOCK_NAME)], stdout=subprocess.PIPE
    )
    assert holder.stdout.readline().strip() == b"held"
    with pytest.raises(QueueTimeout):
        with crawl_lock(settings, "next"):
            pass
    holder.kill()
    holder.wait()
    holder.stdout.close()
    with crawl_lock(settings, "next") as slot:
        assert slot.waited_seconds < 5


def test_server_killed_mid_crawl_keeps_the_queue_and_the_config_comes_back(tmp_path):
    home = make_home(tmp_path)
    settings = settings_for(home, queue_timeout_seconds=0)
    proc = spawn_server_call(home, "被杀的", FAKE_CRAWL_SECONDS="2")
    assert wait_until(lambda: b"\xe8\xa2\xab\xe6\x9d\x80" in config_bytes(home))
    time.sleep(0.3)  # let the crawler start
    proc.send_signal(signal.SIGKILL)
    proc.communicate()

    # The crawler is still running: its lock stays held and the patch is still on disk.
    with pytest.raises(QueueTimeout):
        with crawl_lock(settings, "next"):
            pass
    assert config_bytes(home) != ORIGINAL_CONFIG

    assert wait_until(lambda: runs(home)), "the orphaned crawler should finish"
    time.sleep(0.2)  # it exits just after writing that line
    # Nothing has restored the file yet: the server that would have is dead.
    assert config_bytes(home) != ORIGINAL_CONFIG

    # The next crawl finds the snapshot, starts from the true original and leaves it in place.
    next_settings = settings_for(home)
    crawl(next_settings, "下一个")
    assert runs(home)[-1]["keywords"] == "下一个"
    assert config_bytes(home) == ORIGINAL_CONFIG
    assert not (next_settings.guard_dir() / runner._SNAPSHOT_NAME).exists()


def test_a_crawl_returns_only_its_own_records_from_the_shared_day_file(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    monkeypatch.setattr(server, "_settings", settings_for(home))
    monkeypatch.setattr(server, "_guard", lambda platform: None)

    first = server.search_posts("zhihu", "第一个任务")
    second = server.search_posts("zhihu", "第二个任务")

    assert [i["id"] for i in first["items"]] == ["第一个任务"]
    assert [i["id"] for i in second["items"]] == ["第二个任务"]
    # MediaCrawler's file itself is left whole, and is still where the raw data lives.
    day_file = home / "data/zhihu/json/search_contents_2026-11-05.json"
    assert [r["content_id"] for r in json.loads(day_file.read_text(encoding="utf-8"))] == ["第一个任务", "第二个任务"]
    assert second["raw_files"] == [str(day_file)]


# --- config restore ---------------------------------------------------------


def test_crawl_sees_the_patch_and_the_file_comes_back_byte_for_byte(tmp_path):
    home = make_home(tmp_path)
    crawl(settings_for(home), "新的 关键词")
    assert runs(home)[0]["keywords"] == "新的 关键词"
    assert config_bytes(home) == ORIGINAL_CONFIG


def test_config_comes_back_after_a_failed_crawl(tmp_path):
    home = make_home(tmp_path, exit_code=1)
    meta = crawl(settings_for(home))
    assert meta["returncode"] == 1
    assert config_bytes(home) == ORIGINAL_CONFIG


def test_config_comes_back_when_the_runner_cannot_start(tmp_path):
    home = make_home(tmp_path)
    settings = Settings(mediacrawler_home=home, python_runner="/no/such/python")
    with pytest.raises(runner.MediaCrawlerError):
        crawl(settings)
    assert config_bytes(home) == ORIGINAL_CONFIG


def test_config_comes_back_when_the_block_raises(tmp_path):
    home = make_home(tmp_path)
    settings = settings_for(home)
    with pytest.raises(ValueError):
        with patched_config(settings, {"KEYWORDS": "'x'"}):
            assert config_bytes(home) != ORIGINAL_CONFIG
            raise ValueError
    assert config_bytes(home) == ORIGINAL_CONFIG
    assert not (settings.guard_dir() / runner._SNAPSHOT_NAME).exists()


def test_timeout_stops_the_crawler_and_what_it_started(tmp_path):
    # `uv run` keeps Python as a child, so killing only the top process would leave the crawl going.
    home = make_home(
        tmp_path,
        before=(
            "import subprocess\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "open('child.pid', 'w').write(str(child.pid))\n"
            "print('未结束的日志')\n"
            "sys.stdout.flush()\n"
            "time.sleep(60)"
        ),
    )
    meta = crawl(settings_for(home, run_timeout_seconds=1))
    assert meta["timed_out"] is True and meta["returncode"] == -1
    assert "未结束的日志" in meta["stdout_tail"]

    pid = int((home / "child.pid").read_text())

    def gone():
        try:
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            return True

    assert wait_until(gone)
    assert config_bytes(home) == ORIGINAL_CONFIG
    with crawl_lock(settings_for(home, queue_timeout_seconds=0), "next"):
        pass


# --- output -----------------------------------------------------------------

BROKEN_OUTPUT = (
    "sys.stdout.flush()\n"
    "sys.stderr.flush()\n"
    # A character cut in two, followed by a new log line: "invalid continuation byte".
    "os.write(2, '知'.encode()[:2] + b'--- Logging error ---\\n')\n"
    "os.write(1, '乎'.encode()[:1] + b'next line\\n')\n"
    # And one cut off at the very end of the stream: "unexpected end of data".
    "os.write(2, '答'.encode()[:2])\n"
    "os.write(1, '案'.encode()[:1])\n"
)


def test_broken_utf8_in_the_logs_does_not_fail_a_crawl_that_worked(tmp_path, monkeypatch):
    home = make_home(tmp_path, after=BROKEN_OUTPUT)
    monkeypatch.setattr(server, "_settings", settings_for(home))
    monkeypatch.setattr(server, "_guard", lambda platform: None)
    result = server.search_posts("zhihu", "kw")
    assert result["ok"] is True and result["count"] == 1 and "partial" not in result
    assert result["items"][0]["id"] == "kw"


def test_broken_utf8_is_replaced_in_the_tails(tmp_path):
    meta = crawl(settings_for(make_home(tmp_path, after=BROKEN_OUTPUT)))
    assert meta["returncode"] == 0
    assert "�--- Logging error ---" in meta["stderr_tail"]
    assert "�next line" in meta["stdout_tail"]
    assert meta["stderr_tail"].endswith("�") and meta["stdout_tail"].endswith("�")


def test_log_lines_survive_a_child_making_stderr_non_blocking(tmp_path):
    # What Playwright's Node driver does to the stderr it inherits. On a pipe, this long line
    # is cut mid-character and the line after it, the one saying why the run stopped, is lost.
    home = make_home(
        tmp_path,
        after=(
            "import logging\n"
            "logging.basicConfig(level=logging.INFO, format='%(message)s')\n"
            "os.set_blocking(2, False)\n"
            "logging.info('知乎回答' * 200000)\n"
            "logging.info('[RiskGuard] zhihu is paused')\n"
        ),
    )
    meta = crawl(settings_for(home))
    assert meta["stderr_tail"].endswith("[RiskGuard] zhihu is paused\n")
    assert "BlockingIOError" not in meta["stderr_tail"] and "�" not in meta["stderr_tail"]


def test_falling_back_from_the_debug_chrome_is_reported(tmp_path, monkeypatch):
    # The reason is logged well before the end of the run, so it must not depend on the tail.
    line = "[ZhihuCrawler] CDP mode launch failed, falling back to standard mode: port refused"
    home = make_home(
        tmp_path,
        after=f"print({line!r})\nprint('x' * 5000)\n",
    )
    monkeypatch.setattr(server, "_settings", settings_for(home))
    monkeypatch.setattr(server, "_guard", lambda platform: None)
    result = server.search_posts("zhihu", "kw")
    assert result["ok"] is True
    assert any("could not attach to the debug Chrome" in w and "port refused" in w for w in result["warnings"])


@pytest.mark.parametrize(
    "line",
    [
        "[ZhihuCrawler] CDP mode launch failed, falling back to standard mode: x",
        "[BilibiliCrawler] CDP mode launch failed, fallback to standard mode: x",
        "[WeiboCrawler] CDP mode startup failed, falling back to standard mode: x",
    ],
)
def test_every_platforms_fallback_wording_is_recognised(line):
    warnings = risk_warnings(line, "")
    assert len(warnings) == 1 and "could not attach to the debug Chrome" in warnings[0]


def test_choosing_standard_mode_is_not_reported_as_a_fallback():
    assert risk_warnings("[ZhihuCrawler] Launching browser in standard mode", "") == []


def test_diagnostics_survives_broken_utf8(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    (home / "main.py").write_text("import os\nos.write(1, '知'.encode()[:2] + b'usage')\n", encoding="utf-8")
    monkeypatch.setattr(server, "_settings", settings_for(home))
    result = server.diagnostics()
    assert result["ok"] is True and "�usage" in result["help"]
