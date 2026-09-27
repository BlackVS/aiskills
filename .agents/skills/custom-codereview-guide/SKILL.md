---
name: custom-codereview-guide
description: Repository-specific review context for ai-skills, including runtime contracts observed on the reference hands site, so reviews do not re-derive or re-open them.
triggers: [/oh-codereview]
---

# Review guide for ai-skills

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
