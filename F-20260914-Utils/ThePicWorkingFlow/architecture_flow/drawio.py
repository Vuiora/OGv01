"""Deterministic project-style native mxGraph generation and independent XML lint."""
import base64
from hashlib import sha256
import html
import math
import re
from urllib.parse import unquote
import xml.etree.ElementTree as ET
import zlib

from .models import Architecture, Review, Finding

COLORS = dict(raw="#0000FF", standard="#202020", analysis="#FF0000", dispatch="#005700", result="#FF0080", control="#7B1FA2")
PREFIX = "8rw-NWJHrkYK3E8QMIOX-"
STYLE_SOURCES = dict(device="75", infrastructure="14", data="1", group="54", plda="83", core="53", component="27", payload="2")


def parse(blob):
    if len(blob) > 8_000_000 or b"<!DOCTYPE" in blob.upper() or b"<!ENTITY" in blob.upper():
        raise ValueError("draw.io template exceeds budget or contains XML entities")
    root = ET.fromstring(blob)
    if root.tag != "mxfile":
        raise ValueError("expected mxfile")
    return root


def model(page):
    found = page.find("mxGraphModel")
    if found is not None:
        return found
    packed = base64.b64decode(page.text or "", validate=True)
    decoder = zlib.decompressobj(-15)
    unpacked = decoder.decompress(packed, 8_000_001)
    if len(unpacked) > 8_000_000 or decoder.unconsumed_tail or not decoder.eof:
        raise ValueError("compressed page exceeds budget")
    return ET.fromstring(unquote(unpacked.decode()))


def entries(page):
    output = {}
    for item in model(page).find("root"):
        cell = item if item.tag == "mxCell" else item.find("mxCell")
        if cell is None:
            continue
        output[item.get("id")] = (item, cell)
    return output


def css(style):
    return dict(p.split("=", 1) for p in style.split(";") if "=" in p)


def profile(template):
    first = parse(template).find("diagram")
    values = entries(first)
    palette = {}
    for role, suffix in STYLE_SOURCES.items():
        _, cell = values[PREFIX+suffix]
        style = css(cell.get("style", ""))
        palette[role] = {key: style.get(key, fallback) for key, fallback in
                         (("fillColor", "#ffffff"), ("strokeColor", "#000000"), ("fontColor", "#202020"))}
    return {"name": "OGv01 TheStructure", "source_sha256": sha256(template).hexdigest(),
            "module_definition": "除背景板外，不被其他区域包含的最大区域", "palette": palette,
            "font": "Microsoft YaHei", "font_size": 16, "edge_colors": COLORS,
            "source_cells": {r:PREFIX+s for r,s in STYLE_SOURCES.items()}}


def lines(label, budget=26):
    rows, row, size = [], "", 0
    for char in label:
        width = 2 if ord(char)>255 else 1
        if char == "\n" or size+width > budget:
            rows.append(row)
            row, size = "", 0
            if char == "\n":
                continue
        row, size = row+char, size+width
    if row:
        rows.append(row)
    return rows or [""]


def box(root, ident, label, x,y,w,h, theme, *, parent="1", kind="component", semantic="", module="", link="", role="component", top=False):
    attrs = {"id":ident,"label":"<br>".join(html.escape(s) for s in lines(label, max(12,int(w/10)))),
             "kind":kind,"semantic_id":semantic,"module_id":module,"role":role}
    if link:
        attrs["link"] = "data:page/id,"+link
    obj = ET.SubElement(root,"object",attrs)
    style = ("rounded=0;whiteSpace=wrap;html=1;fontFamily=Microsoft YaHei;fontSize=16;spacing=8;"
             +";".join(f"{k}={v}" for k,v in theme.items())+";"
             +("align=left;verticalAlign=top;fontStyle=1;" if top else "align=center;verticalAlign=middle;"))
    cell=ET.SubElement(obj,"mxCell",vertex="1",parent=parent,style=style)
    ET.SubElement(cell,"mxGeometry",x=str(x),y=str(y),width=str(w),height=str(h),**{"as":"geometry"})
    return obj


