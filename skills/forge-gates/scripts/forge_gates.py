#!/usr/bin/env python3
"""Read the review gates of a pull (or merge) request from the forge: wait for the external
reviewer's answer for a head, say whether the current head is ready for a person to merge,
or give verify-delivery's delivery verdict; post the local review once it has the form
verify-delivery reads.

Read-only except post-review: every other request is an HTTP GET. post-review creates one
comment and never edits one; nothing here labels, pushes or merges.
Standard library only; it imports verify_delivery.py from the sibling verify-delivery skill.

    forge_gates.py wait-review --pr https://github.com/OWNER/REPO/pull/7 --head <40-digit SHA>
    forge_gates.py gates --pr https://github.com/OWNER/REPO/pull/7
    forge_gates.py merge-check --pr https://github.com/OWNER/REPO/pull/7
    forge_gates.py post-review --pr https://github.com/OWNER/REPO/pull/7 --body-file review.md [--dry-run]

Prints one JSON document on stdout and one summary line on stderr. Exit codes: gates 0 ready,
3 not ready, 4 pending; wait-review 0 review found, 3 the wait ended otherwise, 4 timeout;
merge-check verify-delivery's; post-review 0 posted (or, with --dry-run, would post), 3
refused, 4 not posted for a forge error; 2 usage error. See SKILL.md.
"""
import datetime, json, os, pathlib, re, socket, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

sys.dont_write_bytecode = True  # no __pycache__ inside an installed skill
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent / "verify-delivery"))
try:
    import verify_delivery as vd
except ImportError:
    vd = None

