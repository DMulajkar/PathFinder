# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Slack bot for the Slack Agent Builder Challenge ("Agent for Good" accessibility track). It receives diagrams (images, Figma links, Lucidchart links) posted in Slack and returns structured text descriptions for blind/low-vision users.

**Current state: Phase 1 only** — echo bot confirming the event pipeline (Socket Mode, threaded replies, file attachment detection). Gemini description layer and Figma MCP are not built yet.

## Running the bot

```
# First time
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# Every time
.venv\Scripts\python app.py
```

Use `py` (the Python Launcher), not `python` — the Windows Store stub is first on PATH and doesn't work.

## Architecture

Single file: `app.py`. All Slack event handling lives there.

- **Transport**: Socket Mode (WebSocket from bot to Slack, no public URL needed)
- **Framework**: `slack_bolt` — event handlers registered with `@app.event()`
- **Auth**: `SLACK_BOT_TOKEN` (xoxb-) for API calls, `SLACK_APP_TOKEN` (xapp-) for Socket Mode connection
- **Thread replies**: always use `thread_ts = event.get("thread_ts") or event["ts"]` and pass it to `say()`

## Slack app config

`manifest.json` holds the full app definition (scopes, events, Socket Mode flag). To reconfigure the Slack app: go to api.slack.com → your app → *App Manifest* → paste and save.

Bot token scopes: `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`, `im:history`, `files:read`.
App-level token scope: `connections:write`.

## Roadmap

### Phase 2 — Diagram Intake
Three separate intake functions, prioritize in order:

1. **Image attachments** (PNG, JPG, PDF): download file bytes using Slack Files API with bot token (`Authorization: Bearer $SLACK_BOT_TOKEN` on `url_private_download`)
2. **Figma links**: detect `figma.com` URLs, extract `fileKey` and `node-id` from the URL path/query string
3. **Lucidchart links**: detect `lucid.app` URLs — flag as unsupported for now, reply asking user to export as PNG and re-upload

### Phase 3 — AI Description Layer
Send image to Gemini multimodal API with this base prompt:

> "You are an accessibility assistant helping a blind user understand a diagram shared in their workplace Slack channel. Analyze this diagram and describe it in a way that is fully understandable without seeing it. Extract: (1) the overall purpose of the diagram in one sentence, (2) all nodes or steps in logical order, (3) all decision points and their branches with explicit yes/no or condition labels, (4) the directional flow between steps, (5) any labels, annotations, or swimlane groupings. Do not describe colors or visual styling. Focus entirely on structure, logic, and content."

Output must be structured (numbered steps, explicit branch notation), not prose.

### Phase 4 — Accessible Output Format
Format Gemini response before sending to Slack:
- One-line summary
- Numbered steps in order
- Decision branches on their own lines: `Decision: Is X? → YES: go to Step N | NO: go to Step M`
- "Notes" section for annotations that didn't fit the flow
- Use Slack mrkdwn (`*bold*`, line breaks) — no tables, no emoji

### Phase 5 — Figma MCP Integration
- Connect to Figma MCP at `https://mcp.figma.com/mcp`
- Use file key + node ID (extracted in Phase 2) to pull actual component/frame data
- Feed structured Figma node data into the Gemini prompt alongside or instead of a screenshot
- More accurate than image recognition for structured diagrams

### General requirements
- All replies in the thread of the original message, never in channel root
- If processing fails, reply explicitly with what went wrong and what the user should do instead
- `slackhack@salesforce.com` and `testing@devpost.com` need workspace access before submission
