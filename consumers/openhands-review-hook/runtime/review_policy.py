"""Shared review configuration. No provider credentials are stored here."""
import json
import os
import re
import tempfile
from pathlib import Path


PROFILE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


class PairMismatch(ValueError):
    """The reading and primary agent profiles differ beyond the generated fields.
    Its message names only profile field names, so the control server may
    return it to the app as is."""


def profiles_dir():
    return Path(os.environ.get('PROFILES_DIR', '/opt/openhands/canvas-state/agent-profiles'))


def profiles():
    result = []
    for path in sorted(profiles_dir().glob('*.json')):
        if not PROFILE_NAME.fullmatch(path.stem):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue  # a half-written or unreadable profile file never breaks the listing
        if not isinstance(data, dict):
            continue
        acp = data.get('agent_kind') == 'acp'
        result.append({'name': path.stem, 'model': data.get('acp_model') or data.get('llm_profile_ref') or path.stem,
                       'llm_profile_ref': None if acp else data.get('llm_profile_ref'),
                       'switch': bool(not acp and data.get('enable_switch_llm_tool'))})
    return result


# What the Auto Reviews app generates per profile of a combined-mode pair; every
# other agent setting is shared by the pair, because the conversation runs on the
# reading profile's agent and the primary contributes only its LLM and signature.
GENERATED_FIELDS = frozenset({'id', 'name', 'revision', 'llm_profile_ref', 'enable_switch_llm_tool'})


def agent_settings_diff(primary, secondary):
    """The agent settings on which two OpenHands-kind profiles differ beyond the
    generated fields, sorted; empty when combined mode would run on exactly the
    primary's settings. Unreadable profiles count as fully different."""
    docs = []
    for name in (primary, secondary):
        try:
            docs.append(json.loads((profiles_dir() / (name + '.json')).read_text()))
        except (OSError, ValueError):
            return ['(unreadable profile)']
    a, b = ({k: v for k, v in d.items() if k not in GENERATED_FIELDS} for d in docs)
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def profile_llm_ref(name):
    """The LLM profile an OpenHands-kind agent profile starts on; None for ACP agents."""
    if not PROFILE_NAME.fullmatch(name or ''):
        return None
    try:
        data = json.loads((profiles_dir() / (name + '.json')).read_text())
    except (OSError, ValueError):
        return None
    return None if data.get('agent_kind') == 'acp' else (data.get('llm_profile_ref') or None)


def settings_path():
    return Path(os.environ.get('REVIEW_SETTINGS_FILE', '/opt/openhands/hooks/review-settings.json'))


def validate(data, require_profiles=True):
    """Settings are agent profile names. `secondary` is the reading profile of
    combined mode (absent in files written before 1.13.0, meaning None).

    Reading tolerates names whose profile is gone (`require_profiles=False`):
    the saved file is never rewritten by the runtime, `settings_problems()`
    says what is missing and a person fixes or replaces it. Writing is strict.
    """
    keys = set(data) if isinstance(data, dict) else set()
    if not {'revision', 'primary', 'fallback'} <= keys or not keys <= {'revision', 'primary', 'secondary', 'fallback'}:
        raise ValueError('Expected revision, primary, secondary and fallback')
    data = {'secondary': None, **data}
    if type(data['revision']) is not int or data['revision'] < 0:
        raise ValueError('Invalid revision')
    if not isinstance(data['primary'], str) or not PROFILE_NAME.fullmatch(data['primary']):
        raise ValueError('Select a primary agent profile')
    for key in ('secondary', 'fallback'):
        if data[key] is not None and (not isinstance(data[key], str) or not PROFILE_NAME.fullmatch(data[key])):
            raise ValueError(f'Select a {key} agent profile or None')
    chosen = [data[k] for k in ('primary', 'secondary', 'fallback') if data[k] is not None]
    if len(set(chosen)) != len(chosen):
        raise ValueError('Primary, secondary and fallback must be different')
    if not require_profiles:
        return data
    known = {p['name']: p for p in profiles()}
    for key in ('primary', 'secondary', 'fallback'):
        if data[key] is not None and data[key] not in known:
            raise ValueError(f'Select an existing {key} agent profile')
    if data['secondary'] is not None:
        # The switch target must be a saved LLM profile and the reading profile
        # must actually have the switch_llm tool (a per-profile flag).
        if not known[data['primary']]['llm_profile_ref'] or not known[data['secondary']]['llm_profile_ref']:
            raise ValueError('Reasoning profiles need API providers for primary and secondary')
        if not known[data['secondary']]['switch']:
            raise ValueError('The secondary profile must have the switch_llm tool enabled')
        diff = agent_settings_diff(data['primary'], data['secondary'])
        if diff:
            raise PairMismatch('The secondary profile must match the primary except for its LLM profile and the switch tool '
                               f'(differs in: {", ".join(diff)}); the review runs on the secondary\'s agent settings')
    return data


