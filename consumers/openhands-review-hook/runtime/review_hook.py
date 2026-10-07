#!/usr/bin/env python3
"""Gitea webhook -> OpenHands review conversation.

Lifecycle: label L_REQUEST on a PR ->
  receiver swaps it to L_WORKING and starts a review conversation ->
  watcher thread waits for the posted "[<bot> review]" comment ->
  swaps L_WORKING to L_DONE.
Re-review after fixes: add L_REQUEST again (any time; the label swap itself
prevents double-fires).
A service restart is safe mid-run: each run is recorded on disk once its
conversation exists, and the next start re-attaches a watcher to it.

Which model reviews is an OpenHands **agent profile** (Canvas
`/api/agent-profiles`, each pinning its backend + model): REVIEW_PROFILE is
the default, and a request label of the form "<L_REQUEST>:<profile>" (e.g.
`review-this:codex-sol`) picks another profile for that PR only.

Site configuration comes from the environment (systemd EnvironmentFile);
the values marked (required) name the site's forge and credentials; the rest
have defaults for a typical `hands` host:

  HOOK_SECRET       (required) HMAC secret shared with the Gitea webhook
  GITEA_API         (required) Gitea API base, e.g. https://gitea.example.com/api/v1
  GITEA_TOKEN_FILE  (required) path of the file holding the bot's token
  OPENHANDS_API     Agent Canvas ingress base, default http://127.0.0.1:8000
  LOCAL_BACKEND_API_KEY  Canvas API key (load canvas.env as a second
                    systemd EnvironmentFile)
  REVIEW_PROFILE    default agent profile name, default codex-astra
  PROFILES_DIR      Canvas agent-profile files (host path), default
                    /opt/openhands/canvas-state/agent-profiles - read for the
                    profile id and the model stamped into the signature
  WORKSPACES_DIR    per-review working dirs (container path), default
                    /projects/reviews
  REVIEW_ORG        (required) org the webhook covers
  BOT_NAME          Gitea account that posts reviews, default hands-bot
  LABEL_REQUEST / LABEL_WORKING / LABEL_DONE
                    default review-this / hands-reviewing / hands-reviewed
  WATCH_MINUTES     review deadline, default 45
  REVIEW_RUNS_DIR   directory of review-runs-gitea.json, the in-flight runs a
                    restart re-attaches to, default /opt/openhands/hooks
  PROMPT_FILE       conversation prompt template, default
                    /opt/openhands/hooks/review_prompt.txt
                    (placeholders: {repo} {num} {title!r} {marker} {profile}
                    {label})

The prompt must instruct the agent to post ONE PR comment STARTING with the
marker "[<BOT_NAME> review]" - that comment is how the watcher detects
completion.
"""
import hashlib
import hmac
import json
import re
from review_policy import requested_profiles
from review_runner import recovered_head, next_page, patch_identity, retried
from review_runner import Runner, RunStore
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ["HOOK_SECRET"].encode()
GITEA = os.environ["GITEA_API"]
TOKEN = (
    open(os.environ["GITEA_TOKEN_FILE"])
    .read()
    .strip()
)
APP = os.environ.get("OPENHANDS_API", "http://127.0.0.1:8000")
APP_KEY = os.environ.get("LOCAL_BACKEND_API_KEY", "")
DEFAULT_PROFILE = os.environ.get("REVIEW_PROFILE", "codex-astra")
PROFILES_DIR = os.environ.get(
    "PROFILES_DIR", "/opt/openhands/canvas-state/agent-profiles"
)
WORKSPACES_DIR = os.environ.get("WORKSPACES_DIR", "/projects/reviews")
ORG = os.environ["REVIEW_ORG"]
BOT_NAME = os.environ.get("BOT_NAME", "hands-bot")
L_REQUEST = os.environ.get("LABEL_REQUEST", "review-this")
L_WORKING = os.environ.get("LABEL_WORKING", "hands-reviewing")
L_DONE = os.environ.get("LABEL_DONE", "hands-reviewed")
WATCH_SECONDS = int(os.environ.get("WATCH_MINUTES", "45")) * 60
MARKER = f"[{BOT_NAME} review]"
PROMPT_FILE = os.environ.get("PROMPT_FILE", "/opt/openhands/hooks/review_prompt.txt")
RUNS = RunStore(os.path.join(os.environ.get("REVIEW_RUNS_DIR", "/opt/openhands/hooks"), "review-runs-gitea.json"))

in_flight = set()
lock = threading.Lock()


def api(path, method="GET", data=None):
    """One forge call. A paged list answer (Link: rel="next") is followed to
    its end, so callers always see the whole list."""
    url, result = GITEA + path, None
    for _ in range(100):  # a page bound, never reached in practice
        req = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": "token " + TOKEN,
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
        url = next_page(link, GITEA) if method == "GET" and isinstance(page, list) else None
        result = page if result is None else result + page
        if not url:
            return result
    return result


