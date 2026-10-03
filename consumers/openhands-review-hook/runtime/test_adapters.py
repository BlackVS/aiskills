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
    def run_adapter(self, github):
        head = 'a' * 40
        names = ['review-this', 'hands-reviewing', 'hands-reviewed', 'review-this:codex-astra', 'review-this:claude-opus']
        labels = {'review-this'}
        starts, failures, errors = [], [], []
        done = threading.Event()
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
                    result = {'items': items}
                elif path == '/forge/repos/issues/search': result = []
                elif path == '/forge/repos/owner/repo/pulls/1': result = {'state': 'open', 'head': {'sha': head}}
                elif path == '/forge/repos/owner/repo/issues/1/labels':
                    if self.command == 'POST':
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
            try:
                if not github:
                    body = json.dumps({'action':'label_updated','repository':{'full_name':'owner/repo'},'pull_request':{'number':1,'title':'test','labels':[{'name':'review-this'}]}}).encode()
                    request = urllib.request.Request(f'http://127.0.0.1:{hook_port}/hooks/gitea',data=body,
                        headers={'X-Gitea-Signature':hmac.new(b'test-only',body,hashlib.sha256).hexdigest()})
                    for _ in range(40):
                        try:
                            with urllib.request.urlopen(request,timeout=2) as response: self.assertEqual(response.status,204)
                            break
                        except urllib.error.URLError: time.sleep(.05)
                success = done.wait(12)
            finally:
                proc.terminate(); output = proc.communicate(timeout=5)[0]
                server.shutdown(); server.server_close(); thread.join()
            self.assertTrue(success,output)
            self.assertEqual([s['agent_profile_id'] for s in starts],['codex-astra','claude-opus'])
            self.assertFalse(failures); self.assertFalse(errors)
            self.assertEqual(labels,{'hands-reviewed'})
            self.assertIn(head,starts[1]['initial_message']['content'][0]['text'])

    def run_unchanged(self, github, author='test-bot', owner='test-bot', review_author=None, gitea_json=False):
        """The newest review, by `author`, is of an older head; the forge's compare diff of both
        heads has the same patch identity. Returns what the adapter did: the posted comments,
        the compares read, the conversations started, the labels, the forge paths read, errors."""
        head, old = 'a' * 40, 'b' * 40
        names = ['review-this', 'hands-reviewing', 'hands-reviewed']
        labels, posted, compares, starts, paths, errors = {'review-this'}, [], [], [], [], []
        outcome = threading.Event()  # labelled done, or a conversation started
        diff = b'diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n'
        class Fake(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self): self.handle_request()
            def do_POST(self): self.handle_request()
            def do_DELETE(self): self.handle_request()
            def handle_request(self):
                path = urllib.parse.unquote(self.path.split('?')[0])
                query = urllib.parse.urlsplit(self.path).query
                body = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.headers.get('Content-Length') else None
                result, raw = None, None
                paths.append(path)
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

    def test_gitea_before_1_27_reviews_in_full(self):
        # an older Gitea ignores ?output=diff and answers JSON: no identity, a full review
        posted, compares, starts, labels, _, _ = self.run_unchanged(False, gitea_json=True)
        self.assertEqual(([s['agent_profile_id'] for s in starts], posted), (['codex-astra'], []))
        self.assertTrue(compares)

    def test_github_adapter_quota_fallback(self): self.run_adapter(True)
    def test_gitea_adapter_quota_fallback(self): self.run_adapter(False)

    def run_restart(self, github):
        """The receiver is killed while a review conversation runs and started again:
        the restarted service watches the same conversation, and its stale-label
        recovery leaves that PR alone."""
        head = 'a' * 40
        names = ['review-this', 'hands-reviewing', 'hands-reviewed']
        labels = {'review-this'}
        starts, failures, errors = [], [], []
        posted, done, searched = threading.Event(), threading.Event(), threading.Event()
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
                        searched.set(); items = [dict(item, labels=[{'name': n} for n in labels])] if 'hands-reviewing' in labels else []
                    else: items = [dict(item, labels=[{'name': n} for n in labels])]
                    result = {'items': items}
                elif path == '/forge/repos/issues/search':
                    searched.set(); result = [item] if 'hands-reviewing' in labels else []
                elif path == '/forge/repos/owner/repo/pulls/1': result = {'state': 'open', 'head': {'sha': head}}
                elif path == '/forge/repos/owner/repo/issues/1/labels':
                    if self.command == 'POST':
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
            proc = start(); outputs = []
            try:
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

    def test_github_adapter_resumes_after_restart(self): self.run_restart(True)
    def test_gitea_adapter_resumes_after_restart(self): self.run_restart(False)


if __name__ == '__main__': unittest.main()
