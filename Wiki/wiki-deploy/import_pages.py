import os, re, json, sys, time
import requests

API = "http://localhost:8080/api.php"
PW = open(os.path.join(os.path.dirname(__file__), "botpw.txt")).read().strip()
PAGE_DIR = os.path.join(os.path.dirname(__file__), "pages")

S = requests.Session()
S.headers.update({"User-Agent": "OGWiki-Importer/1.0 (local; knowledge import)"})


def get_token(kind="csrf"):
    r = S.get(API, params={"action": "query", "meta": "tokens", "type": kind, "format": "json"}, timeout=30)
    return r.json()["query"]["tokens"][kind + "token"]


def login():
    tok = get_token("login")
    r = S.post(API, data={
        "action": "login", "format": "json",
        "lgname": "Admin@ogwikiimport", "lgpassword": PW, "lgtoken": tok,
    }, timeout=30)
    res = r.json()["login"]["result"]
    print("login:", res)
    assert res == "Success", r.text
    return get_token("csrf")


def edit(title, text, summary, csrf, bot=True):
    r = S.post(API, data={
        "action": "edit", "format": "json",
        "title": title, "text": text, "summary": summary,
        "token": csrf, "bot": "1" if bot else "0",
        "contentmodel": "wikitext",
        "recreate": "1",
    }, timeout=60)
    return r.json()


# filename -> wiki page title
TITLE_MAP = {
    "首页": "首页",
    "Template_课程信息": "Template:课程信息",
    "Template_Quote": "Template:Quote",
}


def to_title(stem):
    if stem in TITLE_MAP:
        return TITLE_MAP[stem]
    return stem


def main():
    csrf = login()
    files = sorted(os.listdir(PAGE_DIR))
    results = []
    for fn in files:
        if not fn.endswith(".wiki"):
            continue
        stem = fn[:-5]
        title = to_title(stem)
        with open(os.path.join(PAGE_DIR, fn), encoding="utf-8") as fh:
            text = fh.read()
        res = edit(title, text, "从课程讲义导入知识条目", csrf)
        if "edit" in res and res["edit"].get("result") == "Success":
            print(f"OK   {title}  (rev {res['edit'].get('newrevid')})")
            results.append((title, "ok", res['edit'].get('newrevid')))
        else:
            print(f"FAIL {title}  -> {json.dumps(res, ensure_ascii=False)[:300]}")
            results.append((title, "fail", json.dumps(res, ensure_ascii=False)[:200]))
        time.sleep(0.3)

    print("\n=== 汇总 ===")
    ok = sum(1 for _, s, _ in results if s == "ok")
    print(f"成功 {ok} / {len(results)}")
    with open(os.path.join(os.path.dirname(__file__), "import_result.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
