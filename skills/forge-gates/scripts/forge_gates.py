#!/usr/bin/env python3
"""Read the review gates of a pull (or merge) request from the forge: wait for the external
reviewer's answer for a head, say whether the current head is ready for a person to merge,
or give verify-delivery's delivery verdict; post the local review once it has the form
verify-delivery reads.

Read-only except post-review and request-review: every other request is an HTTP GET. post-review
creates one comment and never edits one; request-review adds one existing label and removes none;
nothing here pushes or merges.
Standard library only; it imports verify_delivery.py from the sibling verify-delivery skill.

    forge_gates.py wait-review --pr https://github.com/OWNER/REPO/pull/7 --head <40-digit SHA>
    forge_gates.py gates --pr https://github.com/OWNER/REPO/pull/7
    forge_gates.py merge-check --pr https://github.com/OWNER/REPO/pull/7
    forge_gates.py post-review --pr https://github.com/OWNER/REPO/pull/7 --body-file review.md [--dry-run]
    forge_gates.py request-review --pr https://github.com/OWNER/REPO/pull/7 --head <40-digit SHA> [--dry-run]

Prints one JSON document on stdout and one summary line on stderr. Exit codes: gates 0 ready,
3 not ready, 4 pending; wait-review 0 review found, 3 the wait ended otherwise, 4 timeout;
merge-check verify-delivery's; post-review 0 posted (or, with --dry-run, would post), 3
refused, 4 not posted for a forge error; request-review as wait-review once the label is on, 3
refused, 4 the label write failed or its answer was lost, 0 with --dry-run; 2 usage error. See SKILL.md.
"""
import copy, datetime, json, os, pathlib, re, socket, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

sys.dont_write_bytecode = True  # no __pycache__ inside an installed skill
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent / "verify-delivery"))
try:
    import verify_delivery as vd
except ImportError:
    vd = None

EXIT_READY, EXIT_USAGE, EXIT_NOT_READY, EXIT_PENDING = 0, 2, 3, 4
COMMANDS = ("wait-review", "request-review", "gates", "merge-check", "post-review", "check-links", "check-words",
            "record-review", "pre-push", "install-hook", "uninstall-hook")
LOCAL = ("check-links", "check-words", "record-review", "pre-push", "install-hook", "uninstall-hook")  # no forge needed
# gh lets these override its stored logins whatever --hostname names: a token for instance A
# would be returned for B. They are removed from the environment gh is run with.
GH_OVERRIDES = {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST"}
GH_TIMEOUT = 15
GITEA_DRAFT_PREFIXES = ("WIP:", "[WIP]")  # Gitea's default WORK_IN_PROGRESS_PREFIXES
MIN_INTERVAL = 30
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # a reviewer profile, the suffix of a variant request label
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
# The commit trailer a client adds for such a link.
SESSION_TRAILER = re.compile(r"[ \t]*Claude-Session[ \t]*:", re.I)
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


class NoForgeDiff(ValueError):
    """The forge gives no diff to compute a change identity from: with --repo, git computes it."""


def change_identity(forge, kind, tip, sha):
    """verify-delivery's patch_identity of SHA's change against its merge base with TIP."""
    path = f"/compare/{tip}...{sha}" + ("?output=diff" if kind == "gitea" else "")
    try:
        text = forge.diff(path)
    except vd.NotFound:
        raise NoForgeDiff("the forge has no compare diff for it" + (" (Gitea before 1.22)" if kind == "gitea" else ""))
    if kind == "gitea" and text.lstrip().startswith(b"{"):
        raise NoForgeDiff("this Gitea returns no diff from its compare API (?output=diff needs Gitea 1.27 or later)")
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
    # Both identities of a comparison come from one source: the forge, or, where the forge gives no
    # diff and --repo names a clone, git in that clone. The source only ever moves from the forge
    # to git, and every identity is read again from git once it has.
    source = ["forge"]

    def identity(sha):
        key = (source[0], sha)
        if key not in identities:
            try:
                if not tips:
                    tips.append(base_tip(forge, kind, pr["base"]))
                if source[0] == "git":
                    identities[key] = (vd.git_change_identity(args.repo, tips[0], sha), None)
                else:
                    identities[key] = (change_identity(forge, kind, tips[0], sha), None)
            except NoForgeDiff as e:
                if args.repo:
                    source[0] = "git"
                    return identity(sha)
                identities[key] = (None, f"{e}; with a local clone, --repo DIR computes it with git")
            except (vd.Unavailable, ValueError) as e:
                identities[key] = (None, str(e))
            except vd.NotFound:
                identities[key] = (None, f"the base branch {pr['base']} was not found")
        return identities[key]

    for r in reviews:
        if r["status"] != "ready" or r["at_final_head"]:
            continue
        sha = r["sha"]
        if len(sha) < 40:
            r.update(status="not_ready", detail=f"names an older head by the abbreviated SHA {sha}, which cannot be "
                                                "expanded safely: re-review at the current head, naming its full SHA")
            continue
        identity(sha), identity(head)  # the second read may move the source to git
        (reviewed, why), (current, why_current) = identity(sha), identity(head)
        r["identity_source"] = source[0]
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


def trusted_comments(args, forge, kind, number):
    """The pull request's comments by an author the awaited review trusts, oldest first."""
    items = sorted(vd.comment_items(forge, number, formal=False),
                   key=lambda c: vd.when(c["created_at"]) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc))
    name = args.wait_pattern[0]
    return [c for c in items if not vd.author_verdict(c, name, kind, args.review_authors, args.trust_any_author)]


def head_review(args, trusted, head, after=None):
    """The latest trusted review comment that names HEAD, as (comment, sha), or None. AFTER, when
    given, counts only a comment created after it."""
    found = None
    for c in trusted:
        if after and not ((vd.when(c["created_at"]) or after) > after):
            continue
        m = args.wait_pattern[1].search(c["body"] or "")
        sha = (m.group("sha") or "").lower() if m else ""
        if re.fullmatch(r"[0-9a-f]{7,40}", sha) and head.startswith(sha):
            found = (c, sha)
    return found


def head_failures(args, trusted, head):
    """The reviewer's failure comments that can be about HEAD, newest first. A comment that is itself
    a review (it carries a configured review's header, the local one included) is never the
    reviewer's failure comment, whatever its prose says."""
    return [c for c in reversed(trusted) if args.failure.search(c["body"] or "") and names_head(c["body"], head)
            and vd.when(c["created_at"]) and not any(p.search(c["body"] or "") for p in args.review_headers)]


