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
from slack_bolt.oauth.oauth_settings import OAuthSettings
from slack_sdk.oauth.installation_store import FileInstallationStore
from slack_sdk.oauth.state_store import FileOAuthStateStore

import figma
import lucid
import lucid_mcp
import visio

load_dotenv()

BOT_SCOPES = [
    "app_mentions:read", "assistant:write", "chat:write",
    "channels:history", "groups:history", "im:history", "files:read",
]

# Distributed (multi-workspace) when SLACK_CLIENT_ID is set: per-install tokens
# come from the file install store, so anyone can add PathFinder via the
# "Add to Slack" link. Without it, fall back to the single-workspace dev token.
# ponytail: file stores (no DB) are plenty for a challenge-scale app.
_DISTRIBUTED = bool(os.environ.get("SLACK_CLIENT_ID"))
if _DISTRIBUTED:
    app = App(
        signing_secret=os.environ["SLACK_SIGNING_SECRET"],
        oauth_settings=OAuthSettings(
            client_id=os.environ["SLACK_CLIENT_ID"],
            client_secret=os.environ["SLACK_CLIENT_SECRET"],
            scopes=BOT_SCOPES,
            installation_store=FileInstallationStore(base_dir="./data/installations"),
            state_store=FileOAuthStateStore(expiration_seconds=600, base_dir="./data/states"),
        ),
    )
else:
    app = App(token=os.environ["SLACK_BOT_TOKEN"])

# Slack user ID allowed to connect Lucid (one-time, shared for everyone).
# Unset = anyone may connect. ponytail: a single ID is the whole access policy.
LUCID_ADMIN = os.environ.get("LUCID_ADMIN_USER", "")

# Public base URL of this server (set in prod). With it, Lucid OAuth redirects
# the code straight to /lucid/callback; without it, fall back to manual paste.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
LUCID_REDIRECT = f"{PUBLIC_BASE_URL}/lucid/callback" if PUBLIC_BASE_URL else lucid_mcp.OOB_REDIRECT

gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

FIGMA_RE = re.compile(
    r"https://(?:www\.)?figma\.com/(file|design|make|board|proto|slides)/([A-Za-z0-9]+)"
)
LUCID_RE = re.compile(r"https://lucid\.app/")
SUPPORTED_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}

# Daily diagram quota, per workspace. Vision tokens are the cost driver, so cap
# how many diagrams a workspace can run per day.
# ponytail: in-memory counter — resets on restart and at date rollover. One
# Socket Mode process, so no shared store needed; per-team so one busy workspace
# can't drain another's. Swap to a DB if you ever run multiple instances.
DAILY_DIAGRAM_QUOTA = int(os.environ.get("DAILY_DIAGRAM_QUOTA", "5"))
_quota: dict[str, list] = {}  # team_id -> [date_str, count]


def _quota_ok(team: str) -> bool:
    """True (and consume one) if the team is under today's quota, else False."""
    today = time.strftime("%Y-%m-%d")
    day, n = _quota.get(team, [today, 0])
    if day != today:
        day, n = today, 0
    if n >= DAILY_DIAGRAM_QUOTA:
        _quota[team] = [day, n]
        return False
    _quota[team] = [day, n + 1]
    return True


def _diagram_intent(event, text: str) -> bool:
    """Will this message actually run a (costly) describe? Gate quota only on
    these — not on wrong file types or non-diagram chatter."""
    for f in event.get("files") or []:
        if _is_visio(f) or f.get("mimetype") in SUPPORTED_MIME:
            return True
    return bool(extract_figma_key(text)) or has_lucidchart(text)

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

