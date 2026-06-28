"""Microsoft Visio (.vsdx) direct read — no Microsoft API needed.

A .vsdx is a ZIP of XML (Open Packaging, like .docx). The flow is already in
there: each page's `<Shape>` carries its `<Text>`, and the `<Connects>` section
links connector shapes to the shapes they join. So we unzip, read the shapes and
their connections, and hand Gemini a structured outline — the same "real
structure beats a screenshot" win we get from Figma/Lucid, with stdlib only.

ponytail: parses .vsdx *uploads*. Visio files that live in SharePoint/OneDrive
(M365 links) need Microsoft Graph + its own OAuth — see get_visio_url() stub.
"""
import io
import re
import zipfile
from xml.etree import ElementTree as ET

# Content pages are page1.xml, page2.xml, …; pages.xml is just the index.
_PAGE_RE = re.compile(r"visio/pages/page[0-9]+\.xml$")
_PAGE_NUM_RE = re.compile(r"page([0-9]+)\.xml$")


def _local(tag: str) -> str:
    """Strip the XML namespace: '{...main}Shape' -> 'Shape'."""
    return tag.rsplit("}", 1)[-1]


def _shape_text(shape: ET.Element) -> str:
    """Text of a shape from its *direct* <Text> child (not nested group shapes)."""
    for child in shape:
        if _local(child.tag) == "Text":
            # itertext() flattens the <cp>/<pp>/<tp> formatting runs Visio splits
            # text into; join and collapse whitespace.
            return " ".join("".join(child.itertext()).split())
    return ""


def _walk_shapes(parent: ET.Element, out: dict) -> None:
    """Recursively collect {shape_id: text}. Descends both the page's <Shapes>
    container and the nested <Shapes> inside group shapes."""
    for el in parent:
        tag = _local(el.tag)
        if tag == "Shapes":  # a container (page-level or inside a group)
            _walk_shapes(el, out)
        elif tag == "Shape":
            sid = el.get("ID")
            if sid:
                out[sid] = _shape_text(el)
            for sub in el:  # a group shape holds its members in a nested <Shapes>
                if _local(sub.tag) == "Shapes":
                    _walk_shapes(sub, out)


def _edges(page: ET.Element, shapes: dict) -> tuple[list, set]:
    """Reconstruct directed edges from <Connects>.

    A connector is a shape glued to two others: one Connect row with FromCell
    'Begin*' (source) and one with 'End*' (target), both FromSheet=connector id.
    Returns (edges, connector_ids); each edge is (from_id, to_id, label).
    """
    by_connector: dict = {}
    for connects in page.iter():
        if _local(connects.tag) != "Connects":
            continue
        for c in connects:
            if _local(c.tag) != "Connect":
                continue
            conn_id, target, cell = c.get("FromSheet"), c.get("ToSheet"), c.get("FromCell", "")
            if not conn_id or not target:
                continue
            ends = by_connector.setdefault(conn_id, {})
            if cell.startswith("Begin"):
                ends["from"] = target
            elif cell.startswith("End"):
                ends["to"] = target

    edges = []
    for conn_id, ends in by_connector.items():
        if "from" in ends and "to" in ends:
            edges.append((ends["from"], ends["to"], shapes.get(conn_id, "")))
    return edges, set(by_connector)


def _outline_page(page_xml: bytes, title: str) -> str:
    shapes: dict = {}
    root = ET.fromstring(page_xml)
    _walk_shapes(root, shapes)
    edges, connector_ids = _edges(root, shapes)

    def label(sid: str) -> str:
        return shapes.get(sid) or f"(unlabeled shape {sid})"

    lines = [title]
    nodes = [(sid, txt) for sid, txt in shapes.items() if sid not in connector_ids]
    if nodes:
        lines.append("Shapes:")
        lines += [f"- {txt or f'(unlabeled shape {sid})'}" for sid, txt in nodes]
    if edges:
        lines.append("Connections:")
        for frm, to, lbl in edges:
            tail = f'  (labeled "{lbl}")' if lbl else ""
            lines.append(f"- {label(frm)} -> {label(to)}{tail}")
    if not nodes and not edges:
        lines.append("(no shapes found on this page)")
    return "\n".join(lines)


