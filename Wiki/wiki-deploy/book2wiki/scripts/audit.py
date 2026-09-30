import re, glob, sys

SUS = re.compile("[=<>|:=\\u2208\\u2286\\u2211\\u220f\\u2265\\u2264\\u2192\\u2190\\u00d7\\u00b7\\\\]")


def audit(pat, label):
    files = sorted(glob.glob(pat))
    tot = code = math = heads = 0
    sus = []
    for f in files:
        s = open(f, encoding="utf-8").read()
        tot += len(s)
        code += len(re.findall(r"<syntaxhighlight", s))
        math += len(re.findall(r"<math>", s))
        for m in re.finditer(r"^==+ (.+?) ==+$", s, re.M):
            heads += 1
            t = m.group(1)
            if SUS.search(t) or len(t) > 40 or t.strip() in ("0", "1", "EV", "VE", "pk"):
                sus.append((f, t))
    print("%-6s files=%d chars=%d code=%d math=%d headings=%d  可疑标题=%d"
          % (label, len(files), tot, code, math, heads, len(sus)))
    for f, t in sus[:20]:
        print("     !", t[:60], "  <-", f.split("/")[-1])


if __name__ == "__main__":
    audit("ostep/pages/*.wiki", "OSTEP")
    audit("graphtheory/pages/*.wiki", "GT")
