#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
media.py —— 抽取 PDF 插图区，生成「图形区映射」figmap，供 layout 压制图形内的 OCR 文本碎片。

背景（见 README）：
  图中文字（状态图标签、时间轴刻度、表格单元）被 OCR 拍平成一堆竖直单词段落，
  例如「图4.1 加载」被展开为 CPU / 内存 / 代码 / 静态数据 / 堆 / 栈……
  正确做法：把图形区域渲染成 PNG 上传，正文只留「图注 + 图片」，并**压制**区内 OCR 文本。

两类图形的可靠定位方式（因书而异）：
  - **raster**（OSTEP）：图形是**内嵌位图**，`get_image_rects()` 给出精确页面位置。
  - **pile**（图论）：图形是**矢量轮廓**（该 PDF 的正文也是矢量字形轮廓，无法用 drawings 区分），
    但图形内的标签会被 OCR 成「**短碎片竖排堆**」——由 ≤5 个短行、无句读、紧邻图注构成。
    以此堆为锚，从页面渲染裁剪图形区。

产物：
  <outdir>/bookimg__<tag>_p<page>_<k>.png   图形位图
  <outdir>/figmap.json                      {page:[{file,x0,y0,x1,y1,caption,src}]}（OCR 像素坐标）
"""
import os, json, re, argparse
import pymupdf

DPI = 220
SCALE = DPI / 72.0
MIN_W_PT, MIN_H_PT = 55, 32

CAPTION_PAT = re.compile(r"^(图|表)\s?\d+([.\-]\d+)*")
PUNCT_END = re.compile(r"[。，；：、,.!?;:]$")


def render_clip(pg, rect, dpi=200):
    r = pymupdf.Rect(rect); r.normalize()
    if r.width < 1 or r.height < 1:
        return None
    try:
        return pg.get_pixmap(matrix=pymupdf.Matrix(dpi / 72.0, dpi / 72.0), clip=r).tobytes("png")
    except Exception:
        return None


def _lines_px(o):
    out = []
    for l in o["lines"]:
        if not l.get("box"):
            continue
        xs = [p[0] for p in l["box"]]; ys = [p[1] for p in l["box"]]
        out.append({"x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys),
                    "text": l["text"].strip()})
    out.sort(key=lambda r: (round(r["y0"] / 8), r["x0"]))
    return out


# ───────────────────────── raster（OSTEP）─────────────────────────

def y_clusters(rects, gap=14.0):
    if not rects:
        return []
    rects = sorted(rects, key=lambda r: r.y0)
    out, cur = [], [rects[0]]
    y0, y1 = rects[0].y0, rects[0].y1
    for r in rects[1:]:
        if r.y0 <= y1 + gap:
            cur.append(r); y1 = max(y1, r.y1)
        else:
            out.append((y0, y1, cur)); cur = [r]; y0, y1 = r.y0, r.y1
    out.append((y0, y1, cur))
    return out


def raster_rects(pg, min_w=MIN_W_PT, min_h=MIN_H_PT):
    rs = []
    for im in pg.get_images(full=True):
        xref, w, h = im[0], im[2], im[3]
        if w < 16 or h < 16:
            continue
        for r in pg.get_image_rects(xref):
            if r.width >= min_w and r.height >= min_h:
                rs.append(r)
    out = []
    for y0, y1, grp in y_clusters(rs):
        x0 = min(r.x0 for r in grp); x1 = max(r.x1 for r in grp)
        if (x1 - x0) >= min_w and (y1 - y0) >= min_h:
            out.append(pymupdf.Rect(x0, y0, x1, y1))
    return out


# ───────────────────────── pile（图论 / 表格）─────────────────────────

NUMLIST = re.compile(r"^\s*(\d{1,3}[.)、]|[（(]\d+[）)]|[a-zA-Z][.)])\s")
# 代码行：行号 + 代码（如 "14 exit(1);" / "5 #include <stdio.h>"）
CODE_LINE = re.compile(r"^\s*\d{1,3}\s+\S")


def _is_code_band(b, col_w):
    """带是否为「行号 + 代码」行（图形/表格检测应跳过代码）。

    代码行特征：窄、左对齐（不横跨文本列）、以「行号 + ASCII 代码」开头。
    表格数据行虽也以数字开头，但**横跨多列**（宽），须排除。
    """
    rows = b["rows"]
    if not rows:
        return False
    span = b["x1"] - b["x0"]
    if span > 0.62 * col_w:          # 横跨多列 → 表格/图形行，不是代码
        return False
    if len(rows) >= 3:               # 多单元 → 表格行
        return False
    first = min(rows, key=lambda r: r["x0"])["text"]
    if not (CODE_LINE.match(b["text"]) or CODE_LINE.match(first)):
        return False
    # 表格数据行也形如「5 运行」——须要求行号后的内容是 ASCII 代码，而非中文单元格。
    if re.search(r"[\u4e00-\u9fff]", b["text"]):
        return False
    return True

# 图注「锚」：图/表 + 编号（可跟标题文字）
CAP_START = re.compile(r"^[图表]\s?\d+([.\-]\d+)*")
# 图注的「引用性句子」标志（"表9.1展示了一段时间内……" 不是图注而是正文引用）
CAP_REF = re.compile(r"(展示|所示|可以看出|说明了|给出了|表明|见下表|见下图|如下|则是|就是|是一张示意图|如示意图|是示意图|见\s?图|见\s?表)")

BAND_GAP = 12       # 同一 y 带内允许的行中心距（px）
CONTENT_GAP = 120   # 图形内相邻「内容带」的允许间距（px）；需容忍「图注↔图形」的空隙
CAP_GAP = 240       # 「图注 ↔ 图形」单独放宽的间距：矢量图形（图论）内无 OCR 文字时二者离得远
FIG_XPAD = 14


def is_prose(t):
    """判定「正文行」：较长、或以句读结尾、或编号列表项。

    图形/表格内的标签通常短、无句读；正文行则相反。以此区分。
    （仅用于「单行」判断；多片段行由 _band_prose 处理。）
    """
    if not t:
        return True
    if CAPTION_PAT.match(t):
        return True
    if len(t) >= 12:
        return True
    if PUNCT_END.search(t):
        return True
    if NUMLIST.match(t):
        return True
    return False


def _mk_band(rows):
    rows = sorted(rows, key=lambda r: r["x0"])
    return {
        "rows": rows,
        "y0": min(r["y0"] for r in rows),
        "y1": max(r["y1"] for r in rows),
        "x0": min(r["x0"] for r in rows),
        "x1": max(r["x1"] for r in rows),
        "text": re.sub(r"[\s\u3000]+", " ", " ".join(r["text"] for r in rows)).strip(),
    }


def y_bands(lines_px):
    """把行按 y 近似聚成「带」：同一视觉行内的多个 x 片段合成一带。

    表格一行常被 OCR 拆成多个单元格片段（x 不同、y 相近），必须先按 y 归并，
    否则无法判断「这是一行正文」还是「这是一行表格单元」。
    """
    if not lines_px:
        return []
    ls = sorted(lines_px, key=lambda r: (r["y0"] + r["y1"]) / 2)
    bands, cur = [], [ls[0]]
    for r in ls[1:]:
        cy = (r["y0"] + r["y1"]) / 2
        ok = any(abs(cy - (b["y0"] + b["y1"]) / 2) <= BAND_GAP for b in cur)
        if ok:
            cur.append(r)
        else:
            bands.append(_mk_band(cur)); cur = [r]
    bands.append(_mk_band(cur))
    return bands


def _band_prose(b, col_w, strict=False):
    """判定「带」是否为正文行（而非图形/表格内容）。

    正文：片段极少（≤2）且并起来够长、够宽（横跨文本列）；或并起来含句末标点且够长。
    表格/图形：通常 ≥3 个片段，或片段很短。
    strict=True 时（用于**表格**向下扩展）：只把「以句末标点结尾的较长句」当正文——
    因为表格数据行常被 OCR 并成长句，形似正文。
    """
    rows = b["rows"]
    span = b["x1"] - b["x0"]
    t = b["text"]
    if strict:
        # 正文段行：横跨文本栏（span 大）且片段间无明显列间隙；
        # 表格数据行：单元只占一列（span 小）或单元间有明显间隙。
        if len(t) < 14:
            return False
        if span < 0.55 * col_w:
            return False
        rs = sorted(rows, key=lambda r: r["x0"])
        max_gap = max((rs[i + 1]["x0"] - rs[i]["x1"] for i in range(len(rs) - 1)),
                      default=0)
        return max_gap < 0.035 * col_w
    # 代码清单行：含「行号」片段（1-3 位纯数字，独立成片段）且带内含 C 代码记号 → 非正文。
    if _looks_like_code(t, rows, col_w):
        return False
    # 以句末标点结尾（含 ASCII 句点/问叹号——书中译本正文常用 "." 断句）→ 正文
    if len(t) >= 13 and re.search(r"[。！？.!?]$", t):
        return True
    # 单片段且以句末标点结尾的短句（如 "问来查找有效的转换映射。"）→ 正文
    if len(rows) == 1 and len(t) >= 8 and re.search(r"[。！？.!?]$", t):
        return True
    # 含句末/分隔标点且较长 → 正文（即使被 OCR 拆成多片段，如 "只能提供一半的峰值带宽。因此…"）
    if len(t) >= 18 and re.search(r"[。！？；]", t):
        return True
    if len(rows) >= 3:
        return False                      # 多单元行 → 表格/图形
    if len(rows) == 1:
        r = rows[0]
        return len(r["text"]) >= 12 and span >= 0.42 * col_w
    # 2 片段：可能为「表格两单元」（左单元短 + 大间隙）或「正文被拆两半」。
    rs = sorted(rows, key=lambda r: r["x0"])
    gap = rs[1]["x0"] - rs[0]["x1"]
    if len(rs[0]["text"]) <= 24 and gap > 0.10 * col_w:
        return False                      # 表格单元行 → 非正文
    # 并起来是长句且横跨 → 正文
    return len(t) >= 20 and span >= 0.42 * col_w


CODE_TOK = re.compile(r"[<>{};=]|->|\bint\b|\bchar\b|\breturn\b|\bprintf\b|\bwhile\b"
                      r"|\bif\b|\binclude\b|\bvoid\b|\bstruct\b|\bexit\b|\bmalloc\b"
                      r"|\bfor\b|\bmain\b|\bNULL\b|%e[a-z]{2}|%[a-z]{2}x")
# 代码标点（含汇编寄存器记号 % 与注释 #）：用于「无行号、几乎全 ASCII」行判定
CODE_PUNCT = re.compile(r"[;=(){}\[\]<>%*&#]|\b\w+\(.*\)\s*;")


def _looks_like_code(t, rows, col_w):
    """代码清单行：含 C/汇编代码记号，且为「行号式」或「ASCII 占比极高」。

    三类代码行：
      1) 带行号：如 "14 exit(1);" / "9 RaiseException(PROTECTION_FAULT)" ——
         存在 1-3 位纯数字的窄行号片段，且该行几乎无中文；
      2) 无行号但含代码记号：如 "struct inode *cwd;"；
      3) 汇编等：含 %寄存器/注释 #，且几乎全 ASCII。
    """
    cjk = sum(1 for c in t if "\u4e00" <= c <= "\u9fff")
    has_lineno = any(re.fullmatch(r"\d{1,3}", r["text"].strip())
                     and (r["x1"] - r["x0"]) < 0.08 * col_w for r in rows)
    if has_lineno and cjk <= max(2, len(t) * 0.18):
        return True
    if CODE_TOK.search(t) and cjk <= max(2, len(t) * 0.15):
        return True
    if CODE_PUNCT.search(t) and cjk <= max(1, len(t) * 0.10):
        return True
    return False


def is_caption_band(b):
    """图注带：以「图/表 N.M」开头，且不是「引用句」（如『表9.1展示了……』）。

    注意：真图注常含逗号/冒号（『图8.1.1 梳背为 R = x0x1··，梳齿为白色顶点的梳』），
    故**不能**因含逗号而排除；只有含「展示/所示/见下」等引用动词、或以句号收尾的长句才是引用。
    """
    t = b["text"]
    m = CAP_START.match(t)
    if not m:
        return False
    if CAP_REF.search(t):
        return False
    if len(t) >= 20 and re.search(r"[。！？]$", t):
        return False
    return True


def figure_regions(pg, lines_px, min_h=36, x_pad=FIG_XPAD):
    """以**图注带**为锚，向【上/下】扩「内容带」，取较长的一侧作为图形区。

    - 图形（图）：图注通常在图形**下方** → 向上扩展；
    - 表格（表）：表题通常在表格**上方** → 向下扩展；
    - 二者都靠「内容带 vs 正文带」的判据自动选择方向，无需预设。
    返回 [(rect_pt, caption, suppress_px=(top,bot,x0,x1))]。
    """
    bands = y_bands(lines_px)
    if not bands or _is_toc_page(lines_px):
        return []
    # 文本列范围（用正文行估计；找不到则用整页）
    prose = [r for r in lines_px
             if is_prose(r["text"]) and not CAP_START.match(r["text"])]
    if prose:
        col_x0 = min(l["x0"] for l in prose) - x_pad
        col_x1 = max(l["x1"] for l in prose) + x_pad
    else:
        col_x0, col_x1 = 0, pg.rect.width * SCALE
    col_w = max(1, col_x1 - col_x0)
    max_bands = 60
    max_h_px = 0.92 * pg.rect.height * SCALE

    out = []
    H = pg.rect.height * SCALE
    for ci, b in enumerate(bands):
        if not is_caption_band(b):
            continue
        # 向下扩展（表题在表格上方：表格数据行形似正文，用 strict 判据）
        d_end, j, last_y = ci, ci + 1, b["y1"]
        while j < len(bands) and (j - ci) <= max_bands:
            nb = bands[j]
            gap = CAP_GAP if j == ci + 1 else CONTENT_GAP   # 图注↔首块放宽
            if _is_margin(nb, H) or nb["y0"] - last_y > gap: break
            if _band_prose(nb, col_w, strict=True) or is_caption_band(nb): break
            d_end, last_y = j, nb["y1"]; j += 1
        # 向上扩展
        u_start, j, first_y = ci, ci - 1, b["y0"]
        while j >= 0 and (ci - j) <= max_bands:
            nb = bands[j]
            gap = CAP_GAP if j == ci - 1 else CONTENT_GAP   # 图注↔首块放宽
            if _is_margin(nb, H) or first_y - nb["y1"] > gap: break
            if _band_prose(nb, col_w) or is_caption_band(nb): break
            u_start, first_y = j, nb["y0"]; j -= 1
        down_n, up_n = d_end - ci, ci - u_start
        if down_n == 0 and up_n == 0:
            continue
        # 方向优先：图 N.M 的图注在图形**下方** → 向上扩展；表 N.M 的表题在表格**上方** → 向下扩展。
        # 仅当优先方向无内容时才回退到另一侧。
        is_table = b["text"].lstrip().startswith("表")
        prefer_down = is_table
        if prefer_down:
            if down_n == 0 and up_n > 0:
                seg = bands[u_start:ci] + [b]
            else:
                seg = [b] + bands[ci + 1:d_end + 1]
        else:
            if up_n == 0 and down_n > 0:
                seg = [b] + bands[ci + 1:d_end + 1]
            else:
                seg = bands[u_start:ci] + [b]
        ex = _seg_xext(seg, x_pad)
        if ex is None:
            continue
        sx0, sx1 = ex
        # 双栏页：图形区不得跨越栏间空白。若该段内容只占一栏，就只取那一栏宽度。
        sx0 = max(sx0, col_x0); sx1 = min(sx1, col_x1)
        if sx1 - sx0 < 40:
            continue
        # 依实际选取的 seg 计算上下边界（seg[0]/seg[-1] 即区间首尾带）
        top = min(x["y0"] for x in seg) - 6
        bot = max(x["y1"] for x in seg) + 6
        if bot - top < min_h or bot - top > max_h_px:
            continue
        rect = pymupdf.Rect(sx0 / SCALE, top / SCALE, sx1 / SCALE, bot / SCALE)
        out.append((rect, b["text"], (top, bot, sx0, sx1)))
    return out


def trim_png(png_bytes, bg_thr=247, pad=6):
    """裁掉图片四周的纯白边（图形区检测的兜底：先粗裁再自动收紧）。"""
    try:
        import numpy as np
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(png_bytes)).convert("L")
        a = np.asarray(im)
        mask = a < bg_thr
        if not mask.any():
            return png_bytes
        ys, xs = np.where(mask)
        y0, y1 = max(0, ys.min() - pad), min(a.shape[0], ys.max() + pad + 1)
        x0, x1 = max(0, xs.min() - pad), min(a.shape[1], xs.max() + pad + 1)
        im2 = Image.open(io.BytesIO(png_bytes)).crop((x0, y0, x1, y1))
        buf = io.BytesIO(); im2.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return png_bytes


def _col_range(lines_px, x_pad):
    prose = [r for r in lines_px
             if is_prose(r["text"]) and not CAP_START.match(r["text"])]
    if prose:
        return min(l["x0"] for l in prose) - x_pad, max(l["x1"] for l in prose) + x_pad
    if lines_px:
        return min(l["x0"] for l in lines_px) - x_pad, max(l["x1"] for l in lines_px) + x_pad
    return None


def _is_margin(b, H):
    """页眉/页脚带（高度占比）。"""
    cy = (b["y0"] + b["y1"]) / 2
    return cy < 0.10 * H or cy > 0.94 * H


def _is_toc_page(lines_px):
    """目录/索引页：大量「点线 + 尾部页码」行。此类页整体跳过（不抓示意图）。"""
    if not lines_px:
        return False
    endnum = sum(1 for r in lines_px if re.search(r"[.·…]{2,}\s*\d{1,3}\s*$", r["text"]))
    if endnum >= max(4, len(lines_px) * 0.10):
        return True
    head = "".join(r["text"] for r in lines_px[:26])
    if re.search(r"目\s*录", head) or re.search(r"^\s*CONTENTS", head, re.I):
        pn = sum(1 for r in lines_px if re.fullmatch(r"[·.\s]*\d{1,3}[·.\s]*", r["text"]))
        return pn >= 6
    return False


MATH_GLYPH = set("∈∉⊆⊇⊂⊃∪∩∅≠≤≥≈≅→←↦⇒∀∃∑∏∫√∞∂∇±×÷·∘⋯…")
FORMULAISH = re.compile(r"[=≥≤≠≈⊆⊇∈∑∏∫√∞∇∂ΔΣΠΓΩκλμσ]|:=|→|←")


def _is_formula_band(b):
    """判定带是否为公式：片段少、数学符号密度高、或含明显公式记号。"""
    t = b["text"]
    if not t:
        return False
    dens = sum(1 for c in t if c in MATH_GLYPH) / len(t)
    if dens >= 0.10:
        return True
    if len(t) <= 70 and FORMULAISH.search(t):
        return True
    return False


def _seg_xext(seg, x_pad):
    """图形区的横向范围：**只用该段自身内容带**的 x 范围（避免双栏页跨栏误并）。"""
    rows = [r for b in seg for r in b["rows"]]
    if not rows:
        return None
    return (min(r["x0"] for r in rows) - x_pad, max(r["x1"] for r in rows) + x_pad)


EQNUM = re.compile(r"\(\d{1,2}[.\-]\d{1,2}\)")


def _seg_math_density(seg):
    t = "".join(b["text"] for b in seg)
    if not t:
        return 0.0
    return sum(1 for c in t if c in MATH_GLYPH) / len(t)


def _seg_is_formula(seg):
    """整段是否为「公式堆」：数学符号密度高 / 含公式编号 / 多数带是公式。"""
    t = "".join(b["text"] for b in seg)
    if EQNUM.search(t):
        return True
    if _seg_math_density(seg) >= 0.075:
        return True
    fml = sum(1 for b in seg if _is_formula_band(b))
    return fml * 3 >= len(seg) * 2      # ≥2/3 的带是公式



def fragment_regions(pg, lines_px, existing, min_h=40, page_no=None):
    """**兜底**：无图注的示意图 / 跨页续表。

    OSTEP 大量示意图只由「如上所示：」引出、无「图 N.M」图注（如空闲列表、盘块布局、
    老 UNIX 文件系统结构）；表格跨页续也常无图注（页首直接是「续表」+ 数据行）。
    特征：连续若干「非正文带」中存在**多列带**（≥3 片段，说明是排布的行列单元）。
    以这些带聚成一段，整段渲染为图片。
    """
    bands = y_bands(lines_px)
    if not bands or _is_toc_page(lines_px):
        return []
    H = pg.rect.height * SCALE
    cr = _col_range(lines_px, FIG_XPAD)
    if not cr:
        return []
    col_x0, col_x1 = cr
    col_w = max(1, col_x1 - col_x0)
    ex = [(r.x0 * SCALE, r.y0 * SCALE, r.x1 * SCALE, r.y1 * SCALE) for r in existing]

    def overlaps(top, bot):
        for a0, b0, a1, b1 in ex:
            if min(bot, b1) - max(top, b0) > 8:
                return True
        return False
    out = []
    i, nb = 0, len(bands)
    min_h_seg = 76
    while i < nb:
        b = bands[i]
        if _band_prose(b, col_w) or is_caption_band(b) or _is_margin(b, H) or _is_code_band(b, col_w):
            i += 1; continue
        seg, j = [b], i + 1
        while j < nb:
            x = bands[j]
            if _band_prose(x, col_w) or is_caption_band(x) or _is_margin(x, H) or _is_code_band(x, col_w):
                break
            if x["y0"] - seg[-1]["y1"] > CONTENT_GAP:
                break
            seg.append(x); j += 1
        rows_multi = max(len(x["rows"]) for x in seg)
        rows_total = sum(len(x["rows"]) for x in seg)
        top = seg[0]["y0"] - 8
        bot = seg[-1]["y1"] + 8
        xext = _seg_xext(seg, FIG_XPAD)
        multi_ok = (len(seg) >= 2 and rows_multi >= 3 and rows_total >= 5 and bot - top >= min_h_seg) \
            or (rows_multi >= 4 and rows_total >= 4)     # 单行多单元块图（如盘块布局条）
        if xext and multi_ok and not _seg_is_formula(seg) and not overlaps(top, bot):
            sx0 = max(xext[0], col_x0); sx1 = min(xext[1], col_x1)
            if sx1 - sx0 >= 40:
                rect = pymupdf.Rect(sx0 / SCALE, top / SCALE, sx1 / SCALE, bot / SCALE)
                out.append((rect, "示意图 p%s" % (page_no if page_no is not None else "?"),
                            (top, bot, sx0, sx1)))
        i = j
    return out


# ───────────────────────── 统一入口 ─────────────────────────

def extract(pdf_path, ocr_pages, outdir, tag, mode, dpi=DPI):
    doc = pymupdf.open(pdf_path)
    os.makedirs(outdir, exist_ok=True)
    figmap = {}
    n = 0
    for pno in range(doc.page_count):
        pg = doc[pno]
        o = ocr_pages.get(pno)
        items = []          # (rect_pt, caption, suppress_px=(y0,y1,x0,x1))
        lines_px = _lines_px(o) if o else []
        if mode == "raster":
            for r in sorted(raster_rects(pg), key=lambda z: z.y0):
                cap = find_caption(lines_px, r) or ("图 p%d" % pno)
                items.append((r, cap, None))
            # 补充：OCR 碎片堆（无位图对应的表格/文字图）
            if o:
                claimed = set(_cap_key(it[1]) for it in items if not it[1].startswith("图 p"))
                for r, cap, sup in figure_regions(pg, lines_px):
                    if any(_overlap_px(r, it[0]) for it in items):
                        continue
                    if _bad_caption(cap):        # 图注实为标题/正文引用 → 跳过
                        continue
                    # 该图注已被位图认领 → 不再重复造图
                    if _cap_key(cap) in claimed:
                        continue
                    items.append((r, cap, sup))
                # 兜底：无图注示意图 / 跨页续表
                for r, cap, sup in fragment_regions(pg, lines_px, [it[0] for it in items],
                                                    page_no=pno):
                    if not any(_overlap_px(r, it[0]) for it in items):
                        items.append((r, cap, sup))
        else:  # pile
            if not o:
                continue
            for r, cap, sup in figure_regions(pg, lines_px):
                if _bad_caption(cap):
                    continue
                items.append((r, cap, sup))
        entries = []
        for k, (r, cap, sup) in enumerate(items):
            png = render_clip(pg, r, dpi)
            if png is None:
                continue
            if mode == "pile":
                png = trim_png(png)
            # 近空白图（渲染后墨迹极少）丢弃
            if _ink_ratio(png) < 0.004:
                continue
            nm = "bookimg__%s_p%03d_%d.png" % (tag, pno, k)
            open(os.path.join(outdir, nm), "wb").write(png)
            e = {"file": nm, "x0": round(r.x0 * SCALE), "y0": round(r.y0 * SCALE),
                 "x1": round(r.x1 * SCALE), "y1": round(r.y1 * SCALE), "caption": cap}
            if sup:
                e["sup"] = [round(v) for v in sup]
            entries.append(e)
            n += 1
        if entries:
            figmap[pno] = entries
    doc.close()
    json.dump(figmap, open(os.path.join(outdir, "figmap.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return figmap, n


def find_caption(lines_px, rect_pt):
    x0, y0, x1, y1 = [v * SCALE for v in (rect_pt.x0, rect_pt.y0, rect_pt.x1, rect_pt.y1)]
    cands = []
    for l in lines_px:
        cx = (l["x0"] + l["x1"]) / 2
        if not (x0 - 40 <= cx <= x1 + 40):
            continue
        d = l["y0"] - y1
        if 0 <= d <= 46 and CAPTION_PAT.match(l["text"]):
            cands.append((d, l["text"]))
    if not cands:
        for l in lines_px:
            cx = (l["x0"] + l["x1"]) / 2
            if not (x0 - 40 <= cx <= x1 + 40):
                continue
            d = y0 - l["y1"]
            if 0 <= d <= 30 and CAPTION_PAT.match(l["text"]):
                cands.append((d, l["text"]))
    cands.sort()
    return cands[0][1] if cands else None


def _cap_key(cap):
    """图注归一化 key（去掉空白/标点），用于去重。"""
    return re.sub(r"[\s\u3000·.。，,：:；;]", "", cap or "")


# 明显不是图注的文字：章节标题 / 引用句 / 正文残句
HEAD_LIKE = re.compile(r"^(\d{1,2}(\.\d{1,2}){1,2}\s+\S|第\s?\d+\s?章|附录)")
CAP_BAD_WORDS = re.compile(r"(展示了|所示|可以看出|说明了|给出了|表明|如下|则是|参见|见其他|是一张示意图|是示意图|运行它|阅读它|应该作|该协议)")
def _bad_caption(cap):
    """图注本身是正文/标题（而非真图注）→ 不应据此造图。"""
    if not cap:
        return False
    t = cap.strip()
    if HEAD_LIKE.match(t) and not re.match(r"^[图表]", t):
        return True
    # 以「图/表」开头但其实是引用句
    if re.match(r"^[图表]", t) and CAP_BAD_WORDS.search(t):
        return True
    if len(t) >= 18 and re.search(r"[。！？]$", t):
        return True
    return False


def _ink_ratio(png_bytes):
    """返回渲染图的「非白像素」比例，用于剔除近空白图。"""
    try:
        import numpy as np, io
        from PIL import Image
        a = np.asarray(Image.open(io.BytesIO(png_bytes)).convert("L"))
        return float((a < 235).mean())
    except Exception:
        return 1.0


def _overlap_px(rect_pt, other_pt, thr=0.4):
    a = (rect_pt.x0 * SCALE, rect_pt.y0 * SCALE, rect_pt.x1 * SCALE, rect_pt.y1 * SCALE)
    b = (other_pt.x0 * SCALE, other_pt.y0 * SCALE, other_pt.x1 * SCALE, other_pt.y1 * SCALE)
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return False
    amin = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return amin > 0 and inter / amin >= thr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("ocr_jsonl")
    ap.add_argument("outdir")
    ap.add_argument("--tag", default="img")
    ap.add_argument("--mode", choices=["raster", "pile"], default="raster")
    args = ap.parse_args()
    ocr = {}
    for ln in open(args.ocr_jsonl, encoding="utf-8"):
        x = json.loads(ln); ocr[x["page"]] = x
    fm, n = extract(args.pdf, ocr, args.outdir, args.tag, args.mode)
    print("提取图形 %d 张，覆盖 %d 页 -> %s" % (n, len(fm), args.outdir))


if __name__ == "__main__":
    main()
