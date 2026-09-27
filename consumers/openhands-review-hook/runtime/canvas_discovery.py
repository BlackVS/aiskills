"""Run inside the existing Canvas environment; emit only public model metadata."""
import asyncio
import json
import re
import os
import shutil
import signal
import urllib.request
import urllib.error
from urllib.parse import urlsplit
from pathlib import Path


def server_environment(proc_root='/proc'):
    """Match the running server's cipher and storage without persisting secrets."""
    configs = []
    for command in Path(proc_root).glob('[0-9]*/cmdline'):
        try:
            executable = command.read_bytes().split(b'\0')[0].split(b'/')[-1]
            if executable != b'openhands-agent-server':
                continue
            raw = dict(part.split(b'=', 1) for part in
                (command.parent / 'environ').read_bytes().split(b'\0') if b'=' in part)
            key = raw.get(b'OH_SECRET_KEY')
            if not key:
                continue
            cwd = (command.parent / 'cwd').resolve(strict=True)
            config = {'OH_SECRET_KEY': key.decode()}
            for name, default in [('OH_PERSISTENCE_DIR', '.openhands'),
                    ('OPENHANDS_AGENT_SERVER_CONFIG_PATH', 'workspace/openhands_agent_server_config.json')]:
                value = raw.get(name.encode())
                # Leave the persistence default to the SDK/config file.
                if value is None and name == 'OH_PERSISTENCE_DIR':
                    continue
                path = Path(value.decode() if value else default)
                config[name] = str(path if path.is_absolute() else cwd / path)
            configs.append(config)
        except (OSError, ValueError, UnicodeError):
            continue
    if not configs or any(config != configs[0] for config in configs[1:]):
        raise ValueError('Cannot identify a unique Canvas server configuration')
    return configs[0]

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# Reasoning effort per provider kind. API providers carry it on the LLM profile
# (SDK: reasoning_effort, provider strings pass through). Codex account agents
# carry it in the ACP model id ("model/effort", which the SDK splits into the
# reasoning_effort config option); the Claude account adapter takes none.
API_EFFORTS = ['none', 'low', 'medium', 'high', 'xhigh', 'max']
CODEX_EFFORTS = ['low', 'medium', 'high', 'xhigh']
API_DEFAULT_EFFORT = 'high'  # the SDK default when a profile sets none


def effort_options(provider):
    kind, name = provider.split(':', 1)
    if kind != 'acp':
        return API_EFFORTS
    return CODEX_EFFORTS if name == 'codex' else []


def split_codex_model(acp_model):
    """'gpt-6-astra/xhigh' -> ('gpt-6-astra', 'xhigh'); no suffix -> (model, None)."""
    base, sep, effort = (acp_model or '').rpartition('/')
    if sep and base and effort in CODEX_EFFORTS:
        return base, effort
    return acp_model, None


def normalize_effort(provider, effort):
    """Validate an effort for a provider kind and apply the API default."""
    options = effort_options(provider)
    if effort is not None and (not isinstance(effort, str) or effort not in options):
        raise ValueError('Reasoning effort is not available for this provider')
    if effort is None and provider.split(':', 1)[0] != 'acp':
        return API_DEFAULT_EFFORT
    return effort


def api_models(base_url, key):
    """Query the configured provider only; never forward its key on redirects."""
    parsed = urlsplit(base_url or '')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Invalid provider URL')
    request = urllib.request.Request(base_url.rstrip('/') + '/models',
        headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('Model response too large')
    data = json.loads(raw)
    rows = data.get('data')
    if not isinstance(rows, list):
        raise ValueError('Provider did not return a model list')
    return [{'id': model, 'label': model} for model in sorted({
        row['id'] for row in rows if isinstance(row, dict)
        and isinstance(row.get('id'), str) and 0 < len(row['id']) <= 256})]


def session_models(result):
    rows = []
    for option in result.get('configOptions', []):
        if option.get('id') == 'model' or option.get('category') == 'model':
            for item in option.get('options', []):
                rows.extend(item.get('options', [item]))
    if not rows:
        rows = result.get('models', {}).get('availableModels', [])
    found = {}
    for row in rows:
        model = row.get('value', row.get('modelId'))
        if isinstance(model, str) and model:
            found[model] = {'id': model, 'label': row.get('name', model)}
    if not found:
        raise ValueError('ACP did not advertise models')
    return list(found.values())


async def acp_models(binary):
    process = await asyncio.create_subprocess_exec(binary,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=2_000_000)
    async def rpc(number, method, params):
        process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': number,
            'method': method, 'params': params}) + '\n').encode())
        await process.stdin.drain()
        while True:
            line = await process.stdout.readline()
            if not line:
                raise ValueError('ACP exited before discovery completed')
            event = json.loads(line)
            if event.get('id') == number and 'method' not in event:
                if 'error' in event:
                    raise ValueError('ACP authentication or discovery failed')
                return event['result']
            if 'id' in event and 'method' in event:
                process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': event['id'],
                    'error': {'code': -32601, 'message': 'Discovery does not execute tools'}}) + '\n').encode())
                await process.stdin.drain()
    try:
        async with asyncio.timeout(35):
            await rpc(1, 'initialize', {'protocolVersion': 1, 'clientCapabilities': {}})
            return session_models(await rpc(2, 'session/new', {'cwd': '/tmp', 'mcpServers': []}))
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


