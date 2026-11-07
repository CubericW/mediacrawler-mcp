import json
import time

from mediacrawler_mcp.config import Settings
from mediacrawler_mcp.risk_state import blocked_payload, read_state, risk_warnings
from mediacrawler_mcp.runner import build_config_patch


def test_no_state_file_means_not_tripped(tmp_path):
    status = read_state(tmp_path, "xhs")
    assert status["tripped"] is False and status["known"] is False
    assert blocked_payload(status) is None


def test_tripped_state_blocks_with_retry_after(tmp_path):
    now = time.time()
    (tmp_path / "xhs.json").write_text(json.dumps({
        "tripped_until": now + 600,
        "trip_reason": "captcha HTTP 461",
        "requests": {"account": [now - 10, now - 4000], "anon": [now - 5]},
        "history": [{"at": "x", "reason": "captcha HTTP 461", "cooldown_s": 1800}],
    }))
    status = read_state(tmp_path, "xhs")
    assert status["tripped"] is True
    assert 590 <= status["retry_after_seconds"] <= 600
    assert status["requests_last_hour"] == {"account": 1, "anon": 1}
    payload = blocked_payload(status)
    assert payload["ok"] is False and payload["error"] == "risk_guard_tripped"


def test_expired_trip_does_not_block(tmp_path):
    (tmp_path / "xhs.json").write_text(json.dumps({"tripped_until": time.time() - 1, "trip_reason": "old"}))
    status = read_state(tmp_path, "xhs")
    assert status["tripped"] is False and status["trip_reason"] is None


def test_risk_warnings_detect_guard_stop():
    tail = "Traceback ...\ntools.risk_guard.RiskControlTripped: xhs risk control: captcha HTTP 461."
    assert risk_warnings("", tail)
    assert risk_warnings("normal run", "") == []


def test_risk_warnings_explain_rejected_session():
    stopped = "media_platform.xhs.exception.LoginRequired: Xiaohongshu did not accept the browser session"
    assert "login session was not accepted" in risk_warnings("", stopped)[0]
    anon_only = "WARNING ... Continuing with anonymous note pages only: no logged-in fallback"
    (warning,) = risk_warnings(anon_only, "")
    assert "public note pages only" in warning


def test_detail_patch_sets_xhs_note_url_list():
    url = "https://www.xiaohongshu.com/explore/abc?xsec_token=T&xsec_source=pc_search"
    patch = build_config_patch(Settings(), keywords=None, specified_ids=[url], creator_ids=None,
                               max_count=1, with_comments=False)
    assert url in patch["XHS_SPECIFIED_NOTE_URL_LIST"]


def test_safer_defaults(monkeypatch):
    for var in ("MEDIACRAWLER_ENABLE_COMMENTS", "MEDIACRAWLER_MAX_HARD_LIMIT", "MEDIACRAWLER_RUN_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.enable_comments is False
    assert s.max_count_hard_limit == 50
    assert s.run_timeout_seconds == 1800


def test_run_keeps_anonymous_only_notice_beyond_the_tail(monkeypatch, tmp_path):
    import contextlib
    import sys

    from mediacrawler_mcp import runner

    code = "print('WARNING Continuing with anonymous note pages only: no comments'); print('note fetched\\n' * 500)"
    monkeypatch.setattr(runner, "_build_command", lambda *a: [sys.executable, "-c", code])
    monkeypatch.setattr(runner, "patched_config", lambda *a: contextlib.nullcontext())
    settings = Settings(mediacrawler_home=tmp_path)
    with runner.crawl_lock(settings, "test") as slot:
        meta = runner.run_crawl(settings, slot=slot, platform="xhs", crawl_type="detail", specified_ids=["u"])
    assert "anonymous note pages only" not in meta["stdout_tail"]
    assert len(meta["notices"]) == 1
    assert risk_warnings("\n".join(meta["notices"]) + "\n" + meta["stdout_tail"], meta["stderr_tail"])
