---
name: custom-codereview-guide
description: Repository-specific review context for aiskills, including runtime contracts observed on the reference hands site, so reviews do not re-derive or re-open them.
triggers: [/oh-codereview]
---

# Review guide for aiskills

Read this on top of the `oh-code-review` rules. It records facts a review cannot
see in the source, with the date and the way they were observed, and the
repository conventions that decide dispositions here.

## Conventions

- The review gates and their levels are defined in `agents/review-gates.md`
  (the managed block). A local pre-merge review is one pass at `high`; the
  external reviewer carries the pre-merge weight.
- Findings against `consumers/openhands-review-hook/runtime/` that depend on
  the Canvas server's behaviour are runtime hypotheses until observed at the
  API (below). Reproducers are read-only calls to the local Canvas API from
  the host, with the session key from the service environment, never printed.
- Nothing internal (private hostnames, hub or backend names, credential paths)
  goes into a review comment.
- The 7-day rule for new dependency versions (`oh-code-review`, supply-chain
  check) applies to what ships to users: code the installers copy and the
  Python the review-hook consumer runs. It does not apply to the agent clients
  that `.github/check-clients.py` pins or that its weekly run takes at
  `@latest`: they are the subjects of that check, installed only on a
  disposable CI runner with read-only permissions and no model credentials,
  and checking current releases is the job's purpose. A fresh client pin is
  not a finding. (Owner decision, 2026-09-27, on the 1.23.0 pull request.)

## Observed runtime contracts (reference site)

Canvas image agent-canvas 1.20.0, SDK 1.49.1, observed 2026-09-24 through the
local Canvas HTTP API:

- `GET /api/conversations/{id}/events/search`: `limit` is at most 100 (422
  above). The `kind=<EventKind>` filter returns an empty page for every kind
  tried (`ActionEvent`, `ConversationStateUpdateEvent`, `ConversationErrorEvent`),
  and `kind__eq=` is ignored (all kinds come back). `sort_order=TIMESTAMP_DESC`
  is newest first overall but not strictly ordered. The runner therefore reads
  unfiltered, newest first, limit 100, and selects error events by timestamp.
- `GET /api/conversations/{id}`: `agent.llm.usage_id` is `default` for every
  conversation until an LLM-profile (OpenHands-kind) agent calls `switch_llm`,
  after which it is `profile:<name>` of the profile switched to (observed with
  one switching and one non-switching conversation on the reading profile,
  2026-09-24); an ACP conversation stays `default`. The value therefore
  answers "has it switched to X", never "is it still on Y". `current_model_id`
  is the bare model name and does not distinguish two profiles of one model.
- `switch_llm` appears as an `ActionEvent` with `tool_name: switch_llm` and
  `action: {profile_name, reason}`, followed by an `ObservationEvent` of the
  same `tool_name` and `tool_call_id`, plus a `ConversationStateUpdateEvent`
  with `key: agent` carrying the new `llm`.
- An ACP-agent conversation may carry one `LLMAuthenticationError`
  `ConversationErrorEvent` at its start (the default LLM has no key) and still
  finish normally; only the terminal error of a conversation in `error` state
  is meaningful.
- The receivers run as root on the reference site; the hooks directory is
  writable, so the run records of 1.18.0 are written there.

Auto Reviews discovery helper and app, observed 2026-10-01 on agent-canvas
1.20.0 (openhands-agent-server 1.27.1, SDK 1.49.1) with aiskills 1.29.2
deployed (every runtime file and app 0.2.7 equal to the release by `cmp`),
from read-only checks inside the Canvas container and through the
review-control API. These re-check the contracts first noted on 2026-09-14
(agent-canvas 1.17.0) in issue #11:

- Two `openhands-agent-server` processes run, both with cwd `/`, both with
  `OH_SECRET_KEY` and `OH_PERSISTENCE_DIR` set to identical values, and
  without `OPENHANDS_AGENT_SERVER_CONFIG_PATH`. `server_environment()`
  therefore finds one unique configuration; two processes is normal, not a
  sign of a stale server.
- The SDK has no config file and no default config path any more:
  `get_default_config()` is built from `OH_*` environment variables only
  (`from_env(Config, "OH")`), and no installed `openhands` module mentions
  `OPENHANDS_AGENT_SERVER_CONFIG_PATH` or `openhands_agent_server_config`. The
  helper's literal `workspace/openhands_agent_server_config.json` (and the
  variable it reads) has no SDK counterpart now; it is harmless, since the
  helper only exports the computed path and takes the cipher from
  `get_default_config().cipher`, i.e. from `OH_SECRET_KEY`. On 1.17.0 the
  literal equalled the SDK default.
- `LLMProfileStore.load(name, cipher=None)` returns the Fernet ciphertext as
  the profile's `api_key`: a `SecretStr` that is truthy, whose value is
  non-empty, starts with `gAAAAA`, and differs from the value loaded with the
  server cipher. A truthy `api_key` from a `cipher=None` load is therefore no
  evidence of a usable key.
- Every stored provider connection has a `base_url` (2 of 2, provider
  `custom`). Code may still treat a missing one as possible (OpenAI's default
  endpoint), but no reviewer should report it as observed.
- The "401 with a body" race does not reproduce: 140 sequential
  `POST /api/review-control/test-provider` calls for one saved connection all
  answered `ok: true`, with no HTTP error and no transport error (also not
  reproduced in 140 runs on 2026-09-14).
- Canvas's app request helper `host.agentServer.request()` **throws** on an
  error status; it does not resolve with the body. The error's `name` is
  `HttpError` (its constructor name is minified), with `status`, an empty
  `statusText`, `message` `HTTP request failed (<status> ): <raw body>`, and
  `response` holding the parsed JSON body. It has no `statusCode`, `body`,
  `data` or `cause`. Seen on two 404s: the control service's
  `{"ok": false, "message": "Not found"}` and the agent server's
  `{"error": "Not found"}`. The app's `testProvider()` (1.27.3) handles both
  resolving and throwing, so this contract confirms it rather than changing
  it; an app that wants the server's own text on an error status reads
  `error.response.message`.

Forge pagination, observed credential-free on 2026-09-24:

- GitHub `GET /repos/{owner}/{repo}/issues/{n}/comments?since=...` returns
  30 comments per page by default (100 with `per_page=100`) and a `Link`
  header whose `rel="next"` entry is an absolute URL under
  `api.github.com/repositories/<id>/...`; the receivers follow it with the
  same headers.
- Gitea 1.26.4 `GET /repos/{owner}/{repo}/issues/{index}/comments` takes only
  `since` and `before` (no `page`/`limit`; the swagger says so and `limit=2`
  is ignored) and returns every comment with `X-Total-Count`; the repo-wide
  `/issues/comments` endpoint is the paged one. No pagination applies to the
  per-issue reads the receivers make.
