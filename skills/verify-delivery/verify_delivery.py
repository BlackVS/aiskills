#!/usr/bin/env python3
"""Confirm from the forge that a pull request was delivered: reviewed at its
final head, merged (by a person, by default), merged content equal to the
reviewed content, and CI green on the merge commit.

Read-only: every request is an HTTP GET. It never merges, comments, labels or
pushes. Standard library only.

    GITHUB_TOKEN_FILE=/path/to/token python3 verify_delivery.py --pr https://github.com/OWNER/REPO/pull/7
    GITEA_TOKEN_FILE=/path/to/token  python3 verify_delivery.py --pr https://git.example.org/OWNER/REPO/pulls/7 \\
        --api-base https://git.example.org/api/v1

Prints one JSON document on stdout. Exit codes: 0 confirmed, 3 not confirmed,
4 pending or retryable (the API was unavailable), 2 usage error. See SKILL.md.
"""
import argparse, datetime, hashlib, json, os, re, socket, sys, time, urllib.error, urllib.parse, urllib.request

EXIT_CONFIRMED, EXIT_USAGE, EXIT_NOT_CONFIRMED, EXIT_PENDING = 0, 2, 3, 4
TIMEOUT = 20        # seconds per request
MAX_PAGES = 20      # per listing; 20 pages of 50-100 items is far past any real pull request
MAX_REF = 512       # bytes per evidence ref
MAX_EVIDENCE = 16   # evidence entries
MAX_DIFF = 8 << 20  # bytes per diff read for the patch identity; a larger one is not read (pending)
MAX_JSON = 32 << 20  # bytes per JSON response; a larger one is not read (pending)
MAX_ERROR = 64 << 10  # bytes read from an HTTP error response's body; the rest is never read
SHA = r"(?P<sha>[0-9a-fA-F]{7,40})"

# The review comment format of the oh-code-review skill (references/dispositions.md):
# "[<reviewer> review] reviewed at head <sha>", then a VERDICT line.
DEFAULT_REVIEWS = [
    ("local", rf"(?m)^\[oh-code-review \((?:high|max|ultra)\) review\] reviewed at head {SHA}\b"),
    ("external", rf"(?m)^\[(?!oh-code-review\b)[^\]\n]+ review\] reviewed at head {SHA}\b"),
]
DEFAULT_VERDICT = r"(?m)^[ \t]*VERDICT[ \t]*\r?\n[ \t]*READY_FOR_HUMAN_MERGE\b"
GITHUB_PASS = {"success", "neutral", "skipped"}
# GitHub author_association values that count as the repository trusting a review's author.
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
GITEA_PASS = {"success"}
GITEA_PENDING = {"pending"}


class UsageError(Exception):
    """The invocation cannot work: fix the arguments, the token or the reference (exit 2)."""


class Unavailable(Exception):
    """The forge did not answer usefully right now; retry later (exit 4)."""


class NotFound(Exception):
    pass


# ---------------------------------------------------------------- HTTP

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would resend the Authorization header to
    wherever it points. The 3xx comes back as an HTTP error instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def default_opener(*handlers):
    return urllib.request.build_opener(NoRedirect(), *handlers).open


class Http:
    """GET-only JSON client. One retry, and only on a clear network error."""

    def __init__(self, token, kind, opener=None, sleep=time.sleep, timeout=TIMEOUT):
        self._token = token
        self._kind = kind
        self._open = opener or default_opener()
        self._sleep = sleep
        self._timeout = timeout

    def get(self, url, accept=None, limit=None):
        """(status, headers, body). At most limit + 1 bytes of a body are read, and at most
        MAX_ERROR bytes of an error response's body, so no response is read without a bound."""
        limit = MAX_JSON if limit is None else limit
        headers = {"Accept": "application/json", "User-Agent": "aiskills-verify-delivery"}
        if self._kind == "github":
            headers["Accept"] = "application/vnd.github+json"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        if accept:
            headers["Accept"] = accept
        if self._token:
            headers["Authorization"] = ("Bearer " if self._kind == "github" else "token ") + self._token
        for attempt in (1, 2):
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with self._open(req, timeout=self._timeout) as resp:
                    body = resp.read(limit + 1)
                    return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body
            except urllib.error.HTTPError as e:
                hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
                return e.code, hdrs, e.read(MAX_ERROR) if e.fp else b""
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
                if attempt == 2:
                    reason = getattr(e, "reason", e)
                    raise Unavailable(f"network error after one retry: {type(reason).__name__}")
                self._sleep(1)