def poll(args, forge, kind, number, head, since):
    """One look at the pull request: (outcome, details), or None to keep waiting."""
    pr = read_pull(forge, kind, number)
    name = args.wait_pattern[0]
    trusted = trusted_comments(args, forge, kind, number)
    found = head_review(args, trusted, head, getattr(args, "review_after", None))
    if found:
        c, sha = found
        word = VERDICT_WORD.search(c["body"] or "")
        return "review_found", {"review": {"review": name, "sha": sha, "abbreviated": len(sha) < 40,
                                           "verdict": word.group(1) if word else None,
                                           "ready_verdict": bool(args.verdict.search(c["body"] or "")),
                                           "url": c["url"], "author": c["author"], "created_at": c["created_at"]}}
    failures = head_failures(args, trusted, head)
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
    args.review_headers = list(patterns.values())
    try:
        args.failure = re.compile(args.failure_pattern)
    except re.error:
        raise vd.UsageError("--failure-pattern is not a valid regular expression")
    if getattr(args, "variant", None) is not None and not PROFILE.fullmatch(args.variant):
        raise vd.UsageError("--variant must be a profile name: letters, digits, '.', '_' and '-'")


# ---------------------------------------------------------------- requesting the external review

def label_names(items, what, plain=False):
    """The names in a raw list of labels: objects with a name or, PLAIN (GitLab's merge request),
    the names themselves. Refused when the list or any entry has another shape."""
    if not isinstance(items, list):
        raise vd.Refused(f"{what} is not a list")
    names = [vd.text(i) if plain else vd.text(i.get("name")) if isinstance(i, dict) else None for i in items]
    if None in names:
        raise vd.Refused(f"{what} has an entry without a name")
    return names


def root_pages(forge, path):
    """A strict listing at PATH under the API root instead of the repository's path."""
    view = copy.copy(forge)
    view.repo_path = ""
    return vd.Forge.pages(view, path, strict=True)


def forge_label(forge, kind, owner, name):
    """The label NAME as the forge defines it for the repository ({name}, and on Gitea its id), or
    Refused. It is looked up before the write because GitLab's add_labels creates a label it does
    not know, and what GitHub or Gitea do with an unknown name is not relied on either."""
    missing = f"the label {name} does not exist on the forge, and this command never creates one"
    if kind == "github":
        try:
            item = forge.get("/labels/" + urllib.parse.quote(name, safe=""))
        except vd.NotFound:
            raise vd.Refused(missing)
        if not isinstance(item, dict):
            raise vd.Refused(f"the forge's answer for the label {name} is not an object")
        if vd.text(item.get("name")) != name:
            raise vd.Refused(f"the forge has no label named exactly {name}")
        return {"name": name}
    if kind == "gitlab":
        items = forge.pages("/labels?include_ancestor_groups=true", strict=True)
    else:
        items = forge.pages("/labels", strict=True)
        try:
            items += root_pages(forge, f"/orgs/{urllib.parse.quote(owner, safe='')}/labels")
        except vd.NotFound:
            pass  # a repository a user owns has no organisation labels
    for item in items:  # the repository's own label first
        if vd.text(item.get("name")) == name:
            if kind == "gitea" and vd.integer(item.get("id")) is None:
                raise vd.Refused(f"the forge's label {name} has no numeric id")
            return {"name": name, "id": item["id"]} if kind == "gitea" else {"name": name}
    raise vd.Refused(missing)


def label_write(kind, number, label):
    """The one write, as (method, path, payload): GitHub takes the name, Gitea the id, and GitLab
    adds the name to the merge request's labels."""
    if kind == "github":
        return "POST", f"/issues/{number}/labels", {"labels": [label["name"]]}
    if kind == "gitea":
        return "POST", f"/issues/{number}/labels", {"labels": [label["id"]]}
    return "PUT", f"/merge_requests/{number}", {"add_labels": label["name"]}


def request_checks(args, forge, kind, owner, number, name, step):
    """The refusals of request-review, decided on the forge's raw answers before anything is written:
    (checks, the pull request, the label as the forge defines it, the time of an earlier review of
    the head that a newer failure answered, the reviewed-label entry). STEP[0] names the read in
    progress, for a refusal."""
    head, base = args.head, args.request_label
    pr = read_pull(forge, kind, number)
    raw = pr["raw"]
    state = vd.text(raw.get("state"))
    raw_head = vd.text(raw.get("sha")) if kind == "gitlab" else \
        vd.text(raw["head"].get("sha")) if isinstance(raw.get("head"), dict) else None
    names = label_names(raw.get("labels"), "the pull request's labels", plain=kind == "gitlab")
    is_open = state == ("opened" if kind == "gitlab" else "open")
    checks = [{"name": "state", "state": state, "status": "passed" if is_open else "failed"},
              {"name": "head", "head_sha": raw_head, "status": "passed" if raw_head and raw_head.lower() == head
               else "failed"}]
    if not is_open:
        checks[0]["detail"] = "the pull request is not open: " + (
            "merged" if pr["state"] == "merged" else f"its state is {state!r}")
    if checks[1]["status"] == "failed":
        checks[1]["detail"] = (f"the pull request's head is {raw_head}, not --head: a review would be of another head"
                               if raw_head else "the forge's answer names no head")
    on = [n for n in names if n == base or n.startswith(base + ":")]
    checks.append({"name": "request_label", "status": "failed" if on else "passed"})
    if on:
        checks[-1]["detail"] = f"a request label is already on: {', '.join(on)}"
    running = args.in_progress_label in names
    checks.append({"name": "in_progress_label", "status": "failed" if running else "passed"})
    if running:
        checks[-1]["detail"] = f"{args.in_progress_label} is on: a review is running"
    reviewed = {"name": args.reviewed_label, "on": args.reviewed_label in names}
    if reviewed["on"]:
        reviewed["detail"] = ("the label of an earlier review is on: remove it by hand, as the label protocol asks; "
                              "this command removes nothing")

    step[0] = "reading the comments"
    trusted = trusted_comments(args, forge, kind, number)
    found, after = head_review(args, trusted, head), None
    review = {"name": "head_review", "status": "passed"}
    if found:
        c = found[0]
        after = vd.when(c["created_at"])
        newer = [f for f in head_failures(args, trusted, head) if after and vd.when(f["created_at"]) > after]
        review["review_url"] = c["url"]
        if not after:
            review.update(status="failed", detail="a review of this head exists and its time cannot be read")
        elif newer:
            review.update(failure_url=newer[0]["url"],
                          detail="a review of this head exists, and a newer failure comment asks for it again")
        else:
            review.update(status="failed", detail="a review of this head exists with no newer failure: read it")
    checks.append(review)

    step[0] = "reading the forge's labels"
    try:
        label = forge_label(forge, kind, owner, name)
        checks.append({"name": "label", "label": name, "status": "passed"})
    except vd.Refused as e:
        label = None
        checks.append({"name": "label", "label": name, "status": "failed", "detail": str(e)})
    return checks, pr, label, after, reviewed


def review_count(args, forge, kind, number, pr):
    """The reviews of the gate row as gates counts them (--review, --required-reviews): reported,
    never a ground for a refusal."""
    try:
        gate = review_gate(args, forge, kind, number, pr)
    except Exception as e:  # the count is information only
        return {"error": f"the reviews could not be counted ({type(e).__name__})"}
    return {"ready": gate["ready"], "required": gate["required"],
            "summary": f"{gate['ready']} of {gate['required']} READY"}


