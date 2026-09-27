## Code review gates (ai-skills)

Reviews use the `oh-code-review` skill (from the `ai-skills` set) at a level
chosen by the gate. State the level in the request, e.g. "review this at max";
in Claude Code `/oh-code-review max` also works. The levels:

- **low**: fast pass, confident critical issues only.
- **medium** (default): the standard single-pass rubric.
- **high**: single pass plus a verification stage that re-grounds every finding
  and labels it CONFIRMED or PLAUSIBLE; only refuted findings are dropped.
- **max**: one sub-agent per changed file (batched, capped at 8), consolidation
  and dedup, a mandatory cross-file pass by the coordinator, then verification.
- **ultra**: max plus whole-diff specialist passes for security and supply chain,
  test adequacy, and cross-file data flow, reported with a coverage note.

| Gate | Level |
| --- | --- |
| Pre-push, every push | **medium**, run inline by the reviewer itself, no sub-agents. Cheap, keeps the rhythm; its misses are what the next gate exists for. |
| Pre-merge local review, posted to the PR | **high**: one pass plus verification, no fan-out. Where an external reviewer (hands) is deployed it carries the pre-merge weight and the local pass is the second reader from another model family. |
| Pre-merge re-review after fixes | **high** on the delta only, naming the new head; re-trigger the external reviewer, which costs nothing on the local side. |
| Docs-only PRs | **high**. |
| `max` and `ultra` | Only when the person asks, for example a security or installer change right before a production deploy. No gate chooses them: a fan-out costs on the order of a million tokens per pass, and in practice the external reviewer finds the blockers that matter while the fan-out's own catch is test adequacy, which a single pass finds as well. |

Rules:

- The reviewer may recommend `max` for a diff that spans 4+ files or 500+
  changed lines on a sensitive surface (sync or wire protocols, authentication
  and security code, storage schemas and migrations, installers or anything
  that runs as root; repositories may extend the list in their AGENTS.md), but
  never escalates on its own: `max` and `ultra` launch sub-agents and multiply
  cost, so the level is always chosen by the person.
- `oh-code-review` is the whole gate. Claude Code's built-in `/code-review` is
  an occasional cross-check, not a standing second reviewer.
- Reviews are delivery-first. The reviewer judges the candidate against the PR's
  **frozen scope** (objective, acceptance criteria, non-goals, threat model) and
  gives every finding one disposition: `BLOCKER`, `CURRENT_SCOPE_IMPROVEMENT`,
  `FOLLOW_UP`, `OBSERVATION`, `REJECTED`, `RISK_ACCEPTED`. Severity and
  disposition are independent. **Only a BLOCKER** — concrete failure path,
  named acceptance criterion violated, fixable in scope, unsafe if left —
  returns the PR to implementation; the verdict is `READY_FOR_HUMAN_MERGE`
  whenever there is no BLOCKER, even with follow-ups. Do not act on a
  FOLLOW_UP inside the current PR: it starts a separate task (the review names
  its boundary and smallest first increment). Metadata-only triage, rejection
  with evidence or owner risk acceptance never needs a re-review; a changed
  source commit does.
- A finding is a hypothesis. One that rests on a fact outside the reviewed
  source (container UID, runner or mount configuration, credential scope,
  platform or pinned-tool behavior) is `PLAUSIBLE-RUNTIME` until observed at
  the real boundary, and implementation must not react to it by hardening
  production code: the answer is the smallest safe, non-mutating reproducer
  the review names, then REJECTED (record the observed contract in the repo's
  `custom-codereview-guide`) or CONFIRMED-RUNTIME. Never fix a hypothetical
  the environment has not been shown to produce.
- A review that finds nothing says so in one line. Do not manufacture findings.
- Repository-specific exceptions go in
  `.agents/skills/custom-codereview-guide/SKILL.md`, which the reviewer reads
  before every review.
- A review posted to a PR carries at most a one-line tool attribution ("Review:
  oh-code-review (max) via Claude Code") — never a session or conversation URL:
  PRs are often public, the links lead into the author's private workspace, and
  they are dead for every other reader. Commit/PR-description attribution
  conventions do not extend to review comments.
- Nothing internal goes into text posted to a PR — review, QA report, or any
  comment: no private hostnames or domains, hub/deployment names, backend
  binding names, token or credential names/paths, including inside quoted
  commands, logs, and evidence blocks. Describe environments generically
  ("the primary hub", "a peer hub", "a named backend"); when unsure whether a
  name is public, leave it out.

External reviewer labels (hands / OpenHands, where deployed — org-wide on the
Gitea orgs a hands site covers, and on GitHub repos hands polls):

- To REQUEST an external review of a PR, add the **`review-this`** label.
  hands drives the rest: it swaps to **`hands-reviewing`** when it picks the
  PR up and lands **`hands-reviewed`** when its review comment is posted;
  a failed run shows a `⚠️ … could not run` comment and, where defined,
  **`hands-review-failed`** — re-add `review-this` to retry.
- Some sites let you pick the reviewer model per PR with a variant label
  **`review-this:<profile>`** (e.g. `review-this:codex-sol`); older ones have
  one fixed reviewer and know only `review-this`. **Resolve it from the
  site's label list before requesting**: variant labels present → selection
  is supported (the suffixes are the profiles); only `review-this` → use
  that and nothing else, and never create a variant label yourself (it would
  be an inert ordinary label there). The external pass exists to be a
  reader from a *different* model family than the local one, and site
  defaults are set that way (a hands site defaulting to a GPT model means a
  Claude Code session's plain `review-this` already gets the other family).
  Use a variant only when the user asks for a specific model or the default
  would repeat the family that did the local pass (e.g. a Codex-driven
  session asks for the Claude profile). Full protocol: the installed
  `oh-code-review` skill's `references/external-review-labels.md`.
- The labels are the progress signal, never the verdict: `hands-reviewed`
  means a review EXISTS for the head hands read — the findings live in the
  `[hands-bot review]` comment (which names that head) and still have to be
  read and acted on.
- Do not re-add `review-this` while `hands-reviewing` is present, and never
  hand-edit the other two labels.
- Any later commit or rebase makes the review stale: re-trigger by adding
  `review-this` again, and clear the stale `hands-reviewed` when you do.
- Never push to the PR while `hands-reviewing` is present: the reviewer
  refuses to post for a head that moved, and the runner then holds the label
  for its whole watch (45 minutes) before anyone can re-trigger. Land
  follow-up commits after the result arrives, then re-trigger.
- Adding the label means watching for the result: start a background watch,
  and act on the findings when the comment lands — do not leave a triggered
  review for the user to discover.

QA gate (optional, execution-based):

- `oh-qa-changes` validates a change by RUNNING the software — environment
  setup, exercising the changed behavior as a user, structured
  PASS/FAIL report with before/after evidence. Complementary to review
  (which reads) and CI (which tests); replaces neither. Use it on request,
  or when a change's risk is behavioral rather than structural.
