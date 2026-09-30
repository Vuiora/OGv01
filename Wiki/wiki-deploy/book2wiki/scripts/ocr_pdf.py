#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
对矢量/扫描 PDF 渲染页面并做高质量 OCR（rapidocr），按页缓存为 JSON。

为什么不用 PDF 自带文字层？
- 本任务两本书：OSTEP 中文版的 ToUnicode 映射有系统性错误（"时"->"谁"）；
  图论中文版是矢量图 + 隐藏的低质 OCR 层（"币，外"/"加速通性"）。
  直接抽取都不可用。渲染 dpi=220 后用 rapidocr 重做，质量远优于原层。

性能：rapidocr(onnxruntime) 默认多线程。串行 ~5s/页 太慢；
改多进程池 + 每进程单线程（OMP_NUM_THREADS=1），20 核可提速近 10 倍。

缓存格式（<out>/ocr_pages.jsonl，每行一个页对象）：
  {"page": i, "dpi": 220, "w": W, "h": H,
   "lines": [{"box": [[x1,y1],...], "text": "...", "score": 0.95}]}

用法：
  python ocr_pdf.py <pdf> <outdir> [--dpi 220] [--workers 12] [--start 0] [--end N]
"""
import os
# 必须在 import onnxruntime 之前设置，限制每进程线程数
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("ORT_NUM_THREADS", "1")

import sys, json, time, argparse
import multiprocessing as mp

_ENG = None
_DOC = None
_PDF = None
_DPI = 220


def _init_worker(pdf, dpi):
    global _ENG, _DOC, _PDF, _DPI
    _PDF, _DPI = pdf, dpi
    import fitz
    _DOC = fitz.open(pdf)
    from rapidocr import RapidOCR
    _ENG = RapidOCR()


def _ocr_page(idx):
    import numpy as np
    pg = _DOC[idx]
    pix = pg.get_pixmap(dpi=_DPI)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = img[:, :, :3]
    W, H = pix.width, pix.height
    try:
        res = _ENG(img)
    except Exception as e:
        return {"page": idx, "dpi": _DPI, "w": W, "h": H, "error": str(e), "lines": []}
    lines = []
    if res is not None and getattr(res, "boxes", None) is not None:
        for box, txt, score in zip(res.boxes, res.txts, res.scores):
            box = [[float(x), float(y)] for x, y in box]
            lines.append({"box": box, "text": txt, "score": float(score)})
    elif res is not None and getattr(res, "txts", None) is not None:
        for txt in res.txts:
            lines.append({"box": None, "text": txt, "score": None})
    return {"page": idx, "dpi": _DPI, "w": W, "h": H, "lines": lines}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("outdir")
    ap.add_argument("--dpi", type=int, default=220)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=-1)
    args = ap.parse_args()

    import fitz
    os.makedirs(args.outdir, exist_ok=True)
    out = os.path.join(args.outdir, "ocr_pages.jsonl")
    done = set()
    if os.path.exists(out):
        for line in open(out, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                done.add(json.loads(line)["page"])
            except Exception:
                pass

    doc = fitz.open(args.pdf)
    total = doc.page_count
    doc.close()
    end = total if args.end < 0 else min(args.end, total)
    todo = [i for i in range(args.start, end) if i not in done]
    print(f"[ocr] {os.path.basename(args.pdf)}: total={total} done={len(done)} "
          f"todo={len(todo)} workers={args.workers}", flush=True)
    if not todo:
        print("[ocr] nothing to do", flush=True)
        return

    fout = open(out, "a", encoding="utf-8")
    t0 = time.time()
    n = 0
    # chunksize 适中，减少 IPC 开销
    with mp.Pool(args.workers, initializer=_init_worker, initargs=(args.pdf, args.dpi)) as pool:
        for o in pool.imap_unordered(_ocr_page, todo, chunksize=4):
            fout.write(json.dumps(o, ensure_ascii=False) + "\n")
            fout.flush()
            n += 1
            if n % 10 == 0 or n == len(todo):
                el = time.time() - t0
                eta = el / n * (len(todo) - n)
                print(f"[ocr] {n}/{len(todo)}  {el:.0f}s elapsed, ETA {eta:.0f}s", flush=True)
    fout.close()
    print(f"[ocr] DONE {os.path.basename(args.pdf)}  {n} pages in {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    mp.freeze_support()
    main()
