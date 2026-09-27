# Code Review

Rigorous code review focusing on data structures, simplicity, security, pragmatism, and risk/safety evaluation. Provides brutally honest, actionable feedback on pull requests or merge requests, including a risk assessment (🟢 Low / 🟡 Medium / 🔴 High) for every review.

This skill combines the previous `code-review` (standard) and `codereview-roasted` skills into a single unified review skill.

## Triggers

This skill is activated by the following keywords:

- `/oh-codereview [low|medium|high|max|ultra]`
- `/oh-codereview-roasted` (backward-compatible alias)

## Review levels

- **low** — fast pass, confident critical issues only.
- **medium** (default) — the classic single-pass review.
- **high** — single pass plus a verification stage: each finding is re-checked
  against the workspace and labeled `[CONFIRMED]` or `[PLAUSIBLE]`; only refuted
  findings are dropped.
- **max** — per-file sub-agent fan-out (modeled on the OpenHands `pr-review`
  plugin: coordinator + file reviewers returning JSON findings), a cross-file
  pass, then verification.
- **ultra** — max plus whole-diff specialist passes (security, test adequacy,
  cross-file data flow).

## Details

See [SKILL.md](./SKILL.md) for the full skill content including review scenarios, output format, and communication style guidelines.

The risk evaluation framework is defined in [`references/risk-evaluation.md`](references/risk-evaluation.md) and classifies PR risk based on pattern conformance, security sensitivity, infrastructure dependencies, blast radius, and core system impact. The multi-agent protocol for `max`/`ultra` is defined in [`references/fan-out.md`](references/fan-out.md).
