---
name: oh-learn-from-code-review
description: Distill code review feedback from merged PRs into reusable skills and reviewer guidelines. Use when asked to "learn from code reviews", "distill PR feedback", "improve coding standards", "extract learnings from reviews", or to generate guidelines from historical review comments. Works against Gitea (tea/API) and GitHub (gh); feeds the custom-codereview-guide that oh-code-review reads.
triggers:
- /oh-learn-from-reviews
- learn from code review
- distill reviews
---

# Learn from Code Review

Analyze code review comments from merged pull requests and distill them into
reusable skills or repository guidelines that improve future code quality —
including the guideline file the `oh-code-review` reviewer itself reads.

## Overview

Review feedback contains institutional knowledge that gets buried across
hundreds of PRs. This skill extracts recurring patterns and turns them into:

1. **Repository-specific skills** — `.agents/skills/{domain}/SKILL.md`
   (readable by OpenCode/Codex directly; copy or symlink into
   `.claude/skills/` for Claude Code project scope where wanted).
2. **Reviewer guidelines** — `.agents/skills/custom-codereview-guide/SKILL.md`
   with `triggers: [/oh-codereview]`, which `oh-code-review` (and the hands
   automated reviewer) loads before every review. Recurring false positives
   and repo-specific exceptions belong HERE — this closes the loop between
   reviews and the reviewer.
3. **AGENTS.md guidelines** — only for conventions too broad for a skill.

## Prerequisites

One of, depending on the host:

- **Gitea**: `tea` CLI logged in, or a token for `/api/v1` `curl` calls.
- **GitHub**: `gh` CLI authenticated, or `GITHUB_TOKEN` for API calls.

## Workflow

### Step 1: Identify Target Repository

From the working copy's `origin` remote, or ask the user. Note the host type
from the remote URL.

### Step 2: Fetch Review Comments

Fetch merged PRs from the last 30 days (adjustable), then their review
comments — both line-level and PR-level.

**Gitea** (`{base}` = `https://<host>/api/v1`, auth header `Authorization: token $TOKEN`):

```bash
# merged PRs
curl -s "{base}/repos/{owner}/{repo}/pulls?state=closed&limit=50" \
  | jq '.[] | select(.merged) | {number, title, merged_at}'
# review-level comments
curl -s "{base}/repos/{owner}/{repo}/pulls/{n}/reviews" \
  | jq '.[] | select(.body != "") | {body, user: .user.login, state}'
# discussion comments (where bot reviewers like hands-bot post)
curl -s "{base}/repos/{owner}/{repo}/issues/{n}/comments" \
  | jq '.[] | {body, user: .user.login, created_at}'
```

**GitHub**: `gh pr list --state merged --limit 50 --json number,title,mergedAt`,
then `gh api repos/{owner}/{repo}/pulls/{n}/comments` and `.../reviews`.

### Step 3: Filter and Categorize Comments

Apply noise filtering to keep only meaningful feedback:

**Exclude:**
- Automation noise (dependabot, CI status bots, coverage reports)
- Low-signal responses ("LGTM", "+1", "looks good", "thanks", "nice")
- Comments shorter than 30 characters

**Include even though they are bots:** designated AI reviewers
(e.g. `hands-bot`, review comments posted by session bots) — their structured
findings are exactly the feedback to learn from. Treat their `[CONFIRMED]`
findings as high-signal and their false positives (findings the author
refuted and the team waived) as candidates for the custom-codereview-guide.

**Categorize remaining comments by:** security, performance, style and
conventions, architecture and design, error handling, testing requirements,
documentation standards.

### Step 4: Distill Patterns

For each category with sufficient examples (3+ similar comments), identify:

1. **The recurring issue** — what mistake or oversight keeps appearing
2. **The desired pattern** — what reviewers consistently ask for
3. **Example context** — concrete before/after snippets when available

Also collect the inverse: findings that were repeatedly REFUTED or waived —
these become "do not flag X here because Y" lines for the reviewer guideline.

### Step 5: Generate Output

If clear, actionable patterns emerge, write focused files; if none do, say so
— no output is a valid result for a codebase with strong conventions.

- Domain patterns → `.agents/skills/{domain}/SKILL.md` (frontmatter: `name`,
  `description`).
- Reviewer corrections → `.agents/skills/custom-codereview-guide/SKILL.md`
  (frontmatter must include `triggers: [/oh-codereview]`).
- Prefer skills over AGENTS.md updates; check existing files first and
  extend rather than duplicate. Cite source PR numbers next to each pattern.

### Step 6: Propose as a PR

Open a draft PR with the generated files — Gitea: branch + push +
`tea pr create --repo {owner}/{repo} --title ... --description ...`;
GitHub: `gh pr create --draft`. The description states: PRs analyzed,
comments processed, categories found, files added/changed. Generated
content is a draft; human review before merge is essential.

## Error Handling

- **Few PRs in range** (<10): proceed, note the limited sample.
- **No patterns emerge**: report that; suggest widening the time range.
- **403/404 from the API**: the token lacks access — say which token and
  which permission is missing.
- **`tea`/`gh` unavailable**: fall back to raw `curl` API calls.

## Limitations

- Patterns may reflect individual reviewer preference, not team consensus.
- Historical comments may reference code that has since changed — verify a
  pattern still applies before writing it down.
- Verbal/meeting feedback is invisible to this analysis.