class Forge:
    """The few read endpoints this check needs, for GitHub and Gitea."""

    def __init__(self, http, kind, api_base, owner, repo):
        self.http, self.kind = http, kind
        self.api = api_base.rstrip("/")
        self.origin = urllib.parse.urlsplit(self.api)[:2]
        self.repo_path = f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo, safe='')}"

    def _fetch(self, url, accept=None, limit=None):
        limit = MAX_JSON if limit is None else limit
        status, headers, body = self.http.get(url, accept, limit) if accept else self.http.get(url)
        if status == 200 and len(body) > limit:
            raise Unavailable(f"the response is larger than {limit} bytes")
        if status == 200 and accept:
            return body, headers  # a diff stays bytes: its identity is computed without decoding
        if status == 200:
            try:
                return json.loads(body.decode("utf-8")), headers
            except ValueError:
                raise Unavailable("the forge returned a response that is not JSON")
        if status == 404:
            raise NotFound(url)
        if status == 401:
            raise UsageError("the forge rejected the token (HTTP 401)")
        if status == 429 or (status == 403 and headers.get("x-ratelimit-remaining") == "0"):
            raise Unavailable(f"rate limited (HTTP {status})")
        if status == 403:
            raise UsageError("the token may not read this repository (HTTP 403)")
        if 300 <= status < 400:
            raise UsageError(f"the forge redirected the request (HTTP {status}); redirects are not followed, "
                             "so give the API base and repository the forge uses now")
        if status >= 500:
            raise Unavailable(f"the forge answered HTTP {status}")
        raise Unavailable(f"unexpected HTTP {status}")

    def get(self, path):
        return self._fetch(self.api + self.repo_path + path)[0]

    def diff(self, path):
        """A diff as raw bytes, from GitHub's compare API (diff media type) or a Gitea .diff endpoint."""
        accept = "application/vnd.github.diff" if self.kind == "github" else "text/plain"
        return self._fetch(self.api + self.repo_path + path, accept=accept, limit=MAX_DIFF)[0]

    def get_or_none(self, path):
        try:
            return self.get(path)
        except NotFound:
            return None

    def pages(self, path, key=None):
        """A listing across pages. Follows Link rel="next" only on the API's own host,
        so the token is never sent anywhere else. A next page that cannot be followed
        is an error: a partial listing could hide a later review or a failed check."""
        sep = "&" if "?" in path else "?"
        url = self.api + self.repo_path + path + sep + ("per_page=100" if self.kind == "github" else "limit=50")
        items = []
        for _ in range(MAX_PAGES):
            data, headers = self._fetch(url)
            items.extend(data.get(key, []) if key else data)
            nxt = next_link(headers.get("link", ""))
            if not nxt:
                return items
            if urllib.parse.urlsplit(nxt)[:2] != self.origin:
                raise UsageError("the forge's next-page link points to another host, so the listing "
                                 "cannot be read in full; give the API base the forge itself uses")
            url = nxt
        raise Unavailable(f"more than {MAX_PAGES} pages of {path}")


def next_link(header):
    for part in header.split(","):
        m = re.match(r'\s*<([^>]+)>\s*;\s*rel="?next"?', part)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------- reference parsing

