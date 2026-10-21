---
name: forge-gates
description: Read a pull request's review gates from the forge (GitHub, Gitea or GitLab) instead of writing a watcher. wait-review waits for the external reviewer's answer for the head just pushed (any SHA abbreviation, trusted authors only, a correlated failure comment ends the wait); gates says whether the current head is ready for a person to merge (open, not a draft, the required reviews READY at the head or carried over a base-only update, CI green at the head); merge-check gives verify-delivery's delivery verdict. Read-only; JSON on stdout. Use when an agent must wait for the external review, or decide whether to mark a pull request ready.
---

# Forge gates

Use this skill for the two steps of the review gates that come before
delivery: waiting for the external reviewer after a push, and deciding whether
the current head is ready for a person to merge. Do not write your own watcher
for either: the rules here are the ones `verify-delivery` applies at the end,
from the same code.

The helper is read-only. Every request is an HTTP `GET`: it never labels,
comments, pushes or merges. Request the review (the `review-this` label), mark
the pull request ready and merge as the gates describe; this skill only reads.

It needs the `verify-delivery` skill next to it, in the same skills directory:
`scripts/forge_gates.py` imports its module. The installers add it whenever
`forge-gates` is selected. Without it the script stops with a usage error that
names the skill to install.

## Commands

    python3 scripts/forge_gates.py wait-review --pr PR --head SHA [--wait-for NAME] [--timeout S] [--interval S]
    python3 scripts/forge_gates.py gates --pr PR
    python3 scripts/forge_gates.py merge-check --pr PR

`PR` is a pull or merge request URL, `owner/repo#N`, or `owner/repo` with
`--number`. Each command also takes every option of `verify-delivery`
(`--forge`, `--api-base`, `--token-env`, `--anonymous`, `--review`,
`--required-reviews`, `--review-author`, `--trust-any-author`,
`--verdict-pattern`, `--known-flaky`, ...), with the same meaning; read that
skill's SKILL.md for them. Gitea needs `--api-base`, and Gitea and GitLab need
`--review-author NAME=LOGIN` for each review (they have no author
association).

Each command prints one JSON document on stdout and one summary line on
stderr.

### wait-review

Waits for the answer of one configured review (`--wait-for`, default
`external`) for `--head`, which must be the full 40-digit SHA you pushed.
Run it in the background right after adding the request label.

- A review comment answers for the head when the SHA it names is a prefix of
  the head, 7 to 40 hexadecimal digits, in any case. No length is assumed.
  Its author must pass the same trust rules as in `gates`; an untrusted
  comment never ends the wait.
- A failure comment of the reviewer (`--failure-pattern`, default
  `could not run`) ends the wait only when its author is trusted, it names
  the head or no SHA at all, and it is newer than the latest time the request
  label (`--request-label`, default `review-this`, `review-this:<profile>`
  included) was added, read from the label events. Where those cannot be
  read, the start of the wait is used, and the result says
  `"correlation": "wait_start"`.
- `--timeout` defaults to 2700 seconds (the reviewer's own watch window),
  `--interval` to 60 and never below 30. A forge that does not answer does
  not end the wait; the last error is reported with the timeout.

| `outcome` | When | Exit |
| --- | --- | --- |
| `review_found` | a trusted review names the head; `review` carries its `verdict`, `url` and `abbreviated` | 0 |
| `reviewer_failed` | a correlated failure comment (`failure`) | 3 |
| `head_moved` | the head is no longer `--head` (`current_head`): any review would be stale | 3 |
| `closed` / `merged` | closed, or merged, before a review of the head appeared | 3 |
| `timeout` | none of the above in time (`last_error`, if any) | 4 |

`review_found` says the reviewer answered, not that the head is ready: read the
verdict, act on the findings, and use `gates` for readiness. On
`reviewer_failed`, re-add the request label (never while `hands-reviewing` is
on) and wait again.

### gates

Whether the current head is ready for a person to merge. `status` is `ready`
(exit 0) only when every check passed:

- `state`: the pull request is open.
- `draft`: it is not a draft. GitHub and GitLab say so directly; Gitea too
  when its API has the field, otherwise a title starting with `WIP:` or
  `[WIP]` (any case) counts as a draft. An installation with its own prefixes
  replaces them with `--draft-prefix` (repeatable).
- `reviews`: the required reviews are `ready`. The latest trusted review that
  names the current head counts, even when a newer one names an older head; a
  newer review of the head always supersedes an earlier one. A review names
  the head by its full SHA; an abbreviation counts only on GitLab, when it
  matches the head alone among the merge request's versions and commits.
  Elsewhere such a review is `not_ready`, with the reason: name the full SHA.
  When no review names the head, the latest one of an older head counts only
  if it names that head by its full SHA and the change is the same: the diff
  of each head against its merge base with the base branch's current tip,
  read from the forge's three-dot compare, has the same `change_identity`.
  Then the review is `ready` with `carried_from`. A different change is
  `stale`; a diff that could not be read is `unknown`.
- `ci`: at least one CI result for the head, and every one passed. GitHub:
  check runs (latest) and commit statuses. Gitea: commit statuses. GitLab: the
  merge request's `head_pipeline`, counted only when it ran on the head itself,
  so a merged-results pipeline shows CI as pending with that reason. No
  result yet is `pending`.

Exit 3 (`not_ready`) when any check failed: closed or merged, a draft, a
review missing, not READY, `stale` or unsatisfiable, a CI failure. Exit 4
(`pending`) when nothing failed but something is still open: CI running or
absent, an `unknown` identity, a forge that did not answer.

`change_identity` is `verify-delivery`'s patch identity (a SHA-256 over the
diff's file headers and hunk lines, context included). It is not a
`git patch-id` and is never compared with one; it is the stricter of the two,
so where they disagree the head is not ready and a re-review settles it. On
Gitea the compare diff needs Gitea 1.27 (`?output=diff`); an older Gitea
gives `unknown` for a review of an older head: re-review at the current head.

### merge-check

Exactly the document and exit code `verify-delivery` produces for the same
options: use it after the merge, before recording the pull request as
delivered.

## Credentials

The first rule that applies gives the token:

1. `--anonymous`: none. The GitHub CLI is never asked.
2. `--token-env VAR`: the file named by `VAR`.
3. The forge's default variable, when set: `GITHUB_TOKEN_FILE`,
   `GITEA_TOKEN_FILE` or `GITLAB_TOKEN_FILE`, each naming a token file.
4. GitHub only: `gh auth token --hostname HOST`, for the host the API base
   belongs to (`https://api.github.com` is `github.com`,
   `https://HOST/api/v3` is `HOST`). A pull request URL must name that same
   host, or nothing is read. `gh` runs once, without a shell and with a
   timeout, and without `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`,
   `GITHUB_ENTERPRISE_TOKEN` and `GH_HOST` in its environment, so only a login
   stored for that host is used. To use a token from the environment, write it
   to a file and pass it by rule 2 or 3.

Gitea and GitLab need a token file or `--anonymous`. The token is kept in
memory, sent only to the API base's origin (no redirect is followed, and no
next-page link to another host), and masked in the output.

## Exit codes

| Command | 0 | 3 | 4 | 2 |
| --- | --- | --- | --- | --- |
| `wait-review` | review found | failed, moved, closed, merged | timeout | usage error |
| `gates` | ready | not ready | pending | usage error |
| `merge-check` | confirmed | not confirmed | pending | usage error |
