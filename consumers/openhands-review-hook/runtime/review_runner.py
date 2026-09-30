"""One review, optionally one quota/rate-limit fallback, with a fixed source head."""
import json
import os
import re
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from review_policy import read_settings, profile_llm_ref, settings_problems
from reasoning_profiles import block as reasoning_block


def next_page(link_header, base=None):
    """The URL of the `rel="next"` entry of an HTTP Link header, else None.
    GitHub pages issue comments at 30 by default and links the next page this
    way (absolute URL); Gitea does the same on its paged endpoints, while its
    per-issue comments endpoint returns every comment (1.26.4). With `base`,
    a link to another host is ignored: the caller sends its forge token along."""
    for part in (link_header or '').split(','):
        url, _, params = part.strip().partition(';')
        if url.startswith('<') and url.endswith('>') and 'rel="next"' in params.replace(' ', ''):
            url = url[1:-1]
            if base and urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(base).netloc:
                return None
            return url
    return None


def newest_first(events):
    """The events by timestamp, newest first: the server's TIMESTAMP_DESC order is
    not strict (observed on agent-canvas 1.20.0), and only the terminal error counts."""
    return sorted(events.get('items', []), key=lambda e: e.get('timestamp') or '', reverse=True)


def config_error(events):
    """The model or its endpoint rejected the request as configured (a bad request
    the platform classifies as a settings problem): an effort the model does not
    accept, a parameter the endpoint refuses. Returns the error code or None."""
    for event in newest_first(events):
        if event.get('kind') != 'ConversationErrorEvent':
            continue
        code = event.get('code')
        if code == 'LLMBadRequestError' or (event.get('classification') or {}).get('kind') == 'config':
            return code or 'config'
        return None  # events are newest first: only the terminal error counts, as for quota
    return None


def limit_error(events):
    """Recognize terminal quota/rate-limit events, not proof of exhausted credits.

    Only inspect server error events, never model output or repository text.
    """
    for event in newest_first(events):
        if event.get('kind') != 'ConversationErrorEvent':
            continue
        if event.get('code') in {'RateLimitError', 'LLMRateLimitError', 'QuotaExceededError'}:
            return True
        if event.get('code') == 'ACPPromptError':
            detail = event.get('detail', '')
            # ACP carries structured error data after its human-readable message.
            for match in re.finditer(r'\{[^{}]*\}', detail):
                try:
                    if json.loads(match.group()).get('errorKind') == 'rate_limit':
                        return True
                except (ValueError, AttributeError):
                    pass
        # Events are newest first. Do not retry an old, recovered quota error
        # when the terminal failure was something else.
        return False
    return False


def switched(conversation, deep_ref):
    """Whether the conversation has switched to the deep LLM profile. Observed on
    agent-canvas 1.20.0 (SDK 1.49): `agent.llm.usage_id` is `default` until the
    agent calls `switch_llm`, then `profile:<name>` of the profile switched to.
    A server without the field reads as switched, the older assumption."""
    usage = ((conversation.get('agent') or {}).get('llm') or {}).get('usage_id')
    return usage is None or usage == f'profile:{deep_ref}'


def valid_review(comment, marker, head, bot=None):
    if bot and comment.get('user', {}).get('login') != bot:
        return False
    body = comment.get('body', '')
    match = re.match(re.escape(marker) + r' reviewed at head ([0-9a-f]{7,40})\b', body)
    return bool(match and head.startswith(match[1]) and
                re.search(r'^VERDICT\s*\n(?:READY_FOR_HUMAN_MERGE|RETURN_TO_IMPLEMENTATION)\b', body, re.M))


