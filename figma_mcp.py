"""Phase 5 (intended primary path): Figma remote MCP client.

STATUS: BLOCKED by Figma policy, not by this code. Figma's remote MCP server is
allowlist-only — "Only clients listed in the Figma MCP Catalog can connect"
(https://developers.figma.com/docs/figma-mcp-server/). Self-registration via the
advertised DCR endpoint returns 403 Forbidden for any non-catalog client, so this
bot cannot authorize. The OAuth/PKCE/transport wiring below is correct and would
work if the client were catalog-listed.

Runtime is unaffected: authorize() can't complete, so no token file is created,
so fetch() short-circuits and figma.get_figma_data() falls back to the REST API
(figma.py) — which delivers the same structured node/connector data. ponytail:
kept as a documented best-effort primary; delete if catalog listing never happens.

If catalog-listed in future, authorize once in a browser:
    .venv\\Scripts\\python figma_mcp.py
Tokens are cached to .figma_mcp_tokens.json (gitignored) and refreshed
non-interactively; the bot never opens a browser mid-message.
"""
import asyncio
import json
import queue
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

MCP_URL = "https://mcp.figma.com/mcp"
CALLBACK_PORT = 8765
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"
TOKENS_FILE = Path(__file__).with_name(".figma_mcp_tokens.json")
# Prefer a structural/metadata tool; fall back to whatever the server exposes.
TOOL_PREFERENCE = ("get_metadata", "get_code", "get_design_context", "get_screenshot")


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
        client_name="Slack Diagram Describer",
        redirect_uris=[REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="mcp:connect",
        token_endpoint_auth_method="client_secret_post",
    )


def _figma_url(file_key: str, node_id: str | None) -> str:
    url = f"https://www.figma.com/design/{file_key}"
    if node_id:
        url += f"?node-id={node_id.replace(':', '-')}"  # API '1:2' -> URL '1-2'
    return url


def _pick_tool(tools):
    by_name = {t.name: t for t in tools}
    for pref in TOOL_PREFERENCE:
        if pref in by_name:
            return by_name[pref]
    if not tools:
        raise RuntimeError("Figma MCP exposed no tools")
    return tools[0]


def _build_args(schema: dict | None, file_key: str, node_id: str | None) -> dict:
    """Fill only the parameters the chosen tool actually declares."""
    props = (schema or {}).get("properties", {})
    url = _figma_url(file_key, node_id)
    candidates = {
        "url": url, "fileUrl": url, "figmaUrl": url,
        "fileKey": file_key, "file_key": file_key,
        "nodeId": node_id, "node_id": node_id,
        "clientLanguages": "", "clientFrameworks": "",
    }
    return {k: v for k, v in candidates.items() if k in props and v is not None}


def _result_text(result) -> str:
    out = [getattr(b, "text", "") for b in result.content if getattr(b, "text", "")]
    if not out:
        raise RuntimeError("Figma MCP returned no text content")
    return "\n".join(out)


async def _no_interactive_auth(*_args, **_kwargs):
    # The bot must never block a Slack message on a browser. If stored tokens
    # can't satisfy/refresh the request, fail so the caller falls back to REST.
    raise RuntimeError("Figma MCP not authorized — run: python figma_mcp.py")


async def _fetch(file_key: str, node_id: str | None) -> str:
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
            result = await session.call_tool(
                tool.name, _build_args(tool.inputSchema, file_key, node_id)
            )
            return _result_text(result)


def fetch(file_key: str, node_id: str | None) -> str:
    """Sync entry point used by figma.get_figma_data(). Raises if not authorized."""
    if not TOKENS_FILE.exists():
        raise RuntimeError("Figma MCP not authorized")  # short-circuit -> REST
    return asyncio.run(_fetch(file_key, node_id))


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
            self.wfile.write(b"<h1>Figma authorized. You can close this tab.</h1>")

        def log_message(self, *_):  # silence per-request logging
            pass

    server = HTTPServer(("localhost", CALLBACK_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def redirect_handler(auth_url: str) -> None:
        print("Opening browser to authorize Figma MCP...\nIf it doesn't open:", auth_url)
        webbrowser.open(auth_url)

    async def callback_handler():
        return await asyncio.to_thread(captured.get)  # blocks until redirect hits

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
                print("Authorized. Figma MCP tools:", names)

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
    print(f"Tokens saved to {TOKENS_FILE.name}. The bot will use MCP now.")


if __name__ == "__main__":
    authorize()
