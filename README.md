# PathFinder

A Slack assistant that turns diagrams into accessible, structured text descriptions for
blind and low-vision users. Built for the Slack Agent Builder Challenge
("Agent for Good" accessibility track).

A sighted teammate uploads a diagram (or pastes a Figma / Lucidchart link) in Slack, and
PathFinder replies **in-thread** with a screen-reader-friendly breakdown for blind and
low-vision teammates: a one-line summary, numbered steps in flow order, decision branches
with explicit `→` notation, and a Notes section.

It works both in **channels** and as a dedicated **Slack AI assistant** (the side pane,
with a greeting and suggested prompts).

## Features

- **Image / PDF attachments** (PNG, JPG, GIF, WebP, PDF) — described via Gemini vision.
- **Figma links** — described from real node/connector structure (Figma MCP attempted,
  REST API fallback), more accurate than reading a screenshot.
- **Lucidchart links** — described from real document structure via the **Lucid MCP**
  (works), with a REST PNG-export fallback.
- **Follow-up Q&A in-thread** — reply in the thread to ask questions about the same
  diagram ("what happens if approval fails?"); answers come from the diagram's content.
- **Output options** — add a keyword to the message:
  - `summary` / `brief` — just the summary + a short overview
  - `detailed` / `verbose` — exhaustive, every node and its purpose
  - `plain language` / `non-technical` — for non-engineer stakeholders
  - `mermaid` / `diagram code` — also emit a re-editable Mermaid flowchart code block
  - (options compose, e.g. `detailed plain language mermaid`)

## How it works

```
Slack message ──► app.py (Socket Mode handler / Assistant)
                   ├─ image/PDF ─► download (Slack Files API) ─► Gemini ─► reply
                   ├─ figma.com ─► figma.get_figma_data ─► Gemini ─► reply
                   │                 ├─ figma_mcp.fetch  (MCP, OAuth)   ← tried first (blocked)
                   │                 └─ figma REST API   (FIGMA_TOKEN)  ← fallback (works)
                   ├─ lucid.app ─► lucid.get_lucid ─► Gemini ─► reply
                   │                 ├─ lucid_mcp.fetch  (MCP, OAuth)   ← tried first (works)
                   │                 └─ lucid REST export (LUCID_API_TOKEN) ← fallback
                   └─ thread reply ─► recall the thread's diagram ─► Gemini ─► answer
```

Socket Mode is used, so no public URL or webhook server is needed.

## Setup

1. **Install Python 3.11+** from python.org (the Windows Store stub won't run pip reliably).
2. **Create the Slack app** at <https://api.slack.com/apps> → *Create New App* →
   *From a manifest* → paste `manifest.json`. This enables the assistant, the Messages
   tab, and all scopes/events.
3. **Slack tokens:**
   - *OAuth & Permissions* → Install to workspace → copy the **Bot User OAuth Token** (`xoxb-`).
   - *Basic Information* → App-Level Tokens → generate one with `connections:write` (`xapp-`).
4. **Gemini key:** create one at <https://aistudio.google.com/apikey>
   (use *Create API key in new project* — free tier).
5. **Figma token (optional, for the REST fallback):** Figma → *Settings → Security →
   Personal access tokens* → generate with **File content: Read-only**.
6. `copy .env.example .env` and fill it in (see below).
7. Install deps and run:
   ```
   py -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   .venv\Scripts\python app.py
   ```
   Use `py` / `.venv\Scripts\python` — the Windows Store `python` stub doesn't work.
8. **Authorize the Lucid MCP (optional, one-time)** so Lucidchart links are read directly:
   ```
   .venv\Scripts\python lucid_mcp.py
   ```
   A browser opens; sign in and click **Allow**. Tokens are cached to
   `.lucid_mcp_tokens.json` (gitignored) and auto-refreshed — no need to repeat.

## Environment (`.env`)

| Var | Required | Purpose |
|-----|----------|---------|
| `SLACK_BOT_TOKEN` | yes | Bot identity / API calls (`xoxb-`) |
| `SLACK_APP_TOKEN` | yes | Socket Mode connection (`xapp-`) |
| `GEMINI_API_KEY` | yes | Diagram description |
| `GEMINI_MODEL` | no | Override model (default `gemini-2.5-flash`; `gemini-2.0-flash` has no free quota) |
| `FIGMA_TOKEN` | for Figma | Figma REST fallback (`figd_...`) |
| `LUCID_API_TOKEN` | no | Lucid REST fallback (API key or OAuth token); only used if the Lucid MCP isn't authorized |

## Lucid MCP (working primary)

Lucidchart links are read from real document structure via the remote **Lucid MCP**
(`https://mcp.lucid.app/mcp`). Unlike Figma's MCP, Lucid's allows **open dynamic client
registration**, so PathFinder can authorize itself — run `python lucid_mcp.py` once. The
MCP's `fetch` tool returns structured content (pages → diagram elements with labels, shape
types, positions), which Gemini turns into the accessible description. If the MCP isn't
authorized, the bot falls back to a REST PNG export (`LUCID_API_TOKEN`), then to a
"export as PNG" message.

> Token note: the SDK doesn't persist token expiry, so PathFinder records the issue time
> and proactively refreshes via the `refresh_token` grant before each call.

## Figma MCP (attempted primary — blocked by Figma)

The bot prefers the remote Figma MCP over REST, but **Figma's remote MCP is allowlist-only**:
*"Only clients listed in the Figma MCP Catalog can connect."* Self-registration returns
`403`, so a custom bot can't authorize. `figma_mcp.py` holds the (correct) OAuth/MCP client
for the day this client is catalog-listed; until then it short-circuits and **Figma links
use the REST API** (`FIGMA_TOKEN`), which returns the same structured data.

> Note: Figma/Lucid access is scoped to the **single account** whose credentials/MCP
> authorization are configured. PathFinder reads diagrams that account can see — which fits
> the use case: a shared team/service account makes all team diagrams accessible to blind
> teammates. Image/PDF uploads have no such limit.

## Run the tests

```
.venv\Scripts\python test_intake.py
```

Covers URL/mimetype detection, node-id parsing, Figma/Lucid outline handling, verbosity /
plain-language / mermaid option parsing, fence-safe mrkdwn (Mermaid `-->` survives),
follow-up thread recall, MCP helpers and token-refresh logic, the MCP→REST fallbacks, and
the Gemini 5xx retry. No network or live keys needed.

## Verify end-to-end

- **Plain text in a channel** → bot stays silent (no spam).
- **PNG/JPG/PDF** → threaded reply with a structured, accessible description.
- **Figma link** (to a file your account can access) → threaded reply from structured data.
- **Lucidchart link** → threaded reply from the Lucid MCP (after `python lucid_mcp.py`).
- **Reply in the thread** with a question → answer about that diagram.
- **Add `summary` / `detailed` / `plain language` / `mermaid`** → output adapts.
- **Assistant pane** → open PathFinder from the sidebar; greeting + suggested prompts; all
  of the above work there too, with a "thinking" status while it processes.
