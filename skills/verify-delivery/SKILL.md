---
name: verify-delivery
description: Confirm from the forge (GitHub, Gitea or GitLab) that a pull request (a GitLab merge request) was actually delivered, before recording it as done. Checks that the required reviews are READY_FOR_HUMAN_MERGE at exactly the final head, that the pull request was merged by a person, that the merged content is the reviewed content (the same tree, or the same patch identity after a base-only update), and that CI on the merge commit is green. Read-only; returns JSON with a verdict and, only when confirmed, the evidence to record. Use when a coordinator or any agent must decide whether a PR counts as delivered.
---

# Verify delivery

Use this skill when you have to record a pull request as **delivered**: before
closing a task, reporting a result, or writing evidence into a team system.
It asks the forge itself, because only the forge knows what was reviewed,
what was merged and what CI said afterwards. A statement in a chat, a task
note or a PR description is not evidence.

The helper is read-only. Every request it makes is an HTTP `GET`; it never
merges, comments, labels or pushes. Do not add any of that around it: this
skill confirms, it does not act.

## What it checks

All four must pass for the verdict `confirmed`:

1. **Reviewed head.** The PR's final head commit is taken from the forge.
   The required reviews must be `READY_FOR_HUMAN_MERGE`: the latest review
   comment that names the final head counts, and one posted or edited after
   the merge does not. Only comments on the pull request count: a formal
   pull-request review is listed under `ignored`, because its body can be
   edited without a recorded edit time (GitHub's API gives it none). With no review of the final head, the latest one of an
   older head counts instead, but only as far as check 3 finds the merged
   change to be that head's change (a base-only update after the review keeps
   it valid; any other later change does not). On Gitea the link is read
   from the compare API with `?output=diff`, which Gitea serves from 1.27; an
   older Gitea answers JSON (or, before 1.22, has no compare endpoint), and
   there a review of an older head fails: re-review at the final head. `reviewed_heads` lists the heads the READY reviews name.
   An older head must be named by its full 40-digit SHA on GitHub and Gitea:
   an abbreviated one would resolve to a branch or tag of that name first,
   and their commit lists lose a head that was force-pushed away, so a commit
   made to share the abbreviation could stand alone there. The check then
   fails: name the full SHA, or re-review at the final head. On GitLab an
   abbreviated SHA is expanded to the head of a version (GitLab keeps one for
   each push while the merge request is open, a force-push included), and
   only when nothing else among the versions and the commits matches it; no
   match, more than one, or no versions list fails the same way.
   On GitLab the comments are the merge request's notes; system notes are
   skipped, and approvals carry no text, so they are not reviews. Resolving a
   resolvable note after the merge moves its `updated_at`, so it then reads as
   edited after the merge: a false "not confirmed", never a false confirm.
   Reviews are recognised by their text **and by an author the repository
   trusts**; the author is checked first, so an untrusted comment never
   counts and never displaces a trusted one, even when it is newer. By default
   two reviews are required, both in the `oh-code-review` comment format
   (`[<reviewer> review] reviewed at head <sha>`, then `VERDICT` /
   `READY_FOR_HUMAN_MERGE`):
   - `local`: the pre-merge `oh-code-review` review at level high (or max,
     ultra);
   - `external`: any other reviewer's comment in that format.

   Who counts as trusted: the accounts you list for that review with
   `--review-author NAME=LOGIN`; without a list, on GitHub, an author whose
   `author_association` is `OWNER`, `MEMBER` or `COLLABORATOR` (anything else,
   or none reported, is ignored with the reason). Gitea reports no
   association, and neither does GitLab, so there each review needs a
   `--review-author` list or it cannot be satisfied (the result is
   `not_confirmed`). `--trust-any-author`
   turns the check off, for private repositories where only trusted accounts
   can comment; the output then says `"author_check": "disabled"`. The forge
   does not report who edited a comment, so the check applies to its author.
   Logins match case-insensitively. A reviewer that posts as an app or bot
   account usually has no trusted association on GitHub: list it.
