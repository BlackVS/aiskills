---
name: architecture-review
description: 'Review the architecture and design of one or more related repositories: goals, boundaries, input/output artifacts, data flow, consumer contracts, failure modes, and risk. Use when asked to "review the architecture", "analyze the design", "how is X used by Y", or to assess a system rather than a single change. Reuses the reviewer stance and risk framework from the oh-code-review skill and the prose rules from oh-technical-writing.'
triggers:
- /archreview
- architecture review
- design review
---

# Architecture Review

Review a system, not a diff. Adopt the reviewer stance from `../oh-code-review/SKILL.md`
(data structures first, simplicity, pragmatism, "never break userspace", no
bikeshedding) and grade risk with `../oh-code-review/references/risk-evaluation.md`.
Write the report following `../oh-technical-writing/SKILL.md`. Do not modify code.

## 1. Fix the scope before reading code

Write down, in one paragraph each, and confirm against the repositories rather
than the README:

- **Goal**: what problem the system solves and for whom. Quote the README, then
  state what the code actually optimises for if the two differ.
- **Inputs**: every external input, where it comes from, its format, who owns
  the schema, how often it changes, and what happens when it is missing.
- **Outputs / artifacts**: every artifact produced (files, packages, indexes,
  manifests, release assets), its format, where it is published, who consumes
  it, and the contract the consumer relies on (paths, naming, checksums, fields).
- **Consumers**: each downstream project, the exact integration points (scripts,
  URLs, file paths, env vars), and whether the coupling is by contract or by
  accident.

Produce a table `artifact | producer | consumer(s) | contract | how a break shows up`.

## 2. Trace the real data flow

Follow one real artifact end to end, from source input through build/CI to the
consumer that reads it. Use `grep -RIn` across all repositories for artifact
names, paths and env vars. Record where the flow is:

- implicit (a path or naming convention both sides happen to agree on),
- duplicated (the same logic or constant maintained in two repositories),
- one-way only (consumer cannot verify what it received: no checksum, version or
  generation marker),
- manual (a human step between two automated steps).

Every one of these is a finding. Prefer citing `file:line` in each repository.

## 3. Evaluate against the code-review principles

For the system as a whole, answer the Three Questions from code-review: is this
solving a real problem, is there a simpler way, what will this break. Then check:

- **Data structures and ownership**: is there one source of truth per concept?
  Where is the same fact stored twice? Who may write it?
- **Boundaries**: can each repository be changed and released independently?
  Which changes require lockstep releases, and is that documented?
- **Special cases**: per-distribution, per-branch or per-environment branches
  in the logic that a better structure would eliminate.
- **Failure modes**: what happens on partial output, stale cache, missing input,
  network failure, concurrent runs. Is failure loud or silent?
- **Verification**: which claims does CI actually prove? Which "tests" only check
  that a script ran? What has never been exercised end to end?
- **Operability**: how does an operator know the last run was correct, and how
  do they rebuild from scratch?

## 4. Already-known issues

Collect known issues and fixes from issues, PRs, commit messages and the user.
For each, state whether the fix addressed the cause or the symptom, and whether
the same class of bug can still occur elsewhere in the flow.

## 5. Output format

Lead with a two-sentence verdict. Then:

**Risk rating** for the system using the 🟢 / 🟡 / 🔴 scale from
`oh-code-review/references/risk-evaluation.md`, with the two or three factors that
drove it.

**Architecture summary**: goal, the artifact table from section 1, and a
plain-prose description of the real data flow from section 2.

**Findings**, ordered by consequence, each with: what, where (`repo/path:line`),
why it matters (mechanism and consequence, not a label), and the simplest fix
that removes the cause. Group as *Critical* (breaks consumers or silently
produces wrong artifacts), *Structural* (duplication, implicit contracts, lockstep
coupling), *Operational* (verification and observability gaps).

**Recommendations**: at most five, ordered by value over effort, each one
sentence plus the finding numbers it resolves.

Do not manufacture findings. If a part of the design is sound, say so in one
sentence and move on. Skip style. Skip anything a linter would catch.
