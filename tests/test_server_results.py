"""A failed crawl must not look like a search with no results."""
import subprocess

from mediacrawler_mcp import runner, server

CRASH = "ModuleNotFoundError: No module named 'tools'"


def search(monkeypatch, *, returncode=0, timed_out=False, stderr="", count=0):
    meta = {
        "command": "uv run main.py",
        "started_ts": 0.0,
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout_tail": "",
        "stderr_tail": stderr,
        "notices": [],
    }
    collected = {
        "count": count,
        "items": [{"id": str(i)} for i in range(count)],
        "raw_files": [],
        "warnings": [],
    }
    monkeypatch.setattr(server, "_guard", lambda platform: None)
    monkeypatch.setattr(server, "run_crawl", lambda *a, **kw: meta)
    monkeypatch.setattr(server, "count_records", lambda *a: {})
    monkeypatch.setattr(server, "collect_results", lambda **kw: collected)
    monkeypatch.setattr(server, "read_state", lambda *a: {"tripped": False})
    return server.search_posts("bili", "kw")


def test_startup_crash_is_a_failure(monkeypatch):
    result = search(monkeypatch, returncode=1, stderr=CRASH)
    assert result["ok"] is False
    assert result["error"] == "crawl_failed"
    assert "code 1" in result["details"][0] and CRASH in result["details"][0]
    assert result["items"] == [] and "partial" not in result
    assert result["risk_status"] == {"tripped": False}


def test_killed_by_a_signal_is_a_failure(monkeypatch):
    assert search(monkeypatch, returncode=-1)["ok"] is False


def test_timeout_without_results_is_a_failure(monkeypatch):
    result = search(monkeypatch, returncode=-1, timed_out=True)
    assert result["ok"] is False and result["error"] == "crawl_failed"
    assert "timed out" in result["details"][0]


def test_stopped_run_keeps_what_it_saved(monkeypatch):
    result = search(monkeypatch, returncode=1, stderr="RiskControlTripped: captcha", count=3)
    assert result["ok"] is True and result["partial"] is True
    assert len(result["items"]) == 3 and "error" not in result
    assert any("code 1" in w for w in result["warnings"])
    assert result["query"] == "kw"


def test_timeout_with_results_is_partial(monkeypatch):
    result = search(monkeypatch, returncode=-1, timed_out=True, count=2)
    assert result["ok"] is True and result["partial"] is True
    assert any("timed out" in w for w in result["warnings"])


def test_finished_without_results_is_still_ok(monkeypatch):
    result = search(monkeypatch, count=0)
    assert result["ok"] is True and result["count"] == 0
    assert "error" not in result and "partial" not in result


def test_clean_run_is_unchanged(monkeypatch):
    result = search(monkeypatch, count=2)
    assert result["ok"] is True and result["count"] == 2
    assert "error" not in result and "partial" not in result and result["warnings"] == []


def test_diagnostics_reports_startup_failure(monkeypatch):
    monkeypatch.setattr(
        runner.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", CRASH)
    )
    result = server.diagnostics()
    assert result["ok"] is False and result["error"] == "startup_failed"
    assert CRASH in result["help"]


def test_diagnostics_reports_a_probe_that_cannot_run(monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError("uv")

    monkeypatch.setattr(runner.subprocess, "run", boom)
    result = server.diagnostics()
    assert result["ok"] is False and "failed to run" in result["help"]


def test_diagnostics_ok_when_it_starts(monkeypatch):
    monkeypatch.setattr(
        runner.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "Usage: main.py", "")
    )
    result = server.diagnostics()
    assert result["ok"] is True and "error" not in result
