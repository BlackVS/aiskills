# Design proposal: `forge-gates`

Status: proposal for review, revision 2. Nothing is implemented yet. The
review is asked to judge the design and the contract below, not code.
Revision 2 takes option A, which the first review preferred, and closes the
three contract gaps it found: how a review of an older head is carried to the
current head, GitLab's state before the merge, and which host an automatic
credential belongs to. It also adds a separate wait selector and defined wait
outcomes.

## Problem

The review gates (`agents/review-gates.md`, the aimem process handbook,
`oh-code-review`'s `references/external-review-labels.md`) tell an agent to
"start a background watch for the external review" and to "verify the reviews
at exactly the final head", but they ship no executable for either. So every
agent writes its own watcher, and those watchers fail in small ways:

- One agent's wait script accepted only 8-character commit abbreviations. The
  reviewer's comment named a 7-character one, and the script failed.
- Another used a regular expression with `\[` inside `gh`'s jq `test()`, which
  that jq rejects. The watch never matched and ran until its own timeout.
- An anonymous poll of the GitHub API every 30 seconds hit the rate limit.
- A local review was posted with its verdict on one line
  (`**VERDICT:** READY...`). The repository's own delivery check needs
  `VERDICT` on its own line, so the review did not count, and nothing caught
  it before the merge.

`verify-delivery` already covers the last step (was this pull request
delivered?) for GitHub, Gitea and GitLab. Nothing covers the two earlier
steps: waiting for the external review of the head just pushed, and reading
whether the gates are green at the current head before marking a pull
request ready.

## Goals and non-goals

A skill `forge-gates` with one reviewed script and three subcommands, each
printing one JSON document and a one-line summary:

- `wait-review`: waits for the external reviewer's answer for a given head.
- `gates`: whether the current head is ready for a person to merge under the
  review-gate rules, with the reasons.
- `merge-check`: `verify-delivery`'s delivery verdict.

Non-goals: no labelling, commenting, pushing or merging (read and wait only);
no daemon or scheduler (one process per call); no replacement for an
orchestrator's own forge automation. Sharing code with `verify-delivery`
keeps the rules from drifting, but `gates` saying "ready" does not promise
delivery: the merged content, post-merge CI and edits made after the merge
are still checked by `merge-check`.

## Options

### A. On top of `verify-delivery` (chosen)

`skills/forge-gates/scripts/forge_gates.py` imports the `verify_delivery`
module from the sibling `verify-delivery` skill directory, which the
installers place next to it in every client's skills directory.

- **Shared:** the transport (`Http` and `Forge`: GET only, no redirects
  followed, bounded reads, one retry on a network error, rate limits reported
  as pending), comment parsing and the verdict pattern, author trust
  (`author_verdict`), the reading of reviews (`check_reviews`), the change
  identity (`patch_identity`), GitLab's diff rebuilding, and the whole
  delivery check.
- **New, in `forge_gates.py`:** the pre-merge adapters (draft state and CI at
  the head on each forge, GitLab's pipeline for the merge request), the
  carry-over of a review from an older head (below), the wait loop, and the
  credential lookup for the GitHub CLI.
- **Refactor of `verify_delivery.py`, with no change in behaviour:** the part
  of `main` that turns the checks into the verdict document and exit code
  becomes a function, `deliver(args, forge, kind, owner, repo, number)` →
  `(document, exit_code)`. `main` and `merge-check` both call it, so
  `merge-check` returns exactly the document `verify-delivery` prints. The
  existing `tests/test_verify_delivery.py` must pass unchanged.
- **Installer rule:** `install.sh` and `install.ps1` install `verify-delivery`
  whenever `forge-gates` is selected, including when it is named alone with
  `-s`. `verify-delivery` is not in the core set today. If the module is
  still missing at run time, the script stops with a usage error naming the
  skill to install, not a traceback.

### B. A separate script on `gh` and `tea` (not chosen)

It needs no dependency between skills, but it re-implements comment parsing,
the verdict rule, author trust, the change identity and the merge checks.
Those two copies would drift, and the abbreviated-SHA rule already differs. It
also depends on two external CLIs and their output formats, and it gets no
GitLab support.

### C. Inside `verify-delivery` (not chosen)

The import is trivial and there is no installer rule, but `verify-delivery`
stops being single-purpose. Its description would have to cover waiting and
pre-merge gates as well, which makes it harder to pick the right skill from
the description alone.

## Credentials

`gates` and `merge-check` take the same options as `verify-delivery`
(`--pr`, `--number`, `--forge`, `--api-base`, `--token-env`, `--anonymous`).
The token is chosen in this order, and the first rule that applies decides:

1. `--anonymous`: no token at all. The GitHub CLI is never asked.
2. `--token-env VAR`: the file named by `VAR`, as in `verify-delivery`.
3. The forge's default variable (`GITHUB_TOKEN_FILE`, `GITEA_TOKEN_FILE`,
   `GITLAB_TOKEN_FILE`), when it is set.
4. GitHub only: the GitHub CLI, for the host the request goes to.

For rule 4 the API base is resolved first, exactly as `verify-delivery`
resolves it. The host the token is asked for is then derived from that API
base: `https://api.github.com` means `github.com`, and `https://HOST/api/v3`
means `HOST`. Any other API base shape is a usage error for rule 4. When the
pull request is given as a URL, its host must be that same host; a mismatch
(for example a `--api-base` override that points elsewhere) is a usage error,
and no credential is read. The script runs
`gh auth token --hostname HOST` once, with a timeout and with no shell, and
keeps the token in memory only. A `gh` that is missing, not logged in to
`HOST` or failing gives a usage error that names the three other choices. The
token is then sent only to the API base's origin. `Http` never follows a
redirect, and `Forge.pages` refuses a next-page link to another host, so it
cannot leave that origin. Gitea and GitLab have no CLI rule: their CLIs keep
tokens per login in ways that cannot be tied to one host safely, so a token
file or `--anonymous` is required there.

## Reviews at the current head

`gates` reads the reviews with `check_reviews` for the current head and with
no merge time, so every review comment counts and the latest one of each
review supersedes earlier ones. The result for each configured review is then
one of these:

- **READY at the current head:** the review names the current head by its
  full SHA and its verdict is READY. On GitLab, an abbreviation also counts
  when it resolves uniquely, among every version head and every commit of the
  merge request, to the current head (`verify-delivery`'s rule).
- **Carried from an older head:** the latest review names an older head `H`
  by its full SHA and is READY, and the change of `H` is the same change as
  that of the current head (below). Its status is `ready` with
  `carried_from: H` and both identities.
- **Not ready:** anything else: the verdict is not READY; the review names
  the current head by an abbreviation on GitHub or Gitea (the reason says to
  name the full SHA); it names an older head by an abbreviation (it cannot be
  expanded safely); the two changes differ (`stale`); or the identity could
  not be read (`unknown`, with the reason).

**Comparing the changes.** The change of a head is its diff against its merge
base with the pull request's base branch, read from the forge's three-dot
compare. The base branch is named by its current tip SHA, read once per run.
The diff of the older head `H` and the diff of the current head are read the
same way:

- GitHub: `GET /compare/{base_tip}...{sha}` with the diff media type.
- Gitea: `GET /compare/{base_tip}...{sha}?output=diff`. That needs Gitea
  1.27. An older Gitea answers JSON or has no compare endpoint, and there a
  carried review is `unknown`, so the head is not ready: re-review at the
  current head.
- GitLab: `GET /repository/compare?from={base_tip}&to={sha}` (merge-base
  compare), rebuilt with `gitlab_diff`.

Both diffs go through `verify_delivery.patch_identity`, the same function the
delivery check uses. Equal digests mean the same change. A read that fails,
times out or is too large gives `unknown`, never `ready`.

**The identity is not `git patch-id`.** `agents/review-gates.md` tells a
person or agent with a clone to compare `git patch-id --stable` values in the
delta note. The forge digest is a different number: a SHA-256 over the diff's
file headers and every hunk line, context included, with only line numbers,
index lines and CRLF line endings dropped. It is never labelled a patch-id in
the output; the field is `change_identity`. It is the stricter of the two
(`git patch-id` also ignores whitespace), so where they disagree the forge
digest says "different" and the head is not ready, which only asks for a
re-review. `review-gates.md` is updated to say that a tool without a clone
may use the forge digest, and that the two numbers are never compared with
each other.

## Draft state and CI before the merge

`gates` reads the pull request itself on each forge, not the normalised
record `verify-delivery` reads for the delivery check:

| Forge | Draft | CI at the head |
| --- | --- | --- |
| GitHub | `draft` of the pull request | check runs `GET /commits/{head}/check-runs?filter=latest` and the combined status `GET /commits/{head}/status` |
| Gitea | `draft` when the API returns it; otherwise a title that starts with one of Gitea's default work-in-progress prefixes (`WIP:`, `[WIP]`), compared case-insensitively | statuses `GET /commits/{head}/status` |
| GitLab | `draft` (or `work_in_progress` on older servers) of the merge request | `head_pipeline` of the merge request, which is the pipeline of its source branch or of the merge request itself; it counts only when its `sha` is the head |

CI is green when at least one result exists for the head and every result has
passed. A running or queued result makes CI `pending`. No result at all is
`pending`. A failed one is `failed`. GitHub's pass set (`success`, `neutral`,
`skipped`) and Gitea's and GitLab's (`success`) are `verify-delivery`'s.
`merge-check` keeps the delivery check's own selection for post-merge CI (on
GitLab, the target branch's push pipeline at the merged commit).

`ready` is true only when the pull request is open, is not a draft, every
required review is `ready` (at the current head or carried), and CI at the
head is green.

## Waiting for the external review

    forge_gates.py wait-review --pr PR --head SHA [--wait-for NAME] [--timeout S] [--interval S]

- `--head` is required and must be the full 40-character SHA of the head that
  was pushed.
- `--wait-for NAME` selects which configured review to wait for (default
  `external`). `--review NAME=REGEX` keeps its `verify-delivery` meaning: it
  configures the reviews, and `--wait-for` must name one of them.
- `--timeout` defaults to 2700 seconds, the external reviewer's own watch
  window. `--interval` defaults to 60 seconds and cannot be below 30.

A reviewer comment answers for `--head` when the SHA it names is a prefix of
`--head`, 7 to 40 hexadecimal characters, compared case-insensitively. No
fixed length is ever assumed. Its author must pass the same trust rules
`gates` applies (`author_verdict`), so an untrusted comment never ends the
wait. An abbreviated match is reported (`abbreviated: true`) and says nothing
about readiness: `gates` decides that, by the rules above.

A failure comment of the reviewer ends the wait only when it is tied to this
request:

- its author passes the same trust rules as the review;
- it matches `--failure-pattern` (default: `could not run`);
- if it names a SHA, that SHA is a prefix of `--head`;
- it is newer than the latest time the request label was added, read from the
  issue's label events (GitHub and Gitea issue events, GitLab resource label
  events). When those cannot be read, the start of the wait is used instead,
  and the result says so (`correlation: "wait_start"`).

