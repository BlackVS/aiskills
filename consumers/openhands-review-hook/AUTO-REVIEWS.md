# Auto Reviews Canvas App

The app and portable runtime live in ai-skills. Site configuration, credentials,
and saved selections remain on each hands host. Requires Canvas host API 1 and
the Canvas Extensions backend API (verified on Agent Canvas 1.17.0).

## Behavior

The primary profile handles `review-this`. A single explicit
`review-this:<profile>` label overrides it; multiple explicit labels are rejected.
The configured fallback applies only when the selected profile is the primary.
Each profile carries a reasoning effort: API providers set it on the generated
LLM profile (`none`, `low`, `medium`, `high`, `xhigh`, `max`; `high` is the
SDK default), the Codex account agent encodes it in the ACP model id
(`model/effort`, `low`–`xhigh`; that combined id is what the review
signature then shows), and the Claude account agent keeps its adapter default. Effort support is model-specific on a shared endpoint (on one
proxied endpoint `none` was accepted by one model and rejected by another,
`minimal` by both); the app cannot know this at save time, so a review whose
model rejects the configured effort fails with that reason and points back to
the profile. An optional **secondary** reading profile turns on combined
mode for the default request: the conversation starts on the secondary's
profile (fast, cheap reading of the PR), and the prompt tells the agent to
switch to the primary's LLM profile before forming the frozen scope, so the
review pass, verification and write-up run on the primary, which also signs
the review. This needs the `switch_llm` tool, which only OpenHands-kind (API
provider) agents have, so a secondary requires API providers for both; an
explicit label never uses it. The conversation runs on the secondary's
*agent* (its tools, condenser and the rest); the primary contributes its LLM
profile and the signature, so the two agent profiles must match except for
the LLM profile and the switch tool: the app refuses to save a pair that
differs elsewhere and names the fields, a saved pair that drifted shows the
secondary with a warning and fails the default request with that reason, and
pairs the app generates always match. When the review completes, the runtime
reads which LLM profile the conversation ended on; a review written entirely
on the reading profile (the agent never switched) is still labelled done, but
the PR gets a note comment saying so, since the signature names the primary
model. With no secondary the review is single-profile, exactly as before. A
quota fallback keeps the reading profile when the fallback is itself an API
provider and runs plainly otherwise; a limit that hits the reading profile
before the switch makes the fallback run single-profile.
A review conversation that finishes without posting its comment fails the
request at once (the failure names the conversation so it can be inspected in
Canvas) instead of holding the working label for the whole watch.
On a confirmed quota or rate-limit error the runtime starts one fresh conversation, retaining
the same PR head and recording the original and fallback models in its prompt
and service log. The fallback review must disclose the switch. A rate-limit event confirms throttling, not exhausted credits; the fallback message does not claim otherwise. No loops, no
retry on generic errors, unknown startup outcomes or timeouts. If the head
changes, request a new review. A completed review is recognized by head and
verdict, not merely the presence of the bot marker.

The settings file contains `revision`, `primary`, nullable `secondary` and
nullable `fallback` — agent profile names only. A file written before 1.13.0
has no `secondary` and reads as None.
The API is its single writer, saves atomically, and rejects stale revisions.
Services read the settings and prompt for each new review; running reviews keep
their snapshot. A missing file preserves the environment's primary with no
fallback; a corrupt file fails closed. Canvas's ordinary conversation default
is independent. The app lists Canvas API providers and installed Claude/Codex
account adapters separately. Successful model lists are cached in memory for the current app session and reused when reopening the page or selecting the provider again. Reload clears the model cache; Test connection refreshes that provider. Failed lookups are retried, and provider edits replace the cached list. Selecting an uncached provider fetches its live model list;
authentication failures leave the model picker unavailable. A provider's model
catalog can include models that do not support review tools, so validate a new
model with a small review before using it as the default.

Each provider has List models and Test connection actions independent of reviewer selections. These check discovery, not inference or tool support, and send no inference prompt. API connections can be edited; leave the token blank to retain it. A failed preflight prevents the add/update request, preserving the existing connection. Legacy inline-key profiles remain managed through Canvas profiles.