def read_settings():
    try:
        data = json.loads(settings_path().read_text())
    except FileNotFoundError:
        data = {'revision': 0, 'primary': os.environ.get('REVIEW_PROFILE', 'codex-astra'), 'secondary': None, 'fallback': None}
    return validate(data, require_profiles=False)


def load_settings():
    """Best-effort view for the app: never raises. Problems explain what is wrong."""
    try:
        data = read_settings()
    except (ValueError, OSError) as error:
        placeholder = {'revision': 0, 'primary': None, 'secondary': None, 'fallback': None}
        return placeholder, {'settings': f'the saved settings file cannot be read ({error}); choose profiles and save to replace it'}
    return data, settings_problems(data)


def settings_problems(data):
    """Per role, why the saved choice cannot run as-is; empty when all is well."""
    known = {p['name']: p for p in profiles()}
    problems = {}
    for role in ('primary', 'secondary', 'fallback'):
        name = data.get(role)
        if name is not None and name not in known:
            problems[role] = f'agent profile {name!r} no longer exists'
    secondary = data.get('secondary')
    if secondary is not None and 'secondary' not in problems and 'primary' not in problems:
        if not known[data['primary']]['llm_profile_ref'] or not known[secondary]['llm_profile_ref'] or not known[secondary]['switch']:
            problems['secondary'] = f'agent profile {secondary!r} is no longer a switch-enabled API profile paired with an API primary'
        else:
            diff = agent_settings_diff(data['primary'], secondary)
            if diff:
                problems['secondary'] = (f'agent profile {secondary!r} differs from the primary beyond its LLM profile and the switch tool '
                                         f'({", ".join(diff)}); the review would run on its agent settings, not the primary\'s')
    return problems


def save_settings(data):
    """The control server is the only writer and serializes requests."""
    data = validate(data)
    try:
        current = read_settings()
    except ValueError:  # an unreadable file is replaced only by this explicit save at revision 0
        current = {'revision': 0}
    if data['revision'] != current['revision']:
        raise FileExistsError('Settings changed; reload before saving')
    data = dict(data, revision=current['revision'] + 1)
    # Pre-1.13 runtimes reject the key: write it only once a secondary is set.
    written = {k: v for k, v in data.items() if k != 'secondary' or v is not None}
    path = settings_path()
    fd, temp = tempfile.mkstemp(prefix='.review-settings-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(written, out)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return data


def requested_profiles(names, label):
    # Resolve the default when the trigger is handled, never at service startup.
    requested = sorted({n for n in names if n == label or n.startswith(label + ':')})
    explicit = [n for n in requested if n != label]
    if len(explicit) > 1:
        raise ValueError('Use only one explicit reviewer label')
    out = [(n, n[len(label) + 1:]) for n in explicit]
    if label in requested:
        out.append((label, None))  # the runner resolves the primary from the settings when it runs
    return out