def stores():
    """(cipher, provider connections store, LLM profile store) of the running server.

    SDK 1.46 exposed process-wide accessors in the agent server's persistence
    package; SDK 1.49 dropped them and constructs the stores on the default
    persistence directory (`OH_PERSISTENCE_DIR`, else `~/.openhands`).
    """
    from openhands.agent_server.config import get_default_config
    cipher = get_default_config().cipher
    try:
        from openhands.agent_server.persistence import get_provider_connections_store, get_llm_profile_store
        return cipher, get_provider_connections_store(), get_llm_profile_store()
    except ImportError:
        from openhands.sdk.llm.provider_connection_store import ProviderConnectionStore
        from openhands.sdk.llm.llm_profile_store import LLMProfileStore
        base = persistence_dir()
        connections = ProviderConnectionStore(base / 'provider-connections')
        return cipher, connections, LLMProfileStore(base / 'profiles', provider_store=connections)


def persistence_dir():
    """The server's persistence root (`OH_PERSISTENCE_DIR`, else `~/.openhands`).

    SDK 1.49 builds the two profile stores under it by default but not the
    provider-connection store, so every store is given its directory explicitly.
    """
    from openhands.sdk.utils.path import get_user_persistence_dir
    return Path(get_user_persistence_dir())


def agent_store():
    try:
        from openhands.agent_server.persistence import get_agent_profile_store
    except ImportError:  # SDK >= 1.49
        from openhands.sdk.profiles.agent_profile_store import AgentProfileStore
        return AgentProfileStore(persistence_dir() / 'agent-profiles')
    return get_agent_profile_store()


GENERATED_PREFIXES = ('review-', 'auto-review-')


def is_generated(agent):
    """An agent profile the app created: OpenHands kind, its LLM profile shares its
    name, and the name carries the app's prefix (`review-`; `auto-review-` before
    1.14.0). Hand-made pairs that happen to share a name are not generated."""
    return (agent.agent_kind != 'acp' and agent.llm_profile_ref == agent.name
            and agent.name.startswith(GENERATED_PREFIXES))


def inline_sources():
    """Hand-made inline-key LLM profiles by name -> (base_url, key); unreadable ones skipped."""
    cipher, _, llms = stores()
    agents = agent_store()
    generated = {a.name for a in (agents.load(f) for f in agents.list()) if is_generated(a)}
    sources = {}
    for summary in sorted(llms.list_summaries(), key=lambda row: row['name']):
        name = summary['name']
        if name in generated or summary.get('provider_connection_id') or not summary.get('api_key_set'):
            continue
        try:
            llm = llms.load(name, cipher=cipher, resolve_provider=False)
        except (ValueError, FileNotFoundError):
            continue
        sources[name] = (llm.base_url, llm.api_key.get_secret_value() if llm.api_key else None)
    return sources


def canonical_source(name, sources=None):
    """The first (by name) hand-made inline profile with the same endpoint and key.

    Several hand-made profiles may share one endpoint and key; the app treats them
    as one source so a variant is attributed and reused the same way whichever of
    them was picked."""
    sources = inline_sources() if sources is None else sources
    identity = sources.get(name)
    if identity is None:
        return name
    return next(n for n, i in sources.items() if i == identity)


