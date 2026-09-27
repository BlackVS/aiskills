# Prompt: architecture review of a producer repo and its consumers

Use with the `architecture-review` skill loaded. Replace the bracketed parts.
Give the agent local checkouts (or clone access) of every repository named.

---

Review the architecture and design of [PRODUCER REPO, e.g. kernel-headers-catalog].

Scope:
- Goals of the project and what it optimises for in practice.
- Every input artifact and every output artifact: format, location, owner of the
  schema, and the contract consumers rely on.
- How it is consumed by [CONSUMER REPOS, e.g. app-a, app-b, app-c].
  Trace the exact integration points in code, not in docs.
- Known issues and fixes so far: [LIST OR LINK ISSUES / PRs]. For each, say whether
  the fix removed the cause or only the symptom.

Method:
- Follow one real artifact end to end, from input through CI to the consumer that
  reads it. Cite `repo/path:line` for every claim.
- Check branches [BRANCHES, e.g. main and feat/*] where the integration differs.
- Do not modify any repository.

Deliver the report in the architecture-review output format: verdict, risk rating,
artifact table, real data flow, findings ordered by consequence, at most five
recommendations. Prose over bullet fragments; no style notes.
