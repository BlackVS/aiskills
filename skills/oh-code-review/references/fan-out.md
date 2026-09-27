# Fan-out review protocol (levels `max` and `ultra`)

Multi-agent review modeled on the OpenHands `pr-review` plugin (a coordinator
delegating to `file_reviewer` sub-agents, findings returned as structured JSON),
extended with two things the upstream plugin lacks: a verification stage and, at
`ultra`, cross-cutting specialist passes.

At these levels you are the **coordinator**: you partition, delegate,
consolidate, verify, and write the final review. You do not do the per-file
reviewing yourself unless no sub-agent mechanism exists (see step 3).

## 1. Build the manifest

- List changed files with change sizes: `git diff --stat <base>...HEAD`, or the
  diff/PR manifest you were given.
- Exclude generated and vendored files from per-file review (lock files,
  `*.min.*`, generated code) — but keep the dependency-change analysis
  (SKILL.md scenario 8) for lock/manifest files at the coordinator level.

## 2. Partition into review units

- One sub-agent per changed file.
- Group small files (under ~20 changed lines) into batches of up to 3 related
  files.
- Cap at 8 sub-agents; with more files, group by directory or subsystem so
  every file is still covered. Launch them in batches of at most 4 and wait for
  a batch before starting the next (harnesses have no concurrency knob).

## 3. Launch file reviewers

Use whatever sub-agent mechanism your harness provides:

- **Claude Code**: the Agent tool — read-only `Explore` agents are ideal;
  `general-purpose` otherwise. Launch a batch of independent reviewers in a
  single message.
- **OpenCode**: the Task tool — the built-in read-only `explore` subagent is
  ideal; `general` otherwise (it can edit files, so tell it not to).
- **OpenHands**: task delegation (`TaskToolSet`).
- **No sub-agent mechanism**: run the same per-file reviews yourself,
  sequentially — one file at a time with fresh, full attention — then continue
  with step 4.

Prompt template for each file reviewer (fill the placeholders):

```
Review the changes to <files> as a critical code reviewer. First read
<path-to-this-skill>/SKILL.md (PERSONA through CODE REVIEW SCENARIOS) and apply
those standards.

Scope: the changed lines in <files>, judged in the context of the whole
repository — read any other workspace files you need (callers, tests, configs),
but report findings only for <files>.

Diff for your files:
<per-file patch, or instructions to obtain it, e.g. `git diff <base>...HEAD -- <file>`>

Return ONLY a JSON array of findings (an empty array if the changes are clean),
one object per finding, in the structured-finding shape of
`references/dispositions.md`:
[{"file": "...", "line": <int or null when the finding is file-level>,
  "severity": "critical|high|medium|low",
  "disposition": "BLOCKER|CURRENT_SCOPE_IMPROVEMENT|FOLLOW_UP|OBSERVATION|REJECTED|RISK_ACCEPTED",
  "category": "data-structure|complexity|breaking-change|security|pragmatism|testing|dependency",
  "summary": "<one sentence>",
  "failure_path": "<concrete inputs/state -> observed failure, or null>",
  "acceptance_criterion": "<named criterion a BLOCKER violates, else null>",
  "boundary": "<affected component/contract, required for FOLLOW_UP, else null>",
  "first_increment": "<smallest independently testable next step, required for FOLLOW_UP, else null>",
  "confidence": "confirmed|confirmed-runtime|plausible|plausible-runtime|rejected-runtime",
  "assumption": "<runtime-dependent only, else null>", "reproducer": "<runtime-dependent only, else null>",
  "current_evidence": "<runtime-dependent only, else null>", "unverified_is_criteria_failure": false}]

A finding that rests on a fact outside the reviewed source (container UID, runner
or mount configuration, credential scope, platform or service behavior, pinned
tool behavior) is `plausible-runtime` with its assumption and the smallest safe
non-mutating reproducer named; it is never BLOCKER on source evidence alone.

The frozen scope (objective, acceptance criteria, non-goals, threat model) is
given above; apply the four-part BLOCKER test from dispositions.md before using
BLOCKER. Severity and disposition are independent.
Do not manufacture findings for good code. Skip style nits.
```

## 4. Consolidate

- Merge all findings; deduplicate near-duplicates (same file + line + category,
  or the same root cause reported from two files — keep the better-evidenced
  one).
- When duplicates carry different dispositions, resolve toward the **less**
  blocking one unless the BLOCKER test holds on the merged evidence. Never let
  consolidation escalate a finding to BLOCKER that no reviewer classified so.
- Specialist-pass findings (security, tests, data flow) keep the disposition
  their reviewer assigned; severity from a specialist is not a reason to change it.
- Discard style-only nits per SKILL.md rules.

## 5. Cross-cutting passes

Per-file review structurally misses interactions between files. The
coordinator ALWAYS does a cross-file pass itself (both `max` and `ultra`):
changed interfaces vs. their callers, responsibilities moved between files,
logic duplicated across files, config/docs drift relative to the code change.

At **ultra**, additionally launch specialist sub-agents over the *entire* diff,
with the same JSON output contract:

- **Security**: SKILL.md scenario 5 plus `references/supply-chain-security.md`,
  over the whole diff and any touched dependency manifests.
- **Test adequacy**: scenario 6 — do the tests prove the changed behavior;
  flag mock-only tests; name the missing test cases.
- **Data flow / architecture**: scenario 1 across file boundaries — data
  ownership, needless copies/transformations, structures that force special
  cases in other files.

## 6. Verify

Run the VERIFICATION STAGE from SKILL.md over the consolidated findings.
Sub-agent line numbers are untrusted input — ground every `file:line` you will
cite against the actual file before publishing it.

## 7. Report

Write the final review yourself in the disposition-grouped format of
`references/dispositions.md` (CURRENT OBJECTIVE → BLOCKERS → CURRENT-SCOPE
IMPROVEMENTS → FOLLOW-UPS → OBSERVATIONS → REJECTED → RISK ACCEPTED → VERDICT →
RISK), with **[CONFIRMED]** / **[PLAUSIBLE]** labels on each finding. The verdict
is computed from the BLOCKER count alone. Add a short
**Coverage** note: which files went to which reviewer, which specialist passes
ran, and anything excluded — so gaps are visible instead of silent.
