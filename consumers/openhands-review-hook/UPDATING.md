# Updating a hands site after an aiskills release

Operator checklist for whoever runs a `hands` review deployment (site A,
site B, or a new site). The receiver code never changes for a skill release;
only the rendered prompt does, and sometimes the agent-facing gates block.

## Every release

```sh
cd aiskills && git pull                       # or refresh the local copy on hosts that cannot reach the repo
python3 consumers/openhands-review-hook/render.py --check          # every example site must print OK
python3 consumers/openhands-review-hook/render.py \
        <site file> > <PROMPT_FILE>   # per prompt the receiver uses
```

- `<PROMPT_FILE>` is the receiver's `PROMPT_FILE` (site A: `review_prompt.txt`
  and, for the GitHub poller, `github_review_prompt.txt`; site B:
  `/opt/openhands/hooks/review_prompt.txt`). The new `runtime/` receiver reads
  it per trigger; no restart. Legacy receivers load it at module startup:
  restart those services while idle after replacing their prompt files.
- Hosts that read the skill from a local copy (site B) also refresh that copy
  (`skills/oh-code-review`, `skills/oh-technical-writing`).
- Re-apply the review-gates managed block wherever the site's agents read it:
  `./install.sh --user --agents-md` (or `--agents-md <repo>`). It tells the agent
  that *receives* a review what the verdict and dispositions mean.
- Verify with one `review-this` on a small PR: the comment starts with the
  marker line, then the skill's first section, and ends with a verdict the skill
  defines. Watch the receiver journal for `review done`.

## If the site's plumbing changes

tea/gh syntax, credential model, where the skill is read from, forge or hands
URL: edit the site's own site file (start from the closest example in `sites/`;
all six variables are documented in README.md), run `--check`, re-render. Never edit the template for a site
difference, and never hand-edit a rendered prompt — the next render overwrites it.

## Runtime code and the Canvas app

`runtime/` (the receiver modules and unit files) and `auto-reviews/` are
deployed **from this repository** and never edited in place on a site.
`/opt/openhands/hooks` is not a checkout: an in-place fix has no commit, and
the next deploy from the repo silently reverts it — 1.11.0 exists because
exactly that happened. To change receiver or app code: branch here, run
`python3 -m unittest test_review_control test_adapters test_discovery
test_canvas_integration` from `runtime/` (the integration cases run only
where the SDK is installed, inside the Canvas container or a virtual
environment with the image's SDK versions, with `OH_PERSISTENCE_DIR` set to a
fresh `/tmp/auto-reviews-test-*/state` and a test-only `OH_SECRET_KEY`, as
AUTO-REVIEWS.md "Verify" describes — elsewhere they skip, so a green run
without the SDK has not exercised the SDK store) and the browser fixture in
`tests/test_extension.html`, review, merge, then deploy.

Deploying: back up the current files to
`/opt/openhands/backups/<change>-<UTC timestamp>/`; copy the modules **as
bytes** (`git archive`, `tar`, `scp` — not a Windows text tool: a PowerShell
`Set-Content`/`Out-File` copy turned `⚠️` into `??????` on one site); restart
only the affected idle service, by what each one loads at start:
`review-hook` and `github-review-poller` import their own module plus
`review_policy.py` and `review_runner.py`, so a change to any of those four
needs the matching receiver restarted; `review-control` imports
`review_control.py` and `review_policy.py`; `canvas_discovery.py` is re-read
from disk on every request and the rendered prompt per trigger, so neither
needs a restart. Re-install and enable the app from the repo path. Then verify
per file, not per directory: `cmp` each of the seven runtime modules and the
three unit files against the site's copies, and the two app files against the
installed app. Site-only files in the hooks directory (env files, rendered
prompts, `review-settings.json`) and repo-only files in `runtime/` (tests) are
expected and are not drift; a differing module is.

## Release notes for operators

