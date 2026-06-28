"""Lucid remote MCP client (primary path for Lucidchart links).

Unlike Figma's MCP, Lucid's MCP (https://mcp.lucid.app/mcp) allows open dynamic
client registration, so this actually authorizes. fetch() returns the document's
content/summary text for Gemini; lucid.get_lucid() falls back to REST export on
any failure. Authorize once in a browser:

    .venv\\Scripts\\python lucid_mcp.py

Tokens are cached to .lucid_mcp_tokens.json (gitignored) and refreshed
non-interactively; the bot never opens a browser mid-message.

ponytail: mirrors figma_mcp.py OAuth/MCP plumbing. Extract a shared mcp_oauth
module if a third remote MCP shows up — two is not yet worth the abstraction.
"""
import asyncio
import base64
import hashlib
import json
import queue
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

MCP_URL = "https://mcp.lucid.app/mcp"
TOKEN_ENDPOINT = "https://mcp.lucid.app/oauth/token"
AUTHORIZE_ENDPOINT = "https://mcp.lucid.app/oauth/authorize"
REGISTER_ENDPOINT = "https://mcp.lucid.app/oauth/register"
# Out-of-band redirect: Lucid shows the code on a page instead of calling back,
# so the in-Slack "Connect Lucidchart" button needs no public HTTPS callback.
OOB_REDIRECT = "urn:ietf:wg:oauth:2.0:oob"
CALLBACK_PORT = 8765
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"
TOKENS_FILE = Path(__file__).with_name(".lucid_mcp_tokens.json")
# Pinned to Lucid's real tools: `fetch` returns structured document content
# (pages -> spatial regions of diagram elements) by document ID — exactly what
# we want. PNG export is a within-MCP image fallback.
TOOL_PREFERENCE = ("fetch", "lucid_export_document_as_PNG")


class _FileStorage(TokenStorage):
    """Persist OAuth tokens + dynamic client registration to a JSON file."""

    def __init__(self, path: Path = TOKENS_FILE):
        self._path = path

    def _load(self) -> dict:
        return json.loads(self._path.read_text()) if self._path.exists() else {}

    def _save(self, data: dict) -> None:
        self._path.write_text(json.dumps(data))

    async def get_tokens(self) -> OAuthToken | None:
        d = self._load().get("tokens")
        return OAuthToken.model_validate(d) if d else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        d = self._load()
        d["tokens"] = tokens.model_dump(mode="json")
        d["obtained_at"] = time.time()  # the SDK never persists expiry; we do
        self._save(d)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        d = self._load().get("client")
        return OAuthClientInformationFull.model_validate(d) if d else None

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        d = self._load()
        d["client"] = info.model_dump(mode="json")
        self._save(d)


def _client_metadata() -> OAuthClientMetadata:
    return OAuthClientMetadata(
        client_name="PathFinder",
        redirect_uris=[REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post",
    )


def _doc_url(doc_id: str) -> str:
    return f"https://lucid.app/lucidchart/{doc_id}/edit"


def _pick_tool(tools):
    by_name = {t.name: t for t in tools}
    for pref in TOOL_PREFERENCE:
        if pref in by_name:
            return by_name[pref]
    if not tools:
        raise RuntimeError("Lucid MCP exposed no tools")
    return tools[0]


def _build_args(schema: dict | None, doc_id: str) -> dict:
    props = (schema or {}).get("properties", {})
    url = _doc_url(doc_id)
    candidates = {
        "url": url, "documentUrl": url, "document_url": url,
        "documentId": doc_id, "document_id": doc_id, "id": doc_id,
    }
    return {k: v for k, v in candidates.items() if k in props}


def _result_text(result) -> str:
    out = [getattr(b, "text", "") for b in result.content if getattr(b, "text", "")]
    if not out:
        raise RuntimeError("Lucid MCP returned no text content")
    return "\n".join(out)


async def _no_interactive_auth(*_args, **_kwargs):
    raise RuntimeError("Lucid MCP not authorized — run: python lucid_mcp.py")


async def _fetch(doc_id: str) -> str:
    provider = OAuthClientProvider(
        server_url=MCP_URL,
        client_metadata=_client_metadata(),
        storage=_FileStorage(),
        redirect_handler=_no_interactive_auth,
        callback_handler=_no_interactive_auth,
    )
    async with streamablehttp_client(MCP_URL, auth=provider) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool = _pick_tool((await session.list_tools()).tools)
            result = await session.call_tool(tool.name, _build_args(tool.inputSchema, doc_id))
            return _result_text(result)


def _needs_refresh(data: dict, now: float | None = None) -> bool:
    """True if the stored access token is missing/expired (2-min buffer).

    Works around the SDK not persisting expiry: on a fresh process it would
    treat an expired token as valid, skip refresh, 401, then full re-auth.
    """
    obtained = data.get("obtained_at")
    if obtained is None:
        return True
    expires_in = data.get("tokens", {}).get("expires_in", 0)
    return (now or time.time()) >= obtained + expires_in - 120


def _refresh_tokens() -> None:
    """Refresh the access token via the refresh_token grant and persist it."""
    store = _FileStorage()
    data = store._load()
    tok, client = data.get("tokens", {}), data.get("client", {})
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": client["client_id"],
        "resource": MCP_URL,
    }
    if client.get("client_secret"):  # public (PKCE) clients have none
        payload["client_secret"] = client["client_secret"]
    resp = requests.post(TOKEN_ENDPOINT, data=payload, timeout=30)
    resp.raise_for_status()
    new = resp.json()
    new.setdefault("refresh_token", tok.get("refresh_token"))  # keep if not rotated
    data["tokens"] = new
    data["obtained_at"] = time.time()
    store._save(data)