def recovered_head(api, repo, num, marker, bot=None, window=2700):
    """After a service restart: the head of an open PR whose review comment for
    that head was posted within the watch window, else None.

    A run that was in flight started at most one watch window ago, so its
    comment, if any, lies inside that window; only the live PR state counts.
    """
    pr = api(f'/repos/{repo}/pulls/{num}') or {}
    if pr.get('state') != 'open':
        return None
    head = pr['head']['sha']
    since = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - window))
    comments = api(f'/repos/{repo}/issues/{num}/comments?since={since}') or []
    if not any(valid_review(c, marker, head, bot) for c in comments):
        return None
    # The PR may have moved while the comments were read: the review must still be for its head.
    pr = api(f'/repos/{repo}/pulls/{num}') or {}
    if pr.get('state') != 'open' or (pr.get('head') or {}).get('sha') != head:
        return None
    return head


class RunStore:
    """In-flight runs on disk, one JSON file per receiver, so a service restart
    re-attaches the watch to a conversation that is still running in Canvas
    instead of failing the request.

    A record is written once the conversation exists and removed when the run
    ends either way. The file is never a reason to fail a review: a write that
    fails is logged and the run goes on unrecorded (a restart then treats it
    as before: recovered if its comment is already posted, failed otherwise).
    The one exception is the fallback: it starts only after the previous
    attempt's record is gone (`clear` reports whether it is), so a restart can
    never resume the first attempt and start a second fallback.
    """

    REQUIRED = ('repo', 'num', 'label', 'profile', 'choices', 'head', 'since', 'attempt', 'conversation', 'started', 'deadline')

    def __init__(self, path, log=print):
        self.path, self.log = Path(path), log
        self.lock = threading.Lock()

    @staticmethod
    def key(repo, num):
        return f'{repo}#{num}'

    def _read(self):
        try:
            runs = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            self.log(f'run state unreadable, starting empty: {self.path.name}: {type(error).__name__}')
            return {}
        return runs if isinstance(runs, dict) else {}

    def _write(self, runs):
        """True when the runs are on disk; a failure is logged, never raised."""
        try:
            tmp = self.path.with_name(self.path.name + '.tmp')
            tmp.write_text(json.dumps(runs, indent=1, sort_keys=True))
            os.replace(tmp, self.path)  # atomic: a crash mid-write leaves the previous file
            return True
        except OSError as error:
            self.log(f'run state not saved: {self.path.name}: {type(error).__name__}')
            return False

    def load(self):
        """The recorded runs, oldest first. A record the receiver could not act
        on (fields missing: an edited file, another runtime's schema) is
        dropped with a log line rather than stopping the service at start."""
        with self.lock:
            runs = self._read()
            usable = {k: r for k, r in runs.items() if isinstance(r, dict) and all(f in r for f in self.REQUIRED)}
            if len(usable) != len(runs):
                self.log(f'run state: {len(runs) - len(usable)} unusable record(s) dropped from {self.path.name}')
                self._write(usable)
            return sorted(usable.values(), key=lambda r: r['started'])

    def save(self, record):
        with self.lock:
            runs = self._read()
            runs[self.key(record['repo'], record['num'])] = record
            return self._write(runs)

    def clear(self, repo, num):
        """True when no record of the run is left to resume."""
        with self.lock:
            runs = self._read()
            if runs.pop(self.key(repo, num), None) is None:
                return True
            return self._write(runs)


