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
- **Stays silent on non-diagrams** — an uploaded image that isn't a flowchart/process
  diagram (a photo, screenshot, chart, logo, meme) is classified by Gemini and **ignored**,
  so the bot never spams channels. Figma/Lucid links always reply (explicit intent).
- **App Home tab** — a help screen plus a one-click **Connect Lucidchart** button (see below).
- **Multi-workspace** — distributable via an **"Add to Slack"** OAuth link; each workspace
  gets its own bot token, all served over one process.

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

Events always arrive over **Socket Mode** (no public webhook needed for messages). In
**distributed mode** (when `SLACK_CLIENT_ID` is set) a small Flask server also runs to host
the three HTTP endpoints OAuth needs — `/slack/install`, `/slack/oauth_redirect`, and
`/lucid/callback` — on `localhost:3000`, fronted by Caddy for HTTPS. In single-workspace
dev mode (no client ID) only Socket Mode runs.

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
8. **Connect Lucid (optional, one-time)** so Lucidchart links are read directly — done
   from Slack, not a terminal: open PathFinder's **App Home → Connect Lucidchart**. See the
   [Lucid MCP](#lucid-mcp-working-primary) section for the flow.

## Environment (`.env`)

| Var | Required | Purpose |
|-----|----------|---------|
| `SLACK_BOT_TOKEN` | dev only | Bot identity / API calls (`xoxb-`). Used only in single-workspace dev mode; ignored when `SLACK_CLIENT_ID` is set |
| `SLACK_APP_TOKEN` | yes | Socket Mode connection (`xapp-`) |
| `GEMINI_API_KEY` | yes | Diagram description |
| `GEMINI_MODEL` | no | Override model (default `gemini-2.5-flash`; `gemini-2.0-flash` has no free quota) |
| `FIGMA_TOKEN` | for Figma | Figma REST fallback (`figd_...`) |
| `LUCID_API_TOKEN` | no | Lucid REST fallback (API key or OAuth token); only used if the Lucid MCP isn't connected |
| `SLACK_SIGNING_SECRET` | distribution | Required to enable OAuth distribution (*Basic Information → App Credentials*) |
| `SLACK_CLIENT_ID` | distribution | **Setting this flips the bot into distributed (multi-workspace) mode** and starts the Flask OAuth server |
| `SLACK_CLIENT_SECRET` | distribution | OAuth client secret (*Basic Information → App Credentials*) |
| `PUBLIC_BASE_URL` | distribution | Public HTTPS base, e.g. `https://pathfinder-slackhack.duckdns.org`. Drives the Lucid auto-callback; without it Lucid connect falls back to manual code paste |
| `LUCID_ADMIN_USER` | no | Slack user ID allowed to press **Connect Lucidchart**. Unset = anyone may |

## Lucid MCP (working primary)

Lucidchart links are read from real document structure via the remote **Lucid MCP**
(`https://mcp.lucid.app/mcp`). Unlike Figma's MCP, Lucid's allows **open dynamic client
registration**, so PathFinder can authorize itself. The MCP's `fetch` tool returns
structured content (pages → diagram elements with labels, shape types, positions), which
Gemini turns into the accessible description. If Lucid isn't connected, the bot falls back
to a REST PNG export (`LUCID_API_TOKEN`), then to a "export as PNG" message.

**Connecting Lucid is a one-time, in-Slack action** — no terminal:

1. Open PathFinder's **App Home** tab → click **Connect Lucidchart**.
2. A modal gives you a link → click it → click **Allow** on Lucid's consent page.
3. With `PUBLIC_BASE_URL` set, Lucid redirects the authorization code to `/lucid/callback`
   and the bot exchanges it automatically — you land on a "connected" page, done. (Without
   a public URL, e.g. local dev, the modal instead asks you to paste the code Lucid shows.)

It's a **single shared Lucid account** for the whole workspace — one person connects, and
every teammate's Lucid links then work. Tokens are cached to `.lucid_mcp_tokens.json`
(gitignored) and auto-refreshed.

> Why a callback and not the terminal: Lucid's out-of-band (manual-code) mode never reliably
> shows a copyable code in-browser. Once the server has a public HTTPS URL, the real
> `/lucid/callback` redirect is cleaner and fully automatic.
>
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

## Deployment (self-hosted, always-on)

Production runs on a self-hosted Ubuntu server (Oracle Cloud) under **systemd**, so it
restarts on crash/reboot.

**systemd service** — `/etc/systemd/system/pathfinder.service`:

```ini
[Unit]
Description=PathFinder Slack Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/PathFinder
EnvironmentFile=/home/ubuntu/PathFinder/.env
ExecStart=/home/ubuntu/PathFinder/venv/bin/python3 /home/ubuntu/PathFinder/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Deploy an update:

```
cd /home/ubuntu/PathFinder
git pull
venv/bin/pip install -r requirements.txt   # only when deps change
sudo systemctl restart pathfinder
sudo systemctl status pathfinder           # expect: active (running)
```

## Distribution ("Add to Slack")

To let **other workspaces** install PathFinder, it runs in distributed OAuth mode behind a
public HTTPS URL. Setup, once:

1. **Domain + TLS** — point a domain at the server (a free [DuckDNS](https://duckdns.org)
   subdomain works) and put **Caddy** in front for automatic Let's Encrypt certs.
   `/etc/caddy/Caddyfile`:
   ```
   pathfinder-slackhack.duckdns.org {
     reverse_proxy localhost:3000
   }
   ```
2. **Open ports 80 and 443** at both layers: the Oracle **VCN Security List** (cloud console
   → ingress rules, `0.0.0.0/0` TCP 80 & 443) *and* the host firewall — insert the rules
   **above** the default REJECT:
   ```
   sudo iptables -I INPUT 5 -m state --state NEW -p tcp --dport 80 -j ACCEPT
   sudo iptables -I INPUT 5 -m state --state NEW -p tcp --dport 443 -j ACCEPT
   sudo netfilter-persistent save
   ```
3. **Slack app config** — *App Manifest*: ensure `oauth_config.redirect_urls` lists
   `https://<domain>/slack/oauth_redirect` and interactivity is enabled (both already in
   `manifest.json`). *Basic Information* → copy **Client ID** + **Client Secret**.
   *Manage Distribution* → **Activate Public Distribution**.
