#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""导入 book2wiki 生成的两本书页面到 MediaWiki。幂等可重跑。

复用 D2L 导入器的 MW 类（cookie 管理 + API）。图片按需上传（图书插图，命名 bookimg__*）。
"""
import os, re, json, sys, time, hashlib
import urllib.parse, urllib.request

API = "http://localhost:8080/api.php"
UA = "OGWiki-book2wiki/1.0"
ROOT = r"C:\Users\Lenovo\Desktop\OGv01\Wiki\.build\book2wiki"
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
        with urllib.request.urlopen(req, timeout=180) as r:
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

    def edit(self, title, text, summary):
        return self.call({"action": "edit", "title": title, "text": text,
                          "summary": summary, "token": self.csrf,
                          "bot": "1", "contentmodel": "wikitext", "recreate": "1"})

    def upload(self, name, data, ct):
        return self.call({"action": "upload", "filename": name, "token": self.csrf,
                          "ignorewarnings": "1", "comment": "图书插图"},
                         files={"file": (name, data, ct)})

    def delete(self, title, reason="清理规范化标题前的旧页面"):
        return self.call({"action": "delete", "title": title, "token": self.csrf,
                          "reason": reason})


CT = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def import_dir(mw, pages_dir, summary):
    if not os.path.isdir(pages_dir):
        print("目录不存在:", pages_dir)
        return 0, 0
    files = sorted(f for f in os.listdir(pages_dir) if f.endswith(".wiki"))
    ok = fail = 0
    for fn in files:
        title = fn[:-5]
        text = open(os.path.join(pages_dir, fn), encoding="utf-8").read()
        r = mw.edit(title, text, summary)
        if "edit" in r and r["edit"].get("result") == "Success":
            ok += 1
        else:
            fail += 1
            print("FAIL", title, json.dumps(r, ensure_ascii=False)[:160])
        time.sleep(0.12)
    return ok, fail


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    mw = MW(); mw.login()
    if which in ("ostep", "both"):
        ok, fail = import_dir(mw, os.path.join(ROOT, "ostep", "pages"),
                              "导入《操作系统导论》知识条目")
        print("OSTEP 页面: 成功 %d / 失败 %d" % (ok, fail))
    if which in ("gt", "both"):
        ok, fail = import_dir(mw, os.path.join(ROOT, "graphtheory", "pages"),
                              "导入《图论》知识条目")
        print("图论 页面: 成功 %d / 失败 %d" % (ok, fail))


if __name__ == "__main__":
    main()
