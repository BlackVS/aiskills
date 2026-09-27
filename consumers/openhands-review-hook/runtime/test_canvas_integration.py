"""Run in Canvas with OH_PERSISTENCE_DIR pointing to a fresh temporary directory."""
import json
import os
import threading
import unittest
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
            self.assertEqual(len(get_agent_profile_store().list()), 3)  # deep, low-effort, and the reading profile
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
            self.assertEqual(len(get_agent_profile_store().list()), 15)  # 3 connection variants, codex-probe, 2 codex variants, the namesake, the inline variant, hand-pair, medium-2, orphan, the old-prefix xhigh pair, the no-body pair
            for path in list((root / 'agent-profiles').glob('*.json')):
                self.assertNotIn('test-key', path.read_text()); self.assertNotIn('other-key', path.read_text())
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main()