def edge(root, ident, source,target,kind,label,points,exit_x,entry_x,semantic):
    obj=ET.SubElement(root,"object",id=ident,label=html.escape(label),kind="connection",semantic_id=semantic,flow=kind)
    color=COLORS[kind]
    style=(f"edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeColor={color};fontColor={color};"
           f"fontFamily=Microsoft YaHei;fontSize=14;labelBackgroundColor=#ffffff;strokeWidth=2;endArrow=block;endFill=1;"
           f"exitX={exit_x};exitY=0.5;entryX={entry_x};entryY=0.5;jumpStyle=arc;jumpSize=6;"
           +("dashed=1;dashPattern=6 4;" if kind=="control" else ""))
    cell=ET.SubElement(obj,"mxCell",edge="1",parent="1",source=source,target=target,style=style)
    geometry=ET.SubElement(cell,"mxGeometry",relative="1",**{"as":"geometry"})
    points_el=ET.SubElement(geometry,"Array",**{"as":"points"})
    for x,y in points:
        ET.SubElement(points_el,"mxPoint",x=str(x),y=str(y))


def new_page(ident,name,width,height):
    page=ET.Element("diagram",id=ident,name=name)
    graph=ET.SubElement(page,"mxGraphModel",grid="1",gridSize="10",page="1",pageScale="1",pageWidth=str(width),pageHeight=str(height),math="0",shadow="0")
    root=ET.SubElement(graph,"root")
    ET.SubElement(root,"mxCell",id="0")
    ET.SubElement(root,"mxCell",id="1",parent="0")
    return page,root