def generated_origins():
    """Map each LLM profile the app generated from an inline-key profile to its
    canonical source. A variant whose source is gone maps to nothing and is
    treated like a hand-made profile (listed, and its own provider)."""
    cipher, _, llms = stores()
    agents = agent_store()
    sources = inline_sources()
    by_identity = {}
    for name, identity in sources.items():
        by_identity.setdefault(identity, name)
    origins = {}
    for agent in (agents.load(f) for f in agents.list()):
        if not is_generated(agent):
            continue
        try:
            llm = llms.load(agent.name, cipher=cipher, resolve_provider=False)
        except (ValueError, FileNotFoundError):
            continue
        if llm.provider_connection_id:
            continue
        source = by_identity.get((llm.base_url, llm.api_key.get_secret_value() if llm.api_key else None))
        if source:
            origins[agent.name] = source
    return origins


def inventory():
    cipher, connections, profiles = stores()
    generated = generated_origins()
    providers = [{'id': 'connection:' + c.id, 'name': c.display_name,
        'kind': 'api', 'url': c.base_url, 'configured': bool(c.api_key_value())}
        for c in connections.list(cipher=cipher)]
    for summary in profiles.list_summaries():
        if not summary.get('provider_connection_id') and summary.get('api_key_set') and summary['name'] not in generated:
            providers.append({'id': 'profile:' + summary['name'], 'name': summary['name'],
                'kind': 'api', 'url': summary.get('base_url'), 'configured': True})
    for provider, binary, label in [('claude-code', 'claude-agent-acp', 'Claude'), ('codex', 'codex-acp', 'Codex')]:
        if shutil.which(binary):
            providers.append({'id': 'acp:' + provider, 'name': label,
                'kind': 'subscription', 'configured': True})
    for row in providers:
        row['efforts'] = effort_options(row['id'])
        row['switchable'] = row['kind'] == 'api'  # switch_llm exists only for LLM-profile agents
    return {'providers': providers, 'selections': selections()}


def selections():
    from openhands.sdk.settings.acp_providers import detect_acp_provider_by_command
    import shlex
    cipher, _, llms = stores()
    agents = agent_store()
    origins = generated_origins()
    result = {}
    for filename in agents.list():
        agent = agents.load(filename)
        if agent.agent_kind == 'acp':
            server = agent.acp_server
            if server == 'custom' and agent.acp_command:
                detected = detect_acp_provider_by_command(shlex.split(agent.acp_command))
                server = detected.key if detected else server
            provider = 'acp:' + server
            model, effort = split_codex_model(agent.acp_model) if server == 'codex' else (agent.acp_model, None)
            switch = False
        else:
            try:
                llm = llms.load(agent.llm_profile_ref, cipher=cipher)
            except (ValueError, FileNotFoundError):
                # Shown to the person with what is missing; never dropped or repaired here.
                result[agent.name] = {'provider': 'profile:' + str(agent.llm_profile_ref), 'model': None, 'effort': None,
                                      'switch': bool(getattr(agent, 'enable_switch_llm_tool', False)),
                                      'problem': f'LLM profile {agent.llm_profile_ref!r} not found'}
                continue
            provider = ('connection:' + llm.provider_connection_id if llm.provider_connection_id
                else 'profile:' + origins.get(agent.llm_profile_ref, agent.llm_profile_ref))
            model = bare_model(llm.model)
            effort = getattr(llm, 'reasoning_effort', None) or API_DEFAULT_EFFORT
            switch = bool(getattr(agent, 'enable_switch_llm_tool', False))
        result[agent.name] = {'provider': provider, 'model': model, 'effort': effort, 'switch': switch}
    return result


def kind_of(provider):
    return provider.split(':', 1)[0]


MODEL_PREFIXES = ('litellm_proxy/', 'openai/')


def bare_model(model):
    """The advertised model id behind a saved LLM profile's model string."""
    for prefix in MODEL_PREFIXES:
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def model_string(model, proxied):
    """Model string for a generated LLM profile.

    A custom endpoint (any base_url) is addressed as `litellm_proxy/<model>`:
    with `openai/`, litellm's chat-to-Responses bridge reroutes every step that
    carries tools and a reasoning effort for a model its cost map knows to
    `/responses`, which such proxies do not serve (every gpt-6-astra review died
    on a 404). `openai/` stays for OpenAI itself.
    """
    return ('litellm_proxy/' if proxied else 'openai/') + model


def effort_body(effort, proxied):
    """litellm's proxy provider drops `reasoning_effort` (it does not know the model
    as a reasoning model and `drop_params` is on), and a proxied upstream refused
    every tool call that arrived without one. The extra body is sent verbatim,
    so the effort travels there as well for proxied profiles."""
    return {'reasoning_effort': effort} if proxied and effort else {}


