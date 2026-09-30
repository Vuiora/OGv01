#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
d2l markdown -> MediaWiki wikitext 转换器（v2）。

关键设计：
- 只提取 PyTorch 代码块：连续代码块构成 tab 组，组内优先取含 pytorch 的块，
  其次取 all 的块；组内无 tab 标记的块是 mxnet（d2l 默认），仅在无其他选择时保留。
- 行内公式 $..$ -> <math>..</math>；显示公式 $$..$$ -> <math display="block">..</math>
- 图片 ![alt](path) -> 带锚点的 [[文件:basename|alt]]
- :numref:`label` / :eqref:`label` -> 带锚点的内部链接，显示「图 N」/「式 (N)」
- :label:`x` / :eqlabel:`x` -> <span id="x"></span> 锚点
- (**...**) 与 [**...**] -> '''...'''（d2l 用它标注重难点）
"""
import re, os, json, glob, collections

ROOT = r"C:\Users\Lenovo\Desktop\OGv01\Wiki\.build\d2l"
SRC = os.path.join(ROOT, "src")

# 页面命名：章索引页与同名首节消歧
TITLE_OVERRIDE = {
    "chapter_multilayer-perceptrons__index": "多层感知机（章）",
    "chapter_recurrent-neural-networks__index": "循环神经网络（章）",
    "index": "动手学深度学习",
    "chapter_references__zreferences": "参考文献",
    "chapter_appendix-tools-for-deep-learning__d2l": "d2l API 文档",
}

# ============ 1) 标签系统 ============
def build_labels():
    """扫描所有文件，建立 label -> {page, type, num} 映射。"""
    lab2info = {}
    files = sorted(glob.glob(os.path.join(SRC, "*.md")))
    for f in files:
        raw = open(f, encoding="utf-8").read()
        base = os.path.basename(f)
        m = re.search(r"^#\s+(.+)$", raw, re.M)
        title = m.group(1).strip() if m else base[:-3]
        stem = base[:-3]
        title = TITLE_OVERRIDE.get(stem, title)
        lines = raw.split("\n")
        counters = collections.Counter()
        img_pending = False
        in_code = False
        for ln in lines:
            s = ln.strip()
            if s.startswith("```"):
                in_code = not in_code
                img_pending = False
                continue
            if in_code:
                continue
            if re.match(r"^!\[", s):
                img_pending = True
                continue
            mm = re.match(r"^:(label|eqlabel):`([^`]+)`", s)
            if mm:
                kind, lbl = mm.group(1), mm.group(2)
                if lbl.startswith("fig") or (kind == "label" and img_pending):
                    t = "fig"
                elif kind == "eqlabel" or lbl.startswith("eq"):
                    t = "eq"
                elif lbl.startswith(("tbl", "tab")):
                    t = "tbl"
                else:
                    t = "sec"
                counters[t] += 1
                lab2info[lbl] = {"page": title, "stem": base[:-3], "type": t, "num": counters[t]}
                img_pending = False
            elif s:
                img_pending = False
    return lab2info

LABELS = build_labels()
TYPE_CN = {"fig": "图", "eq": "式", "tbl": "表", "sec": "节"}

# ============ 2) 代码块提取 ============
FENCE = re.compile(r"^```([^\n]*)\n(.*?)^```[ \t]*$", re.S | re.M)
CODE_PH = "\x00C%d\x00"

def extract_codes(text):
    bs = []
    for m in FENCE.finditer(text):
        bs.append({"lang": m.group(1).strip(), "body": m.group(2),
                   "start": m.start(), "end": m.end()})
    picked = []
    dropped = []
    edits = []
    codes_holder = []
    i = 0
    while i < len(bs):
        j = i
        while j + 1 < len(bs) and text[bs[j]["end"]:bs[j+1]["start"]].strip() == "":
            j += 1
        grp = bs[i:j+1]
        tabs_list = []
        for b in grp:
            m2 = re.match(r"#@tab ([^\n]+)\n", b["body"])
            tabs = m2.group(1).strip() if m2 else ""
            body = b["body"][m2.end():] if m2 else b["body"]
            tabs_list.append((tabs, body))
        has_tab = any(t for t, _ in tabs_list)
        if has_tab:
            chosen = None; ctype = None
            for tabs, body in tabs_list:
                if "pytorch" in tabs: chosen = body; ctype = "py"; break
            if chosen is None:
                for tabs, body in tabs_list:
                    if "all" in tabs: chosen = body; ctype = "py"; break
            if chosen is not None:
                edits.append((grp[0]["start"], grp[-1]["end"], CODE_PH % len(codes_holder)))
                codes_holder.append((chosen.rstrip("\n"), ctype))
            else:
                edits.append((grp[0]["start"], grp[-1]["end"], ""))
        else:
            # 非 tab 组：逐个块独立处理（语言不同，不能合并）
            for b in grp:
                lang = b["lang"]
                body = b["body"]
                if lang in ("", "{.python .input}"):
                    ct = "py"
                elif lang == "bash":
                    ct = "bash"
                elif lang == "toc":
                    ct = "toc"
                elif lang == "eval_rst":
                    ct = None          # 丢弃
                elif ".output" in lang:
                    ct = "text"
                elif lang:
                    ct = "text"
                else:
                    ct = None          # 无标记的孤立块，丢弃
                if ct is None:
                    edits.append((b["start"], b["end"], ""))
                else:
                    edits.append((b["start"], b["end"], CODE_PH % len(codes_holder)))
                    codes_holder.append((body.rstrip("\n"), ct))
        i = j + 1
    # 单次从后往前应用所有编辑，避免长度变化导致索引错位
    new = text
    for s, e, rep in sorted(edits, key=lambda x: -x[0]):
        new = new[:s] + rep + new[e:]
    return new, codes_holder

def restore_codes(text, codes):
    def repl(m):
        body, ctype = codes[int(m.group(1))]
        if ctype == "toc":
            lines = []
            for ln in body.split("\n"):
                s = ln.strip()
                if not s or s.startswith(":"):
                    continue
                title = toc_title(s)
                lines.append("* [[%s|%s]]" % (title, title))
            return "\n" + "\n".join(lines) + "\n"
        lang = {"py": "python", "bash": "bash", "text": "text"}[ctype]
        safe = body.replace("</syntaxhighlight>", "</ syntaxhighlight>")
        return '\n<syntaxhighlight lang="%s">\n%s\n</syntaxhighlight>\n' % (lang, safe)
    return re.sub(r"\x00C(\d+)\x00", repl, text)

# 页面命名：章索引页与同名首节消歧（已上移至文件顶部）

def toc_title(s):
    """toc 条目（如 'linear-algebra' 或 'chapter_xxx/index'）-> 页面标题。"""
    s = s.strip()
    if s.startswith("chapter_"):
        stem = s.replace("/", "__")
    else:
        stem = "chapter_%s__%s" % (CUR_CHAP[0], s)
    if stem in TITLE_OVERRIDE:
        return TITLE_OVERRIDE[stem]
    f = os.path.join(SRC, stem + ".md")
    if os.path.exists(f):
        raw = open(f, encoding="utf-8").read()
        m = re.search(r"^#\s+(.+)$", raw, re.M)
        if m:
            return TITLE_OVERRIDE.get(stem, m.group(1).strip())
    return stem

CUR_CHAP = [""]

# ============ 2b) tab 文本指令处理 ============
TABTEXT = re.compile(r"^:begin_tab:`([^`]*)`[ \t]*\n(.*?)^:end_tab:[ \t]*$", re.S | re.M)

def resolve_tabtext(text):
    """处理 :begin_tab:`x` ... :end_tab: 包裹的框架特定文本：
    仅保留含 pytorch 的分支（若无 pytorch 分支则保留第一个）。"""
    def repl(m):
        tabs, body = m.group(1), m.group(2)
        return body  # 单块内已按框架分；下方再做分组筛选
    # 先按"连续 tab 文本组"处理
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        m = re.match(r"^:begin_tab:`([^`]*)`[ \t]*$", lines[i])
        if m:
            grp = []
            j = i
            while j < len(lines):
                mm = re.match(r"^:begin_tab:`([^`]*)`[ \t]*$", lines[j])
                if not mm:
                    break
                tabs = mm.group(1)
                k = j + 1
                body = []
                while k < len(lines) and not re.match(r"^:end_tab:", lines[k]):
                    body.append(lines[k]); k += 1
                grp.append((tabs, "\n".join(body).strip("\n")))
                # 跳过块之间的空行，寻找下一个 :begin_tab:
                k += 1
                while k < len(lines) and lines[k].strip() == "":
                    k += 1
                if k < len(lines) and re.match(r"^:begin_tab:", lines[k]):
                    j = k
                else:
                    j = k
                    break
            chosen = None
            for tabs, body in grp:
                if "pytorch" in tabs: chosen = body; break
            if chosen is None and grp:
                chosen = grp[0][1]
            if chosen is not None:
                out.append(chosen)
            i = j
        else:
            out.append(lines[i]); i += 1
    return "\n".join(out)


# ============ 3) 公式保护 ============
def protect_math(text):
    store = []
    def repl_disp(m):
        store.append(("block", m.group(1).strip()))
        return "\x00M%d\x00" % (len(store) - 1)
    def repl_inl(m):
        store.append(("inline", m.group(1).strip()))
        return "\x00M%d\x00" % (len(store) - 1)
    text = re.sub(r"\$\$(.+?)\$\$", repl_disp, text, flags=re.S)
    text = re.sub(r"(?<!\$)\$([^$\n]+?)\$(?!\$)", repl_inl, text)
    return text, store

def restore_math(text, store):
    def repl(m):
        kind, tex = store[int(m.group(1))]
        if kind == "block":
            return '<math display="block">%s</math>' % tex
        return "<math>%s</math>" % tex
    return re.sub(r"\x00M(\d+)\x00", repl, text)

# ============ 3b) 代码块保护（行内处理前屏蔽，防止 (**kwargs) 等误匹配）============
CODEBLK_PH = "\x00K%d\x00"

def protect_codeblocks(text):
    store = []
    def repl(m):
        store.append(m.group(0))
        return CODEBLK_PH % (len(store) - 1)
    text = re.sub(r"<syntaxhighlight\b.*?</syntaxhighlight>", repl, text, flags=re.S)
    return text, store

def restore_codeblocks(text, store):
    return re.sub(r"\x00K(\d+)\x00", lambda m: store[int(m.group(1))], text)


# ============ 4) 引用替换 ============
def ref_display(info):
    t = info["type"]
    if t == "eq":  return "式 (%d)" % info["num"]
    if t == "fig": return "图 %d" % info["num"]
    if t == "tbl": return "表 %d" % info["num"]
    return "《%s》" % info["page"]

def replace_refs(text, cur_page):
    def mk(lbl):
        info = LABELS.get(lbl.strip())
        if not info:
            return None
        disp = ref_display(info)
        if info["page"] == cur_page:
            return "[[#%s|%s]]" % (lbl.strip(), disp)
        return "[[%s#%s|%s]]" % (info["page"], lbl.strip(), disp)
    text = re.sub(r":numref:`([^`]+)`", lambda m: mk(m.group(1)) or m.group(1), text)
    text = re.sub(r":eqref:`([^`]+)`", lambda m: mk(m.group(1)) or m.group(1), text)
    text = re.sub(r":cite[t]?:`([^`]+)`", lambda m: "<sup>[%s]</sup>" % m.group(1), text)
    return text

# ============ 5) 行内格式 ============
def inline(text):
    def r_img(m):
        alt = m.group(1).strip()
        path = m.group(2).strip()
        name = os.path.basename(path)
        cap = ("|" + alt.replace("|", "&#124;")) if alt else ""
        return "\n[[文件:%s%s]]\n" % (name, cap)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r_img, text)
    # d2l 重点标记（允许跨行，用 DOTALL；代码块已被屏蔽）
    text = re.sub(r"\[\*\*(.+?)\*\*\]", r"'''\1'''", text, flags=re.S)
    text = re.sub(r"\(\*\*(.+?)\*\*\)", r"'''\1'''", text, flags=re.S)
    # 链接（先匹配含嵌套括号的 URL，如 [文章](https://x/2017/momentum/)）
    def r_link(m):
        t = m.group(1).strip(); u = m.group(2).strip()
        if u.startswith("http"):
            return "[%s %s]" % (u, t)
        if "../img/" in u or u.startswith("img/"):
            return "[[文件:%s|%s]]" % (os.path.basename(u), t)
        return t
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s）)]*)[）)]", r_link, text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r_link, text)
    text = re.sub(r"(?<!\*)\*\*([^*\n]+?)\*\*(?!\*)", r"'''\1'''", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"''\1''", text)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    return text

# ============ 6) 块级 ============
def convert_file(fn):
    raw = open(fn, encoding="utf-8").read()
    base = os.path.basename(fn)
    CUR_CHAP[0] = base.split("__")[0].replace("chapter_", "")
    m = re.search(r"^#\s+(.+)$", raw, re.M)
    title = m.group(1).strip() if m else base[:-3]
    title = TITLE_OVERRIDE.get(base[:-3], title)

    text, codes = extract_codes(raw)
    # 分框架文本：仅保留 pytorch 分支（须在移除指令行之前处理）
    text = resolve_tabtext(text)
    text = re.sub(r"^:label:`([^`]+)`[ \t]*$", lambda m: '<span id="%s"></span>' % m.group(1), text, flags=re.M)
    text = re.sub(r"^:eqlabel:`([^`]+)`[ \t]*$", lambda m: '<span id="%s"></span>' % m.group(1), text, flags=re.M)
    text = re.sub(r"^:(?:begin|end)_tab:`[^`]*`[ \t]*\n?", "", text, flags=re.M)
    text = re.sub(r"^```toc\n.*?^```[ \t]*$", "", text, flags=re.S | re.M)
    text = re.sub(r"^```eval_rst\n.*?^```[ \t]*$", "", text, flags=re.S | re.M)

    def r_other(m):
        lang = (m.group(1) or "").strip()
        if lang.startswith("{."):
            lang = "python" if ".python" in lang else "text"
        if not lang:
            lang = "text"
        return '\n<syntaxhighlight lang="%s">\n%s\n</syntaxhighlight>\n' % (lang, m.group(2).rstrip("\n"))
    text = FENCE.sub(r_other, text)
    text = restore_codes(text, codes)
    text, store = protect_math(text)
    text = replace_refs(text, title)
    text, cstore = protect_codeblocks(text)   # 屏蔽代码块，防行内正则误伤
    text = inline(text)
    text = restore_codeblocks(text, cstore)

    lines = []
    for ln in text.split("\n"):
        h = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", ln)
        if h:
            lvl = min(len(h.group(1)) + 1, 6)
            lines.append("=" * lvl + " " + h.group(2).strip() + " " + "=" * lvl)
        else:
            lines.append(ln)
    text = "\n".join(lines)
    # setext 标题：上一行是文本、下一行是 === 或 ---
    text = convert_setext(text)
    text = restore_math(text, store)
    # 分类与导航
    text = add_footer(text, base, title)
    return title, text

# 章节中文名
CHAP_CN = {
    "preface": "前言与总览", "installation": "前言与总览", "notation": "前言与总览",
    "introduction": "引言",
    "preliminaries": "预备知识",
    "linear-networks": "线性神经网络",
    "multilayer-perceptrons": "多层感知机",
    "deep-learning-computation": "深度学习计算",
    "convolutional-neural-networks": "卷积神经网络",
    "convolutional-modern": "现代卷积神经网络",
    "recurrent-neural-networks": "循环神经网络",
    "recurrent-modern": "现代循环神经网络",
    "attention-mechanisms": "注意力机制",
    "optimization": "优化算法",
    "computational-performance": "计算性能",
    "computer-vision": "计算机视觉",
    "natural-language-processing-pretraining": "自然语言处理：预训练",
    "natural-language-processing-applications": "自然语言处理：应用",
    "appendix-tools-for-deep-learning": "附录：深度学习工具",
    "references": "参考文献",
    "__root__": "总览",
}

def add_footer(text, base, title):
    stem = base[:-3]
    if stem == "chapter_references__zreferences":
        chap = "references"
        cat = "参考文献"
    elif stem == "index":
        chap = "__root__"
        cat = "总览"
    else:
        chap = stem.split("__")[0].replace("chapter_", "")
        cat = CHAP_CN.get(chap, chap)
    parts = [text.rstrip()]
    parts.append("")
    parts.append("[[Category:动手学深度学习]]")
    parts.append("[[Category:%s]]" % cat)
    return "\n".join(parts) + "\n"

# ============ 7) 列表/表格/清理 ============
def convert_setext(text):
    """转换 setext 式标题：'标题\\n====' -> '== 标题 =='。"""
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        if i + 1 < len(lines) and lines[i].strip() and re.match(r"^={3,}\s*$", lines[i+1]):
            out.append("== %s ==" % lines[i].strip())
            i += 2
            continue
        if i + 1 < len(lines) and lines[i].strip() and re.match(r"^-{3,}\s*$", lines[i+1]) \
           and not lines[i].strip().startswith(("|", "*", "#", "=", "-")):
            out.append("=== %s ===" % lines[i].strip())
            i += 2
            continue
        out.append(lines[i]); i += 1
    return "\n".join(out)

def convert_lists(text):
    out = []
    for ln in text.split("\n"):
        m = re.match(r"^(\s*)\*\s+(.*)$", ln)
        if m:
            out.append(m.group(1) + "* " + m.group(2)); continue
        m = re.match(r"^(\s*)(\d+)\.\s+(.*)$", ln)
        if m:
            out.append(m.group(1) + "# " + m.group(3)); continue
        out.append(ln)
    return "\n".join(out)

def convert_tables(text):
    lines = text.split("\n"); out = []; i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("|") and ln.strip().endswith("|") and i + 1 < len(lines) \
           and re.match(r"^\|[\s:|-]+\|$", lines[i+1].strip()):
            hdr = [c.strip() for c in ln.strip().strip("|").split("|")]
            rows = []; j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append([c.strip().replace("|", "&#124;") for c in lines[j].strip().strip("|").split("|")])
                j += 1
            t = ['{| class="wikitable" style="margin:auto"']
            t.append("! " + " !! ".join(hdr))
            for r in rows:
                t.append("|-")
                t.append("| " + " || ".join(r))
            t.append("|}")
            out.append("\n".join(t)); i = j
        else:
            out.append(ln); i += 1
    return "\n".join(out)

def cleanup(text):
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(l.rstrip() for l in text.split("\n"))
    return text.strip() + "\n"

def main():
    files = sorted(glob.glob(os.path.join(SRC, "*.md")))
    outdir = os.path.join(ROOT, "pages")
    os.makedirs(outdir, exist_ok=True)
    # 清理旧产物
    for old in glob.glob(os.path.join(outdir, "*.wiki")):
        os.remove(old)
    n = 0
    index = {}   # stem -> title
    for f in files:
        title, text = convert_file(f)
        text = convert_lists(text)
        text = convert_tables(text)
        text = cleanup(text)
        stem = os.path.basename(f)[:-3]
        # 输出文件名 = 页面标题（做文件名安全处理）
        safe = re.sub(r'[\\/:*?"<>|]', "_", title)
        open(os.path.join(outdir, safe + ".wiki"), "w", encoding="utf-8").write(text)
        index[stem] = title
        n += 1
    json.dump(index, open(os.path.join(ROOT, "pageindex.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("转换完成:", n, "个文件 ->", outdir)

if __name__ == "__main__":
    main()
