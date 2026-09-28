---
name: verify-delivery
description: Confirm from the forge (GitHub or Gitea) that a pull request was actually delivered, before recording it as done. Checks that the required reviews are READY_FOR_HUMAN_MERGE at exactly the final head, that the pull request was merged by a person, that the merge commit's tree equals the reviewed head's tree, and that CI on the merge commit is green. Read-only; returns JSON with a verdict and, only when confirmed, the evidence to record. Use when a coordinator or any agent must decide whether a PR counts as delivered.
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
   The required reviews must be `READY_FOR_HUMAN_MERGE` at exactly that
   head: the latest review comment that names the head counts, one naming an
   older head does not, and one posted or edited after the merge does not.
   Reviews are recognised by their text, not by who posted them. By default
   two reviews are required, both in the `oh-code-review` comment format
   (`[<reviewer> review] reviewed at head <sha>`, then `VERDICT` /
   `READY_FOR_HUMAN_MERGE`):
   - `local`: the pre-merge `oh-code-review` review at level high (or max,
     ultra);
   - `external`: any other reviewer's comment in that format.
2. **Merge.** The PR is merged; the merge commit and the account that
   merged it are recorded. By default that account must be a person (a
   GitHub `User`, not a `Bot`). Gitea has no bot flag: its system accounts
   count as bots, and you name any other bot accounts with `--bot-account`.
   `--merger NAME` restricts who may merge; `--allow-bot-merge` accepts bots.
3. **Tree equality.** The merge commit's tree equals the reviewed head's
   tree, so the merged content is exactly what was reviewed. This holds for
   merge, squash and rebase merges of an up-to-date branch.
4. **Post-merge CI.** Every check run (GitHub) or commit status (Gitea) on
   the merge commit completed successfully (`skipped` and `neutral` pass on
   GitHub; on Gitea only `success` passes, `pending` is pending, and `error`,
   `failure` and `warning` fail). Still running means pending. On GitHub only
   check runs are read: CI that reports solely through the older commit-status
   API shows up as no CI yet, so the result stays pending. A failure fails. A failing check you
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
  --api-base https://git.example.org/api/v1
```

On Windows run it with `python` or `py -3`. The token needs read access to
pull requests, comments, commits and checks (or statuses) only. Other
references: `--pr OWNER/REPO#123`, or `--pr OWNER/REPO --number 123`;
`--forge gitea` when it cannot be told from the URL; `--api-base` for
GitHub Enterprise (`https://HOST/api/v3` is assumed from a URL) and always
for Gitea, over https whenever a token is used; `--token-env NAME` for a different variable; `--anonymous` for a
public repository without a token.

Options for the checks:

| Option | Default | Meaning |
| --- | --- | --- |
| `--review NAME=REGEX` (repeatable) | `local` and `external` as above | A required review. The regex must have a `(?P<sha>...)` group capturing the head it names (7 to 40 hex digits). Giving any `--review` replaces the defaults. |
| `--required-reviews N` | all of them | How many of the reviews must be READY. |
| `--verdict-pattern REGEX` | a `VERDICT` line followed by `READY_FOR_HUMAN_MERGE` | What makes a review READY. |
| `--merger NAME` (repeatable) | anyone | Allowlist of accounts that may merge. |
| `--bot-account NAME` (repeatable) | none | Treat this account as a bot. |
| `--allow-bot-merge` | off | Accept a merge by a bot or app. |
| `--known-flaky NAME` (repeatable) | none | Report this check's failure as `failed: known flaky` (still not confirmed). |

## Reading the result

One JSON document on stdout:

- `verdict`: `confirmed`, `not_confirmed` or `pending`.
- `checks`: one entry per check (`reviewed_head`, `merge`, `tree_equality`,
  `post_merge_ci`) with its `status` (`passed`, `failed`, `pending`) and the
  details: SHAs, URLs, the reviews found and the ones ignored with the
  reason, who merged and their account type, each CI run with its result.
- `evidence`: present **only** when the verdict is `confirmed`: a list of
  `{kind, ref}` to record as they are, with at most 16 entries, each ref at
  most 512 bytes:
  - `reviewed_head`: the reviewed head commit's URL;
  - `human_merge`: the pull request's URL;
  - `post_merge_ci`: the CI run URLs (the merge commit's URL when there are
    too many runs to list).
- `error`: present when the run could not complete, with its `kind`
  (`usage`, `unavailable`, `not_found`).

| Exit code | Verdict | What to do |
| --- | --- | --- |
| 0 | `confirmed` | Record the `evidence` entries. |
| 3 | `not_confirmed` | Do not record delivery. Report the failed checks. |
| 4 | `pending` | Do not record delivery. CI is running, the PR is not merged yet, or the forge was unavailable: check again later. |
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