VISIO_PROMPT = (
    "You are an accessibility assistant helping a blind user understand a diagram "
    "from a Microsoft Visio (.vsdx) file shared in their workplace Slack channel. "
    "Below is the file's structured content (each page's shapes and the directed "
    "connections between them, with any connector labels) parsed from the .vsdx. "
    "Use the connections to reconstruct the diagram's flow and describe it so it is "
    "fully understandable without seeing it.\n\n" + _ANALYSIS
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

def download_slack_file(url: str, token: str) -> bytes:
    """Download a private Slack file using the installing workspace's bot token."""
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
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


def _is_visio(f: dict) -> bool:
    """True if a Slack file is a Visio drawing (.vsdx). Slack sets filetype=vsdx;
    fall back to the name extension if the mimetype came through as octet-stream."""
    return (f.get("filetype") == "vsdx"
            or (f.get("name") or "").lower().endswith(".vsdx"))


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


NOT_DIAGRAM = "NOT_A_DIAGRAM"
_GATE = (
    "\n\nFIRST decide whether this image is a flowchart, process/sequence diagram, "
    "or similar node-and-arrow structured diagram. If it is NOT (e.g. a photo, "
    "UI screenshot, chart/graph, logo, meme, or plain text), reply with exactly "
    "NOT_A_DIAGRAM and nothing else — no explanation."
)


def describe_diagram(image_bytes: bytes, mime_type: str, style: str = "", gate: bool = False) -> str:
    """Describe a diagram image/PDF via Gemini (image recognition path).

    gate=True (direct uploads) makes Gemini return NOT_DIAGRAM for non-flowcharts
    so the caller can stay silent; off for Figma/Lucid image fallback (explicit intent).
    """
    prompt = DESCRIBE_PROMPT + style + (_GATE if gate else "")
    return _generate([prompt, types.Part.from_bytes(data=image_bytes, mime_type=mime_type)])


def describe_figma(outline: str, style: str = "") -> str:
    """Describe a Figma file from its structured node outline (no screenshot)."""
    return _generate([FIGMA_PROMPT + style + "\n\nFigma node outline:\n" + outline])


def describe_lucid(content: str, style: str = "") -> str:
    """Describe a Lucidchart document from MCP content text (no screenshot)."""
    return _generate([LUCID_PROMPT + style + "\n\nLucid document content:\n" + content])


def describe_visio(outline: str, style: str = "") -> str:
    """Describe a Visio file from its parsed shape/connection outline (no screenshot)."""
    return _generate([VISIO_PROMPT + style + "\n\nVisio document content:\n" + outline])


def answer_question(description: str, conversation: str) -> str:
    """Answer a follow-up from the description the bot already posted — no image
    re-send. The structured description has the nodes/branches/flow that
    follow-ups ask about; re-analyzing the image would just repay vision tokens."""
    return _generate([
        QA_PROMPT
        + "\n\nDiagram description:\n" + description
        + "\n\nConversation so far:\n" + conversation
    ])


HELP_TEXT = (
    "Share a diagram and I'll describe it accessibly: upload a *PNG, JPG, or PDF*, "
    "or paste a *Figma* or *Lucidchart* link. Add *summary* or *detailed* to set "
    "how much detail, *plain language* for a non-technical version, *mermaid* for a "
    "re-editable code version, and reply in the thread to ask follow-ups."
)


# -- Shared intake routing ----------------------------------------------------

def route_diagram(event, say, client, set_status=None) -> bool:
    """Route a message to the right describer. Returns True if it responded.

    Shared by the channel handler and the assistant. client carries the
    installing workspace's token (for file downloads); set_status (assistant
    only) shows a 'thinking' indicator while Gemini/Figma work.
    """
    thread_ts = event.get("thread_ts") or event["ts"]
    text = event.get("text") or ""
    style = describe_style(text)

    if _diagram_intent(event, text) and not _quota_ok(event.get("team", "")):
        say(text="Workplace quota hit, try again tomorrow.", thread_ts=thread_ts)
        return True

    # Priority 1: file attachment
    for f in event.get("files") or []:
        # Visio .vsdx: parse structure locally (zip of XML), no image/mime path.
        if _is_visio(f):
            url = f.get("url_private_download")
            if not url:
                say(text="Could not get a download URL for that file.", thread_ts=thread_ts)
                return True
            try:
                if set_status:
                    set_status("Reading the Visio file…")
                description = describe_visio(
                    visio.get_visio(download_slack_file(url, client.token)), style
                )
            except Exception as e:
                say(text=f"Sorry, I couldn't read *{f.get('name')}*: {e}", thread_ts=thread_ts)
                return True
            say(text=description, thread_ts=thread_ts)
            return True

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
            description = describe_diagram(download_slack_file(url, client.token), mime, style, gate=True)
        except Exception as e:
            say(text=f"Sorry, I couldn't analyze *{f.get('name')}*: {e}", thread_ts=thread_ts)
            return True
        if NOT_DIAGRAM in description:  # not a flowchart
            # In the assistant pane the 'thinking' status only clears when we post,
            # so a silent return leaves it spinning forever (looks like a stuck loop).
            # Reply there; stay silent in channels (no status to clear, no spam).
            if set_status:
                say(text=(
                    "That doesn't look like a diagram I can break down. Upload a "
                    "flowchart or process diagram, or paste a Figma/Lucidchart link, "
                    "and I'll describe it."
                ), thread_ts=thread_ts)
            return True
        say(text=description, thread_ts=thread_ts)
        return True

    # Priority 2: Figma link — structured node data (MCP, REST fallback)
    figma_key = extract_figma_key(text)
    if figma_key:
        try:
            if set_status:
                set_status("Reading the Figma file…")
            kind, payload = figma.get_figma_data(figma_key, figma.extract_node_id(text))
            description = (
                describe_figma(payload, style) if kind == "text"
                else describe_diagram(payload, "image/png", style)
            )
        except Exception as e:
            if "429" in str(e):
                msg = (
                    "Figma is rate-limiting its API for this token (429), and its free "
                    "quota can take a while to reset. Please export the frame as a PNG and "
                    "upload it here — that path has no Figma limit — or try the link later."
                )
            else:
                msg = (
                    f"Sorry, I couldn't read that Figma file: {e}\n"
                    "Make sure the file is accessible and `FIGMA_TOKEN` is set, "
                    "or export the frame as a PNG and upload it here instead."
                )
            say(text=msg, thread_ts=thread_ts)
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
            except Exception as e:
                print(f"[lucid] get_lucid/describe failed for {doc_id}: {e!r}", flush=True)
                # fall through to manual-export instructions
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

_bot_user_ids: dict[str, str] = {}  # per-workspace: the bot's user_id differs by install


def _get_bot_user_id(client) -> str:
    tok = client.token
    if tok not in _bot_user_ids:
        _bot_user_ids[tok] = client.auth_test()["user_id"]
    return _bot_user_ids[tok]


def _diagram_description(messages, bot_id) -> str | None:
    """The bot's original description in a thread (its first substantive post),
    or None if it never described a diagram here — so we stay silent in threads
    that aren't ours. The description is what follow-ups are answered from."""
    has_source = any(_diagram_intent(m, m.get("text") or "") for m in messages)
    if not has_source:
        return None
    for m in messages:
        if m.get("user") == bot_id and (m.get("text") or "").strip():
            return m["text"]
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
    Returns True if it responded. Answers from the description the bot already
    posted — no image re-download or re-analysis (and so it's not quota-gated)."""
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
    description = _diagram_description(messages, bot_id)
    if not description:
        return False  # not a thread where we described a diagram
    if set_status:
        set_status("Looking at the diagram…")
    say(text=answer_question(description, _thread_text(messages, bot_id)),
        thread_ts=thread_ts)
    return True


# -- App Home tab (static help screen) ----------------------------------------

def _home_view(user_id: str = "") -> dict:
    def section(text):
        return {"type": "section", "text": {"type": "mrkdwn", "text": text}}

    if lucid_mcp.is_connected():
        lucid_blocks = [section("*Lucidchart:* ✓ connected — Lucid links work for everyone.")]
    elif not LUCID_ADMIN or user_id == LUCID_ADMIN:
        lucid_blocks = [
            section(
                "*Lucidchart:* not connected. One person connects once here, then "
                "everyone's Lucid links work — no terminal, no setup."
            ),
            {"type": "actions", "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "Connect Lucidchart"},
                "style": "primary",
                "action_id": "connect_lucid",
            }]},
        ]
    else:
        lucid_blocks = [section("*Lucidchart:* not connected — ask a workspace admin to connect it.")]

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
            *lucid_blocks,
            {"type": "divider"},
            section(
                "_Built for the Slack Agent Builder Challenge — Agent for Good "
                "(accessibility track)._"
            ),
        ],
    }


