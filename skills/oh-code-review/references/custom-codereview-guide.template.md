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
