import json
import os
import tempfile
from pathlib import Path
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from canvas_discovery import api_models, acp_models, session_models, probe, server_environment, split_codex_model, effort_options, normalize_effort, profile_name, _stem, bare_model, model_string, effort_body
from reasoning_profiles import block


class DiscoveryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'server_environment resolves container (POSIX) paths')
    def test_server_environment_uses_running_cipher_and_rejects_ambiguity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process = root / '11'; process.mkdir(); (process / 'cwd').mkdir()
            (process / 'cmdline').write_bytes(b'openhands-agent-server\0--port\018000')
            (process / 'environ').write_bytes(b'OH_SECRET_KEY=test-only-secret\0OH_PERSISTENCE_DIR=/tmp/test-store\0UNRELATED_SECRET=do-not-copy')
            config = server_environment(root)
            self.assertEqual(config['OH_SECRET_KEY'], 'test-only-secret')
            self.assertEqual(config['OH_PERSISTENCE_DIR'], '/tmp/test-store')
            self.assertNotIn('UNRELATED_SECRET', config)
            (process / 'environ').write_bytes(b'OH_PERSISTENCE_DIR=/tmp/test-store')
            with self.assertRaises(ValueError): server_environment(root)
            (process / 'environ').write_bytes(b'OH_SECRET_KEY=one')
            second = root / '23'; second.mkdir(); (second / 'cwd').mkdir()
            (second / 'cmdline').write_bytes(b'openhands-agent-server\0')
            (second / 'environ').write_bytes(b'OH_SECRET_KEY=two')
            with self.assertRaises(ValueError): server_environment(root)

    def test_acp_config_and_legacy_model_lists(self):
        self.assertEqual(session_models({'configOptions': [{'id': 'model', 'options': [
            {'value': 'one', 'name': 'One'}, {'name': 'Group', 'options': [{'value': 'two', 'name': 'Two'}]}]}]}),
            [{'id': 'one', 'label': 'One'}, {'id': 'two', 'label': 'Two'}])
        self.assertEqual(session_models({'models': {'availableModels': [{'modelId': 'three', 'name': 'Three'}]}}),
            [{'id': 'three', 'label': 'Three'}])
        with self.assertRaises(ValueError): session_models({})

    def test_provider_discovery_and_redirect_does_not_forward_key(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                received.append((self.path, self.headers.get('Authorization')))
                if self.path == '/redirect/models':
                    self.send_response(302)
                    self.send_header('Location', '/stolen')
                    self.end_headers()
                else:
                    self.send_response(200); self.end_headers()
                    self.wfile.write(json.dumps({'data': [{'id': 'b'}, {'id': 'a'}, {'id': 'b'}]}).encode())
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            self.assertEqual([m['id'] for m in api_models(url, 'test-secret')], ['a', 'b'])
            with self.assertRaises(urllib.error.HTTPError): api_models(url + '/redirect', 'test-secret')
            self.assertEqual(received, [('/models', 'Bearer test-secret'), ('/redirect/models', 'Bearer test-secret')])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_probe_reports_safe_errors_without_provider_body(self):
        import io
        for code, text in [(401, 'Authentication rejected'), (403, 'Authentication rejected'), (429, 'rate-limiting'), (302, 'redirects'), (404, 'not found')]:
            error = urllib.error.HTTPError('https://example.com', code, 'secret body', {}, io.BytesIO(b'private-token'))
            with patch('canvas_discovery.api_models', side_effect=error):
                result = probe({'base_url': 'https://example.com', 'api_key': 'private-token'})
            self.assertFalse(result['ok'])
            self.assertIn(text, result['message'])
            self.assertNotIn('private-token', json.dumps(result))
            self.assertNotIn('secret body', json.dumps(result))
        with patch('canvas_discovery.api_models', return_value=[]):
            self.assertFalse(probe({'base_url': 'https://example.com', 'api_key': 'key'})['ok'])

    def test_probe_existing_and_unsaved_connections_are_read_only(self):
        from types import SimpleNamespace
        models = [{'id': 'one', 'label': 'One'}]
        with patch('canvas_discovery.discover', return_value={'models': models}):
            self.assertEqual(probe({'provider': 'connection:one'}), {'ok': True, 'models': models})
        with patch('canvas_discovery.api_models', return_value=models) as query:
            self.assertTrue(probe({'base_url': 'https://example.com', 'api_key': 'new-key'})['ok'])
            query.assert_called_with('https://example.com', 'new-key')
            connection = SimpleNamespace(api_key_value=lambda: 'saved-key', base_url='https://provider.example/v1',
                                         provider='custom')
            cipher, opened = object(), []
            def get(name, cipher=None):
                opened.append((name, cipher))
                return connection
            with patch('canvas_discovery.stores') as stores:
                stores.return_value = (cipher, SimpleNamespace(get=get), None)
                # The same origin, another path: the saved key may be reused.
                self.assertTrue(probe({'provider': 'connection:one', 'base_url': 'https://Provider.example:443/v2'})['ok'])
                query.assert_called_with('https://Provider.example:443/v2', 'saved-key')
                self.assertTrue(probe({'provider': 'connection:one', 'base_url': 'https://other.example', 'api_key': 'replacement'})['ok'])
                query.assert_called_with('https://other.example', 'replacement')
            self.assertEqual(opened, [('one', cipher)] * 2, 'the connection is opened by name with the server cipher')

    def test_probe_refuses_other_shapes_and_reports_timeouts_and_failures_safely(self):
        import asyncio
        with patch('canvas_discovery.api_models') as query, patch('canvas_discovery.discover') as discover:
            for request in [None, [], {}, {'provider': ''}, {'provider': 7}, {'base_url': 'https://example.com'},
                            {'base_url': 'https://example.com', 'api_key': ''}, {'api_key': 'key'},
                            {'provider': 'connection:one', 'api_key': 'key'},
                            {'base_url': 'https://example.com', 'api_key': 'key', 'extra': 'x'}]:
                with self.subTest(request=request):
                    self.assertEqual(probe(request), {'ok': False, 'message': 'Could not discover models. Check the endpoint, token, or account login.'})
            query.assert_not_called(); discover.assert_not_called()
        for error in [TimeoutError(), asyncio.TimeoutError()]:
            with self.subTest(error=type(error)), patch('canvas_discovery.api_models', side_effect=error):
                self.assertEqual(probe({'base_url': 'https://example.com', 'api_key': 'key'}),
                                 {'ok': False, 'message': 'Connection timed out. Try again or check the endpoint.'})
        with patch('canvas_discovery.api_models', side_effect=RuntimeError('private-token in a provider body')):
            result = probe({'base_url': 'https://example.com', 'api_key': 'private-token'})
        self.assertEqual(result, {'ok': False, 'message': 'Could not discover models. Check the endpoint, token, or account login.'})

    def test_probe_never_sends_a_saved_key_to_another_origin(self):
        from types import SimpleNamespace
        cases = {'custom': ('https://provider.example/v1', ['https://other.example/v1', 'http://provider.example/v1',
                                                           'https://provider.example:8443/v1', 'https://provider.example.evil/v1',
                                                           'https://sub.provider.example/v1']),
                 'openai': (None, ['https://api.openai.com.evil/v1', 'http://api.openai.com/v1'])}
        for kind, (stored, elsewhere) in cases.items():
            connection = SimpleNamespace(api_key_value=lambda: 'saved-key', base_url=stored, provider=kind)
            with patch('canvas_discovery.stores') as stores, patch('canvas_discovery.api_models', return_value=[]) as query:
                stores.return_value = (None, SimpleNamespace(get=lambda *a, **kw: connection), None)
                for url in elsewhere:
                    with self.subTest(kind=kind, url=url):
                        result = probe({'provider': 'connection:one', 'base_url': url})
                        self.assertFalse(result['ok'])
                        self.assertIn('Enter the API token', result['message'])
                        self.assertNotIn('saved-key', json.dumps(result))
                query.assert_not_called()
        # A connection without a stored endpoint never lends its token, not even to an invalid URL.
        connection = SimpleNamespace(api_key_value=lambda: 'saved-key', base_url=None, provider='custom')
        with patch('canvas_discovery.stores') as stores, patch('canvas_discovery.api_models') as query:
            stores.return_value = (None, SimpleNamespace(get=lambda *a, **kw: connection), None)
            self.assertIn('Enter the API token', probe({'provider': 'connection:one', 'base_url': 'not a url'})['message'])
            query.assert_not_called()
        # OpenAI's default endpoint is the stored one when a connection has no URL.
        connection = SimpleNamespace(api_key_value=lambda: 'saved-key', base_url=None, provider='openai')
        with patch('canvas_discovery.stores') as stores, patch('canvas_discovery.api_models', return_value=[{'id': 'm', 'label': 'm'}]) as query:
            stores.return_value = (None, SimpleNamespace(get=lambda *a, **kw: connection), None)
            self.assertTrue(probe({'provider': 'connection:one', 'base_url': 'https://api.openai.com/v1'})['ok'])
            query.assert_called_with('https://api.openai.com/v1', 'saved-key')

    @unittest.skipIf(os.name == 'nt', 'the stand-in ACP binary is a POSIX shell script')
    def test_acp_discovery_child_never_receives_the_cipher_key(self):
        import asyncio
        with tempfile.TemporaryDirectory() as temp:
            out, binary = Path(temp) / 'seen', Path(temp) / 'fake-acp'
            binary.write_text('#!/bin/sh\n'
                              'if [ -n "${OH_SECRET_KEY+x}" ]; then echo inherited; else echo absent; fi > "$FAKE_ACP_OUT"\n'
                              'echo "$KEEP_ME" >> "$FAKE_ACP_OUT"\n'
                              'read -r request\n')  # take the initialize request before exiting: otherwise the
            # write can meet an already closed pipe and fail with ConnectionResetError instead
            binary.chmod(0o755)
            with patch.dict(os.environ, {'OH_SECRET_KEY': 'test-only-value', 'FAKE_ACP_OUT': str(out), 'KEEP_ME': 'kept'}):
                with self.assertRaises(ValueError):  # the stand-in answers nothing, so discovery fails
                    asyncio.run(asyncio.wait_for(acp_models(str(binary)), 10))
                self.assertEqual(os.environ['OH_SECRET_KEY'], 'test-only-value', 'only the child loses the key')
            self.assertEqual(out.read_text().split(), ['absent', 'kept'], 'the key is withheld, the rest is passed on')

    @unittest.skipIf(os.name == 'nt', 'the fake /proc uses a symlinked cwd')
    def test_main_puts_the_server_key_in_place_before_dispatch_and_fails_closed(self):
        from canvas_discovery import main
        def fake_proc(root, entries):
            for pid, (executable, environ) in entries.items():
                entry = Path(root) / str(pid); entry.mkdir()
                (entry / 'cmdline').write_bytes(executable + b'\0--port\x008000\0')
                (entry / 'environ').write_bytes(b'\0'.join(environ) + b'\0')
                (entry / 'cwd').symlink_to(root)
        seen = []
        with tempfile.TemporaryDirectory() as temp:
            proc = Path(temp) / 'proc'; proc.mkdir()
            fake_proc(proc, {
                101: (b'/usr/bin/openhands-agent-server', [b'OH_SECRET_KEY=server-key', b'PATH=/usr/bin']),
                202: (b'/usr/bin/python3', [b'OH_SECRET_KEY=decoy-key']),  # not the agent server: ignored
            })
            with patch.dict(os.environ, {}, clear=False), \
                 patch('canvas_discovery.inventory', side_effect=lambda: seen.append(os.environ.get('OH_SECRET_KEY')) or {'ok': 1}):
                os.environ.pop('OH_SECRET_KEY', None)
                self.assertEqual(main({'action': 'inventory'}, proc_root=str(proc)), {'ok': 1})
            self.assertEqual(seen, ['server-key'], 'the server\'s key is in place when the action runs')
            empty = Path(temp) / 'empty'; empty.mkdir()
            with patch.dict(os.environ, {}, clear=False), patch('canvas_discovery.inventory') as action:
                os.environ.pop('OH_SECRET_KEY', None)
                with self.assertRaisesRegex(ValueError, 'unique Canvas server'):
                    main({'action': 'inventory'}, proc_root=str(empty))
                action.assert_not_called()
                self.assertNotIn('OH_SECRET_KEY', os.environ, 'nothing is put in place')
            with patch.dict(os.environ, {}, clear=False):
                with self.assertRaisesRegex(ValueError, 'Unknown action'):
                    main({'action': 'unknown'}, proc_root=str(proc))

    def test_effort_helpers(self):
        self.assertEqual(split_codex_model('gpt-6-astra/xhigh'), ('gpt-6-astra', 'xhigh'))
        self.assertEqual(split_codex_model('gpt-6-astra'), ('gpt-6-astra', None))
        self.assertEqual(split_codex_model('vendor/model'), ('vendor/model', None))  # unknown suffix is part of the id
        self.assertEqual(effort_options('acp:codex'), ['low', 'medium', 'high', 'xhigh'])
        self.assertEqual(effort_options('acp:claude-code'), [])
        self.assertIn('max', effort_options('connection:x'))
        self.assertEqual(normalize_effort('connection:x', None), 'high')
        self.assertEqual(normalize_effort('acp:codex', None), None)
        self.assertEqual(normalize_effort('acp:codex', 'xhigh'), 'xhigh')
        for provider, effort in [('acp:codex', 'ultra'), ('acp:claude-code', 'high'), ('connection:x', 'turbo'), ('connection:x', 5)]:
            with self.subTest(provider=provider, effort=effort), self.assertRaises(ValueError): normalize_effort(provider, effort)
        text = block('fast', 'deep')
        self.assertIn('starts on LLM profile `fast`', text); self.assertIn('profile_name\n`deep`', text)
        for reading, deep in [('same', 'same'), ('', 'deep'), ('fast', None)]:
            with self.assertRaises(ValueError): block(reading, deep)
        # Drift guard: the site that renders this contract statically must carry the same prose.
        site = json.loads((Path(__file__).resolve().parent.parent / 'sites' / 'site-b-gitea.json').read_text(encoding='utf-8'))
        self.assertEqual(block('astra-high', 'astra').strip(), site['REASONING_PROFILES'].strip())

    def test_model_strings_use_the_proxy_prefix_for_custom_endpoints(self):
        self.assertEqual(model_string('gpt-6-astra', proxied=True), 'litellm_proxy/gpt-6-astra')
        self.assertEqual(model_string('gpt-6-astra', proxied=False), 'openai/gpt-6-astra')
        for saved in ('litellm_proxy/gpt-6-astra', 'openai/gpt-6-astra', 'gpt-6-astra'):
            self.assertEqual(bare_model(saved), 'gpt-6-astra')
        self.assertEqual(bare_model('vendor/model'), 'vendor/model')  # unknown prefixes are part of the id
        self.assertEqual(effort_body('high', proxied=True), {'reasoning_effort': 'high'})
        self.assertEqual((effort_body('high', proxied=False), effort_body(None, proxied=True)), ({}, {}))

    def test_profile_names_are_readable_unique_and_legal(self):
        self.assertEqual(profile_name('gpt-5-6-sol', 'max', False, set()), 'review-gpt-5-6-sol-max')
        self.assertEqual(profile_name('gpt-5-6-sol', 'low', True, set()), 'review-gpt-5-6-sol-low-reading')
        self.assertEqual(profile_name('opus[1m]', None, False, set()), 'review-opus-1m')
        self.assertEqual([_stem(e) for e in ('CC-gpt-5.6-sol', 'CC-gpt-5.6-sol.json', 'a.json.json')], ['CC-gpt-5.6-sol', 'CC-gpt-5.6-sol', 'a.json'])
        self.assertEqual(profile_name('gpt-6-astra', 'xhigh', False, {'review-gpt-6-astra-xhigh', 'review-gpt-6-astra-xhigh-2'}), 'review-gpt-6-astra-xhigh-3')
        legal = __import__('re').compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
        for model in ('a' * 80, '../../etc', '[[]]', 'GPT-4.1 mini', 'x/y'):
            with self.subTest(model=model):
                name = profile_name(model, 'xhigh', True, {f'review-{model}-xhigh-reading'})
                self.assertTrue(legal.fullmatch(name), name); self.assertLessEqual(len(name), 64)

    def test_invalid_provider_urls(self):
        for url in ['file:///etc/passwd', 'https://user:pass@example.com', 'https://example.com?key=x']:
            with self.assertRaises(ValueError): api_models(url, 'test-secret')

    def test_control_passes_only_metadata_and_suppresses_subprocess_errors(self):
        import review_control
        with patch('review_control.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"models": []}'
            self.assertEqual(review_control.discovery('models', provider='acp:codex'), {'models': []})
            self.assertEqual(json.loads(run.call_args.kwargs['input']), {'action': 'models', 'provider': 'acp:codex'})
            run.return_value.returncode = 1
            run.return_value.stdout = 'secret data'
            with self.assertRaisesRegex(OSError, '^Canvas discovery failed$'):
                review_control.discovery('inventory')


if __name__ == '__main__': unittest.main()