def request_review(args, forge, kind, owner, repo, number, token, opener, clock, sleep):
    """Add the request label for --head, once every refusal was decided, then wait as wait-review.
    Nothing is written when a read fails: an outage or a usage error passes up as for every command,
    and any other surprise in the reads refuses, naming the read. The time between the last read and
    the write is not closed: a second requester in it adds the same label, which the forge holds once."""
    if not token and not args.dry_run:
        raise vd.UsageError("requesting needs a token that may label: --anonymous works only with --dry-run")
    name = f"{args.request_label}:{args.variant}" if args.variant else args.request_label
    doc = {"outcome": None, "pr": {"forge": kind, "repo": f"{owner}/{repo}", "number": number}, "head": args.head,
           "label": name}
    step = ["reading the pull request"]
    try:
        checks, pr, label, after, reviewed = request_checks(args, forge, kind, owner, number, name, step)
    except (vd.UsageError, vd.Unavailable):
        raise
    except vd.NotFound:
        doc.update(outcome="refused", reason=f"the forge answered 404 while {step[0]}")
        return doc, EXIT_NOT_READY
    except Exception as e:  # fails closed: a malformed answer refuses, and nothing is written
        reason = f"{e} while {step[0]}" if isinstance(e, vd.Refused) else f"{type(e).__name__} while {step[0]}"
        doc.update(outcome="refused", reason=reason)
        return doc, EXIT_NOT_READY
    doc["pr"].update(url=pr["url"], head_sha=pr["head"])
    doc.update(checks=checks, reviewed_label=reviewed, reviews=review_count(args, forge, kind, number, pr))
    if any(c["status"] == "failed" for c in checks):
        doc["outcome"] = "refused"
        return doc, EXIT_NOT_READY
    method, path, payload = label_write(kind, number, label)
    request = doc["request"] = {"label": name, "method": method, "path": path}
    if args.dry_run:
        request["added"] = False
        doc["outcome"] = "dry_run"
        return doc, EXIT_READY
    try:
        status, _ = send(kind, forge, token, method, path, payload, opener)
    except urllib.error.HTTPError as e:
        denied = e.code in (401, 403)
        request.update(added=False, detail=f"the forge refused the label (HTTP {e.code})"
                       + (": the token may not label here" if denied else ""))
        doc["outcome"] = "write_failed"
        return doc, EXIT_USAGE if denied else EXIT_PENDING
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError):
        status = None
    if status is None or not 200 <= status < 300:
        request.update(added="unknown", detail=("no answer from the forge" if status is None else f"HTTP {status}")
                       + ": read the pull request's labels before requesting again")
        doc["outcome"] = "write_unknown"
        return doc, EXIT_PENDING
    request.update(added=True, added_at=datetime.datetime.fromtimestamp(clock(), datetime.timezone.utc).isoformat())
    args.review_after = after  # a review of the head from before this request is not its answer
    try:
        wait, code = wait_review(args, forge, kind, owner, repo, number, clock, sleep)
    except Exception as e:  # the label is on: the document says so whatever ends the wait
        usage = isinstance(e, (vd.UsageError, vd.NotFound))
        doc.update(outcome="error", error={"kind": "usage" if usage else "unexpected",
                                           "message": str(e) if usage else f"{type(e).__name__} while waiting"})
        return doc, EXIT_USAGE if usage else EXIT_NOT_READY
    doc.update({k: v for k, v in wait.items() if k not in ("pr", "head")})
    return doc, code


# ---------------------------------------------------------------- posting the local review

def read_text(path, what):
    try:
        return pathlib.Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise vd.UsageError(f"the {what} is not UTF-8 text")
    except OSError:
        raise vd.UsageError(f"the {what} cannot be read")


def session_link(line):
    """Whether a line links into an agent's session or conversation: a session URL anywhere in it,
    or the trailer a client adds for one. No git artifact carries either, on any remote."""
    return bool(SESSION_URL.search(line) or SESSION_TRAILER.match(line))


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

    private = [i + 1 for i, l in enumerate(lines) if session_link(l)]
    check("session_links", private and "a link into an agent's session or conversation: remove it", private[0] if private else None)
    for name, words, what in deny:
        if words is None:  # a list not used here; WHAT says why
            checks.append({"name": name, "status": "passed", "note": what})
            continue
        denied = words.lines(text)
        check(name, denied and f"the text holds an entry of the {what} (neither the entry nor the line is shown)",
              denied[0] if denied else None)
    check("size", len(text) > MAX_BODY and f"longer than {MAX_BODY} characters")
    return checks, sha


def send(kind, forge, token, method, path, payload, opener):
    """One write request to the repository's API path, never retried: (status, the first MAX_ANSWER
    bytes of the answer). urllib's errors pass through for the caller to name."""
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "aiskills-forge-gates"}
    if kind == "github":
        headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    headers["Authorization"] = ("token " if kind == "gitea" else "Bearer ") + token
    req = urllib.request.Request(forge.api + forge.repo_path + path, data=json.dumps(payload).encode(),
                                 headers=headers, method=method)
    with (opener or vd.default_opener())(req, timeout=vd.TIMEOUT) as resp:
        return resp.status, resp.read(MAX_ANSWER)


def post_comment(kind, forge, number, token, body, opener):
    """Create one comment; never retried, since a retry after a lost answer could post twice.
    Returns its URL (or None when the forge gives none)."""
    path = f"/merge_requests/{number}/notes" if kind == "gitlab" else f"/issues/{number}/comments"
    try:
        status, data = send(kind, forge, token, "POST", path, {"body": body}, opener)
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


def review_denies(args, env, api_base):
    """The word lists a review is checked against: the private-word file unless the forge's host
    is listed as private, and --deny-file when given; a list not used carries the reason instead."""
    words, path = private_words(env)
    if not words:
        deny = [("private_words", None, off_note(path))]
    elif is_private(remote_host(api_base), private_hosts(env)):
        deny = [("private_words", None, "the forge's host is listed as private: the private-word file is not used")]
    else:
        deny = [("private_words", words, "private-word file")]
    if args.deny_file:
        deny.append(("deny_file", DenyFile(read_text(args.deny_file, "deny file")), "deny file"))
    return deny


class DenyFile:
    """post-review's --deny-file as released: one entry per line (blank and # lines skipped),
    matched as a substring in any case, so a token prefix or a host fragment still matches."""

    def __init__(self, text):
        self.entries = [line.strip().lower() for line in text.splitlines()
                        if line.strip() and not line.lstrip().startswith("#")]

    def lines(self, text):
        return [i + 1 for i, line in enumerate(text.splitlines()) if any(e in line.lower() for e in self.entries)]