4. **Server `.env`** — add `SLACK_SIGNING_SECRET`, `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`,
   and `PUBLIC_BASE_URL`, then restart. Setting `SLACK_CLIENT_ID` switches on distributed
   mode (Flask serves the OAuth endpoints; events still come over Socket Mode).
5. **Install link** — share `https://<domain>/slack/install`. Each install's bot token is
   saved to `data/installations/` (file store, gitignored); the right token is used
   per-workspace automatically.

Notes:
- Flask sits behind Caddy, so `ProxyFix` trusts `X-Forwarded-Proto`; without it Bolt builds
  an `http://` redirect and Slack rejects the install with `invalid_browser`.
- Always start the flow at `/slack/install` (not `/slack/oauth_redirect`, and don't reload
  the redirect page) — the one-time state cookie is set at `/slack/install`.

## Run the tests

```
.venv\Scripts\python test_intake.py
```

Covers URL/mimetype detection, node-id parsing, Figma/Lucid outline handling, verbosity /
plain-language / mermaid option parsing, fence-safe mrkdwn (Mermaid `-->` survives),
follow-up thread recall, MCP helpers and token-refresh logic, the MCP→REST fallbacks, and
the Gemini 5xx retry. No network or live keys needed.

## License

[MIT](LICENSE) — free to use, modify, and distribute.

## Verify end-to-end

- **Plain text in a channel** → bot stays silent (no spam).
- **A non-diagram image** (photo, screenshot, meme) → bot stays silent (classified out).
- **PNG/JPG/PDF of a flowchart** → threaded reply with a structured, accessible description.
- **Figma link** (to a file your account can access) → threaded reply from structured data.
- **Lucidchart link** → threaded reply from the Lucid MCP (after connecting via App Home).
- **Reply in the thread** with a question → answer about that diagram.
- **Add `summary` / `detailed` / `plain language` / `mermaid`** → output adapts.
- **Assistant pane** → open PathFinder from the sidebar; greeting + suggested prompts; all
  of the above work there too, with a "thinking" status while it processes.
