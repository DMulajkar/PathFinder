"""Phase 2 intake checks. Run: .venv\\Scripts\\python test_intake.py

Tests the pure URL/mimetype logic. Network download is not exercised here.
"""
from app import extract_figma_key, has_lucidchart, to_slack_mrkdwn, SUPPORTED_MIME


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


if __name__ == "__main__":
    test_figma_key()
    test_lucidchart()
    test_supported_mime()
    test_slack_mrkdwn()
    print("all intake + format checks passed")
