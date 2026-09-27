# QA a pull request by running it

Fill in and paste:

---

Apply the `oh-qa-changes` skill to QA pull request **<PR number or URL>** of
**<owner/repo>**.

Context you may need:

- How to run the project: <dev-server command / CLI entry point / API base,
  or "infer from README/Makefile">
- The behavior the PR claims to change: <one sentence, or "take it from the
  PR description">
- Where to post: <"comment on the PR" | "report here">

Remember the skill's boundaries: do not run the test suite, do not review the
code — run the software and report PASS / PASS WITH ISSUES / FAIL / PARTIAL
with before/after evidence in collapsible blocks.
