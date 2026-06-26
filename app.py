"""PathFinder — Slack assistant for diagram accessibility.

Phase 1: event pipeline (done)
Phase 2: diagram intake — image download, Figma/Lucidchart URL detection (done)
Phase 3-4: Gemini description + accessible Slack mrkdwn output (done)
Phase 5: Figma structured data — MCP primary (figma_mcp), REST fallback (figma)
"""
import os
import re
import time

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from slack_bolt import App, Assistant
from slack_bolt.adapter.socket_mode import SocketModeHandler

import figma
import lucid

load_dotenv()

app = App(token=os.environ["SLACK_BOT_TOKEN"])

gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

FIGMA_RE = re.compile(
    r"https://(?:www\.)?figma\.com/(file|design|make|board|proto|slides)/([A-Za-z0-9]+)"
)
LUCID_RE = re.compile(r"https://lucid\.app/")
SUPPORTED_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}

_ANALYSIS = """Extract:
(1) the overall purpose of the diagram in one sentence,
(2) all nodes or steps in logical order,
(3) all decision points and their branches with explicit yes/no or condition labels,
(4) the directional flow between steps,
(5) any labels, annotations, or swimlane groupings.

Do not describe colors or visual styling. Focus entirely on structure, logic, and content.

Accuracy rules (most important):
- Account for EVERY node. Do not omit, merge, or invent nodes. If unsure of a label, transcribe it as best you can and flag it in Notes.
- Trace every arrow/connection to its actual destination node by that node's name. Do not assume flow continues to the next-numbered step just because it is listed next.
- A numbered list does NOT imply sequential flow. For every step whose outgoing arrow does not go to the immediately following step, end its line with "(then go to Step N)". For a step with multiple outgoing arrows, describe it as a Decision instead.

Format the answer as Slack mrkdwn for a screen reader:
- First line: "*Summary:* <one sentence>"
- Then a numbered list of steps, one step per line, following the diagram's flow as closely as a linear list allows.
- Put each decision on its own line using exactly this notation: "Decision: <question>? -> <LABEL>: go to Step N | <LABEL>: go to Step M" using the real branch labels (YES/NO or the actual conditions).
- Then a "*Notes:*" section listing any labels, annotations, swimlane groupings, or uncertain transcriptions that did not fit the flow. Omit this section entirely if there are none.
- Use single asterisks for bold. Do NOT use markdown headers (#), tables, bullet characters, horizontal rules, or emoji.
"""

DESCRIBE_PROMPT = (
    "You are an accessibility assistant helping a blind user understand a diagram "
    "shared in their workplace Slack channel. Analyze this diagram and describe it "
    "so it is fully understandable without seeing it.\n\n" + _ANALYSIS
)

FIGMA_PROMPT = (
    "You are an accessibility assistant helping a blind user understand a diagram "
    "from a Figma file shared in their workplace Slack channel. Below is the "
    "structured node outline of that file (indented layer hierarchy with names, "
    "types, text, x/y positions and sizes, and connector endpoints). Use the "
    "positions and connectors to infer the diagram's flow and layout, and describe "
    "it so it is fully understandable without seeing it.\n\n" + _ANALYSIS
)


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


def _generate(contents) -> str:
    """Call Gemini and return Slack mrkdwn. Retries 5xx (e.g. 503 overload) with
    exponential backoff. Does NOT retry 4xx like 429 limit:0 — not transient.
    ponytail: 3 tries is plenty.
    """
    for attempt in range(3):
        try:
            resp = gemini.models.generate_content(model=GEMINI_MODEL, contents=contents)
            return to_slack_mrkdwn(resp.text)
        except genai_errors.ServerError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)  # 1s, then 2s


def describe_diagram(image_bytes: bytes, mime_type: str) -> str:
    """Describe a diagram image/PDF via Gemini (image recognition path)."""
    return _generate(
        [DESCRIBE_PROMPT, types.Part.from_bytes(data=image_bytes, mime_type=mime_type)]
    )


def describe_figma(outline: str) -> str:
    """Describe a Figma file from its structured node outline (no screenshot)."""
    return _generate([FIGMA_PROMPT + "\n\nFigma node outline:\n" + outline])