class Runner:
    def __init__(self, api, app_api, set_label, profile_info, fail, log=print,
                 marker='[hands-bot review]', bot=None, working='hands-reviewing',
                 done='hands-reviewed', timeout=2700, poll=30, sleep=time.sleep,
                 llm_ref=profile_llm_ref, problems=settings_problems, runs=None, note=None):
        self.api, self.app_api, self.set_label = api, app_api, set_label
        self.profile_info, self.fail, self.log = profile_info, fail, log
        self.runs = runs  # a RunStore, or None to keep nothing across restarts
        self.note = note  # note(repo, num, text): a PR comment that is not a review, or None
        self.llm_ref = llm_ref
        self.problems = problems
        self.marker, self.bot = marker, bot
        self.working, self.done = working, done
        self.timeout, self.poll, self.sleep = timeout, poll, sleep

    def run(self, repo, num, title, label, selected, prompt_file, workspaces, resume=None):
        """One review. `resume` is a record from the RunStore: the conversation
        it names is watched instead of a new one being started."""
        try:
            self._run(repo, num, title, label, selected, prompt_file, workspaces, resume)
        except Exception as error:
            # Provider/API errors may contain credentials or operator details.
            self.log(f'review failed: {repo}#{num}: {type(error).__name__}')
            self.fail(repo, num, 'review service could not complete the request; inspect the service locally')
        finally:
            if self.runs:
                self.runs.clear(repo, num)

    def resume(self, record, prompt_file, workspaces):
        self.run(record['repo'], record['num'], record.get('title', ''), record['label'],
                 record['profile'], prompt_file, workspaces, resume=record)

    def _complete(self, repo, num, head, profile, conv_id=None, switch=None):
        current = self.api(f'/repos/{repo}/pulls/{num}')
        if current.get('state') != 'open' or current['head']['sha'] != head:
            self.fail(repo, num, 'the review is stale because the pull request changed or closed')
            return
        self.set_label(repo, num, self.working, False)
        self.set_label(repo, num, self.done, True)
        self.log(f'review done: {repo}#{num} profile={profile}')
        if switch and conv_id:
            self._verify_switch(repo, num, profile, conv_id, switch)

    def _verify_switch(self, repo, num, profile, conv_id, switch):
        """Combined mode signs the review with the primary's model; say so on the
        PR when the conversation did not end on the primary's LLM profile. The
        review is posted and labelled by now: nothing here changes that outcome."""
        reading_ref, deep_ref = switch
        try:
            info = self.app_api(f'/api/conversations/{conv_id}')
            if switched(info, deep_ref):
                self.log(f'switch verified: {repo}#{num} conversation={conv_id} on `{deep_ref}`')
                return
            usage = ((info.get('agent') or {}).get('llm') or {}).get('usage_id')
            still_reading = usage in ('default', f'profile:{reading_ref}')
            where = f'the reading LLM profile `{reading_ref}`' if still_reading else f'LLM profile `{usage}`'
            self.log(f'review written off the primary: {repo}#{num} conversation={conv_id} on {where} (signed as {profile})')
            if self.note:
                self.note(repo, num, f'the review above was written on {where}, not on `{deep_ref}`: the agent never switched to it, '
                                     'although the signature names the primary model. Read it as a fast-reading pass; '
                                     're-add the label for another attempt.')
        except Exception as error:
            self.log(f'switch check incomplete: {repo}#{num} conversation={conv_id}: {type(error).__name__}')

    def _start(self, repo, num, label, selected):
        """Settings snapshot and PR state for a fresh run: (profile choices,
        reading profile, head, comment window start), or Nones after a failure
        that has been reported."""
        try:
            settings = read_settings()  # Snapshot for the entire run.
        except ValueError as error:
            self.log(f'settings unreadable: {repo}#{num}: {type(error).__name__}')
            self.fail(repo, num, 'the review settings file cannot be read; open Auto Reviews, choose profiles and save')
            return None, None, None, None
        default_request = label == os.environ.get('LABEL_REQUEST', 'review-this')
        if default_request:
            selected = settings['primary']
            problems = self.problems(settings)
            for role in ('primary', 'secondary'):  # a saved choice that cannot run is reported, never worked around
                if role in problems:
                    self.log(f'settings problem: {repo}#{num} {role}: {problems[role]}')
                    self.fail(repo, num, f'{problems[role]}; select or clear the {role} profile in Auto Reviews')
                    return None, None, None, None
            if 'fallback' in problems:
                self.log(f'settings problem: {repo}#{num} fallback: {problems["fallback"]}')
                settings = dict(settings, fallback=None)
        fallback = settings['fallback'] if selected == settings['primary'] else None
        if fallback == selected:
            fallback = None
        # Combined mode: the conversation starts on the reading profile and the
        # prompt tells the agent to switch to the deep one. Only the default
        # request uses it - an explicit label naming the primary does not - and
        # only between two LLM-profile (OpenHands-kind) agents.
        reading = settings.get('secondary') if default_request else None
        choices = [selected] + ([fallback] if fallback else [])
        pr = self.api(f'/repos/{repo}/pulls/{num}')
        if pr.get('state') != 'open':
            self.fail(repo, num, 'the pull request is no longer open')
            return None, None, None, None
        head = pr['head']['sha']
        since = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        return choices, reading, head, since

    def _run(self, repo, num, title, label, selected, prompt_file, workspaces, resume=None):
        if resume:
            # The choices, head and window were fixed when the run started; the
            # settings file is not re-read, so a save during the restart
            # cannot change a review that is already under way.
            choices, reading = list(resume['choices']), resume.get('reading')
            head, since = resume['head'], resume['since']
        else:
            choices, reading, head, since = self._start(repo, num, label, selected)
            if not choices:
                return
        template = Path(prompt_file).read_text()  # Changes apply without restart.
        initial_model = None
        for attempt, profile in enumerate(choices):
            if resume and attempt < resume['attempt']:
                continue  # already tried before the restart
            try:
                pid, model = self.profile_info(profile)
            except (ValueError, KeyError) as error:  # deleted or broken in Canvas: say so, change nothing
                self.log(f'profile unavailable: {repo}#{num} {profile}: {type(error).__name__}')
                role = 'the fallback profile' if attempt else 'reviewer profile'
                prefix = 'quota or rate limit reached; ' if attempt else ''
                self.fail(repo, num, f'{prefix}{role} `{profile}` is not available (deleted or broken in Canvas); '
                          'select an existing profile in Auto Reviews')
                return
            initial_model = initial_model or model
            start, switch = profile, None
            if reading and reading != profile and 'Reasoning profiles:' not in template:
                reading_ref, deep_ref = self.llm_ref(reading), self.llm_ref(profile)
                if reading_ref and deep_ref and reading_ref != deep_ref:
                    try:
                        pid, reading_model = self.profile_info(reading)
                        start, switch = reading, (reading_ref, deep_ref)
                    except Exception as error:  # a broken reading profile degrades to single-profile
                        self.log(f'reading profile unavailable, single-profile review: {repo}#{num} {reading}: {type(error).__name__}')
            message = template.format(num=num, repo=repo, title=title, marker=self.marker,
                                      profile=profile, label=label, model=model)
            message += (f'\nReview only source head {head}. Before posting, verify the PR is still open '
                        f'and its head is exactly {head}; if it changed, do not post a review. '
                        'Use this full head in the review marker.\n')
            if switch:
                message += reasoning_block(*switch)
            if attempt:
                message += (f'This is the single configured quota/rate-limit fallback: `{initial_model}` '
                            f'ended with a confirmed quota or rate-limit error; you are `{model}`. Start a fresh review '
                            'and add that factual fallback note before the final signature. '
                            'The request label names the original request, not a second trigger.\n')
            current = self.api(f'/repos/{repo}/pulls/{num}')
            if current.get('state') != 'open' or current['head']['sha'] != head:
                self.fail(repo, num, 'the pull request changed or closed; request a fresh review')
                return
            if resume and attempt == resume['attempt']:
                conv_id, ends = resume['conversation'], resume['deadline']
                self.log(f'review resumed: {repo}#{num} head={head} profile={profile} attempt={attempt + 1} conversation={conv_id}')
            else:
                if attempt and self.runs and not self.runs.clear(repo, num):
                    # The previous attempt's record must not outlive it: a restart would
                    # resume the old conversation, see its quota error again and start a
                    # second fallback. When it cannot be removed, no fallback starts here:
                    # at most one fallback conversation ever exists for the request.
                    self.fail(repo, num, 'quota or rate limit reached; the fallback was not started because the run '
                              'state could not be updated (check the receiver\'s run state file), then re-add the label')
                    return
                workdir = f'{workspaces}/{repo.replace("/", "-")}-{num}-{uuid.uuid4().hex}'
                conv = self.app_api('/api/conversations', {
                    'agent_profile_id': pid,
                    'workspace': {'kind': 'LocalWorkspace', 'working_dir': workdir},
                    'initial_message': {'content': [{'text': message}]},
                })
                conv_id, started = conv['id'], time.time()
                ends = started + self.timeout  # the attempt's deadline, fixed here: a restart keeps it whatever WATCH_MINUTES says then
                self.log(f'review attempt: {repo}#{num} head={head} profile={profile}'
                         + (f' start={start} agent-settings={start}' if switch else '') + f' attempt={attempt + 1} conversation={conv_id}')
                if self.runs:
                    self.runs.save({'repo': repo, 'num': num, 'title': title, 'label': label, 'profile': profile,
                                    'choices': choices, 'reading': reading, 'head': head, 'since': since,
                                    'attempt': attempt, 'conversation': conv_id, 'started': started, 'deadline': ends})
            deadline = time.monotonic() + max(0.0, ends - time.time())
            while time.monotonic() < deadline:
                self.sleep(self.poll)
                comments = self.api(f'/repos/{repo}/issues/{num}/comments?since={since}')
                if any(valid_review(c, self.marker, head, self.bot) for c in comments or []):
                    self._complete(repo, num, head, profile, conv_id, switch)
                    return
                info = self.app_api(f'/api/conversations/{conv_id}')
                state = info.get('execution_status')
                if state == 'finished':
                    # The agent posts, then finishes: one fresh look at the comments before
                    # calling it a miss, so the next iteration can complete normally.
                    comments = self.api(f'/repos/{repo}/issues/{num}/comments?since={since}')
                    if any(valid_review(c, self.marker, head, self.bot) for c in comments or []):
                        self._complete(repo, num, head, profile, conv_id, switch)  # not another iteration: the deadline may have passed
                        return
                    self.log(f'review finished without posting: {repo}#{num} conversation={conv_id}')
                    self.fail(repo, num, f'the review conversation finished without posting a review (conversation {conv_id}); '
                              'inspect it in Canvas, then re-add the label')
                    return
                if state != 'error':
                    continue
                # No server-side kind filter: agent-canvas 1.20.0 answers `kind=` with nothing and
                # ignores `kind__eq=`; limit_error/config_error pick the error events themselves.
                events = self.app_api(f'/api/conversations/{conv_id}/events/search?sort_order=TIMESTAMP_DESC&limit=100')
                limited = limit_error(events)
                if limited and attempt + 1 < len(choices):
                    if switch and not switched(info, switch[1]):
                        # The limit hit the reading profile, before any switch: the fallback
                        # starts plainly on its own profile and the note names the reading model.
                        self.log(f'review quota/rate-limit on the reading profile {reading}: {repo}#{num} fallback runs single-profile')
                        reading, initial_model = None, reading_model
                    self.log(f'review quota/rate-limit fallback: {repo}#{num} {profile} -> {choices[attempt + 1]}')
                    break
                rejected = None if limited else config_error(events)
                reason = ('quota or rate limit reached' if limited
                          else f'the model rejected the request as configured (code {rejected}); check the profile model and effort in Auto Reviews, effort support is model-specific' if rejected
                          else 'the review conversation entered error state')
                suffix = '; the single fallback also failed' if attempt else '; no fallback applies'
                self.fail(repo, num, reason + suffix)
                return
            else:
                self.fail(repo, num, f'no review posted within {self.timeout // 60} minutes; no automatic retry')
                return
