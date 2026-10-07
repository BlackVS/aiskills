#!/usr/bin/env python3
"""GitHub label poller -> OpenHands review conversation.

Same lifecycle as review_hook.py (the Gitea receiver), but GitHub cannot reach
this host, so instead of a webhook the poller ASKS GitHub every POLL_SECONDS
for open PRs carrying L_REQUEST:

  label L_REQUEST on a PR ->
  poller swaps it to L_WORKING and starts a review conversation ->
  watcher thread waits for the posted "<MARKER>" comment ->
  swaps L_WORKING to L_DONE.

Re-review after fixes: add L_REQUEST again. The comment is posted by the
PAT's owner (GitHub has no bot user here), so completion is detected by the
marker alone, not by author.
A service restart is safe mid-run: each run is recorded on disk once its
conversation exists, and the next start re-attaches a watcher to it.

Which model reviews is an OpenHands **agent profile** (Canvas
`/api/agent-profiles`, each pinning its backend + model): REVIEW_PROFILE is
the default, and a request label of the form "<L_REQUEST>:<profile>" (e.g.
`review-this:codex-sol`) picks another profile for that PR only.

Site configuration comes from the environment (systemd EnvironmentFile);
GITHUB_TOKEN_FILE is required; the rest have defaults for a typical `hands`
host:

  GITHUB_TOKEN_FILE  (required) path of the file holding a fine-grained PAT
                     (needs Contents R, Pull requests R/W, Issues R/W,
                     Metadata R on the watched repos)
  GITHUB_API         default https://api.github.com
  GITHUB_OWNER       account/org whose PRs are watched, default BlackVS
  GITHUB_REPOS       optional comma-separated "owner/repo" allow-list; when
                     set, only these repos are watched (else every repo of
                     GITHUB_OWNER the token can see)
  POLL_SECONDS       default 120
  OPENHANDS_API / LOCAL_BACKEND_API_KEY / REVIEW_PROFILE / PROFILES_DIR /
  WORKSPACES_DIR / LABEL_REQUEST / LABEL_WORKING / LABEL_DONE / WATCH_MINUTES
                     as in review_hook.py
  REVIEW_RUNS_DIR    directory of review-runs-github.json, the in-flight runs
                     a restart re-attaches to, default /opt/openhands/hooks
  MARKER             default "[hands-bot review]" - what the posted comment
                     must START with
  REVIEW_AUTHOR      the GitHub login that posts the review comments, default
                     the token's owner (GET /user); a repeated request on an
                     unchanged patch keeps only a review by this login. Set it
                     when the token has no user (a GitHub App installation
                     token answers 403 there): otherwise every request is
                     reviewed in full
  PROMPT_FILE        default /opt/openhands/hooks/github_review_prompt.txt
                     (placeholders: {repo} {num} {title!r} {marker} {profile}
                     {label})

GitHub labels are per-repo and adding by name silently 404s when the label
does not exist, so the poller creates what it needs on first use AND, for
every repo with an open PR in scope, the full request-label set
(L_REQUEST plus one variant per agent profile) so requesters can pick a
reviewer without creating labels themselves.
"""
import json
import re
from review_policy import requested_profiles
from review_runner import recovered_head, next_page, patch_identity, retried
from review_runner import Runner, RunStore
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = (
    open(os.environ["GITHUB_TOKEN_FILE"])
    .read()
    .strip()
)
GITHUB = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
OWNER = os.environ.get("GITHUB_OWNER", "BlackVS")
REPOS = [r.strip() for r in os.environ.get("GITHUB_REPOS", "").split(",") if r.strip()]
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))
APP = os.environ.get("OPENHANDS_API", "http://127.0.0.1:8000")
APP_KEY = os.environ.get("LOCAL_BACKEND_API_KEY", "")
DEFAULT_PROFILE = os.environ.get("REVIEW_PROFILE", "codex-astra")
PROFILES_DIR = os.environ.get(
    "PROFILES_DIR", "/opt/openhands/canvas-state/agent-profiles"
)
WORKSPACES_DIR = os.environ.get("WORKSPACES_DIR", "/projects/reviews")
L_REQUEST = os.environ.get("LABEL_REQUEST", "review-this")
L_WORKING = os.environ.get("LABEL_WORKING", "hands-reviewing")
L_DONE = os.environ.get("LABEL_DONE", "hands-reviewed")
WATCH_SECONDS = int(os.environ.get("WATCH_MINUTES", "45")) * 60
MARKER = os.environ.get("MARKER", "[hands-bot review]")
PROMPT_FILE = os.environ.get("PROMPT_FILE", "/opt/openhands/hooks/github_review_prompt.txt")
REVIEW_AUTHOR = os.environ.get("REVIEW_AUTHOR", "").strip()
RUNS = RunStore(os.path.join(os.environ.get("REVIEW_RUNS_DIR", "/opt/openhands/hooks"), "review-runs-github.json"))

