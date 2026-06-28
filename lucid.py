"""Lucidchart direct read via the Lucid REST API.

Exports a Lucidchart document to PNG using LUCID_API_TOKEN, then hands the PNG to
the existing image pipeline so the bot describes it without a manual export.

LUCID_API_TOKEN can be either:
  - a Lucid REST *API key* (preferred — does not expire), or
  - an OAuth access token with `lucidchart.document.content:readonly` scope.
Both authenticate as `Authorization: Bearer <token>`. Either only reads
documents the owning account can access (single-account scoping).

On any failure the handler falls back to "export as PNG and re-upload", so it
degrades gracefully. ponytail: API key avoids OAuth refresh entirely.
"""
import os
import re

import requests

# lucid.app/lucidchart/<id>/edit  or  lucid.app/documents/<id>
LUCID_DOC_RE = re.compile(r"lucid\.app/(?:lucidchart|documents(?:/edit)?)/([0-9a-zA-Z-]+)")


def extract_doc_id(text: str) -> str | None:
    m = LUCID_DOC_RE.search(text)
    return m.group(1) if m else None


def export_png(doc_id: str) -> bytes:
    """Export the document's first page as PNG. Raises on auth/access errors."""
    resp = requests.get(
        f"https://api.lucid.co/documents/{doc_id}",
        headers={
            "Authorization": f"Bearer {os.environ['LUCID_API_TOKEN']}",
            "Lucid-Api-Version": "1",
            "Accept": "image/png",
        },
        params={"page": 1},  # ponytail: first page only; loop pages if multi-page needed
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


def get_lucid(doc_id: str) -> tuple[str, object]:
    """MCP-first, REST fallback. Returns ('text', str) from MCP content, or
    ('image', bytes) from a REST PNG export. Raises if neither is available.
    """
    try:
        import lucid_mcp

        return ("text", lucid_mcp.fetch(doc_id))
    except Exception as e:
        # MCP not authorized / failed -> REST export (raises if no token/access).
        # Log the real MCP error; otherwise both failures are invisible.
        print(f"[lucid] MCP fetch failed for {doc_id}: {e!r}", flush=True)
        return ("image", export_png(doc_id))
