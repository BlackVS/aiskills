#!/usr/bin/env python3
"""Read the review gates of a pull (or merge) request from the forge: wait for the external
reviewer's answer for a head, say whether the current head is ready for a person to merge,
or give verify-delivery's delivery verdict.

Read-only: every request is an HTTP GET. It never labels, comments, pushes or merges.
Standard library only; it imports verify_delivery.py from the sibling verify-delivery skill.

    forge_gates.py wait-review --pr https://github.com/OWNER/REPO/pull/7 --head <40-digit SHA>
    forge_gates.py gates --pr https://github.com/OWNER/REPO/pull/7
    forge_gates.py merge-check --pr https://github.com/OWNER/REPO/pull/7

Prints one JSON document on stdout and one summary line on stderr. Exit codes: gates 0 ready,
3 not ready, 4 pending; wait-review 0 review found, 3 the wait ended otherwise, 4 timeout;
merge-check verify-delivery's; 2 usage error. See SKILL.md.
"""
import datetime, json, os, pathlib, re, subprocess, sys, time, urllib.parse

sys.dont_write_bytecode = True  # no __pycache__ inside an installed skill
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent / "verify-delivery"))
try:
    import verify_delivery as vd
except ImportError:
    vd = None

EXIT_READY, EXIT_USAGE, EXIT_NOT_READY, EXIT_PENDING = 0, 2, 3, 4
COMMANDS = ("wait-review", "gates", "merge-check")
# gh lets these override its stored logins whatever --hostname names: a token for instance A
# would be returned for B. They are removed from the environment gh is run with.
GH_OVERRIDES = {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST"}
GH_TIMEOUT = 15
GITEA_DRAFT_PREFIXES = ("WIP:", "[WIP]")  # Gitea's default WORK_IN_PROGRESS_PREFIXES
MIN_INTERVAL = 30
SHA_TOKEN = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{7,40}(?![0-9A-Za-z])")
VERDICT_WORD = re.compile(r"(?m)^[ \t]*VERDICT[ \t]*\r?\n[ \t]*([A-Z_]+)")


# ---------------------------------------------------------------- credentials

def resolve(args, env, run):
    """verify-delivery's resolve, plus the GitHub CLI as the last source of a GitHub token."""
    kind, api_base, _, _, _ = vd.parse_ref(args.pr, args.number, args.forge, args.api_base)
    var = {"github": "GITHUB_TOKEN_FILE", "gitlab": "GITLAB_TOKEN_FILE"}.get(kind, "GITEA_TOKEN_FILE")
    if args.anonymous or args.token_env or env.get(var) or kind != "github":
        return vd.resolve(args, env)
    args.anonymous = True  # resolve everything but the token
    try:
        kind, api_base, owner, repo, number, _ = vd.resolve(args, env)
    finally:
        args.anonymous = False
    if not api_base.startswith("https://"):
        raise vd.UsageError("a token is only sent over https: use an https --api-base (or --anonymous)")
    return kind, api_base, owner, repo, number, gh_token(cli_host(api_base, args.pr), env, run)


def cli_host(api_base, pr):
    """The GitHub host a CLI token is asked for: the one the API base belongs to, which a pull
    request URL must name as well."""
    parts = urllib.parse.urlsplit(api_base)
    host, path = parts.netloc.lower(), parts.path.rstrip("/")
    if host == "api.github.com" and not path:
        host = "github.com"
    elif path != "/api/v3":
        raise vd.UsageError("the GitHub CLI is asked for a token only for https://api.github.com or https://HOST/api/v3: "
                            "set GITHUB_TOKEN_FILE, name a token file with --token-env, or pass --anonymous")
    m = re.match(r"^https?://([^/?#]+)/", pr or "")
    if m and m.group(1).lower() != host:
        raise vd.UsageError("the pull request URL and the API base name different hosts, so no automatic token is "
                            "read: give a matching --api-base, or a token file with --token-env")
    return host


def gh_token(host, env, run):
    """The token the GitHub CLI stores for HOST, from one `gh auth token --hostname HOST` with no
    shell, a timeout and the global token overrides removed. Kept in memory only."""
    other = "set GITHUB_TOKEN_FILE, name a token file with --token-env, or pass --anonymous"
    clean = {k: v for k, v in env.items() if k.upper() not in GH_OVERRIDES}
    try:
        r = run(["gh", "auth", "token", "--hostname", host], env=clean, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=GH_TIMEOUT)
    except FileNotFoundError:
        raise vd.UsageError(f"no token: the GitHub CLI (gh) is not installed; {other}")
    except subprocess.TimeoutExpired:
        raise vd.UsageError(f"no token: the GitHub CLI did not answer within {GH_TIMEOUT} seconds; {other}")
    except OSError:
        raise vd.UsageError(f"no token: the GitHub CLI could not be run; {other}")
    token = (r.stdout or "").strip()
    if r.returncode != 0 or not token or any(c.isspace() for c in token):
        raise vd.UsageError(f"no token: the GitHub CLI has no login for {host} (gh auth login --hostname {host}); {other}")
    return token


# ---------------------------------------------------------------- the pull request before the merge

def read_pull(forge, kind, number):
    """The pull request itself, not verify-delivery's normalised record: {state (open, closed or
    merged), draft (None on Gitea without the field), head, base, title, url, raw}."""
    try:
        if kind == "gitlab":
            pr = forge.mr = vd.Forge.get(forge, f"/merge_requests/{number}")
            state = {"opened": "open", "locked": "open"}.get(pr.get("state"), pr.get("state"))
            return {"state": state, "draft": bool(pr.get("draft") or pr.get("work_in_progress")),
                    "head": (pr.get("sha") or "").lower(), "base": pr.get("target_branch"),
                    "title": pr.get("title") or "", "url": pr.get("web_url") or "", "raw": pr}
        pr = forge.get(f"/pulls/{number}")
    except vd.NotFound:
        raise vd.UsageError(f"pull request {number} was not found (or the token cannot see it)")
    return {"state": "merged" if pr.get("merged") else pr.get("state"),
            "draft": bool(pr["draft"]) if "draft" in pr else None,
            "head": ((pr.get("head") or {}).get("sha") or "").lower(), "base": (pr.get("base") or {}).get("ref"),
            "title": pr.get("title") or "", "url": pr.get("html_url") or "", "raw": pr}


def draft_state(kind, pr, prefixes):
    """(draft, basis)."""
    if pr["draft"] is not None:
        return pr["draft"], "the forge's draft flag"
    title = pr["title"].upper()
    hit = next((p for p in prefixes if title.startswith(p.upper())), None)
    if hit:
        return True, f"the title starts with {hit}"
    return False, "the title has none of the work-in-progress prefixes " + ", ".join(prefixes)


def base_tip(forge, kind, branch):
    """The base branch's current tip SHA."""
    if not branch:
        raise ValueError("the forge does not name the base branch")
    if kind == "gitlab":
        path = "/repository/branches/" + urllib.parse.quote(branch, safe="")
    else:
        path = "/branches/" + urllib.parse.quote(branch, safe="/")
    commit = vd.Forge.get(forge, path).get("commit") or {}
    sha = (commit.get("sha") or commit.get("id") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"the forge does not give the tip of {branch}")
    return sha


def change_identity(forge, kind, tip, sha):
    """verify-delivery's patch_identity of SHA's change against its merge base with TIP."""
    path = f"/compare/{tip}...{sha}" + ("?output=diff" if kind == "gitea" else "")
    try:
        text = forge.diff(path)
    except vd.NotFound:
        raise ValueError("the forge has no compare diff for it" + (" (Gitea before 1.22)" if kind == "gitea" else ""))
    if kind == "gitea" and text.lstrip().startswith(b"{"):
        raise ValueError("this Gitea returns no diff from its compare API (?output=diff needs Gitea 1.27 or later)")
    return vd.patch_identity(text)[0]


def review_gate(args, forge, kind, number, pr):
    """The reviews check: verify-delivery's reading of the reviews at the current head, with a
    READY review of an older head carried over only when its change is the current one's."""
    head = pr["head"]
    items = vd.comment_items(forge, number)
    _, short_at_head = vd.head_abbreviations(args, forge, number, head)
    _, reviews, ignored = vd.check_reviews(items, head, None, args.reviews, args.verdict, kind,
                                           args.review_authors, args.trust_any_author, short_at_head)
    identities, tips = {}, []

    def identity(sha):
        if sha not in identities:
            try:
                if not tips:
                    tips.append(base_tip(forge, kind, pr["base"]))
                identities[sha] = (change_identity(forge, kind, tips[0], sha), None)
            except (vd.Unavailable, ValueError) as e:
                identities[sha] = (None, str(e))
        return identities[sha]

    for r in reviews:
        if r["status"] != "ready" or r["at_final_head"]:
            continue
        sha = r["sha"]
        if len(sha) < 40:
            r.update(status="not_ready", detail=f"names an older head by the abbreviated SHA {sha}, which cannot be "
                                                "expanded safely: re-review at the current head, naming its full SHA")
            continue
        (reviewed, why), (current, why_current) = identity(sha), identity(head)
        if why or why_current:
            r.update(status="unknown", detail=f"names the older head {sha}, and the change identity could not be read "
                                              f"({why or why_current}): retry, or re-review at the current head")
            continue
        r["change_identity"] = {"reviewed": reviewed, "current": current}
        if reviewed == current:
            r.update(carried_from=sha, detail="the review of an older head carries over: its change is the current head's")
        else:
            r.update(status="stale", detail=f"names the older head {sha}, whose change is not the current head's: "
                                            "re-review at the current head")
    ready = sum(r["status"] == "ready" for r in reviews)
    waiting = sum(r["status"] in ("pending", "unknown") for r in reviews)
    status = ("passed" if ready >= args.required_reviews else "pending" if ready + waiting >= args.required_reviews
              else "failed")
    gate = {"name": "reviews", "status": status, "required": args.required_reviews, "ready": ready,
            "reviews": reviews, "ignored": ignored}
    if tips:
        gate["base_tip"] = tips[0]
    return gate


def ci_gate(args, forge, kind, pr):
    """CI at the head: GitHub's check runs and commit statuses, Gitea's statuses, GitLab's
    merge request pipeline when it ran on the head."""
    head, detail, runs = pr["head"], None, []
    if kind == "gitlab":
        p = pr["raw"].get("head_pipeline")
        if not p:
            detail = "the merge request has no pipeline yet"
        elif (p.get("sha") or "").lower() != head:
            detail = (f"the merge request's pipeline ran on {(p.get('sha') or '')[:12]}, not on the head (a merged-results "
                      "pipeline runs on a commit that combines the branches): no CI result for the head itself")
        else:
            state = p.get("status")
            runs.append({"name": p.get("name") or "pipeline", "id": p.get("id"), "url": p.get("web_url") or "",
                         "state": state, "conclusion": state,
                         "result": "passed" if state in vd.GITLAB_PASS else "pending" if state in vd.GITLAB_PENDING
                         else "failed"})
    else:
        runs = vd.ci_results(kind, head, forge, set())
        if kind == "github":
            for s in forge.get(f"/commits/{head}/status?per_page=100").get("statuses") or []:
                state = s.get("state")
                runs.append({"name": s.get("context"), "id": s.get("id"), "url": s.get("target_url") or "",
                             "state": state, "conclusion": state,
                             "result": "passed" if state == "success" else "pending" if state == "pending" else "failed"})
    flaky = set(args.known_flaky)
    for r in runs:
        if r["result"] == "failed" and r["name"] in flaky:
            r["result"] = "failed: known flaky"
    results = {r["result"] for r in runs}
    gate = {"name": "ci", "head_sha": head, "runs": runs}
    if any(r.startswith("failed") for r in results):
        gate["status"] = "failed"
    elif not runs:
        gate.update(status="pending", detail=detail or "no CI results at the head yet")
    elif "pending" in results:
        gate.update(status="pending", detail="CI is still running at the head")
    else:
        gate["status"] = "passed"
    return gate


def gates(args, forge, kind, owner, repo, number):
    pr = read_pull(forge, kind, number)
    state = {"name": "state", "state": pr["state"], "status": "passed" if pr["state"] == "open" else "failed"}
    if pr["state"] != "open":
        state["detail"] = "already merged" if pr["state"] == "merged" else "closed without merging"
    draft, basis = draft_state(kind, pr, args.draft_prefix or GITEA_DRAFT_PREFIXES)
    checks = [state, {"name": "draft", "draft": draft, "basis": basis, "status": "failed" if draft else "passed"},
              review_gate(args, forge, kind, number, pr), ci_gate(args, forge, kind, pr)]
    statuses = {c["status"] for c in checks}
    status = "not_ready" if "failed" in statuses else "pending" if "pending" in statuses else "ready"
    doc = {"status": status, "ready": status == "ready",
           "pr": {"forge": kind, "repo": f"{owner}/{repo}", "number": number, "url": pr["url"], "head_sha": pr["head"],
                  "state": pr["state"], "draft": draft, "base_branch": pr["base"]},
           "checks": checks}
    return doc, {"ready": EXIT_READY, "pending": EXIT_PENDING}.get(status, EXIT_NOT_READY)


# ---------------------------------------------------------------- waiting for the external review

def label_added(forge, kind, number, label):
    """When the request label (LABEL, or a LABEL:<profile> variant) was last added, or None when
    the label events cannot be read or show no such addition."""
    def matches(item):
        name = (item or {}).get("name") or ""
        return name == label or name.startswith(label + ":")
    try:
        if kind == "github":
            times = [e.get("created_at") for e in forge.pages(f"/issues/{number}/events")
                     if e.get("event") == "labeled" and matches(e.get("label"))]
        elif kind == "gitea":
            # a label comment's body is "1" when the label was added
            times = [e.get("created_at") for e in forge.pages(f"/issues/{number}/timeline")
                     if e.get("type") == "label" and (e.get("body") or "").strip() == "1" and matches(e.get("label"))]
        else:
            times = [e.get("created_at") for e in forge.pages(f"/merge_requests/{number}/resource_label_events")
                     if e.get("action") == "add" and matches(e.get("label"))]
    except (vd.NotFound, vd.Unavailable, vd.UsageError):
        return None
    stamps = [t for t in map(vd.when, times) if t]
    return max(stamps) if stamps else None


def names_head(body, head):
    """Whether a failure comment can be about HEAD: it names no SHA, or one that HEAD starts with.
    A run of 7 or more digits only is a SHA only at full length (it is more often a number)."""
    shas = [t.lower() for t in SHA_TOKEN.findall(body or "") if len(t) == 40 or re.search(r"[a-fA-F]", t)]
    return not shas or any(head.startswith(s) for s in shas)


def poll(args, forge, kind, number, head, since):
    """One look at the pull request: (outcome, details), or None to keep waiting."""
    pr = read_pull(forge, kind, number)
    name, pattern = args.wait_pattern
    items = sorted(vd.comment_items(forge, number, formal=False),
                   key=lambda c: vd.when(c["created_at"]) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc))
    trusted = [c for c in items if not vd.author_verdict(c, name, kind, args.review_authors, args.trust_any_author)]
    found = None
    for c in trusted:
        m = pattern.search(c["body"] or "")
        sha = (m.group("sha") or "").lower() if m else ""
        if re.fullmatch(r"[0-9a-f]{7,40}", sha) and head.startswith(sha):
            found = (c, sha)
    if found:
        c, sha = found
        word = VERDICT_WORD.search(c["body"] or "")
        return "review_found", {"review": {"review": name, "sha": sha, "abbreviated": len(sha) < 40,
                                           "verdict": word.group(1) if word else None,
                                           "ready_verdict": bool(args.verdict.search(c["body"] or "")),
                                           "url": c["url"], "author": c["author"], "created_at": c["created_at"]}}
    label = label_added(forge, kind, number, args.request_label)
    correlation = {"correlation": "label" if label else "wait_start", "since": (label or since).isoformat()}
    for c in reversed(trusted):
        stamp = vd.when(c["created_at"])
        if (args.failure.search(c["body"] or "") and names_head(c["body"], head) and stamp
                and stamp > (label or since)):
            return "reviewer_failed", {"failure": {"url": c["url"], "author": c["author"], "created_at": c["created_at"]},
                                       **correlation}
    if pr["head"] != head:
        return "head_moved", {"current_head": pr["head"]}
    if pr["state"] in ("merged", "closed"):
        return pr["state"], {}
    return None, correlation