def _stem(entry):
    """Profile name from a store listing entry, whether it is `name` or `name.json`."""
    return entry[:-5] if entry.endswith('.json') else entry


def profile_name(model, effort, switch, taken):
    """Readable, unique name for a generated profile: review-<model>[-<effort>][-reading].

    A numeric suffix resolves a clash with any existing agent or LLM profile;
    the caller matches profiles by content first, so a name is only chosen for
    a profile that does not exist yet.
    """
    slug = re.sub(r'[^A-Za-z0-9.]+', '-', model).strip('-.')[:36] or 'model'  # 7 + 36 + 7 + 8 + 4 <= 64 up to suffix -999
    base = 'review-' + slug + ('-' + effort if effort else '') + ('-reading' if switch else '')
    name, n = base, 2
    while name in taken:
        name = f'{base}-{n}'; n += 1
    return name


def prepare(selection, switch=False):
    """Reuse matching profiles; otherwise create a new immutable launch choice.

    `selection` is {provider, model[, effort]}. `switch` asks for the reading
    profile of combined mode: an OpenHands-kind agent with the switch_llm tool.
    """
    from openhands.sdk.profiles import validate_agent_profile, save_profile_preserving_identity
    from openhands.sdk.llm import LLM
    if not isinstance(selection, dict) or not {'provider', 'model'} <= set(selection) <= {'provider', 'model', 'effort'}:
        raise ValueError('Expected provider, model and optional effort')
    provider, model = selection['provider'], selection['model']
    if not isinstance(provider, str) or not isinstance(model, str) or ':' not in provider:
        raise ValueError('Invalid selection')
    effort = normalize_effort(provider, selection.get('effort'))
    if switch and provider.startswith('acp:'):
        raise ValueError('Reasoning profiles need an API provider')
    if model not in {m['id'] for m in discover(provider)['models']}:
        raise ValueError('Model is not advertised by this provider')
    if kind_of(provider) == 'profile':  # several hand-made profiles with one endpoint and key are one source
        provider = 'profile:' + canonical_source(provider.split(':', 1)[1])
    wanted = {'provider': provider, 'model': model, 'effort': effort}
    known = selections()
    cipher, connections, llms = stores()
    agents = agent_store()
    kind, source = provider.split(':', 1)
    if kind == 'profile':
        proxied = bool(llms.load(source, cipher=cipher, resolve_provider=False).base_url)
    elif kind == 'connection':
        proxied = bool(connections.get(source, cipher=cipher).base_url)
    else:
        proxied = False
    for name, existing in known.items():
        if {k: existing[k] for k in wanted} != wanted or existing['switch'] != bool(switch):
            continue
        if kind != 'acp':  # a variant saved under the other prefix is left alone, never reused
            try:
                saved = llms.load(agents.load(name).llm_profile_ref, cipher=cipher, resolve_provider=False)
            except (ValueError, FileNotFoundError):
                continue
            if saved.model != model_string(model, proxied):
                continue
            if (getattr(saved, 'litellm_extra_body', None) or {}).get('reasoning_effort') != effort_body(effort, proxied).get('reasoning_effort'):
                continue  # a proxied variant without the effort in its extra body cannot run tool calls
        return {'profile': name}
    taken = set(known) | {_stem(f) for f in agents.list()} | {_stem(f) for f in llms.list()}
    name = profile_name(model, effort, switch, taken)
    template = next((n for n, s in known.items() if s['provider'] == provider), None)
    if kind == 'acp':
        payload = agents.load(template).model_dump(mode='json') if template else {
            'agent_kind': 'acp', 'acp_server': source}
        payload['acp_model'] = model + ('/' + effort if effort else '')
    else:
        if kind == 'profile':
            llm = llms.load(source, cipher=cipher, resolve_provider=False)
            extra = dict(getattr(llm, 'litellm_extra_body', None) or {}, **effort_body(effort, proxied))
            llm = llm.model_copy(update={'model': model_string(model, proxied), 'reasoning_effort': effort, 'litellm_extra_body': extra})
        else:
            llm = LLM(model=model_string(model, proxied), provider_connection_id=source, reasoning_effort=effort,
                      litellm_extra_body=effort_body(effort, proxied))
        # An existing inline-key profile stays in Canvas's existing LLM store.
        try:
            stored = llms.load(name, cipher=cipher, resolve_provider=False)
        except FileNotFoundError:
            llms.save(name, llm, include_secrets=True, cipher=cipher, max_profiles=50)
        else:
            if stored.model_dump(mode='json') != llm.model_dump(mode='json') or stored.api_key != llm.api_key:
                raise ValueError('Generated LLM profile name already in use')
        payload = {'agent_kind': 'openhands', 'llm_profile_ref': name,
            'enable_switch_llm_tool': bool(switch)}
    payload.update(name=name)
    payload.pop('id', None)
    payload.pop('revision', None)
    with agents.lock():
        try:
            agents.load(name)
        except FileNotFoundError:
            save_profile_preserving_identity(agents, validate_agent_profile(payload), max_profiles=50)
        else:
            raise ValueError('Generated agent profile name already in use')
    return {'profile': name}