def render(architecture: Architecture, template: bytes, preserved_ids: list[str]):
    p=profile(template)
    palette=p["palette"]
    nodes={n.id:n for n in architecture.components}
    children={k:[] for k in [None]+list(nodes)}
    for n in nodes.values(): children[n.parent].append(n)
    heights={}; label_heights={}
    def measure(n,depth=0,width=450):
        if depth>5: raise ValueError("nesting exceeds renderer limit of 5")
        label_h=max(72,24*len(lines(n.label,max(12,int(width/10))))+24)
        label_heights[n.id]=label_h
        heights[n.id]=label_h+sum(measure(c,depth+1,width-56)+28 for c in children[n.id])
        return heights[n.id]
    module_heights={}; module_headers={}
    for m in architecture.modules:
        module_headers[m.id]=max(72,24*len(lines(m.label,64))+24)
        module_heights[m.id]=module_headers[m.id]+28+sum(measure(n)+36 for n in children[None] if n.module==m.id)
    all_module_boxes={}
    def put_module(root,m,x,y,positions):
        w,h=640,module_heights[m.id]
        box(root,"m-"+m.id,m.label,x,y,w,h,palette[m.kind],kind="module",semantic=m.id,module=m.id,role=m.kind,top=True,link="module-"+m.id)
        def put(n,parent,xx,yy,ww,absx,absy):
            role="payload" if n.role=="data" else n.role
            box(root,"n-"+n.id,n.label,xx,yy,ww,heights[n.id],palette[role],parent=parent,semantic=n.id,module=m.id,role=role,top=bool(children[n.id]))
            positions[n.id]=(absx+xx,absy+yy,ww,heights[n.id])
            cursor=label_heights[n.id]
            for child in children[n.id]:
                put(child,"n-"+n.id,28,cursor,ww-56,absx+xx,absy+yy)
                cursor+=heights[child.id]+28
        cursor=module_headers[m.id]
        for n in children[None]:
            if n.module==m.id:
                put(n,"m-"+m.id,95,cursor,450,x,y)
                cursor+=heights[n.id]+36
        return x,y,w,h

    def header(root,title,width,back=False):
        neutral={"fillColor":"none","strokeColor":"none","fontColor":"#202020"}
        box(root,"title",title,30,20,width-450,70,neutral,kind="title",role="title")
        if back:
            box(root,"back","返回总图 ↗",width-360,30,300,50,palette["infrastructure"],kind="navigation",role="navigation",link="architecture-overview")

    def footer(root,y,width):
        label="蓝：原始数据   黑：标准数据   红：分析数据   绿：执行任务   洋红：结果   紫色虚线：控制 / 异常"
        box(root,"legend",label,30,y,width-60,75,{"fillColor":"#f5f5f5","strokeColor":"#bbbbbb"},kind="legend",role="legend")

    page_w=len(architecture.modules)*860+40
    base_h=max(module_heights.values())+200
    page_h=base_h+len(architecture.connections)*22+150
    overview,root=new_page("architecture-overview","系统总览",page_w,page_h)
    header(root,architecture.title+" · 系统总览",page_w)
    positions={}
    for i,m in enumerate(architecture.modules):
        all_module_boxes[m.id]=put_module(root,m,30+i*860,140,positions)
    for i,e in enumerate(architecture.connections):
        source,target=nodes[e.source],nodes[e.target]
        sx,sy,sw,sh=positions[e.source]; tx,ty,tw,th=positions[e.target]
        sm,tm=all_module_boxes[source.module],all_module_boxes[target.module]
        if source.module==target.module:
            lane=sm[0]+20+(i%9)*6
            points=[(lane,sy+sh/2),(lane,ty+th/2)]
            exit_x,entry_x=0,0
        else:
            forward=sm[0]<tm[0]
            a=sm[0]+sm[2]+45 if forward else sm[0]-25
            b=tm[0]-25 if forward else tm[0]+tm[2]+45
            bus=base_h+i*22
            points=[(a,sy+sh/2),(a,bus),(b,bus),(b,ty+th/2)]
            exit_x,entry_x=(1,0) if forward else (0,1)
        edge(root,"e-"+e.id,"n-"+e.source,"n-"+e.target,e.kind,e.label,points,exit_x,entry_x,e.id)
    footer(root,page_h-100,page_w)
    generated=[overview]
    for m in architecture.modules:
        related=[e for e in architecture.connections if nodes[e.source].module==m.id or nodes[e.target].module==m.id]
        peers=sorted({k for e in related for k in (e.source,e.target) if nodes[k].module!=m.id})
        port_labels={k:"外部接口 · "+next(r.label for r in architecture.modules if r.id==nodes[k].module)+"\n"+nodes[k].label for k in peers}
        port_heights={k:max(100,24*len(lines(port_labels[k],29))+24) for k in peers}
        height=max(module_heights[m.id]+210,sum(port_heights[k]+16 for k in peers)+230)+130
        page,root=new_page("module-"+m.id,m.label,1480,height)
        header(root,m.label+" · 独立模块页",1480,True)
        positions={}
        rect=put_module(root,m,390,140,positions)
        port_y=145
        for k in peers:
            peer=nodes[k]
            box(root,"port-"+k,port_labels[k],1150,port_y,290,port_heights[k],
                {"fillColor":"#ffffff","strokeColor":"#808080"},kind="interface",semantic=k,module=peer.module,role="interface",link="module-"+peer.module)
            positions[k]=(1150,port_y,290,port_heights[k])
            port_y+=port_heights[k]+16
        for i,e in enumerate(related):
            local_source=nodes[e.source].module==m.id
            local_target=nodes[e.target].module==m.id
            sx,sy,sw,sh=positions[e.source];tx,ty,tw,th=positions[e.target]
            if local_source and local_target:
                lane=rect[0]+20+(i%9)*6; exit_x=entry_x=0
            else:
                lane=1070+(i%7)*9
                exit_x,entry_x=(1,0) if local_source else (0,1)
            edge(root,"e-"+e.id,("n-" if local_source else "port-")+e.source,("n-" if local_target else "port-")+e.target,
                 e.kind,e.label,[(lane,sy+sh/2),(lane,ty+th/2)],exit_x,entry_x,e.id)
        footer(root,height-100,1480)
        generated.append(page)
    original={ET.fromstring(seg).get("id"):seg for seg in re.findall(rb"<diagram\b[^>]*>.*?</diagram>",template,re.S)}
    if len(preserved_ids)!=len(set(preserved_ids)) or set(preserved_ids)-original.keys():
        raise ValueError("preserved page IDs missing or duplicated")
    generated_ids={page.get("id") for page in generated}
    if generated_ids & set(preserved_ids): raise ValueError("preserved/generated page ID collision")
    serialized=[]
    for i,page in enumerate(generated):
        ET.indent(page,space="  ")
        serialized.append(ET.tostring(page,encoding="utf-8"))
        if i==0: serialized.extend(original[k] for k in preserved_ids)
    return b'<mxfile host="architecture-flow" pages="'+str(len(serialized)).encode()+b'">\n'+b"\n".join(serialized)+b"\n</mxfile>\n",p