The wait ends with exactly one outcome:

| Outcome | When | Exit |
| --- | --- | --- |
| `review_found` | a trusted review comment answers for the head; the result carries its verdict, URL and `abbreviated` | 0 |
| `reviewer_failed` | a correlated failure comment | 3 |
| `head_moved` | the pull request's head is no longer `--head` (any review would be stale) | 3 |
| `closed` | closed without merging | 3 |
| `merged` | merged before a review of the head appeared | 3 |
| `timeout` | none of the above within `--timeout`; the last forge error, if any, is included | 4 |

A forge that is unavailable during the wait does not end it: the wait keeps
polling at the interval until the timeout, and reports the last error with the
timeout. A usage error exits with 2 before the wait starts.

## Output and exit codes

Each subcommand prints one JSON document on stdout and one summary line on
stderr. A token appears in neither; the output is masked against the token in
use, as in `verify-delivery`. Exit codes for `gates`: 0 ready, 3 not ready
(including `stale` and failed CI), 4 pending (CI running, an identity that
could not be read, an unavailable forge), 2 usage error. `merge-check`
returns `verify-delivery`'s codes. `wait-review` uses the table above.

## Tests

Standard library `unittest`, with recorded forge responses served through
`verify-delivery`'s opener hook (no network):

- `wait-review`: finds the review for a head given as 7, 8, 12 and 40
  characters; ignores a comment naming another head and an untrusted
  comment; ends on a correlated failure, but not on a failure that predates
  the latest request label; ends on a moved head, a closed and a merged pull
  request, and on a timeout (with an injected clock and sleep); keeps waiting
  through an unavailable forge.
