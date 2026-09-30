#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
book2wiki.py —— 把一本书的 OCR 结果按结构切分成「章/节」页面，生成 wikitext。

流程：
  1. 读 struct_<book>.json（structure.py 产出：每页的起止页码、标题、子节）
  2. 读 ocr_pages.jsonl（ocr_pdf.py 产出）
  3. 对每页 page in [start, end]：layout.parse_page -> blocks
  4. 拼接该「页」所有 blocks；识别子节标题做锚点
  5. 生成 wikitext：标题层级、<syntaxhighlight>、<math>、图注、分类、导航

页面组织（用户选择「一节一页」）：
  OSTEP：一章一页（章即最小教学单元），约 51 页 + 目录页
  图论：一章一页，章内小节用二级标题 + 锚点

公式处理（用户选择「OCR 文本 + <math> 标注」）：
  OCR 出的公式文本力求贴近 LaTeX：把 = + - 等包进 \text{} 或直接用简单数学。
  由于 OCR 公式无法 100% 还原 LaTeX，采用「保留可读文本」策略：
  - 纯符号公式 → 尽量转 <math>
  - 含中文的公式（如 T 周转时间）→ 用 <math>\text{...}</math>
"""
import os, re, json, argparse, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import layout

ROOT = os.path.dirname(os.path.abspath(__file__))

# 跨页重复出现的页眉/噪声模式
NOISE = re.compile(r"^(参考资料|参考文献|习题|练习)\s*$")


def load_ocr(path):
    d = {}
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
            d[o["page"]] = o
        except Exception:
            pass
    return d


def mathify(txt):
    """把 OCR 公式文本转成可接受的 <math> 内容（保守）。

    中文/空白多的用 \text{}，纯符号/拉丁字母数字组合直接放。
    """
    t = txt.strip()
    # 去掉尾部编号 (7.1)
    num = ""
    m = re.search(r"\((\d+[.\-]\d+)\)\s*$", t)
    if m:
        num = m.group(1)
        t = t[:m.start()].strip()
    # 替换常见 OCR 符号
    t = t.replace("—", "-").replace("－", "-").replace("＝", "=").replace("＋", "+")
    return t, num


def wl_escape(s):
    """wikitext 中转义可能被解释为标记的字符（保守：不动中文）。"""
    return s


def clean_title(t):
    """规整标题：去掉 OCR 误加的尾部句点、首尾空白。"""
    return re.sub(r"[\s\u3000]+", " ", t).strip().rstrip(".。").strip()


def render_blocks(blocks, page_title, sections):
    """blocks -> wikitext 文本（不含分类/页脚）。

    sections: [{"title":..., "page":...}] 该书页的子节列表，用于识别标题层级。
    """
    # 合并被 OCR 拆开的连续标题（如 "第7章" + "进程调度：介绍"）
    merged = []
    for b in blocks:
        if (b["type"].startswith("h") and merged and merged[-1]["type"].startswith("h")
                and re.fullmatch(r"第\s?\d+\s?章", merged[-1]["text"].strip())):
            merged[-1] = {"type": merged[-1]["type"],
                          "text": merged[-1]["text"].strip() + " " + b["text"].strip(),
                          "page": merged[-1]["page"]}
            continue
        merged.append(dict(b))
    blocks = merged

    sec_titles = set()
    for s in sections:
        sec_titles.add(re.sub(r"[\s\u3000]+", "", s["title"]))

    def sec_match(lbl):
        """判断标题文本是否命中某个书签节标题（容忍空格/标点差异）。"""
        key = re.sub(r"[\s\u3000.。]+", "", lbl)
        exact = {re.sub(r"[\s\u3000.。]+", "", t) for t in sec_titles}
        if key in exact:
            return True
        # 前缀匹配仅对足够长的标题生效，避免 "1" 命中 "1.1图"
        if len(key) < 3:
            return False
        for k2 in exact:
            if len(k2) >= 3 and (key.startswith(k2) or k2.startswith(key)) \
                    and abs(len(key) - len(k2)) <= 3:
                return True
        return False

    out = []
    emitted_figcaps = set()
    for b in blocks:
        t = b["type"]
        if t.startswith("h"):
            lbl = clean_title(b["text"])
            lvl = int(t[1])
            if lvl == 1 or lbl.startswith("第") or re.match(r"^附录", lbl):
                out.append("== %s ==" % lbl)
            elif sec_match(lbl):
                out.append("== %s ==" % lbl)
            elif re.match(r"^\d+\.\d+", lbl):
                out.append("=== %s ===" % lbl)
            else:
                out.append("== %s ==" % lbl)
        elif t == "code":
            safe = b["text"].replace("</syntaxhighlight>", "</ syntaxhighlight>")
            out.append('<syntaxhighlight lang="c">\n%s\n</syntaxhighlight>' % safe)
            prev_type = "code"
        elif t == "formula":
            tex, num = mathify(b["text"])
            if tex:
                expr = '<math>\\text{%s}</math>' % tex if re.search(r"[\u4e00-\u9fff]", tex) else "<math>%s</math>" % tex
                if num:
                    out.append(': %s <span style="color:#666">(%s)</span>' % (expr, num))
                else:
                    out.append(": %s" % expr)
            prev_type = "formula"
        elif t == "figcap":
            # 若该图注已由 figure 块输出（图形区自带图注），则跳过
            key = re.sub(r"[\s\u3000]+", "", b["text"])
            if key in emitted_figcaps:
                prev_type = "figcap"; continue
            out.append("''%s''" % b["text"])
            prev_type = "figcap"
        elif t == "figure":
            cap = b.get("caption", "").strip()
            fname = b["file"]
            if cap:
                emitted_figcaps.add(re.sub(r"[\s\u3000]+", "", cap))
                out.append("[[File:%s|class=bookfig|alt=%s]]\n\n''%s''"
                           % (fname, cap.replace("|", "｜"), cap))
            else:
                out.append("[[File:%s|class=bookfig]]" % fname)
            prev_type = "figure"
        elif t == "list":
            items = b["text"] if isinstance(b["text"], list) else [b["text"]]
            for it in items:
                s = it.strip()
                # 有序项（1. / (1) / （1） / a) ）→ #；无序项 → *
                m = re.match(r"^(?:(\d+)[.、\)）]|\((\d+)\)|（(\d+)）)\s*(.*)$", s)
                if m:
                    out.append("# " + m.group(4))
                    continue
                m2 = re.match(r"^[\(（]([a-zA-Z])[\)）]\s*(.*)$", s)
                if m2:
                    out.append("# " + m2.group(2))
                    continue
                s2 = re.sub(r"^([·•▪◦]|[-–])\s*", "", s)
                out.append("* " + s2)
            prev_type = "list"
        else:  # p
            lbl = clean_title(b["text"])
            if len(lbl) <= 24 and layout.looks_like_heading(lbl) and sec_match(lbl):
                # 段落恰好等于某个书签节标题 → 提升为节标题（补 OCR 漏判）
                out.append("== %s ==" % lbl)
            else:
                out.append(b["text"])
    return "\n\n".join(out)


def cleanup_wikitext(s):
    # 合并 3+ 连续空行为 1 个空行
    s = re.sub(r"\n{3,}", "\n\n", s)
    # 行尾空白
    s = "\n".join(l.rstrip() for l in s.split("\n"))
    return s.strip()


def safe_filename(title):
    """把 Windows 非法文件名字符替换为全角等价字符，使文件名≈页面标题
    （MediaWiki 会把半角 : 当命名空间分隔、_ 当空格，故必须用全角）。"""
    tr = {"/": "／", ":": "：", "\\": "＼", "*": "＊", "?": "？",
          '"': "＂", "<": "＜", ">": "＞", "|": "｜"}
    return "".join(tr.get(c, c) for c in title)


def load_code(path):
    if path and os.path.exists(path):
        return {int(k): v for k, v in json.load(open(path, encoding="utf-8")).items()}
    return {}


def load_figs(path):
    if path and os.path.exists(path):
        d = {int(k): v for k, v in json.load(open(path, encoding="utf-8")).items()}
        # 按 y 排序，便于与正文顺序穿插
        for k in d:
            d[k].sort(key=lambda f: f["y0"])
        return d
    return {}


def build_book(book, struct_path, ocr_path, outdir, page_prefix, code_path=None, figmap_path=None):
    struct = json.load(open(struct_path, encoding="utf-8"))
    ocr = load_ocr(ocr_path)
    pdf_code = load_code(code_path)
    figs = load_figs(figmap_path)
    os.makedirs(outdir, exist_ok=True)
    done = 0
    missing = []
    index = []
    for p in struct["pages"]:
        blocks = []
        for pg in range(p["start"], p["end"] + 1):
            o = ocr.get(pg)
            if o is None:
                missing.append(pg)
                continue
            blocks.extend(layout.parse_page(o, pdf_code, figs))
        body = render_blocks(blocks, p["title"], p.get("sections", []))
        body = cleanup_wikitext(body)
        # 标题规整：去掉开头 "NN" 前缀编号空格
        title = p["title"]
        text = []
        text.append(body)
        text.append("")
        text.append("[[Category:%s]]" % BOOK_CAT[book])
        text.append("[[Category:%s]]" % CAT[book].get(p["slug"], "其他"))
        wiki = "\n".join(text).rstrip() + "\n"
        safe = safe_filename(title)
        open(os.path.join(outdir, safe + ".wiki"), "w", encoding="utf-8").write(wiki)
        index.append({"slug": p["slug"], "title": title, "file": safe + ".wiki"})
        done += 1
    json.dump(index, open(os.path.join(outdir, "pageindex.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    if missing:
        print("  缺失页:", len(set(missing)), sorted(set(missing))[:10])
    return done, index


BOOK_CAT = {"ostep": "操作系统导论", "gt": "图论"}
CAT = {
    "ostep": {
        "preface": "前言与介绍", "intro": "前言与介绍",
        "ch01": "介绍", "ch02": "介绍",
        "ch03": "CPU 虚拟化", "ch04": "CPU 虚拟化", "ch05": "CPU 虚拟化", "ch06": "CPU 虚拟化",
        "ch07": "CPU 虚拟化", "ch08": "CPU 虚拟化", "ch09": "CPU 虚拟化", "ch10": "CPU 虚拟化", "ch11": "CPU 虚拟化",
        "ch12": "内存虚拟化", "ch13": "内存虚拟化", "ch14": "内存虚拟化", "ch15": "内存虚拟化",
        "ch16": "内存虚拟化", "ch17": "内存虚拟化", "ch18": "内存虚拟化", "ch19": "内存虚拟化",
        "ch20": "内存虚拟化", "ch21": "内存虚拟化", "ch22": "内存虚拟化", "ch23": "内存虚拟化", "ch24": "内存虚拟化",
        "ch25": "并发", "ch26": "并发", "ch27": "并发", "ch28": "并发", "ch29": "并发",
        "ch30": "并发", "ch31": "并发", "ch32": "并发", "ch33": "并发", "ch34": "并发",
        "ch35": "持久性", "ch36": "持久性", "ch37": "持久性", "ch38": "持久性", "ch39": "持久性",
        "ch40": "持久性", "ch41": "持久性", "ch42": "持久性", "ch43": "持久性", "ch44": "持久性", "ch45": "持久性",
        "intro_dist": "分布式", "ch46": "分布式", "ch47": "分布式", "ch48": "分布式", "ch49": "分布式", "ch50": "分布式",
        "appA": "附录", "appB": "附录", "appC": "附录", "appD": "附录", "appE": "附录", "appF": "附录", "appG": "附录",
    },
    "gt": {
        "front": "前言", "ch01": "基础知识", "ch02": "匹配覆盖填装", "ch03": "连通性",
        "ch04": "可平面图", "ch05": "着色", "ch06": "流", "ch07": "极值图论", "ch08": "无限图",
        "ch09": "图的Ramsey理论", "ch10": "Hamilton圈", "ch11": "随机图", "ch12": "图子式与树宽",
        "appA": "附录", "appB": "附录", "hints": "练习提示", "index": "索引", "series": "出版书目",
    },
}


OSTEP_PARTS = [
    ("前言与介绍", ["preface", "ch01", "ch02"]),
    ("CPU 虚拟化", ["ch03", "ch04", "ch05", "ch06", "ch07", "ch08", "ch09", "ch10", "ch11",
                    "ch12", "ch13", "ch14", "ch15", "ch16", "ch17", "ch18", "ch19", "ch20",
                    "ch21", "ch22", "ch23", "ch24"]),
    ("并发", ["ch25", "ch26", "ch27", "ch28", "ch29", "ch30", "ch31", "ch32", "ch33", "ch34"]),
    ("持久性", ["ch35", "ch36", "ch37", "ch38", "ch39", "ch40", "ch41", "ch42", "ch43", "ch44", "ch45"]),
    ("分布式", ["ch46", "ch47", "ch48", "ch49", "ch50"]),
    ("附录", ["appA", "appB", "appC", "appD", "appE", "appF", "appG"]),
]


def build_landing_ostep(outdir):
    idx = json.load(open(os.path.join(outdir, "pageindex.json"), encoding="utf-8"))
    byslug = {x["slug"]: x for x in idx}
    lines = []
    lines.append("{| style=\"width:100%;border:1px solid #a2a9b1;background:#f8f9fa;padding:0.8em 1em;margin-bottom:1em;\"")
    lines.append("|-")
    lines.append("| '''操作系统导论'''（Operating Systems: Three Easy Pieces，OSTEP）——本 wiki 收录其中文版"
                 "《操作系统导论（三座大山中文版）》的完整知识条目，按原书章节组织为 58 个条目。")
    lines.append("|-")
    lines.append("| style=\"padding-top:0.5em;\" | '''三大主题'''：[[13抽象：地址空间|虚拟化]] · [[26并发：介绍|并发]] · [[36I/O设备|持久性]]")
    lines.append("|}")
    lines.append("")
    for part, slugs in OSTEP_PARTS:
        lines.append("== %s ==" % part)
        lines.append("")
        for s in slugs:
            it = byslug.get(s)
            if not it:
                continue
            lines.append("* [[%s]]" % safe_filename(it["title"]))
        lines.append("")
    body = cleanup_wikitext("\n".join(lines))
    body += "\n\n[[Category:操作系统导论]]\n"
    open(os.path.join(outdir, "操作系统导论.wiki"), "w", encoding="utf-8").write(body)
    print("[ostep] 生成总览页 操作系统导论.wiki")


def build_landing_gt(outdir):
    idx = json.load(open(os.path.join(outdir, "pageindex.json"), encoding="utf-8"))
    byslug = {x["slug"]: x for x in idx}
    lines = []
    lines.append("{| style=\"width:100%;border:1px solid #a2a9b1;background:#f8f9fa;padding:0.8em 1em;margin-bottom:1em;\"")
    lines.append("|-")
    lines.append("| '''图论'''（Graph Theory, 5th Edition，Reinhard Diestel 著，于青林 译）——"
                 "本 wiki 收录其中文版第五版的完整知识条目，按原书 12 章组织。")
    lines.append("|-")
    lines.append("| style=\"padding-top:0.5em;\" | '''核心篇章'''：[[第4章可平面图|可平面图]] · [[第5章着色|着色]] · [[第6章流|流]] · [[第8章无限图|无限图]]")
    lines.append("|}")
    lines.append("")
    order = ["front", "ch01", "ch02", "ch03", "ch04", "ch05", "ch06", "ch07", "ch08",
             "ch09", "ch10", "ch11", "ch12", "appA", "appB", "hints", "index", "series"]
    for s in order:
        it = byslug.get(s)
        if it:
            lines.append("* [[%s]]" % safe_filename(it["title"]))
    body = cleanup_wikitext("\n".join(lines))
    body += "\n\n[[Category:图论]]\n"
    open(os.path.join(outdir, "图论.wiki"), "w", encoding="utf-8").write(body)
    print("[gt] 生成总览页 图论.wiki")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("book", choices=["ostep", "gt"])
    args = ap.parse_args()
    if args.book == "ostep":
        struct_path = os.path.join(ROOT, "struct_ostep.json")
        ocr_path = os.path.join(ROOT, "ostep", "ocr_pages.jsonl")
        outdir = os.path.join(ROOT, "ostep", "pages")
        code_path = os.path.join(ROOT, "ostep", "code_pdf.json")
        figmap_path = os.path.join(ROOT, "ostep", "img", "figmap.json")
        prefix = ""
    else:
        struct_path = os.path.join(ROOT, "struct_gt.json")
        ocr_path = os.path.join(ROOT, "graphtheory", "ocr_pages.jsonl")
        outdir = os.path.join(ROOT, "graphtheory", "pages")
        code_path = os.path.join(ROOT, "graphtheory", "code_pdf.json")
        figmap_path = os.path.join(ROOT, "graphtheory", "img", "figmap.json")
        prefix = "图论："
    n, index = build_book(args.book, struct_path, ocr_path, outdir, prefix, code_path, figmap_path)
    if args.book == "ostep":
        build_landing_ostep(outdir)
    else:
        build_landing_gt(outdir)
    print(f"[{args.book}] 生成 {n} 页 -> {outdir}")


if __name__ == "__main__":
    main()
