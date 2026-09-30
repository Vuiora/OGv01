#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成图书插图的 contact sheet，供视觉终审。

为控制单文件体积（避免 agent 请求体过大 413），每张 sheet 仅放 N 张小图，
缩略图宽度 T，上标红字文件名。输出 _verify2/<tag>_sheet_XX.png。
"""
import os, sys, math
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "_verify2")
THUMB_W = 300
PER_SHEET = 16
COLS = 4
PAD = 10
LABEL_H = 18


def font(size=13):
    for p in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
              "C:/Windows/Fonts/arial.ttf"]:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def make_sheet(files, imgdir, tag, idx):
    f = font(LABEL_H - 4)
    rows = math.ceil(len(files) / COLS)
    cell_w = THUMB_W + PAD
    # 先量最大高度
    thumbs = []
    for fn in files:
        try:
            im = Image.open(os.path.join(imgdir, fn)).convert("RGB")
        except Exception:
            continue
        w, h = im.size
        nh = max(1, int(h * THUMB_W / w))
        im = im.resize((THUMB_W, nh))
        thumbs.append((fn, im))
    cell_h = max((im.size[1] for _, im in thumbs), default=THUMB_W) + LABEL_H + PAD
    W = COLS * cell_w + PAD
    H = rows * cell_h + PAD
    sheet = Image.new("RGB", (W, H), (250, 250, 250))
    d = ImageDraw.Draw(sheet)
    for i, (fn, im) in enumerate(thumbs):
        r, c = divmod(i, COLS)
        x = PAD + c * cell_w
        y = PAD + r * cell_h
        d.text((x, y), fn, fill=(200, 0, 0), font=f)
        sheet.paste(im, (x, y + LABEL_H))
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, "%s_sheet_%02d.png" % (tag, idx))
    sheet.save(p, "PNG", optimize=True)
    return p


def run(tag, imgdir):
    files = sorted(f for f in os.listdir(imgdir) if f.lower().endswith(".png"))
    sheets = []
    for i in range(0, len(files), PER_SHEET):
        sheets.append(make_sheet(files[i:i + PER_SHEET], imgdir, tag,
                                 i // PER_SHEET))
    return sheets


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if which in ("ostep", "both"):
        ss = run("ostep", os.path.join(ROOT, "ostep", "img"))
        print("OSTEP sheets:", len(ss))
    if which in ("gt", "both"):
        ss = run("gt", os.path.join(ROOT, "graphtheory", "img"))
        print("GT sheets:", len(ss))
