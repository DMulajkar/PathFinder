# Diagram Describer — Phase 1

Slack bot that (eventually) turns diagrams into accessible structured text for
blind/low-vision users. Phase 1 = event pipeline only: receives messages and
file attachments, replies in-thread.

## Setup

1. **Install Python 3.11+** from python.org (the Windows Store stub won't run pip reliably).
2. **Create the Slack app** at <https://api.slack.com/apps> → *Create New App* → *From a manifest* → paste `manifest.json`.
3. **Get tokens:**
   - *OAuth & Permissions* → Install to workspace → copy the **Bot User OAuth Token** (`xoxb-`).
   - *Basic Information* → App-Level Tokens → generate one with `connections:write` → copy it (`xapp-`).
4. `copy .env.example .env` and fill in both tokens.
5. Install deps and run:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   python app.py
   ```

## Verify (Phase 1 done = all pass)
- Startup logs a Socket Mode connection, no errors.
- Invite the bot to a channel, post text → it replies **in a thread** with `Echo: ...`.
- Post a message with a PNG/JPG → it replies in-thread with the filename + mimetype.

Socket Mode is used, so no public URL / FastAPI is needed yet.