# Same palette as the Gitea org labels, so the two hosts look alike.
LABEL_COLORS = {L_REQUEST: "0e8a16", L_WORKING: "fbca04", L_DONE: "5319e7"}

in_flight = set()
lock = threading.Lock()
labeled_repos = set()


def log(msg):
    print(msg, flush=True)


def api(path, method="GET", data=None):
    """One forge call. A paged list answer (Link: rel="next", 30 comments per
    page by default) is followed to its end, so callers always see the whole list."""
    url, result = GITHUB + path, None
    for _ in range(100):  # a page bound, never reached in practice
        req = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": "Bearer " + TOKEN,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        def fetch(req=req):
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read(), r.headers.get("Link")
        # A read is tried again after a transient error (a brief forge outage); a write is
        # not, since one that timed out may still have happened.
        b, link = retried(fetch) if method == "GET" else fetch()
        page = json.loads(b) if b else None
        url = next_page(link, GITHUB) if method == "GET" and isinstance(page, list) else None
        result = page if result is None else result + page
        if not url:
            return result
    return result


def change_identity(repo, base, head):
    """The patch identity of the change at `head`: GitHub's three-dot compare from the merge
    base with `base`, as a raw diff (the same diff verify-delivery hashes)."""
    req = urllib.request.Request(
        f"{GITHUB}/repos/{repo}/compare/{urllib.parse.quote(base, safe='/')}...{head}",
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Accept": "application/vnd.github.diff",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return patch_identity(r.read())[0]


_token_owner = []


def reviewer_login():
    """The login whose review comments the patch check trusts: REVIEW_AUTHOR, else the
    token's owner, read once (GET /user). A failed read raises: that request is reviewed."""
    if REVIEW_AUTHOR:
        return REVIEW_AUTHOR
    if not _token_owner:
        _token_owner.append(api("/user")["login"])
    return _token_owner[0]


def ensure_label(repo, name):
    try:
        api(f"/repos/{repo}/labels/{urllib.parse.quote(name)}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        color = LABEL_COLORS.get(name) or (LABEL_COLORS[L_REQUEST] if name.startswith(L_REQUEST) else "ededed")
        api(f"/repos/{repo}/labels", "POST", {"name": name, "color": color})
        log(f"label {name} created in {repo}")


def ensure_request_labels(repo):
    """Make every request label (plain + one per profile) exist in `repo`."""
    if repo in labeled_repos:
        return
    profiles = sorted(f[:-5] for f in os.listdir(PROFILES_DIR) if f.endswith(".json"))
    for name in [L_REQUEST] + [f"{L_REQUEST}:{p}" for p in profiles]:
        ensure_label(repo, name)
    labeled_repos.add(repo)


def labels_of(repo, num):
    return [l["name"] for l in api(f"/repos/{repo}/issues/{num}/labels")]


def set_label(repo, num, name, present):
    have = labels_of(repo, num)
    if present and name not in have:
        ensure_label(repo, name)
        api(f"/repos/{repo}/issues/{num}/labels", "POST", {"labels": [name]})
    if not present and name in have:
        api(f"/repos/{repo}/issues/{num}/labels/{urllib.parse.quote(name)}", "DELETE")


def request_labels(names):
    return requested_profiles(names, L_REQUEST)


def app_api(path, data=None):
    req = urllib.request.Request(
        APP + path,
        method="POST" if data is not None else "GET",
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "X-Session-API-Key": APP_KEY,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def profile_info(name):
    """(uuid, model label) of the agent profile `name`, read from the Canvas
    state dir - the API's list/get endpoints omit the model fields. Models
    lie about their own identity, so the review signature is stamped from
    here, never self-reported. Raises with the valid names otherwise."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise ValueError("Invalid agent profile")
    try:
        p = json.load(open(f"{PROFILES_DIR}/{name}.json"))
    except FileNotFoundError:
        names = [f[:-5] for f in os.listdir(PROFILES_DIR) if f.endswith(".json")]
        raise ValueError(
            f"unknown agent profile `{name}` (available: {', '.join(sorted(names))})"
        )
    if p.get("agent_kind") == "acp":
        model = p.get("acp_model") or f"{p.get('acp_server')}-default"
    else:
        ref = p.get("llm_profile_ref")
        llm = {x["name"]: x for x in app_api("/api/profiles")["profiles"]}.get(ref, {})
        model = (llm.get("model") or ref or "unknown").split("/", 1)[-1]
    return p["id"], model


def fail_review(repo, num, reason):
    set_label(repo, num, L_WORKING, False)
    api(
        f"/repos/{repo}/issues/{num}/comments",
        "POST",
        {
            "body": f"⚠️ {MARKER} could not run: "
            + reason
            + f"\n\nRe-add the `{L_REQUEST}` label to retry."
        },
    )
    log(f"review failed: {repo}#{num}: {reason}")


def note_review(repo, num, text):
    """A PR comment that is not a review (never starts with the review marker line)."""
    api(f"/repos/{repo}/issues/{num}/comments", "POST", {"body": f"⚠️ {MARKER} note: " + text})
    log(f"review note: {repo}#{num}: {text[:80]}")


def run_review(repo, num, title, label, profile, resume=None):
    try:
        runner = Runner(api, app_api, set_label, profile_info, fail_review,
                        marker=MARKER, bot=None, working=L_WORKING,
                        done=L_DONE, timeout=WATCH_SECONDS,
                        poll=int(os.environ.get('REVIEW_POLL_SECONDS', '30')), runs=RUNS, note=note_review,
                        change_identity=change_identity, reviewer=reviewer_login)
        runner.run(repo, num, title, label, profile, PROMPT_FILE, WORKSPACES_DIR, resume=resume)
    finally:
        with lock:
            in_flight.discard((repo, num))


def resume_runs():
    """On service start, re-attach a watcher to every run recorded before the
    restart; its conversation kept running in Canvas meanwhile. Returns the
    PRs resumed: their runs label them, so restart recovery leaves them alone
    even once a run has ended (one holding a posted review ends at once)."""
    resumed = set()
    for record in RUNS.load():
        repo, num = record["repo"], record["num"]
        resumed.add((repo, num))
        with lock:
            in_flight.add((repo, num))
        log(f"review resume: {repo}#{num} conversation={record['conversation']}")
        threading.Thread(target=run_review,
                         args=(repo, num, record.get("title", ""), record["label"], record["profile"], record),
                         daemon=True).start()
    return resumed

def search_prs(label=None):
    """Open PRs in the watched scope (carrying `label` if given):
    [(repo, number, title, [label names])]."""
    scope = " ".join(f"repo:{r}" for r in REPOS) if REPOS else f"user:{OWNER}"
    want = f' label:"{label}"' if label else ""
    q = urllib.parse.quote(f"is:pr is:open{want} {scope}")
    res = api(f"/search/issues?q={q}&per_page=100")
    return [
        (
            i["repository_url"].split("/repos/")[1],
            i["number"],
            i.get("title", ""),
            [l["name"] for l in i.get("labels", [])],
        )
        for i in (res or {}).get("items", [])
    ]


def trigger(repo, num, title):
    key = (repo, num)
    # The search index lags label edits by up to a minute; trust only the
    # live label state, same as the Gitea receiver does for redeliveries.
    requested = request_labels(labels_of(repo, num))
    if not requested:
        return
    label, profile = requested[0]
    with lock:
        if key in in_flight:
            return
        in_flight.add(key)
    log(f"review trigger: {repo}#{num} via {label} -> profile {profile or 'primary'}")
    for l, _ in requested:
        set_label(repo, num, l, False)
    set_label(repo, num, L_DONE, False)
    set_label(repo, num, L_WORKING, True)
    threading.Thread(target=run_review, args=(repo, num, title, label, profile),
                     daemon=True).start()


def recover_stale(resumed=()):
    """On service start, clear L_WORKING left over from a lost run."""
    try:
        for repo, num, _, _ in search_prs(L_WORKING):
            with lock:
                running = (repo, num) in in_flight
            if running or (repo, num) in resumed:
                continue  # its watcher was re-attached by resume_runs()
            # Search lags label edits; a review that finished seconds before
            # the restart is still indexed as working. Only the live labels count.
            if L_WORKING in labels_of(repo, num):
                # A run whose review was already posted only lost its label swap.
                if recovered_head(api, repo, num, MARKER, None, int(os.environ.get("WATCH_MINUTES", "45")) * 60):
                    set_label(repo, num, L_WORKING, False)
                    set_label(repo, num, L_DONE, True)
                    log(f"review done (recovered after restart): {repo}#{num}")
                    continue
                fail_review(repo, num, "the review service restarted mid-run")
    except Exception as e:
        log(f"recover error: {e}")


recover_stale(resume_runs())
log(f"polling {', '.join(REPOS) if REPOS else 'user:' + OWNER} every {POLL_SECONDS}s "
    f"(default profile {DEFAULT_PROFILE})")
while True:
    try:
        # One unfiltered search: GitHub ANDs multiple label: qualifiers, so
        # the request-label variants are matched client-side.
        for repo, num, title, names in search_prs():
            ensure_request_labels(repo)
            if request_labels(names):
                trigger(repo, num, title)
    except Exception as e:
        log(f"poll error: {e}")
    time.sleep(POLL_SECONDS)
