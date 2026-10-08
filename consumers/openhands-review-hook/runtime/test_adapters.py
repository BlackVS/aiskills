"""Exercise both real adapter processes against local HTTP forge/agent fakes."""
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class AdapterTests(unittest.TestCase):
    def run_adapter(self, github, refused=False, blocked=False, stuck=False, lost=False, kept=False, blind=False, own=False):
        """A request runs the primary, hits a rate limit and completes on the fallback. REFUSED: the
        forge stores the first write of hands-reviewing but answers it 503, so no run starts and the
        label is taken off again; the request is put back
        and must run then: the failed start released the PR (#64 item 16). BLOCKED (GitHub): another
        PR, listed first, keeps failing its start on every poll; the request still runs. STUCK (with
        REFUSED): taking hands-reviewing off fails too; that is logged and the PR is still released.
        LOST: the first removal of the request label is applied but answered 503; KEPT: it is refused and
        the label stays. Only LOST (like REFUSED) leaves nothing to ask again, so only it is reported on
        the PR on GitHub (#64 item 18); Gitea, which sends a webhook once, reports both, KEPT with
        advice to take the label off first (#64 item 21). BLIND: after the failed write, the labels
        cannot be read for a while (three answers of 503, as many as a read is tried), so how far the
        start got decides. OWN (Gitea, with KEPT): a stale hands-reviewing is on the PR, and its removal
        by the failed start's report sends a webhook by the receiver's own account that still lists the
        request label; it starts nothing, so the note is posted once (#64 item 22), and the request then
        asked again from that same account (a site whose token is a person's) runs. With REFUSED instead
        of KEPT the request label is already off, so that webhook carries none, and must still use up
        the count before the request-label filter, or the re-request would be the one skipped.
        OWN='idle': the request itself comes from the token's account, and the webhook of the run's own
        relabelling at its end is not counted as the receiver's (only a failed start's report is).
        OWN='other' (with KEPT): the same webhook comes from another account, so it is no own change:
        it is served, and runs the request whose label is still on."""
        head = 'a' * 40
        names = ['review-this', 'hands-reviewing', 'hands-reviewed', 'review-this:codex-astra', 'review-this:claude-opus']
        labels = {'review-this', 'hands-reviewing'} if own in (True, 'other') and kept else {'review-this'}
        echo = []  # OWN: sends the webhook of the receiver's own removal of hands-reviewing
        starts, failures, errors = [], [], []
        done, refusal, cleanup = threading.Event(), threading.Event(), threading.Event()
        blind_left = [3 if blind else 0]
        class Fake(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self): self.handle_request()
            def do_POST(self): self.handle_request()
            def do_DELETE(self): self.handle_request()
            def handle_request(self):
                path = urllib.parse.unquote(self.path.split('?')[0])
                body = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.headers.get('Content-Length') else None
                result, link = None, None
                if path == '/forge/search/issues':
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)['q'][0]
                    items = [] if 'label:' in query else [{'repository_url': 'https://example.test/repos/owner/repo', 'number': 1, 'title': 'test', 'labels': [{'name': n} for n in labels]}]
                    if blocked and items:
                        items.insert(0, {'repository_url': 'https://example.test/repos/owner/repo', 'number': 2, 'title': 'other', 'labels': [{'name': 'review-this'}]})
                    result = {'items': items}
                elif path == '/forge/repos/issues/search': result = []
                elif path == '/forge/user': result = {'login': 'test-bot'}  # the token's account
                elif path == '/forge/repos/owner/repo/pulls/1': result = {'state': 'open', 'head': {'sha': head}}
                elif blocked and path.startswith('/forge/repos/owner/repo/issues/2/labels'):
                    if self.command == 'DELETE':  # PR 2's request label cannot be taken off: its start fails on every poll
                        refusal.set(); self.send_response(403); self.send_header('Content-Length', '0'); self.end_headers(); return
                    result = [{'name': 'review-this', 'id': 1}]
                elif stuck and refusal.is_set() and not cleanup.is_set() and self.command == 'DELETE' \
                        and path.startswith('/forge/repos/owner/repo/issues/1/labels/'):
                    cleanup.set(); self.send_response(503); self.send_header('Content-Length', '0'); self.end_headers(); return
                elif refusal.is_set() and blind_left[0] and self.command == 'GET' and path == '/forge/repos/owner/repo/issues/1/labels':
                    blind_left[0] -= 1; self.send_response(503); self.send_header('Content-Length', '0'); self.end_headers(); return
                elif (lost or kept) and not refusal.is_set() and self.command == 'DELETE' \
                        and path.startswith('/forge/repos/owner/repo/issues/1/labels/') \
                        and path.split('/')[-1] in ('review-this', '1'):
                    if lost:
                        labels.discard('review-this')
                    refusal.set(); self.send_response(503); self.send_header('Content-Length', '0'); self.end_headers(); return
                elif refused and not refusal.is_set() and self.command == 'POST' and path.endswith('/issues/1/labels') \
                        and (2 in body['labels'] or 'hands-reviewing' in body['labels']):
                    # the write is stored, its answer lost: hands-reviewing is set with no run behind it
                    labels.add('hands-reviewing')
                    refusal.set(); self.send_response(503); self.send_header('Content-Length', '0'); self.end_headers(); return
                elif path == '/forge/repos/owner/repo/issues/1/labels':
                    if self.command == 'POST':
                        labels.update(body['labels'] if github else (names[i-1] for i in body['labels']))
                        if 'hands-reviewed' in labels: done.set()
                    result = [{'name': n, 'id': names.index(n)+1} for n in labels]
                elif path.startswith('/forge/repos/owner/repo/issues/1/labels/'):
                    name = path.split('/')[-1]
                    name = name if github else names[int(name)-1]
                    labels.discard(name)
                    if echo and name == 'hands-reviewing':  # Gitea reports the change, as done by the token's account
                        threading.Thread(target=echo[0], daemon=True).start()
                elif path == '/forge/orgs/owner/labels' or path == '/forge/repos/owner/repo/labels':
                    result = [{'name': n, 'id': i+1} for i,n in enumerate(names)]
                elif path.startswith('/forge/repos/owner/repo/labels/'):
                    result = {'name': path.split('/')[-1]}
                elif path == '/forge/repos/owner/repo/issues/1/comments':
                    if self.command == 'POST': failures.append(body)
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    if query.get('page') == ['2']:
                        result = [{'user': {'login': 'test-bot'}, 'body': f'[test-bot review] reviewed at head {head}\n\nVERDICT\nREADY_FOR_HUMAN_MERGE'}]
                    else:  # page 1 never holds the review: only a receiver that follows the Link header finds it
                        result = [{'user': {'login': 'someone'}, 'body': 'first page: discussion only'}]
                        if len(starts) >= 2:
                            link = f'<http://127.0.0.1:{self.server.server_port}/forge/repos/owner/repo/issues/1/comments?since=x&page=2>; rel="next"'
                elif path == '/api/conversations':
                    starts.append(body); result = {'id': str(len(starts))}
                elif path.endswith('/events/search'):
                    result = {'items': [{'kind': 'ConversationErrorEvent', 'code': 'ACPPromptError', 'detail': 'limit: {"errorKind":"rate_limit"}'}]}
                elif path.startswith('/api/conversations/'):
                    result = {'execution_status': 'error' if len(starts) == 1 else 'finished'}
                else: errors.append((self.command, path))
                payload = json.dumps(result).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                if link: self.send_header('Link', link)
                self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); profiles = root/'profiles'; profiles.mkdir()
            for p in ['codex-astra','claude-opus']:
                (profiles/(p+'.json')).write_text(json.dumps({'id': p,'agent_kind':'acp','acp_model':p}))
            (root/'token').write_text('test-only')
            (root/'settings').write_text(json.dumps({'revision':1,'primary':'codex-astra','fallback':'claude-opus'}))
            (root/'prompt').write_text('Review {repo} #{num}, model {model}, marker {marker}, label {label}')
            with socket.socket() as sock: sock.bind(('127.0.0.1',0)); hook_port = sock.getsockname()[1]
            base = f'http://127.0.0.1:{server.server_port}'
            env = dict(os.environ, GITHUB_API=base+'/forge', GITEA_API=base+'/forge',
                GITHUB_TOKEN_FILE=str(root/'token'), GITEA_TOKEN_FILE=str(root/'token'),
                OPENHANDS_API=base, LOCAL_BACKEND_API_KEY='test-only', HOOK_SECRET='test-only',
                PROFILES_DIR=str(profiles), REVIEW_SETTINGS_FILE=str(root/'settings'),
                PROMPT_FILE=str(root/'prompt'), WORKSPACES_DIR='/tmp/reviews', REVIEW_POLL_SECONDS='1',
                GITHUB_OWNER='owner', GITHUB_REPOS='owner/repo', REVIEW_ORG='owner',
                BOT_NAME='test-bot', MARKER='[test-bot review]', POLL_SECONDS='1', HOOK_PORT=str(hook_port),
                REVIEW_RUNS_DIR=str(root), PYTHONUNBUFFERED='1')
            script = 'github_review_poller.py' if github else 'review_hook.py'
            proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name(script))],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            def request(sender='someone', listed=('review-this',)):
                if github:
                    return  # the poller finds the request label on its own
                body = json.dumps({'action':'label_updated','repository':{'full_name':'owner/repo'},'sender':{'login':sender},
                                   'pull_request':{'number':1,'title':'test','labels':[{'name': n} for n in listed]}}).encode()
                hook = urllib.request.Request(f'http://127.0.0.1:{hook_port}/hooks/gitea',data=body,
                    headers={'X-Gitea-Signature':hmac.new(b'test-only',body,hashlib.sha256).hexdigest()})
                for _ in range(40):
                    try:
                        with urllib.request.urlopen(hook,timeout=2) as response: self.assertEqual(response.status,204)
                        break
                    except urllib.error.URLError: time.sleep(.05)
            if own:  # Gitea reports a removal of hands-reviewing as the token's account, with the labels it left
                echo.append(lambda: request('someone' if own == 'other' else 'test-bot', sorted(labels)))
            try:
                request('test-bot' if own == 'idle' else 'someone')
                if own == 'other':  # the echo by another account is served: it runs the request itself
                    self.assertTrue(refusal.wait(12), 'the request never reached the label change')
                elif refused or lost or kept:
                    self.assertTrue(refusal.wait(12), 'the request never reached the label change')
                    if blind:  # a blind read is retried for about 3 s: wait for all three answers
                        for _ in range(300):
                            if not blind_left[0]: break
                            time.sleep(.05)
                        self.assertEqual(blind_left, [0], 'the labels were never read again')
                    time.sleep(.5)  # the failed start unwinds
                    self.assertEqual(starts, [])
                    if stuck:  # the forge refused that too: the label stays, the PR is released all the same
                        self.assertTrue(cleanup.is_set()); self.assertIn('hands-reviewing', labels)
                    elif kept and github:
                        pass  # the request label stayed: the next poll may already have run it
                    else:
                        self.assertNotIn('hands-reviewing', labels, 'the failed start takes hands-reviewing off again (#64 item 17)')
                    if own is True:  # the webhook of the receiver's own removal is handled, and starts nothing
                        time.sleep(1)
                        self.assertEqual(starts, [], 'a label change by the receiver itself started a review')
                    labels.add('review-this')  # the user asks again
                    request('test-bot' if own else 'someone')
                success = done.wait(12)
            finally:
                proc.terminate(); output = proc.communicate(timeout=5)[0]
                server.shutdown(); server.server_close(); thread.join()
            self.assertTrue(success,output)
            if refused or lost or kept:
                self.assertIn('review start failed: owner/repo#1: HTTPError 503', output)
            if stuck:  # the cleanup was refused too: no failure comment could follow it
                self.assertIn('failed start not cleaned up: owner/repo#1: HTTPError 503', output)
            # a start that failed after its request label was taken off says so on the PR, once (#64 item 18);
            # one that still has its request label (PR 2) is retried by the next poll and never comments
            reported = '⚠️ [test-bot review] could not run: the review could not start (HTTPError 503)'
            expected = ([] if stuck else [reported] if refused or lost else
                        [reported + '; its request label is still on the pull request: remove it before adding it again']
                        if kept and not github else [])
            self.assertEqual([f['body'].split('\n')[0] for f in failures], expected)
            if blind:
                self.assertEqual(blind_left, [0], 'the labels were read after the failed write')
            if own is True:
                self.assertEqual(output.count('own label change ignored: owner/repo#1'), 1, output)
            if own in ('idle', 'other'):
                self.assertNotIn('own label change ignored', output)
            failures.clear()
            if blocked:
                self.assertIn('review start failed: owner/repo#2: HTTPError 403', output)
            self.assertEqual([s['agent_profile_id'] for s in starts],['codex-astra','claude-opus'])
            self.assertFalse(failures); self.assertFalse(errors)
            self.assertEqual(labels,{'hands-reviewed'})
            self.assertIn(head,starts[1]['initial_message']['content'][0]['text'])

    def run_unchanged(self, github, author='test-bot', owner='test-bot', review_author=None, gitea_json=False, requests=1,
                      flaky=None):
        """The newest review, by `author`, is of an older head; the forge's compare diff of both
        heads has the same patch identity. With `requests` > 1 (GitHub), the request label is
        put back after each outcome, so one poller process handles that many requests. `flaky`
        maps a forge path (or "METHOD path") to how many times it first answers 503. Returns what the adapter did:
        the posted comments, the compares read, the conversations started, the labels, the forge
        paths read."""
        flaky = dict(flaky or {})
        head, old = 'a' * 40, 'b' * 40
        names = ['review-this', 'hands-reviewing', 'hands-reviewed']
        labels, posted, compares, starts, paths, errors = {'review-this'}, [], [], [], [], []
        outcome = threading.Event()  # labelled done, or a conversation started
        # One request at a time, and the labels reset under the same lock: a handler never reads
        # `labels` while the test thread refills it for the next request.
        guard = threading.Lock()
        diff = b'diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n'
        class Fake(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self): self.locked()
            def do_POST(self): self.locked()
            def do_DELETE(self): self.locked()
            def locked(self):
                with guard:
                    self.handle_request()
            def handle_request(self):
                path = urllib.parse.unquote(self.path.split('?')[0])
                query = urllib.parse.urlsplit(self.path).query
                body = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.headers.get('Content-Length') else None
                result, raw = None, None
                paths.append(path)
                key = path if flaky.get(path) else f'{self.command} {path}'
                if flaky.get(key):
                    flaky[key] -= 1
                    self.send_response(503); self.send_header('Content-Length', '0'); self.end_headers()
                    return
                if path == '/forge/search/issues':
                    q = urllib.parse.parse_qs(query)['q'][0]
                    result = {'items': [] if 'label:' in q else [{'repository_url': 'https://example.test/repos/owner/repo', 'number': 1, 'title': 'test', 'labels': [{'name': n} for n in labels]}]}
                elif path == '/forge/repos/issues/search': result = []
                elif path == '/forge/user': result = {'login': owner}
                elif path == '/forge/repos/owner/repo/pulls/1': result = {'state': 'open', 'head': {'sha': head}, 'base': {'ref': 'release/1'}}
                elif path.startswith('/forge/repos/owner/repo/compare/'):
                    compares.append((path, query, self.headers.get('Accept')))
                    raw = (b'{"total_commits":1,"commits":[]}' if gitea_json else
                           diff.replace(b'\n', b'\r\n') if head in path else diff)
                elif path == '/forge/repos/owner/repo/issues/1/labels':
                    if self.command == 'POST':
                        labels.update(body['labels'] if github else (names[i-1] for i in body['labels']))
                        if 'hands-reviewed' in labels: outcome.set()
                    result = [{'name': n, 'id': names.index(n)+1} for n in labels]
                elif path.startswith('/forge/repos/owner/repo/issues/1/labels/'):
                    name = path.split('/')[-1]
                    labels.discard(name if github else names[int(name)-1])
                elif path == '/forge/orgs/owner/labels' or path == '/forge/repos/owner/repo/labels':
                    result = [{'name': n, 'id': i+1} for i, n in enumerate(names)]
                elif path.startswith('/forge/repos/owner/repo/labels'): result = {'name': path.split('/')[-1]}
                elif path == '/forge/repos/owner/repo/issues/1/comments':
                    if self.command == 'POST': posted.append(body['body'])
                    result = [{'user': {'login': author}, 'body': f'[test-bot review] reviewed at head {old}\n\nVERDICT\nREADY_FOR_HUMAN_MERGE'}]
                elif path == '/api/conversations': starts.append(body); result = {'id': '1'}; outcome.set()
                elif path.startswith('/api/conversations'): result = {'execution_status': 'running'}
                else: errors.append((self.command, path))
                payload = raw if raw is not None else json.dumps(result).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); profiles = root/'profiles'; profiles.mkdir()
            (profiles/'codex-astra.json').write_text(json.dumps({'id': 'codex-astra', 'agent_kind': 'acp', 'acp_model': 'codex-astra'}))
            (root/'token').write_text('test-only')
            (root/'settings').write_text(json.dumps({'revision': 1, 'primary': 'codex-astra', 'fallback': None}))
            (root/'prompt').write_text('Review {repo} #{num}, model {model}, marker {marker}, label {label}')
            with socket.socket() as sock: sock.bind(('127.0.0.1', 0)); hook_port = sock.getsockname()[1]
            base = f'http://127.0.0.1:{server.server_port}'
            env = dict(os.environ, GITHUB_API=base+'/forge', GITEA_API=base+'/forge',
                GITHUB_TOKEN_FILE=str(root/'token'), GITEA_TOKEN_FILE=str(root/'token'), OPENHANDS_API=base,
                LOCAL_BACKEND_API_KEY='test-only', HOOK_SECRET='test-only', PROFILES_DIR=str(profiles),
                REVIEW_SETTINGS_FILE=str(root/'settings'), PROMPT_FILE=str(root/'prompt'), WORKSPACES_DIR='/tmp/reviews',
                REVIEW_POLL_SECONDS='1', GITHUB_OWNER='owner', GITHUB_REPOS='owner/repo', REVIEW_ORG='owner',
                BOT_NAME='test-bot', MARKER='[test-bot review]', POLL_SECONDS='1', HOOK_PORT=str(hook_port),
                REVIEW_RUNS_DIR=str(root), PYTHONUNBUFFERED='1')
            env.pop('REVIEW_AUTHOR', None)
            if review_author:
                env['REVIEW_AUTHOR'] = review_author
            script = 'github_review_poller.py' if github else 'review_hook.py'
            proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name(script))], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                if not github:
                    body = json.dumps({'action': 'label_updated', 'repository': {'full_name': 'owner/repo'},
                                       'pull_request': {'number': 1, 'title': 'test', 'labels': [{'name': 'review-this'}]}}).encode()
                    request = urllib.request.Request(f'http://127.0.0.1:{hook_port}/hooks/gitea', data=body,
                        headers={'X-Gitea-Signature': hmac.new(b'test-only', body, hashlib.sha256).hexdigest()})
                    for _ in range(40):
                        try:
                            with urllib.request.urlopen(request, timeout=2) as response: self.assertEqual(response.status, 204)
                            break
                        except urllib.error.URLError: time.sleep(.05)
                success = outcome.wait(12)
                for _ in range(requests - 1):
                    if not success:
                        break
                    with guard:
                        outcome.clear(); labels.clear(); labels.add('review-this')
                    success = outcome.wait(12)
            finally:
                proc.terminate(); output = proc.communicate(timeout=5)[0]
                server.shutdown(); server.server_close(); thread.join()
        self.assertTrue(success, output)
        self.assertEqual(errors, [], output)
        return posted, compares, starts, labels, paths, (head, old)

    def assert_verdict_stands(self, posted, starts, labels, old):
        self.assertEqual(starts, [], 'no conversation is started')
        [note] = posted
        self.assertTrue(note.startswith(f'⚠️ [test-bot review] note: patch unchanged since {old}; previous verdict stands'), note)
        self.assertEqual(labels, {'hands-reviewed'})

    def test_github_adapter_keeps_the_verdict_of_an_unchanged_patch(self):
        # the review comment is trusted as the token owner's (GET /user)
        posted, compares, starts, labels, paths, (head, old) = self.run_unchanged(True)
        self.assert_verdict_stands(posted, starts, labels, old)
        self.assertEqual(compares, [(f'/forge/repos/owner/repo/compare/release/1...{old}', '', 'application/vnd.github.diff'),
                                    (f'/forge/repos/owner/repo/compare/release/1...{head}', '', 'application/vnd.github.diff')])
        self.assertEqual(paths.count('/forge/user'), 1)

    def test_github_adapter_reads_the_token_owner_once(self):
        # two requests in one poller process: the second uses the login read for the first
        posted, compares, starts, _, paths, _ = self.run_unchanged(True, requests=2)
        self.assertEqual((len(posted), len(compares), starts), (2, 4, []), 'both requests keep the verdict')
        self.assertEqual(paths.count('/forge/user'), 1)

    def test_github_adapter_trusts_only_the_reviewer_login(self):
        # REVIEW_AUTHOR names the login that posts the reviews; the token owner is then not read
        posted, _, starts, labels, paths, (_, old) = self.run_unchanged(True, author='review-app', review_author='Review-App')
        self.assert_verdict_stands(posted, starts, labels, old)
        self.assertNotIn('/forge/user', paths)
        # a review-looking comment by anyone else is not a review: the request is reviewed in full
        for name, kw in {'not the token owner': {'author': 'stranger'},
                         'not REVIEW_AUTHOR': {'author': 'test-bot', 'review_author': 'review-app'}}.items():
            with self.subTest(name):
                posted, compares, starts, _, _, _ = self.run_unchanged(True, **kw)
                self.assertEqual(([s['agent_profile_id'] for s in starts], posted, compares), (['codex-astra'], [], []))

    def test_gitea_adapter_keeps_the_verdict_of_an_unchanged_patch(self):
        # Gitea 1.27+ serves the compare as a raw diff with ?output=diff
        posted, compares, starts, labels, paths, (head, old) = self.run_unchanged(False)
        self.assert_verdict_stands(posted, starts, labels, old)
        self.assertEqual(compares, [(f'/forge/repos/owner/repo/compare/release/1...{old}', 'output=diff', 'text/plain'),
                                    (f'/forge/repos/owner/repo/compare/release/1...{head}', 'output=diff', 'text/plain')])
        self.assertNotIn('/forge/user', paths)
        # the receiver trusts only BOT_NAME's reviews
        posted, compares, starts, _, _, _ = self.run_unchanged(False, author='stranger')
        self.assertEqual(([s['agent_profile_id'] for s in starts], posted, compares), (['codex-astra'], [], []))

    def test_a_forge_read_that_fails_once_is_tried_again(self):
        # a brief forge outage (503) on a read must not fail the request: both receivers retry GETs
        for github in (True, False):
            with self.subTest('github' if github else 'gitea'):
                posted, _, starts, labels, paths, (_, old) = self.run_unchanged(
                    github, flaky={'/forge/repos/owner/repo/pulls/1': 1})
                self.assert_verdict_stands(posted, starts, labels, old)
                self.assertGreaterEqual(paths.count('/forge/repos/owner/repo/pulls/1'), 3, 'the failed read was asked again')

    def test_a_forge_write_is_never_sent_twice(self):
        # a write that failed may still have happened: the note's POST is not retried, so the
        # verdict check gives up and the request is reviewed in full instead
        for github in (True, False):
            with self.subTest('github' if github else 'gitea'):
                posted, _, starts, _, _, _ = self.run_unchanged(
                    github, flaky={'POST /forge/repos/owner/repo/issues/1/comments': 1})
                self.assertEqual(posted, [], 'the one note POST failed and was not sent again')
                self.assertEqual([s['agent_profile_id'] for s in starts], ['codex-astra'])

    def test_gitea_before_1_27_reviews_in_full(self):
        # an older Gitea ignores ?output=diff and answers JSON: no identity, a full review
        posted, compares, starts, labels, _, _ = self.run_unchanged(False, gitea_json=True)
        self.assertEqual(([s['agent_profile_id'] for s in starts], posted), (['codex-astra'], []))
        self.assertTrue(compares)

    def test_github_adapter_quota_fallback(self): self.run_adapter(True)
    def test_gitea_adapter_quota_fallback(self): self.run_adapter(False)
    def test_github_adapter_runs_a_request_after_a_failed_start(self): self.run_adapter(True, refused=True)
    def test_gitea_adapter_runs_a_request_after_a_failed_start(self): self.run_adapter(False, refused=True)
    def test_github_adapter_releases_a_pr_whose_label_cannot_be_cleaned(self): self.run_adapter(True, refused=True, stuck=True)
    def test_gitea_adapter_releases_a_pr_whose_label_cannot_be_cleaned(self): self.run_adapter(False, refused=True, stuck=True)
    def test_github_adapter_reports_a_start_whose_label_removal_lost_its_answer(self): self.run_adapter(True, lost=True)
    def test_gitea_adapter_reports_a_start_whose_label_removal_lost_its_answer(self): self.run_adapter(False, lost=True)
    def test_github_adapter_stays_quiet_while_the_request_label_remains(self): self.run_adapter(True, kept=True)
    def test_gitea_adapter_reports_a_start_whose_request_label_remains(self): self.run_adapter(False, kept=True)
    def test_github_adapter_falls_back_to_the_start_when_labels_cannot_be_read(self):
        self.run_adapter(True, refused=True, blind=True)
        self.run_adapter(True, kept=True, blind=True)
    def test_gitea_adapter_falls_back_to_the_start_when_labels_cannot_be_read(self):
        self.run_adapter(False, refused=True, blind=True)
        self.run_adapter(False, kept=True, blind=True)
    def test_gitea_adapter_ignores_its_own_label_changes(self): self.run_adapter(False, kept=True, own=True)
    def test_gitea_adapter_uses_up_its_own_change_without_a_request_label(self): self.run_adapter(False, refused=True, own=True)
    def test_gitea_adapter_serves_the_same_change_from_another_account(self): self.run_adapter(False, kept=True, own='other')
    def test_gitea_adapter_serves_a_request_from_its_own_account(self): self.run_adapter(False, own='idle')
    def test_github_adapter_serves_other_prs_past_a_failing_start(self): self.run_adapter(True, blocked=True)

    def run_restart(self, github, held=False, down=False, boundary=False, broken=False):
        """The receiver is killed while a review conversation runs and started again:
        the restarted service watches the same conversation, and its stale-label
        recovery leaves that PR alone. HELD: the service starts with a run on disk whose
        review was posted but never labelled (the forge was down at its deadline, #64
        item 7): it is labelled done, and the recovery, which no longer sees that review
        in its window, leaves the PR alone instead of calling the run lost. DOWN: the forge still
        refuses label writes at that start: the run is kept again, and recovery still leaves the PR
        alone (on GitHub the live labels no longer tell it so). BOUNDARY: such a held run is on
        disk when a new request arrives: it is gone before the request's labels change, so a
        restart from then on cannot answer the new request with the older review. BROKEN: the run
        state cannot be read at all: the request is still reviewed, and the receiver says so."""
        head = 'a' * 40
        names = ['review-this', 'hands-reviewing', 'hands-reviewed']
        labels = {'hands-reviewing'} if held else set() if boundary or broken else {'review-this'}
        at_working = []  # the run state file as the request's working label is set
        starts, failures, errors = [], [], []
        posted, done, searched, attempted = threading.Event(), threading.Event(), threading.Event(), threading.Event()
        def stale():
            # HELD: the search answers as the PR stood when it was asked, after the run has ended
            # (a search index lags; the run takes no time): only the run's own claim keeps recovery off
            if held:
                (attempted if down else done).wait(10); time.sleep(.5)
        item = {'repository_url': 'https://example.test/repos/owner/repo', 'repository': {'full_name': 'owner/repo'}, 'number': 1, 'title': 'test'}
        class Fake(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self): self.handle_request()
            def do_POST(self): self.handle_request()
            def do_DELETE(self): self.handle_request()
            def handle_request(self):
                path = urllib.parse.unquote(self.path.split('?')[0])
                body = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.headers.get('Content-Length') else None
                result = None
                if path == '/forge/search/issues':
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)['q'][0]
                    if 'label:' in query:  # the restart recovery's search for working PRs
                        items = [dict(item, labels=[{'name': n} for n in labels])] if 'hands-reviewing' in labels else []
                        stale(); searched.set()
                    else: items = [dict(item, labels=[{'name': n} for n in labels])]
                    result = {'items': items}
                elif path == '/forge/repos/issues/search':
                    result = [item] if 'hands-reviewing' in labels else []
                    stale(); searched.set()
                elif down and self.command in ('POST', 'DELETE') and '/issues/1/labels' in path:
                    attempted.set(); self.send_response(503); self.send_header('Content-Length', '0'); self.end_headers(); return
                elif path == '/forge/repos/owner/repo/pulls/1': result = {'state': 'open', 'head': {'sha': head}}
                elif path == '/forge/repos/owner/repo/issues/1/labels':
                    if self.command == 'POST':
                        if boundary and (2 in body['labels'] or 'hands-reviewing' in body['labels']):
                            at_working.append(runs_file.read_text() if runs_file.is_file() else '')
                        labels.update(body['labels'] if github else (names[i-1] for i in body['labels']))
                        if 'hands-reviewed' in labels: done.set()
                    result = [{'name': n, 'id': names.index(n)+1} for n in labels]
                elif path.startswith('/forge/repos/owner/repo/issues/1/labels/'):
                    name = path.split('/')[-1]
                    labels.discard(name if github else names[int(name)-1])
                elif path == '/forge/orgs/owner/labels' or path == '/forge/repos/owner/repo/labels':
                    result = [{'name': n, 'id': i+1} for i,n in enumerate(names)]
                elif path.startswith('/forge/repos/owner/repo/labels/'):
                    result = {'name': path.split('/')[-1]}
                elif path == '/forge/repos/owner/repo/issues/1/comments':
                    if self.command == 'POST': failures.append(body)
                    result = [{'user': {'login': 'test-bot'}, 'body': f'[test-bot review] reviewed at head {head}\n\nVERDICT\nREADY_FOR_HUMAN_MERGE'}] if posted.is_set() else []
                elif path == '/api/conversations':
                    starts.append(body); result = {'id': 'conv-' + str(len(starts))}
                elif path.startswith('/api/conversations/'):
                    result = {'execution_status': 'running'}
                else: errors.append((self.command, path))
                payload = json.dumps(result).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); profiles = root/'profiles'; profiles.mkdir()
            (profiles/'codex-astra.json').write_text(json.dumps({'id': 'codex-astra','agent_kind':'acp','acp_model':'codex-astra'}))
            (root/'token').write_text('test-only')
            (root/'settings').write_text(json.dumps({'revision':1,'primary':'codex-astra','fallback':None}))
            (root/'prompt').write_text('Review {repo} #{num}')
            with socket.socket() as sock: sock.bind(('127.0.0.1',0)); hook_port = sock.getsockname()[1]
            base = f'http://127.0.0.1:{server.server_port}'
            env = dict(os.environ, GITHUB_API=base+'/forge', GITEA_API=base+'/forge',
                GITHUB_TOKEN_FILE=str(root/'token'), GITEA_TOKEN_FILE=str(root/'token'),
                OPENHANDS_API=base, LOCAL_BACKEND_API_KEY='test-only', HOOK_SECRET='test-only',
                PROFILES_DIR=str(profiles), REVIEW_SETTINGS_FILE=str(root/'settings'),
                PROMPT_FILE=str(root/'prompt'), WORKSPACES_DIR='/tmp/reviews', REVIEW_POLL_SECONDS='1',
                GITHUB_OWNER='owner', GITHUB_REPOS='owner/repo', REVIEW_ORG='owner',
                BOT_NAME='test-bot', MARKER='[test-bot review]', POLL_SECONDS='1', HOOK_PORT=str(hook_port),
                REVIEW_RUNS_DIR=str(root), PYTHONUNBUFFERED='1')
            script = str(Path(__file__).with_name('github_review_poller.py' if github else 'review_hook.py'))
            runs_file = root / ('review-runs-github.json' if github else 'review-runs-gitea.json')
            def start(): return subprocess.Popen([sys.executable, script], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            def wait_for(condition, seconds=12):
                for _ in range(int(seconds / .05)):
                    if condition(): return True
                    time.sleep(.05)
                return False
            if held:
                now = time.time()
                runs_file.write_text(json.dumps({'owner/repo#1': {
                    'repo': 'owner/repo', 'num': 1, 'title': 'test', 'label': 'review-this', 'profile': 'codex-astra',
                    'choices': ['codex-astra'], 'reading': None, 'head': head, 'since': '2026-01-01T00:00:00Z',
                    'attempt': 0, 'conversation': 'conv-0', 'started': now - 7200, 'deadline': now - 4500, 'posted': True}}))
                proc = start(); outputs = []
                try:
                    success = (attempted if down else done).wait(12)
                    self.assertTrue(wait_for(searched.is_set), 'the service never ran its recovery')
                    time.sleep(.5)  # the recovery would act on the PR here, from its stale search answer
                finally:
                    proc.terminate(); outputs.append(proc.communicate(timeout=5)[0])
                    server.shutdown(); server.server_close(); thread.join()
                self.assertTrue(success, outputs[-1])
                self.assertNotIn('restarted mid-run', outputs[0]); self.assertNotIn('recover error', outputs[0])
                if down:
                    self.assertIn('posted review kept for the next start: owner/repo#1', outputs[0])
                    self.assertEqual((starts, failures, errors, labels), ([], [], [], {'hands-reviewing'}))
                    self.assertEqual([r.get('posted') for r in json.loads(runs_file.read_text()).values()], [True])
                    return
                self.assertIn('review done (posted before the restart): owner/repo#1', outputs[0])
                self.assertEqual((starts, failures, errors), ([], [], []))  # no conversation, nothing said on the PR
                self.assertEqual(labels, {'hands-reviewed'})
                self.assertEqual(json.loads(runs_file.read_text()), {})
                return
            proc = start(); outputs = []
            try:
                if boundary:  # once the service is up, an older run's posted review is held, then a request comes
                    self.assertTrue(wait_for(searched.is_set), 'the service never ran its recovery')
                    now = time.time()
                    runs_file.write_text(json.dumps({'owner/repo#1': {
                        'repo': 'owner/repo', 'num': 1, 'title': 'test', 'label': 'review-this', 'profile': 'codex-astra',
                        'choices': ['codex-astra'], 'reading': None, 'head': head, 'since': '2026-01-01T00:00:00Z',
                        'attempt': 0, 'conversation': 'conv-0', 'started': now - 7200, 'deadline': now - 4500, 'posted': True}}))
                    labels.add('review-this')
                if broken:  # the file cannot be read (a directory stands in for an I/O error), once the service is up
                    self.assertTrue(wait_for(searched.is_set), 'the service never ran its recovery')
                    runs_file.mkdir(); labels.add('review-this')
                if not github:
                    body = json.dumps({'action':'label_updated','repository':{'full_name':'owner/repo'},'pull_request':{'number':1,'title':'test','labels':[{'name':'review-this'}]}}).encode()
                    request = urllib.request.Request(f'http://127.0.0.1:{hook_port}/hooks/gitea',data=body,
                        headers={'X-Gitea-Signature':hmac.new(b'test-only',body,hashlib.sha256).hexdigest()})
                    for _ in range(40):
                        try:
                            with urllib.request.urlopen(request,timeout=2) as response: self.assertEqual(response.status,204)
                            break
                        except urllib.error.URLError: time.sleep(.05)
                def recorded():
                    try: return runs_file.is_file() and 'conv-1' in runs_file.read_text()
                    except OSError: return False
                if broken:
                    started = wait_for(lambda: len(starts) == 1)
                    proc.terminate(); outputs.append(proc.communicate(timeout=5)[0])
                    self.assertTrue(started, outputs[-1])
                    self.assertIn('run state not updated for the new request: owner/repo#1', outputs[-1])
                    self.assertEqual(labels, {'hands-reviewing'})
                    return
                started = wait_for(lambda: len(starts) == 1 and recorded())
                proc.terminate(); outputs.append(proc.communicate(timeout=5)[0])
                self.assertTrue(started, outputs[-1])
                self.assertEqual(labels, {'hands-reviewing'})
                record = json.loads(runs_file.read_text())['owner/repo#1']
                self.assertEqual((record['conversation'], record['head'], record['attempt']), ('conv-1', head, 0))
                self.assertAlmostEqual(record['deadline'] - record['started'], 45 * 60, places=3)
                proc = start()  # the restart: the run is re-attached, then the stale-label recovery runs
                self.assertTrue(wait_for(searched.is_set), 'the restarted service never ran its recovery')
                posted.set()  # the review comment lands after the restart
                success = done.wait(12)
                def cleared():
                    try: return json.loads(runs_file.read_text()) == {}
                    except (OSError, ValueError): return False
                wait_for(cleared)  # the run logs its completion, then drops its record
            finally:
                proc.terminate(); outputs.append(proc.communicate(timeout=5)[0])
                server.shutdown(); server.server_close(); thread.join()
            self.assertTrue(success, outputs[-1])
            self.assertIn('review attempt: owner/repo#1', outputs[0]); self.assertIn('conversation=conv-1', outputs[0])
            self.assertIn('review resume: owner/repo#1 conversation=conv-1', outputs[1])
            self.assertIn('review resumed: owner/repo#1', outputs[1]); self.assertIn('review done: owner/repo#1', outputs[1])
            self.assertNotIn('recovered after restart', outputs[1]); self.assertNotIn('restarted mid-run', outputs[1])
            self.assertEqual(len(starts), 1)  # the same conversation, no second one
            self.assertFalse(failures); self.assertFalse(errors)
            self.assertEqual(labels, {'hands-reviewed'})
            self.assertEqual(json.loads(runs_file.read_text()), {})  # nothing left to resume
            if boundary:
                self.assertEqual(len(at_working), 1, at_working)
                self.assertNotIn('posted', at_working[0], 'the held run outlived the new request\'s label change')

    def test_github_adapter_resumes_after_restart(self): self.run_restart(True)
    def test_gitea_adapter_resumes_after_restart(self): self.run_restart(False)
    def test_github_adapter_labels_a_held_posted_review_at_start(self): self.run_restart(True, held=True)
    def test_gitea_adapter_labels_a_held_posted_review_at_start(self): self.run_restart(False, held=True)
    def test_github_adapter_drops_a_held_review_before_a_new_request(self): self.run_restart(True, boundary=True)
    def test_gitea_adapter_drops_a_held_review_before_a_new_request(self): self.run_restart(False, boundary=True)
    def test_github_adapter_reviews_when_the_run_state_is_unreadable(self): self.run_restart(True, broken=True)
    def test_gitea_adapter_reviews_when_the_run_state_is_unreadable(self): self.run_restart(False, broken=True)
    def test_github_adapter_keeps_a_held_review_while_the_forge_is_down(self): self.run_restart(True, held=True, down=True)
    def test_gitea_adapter_keeps_a_held_review_while_the_forge_is_down(self): self.run_restart(False, held=True, down=True)


if __name__ == '__main__': unittest.main()