def change_identity(repo, base, head):
    """The patch identity of the change at `head`: Gitea's three-dot compare from the merge
    base with `base`, as a raw diff (?output=diff, Gitea 1.27 or later; the diff
    verify-delivery hashes). An older Gitea ignores the parameter and answers JSON, which
    has no identity (ValueError), or 404 before 1.22: either way the request gets a full review."""
    req = urllib.request.Request(
        f"{GITEA}/repos/{repo}/compare/{urllib.parse.quote(base, safe='/')}...{head}?output=diff",
        headers={"Authorization": "token " + TOKEN, "Accept": "text/plain"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return patch_identity(r.read())[0]


def label_id(repo, name):
    org = repo.split("/")[0]
    for path in (f"/orgs/{org}/labels", f"/repos/{repo}/labels"):
        for l in api(path) or []:
            if l["name"] == name:
                return l["id"]
    return None


def labels_of(repo, num):
    return {l["name"]: l["id"] for l in api(f"/repos/{repo}/issues/{num}/labels")}


def set_label(repo, num, name, present):
    have = labels_of(repo, num)
    if present and name not in have:
        lid = label_id(repo, name)
        if lid is None:
            print(f"label {name} not found for {repo}", flush=True)
            return
        api(f"/repos/{repo}/issues/{num}/labels", "POST", {"labels": [lid]})
    if not present and name in have:
        api(f"/repos/{repo}/issues/{num}/labels/{have[name]}", "DELETE")


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
    print(f"review failed: {repo}#{num}: {reason}", flush=True)


def note_review(repo, num, text):
    """A PR comment that is not a review (never starts with the review marker line)."""
    api(f"/repos/{repo}/issues/{num}/comments", "POST", {"body": f"⚠️ {MARKER} note: " + text})
    print(f"review note: {repo}#{num}: {text[:80]}", flush=True)


def run_review(repo, num, title, label, profile, resume=None):
    try:
        runner = Runner(api, app_api, set_label, profile_info, fail_review,
                        marker=MARKER, bot=BOT_NAME, working=L_WORKING,
                        done=L_DONE, timeout=WATCH_SECONDS,
                        poll=int(os.environ.get('REVIEW_POLL_SECONDS', '30')), runs=RUNS, note=note_review,
                        change_identity=change_identity)
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
        print(f"review resume: {repo}#{num} conversation={record['conversation']}", flush=True)
        threading.Thread(target=run_review,
                         args=(repo, num, record.get("title", ""), record["label"], record["profile"], record),
                         daemon=True).start()
    return resumed


def recover_stale(resumed=()):
    """On service start, clear L_WORKING left over from a lost run."""
    try:
        res = api(
            "/repos/issues/search?owner="
            + ORG
            + "&type=pulls&state=open&labels="
            + L_WORKING
        )
        for issue in res or []:
            repo = issue["repository"]["full_name"]
            num = issue["number"]
            with lock:
                running = (repo, num) in in_flight
            if running or (repo, num) in resumed:
                continue  # its watcher was re-attached by resume_runs()
            # A run whose review was already posted only lost its label swap.
            if recovered_head(api, repo, num, MARKER, BOT_NAME, WATCH_SECONDS):
                set_label(repo, num, L_WORKING, False)
                set_label(repo, num, L_DONE, True)
                print(f"review done (recovered after restart): {repo}#{num}", flush=True)
                continue
            fail_review(repo, num, "the review service restarted mid-run")
    except Exception as e:
        print(f"recover error: {e}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        sig = self.headers.get("X-Gitea-Signature", "")
        if not hmac.compare_digest(
            hmac.new(SECRET, body, hashlib.sha256).hexdigest(), sig
        ):
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        self.end_headers()
        try:
            self.handle_event(json.loads(body))
        except Exception as e:
            print("hook error:", e, flush=True)

    def handle_event(self, p):
        pr = p.get("pull_request")
        if not pr or not str(p.get("action", "")).startswith("label"):
            return
        if not request_labels(l["name"] for l in pr.get("labels", [])):
            return
        repo = p["repository"]["full_name"]
        num = pr["number"]
        # The payload is a snapshot; Gitea redelivers webhooks, and a stale
        # redelivery once restarted a finished review. Trust only the live
        # label state.
        requested = request_labels(labels_of(repo, num))
        if not requested:
            print(f"stale delivery ignored: {repo}#{num}", flush=True)
            return
        label, profile = requested[0]
        key = (repo, num)
        with lock:
            if key in in_flight:
                return
            in_flight.add(key)
        # A posted review held for the next start (#64 item 7) belongs to an older request: it goes
        # before this request's labels change, so a restart in between cannot answer this request with it.
        if not RUNS.clear(repo, num):
            print(f"review deferred: {repo}#{num} the run state cannot be read", flush=True)
            with lock:
                in_flight.discard(key)
            return
        print(f"review trigger: {repo}#{num} via {label} -> profile {profile or 'primary'}", flush=True)
        for l, _ in requested:
            set_label(repo, num, l, False)
        set_label(repo, num, L_DONE, False)
        set_label(repo, num, L_WORKING, True)
        threading.Thread(target=run_review,
                         args=(repo, num, pr.get('title', ''), label, profile),
                         daemon=True).start()


recover_stale(resume_runs())
HTTPServer(("127.0.0.1", int(os.environ.get("HOOK_PORT", "8081"))), Handler).serve_forever()
