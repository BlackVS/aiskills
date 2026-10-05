# Changelog

All notable changes to this skill set. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semver](https://semver.org/) for the set as a whole: bump **major** for
breaking changes (a skill renamed or removed, installer flags changed),
**minor** for new skills, prompts, or installer features, **patch** for
content fixes inside existing skills. The current version is in `VERSION`.

## [Unreleased]

## [1.32.0] - 2026-10-05

### Added
- verify-delivery: GitLab merge requests (#54), with `--forge gitlab`, a
  merge request URL (subgroups included) or `GROUP/REPO!N`, and
  `GITLAB_TOKEN_FILE` (sent as a Bearer token). The four checks read:
  - **reviewed head:** the merge request's notes, without system notes. As on
    Gitea, each review needs a `--review-author` list, since GitLab has no
    author association.
  - **merge:** `merge_user`, typed by the `bot` flag of its user record (no
    flag, for example without a token, is `unknown`, not a person). The merged
    commit is the merge commit, else the squash commit, else the head itself
    (a fast-forward), reported as `merged_as`.
  - **merged content:** tree equality as a straight compare with no diffs (GitLab
    commits carry no tree id). The patch identity uses diffs rebuilt from the
    three-dot compare, with paths quoted as git quotes them, which hash like
    GitLab's raw diff. A fast-forward
    compares from the merge request's base. A change past GitLab's diff
    limits, text in which GitLab replaced bytes that are not UTF-8, a path
    with a control character or replaced bytes, and (before GitLab 18.4) a
    renamed file sent without text have no identity.
  - **post-merge CI:** the latest pipeline on the merged commit for the target
    branch with source `push`. Only `success` passes. `manual` and running
    pipelines are pending. `failed`, `canceled` and `skipped` fail, since a
    skipped pipeline ran no job.
  - **tests:** fixtures recorded from a live run on gitlab.com, one per merge
    strategy (a merge commit and a squash by a bot account, a fast-forward
    by a person), with the project, accounts, ids, SHAs, messages and file
    contents replaced by placeholders.

## [1.31.9] - 2026-10-05

### Fixed
- hands consumer: the watch always takes one last look at the PR comments at
  its deadline unless a review was already seen. Before, it looked only when
  the last comments read had failed, so a review posted after a good comments
  read and a failed conversation read was reported as missing (#64 item 10).
- hands consumer: once the "previous verdict stands" note is posted, the
  request ends. A transient error in the label swap after the note is tried
  again (three attempts in all), and a swap that still fails is logged. Before,
  any error there started a full review on a PR just told that no new review
  would run (#64 item 11).

## [1.31.8] - 2026-10-05

### Changed
- oh-technical-writing: the skill's description, scope sentence and README
  list name technical text persisted through write tools or saved as
  Markdown, which
  1.31.7's "Persisted text" section covers. A client that picks skills by
  their description now loads it for that text too.

### Fixed
- hands consumer tests: the review-control drain test sizes its refused body
  from `io.DEFAULT_BUFFER_SIZE` plus half the drain bound, instead of a fixed
  32 KiB. A Python with a larger default read buffer no longer makes the
  refusal cases pass trivially and the bound case fail (checked with a
  simulated 128 KiB buffer).

## [1.31.7] - 2026-10-05

### Changed
- oh-technical-writing: a short "Persisted text" section says the guide
  applies equally to text sent to write tools and to saved Markdown. That text
  is shortened by cutting, never by compressing, even under pressure or after
  compaction, and code, identifiers, paths, hashes and quoted evidence stay
  exact. Where aimem is installed, its `writing_rule` tool gives the full rule
  for persisted text. The section points to "Cut without compressing" and
  "Final pass" instead of restating them, and reads correctly without aimem
  (#49).

## [1.31.6] - 2026-10-05

### Fixed
- hands consumer: `review-control` no longer resets the connection after
  refusing a request whose body it did not read (401, 413, 415, or a 400 for a
  bad `Content-Length`). Closing with unread body bytes sends a reset, and on
  Windows a client that had not read the answer yet lost it. That made
  `test_json_endpoints_refuse_bad_bodies_before_any_discovery` fail now and
  then on Windows. The handler now ends the answer, then reads and discards the
  rest of the body, at most 64 KiB or 1 s (the server is single-threaded),
  before closing. A new test peeks at the connection after each refusal for
  unread bytes, and checks the bound (#36).

## [1.31.5] - 2026-10-04

### Fixed
- hands consumer: the watch deadline no longer misreports a review hidden by a
  forge outage (#64, items 2, 6 and 8).
  - **Missed last look:** if the last look at the comments failed, or there
    was none (a run resumed after its deadline had passed), the deadline looks
    once more. A review found then is labelled done as usual. If
    that look fails too, the request fails with "the forge could not be
    reached … to see whether the review was posted; look at the pull request
    before requesting another review", not "no review posted".
  - **One failure comment:** a request's failure report is attempted once,
    whichever path asks for it again (a second call only logs "failure already
    reported, not again"). If something raises after it, that is only logged.
    A stale report from the patch check ends the request even when it raises.
    A report that itself raises is logged as "failure report raised, may not
    be on the PR", so an operator knows to look. This line replaces 1.31.2's `failure not reported`.
  - **Log wording:** the tries at the deadline (the last look at the comments,
    the pull-request read and the label swap) log "the deadline has passed",
    not "retrying at the next poll". The 1.31.3 entry's deadline
    note describes attempts: the label removal and the note are skipped while
    the forge is still down.

## [1.31.4] - 2026-10-03

### Fixed
- verify-delivery: `SKILL.md` says the rebase-merge check is `pending` only
  while the forge is unavailable for the moment; a commit list or commit the
  forge does not find means "not a rebase merge", and the first-parent result
  stands (#62).
- hands consumer tests: the adapter tests' fake forge handles one request at a
  time, and the test resets the labels under the same lock, so a handler never
  reads the label set while the test refills it (#62).

## [1.31.3] - 2026-10-03

### Fixed
- hands consumer: a review that is on the PR is no longer failed by a forge
  outage while it is labelled done. If the pull-request read or a label write
  that completes a posted review fails transiently, the watch logs it (`label
  swap failed, retrying at the next poll`) and completes the review at the next
  poll. Before, an outage that outlasted 1.31.2's three GET attempts, or any
  failed label write, reported "could not complete" and removed
  `hands-reviewing` from a reviewed PR. Both label writes are idempotent, so
  trying again is safe. If the forge stays unreachable until the watch
  deadline, the deadline tries once more. If that fails too, it removes
  `hands-reviewing` and posts a note (never a review, and no retry advice)
  saying the review stands and `hands-reviewed` is to be added by hand. Before,
  it failed the request as "no review posted", which invited a duplicate
  review. A new
  adapter test shows that a forge write is never sent twice (first items of
  #64).

## [1.31.2] - 2026-10-03

### Fixed
- hands consumer: a brief forge or Canvas outage no longer loses a review.
  Before, one timed-out comment read while the review conversation ran failed
  the review. The failure report then hit the same outage, and the review
  thread died, leaving `hands-reviewing` on the PR with no comment, even though
  the conversation went on to post a correct review (#61).
  - **Watch:** a read that fails transiently (timeout, refused or dropped
    connection, HTTP 5xx or 429) is logged and tried again at the next poll;
    only the watch deadline ends the watch. Any other error still fails the
    review.
  - **Failure report:** a failure while reporting a failure is logged
    (`failure not reported`) and never escapes the review thread.
  - **Forge reads:** both receivers try a forge read (GET) up to three times
    on transient errors (1 s, then 2 s apart). Writes are not retried, since one
    that timed out may still have happened.

## [1.31.1] - 2026-10-03

### Fixed
- verify-delivery: a rebase-merge walk that cannot be read is tried once per
  run, not once for every reviewed head (the result was already `pending`;
  only the requests repeated). A failed rebase comparison now says the
  combined change of the rebase merge's commits is not the reviewed change.
  `SKILL.md` says the reviewed change is read again from the rebase's base,
  and that an unreadable commit listing or walk is `pending`. New tests: the
  walk read once with two reviewed heads, and an unreadable compare after the
  base is found is `pending`. Closes #57.
- hands consumer: the GitHub poller's `REVIEW_AUTHOR` documentation and the
  1.31.0 `UPDATING.md` row say to set it when the token has no user (a GitHub
  App installation token). New tests: the token owner is read once across
  requests, and the reviewer login is read only when the pull request has a
  review comment. The review guide records the Gitea 1.27 compare diff and the
  commit order of `pulls/{index}/commits` observed on gitea.com. Closes #59.

## [1.31.0] - 2026-10-03

### Added
- hands consumer: the Gitea receiver keeps the previous verdict of an
  unchanged patch, as the GitHub poller has since 1.30.0. A plain
  `review-this` on a PR whose newest review is of an earlier head with the
  same patch identity gets the "previous verdict stands" note instead of a new
  review. The identity is read from Gitea's three-dot compare with
  `?output=diff` (Gitea 1.27 or later); an older Gitea answers JSON, which has
  no identity, so there every request is still reviewed in full.

### Changed
- hands consumer: only the reviewer's own review comments can make a
  previous verdict stand. The Gitea receiver uses `BOT_NAME`; the GitHub
  poller uses the new optional `REVIEW_AUTHOR`, else the token's owner (read
  once from `GET /user`). Before, the GitHub poller took a review-looking
  comment from any author, so a forged one on an earlier head could stand in
  for a review. A site whose GitHub reviews are posted by another account
  sets `REVIEW_AUTHOR`, or every request is reviewed in full. Logins compare
  case-insensitively. Closes the remaining parts of #13.

## [1.30.4] - 2026-10-03

### Fixed
- verify-delivery: a rebase merge of several commits onto a moved base is
  confirmed through the patch identity. The forge reports the last rebased
  commit as the merge commit, so its change against its first parent covered
  that commit only and never matched the reviewed change: such a delivery was
  `not_confirmed` (a false negative, never a false pass). When that change
  does not match and the merge is a rebase merge of the pull request (its
  commits form one line of 2 to 100 commits from its head, and as many commits
  ending at the merge commit each have one parent and the same commit message,
  in order), the merged change is read from the base the rebase landed on; the
  check reports it as `base_parent_sha`, with `rebased_commits`. The commits
  are ordered by their parents, since GitHub lists them oldest first and Gitea
  newest first. On Gitea this needs the compare diff of 1.27 or later; an
  older Gitea fails with the reason. Fixtures of a two-commit rebase merge
  cover both forges.

## [1.30.3] - 2026-10-03

### Fixed
- verify-delivery: a formal pull-request review no longer counts as a review.
  Its body can be edited after the merge, and GitHub's REST API gives it no
  edit time (GraphQL's `lastEditedAt` exists, but needs a token and has no
  counterpart elsewhere), so an edit could turn it READY unseen. Only comments
  on the pull request count, the record every forge keeps with both a creation
  and an edit time; the review gates and the hands receivers already post
  reviews that way. A formal review that matches a review pattern is listed
  under `ignored` with the reason. A test on each forge covers a READY formal
  review.

## [1.30.2] - 2026-10-03

### Fixed
- verify-delivery: `--merger` and `--bot-account` compare logins
  case-insensitively, as `--review-author` already did. Logins are
  case-insensitive on GitHub and Gitea, so `--merger blackvs` used to reject a
  merge by `BlackVS`. Both sides are lowercased; a test on each forge covers
  the listed name and the forge's reported login in a different case.

## [1.30.1] - 2026-10-02

### Fixed
- verify-delivery on Gitea: a trusted review of an older head is now carried
  across a base-only update, as on GitHub. Gitea serves the compare diff with
  `compare/<a>...<b>?output=diff` from 1.27. This was observed on gitea.com
  (1.27.0+dev, 2026-10-02), where its patch identity equals that of git's own
  three-dot diff. The parameter is absent from the 1.23.8, 1.24.7, 1.25.5 and
  1.26.4 API specs. The helper reads both changes from that compare. When a
  Gitea answers JSON (before 1.27) or has no compare endpoint (before 1.22),
  the review still fails with "re-review at the final head", and the detail
  now names the Gitea version needed. Tests cover the carried review, a later
  change it does not cover, and the three ways an older Gitea answers; a new
  Gitea fixture holds the older head's diff.

## [1.30.0] - 2026-10-02

### Added
- Auto Reviews runner: a plain `review-this` on a GitHub PR whose newest
  review is of an earlier head, with the same patch identity at both heads, is
  not reviewed again. A base-only update is the typical case. The poller posts
  "patch unchanged since <old head>; previous verdict stands", naming both
  heads and both identities, labels the request done and starts no
  conversation. The PR is read again after the comparison: one that moved to
  another head or closed meanwhile fails as stale and is never completed. The
  identity is `verify-delivery`'s digest of GitHub's
  three-dot compare diff, so the runtime and the delivery check agree; a test
  keeps the two functions identical. The note is not a review, so it never
  satisfies `verify-delivery`. A request for the same head, an explicit
  `review-this:<profile>` label, a changed patch and an unreadable or binary
  diff still get a full review. Gitea's API has no diff of an older head, so
  the Gitea receiver always reviews. Runner tests cover each case; an adapter
  test runs the GitHub poller against a fake compare endpoint.

## [1.29.10] - 2026-10-02

### Changed
- Auto Reviews runner test: the read-failure case of 1.29.8 now reaches its
  restart branch. Reads fail both before the fallback and at the end of the
  run, so the primary's record survives. The test asserts that exactly that
  record is left, that the resumed run starts exactly one fallback, and that
  nothing is left to resume after it. Before, the end-of-run clear removed the
  record and the resume check passed without running. Test only; nothing to
  deploy.

## [1.29.9] - 2026-10-02

### Changed
- Auto Reviews docs: AUTO-REVIEWS states that generated API-provider agent
  profiles run on the SDK's agent defaults, including the condenser (240
  events and no token cap of its own on SDK 1.49.1; the SDK still condenses at
  the model's input limit). They take no tools or condenser
  tuning from a hand-made source profile, which keeps generated primary and
  reading profiles matching for combined mode. A duplicated word in the same
  paragraph is fixed. Nothing to deploy.

## [1.29.8] - 2026-10-02

### Fixed
- Auto Reviews runner: a run-state file that cannot be read no longer counts
  as "no record". `RunStore.clear()` turned a read error into an empty state
  and reported the previous attempt's record as removed, so after a transient
  read failure, a failed fallback save and a restart, the old record could
  resume and start a second fallback. A read error now makes `clear()` report
  failure, and the runner then does not start the fallback (as it already did
  when the record could not be written away). Content that is not JSON still
  reads as empty: writes replace the file atomically, so it stays unparseable
  and holds nothing that could resume. A fault-injection test covers the
  failed read before the fallback and the resume that follows.

## [1.29.7] - 2026-10-02

### Fixed
- Auto Reviews app 0.2.10: when a saved secondary reading profile was deleted
  or broken and the primary was then switched to an account agent, the app
  cleared and disabled the secondary but kept its warning, and the open
  warning kept Save disabled. Clearing the secondary now clears its warning
  too, so Save is enabled for the new choice. A fixture case covers it.

## [1.29.6] - 2026-10-01

### Added
- Auto Reviews: an end-to-end test of combined mode on the SDK's own stores
  (`test_canvas_integration.py`). It saves a primary, a secondary and a
  fallback through the settings API, so `prepare()` writes real agent and LLM
  profiles, then runs the runner on those files and checks that the review
  starts on the reading profile, is told to switch to the primary's LLM
  profile, is signed with the primary's model and has the switch verified.
  Like the existing integration case it skips without the SDK; both now run
  outside Canvas too, in a Python 3.12 environment with the Canvas image's
  `openhands-sdk` 1.49.1 and `openhands-agent-server` 1.27.1. The existing
  case counts profiles relative to its start, so the two run in either order.
  Nothing to deploy.

## [1.29.5] - 2026-10-01

### Fixed
- Auto Reviews: a save that needs a new generated profile while Canvas's agent
  or LLM profile store holds its limit of 50 is refused before anything is
  written, so a full agent store no longer leaves a new LLM profile behind. The
  settings API answers 409 with a message that gives the count and how many of
  those profiles Auto Reviews generated (no names), and app 0.2.9 shows the
  settings API's own error text on a failed save, falling back to the generic
  text when there is none. A selection that matches an existing profile still
  saves. The discovery helper reports the full store as `profile-limit`; any
  other failure keeps its fixed text. Unit tests cover the check, the helper's
  answer and the 409; fixture cases cover the shown text and the fallback.

## [1.29.4] - 2026-10-01

### Changed
- Auto Reviews discovery helper: `server_environment()` no longer computes
  `OPENHANDS_AGENT_SERVER_CONFIG_PATH` or its default
  `workspace/openhands_agent_server_config.json`. The SDK builds its
  configuration from `OH_*` variables only and has no config file (observed
  on agent-canvas 1.20.0, SDK 1.49.1; 1.29.3), so the helper copies only
  `OH_SECRET_KEY` and, when set, `OH_PERSISTENCE_DIR`. Two agent-server
  processes that differ only in that unused variable no longer count as
  ambiguous.

### Fixed
- Auto Reviews app 0.2.8: when Canvas's request helper throws on an error
  status (it does, with the parsed body in `response`), the connection test
  shows the server's own `message` instead of the generic "unavailable" text.
  Only the message is taken from a thrown answer, so it is never a success.
  Fixture cases cover a thrown message, a thrown body without one, and a
  thrown body that claims success.

## [1.29.3] - 2026-10-01

### Changed
- The repository review guide (`.agents/skills/custom-codereview-guide`)
  records the Auto Reviews runtime contracts observed on 2026-10-01 on
  agent-canvas 1.20.0 with aiskills 1.29.2 deployed, closing the last note of
  issue #11: two agent-server processes with one configuration and cwd `/`;
  the SDK has no config file or default config path any more (the helper's
  literal is unused but harmless); a `cipher=None` profile load returns the
  Fernet ciphertext as a truthy `SecretStr`; no stored connection lacks a
  `base_url`; the "401 with a body" race does not reproduce in 140 runs; and
  Canvas's app request helper throws an `HttpError` carrying `status` and the
  parsed body in `response` on an error status. Documentation only.

## [1.29.2] - 2026-10-01

### Changed
- Auto Reviews app 0.2.7, a refactor with no change in behavior (issue #11,
  item 11). The provider editor's state is one value, the row being edited
  or none, and `showEditor()` renders every part of the editor from it (form
  values, token rule and placeholder, button caption, Cancel, heading, hint);
  Edit, Cancel and a saved change all go through it. Each provider row is
  built by `providerRow()`, and its buttons carry an explicit action key
  (`data-action`: `list`, `test`, `edit`) that one handler dispatches on,
  instead of comparing caption text. The fixture from 1.29.1 passes
  unchanged against the new app; a new case pins the action keys.

## [1.29.1] - 2026-10-01

### Changed
- Auto Reviews: the app no longer parses provider ids (issue #11, item 10).
  The discovery helper's inventory rows now say `editable` (true for Canvas
  provider connections) and carry the connection's own `connection_id`; the
  app shows Edit by `editable`, sends the edit to `connection_id`, and finds
  a newly added connection by `connection_id` after the reload, so the id's
  namespace (`connection:`, `profile:`, `acp:`) is known only to the helper.
  The unread `configured` flag is gone, and with it the decryption of every
  connection's key on each inventory read. App 0.2.6. The fixture gives its
  fake connection an id that is not `connection:<id>` to prove the app treats
  ids as opaque; a unit test covers the inventory rows.

## [1.29.0] - 2026-10-01

### Added
- The one-liners verify what they install (issue #25). For a release (the
  latest, or `AI_SKILLS_REF=vX.Y.Z`), `boot.sh` and `boot.ps1` now download
  the release asset `aiskills-X.Y.Z.tar.gz` and the release's `SHA256SUMS`,
  and install only when the archive's SHA-256 matches its single entry there.
  A missing asset, a missing `SHA256SUMS`, a missing or ambiguous entry, or a
  mismatch stops with nothing installed and no fallback. A failed download
  says what stopped it: not found (HTTP 404), another HTTP status from the
  server, or no answer at all.
  - A private fork (`AI_SKILLS_TOKEN`) downloads the assets through the API.
  - A Gitea base (`AI_SKILLS_BASE`) uses that release's attachments.
  - Every archive's digest, verified, local or unverified, is passed to the
    installer as `AI_SKILLS_ARCHIVE_SHA256`, so it reaches the manifest's
    `archive_sha256` (1.28.0). boot.ps1 restores the caller's value of that
    variable afterwards.

### Changed
- A ref that is not a release (`main`, a branch, a commit) has no
  `SHA256SUMS` to check against. It is refused unless `AI_SKILLS_UNVERIFIED=1`
  is set, and is then installed from the source archive with an "UNVERIFIED"
  warning. The same applies to the fallback to `main` while a repository has
  no release, and to `AI_SKILLS_BASE` without `AI_SKILLS_REF` (which installs
  `main`). CI's "install this commit" steps set the opt-in.

## [1.28.0] - 2026-10-01

### Added
- The installers record what they installed (issue #24). Every run of
  `install.sh` and `install.ps1` writes `<dest>/.ai-skills.json` into each
  skills directory it installs to, with `version`, `commit`, `skills`,
  `archive_sha256` and `installed_at`. The file is replaced whole, holds
  UTF-8 without a BOM and LF line endings, and the same fields come from
  both installers. `skills` names only what this run installed there.
  `commit` is the source checkout's HEAD, or `AI_SKILLS_COMMIT` outside a
  checkout. `archive_sha256` comes from `AI_SKILLS_ARCHIVE_SHA256`. A field
  that is unknown or malformed is `null`. The README documents the format
  and that a missing manifest means "version unknown". The client check
  (`.github/check-clients.py`) now fails if a client lists the manifest as a
  skill.

## [1.27.7] - 2026-10-01

### Fixed
- Tests only, no change to what ships.
  - `test_acp_discovery_child_never_receives_the_cipher_key` failed about
    once in 40 runs. Its stand-in ACP agent exited without reading, so the
    discovery helper's first write could meet a closed pipe and raise
    `ConnectionResetError` instead of the expected `ValueError`. The stand-in
    now reads the request before exiting (300 of 300 runs pass; before, 7 of
    300 failed).
  - The Auto Reviews fixture's provider edit now submits the URL without the
    trailing slash that the fake inventory stores, so the 1.27.6 check
    "List models after an edit reuses its test result" covers the URL drift
    it names. Follow-up from the external review of 1.27.6.

## [1.27.6] - 2026-10-01

### Fixed
- Auto Reviews app 0.2.5: the model cache is keyed on the provider id
  (issue #11, item 9). It was keyed on the id and URL. After adding or editing
  a provider, the app stored the test's model list under the URL as typed, then
  dropped it on the next load because Canvas returned the URL in another form
  (a trailing slash): List models fetched again. A fixture case has the fake
  inventory return the URL with a trailing slash, for the add and the edit.
  A URL changed in Canvas outside the app shows its models after Reload.

## [1.27.5] - 2026-10-01

### Fixed
- `AUTO-REVIEWS.md`: the paragraph on "unavailable" messages (added in 1.27.4)
  said the provider was not asked. The discovery helper may reach the provider
  before failing, for example when the provider answers without a model list, and the settings
  answer also covers a settings file that cannot be read or saved. It now says
  so, and to check the logged reason before changing a provider's URL or
  credentials. Documentation only.

## [1.27.4] - 2026-10-01

### Fixed
- Auto Reviews settings API: a failure of the discovery helper itself (exit
  status, timeout, output that is not JSON, an error it reports, `docker`
  missing) is no longer only "unavailable" with no trace (issue #11, item 8).
  The user text stays the same; `review-control` now logs a line with a fixed,
  non-secret reason on stderr, never the helper's output. Helper output that
  is not JSON was answered as an invalid request (400 "Invalid connection
  test" or "Invalid settings"); it is now the same "unavailable" answer as
  any other helper failure (200 for the connection test, 503 for settings
  and discovery). `AUTO-REVIEWS.md` says where to find the reason.

## [1.27.3] - 2026-10-01

### Fixed
- Auto Reviews connection test: one answer shape (issue #11, item 7). The
  test-provider endpoint answered a failed test as `{ok: false, message}` but
  its refusals (400, 401, 404, 413, 415) as `{error}`, while the app reads only
  `message`. Every answer of that endpoint is now `{ok, message}` (or
  `{ok: true, models}`), with the status codes unchanged; the settings API
  keeps `{error}`. App 0.2.4 makes the shape question moot on its side:
  whether Canvas's request helper resolves or throws on an error status,
  only `ok: true` with a model list counts as success, and only a text
  `message` is shown, never `undefined` or an object. Tests cover each
  endpoint's shape at every refusal and both helper behaviours.

## [1.27.2] - 2026-10-01

### Fixed
- Auto Reviews app 0.2.3: a provider token of spaces only is refused in the
  form before anything is sent (issue #11, item 6). It passed the field's
  `required` check and was then trimmed away, so adding a provider sent a
  connection test without a token (refused by the server), and editing one
  quietly kept the saved token. The form now says what to do: enter a token
  when adding; clear the field to keep the current token when editing. The
  message clears as soon as the token is changed, the edit is cancelled or
  another edit starts. A fixture case covers both modes.

## [1.27.1] - 2026-10-01

### Added
- Auto Reviews browser fixture (`tests/test_extension.html`): four
  assertions that used to stay green on a regression now fail on it
  (issue #11, item 5). The fixture checks that a provider edit sends its
  PATCH to the edited connection's path and then leaves edit mode (a failed
  edit stays in it), that a new provider is tested with its URL and token
  only, and that Test connection refreshes the model list: List models right
  after it makes no request and shows the refreshed list. No change to the
  app.

## [1.27.0] - 2026-10-01

### Changed
- The set is named `aiskills` everywhere, like its repository; `ai-skills`
  remained in the docs, the gates block, release titles and tag messages,
  user agents and temporary-file prefixes. The `AI_SKILLS_*` environment
  variables of the one-line installers keep their names.
- The review-gates block the installers write is now marked
  `<!-- aiskills:review-gates start ... -->` / `<!-- aiskills:review-gates end -->`.
  Both installers also recognize the old `ai-skills:` markers and replace a
  block written by an earlier release, so an upgrade leaves one block, never
  two; text outside the block is kept. Anything that searched an instruction
  file for the old marker should search for the new one.

## [1.26.7] - 2026-10-01

### Changed
- Auto Reviews backend: the connection-test request shapes are listed once
  (`PROBE_SHAPES` and `probe_shape()` in the discovery helper), and the
  settings API checks a request against that same list instead of its own
  copy. The JSON body checks that saving settings and testing a connection
  both made (415 for another content type, 413 for an empty, oversized or
  chunked body) are one method, `json_body()`. Nothing a user sees changes;
  a connection test with an empty provider id is now refused on its shape
  before the helper reads the provider list, with the same message as before
  (issue #11, item 4).

### Added
- Tests for the backend paths that had none: the connection-test endpoint's
  415, 413 and 400 answers (and that none reaches discovery), its "unavailable"
  answer when discovery fails or times out, the same body checks on saving
  settings, the helper refusing every other request shape, its timeout and
  catch-all messages, and that a saved connection is opened with the agent
  server's cipher.

## [1.26.6] - 2026-10-01

### Changed
- Auto Reviews discovery helper: the entry point is factored into
  `main(request, proc_root)` with no change in behavior, so a test can
  cover the line that puts the agent server's environment (its cipher key)
  in place before an action runs. The new test uses a fake `/proc` with the
  agent server and a decoy process holding another key: the action sees the
  server's key, and an empty `/proc` fails closed before any action runs
  (issue #11, item 3). Hands sites deploy `canvas_discovery.py` (see
  `UPDATING.md`).

## [1.26.5] - 2026-10-01

### Security
- Auto Reviews discovery helper: the account agent (`claude-agent-acp`,
  `codex-acp`) it starts to list a provider's models no longer inherits
  `OH_SECRET_KEY`. The helper copies the agent server's cipher key into its
  own environment to read the stores, and every child process inherited it;
  the agent is now started with that environment minus the key (issue #11,
  item 2). Reproduced with a stand-in binary before the fix. Hands sites
  deploy `canvas_discovery.py` (see `UPDATING.md`).

## [1.26.4] - 2026-09-30

### Fixed
- Auto Reviews app 0.2.2 (issue #12): saving a provider edit reloaded the
  page from the saved settings and discarded reviewer choices that had not
  been saved yet (a changed primary or fallback model disappeared, and Save
  and Revert both lost it). The pending choices now survive the reload,
  still marked unsaved, and the edit says so; Revert still restores the
  saved selection. A browser-fixture case in `tests/test_extension.html`
  covers it. Hands sites re-install the app (see `UPDATING.md`).

### Changed
- `.gitignore` also ignores `.mcp.json`, the checkout-local MCP server
  wiring, next to `.aimem.json`; nothing installed changes.

## [1.26.3] - 2026-09-30

### Fixed
- Review runner, restart recovery (issue #10, item 10): `recovered_head()`
  reads the pull request again after matching the review comment and
  requires it to be open at the same head, so a PR that moved while the
  comments were read no longer completes a restarted run with a review of
  the old head.
- Review runner, fallback after a failed state write (issue #10, item 15):
  `RunStore.clear` and `save` report whether the state is on disk, and the
  single quota/rate-limit fallback starts only after the previous attempt's
  record is gone. When it cannot be removed, the run fails with that reason
  instead of starting the fallback, so a restart can resume the first attempt
  and start the one fallback, never a second. Hands sites deploy
  `review_runner.py` (see `UPDATING.md`).

### Tests
- The installer contract test (bash and PowerShell) now proves the managed
  review-gates block is replaced on a re-run: the re-run must succeed, stale
  text seeded inside the block is gone, the block carries the shipped
  `agents/review-gates.md`, and text outside the markers survives
  (issue #9). Test-only; nothing installed changes.

## [1.26.2] - 2026-09-30

### Security
- Auto Reviews: testing an edited provider connection no longer sends its
  saved token to a caller-typed base URL. The saved token is reused only
  when the typed URL has the saved endpoint's origin (scheme, host, port;
  OpenAI's default endpoint when the connection has none). Another host, a
  switch to http or another port needs the token entered again, and the
  check says so. Since the app saves an edit only after this check passes,
  the saved token no longer follows a host change either. Hands sites deploy
  `canvas_discovery.py` (see `UPDATING.md`). Closes item 1 of issue #11.

## [1.26.1] - 2026-09-30

### Fixed
- `verify-delivery`'s patch identity is stricter (follow-ups from 1.26.0):
  - context lines now count, in order, as in `git patch-id --stable`. The
    same added line at another position in a file no longer matches, and a
    base update that changed the lines next to a hunk falls back to "not
    matched" (re-review), the conservative outcome. Index lines, hunk-header
    line numbers and line endings are still normalised away;
  - the diff's bytes are hashed as they are, never decoded, so two diffs
    that differ only in a byte that is not valid UTF-8 no longer collide;
  - every response is read with a bound: 8 MiB for a diff, 32 MiB for JSON
    (larger is pending) and 64 KiB of an HTTP error body, which was read in
    full before.
  Exit codes, the evidence format and GET-only behaviour are unchanged.

## [1.26.0] - 2026-09-30

### Changed
- A base-only update keeps reviews valid when the patch is unchanged. With
  several agents working on one repository, every merge puts the other open
  PRs behind their base, and updating a branch from its base produced a new
  head that forced a full external re-review and a WIP round-trip for a
  byte-identical change. The review gates (`agents/review-gates.md`, the
  block the installers write into agent instructions), the external review
  labels reference of `oh-code-review`, `AGENTS.md` and the aimem handbook
  now define the **patch identity** (`git diff $(git merge-base <base>
  <head>) <head> | git patch-id --stable`): when a base-only update leaves it
  unchanged, both reviews stay valid for the new head. The author posts a
  delta note naming both heads, both identities and the command, does not
  re-add `review-this`, keeps `hands-reviewed`, and the PR stays ready. Any
  change to the patch itself makes both reviews stale as before, and CI must
  still be green on the new head.

### Added
- `verify-delivery` accepts merged content by either rule, and reports which
  one matched (`rule` in the `tree_equality` check): `tree_equality` (the
  merge commit's tree equals the reviewed head's) or `patch_identity` (the
  merge commit's change against its first parent has the same patch identity
  as the reviewed head's change against its merge base). The diffs come from
  GitHub's compare API (diff media type) and Gitea's `.diff` endpoints; the
  identity is a documented canonical digest, the same on both forges. A diff
  that cannot be read or is over 8 MiB leaves the check pending, never
  passed. Exit codes, evidence and GET-only behaviour are unchanged.
- `verify-delivery` carries a review across a base-only update: with no
  review of the final head, the latest trusted review of an older head
  counts, and the merged change must then have that head's patch identity
  (read with GitHub's compare API). A later content change is therefore
  never covered. Gitea's API cannot diff an older head, so there such a
  review fails with "re-review at the final head". `reviewed_head` lists
  `reviewed_heads`, and each review says `at_final_head`.
- `verify-delivery` ignores a review comment whose `(?P<sha>...)` capture is
  not 7 to 40 hex digits (a custom `--review` pattern could otherwise match
  every head, and the value now goes into a compare request).

## [1.25.0] - 2026-09-29

### Added
- `verify-delivery` checks who posted a review, not only its text: on a
  public repository anyone can comment, so a comment in the review format
  naming the final head could stand in for a real review. The author is
  checked before the head and the verdict, so an untrusted comment never
  counts and never displaces a trusted one. `--review-author NAME=LOGIN`
  (repeatable) sets the accounts that may give review `NAME`; without it,
  GitHub accepts authors whose `author_association` is `OWNER`, `MEMBER` or
  `COLLABORATOR`, and on Gitea, which reports no association, the review
  cannot be satisfied (`not_confirmed`). `--trust-any-author` turns the check
  off and is reported as `"author_check": "disabled"`. Each comment ignored
  for its author is listed with the reason and the login. Exit codes,
  evidence format and GET-only behaviour are unchanged. **Gitea callers must
  now pass `--review-author` for each review** (or `--trust-any-author`).

### Changed
- The review-gates block (`agents/review-gates.md`) now states the pull
  request lifecycle: open as a draft with a `WIP: ` title prefix, mark ready
  and drop the prefix in the same step only when both pre-merge gates are
  green at the current head, go back to draft if fixes are needed after
  that, and never merge as an agent. The repository's `AGENTS.md` says the
  same for its own PRs.

## [1.24.0] - 2026-09-28

### Added
- New skill `verify-delivery` (written here, PolyForm Noncommercial 1.0.0):
  confirms from the forge, read-only, that a pull request was delivered. The
  required reviews are `READY_FOR_HUMAN_MERGE` at exactly the final head
  (defaults: the local `oh-code-review` pre-merge review and an external
  reviewer's `reviewed at head <sha>` comment; patterns and count
  configurable), the PR was merged by a person (bots and apps refused unless
  allowed; optional merger allowlist), the merge commit's tree equals the
  reviewed head's tree, and every check run (GitHub) or commit status (Gitea)
  on the merge commit succeeded; known-flaky failures are reported apart and
  still block. Its stdlib-only helper `verify_delivery.py` prints one JSON
  document with the verdict, per-check details and, only when confirmed, the
  evidence (`reviewed_head`, `human_merge`, `post_merge_ci`; at most 16
  entries of at most 512 bytes), and exits 0 confirmed, 3 not confirmed, 4
  pending or retryable, 2 usage error. The token comes from a file named by an
  environment variable and never appears in the output; redirects and
  next-page links to another host are never followed, and stop the run
  rather than yield a verdict from a partial listing. Tested offline against
  recorded GitHub and Gitea responses (`tests/test_verify_delivery.py`).

## [1.23.1] - 2026-09-27

### Fixed
- `.github/check-clients.py` URL-encodes the directory it passes to OpenCode
  v2's API; a `--workdir` whose path held a space or `&` stopped the run with
  `InvalidURL` before its summary. Covered by `tests/test_check_clients.py`.

### Changed
- The repository's reviewer guide says the 7-day rule for new dependency
  versions applies to what ships to users, not to the agent clients the client
  checks pin or take at `@latest`.

## [1.23.0] - 2026-09-27

### Added
- `.github/check-clients.py` and the Clients workflow: Claude Code, Codex,
  OpenCode 1.x and v2 and Gemini CLI are installed from npm and each lists
  what it discovered without a model call (Claude Code's init event, `codex
  debug prompt-input`, `opencode debug skill`/`debug config`, OpenCode v2's
  `/api/skill` and `/api/command`, `gemini skills list` and Gemini's memory
  discovery), after a user-level install, a Claude Code only install, a
  user-level install with `XDG_CONFIG_HOME` set, and a project install. Pull requests that change what gets installed run it with
  pinned client versions; a weekly run checks the latest release against each
  client's latest version.
- `--agents-md` reaches Gemini CLI: a project install gives `<repo>/GEMINI.md`
  an `@AGENTS.md` import (created with just the import if absent, as for
  `CLAUDE.md`), and a user install writes the block into `~/.gemini/GEMINI.md`
  when `~/.gemini` exists.

### Changed
- `--user --agents-md` writes the block into `~/.codex/AGENTS.md` whenever
  `~/.codex` exists, creating the file; it used to require the file to exist
  already, which a fresh Codex setup does not have, so Codex never saw the
  gates.
- The installers put OpenCode's user-level files (skills with `-t opencode`,
  prompts as commands, the gates block) in `$XDG_CONFIG_HOME/opencode` when
  `XDG_CONFIG_HOME` is set, which is where OpenCode 1.x and v2 read them; they
  always used `~/.config/opencode`, which OpenCode then ignored. Found by the
  first CI run of the client checks (runners set `XDG_CONFIG_HOME`).
- README: a per-client table with the versions checked; OpenCode v2's own
  install (`@opencode/cli`), its separate skill and command catalogs, and the
  note that 1.x and v2 cannot share a home directory.

## [1.22.1] - 2026-09-27

### Added
- The Release workflow runs from the Actions tab (or through the API) with a
  version: it checks `VERSION` and the `CHANGELOG.md` section on `main`, tags
  the tip of `main` and publishes, so a release needs no tag push from a
  workstation. The version must be a single `MAJOR.MINOR.PATCH` value (a
  multi-line input is refused before anything is written to the job's
  environment). Given an existing tag it publishes that tag, for a run that
  failed or was cancelled. The steps live in `.github/release.sh`, tested
  against scratch repositories by `tests/test_release.py`.

### Fixed
- Two overlapping release runs could mark the lower version latest: the run
  for the lower tag decided from the tags it had fetched when it started.
  Releases now run one at a time across all tags, and the latest decision
  re-reads the tags from the repository just before publishing.

## [1.22.0] - 2026-09-27

### Changed
- The one-line installers install the latest GitHub release by default instead
  of the tip of `main`, and fall back to `main` while the repository has no
  release. `AI_SKILLS_REF=<branch|tag|commit>` still picks any ref
  (`AI_SKILLS_REF=main` for unreleased work); with `AI_SKILLS_BASE` the default
  stays `main`. Only a clear "no release" answer (the redirect to `/releases`,
  or 404 from the API with `AI_SKILLS_TOKEN`) falls back to `main`; a failed or
  unexpected lookup stops the install, so a lookup error never installs
  unreleased work.
- `install.sh` trims trailing blank lines of the agent-instructions file with
  awk instead of GNU `sed -i`, which BSD sed on macOS reads differently.
- `install.sh` runs under macOS's bash 3.2: an empty tool or skill list no
  longer stops it with `unbound variable` before the intended message (bash
  before 4.4 treats an empty array as unset under `set -u`).
- The canonical repository is now `github.com/BlackVS/aiskills`, published as
  a fresh snapshot of 1.21.0. `boot.sh` and `boot.ps1` download the GitHub
  archive of `AI_SKILLS_REF` from `BlackVS/aiskills`; with
  `AI_SKILLS_TOKEN` they use the GitHub API tarball (a private fork), and
  `AI_SKILLS_BASE=<url>` keeps the Gitea archive download for a mirror.
- `consumers/openhands-review-hook/sites/` holds neutral examples
  (`site-a-gitea.json`, `site-a-github.json`, `site-b-gitea.json`); a
  deployment renders from its own site file kept in its private configuration.
- Receivers: `review_hook.py` requires `GITEA_API`, `GITEA_TOKEN_FILE` and
  `REVIEW_ORG`, and `github_review_poller.py` requires `GITHUB_TOKEN_FILE`;
  they no longer default to one site's values. A site that relied on those
  defaults sets them in `review.env` before deploying these modules; a
  missing one stops the service at start.

### Added
- GitHub CI (`.github/workflows/ci.yml`): the repository checks on Linux,
  Windows and macOS, plus the one-liners run against GitHub on all three (the
  release lookup, then the commit under test). Release workflow
  (`.github/workflows/release.yml`): a `v*` tag on a commit of `main` whose
  `VERSION` and `CHANGELOG.md` section match publishes a GitHub release with
  the changelog section as its body and `.tar.gz`/`.zip` archives with
  `SHA256SUMS`; only the highest version is marked latest, so a patch for an
  older line never becomes what the one-liners install. Importable rulesets in `.github/rulesets/` (main PR-only with
  required checks; release tags immutable), a PR template, Dependabot for the
  pinned actions, and `SECURITY.md`.
- `AGENTS.md` (repository conventions for coding agents), `NOTICE.md`,
  `LICENSE` (PolyForm Noncommercial 1.0.0) and `LICENSES/MIT-OpenHands.txt`
  (the upstream notice for the `oh-*` skills).

## [1.21.0] - 2026-09-24

### Added
- Combined mode, post-hoc switch check (issue #7 item 2): when a default-request
  review completes, the runner reads which LLM profile the conversation ended
  on (`agent.llm.usage_id`). A review written entirely on the reading profile
  is still labelled done, since it exists, but the PR gets a `note` comment
  naming the profile it was written on and saying that the signature names
  the primary model; the journal says `switch verified` or `review written
  off the primary`. The check is best-effort: a Canvas read or a note that
  fails is logged (`switch check incomplete`) and never changes the outcome.
  Single-profile reviews are not checked.
- Combined mode, the pair must match (issue #7 item 3): the conversation runs
  on the secondary's agent, so a primary whose agent settings differ beyond
  its LLM profile and the switch tool (tools, condenser, sub-agents, ...)
  would be silently ignored. Saving such a pair is refused with the differing
  fields named (the control server returns that message to the app), a saved
  pair that drifted is reported as a secondary problem (the default request
  fails with that reason), and the attempt log line
  shows `agent-settings=<reading profile>`. Pairs generated by the app always
  match.

### Fixed
- The 1.18.1 reading-profile check keyed on the wrong value: before any
  switch a conversation's `agent.llm.usage_id` is `default`, not the reading
  profile's name (observed on the reference site on 2026-09-24 with a
  conversation told not to switch), so the single-profile fallback after a
  limit on the reading profile never triggered. Both checks now ask whether
  the conversation has switched to the primary's LLM profile.

## [1.20.1] - 2026-09-24

### Fixed
- Receivers (issue #7 item 11): the forge request helpers of `review_hook.py`
  and `github_review_poller.py` follow `Link: rel="next"` pages, so the
  runner's watch and the restart recovery see every comment of a PR. GitHub
  pages issue comments at 30 by default (observed on a public issue on
  2026-09-24); a review posted beyond the first page timed out after the
  watch window. Gitea's per-issue comments endpoint returns every comment
  (1.26.4, no paging parameters), so nothing changes there. Adapter tests
  serve the review on page two only.

## [1.20.0] - 2026-09-24

### Added
- OpenCode v2 support in both installers. v2 reads instructions from
  `AGENTS.md` only (the global `~/.config/opencode/AGENTS.md`, then every
  `AGENTS.md` from the working directory up to home) and no longer falls back
  to `~/.claude/CLAUDE.md`, so `--user --agents-md` now writes the gates block
  into `~/.config/opencode/AGENTS.md` whenever `~/.config/opencode` exists,
  creating the file (before: only when the file already existed). Prompts
  install as OpenCode commands: the `opencode` tool copies them to
  `.opencode/commands/` or `~/.config/opencode/commands/` instead of a
  `prompts/` directory OpenCode never read, and a user-level `-p` run does the
  same when `~/.config/opencode` exists, like the Codex extras. Skill discovery
  is unchanged in v2 (`.opencode/skills`, `.claude/skills`, `.agents/skills`
  and their user-level twins). README: v2 rules (IDs, slash catalog,
  precedence, commands, instructions) with the documentation checked on
  2026-09-24. Installer tests for both cases.

## [1.19.0] - 2026-09-24

### Added
- `runtime/sdk_patch_files.py` (issue #7 item 13): the tool that applies the two
  tool-message patches (`toolname`, `threading`) to extracted copies of the
  SDK's `message.py` and `llm.py` now lives in the repository, site-neutral,
  with a `--check` mode that reports without writing. `test_sdk_patch.py`
  carries the unpatched excerpts of agent-canvas 1.20.0 (SDK 1.49.1) around both
  targets and checks patching, idempotence, the unrecognized-shape failure and
  the threading helper's ordering. Sites regenerate their mounts from the
  checkout instead of a local copy.

## [1.18.1] - 2026-09-24

### Fixed
- Runner: the quota fallback and the config-error reason read the conversation's
  error events through a server-side `kind=ConversationErrorEvent` filter that
  agent-canvas 1.20.0 answers with an empty page (`kind__eq=` is ignored too;
  observed on the reference site on 2026-09-24), so since that image every
  error-state review failed with the generic reason and no fallback. The runner
  now reads the newest 100 events unfiltered and picks the error events itself,
  by timestamp (the server's newest-first order is not strict).
- Runner, combined mode (issue #7 item 1): when the quota or rate limit hits the
  reading profile before the switch, the fallback runs single-profile on its own
  profile and the fallback note names the reading model; the conversation's
  `agent.llm.usage_id` (`profile:<name>`, observed on agent-canvas 1.20.0) tells
  the runner whether the switch had happened. A conversation that had switched,
  or a server without the field, keeps the previous behaviour.
- Runner (issue #7 item 14): the previous attempt's run record is removed before
  the next conversation is created, so a failed save of the fallback's record
  can no longer make a restart resume the first conversation and start a second
  fallback. Fault-injection test.
- Installers: the closing "Prompts" line names the directories the prompts were
  copied to; the source path only appears when nothing was copied. Through the
  one-line installers the source is a temporary download that is gone by then.
- Repository review guide (`.agents/skills/custom-codereview-guide/SKILL.md`)
  recording the runtime contracts observed on the reference site, so reviews do
  not re-derive them.

## [1.18.0] - 2026-09-24

### Added
- Receivers resume in-progress reviews across a service restart (issue #7,
  item 12). The runner records each run to `REVIEW_RUNS_DIR/review-runs-<forge>.json`
  once its Canvas conversation exists (repository, PR, head, choices, attempt,
  conversation id, start time, absolute deadline) and removes the record when
  the run ends either way; on start, `review-hook` and `github-review-poller` re-attach a
  watcher to every recorded run before the stale-label recovery, which skips
  those PRs. A resumed run keeps its head, comment window and deadline, and
  the single quota fallback still applies after a restart. Tests: runner unit
  cases and one restart case per receiver (the service is killed mid-review
  and started again against the fake forge). Restarting a receiver mid-run
  no longer loses the review; the idle rule now covers only the Canvas
  container itself.

## [1.17.0] - 2026-09-24

### Added
- One-line installers, no checkout needed: `boot.sh` (Linux, macOS; `curl | bash`)
  and `boot.ps1` (Windows; `irm | iex`). They fetch the archive of one ref
  (`AI_SKILLS_REF`, default `main`) from the Gitea into a temporary directory,
  run the real installer with the flags given (default `--user -s all -p -a`)
  and clean up; `AI_SKILLS_ARCHIVE` installs from a local archive, which the
  new `tests/test_boot.py` uses to exercise both scripts offline.

## [1.16.1] - 2026-09-24

### Fixed
- `install.ps1`: an explicit empty array (`-Tool @()`) is refused like `''` and
  `','`; the guard keys on whether `-Tool` was supplied, so only an omitted
  parameter gets the default set. Native PowerShell test case added.

## [1.16.0] - 2026-09-24

### Changed
- Installer (`install.sh`, `install.ps1`): the default tool set is now
  `claude,codex`, so a plain install reaches both Claude Code
  (`.claude/skills`) and Codex (`.agents/skills`), which read different
  directories. `codex` is a first-class tool name; `agents` remains as the
  older name for the same directory, and giving both copies once. Tool
  lists accept comma-separated values in both installers; re-runs report
  `replaced` versus `installed` per skill. Observed on a Linux workstation:
  the previous default installed only for Claude Code, Codex listed nothing,
  and the process tooling still reported the skill installed (follow-up
  recorded separately, ai-skills issue #20).

### Fixed
- `architecture-review`: the `description` contained an unquoted `: `, which
  is not strict YAML; Codex silently dropped the skill while Claude Code loaded
  it. Quoted. Verified: Codex 0.156 on Windows now lists all eight skills.

### Added
- `tests/test_install.py`: both installers on scratch repo and home (default
  set, `codex`/`agents` aliasing, re-run replaces and leaves others, user
  level, unknown tool refused). `tests/test_skill_frontmatter.py`: strict
  YAML, name and description limits for every shipped skill. README gains the
  platform table (Linux and Windows exercised, macOS expected) and the Codex
  discovery order with a verification command.

## [1.15.1] - 2026-09-22

### Added
- aimem process set: a bounded "Team work" section in the handbook (joined
  workers wait for addressed offers and never pull from the backlog, offers
  record the least-costly-suitable choice with rationale, evidence-backed
  asynchronous question resolution with sources and freshness, human
  escalation contents, expiry release through the hub's sequence, results
  reviewed at their candidate head) and one team-scoped item in each of the
  READY and DONE checklists, recorded as not applicable for unmanaged tasks.

## [1.15.0] - 2026-09-21

### Added
- An aimem process set with a bounded handbook, five-item READY/DONE checklists,
  task and investigation templates, and immutable-commit selection guidance.
  Review, token, decomposition and release rules follow current aimem policy.

## [1.14.10] - 2026-09-21

### Changed
- UPDATING: the Canvas image upgrade procedure (patch regeneration, throwaway
  rehearsal on a state copy with the list of checks, then the switch), as
  rehearsed and applied for agent-canvas 1.17.0 to 1.20.0 on one site's host.

## [1.14.9] - 2026-09-21

### Fixed
- Auto Reviews discovery helper: compatible with SDK 1.49 (agent-canvas 1.19
  and later). The agent server's persistence package no longer exports the
  profile and connection store accessors; the helper now falls back to
  constructing the stores itself under the server's persistence directory
  (SDK 1.49 builds the two profile stores there by default but not the
  provider-connection store, so every store gets its directory explicitly).
  SDK 1.46 keeps the old path. Rehearsed on a throwaway 1.20.0 container over
  a copy of the live state: profiles, connections, discovery, app endpoints,
  a tool call on the Astra profile (litellm 1.101 now keeps the plain effort
  and the extra-body copy agrees) and the integration test all pass.

## [1.14.8] - 2026-09-21

### Fixed
- Auto Reviews receivers: restart recovery no longer fails a run whose review
  comment was already posted for the current head; it swaps the labels and
  logs a recovered completion. Seen on 2026-09-21: a restart fourteen seconds
  after a posted review produced a misleading "could not run" note and
  dropped the labels on a GitHub PR. Runs still in progress are failed as
  before. The check reads only comments within one watch window.

## [1.14.7] - 2026-09-21

### Fixed
- Auto Reviews runner: a review whose model or endpoint rejects the request as
  configured (the platform's `LLMBadRequestError` / config classification,
  e.g. an effort the model does not accept) fails with that reason and points
  to the profile, instead of "entered error state". The endpoint's own error
  text stays out of the PR comment. Effort support is model-specific: on the
  site A endpoint `none` works for one model and not another, `minimal` for
  neither, so this is reported at review time rather than guessed at save time.

## [1.14.6] - 2026-09-21

### Fixed
- Auto Reviews runner: a review conversation that finishes without posting its
  comment fails the request at once, naming the conversation, instead of
  holding `hands-reviewing` for the whole 45-minute watch (seen on 2026-09-20
  when a posting command hung on an interactive prompt). One fresh look at the
  comments precedes the failure, so an agent that posts and then finishes is
  never mistaken for a miss.

## [1.14.5] - 2026-09-21

### Fixed
- Auto Reviews runtime: generated LLM profiles on a custom endpoint carry the
  reasoning effort in litellm's extra body as well. The 1.14.4 runtime check
  found that litellm's proxy provider drops the plain `reasoning_effort` field
  (it does not know the model as a reasoning model and `drop_params` is on),
  and site A's upstream refuses every tool call that arrives without an
  effort (gpt-6-astra: 400 on the first step; a plain message without tools
  passes). A variant saved without the body is not reused; re-save once.
- Consumer README: the example of the canonical reasoning-profiles wording now
  names the current site B pair (`astra-high` → `astra`), matching the site
  file and the drift guard since 1.14.2.

## [1.14.4] - 2026-09-21

### Fixed
- Auto Reviews runtime: generated LLM profiles address a custom endpoint (any
  connection or source profile with a base URL) as `litellm_proxy/<model>`;
  `openai/` stays for OpenAI itself. Reading is symmetric for both prefixes.
  A variant saved earlier under the other prefix is never reused or rewritten:
  the next save builds a suffixed fresh one (UPDATING has the one-time step).
  Closes the 1.14.3 To do. The runtime check (a full review through a proxied
  endpoint with a GPT-6 Astra pairing) runs after deploy and is recorded on
  the PR.

## [1.14.3] - 2026-09-21

### Fixed
- Docs (consumer README, UPDATING): saved LLM profiles behind a LiteLLM proxy
  must use the `litellm_proxy/` model prefix. With `openai/`, litellm's
  chat→Responses bridge reroutes tool+reasoning requests for cost-map-known
  models to `/responses`; site B lost every `gpt-6-astra` review to a 404
  until the two profiles were re-pointed. No code change; the site B
  file names the profiles, not the model strings.

## [1.14.2] - 2026-09-21

### Changed
- Site B: reasoning profiles `astra-high` → `astra` (GPT-6 Astra) replace
  `56sol-high` → `56sol` as the default reviewer pair; drift guard updated.

## [1.14.1] - 2026-09-20

### Changed
- Review gates (`agents/review-gates.md`, the managed block installed into the
  agent's global instructions): the pre-merge local review is **high** (one
  pass plus verification, no fan-out) with the external reviewer carrying the
  weight where deployed; re-reviews after fixes are high on the delta; `max`
  and `ultra` run only when the person asks, never by a gate. Measured on one
  day of work: a fan-out costs on the order of a million tokens per pass, the
  external reviewer found the blockers that mattered, and the fan-out's own
  catch (test adequacy) is within reach of a single pass. Also documented:
  never push to a PR while the external reviewer holds `hands-reviewing`.
  Re-run `install.sh -a` to refresh the block.

## [1.14.0] - 2026-09-20

### Changed
- Auto Reviews: profiles the app generates are named
  `review-<model>[-<effort>][-reading]` (`review-gpt-5-6-sol-max`,
  `review-gpt-5-6-sol-low-reading`) instead of `auto-review-<hash>`, so the
  Canvas conversation list, the service log and the settings file read
  plainly. The model id is slugged to the profile-name alphabet, a numeric
  suffix resolves a clash with any existing agent or LLM profile (never
  overwritten), and matching stays content-first, so profiles generated
  before this release keep their names and remain in use.
- Auto Reviews: a variant the app generated from an inline-key profile is
  attributed to that source profile (matched by endpoint and key) instead of
  to itself, so the provider list no longer offers generated variants as
  sources and re-saving the same selection reuses the variant instead of
  failing on a name clash or creating a duplicate.

### Fixed
- Auto Reviews: deleting a profile in Canvas that the review settings still
  name no longer breaks the app ("Could not load settings"). Reading the
  settings tolerates missing profiles, the control service reports per-role
  `problems`, the app (0.2.1) shows the role with a warning naming what is
  missing and waits for a replacement, an agent profile whose LLM profile is
  gone is listed with that fact, and a default-label review requested
  meanwhile fails with the reason for a broken primary or secondary (a broken
  fallback is skipped); the receivers no longer resolve the primary before the
  runner, so an unreadable file is reported on the PR too. An unreadable settings file likewise loads the tab with nothing
  selected and a warning; only an explicit Save replaces it. Nothing is
  deleted or rewritten by the runtime.

## [1.13.2] - 2026-09-20

### Fixed
- Auto Reviews runtime: the three service units set `PYTHONUNBUFFERED=1`, so
  `review trigger`/`review attempt`/`review done` lines reach the journal when
  they happen. Under systemd the receivers' stdout was block-buffered and a
  run's lines surfaced only when the next trigger flushed them, which made a
  running review look idle in `journalctl`.

## [1.13.1] - 2026-09-20

### Fixed
- Review prompt template: a "working notes" instruction (site-neutral, step 3).
  With condensation active the reviewer lost file contents at every summary and
  re-read the same files until the SDK's 500-iteration cap (site B, two
  runs on one PR: 855 tool calls, 62 summaries, one file read 58 times).
  The notes file survives summaries; the agent resumes from it instead of
  re-reading, reads in bounded ranges, and does not repeat a profile switch.

## [1.13.0] - 2026-09-20

### Added
- Auto Reviews (app 0.2.0, runtime): **reasoning effort per reviewer profile**
  and an optional **secondary reading profile** that turns on combined mode.
  Effort: API providers get it on the generated LLM profile
  (`reasoning_effort`: `none`/`low`/`medium`/`high`/`xhigh`/`max`, default
  `high`); the Codex account agent gets it in the ACP model id
  (`model/effort`, `low`–`xhigh`, the set the SDK maps to the
  `reasoning_effort` config option); the Claude account adapter takes none.
  Combined mode: when a secondary is set, the default-request review starts
  on the secondary's profile and the prompt carries the `REASONING_PROFILES`
  contract (`runtime/reasoning_profiles.py`, the same wording site B renders
  statically) naming the two generated LLM profiles, so the agent reads on
  the fast profile and switches to the primary — which signs the review —
  before forming the frozen scope. Requires API providers for both (only
  OpenHands-kind agents have `switch_llm`); explicit labels never use it; a
  quota fallback keeps the reading profile only for an API fallback.
- `canvas_discovery`: inventory rows carry `efforts` and `switchable`;
  `selections()` reports `effort` and `switch`; `prepare(selection, switch)`
  validates the effort per provider kind, hashes it into the generated
  profile name and enables the switch tool for a reading profile.
- `review_policy`: settings gain nullable `secondary` (a pre-1.13 file reads
  as `null`; the runner treats that as the old single-profile scheme);
  validation requires LLM-profile agents for primary and secondary when set.
- Tests: 11 backend cases (policy rules incl. the switch-tool requirement,
  legacy file, control PUT with a secondary and its 400 for an account agent
  on either side, runner combined mode, its fallback forms, an explicit label
  naming the primary, a shared LLM profile, an unstartable reading profile
  and a static site block, effort helpers and the site B drift guard),
  15 fixture checks (effort per profile, secondary gated to API providers,
  combined-mode save, reload restore, revert, unsaved marker, legacy server
  shapes) and the in-container integration test covers the effort written
  on the generated LLM profile, effort isolated from the switch flag, the
  switch-enabled reading profile, the Codex `model/effort` write and read
  paths, the inventory `efforts`/`switchable` seam and `selections()`.

### Fixed (pre-merge review)
- Combined mode is gated on the default request label; an explicit label
  naming the primary stays single-profile.
- `validate()` requires the secondary to carry the switch tool (a per-profile
  flag), not merely an LLM profile; ACP profiles never count as LLM-profile
  agents even if a stale `llm_profile_ref` is present.
- The settings file omits `secondary` while it is null, so an unchanged
  single-profile configuration stays readable by a pre-1.13 runtime; the
  rollback note explains what to do once a secondary was saved.
- `prepare()` reuses a profile only when its switch flag matches the request;
  the UI keys the reading-profile rule on the inventory's `switchable`; the
  control service normalizes efforts before its duplicate check; the runner
  degrades to single-profile when the reading profile cannot start and never
  appends a second block to a template that already carries one; effort
  pickers no longer escape the busy lock during discovery.
- Install docs list the seventh runtime module (`reasoning_profiles.py`).

## [1.12.1] - 2026-09-20

### Fixed
- `REASONING_PROFILES` contract: switch to the deep profile before the review
  pass, not before the verification stage; measured that the later switch
  point left every reasoning call on the fast profile.

## [1.12.0] - 2026-09-20

### Added
- `consumers/openhands-review-hook`: optional `REASONING_PROFILES` site
  variable. The universal review prompt can now tell an agent that has the
  OpenHands `switch_llm` tool to read on a fast LLM profile and switch to the
  deep one before the verification stage and the write-up. `render.py` drops
  the line when a site sets the value to `""`; `--check` requires the key.
  Sites: site B set (`56sol-high` → `56sol`), site A empty.

## [1.11.1] - 2026-09-18

### Fixed
- `external-review-labels.md` and the review-gates block: document the
  `hands-review-failed` end state (⚠️ comment + label, re-add `review-this`
  to retry) and that a long-running `hands-reviewing` review is not a failure.

### To do
- Automated compatibility check (no symlinks, LF endings, exec bits, name
  regex, description length, installer flag parity) runnable on both Linux
  and Windows, so the rules in README "Compatibility rules" are enforced
  rather than remembered.

## [1.11.0] - 2026-09-14

### Added
- Auto Reviews app 0.1.8 (from 0.1.5): per-provider **List models** and
  **Test connection** actions, **Edit** for API connections, and a discovery
  preflight before add/update so failed credentials are never saved (a blank
  token on edit keeps the existing one); compact unframed model rows; an
  in-memory session model cache (Reload clears it, Test connection refreshes
  that provider; rejected or empty lists are never cached).
- `runtime/review_control.py`: authenticated `POST
  /api/review-control/test-provider` — a read-only probe through discovery for
  a stored provider or an unsaved URL/key; returns generic classifications for
  401/403/404/429/redirects, never a provider body or credential.
- `runtime/canvas_discovery.py`: `probe()` and `server_environment()` — the
  helper reads the running agent server's cipher and storage configuration
  from its process environment inside the container, in memory only, and
  fails closed on a missing or ambiguous configuration.
- Backend regression cases for the above (28 total) and browser-fixture cases
  in `tests/test_extension.html`; `AUTO-REVIEWS.md` gains "Model sources and
  existing profiles" (coexistence of Canvas profiles, shared connections and
  account adapters; manual-chat models stay Canvas-configured by design).
- `UPDATING.md`: "Runtime code and the Canvas app" — receiver modules and the
  app are deployed from this repository and never edited in place; copy them
  as bytes.

### Fixed
- Provider discovery ran in a `docker exec` environment without the
  startup-generated `OH_SECRET_KEY`, so manually added encrypted tokens
  resolved to ciphertext and authentication failed (401) although the saved
  token was correct.
- `test_discovery.py`: the `server_environment` case is POSIX-only (it
  resolves container paths); skipped on Windows so `python -m unittest` passes
  on both platforms.

### Process
- This release recovers code that has run on site A's hands host since
  2026-09-14 04:33 UTC without a commit: `/opt/openhands/hooks` is not a
  checkout, and the work stayed uncommitted in a local worktree
  (`feat/provider-connection-checks`, based on the merged 340d0ef). The
  committed files are byte-identical to the deployed ones, and the host's
  pre-change backup equals 1.10.0 exactly, so nothing else diverged. The
  host's `review_hook.py` and `github_review_poller.py` had additionally lost
  the `⚠️` in their failure comment to a non-UTF-8 copy (`??????`); the repo
  copies are correct and were deployed back to that host as bytes, so every
  runtime module, unit file and app file there now `cmp`s identical to this
  commit.

## [1.10.0] - 2026-09-14

### Added
- `oh-code-review`: **Runtime-dependent findings** rule (SKILL.md, before the
  verification stage). A finding that rests on a fact outside the reviewed
  source (container UID/GID, runner/mount/user-namespace configuration,
  credential scope, platform/proxy/service behaviour, deployed state, pinned
  tool behaviour) is a hypothesis: name the assumption, define the smallest
  safe non-mutating reproducer at the real boundary, do not broaden
  production code to satisfy it, and dispose it OBSERVATION/FOLLOW_UP until
  reproduced. Motivated by a real case where a hypothetical CI-container UID
  problem that never occurs in the environment was "fixed" in production code.
- Verification labels `[CONFIRMED-RUNTIME]`, `[PLAUSIBLE-RUNTIME]`,
  `[REJECTED-RUNTIME]` beside `[CONFIRMED]`/`[PLAUSIBLE]`; a
  PLAUSIBLE-RUNTIME finding cannot be a BLOCKER unless leaving it unverified
  is itself a stated acceptance-criteria failure.
- Runtime-dependent findings carry `Assumption / Reproducer / Current
  evidence / Disposition` lines in the posted review; structured schema
  (`dispositions.md`, `fan-out.md`) gains `assumption`, `reproducer`,
  `current_evidence`, `unverified_is_criteria_failure`.
- `references/custom-codereview-guide.template.md`: template for a
  repository's `.agents/skills/custom-codereview-guide/SKILL.md` with an
  **Observed Runtime Contracts** table (image, routes, observed UID/mount
  behaviour, date and pipeline) and a rejected-hypotheses list.
- `agents/review-gates.md`: implementation-side rule — never harden
  production code for a PLAUSIBLE-RUNTIME finding; probe, then REJECTED with
  the observed contract recorded, or CONFIRMED-RUNTIME.
- `tests/test_dispositions.py`: six runtime-dependent cases (27 total).
- Auto Reviews Canvas App and portable review runtime: persistent primary and
  fallback provider/model selection with live discovery, shared Canvas provider
  connections, theme-aware controls, unsaved-change and loading feedback,
  one retry on a confirmed quota error, fixed-head
  review checks, authenticated settings API, and operator installation guide.

### Fixed
- Hands update instructions now distinguish legacy prompts loaded at startup
  from the new runtime's prompt reload on every review.
- `tests/__pycache__` is no longer tracked.

## [1.9.1] - 2026-09-13

### Added
- `consumers/openhands-review-hook/UPDATING.md`: operator checklist for a hands
  site after any ai-skills release (pull, `--check`, render per prompt file,
  refresh local skill copies, re-apply the gates block, verify with one PR),
  plus per-release notes and the site-plumbing facts encoded in `sites/*.json`.

## [1.9.0] - 2026-09-13

### Changed
- `consumers/openhands-review-hook/` is now **one universal prompt template**
  (`review_prompt.template.txt`) plus per-site variable files
  (`sites/*.json`: forge URL, hands URL, skill source, clone/fetch form, post
  command) and `render.py` (`--check` validates every site). Replaces the
  per-site prompt copies of 1.8.x and `review-steps.md`; nothing site-specific
  remains in the template. Runtime placeholders now include `{label}` on every
  site (the site B receiver passes it).

## [1.8.1] - 2026-09-13

### Fixed
- Site B's prompt file under `consumers/openhands-review-hook/sites/` had drifted from
  `review-steps.md` by one word ("CI YAML (Woodpecker)"); re-embedded verbatim.
- `consumers/openhands-review-hook/check.py`: verifies placeholders, steps 3–4
  identity and absence of pre-1.7 wording for every site prompt; run it before
  committing a change under `consumers/`.

## [1.8.0] - 2026-09-13

### Added
- `consumers/openhands-review-hook/`: the hands sites' hook **prompts** now live
  with the skill set — one file per site under `sites/` (site A on Gitea
  and GitHub, site B on Gitea) — plus `review-steps.md` (moved from `prompts/`)
  and a README with the receiver's placeholder/marker contract and the
  per-site differences. A skill release that changes what a hook prompt must
  say updates these in the same commit; sites copy their file to `PROMPT_FILE`.
  Site A's prompts here carry steps 3–4 for the 1.7 disposition model (the
  copies in its infrastructure repositories still name the removed taste/KEY
  INSIGHT format).

## [1.7.1] - 2026-09-13

### Added
- `prompts/label-review-steps.md`: the two review steps every hands hook prompt
  must carry (frozen scope + "use the skill's output format"), written to name
  no output sections so future skill releases need no prompt edits on any
  site. The hook prompts themselves are not part of this set (site A: its
  infrastructure repository; site B: the host);
  after 1.7.0 their step 4 still named the removed taste/KEY INSIGHT format.

## [1.7.0] - 2026-09-13

### Changed
- `oh-code-review` moves to a delivery-first (Scrum/Kanban) review model. The
  reviewer is the final Review lane for an already implemented, CI-green
  candidate and judges it against the PR's **frozen scope** (objective,
  acceptance criteria, non-goals, threat model); findings never expand that
  scope. Every finding carries exactly one **disposition** — `BLOCKER`,
  `CURRENT_SCOPE_IMPROVEMENT`, `FOLLOW_UP`, `OBSERVATION`, `REJECTED`,
  `RISK_ACCEPTED` — as a structured enum independent of severity. Only a
  BLOCKER (concrete failure path + named acceptance criterion + fixable in
  scope + unsafe if left) returns a PR to implementation.
- Output format replaced: CURRENT OBJECTIVE, then one section per disposition
  (always present, `- None` when empty), VERDICT (`READY_FOR_HUMAN_MERGE` /
  `RETURN_TO_IMPLEMENTATION` / `REVIEW_COULD_NOT_RUN`) computed from the
  BLOCKER count alone, and RISK reported separately. The taste rating, the
  CRITICAL/IMPROVEMENT/TESTING sections and "Worth merging / Needs rework" are
  gone. Forge `REQUEST_CHANGES` only for RETURN_TO_IMPLEMENTATION.
- Fan-out: sub-agent and specialist findings carry `disposition`,
  `acceptance_criterion`, `boundary`, `first_increment`; consolidation resolves
  duplicates toward the less blocking disposition and never escalates to
  BLOCKER on its own.
- Exact-head semantics: metadata-only triage, backlog creation, rejection with
  evidence and owner risk acceptance do not require a re-review; a changed
  source commit does.

### Added
- `references/dispositions.md`: the disposition definitions, BLOCKER test,
  evidence order, verdict rule, structured-finding schema and output format.
- `tests/test_dispositions.py` (21 tests, stdlib unittest): reference
  implementation of verdict, BLOCKER gating, conservative consolidation,
  specialist-disposition preservation, exact-head rules, forge-state mapping
  and rendered-output shape; keeps the reference text and any automation in
  step.
- `prompts/code-review.md` asks for the frozen scope up front.
- `agents/review-gates.md` (the managed block for CLAUDE.md/AGENTS.md) states the
  disposition rule for the agent *receiving* a review: only BLOCKERs return the
  PR to implementation; FOLLOW_UPs start separate tasks, never grow the current PR.

### Why
A naming-only PR (three renamed Woodpecker workflows) grew to 20+ files and
~800 lines through a review-fix-review loop in which every valid finding was
treated as a reason to change the current PR. The restored naming-only
candidate then passed CI and review with no issue. The findings were not
wrong; letting them redefine the task was.

## [1.6.6] - 2026-09-12

### Added
- Public-host hygiene for posted text, prompted by internal hub/backend names
  found in review comments on a public repo: `oh-code-review` (posting rules)
  and `oh-qa-changes` (Phase 4) now require scrubbing internal infrastructure
  identifiers — private hostnames/domains, hub/deployment names, backend
  binding names, token/credential names and paths — from anything posted to a
  PR, including quoted commands, logs, and evidence blocks; describe
  environments generically ("the primary hub", "a peer hub", "a named
  backend"). Same rule added to the review-gates managed block. The concrete
  identifier list lives in the user's private global instructions, never in
  skill text or posted output.

### Fixed
- `oh-code-review` scenario 7 (PR Description Evidence) inherited upstream
  text that *demanded* a conversation link as evidence, contradicting the
  posting rules on public repos: now the link is asked for only on private
  hosts where reviewers can open it, and on public repositories its absence is
  never a finding (the TESTING GAPS template line updated to match).

## [1.6.5] - 2026-09-12

### Fixed
- Posted reviews no longer carry session links. A session appended "Review
  generated with Claude Code · session: https://claude.ai/code/session_…" to a
  PR review comment on a public GitHub repo — nothing in the set asked for
  that; the session generalized the harness's commit/PR-description
  attribution. Now explicit in `oh-code-review` (POSTED REVIEW ATTRIBUTION)
  and the review-gates managed block: a posted review carries at most a
  one-line tool attribution, never a session or conversation URL.

## [1.6.4] - 2026-09-12

### Changed
- `external-review-labels.md`: on GitHub, hands now pre-creates the whole
  request-label set per polled repo (plain + one per profile), so a missing
  variant there means "not seen yet / no such profile", never "create it".

## [1.6.3] - 2026-09-12

### Changed
- External review guidance: the external pass should be a different model
  family than the local one, and site defaults are set that way (site A's
  hands now defaults to `codex-astra`, GPT) — so a Claude Code session uses
  plain `review-this`, and a variant is for the user's explicit choice or
  when the default would repeat the local family (e.g. a Codex-driven
  session asks for `review-this:claude-fable`). Managed block and
  `external-review-labels.md` (known-sites table) updated.

## [1.6.2] - 2026-09-11

### Added
- `oh-code-review/references/external-review-labels.md`: the external
  reviewer label protocol now lives in the skill docs (lifecycle, label
  semantics, re-trigger, watching, GitHub vs Gitea scope) including per-PR
  model selection with `review-this:<profile>` — and the rule that resolves
  the two kinds of deployment: read the site's label list; variant labels
  present → selection supported, only `review-this` → fixed reviewer, never
  create a variant label yourself. Known-sites table (site A hands:
  selectable; site B classic OpenHands: fixed). SKILL.md gains a
  "Requesting an external review" pointer; README documents the split.

### Changed
- `agents/review-gates.md`: the label bullets shrink to the short rules plus
  the site-resolution rule, and point at the reference for the rest.

## [1.6.1] - 2026-09-11

### Changed
- `agents/review-gates.md` (the installed `ai-skills:review-gates` block):
  hands' external review can now be requested on GitHub repos it polls, not
  only the Gitea org it covers, and the reviewer model is selectable per PR
  with the variant label `review-this:<profile>` (plain `review-this` = the
  server's default model). The review comment's signature names the model.
  Reinstall with `--user --agents-md` to refresh the managed block.

## [1.6.0] - 2026-09-11

### Added
- Codex CLI support, verified against the official docs (checked 2026-09-11):
  skills already reach Codex through `--tool agents` (Codex loads
  `.agents/skills/` in-repo and `~/.agents/skills/` user-level; `$` mentions a
  skill, `/skills` lists them). New installer behavior, both installers:
  - `--user --agents-md` also writes the review-gates block into
    `~/.codex/AGENTS.md` when that file exists (Codex's global instructions,
    no fallback to the Claude/OpenCode files) — same never-create rule as
    OpenCode's global `AGENTS.md`; project mode needs nothing because Codex
    natively reads `<repo>/AGENTS.md`.
  - `--user --prompts` also copies `prompts/` to `~/.codex/prompts/` when
    `~/.codex` exists; each file becomes a `/prompts:<name>` command (Codex
    marks custom prompts deprecated in favor of skills, but they work and fit
    our fill-in prompts).
- README: "Enabling in Codex CLI" section; Gemini CLI stays under "Other
  agents"; Codex added to the compatibility matrix.

### Fixed
- `install.ps1` rejected an unquoted comma list (`-Skills a,b`): PowerShell
  parses that as an array and the `[string]` parameter refused to bind.
  `-Skills` is now `[string[]]` and both forms are flattened, matching
  `install.sh`. Found while running the Codex headless test.

## [1.5.1] - 2026-09-10

### Fixed
- `install.ps1` `-AgentsMd` mojibake: PS 5.1 `Get-Content` defaults to the
  ANSI codepage, so reading the UTF-8 `agents/review-gates.md` (and the
  target file) corrupted em-dashes into `вЂ”` in `~/.claude/CLAUDE.md` /
  OpenCode `AGENTS.md`. All three reads now pass `-Encoding UTF8`; re-running
  the installer rewrites the managed block and heals previously corrupted
  installs.

## [1.5.0] - 2026-09-10

### Added
- `oh-qa-changes`, imported from upstream `qa-changes` @ 56671df: QA a PR by
  executing the software (four phases, PASS/FAIL report with before/after
  evidence). Adapted: report posting is host-agnostic (Gitea `tea comment`,
  GitHub `gh pr comment`, or in-session).
- `oh-learn-from-code-review`, imported from upstream `learn-from-code-review`
  @ 56671df: distill merged-PR review feedback into repo skills and reviewer
  guidelines. Adapted: Gitea-first (tea + /api/v1) alongside GitHub; output
  targets `.agents/skills/` and the `custom-codereview-guide` contract
  (`triggers: [/oh-codereview]`) so distilled corrections feed oh-code-review
  and the hands automated reviewer; designated AI reviewers (hands-bot) are
  included as signal instead of upstream's blanket bot exclusion.
- `prompts/qa-changes.md`, `prompts/learn-from-reviews.md`.
- `agents/review-gates.md`: the hands external-reviewer label workflow
  (`review-this` → `hands-reviewing` → `hands-reviewed`; state, not verdict;
  re-trigger semantics; watch obligation) and the optional execution-based QA
  gate — previously the managed block said nothing about either.

### Not adopted (evaluated 2026-09-10)
- upstream `upstream-fork-sync` (OpenHands-Cloud automation + GitHub-PAT
  shaped; our curated-copy sync stays manual per README), `iterate` (collides
  with the human-merge / serial-PR rules), `issue-duplicate-checker` plugin
  (Cloud/GitHub-bound).

## [1.4.1] - 2026-09-06

### Fixed
- README: the `--user --agents-md` bullet now says the installer never creates
  `~/.config/opencode/AGENTS.md` (a fresh OpenCode setup is skipped without a
  message) and shows how to create the file and re-run so the block lands there.

## [1.4.0] - 2026-09-06

### Added
- `agents/review-gates.md`: which `oh-code-review` level each gate uses
  (pre-push medium, pre-merge max, sensitive surfaces ultra, docs-only high),
  plus the no-self-escalation and single-reviewer rules.
- Installer option `--agents-md` (`-AgentsMd` in PowerShell) writing that file
  as a managed, idempotent block into `<repo>/AGENTS.md` (with an `@AGENTS.md`
  import ensured in `<repo>/CLAUDE.md`) or, with `--user`, into
  `~/.claude/CLAUDE.md` and, when present, `~/.config/opencode/AGENTS.md`.
- README "Review gates" section explaining the file placement per tool.

## [1.3.2] - 2026-09-06

### Fixed
- `oh-code-review` self-improvement footer (upstream text) still referred to the
  unprefixed `/codereview` trigger and a bare `custom-codereview-guide.md`
  file; it now names `/oh-codereview` and a proper skill directory
  (`.agents/skills/custom-codereview-guide/SKILL.md`).
- The footer is now required only when the review is posted as a PR/MR
  comment; interactive reviews get a one-line pointer instead of the
  GitHub-bot boilerplate (react with 👍/👎, re-request review).
- New "REPOSITORY REVIEW GUIDELINES" step: the reviewer reads the guideline
  skill explicitly from `.agents/`, `.claude/` or `.openhands/` skill dirs, so
  the footer's promise holds in Claude Code and OpenCode too (only OpenHands'
  hosted reviewer auto-loads it by trigger).

## [1.3.1] - 2026-09-06

### Fixed
- `oh-code-review` fan-out: OpenCode added as a sub-agent mechanism (Task tool,
  read-only `explore` subagent); it previously fell to the sequential fallback.
- Escalation from `medium` now only recommends `max`; the coordinator proceeds
  at `max` on its own solely when the user explicitly delegated the level, since
  `max`/`ultra` launch sub-agents and multiply model calls.
- "At most 4 concurrently" reworded to batches of 4 (no harness exposes a
  concurrency knob); `line` in the findings JSON may be null for file-level
  findings; levels documented as statable in prose where no slash command exists.

## [1.3.0] - 2026-09-06

### Added
- Review levels in `oh-code-review`: `/oh-codereview [low|medium|high|max|ultra]`
  (default `medium`, which is the unchanged single-pass behavior).
  - `high`+ adds a verification stage: findings are re-checked against the
    workspace and labeled `[CONFIRMED]`/`[PLAUSIBLE]`; unlike Claude Code's
    built-in review, credible-but-unverified findings are kept (labeled), only
    refuted ones are dropped — preserving the OpenHands-style breadth.
  - `max` adds per-file sub-agent fan-out, modeled on the OpenHands `pr-review`
    plugin (coordinator + file reviewers with a JSON findings contract,
    concurrency 4, escalation hint at 4+ files / 500+ changed lines), plus a
    coordinator cross-file pass.
  - `ultra` adds whole-diff specialist passes: security, test adequacy,
    cross-file data flow.
- `references/fan-out.md` — the multi-agent protocol for `max`/`ultra`,
  tool-agnostic (Claude Code Agent tool, OpenHands task delegation, or a
  sequential fallback when no sub-agent mechanism exists).

### Fixed
- `oh-code-review/README.md` listed the un-prefixed `/codereview` triggers;
  now matches the actual `/oh-codereview` frontmatter.

## [1.2.0] - 2026-09-05

### Added
- `oh-improve-agent-readiness` skill, imported from upstream
  `plugins/onboarding/skills/improve-agent-readiness` and renamed with the
  `oh-` prefix; turns a readiness report's gaps into ranked, repo-specific
  fixes.
- `prompts/improve-agent-readiness.md` ready-to-fill prompt for it.

### Fixed
- Restored `references/criteria.md` (the 74-feature scoring list) in
  `oh-agent-readiness-report` — upstream stores it as a symlink to a shared
  file and the symlink did not survive the copy to Windows, leaving the
  references directory empty. Both readiness skills now carry a real copy.
- Cross-references updated for the `oh-` renames: the readiness report's
  README now points to `oh-improve-agent-readiness` and lists its trigger with
  the prefix; `prompts/agent-readiness.md` uses the prefixed skill name;
  `oh-technical-writing`'s README marks `plain-english-content` as an
  upstream-only skill and links to it.
- README's upstream-sync instructions now diff against the correct upstream
  paths and explain the criteria.md symlink situation.

## [1.1.0] - 2026-09-05

### Added
- `install.ps1` — Windows PowerShell installer, feature-equivalent to
  `install.sh` (`-Tool`, `-Skills`, `-Prompts`, `-User`, `-DryRun`).
- README section for installing on Windows.

## [1.0.0] - 2026-09-05

### Added
- Initial set, copied from `OpenHands/extensions` @ `8d3919b` with the `oh-`
  prefix: `oh-code-review`, `oh-technical-writing`, `oh-code-simplifier`,
  `oh-agent-readiness-report`.
- `architecture-review` — local skill building on `oh-code-review` and
  `oh-technical-writing`.
- `prompts/` — ready-to-fill prompts replacing upstream's command stubs.
- `install.sh` — installer for Claude Code, OpenCode, OpenHands, and
  `.agents` (Codex / Gemini CLI) skill directories, project- or user-level.