| Release | What a site must do |
| --- | --- |
| 1.31.6 | `review-control` reads the rest of a refused request body (at most 64 KiB or 1 s) before closing, so a client never loses the refusal to a connection reset. Deploy `review_control.py` and restart `review-control` while idle. |
| 1.31.5 | At the watch deadline, a review hidden by a forge outage is looked for once more instead of being reported as missing, and a request never gets a second failure comment. Deploy `review_runner.py`, restart `review-hook` and `github-review-poller` (safe mid-run since 1.18.0). |
| 1.31.3 | A review that is on the PR is completed at the next poll when the label swap after it hits a forge outage, instead of being reported as failed. Deploy `review_runner.py`, restart `review-hook` and `github-review-poller` (safe mid-run since 1.18.0). |
| 1.31.2 | Receivers survive a brief forge or Canvas outage. A transient read error while a review runs is retried at the next poll instead of failing the review, and a failure while reporting a failure is logged instead of killing the review thread. Forge reads (GET) are tried up to three times. Deploy `review_runner.py`, `review_hook.py` and `github_review_poller.py`, then restart `review-hook` and `github-review-poller` (safe mid-run since 1.18.0). A PR left on `hands-reviewing` by the old behaviour is not repaired by this change: swap its label by hand, or restart the receiver while its review comment is inside the watch window. |
| 1.31.1 | Tests and documentation only, plus the `REVIEW_AUTHOR` entry in `github_review_poller.py`'s docstring (set it when the token has no user, as with a GitHub App installation token). Deploy `github_review_poller.py` so the per-file `cmp` stays clean; no restart is needed, nothing runs differently. |
| 1.31.0 | Gitea receiver: a plain `review-this` on an unchanged patch now gets the "previous verdict stands" note too, on Gitea 1.27 or later (an older Gitea keeps reviewing every request). Both receivers trust only the reviewer's own review comments for it: `BOT_NAME` on Gitea; on GitHub the new optional `REVIEW_AUTHOR`, else the token's owner (read once from `GET /user`). If the GitHub review comments are posted by an account other than the poller token's owner, or the token has no user (a GitHub App installation token, for which `GET /user` answers 403), set `REVIEW_AUTHOR` to the posting login in the poller's environment file, or every request is reviewed in full. Deploy `review_runner.py`, `review_hook.py` and `github_review_poller.py`, restart `review-hook` and `github-review-poller` (safe mid-run since 1.18.0). |
| 1.30.0 | GitHub poller: a plain `review-this` on an unchanged patch (same patch identity as the newest review's head) gets a "previous verdict stands" note instead of a new review. Deploy `review_runner.py` and `github_review_poller.py`, restart `github-review-poller` and `review-hook` (both load the runner; safe mid-run since 1.18.0). The Gitea receiver's behaviour is unchanged. |
| 1.29.8 | Runner: an unreadable run-state file is no longer read as "no record" when the previous attempt's record is cleared, so a transient read failure cannot lead to a second fallback; the fallback is not started and the review fails with the existing run-state message. Deploy `review_runner.py`, restart `review-hook` and `github-review-poller` (safe mid-run since 1.18.0). |
| 1.29.7 | Auto Reviews app 0.2.10: switching the primary to an account agent clears a deleted secondary's warning along with the secondary, so Save is no longer stuck. Re-install and enable app 0.2.10; no runtime file changes, no restart. |
| 1.29.5 | A save that needs a new generated profile in a full Canvas profile store (50 agent or 50 LLM profiles) is refused before anything is written, with the counts; app 0.2.9 shows that text. Deploy `canvas_discovery.py` and `review_control.py`, restart `review-control` while idle, re-install and enable app 0.2.9; either order works (an older part shows the generic failure). If saves are refused, delete unselected `review-...` profiles in Canvas. |
| 1.29.4 | Discovery helper drops the unused config-path variable; Auto Reviews app 0.2.8 shows the server's own message when a connection test is refused. Deploy `canvas_discovery.py` (re-read on every request, no restart) and re-install and enable app 0.2.8; either order works. |
| 1.29.2 | Auto Reviews app 0.2.7: a refactor of the provider list and editor with no change in behavior. Re-install and enable the app from the repo path (no service restart) to keep the installed copy equal to the repository. |
| 1.29.1 | Inventory rows carry `editable` and `connection_id`, and Auto Reviews app 0.2.6 uses them instead of parsing provider ids. Deploy `canvas_discovery.py` first (re-read on every request, no restart), then re-install and enable app 0.2.6: on an older helper the new app would show no Edit button. The old app keeps working with the new helper. |
| 1.27.6 | Auto Reviews app 0.2.5: the model list tested when adding or editing a provider is kept, even when Canvas stores its URL in another form. Re-install and enable the app from the repo path (no service restart). |
| 1.27.4 | Settings API logs why the discovery helper failed (one stderr line, no secrets) and no longer answers helper output that is not JSON as an invalid request. Deploy `review_control.py`, restart `review-control` while idle. |
| 1.27.3 | Connection test answers in one shape: deploy `review_control.py` and restart `review-control` while idle; re-install and enable Auto Reviews app 0.2.4. Either order works: the new app reads both shapes, and the old app shows the new error messages where it showed nothing before. |
| 1.27.2 | Auto Reviews app 0.2.3: a provider token of spaces only is refused in the form instead of being sent as no token (add) or keeping the saved token (edit). Re-install and enable the app from the repo path (no service restart). |
| 1.26.7 | Backend refactor, no behavior change: the connection-test request shapes are listed once in the discovery helper and the settings API imports them. Deploy `canvas_discovery.py` and `review_control.py` together (the new `review_control.py` imports `probe_shape` from the helper, so it does not start beside an older helper), then restart `review-control` while idle. |
| 1.26.6 | Discovery helper refactor, no behavior change: the entry point is now `main(request, proc_root)`, so a test covers putting the agent server's key in place before an action and failing closed without one. Deploy `canvas_discovery.py` (re-read on every request, no restart) to keep the per-file `cmp` check clean. |
| 1.26.5 | Discovery helper: the account agents (ACP) it starts to list models no longer inherit `OH_SECRET_KEY`, which the helper copies in from the agent server. Deploy `canvas_discovery.py` (re-read on every request, no restart). |
| 1.26.4 | Auto Reviews app 0.2.2: saving a provider edit no longer discards reviewer choices that were not saved yet; they stay selected and marked unsaved. Re-install and enable the app from the repo path (no service restart). |
| 1.26.3 | Runner fixes: restart recovery re-reads the PR head after finding the review comment, and the quota fallback starts only after the previous attempt's run record is removed (otherwise the run fails with that reason, so no second fallback is possible after a restart). Deploy `review_runner.py`, restart `review-hook` and `github-review-poller` (safe mid-run since 1.18.0). |
| 1.26.2 | Security fix in the discovery helper: when a provider connection is edited, its saved token is now tested only against the saved endpoint's own origin (scheme, host, port). A base URL for another host needs its token entered again; before, the saved token was sent to whatever URL was typed. Deploy `canvas_discovery.py` (re-read on every request, no restart). |
| 1.22.0 | Receivers no longer default the forge and credentials: set `GITEA_API`, `GITEA_TOKEN_FILE` and `REVIEW_ORG` (Gitea receiver) and `GITHUB_TOKEN_FILE` (GitHub poller) in `review.env` before deploying `review_hook.py` / `github_review_poller.py`; a missing one stops the service at start. Render from the site's own site file (the files in `sites/` are examples), and point the skill source at `https://github.com/BlackVS/aiskills`. |
| 1.7.0 | Review model changed to dispositions; the old per-site prompts named the removed output format. Re-render (1.9.0 template) — a 1.6-era prompt contradicts the skill. |
| 1.9.0 | Prompts moved into the skill set as one template + site files. From here on: re-render per release, as above. The site B receiver passes `{label}`; site A already did. |
| 1.12.0 | Template gains the optional `REASONING_PROFILES` block (see README): every site file must define it (`""` to omit). Sites that use it also set `enable_switch_llm_tool: true` on the reviewer agent profile and keep both named LLM profiles saved. Re-render and restart the receiver (it reads the prompt at start). |
| 1.13.0 | Runtime: reasoning effort per reviewer profile and the optional secondary reading profile (combined mode) for sites on the Auto Reviews app. Deploy all seven runtime modules (the new one is `reasoning_profiles.py`, imported by the runner), restart `review-hook`, `github-review-poller` and `review-control` while idle, re-install and enable app 0.2.0. An old settings file reads as `secondary: null` (single-profile, unchanged behavior); the key is written only once a secondary is set, and a pre-1.13 runtime rejects it — clear the secondary before rolling the runtime back. Sites with hand-saved profiles keep their static `REASONING_PROFILES` value. |
| 1.21.0 | Combined mode: post-hoc switch check with a PR note, and the reading/primary pair must match beyond the generated fields. Deploy `review_runner.py`, `review_policy.py`, `review_hook.py`, `github_review_poller.py`; restart both receivers (safe mid-run) and `review-control` (it validates saves). A site with a hand-made pair whose agent settings differ sees the secondary reported as a problem in Auto Reviews and the default request failing with that reason until the pair matches or the secondary is cleared. |
| 1.20.1 | Receivers follow comment pagination (GitHub pages at 30). Deploy `review_runner.py`, `review_hook.py`, `github_review_poller.py`, restart both receivers (safe mid-run since 1.18.0). |
| 1.19.0 | The SDK patch tool is in the repository (`runtime/sdk_patch_files.py`); no service change. Sites that kept a local copy next to the receivers replace it with the repository file (same command line) and confirm `--check` on their current patch directory reports both files already patched. |
| 1.18.1 | Runner fixes only: error events are read without the server-side kind filter that agent-canvas 1.20.0 answers empty (the quota fallback and the config-error reason were inert on that image), a limit that hits the reading profile falls back single-profile, and the previous attempt's run record is cleared before the fallback starts. Deploy `review_runner.py`, restart `review-hook` and `github-review-poller` (safe mid-run since 1.18.0). |
| 1.18.0 | Receivers resume in-progress reviews across a restart. Deploy `review_runner.py`, `review_hook.py`, `github_review_poller.py`; this last restart still follows the idle rule (a run started by the old runtime has no record), after it the rule applies to the Canvas container only. The receivers write `review-runs-gitea.json` / `review-runs-github.json` under `REVIEW_RUNS_DIR` (default `/opt/openhands/hooks`, must be writable by the services; set the variable in `review.env` to move it). Verify: trigger one review, `systemctl restart review-hook` while it runs, watch the journal for `review resume` and `review resumed`, then `review done`. Verified on the reference site on 2026-09-24 with a restart of `review-hook` during a running review. |
| 1.14.9 | Discovery helper: works on SDK 1.49 (agent-canvas 1.19+), where the agent server no longer exports the profile and connection store accessors; the helper constructs the stores under the server's persistence directory itself. Deploy `canvas_discovery.py`, restart `review-control` while idle. Prerequisite for moving the Canvas image from 1.17.0 to 1.20.0 (rehearsed on the site A host: profiles, connections, discovery, app endpoints and a tool call all pass; the SDK patch script recognizes the 1.20.0 files). |
| 1.14.8 | Receivers: restart recovery completes a run whose review comment is already posted for the current head (labels swapped, no failure note) and fails only runs still in progress. Deploy `review_runner.py`, `review_hook.py`, `github_review_poller.py`; restart `review-hook` and `github-review-poller` only when the journal shows no `review attempt` without a later `review done`/`review failed` and no PR carries `hands-reviewing`. |
| 1.14.7 | Runner: a request the model or endpoint rejects as configured (an unsupported effort, a refused parameter) fails the review with that reason instead of the generic error-state message. Deploy `review_runner.py`, restart `review-hook` and `github-review-poller` while idle. |
| 1.14.6 | Runner: a review conversation that finishes without posting fails the request at once, naming the conversation. Deploy `review_runner.py`, restart `review-hook` and `github-review-poller` while idle. |
| 1.14.5 | Generated LLM profiles on a custom endpoint also carry the reasoning effort in litellm's extra body (`litellm_extra_body`): the proxy provider drops the plain field and site A's upstream refused every tool call without an effort (observed 2026-09-21, gpt-6-astra). Deploy `canvas_discovery.py`, restart `review-control` while idle; a 1.14.4 variant without the body is not reused, so re-save once and delete it. Hand-made proxied profiles need the same extra-body entry by hand if their effort matters. |
| 1.14.4 | Generated LLM profiles address custom endpoints as `litellm_proxy/<model>` (OpenAI itself stays `openai/`). Deploy `canvas_discovery.py`, restart `review-control` while idle. A variant saved earlier under `openai/` on a custom endpoint is never reused: re-save each affected selection once (a suffixed variant is built) and delete the old variant in Canvas. Hand-made profiles are untouched; re-point them by hand per the note above. |
| 1.14.0 | Generated reviewer profiles get readable names (`review-<model>[-<effort>][-reading]`); settings that name a deleted or broken profile load with a warning instead of failing. Deploy `canvas_discovery.py`, `review_policy.py`, `review_control.py` and `review_runner.py`, restart the three services while idle, re-install app 0.2.1; existing `auto-review-<hash>` profiles stay in use unchanged. |
| 1.13.2 | Service units only: re-install the three unit files (they gain `Environment=PYTHONUNBUFFERED=1`), `systemctl daemon-reload`, restart the services while idle. No module, prompt or app change. |
| 1.11.0 | Receiver runtime and Canvas app code now live here as the source of truth. Deploy app 0.1.8 plus `review_control.py`/`canvas_discovery.py` where not already live, then `cmp` every module and app file against the repo as above. New precondition: `canvas_discovery.py` now bootstraps the cipher from the running agent server for every action and fails closed, so before deploying confirm the Canvas container runs the server as a process named `openhands-agent-server` with `OH_SECRET_KEY` in its environment (read-only: list `/proc/*/cmdline` inside the container and count matches); a site that starts the server differently must not deploy this file until the helper is adapted. |

Site-specific facts learned the hard way, encoded in the site files so they are
not relearned per run: tea 0.15 takes `--login`/`--repo` as subcommand options;
a host without a git credential helper must pass the token per command or a bare
fetch hangs on a username prompt; the agent must be given the model id rather
than asked to discover it.

**Upgrading the Canvas image.** The runtime talks to the SDK inside the
image through the discovery helper and to the server's HTTP API; both have
moved between releases (SDK 1.49 dropped the agent server's profile-store
accessors, see 1.14.9). A site that mounts patched SDK files over the image
(the tool-message fixes) must regenerate them per image. Procedure, rehearsed
for agent-canvas 1.17.0 to 1.20.0 on 2026-09-21:

1. Pull the new image, extract `openhands/sdk/llm/llm.py` and `message.py`
   from it (`docker run --rm --entrypoint cat <image> <site-packages path>/openhands/sdk/llm/llm.py > llm.py`,
   the same for `message.py`) and run the repository's patch tool on the
   copies: `python3 consumers/openhands-review-hook/runtime/sdk_patch_files.py <dir>`
   (`--check <dir>` only reports). It exits non-zero on an unrecognized shape:
   stop, adapt `PATCHES` and the excerpts in `test_sdk_patch.py` together.
2. Start a throwaway container from the new image on a copy of the state
   directory, unpublished, with the same secret key and the patched files
   mounted. Inside it: load every agent profile, LLM profile and provider
   connection through the stores; run `inventory()` and `selections()` from
   the helper; list, re-install and enable the app; confirm every API path the
   runtime uses (`/api/agent-profiles`, `/api/profiles`, `/api/conversations`
   with `agent_profile_id`, `/api/canvas-extensions/*`) and run the container
   integration test on an isolated persistence directory. One real SDK tool
   call on a generated profile shows what litellm sends.
3. Only then switch: compose image tag and patch mounts, restart Canvas under
   the idle rule above, verify server info, the app, the control service's
   settings and providers, then one review. Keep the old tag and patch files
   for rollback.

**LLM profiles behind a LiteLLM proxy: use the `litellm_proxy/<model>` prefix,
not `openai/<model>`.** With `openai/`, the litellm inside agent-server applies
OpenAI-specific logic, including a chat→Responses bridge
(`responses_api_bridge_check` in `litellm/main.py`) that fires for any model
litellm's public cost map knows when the request carries both `tools` and a
`reasoning_effort` — i.e. every agent step. The call then goes to
`/responses`, which a proxy forwards to an upstream that may only serve chat
completions. Site B hit this on 2026-09-21 with `gpt-6-astra` (known to the
map; every review died in 4 s on a 404) while `gpt-5-6-sol` (unknown to the
map) never did. `litellm_proxy/` passes the request through as chat with all
parameters intact; verified with full SDK agent runs. The Auto Reviews runtime
generates profiles as `litellm_proxy/<model>` for any custom endpoint since
1.14.4 and, since 1.14.5, also writes the effort into `litellm_extra_body`
(`canvas_discovery.py`): the proxy provider drops the plain `reasoning_effort`
field (it does not know the model as a reasoning model and `drop_params` is
on), and site A's upstream refused every tool call that arrived without an
effort. Variants saved earlier under `openai/` or without the body are not
reused, so re-save the selection once after deploying and delete the old
variant in Canvas.
