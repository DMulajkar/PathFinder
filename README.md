# Diagram Describer

A Slack bot that turns diagrams into accessible, structured text descriptions for
blind and low-vision users. Built for the Slack Agent Builder Challenge
("Agent for Good" accessibility track).

Post a diagram in Slack and the bot replies in-thread with a screen-reader-friendly
breakdown: a one-line summary, numbered steps in flow order, decision branches with
explicit `→` notation, and a Notes section. It handles:

- **Image / PDF attachments** (PNG, JPG, PDF) — described via Gemini vision.
- **Figma links** — described from real node/connector structure (Figma MCP, with
  REST API fallback), which is more accurate than reading a screenshot.
- **Lucidchart links** — returns export instructions (not directly readable).

## How it works

```
Slack message ──► app.py (Socket Mode handler)
                   ├─ image/PDF ─► download (Slack Files API) ─► Gemini ─► reply
                   ├─ figma.com ─► figma.get_figma_data ─► Gemini ─► reply
                   │                 ├─ figma_mcp.fetch  (MCP, OAuth)   ← tried first
                   │                 └─ figma REST API   (FIGMA_TOKEN)  ← fallback
                   └─ lucid.app ─► "export as PNG" message
```

## Setup

1. **Install Python 3.11+** from python.org (the Windows Store stub won't run pip reliably).
2. **Create the Slack app** at <https://api.slack.com/apps> → *Create New App* →
   *From a manifest* → paste `manifest.json`.
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

## Environment (`.env`)

| Var | Required | Purpose |
|-----|----------|---------|
| `SLACK_BOT_TOKEN` | yes | Bot identity / API calls (`xoxb-`) |
| `SLACK_APP_TOKEN` | yes | Socket Mode connection (`xapp-`) |
| `GEMINI_API_KEY` | yes | Diagram description |
| `GEMINI_MODEL` | no | Override model (default `gemini-2.5-flash`; `gemini-2.0-flash` has no free quota) |
| `FIGMA_TOKEN` | for Figma REST | Figma REST fallback (`figd_...`) |

## Figma MCP (attempted primary — blocked by Figma)

The bot is wired to prefer the remote Figma MCP over REST, but **Figma's remote MCP is
allowlist-only**: *"Only clients listed in the Figma MCP Catalog can connect."*
Self-registration returns `403`, so a custom bot can't authorize. `figma_mcp.py` holds
the (correct) OAuth/MCP client for the day this client is catalog-listed; until then it
short-circuits and **Figma links use the REST API** (`FIGMA_TOKEN`), which returns the
same structured node/connector data. No action needed — REST is the working path.

> Note: Figma access (both MCP and REST) is scoped to the **single account** whose
> credentials are in `.env`. The bot can only read Figma files that account can see.
> Image/PDF uploads have no such limit. Per-user Figma auth is a backlog item.

## Run the tests

```
.venv\Scripts\python test_intake.py
```

Covers URL/mimetype detection, node-id parsing, Figma outline flattening, MCP helper
logic, the MCP→REST fallback, and the Gemini 5xx retry. No network or live keys needed.

## Verify end-to-end

- **Text** → bot stays silent (no spam; it only responds to diagrams/links).
- **PNG/JPG/PDF** → threaded reply with a structured, accessible description.
- **Figma link** (`/design`, `/file`, `/board`, …) to a file your account can access →
  threaded reply describing the flow from structured data.
- **Lucidchart link** → reply asking you to export as PNG/PDF.

Socket Mode is used, so no public URL or webhook server is needed.