Add an OpenAI-compatible API provider with its base URL and key/token. The app
tests authentication and model discovery before saving through Canvas's provider-connections API. Test requests pass through the authenticated control endpoint to the container and do not persist credentials. The discovery
helper resolves them inside the existing Canvas container and returns model
IDs only. Redirects are rejected so a provider cannot redirect its key elsewhere.
The helper reads the running agent server's cipher and storage configuration from its process environment inside the container. It uses the encryption key in memory only and fails closed if it cannot identify a unique server configuration. This is required when the entrypoint generates a key that new `docker exec` processes do not inherit. The helper does not persist or expose the key.
No credentials enter review settings. Existing inline-key LLM profiles are also
available; new model variants remain in Canvas's LLM profile store. New provider
connections are preferable when credentials should rotate across model variants.

Saving reuses a matching agent profile (same provider, model and effort; a
secondary additionally needs the switch tool enabled) or creates a separate
profile for the selected model. Its LLM profile addresses a custom endpoint
(any connection or source profile with a base URL) as `litellm_proxy/<model>`
and OpenAI itself as `openai/<model>`: with `openai/`, litellm reroutes every
tool-and-reasoning step for a model its cost map knows to a Responses endpoint
that proxies do not serve (see UPDATING). For a proxied profile the effort is
also written into litellm's extra body, because the proxy provider drops the
`reasoning_effort` field and a proxied upstream refused every tool call that
arrived without one. A variant saved earlier under the other prefix, or without
the effort body, is never reused or rewritten; the next save builds a fresh one
with a numeric suffix, and the old one can be deleted in Canvas. The profile is
named named `review-<model>[-<effort>][-reading]`
(for example `review-gpt-5-6-sol-max` and `review-gpt-5-6-sol-low-reading`; a
numeric suffix resolves a clash with an existing profile, which is never
overwritten). Profiles generated before 1.14.0 keep their `auto-review-<hash>`
names and stay in use because matching is by content, not by name. Generated
profiles are launch choices, not sources: while their source profile exists the
provider list never offers them, even when they carry a copied inline key. Names
starting with `review-` (and `auto-review-`) are reserved for the app; several
hand-made inline-key profiles sharing one endpoint and key count as one source.

Deleting a profile in Canvas that the settings still name changes nothing on
disk: the app shows that role with a warning naming what is missing (the agent
profile, or the LLM profile an agent profile references), Save waits for a
replacement, and a default-label review requested meanwhile fails with the same
reason for a broken primary or secondary (a broken fallback is skipped). The
runtime never deletes or rewrites profiles or settings; a person fixes or
replaces them. It does not activate profiles or change Canvas's conversation
default. Failed saves can leave an unused generated profile in Canvas; existing
review settings remain intact. Profiles created from shared connections reference those connections; variants of existing inline-key profiles stay in Canvas's LLM profile store.

## Model sources and existing profiles

Auto Reviews supports both existing Canvas model profiles and shared API provider
connections. Adding a connection through Auto Reviews stores it in Canvas; Auto
Reviews does not maintain a separate credential store. Its primary and fallback
selections remain independent of Canvas's default conversation model.

Manual chats use models configured in Canvas. Adding a shared API connection
through Auto Reviews does not by itself configure that connection's models for
manual chat; configure the chat model in Canvas separately. Auto Reviews can use
both those existing Canvas profiles and its separately selected provider/model
connections. This separation is intentional.

| Source | How Auto Reviews uses it | Where to edit it |
| --- | --- | --- |
| Existing Canvas model profile with its own API credentials | Appears as an API provider under the profile name; its endpoint supplies the available models | Canvas profile settings |
| Shared Canvas API provider connection, including one added through Auto Reviews | Appears under the connection name; reviewer model profiles reference the shared connection | Edit in Auto Reviews or Canvas provider settings |
| Installed Claude/Codex account connection | Discovers models through the installed account adapter | The account connection's own configuration |

