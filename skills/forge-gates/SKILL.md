---
name: forge-gates
description: Read a pull request's review gates from the forge (GitHub, Gitea or GitLab) instead of writing a watcher, and post the local review only in the form the delivery check reads. wait-review waits for the external reviewer's answer for the pushed head; gates says whether the current head is ready for a person to merge (open, not a draft, the required reviews READY at the head or carried over a base-only update, CI green); merge-check gives verify-delivery's verdict; post-review posts a review file as one new comment after checking its header, head, verdict layout and attribution; check-links finds session links in commit messages and a pull request description; install-hook adds a git pre-push hook that refuses a head without a clean pre-push review recorded by record-review. Read-only on the forge except post-review; JSON on stdout. Use to wait for the external review, post or record a review, check text before a push, or decide whether to mark a pull request ready.
---

# Forge gates

Use this skill for the two steps of the review gates that come before
delivery: waiting for the external reviewer after a push, and deciding whether
the current head is ready for a person to merge. Do not write your own watcher
for either: the rules here are the ones `verify-delivery` applies at the end,
from the same code.

The helper is read-only except `post-review`. Every other request is an HTTP
`GET`; `post-review` makes at most one `POST`, which creates one comment, and
never edits or deletes one. Nothing here labels, pushes or merges. Request the
review (the `review-this` label), mark the pull request ready and merge as the
gates describe.

It needs the `verify-delivery` skill next to it, in the same skills directory:
`scripts/forge_gates.py` imports its module. The installers add it whenever
`forge-gates` is selected. Without it the script stops with a usage error that
names the skill to install.

## Commands

    python3 scripts/forge_gates.py wait-review --pr PR --head SHA [--wait-for NAME] [--timeout S] [--interval S]
    python3 scripts/forge_gates.py gates --pr PR
    python3 scripts/forge_gates.py merge-check --pr PR
    python3 scripts/forge_gates.py post-review --pr PR --body-file FILE [--deny-file FILE] [--dry-run]
    python3 scripts/forge_gates.py check-links [--commits RANGE]... [--file FILE]...
    python3 scripts/forge_gates.py record-review --head SHA --level medium --verdict clean|blocker
    python3 scripts/forge_gates.py install-hook [--repo DIR] [--shared-hooks-path]
    python3 scripts/forge_gates.py uninstall-hook [--repo DIR]

`PR` is a pull or merge request URL, `owner/repo#N`, or `owner/repo` with
`--number`. Each command also takes every option of `verify-delivery`
(`--forge`, `--api-base`, `--forge-user`, `--token-env`, `--anonymous`, `--review`,
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
  the head or no SHA at all (the groups of a UUID, such as the conversation
  id a failure names, are no SHA), and it is newer than the latest time the request
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

### post-review

Posts the review in `--body-file` (UTF-8, posted exactly as it is) as one new
comment on the pull request, and only when the delivery check will read it.
Write the review to a file, run this, and never post it any other way. Every
check runs before anything is written, and each one is reported in `checks`
with the `line` it failed on:

- `header`: line 1 is the header of the review named by `--post-as` (default
  `local`), as the delivery check's pattern for it reads it: for the default,
  `[oh-code-review (high) review]`, `[oh-code-review high]` (or `max`,
  `ultra`) or `[oh-code-review review]`, then `reviewed at head` and the full
  40-digit SHA. An abbreviated SHA or another layout fails.
- `verdict`: exactly one line `VERDICT`, and the next non-blank line holds only
  `READY_FOR_HUMAN_MERGE`, `RETURN_TO_IMPLEMENTATION` or `REVIEW_COULD_NOT_RUN`.
  A line such as `VERDICT: READY_FOR_HUMAN_MERGE` or `**VERDICT:**` fails with
  its line number, and a READY verdict must also match `--verdict-pattern`.
- `attribution`: a line `Review: oh-code-review (<level>) via <client>`, at the
  header's level when the header names one.
- `session_links`: no link into an agent's session or conversation
  (claude.ai, chatgpt.com).
- `deny_file`: no entry of `--deny-file` appears, matched as text in any case
  (one entry per line; blank lines and `#` lines are skipped). Use it for
  private host and deployment names; the entries are never printed.
- `size`: at most 65536 characters.
- `state`: the pull request is open. A review posted after the merge is
  ignored by the delivery check, so it is refused.
- `head`: the header's SHA is the pull request's current head.

| `outcome` | When | Exit |
| --- | --- | --- |
| `posted` | every check passed and the comment was created; `comment.url` | 0 |
| `dry_run` | `--dry-run`: every check passed, nothing was posted | 0 |
| `refused` | a check failed; nothing was posted | 3 |
| `error` | the forge refused the comment or did not answer | 2 (token) or 4 |

The comment is never retried: when the forge does not answer, it may or may
not have been posted, and the error says to look at the pull request first. A
review that needs a correction is a new comment naming the head, never an edit.
Posting needs a token that may comment (the credential rules below);
`--anonymous` works only with `--dry-run`.

## Credentials

The first rule that applies gives the token:

1. `--anonymous`: none. Neither the GitHub CLI nor Git's credential helper
   is asked.
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
5. Gitea and GitLab: Git's credential helper, for the API base's host and
   the `--forge-user` given, exactly as `verify-delivery` reads it (one
   `git credential fill`, no shell, a timeout, no prompt).

So the usual setup stores the token once: on GitHub with `gh auth login`, on
Gitea and GitLab in the platform's credential store that `git push` over
https uses. Rules 2 and 3 remain for a token that lives in a file. With no
source, the script stops with a usage error that names them. The token is
kept in memory, sent only to the API base's origin (no redirect is followed, and no
next-page link to another host), and masked in the output.

