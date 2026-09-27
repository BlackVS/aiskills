# QA Changes

Validate a pull request by **running the software**, not reading it: set up
the environment, exercise the changed behavior the way a real user would
(browser, CLI, HTTP), and post a structured PASS / PASS WITH ISSUES / FAIL /
PARTIAL report with before/after evidence in collapsible blocks.

Explicitly NOT: re-running the test suite (CI's job) or analyzing code
(code review's job). This is the third gate dimension - review reads, CI
tests, QA executes.

## Triggers

- `/oh-qa-changes` (optionally followed by a PR reference)

## Adaptations vs upstream `qa-changes`

- Report posting is host-agnostic: Gitea via `tea comment`, GitHub via
  `gh pr comment`, or in-session for a local diff.
- `oh-` prefix keeps it distinct from the upstream copy bundled by OpenHands
  runtimes.

See [SKILL.md](./SKILL.md) for the four-phase methodology and report format.