2. **Merge.** The PR is merged; the merge commit and the account that
   merged it are recorded. By default that account must be a person (a
   GitHub `User`, not a `Bot`). Gitea has no bot flag: its system accounts
   count as bots, and you name any other bot accounts with `--bot-account`.
   On GitLab the account is the merge request's `merge_user`, and its type
   comes from the `bot` flag of its user record (a project or group access
   token's account is a bot). That flag needs a token; without it the type is
   `unknown`, which is not a person. GitLab's merged commit is the merge
   commit, else the squash commit (a fast-forward squash), else the head itself
   (a fast-forward merge, also when GitLab reports the head as the merge
   commit); `merged_as` says which.
   `--merger NAME` restricts who may merge; `--allow-bot-merge` accepts bots.
3. **Merged content = reviewed content** (the `tree_equality` check), for
   every head a READY review names (`reviewed` lists each with its result).
   One of two rules must match, and the check's `rule` says which:
   - `tree_equality`: the merge commit's tree equals the reviewed head's
     tree. This holds for merge, squash and rebase merges of an up-to-date
     branch, and needs no diff.
   - `patch_identity`: the trees differ because the base moved after the
     review (a base-only update, or a squash onto a newer base), but the
     merge commit's change against its first parent has the same patch
     identity as the reviewed head's change against its merge base. The
     reviewed patch on a newer base is the same change. The diffs are read
     on GitHub from the compare API with the diff media type
     (`<first parent>...<head>` and `<first parent>...<merge commit>`), and
     on Gitea from `pulls/{index}.diff` and `git/commits/{merge}.diff` for the
     final head, and from the same two compares with `?output=diff` for an
     older head (Gitea 1.27 or later).

   On GitLab commits carry no tree id, so `tree_equality` holds when a
   straight compare of the head and the merged commit has no diffs
   (`tree_basis` says so). The diffs are rebuilt from GitLab's three-dot
   compare (`repository/compare?from=…&to=…&unidiff=true`): each file's hunks
   under the `diff --git`, mode and rename lines git writes, with paths quoted
   as git quotes them, which gives the
   same patch identity as GitLab's raw diff of the merge request (checked on
   gitlab.com). A fast-forward compares from the merge request's base
   (`diff_refs.base_sha`) instead of the head's first parent; one without a
   recorded base fails unless the reviewed head is the merged head. GitLab sends a
   binary file as a "Binary files … differ" line, as git does, so it has no
   identity. A file it collapsed, found too large or sent without text, a
   change past its diff limits (`compare_timeout`), diff text in which it
   replaced bytes that are not UTF-8, and a path with a control character (a
   newline included) or replaced bytes have none either, nor, before GitLab
   18.4 (which first reports `too_large`), a renamed file sent without text:
   the check fails.

   A rebase merge of several commits reports its last rebased commit as the
   merge commit, so that commit's change alone never matches. When it does
   not, and the merge is a rebase merge of the pull request, the merged
   change is read from the base the rebase landed on instead
   (`<base>...<merge commit>`; on Gitea a compare with `?output=diff`, so
   1.27 or later), and the reviewed change is read again from that base
   (`<base>...<reviewed head>` on GitHub, and on Gitea for an older head).
   The check reports that base as `base_parent_sha` and the number of
   commits as `rebased_commits`. It counts as a rebase merge when the pull
   request's commits form one line of 2 to 100 commits from its head, and as
   many commits ending at the merge commit each have one parent and carry the
   same commit messages in the same order; anything else is compared by its
   first parent only. When the forge is unavailable for the moment while the
   pull request's commits or the rebased commits are read, the check is
   `pending` (retry later), not `failed`, even though the first-parent change
   did not match. A commit list or commit the forge does not find (404) means
   the merge is not a rebase merge, and the first-parent result stands.

   The patch identity is a SHA-256 over a canonical form of the diff, the
   same function for every forge (not byte-compatible with `git patch-id`):
   each `diff --git` line, the mode, new/deleted file and rename/copy lines,
   and every hunk line (context, added, removed and `\ No newline`) are kept
   in order; index lines, the `---`/`+++` lines, hunk headers with their line
   numbers, similarity scores and blank separator lines are dropped. Only
   CRLF line endings are normalised: the bytes are hashed as they are, never
   decoded, so a file that is not UTF-8 keeps every byte. GitLab's API does
   not return such bytes as they are: it re-encodes them, drops them or
   replaces them. Only a replacement (U+FFFD) is detected and refused, so on
   GitLab two changes that differ only in such bytes can share an identity.
   Because context
   counts, the same line added elsewhere in a file does not match, and a
   base update that changed the lines next to a hunk does not match either
   (the conservative outcome: re-review). A diff that cannot be read, or is
   larger than 8 MiB, leaves the check `pending`, never passed. Every
   response is read with a bound (32 MiB for JSON, 64 KiB of an error
   body). A binary or empty change, or a merge
   commit without a parent, has no identity and fails.