def parse_ref(ref, number, forge, api_base):
    """(kind, api_base, owner, repo, number) from a PR URL, or owner/repo plus a number."""
    m = re.match(r"^(https?)://([^/]+)(/.*)?/([^/]+)/([^/]+)/(pull|pulls)/(\d+)(?:/[^?#]*)?(?:[?#].*)?$", ref or "")
    if m:
        scheme, host, prefix, owner, repo, word, num = m.groups()
        kind = forge or ("github" if word == "pull" else "gitea")
        if kind == "github" and not api_base:
            api_base = "https://api.github.com" if host == "github.com" else f"{scheme}://{host}/api/v3"
        return kind, api_base, owner, repo, int(num)
    m = re.match(r"^([\w.-]+)/([\w.-]+)(?:#(\d+))?$", ref or "")
    if m:
        owner, repo, num = m.groups()
        num = int(num) if num else number
        if not num:
            raise UsageError("give the pull request number: owner/repo#N or --number N")
        kind = forge or "github"
        if kind == "github" and not api_base:
            api_base = "https://api.github.com"
        return kind, api_base, owner, repo, num
    raise UsageError("--pr must be a pull request URL, owner/repo#N, or owner/repo with --number")


# ---------------------------------------------------------------- the checks

def when(stamp):
    """An API timestamp as an aware datetime (GitHub writes Z, Gitea an offset); None if absent."""
    if not stamp:
        return None
    try:
        return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def author_verdict(c, name, kind, authors, trust_any):
    """None when the comment's author may give review NAME, else the reason it may not."""
    if trust_any:
        return None
    if name in authors:
        # Logins are case-insensitive on both forges.
        return None if (c["author"] or "").lower() in authors[name] else "author not allowed"
    if kind == "github":
        assoc = c.get("association") or "missing"
        return None if assoc in TRUSTED_ASSOCIATIONS else f"author association {assoc} not trusted"
    return "no author allowlist for this review (required on Gitea)"


def check_reviews(items, head, merged_at, specs, verdict_re, kind="github", authors=None, trust_any=False):
    """Each configured review must be READY_FOR_HUMAN_MERGE in the latest comment by an author the
    repository trusts, posted before the merge. The latest one naming the final head counts; with
    none, the latest one naming an older head does, and the merged-content check then requires
    the merged change to be that head's change (a base-only update keeps a review valid).
    The author is checked first: an untrusted comment never counts and never displaces one."""
    authors = authors or {}
    reviews, ignored = [], []
    for name, pattern in specs:
        rule = ("disabled" if trust_any else "allowlist" if name in authors
                else "association" if kind == "github" else "allowlist required")
        latest, older = None, []
        epoch = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        for c in sorted(items, key=lambda c: when(c["created_at"]) or epoch):
            m = pattern.search(c["body"] or "")
            if not m:
                continue
            sha = (m.group("sha") or "").lower()
            untrusted = author_verdict(c, name, kind, authors, trust_any)
            if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
                ignored.append({"review": name, "reason": "does not name a commit SHA (7 to 40 hex digits)", "url": c["url"]})
            elif untrusted:
                ignored.append({"review": name, "reason": untrusted, "author": c["author"], "sha": sha, "url": c["url"]})
            elif when(merged_at) and when(c["created_at"]) and when(c["created_at"]) > when(merged_at):
                ignored.append({"review": name, "reason": "posted after the merge", "sha": sha, "url": c["url"]})
            elif when(merged_at) and when(c.get("updated_at")) and when(c["updated_at"]) > when(merged_at):
                ignored.append({"review": name, "reason": "edited after the merge", "sha": sha, "url": c["url"]})
            elif head.startswith(sha):
                latest = (c, sha)
            else:
                older.append((c, sha))
        at_final = latest is not None
        if not at_final and older:
            latest = older.pop()
        for c, sha in older:
            ignored.append({"review": name, "sha": sha, "url": c["url"],
                            "reason": "names an older or different head" if at_final else "superseded by a later review"})
        if rule == "allowlist required":
            reviews.append({"review": name, "status": "unsatisfiable", "author_rule": rule,
                            "detail": "an author allowlist is required on Gitea: give --review-author "
                                      f"{name}=LOGIN (or --trust-any-author where only trusted accounts can comment)"})
            continue
        if latest is None:
            reviews.append({"review": name, "status": "missing", "author_rule": rule,
                            "detail": "no review by a trusted author names a head of this pull request"})
            continue
        c, sha = latest
        ready = bool(verdict_re.search(c["body"] or ""))
        reviews.append({"review": name, "status": "ready" if ready else "not_ready", "author_rule": rule, "sha": sha,
                        "at_final_head": at_final, "url": c["url"], "author": c["author"], "created_at": c["created_at"]})
    ready = sum(r["status"] == "ready" for r in reviews)
    return ready, reviews, ignored


