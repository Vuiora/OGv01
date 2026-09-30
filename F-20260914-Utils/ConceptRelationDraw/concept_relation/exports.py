import xml.etree.ElementTree as ET
from html import escape

from .layout import graph_layout, NODE_WIDTH, NODE_HEIGHT
from .models import RELATION_LABELS

COLORS = ["#488879", "#668dcc", "#b59056", "#9a78ae", "#bd8072", "#6096a5"]


def visible_edges(graph, view="all"):
    backbone = set(graph_layout(graph)["backbone_ids"]) if view == "backbone" else None
    return [edge for edge in graph["relations"] if edge["review"] != "rejected"
            and (backbone is None or edge["id"] in backbone)]


def mermaid(graph, view="backbone"):
    def label(value):
        return "".join(f"#{ord(character)};" for character in value)
    lines = ["flowchart LR", f"  %% view={view}; JSON retains all relationships and evidence"]
    lines.extend(f'  {node["id"]}["{label(node["label"])}"]' for node in graph["concepts"])
    for edge in visible_edges(graph, view):
        connector = "---" if edge["type"] in ("related", "contrasts") else "-->"
        lines.append(f'  {edge["source"]} {connector}|"{label(RELATION_LABELS[edge["type"]])}"| {edge["target"]}')
    return "\n".join(lines)


def drawio(graph, view="backbone"):
    layout = graph_layout(graph)
    mxfile = ET.Element("mxfile", host="ConceptRelationDraw")
    diagram = ET.SubElement(mxfile, "diagram", id="concept-map", name="主干关系" if view == "backbone" else "全部关系")
    model = ET.SubElement(diagram, "mxGraphModel", grid="1", page="0")
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")
    for node in graph["concepts"]:
        point = layout["positions"][node["id"]]
        cell = ET.SubElement(root, "mxCell", id=node["id"], value=node["label"], vertex="1", parent="1",
                             style="rounded=1;whiteSpace=wrap;html=0;fillColor=#e8f1ed;strokeColor=#488879;fontSize=14;")
        ET.SubElement(cell, "mxGeometry", x=str(point["x"] - NODE_WIDTH / 2), y=str(point["y"] - NODE_HEIGHT / 2),
                      width=str(NODE_WIDTH), height=str(NODE_HEIGHT), **{"as": "geometry"})
    for edge in visible_edges(graph, view):
        arrow = "none" if edge["type"] in ("related", "contrasts") else "block"
        route = layout["routes"][edge["id"]]
        source, target = layout["positions"][edge["source"]], layout["positions"][edge["target"]]
        exit_side = 1 if route[0][0] > source["x"] else 0
        entry_side = 1 if route[-1][0] > target["x"] else 0
        cell = ET.SubElement(root, "mxCell", id=edge["id"], value=RELATION_LABELS[edge["type"]], edge="1", parent="1",
                             source=edge["source"], target=edge["target"],
                             style=f"noEdgeStyle=1;rounded=1;html=0;endArrow={arrow};strokeColor=#79918b;"
                                   f"exitX={exit_side};exitY=0.5;entryX={entry_side};entryY=0.5;")
        geometry = ET.SubElement(cell, "mxGeometry", relative="1", **{"as": "geometry"})
        points = ET.SubElement(geometry, "Array", **{"as": "points"})
        for horizontal, vertical in route[1:-1]:
            ET.SubElement(points, "mxPoint", x=str(horizontal), y=str(vertical))
    return ET.tostring(mxfile, encoding="unicode", xml_declaration=True)


def svg(graph, view="backbone"):
    layout = graph_layout(graph)
    edges = visible_edges(graph, view)
    caption = f'{"主干关系" if view == "backbone" else "全部关系"} · 显示 {len(edges)} / {len(visible_edges(graph))} 条；完整证据与复核记录见 JSON。'
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout["width"]}" height="{layout["height"]}" viewBox="0 0 {layout["width"]} {layout["height"]}">',
             '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#79918b"/></marker></defs>',
             '<rect width="100%" height="100%" fill="#fafbf8"/>',
             '<g font-family="Arial,Microsoft YaHei,sans-serif">',
             '<text x="45" y="50" font-size="24" fill="#253e37">文档概念关系图</text>',
             f'<text x="45" y="78" font-size="12" fill="#6b7a74">{escape(caption)}</text>']
    for edge in edges:
        route = layout["routes"][edge["id"]]
        points = " ".join(f"{horizontal},{vertical}" for horizontal, vertical in route)
        arrow = '' if edge["type"] in ("related", "contrasts") else ' marker-end="url(#arrow)"'
        title = escape(f'{edge["source"]} {RELATION_LABELS[edge["type"]]} {edge["target"]}：{edge["explanation"]}')
        parts.append(f'<polyline points="{points}" fill="none" stroke="#79918b" stroke-linejoin="round"{arrow}><title>{title}</title></polyline>')
    for index, node in enumerate(graph["concepts"]):
        point = layout["positions"][node["id"]]
        horizontal, vertical = point["x"], point["y"]
        parts.append(f'<rect x="{horizontal-88}" y="{vertical-28}" width="176" height="56" rx="12" fill="#f3f7f3" stroke="{COLORS[index % len(COLORS)]}"/>')
        label = node["label"]
        lines = [label] if len(label) <= 12 else [label[:12], label[12:23] + ("…" if len(label) > 23 else "")]
        parts.append(f'<text x="{horizontal}" text-anchor="middle" font-size="13" fill="#253e37"><title>{escape(label)}</title>')
        for line_index, line in enumerate(lines):
            parts.append(f'<tspan x="{horizontal}" y="{vertical + 5 + line_index*18 - (9 if len(lines)>1 else 0)}">{escape(line)}</tspan>')
        parts.append('</text>')
    parts.append("</g></svg>")
    return "".join(parts)