def lint(blob,architecture,style,preserved_ids,template):
    errors=[]
    def error(code,where,message): errors.append(Finding(code=code,location=where,message=message))
    try:
        pages=parse(blob).findall("diagram")
    except (ValueError,ET.ParseError) as exc:
        return Review(passed=False,errors=[Finding(code="XML",location="file",message=str(exc))],page_count=0,module_count=len(architecture.modules))
    page_ids=[p.get("id") for p in pages]
    expected={"architecture-overview",*("module-"+m.id for m in architecture.modules),*preserved_ids}
    if len(page_ids)!=len(set(page_ids)) or set(page_ids)!=expected:
        error("PAGES","file","requires exactly one overview, every module page, and preserved pages")
    originals={ET.fromstring(s).get("id"):s for s in re.findall(rb"<diagram\b[^>]*>.*?</diagram>",template,re.S)}
    actual_segments={ET.fromstring(s).get("id"):s for s in re.findall(rb"<diagram\b[^>]*>.*?</diagram>",blob,re.S)}
    for ident in preserved_ids:
        if actual_segments.get(ident)!=originals.get(ident):error("PRESERVE",ident,"preserved page changed")
    nodes={n.id:n for n in architecture.components}
    for page in pages:
        pid=page.get("id")
        if pid in preserved_ids:continue
        graph=model(page); root=graph.find("root"); items=entries(page)
        if len(items)!=len(root):error("IDS",pid,"duplicate cell IDs")
        mid=pid.removeprefix("module-") if pid.startswith("module-") else None
        expected_nodes={n.id for n in nodes.values() if mid is None or n.module==mid}
        actual_nodes=[o.get("semantic_id") for o,c in items.values() if o.get("kind")=="component"]
        if len(actual_nodes)!=len(set(actual_nodes)) or set(actual_nodes)!=expected_nodes:
            error("OWNERSHIP",pid,"component coverage/ownership mismatch")
        module_objs=[(o,c) for o,c in items.values() if o.get("kind")=="module"]
        expected_modules={m.id for m in architecture.modules if mid is None or m.id==mid}
        if {o.get("semantic_id") for o,c in module_objs}!=expected_modules or len(module_objs)!=len(expected_modules):
            error("MODULES",pid,"incorrect outer module regions")
        expected_edges={e.id:e for e in architecture.connections if mid is None or nodes[e.source].module==mid or nodes[e.target].module==mid}
        actual_edges=[o.get("semantic_id") for o,c in items.values() if c.get("edge")=="1"]
        if set(actual_edges)!=set(expected_edges) or len(actual_edges)!=len(expected_edges):error("EDGES",pid,"missing/duplicate connections")
        geometries={}
        def absolute(ident,seen=None):
            seen=set() if seen is None else seen
            if ident in seen:raise ValueError("parent cycle")
            seen.add(ident)
            o,c=items[ident];g=c.find("mxGeometry")
            x,y,w,h=(float(g.get(k,0)) for k in ("x","y","width","height"))
            parent=c.get("parent")
            if parent not in {None,"0","1"}:
                px,py,_,_=absolute(parent,seen);x+=px;y+=py
            return x,y,w,h
        for ident,(obj,cell) in items.items():
            for attr in ("parent","source","target"):
                if cell.get(attr) and cell.get(attr) not in items:error("ENDPOINT",pid+"/"+ident,"unknown "+attr)
            if obj.get("link") and obj.get("link").removeprefix("data:page/id,") not in page_ids:error("LINK",pid+"/"+ident,"page link target missing")
            kind=obj.get("kind")
            if cell.get("vertex")=="1":
                try:
                    x,y,w,h=absolute(ident)
                    geometries[ident]=(x,y,w,h)
                    if not all(math.isfinite(v) for v in (x,y,w,h)) or min(x,y)<0 or min(w,h)<=0 or x+w>float(graph.get("pageWidth"))+1 or y+h>float(graph.get("pageHeight"))+1:
                        error("BOUNDS",pid+"/"+ident,"invalid geometry or outside page")
                    parent=cell.get("parent")
                    if parent not in {"0","1",None}:
                        px,py,pw,ph=absolute(parent)
                        if x<px or y<py or x+w>px+pw+1 or y+h>py+ph+1:error("CONTAINMENT",pid+"/"+ident,"child outside parent")
                except (KeyError,ValueError,TypeError,AttributeError):error("GEOMETRY",pid+"/"+ident,"invalid geometry hierarchy")
                if kind in {"component","module"}:
                    semantic=nodes.get(obj.get("semantic_id")) if kind=="component" else next((m for m in architecture.modules if m.id==obj.get("semantic_id")),None)
                    expected_role=("payload" if semantic.role=="data" else semantic.role) if kind=="component" and semantic else semantic.kind if semantic else None
                    actual_style=css(cell.get("style","")); expected_style=style["palette"].get(expected_role,{})
                    if obj.get("role")!=expected_role or not expected_style or any(actual_style.get(k)!=v for k,v in expected_style.items()) or actual_style.get("fontFamily")!=style["font"] or actual_style.get("fontSize")!=str(style["font_size"]) or actual_style.get("rounded")!="0":
                        error("STYLE",pid+"/"+ident,"project rectangle / palette / font mismatch")
                    if kind=="component":
                        n=nodes.get(obj.get("semantic_id"))
                        if n and (obj.get("module_id")!=n.module or cell.get("parent")!=("n-"+n.parent if n.parent else "m-"+n.module)):
                            error("OWNERSHIP",pid+"/"+ident,"wrong module or parent")
                    elif cell.get("parent")!="1":error("MODULE_ROOT",pid+"/"+ident,"module must be an outer region")
                elif kind not in {"title","navigation","legend","interface"}:error("UNASSIGNED",pid+"/"+ident,"unclassified free-floating area")
            elif cell.get("edge")=="1":
                e=expected_edges.get(obj.get("semantic_id")); s=css(cell.get("style",""))
                if e:
                    for attr,semantic in (("source",e.source),("target",e.target)):
                        endpoint=items.get(cell.get(attr))
                        if endpoint is None or endpoint[0].get("semantic_id")!=semantic:error("FLOW",pid+"/"+ident,"edge direction/endpoint changed")
                    if s.get("strokeColor")!=COLORS[e.kind] or s.get("endArrow")!="block" or (s.get("dashed")=="1")!=(e.kind=="control"):
                        error("EDGE_STYLE",pid+"/"+ident,"semantic color or arrow style mismatch")
        ids=list(geometries)
        # Detect sibling overlaps: ancestors may visually contain descendants.
        for i,a in enumerate(ids):
            for b in ids[i+1:]:
                ao,ac=items[a];bo,bc=items[b]
                if ac.get("parent")!=bc.get("parent"):continue
                ax,ay,aw,ah=geometries[a];bx,by,bw,bh=geometries[b]
                if min(ax+aw,bx+bw)>max(ax,bx)+1 and min(ay+ah,by+bh)>max(ay,by)+1:
                    error("OVERLAP",pid+"/"+a+"/"+b,"sibling regions overlap")
    return Review(passed=not errors,errors=errors,page_count=len(pages),module_count=len(architecture.modules),
                  warnings=["结构和样式校验不能证明大模型对代码语义的理解完全正确；observed / proposed / external 见 architecture.json。"])
