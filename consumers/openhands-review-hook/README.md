# Consumer: OpenHands label-driven review hook (hands)

A `hands` deployment (the examples here are site A and site B) runs an
`openhands-review-hook` receiver: a `review-this` label starts an OpenHands conversation from a **prompt
file**, and the receiver waits for the posted `[<bot> review]` comment. That
prompt is the one piece of a hands site that must change when this skill set
changes how reviews are done, so it lives here as **one universal template**,
rendered per site from a small variables file.

The optional **Auto Reviews** Canvas App and shared fallback runtime now live
here too: see [AUTO-REVIEWS.md](AUTO-REVIEWS.md). `runtime/` contains portable
receiver adapters and services; credentials, environment files and saved
selections remain site-owned. Existing site forks must deliberately adopt the
runtime before its hot-reload and quota-fallback behavior applies.

## Files

| File | What |
| --- | --- |
| `review_prompt.template.txt` | the universal prompt. Site-neutral review steps 3–4 (frozen scope, "use the skill's output format") plus plumbing steps that reference site variables. |
| `sites/<site>.json` | the six site variables: `FORGE_URL`, `HANDS_URL`, `SKILL_SOURCE`, `CLONE_AND_FETCH`, `POST_COMMAND`, `REASONING_PROFILES`. Multi-line values allowed; an empty value drops the line that carries the marker, so a variable can be an optional block. |
| `render.py` | `render.py sites/<site>.json > review_prompt.txt` fills `@@VAR@@` at install time; `render.py --check` renders every site and validates. |

Two placeholder namespaces, deliberately different:

- `@@VAR@@` — site variables, replaced by `render.py` when the prompt is installed.

`REASONING_PROFILES` is the optional block for sites whose agent can change its
LLM mid-conversation (OpenHands SDK: the built-in `switch_llm` tool, enabled per
agent profile with `enable_switch_llm_tool`). The universal contract it encodes:
gather material (clone, diff, read the changed files) on a cheap/fast profile,
then switch to the deep profile BEFORE forming the frozen scope or writing any
finding, so the review pass, the verification stage and the write-up all run
deep, and never switch back. The first version switched only before the
verification stage; the agent then did the pass and its verification on the
fast profile and used the deep one for six trivial calls (writing and posting
the file, 0 reasoning tokens) — the switch point must precede the thinking,
not just the writing. Each site names its
own two saved profiles in the value (site B: `astra-high` → `astra` since 2026-09-21, before that `56sol-high` → `56sol`; measured
2026-09-20 on the same 13-file PR: 85 min at max, 7 min at high, same findings
and verdict). Sites without the tool (ACP Claude Code, single-model hands) set
the value to `""` and the line disappears. A site whose reviewer profiles
come from the Auto Reviews app (site A) also leaves the value empty: its
runtime appends the same text to each default-label request when a secondary reading profile
is configured, filling in the two generated LLM profile names
(`runtime/reasoning_profiles.py` holds the canonical wording; the site B
value is `block('astra-high', 'astra')`).
- `{num} {repo} {title!r} {marker} {model} {label}` — runtime placeholders the
  receiver fills with `PROMPT.format(...)` on every trigger. The template uses
  exactly this set; site values may use them too (e.g. `{repo}` inside a clone
  command). A receiver that does not pass one of them fails with a KeyError at
  trigger time — the site B fork passes `label` since 2026-09-13 for this reason.

The posted comment must START with `{marker} reviewed at head <sha>`, with the
head's full 40-digit SHA; the receiver counts the review only when that line
names the head by its full SHA, and `verify-delivery` carries a review of an older head over a
base-only update on GitHub and Gitea only by its full SHA. Nothing else in the prompt is load-bearing for it.

## Install / update on a site

Operator checklist per release, including the gates block and verification:
`UPDATING.md` in this directory.

```sh
git clone https://github.com/BlackVS/aiskills.git        # or update the local copy
python3 aiskills/consumers/openhands-review-hook/render.py \
        <path to the site's own site file> > <PROMPT_FILE>
```

The new `runtime/` receiver reads the prompt file per trigger; no restart.
Legacy receivers that load `PROMPT` at module startup need an idle restart
after re-rendering. Verify with one
`review-this` on a small PR: the comment starts with the marker line, then the
skill's first section, and ends with a verdict the skill defines. A site whose
plumbing differs (skill read from a local copy, token passed per command, a
different tea/gh syntax) changes only its site file, never the template.

The files in `sites/` are neutral examples of the two plumbing styles in use
(`site-a-*`: credentials preconfigured on the host, one file per forge;
`site-b-*`: the skill read from a local copy and the token passed per
command). A real deployment keeps its own site file, with its hosts, bot
account and token variable, in its private configuration and renders from
that path; `render.py --check` validates the examples here.

## LLM profile model strings

Point saved LLM profiles at a LiteLLM proxy with `litellm_proxy/<model>`. The
`openai/` prefix makes litellm treat the proxy as OpenAI and, for model names
in litellm's public cost map, reroute tool-bearing reasoning requests to the
Responses API, which most proxied upstreams do not serve (details and the
2026-09-21 incident in UPDATING.md).

## Changing review behavior

1. Change the skill (`skills/oh-code-review/…`). The template names no output
   sections and defers to `references/dispositions.md`, so format changes need
   nothing here.
2. If the *process* a hook must impose changes (what step 3 or 4 says), edit the
   template once; every site inherits it on its next render.
3. `python3 consumers/openhands-review-hook/render.py --check`, then bump
   VERSION/CHANGELOG. Sites re-render on their next update.