EXIT_READY, EXIT_USAGE, EXIT_NOT_READY, EXIT_PENDING = 0, 2, 3, 4
COMMANDS = ("wait-review", "gates", "merge-check", "post-review")
# gh lets these override its stored logins whatever --hostname names: a token for instance A
# would be returned for B. They are removed from the environment gh is run with.
GH_OVERRIDES = {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST"}
GH_TIMEOUT = 15
GITEA_DRAFT_PREFIXES = ("WIP:", "[WIP]")  # Gitea's default WORK_IN_PROGRESS_PREFIXES
MIN_INTERVAL = 30
SHA_TOKEN = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{7,40}(?![0-9A-Za-z])")
# A UUID in its 8-4-4-4-12 form: its first group alone looks like an abbreviated SHA.
UUID = re.compile(r"(?<![0-9A-Za-z-])[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}(?![0-9A-Za-z-])")
VERDICT_WORD = re.compile(r"(?m)^[ \t]*VERDICT[ \t]*\r?\n(?:[ \t]*\r?\n)*[ \t]*([A-Z_]+)")
VERDICTS = ("READY_FOR_HUMAN_MERGE", "RETURN_TO_IMPLEMENTATION", "REVIEW_COULD_NOT_RUN")
VERDICT_LINE = re.compile(r"[ \t]*VERDICT[ \t]*")
# A line that tries to be the verdict in another layout: "VERDICT: X", "**VERDICT:** X", "## VERDICT".
VERDICT_LOOKALIKE = re.compile(r"[ \t>#*_`-]*VERDICT\b")
# The level a header names; "[oh-code-review review]" names none.
LEVEL = re.compile(r"\[oh-code-review(?: \((?P<a>low|medium|high|max|ultra)\) review| (?P<b>low|medium|high|max|ultra))\]")
ATTRIBUTION = re.compile(r"(?m)^Review: oh-code-review \((?P<level>low|medium|high|max|ultra)\) via \S.*$")
# Links into an agent's private workspace: dead for every other reader of a pull request.
SESSION_URL = re.compile(r"https?://(?:[a-z0-9-]+\.)*(?:claude\.ai|chatgpt\.com|chat\.openai\.com)/\S+", re.I)
MAX_BODY = 65536  # characters; GitHub's limit for a comment body
MAX_ANSWER = 1 << 20  # bytes read of the forge's answer to a new comment, which echoes the body


# ---------------------------------------------------------------- credentials

def resolve(args, env, run):
    """verify-delivery's resolve (a token file, else Git's credential helper), except that a
    GitHub token without a token file comes from the GitHub CLI."""
    kind, api_base, _, _, _ = vd.parse_ref(args.pr, args.number, args.forge, args.api_base)
    var = {"github": "GITHUB_TOKEN_FILE", "gitlab": "GITLAB_TOKEN_FILE"}.get(kind, "GITEA_TOKEN_FILE")
    if args.anonymous or args.token_env or env.get(var) or kind != "github":
        return vd.resolve(args, env, run)
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
            except vd.NotFound:
                identities[sha] = (None, f"the base branch {pr['base']} was not found")
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
            # every page: a failed status past the first 100 must not be missed
            for s in forge.pages(f"/commits/{head}/status", key="statuses"):
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
    A run of 7 or more digits only is a SHA only at full length (it is more often a number), and
    the groups of a UUID (a conversation or run id the comment names) are no SHA at all."""
    text = UUID.sub(" ", body or "")
    shas = [t.lower() for t in SHA_TOKEN.findall(text) if len(t) == 40 or re.search(r"[a-fA-F]", t)]
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
    failures = [c for c in reversed(trusted) if args.failure.search(c["body"] or "") and names_head(c["body"], head)
                and vd.when(c["created_at"])]
    correlation = {}
    if failures:  # the label events are read only when there is a failure to correlate
        label = label_added(forge, kind, number, args.request_label)
        correlation = {"correlation": "label" if label else "wait_start", "since": (label or since).isoformat()}
        c = next((c for c in failures if vd.when(c["created_at"]) > (label or since)), None)
        if c:
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
        except vd.UsageError as e:
            if polls == 1:
                raise
            outcome, last_error = None, str(e)
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


# ---------------------------------------------------------------- posting the local review

def read_text(path, what):
    try:
        return pathlib.Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise vd.UsageError(f"the {what} is not UTF-8 text")
    except OSError:
        raise vd.UsageError(f"the {what} cannot be read")


def deny_entries(path):
    """The deny file's entries: one per line, matched as text in any case; blank lines and lines
    starting with # are skipped. The entries are never printed."""
    if not path:
        return []
    return [line.strip().lower() for line in read_text(path, "deny file").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def review_checks(text, header, verdict_re, deny):
    """The checks of a review text before it is posted: [{name, status, detail?, line?}]. HEADER is
    the configured pattern of the review being posted, VERDICT_RE verify-delivery's READY pattern,
    so what passes here is what the delivery check reads. Also returns the SHA the header names."""
    lines = text.splitlines()
    checks, sha = [], None

    def check(name, failure=None, line=None):
        c = {"name": name, "status": "failed" if failure else "passed"}
        if failure:
            c["detail"] = failure
        if line:
            c["line"] = line
        checks.append(c)

    first = lines[0] if lines else ""
    m = header.match(first)
    if not m:
        check("header", "line 1 is not the review header, for example "
                        "[oh-code-review (high) review] reviewed at head <full 40-digit SHA>", 1)
    elif not re.fullmatch(r"[0-9a-fA-F]{40}", m.group("sha")):
        check("header", f"the header names the head by the abbreviated SHA {m.group('sha')}: name its full 40-digit SHA", 1)
    else:
        sha = m.group("sha").lower()
        check("header")

    verdicts = [i for i, l in enumerate(lines) if VERDICT_LINE.fullmatch(l)]
    lookalikes = [i for i, l in enumerate(lines) if i not in verdicts and VERDICT_LOOKALIKE.match(l)]
    if lookalikes:
        n = lookalikes[0] + 1
        check("verdict", f"line {n} is not a VERDICT line: write VERDICT alone on its line and the verdict alone on the next", n)
    elif len(verdicts) != 1:
        check("verdict", "no VERDICT line" if not verdicts else
              "more than one VERDICT line (lines " + ", ".join(str(i + 1) for i in verdicts) + ")",
              verdicts[1] + 1 if len(verdicts) > 1 else None)
    else:
        i = verdicts[0] + 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        value = lines[i].strip() if i < len(lines) else ""
        if value not in VERDICTS:
            check("verdict", "the line after VERDICT must hold only one of " + ", ".join(VERDICTS),
                  i + 1 if i < len(lines) else verdicts[0] + 1)
        elif value == VERDICTS[0] and not verdict_re.search(text):
            check("verdict", "the delivery check's verdict pattern does not read this verdict", i + 1)
        else:
            check("verdict")

    a = ATTRIBUTION.search(text)
    level = LEVEL.match(first)
    level = level and (level.group("a") or level.group("b"))
    if not a:
        check("attribution", "no attribution line, for example: Review: oh-code-review (high) via Claude Code")
    elif level and a.group("level") != level:
        check("attribution", f"the attribution names level {a.group('level')}, the header {level}",
              text.count("\n", 0, a.start()) + 1)
    else:
        check("attribution")

    private = [i + 1 for i, l in enumerate(lines) if SESSION_URL.search(l)]
    check("session_links", private and "a link into an agent's session or conversation: remove it", private[0] if private else None)
    denied = [i + 1 for i, l in enumerate(lines) if any(e in l.lower() for e in deny)]
    check("deny_file", denied and "the text holds an entry of the deny file (the entry is not shown)",
          denied[0] if denied else None)
    check("size", len(text) > MAX_BODY and f"longer than {MAX_BODY} characters")
    return checks, sha


def post_comment(kind, forge, number, token, body, opener):
    """Create one comment; never retried, since a retry after a lost answer could post twice.
    Returns its URL (or None when the forge gives none)."""
    path = f"/merge_requests/{number}/notes" if kind == "gitlab" else f"/issues/{number}/comments"
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "aiskills-forge-gates"}
    if kind == "github":
        headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    headers["Authorization"] = ("token " if kind == "gitea" else "Bearer ") + token
    req = urllib.request.Request(forge.api + forge.repo_path + path, data=json.dumps({"body": body}).encode(),
                                 headers=headers, method="POST")
    try:
        with (opener or vd.default_opener())(req, timeout=vd.TIMEOUT) as resp:
            status, data = resp.status, resp.read(MAX_ANSWER)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise vd.UsageError(f"the forge refused the comment (HTTP {e.code}): the token may not comment here")
        raise vd.Unavailable(f"the forge refused the comment (HTTP {e.code})")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError):
        raise vd.Unavailable("no answer from the forge: the comment may or may not have been posted; "
                             "look at the pull request before posting again")
    if status not in (200, 201):
        raise vd.Unavailable(f"unexpected HTTP {status}: look at the pull request before posting again")
    try:
        c = json.loads(data.decode("utf-8"))
    except ValueError:
        return None
    return c.get("html_url") or (f"{forge.mr.get('web_url')}#note_{c['id']}" if kind == "gitlab" and c.get("id") else None)