def account_type(user, kind, bots):
    """"User" or "Bot", and the basis for saying so."""
    login = user.get("login") or ""
    if login in bots:
        return "Bot", "listed with --bot-account"
    if login.endswith("[bot]"):
        return "Bot", "login ends with [bot]"
    if kind == "github":
        return user.get("type") or "unknown", "GitHub account type"
    if (user.get("id") or 0) < 0:
        return "Bot", "Gitea system account (negative id)"
    return "User", "Gitea has no bot flag; not listed with --bot-account"


def ci_results(kind, merge_sha, forge, flaky):
    """[{name, id, url, state, conclusion, result}] for the merge commit, result being
    passed / pending / failed / "failed: known flaky"."""
    runs = []
    if kind == "github":
        for r in forge.pages(f"/commits/{merge_sha}/check-runs?filter=latest", key="check_runs"):
            done = r.get("status") == "completed"
            ok = done and r.get("conclusion") in GITHUB_PASS
            runs.append({"name": r.get("name"), "id": r.get("id"), "url": r.get("html_url") or "",
                         "state": r.get("status"), "conclusion": r.get("conclusion"),
                         "result": "passed" if ok else "pending" if not done else "failed"})
    else:
        for s in forge.pages(f"/commits/{merge_sha}/status", key="statuses"):
            state = s.get("status") or s.get("state")
            runs.append({"name": s.get("context"), "id": s.get("id"), "url": s.get("target_url") or "",
                         "state": state, "conclusion": state,
                         "result": "passed" if state in GITEA_PASS else "pending" if state in GITEA_PENDING else "failed"})
    for r in runs:
        if r["result"] == "failed" and r["name"] in flaky:
            r["result"] = "failed: known flaky"
    return runs


def ci_evidence(runs, fallback):
    """The CI run URLs, one per run (a GitHub job URL is cut back to its run); the merge
    commit's own page when they would not fit the evidence limits."""
    urls = []
    for r in runs:
        u = re.sub(r"/job/\d+$", "", r["url"] or "")
        if u and u not in urls:
            urls.append(u)
    if not urls or len(urls) > MAX_EVIDENCE - 2 or any(len(u.encode()) > MAX_REF for u in urls):
        return [fallback]
    return urls


