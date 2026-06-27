"""Phase 5: Figma structured-data intake.

get_figma_data(file_key, node_id) returns a compact text outline of a Figma
node tree for Gemini. It tries the Figma MCP first (figma_mcp.fetch) and falls
back to the REST API, so the bot works whether or not MCP auth is set up.
"""
import os
import re
import time

import requests

NODE_ID_RE = re.compile(r"node-id=([0-9]+[-:][0-9]+)")


def extract_node_id(text: str) -> str | None:
    """Return the Figma node id in API form ('1:2') from a URL, or None.

    Figma URLs encode the node as '1-2'; the REST API wants '1:2'.
    """
    m = NODE_ID_RE.search(text)
    return m.group(1).replace("-", ":") if m else None


def fetch_figma_rest(file_key: str, node_id: str | None) -> dict:
    """Fetch node (or whole file) JSON from the Figma REST API. Needs FIGMA_TOKEN.

    Retries transient 429s, honoring Retry-After but capping the wait so a Slack
    reply isn't blocked too long; if Figma asks for a long cool-down, give up and
    let the caller surface a rate-limit message.
    """
    headers = {"X-Figma-Token": os.environ["FIGMA_TOKEN"]}
    if node_id:
        url = f"https://api.figma.com/v1/files/{file_key}/nodes?ids={node_id}"
    else:
        url = f"https://api.figma.com/v1/files/{file_key}"
    for attempt in range(3):
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 429 and attempt < 2:
            wait = int(resp.headers.get("Retry-After", 2 ** attempt))
            if wait <= 15:
                time.sleep(wait)
                continue
        resp.raise_for_status()
        return resp.json()


def figma_outline(data: dict) -> str:
    """Flatten Figma node JSON into an indented text outline for Gemini.

    Captures layer name, type, text, position/size, and connector endpoints —
    enough for the model to infer flow and layout from structure alone.
    """
    roots = []
    if "nodes" in data:  # /nodes?ids= response
        roots = [v["document"] for v in data["nodes"].values() if v and "document" in v]
    elif "document" in data:  # /files/:key response
        roots = [data["document"]]

    lines: list[str] = []

    def walk(node: dict, depth: int) -> None:
        parts = [f"{'  ' * depth}- {node.get('name', '')} [{node.get('type', '')}]"]
        if node.get("characters"):
            parts.append(f'text="{node["characters"]}"')
        bb = node.get("absoluteBoundingBox")
        if bb:
            parts.append(
                f"at({int(bb['x'])},{int(bb['y'])} {int(bb['width'])}x{int(bb['height'])})"
            )
        if node.get("type") == "CONNECTOR":  # FigJam arrows
            s = (node.get("connectorStart") or {}).get("endpointNodeId")
            e = (node.get("connectorEnd") or {}).get("endpointNodeId")
            if s or e:
                parts.append(f"connects {s} -> {e}")
        lines.append(" ".join(parts))
        for child in node.get("children", []):
            walk(child, depth + 1)

    for r in roots:
        walk(r, 0)
    return "\n".join(lines)


def render_png(file_key: str, node_id: str | None) -> bytes:
    """Render a node to PNG via the Figma images endpoint and return the bytes.

    /v1/images is a separate rate-limit bucket from /v1/files, so this still works
    when file-content fetches are throttled (429). Used as a last-resort fallback
    feeding the Gemini vision path. ponytail: scale=2 balances readable text vs
    image size; drop to 1 if a large board renders too big.
    """
    headers = {"X-Figma-Token": os.environ["FIGMA_TOKEN"]}
    ids = node_id or "0:1"
    meta = requests.get(
        f"https://api.figma.com/v1/images/{file_key}?ids={ids}&format=png&scale=2",
        headers=headers, timeout=40,
    )
    meta.raise_for_status()
    img_url = next(iter(meta.json().get("images", {}).values()), None)
    if not img_url:
        raise RuntimeError("Figma returned no rendered image")
    img = requests.get(img_url, timeout=60)
    img.raise_for_status()
    return img.content


def get_figma_data(file_key: str, node_id: str | None) -> tuple[str, object]:
    """Returns (kind, payload): ('text', outline) from the Figma MCP or the
    /v1/files REST API, or ('image', png_bytes) rendered via /v1/images when the
    structured fetch fails (e.g. files-endpoint rate limit). Mirrors get_lucid.
    """
    try:
        import figma_mcp

        return ("text", figma_mcp.fetch(file_key, node_id))
    except Exception:
        pass  # MCP not available (allowlist/blocked) -> structured REST
    try:
        return ("text", figma_outline(fetch_figma_rest(file_key, node_id)))
    except Exception:
        # files endpoint failed (e.g. 429) -> render a PNG from the images endpoint
        return ("image", render_png(file_key, node_id))