4. **Post-merge CI.** Every check run (GitHub) or commit status (Gitea) on
   the merge commit completed successfully (`skipped` and `neutral` pass on
   GitHub; on Gitea only `success` passes, `pending` is pending, and `error`,
   `failure` and `warning` fail). Still running means pending. On GitHub only
   check runs are read: CI that reports solely through the older commit-status
   API shows up as no CI yet, so the result stays pending. On GitLab only the
   latest pipeline on the merged commit for the target branch with source
   `push` counts (a branch or scheduled pipeline on the same commit does not):
   only `success` passes. `manual` and anything still queued, running or
   canceling is pending, and `failed`, `canceled` and `skipped` fail: a skipped pipeline ran
   no job (a `[skip ci]` in the merge commit's message is enough). A failure fails. A failing check you
   list with `--known-flaky` is reported separately as `failed: known flaky`,
   and the delivery is still **not confirmed**.

## How to call it

The helper is `verify_delivery.py` next to this file (Python 3, standard
library only). Give it the token as a **file** named by an environment
variable, never on the command line:

```bash
GITHUB_TOKEN_FILE=/path/to/read-only-token \
  python3 <skill-dir>/verify_delivery.py --pr https://github.com/OWNER/REPO/pull/123

GITEA_TOKEN_FILE=/path/to/read-only-token \
  python3 <skill-dir>/verify_delivery.py --pr https://git.example.org/OWNER/REPO/pulls/45 \
  --api-base https://git.example.org/api/v1 \
  --review-author local=LOCAL_REVIEWER --review-author external=EXTERNAL_REVIEWER

GITLAB_TOKEN_FILE=/path/to/read-only-token \
  python3 <skill-dir>/verify_delivery.py --pr https://gitlab.example.org/GROUP/REPO/-/merge_requests/67 \
  --review-author local=LOCAL_REVIEWER --review-author external=EXTERNAL_REVIEWER
```

On Windows run it with `python` or `py -3`. The token needs read access to
pull requests, comments, commits and checks (or statuses) only; on GitLab a
token with `read_api` (a project access token with the Reporter role is
enough) is sent as a Bearer token. Other references: `--pr OWNER/REPO#123`,
or `--pr OWNER/REPO --number 123`; on GitLab `--forge gitlab` with
`--pr 'GROUP/SUBGROUP/REPO!67'` (quoted: `!` is special to an interactive
shell) and `--api-base`; `#N` is refused there, since on GitLab it names an
issue. `--forge gitea` when it
cannot be told from the URL; `--api-base` for GitHub Enterprise
(`https://HOST/api/v3` is assumed from a URL), for a GitLab not served at
`https://HOST/api/v4` (under a relative URL root, give
`https://HOST/ROOT/api/v4`: `ROOT` is then not read as part of the
namespace), and always for Gitea, over https whenever a token is used. GitLab
projects are addressed by their URL-encoded path (`GROUP%2FREPO`); `--token-env NAME` for a different variable; `--anonymous` for a
public repository without a token.

Options for the checks:

| Option | Default | Meaning |
| --- | --- | --- |
| `--review NAME=REGEX` (repeatable) | `local` and `external` as above | A required review. The regex must have a `(?P<sha>...)` group capturing the head it names (7 to 40 hex digits). Giving any `--review` replaces the defaults. |
| `--required-reviews N` | all of them | How many of the reviews must be READY. |
| `--review-author NAME=LOGIN` (repeatable) | GitHub: `OWNER`, `MEMBER`, `COLLABORATOR`; Gitea and GitLab: none (required) | An account whose comments may give review `NAME`. A list for a review replaces the association default for it, in both directions. |
| `--trust-any-author` | off | Accept a review from any author. Only where just trusted accounts can comment; reported as `"author_check": "disabled"`. Not combined with `--review-author`. |
| `--verdict-pattern REGEX` | a `VERDICT` line followed by `READY_FOR_HUMAN_MERGE` | What makes a review READY. |
| `--merger NAME` (repeatable) | anyone | Allowlist of accounts that may merge. Compared case-insensitively. |
| `--bot-account NAME` (repeatable) | none | Treat this account as a bot. Compared case-insensitively. |
| `--allow-bot-merge` | off | Accept a merge by a bot or app. |
| `--known-flaky NAME` (repeatable) | none | Report this check's failure as `failed: known flaky` (still not confirmed). |

## Reading the result

One JSON document on stdout:

- `verdict`: `confirmed`, `not_confirmed` or `pending`.
- `checks`: one entry per check (`reviewed_head`, `merge`, `tree_equality`,
  `post_merge_ci`) with its `status` (`passed`, `failed`, `pending`) and the
  details: SHAs, URLs, the reviews found (with the author rule that applied)
  and the comments ignored with the reason and, for an author reason, the
  login; `reviewed_head` also says `author_check` (`enabled` or `disabled`).
  Who merged and their account type, each CI run with its result.
- `evidence`: present **only** when the verdict is `confirmed`: a list of
  `{kind, ref}` to record as they are, with at most 16 entries, each ref at
  most 512 bytes:
  - `reviewed_head`: the reviewed head commit's URL;
  - `human_merge`: the pull (or merge) request's URL;
  - `post_merge_ci`: the CI run URLs (on GitLab the pipeline's; the merge
    commit's URL when there are too many runs to list).
- `error`: present when the run could not complete, with its `kind`
  (`usage`, `unavailable`, `not_found`).

| Exit code | Verdict | What to do |
| --- | --- | --- |
| 0 | `confirmed` | Record the `evidence` entries. |
| 3 | `not_confirmed` | Do not record delivery. Report the failed checks. |
| 4 | `pending` | Do not record delivery. CI is running, the PR is not merged yet, or the forge (or a diff for the patch identity) was unavailable: check again later. |
| 2 | (usage) | Fix the invocation: the reference, `--api-base`, the token file, a pattern. Also when the forge redirects or its next-page link points to another host (use the API base and repository name the forge uses now). Nothing was verified. |

## Rules

- **Never record a `not_confirmed` or `pending` result as delivered**, and
  never record evidence the helper did not return. A known-flaky failure is
  still a failure: report it as `failed: known flaky` and leave the delivery
  unconfirmed until CI on the merge commit is green.
- Do not work around a failed check: do not re-run CI, merge, comment or
  relabel from this skill. Report what failed; whoever owns the pull
  request acts on it.
- Never print, log or paste the token, and never pass it as an argument.
  The helper reads it from the file, sends it only in the `Authorization`
  header to the API host, and never includes it in its output. It follows
  neither redirects nor next-page links to another host; since a partial
  listing could hide a later review or a failed check, either one stops the
  run with a usage error instead of a verdict.
- The helper retries once, and only on a clear network error; every request
  has a timeout. It does not wait for CI: call it again later for a
  `pending` result.

Written for this skill set; licensed under the PolyForm Noncommercial License
1.0.0 (see `NOTICE.md` in the repository).