def verify(args, forge, kind, number):
    checks = []
    pr = forge.get_or_none(f"/pulls/{number}")
    if pr is None:
        raise UsageError(f"pull request {number} was not found (or the token cannot see it)")
    head = (pr.get("head") or {}).get("sha", "").lower()
    merged = bool(pr.get("merged"))
    merged_at = pr.get("merged_at")
    merge_sha = (pr.get("merge_commit_sha") or "").lower()
    pr_url = pr.get("html_url") or ""

    # 1. reviewed head
    items = [{"body": c.get("body"), "created_at": c.get("created_at"), "updated_at": c.get("updated_at"),
              "url": c.get("html_url"), "author": (c.get("user") or {}).get("login"),
              "association": c.get("author_association")}
             for c in forge.pages(f"/issues/{number}/comments")]
    items += [{"body": r.get("body"), "created_at": r.get("submitted_at"), "url": r.get("html_url"),
               "author": (r.get("user") or {}).get("login"), "association": r.get("author_association")}
              for r in forge.pages(f"/pulls/{number}/reviews")]
    head_commit = (forge.get_or_none(f"/git/commits/{head}") if head else None) or {}
    head_url = head_commit.get("html_url") or ""
    ready, reviews, ignored = check_reviews(items, head, merged_at, args.reviews, args.verdict,
                                            kind, args.review_authors, args.trust_any_author)
    unsatisfiable = any(r["status"] == "unsatisfiable" for r in reviews)
    status = ("passed" if ready >= args.required_reviews else "failed" if unsatisfiable
              else "pending" if not merged and pr.get("state") == "open" else "failed")
    reviewed_heads = sorted({head if r["at_final_head"] else r["sha"] for r in reviews if r["status"] == "ready"})
    checks.append({"name": "reviewed_head", "status": status, "head_sha": head, "head_url": head_url,
                   "reviewed_heads": reviewed_heads,
                   "author_check": "disabled" if args.trust_any_author else "enabled",
                   "required": args.required_reviews, "ready": ready, "reviews": reviews, "ignored": ignored})

    # 2. merge
    user = pr.get("merged_by") or {}
    merge = {"name": "merge", "merged": merged, "merged_at": merged_at, "merge_commit_sha": merge_sha or None,
             "pr_url": pr_url, "merged_by": user.get("login")}
    if not merged:
        merge["status"] = "pending" if pr.get("state") == "open" else "failed"
        merge["detail"] = "not merged yet" if merge["status"] == "pending" else "closed without merging"
    else:
        acct, basis = account_type(user, kind, args.bot_accounts)
        merge.update(account_type=acct, account_type_basis=basis)
        problems = []
        if not merge_sha:
            problems.append("the forge reports no merge commit")
        if not user.get("login"):
            problems.append("the forge does not say who merged")
        if acct != "User" and not args.allow_bot_merge:
            problems.append(f"merged by a {acct} account; a person is required")
        if args.mergers and user.get("login") not in args.mergers:
            problems.append("the merging account is not in the --merger allowlist")
        merge["status"] = "failed" if problems else "passed"
        if problems:
            merge["detail"] = "; ".join(problems)
    checks.append(merge)

    if not merged or not merge_sha:
        why = "the pull request is not merged"
        checks.append({"name": "tree_equality", "status": merge["status"], "detail": why})
        checks.append({"name": "post_merge_ci", "status": merge["status"], "detail": why})
        return checks, pr_url, head_url, None, []

    # 3. merged content = reviewed content: equal trees, or else an equal patch identity
    merge_commit = forge.get_or_none(f"/git/commits/{merge_sha}") or {}
    merge_url = merge_commit.get("html_url") or ""
    t_head, t_merge = tree_of(head_commit), tree_of(merge_commit)
    same = {"name": "tree_equality", "head_sha": head, "head_tree": t_head, "merge_commit_sha": merge_sha,
            "merge_commit_url": merge_url, "merge_tree": t_merge}
    results = [merged_matches(kind, forge, number, reviewed, head, t_head, merge_sha, merge_commit, t_merge)
               for reviewed in (reviewed_heads or [head])]
    worst = next((r for status in ("failed", "pending") for r in results if r["status"] == status), None)
    shown = worst or next((r for r in results if r["rule"] == "patch_identity"), results[0])
    same.update(shown, reviewed=results)
    if not worst:
        same["rule"] = "patch_identity" if any(r["rule"] == "patch_identity" for r in results) else "tree_equality"
    checks.append(same)

    # 4. post-merge CI
    runs = ci_results(kind, merge_sha, forge, set(args.known_flaky))
    results = {r["result"] for r in runs}
    ci = {"name": "post_merge_ci", "merge_commit_sha": merge_sha, "runs": runs,
          "known_flaky_failures": [r["name"] for r in runs if r["result"] == "failed: known flaky"]}
    if "failed" in results or "failed: known flaky" in results:
        ci["status"] = "failed"
        if ci["known_flaky_failures"]:
            ci["detail"] = "failed: known flaky checks still count as failures; the delivery is not confirmed"
    elif not runs:
        ci["status"], ci["detail"] = "pending", "no CI results on the merge commit yet"
    elif "pending" in results:
        ci["status"], ci["detail"] = "pending", "CI is still running on the merge commit"
    else:
        ci["status"] = "passed"
    checks.append(ci)
    return checks, pr_url, head_url, merge_url, runs


