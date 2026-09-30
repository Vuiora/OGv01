"""Make a local, dependency-free SVG/HTML preview of TPWF's native draw.io pages."""
import argparse
import html
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def preview(source: Path):
    root = ET.fromstring(source.read_bytes())
    pages = []
    for diagram in root.findall("diagram"):
        graph = diagram.find("mxGraphModel")
        cells = {}
        for obj in graph.find("root"):
            cell = obj if obj.tag == "mxCell" else obj.find("mxCell")
            if cell is not None:
                cells[obj.get("id")] = (obj, cell)
        boxes = {}
        def rect(ident):
            if ident in boxes:
                return boxes[ident]
            obj, cell = cells[ident]
            geometry = cell.find("mxGeometry")
            x, y, w, h = [float(geometry.get(k, 0)) for k in ("x", "y", "width", "height")]
            parent = cell.get("parent")
            if parent not in {"0", "1", None}:
                px, py, _, _ = rect(parent)
                x, y = x + px, y + py
            boxes[ident] = (x, y, w, h)
            return boxes[ident]
        width, height = graph.get("pageWidth"), graph.get("pageHeight")
        svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">',
            '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"/></marker></defs>',
            '<rect width="100%" height="100%" fill="white"/>']
        for ident, (obj, cell) in cells.items():
            if cell.get("vertex") != "1":
                continue
            x, y, w, h = rect(ident)
            style = dict(item.split("=", 1) for item in cell.get("style", "").split(";") if "=" in item)
            fill, stroke = style.get("fillColor", "white"), style.get("strokeColor", "black")
            svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}"/>')
            rows = [html.unescape(re.sub(r"<[^>]+>", "", s)) for s in re.split(r"<br\s*/?>", obj.get("label", ""))]
            top = style.get("verticalAlign") == "top"
            tx = x + 12 if top else x + w / 2
            ty = y + 28 if top else y + h / 2 - (len(rows)-1)*11
            anchor = "start" if top else "middle"
            weight = "bold" if style.get("fontStyle") == "1" else "normal"
            for i, row in enumerate(rows):
                svg.append(f'<text x="{tx}" y="{ty+i*22}" text-anchor="{anchor}" font-family="Microsoft YaHei, sans-serif" font-size="16" font-weight="{weight}" fill="{style.get("fontColor", "#202020")}">{html.escape(row)}</text>')
        for ident, (obj, cell) in cells.items():
            if cell.get("edge") != "1":
                continue
            style = dict(item.split("=", 1) for item in cell.get("style", "").split(";") if "=" in item)
            sx, sy, sw, sh = rect(cell.get("source"))
            tx, ty, tw, th = rect(cell.get("target"))
            points = [(sx+sw*float(style.get("exitX",1)), sy+sh/2)]
            points += [(float(p.get("x")), float(p.get("y"))) for p in cell.findall("mxGeometry/Array/mxPoint")]
            points += [(tx+tw*float(style.get("entryX",0)), ty+th/2)]
            route = " ".join(f"{x},{y}" for x,y in points)
            dash = 'stroke-dasharray="6 4"' if style.get("dashed") == "1" else ""
            svg.append(f'<polyline points="{route}" fill="none" stroke="{style.get("strokeColor", "black")}" stroke-width="2" {dash} marker-end="url(#arrow)"/>')
        svg.append("</svg>")
        pages.append({"name": diagram.get("name"), "svg": "".join(svg)})
    payload = json.dumps(pages, ensure_ascii=False).replace("<", "\\u003c")
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>OGv01 · TPWF 架构预览</title>
<style>body{margin:0;background:#eef1f5;font:16px "Microsoft YaHei",sans-serif;color:#18273b}header{background:#fff;padding:18px 24px;display:flex;align-items:center;gap:18px;border-bottom:1px solid #ccd5e0}h1{font-size:20px;margin:0}select,button{font:inherit;padding:7px}a{color:#225dbb}#viewport{overflow:auto;height:calc(100vh - 110px);padding:20px}svg{display:block;max-width:none;box-shadow:0 3px 15px #0001}footer{padding:8px 24px;font-size:13px}header span{margin-left:auto;font-size:13px}</style>
<header><h1>OGv01 业务闭环</h1><select id="page" aria-label="架构页面"></select><button id="fit">适应窗口</button><button id="plus">放大</button><button id="minus">缩小</button><span><a href="architecture.drawio">可编辑 draw.io</a> · <a href="source-manifest.json">来源清单</a></span></header>
<div id="viewport"><div id="canvas"></div></div><footer>原生 TPWF 图的本地预览。总览与模块页可切换；连线颜色沿用 TheStructure 模板。节点证据详见 architecture.json。</footer>
<script>const pages=PAYLOAD;const p=document.querySelector('#page'),c=document.querySelector('#canvas'),v=document.querySelector('#viewport');let scale=1,svg;
pages.forEach((x,i)=>{const o=document.createElement('option');o.value=i;o.textContent=x.name;p.append(o)});
function size(){svg.style.width=(Number(svg.viewBox.baseVal.width)*scale)+'px';svg.style.height=(Number(svg.viewBox.baseVal.height)*scale)+'px'}
function fit(){scale=(v.clientWidth-40)/svg.viewBox.baseVal.width;size()}
function show(){c.innerHTML=pages[p.value].svg;svg=c.querySelector('svg');fit();v.scrollTo(0,0)}p.onchange=show;document.querySelector('#fit').onclick=fit;document.querySelector('#plus').onclick=()=>{scale*=1.25;size()};document.querySelector('#minus').onclick=()=>{scale/=1.25;size()};show();</script></html>'''.replace("PAYLOAD", payload)
    target = source.with_name("architecture-preview.html")
    target.write_text(document, encoding="utf-8")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    print(preview(args.source))
