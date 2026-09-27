# Prompt: rigorous review of a change

Use with the `oh-code-review` skill loaded.

---

Review [PR URL / branch / commit range] with the oh-code-review skill.

Context: [one or two sentences on what the change is for and what must not break].

Constraints:
- The workspace has the PR head checked out; read files directly rather than
  trusting the diff when a patch looks truncated.
- Do not modify code. Report only; if it is good, approve without inventing feedback.
- Skip style. Focus on data structures, complexity, breaking changes, real
  security issues, and whether the tests would fail if the behaviour regressed.

Frozen scope for this PR (findings do not change it):
- Objective: [ONE SENTENCE]
- Acceptance criteria: [LIST]
- Non-goals: [LIST]
- Threat model: [WHAT IS IN / OUT]

Output: the disposition-grouped format from references/dispositions.md. Only a
BLOCKER (concrete failure path + named criterion) returns the PR to
implementation; everything else is CURRENT_SCOPE_IMPROVEMENT, FOLLOW_UP (with
boundary and first increment), OBSERVATION, REJECTED or RISK_ACCEPTED. End with
VERDICT and RISK.