def merged_matches(kind, forge, number, reviewed, head, t_head, merge_sha, merge_commit, t_merge):
    """Is the merged content the content reviewed at head REVIEWED? Rule tree_equality: the merge
    commit's tree is the reviewed (final) head's tree. Rule patch_identity, when the trees differ
    because the base moved after the review: the merge commit's change against its first parent
    has the same patch identity as the reviewed head's change against its merge base."""
    result = {"reviewed_head": reviewed, "rule": None}
    final = bool(head) and head.startswith(reviewed)
    if final and t_head and t_head == t_merge:
        return {**result, "status": "passed", "rule": "tree_equality"}
    parent = ((merge_commit.get("parents") or [{}])[0] or {}).get("sha")
    if not head or not parent:
        return {**result, "status": "failed",
                "detail": "the trees differ, and the merge commit has no parent to compare its change against"}
    if kind == "github":
        # Three-dot compare diffs from the merge base: of the first parent and the reviewed head,
        # the reviewed change; of the first parent and the merge commit, the merge commit's change.
        paths = (f"/compare/{parent}...{reviewed}", f"/compare/{parent}...{merge_sha}")
    elif final:
        paths = (f"/pulls/{number}.diff", f"/git/commits/{merge_sha}.diff")
    else:
        return {**result, "status": "failed",
                "detail": "the review names an older head, and Gitea's API has no diff of an older head's "
                          "change to compare with the merged change: re-review at the final head"}
    try:
        texts = [forge.diff(path) for path in paths]
    except (Unavailable, NotFound) as e:
        why = str(e) if isinstance(e, Unavailable) else "the forge did not find it"
        return {**result, "status": "pending",
                "detail": f"the trees differ, and a diff for the patch identity could not be read ({why})"}
    try:
        (was_reviewed, files), (merged, _) = (patch_identity(t) for t in texts)
    except ValueError as e:
        return {**result, "status": "failed", "detail": f"the trees differ, and the patch identity cannot be established: {e}"}
    result.update(base_parent_sha=parent,
                  patch_identity={"reviewed_change": was_reviewed, "merge_change": merged, "files": files})
    if was_reviewed == merged:
        result.update(status="passed", rule="patch_identity")
    else:
        result.update(status="failed", detail="the trees differ, and the merge commit's change is not the reviewed change")
    return result


def patch_identity(diff):
    """(digest, number of files) of a unified git diff, the same function for both forges.

    Kept, in order: each "diff --git" line, the mode, new/deleted file and rename/copy lines,
    and every hunk line: context, added, removed and "\\ No newline". Dropped: index lines,
    the ---/+++ lines, hunk headers with their line numbers, similarity scores and blank
    separator lines. Only CRLF line endings are normalised; the bytes are hashed as they are, never
    decoded, so text that is not UTF-8 keeps every byte. A change carried onto a newer base
    keeps its digest only when its hunks, context included, are unchanged; any changed line
    or context changes it. A binary or empty change has no identity (ValueError)."""
    if isinstance(diff, str):
        diff = diff.encode("utf-8")
    kept, files, in_hunk = [], 0, False
    for line in diff.replace(b"\r\n", b"\n").split(b"\n"):
        if line.startswith(b"diff --git "):
            files, in_hunk = files + 1, False
            kept.append(line)
        elif not files or not line:
            continue
        elif line.startswith(b"@@"):
            in_hunk = True
        elif in_hunk and line[:1] in (b" ", b"+", b"-", b"\\"):
            kept.append(line)
        elif not line.strip():
            continue
        elif line.startswith((b"Binary files ", b"GIT binary patch")):
            raise ValueError("the change includes a binary file")
        elif not line.startswith((b"index ", b"--- ", b"+++ ", b"similarity index ", b"dissimilarity index ")):
            kept.append(line)
    if not files:
        raise ValueError("the change is empty")
    return hashlib.sha256(b"\n".join(kept)).hexdigest(), files


def tree_of(commit):
    """The tree SHA: top level on GitHub's git/commits, under "commit" on Gitea's."""
    tree = commit.get("tree") or (commit.get("commit") or {}).get("tree") or {}
    return (tree.get("sha") or "").lower() or None


