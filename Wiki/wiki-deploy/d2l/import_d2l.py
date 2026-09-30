#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""导入 d2l 知识库到 MediaWiki：先上传图片，再写入页面。幂等可重跑。"""
import os, re, json, sys, time, hashlib
import urllib.parse, urllib.request

API = "http://localhost:8080/api.php"
UA = "OGWiki-d2l-importer/1.0"
ROOT = r"C:\Users\Lenovo\Desktop\OGv01\Wiki\.build\d2l"
PAGES = os.path.join(ROOT, "pages")
IMGDIR = os.path.join(ROOT, "img")
BOTUSER = "Admin@ogwikiimport"
BOTPW = open(r"C:\Users\Lenovo\Desktop\OGv01\Wiki\wiki-deploy\botpw.txt").read().strip()

class MW:
    def __init__(self):
        self.cookies = {}
    def call(self, params, post=False, files=None):
        params = dict(params); params["format"] = "json"
        if files:
            boundary = "----ogwiki" + hashlib.md5(str(time.time()).encode()).hexdigest()
            body = b""
            for k, v in params.items():
                body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode()
            for k, (fn, data, ct) in files.items():
                body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n" % (boundary, k, fn, ct)).encode()
                body += data + b"\r\n"
            body += ("--%s--\r\n" % boundary).encode()
            req = urllib.request.Request(API, data=body, headers={
                "User-Agent": UA, "Content-Type": "multipart/form-data; boundary=" + boundary})
        else:
            data = urllib.parse.urlencode(params).encode()
            req = urllib.request.Request(API, data=data, headers={
                "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
        if self.cookies:
            req.add_header("Cookie", "; ".join("%s=%s" % (k, v) for k, v in self.cookies.items()))
        with urllib.request.urlopen(req, timeout=120) as r:
            for h in r.headers.get_all("Set-Cookie") or []:
                kv = h.split(";")[0]
                if "=" in kv:
                    k, v = kv.split("=", 1); self.cookies[k] = v
            return json.loads(r.read().decode("utf-8"))

    def login(self):
        t = self.call({"action": "query", "meta": "tokens", "type": "login"})["query"]["tokens"]["logintoken"]
        r = self.call({"action": "login", "lgname": BOTUSER, "lgpassword": BOTPW, "lgtoken": t}, post=True)
        res = r.get("login", {}).get("result")
        print("登录:", res)
        if res != "Success":
            print(json.dumps(r, ensure_ascii=False)); sys.exit(1)
        self.csrf = self.call({"action": "query", "meta": "tokens", "type": "csrf"})["query"]["tokens"]["csrftoken"]

    def upload(self, name, data, ct):
        r = self.call({"action": "upload", "filename": name, "token": self.csrf,
                       "ignorewarnings": "1", "comment": "d2l 教材插图"},
                      files={"file": (name, data, ct)})
        return r

    def edit(self, title, text):
        return self.call({"action": "edit", "title": title, "text": text,
                          "summary": "导入《动手学深度学习》知识条目", "token": self.csrf,
                          "bot": "1", "contentmodel": "wikitext", "recreate": "1"})

CT = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

def main():
    mw = MW(); mw.login()
    # 1) 上传图片（仅正文引用的）
    used = set()
    for f in os.listdir(PAGES):
        t = open(os.path.join(PAGES, f), encoding="utf-8").read()
        for m in re.findall(r"\[\[文件:([^\]|]+)", t):
            used.add(m.strip())
    print("需上传图片:", len(used))
    up_ok = up_skip = up_fail = 0
    fails = []
    for name in sorted(used):
        # 本地文件保存为 'img__<basename>'（原路径 img/xxx）
        src = os.path.join(IMGDIR, "img__" + name)
        if not os.path.exists(src):
            src = os.path.join(IMGDIR, name)
        if not os.path.exists(src):
            up_fail += 1; fails.append((name, "本地缺失")); continue
        ext = os.path.splitext(name)[1].lower()
        data = open(src, "rb").read()
        r = mw.upload(name, data, CT.get(ext, "application/octet-stream"))
        if "upload" in r and r["upload"].get("result") == "Success":
            up_ok += 1
        elif "error" in r and r["error"].get("code") == "fileexists-no-change":
            up_skip += 1
        else:
            up_fail += 1
            fails.append((name, json.dumps(r, ensure_ascii=False)[:120]))
        time.sleep(0.1)
    print("图片上传: 成功 %d / 已存在 %d / 失败 %d" % (up_ok, up_skip, up_fail))
    for n, e in fails[:10]: print("   ", n, e)
    # 2) 写入页面
    print("\n开始写入页面...")
    ok = fail = 0
    resultados = []
    for fn in sorted(os.listdir(PAGES)):
        if not fn.endswith(".wiki"):
            continue
        title = fn[:-5]
        text = open(os.path.join(PAGES, fn), encoding="utf-8").read()
        r = mw.edit(title, text)
        if "edit" in r and r["edit"].get("result") == "Success":
            ok += 1
        else:
            fail += 1
            resultados.append((title, json.dumps(r, ensure_ascii=False)[:150]))
            print("FAIL", title, resultados[-1][1])
        time.sleep(0.15)
    print("\n页面写入: 成功 %d / 失败 %d" % (ok, fail))

if __name__ == "__main__":
    main()
