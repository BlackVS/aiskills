"""Run where the SDK is installed (in Canvas, or a virtual environment with the image's SDK versions)
with OH_PERSISTENCE_DIR pointing to a fresh temporary directory."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from canvas_discovery import prepare, inventory, stores, effort_options


@unittest.skipUnless(os.environ.get('OH_PERSISTENCE_DIR', '').startswith('/tmp/auto-reviews-test-'), 'requires isolated Canvas SDK storage')
class CanvasIntegrationTests(unittest.TestCase):
    def test_connection_model_creates_reusable_secret_free_agent(self):
        from canvas_discovery import agent_store as get_agent_profile_store
        get_provider_connections_store = lambda: stores()[1]; get_llm_profile_store = lambda: stores()[2]
        try:
            from openhands.agent_server.persistence import ProviderConnection
        except ImportError:  # SDK >= 1.49
            from openhands.sdk.llm.provider_connection_store import ProviderConnection
        from pydantic import SecretStr
        root = Path(os.environ['OH_PERSISTENCE_DIR']).resolve()
        self.assertTrue(str(root).startswith('/tmp/auto-reviews-test-'))
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                if self.headers.get('Authorization') != 'Bearer test-key':
                    self.send_response(401); self.end_headers(); return
                self.send_response(200); self.end_headers()
                self.wfile.write(b'{"data":[{"id":"test-model"}]}')
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            cipher, connections, _ = stores()
            before = len(get_agent_profile_store().list())  # the other case may have run first in this store
            self.assertIsNotNone(cipher, 'Set an isolated test encryption key')
            connections.create(ProviderConnection(id='test-connection', display_name='Test',
                provider='custom', api_key=SecretStr('test-key'),
                base_url=f'http://127.0.0.1:{server.server_port}', created_at=1, updated_at=1), cipher=cipher)
            selected = {'provider': 'connection:test-connection', 'model': 'test-model'}
            first = prepare(selected)
            self.assertEqual(prepare(selected), first)
            self.assertEqual(first, {'profile': 'review-test-model-high'})  # readable name: review-<model>-<effort>
            profile = get_agent_profile_store().load(first['profile'])
            llm = get_llm_profile_store().load(profile.llm_profile_ref, cipher=None, resolve_provider=False)
            self.assertEqual(llm.provider_connection_id, 'test-connection')
            self.assertEqual(llm.model, 'litellm_proxy/test-model')  # a custom endpoint is addressed through the proxy prefix
            self.assertEqual(llm.litellm_extra_body, {'reasoning_effort': 'high'})  # and carries the effort where the proxy provider cannot drop it
            self.assertFalse(llm.api_key)
            self.assertFalse(profile.enable_switch_llm_tool)
            self.assertEqual(llm.reasoning_effort, 'high')  # written on the profile, not substituted by selections()
            rows = {p['id']: p for p in inventory()['providers']}  # the seam the UI's effort controls key on
            self.assertEqual(rows['connection:test-connection']['efforts'], effort_options('connection:test-connection'))
            self.assertTrue(rows['connection:test-connection']['switchable'])
            self.assertEqual((rows['connection:test-connection']['editable'], rows['connection:test-connection']['connection_id']), (True, 'test-connection'))
            low = prepare(dict(selected, effort='low'))  # a different effort is a different profile, switch aside
            self.assertNotEqual(low, first); self.assertEqual(prepare(dict(selected, effort='low')), low)
            fast = prepare(dict(selected, effort='low'), switch=True)
            self.assertNotIn(fast, (first, low)); self.assertEqual(prepare(dict(selected, effort='low'), switch=True), fast)
            self.assertEqual((low, fast), ({'profile': 'review-test-model-low'}, {'profile': 'review-test-model-low-reading'}))
            self.assertEqual(prepare(dict(selected, effort='low')), low)  # a plain request never reuses the switch profile
            with self.assertRaises(ValueError): prepare({'provider': 'acp:codex', 'model': 'x'}, switch=True)
            reading = get_agent_profile_store().load(fast['profile'])
            self.assertTrue(reading.enable_switch_llm_tool)
            self.assertEqual(get_llm_profile_store().load(reading.llm_profile_ref, cipher=None, resolve_provider=False).reasoning_effort, 'low')
            self.assertEqual(inventory()['selections'][fast['profile']], dict(selected, effort='low', switch=True))
            with self.assertRaises(ValueError): prepare(dict(selected, effort='turbo'))
            self.assertEqual(inventory()['selections'][first['profile']], dict(selected, effort='high', switch=False))
            self.assertFalse((root / 'settings.json').exists())
            with self.assertRaises(ValueError): prepare(dict(selected, model='unadvertised'))
            self.assertEqual(len(get_agent_profile_store().list()), before + 3)  # deep, low-effort, and the reading profile
            # ACP write path: the Codex effort lives in the model id and round-trips through selections().
            from openhands.sdk.profiles import validate_agent_profile, save_profile_preserving_identity
            save_profile_preserving_identity(get_agent_profile_store(), validate_agent_profile(
                {'name': 'codex-probe', 'agent_kind': 'acp', 'acp_server': 'codex', 'acp_model': 'gpt-6-astra/xhigh'}), max_profiles=50)
            self.assertEqual(inventory()['selections']['codex-probe'], {'provider': 'acp:codex', 'model': 'gpt-6-astra', 'effort': 'xhigh', 'switch': False})
            with patch('canvas_discovery.discover', return_value={'models': [{'id': 'gpt-6-astra', 'label': 'x'}]}):
                self.assertEqual(prepare({'provider': 'acp:codex', 'model': 'gpt-6-astra', 'effort': 'xhigh'}), {'profile': 'codex-probe'})
                created = prepare({'provider': 'acp:codex', 'model': 'gpt-6-astra', 'effort': 'high'})['profile']
            self.assertEqual(get_agent_profile_store().load(created).acp_model, 'gpt-6-astra/high')
            self.assertEqual(created, 'review-gpt-6-astra-high')
            # A hand-made namesake with different content is never overwritten: the next generated name takes a suffix.
            save_profile_preserving_identity(get_agent_profile_store(), validate_agent_profile(
                {'name': 'review-gpt-6-astra-low', 'agent_kind': 'acp', 'acp_server': 'codex', 'acp_model': 'gpt-5.5'}), max_profiles=50)
            with patch('canvas_discovery.discover', return_value={'models': [{'id': 'gpt-6-astra', 'label': 'x'}]}):
                self.assertEqual(prepare({'provider': 'acp:codex', 'model': 'gpt-6-astra', 'effort': 'low'}), {'profile': 'review-gpt-6-astra-low-2'})
            self.assertEqual(get_agent_profile_store().load('review-gpt-6-astra-low').acp_model, 'gpt-5.5')
            # An inline-key LLM profile is a source; the variants the app generates from it are not listed as sources.
            from openhands.sdk.llm import LLM
            get_llm_profile_store().save('inline-src', LLM(model='openai/test-model', base_url=f'http://127.0.0.1:{server.server_port}', api_key=SecretStr('test-key')),
                                         include_secrets=True, cipher=cipher, max_profiles=50)
            variant = prepare({'provider': 'profile:inline-src', 'model': 'test-model', 'effort': 'max'})['profile']
            self.assertEqual(variant, 'review-test-model-max')
            inherited = get_llm_profile_store().load(variant, cipher=cipher, resolve_provider=False)
            self.assertEqual((inherited.model, inherited.litellm_extra_body), ('litellm_proxy/test-model', {'reasoning_effort': 'max'}))  # inherited endpoint: proxy prefix and effort body
            # a hand-made pair sharing one name is a source, not a generated variant; a second hand-made
            # profile with the same endpoint and key is the same source; a different key is a different source
            get_llm_profile_store().save('hand-pair', LLM(model='openai/test-model', base_url=f'http://127.0.0.1:{server.server_port}', api_key=SecretStr('test-key')),
                                         include_secrets=True, cipher=cipher, max_profiles=50)
            save_profile_preserving_identity(get_agent_profile_store(), validate_agent_profile(
                {'name': 'hand-pair', 'agent_kind': 'openhands', 'llm_profile_ref': 'hand-pair', 'enable_switch_llm_tool': False}), max_profiles=50)
            get_llm_profile_store().save('other-key', LLM(model='openai/test-model', base_url=f'http://127.0.0.1:{server.server_port}', api_key=SecretStr('other-key')),
                                         include_secrets=True, cipher=cipher, max_profiles=50)
            ids = {p['id'] for p in inventory()['providers']}
            self.assertTrue({'profile:inline-src', 'profile:hand-pair', 'profile:other-key'} <= ids); self.assertNotIn('profile:' + variant, ids)
            self.assertEqual(inventory()['selections']['hand-pair']['provider'], 'profile:hand-pair')
            self.assertEqual(prepare({'provider': 'profile:hand-pair', 'model': 'test-model', 'effort': 'max'}), {'profile': variant})  # same endpoint and key: same source
            # the LLM store's names count as taken too
            get_llm_profile_store().save('review-test-model-medium', LLM(model='openai/x', api_key=SecretStr('k')), include_secrets=True, cipher=cipher, max_profiles=50)
            self.assertEqual(prepare(dict(selected, effort='medium')), {'profile': 'review-test-model-medium-2'})
            ids = {p['id'] for p in inventory()['providers']}
            self.assertIn('profile:inline-src', ids); self.assertNotIn('profile:' + variant, ids)
            # attribution names the canonical source: the first by name among the hand-made profiles sharing that endpoint and key
            self.assertEqual(inventory()['selections'][variant], {'provider': 'profile:hand-pair', 'model': 'test-model', 'effort': 'max', 'switch': False})
            self.assertEqual(prepare({'provider': 'profile:inline-src', 'model': 'test-model', 'effort': 'max'}), {'profile': variant})  # re-saving reuses the variant
            # An agent profile whose LLM profile was deleted is reported with what is missing, not dropped.
            save_profile_preserving_identity(get_agent_profile_store(), validate_agent_profile(
                {'name': 'orphan', 'agent_kind': 'openhands', 'llm_profile_ref': 'nope', 'enable_switch_llm_tool': False}), max_profiles=50)
            self.assertEqual(inventory()['selections']['orphan'], {'provider': 'profile:nope', 'model': None, 'effort': None, 'switch': False, 'problem': "LLM profile 'nope' not found"})
            self.assertEqual(prepare(selected), first)  # a problem row never breaks reuse
            # A variant saved under the old openai/ prefix on a custom endpoint is left alone: the next save builds a proxy-prefixed one.
            get_llm_profile_store().save('review-test-model-xhigh', LLM(model='openai/test-model', provider_connection_id='test-connection', reasoning_effort='xhigh'),
                                         include_secrets=True, cipher=cipher, max_profiles=50)
            save_profile_preserving_identity(get_agent_profile_store(), validate_agent_profile(
                {'name': 'review-test-model-xhigh', 'agent_kind': 'openhands', 'llm_profile_ref': 'review-test-model-xhigh', 'enable_switch_llm_tool': False}), max_profiles=50)
            self.assertEqual(inventory()['selections']['review-test-model-xhigh']['model'], 'test-model')  # both prefixes read back symmetrically
            self.assertEqual(prepare(dict(selected, effort='xhigh')), {'profile': 'review-test-model-xhigh-2'})
            # A proxied variant saved without the effort body (1.14.4) is not reused either.
            get_llm_profile_store().save('review-test-model-none', LLM(model='litellm_proxy/test-model', provider_connection_id='test-connection', reasoning_effort='none'),
                                         include_secrets=True, cipher=cipher, max_profiles=50)
            save_profile_preserving_identity(get_agent_profile_store(), validate_agent_profile(
                {'name': 'review-test-model-none', 'agent_kind': 'openhands', 'llm_profile_ref': 'review-test-model-none', 'enable_switch_llm_tool': False}), max_profiles=50)
            self.assertEqual(prepare(dict(selected, effort='none')), {'profile': 'review-test-model-none-2'})
            self.assertEqual(get_llm_profile_store().load('review-test-model-xhigh-2', cipher=cipher, resolve_provider=False).model, 'litellm_proxy/test-model')
            self.assertEqual(len(get_agent_profile_store().list()), before + 15)  # 3 connection variants, codex-probe, 2 codex variants, the namesake, the inline variant, hand-pair, medium-2, orphan, the old-prefix xhigh pair, the no-body pair
            for path in list((root / 'agent-profiles').glob('*.json')):
                self.assertNotIn('test-key', path.read_text()); self.assertNotIn('other-key', path.read_text())
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_settings_saved_through_the_api_route_a_combined_mode_review(self):
        """The combined-mode chain end to end on the SDK's own stores: the settings API saves a
        primary, a secondary and a fallback through prepare(), and the runner routes a review
        from the profile files that wrote: it starts on the reading profile, tells the agent to
        switch to the primary's LLM profile, signs with the primary's model and verifies the switch."""
        import review_control
        from canvas_discovery import main, agent_store
        from review_runner import Runner
        try:
            from openhands.agent_server.persistence import ProviderConnection
        except ImportError:  # SDK >= 1.49
            from openhands.sdk.llm.provider_connection_store import ProviderConnection
        from pydantic import SecretStr
        root = Path(os.environ['OH_PERSISTENCE_DIR']).resolve()  # shared with the other case: its own names keep them apart
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                if self.headers.get('Authorization') != 'Bearer test-key':
                    self.send_response(401); self.end_headers(); return
                self.send_response(200); self.end_headers()
                self.wfile.write(b'{"data":[{"id":"chain-model"}]}')
        provider = HTTPServer(('127.0.0.1', 0), Provider)
        control = HTTPServer(('127.0.0.1', 0), review_control.Handler)
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (provider, control)]
        for thread in threads: thread.start()
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            # The helper finds the server's key and store root in its process environment; this
            # stand-in /proc entry names the test's, so the chain can never reach a real store.
            proc = temp / 'proc' / '101'; proc.mkdir(parents=True)
            (proc / 'cmdline').write_bytes(b'/usr/bin/openhands-agent-server\0')
            (proc / 'environ').write_bytes(b'\0'.join(f'{k}={v}'.encode() for k, v in
                {'OH_SECRET_KEY': os.environ['OH_SECRET_KEY'], 'OH_PERSISTENCE_DIR': root}.items()) + b'\0')
            (proc / 'cwd').symlink_to(temp)
            def discovery(action, **kwargs):  # the helper's own dispatch, its answer passed through JSON as over docker exec
                with patch('canvas_discovery.discover', side_effect=lambda p: {'models': [{'id': 'chain-astra', 'label': 'x'}]} if p.startswith('acp:') else real_discover(p)):
                    return json.loads(json.dumps(main(dict(kwargs, action=action), proc_root=str(temp / 'proc'))))
            import canvas_discovery
            real_discover = canvas_discovery.discover
            environment = {'OH_PERSISTENCE_DIR': str(root), 'PROFILES_DIR': str(root / 'agent-profiles'),
                           'REVIEW_SETTINGS_FILE': str(temp / 'settings.json'), 'LOCAL_BACKEND_API_KEY': 'test-key',
                           'LABEL_REQUEST': 'review-this'}
            try:
                with patch.dict(os.environ, environment), patch('review_control.discovery', side_effect=discovery):
                    cipher, connections, llms = stores()
                    connections.create(ProviderConnection(id='chain-connection', display_name='Chain', provider='custom',
                        api_key=SecretStr('test-key'), base_url=f'http://127.0.0.1:{provider.server_port}', created_at=1, updated_at=1), cipher=cipher)
                    api = 'connection:chain-connection'
                    body = {'revision': 0, 'primary': {'provider': api, 'model': 'chain-model', 'effort': 'high'},
                            'secondary': {'provider': api, 'model': 'chain-model', 'effort': 'low'},
                            'fallback': {'provider': 'acp:codex', 'model': 'chain-astra', 'effort': 'xhigh'}}
                    request = urllib.request.Request(f'http://127.0.0.1:{control.server_port}/api/review-control/settings', method='PUT',
                        data=json.dumps(body).encode(), headers={'X-Session-API-Key': 'test-key', 'Content-Type': 'application/json'})
                    try:
                        with urllib.request.urlopen(request) as response:
                            saved = json.load(response)
                    except urllib.error.HTTPError as error:
                        raise AssertionError(json.load(error)) from None
                    settings = saved['settings']
                    self.assertEqual((settings['primary'], settings['secondary'], settings['fallback']),
                                     ('review-chain-model-high', 'review-chain-model-low-reading', 'review-chain-astra-xhigh'))
                    self.assertEqual(saved['problems'], {})
                    agents = agent_store()
                    deep, reading = agents.load(settings['primary']), agents.load(settings['secondary'])
                    self.assertTrue(reading.enable_switch_llm_tool); self.assertFalse(deep.enable_switch_llm_tool)
                    def profile_info(name):  # the receivers' lookup (review_hook, github_review_poller), which start serving on import
                        data = json.loads((root / 'agent-profiles' / f'{name}.json').read_text())
                        llm = {p['name']: p for p in canvas('/api/profiles')['profiles']}.get(data.get('llm_profile_ref'), {})
                        return data['id'], (data.get('acp_model') or llm.get('model') or '').split('/', 1)[-1]
                    def canvas(path, data=None):  # Canvas's API: the LLM profile list, conversations
                        if path == '/api/profiles':
                            return {'profiles': [{'name': n[:-5], 'model': llms.load(n[:-5], cipher=None, resolve_provider=False).model} for n in llms.list()]}
                        if data is not None:
                            starts.append(data); return {'id': 'chain-1'}
                        return {'execution_status': 'running', 'agent': {'llm': {'usage_id': f'profile:{deep.llm_profile_ref}'}}}
                    head = 'c' * 40
                    def forge(path):
                        if '/pulls/' in path:
                            return {'state': 'open', 'head': {'sha': head}}
                        return [{'body': f'[hands-bot review] reviewed at head {head}\n\nVERDICT\nREADY_FOR_HUMAN_MERGE'}] if starts else []
                    starts, labels, failures, logs = [], [], [], []
                    (temp / 'prompt').write_text('model={model} label={label}')
                    Runner(forge, canvas, lambda *args: labels.append(args), profile_info, lambda *args: failures.append(args),
                           log=logs.append, sleep=lambda _: None, timeout=10).run('owner/repo', 1, 'title', 'review-this', 'ignored', str(temp / 'prompt'), str(temp))
                    self.assertEqual(failures, [])
                    self.assertEqual(len(starts), 1)
                    self.assertEqual(starts[0]['agent_profile_id'], str(reading.id), 'the conversation starts on the reading profile')
                    text = starts[0]['initial_message']['content'][0]['text']
                    self.assertIn('model=chain-model', text, 'the primary model signs the review')
                    self.assertIn(f'starts on LLM profile `{reading.llm_profile_ref}`', text)
                    self.assertIn(f'profile_name\n`{deep.llm_profile_ref}`', text, 'the switch names the primary\'s LLM profile')
                    self.assertIn(f'switch verified: owner/repo#1 conversation=chain-1 on `{deep.llm_profile_ref}`', logs)
                    self.assertIn(('owner/repo', 1, 'hands-reviewed', True), labels)
                    self.assertEqual(json.loads((temp / 'settings.json').read_text())['primary'], 'review-chain-model-high')
            finally:
                for server in (provider, control): server.shutdown(); server.server_close()
                for thread in threads: thread.join()


if __name__ == '__main__': unittest.main()