def post_review(args, env, api_base, forge, kind, owner, repo, number, token, opener):
    text = read_text(args.body_file, "review file")
    header = dict(args.reviews).get(args.post_as)
    if header is None:
        raise vd.UsageError("--post-as names a review that is not configured (see --review)")
    checks, sha = review_checks(text, header, args.verdict, review_denies(args, env, api_base))
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


# ---------------------------------------------------------------- checking text before a push

def commit_messages(revisions, run):
    """[(sha, message)] of the commits git log lists for REVISIONS (a range such as
    origin/main..HEAD, or a list of git log's revision arguments), newest first."""
    revs = [revisions] if isinstance(revisions, str) else list(revisions)
    if isinstance(revisions, str) and revisions.startswith("-"):
        raise vd.UsageError("--commits takes a revision range, not an option")
    try:
        p = run(["git", "log", "-z", "--format=%H%n%B", *revs, "--"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise vd.UsageError(f"git log cannot run: {e}")
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()
        raise vd.UsageError(f"git log {' '.join(revs)} failed" + (f": {err[0]}" if err else ""))
    commits = []
    for record in p.stdout.decode("utf-8", "replace").split("\0"):
        sha, _, message = record.lstrip("\n").partition("\n")
        if sha:
            commits.append((sha, message))
    return commits


def link_hits(sources):
    return [{"source": source, "line": i + 1}
            for source, text in sources for i, line in enumerate(text.splitlines()) if session_link(line)]


def check_links(args, run):
    """The session links in the commit messages and files named: each hit by its source and line,
    never by its text, which is the private link. Also the pushes of these commits that skipped
    the pre-push review gate, which the next review names."""
    commits = [c for r in args.commits for c in commit_messages(r, run)]
    sources = [(f"commit {sha}", message) for sha, message in commits]
    sources += [(f"file {path}", read_text(path, f"file {path}")) for path in args.file]
    hits = link_hits(sources)
    doc = {"outcome": "refused" if hits else "clean", "sources": len(sources), "hits": hits}
    skips = skip_records(run, [sha for sha, _ in commits]) if commits else []
    if skips:
        doc["skips"] = skips
    return doc, EXIT_NOT_READY if hits else EXIT_READY


# ---------------------------------------------------------------- private words

WORDS_VAR = "AISKILLS_PRIVATE_WORDS"
HOSTS_VAR = "AISKILLS_PRIVATE_HOSTS"
WITHHELD = "<private>"
HUNK = re.compile(r"(@+) [^@]*\+(\d+)(?:,\d+)? @+")


WORD_CHAR = re.compile(r"[A-Za-z0-9_]")


def whole(entry):
    """ENTRY's pattern: literal, and bounded only at an edge that is itself a word character. So a
    match never runs on into a longer word, while `creds/` or `.host` still match where a path or
    a name continues them."""
    return ((r"(?<![A-Za-z0-9_])" if WORD_CHAR.match(entry[0]) else "") + re.escape(entry)
            + (r"(?![A-Za-z0-9_])" if WORD_CHAR.match(entry[-1]) else ""))


class Words:
    """Entries matched literally, in any case, as whole words: at an edge of the entry that is a
    letter, a digit or `_`, the neighbouring character must not be one. A match that lies wholly
    inside an allowed literal is no hit. Nothing here ever returns or prints the text it matched."""

    def __init__(self, entries, allowed=()):
        self.entries, self.allowed_literals = list(entries), list(allowed)
        by_length = sorted(set(entries), key=len, reverse=True)
        self.pattern = re.compile("|".join(whole(e) for e in by_length), re.I) if entries else None
        self.allowed = re.compile("|".join(re.escape(a) for a in sorted(set(allowed), key=len, reverse=True)),
                                  re.I) if allowed else None

    def __bool__(self):
        return self.pattern is not None

    def __add__(self, other):
        return Words(self.entries + other.entries, self.allowed_literals + other.allowed_literals)

    def hit(self, text):
        if self.pattern is None or not text:
            return False
        spans = [m.span() for m in self.allowed.finditer(text)] if self.allowed else []
        return any(not any(a <= m.start() and m.end() <= b for a, b in spans) for m in self.pattern.finditer(text))

    def lines(self, text):
        """The 1-based numbers of TEXT's lines that hold a hit."""
        return [i + 1 for i, line in enumerate(text.splitlines()) if self.hit(line)]

    def scrub(self, text):
        """TEXT with every match replaced, allowed or not: the last net before anything is printed."""
        return self.pattern.sub(WITHHELD, text) if self.pattern is not None else text


def read_words(path, what):
    """A word file: one entry per line, blank lines and lines starting with # skipped; a line
    starting with ! is an allowed literal, inside which the entries are no hit."""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise vd.UsageError(f"the {what} {path} is not UTF-8 text")
    except OSError:
        raise vd.UsageError(f"the {what} {path} cannot be read")
    entries, allowed = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            if line[1:].strip():
                allowed.append(line[1:].strip())
        else:
            entries.append(line)
    return Words(entries, allowed)


def config_dir(env):
    if os.name == "nt" and env.get("APPDATA"):
        return pathlib.Path(env["APPDATA"]) / "aiskills"
    home = env.get("HOME") or os.path.expanduser("~")
    return pathlib.Path(env.get("XDG_CONFIG_HOME") or pathlib.Path(home) / ".config") / "aiskills"


def words_path(env):
    return pathlib.Path(env.get(WORDS_VAR) or config_dir(env) / "private-words")


def private_words(env, path=None):
    """(Words, path) of the machine's private-word file; Words is empty when there is no file."""
    path = pathlib.Path(path) if path else words_path(env)
    return (read_words(path, "private-word file") if path.exists() else Words([])), path


def private_hosts(env):
    path = pathlib.Path(env.get(HOSTS_VAR) or config_dir(env) / "private-hosts")
    return read_words(path, "private-hosts file").entries if path.exists() else []


def remote_host(url):
    """The host of a git remote URL (https://, ssh://, git://, or scp-like user@host:path); None
    for a local path or anything that does not parse, which counts as public."""
    if not url:
        return None
    if "://" in url:
        try:
            return urllib.parse.urlsplit(url).hostname or None
        except ValueError:
            return None
    m = re.match(r"(?:[^@/\\]+@)?([^/\\:@]+):", url)
    return m.group(1).lower() if m and len(m.group(1)) > 1 else None


def is_private(host, hosts):
    host = (host or "").lower().rstrip(".")
    return bool(host) and any(host == h.lower() or host.endswith("." + h.lower()) for h in hosts)


def off_note(path):
    return f"no private-word file at {path}; the private-word check is off"


def git_stream(env, gitdir, args):
    """git's stdout line by line, never held whole: a diff of any size is read in flat memory."""
    try:
        p = subprocess.Popen(["git", "--git-dir", gitdir, *args], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=dict(env))
    except OSError as e:
        raise vd.UsageError(f"git cannot run: {e}")
    with p.stdout:
        for raw in p.stdout:
            yield raw.decode("utf-8", "replace").rstrip("\r\n")
    if p.wait() != 0:
        raise vd.UsageError(f"git {args[0]} failed (exit {p.returncode})")


def where(path, index, words):
    """How a hit's file is named: by its path, or by its place when the path itself holds a word."""
    return f"path #{index}" if words.hit(path) else path


def diff_hits(lines, words):
    """The hits in `git log -p` output, by commit and file:line, never by text; and the number of
    binary files, whose content git does not show."""
    hits, binaries = [], 0
    sha, path, index, cols, new, in_hunk = None, None, 0, 1, 0, False
    # The patch being read: its header, whether its new path was named (+++, rename to, copy to)
    # or it deletes the file, and whether its path already hit.
    patch = None

    def new_path(text):
        patch.update(named=True)
        if words.hit(text) and not patch["hit"]:
            patch.update(hit=True)
            hits.append({"commit": sha, "in": "path", "path": f"path #{index}"})

    def end_patch():
        # A patch that never names its new path (an empty new file, a binary file, a mode change)
        # is read by its header, which then holds no other path. A deletion adds no path.
        if patch and not patch["named"] and not patch["gone"]:
            new_path(patch["header"])

    for line in lines:
        if line.startswith("\x01"):
            end_patch()
            sha, path, index, in_hunk, patch = line[1:].strip(), None, 0, False, None
        elif line.startswith("diff "):
            end_patch()
            path, in_hunk = None, False
            index += 1
            patch = {"header": line[5:], "named": False, "gone": False, "hit": False}
        elif in_hunk and line[:1] in (" ", "+", "-", "\\"):
            if line.startswith("\\"):
                continue
            prefix, body = line[:cols], line[cols:]
            if "-" in prefix:
                continue
            if "+" in prefix and words.hit(body):
                hits.append({"commit": sha, "in": "line", "path": where(path or "", index, words), "line": new})
            new += 1
        elif line.startswith("@@"):
            m = HUNK.match(line)
            cols, new, in_hunk = (len(m.group(1)) - 1, int(m.group(2)), True) if m else (1, 0, False)
        elif patch is None:
            continue
        elif line.startswith("deleted file mode"):
            patch.update(gone=True)
        elif line.startswith(("+++ ", "rename to ", "copy to ")):
            text = line[4:] if line.startswith("+++ ") else line.split(" ", 2)[2]
            if text == "/dev/null":
                patch.update(gone=True)
                continue
            path = text[2:] if line.startswith("+++ ") and text.startswith("b/") else text
            new_path(text)
        elif line.startswith("Binary files "):  # its path: a rename's, else the header's
            binaries += 1
    end_patch()
    return hits, binaries


def word_hits_in_commits(revs, words, env, run):
    """The private-word hits in the commits REVS names: their messages, the lines they add and
    the paths they add, each commit on its own; and the count of binary files not read."""
    hits = [{"commit": sha, "in": "message", "line": n}
            for sha, message in commit_messages(revs, run) for n in words.lines(message)]
    gitdir = git(run, "rev-parse", "--absolute-git-dir")
    diff, binaries = diff_hits(git_stream(env, gitdir, [
        "-c", "core.quotePath=false", "log", "-p", "-U0", "-M", "--cc", "--no-ext-diff", "--no-textconv",
        "--no-color", "--src-prefix=a/", "--dst-prefix=b/", "--format=%x01%H", *revs, "--"]), words)
    return hits + diff, binaries


def word_hits_in_tree(top, words, run):
    """The private-word hits in the tracked files of the checkout at TOP, by file:line."""
    names = git(run, "ls-files", "-z", repo=top) or ""
    hits, binaries = [], 0
    for index, name in enumerate(n for n in names.split("\0") if n):
        shown = where(name, index + 1, words)
        if shown != name:
            hits.append({"in": "path", "path": shown})
        try:
            data = (pathlib.Path(top) / name).read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8000]:
            binaries += 1
            continue
        hits += [{"in": "line", "path": shown, "line": n} for n in words.lines(data.decode("utf-8", "replace"))]
    return hits, binaries


def hit_text(h):
    place = {"message": f"message line {h.get('line')}", "path": h.get("path"),
             "ref": f"the name of ref {h.get('ref')}", "tag": f"the message of tag {h.get('ref')}",
             "reason": "the skip reason"}.get(h["in"], f"{h.get('path')}:{h.get('line')}")
    return (f"commit {h['commit']} " if h.get("commit") else "") + place


def check_words(args, env, run):
    words, path = private_words(env, args.words)
    if not words:
        return {"outcome": "off", "words": str(path), "note": off_note(path), "hits": []}, EXIT_READY
    hits, binaries = [], 0
    for r in args.commits:
        if r.startswith("-"):
            raise vd.UsageError("--commits takes a revision range, not an option")
        h, b = word_hits_in_commits([r], words, env, run)
        hits, binaries = hits + h, binaries + b
    if args.tree:
        top = git(run, "rev-parse", "--show-toplevel", repo=args.tree)
        h, b = word_hits_in_tree(top, words, run)
        hits, binaries = hits + h, binaries + b
    doc = {"outcome": "refused" if hits else "clean", "words": str(path), "hits": hits, "binary_files_not_read": binaries}
    return doc, EXIT_NOT_READY if hits else EXIT_READY


# ---------------------------------------------------------------- the pre-push review gate

ZERO_SHA = "0" * 40
LEVELS = ("low", "medium", "high", "max", "ultra")  # a record counts from medium up
SKIP_VAR = "AISKILLS_PRE_PUSH_SKIP"
HOOK_MARKER = "# aiskills pre-push review gate: written by forge_gates.py install-hook"


class Refused(Exception):
    """A local command declined to act; the message says why and what to do."""


def git(run, *args, repo=None, ok=(0,)):
    """git's stdout, stripped; a usage error when git fails (any exit outside OK)."""
    cmd = ["git"] + (["-C", str(repo)] if repo else []) + list(args)
    try:
        p = run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise vd.UsageError(f"git cannot run: {e}")
    if p.returncode not in ok:
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()
        raise vd.UsageError(f"git {' '.join(args)} failed" + (f": {err[0]}" if err else ""))
    return p.stdout.decode("utf-8", "replace").strip() if p.returncode == 0 else None


def records_dir(run, repo=None):
    """Where the review records live: inside the repository's common git directory, so every
    worktree shares them and nothing there is committed, pushed or shown by git status."""
    return pathlib.Path(git(run, "rev-parse", "--path-format=absolute", "--git-common-dir", repo=repo)) / "aiskills" / "pre-push"


def full_sha(run, rev, repo=None):
    if rev.startswith("-"):
        raise vd.UsageError("--head takes a commit, not an option")
    sha = git(run, "rev-parse", "--verify", "--quiet", rev + "^{commit}", repo=repo, ok=(0, 1))
    if not sha:
        raise vd.UsageError(f"{rev} is not a commit of this repository")
    return sha


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_record(path):
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def record_review(args, run):
    head = full_sha(run, args.head, args.repo)
    record = {"head": head, "level": args.level, "verdict": args.verdict, "time": now(),
              "tool": "forge_gates.py record-review"}
    path = records_dir(run, args.repo) / f"{head}.json"
    write_json(path, record)
    return {"outcome": "recorded", "record": record, "path": str(path)}, EXIT_READY


def review_problem(record, head):
    """Why RECORD does not let HEAD through, or None when it does."""
    if record is None:
        return "no pre-push review is recorded for it"
    if record.get("head") != head:
        return f"its record names another commit ({str(record.get('head'))[:12]})"
    if record.get("verdict") != "clean":
        return f"its recorded review found a blocker (verdict {record.get('verdict')})"
    if record.get("level") not in LEVELS[1:]:
        return f"its recorded review ran at level {record.get('level')}, below medium"
    return None


def skip_records(run, shas, repo=None):
    d = records_dir(run, repo)
    out = []
    for sha in shas:
        r = read_record(d / f"{sha}.skip.json")
        if r is not None:
            out.append({"head": sha, "reason": r.get("reason"), "time": r.get("time")})
    return out


def pre_push_words(args, env, run):
    """The words the push is checked for, and what the result says about the check. A remote
    whose host is listed private is not checked; its host is never named in the result."""
    words, path = private_words(env)
    if not words:
        return words, {"state": "off", "note": off_note(path)}
    url = args.url or git(run, "config", "--get", f"remote.{args.remote}.url", ok=(0, 1)) or args.remote
    if is_private(remote_host(url), private_hosts(env)):
        return Words([]), {"state": "not checked", "note": "the remote's host is listed as private"}
    return words, {"state": "on", "file": str(path)}


def pre_push(args, env, run, stdin):
    """git's pre-push hook: each pushed branch head that adds commits needs a clean review
    recorded at medium or above, its new commits carry no session link, and on a public remote
    no new text (a ref's name, a tag's message, a commit's message, added lines and paths) holds
    a private word."""
    d = records_dir(run)
    words, check = pre_push_words(args, env, run)
    # Checked against the whole word file even for a private remote: the skip record stays local
    # but is a file this tool writes, and no file it writes holds a private word.
    every_word = private_words(env)[0]
    reason = (env.get(SKIP_VAR) or "").strip()
    reason_refused = bool(reason) and every_word.hit(reason)
    if reason_refused:
        reason = ""
    # A push to a URL names no remote: then a commit any remote-tracking branch holds counts as
    # pushed, so history already pushed elsewhere is not read again.
    named = git(run, "config", "--get", f"remote.{args.remote}.url", ok=(0, 1)) is not None
    already = f"--remotes={args.remote}" if named else "--remotes"
    refs, hits, skips, word_hits, binaries = [], [], [], [], 0
    for n, line in enumerate(stdin.read().splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4:
            raise vd.UsageError(f"line {n} of git's input is not a pre-push line")
        _, local, remote_ref, remote = parts
        ref = {"ref": remote_ref, "head": local}
        refs.append(ref)
        if local == ZERO_SHA:
            ref.update(status="exempt", detail="a deletion")
            continue
        found = []
        # Replaced against the whole word file even for a private remote: the ref goes into the
        # skip record, a file this tool writes.
        if every_word.hit(remote_ref):
            ref["ref"] = f"#{len(refs)}"
            if words.hit(remote_ref):
                found.append({"in": "ref", "ref": ref["ref"]})
        tag = remote_ref.startswith("refs/tags/")
        if tag and words and git(run, "cat-file", "-t", local) == "tag":
            body = git(run, "cat-file", "tag", local).partition("\n\n")[2]
            if words.hit(body):
                found.append({"in": "tag", "ref": ref["ref"]})
        revs = [local, "--not", already]
        if remote != ZERO_SHA and git(run, "cat-file", "-e", remote + "^{commit}", ok=(0, 1, 128)) is not None:
            revs.append(remote)
        new = git(run, "rev-list", *revs).split()
        if new:
            hits += link_hits([(f"commit {sha}", message) for sha, message in commit_messages(revs, run)])
            skips += [s for s in skip_records(run, new) if s["head"] != local]
            if words:
                h, b = word_hits_in_commits(revs, words, env, run)
                found, binaries = found + h, binaries + b
        word_hits += found
        unreviewed = review_problem(read_record(d / f"{local}.json"), local) if new and not tag else None
        problems = ([unreviewed] if unreviewed else []) + (["it adds text that holds a private word"] if found else [])
        checks = (["review"] if unreviewed else []) + (["private-words"] if found else [])
        if not problems:
            if tag:
                ref.update(status="exempt", detail="a tag")
            elif not new:
                ref.update(status="exempt", detail="it adds no commit the remote lacks")
            else:
                ref["status"] = "reviewed"
        elif reason:
            write_json(d / f"{local}.skip.json", {"head": local, "ref": ref["ref"], "reason": reason,
                                                   "checks": checks, "time": now()})
            ref.update(status="skipped", detail=reason, checks=checks)
        else:
            ref.update(status="refused", detail="; ".join(problems), unreviewed=bool(unreviewed))
    refused = hits or any(r["status"] == "refused" for r in refs)
    doc = {"outcome": "refused" if refused else "clean", "refs": refs, "hits": hits,
           "private_words": dict(check, hits=word_hits, binary_files_not_read=binaries)}
    if reason_refused:
        doc["skip_refused"] = "the skip reason holds a private word (not shown): give another reason"
    if skips:
        doc["skips"] = skips
    return doc, EXIT_NOT_READY if refused else EXIT_READY


def hook_path(run, repo, shared):
    """The pre-push hook file git runs. A hooks directory set in the global or system
    configuration is every repository's: the gate goes there only when SHARED says so."""
    setting = git(run, "config", "--show-scope", "--get", "core.hooksPath", repo=repo, ok=(0, 1))
    if setting:
        scope = setting.split(None, 1)[0]
        if scope in ("global", "system") and not shared:
            raise Refused(f"core.hooksPath is set in the {scope} git configuration ({setting.split(None, 1)[-1]}): "
                          "a hook there runs for every repository of this machine. Pass --shared-hooks-path "
                          "to install the gate there anyway")
    return pathlib.Path(git(run, "rev-parse", "--path-format=absolute", "--git-path", "hooks", repo=repo)) / "pre-push"


def sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def hook_script():
    fg = pathlib.Path(__file__).resolve().as_posix()
    py = pathlib.Path(sys.executable).resolve().as_posix()
    return (f"#!/bin/sh\n{HOOK_MARKER}; remove it with uninstall-hook.\n"
            f"fg={sh_quote(fg)}\npy={sh_quote(py)}\n"
            'if [ ! -f "$fg" ]; then\n'
            '  echo "aiskills pre-push gate: $fg is missing: install forge-gates and run its install-hook again, '
            'or delete $0 to remove the gate" >&2\n'
            "  exit 1\n"
            "fi\n"
            'exec "$py" "$fg" pre-push "$@" >/dev/null\n')


def install_hook(args, run):
    hook = hook_path(run, args.repo, args.shared_hooks_path)
    existed = hook.exists()
    if existed and HOOK_MARKER not in hook.read_text(encoding="utf-8", errors="replace"):
        raise Refused(f"{hook} is a pre-push hook this command did not write: it is left as it is")
    hook.parent.mkdir(parents=True, exist_ok=True)
    with open(hook, "w", encoding="utf-8", newline="\n") as f:
        f.write(hook_script())
    hook.chmod(0o755)
    return {"outcome": "updated" if existed else "installed", "hook": str(hook)}, EXIT_READY


def uninstall_hook(args, run):
    hook = hook_path(run, args.repo, True)
    if not hook.exists():
        return {"outcome": "absent", "hook": str(hook)}, EXIT_READY
    if HOOK_MARKER not in hook.read_text(encoding="utf-8", errors="replace"):
        raise Refused(f"{hook} is a pre-push hook this command did not write: it is left as it is")
    hook.unlink()
    return {"outcome": "removed", "hook": str(hook)}, EXIT_READY


# ---------------------------------------------------------------- CLI

def wait_arguments(w):
    w.add_argument("--head", required=True, help="the full 40-digit SHA of the head that was pushed")
    w.add_argument("--wait-for", default="external", metavar="NAME", help="the configured review to wait for (default external)")
    w.add_argument("--timeout", type=int, default=2700, help="seconds to wait (default 2700)")
    w.add_argument("--interval", type=int, default=60, help=f"seconds between polls (default 60, at least {MIN_INTERVAL})")
    w.add_argument("--failure-pattern", default="could not run", help="regex of the reviewer's failure comment")
    w.add_argument("--request-label", default="review-this",
                   help="the label that requests the review; LABEL:<profile> variants count too (default review-this)")
    return w


def build_parser():
    p = vd.ArgParser(prog="forge_gates.py", description=__doc__.split("\n\n")[0], allow_abbrev=False)
    sub = p.add_subparsers(dest="command", required=True, metavar="{" + ",".join(COMMANDS) + "}")
    wait_arguments(vd.add_arguments(sub.add_parser("wait-review", allow_abbrev=False,
                                                   help="wait for the external reviewer's answer for a head")))
    q = wait_arguments(vd.add_arguments(sub.add_parser(
        "request-review", allow_abbrev=False, help="add the request label for a head, then wait as wait-review")))
    q.add_argument("--variant", metavar="PROFILE",
                   help="add REQUEST_LABEL:PROFILE instead of the plain label; it must exist on the forge")
    q.add_argument("--in-progress-label", default="hands-reviewing",
                   help="the reviewer's label while a review runs: refused while it is on (default hands-reviewing)")
    q.add_argument("--reviewed-label", default="hands-reviewed",
                   help="the reviewer's label after a review: reported when on, never removed (default hands-reviewed)")
    q.add_argument("--dry-run", action="store_true", help="run every read and refusal, write nothing")
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
    c = sub.add_parser("check-links", allow_abbrev=False,
                       help="find session links in commit messages and files before a push; no forge needed")
    c.add_argument("--commits", action="append", default=[], metavar="RANGE",
                   help="the commits to read, as git log takes them (for example origin/main..HEAD); repeatable")
    c.add_argument("--file", action="append", default=[], metavar="FILE",
                   help="a text file to read, such as a pull request description (UTF-8); repeatable")
    cw = sub.add_parser("check-words", allow_abbrev=False,
                        help="find the words of the private-word file in commits or a checkout's tracked files; "
                             "never prints a word or the line it stands in")
    cw.add_argument("--commits", action="append", default=[], metavar="RANGE",
                    help="the commits to read, as git log takes them (for example origin/main..HEAD); repeatable")
    cw.add_argument("--tree", nargs="?", const=".", metavar="DIR",
                    help="read the tracked files of the checkout at DIR (default: the current one)")
    cw.add_argument("--words", metavar="FILE",
                    help=f"the word file (default ${WORDS_VAR}, else private-words in the aiskills config directory)")
    rr = sub.add_parser("record-review", allow_abbrev=False,
                        help="record the pre-push review of a head, which the pre-push hook reads")
    rr.add_argument("--head", required=True, help="the commit reviewed (any name git resolves; recorded by full SHA)")
    rr.add_argument("--level", required=True, choices=LEVELS, help="the review's level; the hook counts medium and above")
    rr.add_argument("--verdict", required=True, choices=("clean", "blocker"),
                    help="clean: every finding fixed or waived; blocker: a finding stands")
    rr.add_argument("--repo", metavar="DIR", help="the repository (default: the current directory)")
    pp = sub.add_parser("pre-push", allow_abbrev=False,
                        help="git's pre-push hook: reads git's lines on stdin; the hook install-hook writes runs it")
    pp.add_argument("remote", help="the remote's name (or its URL when the push names none)")
    pp.add_argument("url", nargs="?", help="the remote's URL (unused; git passes it)")
    for name, text in (("install-hook", "write the pre-push hook that runs the review gate"),
                       ("uninstall-hook", "remove the pre-push hook install-hook wrote")):
        h = sub.add_parser(name, allow_abbrev=False, help=text)
        h.add_argument("--repo", metavar="DIR", help="the repository (default: the current directory)")
        if name == "install-hook":
            h.add_argument("--shared-hooks-path", action="store_true",
                           help="install even when core.hooksPath comes from the global or system configuration, "
                                "which puts the gate on every repository that uses it")
    return p


def error_doc(command, e):
    if command == "merge-check":
        return vd.failure(e)
    kind = ("usage" if isinstance(e, vd.UsageError) else "unavailable" if isinstance(e, vd.Unavailable)
            else "not_found")
    code = {"usage": EXIT_USAGE, "unavailable": EXIT_PENDING}.get(kind, EXIT_NOT_READY)
    error = {"kind": kind, "message": str(e) if kind != "not_found" else "a record the pull request refers to was not found"}
    if command in ("wait-review", "request-review", "post-review") + LOCAL:
        return {"outcome": "error", "error": error}, code
    return {"status": "pending" if code == EXIT_PENDING else "not_ready", "ready": False, "error": error}, code


def skip_note(s):
    return f"the push of {s['head']} skipped the pre-push review gate ({s['time']}): {s['reason']}"


def pre_push_summary(doc):
    lines = []
    for r in doc["refs"]:
        if r["status"] == "refused" and not r.get("unreviewed"):
            lines.append(f"pre-push review gate: {r['ref']} at {r['head']} is refused: {r['detail']}.")
        elif r["status"] == "refused":
            lines += [f"pre-push review gate: {r['ref']} at {r['head']} is refused: {r['detail']}.",
                      "  Run the medium review of this head, fix or have waived every finding, then record it:",
                      f'    "{pathlib.Path(sys.executable).resolve().as_posix()}" "{pathlib.Path(__file__).resolve().as_posix()}" '
                      f"record-review --head {r['head']} "
                      "--level medium --verdict clean",
                      "  A record binds to one commit: after an amend, a rebase or an update from the base, "
                      "review the new head and record it."]
        elif r["status"] == "skipped":
            lines.append(f"pre-push review gate SKIPPED for {r['head']}: {r['detail']} "
                         f"(checks skipped: {', '.join(r['checks'])})")
    lines += [f"pre-push: a session link in {h['source']} line {h['line']}: remove it before the push" for h in doc["hits"]]
    pw = doc.get("private_words", {})
    if pw.get("note"):
        lines.append(f"pre-push: {pw['note']}")
    lines += [f"pre-push: a private word in {hit_text(h)} (not shown): remove it before the push" for h in pw.get("hits", [])]
    if pw.get("binary_files_not_read"):
        lines.append(f"pre-push: {pw['binary_files_not_read']} binary file(s) checked by path only")
    if doc.get("skip_refused"):
        lines.append(f"pre-push: {doc['skip_refused']}")
    lines += [f"pre-push: earlier, {skip_note(s)}; the next review names it" for s in doc.get("skips", [])]
    return "\n".join(lines + [f"pre-push: {doc['outcome']}, {len(doc['refs'])} ref(s)"])


def request_summary(doc):
    line = f"request-review: {doc['outcome']} for {doc['head'][:12]}"
    if "reason" in doc:
        return f"{line}: {doc['reason']}"
    parts = [f"{c['name']}: {c['detail']}" for c in doc.get("checks", []) if c["status"] == "failed"]
    req = doc.get("request")
    if req:
        parts.append(f"{req['label']} " + {True: "added", False: "not added"}.get(req["added"], "may or may not be on")
                     + (f" ({req['detail']})" if req.get("detail") else ""))
    if doc.get("review"):
        parts.append(f"{doc['review']['verdict']} at {doc['review']['url']}")
    if doc.get("error"):
        parts.append(f"{doc['error']['kind']} error: {doc['error']['message']}")
    if doc.get("last_error") and not doc.get("review"):
        parts.append(doc["last_error"])
    if doc.get("reviews", {}).get("summary"):
        parts.append(doc["reviews"]["summary"])
    if doc.get("reviewed_label", {}).get("on"):
        parts.append(f"{doc['reviewed_label']['name']} is on: remove it by hand")
    return line + (": " + "; ".join(parts) if parts else "")


def summary(command, doc):
    if command == "request-review" and "head" in doc:
        return request_summary(doc)
    if "error" in doc and command != "merge-check":
        return f"{command}: {doc['error']['kind']} error: {doc['error']['message']}"
    if command == "merge-check":
        return f"merge-check: {doc['verdict']}" + (f": {doc['error']['message']}" if "error" in doc else "")
    if command == "check-links":
        return (f"check-links: {doc['outcome']}, {doc['sources']} source(s)"
                + "".join(f"; a session link in {h['source']} line {h['line']}" for h in doc["hits"])
                + "".join(f"; {skip_note(s)}" for s in doc.get("skips", [])))
    if command == "check-words":
        if doc["outcome"] == "off":
            return f"check-words: {doc['note']}"
        return (f"check-words: {doc['outcome']}, {len(doc['hits'])} hit(s)"
                + "".join(f"; a private word in {hit_text(h)}" for h in doc["hits"])
                + (f"; {doc['binary_files_not_read']} binary file(s) checked by path only"
                   if doc.get("binary_files_not_read") else ""))
    if command == "record-review":
        r = doc["record"]
        return f"record-review: recorded {r['level']} {r['verdict']} for {r['head']}"
    if command in ("install-hook", "uninstall-hook"):
        return f"{command}: {doc['outcome']}: {doc['hook']}"
    if command == "pre-push":
        return pre_push_summary(doc)
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


class Scrubbed:
    """A text stream that withholds every private word written to it."""

    def __init__(self, stream, words):
        self.stream, self.words = stream, words

    def write(self, text):
        return self.stream.write(self.words.scrub(text))

    def flush(self):
        getattr(self.stream, "flush", lambda: None)()


def main(argv=None, env=None, stdout=None, stderr=None, opener=None, sleep=time.sleep, clock=time.time, run=subprocess.run,
         stdin=None):
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
    # The last net: whatever these commands print passes through the private-word file's scrub,
    # whatever path the text took. The checks themselves report positions, never text.
    if command in LOCAL + ("post-review",):
        try:
            net = private_words(env)[0]
        except vd.UsageError:
            net = Words([])
        stdout, stderr = Scrubbed(stdout, net), Scrubbed(stderr, net)
    token = None
    try:
        try:
            args = build_parser().parse_args(argv)
        except SystemExit as e:  # --help
            return EXIT_USAGE if e.code else 0
        if command == "check-words" and args.words:
            try:
                chosen = read_words(args.words, "private-word file")
            except vd.UsageError:
                chosen = Words([])
            stdout.words = stderr.words = stdout.words + chosen
        if command in LOCAL:
            try:
                if command == "check-links":
                    if not args.commits and not args.file:
                        raise vd.UsageError("check-links needs --commits RANGE or --file FILE")
                    doc, code = check_links(args, run)
                elif command == "check-words":
                    if not args.commits and not args.tree:
                        raise vd.UsageError("check-words needs --commits RANGE or --tree [DIR]")
                    doc, code = check_words(args, env, run)
                elif command == "record-review":
                    doc, code = record_review(args, run)
                elif command == "pre-push":
                    doc, code = pre_push(args, env, run, stdin or sys.stdin)
                else:
                    doc, code = (install_hook if command == "install-hook" else uninstall_hook)(args, run)
            except Refused as e:
                doc, code = {"outcome": "refused", "reason": str(e)}, EXIT_NOT_READY
            vd.emit(doc, stdout, None)
            stderr.write((f"{command}: refused: {doc['reason']}" if "reason" in doc else summary(command, doc)) + "\n")
            return code
        kind, api_base, owner, repo, number, token = resolve(args, env, run)
        if command in ("wait-review", "request-review"):
            check_wait_args(args, kind)
        forge = vd.make_forge(token, kind, api_base, owner, repo, opener, sleep)
        if command == "merge-check":
            doc, code = vd.deliver(args, forge, kind, owner, repo, number)
        elif command == "gates":
            doc, code = gates(args, forge, kind, owner, repo, number)
        elif command == "post-review":
            doc, code = post_review(args, env, api_base, forge, kind, owner, repo, number, token, opener)
        elif command == "request-review":
            doc, code = request_review(args, forge, kind, owner, repo, number, token, opener, clock, sleep)
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
