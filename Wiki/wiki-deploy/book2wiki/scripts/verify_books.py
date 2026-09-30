#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证两本书导入质量：渲染错误、红链、分类、统计。"""
import json, urllib.request, urllib.parse, time, os, sys

API = "http://localhost:8080/api.php"


def get(q):
    return json.load(urllib.request.urlopen(API + "?" + urllib.parse.urlencode(q), timeout=40))


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    def norm(t):
        return t.replace("/", "／").replace(":", "：").replace("\\", "＼")

    titles = []
    for d in ["ostep/pages/pageindex.json", "graphtheory/pages/pageindex.json"]:
        p = os.path.join(base, d)
        for x in json.load(open(p, encoding="utf-8")):
            titles.append(norm(x["title"]))
    titles += ["操作系统导论", "图论", "首页"]

    errs = []
    redset = set()
    ok = 0
    for t in titles:
        try:
            r = get({"action": "parse", "page": t, "format": "json", "prop": "text|links"})
        except Exception as e:
            errs.append((t, "REQ " + str(e))); continue
        if "parse" not in r:
            errs.append((t, "missing")); continue
        html = r["parse"]["text"]["*"]
        for kw in ["脚本错误", "Template loop", "解析器函数错误", "mw-ext-cite-error"]:
            if kw in html:
                errs.append((t, kw))
        # 红链统计
        for l in r["parse"].get("links", []):
            if l.get("exists") is None and "*" in l:
                pass
        for m in r["parse"].get("links", []):
            if m.get("exists") is False:
                redset.add(m["*"])
        ok += 1
        time.sleep(0.03)
    print("页面总数", len(titles), "解析成功", ok, "渲染错误", len(errs))
    for e in errs[:30]:
        print("   ERR", e)
    print("红链数(全命名空间)", len(redset))
    for r in sorted(redset)[:40]:
        print("   红链:", r)
    # 站点统计
    st = get({"action": "query", "meta": "siteinfo", "siprop": "statistics", "format": "json"})
    print("站点统计:", json.dumps(st["query"]["statistics"], ensure_ascii=False))


if __name__ == "__main__":
    main()
