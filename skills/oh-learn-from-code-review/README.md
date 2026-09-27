# Learn from Code Review

Distill accumulated PR review feedback into reusable repository skills and —
the key integration — the `.agents/skills/custom-codereview-guide/SKILL.md`
that `oh-code-review` and the hands automated reviewer read before every
review. Recurring reviewer false positives become "don't flag X because Y"
guideline lines; recurring author mistakes become domain pattern skills.

## Triggers

- `/oh-learn-from-reviews`

## Adaptations vs upstream `learn-from-code-review`

- Dual-host: Gitea (`tea` / `/api/v1`) first-class alongside GitHub (`gh`).
- Designated AI reviewers (`hands-bot` etc.) are INCLUDED as high-signal
  sources, unlike upstream's blanket bot exclusion; their refuted findings
  feed the reviewer guideline.
- Output targets `.agents/skills/` and the `custom-codereview-guide`
  contract (`triggers: [/oh-codereview]`) instead of `.openhands/skills/`.
- PR creation via `tea pr create` / `gh pr create --draft` instead of the
  OpenHands `create_pr` tool.

See [SKILL.md](./SKILL.md) for the six-step workflow.
