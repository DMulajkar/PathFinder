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


def get_figma_data(file_key: str, node_id: str | None) -> str:
    """MCP-first, REST fallback. Returns a text outline of the Figma node tree."""
    try:
        import figma_mcp

        return figma_mcp.fetch(file_key, node_id)
    except Exception:
        # ponytail: any MCP failure (not configured, auth expired, network)
        # falls back to REST — the bot stays functional regardless.
        return figma_outline(fetch_figma_rest(file_key, node_id))