def wait_review(args, forge, kind, owner, repo, number, clock, sleep):
    head, start = args.head, clock()
    since = datetime.datetime.fromtimestamp(start, datetime.timezone.utc)
    doc = {"outcome": None, "pr": {"forge": kind, "repo": f"{owner}/{repo}", "number": number}, "head": head,
           "wait_for": args.wait_for}
    polls, last_error, details = 0, None, {}
    while True:
        polls += 1
        try:
            outcome, details = poll(args, forge, kind, number, head, since)
        except vd.UsageError:
            if polls == 1:
                raise
            outcome, last_error = None, "the pull request could no longer be read (it may have been deleted)"
        except vd.Unavailable as e:
            outcome, last_error = None, str(e)
        elapsed = clock() - start
        if outcome or elapsed >= args.timeout:
            break
        sleep(max(0, min(args.interval, args.timeout - elapsed)))
    doc.update(outcome=outcome or "timeout", polls=polls, waited_seconds=int(elapsed), **(details or {}))
    if not outcome and last_error:
        doc["last_error"] = last_error
    return doc, {"review_found": EXIT_READY, None: EXIT_PENDING}.get(outcome, EXIT_NOT_READY)


def check_wait_args(args, kind):
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.head or ""):
        raise vd.UsageError("--head must be the full 40-digit SHA of the head that was pushed")
    args.head = args.head.lower()
    if args.interval < MIN_INTERVAL:
        raise vd.UsageError(f"--interval must be at least {MIN_INTERVAL} seconds")
    if args.timeout < 0:
        raise vd.UsageError("--timeout must not be negative")
    patterns = dict(args.reviews)
    if args.wait_for not in patterns:
        raise vd.UsageError("--wait-for names a review that is not configured (see --review)")
    if (kind != "github" and not args.trust_any_author and args.wait_for not in args.review_authors):
        raise vd.UsageError(f"an author allowlist is required on {'GitLab' if kind == 'gitlab' else 'Gitea'}: give "
                            f"--review-author {args.wait_for}=LOGIN (or --trust-any-author where only trusted accounts can comment)")
    args.wait_pattern = (args.wait_for, patterns[args.wait_for])
    try:
        args.failure = re.compile(args.failure_pattern)
    except re.error:
        raise vd.UsageError("--failure-pattern is not a valid regular expression")


