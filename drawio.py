"""draw.io (.drawio / .xml) direct read — no API needed.

draw.io files are XML. Two formats:
- <mxfile> wrapper: the <diagram> child holds either raw XML or base64+deflate
  compressed XML (wbits=-15 for raw deflate, no zlib header).
- Bare <mxGraphModel>: uncompressed, parse directly.

Shapes are <mxCell vertex="1"> or <object> wrapping one. Edges are
<mxCell edge="1"> with source/target referencing other cell IDs.
"""
import base64
import urllib.parse
import zlib
from xml.etree import ElementTree as ET


def _decompress(encoded: str) -> str:
    """base64 + raw deflate + URL-decode → XML string."""
    raw = zlib.decompress(base64.b64decode(encoded), wbits=-15)
    return urllib.parse.unquote(raw.decode("utf-8"))


def _get_graph_root(root: ET.Element) -> ET.Element:
    """Return the <root> element regardless of whether the file is wrapped in
    <mxfile>/<diagram> (possibly compressed) or is a bare <mxGraphModel>."""
    tag = root.tag.split("}")[-1]  # strip namespace if any

    if tag == "mxfile":
        # May have multiple pages; flatten all into one root for simplicity.
        # ponytail: multi-page is rare for Slack shares; flatten is fine.
        combined = ET.Element("root")
        for diagram in root.findall("diagram"):
            text = (diagram.text or "").strip()
            if not text:
                # already uncompressed XML inside the element
                child = diagram.find("mxGraphModel")
                if child is not None:
                    r = child.find("root")
                    for item in r if r is not None else []:
                        combined.append(item)
            else:
                try:
                    inner = ET.fromstring(_decompress(text))
                    r = inner.find("root")
                    for item in r if r is not None else []:
                        combined.append(item)
                except Exception:
                    # not base64/deflate — try parsing as raw XML
                    try:
                        inner = ET.fromstring(text)
                        r = inner.find("root")
                        for item in r if r is not None else []:
                            combined.append(item)
                    except Exception:
                        pass
        return combined

    if tag == "mxGraphModel":
        r = root.find("root")
        return r if r is not None else ET.Element("root")

    return ET.Element("root")


def _label(el: ET.Element) -> str:
    """Label from an <mxCell> or <object> element."""
    # <object> carries its label in 'label'; <mxCell> uses 'value'
    return (el.get("label") or el.get("value") or "").strip()


def get_drawio(file_bytes: bytes) -> str:
    """Parse a .drawio file and return a structured text outline."""
    try:
        root = ET.fromstring(file_bytes.decode("utf-8", errors="replace"))
    except ET.ParseError as e:
        raise ValueError(f"Invalid draw.io XML: {e}") from e

    graph_root = _get_graph_root(root)

    # Collect shapes and edges. Shapes may be bare <mxCell vertex="1"> or
    # <object> wrappers whose inner <mxCell> has vertex="1".
    shapes: dict[str, str] = {}   # id → label
    edges: list[tuple[str, str, str]] = []  # (source_id, target_id, label)

    for el in graph_root:
        tag = el.tag.split("}")[-1]

        if tag == "object":
            # object wraps an mxCell
            inner = el.find("mxCell")
            if inner is not None and inner.get("vertex") == "1":
                eid = el.get("id") or inner.get("id") or ""
                if eid:
                    shapes[eid] = _label(el)
            # edges wrapped in object are unusual but handle anyway
            if inner is not None and inner.get("edge") == "1":
                src, tgt = inner.get("source", ""), inner.get("target", "")
                edges.append((src, tgt, _label(el)))

        elif tag == "mxCell":
            eid = el.get("id", "")
            if el.get("vertex") == "1" and eid not in ("0", "1"):
                shapes[eid] = _label(el)
            elif el.get("edge") == "1":
                src, tgt = el.get("source", ""), el.get("target", "")
                edges.append((src, tgt, _label(el)))

    def name(eid: str) -> str:
        return shapes.get(eid) or f"(unlabeled shape {eid})"

    lines: list[str] = []

    node_ids = {eid for eid in shapes if shapes[eid] or True}
    # exclude shapes that are purely connector labels (no source/target IDs point to them
    # as a node — can't tell cleanly, so include everything and let Gemini reason)
    if shapes:
        lines.append("Shapes:")
        for eid, lbl in shapes.items():
            lines.append(f"- {lbl or f'(unlabeled shape {eid})'}")

    if edges:
        lines.append("Connections:")
        for src, tgt, lbl in edges:
            label_part = f'  (labeled "{lbl}")' if lbl else ""
            lines.append(f"- {name(src)} -> {name(tgt)}{label_part}")

    if not lines:
        return "(no shapes or connections found — the file may be empty or use an unsupported format)"

    return "\n".join(lines)


if __name__ == "__main__":
    # Self-check: minimal draw.io XML with two shapes and a labeled edge.
    xml = """<?xml version="1.0"?>
<mxfile>
  <diagram name="Test">
    <mxGraphModel><root>
      <mxCell id="0"/>
      <mxCell id="1" parent="0"/>
      <mxCell id="2" value="Start" vertex="1" parent="1">
        <mxGeometry x="100" y="100" width="120" height="60" as="geometry"/>
      </mxCell>
      <mxCell id="3" value="End" vertex="1" parent="1">
        <mxGeometry x="300" y="100" width="120" height="60" as="geometry"/>
      </mxCell>
      <mxCell id="4" value="Go" edge="1" source="2" target="3" parent="1">
        <mxGeometry relative="1" as="geometry"/>
      </mxCell>
    </root></mxGraphModel>
  </diagram>
</mxfile>"""
    result = get_drawio(xml.encode())
    print(result)
    assert "Start" in result
    assert "End" in result
    assert "Start -> End" in result
    assert '(labeled "Go")' in result
    print("\nOK")
