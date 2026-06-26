"""Phase 2 intake checks. Run: .venv\\Scripts\\python test_intake.py

Tests the pure URL/mimetype logic. Network download is not exercised here.
"""
import types as _types

import app
import figma
import figma_mcp
import lucid
import lucid_mcp
from app import extract_figma_key, has_lucidchart, to_slack_mrkdwn, SUPPORTED_MIME
from google.genai import errors as genai_errors


def test_figma_key():
    assert extract_figma_key("look https://figma.com/file/abc123XYZ/My-Diagram") == "abc123XYZ"
    assert extract_figma_key("https://www.figma.com/design/Key999/Flow?node-id=1-2") == "Key999"
    assert extract_figma_key("https://www.figma.com/make/uRn7vlncXvjlXs5QqLBZQ3/Android-App?p=f") == "uRn7vlncXvjlXs5QqLBZQ3"
    assert extract_figma_key("no link here") is None
    assert extract_figma_key("https://lucid.app/lucidchart/foo") is None


def test_lucidchart():
    assert has_lucidchart("https://lucid.app/lucidchart/abc/edit")
    assert not has_lucidchart("https://figma.com/file/x/y")
    assert not has_lucidchart("plain text")


def test_supported_mime():
    assert "image/png" in SUPPORTED_MIME
    assert "application/pdf" in SUPPORTED_MIME
    assert "text/plain" not in SUPPORTED_MIME


def test_slack_mrkdwn():
    assert to_slack_mrkdwn("**Summary:** hi") == "*Summary:* hi"
    assert to_slack_mrkdwn("# Heading") == "*Heading*"
    assert to_slack_mrkdwn("A -> B") == "A → B"
    # plain text passes through, trailing whitespace trimmed
    assert to_slack_mrkdwn("just text\n") == "just text"


def test_extract_node_id():
    # URL form '1-2' is converted to API form '1:2'
    assert figma.extract_node_id("https://figma.com/design/K/x?node-id=1-2") == "1:2"
    assert figma.extract_node_id("...&node-id=10-205&t=z") == "10:205"
    assert figma.extract_node_id("https://figma.com/design/K/x") is None


def test_figma_outline():
    data = {
        "nodes": {
            "1:2": {
                "document": {
                    "name": "Flow", "type": "FRAME",
                    "absoluteBoundingBox": {"x": 0, "y": 0, "width": 100, "height": 50},
                    "children": [
                        {"name": "Start", "type": "TEXT", "characters": "Start"},
                        {"name": "arrow", "type": "CONNECTOR",
                         "connectorStart": {"endpointNodeId": "3:1"},
                         "connectorEnd": {"endpointNodeId": "3:2"}},
                    ],
                }
            }
        }
    }
    out = figma.figma_outline(data)
    assert "Flow [FRAME]" in out
    assert 'text="Start"' in out
    assert "connects 3:1 -> 3:2" in out
    assert "  - Start" in out  # child is indented under the frame


def test_lucid_doc_id():
    url = "https://lucid.app/lucidchart/442fec0f-0444-4035-b1f6-10dc97b1ff18/edit?page=0_0#"
    assert lucid.extract_doc_id(url) == "442fec0f-0444-4035-b1f6-10dc97b1ff18"
    assert lucid.extract_doc_id("no lucid link here") is None


def test_lucid_get_lucid_falls_back_to_rest():
    """When Lucid MCP fails, get_lucid uses REST export (image). No network."""
    import lucid_mcp

    def boom(*_):
        raise RuntimeError("mcp down")

    orig_mcp, orig_export = lucid_mcp.fetch, lucid.export_png
    try:
        lucid_mcp.fetch = boom
        lucid.export_png = lambda doc_id: b"PNGBYTES"
        assert lucid.get_lucid("doc123") == ("image", b"PNGBYTES")
    finally:
        lucid_mcp.fetch, lucid.export_png = orig_mcp, orig_export


def test_lucid_mcp_helpers():
    assert lucid_mcp._doc_url("abc") == "https://lucid.app/lucidchart/abc/edit"
    schema = {"properties": {"url": {}, "documentId": {}}}
    assert lucid_mcp._build_args(schema, "abc") == {
        "url": "https://lucid.app/lucidchart/abc/edit", "documentId": "abc"
    }


def test_lucid_mcp_needs_refresh():
    now = 1_000_000.0
    fresh = {"obtained_at": now - 100, "tokens": {"expires_in": 3600}}
    expired = {"obtained_at": now - 3600, "tokens": {"expires_in": 3600}}
    assert lucid_mcp._needs_refresh(fresh, now) is False
    assert lucid_mcp._needs_refresh(expired, now) is True   # past expiry
    assert lucid_mcp._needs_refresh({}, now) is True        # never timestamped