# ---------------------------------------------------------------- CLI

def build_parser():
    p = vd.ArgParser(prog="forge_gates.py", description=__doc__.split("\n\n")[0], allow_abbrev=False)
    sub = p.add_subparsers(dest="command", required=True, metavar="{" + ",".join(COMMANDS) + "}")
    w = vd.add_arguments(sub.add_parser("wait-review", allow_abbrev=False,
                                        help="wait for the external reviewer's answer for a head"))
    w.add_argument("--head", required=True, help="the full 40-digit SHA of the head that was pushed")
    w.add_argument("--wait-for", default="external", metavar="NAME", help="the configured review to wait for (default external)")
    w.add_argument("--timeout", type=int, default=2700, help="seconds to wait (default 2700)")
    w.add_argument("--interval", type=int, default=60, help=f"seconds between polls (default 60, at least {MIN_INTERVAL})")
    w.add_argument("--failure-pattern", default="could not run", help="regex of the reviewer's failure comment")
    w.add_argument("--request-label", default="review-this",
                   help="the label that requests the review; LABEL:<profile> variants count too (default review-this)")
    g = vd.add_arguments(sub.add_parser("gates", allow_abbrev=False,
                                        help="whether the current head is ready for a person to merge"))
    g.add_argument("--draft-prefix", action="append", default=[], metavar="PREFIX",
                   help="a Gitea work-in-progress title prefix; replaces WIP: and [WIP], repeatable")
    vd.add_arguments(sub.add_parser("merge-check", allow_abbrev=False, help="verify-delivery's delivery verdict"))
    return p


