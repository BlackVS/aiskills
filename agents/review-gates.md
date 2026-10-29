## Code review gates (aiskills)

Reviews use the `oh-code-review` skill (from the `aiskills` set) at a level
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
| Pre-merge local review, XS or S on an ordinary surface | **high** by the implementer at the final head, posted to the PR: one pass plus verification, no fan-out. No second local reader. Where an external reviewer (hands) is deployed it carries the pre-merge weight, and the implementer's pass is the reader from the other model family. |
| Pre-merge local review, M and up, or a sensitive surface | The implementer's **high** as above, plus a second local reader where one is deployed: a coordinating session's own **high** at the same head. |
| Pre-merge re-review after fixes | **high** on the delta only, naming the new head; re-trigger the external reviewer, which costs nothing on the local side. Not needed after a base-only update with an unchanged patch identity (see the PR lifecycle below). |
| Docs-only PRs | **high**. |
| `max` and `ultra` | Only when the person asks, for example a security or installer change right before a production deploy. No gate chooses them: a fan-out costs on the order of a million tokens per pass, and in practice the external reviewer finds the blockers that matter while the fan-out's own catch is test adequacy, which a single pass finds as well. |

The pre-merge rows take two inputs: the task's size (XS, S, M, L, XL) and
its surface, which is ordinary unless the change touches the sensitive list in
the rules below (a repository may extend that list in its AGENTS.md). A change
that meets either condition of the second row takes it. verify-delivery's
default of two required reviews, the local and the external one, is met by
the implementer's high review as the local one; a second local reader adds a
review and replaces none.

Granularity: one board task is one pull request, and lands as one commit on
the base by squash merge. A task is no finer than S. A review's findings go
into one follow-up task per surface, not one task per finding. An L or XL task
is split into parts no finer than M.

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

Pull request lifecycle:

- Open the PR as a **draft** with a **`WIP: `** title prefix, and keep it
  that way while any gate is open.
- Mark it ready only when both pre-merge gates are green at the current head:
  the local high review posted and naming that head with
  `READY_FOR_HUMAN_MERGE`, the external review (where configured) READY at
  that head, and CI green. **Drop the `WIP: ` prefix in the same step**, so
  the title is final before anyone merges.
- A later commit makes both reviews stale, with one exception: a
  **base-only update** (the branch updated from its base, nothing else) whose
  **patch identity** is unchanged. The patch identity is the git patch-id of
  the change against its merge base; compute it for the old and the new head:

  ```sh
  git diff $(git merge-base <base> <head>) <head> | git patch-id --stable
  ```

  Equal identities: both reviews stay valid for the new head, because a
  reviewed patch on a newer base is the same change. Post a short delta note
  on the PR naming the old head, the new head, the two identities and the
  command; do not re-add `review-this`, do not restore `WIP: ` or draft, and
  leave `hands-reviewed` in place. The PR stays ready for human merge. CI
  still runs on the new head and must be green.
  Without a clone, the `forge-gates` skill's `gates` command makes the same
  decision from the forge: it carries a READY review of an older head when
  that head's `change_identity` (a digest of its diff against the base,
  context lines included) equals the new head's. That digest is not a
  git patch-id and is never compared with one; it is the stricter of the two,
  so where they disagree the answer is a re-review.
- Any other later commit (a conflict resolved with edits, any content change)
  makes both reviews stale: the local delta review names the new head, the
  external review is re-requested, and a PR already marked ready goes back
  to a draft with the prefix until the gates are green again.
- The merge stays with the person. An agent never merges, even when every
  gate is green, unless the project's AGENTS.md declares otherwise. A project
  may declare that the coordinating agent session merges an **ordinary** pull
  request (no sensitive surface, not a release preparation) once every review
  its pre-merge row requires is `READY_FOR_HUMAN_MERGE` at the head and CI is
  green there. A pull request on a sensitive surface, and a release
  preparation, stay with the person, and the implementing agent never merges
  its own pull request. Without the declaration the merge is the person's.
  verify-delivery's merge check requires a person account: where the agent
  merges through a bot or app account, the delivery check runs with
  `--allow-bot-merge`, and `--merger <account>` pins the merge to that
  account.

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
- A read that shows none of these labels right after you added `review-this`
  does not mean the request was lost: hands may be switching labels (older
  sites take the old label off before the new one is on). Read again after a
  short wait, and look for a `[hands-bot review]` or `could not run` comment,
  before adding `review-this` again.
- Any later commit or rebase makes the review stale: re-trigger by adding
  `review-this` again, and clear the stale `hands-reviewed` when you do.
  The exception is a base-only update with an unchanged patch identity (see
  the PR lifecycle above): the review stands, `review-this` is not re-added
  and `hands-reviewed` stays.
- Never push to the PR while `hands-reviewing` is present: the reviewer
  refuses to post for a head that moved, and the runner then holds the label
  for its whole watch (45 minutes) before anyone can re-trigger. Land
  follow-up commits after the result arrives, then re-trigger.
- Adding the label means watching for the result: start a background watch,
  and act on the findings when the comment lands — do not leave a triggered
  review for the user to discover. Where the `forge-gates` skill is
  installed, the watch is its `wait-review --head <full SHA>` and the
  readiness check before marking the PR ready is its `gates`; do not write a
  private watcher.

QA gate (optional, execution-based):

- `oh-qa-changes` validates a change by RUNNING the software — environment
  setup, exercising the changed behavior as a user, structured
  PASS/FAIL report with before/after evidence. Complementary to review
  (which reads) and CI (which tests); replaces neither. Use it on request,
  or when a change's risk is behavioral rather than structural.
