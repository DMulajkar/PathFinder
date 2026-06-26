"""Lucidchart direct read via the Lucid REST API.

Exports a Lucidchart document to PNG using LUCID_API_TOKEN (an OAuth access token
with document-content read scope), so the bot can describe it without the user
exporting manually. The PNG is handed to the existing image pipeline.

ponytail: uses a static token from env. Lucid OAuth access tokens expire (~1h);
for a long-running bot, add OAuth refresh (client_id/secret + refresh_token).
Fine for demo/testing with a fresh token. On any failure the handler falls back
to the "export as PNG and re-upload" instructions, so it degrades gracefully.
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
