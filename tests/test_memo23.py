"""Cover the MCP routes, real Actor row shapes and partial/failed paid runs."""
import httpx
import pytest
from mediacrawler_mcp import memo23, server

NOTE = '69f0573d0000000022024ce5'
ROWS = [
    {"type": "reply", "noteId": NOTE, "commentId": "r", "parentCommentId": "c", "text": "回答"},
    {"type": "note", "noteId": NOTE, "title": "标题", "text": "正文", "comments": 27},
    {"type": "comment", "noteId": NOTE, "commentId": "c", "text": "问题", "replyCount": 3},
]


@pytest.fixture
def simulated(monkeypatch, tmp_path):
    monkeypatch.setattr(server._settings, "mediacrawler_home", tmp_path)
    monkeypatch.setenv("APIFY_TOKEN", "dummy-local-fixture")
    monkeypatch.setattr(server._rate_limiter, "acquire", lambda: (True, 0))
    client = httpx.Client
    def install(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(memo23.httpx, "Client", lambda **kw: client(transport=transport, **kw))
    return install


@pytest.mark.parametrize("operation", ["search", "detail", "creator"])
def test_entry_points(operation, simulated, tmp_path):
    import json
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "POST":
            payload = json.loads(request.content)
            assert request.headers["Authorization"] == "Bearer dummy-local-fixture"
            assert "token" not in request.url.params and request.url.params["maxTotalChargeUsd"] == "2.0"
            if operation == "search":
                assert payload["keywords"] == ["一", "二"] and payload["searchSort"] == "latest"
            elif operation == "detail":
                assert payload["noteUrls"] == [NOTE]
            else:
                assert payload["creators"] == [NOTE] and not payload["includeComments"]
            return httpx.Response(201, json={"data": {"id": "run", "status": "SUCCEEDED",
                                                       "defaultDatasetId": "dataset"}})
        assert request.url.path.endswith("/datasets/dataset/items")
        return httpx.Response(200, json=ROWS)
    simulated(handler)
    if operation == "search":
        result = server.search_posts("xhs", "一,二", 2, True, "latest")
    elif operation == "detail":
        result = server.get_post_detail("xhs", [f"https://www.xiaohongshu.com/explore/{NOTE}?xsec_token=unused"], True)
    else:
        result = server.get_creator_posts("xhs", [NOTE], 3)
    assert len(calls) == 2 and result["run_id"] == "run" and result["count"] == 1
    note = result["items"][0]
    assert note["content"] == "正文" and note["comment_count"] == 27
    if operation != "creator":
        assert note["comments"][1]["replies_incomplete"]
        assert note["comments"][1]["replies_returned"] == 1
        assert note["comments"][0]["parent_comment_id"] == "c"
    else:
        assert "comments" not in note
    assert json.loads((tmp_path / "data/xhs/apify/run/rows.json").read_text()) == ROWS


def test_partial_dataset_reads_do_not_resubmit(simulated, tmp_path):
    calls = []
    def handler(req):
        calls.append(req.method)
        if req.method == "POST":
            return httpx.Response(201, json={"data":{"id":"a", "status":"FAILED", "defaultDatasetId":"b"}})
        if req.url.params["offset"] == "0":
            return httpx.Response(200, json=[ROWS[1]]*250)
        return httpx.Response(503)
    simulated(handler)
    result = server.get_post_detail("xhs", [NOTE], True)
    assert result["ok"] and result["partial"] and result["run_status"] == "FAILED"
    assert calls == ["POST", "GET", "GET"]


def test_missing_credential_and_invalid_target(monkeypatch, tmp_path):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.setattr(memo23, "TOKEN_FILE", tmp_path / "missing")
    assert not memo23.status()["token_configured"]
    assert memo23.collect("detail", [NOTE], 1, True, tmp_path)["error"] == "configuration_error"
    assert memo23.collect("detail", ["https://unrelated.example/post"], 1, False, tmp_path)["error"] == "bad_args"
    assert memo23.target("http://xhslink.com/o/hello") == "https://xhslink.com/o/hello"
