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

Gitea sends each webhook once. One that arrives while the receiver cannot
reach Gitea (an outage of the network or of Gitea), or while the receiver is
stopped, would leave its request label on the PR with nothing to act on it.
So the receiver also sweeps the org every SWEEP_MINUTES, and once at start:
an open PR that carries a request label and has no run starts as if its
webhook had come. The sweep's outcome, a webhook that failed on a forge
error, and a request that reached Gitea are written to
forge-status-gitea.json, which the Auto Reviews page shows: an outage cannot
be reported on the PR itself.

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
                    restart re-attaches to, and of forge-status-gitea.json,
                    default /opt/openhands/hooks
  SWEEP_MINUTES     how often open PRs with a request label and no run are
                    looked for, default 5; 0 turns the sweep off (webhooks only)
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
from review_runner import ForgeStatus, Runner, RunStore, contact_error
import os
import threading
import time
import urllib.parse
import urllib.request
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer


class LocalHTTPServer(HTTPServer):
    """An HTTPServer that skips the reverse lookup HTTPServer.server_bind makes for its
    server_name (socket.getfqdn): it listens on a loopback address and never uses the name,
    and the lookup can take 35 s on a host whose resolver stalls on it (observed on GitHub's
    macOS runners)."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

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
RUNS_DIR = os.environ.get("REVIEW_RUNS_DIR", "/opt/openhands/hooks")
RUNS = RunStore(os.path.join(RUNS_DIR, "review-runs-gitea.json"))
SWEEP_SECONDS = float(os.environ.get("SWEEP_MINUTES", "5")) * 60
# interval 0: without the sweep nothing contacts Gitea on a schedule, so a quiet file is no sign of a stop
STATUS = ForgeStatus(RUNS_DIR, "gitea", SWEEP_SECONDS, log=print)

in_flight = set()
lock = threading.Lock()
own_login = None  # the account behind TOKEN, read once (see own_change)
own_writes = {}  # (repo, num) -> times of counted label writes whose webhook is still due
OWN_WINDOW = 60  # seconds within which a label write's webhook is expected
counting = threading.local()  # .on while a request's start, or its failure report, runs in this thread


def own_write(repo, num, write):
    """WRITE, a label change of this receiver. Only the changes made by a request's start and,
    if it fails, by its report (counting.on, set in this thread by handle_event) are counted for
    own_change: there a webhook of the receiver's own could run the request again (#64 items 22
    and 23). A run's own relabelling is not counted, so its webhook still serves a request label
    added meanwhile, and startup recovery's changes are not either: their webhooks are refused
    while the receiver is not yet listening, and one delivered later is served as usual. Counted
    before it is sent, since its webhook may arrive before the answer; a write that raises sends
    no webhook and is no longer counted (one whose answer was lost may still send one: it is then
    served as before)."""
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
    """Whether a webhook reports a label change the receiver made in a request's start or its
    failure report. Such a change never carries a new request: when a request label stayed on,
    the webhook of removing a stale hands-reviewing (#64 item 22), or of removing the first of
    two request labels (#64 item 23), would otherwise run the request again and report it twice.
    Each counted write accounts for one webhook by the token's account within OWN_WINDOW
    seconds, and that webhook uses it up; any further webhook, including one from a person whose
    token the site uses, is served as usual. A count left when the PR's run ends (its webhook
    was lost) is dropped then (run_review), so it cannot use up a later request. The account is
    read once; while it cannot be read, nothing is skipped."""
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
        # The start's counted writes whose webhooks never came (lost, or the receiver was busy)
        # end with the run: a later request from the token's account is then served (#64 item 24).
        with lock:
            own_writes.pop((repo, num), None)
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
                set_label(repo, num, L_DONE, True)  # on before working comes off: never no label
                set_label(repo, num, L_WORKING, False)
                print(f"review done (recovered after restart): {repo}#{num}", flush=True)
                continue
            fail_review(repo, num, "the review service restarted mid-run")
    except Exception as e:
        print(f"recover error: {e}", flush=True)


def start_request(repo, num, title, swept=False):
    """Start the review a request label asks for, from a webhook or, when SWEPT, from the sweep.
    The payload or search result is a snapshot; Gitea redelivers webhooks, and a stale
    redelivery once restarted a finished review. Trust only the live label state.
    The PR is reserved before its labels are read: the sweep and a webhook can both find the
    same request, and a caller that read the labels first and reserved after the other's run
    had already ended (a fast failure) would start the consumed request again."""
    key = (repo, num)
    with lock:
        if key in in_flight:
            return
        in_flight.add(key)
    try:
        requested = request_labels(labels_of(repo, num))
    except Exception as e:
        with lock:
            in_flight.discard(key)
        if isinstance(e, OSError):  # urllib's errors are OSErrors
            STATUS.failed(e)
        raise
    STATUS.ok()  # Gitea answered; with the sweep off, this is what ends a recorded failure
    if not requested:
        with lock:
            in_flight.discard(key)
        if not swept:
            print(f"stale delivery ignored: {repo}#{num}", flush=True)
        return
    label, profile = requested[0]
    # A posted review held for the next start (#64 item 7) belongs to an older request: it goes
    # before this request's labels change, so a restart in between cannot answer this request with it.
    # The run state is never a reason to hold up a review: when it cannot be updated, say so and go on.
    if not RUNS.clear(repo, num):
        print(f"run state not updated for the new request: {repo}#{num} a held review may label it done after a restart", flush=True)
    print(f"review trigger: {repo}#{num} via {label} -> profile {profile or 'primary'}", flush=True)
    # The label changes of this start, and of its report if it fails, are counted: their
    # webhooks are the receiver's own and never carry a new request (#64 items 22 and 23).
    # A run's own relabelling happens in its thread, where nothing is counted.
    counting.on = True
    try:
        consumed = False  # how far the start got: the request labels are off
        try:
            # The new state label goes on before the old ones come off, so the PR is never seen
            # with no hands label at all: a client reading in between took that for a lost
            # request and asked again, and the same head was reviewed twice.
            set_label(repo, num, L_WORKING, True)
            for l, _ in requested:
                set_label(repo, num, l, False)
            consumed = True
            set_label(repo, num, L_DONE, False)
            threading.Thread(target=run_review,
                             args=(repo, num, title, label, profile),
                             daemon=True).start()
        except Exception as e:
            # No run started: release the PR, or every later request on it is ignored
            # until a restart (#64 item 16). First take hands-reviewing off again: a write
            # whose answer was lost may still have set it, and with no run behind it the
            # next start would fail the PR as "restarted mid-run" (#64 item 17).
            print(f"review start failed: {repo}#{num}: {type(e).__name__} {getattr(e, 'code', '')}".rstrip(), flush=True)
            if isinstance(e, OSError):  # a forge error: the status shows it, whatever the cleanup does
                STATUS.failed(e)
            # Say so on the PR (#64 item 18): fail_review takes hands-reviewing off and asks for
            # the label again. Gitea sends a label webhook once, and only the sweep (when on) would
            # retry this request: the note is posted whether or not the request label came off (#64
            # item 21), and says when it is still there. A start from the sweep is the exception (below). When a stale hands-reviewing was on the PR, its
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
                if swept and not consumed:
                    # The sweep found the request and will find it again: like GitHub's poll, it
                    # only cleans up, so a start that keeps failing never comments on every sweep.
                    set_label(repo, num, L_WORKING, False)
                else:
                    fail_review(repo, num, reason)
            except Exception as e:
                print(f"failed start not cleaned up: {repo}#{num}: {type(e).__name__} {getattr(e, 'code', '')}".rstrip(), flush=True)
            with lock:
                in_flight.discard(key)
    finally:
        counting.on = False


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
        start_request(repo, num, pr.get("title", ""))


def sweep():
    """Start every open PR of the org that carries a request label and has no run: its webhook
    was lost (it came during an outage, or while the receiver was stopped) or its start failed
    with the label still on. The search is the sweep's contact with Gitea, and its outcome is
    the status the Auto Reviews page shows."""
    try:
        found = api("/repos/issues/search?owner=" + ORG + "&type=pulls&state=open")
    except Exception as e:
        print(f"sweep error: {contact_error(e)}", flush=True)
        STATUS.failed(e)
        return
    STATUS.ok()
    for issue in found or []:
        try:
            if not request_labels(l["name"] for l in issue.get("labels", [])):
                continue
        except ValueError:
            continue  # two explicit reviewer labels: a person has to choose one; the other PRs go on
        repo, num = issue["repository"]["full_name"], issue["number"]
        with lock:
            if (repo, num) in in_flight:
                continue  # running, or being started by its webhook
        print(f"request found by the sweep: {repo}#{num}", flush=True)
        try:
            start_request(repo, num, issue.get("title", ""), swept=True)
        except Exception as e:  # its labels could not be read: the next sweep tries again
            print(f"sweep start error: {repo}#{num}: {contact_error(e)}", flush=True)


def sweep_forever():
    while True:
        try:
            sweep()
        except Exception as e:  # never let one bad answer end the sweeps
            print(f"sweep error: {type(e).__name__}", flush=True)
        time.sleep(SWEEP_SECONDS)


recover_stale(resume_runs())
if SWEEP_SECONDS > 0:
    threading.Thread(target=sweep_forever, daemon=True).start()
LocalHTTPServer(("127.0.0.1", int(os.environ.get("HOOK_PORT", "8081"))), Handler).serve_forever()