@app.event("app_home_opened")
def publish_home(event, client):
    client.views_publish(user_id=event["user"], view=_home_view(event["user"]))


@app.action("connect_lucid")
def connect_lucid(ack, body, client):
    ack()
    if LUCID_ADMIN and body["user"]["id"] != LUCID_ADMIN:
        return
    url = lucid_mcp.build_auth_url(LUCID_REDIRECT)
    if PUBLIC_BASE_URL:  # real callback: click Allow -> auto-redirect -> done
        view = {
            "type": "modal",
            "title": {"type": "plain_text", "text": "Connect Lucidchart"},
            "close": {"type": "plain_text", "text": "Done"},
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": (
                    f"<{url}|Click here to authorize Lucidchart>, then click *Allow*.\n\n"
                    "You'll see a confirmation page — that's it. Lucid links will work "
                    "for everyone in this workspace."
                )}},
            ],
        }
    else:  # OOB fallback (local dev, no public URL): manual code paste
        view = {
            "type": "modal",
            "callback_id": "lucid_code",
            "title": {"type": "plain_text", "text": "Connect Lucidchart"},
            "submit": {"type": "plain_text", "text": "Connect"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": (
                    f"1. <{url}|Open Lucid authorization> and click *Allow*.\n"
                    "2. Copy the code Lucid shows you.\n"
                    "3. Paste it below and hit *Connect*."
                )}},
                {"type": "input", "block_id": "code",
                 "label": {"type": "plain_text", "text": "Authorization code"},
                 "element": {"type": "plain_text_input", "action_id": "value"}},
            ],
        }
    client.views_open(trigger_id=body["trigger_id"], view=view)


