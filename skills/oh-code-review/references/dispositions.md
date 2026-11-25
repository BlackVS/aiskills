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
| `FOLLOW_UP` | Valid issue outside the current acceptance criteria. Must name its **surface** (the component or contract a reviewer reads as a whole) and the **outcome** that closes it. | None. Goes into the follow-up task for its surface (see "Follow-ups are grouped per surface"). Never implementation advice for this PR. |
| `OBSERVATION` | Useful context, no concrete required change. | None. |
| `REJECTED` | Disproved, duplicate, or outside the documented threat model. Include the evidence. | None. |
| `RISK_ACCEPTED` | A concrete risk the owner explicitly accepted, with the reason. Recorded, not invented by the reviewer. | None. |

## BLOCKER test — all four must hold

1. There is a concrete failure path: specific input or state → observed failure,
   on the **plain path** or in a **compound-fault state that does not fail
   safe** (next section), established by the reviewed source (`CONFIRMED`) or
   reproduced at the runtime boundary (`CONFIRMED-RUNTIME`). A
   `PLAUSIBLE-RUNTIME` finding, one resting on an unverified external fact, is
   not a BLOCKER unless leaving that fact unverified is itself a stated
   acceptance-criteria failure (see SKILL.md, "Runtime-dependent findings").
2. It violates a **named** acceptance criterion of this PR (quote it).
3. It can be fixed without materially expanding the frozen scope.
4. Left unresolved, the candidate does not work, or cannot work safely under the
   documented threat model.

Generalized hardening, architectural redesign, compatibility expansion, extra
recovery mechanisms, unrelated refactoring, and speculative edge cases fail test 3
or 4 and are therefore FOLLOW_UP or OBSERVATION, whatever their severity. So does
a compound-fault state that fails safe, and a missing message, hint or automatic
recovery in one.

A finding without a concrete failure path can never be BLOCKER.

Test 1 is sharpened rather than a test added: it named a specific state but not
how the state is reached, so a state that needs several independent faults met
it as fully as a single command did.

## Plain path and compound-fault state

Every failure path names its kind, first on the finding's `Failure path` line
(`plain:` or `compound, <n> faults:`).

- **Plain path:** one command or request by a documented user, on a state the
  product's own documented paths lead to, including one interruption or one
  failure of that command. Example: an upgrade interrupted once.
