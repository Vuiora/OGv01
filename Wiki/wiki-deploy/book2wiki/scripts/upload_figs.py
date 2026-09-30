#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""上传 book2wiki 抽取的图书插图（bookimg__*.png）到 MediaWiki。幂等可重跑。

- 逐张上传 ostep/img/*.png 与 graphtheory/img/*.png；
- 已存在且同名则覆盖（ignorewarnings=1）；
- 上传失败仅记录，不中断。
"""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from import_book import MW, ROOT

CT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml"}


def upload_dir(mw, imgdir):
    if not os.path.isdir(imgdir):
        print("目录不存在:", imgdir)
        return 0, 0
    files = sorted(f for f in os.listdir(imgdir) if f.lower().endswith((".png", ".jpg", ".jpeg", ".svg")))
    ok = fail = 0
    for fn in files:
        data = open(os.path.join(imgdir, fn), "rb").read()
        ext = os.path.splitext(fn)[1].lower()
        r = mw.upload(fn, data, CT.get(ext, "application/octet-stream"))
        u = r.get("upload", {})
        err = r.get("error", {}).get("code", "")
        if u.get("result") == "Success":
            ok += 1
        elif u.get("result") == "Warning" and u.get("warnings"):
            # 已存在同名文件等 → 视为覆盖成功
            ok += 1
        elif err == "fileexists-no-change":
            ok += 1          # 内容与线上完全一致 → 无需更新，视为成功
        else:
            fail += 1
            print("FAIL", fn, json.dumps(r, ensure_ascii=False)[:150])
        time.sleep(0.05)
    return ok, fail


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    mw = MW(); mw.login()
    if which in ("ostep", "both"):
        ok, fail = upload_dir(mw, os.path.join(ROOT, "ostep", "img"))
        print("OSTEP 插图: 成功 %d / 失败 %d" % (ok, fail))
    if which in ("gt", "both"):
        ok, fail = upload_dir(mw, os.path.join(ROOT, "graphtheory", "img"))
        print("图论 插图: 成功 %d / 失败 %d" % (ok, fail))


if __name__ == "__main__":
    main()
