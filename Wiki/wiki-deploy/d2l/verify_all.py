#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""导入后全站验证：页面数、红链、公式渲染、图片、分类。"""
import os, re, json, sys, time
import urllib.parse, urllib.request

API = "http://localhost:8080/api.php"
UA = "OGWiki-d2l-verify/1.0"

def call(params):
    params = dict(params); params["format"] = "json"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(API, data=data, headers={
        "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))

# 站点统计
st = call({"action": "query", "meta": "siteinfo", "siprop": "statistics"})["query"]["statistics"]
print("=== 站点统计 ===")
print("  页面总数:", st["pages"], "| 条目:", st["articles"], "| 编辑:", st["edits"],
      "| 文件:", st.get("images", 0))

# 红链
wp = call({"action": "query", "list": "querypage", "qppage": "Wantedpages", "qplimit": 100})
res = wp.get("query", {}).get("querypage", {}).get("results", [])
print("\n=== 红链 ===")
print("  数量:", len(res))
for x in res[:15]:
    print("   -", x.get("title"))

# 分类
cats = call({"action": "query", "list": "allcategories", "aclimit": 200})
cl = cats.get("query", {}).get("allcategories", [])
print("\n=== 分类 ===")
print("  数量:", len(cl))
for c in cl:
    if "动手学深度学习" in c["*"] or c["*"] in ("预备知识", "线性神经网络", "优化算法"):
        print("   -", c["*"])
