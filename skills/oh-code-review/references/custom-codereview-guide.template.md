---
name: custom-codereview-guide
description: Repository-specific review guidance read by oh-code-review before every review: local exceptions, always-high-risk paths, and observed runtime contracts that settle otherwise speculative findings.
triggers:
- /oh-codereview
---

# Review guide for <repository>

Read by `oh-code-review` before every review of this repository. Keep it short
and evidence-based; every entry names how it was established.

## Local exceptions

- <"Security concerns about X do not apply here because Y" — with the reason.>

## Always high-risk paths

- <directory or file patterns whose changes always take the ultra gate>

## Plain paths and compound-fault states

The documented users and the commands or requests that make this repository's
plain paths, so a reviewer can tell them from a compound-fault state
(`references/dispositions.md`, "Plain path and compound-fault state"):

- <documented user — command or request, and the states its documented paths leave>

The default form of a follow-up for a compound-fault state (a task that fixes it
in code, or an entry in the operator's recovery document that says how to see the
state and how to recover from it):

- <for example: a recovery entry in docs/RECOVERY.md, no code; or a task per
  surface; or nothing below a stated severity>

## Observed Runtime Contracts

Do not infer runtime identity from image source or workflow YAML alone.

For each accepted contract, record:

- exact image or runtime;
- relevant agent/backend classes;
- date and pipeline used for observation;
- observed UID/GID, mount ownership, or platform behavior;
- whether the result applies to all routes or only named routes.

If current evidence covers the reviewed route, use it. If the route differs or
the evidence is stale after runtime configuration changes, request a focused
probe instead of proposing speculative production hardening.

| Contract | Image / runtime | Routes | Observed | Date · pipeline |
| --- | --- | --- | --- | --- |
| <e.g. workflow steps run as UID 0> | <image:tag> | <all Docker agents / named> | <euid=0; new 0600 file owned root:root> | <YYYY-MM-DD · repo pipeline N> |

## Rejected hypotheses

Findings disproved by a reproducer; a later review must not reopen them without
new evidence.

- <hypothesis> — disproved <date>, <pipeline/command>, observed <fact>.