def discover(provider):
    if provider not in {p['id'] for p in inventory()['providers']}:
        raise ValueError('Unknown provider')
    kind, name = provider.split(':', 1)
    if kind == 'acp':
        binary = {'claude-code': 'claude-agent-acp', 'codex': 'codex-acp'}[name]
        return {'models': asyncio.run(acp_models(shutil.which(binary)))}
    cipher, connections, profiles = stores()
    if kind == 'connection':
        connection = connections.get(name, cipher=cipher)
        url, key = connection.base_url, connection.api_key_value()
        if not url and connection.provider == 'openai':
            url = 'https://api.openai.com/v1'
    else:
        profile = profiles.load(name, cipher=cipher)
        url = profile.base_url
        key = profile.api_key.get_secret_value() if profile.api_key else None
    if not key:
        raise ValueError('Provider credential unavailable')
    return {'models': api_models(url, key)}


def probe(request):
    """Read-only connection check; never return provider error bodies or secrets."""
    try:
        if set(request) == {'provider'} and isinstance(request['provider'], str):
            models = discover(request['provider'])['models']
        elif set(request) in ({'provider', 'base_url'}, {'provider', 'base_url', 'api_key'}) and all(isinstance(v, str) and v for v in request.values()):
            if not request['provider'].startswith('connection:'):
                raise ValueError('Only provider connections can be edited')
            cipher, connections, _ = stores()
            connection = connections.get(request['provider'].split(':', 1)[1], cipher=cipher)
            if connection is None:
                raise ValueError('Unknown provider')
            models = api_models(request['base_url'], request.get('api_key') or connection.api_key_value())
        elif set(request) == {'base_url', 'api_key'} and all(isinstance(v, str) and v for v in request.values()):
            models = api_models(request['base_url'], request['api_key'])
        else:
            raise ValueError('Invalid probe')
        if not models:
            return {'ok': False, 'message': 'Connected, but the provider returned no models.'}
        return {'ok': True, 'models': models}
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            message = 'Authentication rejected. Check the API token and its permissions.'
        elif error.code == 429:
            message = 'The provider is rate-limiting requests. Try again later.'
        elif error.code in (301, 302, 303, 307, 308):
            message = 'The endpoint redirects requests. Enter its final API base URL.'
        elif error.code == 404:
            message = 'Model endpoint not found. Check the API base URL.'
        else:
            message = 'The provider returned an HTTP error. Try again later or check its endpoint.'
        return {'ok': False, 'message': message}
    except (TimeoutError, asyncio.TimeoutError):
        return {'ok': False, 'message': 'Connection timed out. Try again or check the endpoint.'}
    except Exception:
        return {'ok': False, 'message': 'Could not discover models. Check the endpoint, token, or account login.'}

if __name__ == '__main__':
    import sys
    os.environ['OPENHANDS_SUPPRESS_BANNER'] = '1'
    try:
        request = json.load(sys.stdin)
        os.environ.update(server_environment())
        if request['action'] == 'inventory':
            result = inventory()
        elif request['action'] == 'models':
            result = discover(request['provider'])
        elif request['action'] == 'probe':
            result = probe(request['request'])
        elif request['action'] == 'prepare':
            result = prepare(request['selection'], switch=bool(request.get('switch', False)))
        else:
            raise ValueError('Unknown action')
        print(json.dumps(result))
    except Exception:
        # Provider responses and SDK exceptions may contain credentials.
        print(json.dumps({'error': 'Discovery failed. Check provider credentials, URL, and account login in Canvas.'}))
        sys.exit(1)
