#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pdfcode.py —— 从 PDF 的等宽字体（CourierNew）文字层直接抽取代码块。

背景（见 README.md）：
  OSTEP 内嵌了真实文字层：正文 SimSun（ToUnicode 有系统性错字，如「时」→「谁」），
  但**代码行用 CourierNew**，该层 100% 干净（0 个损坏 CJK 字符、缩进保留）。
  因此代码块不应走 OCR，而应直接取文字层。

产物：code_pdf.json
  { pageIndex: [ {"y0":<px>, "y1":<px>, "lines":[...]} , ... ] }
  y 坐标为 OCR 像素空间（= PDF pt × dpi/72），便于与 ocr_pages.jsonl 对齐。
"""
import json, os, statistics, sys

import pymupdf

SHORT_WORDS = ("prompt>", "gcc", "./")


def code_lines_of_page(page, mono="Courier"):
    """返回该页所有等宽字体的逻辑行：[{y0,y1,x0,text,size}]，按 y 排序。"""
    out = []
    d = page.get_text("dict")
    for b in d.get("blocks", []):
        for l in b.get("lines", []):
            if not any(mono in s["font"] for s in l["spans"]):
                continue
            # 只保留等宽 span，避免混排
            txt = "".join(s["text"] for s in l["spans"] if mono in s["font"])
            if not txt.strip():
                # 保留空行（块内空行有意义），但记录
                pass
            x0, y0, x1, y1 = l["bbox"]
            out.append({"x0": x0, "y0": y0, "y1": y1, "text": txt, "size": l["spans"][0]["size"]})
    out.sort(key=lambda r: (round(r["y0"], 1), r["x0"]))
    return out


def group_regions(rows):
    """把等宽行按 y 聚类为代码区（允许块内空行；用中位行距判定断开）。"""
    if not rows:
        return []
    gaps = [rows[i]["y0"] - rows[i - 1]["y0"] for i in range(1, len(rows))]
    pos = [g for g in gaps if g > 0.5]
    med = statistics.median(pos) if pos else 10.0
    thr = max(med * 2.6, 12)      # 超过 ~2.6 倍行距即认为是跨块间距
    regions, cur = [], [rows[0]]
    for r in rows[1:]:
        if r["y0"] - cur[-1]["y0"] > thr:
            regions.append(cur); cur = [r]
        else:
            cur.append(r)
    regions.append(cur)
    return [g for g in regions if any(x["text"].strip() for x in g)]


def dedupe_lines(group):
    """同一逻辑行可能被拆成多个 span/line，按 y 合并。"""
    by_y = {}
    for r in group:
        k = round(r["y0"], 1)
        by_y.setdefault(k, []).append(r)
    out = []
    for y in sorted(by_y):
        parts = sorted(by_y[y], key=lambda r: r["x0"])
        txt = "".join(p["text"] for p in parts)
        out.append(txt.rstrip())
    # 去掉首尾空行、折叠连续空行
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    ded = []
    for l in out:
        if not l.strip() and (not ded or not ded[-1].strip()):
            continue
        ded.append(l)
    return ded


def extract(pdf_path, dpi=220, mono="Courier"):
    doc = pymupdf.open(pdf_path)
    result = {}
    for pno in range(doc.page_count):
        page = doc[pno]
        rows = code_lines_of_page(page, mono)
        if len(rows) < 2:
            continue
        scale = dpi / 72.0
        regs = []
        for g in group_regions(rows):
            lines = dedupe_lines(g)
            if len(lines) < 2:
                continue
            regs.append({
                "y0": round(g[0]["y0"] * scale, 1),
                "y1": round(g[-1]["y1"] * scale, 1),
                "lines": lines,
            })
        if regs:
            result[pno] = regs
    return result


def main():
    ap_pdf = sys.argv[1] if len(sys.argv) > 1 else r"C:/Users/Lenovo/Desktop/操作系统导论(三座大山中文版).pdf"
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "ostep", "code_pdf.json")
    data = extract(ap_pdf)
    json.dump(data, open(out, "w", encoding="utf-8"), ensure_ascii=False)
    nreg = sum(len(v) for v in data.values())
    nline = sum(len(r["lines"]) for v in data.values() for r in v)
    print(f"pages with code: {len(data)}  regions: {nreg}  code lines: {nline} -> {out}")
    for pno in sorted(data)[:2]:
        print("== page", pno)
        for r in data[pno]:
            print("  region y%.0f-%.0f" % (r["y0"], r["y1"]))
            for l in r["lines"][:14]:
                print("     |", l[:78])


if __name__ == "__main__":
    main()
