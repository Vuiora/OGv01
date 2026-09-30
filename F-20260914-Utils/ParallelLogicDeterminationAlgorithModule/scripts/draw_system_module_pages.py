"""Draw the three outermost modules and preserve the existing PLDA second page.

A module is a maximal visual region other than the background, not an internal
PLDA responsibility. Original nodes and internal edges are copied from the
pre-correction overview; cross-boundary endpoints become linked interface boxes.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import html
import json
import re
import shutil
import xml.etree.ElementTree as ET

from draw_plda_architecture import FILE, MODULE, PREFIX, node, line, text, BLUE, BLACK, RED, GREEN, PINK, PURPLE

DEST = MODULE / "docs/diagrams"
BACKUP = DEST / "TheStructure.before-system-module-pages-20260915.drawio"
PLDA_PAGE = "plda-architecture-dataflow-v1"
GROUPS = [
    dict(key="device", name="DEVICE_1", root=PREFIX+"75", index=3,
         x=40, y=170, scale=.85, width=1400, height=1530),
    dict(key="communication", name="通信基础设施", root=PREFIX+"14", index=4,
         x=530, y=180, scale=1.4, width=2280, height=1530),
    dict(key="data-processing", name="数据处理模块", root=PREFIX+"1", index=5,
         x=590, y=200, scale=2.1, width=1640, height=1420),
]
for group in GROUPS:
    group["page_id"] = f"system-module-{group['key']}-v1"


def segments(blob):
    return re.findall(rb"<diagram\b[^>]*>.*?</diagram>", blob, flags=re.DOTALL)


def style_get(cell, key, default=None):
    entries = dict(p.split("=", 1) for p in cell.get("style", "").split(";") if "=" in p)
    return entries.get(key, default)


def link(root, cell, page):
    i = list(root).index(cell)
    obj = ET.Element("object", id=cell.attrib.pop("id"), label=cell.attrib.pop("value", ""), link=f"data:page/id,{page}")
    root.remove(cell)
    obj.append(cell)
    root.insert(i, obj)


def plain(value):
    return html.unescape(re.sub("<[^>]*>", "", value.replace("<br>", " / ")))


def bounds(cell, cells):
    g = cell.find("mxGeometry")
    x, y = float(g.get("x", 0)), float(g.get("y", 0))
    parent = cell.get("parent")
    if parent not in {None, "0", "1"}:
        px, py, _, _ = bounds(cells[parent], cells)
        x, y = x + px, y + py
    return x, y, float(g.get("width", 0)), float(g.get("height", 0))


def inside(inner, outer):
    x, y, w, h = inner
    a, b, c, d = outer
    return x >= a and y >= b and x+w <= a+c+.001 and y+h <= b+d+.001


def transform(cell, scale, dx, dy, fonts=False):
    c = deepcopy(cell)
    g = c.find("mxGeometry")
    if c.get("vertex") == "1":
        absolute = c.get("parent") == "1"
        for key in ("x", "y", "width", "height"):
            if g.get(key) is not None:
                value = float(g.get(key)) * scale
                if absolute and key in {"x", "y"}:
                    value += dx if key == "x" else dy
                g.set(key, f"{value:g}")
    else:
        for point in g.findall(".//mxPoint"):
            if point.get("as") != "offset":
                for axis, delta in (("x", dx), ("y", dy)):
                    if point.get(axis) is not None:
                        point.set(axis, f"{float(point.get(axis))*scale+delta:g}")
    if fonts:
        size = float(style_get(c, "fontSize", 12))*scale
        if c.get("vertex") == "1":
            c.set("style", c.get("style", "") + f"fontFamily=Microsoft YaHei;fontSize={size:g};")
            c.set("value", re.sub(r"font-size:\s*([\d.]+)px", lambda m: f"font-size: {float(m[1])*scale:g}px", c.get("value", "")))
    return c


def legend(root, y, width):
    text(root, "system-legend-title", "数据线颜色沿用原图；外部接口框只是连接说明，不作为本页模块。", 30, y, width-60, 32, 17)
    entries = [(BLUE, "原始数据"), (BLACK, "标准数据"), (RED, "分析数据"), (GREEN, "执行任务 / 设备数据"), (PINK, "结果 / 快速通道")]
    for i, (color, label) in enumerate(entries):
        x = 30 + i*((width-60)/5)
        a, b = f"system-key-a-{i}", f"system-key-b-{i}"
        node(root, a, "", x, y+58, 1, 1, "none", "none", "opacity=0;")
        node(root, b, "", x+50, y+58, 1, 1, "none", "none", "opacity=0;")
        e = line(root, f"system-key-{i}", a, b, color, exit=(1,.5), entry=(0,.5))
        e.set("style", e.get("style").replace("edgeStyle=orthogonalEdgeStyle;", "edgeStyle=none;noEdgeStyle=1;"))
        text(root, f"system-key-text-{i}", label, x+65, y+42, (width-60)/5-75, 35, 16)


def overview(source):
    page = deepcopy(source)
    page.set("name", "系统总览")
    model = page.find("mxGraphModel")
    model.set("pageWidth", "2380")
    model.set("pageHeight", "1740")
    root = model.find("root")
    for cell in list(root):
        if cell.get("vertex") == "1" or cell.get("edge") == "1":
            index = list(root).index(cell)
            root.remove(cell)
            root.insert(index, transform(cell, 1, 1650, 150))
    cells = {c.get("id"): c for c in root}
    text(root, "system-title", "系统总览 · 三个顶层模块", 30, 22, 1700, 56, 34, bold=True)
    text(root, "system-subtitle", "模块 = 除背景板外，不被其他区域包含的最大区域。PLDA 位于通信基础设施内部；其详图保留在第 2 页。", 30, 90, 2300, 40, 20)
    for i, group in enumerate(GROUPS):
        cell = cells[group["root"]]
        cell.set("style", cell.get("style") + "fontSize=20;fontStyle=1;")
        link(root, cell, group["page_id"])
        nav = node(root, f"system-nav-{i}", f"<b>{group['name']}</b><br>查看独立页 · 第 {group['index']} 页 ↗",
                   30+i*790, 1510, 750, 76, "#ffffff", "#7f8c8d", "fontSize=21;")
        link(root, nav, group["page_id"])
    link(root, cells[PREFIX+"83"], PLDA_PAGE)
    legend(root, 1630, 2380)
    return page


def module_page(group, cells, owners, edges, overview_id):
    page = ET.Element("diagram", id=group["page_id"], name=group["name"])
    model = ET.SubElement(page, "mxGraphModel", dx=str(group["width"]), dy=str(group["height"]), grid="1", gridSize="10",
                          guides="1", tooltips="1", connect="1", arrows="1", fold="1", page="1", pageScale="1",
                          pageWidth=str(group["width"]), pageHeight=str(group["height"]), math="0", shadow="0")
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")
    members = {key for key, owner in owners.items() if owner == group["key"]}
    gx, gy, gw, gh = bounds(cells[group["root"]], cells)
    scale = group["scale"]
    dx, dy = group["x"]-gx*scale, group["y"]-gy*scale
    drawn = {}
    for key, cell in cells.items():
        if key in members:
            copy = transform(cell, scale, dx, dy, fonts=True)
            if key == group["root"]:
                copy.set("style", copy.get("style") + "fontSize=24;fontStyle=1;")
            elif group["key"] == "device":
                copy.set("style", copy.get("style") + "fontSize=22;")
            root.append(copy)
            drawn[key] = copy
    internals = [e for e in edges if e.get("source") in members and e.get("target") in members]
    crossing = [e for e in edges if (e.get("source") in members) != (e.get("target") in members)]
    for edge in internals:
        root.append(transform(edge, scale, dx, dy))
    if PREFIX+"83" in members:
        link(root, drawn[PREFIX+"83"], PLDA_PAGE)

    # Peers are represented once, retaining distinct opposite-direction edges.
    peers = {e.get("target") if e.get("source") in members else e.get("source") for e in crossing}
    peer_sides = {}
    for peer in peers:
        peer_sides[peer] = "left" if GROUPS.index(next(g for g in GROUPS if g["key"] == owners[peer])) < GROUPS.index(group) else "right"
    port_boxes = {}
    for side in ("left", "right"):
        subset = sorted([p for p in peers if peer_sides[p] == side], key=lambda p: (bounds(cells[p], cells)[1], p))
        for i, peer in enumerate(subset):
            owner = next(g for g in GROUPS if g["key"] == owners[peer])
            if group["key"] == "device":
                x, y, w, h = 1070, 650, 280, 140
            elif group["key"] == "communication":
                x, y, w, h = (30 if side == "left" else 1910), 270+i*(800/max(1,len(subset)-1)), 320, 110
            else:
                x, y, w, h = 30, 220+i*140, 300, 110
            label = plain(cells[peer].get("value", peer))
            if peer == "plda-overview-dispatch":
                label = "负载均衡 / PLDA<br>数据分发 / 结果回传"
            port_id = "system-interface-"+peer
            port = node(root, port_id, f"<b>外部接口 · {owner['name']}</b><br>{label}", x,y,w,h,
                        "#ffffff", "#808080", "fontSize=17;spacing=8;dashed=1;")
            link(root, port, owner["page_id"])
            port_boxes[peer] = (port_id, x,y,w,h)

    for i, edge in enumerate(crossing):
        is_output = edge.get("source") in members
        local = edge.get("source" if is_output else "target")
        peer = edge.get("target" if is_output else "source")
        port_id, px, py, pw, ph = port_boxes[peer]
        side = peer_sides[peer]
        lx,ly,lw,lh = bounds(cells[local], cells)
        lx,ly,lw,lh = lx*scale+dx, ly*scale+dy, lw*scale, lh*scale
        port_anchor = (1 if side == "left" else 0, .5)
        local_anchor = (0 if side == "left" else 1, .5)
        port_y = py+ph/2
        local_y = ly+lh/2
        lane = (group["x"]-55-(i%8)*16) if side == "left" else (group["x"]+gw*scale+55+(i%8)*16)
        path = [(lane,local_y), (lane,port_y)]
        # Data model and fast channel use their bottom ports so lines do not
        # cross the intervening fast-channel / source-data rectangles.
        if group["key"] == "data-processing" and local in {PREFIX+"17", PREFIX+"19"}:
            local_anchor = (.5,1)
            bottom = group["y"]+gh*scale+(60 if local == PREFIX+"17" else 100)
            path = [(lx+lw/2,bottom),(lane,bottom),(lane,port_y)]
        if not is_output:
            path.reverse()
        color = style_get(edge, "strokeColor", BLACK)
        line(root, edge.get("id"), local if is_output else port_id, port_id if is_output else local, color,
             points=path, exit=local_anchor if is_output else port_anchor, entry=port_anchor if is_output else local_anchor,
             dashed=style_get(edge, "dashed") == "1", label=edge.get("value", ""), width=2)

    text(root, "system-title", group["name"]+" · 独立模块页", 30, 24, group["width"]-430, 54, 32, bold=True)
    back = node(root, "system-back", "↗ 返回三模块总览", group["width"]-370, 30, 330, 48, "#dae8fc", "#6c8ebf", "fontSize=20;")
    link(root, back, overview_id)
    text(root, "system-description", "保留原图中本模块包含的全部子区域；跨模块连线连接到带名称的外部接口。点击接口可查看对方模块。",
         30, 105, group["width"]-60, 42, 18)
    if group["key"] == "communication":
        detail = node(root, "system-detail-link", "↗ PLDA 内部架构与数据流 · 保留的第 2 页", 530, 1290, 1064, 56, "#fff2cc", "#d6b656", "fontSize=22;")
        link(root, detail, PLDA_PAGE)
    legend(root, group["height"]-130, group["width"])
    return page, {"module":group["name"], "page_id":group["page_id"], "page_index":group["index"],
                  "member_ids":sorted(members), "internal_edge_ids":[e.get("id") for e in internals],
                  "cross_boundary_edge_ids":[e.get("id") for e in crossing], "interface_count":len(peers)}


def validate(blob, preserved_second, source_ids):
    pages = ET.fromstring(blob).findall("diagram")
    page_ids = {p.get("id") for p in pages}
    assert len(page_ids) == len(pages) == 5
    assert segments(blob)[1] == preserved_second, "second page changed"
    assert source_ids <= {c.get("id") for c in pages[0].find("mxGraphModel/root")}, "original overview cell removed"
    links = 0
    for p in pages:
        root = p.find("mxGraphModel/root")
        ids = {c.get("id") for c in root}
        assert len(ids) == len(root) and None not in ids
        for item in root:
            cell = item if item.tag == "mxCell" else item.find("mxCell")
            for attr in ("parent", "source", "target"):
                assert cell.get(attr) is None or cell.get(attr) in ids, (p.get("name"),attr,cell.attrib)
            if item.get("link"):
                assert item.get("link").removeprefix("data:page/id,") in page_ids
                links += 1
    return links


def main():
    before = FILE.read_bytes()
    current = ET.fromstring(before)
    current_pages = current.findall("diagram")
    second = next(s for s in segments(before) if ET.fromstring(s).get("id") == PLDA_PAGE)
    if not BACKUP.exists():
        shutil.copy2(FILE, BACKUP)
    baseline = ET.parse(BACKUP).getroot().findall("diagram")[0]
    source_root = baseline.find("mxGraphModel/root")
    cells = {c.get("id"): c for c in source_root}
    edges = [c for c in source_root if c.get("edge") == "1"]
    owners = {}
    for key, cell in cells.items():
        if cell.get("vertex") != "1":
            continue
        matches = [g["key"] for g in GROUPS if inside(bounds(cell,cells), bounds(cells[g["root"]],cells))]
        assert len(matches) == 1, (key,matches)
        owners[key] = matches[0]
    # Only the previous generated PLDA function pages are removed. Any unexpected
    # page is retained by refusing to overwrite, instead of silently discarding it.
    allowed = {baseline.get("id"), PLDA_PAGE, "plda-modules-overview-v1"} | {g["page_id"] for g in GROUPS}
    for p in current_pages:
        assert p.get("id") in allowed or p.get("id", "").startswith("plda-module-"), "unexpected user page"
    first = overview(baseline)
    generated, records = [], []
    for group in GROUPS:
        page, record = module_page(group,cells,owners,edges,baseline.get("id"))
        generated.append(page)
        records.append(record)
    def serialize(page):
        ET.indent(page, space="  ")
        return ET.tostring(page,encoding="utf-8")
    attrs = dict(current.attrib)
    attrs["pages"] = "5"
    root_xml = ET.tostring(ET.Element("mxfile", attrs), encoding="utf-8")
    opening = root_xml.removesuffix(b" />") + b">"
    after = opening+b"\n"+serialize(first)+b"\n"+second+b"\n"+b"\n".join(serialize(p) for p in generated)+b"\n</mxfile>\n"
    link_count = validate(after, second, set(cells))
    assert FILE.read_bytes() == before, "source changed during generation; retry using the latest file"
    temporary = FILE.with_suffix(".drawio.tmp")
    temporary.write_bytes(after)
    temporary.replace(FILE)
    report = {"file":str(FILE), "page_count":5, "module_count":3, "definition":"除背景板外，不被其他区域包含的最大区域",
              "modules":records, "native_page_links":link_count, "preserved_page2_sha256":sha256(second).hexdigest(),
              "second_page_byte_identical":True, "original_overview_cell_ids_preserved":True}
    (DEST/"module-pages.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"pages":5,"modules":3,"page_links":link_count,"second_page_byte_identical":True},ensure_ascii=True))


if __name__ == "__main__":
    main()