### check-links

Reads, before a push, the text that becomes a git artifact for a link into
an agent's session or conversation: the messages of the commits in each
`--commits` range (as `git log` takes it, for example `origin/main..HEAD`,
run in the current directory) and each `--file` (UTF-8, such as a pull
request description). A line counts when it holds a session or conversation
URL of an agent client (the pattern `post-review` refuses) or starts with the
`Claude-Session:` trailer. A plain `Co-Authored-By:` line passes, and so does
the client's footer link to its own product page.
It needs no forge, token or pull request. Each hit is reported in `hits` by
its source (`commit <sha>` or `file <path>`) and line, never by its text.
`outcome` is `clean` (exit 0) or `refused` (exit 3). No `--commits` and no
`--file`, a range that starts with `-`, a range git does not know, or an
unreadable file is a usage error (exit 2). A commit in the ranges whose push
skipped the pre-push review gate is listed in `skips` (head, reason, time);
it does not change the outcome, and the next review names it.

### The pre-push review gate

Makes git refuse a push whose head has no clean pre-push review recorded, so
the order "review, then push" no longer rests on memory alone. It needs no
forge, token or pull request, and nothing it writes is committed or pushed.

- **`install-hook`** writes `pre-push` into the hooks directory git uses for
  the repository (`--repo`, default the current one), `core.hooksPath`
  included. The hook names this Python and this script by absolute path, so
  it runs where git's own shell has no `python3`, as on Windows. It never
  replaces a pre-push hook it did not write: it refuses and names the file.
  A `core.hooksPath` from the global or system git configuration serves
  every repository of the machine, so the hook goes there only with
  `--shared-hooks-path`. Run it again after the skill moved; if the script
  is gone, the hook refuses every push and says how to reinstall or remove
  it.
- **`uninstall-hook`** removes only the hook `install-hook` wrote.
- **`record-review --head SHA --level LEVEL --verdict clean|blocker`** is the
  last step of the pre-push review. It writes `<sha>.json` under
  `aiskills/pre-push/` in the repository's common git directory, which every
  worktree shares and `git status` never shows. The record binds to one
  commit: after an amend, a rebase or an update from the base, the new head
  is reviewed and recorded again. An agent records its own review, so the
  record keeps the order, not proof that the review was done well.
- **`pre-push REMOTE [URL]`** is what the hook runs, with git's lines on
  stdin. For each pushed branch whose head adds commits the remote lacks,
  the head needs a record at level medium or above with the verdict
  `clean`, and the new commits' messages must carry no session link (as
  `check-links`). Deletions, tags and pushes that add no commit (a new
  branch at a commit the remote holds) are exempt. A refusal names the head
  and the exact `record-review` command. Exit 0 lets the push go, 3 refuses
  it.

**The one-time skip is a person's, never an agent's.**
`AISKILLS_PRE_PUSH_SKIP="<reason>" git push ...` lets that one push through
without a record. The hook prints `pre-push review gate SKIPPED for <sha>:
<reason>` and writes `<sha>.skip.json`, which later `pre-push` and
`check-links` runs over that commit report, so the next review names it.
Give the variable inline on the push, never `export` it: an exported value
would skip every later push from that shell. An empty value skips nothing,
and a session link is refused even then. `git push --no-verify` bypasses
every hook without a trace; it is not a supported way past the gate.

## Exit codes

| Command | 0 | 3 | 4 | 2 |
| --- | --- | --- | --- | --- |
| `wait-review` | review found | failed, moved, closed, merged | timeout | usage error |
| `gates` | ready | not ready | pending | usage error |
| `merge-check` | confirmed | not confirmed | pending | usage error |
| `post-review` | posted, or would post (`--dry-run`) | refused | the forge refused or did not answer | usage error, or the token may not comment |
| `check-links` | clean | a session link found | (none) | usage error |
| `record-review` | recorded | (none) | (none) | usage error (not a commit of the repository) |
| `pre-push` | the push may go | refused: a head without a clean record, or a session link | (none) | usage error |
| `install-hook`, `uninstall-hook` | installed, updated, removed, or absent | refused: a hook it did not write, or a shared hooks path without the flag | (none) | usage error |