def evidence_for(pr_url, head_url, merge_url, runs):
    ev = [{"kind": "reviewed_head", "ref": head_url}, {"kind": "human_merge", "ref": pr_url}]
    ev += [{"kind": "post_merge_ci", "ref": u} for u in ci_evidence(runs, merge_url)]
    if len(ev) > MAX_EVIDENCE or any(not e["ref"] or len(e["ref"].encode()) > MAX_REF for e in ev):
        return None
    return ev


# ---------------------------------------------------------------- CLI

def build_parser():
    p = ArgParser(prog="verify_delivery.py", description=__doc__.split("\n\n")[0], allow_abbrev=False)
    p.add_argument("--pr", required=True, help="pull request URL, owner/repo#N, or owner/repo with --number")
    p.add_argument("--number", type=int, help="pull request number when --pr is owner/repo")
    p.add_argument("--forge", choices=("github", "gitea"), help="forge type (default: from the URL; github for owner/repo)")
    p.add_argument("--api-base", help="REST API base, e.g. https://git.example.org/api/v1 (required for Gitea)")
    p.add_argument("--token-env", help="environment variable naming the token FILE (default GITHUB_TOKEN_FILE or GITEA_TOKEN_FILE)")
    p.add_argument("--anonymous", action="store_true", help="no token (public repositories; low rate limits)")
    p.add_argument("--review", action="append", default=[], metavar="NAME=REGEX",
                   help="a required review: a regex with a (?P<sha>...) group; replaces the defaults, repeatable")
    p.add_argument("--required-reviews", type=int, help="how many of the reviews must be READY (default: all)")
    p.add_argument("--verdict-pattern", default=DEFAULT_VERDICT, help="regex a review must match to count as READY")
    p.add_argument("--review-author", action="append", default=[], metavar="NAME=LOGIN",
                   help="an account whose comments may give review NAME; replaces the default for NAME, repeatable")
    p.add_argument("--trust-any-author", action="store_true",
                   help="accept a review from any author (only where just trusted accounts can comment); reported")
    p.add_argument("--allow-bot-merge", action="store_true", help="accept a merge by a bot or app account")
    p.add_argument("--merger", action="append", default=[], help="allowlist of accounts that may merge, repeatable")
    p.add_argument("--bot-account", action="append", default=[], help="treat this account as a bot, repeatable")
    p.add_argument("--known-flaky", action="append", default=[], help="check/status name to report as known flaky, repeatable")
    return p


class ArgParser(argparse.ArgumentParser):
    """Argument errors become a UsageError, so they too produce the JSON document. The message
    never repeats what was typed: a token pasted onto the command line must not be printed."""

    def error(self, message):
        if message.startswith("unrecognized arguments"):
            message = "unrecognized arguments (a token is never passed as an argument; see --token-env)"
        raise UsageError(re.sub(r"(invalid \w+ value|invalid choice): .*", r"\1", message))


