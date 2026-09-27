# Dispositions: what a finding does to the current PR

Severity says how bad a problem would be if it occurred. **Disposition says what
happens to the current PR because of it.** They are independent: a HIGH-severity
issue in an excluded deployment mode is a FOLLOW_UP; a MEDIUM-severity defect that
breaks a named acceptance criterion is a BLOCKER. Never derive one from the other.

The reviewer is the final Review lane for a candidate that is already implemented,
tested and CI-green. It reviews against the PR's **frozen scope** — objective,
acceptance criteria, explicit non-goals, documented threat model — and must not
silently expand that scope. Findings are hypotheses until they carry a concrete
failure path.

## The six dispositions (exactly one per finding)

| Disposition | Meaning | Effect on this PR |
| --- | --- | --- |
| `BLOCKER` | Concrete, reproducible failure of a **named current acceptance criterion**, fixable without materially expanding the frozen scope, and the candidate does not work or cannot work safely under the documented threat model while it stands. | The only disposition that returns the PR to implementation. |
| `CURRENT_SCOPE_IMPROVEMENT` | Small useful change related to the objective, not required for acceptance. | None, unless the owner explicitly promotes it. After the candidate is frozen it normally becomes a FOLLOW_UP. |
| `FOLLOW_UP` | Valid issue outside the current acceptance criteria. Must name the affected **boundary** and the **smallest independently testable first increment**. | None. Starts a separate task. Never implementation advice for this PR. |
| `OBSERVATION` | Useful context, no concrete required change. | None. |
| `REJECTED` | Disproved, duplicate, or outside the documented threat model. Include the evidence. | None. |
| `RISK_ACCEPTED` | A concrete risk the owner explicitly accepted, with the reason. Recorded, not invented by the reviewer. | None. |

## BLOCKER test — all four must hold

1. There is a concrete failure path: specific input or state → observed failure,
   established by the reviewed source (`CONFIRMED`) or reproduced at the runtime
   boundary (`CONFIRMED-RUNTIME`). A `PLAUSIBLE-RUNTIME` finding, one resting on
   an unverified external fact, is not a BLOCKER unless leaving that fact
   unverified is itself a stated acceptance-criteria failure (see SKILL.md,
   "Runtime-dependent findings").
2. It violates a **named** acceptance criterion of this PR (quote it).
3. It can be fixed without materially expanding the frozen scope.
4. Left unresolved, the candidate does not work, or cannot work safely under the
   documented threat model.

Generalized hardening, architectural redesign, compatibility expansion, extra
recovery mechanisms, unrelated refactoring, and speculative edge cases fail test 3
or 4 and are therefore FOLLOW_UP or OBSERVATION, whatever their severity.
A finding without a concrete failure path can never be BLOCKER.

## Missing frozen scope

If the PR gives no acceptance criteria, derive them from its title, description and
the change itself, state them in the review's CURRENT OBJECTIVE line so the owner
can correct them, and apply the BLOCKER test against that stated objective — not
against an idealized version of the feature.

## Evidence order

Weigh evidence in this order: observed product or pipeline behavior on the exact
candidate; focused behavioral reproduction; complete automated tests; static
analysis; speculative reasoning. A green real path plus adequate regression
coverage outweighs requests for generalized completeness not tied to a current
acceptance criterion.

## Verdict — a pure function of dispositions

- Zero `BLOCKER` → `READY_FOR_HUMAN_MERGE`, even when FOLLOW_UP, OBSERVATION or
  CURRENT_SCOPE_IMPROVEMENT items exist.
- One or more confirmed `BLOCKER` → `RETURN_TO_IMPLEMENTATION`.
- Review could not be performed → `REVIEW_COULD_NOT_RUN`.

The risk rating (LOW / MEDIUM / HIGH) informs the human merge decision — HIGH may
require explicit human approval — but never turns a non-blocking finding into a
blocker. A Gitea/GitHub `REQUEST_CHANGES` state is emitted **only** for confirmed
BLOCKER findings; a review with no blockers uses `APPROVE` or `COMMENT`.

## Exact-head semantics

A new review is required only when the candidate source commit changes. Adding
backlog items, discussing dispositions, rejecting a finding with evidence,
recording owner risk acceptance, or editing PR metadata or comments does not
invalidate the review of the reviewed head.

## Structured finding (used by sub-agents and by any automation)

```json
{"file": "path/to/file", "line": 123,
 "severity": "critical|high|medium|low",
 "disposition": "BLOCKER|CURRENT_SCOPE_IMPROVEMENT|FOLLOW_UP|OBSERVATION|REJECTED|RISK_ACCEPTED",
 "summary": "<one sentence>",
 "failure_path": "<concrete state/input -> observed failure, or null>",
 "acceptance_criterion": "<the named criterion a BLOCKER violates, else null>",
 "boundary": "<affected component/contract, required for FOLLOW_UP>",
 "first_increment": "<smallest independently testable next step, required for FOLLOW_UP>",
 "confidence": "confirmed|confirmed-runtime|plausible|plausible-runtime|rejected-runtime",
 "assumption": "<runtime-dependent only: the external fact not established by source, else null>",
 "reproducer": "<runtime-dependent only: smallest safe non-mutating check at the real boundary, else null>",
 "current_evidence": "<runtime-dependent only: what is known / unknown, else null>",
 "unverified_is_criteria_failure": false}
```

`disposition` is an enum, never inferred from prose or from `severity`. A
`*-runtime` confidence requires `assumption` and `reproducer`; `rejected-runtime`
pairs with disposition `REJECTED`; `plausible-runtime` may be BLOCKER only when
`unverified_is_criteria_failure` is true. When two
findings about the same root cause carry different dispositions, resolve
conservatively toward the **less** blocking one unless the BLOCKER test holds on
the merged evidence — never silently escalate to BLOCKER.

## Output format

```
[<bot> review] reviewed at head <short-sha>

CURRENT OBJECTIVE
<one sentence: the frozen objective this review was judged against>

BLOCKERS
- None
- [BLOCKER] [CONFIRMED] <summary>
  Failure path: <specific input/state -> failure>
  Violated criterion: <named acceptance criterion>

CURRENT-SCOPE IMPROVEMENTS
- [CURRENT_SCOPE_IMPROVEMENT] <summary> (file:line)

FOLLOW-UPS
- [FOLLOW_UP] [CONFIRMED|PLAUSIBLE] <summary> (severity: high)
  Failure path: <specific path>
  Boundary: <component/contract>
  First increment: <smallest independently testable next step>

OBSERVATIONS
- [OBSERVATION] <context>

REJECTED
- [REJECTED] <summary> — evidence: <why it does not apply>

RISK ACCEPTED
- [RISK_ACCEPTED] <risk> — owner reason: <reason>

VERDICT
READY_FOR_HUMAN_MERGE | RETURN_TO_IMPLEMENTATION | REVIEW_COULD_NOT_RUN
<one line: why; if READY with follow-ups, say the follow-ups do not block>

RISK
LOW | MEDIUM | HIGH — <short reason>
```

Every section is always present; an empty one prints `- None`, so the output
parses identically whether or not it has entries. Lead with blockers. Keep it
concise: no praise-only entries, no findings manufactured for thoroughness.
