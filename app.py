"""Slack diagram-accessibility bot.

Phase 1: event pipeline (done)
Phase 2: diagram intake — image download, Figma/Lucidchart URL detection
Phase 3+: Gemini description, accessible output, Figma MCP
"""
import os
import re

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

app = App(token=os.environ["SLACK_BOT_TOKEN"])

FIGMA_RE = re.compile(
    r"https://(?:www\.)?figma\.com/(file|design|make|board|proto|slides)/([A-Za-z0-9]+)"
)
LUCID_RE = re.compile(r"https://lucid\.app/")
SUPPORTED_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}


# -- Intake helpers -----------------------------------------------------------

def download_slack_file(url: str) -> bytes:
    """Download a private Slack file using the bot token."""
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def extract_figma_key(text: str) -> str | None:
    """Return the Figma file key from a URL in the text, or None."""
    m = FIGMA_RE.search(text)
    return m.group(2) if m else None


def has_lucidchart(text: str) -> bool:
    return bool(LUCID_RE.search(text))


# -- Event handler ------------------------------------------------------------

@app.event("message")
def handle_message(event, say):
    if event.get("bot_id"):  # ponytail: ignore our own messages, avoid loops
        return

    thread_ts = event.get("thread_ts") or event["ts"]
    text = event.get("text") or ""
    files = event.get("files") or []

    # Priority 1: image/PDF attachment
    for f in files:
        mime = f.get("mimetype", "")
        if mime not in SUPPORTED_MIME:
            say(
                text=f"Unsupported file type `{mime}`. Please upload a PNG, JPG, or PDF.",
                thread_ts=thread_ts,
            )
            continue

        url = f.get("url_private_download")
        if not url:
            say(text="Could not get a download URL for that file.", thread_ts=thread_ts)
            continue

        try:
            image_bytes = download_slack_file(url)
        except Exception as e:
            say(text=f"Failed to download `{f.get('name')}`: {e}", thread_ts=thread_ts)
            continue

        # ponytail: placeholder until Phase 3 wires up Gemini
        say(
            text=f"Downloaded *{f.get('name')}* ({len(image_bytes):,} bytes). Description coming in Phase 3.",
            thread_ts=thread_ts,
        )
        return

    # Priority 2: Figma link
    figma_key = extract_figma_key(text)
    if figma_key:
        # ponytail: placeholder until Phase 5 wires up Figma MCP
        say(
            text=f"Figma file detected (key: `{figma_key}`). Figma MCP integration coming in Phase 5.",
            thread_ts=thread_ts,
        )
        return

    # Priority 3: Lucidchart link
    if has_lucidchart(text):
        say(
            text=(
                "*Lucidchart diagrams cannot be read directly.*\n"
                "Please export your diagram as a PNG or PDF (File → Export) and re-upload it here."
            ),
            thread_ts=thread_ts,
        )
        return

    # Fallback: plain text (keep echo for debugging, remove in Phase 3)
    say(text=f"Echo: {text}", thread_ts=thread_ts)


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