@app.view("lucid_code")
def lucid_code_submit(ack, body, view, client):
    code = view["state"]["values"]["code"]["value"]["value"]
    try:
        lucid_mcp.exchange_code(code)
    except Exception as e:  # show the failure in the modal, don't close it
        ack(response_action="errors", errors={"code": f"Couldn't connect: {e}"})
        return
    ack()
    client.views_publish(user_id=body["user"]["id"], view=_home_view(body["user"]["id"]))


# -- Channel handler ----------------------------------------------------------

@app.event("message")
def handle_message(event, say, client):
    if event.get("bot_id"):  # ponytail: ignore our own messages, avoid loops
        return
    if route_diagram(event, say, client):
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
    if route_diagram(event, say, client, set_status=set_status):
        return
    if handle_followup(event, say, client, set_status=set_status):
        return
    say(HELP_TEXT)  # plain text, no diagram in thread -> guidance


app.assistant(assistant)


if __name__ == "__main__":
    if not _DISTRIBUTED:
        SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
    else:
        # Events (incl. all installed workspaces) over Socket Mode in a thread;
        # the two OAuth GET endpoints over HTTP for Caddy to proxy (HTTPS:443 -> :3000).
        import threading

        from flask import Flask, request
        from slack_bolt.adapter.flask import SlackRequestHandler

        threading.Thread(
            target=lambda: SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start(),
            daemon=True,
        ).start()

        flask_app = Flask(__name__)
        # Behind Caddy (TLS terminator): trust X-Forwarded-Proto/Host so Bolt sees
        # the request as https and builds an https OAuth redirect Slack accepts.
        from werkzeug.middleware.proxy_fix import ProxyFix
        flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_proto=1, x_host=1)
        bolt_handler = SlackRequestHandler(app)

        @flask_app.route("/slack/install", methods=["GET"])
        def slack_install():
            return bolt_handler.handle(request)

        @flask_app.route("/slack/oauth_redirect", methods=["GET"])
        def slack_oauth_redirect():
            return bolt_handler.handle(request)

        @flask_app.route("/lucid/callback", methods=["GET"])
        def lucid_callback():
            code = request.args.get("code")
            if not code:
                return "Missing authorization code.", 400
            try:
                lucid_mcp.exchange_code(code)
            except Exception as e:
                return f"<h1>Couldn't connect Lucidchart: {e}</h1>", 500
            return "<h1>Lucidchart connected. You can close this tab and return to Slack.</h1>"

        flask_app.run(host="127.0.0.1", port=3000)