- **Compound-fault state:** a state that needs two or more independent faults
  (an interrupted run and then a second interrupted, refused or failed run; a
  fault during the recovery from a fault; a process stopped at one exact point
  for longer than a stated time), or an object no command of the product makes
  (a foreign file under the product's own name, a hand edit of its state).
  Example: an upgrade interrupted, and its rollback interrupted too.

In a compound-fault state the reviewer checks only that the product **fails
safe**: no data lost or corrupted, no secret exposed, nothing half-written that a
later plain operation would silently misread, no claim of a success that did not
happen. If it does, the finding is a FOLLOW_UP, whatever its severity and
whatever sentence of a document it contradicts. The finding names the state and
the evidence that it fails safe, and proposes neither code nor the form of the
answer. A follow-up for such a state takes one of two forms: a task that fixes
the state in code, or an entry in the operator's recovery document that says how
to see the state and how to recover from it. Which one, or none, is the
project's choice, and its custom-codereview-guide may set a default. Where the
pull request already changes that document, the entry may go into it before the
merge as a CURRENT_SCOPE_IMPROVEMENT.

What stays BLOCKER is a failure on the plain path and a compound-fault state that
does not fail safe. An unsafe state that needs more than two independent faults
does not return the pull request: it is a FOLLOW_UP, and the review's RISK
section names it to the owner in one line of a fixed form, so a person or a
readiness check cannot miss it:

    OWNER-RISK: <the unsafe state, one sentence> CHANGELOG: <the sentence the changelog needs>

A sentence of a document or of the acceptance criteria that promises more than
the plain path gives ("always recovers", "every refusal names the way back") is
a finding of its own. It does not make a compound-fault state a BLOCKER; how the
sentence is narrowed is the project's.

Security is not rarity. A fault that an attacker inside the documented threat
model can cause on purpose is not independent of the attack: if the threat model
admits a second local user who can write the product's state directory, a
half-written file planted there is that attacker's plain path, and the threat
model judges it.

## Repeat reviews

A repeat review keeps the first round's level and depth over the delta and what
it touches. It first reads every earlier review of the pull request and the
comments in which the scope owner disposed their findings. Such a comment counts
only from an account the repository trusts, by the rule `verify-delivery` applies
to review authors.

- REJECTED binds with its evidence: the finding is not raised again unless the
  reviewer shows the evidence wrong, and says how.
- RISK_ACCEPTED binds when the comment names the owner's acceptance. FOLLOW_UP
  binds for a compound-fault state that fails safe.
- A plain-path failure returns the pull request on any round, whatever
  disposition it got. So does the same unsafe compound-fault state shown again at
  the new head, unless the owner's RISK_ACCEPTED names that state. The review
  says in one line that the disposition does not bind, and why.
- A **new** compound-fault finding on a surface already returned once for one
  does not open another round: the review says so in one line and gives
  READY_FOR_HUMAN_MERGE with the follow-up, and an unsafe one carries the
  `OWNER-RISK:` line.

A READY review that carries an `OWNER-RISK:` line is merged only by a person, and
the owner answers it in one of three ways on the pull request:

1. merge as it stands, naming the RISK_ACCEPTED;
2. leave the state to a follow-up, in one of the two forms above (a code fix, or
   a recovery entry that says how to see the state and recover from it), and
   merge;
3. try a fix within a stated number of rounds, in one line of a fixed form beside
   the risk line:

       OWNER-ROUNDS: <n> for <the unsafe state, as the OWNER-RISK line names it>

   The pull request goes back to a draft. The fix is the smallest one for that
   state, and each external review of it counts as a round. While rounds
   remain, the reviewer reviews as usual and may return the pull request for
   that state or for a new compound-fault state of the fix. When they are used
   up, it gives READY with the `OWNER-RISK:` line again and says in one line
   that the owner's rounds are spent.

The `OWNER-ROUNDS:` line counts only from the owner, and neither the author nor a
coordinator sets the number. Without it, such a state opens no further round. A
plain-path failure returns the pull request whatever the number.

## Follow-ups are grouped per surface

Every pull request has a fixed cost (a CI run, the reviews, the merge and the CI
run after it), so a review's follow-ups never become one task each. The
follow-ups of one review that share a surface become one task, and they are
added to an open task for that surface when one exists. Each follow-up names the
outcome that closes it: the complete, verifiable result, which together with the
rest of its group is a task of size S or larger. It does not name the smallest
possible first step. Whether a large group is split further is decided by the
project's decomposition rule, not by the review.

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
 "failure_path": "<plain: | compound, <n> faults:> <concrete state/input -> observed failure>, or null",
 "acceptance_criterion": "<the named criterion a BLOCKER violates, else null>",
 "surface": "<the component/contract a reviewer reads as a whole, required for FOLLOW_UP>",
 "outcome": "<the complete, verifiable result that closes it, required for FOLLOW_UP>",
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
[<bot> review] reviewed at head <full-sha>

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
  Surface: <component/contract>
  Outcome: <the complete, verifiable result that closes it>

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
OWNER-RISK: <only for an unsafe compound-fault state that opens no round> CHANGELOG: <sentence>
```

Write the verdict word on the line right after `VERDICT`. `verify-delivery`
also accepts blank lines between the two, but no other text: a `VERDICT` line
followed by prose does not count as READY.

Name the head by its full 40-digit SHA. A delivery check carries a review of an
older head over a base-only update only when it can tell that head apart: an
abbreviated SHA can resolve to a branch or tag of that name, and on GitHub and
Gitea it is not accepted for an older head (`verify-delivery`).

Every section is always present; an empty one prints `- None`, so the output
parses identically whether or not it has entries. Lead with blockers. Keep it
concise: no praise-only entries, no findings manufactured for thoroughness.