def test_parse_verbosity():
    assert app.parse_verbosity("give me a summary of this") == "summary"
    assert app.parse_verbosity("describe in detail https://figma.com/x") == "detailed"
    assert app.parse_verbosity("a detailed breakdown please") == "detailed"
    assert app.parse_verbosity("https://lucid.app/lucidchart/x/edit") == "standard"
    assert app.parse_verbosity("") == "standard"


def test_thread_text():
    msgs = [
        {"user": "UBOT", "text": "*Summary:* a flow"},
        {"user": "U123", "text": "what if approval fails?"},
        {"user": "U123", "text": "   "},  # blank skipped
    ]
    assert app._thread_text(msgs, "UBOT") == (
        "Assistant: *Summary:* a flow\nUser: what if approval fails?"
    )


def test_recall_diagram_routes_figma():
    """_recall_diagram picks the diagram from a thread; figma path mocked."""
    orig = figma.get_figma_data
    try:
        figma.get_figma_data = lambda k, n: "OUTLINE"
        msgs = [{"text": "plain"}, {"text": "see https://figma.com/design/KEY/x?node-id=1-2"}]
        assert app._recall_diagram(msgs) == ("text", "OUTLINE", None)
        assert app._recall_diagram([{"text": "no diagram here"}]) is None
    finally:
        figma.get_figma_data = orig


def test_figma_mcp_helpers():
    assert figma_mcp._figma_url("ABC", "1:2") == "https://www.figma.com/design/ABC?node-id=1-2"
    # only declared params are filled
    schema = {"properties": {"url": {}, "nodeId": {}}}
    assert figma_mcp._build_args(schema, "ABC", "1:2") == {
        "url": "https://www.figma.com/design/ABC?node-id=1-2", "nodeId": "1:2"
    }
    # preference order wins over list order
    tools = [_types.SimpleNamespace(name="get_screenshot"),
             _types.SimpleNamespace(name="get_metadata")]
    assert figma_mcp._pick_tool(tools).name == "get_metadata"


def test_get_figma_data_falls_back_to_rest():
    """When MCP fetch fails, get_figma_data uses the REST API. No network."""
    def boom(*_):
        raise RuntimeError("mcp down")

    orig_mcp, orig_rest = figma_mcp.fetch, figma.fetch_figma_rest
    try:
        figma_mcp.fetch = boom
        figma.fetch_figma_rest = lambda fk, nid: {
            "document": {"name": "Root", "type": "CANVAS", "children": []}
        }
        assert "Root [CANVAS]" in figma.get_figma_data("KEY", "1:2")
    finally:
        figma_mcp.fetch, figma.fetch_figma_rest = orig_mcp, orig_rest


def test_describe_retries_on_5xx():
    """Retries transient 5xx then succeeds; gives up after 3 attempts. No network."""
    calls = {"n": 0}

    class FakeResp:
        text = "**ok**"

    def fail_twice_then_ok(model, contents):
        calls["n"] += 1
        if calls["n"] < 3:
            raise genai_errors.ServerError(503, {})
        return FakeResp()

    orig_gen, orig_time = app.gemini.models.generate_content, app.time
    try:
        app.time = _types.SimpleNamespace(sleep=lambda *_: None)  # no real waiting

        app.gemini.models.generate_content = fail_twice_then_ok
        assert app.describe_diagram(b"x", "image/png") == "*ok*"
        assert calls["n"] == 3  # 2 failures + 1 success

        calls["n"] = 0

        def always_fail(model, contents):
            calls["n"] += 1
            raise genai_errors.ServerError(503, {})

        app.gemini.models.generate_content = always_fail
        try:
            app.describe_diagram(b"x", "image/png")
            assert False, "should have re-raised after 3 attempts"
        except genai_errors.ServerError:
            pass
        assert calls["n"] == 3
    finally:
        app.gemini.models.generate_content, app.time = orig_gen, orig_time


if __name__ == "__main__":
    test_figma_key()
    test_lucidchart()
    test_supported_mime()
    test_slack_mrkdwn()
    test_extract_node_id()
    test_figma_outline()
    test_lucid_doc_id()
    test_lucid_get_lucid_falls_back_to_rest()
    test_lucid_mcp_helpers()
    test_lucid_mcp_needs_refresh()
    test_parse_verbosity()
    test_thread_text()
    test_recall_diagram_routes_figma()
    test_figma_mcp_helpers()
    test_get_figma_data_falls_back_to_rest()
    test_describe_retries_on_5xx()
    print("all intake + format + figma + mcp + retry checks passed")
