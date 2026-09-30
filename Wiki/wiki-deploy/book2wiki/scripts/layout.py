#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
layout.py —— 把 OCR 行（含坐标）重排为「逻辑块」序列。

OCR 只给出「行 + 坐标」。要转 wikitext，必须先重建版面逻辑：
  1. 剔除页眉/页脚（页码 + 章名/书名）
  2. 逐行判定「角色」：
       - code    由 pdfcode.py 从等宽字体层直接抽取（首选）；
                 无 PDF 代码层时回退到「连续行号列」启发式
       - heading 大字号 + 章节编号 / 短标题（严格校验）
       - figcap  以「图 x.y」「表 x.y」开头的短行
       - formula 居中 / 高符号密度的数学行
       - list    以项目符号/编号开头
       - text    其余正文
  3. 按缩进把 text 行聚合成段落。

输出 block 序列：
  {"type": "h1|h2|h3|p|code|formula|figcap|list", "text": "...", "page": N}
"""
import re

MATH_CHARS = set("∈∉⊆⊇⊂⊃∪∩∅≠≤≥≈≅→←↦⇒∀∃∑∏∫√∞∂∇±×÷·∘⋯…ΔΣΠΓΩαβγδεζηθικλμνξπρστυφχψω"
                 "⌈⌉⌊⌋∧∨¬≡∼≃≌≪≫⊕⊗⊥∥↺↻")

# 章 / 节 / 附录 标题（节号必须含小数点，避免把 "0, 否则." 误判成标题）
HEAD_PAT = re.compile(r"^(第\s?\d{1,3}\s?章|第\s?\d{1,3}\s?节|附录\s?[A-Z]|\d{1,2}(\.\d{1,2}){1,2}\s*\S)")
FIGCAP_PAT = re.compile(r"^(图|表)\s?\d+([.\-]\d+)?")
LIST_PAT = re.compile(r"^([·•▪◦]\s|\-\s|\([a-zA-Z]\)|（[a-zA-Z]）|\(?\d+\)\s|（?\d+）\s|\d+\.\s)")
CODE_PAT = re.compile(r"^\s*(\d{1,3})\s+(\S.*)$")

# 纯编号标题前缀（"1.5" / "1.5.2" / "28.6"）
SECNUM_PAT = re.compile(r"^\d{1,2}(\.\d{1,2}){1,2}\s*[^\d\s]")
# 数学专用符号（用于「数学密度」判定，不含中文常见字）
SYMBOLS = MATH_CHARS | set("=<>|:=^_/\\+*×÷()[]{}")


def cjk_ratio(s):
    if not s:
        return 0
    return sum(1 for c in s if "\u4e00" <= c <= "\u9fff") / len(s)


def cjk_count(s):
    return sum(1 for c in s if "\u4e00" <= c <= "\u9fff")


def has_math(s):
    return any(c in MATH_CHARS for c in s)


def norm(s):
    return re.sub(r"[\s\u3000]+", " ", s).strip()


def box_xy(box):
    xs = [p[0] for p in box]; ys = [p[1] for p in box]
    return min(xs), min(ys), max(xs), max(ys)


def page_lines(o):
    out = []
    for l in o["lines"]:
        if not l.get("box"):
            continue
        x0, y0, x1, y1 = box_xy(l["box"])
        out.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "h": y1 - y0, "text": l["text"], "score": l.get("score")})
    out.sort(key=lambda r: (round(r["y0"] / 6), r["x0"]))
    return out


def strip_header_footer(lines, W, H):
    top, bot = 0.095 * H, 0.935 * H
    return [r for r in lines if not (r["y1"] <= top or r["y0"] >= bot)]


def stats(lines):
    from collections import Counter
    ch = Counter(round(r["h"] / 4) * 4 for r in lines if r["h"] > 5)
    body_h = ch.most_common(1)[0][0] if ch else 34
    cl = Counter(round(r["x0"] / 10) * 10 for r in lines)
    lm = cl.most_common(1)[0][0] if cl else 160
    return body_h, lm


def math_density(t):
    if not t:
        return 0
    return sum(1 for c in t if c in SYMBOLS) / len(t)


def looks_like_heading(t):
    """标题合理性（严格）：必须短、可读、低符号密度、非列表/非公式/非句子片段。"""
    if len(t) < 2 or len(t) > 40:
        return False
    if t.isdigit():
        return False
    # 中文译本：标题必含汉字，或是「第N章 / x.y / 附录X」形态。
    # 借此排除公式变量名（Twrite/Reffective/EV）、表格词（PFN 200）等。
    if cjk_count(t) == 0 and not re.match(r"^\d{1,2}\.\d{1,2}", t) \
            and not re.match(r"^(第|附录)", t):
        return False
    # 必须含中文或至少 2 个连续字母
    if not re.search(r"[\u4e00-\u9fff]|[A-Za-z]{2,}", t):
        return False
    # 纯数字/点/空格
    if re.fullmatch(r"[\d\s.]+", t):
        return False
    # 数学符号密度过高 → 是公式而非标题
    if math_density(t) > 0.12:
        return False
    # 含明显公式记号
    if re.search(r"[=<>|]|:=|→|←|≥|≤|∈|⊆|∀|∃|∑|∏|∫|√|∞", t):
        return False
    # 不能以句读结尾（正文句尾）
    if re.search(r"[。；，,;、：:]$", t):
        return False
    # 不能以 "数字 + 逗号 + 短词 ." 这种公式分段形态开头（"0, 否则."）
    if re.match(r"^\d+\s*[,，]\s*.{0,6}[.。]$", t):
        return False
    # 不能是列表项 / 图注
    if LIST_PAT.match(t) or FIGCAP_PAT.match(t):
        return False
    # 表格续行
    if re.match(r"^(续表|续上表)", t):
        return False
    # 含数值+单位（"访问时间 10.195ms"）或 LaTeX 记号
    if re.search(r"\d+(\.\d+)?\s*(ms|us|ns|s|KB|MB|GB|TB)$", t) or "\\" in t \
            or "×" in t or "·" in t:
        return False
    # ── 句子片段 / 杂项排除 ──────────────────────────────
    if "，" in t or "," in t:                       # 真标题用「、」不用「，」
        return False
    if re.search(r"[的和所与或为在被是则而其将这]$", t):   # 续行结尾虚词
        return False
    if re.search(r"\d\)$|\(\d+[.\-]\d+\)$", t):      # 公式编号「(3.1)」
        return False
    if cjk_count(t) == 0 and len(t) < 6:             # 纯 ASCII 碎片（EV/VE/pk）
        return False
    if re.match(r"^[a-z]", t):                       # 小写字母开头
        return False
    if re.match(r"^\d+(\.\d+)+节?\.?$", t):           # 交叉引用「1.10节」
        return False
    return True


def roles(lines, W, H, body_h, lm):
    """给每行打 role 标签。"""
    out = []
    indent_thr = lm + max(35, 0.03 * W)
    for r in lines:
        t = norm(r["text"])
        role = "text"
        if not t:
            role = "empty"
        elif CODE_PAT.match(t) and int(CODE_PAT.match(t).group(1)) <= 200 \
                and r["x0"] < 0.24 * W:
            role = "code"
        # 标题：需满足字号放大 + 形态合法
        elif r["h"] >= body_h * 1.10 and looks_like_heading(t) \
                and (HEAD_PAT.match(t) or KEYWORD_HEAD.match(t)):
            role = "heading"
        elif r["h"] >= body_h * 1.40 and len(t) <= 24 and looks_like_heading(t) \
                and not re.match(r"^\d", t) and not re.match(r"^[\"“「]", t):
            role = "heading"
        elif FIGCAP_PAT.match(t) and len(t) <= 130 and not re.search(r"[。；：]$", t) \
                and not re.search(r"(展示了|所示|可以看出|给出了|说明了|如下|则是)", t):
            role = "figcap"
        elif is_formula(t, r, lm, W):
            role = "formula"
        elif LIST_PAT.match(t):
            role = "list"
        out.append({**r, "role": role, "text": t, "indent": r["x0"] >= indent_thr})
    return out


KEYWORD_HEAD = re.compile(r"^(练习|注解|索引|参考文献|参考书目|译者序|前言|绪论|结语|提示)\s*[.。]?$")


def is_formula(t, r, lm, W):
    """判定是否为显示公式行。"""
    if len(t) > 90:
        return False
    centered = r["x0"] > lm + 0.06 * W
    sym = math_density(t)
    # 高符号密度：无论位置
    if sym >= 0.30 and len(t) >= 3:
        return True
    # 含明确数学记号，且行较短
    if len(t) <= 60 and re.search(r"[=≥≤≠≈⊆⊇∈∑∏∫√]|:=|→|←", t):
        return True
    # 居中 + 含数学符号
    if centered and has_math(t) and len(t) <= 60:
        return True
    # 居中 + 末尾公式编号 (N.M)
    if centered and re.search(r"\(\d+[.\-]\d+\)\s*$", t):
        return True
    # 短的分段定义行："0, 否则." / "1, vi ∈ ej,"
    if len(t) <= 18 and re.match(r"^[\dA-Za-z(（]", t) and re.search(r"[,，]", t) and sym >= 0.05:
        return True
    return False


NUMLINE_PAT = re.compile(r"^\s*(\d{1,3})\s*$")
NUMCODE_PAT = re.compile(r"^\s*(\d{1,3})\s+(\S.*)$")
N_FIG_MARGIN = 10       # 位图图形压制带上下放宽（OCR 像素）


def find_code_regions(lines):
    """回退方案：从「连续行号列 + 代码列」启发式识别代码区（无 PDF 代码层时）。

    返回 [(start_idx, end_idx, code_text)]
    """
    W = max((r["x1"] for r in lines), default=1200)
    anchors = []
    for idx, r in enumerate(lines):
        t = r["text"].strip()
        m = NUMLINE_PAT.match(t) or NUMCODE_PAT.match(t)
        if m and int(m.group(1)) <= 250 and r["x0"] < 0.24 * W:
            anchors.append((int(m.group(1)), r["y0"], idx))
    if len(anchors) < 3:
        return []
    anchors.sort(key=lambda a: a[1])
    regions, cur = [], [anchors[0]]
    for a in anchors[1:]:
        if a[1] - cur[-1][1] <= 55:
            cur.append(a)
        else:
            regions.append(cur); cur = [a]
    regions.append(cur)

    out = []
    for reg in regions:
        if len(reg) < 3:
            continue
        seq = [x[0] for x in reg]
        inc = sum(1 for k in range(1, len(seq)) if seq[k] >= seq[k - 1])
        if inc < len(seq) - 1:
            continue
        y0 = reg[0][1] - 8
        y1 = reg[-1][1] + 36
        band = [r for r in lines if y0 <= r["y0"] <= y1]
        band.sort(key=lambda r: (round(r["y0"] / 12), r["x0"]))
        groups, curg = [], []
        for r in band:
            if curg and r["y0"] - curg[-1]["y0"] > 17:
                groups.append(curg); curg = []
            curg.append(r)
        if curg:
            groups.append(curg)
        code_lines = []
        for g in groups:
            g.sort(key=lambda r: r["x0"])
            parts = []
            for r in g:
                s = r["text"].strip()
                if not parts and NUMLINE_PAT.match(s):
                    continue
                m = NUMCODE_PAT.match(s)
                if not parts and m:
                    s = m.group(2)
                parts.append(s)
            code_lines.append(" ".join(parts).rstrip())
        while code_lines and not code_lines[-1].strip():
            code_lines.pop()
        if len(code_lines) >= 3:
            out.append((reg[0][2], reg[-1][2] + 1, "\n".join(code_lines)))
    return out


def is_toc_line(t):
    """目录行：点线后跟页码（…3），或很长的点线（……/....）。"""
    if re.search(r"[.·…]{2,}\s*\d{1,3}\s*$", t):     # 点线 + 尾部页码
        return True
    if re.search(r"[.·…]{4,}", t):                    # 长点线（≥4）
        return True
    return False


ENDNUM_PAT = re.compile(r"[.·…]{2,}\s*\d{1,3}\s*$")   # 点线 + 页码（TOC 标志）


def _formula_frag(b):
    """块是否为「公式碎片」：短、少汉字、多数学/ASCII 记号。

    多行显示公式被 OCR 拆成许多短行（"n"/"2"/"tr−1(n)"/"≤ ex(n,H)"），
    在块序列里表现为一串连续短块；据此把它们重新聚回一个公式块。
    """
    if b["type"] not in ("p", "formula"):
        return False
    t = b["text"] if isinstance(b["text"], str) else " ".join(b["text"])
    t = t.strip()
    if not t or len(t) > 26:
        return False
    if cjk_count(t) > 4:
        return False
    # 含明显数学记号，或几乎不含汉字
    if re.search(r"[=≤≥≠≈<>∈⊆∑∏∫√→←−+\-*/()]|tr|ex|n\d|\\", t) or cjk_count(t) == 0:
        return True
    return False


def merge_formula_frags(blocks):
    """把连续 ≥3 个「公式碎片」块合并为一个 formula 块。"""
    out = []
    i, n = 0, len(blocks)
    while i < n:
        if _formula_frag(blocks[i]):
            j = i
            while j < n and _formula_frag(blocks[j]):
                j += 1
            if j - i >= 3:
                parts = []
                for k in range(i, j):
                    tx = blocks[k]["text"]
                    parts.append(tx if isinstance(tx, str) else " ".join(tx))
                out.append({"type": "formula", "text": " ".join(parts),
                            "page": blocks[i]["page"]})
                i = j
                continue
        out.append(blocks[i]); i += 1
    return out


def classify(o, pdf_code=None, figs=None):
    """OCR 页对象 → blocks。

    pdf_code: {pageIndex: [{"y0","y1","lines":[...]}]}
              —— pdfcode.py 从等宽字体层抽出的代码区（首选，质量远高于 OCR）。
    figs:     {pageIndex: [{"file","y0","y1","caption","sup"}]}
              —— media.py 抽出的图形区；渲染为图片块，并压制区内 OCR 碎片。
    """
    W, H = o["w"], o["h"]
    raw = page_lines(o)
    if raw:
        endnum = sum(1 for r in raw if ENDNUM_PAT.search(r["text"]))
        # 目录页标志：大量「点线 + 页码」行（真正的 TOC 特征）
        if endnum >= max(4, len(raw) * 0.10):
            return []
        # 目录页（点线被 OCR 拆散时）：含「目录」标题 + 多条「标题 + 页码」形态
        head_txt = "".join(r["text"] for r in raw[:24])
        toc_head = re.search(r"目\s*录", head_txt) or ("目" in head_txt[:12] and "录" in head_txt[:12]) \
            or re.search(r"^\s*CONTENTS", head_txt, re.I)
        if toc_head:
            pagenum_like = sum(1 for r in raw if re.fullmatch(r"[·.\s]*\d{1,3}[·.\s]*", r["text"]))
            chap = sum(1 for r in raw if re.match(r"^\d{1,2}(\.\d{1,2}){0,2}\s", r["text"]))
            if pagenum_like >= 6 or (pagenum_like + chap) >= 10:
                return []
    lines = strip_header_footer(raw, W, H)
    if not lines:
        return []
    lines = [r for r in lines if not is_toc_line(r["text"])]
    if len(lines) < max(3, len(raw) * 0.15):
        return []
    if not lines:
        return []

    # ── 图形区：压制区内 OCR 碎片（图中文字），改由图片承载 ──
    figregs = sorted((figs or {}).get(o["page"], []), key=lambda f: f["y0"])
    figs_kept = []
    if figregs:
        def in_fig(r):
            for f in figregs:
                if "sup" in f:
                    sy0, sy1, sx0, sx1 = f["sup"]
                    if sy0 <= r["y0"] <= sy1 and sx0 <= (r["x0"] + r["x1"]) / 2 <= sx1:
                        return True
                else:                       # 位图图形：按图框 y 带压制
                    if f["y0"] - N_FIG_MARGIN <= r["y0"] <= f["y1"] + N_FIG_MARGIN:
                        return True
            return False
        lines = [r for r in lines if not in_fig(r)]
        figs_kept = figregs
    if not lines and not figs_kept:
        return []

    # ── 代码区（首选 PDF 等宽层）──────────────────────────────
    pdfregs = sorted((pdf_code or {}).get(o["page"], []), key=lambda r: r["y0"])
    if pdfregs:
        def in_code(y):
            # 上边距略宽（OCR 首行有时落在区顶上方几像素），下边距紧（区末 +19px 即下段）
            return any(r["y0"] - 9 <= y <= r["y1"] + 12 for r in pdfregs)
        lines = [r for r in lines if not in_code(r["y0"])]
    ocr_code_at = {}
    if not pdfregs:
        for s, e, c in find_code_regions(lines):
            ocr_code_at[s] = (e, c)
    if not lines and not pdfregs:
        return []

    body_h, lm = stats(lines) if lines else (34, 160)
    rows = roles(lines, W, H, body_h, lm) if lines else []

    # 供段落聚合使用：若 [y_prev, y_next] 之间夹着 PDF 代码区，则不合并
    def crosses_code(y_prev, y_next):
        return any(y_prev <= r["y1"] + 6 and r["y0"] - 6 <= y_next for r in pdfregs)

    blocks = []
    pi = 0
    fi = 0

    def emit_figs_upto(y):
        nonlocal fi
        while fi < len(figs_kept) and figs_kept[fi]["y0"] <= y:
            f = figs_kept[fi]
            blocks.append({"type": "figure", "file": f["file"], "caption": f.get("caption", ""),
                           "page": o["page"]})
            fi += 1

    i, n = 0, len(rows)
    while i < n:
        # 先吐出位于当前行之前的图形区
        emit_figs_upto(rows[i]["y0"])
        # 再把位于当前行之前的 PDF 代码区吐出
        while pi < len(pdfregs) and pdfregs[pi]["y0"] <= rows[i]["y0"]:
            blocks.append({"type": "code", "text": "\n".join(pdfregs[pi]["lines"]),
                           "page": o["page"]})
            pi += 1
        if i in ocr_code_at:
            e, code = ocr_code_at[i]
            blocks.append({"type": "code", "text": code, "page": o["page"]})
            i = e
            continue
        r = rows[i]
        if r["role"] == "empty":
            i += 1; continue

        if r["role"] == "heading":
            t = r["text"]
            lvl = 2
            if re.match(r"^\d+\.\d+", t):
                lvl = 3
            elif re.match(r"^第\s?\d+\s?章", t) or re.match(r"^附录", t):
                lvl = 1
            blocks.append({"type": "h%d" % lvl, "text": t, "page": o["page"]})
            i += 1; continue

        if r["role"] == "figcap":
            blocks.append({"type": "figcap", "text": r["text"], "page": o["page"]})
            i += 1; continue

        if r["role"] == "formula":
            parts, j = [r["text"]], i + 1
            while j < n and rows[j]["role"] == "formula":
                parts.append(rows[j]["text"]); j += 1
            blocks.append({"type": "formula", "text": " ".join(parts), "page": o["page"]})
            i = j; continue

        if r["role"] == "list":
            items, j = [r["text"]], i + 1
            while j < n and rows[j]["role"] == "list":
                items.append(rows[j]["text"]); j += 1
            blocks.append({"type": "list", "text": items, "page": o["page"]})
            i = j; continue

        para, j = r["text"], i + 1
        while j < n and rows[j]["role"] == "text" and not rows[j]["indent"] \
                and not crosses_code(rows[j - 1]["y1"], rows[j]["y0"]):
            s = rows[j]["text"]
            if re.search(r"[A-Za-z0-9]$", para) and re.match(r"^[A-Za-z0-9]", s):
                para += " " + s
            else:
                para += s
            j += 1
        blocks.append({"type": "p", "text": para, "page": o["page"]})
        i = j
    while fi < len(figs_kept):
        f = figs_kept[fi]
        blocks.append({"type": "figure", "file": f["file"], "caption": f.get("caption", ""),
                       "page": o["page"]})
        fi += 1
    while pi < len(pdfregs):
        blocks.append({"type": "code", "text": "\n".join(pdfregs[pi]["lines"]),
                       "page": o["page"]})
        pi += 1
    return blocks


def parse_page(o, pdf_code=None, figs=None):
    return merge_formula_frags(classify(o, pdf_code, figs))


if __name__ == "__main__":
    import sys, json
    data = {}
    pdfc = {}
    if len(sys.argv) > 1 and sys.argv[1].endswith(".jsonl"):
        for ln in open(sys.argv[1], encoding="utf-8"):
            x = json.loads(ln); data[x["page"]] = x
        for pg in [int(a) for a in sys.argv[2:]]:
            if pg not in data:
                continue
            print("=" * 60, "page", pg)
            for b in parse_page(data[pg]):
                tx = b["text"] if isinstance(b["text"], str) else " / ".join(b["text"])
                print(f"  {b['type']:7s} | {tx[:90]}")
