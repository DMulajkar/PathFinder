"""Slack diagram-accessibility bot.

Phase 1: event pipeline (done)
Phase 2: diagram intake — image download, Figma/Lucidchart URL detection (done)
Phase 3-4: Gemini description + accessible Slack mrkdwn output (done)
Phase 5: Figma MCP
"""
import os
import re

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

app = App(token=os.environ["SLACK_BOT_TOKEN"])

gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

FIGMA_RE = re.compile(
    r"https://(?:www\.)?figma\.com/(file|design|make|board|proto|slides)/([A-Za-z0-9]+)"
)
LUCID_RE = re.compile(r"https://lucid\.app/")
SUPPORTED_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}

DESCRIBE_PROMPT = """You are an accessibility assistant helping a blind user understand a diagram shared in their workplace Slack channel. Analyze this diagram and describe it so it is fully understandable without seeing it.

Extract:
(1) the overall purpose of the diagram in one sentence,
(2) all nodes or steps in logical order,
(3) all decision points and their branches with explicit yes/no or condition labels,
(4) the directional flow between steps,
(5) any labels, annotations, or swimlane groupings.

Do not describe colors or visual styling. Focus entirely on structure, logic, and content.

Accuracy rules (most important):
- Account for EVERY box/node in the diagram. Do not omit, merge, or invent nodes. If unsure of a label, transcribe it as best you can and flag it in Notes.
- Trace every arrow to its actual destination box by that box's name. Do not assume flow continues to the next-numbered step just because it is listed next.
- A numbered list does NOT imply sequential flow. For every step whose outgoing arrow does not go to the immediately following step, end its line with "(then go to Step N)". For a step with multiple outgoing arrows, describe it as a Decision instead.

Format the answer as Slack mrkdwn for a screen reader:
- First line: "*Summary:* <one sentence>"
- Then a numbered list of steps, one step per line, following the diagram's flow as closely as a linear list allows.
- Put each decision on its own line using exactly this notation: "Decision: <question>? -> <LABEL>: go to Step N | <LABEL>: go to Step M" using the real branch labels (YES/NO or the actual conditions).
- Then a "*Notes:*" section listing any labels, annotations, swimlane groupings, or uncertain transcriptions that did not fit the flow. Omit this section entirely if there are none.
- Use single asterisks for bold. Do NOT use markdown headers (#), tables, bullet characters, horizontal rules, or emoji.
"""


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


# -- Description layer (Phase 3-4) --------------------------------------------

def to_slack_mrkdwn(text: str) -> str:
    """Convert the markdown Gemini tends to emit into Slack mrkdwn."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)                       # **bold** -> *bold*
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.MULTILINE)  # # header -> *bold*
    text = text.replace("->", "→")                                       # arrow notation
    return text.strip()


def describe_diagram(image_bytes: bytes, mime_type: str) -> str:
    """Send a diagram image/PDF to Gemini, return accessible Slack mrkdwn."""
    resp = gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            DESCRIBE_PROMPT,
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
        ],
    )
    return to_slack_mrkdwn(resp.text)


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

        try:
            description = describe_diagram(image_bytes, mime)
        except Exception as e:
            say(text=f"Sorry, I couldn't analyze *{f.get('name')}*: {e}", thread_ts=thread_ts)
            return
        say(text=description, thread_ts=thread_ts)
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

    # No diagram, no recognized link: stay silent so the bot doesn't spam channels.
    # ponytail: add a help reply on @-mention/DM only if users ask for one.


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