- `gates`: a ready head; a stale review after a new commit; a base-only update
  with an equal change identity (carried); an identity that cannot be read
  (`unknown`, not ready); a review naming the head by an abbreviation (not
  ready on GitHub and Gitea, ready on GitLab when unique); a draft on each
  forge, including Gitea's title prefix; a GitLab merge request whose source
  pipeline passed and that has no target-branch pipeline; CI pending, failed
  and absent.
- Credentials: `--anonymous` never asks the GitHub CLI; a token file wins over
  the CLI; the CLI is asked for the host derived from the API base; a pull
  request URL on another host than the API base is a usage error with nothing
  read; a missing or logged-out `gh` is a named usage error.
- `merge-check`: equals `verify-delivery`'s output for the same recorded
  responses, including a merge whose tree differs from the reviewed head.
  `tests/test_verify_delivery.py` passes unchanged after the refactor.
- The installers: selecting `forge-gates` installs `verify-delivery` too; the
  script reports a missing sibling by name.

## Documentation

- The aimem handbook and `agents/review-gates.md` name the script as the way
  to wait for the external review and to check the gates, in place of a
  private watcher. `review-gates.md` also says what the forge change identity
  is and that it is never compared with a `git patch-id`.
- `references/external-review-labels.md` gets the prefix rule for waiting and
  the reason the gate needs the full SHA.
- `verify-delivery`'s SKILL.md gains a pointer to `forge-gates merge-check`.

## Questions for the review

1. Do the carry-over rule and the change identity close the stale-review gap
   without refusing a base-only update the gates allow?
2. Is the credential order, with the host derived from the API base and the
   URL host required to match, enough to keep an automatic token on its own
   host?
3. Are the pre-merge adapters (draft and CI per forge) complete, in
   particular GitLab's `head_pipeline` and Gitea's draft detection?
4. Are the wait outcomes and the failure correlation enough for an agent to
   recover without a person?
