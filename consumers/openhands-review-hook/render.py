#!/usr/bin/env python3
"""Render the universal hook prompt for one site.

    python3 render.py sites/<site>.json > review_prompt.txt

Site variables use the @@NAME@@ syntax and are filled here, at install time;
an empty value drops the line that carries the marker (optional blocks);
runtime placeholders ({num} {repo} {title!r} {marker} {model} {label}) are left
for the receiver's PROMPT.format(). Multi-line site values are re-indented to
the template's step indentation. With --check, renders every site and validates
instead (exit 1 on any failure)."""
import json, os, re, sys
here = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(here, "review_prompt.template.txt")
RUNTIME = dict(num=1, repo="o/r", title="t", marker="[bot review]", model="m", label="review-this")
VARS = ("FORGE_URL", "HANDS_URL", "SKILL_SOURCE", "CLONE_AND_FETCH", "POST_COMMAND", "REASONING_PROFILES")

def render(site_path):
    site = json.load(open(site_path)); tpl = open(TEMPLATE).read(); out = []
    for line in tpl.splitlines():
        m = re.search(r"@@([A-Z_]+)@@", line)
        if not m:
            out.append(line); continue
        key = m.group(1)
        if key not in site: raise SystemExit("%s: missing site variable %s" % (site_path, key))
        if site[key] == "": continue  # optional block: an empty value drops the whole line
        prefix = line[:m.start()]; indent = re.match(r"\s*", prefix).group(0) if prefix.strip() == "" else "   "
        # a value replaces the marker in place; continuation lines get the step indentation
        parts = site[key].split("\n")
        first = line[:m.start()] + parts[0] + line[m.end():]
        out.append(first); out.extend(indent + p for p in parts[1:])
    return "\n".join(out) + "\n"

def check():
    ok = True
    for f in sorted(os.listdir(os.path.join(here, "sites"))):
        if not f.endswith(".json"): continue
        path = os.path.join(here, "sites", f); problems = []
        try:
            text = render(path); text.format(**RUNTIME)
            if "@@" in text: problems.append("unrendered @@variable@@")
            if "{marker} reviewed at head" not in text: problems.append("marker line contract missing")
            for w in ("KEY INSIGHT", "taste", "Worth merging", "Needs rework"): 
                if w in text: problems.append("stale wording: " + w)
            site = json.load(open(path)); extra = set(site) - set(VARS); missing = set(VARS) - set(site)
            if extra or missing: problems.append("site vars extra=%s missing=%s" % (sorted(extra), sorted(missing)))
        except Exception as e:
            problems.append(repr(e))
        print(("OK   " if not problems else "FAIL ") + f + ("" if not problems else ": " + "; ".join(problems))); ok &= not problems
    return ok

if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]: sys.exit(0 if check() else 1)
    if len(sys.argv) != 2: raise SystemExit(__doc__)
    sys.stdout.write(render(sys.argv[1]))
