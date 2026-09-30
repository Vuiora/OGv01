import os, re, sys

PAGE_DIR = os.path.join(os.path.dirname(__file__), "pages")
problems = []

for fn in sorted(os.listdir(PAGE_DIR)):
    if not fn.endswith(".wiki"):
        continue
    path = os.path.join(PAGE_DIR, fn)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    issues = []

    # 1. balanced double braces (templates)
    nopen = text.count("{{")
    nclose = text.count("}}")
    if nopen != nclose:
        issues.append(f"模板花括号不配对 {{={{nopen}} }}={{nclose}}")

    # 2. balanced <math> tags
    mo = len(re.findall(r"<math>", text))
    mc = len(re.findall(r"</math>", text))
    if mo != mc:
        issues.append(f"<math> 标签不配对 open={mo} close={mc}")

    # 3. table syntax: strip <includeonly>/<noinclude> markers, then {| vs |}
    tbl = re.sub(r"</?includeonly>|</?noinclude>", "\n", text)
    so = len(re.findall(r"^\{\|", tbl, re.M))
    sc = len(re.findall(r"^\|\}", tbl, re.M))
    if so != sc:
        issues.append(f"表格不配对 {{|={so} |}}={sc}")

    # 4. heading format: no '=' at level 1; opening/closing runs must be equal
    for i, line in enumerate(text.splitlines(), 1):
        if not re.match(r"^={2,6}", line):
            if re.match(r"^=[^=]", line):
                issues.append(f"L{i}: 使用了 1 级标题")
            continue
        lead = len(line) - len(line.lstrip("="))
        trail = len(line) - len(line.rstrip("="))
        body = line[lead:len(line) - trail] if trail else line[lead:]
        if trail == 0:
            issues.append(f"L{i}: 标题行未闭合 -> {line[:40]!r}")
        elif lead != trail:
            issues.append(f"L{i}: 标题等号不匹配 open={lead} close={trail} -> {line[:40]!r}")
        elif body.rstrip().endswith("="):
            issues.append(f"L{i}: 标题闭合符前有多余等号 -> {line[:40]!r}")

    # 5. category link placement
    for i, line in enumerate(text.splitlines(), 1):
        if "[[Category:" in line and not line.strip().startswith("[[Category:"):
            issues.append(f"L{i}: 分类链接未单独成行")

    if issues:
        problems.append((fn, issues))

if not problems:
    print("全部 %d 个文件语法检查通过" % len([f for f in os.listdir(PAGE_DIR) if f.endswith('.wiki')]))
else:
    for fn, iss in problems:
        print(f"[{fn}]")
        for x in iss:
            print("   -", x)
