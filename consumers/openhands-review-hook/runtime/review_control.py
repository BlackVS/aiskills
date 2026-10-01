"""Authenticated settings API for the Auto Reviews Canvas App."""
import hmac
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, HTTPServer

from canvas_discovery import normalize_effort, probe_shape
from review_policy import PairMismatch, load_settings, profiles, save_settings

REPLIED = object()  # json_body() already answered the request


class DiscoveryFailed(OSError):
    """The discovery helper gave no usable answer. Its text stays generic; `reason` is a fixed
    description for the service log, never the helper's output (which may hold credentials)."""
    def __init__(self, reason):
        super().__init__('Canvas discovery failed')
        self.reason = reason


def discovery(action, **kwargs):
    source = Path(__file__).with_name('canvas_discovery.py').read_text()
    command = ['docker', 'exec', '-i', '-e', 'OPENHANDS_SUPPRESS_BANNER=1',
        os.environ.get('CANVAS_CONTAINER', 'openhands-canvas'), 'python', '-c', source]
    result = subprocess.run(command, input=json.dumps(dict(action=action, **kwargs)),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=55)
    if result.returncode:
        raise DiscoveryFailed(f'helper exited with status {result.returncode}')
    try:
        data = json.loads(result.stdout)
    except ValueError:  # a control-plane failure, not an invalid request
        raise DiscoveryFailed('helper output is not JSON') from None
    if not isinstance(data, dict):
        raise DiscoveryFailed('helper output is not a JSON object')
    if 'error' in data:
        raise DiscoveryFailed('helper reported an error')
    return data


def failure_reason(error):
    """What went wrong on the control plane, safe to log: fixed text and numbers only, never an
    exception's message (a timeout's quotes the whole command, a helper's may quote its output)."""
    if isinstance(error, DiscoveryFailed):
        return error.reason
    if isinstance(error, subprocess.TimeoutExpired):
        return f'helper timed out after {error.timeout:g} s'
    errno = getattr(error, 'errno', None)
    return type(error).__name__ + (f' (errno {errno})' if errno else '')


def log_unavailable(what, error):
    """The user sees a fixed "unavailable" text; the operator finds the reason in the journal."""
    print(f'review-control: {what} unavailable: {failure_reason(error)}', file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status, text):
        """Answer an error. The connection test answers every outcome as {ok, message} (its
        results are {ok: true, models} or {ok: false, message}); the settings API as {error}.
        The app reads only a string message and treats anything but ok: true as a failure,
        whether the host's request helper resolves or throws on an error status."""
        if urlsplit(self.path).path == '/api/review-control/test-provider':
            self.reply(status, {'ok': False, 'message': text})
        else:
            self.reply(status, {'error': text})

    def json_body(self):
        """The request's JSON body (at most 4 KiB), or REPLIED after a 415 or 413 answer.
        Unparseable JSON raises ValueError, which each caller answers with its own 400."""
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            self.fail(415, 'Use application/json')
            return REPLIED
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 4096 or self.headers.get('Transfer-Encoding'):
            self.fail(413, 'Invalid request size')
            return REPLIED
        return json.loads(self.rfile.read(length))

    def authorized(self):
        expected = os.environ.get('LOCAL_BACKEND_API_KEY', '')
        supplied = self.headers.get('X-Session-API-Key', '')
        if not supplied and self.headers.get('Authorization', '').startswith('Bearer '):
            supplied = self.headers['Authorization'][7:]
        if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
            self.fail(401, 'Authentication required')
            return False
        return True

    def handle_settings(self, write=False):
        if not self.authorized():
            return
        path = urlsplit(self.path)
        if path.path not in ('/api/review-control/settings', '/api/review-control/providers', '/api/review-control/models'):
            self.fail(404, 'Not found')
            return
        try:
            if not write and path.path == '/api/review-control/providers':
                self.reply(200, discovery('inventory'))
                return
            if not write and path.path == '/api/review-control/models':
                query = parse_qs(path.query)
                provider = query.get('provider', [''])[0]
                if not provider or len(provider) > 256:
                    raise ValueError('Invalid provider')
                self.reply(200, discovery('models', provider=provider))
                return
            if path.path != '/api/review-control/settings':
                self.fail(405, 'Method not allowed')
                return
            if write:
                candidate = self.json_body()
                if candidate is REPLIED:
                    return
                roles = ('primary', 'secondary', 'fallback')
                if isinstance(candidate, dict) and any(isinstance(candidate.get(k), dict) for k in roles):
                    candidate = {'secondary': None, **candidate}
                    if set(candidate) != {'revision', *roles}:
                        raise ValueError('Invalid settings')
                    optional_ok = all(candidate[k] is None or isinstance(candidate[k], dict) for k in ('secondary', 'fallback'))
                    if type(candidate['revision']) is not int or not isinstance(candidate['primary'], dict) or not optional_ok:
                        raise ValueError('Invalid selections')
                    for key in roles:  # compare what prepare() will actually create
                        if isinstance(candidate[key], dict) and isinstance(candidate[key].get('provider'), str) and ':' in candidate[key]['provider']:
                            candidate[key] = dict(candidate[key], effort=normalize_effort(candidate[key]['provider'], candidate[key].get('effort')))
                    if candidate['revision'] != load_settings()[0]['revision']:
                        raise FileExistsError('Settings changed')
                    chosen = [json.dumps(candidate[k], sort_keys=True) for k in roles if candidate[k] is not None]
                    if len(set(chosen)) != len(chosen):
                        raise ValueError('Choose different reviewers')
                    if candidate['secondary'] is not None and any(
                            str(candidate[k].get('provider', '')).startswith('acp:') for k in ('primary', 'secondary')):
                        raise ValueError('Reasoning profiles need API providers')
                    for key in roles:
                        if isinstance(candidate[key], dict):
                            candidate[key] = discovery('prepare', selection=candidate[key], switch=key == 'secondary')['profile']
                data, problems = save_settings(candidate), {}
            else:
                data, problems = load_settings()
            self.reply(200, {'settings': data, 'profiles': profiles(), 'problems': problems})
        except FileExistsError:
            self.fail(409, 'Settings changed; reload before saving')
        except PairMismatch as error:  # names profile fields only: the caller needs them to repair the pair
            self.fail(400, str(error))
        except (ValueError, TypeError):
            self.fail(400, 'Invalid settings. Select different, existing profiles and reload if needed.')
        except (OSError, subprocess.TimeoutExpired) as error:
            log_unavailable(f'{self.command} {path.path}', error)
            self.fail(503, 'Canvas discovery or settings unavailable. Check provider URL, credentials, and account login, then reload.')

    def do_POST(self):
        if not self.authorized():
            return
        if urlsplit(self.path).path != '/api/review-control/test-provider':
            self.fail(404, 'Not found')
            return
        try:
            candidate = self.json_body()
            if candidate is REPLIED:
                return
            probe_shape(candidate)
            self.reply(200, discovery('probe', request=candidate))
        except (ValueError, TypeError):
            self.fail(400, 'Invalid connection test')
        except (OSError, subprocess.TimeoutExpired) as error:
            # the control plane failed, not the provider: the user text stays, the reason goes to the log
            log_unavailable('connection test', error)
            self.reply(200, {'ok': False, 'message': 'Connection test unavailable or timed out. Try again.'})

    def do_GET(self):
        self.handle_settings()

    def do_PUT(self):
        self.handle_settings(write=True)


if __name__ == '__main__':
    HTTPServer(('127.0.0.1', int(os.environ.get('REVIEW_CONTROL_PORT', '8082'))), Handler).serve_forever()
