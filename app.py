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
- Account for EVERY node. Do not omit, merge, or invent nodes. If unsure of a label, transcribe it exactly as it appears (do not correct typos) and mark it [unclear].
- Trace every arrow/connection to its actual destination node by that node's name. Do not assume flow continues to the next-numbered step just because it is listed next.
- A numbered list does NOT imply sequential flow. For every step whose outgoing arrow does not go to the immediately following step, end its line with "(then go to Step N)". For a step with multiple outgoing arrows, describe it as a Decision instead.

Confidence flagging:
- Append " [unclear]" immediately after any label, number, or connection you are not fully confident about (blurry, ambiguous, or a likely typo). Reproduce the text as written; never silently correct or guess.
- Only flag genuine uncertainty. If the content is given to you as exact text (not read from an image), do not flag it.

Format the answer as Slack mrkdwn for a screen reader:
- First line: "*Summary:* <one sentence>"
- Then a numbered list of steps, one step per line, following the diagram's flow as closely as a linear list allows.
- Put each decision on its own line using exactly this notation: "Decision: <question>? -> <LABEL>: go to Step N | <LABEL>: go to Step M" using the real branch labels (YES/NO or the actual conditions).
- Then a "*Notes:*" section listing any labels, annotations, or swimlane groupings that did not fit the flow, and — if you marked anything [unclear] — a "Please double-check:" line listing those items so the reader knows what to verify. Omit the section entirely if there is nothing to note.
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

LUCID_PROMPT = (
    "You are an accessibility assistant helping a blind user understand a diagram "
    "from a Lucidchart document shared in their workplace Slack channel. Below is "
    "the document's structured content (shapes, text, and connections) pulled from "
    "the Lucid MCP. Use it to reconstruct the diagram's flow and describe it so it "
    "is fully understandable without seeing it.\n\n" + _ANALYSIS
)

QA_PROMPT = (
    "You are an accessibility assistant helping a blind user who has already "
    "received a structured description of a diagram shared in their workplace "
    "Slack thread. Answer their follow-up question using ONLY the diagram's actual "
    "content and the conversation so far. If the answer is not in the diagram, say "
    "so plainly. Be concise. Use Slack mrkdwn: single *asterisks* for bold, no "
    "headers, tables, or emoji."
)

