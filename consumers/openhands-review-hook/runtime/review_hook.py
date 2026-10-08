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
own_login = None  # the account behind TOKEN, read once (see own_change)
own_writes = {}  # (repo, num) -> times of counted label writes whose webhook is still due
OWN_WINDOW = 60  # seconds within which a label write's webhook is expected
counting = threading.local()  # .on while a failed start is reported (see own_write)


def own_write(repo, num, write):
    """WRITE, a label change of this receiver. Only the changes made while a failed start is
    reported (counting.on, set in this thread by handle_event) are counted for own_change: that
    report is where a webhook of the receiver's own would run the request again (#64 item 22).
    A run's own relabelling is not counted, so its webhook still serves a request label added
    meanwhile, and startup recovery's changes are not either: their webhooks are refused while
    the receiver is not yet listening, and one delivered later is served as usual. Counted before it is sent, since its webhook may arrive before
    the answer; a write that raises sends no webhook and is no longer counted (one whose answer
    was lost may still send one: it is then served as before)."""
    if not getattr(counting, "on", False):
        return write()
    stamp = time.monotonic()
    with lock:
        own_writes.setdefault((repo, num), []).append(stamp)
    try:
        return write()
    except Exception:
        with lock:
            if stamp in own_writes.get((repo, num), []):
                own_writes[(repo, num)].remove(stamp)
        raise


def own_change(payload, repo, num):
    """Whether a webhook reports a label change the receiver made while reporting a failed
    start. Such a change never carries a new request: when the request label stayed on,
    removing a stale hands-reviewing would otherwise run the request again and report it
    twice (#64 item 22). Each counted write accounts for one webhook by the token's account
    within OWN_WINDOW seconds, and that webhook uses it up; any further webhook, including
    one from a person whose token the site uses, is served as usual. The account is read
    once; while it cannot be read, nothing is skipped."""
    global own_login
    sender = (payload.get("sender") or {}).get("login")
    now = time.monotonic()
    with lock:
        pending = [t for t in own_writes.get((repo, num), []) if now - t <= OWN_WINDOW]
        own_writes[(repo, num)] = pending
    if not sender or not pending:
        return False
    if own_login is None:
        try:
            own_login = (api("/user") or {}).get("login") or ""
        except Exception as e:
            print(f"token account unreadable, own label changes not skipped: {type(e).__name__}", flush=True)
            return False
    if not own_login or sender != own_login:
        return False
    with lock:
        if own_writes.get((repo, num)):
            own_writes[(repo, num)].pop(0)
    return True


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
        own_write(repo, num, lambda: api(f"/repos/{repo}/issues/{num}/labels", "POST", {"labels": [lid]}))
    if not present and name in have:
        own_write(repo, num, lambda: api(f"/repos/{repo}/issues/{num}/labels/{have[name]}", "DELETE"))


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
        repo = p["repository"]["full_name"]
        num = pr["number"]
        # Every webhook of an own write uses it up, with or without a request label on the PR.
        if own_change(p, repo, num):
            print(f"own label change ignored: {repo}#{num}", flush=True)
            return
        if not request_labels(l["name"] for l in pr.get("labels", [])):
            return
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
        # The run state is never a reason to hold up a review: when it cannot be updated, say so and go on.
        if not RUNS.clear(repo, num):
            print(f"run state not updated for the new request: {repo}#{num} a held review may label it done after a restart", flush=True)
        print(f"review trigger: {repo}#{num} via {label} -> profile {profile or 'primary'}", flush=True)
        consumed = False  # how far the start got: the request labels are off
        try:
            for l, _ in requested:
                set_label(repo, num, l, False)
            consumed = True
            set_label(repo, num, L_DONE, False)
            set_label(repo, num, L_WORKING, True)
            threading.Thread(target=run_review,
                             args=(repo, num, pr.get('title', ''), label, profile),
                             daemon=True).start()
        except Exception as e:
            # No run started: release the PR, or every later request on it is ignored
            # until a restart (#64 item 16). First take hands-reviewing off again: a write
            # whose answer was lost may still have set it, and with no run behind it the
            # next start would fail the PR as "restarted mid-run" (#64 item 17).
            print(f"review start failed: {repo}#{num}: {type(e).__name__} {getattr(e, 'code', '')}".rstrip(), flush=True)
            # Say so on the PR (#64 item 18): fail_review takes hands-reviewing off and asks for
            # the label again. Gitea sends a label webhook once, so nothing retries this request
            # on its own: the note is posted whether or not the request label came off (#64 item
            # 21), and says when it is still there. When a stale hands-reviewing was on the PR, its
            # removal sends one more label webhook that still lists the request label: own_change
            # skips it, so the note is not posted twice (#64 item 22). The live labels decide, as
            # far as they can be read: a removal whose answer was lost took it off all the same.
            try:
                consumed = not request_labels(labels_of(repo, num))
            except Exception:
                pass  # unreadable: how far the start got decides
            reason = "the review could not start " + f"({type(e).__name__} {getattr(e, 'code', '')}".rstrip() + ")"
            if not consumed:
                reason += "; its request label is still on the pull request: remove it before adding it again"
            try:
                counting.on = True  # its label changes are the ones whose webhooks own_change skips
                try:
                    fail_review(repo, num, reason)
                finally:
                    counting.on = False
            except Exception as e:
                print(f"failed start not cleaned up: {repo}#{num}: {type(e).__name__} {getattr(e, 'code', '')}".rstrip(), flush=True)
            with lock:
                in_flight.discard(key)


recover_stale(resume_runs())
HTTPServer(("127.0.0.1", int(os.environ.get("HOOK_PORT", "8081"))), Handler).serve_forever()
