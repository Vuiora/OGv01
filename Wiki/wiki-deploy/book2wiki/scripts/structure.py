#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
structure.py —— 从 PDF 书签(TOC) + OCR 行坐标重建「节」结构，产出 pageindex。

两本书的结构策略：
- OSTEP：TOC 是扁平的章级列表（前言/目录/50 章/附录 A-G）。原书每「章」即最小
  教学单元（平均 ~10 页），按「章一页」组织最自然，符合「一节一页」。
- 图论：TOC 有 2-3 级。按章(level2)切分；每章内的 level3 节用二级标题呈现，
  练习/注解并入所在章页。附录 A/B、索引单独成页或合并。

页码校准：本任务两本书的 TOC 页码偏移均为 0，即 page_index = toc_page - 1。
（脚本仍做一次自动探测，防止其他 PDF 有偏移。）

输出：pageindex.json
  [{ "slug": "ch07", "title": "07 进程调度：介绍", "kind": "chapter",
     "start": 59, "end": 68, "sections": [ {title, page} ... ] }, ... ]
"""
import os, re, json, sys, argparse
import fitz

CN_NUM = "零一二三四五六七八九十"

def detect_offset(doc, toc):
    """探测 TOC 页码偏移：对前若干条目，尝试 offset ∈ {-2,-1,0,1,2}，
    选让 TOC 标题（去掉空格/编号后子串）最常命中的偏移。"""
    def norm(s):
        return re.sub(r"[\s　·、，,：:．.]+", "", s)
    best, bestscore = 0, -1
    for off in (-2, -1, 0, 1, 2):
        score = 0
        for lvl, title, pg in toc[:40]:
            idx = pg - 1 + off
            if not (0 <= idx < doc.page_count):
                continue
            t = norm(doc[idx].get_text())
            key = norm(title)
            # 取标题前 4-6 个字符做子串命中
            for k in (key[:6], key[:4], key[:3]):
                if len(k) >= 3 and k in t:
                    score += 1
                    break
        if score > bestscore:
            bestscore, best = score, off
    return best, bestscore

def load_toc(pdf):
    doc = fitz.open(pdf)
    toc = doc.get_toc()
    off, score = detect_offset(doc, toc)
    n = doc.page_count
    doc.close()
    return toc, off, n, score

# ---------- OSTEP ----------
def build_ostep(pdf):
    toc, off, n, score = load_toc(pdf)
    print(f"[ostep] toc={len(toc)} offset={off} (score={score}) pages={n}")
    # OSTEP toc 是扁平 level1；过滤「目录」条目。附录保留。
    items = [(title.strip(), pg) for lvl, title, pg in toc if lvl == 1]
    items = [it for it in items if it[0] not in ("目录",)]
    # 合并跨「部分」页（虚拟化/并发/持久性 等是分节封面，无独立内容需求时并入下一章区间）
    parts = {"虚拟化", "并发", "持久性"}
    pages = []
    for i, (title, pg) in enumerate(items):
        start = pg - 1 + off
        # 结束页 = 下一条目起始 - 1（末尾用书末）
        if i + 1 < len(items):
            end = items[i + 1][1] - 1 + off - 1
        else:
            end = n - 1
        if title in parts:
            continue  # 部分封面页并入紧邻下一章（其内容很短）
        pages.append({
            "slug": ostep_slug(title),
            "title": title,
            "kind": "chapter",
            "start": start,
            "end": end,
            "sections": [],
        })
    return pages, off, n

def ostep_slug(title):
    m = re.match(r"^(\d+)", title)
    if m:
        return "ch%02d" % int(m.group(1))
    if title.startswith("附录"):
        return "app" + title[2]
    if "前言" in title:
        return "preface"
    return "intro"

# ---------- 图论 ----------
def build_graphtheory(pdf):
    toc, off, n, score = load_toc(pdf)
    print(f"[gt] toc={len(toc)} offset={off} (score={score}) pages={n}")
    # 先把 level2 原始条目取出，按类型分流
    lvl2 = [(t.strip(), pg - 1 + off) for lvl, t, pg in toc if lvl == 2]
    lvl3 = [(t.strip(), pg - 1 + off) for lvl, t, pg in toc if lvl == 3]
    # 判断每个 level3 属于哪个 level2（按页范围）
    def owner_l2(page):
        for i in range(len(lvl2)):
            end = lvl2[i + 1][1] - 1 if i + 1 < len(lvl2) else n - 1
            if lvl2[i][1] <= page <= end:
                return i
        return None

    FRONT_WORDS = ("译者序", "关于第", "第一版前言", "前言")
    pages = []
    front = None
    for i, (t, start) in enumerate(lvl2):
        if any(w in t for w in FRONT_WORDS):
            if front is None:
                front = {"slug": "front", "title": "前言与译者序", "kind": "front",
                         "start": start, "end": n - 1, "sections": []}
                pages.append(front)
            continue
        slug = gt_slug(t)
        # 「练习」「注解」若是第12章的（其前一个 level2 是 ch12 且本 entry 无编号），并入前一章
        m = re.match(r"^第(\d+)章", t)
        if m is None and slug == "front" and pages:
            # 无编号的 level2（练习/注解）——并入现有页区间的归属章
            tgt = None
            for p in pages:
                if p["slug"].startswith("ch") and p["start"] < start:
                    tgt = p
            if tgt is not None:
                tgt["sections"].append({"title": t, "page": start})
                continue
        pages.append({"slug": slug, "title": t, "kind": "chapter",
                      "start": start, "end": n - 1, "sections": []})
    # 修正 end
    for i, p in enumerate(pages):
        if i + 1 < len(pages):
            p["end"] = pages[i + 1]["start"] - 1
    # 把 level3 分配进所属 level2 页（按页范围）
    by_start = {p["start"]: p for p in pages}
    for t, page in lvl3:
        for p in pages:
            if p["start"] <= page <= p["end"] and not p["slug"] == "front":
                p["sections"].append({"title": t, "page": page})
                break
    # front 页：合并所有 < 第1章 的条目为单个前言
    for p in pages:
        p["sections"].sort(key=lambda s: s["page"])
    return pages, off, n

def gt_slug(title):
    m = re.match(r"^第(\d+)章", title)
    if m:
        return "ch%02d" % int(m.group(1))
    if title.startswith("附录A"):
        return "appA"
    if title.startswith("附录B"):
        return "appB"
    if "提示" in title:
        return "hints"
    if "索引" in title:
        return "index"
    if "书目" in title:
        return "series"
    return "front"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("book", choices=["ostep", "gt"])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.book == "ostep":
        pdf = r"C:\Users\Lenovo\Desktop\操作系统导论(三座大山中文版).pdf"
        pages, off, n = build_ostep(pdf)
    else:
        pdf = r"C:\Users\Lenovo\Desktop\图论 (原书第五版) (J Reinhard Diestel 著, 于青林 译) (z-library.sk, 1lib.sk, z-lib.sk).pdf"
        pages, off, n = build_graphtheory(pdf)
    json.dump({"book": args.book, "offset": off, "total_pages": n, "pages": pages},
              open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"写出 {len(pages)} 页 -> {args.out}")
    for p in pages[:8]:
        print(f"  {p['slug']:8s} p{p['start']+1}-{p['end']+1}  {p['title']}  (节{len(p['sections'])})")

if __name__ == "__main__":
    main()