# -- In-Slack OOB authorization (no browser callback, no domain needed) -------

def build_auth_url() -> str:
    """Register a public PKCE client, stash it, and return Lucid's consent URL.

    Pairs with exchange_code(): the OOB redirect makes Lucid display a code the
    admin copies into Slack, so no public callback server is required.
    """
    reg = requests.post(
        REGISTER_ENDPOINT,
        json={
            "client_name": "PathFinder",
            "redirect_uris": [OOB_REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        timeout=30,
    )
    reg.raise_for_status()
    client = reg.json()

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()

    store = _FileStorage()
    data = store._load()
    data["client"] = client
    data["pending_verifier"] = verifier
    store._save(data)

    query = urlencode({
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": OOB_REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": MCP_URL,
    })
    return f"{AUTHORIZE_ENDPOINT}?{query}"


def exchange_code(code: str) -> None:
    """Exchange the pasted OOB code for tokens and persist them. Raises on error."""
    store = _FileStorage()
    data = store._load()
    client = data.get("client") or {}
    verifier = data.get("pending_verifier")
    if not client.get("client_id") or not verifier:
        raise RuntimeError("No pending authorization — click Connect Lucidchart again.")

    payload = {
        "grant_type": "authorization_code",
        "code": code.strip(),
        "redirect_uri": OOB_REDIRECT,
        "client_id": client["client_id"],
        "code_verifier": verifier,
        "resource": MCP_URL,
    }
    if client.get("client_secret"):
        payload["client_secret"] = client["client_secret"]
    resp = requests.post(TOKEN_ENDPOINT, data=payload, timeout=30)
    resp.raise_for_status()

    data["tokens"] = resp.json()
    data["obtained_at"] = time.time()
    data.pop("pending_verifier", None)
    store._save(data)


def is_connected() -> bool:
    """True if we hold Lucid tokens (used to render the App Home button/status)."""
    try:
        return bool(json.loads(TOKENS_FILE.read_text()).get("tokens"))
    except Exception:
        return False


def fetch(doc_id: str) -> str:
    """Sync entry point used by lucid.get_lucid(). Raises if not authorized."""
    if not TOKENS_FILE.exists():
        raise RuntimeError("Lucid MCP not authorized")  # short-circuit -> REST
    if _needs_refresh(_FileStorage()._load()):
        _refresh_tokens()  # keep storage fresh so the SDK loads a valid token
    return asyncio.run(_fetch(doc_id))


def authorize() -> None:
    """One-time interactive browser OAuth. Run this module directly."""
    captured: queue.Queue = queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            captured.put((qs.get("code", [None])[0], qs.get("state", [None])[0]))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Lucid authorized. You can close this tab.</h1>")

        def log_message(self, *_):
            pass

    server = HTTPServer(("localhost", CALLBACK_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def redirect_handler(auth_url: str) -> None:
        print("Opening browser to authorize Lucid MCP...\nIf it doesn't open:", auth_url)
        webbrowser.open(auth_url)

    async def callback_handler():
        return await asyncio.to_thread(captured.get)

    async def run():
        provider = OAuthClientProvider(
            server_url=MCP_URL,
            client_metadata=_client_metadata(),
            storage=_FileStorage(),
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )
        async with streamablehttp_client(MCP_URL, auth=provider) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = [t.name for t in (await session.list_tools()).tools]
                print("Authorized. Lucid MCP tools:", names)

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
    print(f"Tokens saved to {TOKENS_FILE.name}. The bot will use Lucid MCP now.")


if __name__ == "__main__":
    authorize()
