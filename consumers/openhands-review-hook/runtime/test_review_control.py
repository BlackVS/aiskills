import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

import review_policy
from review_control import Handler
from review_runner import Runner, RunStore, limit_error, valid_review, config_error, recovered_head, switched, next_page

HEAD = 'a' * 40
QUOTA = {'items': [{'kind': 'ConversationErrorEvent', 'code': 'ACPPromptError',
                    'detail': 'limit: {"errorKind": "rate_limit"}'}]}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ['codex-astra', 'claude-opus', 'claude-fable']:
            (self.root / (name + '.json')).write_text(json.dumps({'acp_model': name}))
        for name, ref, switch in [('api-deep', 'auto-review-deep', False), ('api-fast', 'auto-review-fast', True), ('api-alt', 'auto-review-alt', False), ('api-noswitch', 'auto-review-noswitch', False)]:
            (self.root / (name + '.json')).write_text(json.dumps({'agent_kind': 'openhands', 'llm_profile_ref': ref, 'enable_switch_llm_tool': switch}))
        self.env = patch.dict(os.environ, {'PROFILES_DIR': str(self.root),
            'REVIEW_SETTINGS_FILE': str(self.root / 'settings'),
            'LOCAL_BACKEND_API_KEY': 'test-key', 'REVIEW_PROFILE': 'codex-astra'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_persist_and_revision(self):
        settings = review_policy.read_settings()
        settings['fallback'] = 'claude-opus'
        saved = review_policy.save_settings(settings)
        self.assertEqual(saved, review_policy.read_settings())
        self.assertIsNone(saved['secondary'])
        with self.assertRaises(FileExistsError): review_policy.save_settings(settings)

    def test_invalid_profiles_and_same_model(self):
        for primary, fallback in [('../secret', None), ('codex-astra', 'unknown'), ('codex-astra', 'codex-astra')]:
            with self.assertRaises(ValueError):
                review_policy.save_settings({'revision': 0, 'primary': primary, 'fallback': fallback})

    def test_legacy_file_without_secondary_loads_as_none(self):
        review_policy.settings_path().write_text(json.dumps({'revision': 3, 'primary': 'codex-astra', 'fallback': None}))
        loaded = review_policy.read_settings()
        self.assertEqual((loaded['revision'], loaded['secondary']), (3, None))
        self.assertIsNone(review_policy.read_settings()['secondary'])
        review_policy.save_settings(loaded)
        self.assertNotIn('secondary', review_policy.settings_path().read_text())  # rollback-safe while unset
        review_policy.save_settings({'revision': 4, 'primary': 'api-deep', 'secondary': 'api-fast', 'fallback': None})
        self.assertIn('"secondary": "api-fast"', review_policy.settings_path().read_text())

    def test_secondary_needs_two_llm_profile_agents(self):
        ok = review_policy.save_settings({'revision': 0, 'primary': 'api-deep', 'secondary': 'api-fast', 'fallback': 'claude-opus'})
        self.assertEqual(ok['secondary'], 'api-fast')
        self.assertEqual(review_policy.profile_llm_ref('api-fast'), 'auto-review-fast')
        self.assertIsNone(review_policy.profile_llm_ref('codex-astra')); self.assertIsNone(review_policy.profile_llm_ref('../x'))
        for primary, secondary, fallback in [('api-deep', 'api-deep', None), ('codex-astra', 'api-fast', None),
                                             ('api-deep', 'codex-astra', None), ('api-deep', 'api-fast', 'api-fast'), ('api-deep', 'missing', None),
                                             ('api-deep', 'api-noswitch', None)]:  # OpenHands-kind but without the switch tool
            with self.subTest(primary=primary, secondary=secondary, fallback=fallback), self.assertRaises(ValueError):
                review_policy.validate({'revision': 1, 'primary': primary, 'secondary': secondary, 'fallback': fallback})

    def test_secondary_must_match_the_primary_beyond_the_generated_fields(self):
        # the conversation runs on the secondary's agent: a primary with its own tools or condenser would be silently ignored
        self.assertEqual(review_policy.agent_settings_diff('api-deep', 'api-fast'), [])
        fast = json.loads((self.root / 'api-fast.json').read_text())
        (self.root / 'api-fast.json').write_text(json.dumps(dict(fast, tools=['bash'], condenser={'max_size': 100})))
        self.assertEqual(review_policy.agent_settings_diff('api-deep', 'api-fast'), ['condenser', 'tools'])
        with self.assertRaises(ValueError) as caught:
            review_policy.validate({'revision': 1, 'primary': 'api-deep', 'secondary': 'api-fast', 'fallback': None})
        self.assertIn('differs in: condenser, tools', str(caught.exception))
        problems = review_policy.settings_problems({'primary': 'api-deep', 'secondary': 'api-fast', 'fallback': None})
        self.assertIn('condenser, tools', problems['secondary']); self.assertIn("not the primary's", problems['secondary'])
        (self.root / 'api-fast.json').write_text(json.dumps(dict(fast, name='api-fast', revision=3)))  # generated fields never count
        self.assertEqual(review_policy.agent_settings_diff('api-deep', 'api-fast'), [])
        self.assertEqual(review_policy.agent_settings_diff('api-deep', 'missing'), ['(unreadable profile)'])

    def test_hot_reload_and_explicit_override(self):
        review_policy.save_settings({'revision': 0, 'primary': 'claude-opus', 'fallback': None})
        self.assertEqual(review_policy.requested_profiles(['review-this'], 'review-this'), [('review-this', None)])  # resolved by the runner
        self.assertEqual(review_policy.requested_profiles(['review-this', 'review-this:claude-fable'], 'review-this')[0][1], 'claude-fable')

    def test_ambiguous_override_rejected(self):
        with self.assertRaises(ValueError):
            review_policy.requested_profiles(['review-this:claude-fable', 'review-this:codex-astra'], 'review-this')

    def test_deleted_profile_is_reported_never_rewritten(self):
        review_policy.settings_path().write_text(json.dumps({'revision': 4, 'primary': 'ghost', 'fallback': 'claude-opus'}))
        loaded = review_policy.read_settings()
        self.assertEqual((loaded['primary'], loaded['revision']), ('ghost', 4))
        self.assertEqual(review_policy.settings_problems(loaded), {'primary': "agent profile 'ghost' no longer exists"})
        with self.assertRaises(ValueError): review_policy.save_settings(dict(loaded))  # writing stays strict
        self.assertIn('"ghost"', review_policy.settings_path().read_text())
        degraded = {'revision': 4, 'primary': 'api-deep', 'secondary': 'api-alt', 'fallback': None}  # secondary lost its switch tool
        self.assertEqual(list(review_policy.settings_problems(degraded)), ['secondary'])
        self.assertEqual(review_policy.settings_problems({'revision': 4, 'primary': 'api-deep', 'secondary': 'api-fast', 'fallback': 'claude-opus'}), {})
        self.assertEqual(set(review_policy.settings_problems({'revision': 4, 'primary': 'api-deep', 'secondary': 'ghost2', 'fallback': 'ghost3'})), {'secondary', 'fallback'})
        (self.root / 'half.json').write_text('{'); self.assertNotIn('half', [p['name'] for p in review_policy.profiles()])

    def test_control_reports_problems_with_settings(self):
        review_policy.settings_path().write_text(json.dumps({'revision': 2, 'primary': 'ghost', 'fallback': None}))
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/api/review-control/settings'
            with urllib.request.urlopen(urllib.request.Request(url, headers={'X-Session-API-Key': 'test-key'})) as response:
                body = json.load(response)
            self.assertEqual((body['settings']['primary'], body['problems']), ('ghost', {'primary': "agent profile 'ghost' no longer exists"}))
            review_policy.settings_path().write_text('broken')
            with urllib.request.urlopen(urllib.request.Request(url, headers={'X-Session-API-Key': 'test-key'})) as response:
                body = json.load(response)
            self.assertEqual((body['settings']['revision'], body['settings']['primary']), (0, None)); self.assertIn('settings', body['problems'])
            headers = {'X-Session-API-Key': 'test-key', 'Content-Type': 'application/json'}
            with patch('review_control.discovery', return_value={'profile': 'claude-opus'}):
                stale = json.dumps({'revision': 3, 'primary': {'provider': 'acp:claude-code', 'model': 'opus[1m]'}, 'fallback': None}).encode()
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(urllib.request.Request(url, method='PUT', data=stale, headers=headers))
                self.assertEqual(caught.exception.code, 409)
                fresh = json.dumps({'revision': 0, 'primary': {'provider': 'acp:claude-code', 'model': 'opus[1m]'}, 'fallback': None}).encode()
                with urllib.request.urlopen(urllib.request.Request(url, method='PUT', data=fresh, headers=headers)) as response:
                    saved = json.load(response)
            self.assertEqual((saved['settings']['revision'], saved['settings']['primary'], saved['problems']), (1, 'claude-opus', {}))
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_corrupt_configuration_does_not_reset(self):
        review_policy.settings_path().write_text('broken')
        with self.assertRaises(ValueError): review_policy.read_settings()
        data, problems = review_policy.load_settings()  # the app still loads, nothing selected, and says why
        self.assertEqual(data, {'revision': 0, 'primary': None, 'secondary': None, 'fallback': None})
        self.assertIn('cannot be read', problems['settings']); self.assertEqual(review_policy.settings_path().read_text(), 'broken')
        with self.assertRaises(FileExistsError): review_policy.save_settings({'revision': 3, 'primary': 'claude-opus', 'fallback': None})
        saved = review_policy.save_settings({'revision': 0, 'primary': 'claude-opus', 'fallback': None})  # only an explicit save at revision 0 replaces it
        self.assertEqual((saved['revision'], review_policy.read_settings()['primary']), (1, 'claude-opus'))

    def test_connection_probe_authenticated_and_does_not_save(self):
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = f'http://127.0.0.1:{server.server_port}/api/review-control/test-provider'
        body = json.dumps({'base_url': 'https://example.com', 'api_key': 'test-only'}).encode()
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'}))
            self.assertEqual(caught.exception.code, 401)
            with patch('review_control.discovery', return_value={'ok': False, 'message': 'Authentication rejected.'}) as discovery:
                request = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json', 'X-Session-API-Key': 'test-key'})
                with urllib.request.urlopen(request) as response:
                    self.assertFalse(json.load(response)['ok'])
                discovery.assert_called_once_with('probe', request={'base_url': 'https://example.com', 'api_key': 'test-only'})
            self.assertFalse(review_policy.settings_path().exists())
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_a_refused_body_is_drained_before_the_close(self):
        # Closing with unread body bytes resets the connection, and on Windows a client that has not
        # read the answer yet loses it (#36). The handler reads the rest of a refused body (bounded)
        # first. Checked on any platform by peeking at the connection after the answer.
        import socket
        import review_control
        unread = []
        class Peek(Handler):
            def finish(self):
                super().finish()
                self.connection.settimeout(0)
                try:
                    unread.append(self.connection.recv(1, socket.MSG_PEEK))  # b'': the client closed, nothing left
                except BlockingIOError:
                    unread.append(None)  # open, nothing waiting
        server = HTTPServer(('127.0.0.1', 0), Peek)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        # more than the handler's read buffer takes with the headers (io.DEFAULT_BUFFER_SIZE), with a rest
        # that stays within the drain's bound, whatever the Python's default buffer size
        body = b'{' + b' ' * (io.DEFAULT_BUFFER_SIZE + review_control.MAX_DRAIN // 2) + b'}'
        def refused(headers):
            raw = socket.create_connection(('127.0.0.1', server.server_port), timeout=10)
            try:
                head = ''.join(f'{k}: {v}\r\n' for k, v in headers.items())
                raw.sendall(f'PUT /api/review-control/settings HTTP/1.1\r\nHost: x\r\n{head}\r\n'.encode() + body)
                answer = b''
                while True:
                    try:
                        chunk = raw.recv(65536)
                    except ConnectionResetError:
                        # a close with body bytes left unread resets the connection (the bounded case
                        # below does that on purpose), and on macOS the reset can overtake the answer:
                        # the refusals then lose their status line and fail on it
                        chunk = b''
                    if not chunk:
                        return answer.split(b'\r\n')[0]
                    answer += chunk
            finally:
                raw.close()
        cases = {
            '413 too large': ({'Content-Length': str(len(body))}, b'413'),
            '400 bad length': ({'Content-Length': 'many'}, b'400'),
            '415 wrong type': ({'Content-Type': 'text/plain', 'Content-Length': str(len(body))}, b'415'),
            '401 wrong key': ({'X-Session-API-Key': 'wrong', 'Content-Length': str(len(body))}, b'401'),
        }
        try:
            for name, (headers, status) in cases.items():
                with self.subTest(name):
                    unread.clear()
                    line = refused({'X-Session-API-Key': 'test-key', 'Content-Type': 'application/json', **headers})
                    self.assertIn(status, line)
                    for _ in range(100):  # the handler's finish runs after the client has its answer
                        if unread:
                            break
                        time.sleep(0.01)
                    self.assertIn(unread, ([b''], [None]), 'no body bytes left unread at the close')
            with self.subTest('the drain is bounded'), patch.object(review_control, 'MAX_DRAIN', 1024):
                unread.clear()
                refused({'X-Session-API-Key': 'test-key', 'Content-Type': 'application/json', 'Content-Length': str(len(body))})
                for _ in range(100):
                    if unread:
                        break
                    time.sleep(0.01)
                self.assertEqual(unread, [b' '], 'a body past MAX_DRAIN is left unread, not read to the end')
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_json_endpoints_refuse_bad_bodies_before_any_discovery(self):
        import http.client
        import subprocess
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def send(method, path, body=b'', **headers):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            try:
                headers = {'X-Session-API-Key': 'test-key', 'Content-Type': 'application/json',
                           'Content-Length': str(len(body)), **headers}
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally: connection.close()
        probe, settings = '/api/review-control/test-provider', '/api/review-control/settings'
        valid = json.dumps({'base_url': 'https://example.com', 'api_key': 'test-only'}).encode()
        try:
            with patch('review_control.discovery') as discovery:
                for method, path in [('POST', probe), ('PUT', settings)]:
                    for expected, body, headers in [
                            (415, valid, {'Content-Type': 'text/plain'}),
                            (413, b'', {}),
                            (413, b'{' + b' ' * 4096 + b'}', {}),
                            (413, valid, {'Transfer-Encoding': 'chunked'}),
                            (400, valid, {'Content-Length': 'many'}),
                            (400, b'{not json', {})]:
                        with self.subTest(method=method, status=expected, headers=headers):
                            status, answer = send(method, path, body, **headers)
                            self.assertEqual(status, expected)
                            # one shape per endpoint: the connection test {ok, message}, settings {error}
                            if path == probe:
                                self.assertEqual((set(answer), answer['ok'], type(answer['message'])), ({'ok', 'message'}, False, str))
                            else:
                                self.assertEqual((set(answer), type(answer['error'])), ({'error'}, str))
                    with self.subTest(method=method, status=401):
                        status, answer = send(method, path, valid, **{'X-Session-API-Key': 'wrong'})
                        self.assertEqual(status, 401)
                        self.assertEqual(answer, {'ok': False, 'message': 'Authentication required'} if path == probe else {'error': 'Authentication required'})
                self.assertEqual(send('POST', '/api/review-control/other', valid), (404, {'error': 'Not found'}))
                self.assertEqual(send('GET', probe), (404, {'ok': False, 'message': 'Not found'}), 'the connection test path keeps its shape for any method')
                for body in [[], {}, {'provider': ''}, {'provider': 7}, {'base_url': 'https://example.com'},
                             {'provider': 'connection:one', 'api_key': 'key'}, dict(json.loads(valid), extra='x')]:
                    with self.subTest(body=body):
                        self.assertEqual(send('POST', probe, json.dumps(body).encode()), (400, {'ok': False, 'message': 'Invalid connection test'}))
                discovery.assert_not_called()
            unavailable = {'ok': False, 'message': 'Connection test unavailable or timed out. Try again.'}
            for error in [OSError('Canvas discovery failed'), subprocess.TimeoutExpired('docker', 55)]:
                with self.subTest(error=type(error)), patch('review_control.discovery', side_effect=error):
                    self.assertEqual(send('POST', probe, valid), (200, unavailable))
            self.assertFalse(review_policy.settings_path().exists())
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_control_plane_failures_keep_the_user_text_and_log_a_safe_reason(self):
        import io
        import subprocess
        from types import SimpleNamespace
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}/api/review-control'
        headers = {'X-Session-API-Key': 'test-key', 'Content-Type': 'application/json'}
        body = json.dumps({'base_url': 'https://example.com', 'api_key': 'test-only'}).encode()
        def answer(request):
            try:
                with urllib.request.urlopen(request) as response: return response.status, json.load(response)
            except urllib.error.HTTPError as error: return error.code, json.load(error)
        secret = 'leaked-provider-token'  # what a failing helper might print: it must never reach the log
        failures = [
            (SimpleNamespace(returncode=1, stdout=secret), 'helper exited with status 1'),
            (SimpleNamespace(returncode=0, stdout='Traceback ' + secret), 'helper output is not JSON'),
            (SimpleNamespace(returncode=0, stdout=json.dumps([secret])), 'helper output is not a JSON object'),
            (SimpleNamespace(returncode=0, stdout=json.dumps({'error': secret})), 'helper reported an error'),
            (subprocess.TimeoutExpired(['docker', 'exec', secret], 55), 'helper timed out after 55 s'),
            (FileNotFoundError(2, 'No such file or directory', secret), 'FileNotFoundError (errno 2)'),
        ]
        try:
            for outcome, reason in failures:
                with self.subTest(reason=reason), patch('sys.stderr', new_callable=io.StringIO) as log, \
                     patch('review_control.subprocess.run', **({'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome})):
                    probe = answer(urllib.request.Request(base + '/test-provider', data=body, headers=headers))
                    self.assertEqual(probe, (200, {'ok': False, 'message': 'Connection test unavailable or timed out. Try again.'}), 'the user text is unchanged')
                    providers = answer(urllib.request.Request(base + '/providers', headers=headers))
                    self.assertEqual(providers[0], 503, 'a helper failure is never answered as an invalid request')
                    self.assertEqual(log.getvalue().splitlines(), [
                        f'review-control: connection test unavailable: {reason}',
                        f'review-control: GET /api/review-control/providers unavailable: {reason}'])
                    self.assertNotIn(secret, log.getvalue())
            self.assertFalse(review_policy.settings_path().exists())
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_a_full_profile_store_answers_409_with_the_helper_counts_and_saves_nothing(self):
        import io
        from types import SimpleNamespace
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = f'http://127.0.0.1:{server.server_port}/api/review-control/settings'
        headers = {'X-Session-API-Key': 'test-key', 'Content-Type': 'application/json'}
        text = 'Canvas holds 50 agent profiles, its limit of 50; 35 of them were generated by Auto Reviews'
        body = json.dumps({'revision': 0, 'primary': {'provider': 'connection:x', 'model': 'm', 'effort': None},
                           'secondary': None, 'fallback': None}).encode()
        try:
            for document, status, error in [({'error': 'profile-limit', 'message': text}, 409, text),
                                            ({'error': 'profile-limit', 'message': ['not text']}, 503, None),
                                            ({'error': 'profile-limit'}, 503, None)]:
                with self.subTest(document=document), patch('sys.stderr', new_callable=io.StringIO), \
                     patch('review_control.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(document))):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(urllib.request.Request(url, method='PUT', data=body, headers=headers))
                    self.assertEqual(caught.exception.code, status)
                    answer = json.load(caught.exception)['error']
                    if error: self.assertEqual(answer, error)
                    else: self.assertNotIn('profile', answer.lower(), 'a malformed answer is a plain helper failure')
            self.assertFalse(review_policy.settings_path().exists(), 'nothing saved')
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_settings_with_secondary_prepare_a_switch_profile(self):
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = f'http://127.0.0.1:{server.server_port}/api/review-control/settings'
        headers = {'X-Session-API-Key': 'test-key', 'Content-Type': 'application/json'}
        deep = {'provider': 'connection:x', 'model': 'm', 'effort': 'high'}
        fast = {'provider': 'connection:x', 'model': 'm', 'effort': 'low'}
        prepared = lambda action, **kw: {'profile': 'api-fast' if kw.get('switch') else 'api-deep'}
        try:
            with patch('review_control.discovery', side_effect=prepared) as discovery:
                body = json.dumps({'revision': 0, 'primary': deep, 'secondary': fast, 'fallback': None}).encode()
                with urllib.request.urlopen(urllib.request.Request(url, method='PUT', data=body, headers=headers)) as response:
                    saved = json.load(response)['settings']
                self.assertEqual((saved['primary'], saved['secondary'], saved['fallback']), ('api-deep', 'api-fast', None))
                discovery.assert_any_call('prepare', selection=deep, switch=False)
                discovery.assert_any_call('prepare', selection=fast, switch=True)
            for primary, secondary in [(dict(deep, provider='acp:codex'), fast), (deep, dict(fast, provider='acp:codex')), (deep, deep),
                                       (dict(deep, effort=None), dict(fast, effort='high'))]:  # null effort is the API default
                with self.subTest(primary=primary), patch('review_control.discovery', side_effect=prepared) as discovery:
                    body = json.dumps({'revision': 1, 'primary': primary, 'secondary': secondary, 'fallback': None}).encode()
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(urllib.request.Request(url, method='PUT', data=body, headers=headers))
                    self.assertEqual(caught.exception.code, 400); discovery.assert_not_called()
            # a prepared pair whose agent settings differ: refused, and the answer names the fields so the pair can be repaired
            fast_doc = json.loads((self.root / 'api-fast.json').read_text())
            (self.root / 'api-fast.json').write_text(json.dumps(dict(fast_doc, tools=['bash'])))
            with patch('review_control.discovery', side_effect=prepared):
                body = json.dumps({'revision': 1, 'primary': deep, 'secondary': fast, 'fallback': None}).encode()
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(urllib.request.Request(url, method='PUT', data=body, headers=headers))
                self.assertEqual(caught.exception.code, 400)
                self.assertIn('differs in: tools', json.load(caught.exception)['error'])
            self.assertEqual(review_policy.read_settings()['revision'], 1)  # nothing saved
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_authenticated_http(self):
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = f'http://127.0.0.1:{server.server_port}/api/review-control/settings'
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught: urllib.request.urlopen(url)
            self.assertEqual(caught.exception.code, 401)
            request = urllib.request.Request(url, headers={'X-Session-API-Key': 'test-key'})
            with urllib.request.urlopen(request) as response: data = json.load(response)
            data['settings']['fallback'] = 'claude-opus'
            request = urllib.request.Request(url, method='PUT', data=json.dumps(data['settings']).encode(),
                headers={'X-Session-API-Key': 'test-key', 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response)['settings']['fallback'], 'claude-opus')
            with self.assertRaises(urllib.error.HTTPError) as caught: urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 409)
        finally: server.shutdown(); server.server_close(); thread.join()


class RunnerTests(unittest.TestCase):
    def execute(self, quota=True, selected='codex-astra', both_fail=False, changed=False, timeout=False, completed_first=False, error_events=None, settings_error=None, problems=None,
                finished=False, comment_after_calls=None, clock=None,
                primary='codex-astra', fallback='claude-opus', secondary=None, llm_refs=None, explicit=False,
                profile_info=None, prompt_text='model={model} label={label}', runs=None, resume=None, on_comments=None, usage_id=None,
                break_after_completion=None, fail=None, on_app=None, on_events=None, on_pulls=None, on_label=None):
        starts, labels, failures, calls = [], [], [], {'comments': 0}
        self.notes = notes = []
        def note(*args):
            if break_after_completion == 'note':
                raise OSError('forge down')
            notes.append(args)
        def api(path):
            if '/pulls/' in path:
                if on_pulls:
                    on_pulls()
                return {'state': 'open', 'head': {'sha': 'b'*40 if changed and starts else HEAD}}
            calls['comments'] += 1
            if on_comments:
                on_comments()
            if comment_after_calls is not None and calls['comments'] >= comment_after_calls:
                return [{'body': f'[hands-bot review] reviewed at head {HEAD}\n\nVERDICT\nREADY_FOR_HUMAN_MERGE'}]
            if len(starts) == 2 and not both_fail or completed_first:
                return [{'body': f'[hands-bot review] reviewed at head {HEAD}\n\nVERDICT\nREADY_FOR_HUMAN_MERGE'}]
            return []
        def app(path, data=None):
            if data:
                starts.append(data); return {'id': str(len(starts))}
            if 'events/search' in path:
                if on_events:
                    on_events()
                return error_events if error_events is not None else QUOTA if quota else {'items': []}
            if break_after_completion == 'read' and labels:  # the labels are swapped before the check reads the conversation
                raise OSError('canvas down')
            if on_app:
                on_app()
            info = {'execution_status': 'finished' if finished else 'error'}
            if usage_id:
                info['agent'] = {'llm': {'usage_id': usage_id}}
            return info  # usage_id None: a server without the field
        with tempfile.TemporaryDirectory() as temp:
            prompt = Path(temp)/'prompt'; prompt.write_text(prompt_text)
            self.logs = logs = []
            def set_label(*args):
                if on_label:
                    on_label()
                labels.append(args)
            runner = Runner(api, app, set_label, profile_info or (lambda p: (p,p)),
                            fail or (lambda *args: failures.append(args)), log=logs.append,
                            timeout=0 if timeout else 10, sleep=lambda _: None,
                            llm_ref=lambda name: (llm_refs or {}).get(name), problems=lambda s: dict(problems or {}), runs=runs,
                            note=note)
            with patch('review_runner.read_settings', **({'side_effect': settings_error} if settings_error else {'return_value': {'primary': primary, 'secondary': secondary, 'fallback': fallback}})), \
                 patch('review_runner.time.monotonic', side_effect=clock) if clock else patch('review_runner.time.monotonic', wraps=time.monotonic):
                label = 'review-this' if selected == primary and not explicit else 'review-this:' + selected
                if resume:
                    runner.resume(resume, str(prompt), '/tmp/reviews')
                else:
                    runner.run('owner/repo', 1, 'title', label, selected, str(prompt), '/tmp/reviews')
        return starts, labels, failures

    @staticmethod
    def raising_once(error):
        """A hook that raises ERROR the first time it is called, then does nothing."""
        raised = []
        def hook():
            if not raised:
                raised.append(1); raise error
        return hook

    @staticmethod
    def http_error(code):
        return urllib.error.HTTPError('https://forge.example/x', code, 'status', {}, io.BytesIO(b'{}'))

    def test_a_transient_read_during_the_watch_is_tried_again_at_the_next_poll(self):
        # one forge or Canvas blip while the conversation runs must not fail a review that then posts
        for name, error in {'timeout': TimeoutError('The read operation timed out'),
                            'connection refused': urllib.error.URLError(ConnectionRefusedError()),
                            'dropped connection': http.client.RemoteDisconnected('closed'),
                            'server error': self.http_error(502), 'rate limited': self.http_error(429)}.items():
            for where in ('forge comments', 'canvas conversation', 'canvas events'):
                with self.subTest(name=name, where=where):
                    hooks = {'forge comments': {'on_comments': self.raising_once(error), 'comment_after_calls': 2},
                             'canvas conversation': {'on_app': self.raising_once(error), 'comment_after_calls': 2},
                             'canvas events': {'on_events': self.raising_once(error), 'comment_after_calls': 2}}[where]
                    starts, labels, failures = self.execute(quota=False, **hooks)
                    self.assertEqual((len(starts), failures), (1, []), 'the review completes')
                    self.assertIn(('owner/repo', 1, 'hands-reviewed', True), labels)
                    self.assertIn(f'watch read failed, retrying at the next poll: owner/repo#1: {type(error).__name__}', self.logs)

    def test_a_failed_second_look_after_the_conversation_finished_is_taken_again(self):
        # the agent posts, then finishes; the fresh look at the comments fails once: look again at
        # the next poll instead of reporting "finished without posting"
        reads = []
        def second_read_times_out():
            reads.append(1)
            if len(reads) == 2:
                raise TimeoutError('The read operation timed out')
        starts, labels, failures = self.execute(quota=False, finished=True, on_comments=second_read_times_out,
                                                comment_after_calls=3)
        self.assertEqual((len(starts), failures), (1, []))
        self.assertIn(('owner/repo', 1, 'hands-reviewed', True), labels)

    @staticmethod
    def raising_at(n, error):
        """A hook that raises ERROR on its Nth call only."""
        calls = []
        def hook():
            calls.append(1)
            if len(calls) == n:
                raise error
        return hook

    def test_a_posted_review_is_labelled_done_after_a_brief_outage(self):
        # the review is on the PR: an outage while it is labelled done is tried again at the next
        # poll, never reported as a failure (#64)
        done = ('owner/repo', 1, 'hands-reviewed', True)
        cases = {
            'label swap': ({'on_label': self.raising_at(1, TimeoutError())}, 'label swap'),
            'label swap, second write': ({'on_label': self.raising_at(2, self.http_error(502))}, 'label swap'),
            'pull request read': ({'on_pulls': self.raising_at(3, urllib.error.URLError(ConnectionRefusedError()))}, 'watch read'),
            # the review shows up at the fresh look after "finished" (the second read), then the swap fails once
            'after the conversation finished': ({'finished': True, 'completed_first': False, 'comment_after_calls': 2,
                                                 'on_label': self.raising_at(1, TimeoutError())}, 'label swap'),
        }
        for name, (hooks, what) in cases.items():
            with self.subTest(name):
                starts, labels, failures = self.execute(quota=False, **{'completed_first': True, **hooks})
                self.assertEqual((len(starts), failures), (1, []), 'no failure is reported')
                self.assertEqual(labels[-1], done)
                self.assertTrue(any(line.startswith(f'{what} failed, retrying at the next poll: owner/repo#1:') for line in self.logs), self.logs)
                self.assertIn('review done: owner/repo#1 profile=codex-astra', self.logs)

    def test_a_posted_review_is_never_called_missing_at_the_deadline(self):
        # one watch iteration (clock: deadline from 0, one check below it, then past it); the label
        # swap fails in it, so the deadline tries once more
        def clock():
            return iter([0, 0] + [100] * 20).__next__
        def always_down():
            raise TimeoutError()
        starts, labels, failures = self.execute(quota=False, completed_first=True, clock=clock(), on_label=always_down)
        self.assertEqual(failures, [], 'not a failure: no "could not run", no advice to request another review')
        self.assertIn('review posted, not labelled done: owner/repo#1 the forge was unreachable until the deadline', self.logs)
        self.assertIn('posted review not marked: owner/repo#1: TimeoutError', self.logs)  # the forge is still down
        # the forge is back for the last word: hands-reviewing removed, a note (never a review) says what to do
        calls = []
        def down_for_the_watch():
            calls.append(1)
            if len(calls) <= 2:  # the swap in the one iteration and the last try at the deadline
                raise TimeoutError()
        starts, labels, failures = self.execute(quota=False, completed_first=True, clock=clock(), on_label=down_for_the_watch)
        self.assertEqual((failures, labels[-1]), ([], ('owner/repo', 1, 'hands-reviewing', False)))
        [(_, _, note)] = self.notes
        self.assertTrue(note.startswith('the review above is posted, but the forge could not be reached to label it done'), note)
        self.assertIn('add `hands-reviewed` by hand; no new review is needed', note)
        self.assertNotIn('re-add', note.lower())
        starts, labels, failures = self.execute(quota=False, completed_first=True, clock=clock(),
                                                on_label=self.raising_at(1, TimeoutError()))
        self.assertEqual((failures, labels[-1]), ([], ('owner/repo', 1, 'hands-reviewed', True)), 'the last try at the deadline completes it')
        # with nothing posted, the deadline still says "no review posted": test_timeout_does_not_retry

    def test_a_missed_last_look_is_taken_again_at_the_deadline(self):
        # one watch iteration whose comment read fails, then the deadline (#64 item 2)
        def clock():
            return iter([0, 0] + [100] * 20).__next__
        down = TimeoutError('The read operation timed out')
        # the review was posted during the outage: the look at the deadline finds it and completes
        starts, labels, failures = self.execute(quota=False, clock=clock(), on_comments=self.raising_at(1, down),
                                                comment_after_calls=2)
        self.assertEqual((failures, labels[-1]), ([], ('owner/repo', 1, 'hands-reviewed', True)))
        # nothing posted: the look at the deadline says so, as before
        starts, labels, failures = self.execute(quota=False, clock=clock(), on_comments=self.raising_at(1, down))
        self.assertTrue(failures[-1][-1].startswith('no review posted within'), failures)
        # the forge is still down at the deadline: unknown, not missing
        def always_down():
            raise down
        starts, labels, failures = self.execute(quota=False, clock=clock(), on_comments=always_down)
        [(_, _, reason)] = failures
        self.assertTrue(reason.startswith('the forge could not be reached at the 0-minute deadline to see whether the '
                                          'review was posted'), reason)
        self.assertIn('watch read failed, the deadline has passed: owner/repo#1: TimeoutError', self.logs)

    def test_the_deadline_always_takes_a_last_look(self):
        # the comments read succeeds, the conversation read then fails, and the review is posted
        # before the deadline: the last look finds it (#64 item 10)
        def clock():
            return iter([0, 0] + [100] * 20).__next__
        def canvas_down():
            raise TimeoutError()
        starts, labels, failures = self.execute(quota=False, clock=clock(), on_app=canvas_down, comment_after_calls=2)
        self.assertEqual((failures, labels[-1]), ([], ('owner/repo', 1, 'hands-reviewed', True)))
        starts, labels, failures = self.execute(quota=False, clock=clock(), on_app=canvas_down)
        self.assertTrue(failures[-1][-1].startswith('no review posted within'), failures)

    def test_a_resumed_run_past_its_deadline_looks_at_the_comments(self):
        # the receiver restarted after the deadline: the watch never runs, the deadline still looks once
        late = self.record(deadline=time.time() - 60)
        starts, labels, failures = self.execute(quota=False, completed_first=True, resume=late)
        self.assertEqual((starts, failures, labels[-1]), ([], [], ('owner/repo', 1, 'hands-reviewed', True)),
                         'the review posted while the receiver was down is labelled done')
        starts, labels, failures = self.execute(quota=False, resume=self.record(deadline=time.time() - 60))
        self.assertTrue(failures[-1][-1].startswith('no review posted within'), failures)

    def test_a_failure_is_reported_once(self):
        # the report's comment is posted, then something raises: no second "could not complete" (#64 item 6)
        failures = []
        def fail(*args):
            failures.append(args)
            raise ValueError('after the comment was posted')
        self.execute(quota=False, finished=True, fail=fail)  # "finished without posting", then the raise
        self.assertEqual([f[-1].split(' (')[0] for f in failures], ['the review conversation finished without posting a review'])
        self.assertIn('review failed: owner/repo#1: ValueError', self.logs)
        self.assertIn('failure report raised, may not be on the PR: owner/repo#1: ValueError', self.logs)

    def test_a_report_that_fails_before_the_pr_says_so_in_the_log(self):
        # the forge is still down at the deadline, and the report itself fails: one attempt, and
        # the log says the report may not be on the PR (so the operator looks)
        def clock():
            return iter([0, 0] + [100] * 20).__next__
        def down():
            raise TimeoutError()
        attempts = []
        def fail(*args):
            attempts.append(args); raise TimeoutError()
        self.execute(quota=False, clock=clock(), on_comments=down, fail=fail)
        self.assertEqual(len(attempts), 1, 'attempted once')
        self.assertEqual(self.logs[-2:], ['failure report raised, may not be on the PR: owner/repo#1: TimeoutError',
                                          'review failed: owner/repo#1: TimeoutError'])

    def test_the_last_swap_at_the_deadline_says_it_is_the_last(self):
        # no "retrying at the next poll" when there is no next poll (#64 item 8)
        def clock():
            return iter([0, 0] + [100] * 20).__next__
        def always_down():
            raise TimeoutError()
        self.execute(quota=False, completed_first=True, clock=clock(), on_label=always_down)
        swaps = [line for line in self.logs if line.startswith('label swap failed')]
        self.assertEqual(swaps, ['label swap failed, retrying at the next poll: owner/repo#1: TimeoutError',
                                 'label swap failed, the deadline has passed: owner/repo#1: TimeoutError'])

    def test_a_label_swap_refused_for_good_still_fails(self):
        starts, labels, failures = self.execute(quota=False, completed_first=True, on_label=self.raising_at(1, self.http_error(403)))
        self.assertEqual([f[-1] for f in failures], ['review service could not complete the request; inspect the service locally'])

    def test_a_read_that_is_an_answer_still_fails_the_review(self):
        for error in (self.http_error(404), self.http_error(401), ValueError('not json')):
            with self.subTest(error=type(error).__name__):
                starts, labels, failures = self.execute(quota=False, on_comments=self.raising_once(error), comment_after_calls=2)
                self.assertEqual([f[-1] for f in failures], ['review service could not complete the request; inspect the service locally'])
                self.assertNotIn(('owner/repo', 1, 'hands-reviewed', True), labels)

    def test_a_failure_report_that_fails_never_escapes_the_review_thread(self):
        def fail(*args):
            raise TimeoutError('The read operation timed out')  # the forge that failed is still down
        starts, labels, failures = self.execute(quota=False, on_comments=self.raising_once(self.http_error(404)),
                                                comment_after_calls=2, fail=fail)  # returns: nothing raised
        self.assertEqual(self.logs[-2:], ['review failed: owner/repo#1: HTTPError',
                                          'failure report raised, may not be on the PR: owner/repo#1: TimeoutError'])

    def test_only_reads_are_retried_and_only_after_transient_errors(self):
        from review_runner import retried, transient
        self.assertTrue(all(transient(e) for e in (TimeoutError(), ConnectionResetError(), urllib.error.URLError('x'),
                                                   http.client.IncompleteRead(b''), self.http_error(500), self.http_error(429))))
        self.assertFalse(any(transient(e) for e in (self.http_error(404), self.http_error(403), ValueError(), KeyError())))
        sleeps, calls = [], []
        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError()
            return 'answer'
        self.assertEqual(retried(flaky, sleep=sleeps.append), 'answer')
        self.assertEqual(sleeps, [1.0, 2.0])
        calls.clear(); sleeps.clear()
        def down():
            calls.append(1); raise TimeoutError()
        with self.assertRaises(TimeoutError):
            retried(down, sleep=sleeps.append)
        self.assertEqual((len(calls), sleeps), (3, [1.0, 2.0]), 'three attempts, then the error')
        calls.clear()
        def missing():
            calls.append(1); raise self.http_error(404)
        with self.assertRaises(urllib.error.HTTPError):
            retried(missing, sleep=sleeps.append)
        self.assertEqual(len(calls), 1, 'an answer is never asked again')

    @staticmethod
    def record(**overrides):
        """What a receiver finds on disk after a restart: a run whose conversation exists."""
        base = {'repo': 'owner/repo', 'num': 1, 'title': 'title', 'label': 'review-this', 'profile': 'codex-astra',
                'choices': ['codex-astra', 'claude-opus'], 'reading': None, 'head': HEAD, 'since': '2026-09-24T00:00:00Z',
                'attempt': 0, 'conversation': 'kept', 'started': time.time(), 'deadline': time.time() + 60}
        return dict(base, **overrides)

    def test_run_store_round_trip_and_never_raises(self):
        logs = []
        with tempfile.TemporaryDirectory() as temp:
            store = RunStore(Path(temp) / 'runs.json', log=logs.append)
            self.assertEqual(store.load(), [])
            store.save(self.record(started=2)); store.save(self.record(num=2, started=1))
            self.assertEqual([r['num'] for r in store.load()], [2, 1])  # oldest first
            store.clear('owner/repo', 1); store.clear('owner/repo', 1)  # a second clear is a no-op
            self.assertEqual([r['num'] for r in store.load()], [2])
            self.assertFalse(list(Path(temp).glob('*.tmp')))  # written through a temp file, replaced atomically
            (Path(temp) / 'runs.json').write_text('{not json')
            self.assertEqual(store.load(), []); self.assertIn('run state unreadable', logs[-1])
            missing = RunStore(Path(temp) / 'no-such-dir' / 'runs.json', log=logs.append)
            missing.save(self.record())  # the review goes on unrecorded
            self.assertIn('run state not saved', logs[-1]); self.assertEqual(missing.load(), [])
            (Path(temp) / 'runs.json').write_text(json.dumps({'a#1': {'repo': 'a', 'num': 1}, 'owner/repo#2': self.record(num=2)}))
            self.assertEqual([r['num'] for r in store.load()], [2])  # a record a receiver cannot act on is dropped, not fatal
            self.assertIn('1 unusable record', logs[-1]); self.assertNotIn('a#1', (Path(temp) / 'runs.json').read_text())
        self.assertFalse(logs[:-3])

    def test_run_is_recorded_once_the_conversation_exists_and_cleared_at_the_end(self):
        with tempfile.TemporaryDirectory() as temp:
            store = RunStore(Path(temp) / 'runs.json'); seen = []
            starts, labels, failures = self.execute(quota=False, completed_first=True, runs=store,
                                                    on_comments=lambda: seen.append(store.load()))
            self.assertEqual(len(starts), 1); self.assertTrue(labels); self.assertFalse(failures)
            record = seen[0][0]  # present while the conversation was being watched
            self.assertEqual((record['repo'], record['num'], record['conversation'], record['attempt'], record['head']),
                             ('owner/repo', 1, '1', 0, HEAD))
            self.assertEqual((record['choices'], record['label'], record['profile']), (['codex-astra', 'claude-opus'], 'review-this', 'codex-astra'))
            self.assertIn('since', record); self.assertLessEqual(record['started'], time.time())
            self.assertAlmostEqual(record['deadline'], record['started'] + 10, places=3)  # the fixture's timeout
            self.assertEqual(store.load(), [])  # gone once the run ended
            starts, labels, failures = self.execute(quota=False, runs=store)  # a failed run is cleared as well
            self.assertTrue(failures); self.assertEqual(store.load(), [])

    def test_previous_attempt_record_is_gone_before_the_fallback_exists(self):
        # a failed save of the fallback's record must leave nothing to resume: the primary's
        # record would replay its quota error after a restart and start a second fallback
        class Faulty(RunStore):
            writes = 0
            def _write(self, runs):
                self.writes += 1
                if self.writes == 3:  # 1 save primary, 2 clear it, 3 save fallback
                    self.log('run state not saved: injected'); return False
                return super()._write(runs)
        logs = []
        with tempfile.TemporaryDirectory() as temp:
            store = Faulty(Path(temp) / 'runs.json', log=logs.append); seen = []
            starts, labels, failures = self.execute(runs=store, on_comments=lambda: seen.append([r['conversation'] for r in store.load()]))
            self.assertEqual([s['agent_profile_id'] for s in starts], ['codex-astra', 'claude-opus'])
            self.assertTrue(labels); self.assertFalse(failures)
            self.assertEqual(seen, [['1'], []])  # watched with the primary's record, then with none at all
            self.assertEqual(store.writes, 3); self.assertIn('injected', logs[-1])
            starts, labels, failures = self.execute(resume=self.record(attempt=1, profile='claude-opus', conversation='2'), completed_first=True, quota=False)
            self.assertEqual(starts, []); self.assertTrue(labels)  # and a recorded fallback resumes as itself

    def test_no_fallback_when_the_previous_record_cannot_be_cleared(self):
        # writes 2 (clearing the primary's record) and 3 (the end-of-run clear) fail: the primary's
        # record stays on disk, so this run must not start the fallback; the restart that resumes
        # the record starts it once, and exactly one fallback conversation exists overall
        class Faulty(RunStore):
            writes = 0
            def _write(self, runs):
                self.writes += 1
                if self.writes in (2, 3):
                    self.log('run state not saved: injected'); return False
                return super()._write(runs)
        logs = []
        with tempfile.TemporaryDirectory() as temp:
            store = Faulty(Path(temp) / 'runs.json', log=logs.append)
            starts, labels, failures = self.execute(runs=store)
            self.assertEqual([s['agent_profile_id'] for s in starts], ['codex-astra'], 'no fallback conversation')
            self.assertIn('the fallback was not started because the run state could not be updated', failures[-1][-1])
            left = store.load()
            self.assertEqual([(r['attempt'], r['conversation']) for r in left], [(0, '1')])
            starts, labels, failures = self.execute(resume=left[0], comment_after_calls=2)
            self.assertEqual([s['agent_profile_id'] for s in starts], ['claude-opus'], 'the restart starts the one fallback')

    def test_no_fallback_when_the_run_state_cannot_be_read_before_it(self):
        # the run state cannot be read before the fallback (read 2: clearing the primary's record)
        # nor at the end of the run (read 3), while the file stays intact: that is no proof the
        # record is gone, so no fallback starts and the primary's record survives; after a restart
        # with readable storage, the resumed record starts the one fallback and the state is cleared
        real = Path.read_text
        reads = {'n': 0}
        def flaky(path, *args, **kwargs):
            if path.name == 'runs.json':
                reads['n'] += 1
                if reads['n'] in (2, 3):
                    raise OSError(5, 'Input/output error')
            return real(path, *args, **kwargs)
        logs = []
        with tempfile.TemporaryDirectory() as temp:
            store = RunStore(Path(temp) / 'runs.json', log=logs.append)
            with patch.object(Path, 'read_text', flaky):
                starts, labels, failures = self.execute(runs=store)
            self.assertEqual(reads['n'], 3, 'save, the clear before the fallback, the clear at the end')
            self.assertEqual([s['agent_profile_id'] for s in starts], ['codex-astra'], 'no fallback conversation')
            self.assertIn('the fallback was not started because the run state could not be updated', failures[-1][-1])
            self.assertEqual(logs.count('run state unreadable: runs.json: OSError'), 2)
            left = store.load()
            self.assertEqual([(r['attempt'], r['conversation'], r['profile']) for r in left], [(0, '1', 'codex-astra')],
                             'exactly the primary\'s record is left to resume')
            starts, labels, failures = self.execute(resume=left[0], comment_after_calls=2, runs=store)
            self.assertEqual([s['agent_profile_id'] for s in starts], ['claude-opus'], 'the restart starts the one fallback')
            self.assertFalse(failures)
            self.assertEqual(store.load(), [], 'and nothing is left to resume after it')

    def unchanged(self, comments, identities, head=HEAD, label='review-this', selected='codex-astra', later=None,
                  bot=None, reviewer='hands-bot', fail=None, on_label=None):
        """A request on `head` with these PR comments; `identities` maps a head (or its prefix)
        to its change's patch identity, or is an exception the identity lookup raises.
        `reviewer` is the login whose reviews the patch check trusts (`bot` the receiver's)."""
        starts, labels, failures, notes, logs, asked = [], [], [], [], [], []
        reads = []
        def api(path):
            if path.endswith('/pulls/1'):  # `later`: the PR as it reads from the third read on
                reads.append(path)
                if later and len(reads) > 2:
                    return later
                return {'state': 'open', 'head': {'sha': head}, 'base': {'ref': 'main'}}
            if path == '/repos/owner/repo/issues/1/comments':
                return comments
            return [{'body': f'[hands-bot review] reviewed at head {head}\n\nVERDICT\nREADY_FOR_HUMAN_MERGE'}]
        def app(path, data=None):
            if data:
                starts.append(data); return {'id': str(len(starts))}
            return {'execution_status': 'running'}
        def identity(repo, base, sha):
            asked.append((repo, base, sha))
            if isinstance(identities, Exception):
                raise identities
            return next(v for k, v in identities.items() if sha.startswith(k))
        with tempfile.TemporaryDirectory() as temp:
            prompt = Path(temp) / 'prompt'; prompt.write_text('model={model} label={label}')
            def set_label(*args):
                if on_label:
                    on_label()
                labels.append(args)
            runner = Runner(api, app, set_label, lambda p: (p, p), fail or (lambda *args: failures.append(args)),
                            log=logs.append, timeout=10, sleep=lambda _: None, llm_ref=lambda name: None,
                            problems=lambda s: {}, note=lambda *args: notes.append(args), change_identity=identity,
                            bot=bot, reviewer=reviewer)
            with patch('review_runner.read_settings', return_value={'primary': 'codex-astra', 'secondary': None, 'fallback': None}):
                runner.run('owner/repo', 1, 'title', label, selected, str(prompt), '/tmp/reviews')
        return starts, labels, failures, notes, logs, asked

    @staticmethod
    def review_comment(sha, verdict='READY_FOR_HUMAN_MERGE', login='hands-bot'):
        return {'user': {'login': login}, 'body': f'[hands-bot review] reviewed at head {sha}\n\nRISK\nLOW\n\nVERDICT\n{verdict}\n'}

    def test_a_request_on_an_unchanged_patch_keeps_the_previous_verdict(self):
        old = 'b' * 40
        comments = [{'body': 'discussion'}, self.review_comment(old, 'RETURN_TO_IMPLEMENTATION'), {'body': 'after it'}]
        starts, labels, failures, notes, logs, asked = self.unchanged(comments, {old: 'same', HEAD: 'same'})
        self.assertEqual(starts, [], 'no conversation is started')
        self.assertEqual(failures, [])
        self.assertEqual(asked, [('owner/repo', 'main', old), ('owner/repo', 'main', HEAD)])
        [(repo, num, text)] = notes
        self.assertTrue(text.startswith(f'patch unchanged since {old}; previous verdict stands (RETURN_TO_IMPLEMENTATION)'))
        for named in (HEAD, old, 'patch identity same', '(same)', 'review-this:<profile>'):
            self.assertIn(named, text)
        self.assertNotIn('[hands-bot review] reviewed at head', text, 'the note is never a review')
        self.assertEqual(labels[-2:], [('owner/repo', 1, 'hands-reviewing', False), ('owner/repo', 1, 'hands-reviewed', True)])
        self.assertIn(f'review not repeated: owner/repo#1 head={HEAD} patch unchanged since {old}', logs)

    def test_a_posted_verdict_note_always_ends_the_request(self):
        # the PR has been told no new review runs: a label swap that fails after the note never
        # starts one (#64 item 11)
        old = 'b' * 40
        comments, same = [self.review_comment(old)], {old: 'same', HEAD: 'same'}
        starts, labels, failures, notes, logs, _ = self.unchanged(comments, same, on_label=self.raising_once(TimeoutError()))
        self.assertEqual((starts, failures, len(notes)), ([], [], 1))
        self.assertEqual(labels[-2:], [('owner/repo', 1, 'hands-reviewing', False), ('owner/repo', 1, 'hands-reviewed', True)],
                         'a transient error is tried again')
        self.assertIn(f'review not repeated: owner/repo#1 head={HEAD} patch unchanged since {old}', logs)
        tries = []
        def down():
            tries.append(1); raise TimeoutError()
        refused = self.raising_once(self.http_error(403))
        def refused_once():
            tries.append(1); refused()
        for name, hook, error, attempts in (('the forge stays down', down, 'TimeoutError', 3),
                                            ('a write refused for good', refused_once, 'HTTPError', 1)):
            with self.subTest(name):
                tries.clear()
                starts, labels, failures, notes, logs, _ = self.unchanged(comments, same, on_label=hook)
                self.assertEqual((starts, failures, len(notes)), ([], [], 1), 'no review starts, no failure is reported')
                self.assertEqual(len(tries), attempts, 'a transient error is tried three times in all, a refusal once')
                self.assertIn(f'previous verdict stands, not labelled done: owner/repo#1: {error}', logs)
                self.assertFalse(any(line.startswith('review not repeated') for line in logs))

    def test_a_request_is_reviewed_afresh_unless_the_newest_verdict_covers_the_same_patch(self):
        old, older = 'b' * 40, 'c' * 40
        cases = {
            'patch changed': ([self.review_comment(old)], {old: 'one', HEAD: 'two'}),
            'same head asked again': ([self.review_comment(HEAD)], {HEAD: 'same'}),
            'no review yet': ([{'body': 'discussion'}], {HEAD: 'same'}),
            'only an older review matches': ([self.review_comment(older), self.review_comment(old)], {older: 'same', old: 'other', HEAD: 'same'}),
            'identity unavailable': ([self.review_comment(old)], ValueError('the change includes a binary file')),
            'a note is not a review': ([{'body': f'⚠️ [hands-bot review] note: patch unchanged since {old}'}], {old: 'same', HEAD: 'same'}),
            'no verdict line': ([{'body': f'[hands-bot review] reviewed at head {old}\n\nnothing else'}], {old: 'same', HEAD: 'same'}),
            'an abbreviated reviewed head': ([self.review_comment(old[:12])], {old[:12]: 'same', HEAD: 'same'}),  # #75
        }
        for name, (comments, identities) in cases.items():
            with self.subTest(name):
                starts, labels, failures, notes, logs, asked = self.unchanged(comments, identities)
                self.assertEqual([s['agent_profile_id'] for s in starts], ['codex-astra'], 'a full review runs')
                self.assertEqual((notes, failures), ([], []))
        starts, _, _, notes, logs, _ = self.unchanged([self.review_comment(old)], ValueError('secret detail'))
        self.assertIn('patch check skipped: owner/repo#1: ValueError', logs)
        self.assertFalse(any('secret detail' in line for line in logs))
        starts, _, _, notes, _, asked = self.unchanged([self.review_comment(old)], {old: 'same', HEAD: 'same'},
                                                       label='review-this:codex-astra')
        self.assertEqual((len(starts), notes, asked), (1, [], []), 'an explicit profile request always runs a review')

    def test_only_a_review_by_the_trusted_login_stands(self):
        old = 'b' * 40
        same = {old: 'same', HEAD: 'same'}
        stands = {
            'the reviewer login': ([self.review_comment(old)], {}),
            'in another case': ([self.review_comment(old, login='Hands-Bot')], {}),
            'from a callable': ([self.review_comment(old)], {'reviewer': lambda: 'hands-bot'}),
            "the receiver's bot when no reviewer is given": ([self.review_comment(old)], {'reviewer': None, 'bot': 'hands-bot'}),
            'a newer comment by another login is not a review': (
                [self.review_comment(old), self.review_comment(HEAD, login='stranger')], {}),
        }
        for name, (comments, kw) in stands.items():
            with self.subTest(name):
                starts, _, failures, notes, _, _ = self.unchanged(comments, same, **kw)
                self.assertEqual((starts, failures, len(notes)), ([], [], 1))
        reviewed = {
            'another login': ([self.review_comment(old, login='stranger')], {}),
            'no login reported': ([{k: v for k, v in self.review_comment(old).items() if k != 'user'}], {}),
            'no trusted login at all': ([self.review_comment(old)], {'reviewer': None}),
            'the trusted login cannot be read': ([self.review_comment(old)], {'reviewer': lambda: {}['login']}),
        }
        for name, (comments, kw) in reviewed.items():
            with self.subTest(name):
                starts, _, failures, notes, logs, asked = self.unchanged(comments, same, **kw)
                self.assertEqual([s['agent_profile_id'] for s in starts], ['codex-astra'], 'a full review runs')
                self.assertEqual((notes, failures, asked), ([], [], []))
        _, _, _, _, logs, _ = self.unchanged([self.review_comment(old)], same, reviewer=None)
        self.assertIn('patch check skipped: owner/repo#1: no trusted reviewer login', logs)

    def test_the_reviewer_login_is_read_only_when_a_review_exists(self):
        # a PR with no review-format comment never needs the login (on GitHub: no GET /user)
        old, calls = 'b' * 40, []
        def reviewer():
            calls.append(1); return 'hands-bot'
        for comments, expected in (([{'body': 'discussion'}], 0), ([self.review_comment(old)], 1)):
            with self.subTest(expected=expected):
                calls.clear()
                self.unchanged(comments, {old: 'same', HEAD: 'same'}, reviewer=reviewer)
                self.assertEqual(len(calls), expected)

    def test_a_pr_that_moves_or_closes_during_the_check_is_never_completed(self):
        # read 1 fixes the head (_start), read 2 gives the base, read 3 confirms the head after
        # the comparison: a PR that moved to another patch or closed fails as stale, unlabelled done
        old = 'b' * 40
        for name, later in [('moved', {'state': 'open', 'head': {'sha': 'd' * 40}, 'base': {'ref': 'main'}}),
                            ('closed', {'state': 'closed', 'head': {'sha': HEAD}, 'base': {'ref': 'main'}})]:
            with self.subTest(name):
                starts, labels, failures, notes, logs, asked = self.unchanged([self.review_comment(old)], {old: 'same', HEAD: 'same'}, later=later)
                self.assertEqual((starts, notes), ([], []), 'no note and no conversation for a superseded head')
                self.assertEqual([f[-1] for f in failures], ['the pull request changed or closed; request a fresh review'])
                self.assertNotIn(('owner/repo', 1, 'hands-reviewed', True), labels)

    def test_a_stale_report_in_the_patch_check_that_raises_ends_the_request(self):
        # the PR moves during the comparison and the stale report raises: the patch check's own catch
        # must not let the request run on into a second report or a conversation (#67's external review)
        old, attempts = 'b' * 40, []
        def fail(*args):
            attempts.append(args); raise TimeoutError()
        moved = {'state': 'open', 'head': {'sha': 'd' * 40}, 'base': {'ref': 'main'}}
        starts, labels, failures, notes, logs, asked = self.unchanged([self.review_comment(old)], {old: 'same', HEAD: 'same'},
                                                                      later=moved, fail=fail)
        self.assertEqual([a[-1] for a in attempts], ['the pull request changed or closed; request a fresh review'])
        self.assertEqual((starts, notes), ([], []))
        self.assertIn('failure report raised, may not be on the PR: owner/repo#1: TimeoutError', logs)
        self.assertNotIn('failure already reported, not again: owner/repo#1', logs, 'the request ended at the patch check')

    def test_a_second_failure_report_is_never_sent(self):
        from review_runner import Runner
        reports, logs = [], []
        runner = Runner(None, None, None, None, lambda *args: reports.append(args), log=logs.append)
        runner.fail('owner/repo', 1, 'first'); runner.fail('owner/repo', 1, 'second')
        self.assertEqual([r[-1] for r in reports], ['first'])
        self.assertIn('failure already reported, not again: owner/repo#1', logs)

    def test_patch_identity_is_verify_deliverys(self):
        import importlib.util
        from review_runner import patch_identity
        source = Path(__file__).resolve().parents[3] / 'skills' / 'verify-delivery' / 'verify_delivery.py'
        spec = importlib.util.spec_from_file_location('verify_delivery_for_drift', source)
        delivery = importlib.util.module_from_spec(spec); spec.loader.exec_module(delivery)
        diff = (b'diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n ctx\n-old\n+new\n'
                b'diff --git a/y b/y\nnew file mode 100644\n--- /dev/null\n+++ b/y\n@@ -0,0 +1 @@\n+\xff raw\n\\ No newline at end of file\n')
        moved = diff.replace(b'@@ -1,2 +1,2 @@', b'@@ -10,2 +10,2 @@').replace(b'index 1..2', b'index 3..4')
        for sample in (diff, diff.replace(b'\n', b'\r\n'), moved, diff.replace(b' ctx', b' other'), diff.decode('latin-1')):
            self.assertEqual(patch_identity(sample), delivery.patch_identity(sample))
        self.assertEqual(patch_identity(diff), patch_identity(moved), 'a base-only move keeps the identity')
        self.assertNotEqual(patch_identity(diff), patch_identity(diff.replace(b' ctx', b' other')), 'changed context does not')
        for bad in (b'', b'diff --git a/z b/z\nBinary files a/z and b/z differ\n'):
            for implementation in (patch_identity, delivery.patch_identity):
                with self.assertRaises(ValueError):
                    implementation(bad)

    def test_clear_and_save_report_whether_the_state_is_on_disk(self):
        with tempfile.TemporaryDirectory() as temp:
            store = RunStore(Path(temp) / 'runs.json', log=lambda _: None)
            self.assertTrue(store.clear('owner/repo', 1), 'nothing to clear')
            self.assertTrue(store.save(self.record()))
            self.assertTrue(store.clear('owner/repo', 1))
            missing = RunStore(Path(temp) / 'no-such-dir' / 'runs.json', log=lambda _: None)
            self.assertFalse(missing.save(self.record()))
            with patch.object(RunStore, '_read', return_value={'owner/repo#1': self.record()}):
                self.assertFalse(missing.clear('owner/repo', 1), 'a record that cannot be removed is reported')
            self.assertTrue(store.save(self.record()))
            with patch.object(Path, 'read_text', side_effect=OSError(5, 'Input/output error')):
                self.assertFalse(store.clear('owner/repo', 1), 'an unreadable file is not proof the record is gone')
            self.assertEqual(len(store.load()), 1, 'and the record is still there')
            (Path(temp) / 'runs.json').write_text('{not json')
            self.assertTrue(store.clear('owner/repo', 1), 'unparseable content holds no record that could resume')

    def test_resumed_run_watches_the_recorded_conversation(self):
        starts, labels, failures = self.execute(quota=False, completed_first=True, resume=self.record())
        self.assertEqual(starts, [])  # no new conversation
        self.assertEqual(labels[-1][-2:], ('hands-reviewed', True)); self.assertFalse(failures)

    def test_resumed_run_falls_back_once_after_a_quota_error(self):
        # the recorded conversation ends in a quota error after the restart: the single fallback still applies
        starts, labels, failures = self.execute(resume=self.record(), comment_after_calls=2)
        self.assertEqual([s['agent_profile_id'] for s in starts], ['claude-opus'])
        self.assertIn('single configured quota/rate-limit fallback: `codex-astra`', starts[0]['initial_message']['content'][0]['text'])
        self.assertTrue(labels); self.assertFalse(failures)

    def test_resumed_fallback_attempt_does_not_fall_back_again(self):
        starts, labels, failures = self.execute(resume=self.record(attempt=1, profile='claude-opus', conversation='second'))
        self.assertEqual(starts, []); self.assertFalse(labels)
        self.assertIn('the single fallback also failed', failures[-1][2])

    def test_resumed_run_keeps_its_head_and_deadline(self):
        starts, labels, failures = self.execute(quota=False, completed_first=True, resume=self.record(head='c' * 40))
        self.assertEqual(starts, []); self.assertFalse(labels)  # the PR moved during the restart
        self.assertIn('changed or closed', failures[-1][2])
        # the recorded deadline had passed: no watch, but the deadline still looks once (1.31.5), so the
        # review posted meanwhile is labelled done (test_a_resumed_run_past_its_deadline_looks_at_the_comments)
        starts, labels, failures = self.execute(quota=False, completed_first=True, resume=self.record(deadline=time.time() - 1))
        self.assertEqual((starts, failures, labels[-1]), ([], [], ('owner/repo', 1, 'hands-reviewed', True)))
        # the recorded deadline rules, not the restarted service's WATCH_MINUTES: a timeout of 0 here still watches
        starts, labels, failures = self.execute(quota=False, completed_first=True, timeout=True, resume=self.record(deadline=time.time() + 100))
        self.assertEqual(starts, []); self.assertTrue(labels); self.assertFalse(failures)

    def test_quota_fallback_completes_once(self):
        starts, labels, failures = self.execute()
        self.assertEqual([s['agent_profile_id'] for s in starts], ['codex-astra', 'claude-opus'])
        self.assertNotEqual(starts[0]['workspace'], starts[1]['workspace'])
        message = starts[1]['initial_message']['content'][0]['text']
        self.assertIn(HEAD, message); self.assertIn('single configured quota/rate-limit fallback', message)
        self.assertNotIn('Reasoning profiles', starts[0]['initial_message']['content'][0]['text'])  # no secondary: the old prompt
        self.assertNotIn('exhausted its usage allowance', message)
        self.assertEqual(labels[-1][-2:], ('hands-reviewed', True)); self.assertFalse(failures)

    REFS = {'api-deep': 'auto-review-deep', 'api-fast': 'auto-review-fast', 'api-alt': 'auto-review-alt'}

    def test_combined_mode_starts_on_reading_profile_and_switches_to_primary(self):
        starts, labels, failures = self.execute(selected='api-deep', primary='api-deep', secondary='api-fast', fallback='claude-opus', llm_refs=self.REFS)
        self.assertEqual([s['agent_profile_id'] for s in starts], ['api-fast', 'claude-opus'])
        first = starts[0]['initial_message']['content'][0]['text']
        self.assertIn('model=api-deep', first)  # the deep profile signs the review
        self.assertIn('starts on LLM profile `auto-review-fast`', first)
        self.assertIn('`switch_llm` tool with profile_name\n`auto-review-deep`', first)
        second = starts[1]['initial_message']['content'][0]['text']
        self.assertNotIn('switch_llm', second)  # an account-agent fallback cannot switch
        self.assertIn('you are `claude-opus`', second); self.assertFalse(failures)

    def test_combined_mode_completion_verifies_the_switch(self):
        # the conversation ended on the deep profile: nothing to say
        starts, labels, failures = self.execute(quota=False, completed_first=True, selected='api-deep', primary='api-deep', secondary='api-fast',
                                                fallback=None, llm_refs=self.REFS, usage_id='profile:auto-review-deep')
        self.assertTrue(labels); self.assertFalse(failures); self.assertEqual(self.notes, [])
        self.assertTrue(any('switch verified' in l and '`auto-review-deep`' in l for l in self.logs))
        self.assertTrue(any('start=api-fast agent-settings=api-fast' in l for l in self.logs))  # item 3: the ignored primary settings are visible
        # it never left the reading profile (usage_id `default`, as observed): the review is labelled done,
        # and the PR gets a note saying what wrote it
        starts, labels, failures = self.execute(quota=False, completed_first=True, selected='api-deep', primary='api-deep', secondary='api-fast',
                                                fallback=None, llm_refs=self.REFS, usage_id='default')
        self.assertEqual(labels[-1][-2:], ('hands-reviewed', True)); self.assertFalse(failures)
        self.assertEqual(len(self.notes), 1); note = self.notes[0][2]
        self.assertIn('written on the reading LLM profile `auto-review-fast`, not on `auto-review-deep`', note)
        self.assertTrue(any('review written off the primary' in l and 'reading LLM profile' in l for l in self.logs))
        # a third profile is named as itself
        self.execute(quota=False, completed_first=True, selected='api-deep', primary='api-deep', secondary='api-fast',
                     fallback=None, llm_refs=self.REFS, usage_id='profile:elsewhere')
        self.assertIn('written on LLM profile `profile:elsewhere`, not on `auto-review-deep`', self.notes[0][2])
        # a note that cannot be posted, or a Canvas read that fails, never changes the outcome
        for broken in ('note', 'read'):
            starts, labels, failures = self.execute(quota=False, completed_first=True, selected='api-deep', primary='api-deep', secondary='api-fast',
                                                    fallback=None, llm_refs=self.REFS, usage_id='default', break_after_completion=broken)
            self.assertEqual(labels[-1][-2:], ('hands-reviewed', True)); self.assertFalse(failures)
            self.assertTrue(any(l.startswith('switch check incomplete') for l in self.logs), self.logs)
        # single-profile reviews are never checked
        starts, labels, failures = self.execute(quota=False, completed_first=True, usage_id='profile:auto-review-fast')
        self.assertTrue(labels); self.assertEqual(self.notes, []); self.assertFalse(any('switch' in l for l in self.logs))

    def test_combined_mode_fallback_keeps_reading_profile_for_an_api_fallback(self):
        # the conversation had switched to the deep profile when the limit hit: the deep model failed
        for usage in ('profile:auto-review-deep', None):  # None: a server without the field keeps this behaviour
            starts, _, failures = self.execute(selected='api-deep', primary='api-deep', secondary='api-fast', fallback='api-alt', llm_refs=self.REFS, usage_id=usage)
            self.assertEqual([s['agent_profile_id'] for s in starts], ['api-fast', 'api-fast'])
            second = starts[1]['initial_message']['content'][0]['text']
            self.assertIn('`auto-review-alt`', second); self.assertIn('model=api-alt', second); self.assertFalse(failures)
            self.assertIn('fallback: `api-deep` ended with a confirmed quota', second)

    def test_combined_mode_limit_before_the_switch_falls_back_single_profile(self):
        # the limit hit the reading profile itself (usage_id still `default`, as observed before any switch):
        # the fallback must not start on it again, and the note names it
        for usage in ('default', 'profile:auto-review-fast'):
            starts, labels, failures = self.execute(selected='api-deep', primary='api-deep', secondary='api-fast', fallback='api-alt', llm_refs=self.REFS, usage_id=usage)
            self.assertEqual([s['agent_profile_id'] for s in starts], ['api-fast', 'api-alt'])
            second = starts[1]['initial_message']['content'][0]['text']
            self.assertNotIn('Reasoning profiles', second); self.assertIn('model=api-alt', second)
            self.assertIn('fallback: `api-fast` ended with a confirmed quota', second); self.assertTrue(labels); self.assertFalse(failures)
        self.assertTrue(switched({'agent': {'llm': {'usage_id': 'profile:x'}}}, 'x'))
        self.assertFalse(switched({'agent': {'llm': {'usage_id': 'default'}}}, 'x'))  # observed: no switch yet
        self.assertFalse(switched({'agent': {'llm': {'usage_id': 'profile:y'}}}, 'x'))
        self.assertTrue(switched({}, 'x'))  # a server without the field: the older assumption

    def test_explicit_label_naming_the_primary_stays_single_profile(self):
        starts, _, failures = self.execute(selected='api-deep', primary='api-deep', secondary='api-fast', fallback='claude-opus', llm_refs=self.REFS, explicit=True)
        self.assertEqual([s['agent_profile_id'] for s in starts], ['api-deep', 'claude-opus'])  # fallback still applies to the primary
        self.assertNotIn('switch_llm', starts[0]['initial_message']['content'][0]['text']); self.assertFalse(failures)

    def test_shared_llm_profile_stays_single_profile(self):
        refs = dict(self.REFS, **{'api-fast': 'auto-review-deep'})  # two agent profiles, one LLM profile
        starts, _, failures = self.execute(selected='api-deep', primary='api-deep', secondary='api-fast', fallback='claude-opus', llm_refs=refs)
        self.assertEqual(starts[0]['agent_profile_id'], 'api-deep'); self.assertNotIn('switch_llm', starts[0]['initial_message']['content'][0]['text']); self.assertFalse(failures)

    def test_unreadable_settings_fail_the_default_request_with_the_reason(self):
        starts, labels, failures = self.execute(settings_error=ValueError('Expecting value'))
        self.assertFalse(starts); self.assertIn('settings file cannot be read', failures[-1][2]); self.assertIn('Auto Reviews', failures[-1][2])

    def test_settings_problems_fail_or_degrade_the_default_request(self):
        starts, _, failures = self.execute(problems={'primary': "agent profile 'codex-astra' no longer exists"})
        self.assertFalse(starts); self.assertIn("agent profile 'codex-astra' no longer exists; select or clear the primary", failures[-1][2])
        starts, _, failures = self.execute(selected='api-deep', primary='api-deep', secondary='api-fast', fallback='claude-opus', llm_refs=self.REFS,
                                           problems={'secondary': "agent profile 'api-fast' no longer exists"})
        self.assertFalse(starts); self.assertIn('select or clear the secondary', failures[-1][2])
        starts, _, failures = self.execute(problems={'fallback': "agent profile 'claude-opus' no longer exists"})
        self.assertEqual(len(starts), 1); self.assertIn('quota', failures[-1][2]); self.assertNotIn('claude-opus', failures[-1][2])  # no fallback attempt on a missing fallback
        starts, _, _ = self.execute(selected='claude-fable', problems={'primary': 'ignored for explicit labels'})
        self.assertEqual(starts[0]['agent_profile_id'], 'claude-fable')

    def test_unavailable_selected_profile_fails_with_the_reason(self):
        starts, _, failures = self.execute(profile_info=lambda p: (_ for _ in ()).throw(KeyError('id')))
        self.assertFalse(starts); self.assertIn('reviewer profile `codex-astra` is not available', failures[-1][2])
        self.assertIn('Auto Reviews', failures[-1][2])

    def test_unavailable_fallback_profile_fails_after_quota_with_the_reason(self):
        starts, _, failures = self.execute(profile_info=lambda p: (_ for _ in ()).throw(KeyError('id')) if p == 'claude-opus' else (p, p))
        self.assertEqual(len(starts), 1); self.assertIn('quota or rate limit reached; the fallback profile `claude-opus` is not available', failures[-1][2])

    def test_unstartable_reading_profile_degrades_to_single_profile(self):
        starts, _, failures = self.execute(selected='api-deep', primary='api-deep', secondary='api-fast', fallback='claude-opus', llm_refs=self.REFS,
                                           profile_info=lambda p: (_ for _ in ()).throw(KeyError('id')) if p == 'api-fast' else (p, p))
        self.assertEqual(starts[0]['agent_profile_id'], 'api-deep'); self.assertNotIn('switch_llm', starts[0]['initial_message']['content'][0]['text']); self.assertFalse(failures)

    def test_static_site_block_is_not_doubled(self):
        starts, _, _ = self.execute(selected='api-deep', primary='api-deep', secondary='api-fast', fallback='claude-opus', llm_refs=self.REFS,
                                    prompt_text='Reasoning profiles: this conversation starts on LLM profile `x`\nmodel={model}')
        text = starts[0]['initial_message']['content'][0]['text']
        self.assertEqual(text.count('Reasoning profiles:'), 1); self.assertEqual(starts[0]['agent_profile_id'], 'api-deep')

    def test_explicit_label_and_missing_refs_skip_combined_mode(self):
        starts, _, _ = self.execute(selected='claude-fable', primary='api-deep', secondary='api-fast', fallback='claude-opus', llm_refs=self.REFS)
        self.assertEqual(starts[0]['agent_profile_id'], 'claude-fable'); self.assertNotIn('switch_llm', starts[0]['initial_message']['content'][0]['text'])
        starts, _, _ = self.execute(selected='api-deep', primary='api-deep', secondary='api-fast', fallback='claude-opus', llm_refs={})
        self.assertEqual(starts[0]['agent_profile_id'], 'api-deep'); self.assertNotIn('switch_llm', starts[0]['initial_message']['content'][0]['text'])

    def test_second_quota_stops(self):
        starts, labels, failures = self.execute(both_fail=True)
        self.assertEqual(len(starts), 2); self.assertFalse(labels); self.assertIn('fallback also failed', failures[0][-1])

    def test_generic_error_does_not_fallback(self):
        starts, labels, failures = self.execute(quota=False)
        self.assertEqual(len(starts), 1); self.assertTrue(failures); self.assertFalse(labels)

    def test_explicit_other_model_does_not_fallback(self):
        starts, _, failures = self.execute(selected='claude-fable')
        self.assertEqual(len(starts), 1); self.assertTrue(failures)

    def test_changed_head_prevents_fallback(self):
        starts, labels, failures = self.execute(changed=True)
        self.assertEqual(len(starts), 1); self.assertFalse(labels); self.assertTrue(failures)

    def test_finished_without_review_fails_at_once(self):
        starts, labels, failures = self.execute(finished=True)
        self.assertEqual(len(starts), 1); self.assertFalse(labels)
        self.assertIn('finished without posting a review (conversation 1)', failures[-1][2]); self.assertIn('re-add the label', failures[-1][2])

    def test_finished_with_the_comment_landing_late_completes(self):
        # the agent posts, then finishes: the comment shows up on the re-check after 'finished', never a false failure
        starts, labels, failures = self.execute(finished=True, comment_after_calls=2)
        self.assertEqual(len(starts), 1); self.assertTrue(labels); self.assertFalse(failures)

    def test_finished_with_the_comment_found_at_the_deadline_completes(self):
        # deadline = 0 + 10; the loop was entered at t=0, the poll crossed the deadline, the fresh check finds the comment:
        # completion must not depend on another loop iteration
        starts, labels, failures = self.execute(finished=True, comment_after_calls=2, clock=iter([0, 0, 100, 100, 100, 100, 100]).__next__)
        self.assertEqual(len(starts), 1); self.assertTrue(labels); self.assertFalse(failures)

    def test_completed_review_wins_over_error(self):
        starts, labels, failures = self.execute(completed_first=True)
        self.assertEqual(len(starts), 1); self.assertTrue(labels); self.assertFalse(failures)

    def test_timeout_does_not_retry(self):
        starts, labels, failures = self.execute(timeout=True)
        self.assertEqual(len(starts), 1); self.assertFalse(labels); self.assertTrue(failures)
        self.assertTrue(failures[-1][-1].startswith('no review posted within'), failures)

    def test_rejected_request_configuration_names_the_cause(self):
        bad = {'items': [{'kind': 'ConversationErrorEvent', 'code': 'LLMBadRequestError', 'detail': 'Upstream API error: 400',
                          'classification': {'kind': 'config', 'retryable': False, 'user_action': 'settings'}}]}
        self.assertEqual(config_error(bad), 'LLMBadRequestError'); self.assertIsNone(config_error(QUOTA))
        self.assertEqual(config_error({'items': [{'kind': 'ConversationErrorEvent', 'code': 'Other', 'classification': {'kind': 'config'}}]}), 'Other')
        starts, labels, failures = self.execute(quota=False, error_events=bad)
        self.assertEqual(len(starts), 1); self.assertFalse(labels)  # no quota fallback for a rejected configuration
        self.assertIn('rejected the request as configured (code LLMBadRequestError)', failures[-1][2]); self.assertIn('Auto Reviews', failures[-1][2])
        self.assertNotIn('Upstream', failures[-1][2])  # the endpoint's own text never reaches the PR
        stale = {'items': [{'kind': 'ConversationErrorEvent', 'code': 'OtherError'}] + bad['items']}  # newest first: an older config error does not count
        self.assertIsNone(config_error(stale))
        # timestamps rule over position: the server's newest-first order is not strict
        by_time = {'items': [dict(bad['items'][0], timestamp='2026-09-24T10:00:01'), {'kind': 'ConversationErrorEvent', 'code': 'OtherError', 'timestamp': '2026-09-24T10:00:02'},
                             {'kind': 'MessageEvent', 'timestamp': '2026-09-24T10:00:03'}]}
        self.assertIsNone(config_error(by_time)); self.assertFalse(limit_error(by_time))
        by_time['items'][0]['timestamp'] = '2026-09-24T10:00:09'
        self.assertEqual(config_error(by_time), 'LLMBadRequestError')
        starts, labels, failures = self.execute(quota=False, error_events=stale)
        self.assertEqual(len(starts), 1); self.assertIn('entered error state', failures[-1][2]); self.assertNotIn('rejected', failures[-1][2])

    def test_next_page_reads_the_link_header(self):
        github = ('<https://api.github.com/repositories/1/issues/2/comments?since=2015-01-01T00%3A00%3A00Z&page=2>; rel="next", '
                  '<https://api.github.com/repositories/1/issues/2/comments?since=2015-01-01T00%3A00%3A00Z&page=6>; rel="last"')
        self.assertEqual(next_page(github), 'https://api.github.com/repositories/1/issues/2/comments?since=2015-01-01T00%3A00%3A00Z&page=2')
        self.assertIsNone(next_page('<https://example.test/x?page=1>; rel="prev", <https://example.test/x?page=6>; rel="last"'))
        self.assertIsNone(next_page(None)); self.assertIsNone(next_page(''))
        self.assertEqual(next_page('<https://g.test/a?page=2>;rel="next"'), 'https://g.test/a?page=2')  # no space after the semicolon
        self.assertEqual(next_page(github, 'https://api.github.com'), 'https://api.github.com/repositories/1/issues/2/comments?since=2015-01-01T00%3A00%3A00Z&page=2')
        self.assertIsNone(next_page(github, 'https://forge.example/api/v1'))  # another host never receives the token

    def test_recovered_head_after_restart(self):
        review = {'user': {'login': 'hands-bot'}, 'body': f'[hands-bot review] reviewed at head {HEAD}\n\nVERDICT\nREADY_FOR_HUMAN_MERGE'}
        seen = []
        def api_for(state='open', comments=(review,), head=HEAD):
            def api(path):
                seen.append(path)
                if '/pulls/' in path: return {'state': state, 'head': {'sha': head}}
                return list(comments)
            return api
        self.assertEqual(recovered_head(api_for(), 'o/r', 1, '[hands-bot review]', 'hands-bot'), HEAD)
        self.assertIn('since=', [p for p in seen if '/comments' in p][-1])  # only the watch window is read
        self.assertIn('/pulls/', seen[-1])  # and the head is read again after the comments
        self.assertIsNone(recovered_head(api_for(comments=()), 'o/r', 1, '[hands-bot review]', 'hands-bot'))  # nothing posted: the run is lost
        self.assertIsNone(recovered_head(api_for(head='c' * 40), 'o/r', 1, '[hands-bot review]', 'hands-bot'))  # a review for an older head does not count
        self.assertIsNone(recovered_head(api_for(state='closed'), 'o/r', 1, '[hands-bot review]', 'hands-bot'))
        self.assertIsNone(recovered_head(api_for(), 'o/r', 1, '[hands-bot review]', 'someone-else'))  # a comment by another user does not count
        self.assertEqual(recovered_head(api_for(), 'o/r', 1, '[hands-bot review]', None), HEAD)  # forges where the poster is the PAT owner

    def test_recovered_head_rechecks_the_head_after_reading_the_comments(self):
        review = {'user': {'login': 'hands-bot'}, 'body': f'[hands-bot review] reviewed at head {HEAD}\n\nVERDICT\nREADY_FOR_HUMAN_MERGE'}
        for later in ({'state': 'open', 'head': {'sha': 'c' * 40}}, {'state': 'closed', 'head': {'sha': HEAD}}):
            with self.subTest(later=later):
                prs = iter([{'state': 'open', 'head': {'sha': HEAD}}, later])
                api = lambda path: next(prs) if '/pulls/' in path else [review]
                self.assertIsNone(recovered_head(api, 'o/r', 1, '[hands-bot review]', 'hands-bot'),
                                  'the PR moved while the comments were read: the old head\'s review does not complete the run')

    def test_only_structured_server_error_triggers_fallback(self):
        self.assertTrue(limit_error(QUOTA))
        for kind in ['MessageEvent', 'ACPToolCallEvent']:
            self.assertFalse(limit_error({'items': [dict(QUOTA['items'][0], kind=kind)]}))
        self.assertFalse(limit_error({'items': [{'kind': 'ConversationErrorEvent', 'code': 'OtherError'}] + QUOTA['items']}))

    def test_api_rate_limit_and_quota_each_trigger_one_fallback(self):
        for code in ['RateLimitError', 'LLMRateLimitError', 'QuotaExceededError']:
            with self.subTest(code=code):
                starts, labels, failures = self.execute(error_events={'items': [
                    {'kind': 'ConversationErrorEvent', 'code': code, 'detail': 'provider rejected request'}]})
                self.assertEqual(len(starts), 2)
                self.assertTrue(labels)
                self.assertFalse(failures)
                self.assertNotIn('exhausted its usage allowance', starts[1]['initial_message']['content'][0]['text'])

    def test_generic_error_prose_does_not_confirm_a_limit(self):
        starts, labels, failures = self.execute(error_events={'items': [
            {'kind': 'ConversationErrorEvent', 'code': 'LLMError', 'detail': 'rate limit or quota may be exhausted'}]})
        self.assertEqual(len(starts), 1)
        self.assertFalse(labels)
        self.assertTrue(failures)

    def test_completion_requires_head_and_verdict(self):
        self.assertFalse(valid_review({'body': '[hands-bot review] failed'}, '[hands-bot review]', HEAD))
        self.assertFalse(valid_review({'body': '[hands-bot review] reviewed at head bbbbbbb\nVERDICT\nREADY_FOR_HUMAN_MERGE'}, '[hands-bot review]', HEAD))
        # only the full head counts: an abbreviation could name a commit made to share it (#75)
        self.assertFalse(valid_review({'body': f'[hands-bot review] reviewed at head {HEAD[:12]}\nVERDICT\nREADY_FOR_HUMAN_MERGE'},
                                      '[hands-bot review]', HEAD))
        self.assertTrue(valid_review({'body': f'[hands-bot review] reviewed at head {HEAD}\nVERDICT\nREADY_FOR_HUMAN_MERGE'},
                                     '[hands-bot review]', HEAD))
        self.assertFalse(valid_review({'body': f'[hands-bot review] reviewed at head {HEAD}\nVERDICT\nREVIEW_COULD_NOT_RUN'}, '[hands-bot review]', HEAD))


if __name__ == '__main__': unittest.main()