def _page_names(zf: zipfile.ZipFile) -> list:
    """Page Name attributes in document order (best-effort, for nicer titles).

    ponytail: pairs names to page files by document order, not the rels graph —
    correct for the common single-flow file; wire up _rels/pages.xml.rels if a
    multi-page reorder ever mislabels.
    """
    try:
        idx = ET.fromstring(zf.read("visio/pages/pages.xml"))
    except KeyError:
        return []
    return [el.get("Name") or el.get("NameU")
            for el in idx.iter() if _local(el.tag) == "Page"]


def parse_vsdx(file_bytes: bytes) -> str:
    """Return a structured text outline (shapes + connections) for every page."""
    zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    page_files = sorted(
        (n for n in zf.namelist() if _PAGE_RE.search(n)),
        key=lambda n: int(_PAGE_NUM_RE.search(n).group(1)),
    )
    if not page_files:
        raise ValueError("No Visio pages found — is this a valid .vsdx file?")
    names = _page_names(zf)
    out = []
    for i, name in enumerate(page_files):
        title = names[i] if i < len(names) and names[i] else f"Page {i + 1}"
        out.append(_outline_page(zf.read(name), f"Page: {title}"))
    return "\n\n".join(out)


def get_visio(file_bytes: bytes) -> str:
    """Entry point used by app.route_diagram for .vsdx uploads. Raises on bad file."""
    return parse_vsdx(file_bytes)


def get_visio_url(url: str) -> str:
    """Visio files shared as M365/SharePoint links.

    Not implemented: unlike Lucid's open MCP, Microsoft files require Graph API
    auth (app registration in Entra ID + delegated Files.Read + the full OAuth
    code flow) to download the .vsdx, after which parse_vsdx() handles the rest.
    Tracked separately; uploads cover the common case.
    """
    raise NotImplementedError(
        "Visio links (SharePoint/OneDrive) aren't supported yet — please download "
        "the .vsdx and upload the file here, or export it as a PNG/PDF."
    )


if __name__ == "__main__":
    # Self-check: build a minimal .vsdx in memory (Start -> Decision -> End) and
    # verify the parser recovers the shapes and the directed, labeled edges.
    NS = "http://schemas.microsoft.com/office/visio/2012/main"
    page = f"""<?xml version="1.0"?>
<PageContents xmlns="{NS}">
  <Shapes>
    <Shape ID="1"><Text>Start</Text></Shape>
    <Shape ID="2"><Text>Is it valid?</Text></Shape>
    <Shape ID="3"><Text>End</Text></Shape>
    <Shape ID="10"><Text>Yes</Text></Shape>
    <Shape ID="11"><Text></Text></Shape>
  </Shapes>
  <Connects>
    <Connect FromSheet="10" ToSheet="1" FromCell="BeginX"/>
    <Connect FromSheet="10" ToSheet="2" FromCell="EndX"/>
    <Connect FromSheet="11" ToSheet="2" FromCell="BeginX"/>
    <Connect FromSheet="11" ToSheet="3" FromCell="EndX"/>
  </Connects>
</PageContents>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("visio/pages/pages.xml",
                   f'<Pages xmlns="{NS}"><Page Name="Flow"/></Pages>')
        z.writestr("visio/pages/page1.xml", page)
    text = parse_vsdx(buf.getvalue())
    print(text)
    assert "Start" in text and "Is it valid?" in text and "End" in text
    assert "Start -> Is it valid?" in text          # connector 10, begin->end
    assert '(labeled "Yes")' in text                # connector carried a label
    assert "Is it valid? -> End" in text            # connector 11, unlabeled
    assert "Page: Flow" in text                     # page name picked up
    assert "Yes" not in text.split("Connections:")[0].split("Shapes:")[1]  # connector not a node
    print("\nOK")