An existing Canvas profile and a newly added connection can coexist for the same
upstream provider. The older profile is still supported; absence of an Edit
button in Auto Reviews does not mean it is an account subscription or unusable.
List models and Test connection work for either API source.

Explicit review labels select an agent profile, which may reference an existing
Canvas model profile or a shared provider connection. Keep that agent profile and
its referenced model profile while the label remains in use. Adding a new provider
connection does not migrate these references or change Canvas's default model.
Do not remove an older profile until its default-model and reviewer references
have been migrated and any required compatibility proxy has been preserved.

## Install

Do this while both review services are idle. Back up existing receiver scripts,
units, proxy configuration and review settings before changing them. Keep the
Canvas image unchanged during this installation.

1. Copy the seven runtime modules (`review_hook.py`, `github_review_poller.py`,
   `review_runner.py`, `review_policy.py`, `review_control.py`,
   `canvas_discovery.py`, `reasoning_profiles.py`) from `runtime/`
   into the site's existing hooks directory. The service examples use
   `/opt/openhands/hooks`. Keep existing site environment files and rendered
   prompts. Review the adapter defaults and explicitly set forge URL, owner or
   repository scope, bot identity and credential locations for your site.
2. Install `runtime/review-control.service`, load the same Canvas API key and
   profile-directory configuration as the reviewers, and start it. It listens
   on loopback port 8082. `REVIEW_SETTINGS_FILE` overrides the default
   `/opt/openhands/hooks/review-settings.json`; `REVIEW_CONTROL_PORT` changes
   its port. Never commit this site's state or credentials.
   The control service needs Docker access to execute the discovery helper in
   the existing Canvas container. `CANVAS_CONTAINER` defaults to
   `openhands-canvas`. It uses that container's installed SDK and credential
   configuration; no additional Python package or credential store is installed.
3. At the same authenticated Canvas origin, proxy just
   `/api/review-control/*` to `127.0.0.1:8082` before the general Canvas route:

   ```caddy
   handle /api/review-control/* {
       reverse_proxy 127.0.0.1:8082
   }
   ```

   Preserve the client's `X-Session-API-Key`/Bearer header. The API validates
   the existing Canvas key itself. Do not expose an unauthenticated settings
   endpoint or add wildcard CORS. Remote Canvas backends must use this proxy
   origin for the app to reach the API.
4. Validate and reload the proxy, restart the two idle reviewer services once
   to load the new code, and verify they stay active.
5. Make `auto-reviews/` available inside the Agent Server container through an
   existing persistent mount, or install directly from this Git repository
   with repo path `consumers/openhands-review-hook/auto-reviews`. In Canvas,
   open **Customize > Apps > Add app** and select the source. New apps install
   disabled. Inspect the source and enable **Auto Reviews**; its page appears
   in the sidebar. It uses the official authenticated request helper and does
   not scrape browser credentials.
6. Select **Codex**, then its advertised Astra model and an effort for primary;
   select **Claude**, then the desired fallback model. Save and reload to
   confirm both selections. For combined mode choose an API provider as
   primary, then a secondary reading profile (an API provider, typically the
   same model at a lower effort); the secondary picker stays disabled while the
   primary is an account agent. For the temporary test configuration choose Opus 5; Fable can be
   selected later without code changes. Set fallback provider to **None** to
   disable retry.

## Verify

Run `python3 -m unittest -v test_review_control test_adapters test_discovery` from `runtime/`, plus
`python3 -m py_compile *.py`. Run `python3 render.py --check` from the consumer
directory. The tests use fake forges and agents and never post a PR comment.
`test_canvas_integration.py` runs inside Canvas only with `OH_PERSISTENCE_DIR`
set to a fresh `/tmp/auto-reviews-test-*/state` directory and `OH_SECRET_KEY` set to an arbitrary test-only value. It creates an encrypted fake
provider and checks model selection, profile reuse, and credential separation
using the real SDK stores. Never point it at live persistence.

Verify API requests without authentication return 401. In Canvas, save, reload
and confirm both selections persist; a second tab saving an old revision must
receive 409. Verify a tiny no-tools conversation with the fallback profile
before using it for reviews. Test quota handling with a fake Agent Server;
never exhaust real account credits to manufacture a quota failure. A real PR
test posts publicly and must use an explicitly authorized test PR.