def error_doc(command, e):
    if command == "merge-check":
        return vd.failure(e)
    kind = ("usage" if isinstance(e, vd.UsageError) else "unavailable" if isinstance(e, vd.Unavailable)
            else "not_found")
    code = {"usage": EXIT_USAGE, "unavailable": EXIT_PENDING}.get(kind, EXIT_NOT_READY)
    error = {"kind": kind, "message": str(e) if kind != "not_found" else "a record the pull request refers to was not found"}
    if command == "wait-review":
        return {"outcome": "error", "error": error}, code
    return {"status": "pending" if code == EXIT_PENDING else "not_ready", "ready": False, "error": error}, code


def summary(command, doc):
    if "error" in doc and command != "merge-check":
        return f"{command}: {doc['error']['kind']} error: {doc['error']['message']}"
    if command == "merge-check":
        return f"merge-check: {doc['verdict']}" + (f": {doc['error']['message']}" if "error" in doc else "")
    if command == "wait-review":
        line = f"wait-review: {doc['outcome']} for {doc['head'][:12]} after {doc['polls']} poll(s)"
        review = doc.get("review")
        return line + (f": {review['verdict']} at {review['url']}" if review else
                       f": {doc['last_error']}" if doc.get("last_error") else "")
    reasons = []
    for c in doc["checks"]:
        if c["name"] == "reviews":
            reasons += [f"review {r['review']} {r['status']}" for r in c["reviews"] if r["status"] != "ready"]
        elif c["status"] != "passed":
            reasons.append(f"{c['name']} {c['status']}" + (f" ({c['detail']})" if c.get("detail") else ""))
    return f"gates: {doc['status']} at {doc['pr']['head_sha'][:12]}" + (": " + "; ".join(reasons) if reasons else "")