def resolve(args, env):
    specs = []
    for item in args.review or []:
        name, sep, rx = item.partition("=")
        if not sep or not name or not rx:
            raise UsageError("--review takes NAME=REGEX")
        specs.append((name, rx))
    specs = specs or DEFAULT_REVIEWS
    compiled = []
    for name, rx in specs:
        try:
            pat = re.compile(rx)
        except re.error:
            raise UsageError("a --review pattern is not a valid regular expression")
        if "sha" not in pat.groupindex:
            raise UsageError("a --review pattern needs a (?P<sha>...) group")
        compiled.append((name, pat))
    args.reviews = compiled
    names = {name for name, _ in compiled}
    args.review_authors = {}
    for item in args.review_author:
        name, sep, login = item.partition("=")
        if not sep or not name or not login.strip():
            raise UsageError("--review-author takes NAME=LOGIN")
        if name not in names:
            raise UsageError("--review-author names a review that is not configured (see --review)")
        args.review_authors.setdefault(name, set()).add(login.strip().lower())
    if args.trust_any_author and args.review_authors:
        raise UsageError("give either --review-author or --trust-any-author, not both")
    args.required_reviews = len(compiled) if args.required_reviews is None else args.required_reviews
    if not 1 <= args.required_reviews <= len(compiled):
        raise UsageError(f"--required-reviews must be between 1 and {len(compiled)}")
    try:
        args.verdict = re.compile(args.verdict_pattern)
    except re.error:
        raise UsageError("--verdict-pattern is not a valid regular expression")
    args.bot_accounts = set(args.bot_account)
    args.mergers = set(args.merger)
    kind, api_base, owner, repo, number = parse_ref(args.pr, args.number, args.forge, args.api_base)
    if not api_base:
        raise UsageError("--api-base is required for Gitea (for example https://git.example.org/api/v1)")
    if not api_base.startswith(("https://", "http://")):
        raise UsageError("--api-base must be an http(s) URL")
    if api_base.startswith("http://") and not args.anonymous:
        raise UsageError("a token is only sent over https: use an https --api-base (or --anonymous)")
    token = None
    if not args.anonymous:
        var = args.token_env or ("GITHUB_TOKEN_FILE" if kind == "github" else "GITEA_TOKEN_FILE")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var):
            raise UsageError("--token-env takes the NAME of an environment variable, not a token")
        # A token can look like a variable name, so a --token-env value is never repeated.
        shown = "the variable named by --token-env" if args.token_env else var
        path = env.get(var)
        if not path:
            raise UsageError(f"set {shown} to the path of a file holding a read-only token (or pass --anonymous)")
        try:
            with open(path, encoding="utf-8") as f:
                token = f.read().strip()
        except OSError:
            raise UsageError(f"the token file named by {shown} cannot be read")
        if not token:
            raise UsageError(f"the token file named by {shown} is empty")
    return kind, api_base, owner, repo, number, token


def emit(doc, stdout, token):
    text = json.dumps(doc, indent=2, sort_keys=False)
    if token and len(token) >= 8:
        text = text.replace(token, "***")  # defence in depth: nothing above ever includes it
    stdout.write(text + "\n")


def main(argv=None, env=None, stdout=None, stderr=None, opener=None, sleep=time.sleep):
    env = os.environ if env is None else env
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    token = None
    try:
        try:
            args = build_parser().parse_args(argv)
        except SystemExit as e:  # --help
            return EXIT_USAGE if e.code else 0
        kind, api_base, owner, repo, number, token = resolve(args, env)
        forge = Forge(Http(token, kind, opener=opener, sleep=sleep), kind, api_base, owner, repo)
        checks, pr_url, head_url, merge_url, runs = verify(args, forge, kind, number)
    except UsageError as e:
        emit({"verdict": "not_confirmed", "error": {"kind": "usage", "message": str(e)}, "checks": []}, stdout, token)
        return EXIT_USAGE
    except Unavailable as e:
        emit({"verdict": "pending", "error": {"kind": "unavailable", "message": str(e)}, "checks": []}, stdout, token)
        return EXIT_PENDING
    except NotFound:
        emit({"verdict": "not_confirmed", "error": {"kind": "not_found", "message": "a record the pull request refers to was not found"},
              "checks": []}, stdout, token)
        return EXIT_NOT_CONFIRMED

    statuses = {c["status"] for c in checks}
    verdict = "not_confirmed" if "failed" in statuses else "pending" if "pending" in statuses else "confirmed"
    doc = {"verdict": verdict, "pr": {"forge": kind, "repo": f"{owner}/{repo}", "number": number, "url": pr_url},
           "checks": checks}
    if verdict == "confirmed":
        evidence = evidence_for(pr_url, head_url, merge_url, runs)
        if evidence is None:
            verdict = doc["verdict"] = "not_confirmed"
            doc["error"] = {"kind": "evidence", "message": "the evidence would not fit its limits (16 entries, 512 bytes each)"}
        else:
            doc["evidence"] = evidence
    emit(doc, stdout, token)
    return {"confirmed": EXIT_CONFIRMED, "pending": EXIT_PENDING}.get(verdict, EXIT_NOT_CONFIRMED)


if __name__ == "__main__":
    sys.exit(main())
