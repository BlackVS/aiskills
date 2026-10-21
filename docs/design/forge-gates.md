# Design proposal: `forge-gates`

Status: proposal for review. Nothing is implemented yet. The review is asked
to judge the choice between the options below and the contract of the
recommended one, not code.

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

## Goals

A skill `forge-gates` with one reviewed script and three subcommands, each
printing one JSON document and a one-line summary:

- `wait-review <pr> --head <sha> [--timeout S]`: waits until the external
  reviewer's comment for that head exists, or its "could not run" comment, or
  the timeout. It never adds or removes a label.
- `gates <pr>`: the current head, CI at that head, each configured review's
  verdict and the head it names, the patch identity of the change against its
  merge base, and whether the head is ready for a person to merge under the
  review-gate rules.
- `merge-check <pr>`: whether the merged content is the reviewed content, and
  post-merge CI.

Non-goals: no labelling, commenting, pushing or merging (read and wait only);
no daemon or scheduler (one process per call); no replacement for an
orchestrator's own forge automation.

## Options

### A. On top of `verify-delivery` (recommended)

`skills/forge-gates/scripts/forge_gates.py` imports the `verify_delivery`
module from the sibling `verify-delivery` skill directory, which the
installers place next to it in every client's skills directory.

- **Same rules, one place.** `gates` counts a review as READY with
  `verify_delivery.check_reviews`: the same comment formats, the same verdict
  pattern, the same author trust rules, the same handling of edits after the
  merge. `merge-check` is `verify_delivery.verify` and returns its verdict and
  evidence unchanged. Whatever `gates` calls READY, `verify-delivery` later
  confirms; they cannot drift apart.
- **Same transport.** The HTTP client is `verify_delivery.Http` and `Forge`:
  GET only, no redirects followed (so a token is never sent to another host),
  bounded reads, one retry on a network error, rate limits reported as
  "pending". The token comes from a file named by an environment variable, as
  in `verify-delivery`. New: on GitHub, when no token file is set, the script
  asks `gh auth token` once and keeps the token in memory only. This avoids
  both the anonymous rate limit and a token on the command line.
- **GitLab for free.** `GitLabForge` already reads merge requests, notes and
  pipelines, so all three subcommands work there too.
- **Cost.** The installers must install `verify-delivery` whenever
  `forge-gates` is selected (a small dependency rule in `install.sh` and
  `install.ps1`, tested). If the module is still missing, the script stops
  with a named error that says which skill to install, not a traceback.
  `verify_delivery.py` keeps its own CLI unchanged; it gains no new behaviour.

### B. A separate script on `gh` and `tea`

The task as first written: a standalone script that shells out to `gh`
(GitHub) and `tea` or the Gitea API.

- No dependency between skills.
- It re-implements comment parsing, the verdict rule, author trust, patch
  identity and the merge checks. Two copies of those rules will drift, and
  the first difference is already visible: see "Abbreviated SHAs" below.
- It depends on two external CLIs, their versions and their output formats
  (`gh`'s jq is the one that broke the watcher above). No GitLab.

### C. Inside `verify-delivery`

A second script in `skills/verify-delivery/`, with no new skill.

- The import is trivial and there is no installer dependency.
- `verify-delivery` stops being a single-purpose skill ("confirm delivery,
  read-only"); its SKILL.md and its trigger description grow to cover
  waiting and pre-merge gates, which makes it harder for an agent to pick
  the right skill from the description alone.

## Abbreviated SHAs

The two uses need different rules, and the proposal keeps them apart:

- **Waiting** (`wait-review`) knows the full head: the caller pushed it. A
  reviewer comment names that head when the name is a prefix of the full SHA,
  7 to 40 hexadecimal characters, compared case-insensitively. A fixed length
  is never assumed. A comment naming another head is not accepted. The result
  says whether the match was abbreviated.
- **Gating** (`gates`, `merge-check`) keeps `verify-delivery`'s rule: a review
  naming the final head by an abbreviation is not READY on GitHub and Gitea,
  because a commit made to share the abbreviation and force-pushed after the
  review would inherit it. GitLab, which lists every head a merge request
  had, is the exception, as it is in `verify-delivery` today.

So `wait-review` can report "the reviewer answered" for an abbreviated name,
while `gates` reports that review as `not_ready` with the reason, and the
agent then asks for a review naming the full SHA. The prefix rule goes into
`references/external-review-labels.md`, together with the reason the gate
needs the full SHA.

## Contract (option A)

Arguments follow `verify-delivery`: `--pr` takes a pull or merge request URL
or `owner/repo#N`; `--api-base`, `--forge`, `--token-env`, `--anonymous`,
`--review`, `--review-author` and `--verdict-pattern` mean what they mean
there.

`wait-review` adds `--head SHA` (required, the full 40-character SHA),
`--timeout` (default 2700 seconds, the external reviewer's own watch window),
`--interval` (default 60 seconds, at least 30) and `--review NAME`, the
review to wait for (default `external`). It polls the pull request's
comments, and returns when:

- a comment of that review names `--head` (verdict and comment URL in the
  result);
- a "could not run" comment newer than the start of the wait appears;
- the pull request's head moves away from `--head` (the review would be
  stale anyway);
- the pull request is closed or merged;
- the timeout passes.

`gates` returns `head`, `ci` (each check at the head: name, state, result),
`reviews` (as `verify-delivery`'s `reviewed_head` check, computed for the
current head with no merge time), `patch_identity` (the digest of the change
against its merge base, from the forge's compare diff, with the base and head
SHAs) and `ready`: true when every required review is READY at the current
head and CI is green there. `ready` is never true for a draft or a closed
pull request.

`merge-check` returns `verify-delivery`'s document unchanged.

Exit codes follow `verify-delivery`: 0 for done/ready/confirmed, 3 for
not ready or not confirmed, 4 for pending, a timeout or an unavailable forge,
2 for a usage error. Output is one JSON document on stdout; the summary line
goes to stderr. A token never appears in either.

## Tests

Standard library `unittest`, with recorded forge responses served through
`verify-delivery`'s opener hook (no network):

- `wait-review` finds the review for a head given as 7, 8, 12 and 40
  characters; ignores a comment naming another head; ends on a "could not run"
  comment, on a moved head and on a timeout (with an injected clock).
- `gates`: a READY head; a stale head after a new commit; a base-only update
  with an unchanged patch identity; a review naming the head by an
  abbreviation (not READY, with the reason); a draft.
- `merge-check`: equals `verify-delivery`'s output for the same recorded
  responses, including a merge whose tree differs from the reviewed head.
- The installers: selecting `forge-gates` installs `verify-delivery` too; the
  script reports a missing sibling by name.

## Documentation

- The aimem handbook and `agents/review-gates.md` name the script as the way
  to wait for the external review and to check the gates, in place of a
  private watcher.
- `references/external-review-labels.md` gets the prefix rule and the reason
  the gate needs the full SHA.
- `verify-delivery`'s SKILL.md is unchanged, apart from a pointer to
  `forge-gates merge-check`.

## Questions for the review

1. Is option A the right trade-off against B and C, given the installer
   dependency it brings?
2. Is it sound for `wait-review` to accept an abbreviated head while `gates`
   does not count it as READY?
3. Is the `gh auth token` fallback acceptable, given that the token is read
   once, kept in memory and never printed?
4. Anything missing from the contract for an agent that has only this skill,
   its forge and a task board?