def main(argv=None, env=None, stdout=None, stderr=None, opener=None, sleep=time.sleep, clock=time.time, run=subprocess.run):
    env = os.environ if env is None else env
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv and argv[0] in COMMANDS else None
    if vd is None:
        message = ("forge-gates needs the verify-delivery skill installed next to it (skills/verify-delivery): "
                   "install both, for example install.sh -s forge-gates")
        stdout.write(json.dumps({"error": {"kind": "usage", "message": message}}, indent=2) + "\n")
        stderr.write(f"forge-gates: {message}\n")
        return EXIT_USAGE
    token = None
    try:
        try:
            args = build_parser().parse_args(argv)
        except SystemExit as e:  # --help
            return EXIT_USAGE if e.code else 0
        kind, api_base, owner, repo, number, token = resolve(args, env, run)
        if command == "wait-review":
            check_wait_args(args, kind)
        forge = vd.make_forge(token, kind, api_base, owner, repo, opener, sleep)
        if command == "merge-check":
            doc, code = vd.deliver(args, forge, kind, owner, repo, number)
        elif command == "gates":
            doc, code = gates(args, forge, kind, owner, repo, number)
        else:
            doc, code = wait_review(args, forge, kind, owner, repo, number, clock, sleep)
    except (vd.UsageError, vd.Unavailable, vd.NotFound) as e:
        doc, code = error_doc(command, e)
    vd.emit(doc, stdout, token)
    line = summary(command, doc) if command else f"forge-gates: {doc['error']['message']}"
    if token and len(token) >= 8:
        line = line.replace(token, "***")
    stderr.write(line + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