# Style instructions appended to a describe prompt. Composable: verbosity (how
# much) + plain-language (reading level) are independent axes.
_VERBOSITY = {
    "summary": (
        "\n\nVERBOSITY: SUMMARY. Output only the *Summary:* line followed by a "
        "2-3 sentence plain-language overview of the diagram's purpose and main "
        "path. Do NOT include the numbered steps or the Notes section."
    ),
    "standard": "",
    "detailed": (
        "\n\nVERBOSITY: DETAILED. Be exhaustive: include every node, decision, "
        "label, and annotation, and add a brief clause explaining each step's "
        "purpose. Keep the same structure (Summary, numbered steps, decisions, "
        "Notes)."
    ),
}
_PLAIN = (
    "\n\nAUDIENCE: NON-TECHNICAL. Write for a non-engineer stakeholder. Avoid "
    "jargon and tool-specific terms; if a technical term is unavoidable, explain "
    "it in plain words. Keep the same structure."
)
_MERMAID = (
    "\n\nAlso append a Mermaid version of the diagram in a ```mermaid fenced code "
    "block: use `flowchart TD`, one node per box with its real label, and the real "
    "edges; label decision branches like -->|Yes| and -->|No|. Include only nodes "
    "and connections that actually exist in the diagram."
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

def parse_verbosity(text: str) -> str:
    """Pick a verbosity level from a keyword in the message. Default standard."""
    t = (text or "").lower()
    if re.search(r"\b(summary|summarize|summarise|brief|short|tl;?dr)\b", t):
        return "summary"
    if re.search(r"\b(detailed|detail|verbose|in[- ]?depth|thorough)\b", t):
        return "detailed"
    return "standard"


_PLAIN_RE = re.compile(
    r"\b(plain[- ]?(language|english|terms)|non[- ]?technical|layman'?s?|eli5|simple terms)\b",
    re.I,
)


def parse_plain_language(text: str) -> bool:
    """True if the message asks for a non-technical, plain-language description."""
    return bool(_PLAIN_RE.search(text or ""))


def parse_mermaid(text: str) -> bool:
    """True if the message asks for a Mermaid/code version of the diagram."""
    return bool(re.search(r"\b(mermaid|diagram code|flowchart code|as code)\b", (text or "").lower()))


def describe_style(text: str) -> str:
    """Combine verbosity + plain-language + mermaid into one prompt suffix."""
    style = _VERBOSITY.get(parse_verbosity(text), "")
    if parse_plain_language(text):
        style += _PLAIN
    if parse_mermaid(text):
        style += _MERMAID
    return style


def to_slack_mrkdwn(text: str) -> str:
    """Convert the markdown Gemini tends to emit into Slack mrkdwn.

    Splits on ``` fences and transforms only the prose segments, so code blocks
    (e.g. Mermaid '-->' edges) survive the arrow/bold rewrites untouched.
    """
    parts = text.split("```")
    for i in range(0, len(parts), 2):  # even indexes are outside code fences
        seg = re.sub(r"\*\*(.+?)\*\*", r"*\1*", parts[i])                 # **bold** -> *bold*
        seg = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", seg, flags=re.MULTILINE)  # # header -> *bold*
        parts[i] = seg.replace("->", "→")                                # arrow notation
    return "```".join(parts).strip()


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


def describe_diagram(image_bytes: bytes, mime_type: str, style: str = "") -> str:
    """Describe a diagram image/PDF via Gemini (image recognition path)."""
    return _generate([DESCRIBE_PROMPT + style, types.Part.from_bytes(data=image_bytes, mime_type=mime_type)])


def describe_figma(outline: str, style: str = "") -> str:
    """Describe a Figma file from its structured node outline (no screenshot)."""
    return _generate([FIGMA_PROMPT + style + "\n\nFigma node outline:\n" + outline])


def describe_lucid(content: str, style: str = "") -> str:
    """Describe a Lucidchart document from MCP content text (no screenshot)."""
    return _generate([LUCID_PROMPT + style + "\n\nLucid document content:\n" + content])


def answer_question(kind: str, payload, mime, conversation: str) -> str:
    """Answer a follow-up question about a recalled diagram (image or text)."""
    prompt = QA_PROMPT + "\n\nConversation so far:\n" + conversation
    if kind == "image":
        return _generate([prompt, types.Part.from_bytes(data=payload, mime_type=mime)])
    return _generate([prompt + "\n\nDiagram content:\n" + payload])


HELP_TEXT = (
    "Share a diagram and I'll describe it accessibly: upload a *PNG, JPG, or PDF*, "
    "or paste a *Figma* or *Lucidchart* link. Add *summary* or *detailed* to set "
    "how much detail, *plain language* for a non-technical version, *mermaid* for a "
    "re-editable code version, and reply in the thread to ask follow-ups."
)


# -- Shared intake routing ----------------------------------------------------

def route_diagram(event, say, set_status=None) -> bool:
    """Route a message to the right describer. Returns True if it responded.

    Shared by the channel handler and the assistant. set_status (assistant only)
    shows a 'thinking' indicator while Gemini/Figma work.
    """
    thread_ts = event.get("thread_ts") or event["ts"]
    text = event.get("text") or ""
    style = describe_style(text)

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
            description = describe_diagram(download_slack_file(url), mime, style)
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
            description = describe_figma(figma.get_figma_data(figma_key, figma.extract_node_id(text)), style)
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

    # Priority 3: Lucidchart link — Lucid MCP (text) with REST export (image) fallback
    if has_lucidchart(text):
        doc_id = lucid.extract_doc_id(text)
        if doc_id:
            try:
                if set_status:
                    set_status("Reading the Lucidchart document…")
                kind, payload = lucid.get_lucid(doc_id)
                description = (
                    describe_lucid(payload, style) if kind == "text"
                    else describe_diagram(payload, "image/png", style)
                )
                say(text=description, thread_ts=thread_ts)
                return True
            except Exception:
                pass  # fall through to manual-export instructions
        say(
            text=(
                "I couldn't read that Lucidchart diagram directly "
                "(it may be private, or Lucid access isn't set up).\n"
                "Please export it as a PNG or PDF (File → Export) and upload it here."
            ),
            thread_ts=thread_ts,
        )
        return True

    return False


# -- Follow-up Q&A in-thread (Phase 6) ----------------------------------------

_bot_user_id = None


def _get_bot_user_id(client) -> str:
    global _bot_user_id
    if _bot_user_id is None:
        _bot_user_id = client.auth_test()["user_id"]
    return _bot_user_id


def _recall_diagram(messages):
    """Re-derive the diagram from a thread's messages, in post order.
    ponytail: stateless re-fetch (no cache) — re-downloads/re-queries each
    follow-up; add a thread_ts->payload cache if calls get expensive."""
    for m in messages:
        for f in m.get("files") or []:
            mime = f.get("mimetype", "")
            url = f.get("url_private_download")
            if mime in SUPPORTED_MIME and url:
                return ("image", download_slack_file(url), mime)
        text = m.get("text") or ""
        key = extract_figma_key(text)
        if key:
            return ("text", figma.get_figma_data(key, figma.extract_node_id(text)), None)
        if has_lucidchart(text):
            doc_id = lucid.extract_doc_id(text)
            if doc_id:
                kind, payload = lucid.get_lucid(doc_id)
                return (kind, payload, "image/png" if kind == "image" else None)
    return None


def _thread_text(messages, bot_user_id) -> str:
    lines = []
    for m in messages:
        body = (m.get("text") or "").strip()
        if body:
            who = "Assistant" if m.get("user") == bot_user_id else "User"
            lines.append(f"{who}: {body}")
    return "\n".join(lines)


def handle_followup(event, say, client, set_status=None) -> bool:
    """Answer a plain-text follow-up in a thread where we described a diagram.
    Returns True if it responded. Stays silent unless the bot already posted in
    the thread AND a diagram can be recalled from it."""
    thread_ts = event.get("thread_ts")
    if not thread_ts or not (event.get("text") or "").strip():
        return False  # only thread replies can be follow-ups
    try:
        messages = client.conversations_replies(
            channel=event["channel"], ts=thread_ts, limit=50
        )["messages"]
    except Exception:
        return False
    bot_id = _get_bot_user_id(client)
    if not any(m.get("user") == bot_id for m in messages):
        return False  # we never described anything here -> not our thread
    diagram = _recall_diagram(messages)
    if not diagram:
        return False
    if set_status:
        set_status("Looking at the diagram…")
    kind, payload, mime = diagram
    say(text=answer_question(kind, payload, mime, _thread_text(messages, bot_id)),
        thread_ts=thread_ts)
    return True


# -- App Home tab (static help screen) ----------------------------------------

def _home_view() -> dict:
    def section(text):
        return {"type": "section", "text": {"type": "mrkdwn", "text": text}}

    return {
        "type": "home",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "PathFinder"}},
            section(
                "I turn diagrams into *accessible, structured text* for blind and "
                "low-vision teammates. Share a diagram in any channel I'm in (or here in "
                "the *Messages* tab) and I'll reply in-thread with a screen-reader "
                "friendly breakdown."
            ),
            {"type": "divider"},
            section(
                "*What I can read*\n"
                "• Image / PDF uploads (PNG, JPG, GIF, WebP, PDF)\n"
                "• *Figma* links — from real node/connector structure\n"
                "• *Lucidchart* links — from real document structure via the Lucid MCP"
            ),
            section(
                "*Each description includes*\n"
                "• A one-line summary\n"
                "• Numbered steps in flow order\n"
                "• Decision branches with explicit → notation\n"
                "• `[unclear]` flags on anything I'm unsure about, so you know what to verify"
            ),
            section(
                "*Options* — add a word to your message:\n"
                "• *summary* or *detailed* — how much detail\n"
                "• *plain language* — non-technical version for stakeholders\n"
                "• *mermaid* — also get a re-editable flowchart code block\n"
                "• Reply in the thread to ask *follow-up questions* about the diagram"
            ),
            {"type": "divider"},
            section(
                "_Built for the Slack Agent Builder Challenge — Agent for Good "
                "(accessibility track)._"
            ),
        ],
    }


@app.event("app_home_opened")
def publish_home(event, client):
    client.views_publish(user_id=event["user"], view=_home_view())


# -- Channel handler ----------------------------------------------------------

@app.event("message")
def handle_message(event, say, client):
    if event.get("bot_id"):  # ponytail: ignore our own messages, avoid loops
        return
    if route_diagram(event, say):
        return
    # Plain text: answer if it's a follow-up in a thread we described; else silent.
    handle_followup(event, say, client)


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
def assistant_message(event, say, set_status, client):
    if route_diagram(event, say, set_status=set_status):
        return
    if handle_followup(event, say, client, set_status=set_status):
        return
    say(HELP_TEXT)  # plain text, no diagram in thread -> guidance


app.assistant(assistant)


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