def post_review(args, forge, kind, owner, repo, number, token, opener):
    text = read_text(args.body_file, "review file")
    header = dict(args.reviews).get(args.post_as)
    if header is None:
        raise vd.UsageError("--post-as names a review that is not configured (see --review)")
    checks, sha = review_checks(text, header, args.verdict, deny_entries(args.deny_file))
    pr = read_pull(forge, kind, number)
    state = {"name": "state", "state": pr["state"], "status": "passed" if pr["state"] == "open" else "failed"}
    if pr["state"] != "open":
        state["detail"] = "the pull request is merged: a review posted now is ignored by the delivery check" \
            if pr["state"] == "merged" else "the pull request is closed"
    head = {"name": "head", "head_sha": pr["head"], "status": "passed" if sha == pr["head"] else "failed"}
    if sha and sha != pr["head"]:
        head["detail"] = f"the review names {sha[:12]}, the pull request's head is {pr['head'][:12]}: review the current head"
    elif not sha:
        head["detail"] = "the header names no full SHA to compare"
    checks += [state, head]
    doc = {"outcome": None, "pr": {"forge": kind, "repo": f"{owner}/{repo}", "number": number, "url": pr["url"],
                                   "head_sha": pr["head"]}, "checks": checks}
    if any(c["status"] == "failed" for c in checks):
        doc["outcome"] = "refused"
        return doc, EXIT_NOT_READY
    if args.dry_run:
        doc["outcome"] = "dry_run"
        return doc, EXIT_READY
    if not token:
        raise vd.UsageError("posting needs a token that may comment: --anonymous works only with --dry-run")
    doc.update(outcome="posted", comment={"url": post_comment(kind, forge, number, token, text, opener)})
    return doc, EXIT_READY


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
    r = vd.add_arguments(sub.add_parser("post-review", allow_abbrev=False,
                                        help="post the local review, only in the form the delivery check reads"))
    r.add_argument("--body-file", required=True, metavar="FILE", help="the review text (UTF-8), posted as it is")
    r.add_argument("--post-as", default="local", metavar="NAME", help="the configured review it is (default local)")
    r.add_argument("--deny-file", metavar="FILE",
                   help="text that must not appear (private names), one entry per line; never printed")
    r.add_argument("--dry-run", action="store_true", help="run every check, read the pull request, post nothing")
    return p


def error_doc(command, e):
    if command == "merge-check":
        return vd.failure(e)
    kind = ("usage" if isinstance(e, vd.UsageError) else "unavailable" if isinstance(e, vd.Unavailable)
            else "not_found")
    code = {"usage": EXIT_USAGE, "unavailable": EXIT_PENDING}.get(kind, EXIT_NOT_READY)
    error = {"kind": kind, "message": str(e) if kind != "not_found" else "a record the pull request refers to was not found"}
    if command in ("wait-review", "post-review"):
        return {"outcome": "error", "error": error}, code
    return {"status": "pending" if code == EXIT_PENDING else "not_ready", "ready": False, "error": error}, code


def summary(command, doc):
    if "error" in doc and command != "merge-check":
        return f"{command}: {doc['error']['kind']} error: {doc['error']['message']}"
    if command == "merge-check":
        return f"merge-check: {doc['verdict']}" + (f": {doc['error']['message']}" if "error" in doc else "")
    if command == "post-review":
        failed = [f"{c['name']}" + (f" (line {c['line']})" if c.get("line") else "") + f": {c['detail']}"
                  for c in doc["checks"] if c["status"] == "failed"]
        return (f"post-review: {doc['outcome']} at {doc['pr']['head_sha'][:12]}"
                + (f": {doc['comment']['url']}" if doc.get("comment", {}).get("url") else "")
                + ("; " + "; ".join(failed) if failed else ""))
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
        elif command == "post-review":
            doc, code = post_review(args, forge, kind, owner, repo, number, token, opener)
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