### Deployment checks

Before stopping services, check both forges for open PRs carrying
`hands-reviewing`, then confirm their current labels (search indexes can lag).
Also check Canvas for running review conversations. Wait for any active review
to finish. Create a restricted backup directory and copy the hooks directory,
existing control-service unit if present, and proxy configuration into it.
Record its path in the private deployment log.

For the example systemd installation, validate and check services with:

```sh
caddy validate --config /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now review-control
systemctl reload caddy
systemctl restart review-hook github-review-poller
systemctl is-active review-control review-hook github-review-poller
```

Use the host's existing Canvas authentication when calling these endpoints:

| Request | Expected result |
| --- | --- |
| Unauthenticated `GET /api/review-control/settings` | 401 |
| Authenticated `GET /api/review-control/providers` | API and installed account providers, no credentials |
| `GET /api/review-control/models?provider=acp%3Acodex` | Live account model list |
| `PUT /api/review-control/settings` with current revision and selections | Saved profile references and incremented revision |
| `PUT` with a `secondary` selection while the primary is an account agent | 400; nothing prepared or saved |
| A default-request review with a secondary configured | Service log shows `start=<secondary profile> agent-settings=<secondary profile>`, then `switch verified`; the review comment's signature names the primary model. A conversation that never switched logs `review written off the primary` and adds a note comment to the PR naming the profile it ran on |
| `POST /api/review-control/test-provider` with a stored provider ID | Connection status and models; settings and credentials unchanged |
| Repeat the same PUT revision | 409; saved settings unchanged |
| Reload the app | Both saved provider/model selections restored |

For API installation from an existing container mount, call
`POST /api/canvas-extensions/install` with
`{"source":"/projects/auto-reviews"}` after placing the two package files there.
Then call `PATCH /api/canvas-extensions/installed/auto-reviews` with
`{"enabled":true}`. This is equivalent to installing and enabling through
Customize > Apps. Neither operation requires a Canvas container restart.

The browser fixture in `tests/test_extension.html` exercises the actual
extension module with fake APIs. Open it with a local static file server, or
headless Chrome with file-module access enabled. A successful run displays
`PASS` for selection restore, provider changes, save, provider creation,
credential clearing, HTML escaping, failure handling, and reload. This fixture
checks UI behavior; repeat Save/Reload in the installed Canvas app to check its
host integration.

## Upgrade and rollback

Update app and runtime together from a pinned ai-skills revision. Code changes
need a service restart; prompt and selection changes do not with this runtime.
The app uses the current beta Canvas API, so check it after Canvas upgrades.

For rollback, disable the app, restore the old scripts/units and proxy
configuration, and restart the idle reviewers. The settings file is
backward-compatible only while no secondary has been saved: 1.13.0 writes
`secondary` into the file only when one is set, and a pre-1.13 runtime rejects
the key — before rolling back, set the secondary to None and save once, or
remove the `secondary` entry from the file by hand. Keep provider credentials and
profiles intact. A receiver restart during a review is safe from 1.18.0: each
run is recorded in `REVIEW_RUNS_DIR` (default `/opt/openhands/hooks`,
`review-runs-gitea.json` and `review-runs-github.json`) once its conversation
exists, and the restarted service re-attaches its watcher (journal:
`review resume`, then `review resumed`) with the same head and deadline. Only a
run that was never recorded (a write to that directory failed, or a pre-1.18
runtime started it) falls back to the older recovery: completed when its review
comment is already posted for the current head, failed otherwise. The Canvas
container is different: restarting it ends every conversation, so keep that
restart for an idle moment.

If the control service did not exist before this installation, stop and disable
it during rollback; otherwise restore its previous unit. After restoring the
proxy configuration, validate it before reloading Caddy. Reload systemd and
start the two reviewer services, then check that both are active. Keep the
provider connections and generated profiles: deleting them can break other
Canvas users and is unnecessary to restore the old review runtime.