HELP_TEXT = (
    "Share a diagram and I'll describe it accessibly: upload a *PNG, JPG, or PDF*, "
    "or paste a *Figma* link. Lucidchart links must be exported as a PNG first."
)


# -- Shared intake routing ----------------------------------------------------

def route_diagram(event, say, set_status=None) -> bool:
    """Route a message to the right describer. Returns True if it responded.

    Shared by the channel handler and the assistant. set_status (assistant only)
    shows a 'thinking' indicator while Gemini/Figma work.
    """
    thread_ts = event.get("thread_ts") or event["ts"]
    text = event.get("text") or ""

    # Priority 1: image/PDF attachment
    for f in event.get("files") or []:
        mime = f.get("mimetype", "")
        if mime not in SUPPORTED_MIME:
            say(text=f"Unsupported file type `{mime}`. Please upload a PNG, JPG, or PDF.", thread_ts=thread_ts)
            return True
        url = f.get("url_private_download")
        if not url:
            say(text="Could not get a download URL for that file.", thread_ts=thread_ts)
            return True
        try:
            if set_status:
                set_status("Analyzing your diagram…")
            description = describe_diagram(download_slack_file(url), mime)
        except Exception as e:
            say(text=f"Sorry, I couldn't analyze *{f.get('name')}*: {e}", thread_ts=thread_ts)
            return True
        say(text=description, thread_ts=thread_ts)
        return True

    # Priority 2: Figma link — structured node data (MCP, REST fallback)
    figma_key = extract_figma_key(text)
    if figma_key:
        try:
            if set_status:
                set_status("Reading the Figma file…")
            description = describe_figma(figma.get_figma_data(figma_key, figma.extract_node_id(text)))
        except Exception as e:
            say(
                text=(
                    f"Sorry, I couldn't read that Figma file: {e}\n"
                    "Make sure the file is accessible and `FIGMA_TOKEN` is set, "
                    "or export the frame as a PNG and upload it here instead."
                ),
                thread_ts=thread_ts,
            )
            return True
        say(text=description, thread_ts=thread_ts)
        return True

    # Priority 3: Lucidchart link — try direct export, else instruct manual export
    if has_lucidchart(text):
        doc_id = lucid.extract_doc_id(text)
        if doc_id and os.environ.get("LUCID_API_TOKEN"):
            try:
                if set_status:
                    set_status("Exporting the Lucidchart diagram…")
                description = describe_diagram(lucid.export_png(doc_id), "image/png")
                say(text=description, thread_ts=thread_ts)
                return True
            except Exception:
                pass  # fall through to manual-export instructions
        say(
            text=(
                "I couldn't read that Lucidchart diagram directly "
                "(it may be private, or the Lucid token is missing/expired).\n"
                "Please export it as a PNG or PDF (File → Export) and upload it here."
            ),
            thread_ts=thread_ts,
        )
        return True

    return False


# -- Channel handler ----------------------------------------------------------

@app.event("message")
def handle_message(event, say):
    if event.get("bot_id"):  # ponytail: ignore our own messages, avoid loops
        return
    # Stay silent on plain text in channels so the bot doesn't spam.
    route_diagram(event, say)


# -- Assistant (Slack AI app) -------------------------------------------------

assistant = Assistant()


@assistant.thread_started
def assistant_started(say, set_suggested_prompts):
    say(
        "Hi! I make diagrams accessible for blind and low-vision teammates. "
        "Upload an image or PDF of a diagram, or paste a Figma link, and I'll "
        "describe its purpose, steps, and decision branches in plain text."
    )
    set_suggested_prompts(
        prompts=[
            {"title": "How do I use this?", "message": "How do I use this assistant?"},
            {"title": "What can you read?", "message": "What kinds of diagrams can you describe?"},
        ]
    )


@assistant.user_message
def assistant_message(event, say, set_status):
    if not route_diagram(event, say, set_status=set_status):
        say(HELP_TEXT)  # plain text in the assistant pane -> guidance


app.assistant(assistant)


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
