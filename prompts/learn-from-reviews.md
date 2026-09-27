# Distill review feedback into guidelines

Fill in and paste:

---

Apply the `oh-learn-from-code-review` skill to **<owner/repo>**.

- Time range: last <30> days of merged PRs.
- Host: <Gitea | GitHub> (tea/gh is logged in; token in <env var> if needed).
- AI reviewer accounts to treat as signal: <hands-bot, ...>.
- Output: repo skills under `.agents/skills/`, reviewer corrections into
  `.agents/skills/custom-codereview-guide/SKILL.md`
  (`triggers: [/oh-codereview]`), proposed as a draft PR — do not merge.

If no recurring patterns emerge, say so instead of manufacturing guidelines.
