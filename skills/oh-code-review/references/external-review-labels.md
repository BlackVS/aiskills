# External review by label (hands / OpenHands)

How to ask a self-hosted OpenHands reviewer ("hands") for an independent
review of a pull request, and how to read what comes back. This is the
protocol side of `oh-code-review`: the reviewer on the other end runs this
same skill at level **high**, from outside your session, on the PR head it
checks out itself.

## Lifecycle

```
review-this  →  hands-reviewing  →  hands-reviewed
 (request)      (in progress)       (review comment posted)
```

1. Add the **`review-this`** label to the PR. Nothing else — no comment, no
   mention.
2. hands swaps it to **`hands-reviewing`** when it picks the PR up (seconds
   on a webhook-driven site, up to a couple of minutes on a polling one).
3. hands posts ONE comment starting with `[hands-bot review] reviewed at head
   <sha>` and lands **`hands-reviewed`**. The comment ends with a signature
   line naming the model that produced it.

Failures are never silent: if the review could not run, a
`⚠️ [hands-bot review] could not run: …` comment appears, the working label
is cleared and, on sites that define it, **`hands-review-failed`** is set —
re-add `review-this` to retry (it clears the failure label). A slow review
is not a failure: the receiver waits past its soft deadline while the
conversation is still running, so leave a `hands-reviewing` PR alone.

## What the labels mean (and do not mean)

- The labels are the **progress signal, never the verdict**. `hands-reviewed`
  says a review EXISTS for the head named in the comment; the findings still
  have to be read and acted on.
- Never hand-edit `hands-reviewing` or `hands-reviewed`; hands owns them.
- Do not re-add `review-this` while `hands-reviewing` is present.
- Any later commit or rebase makes the review stale: re-trigger by adding
  `review-this` again and clear the stale `hands-reviewed` when you do. The
  reviewer is told to treat a PR with earlier `[hands-bot review]` comments as
  a RE-review (did the previous findings get addressed? what changed?).
- Adding the label means **watching for the result**: start a background
  watch and act on the findings when the comment lands — never leave a
  triggered review for the user to discover.

## Choosing the reviewer model — only where the site supports it

Some deployments let the requester pick which model reviews, per PR, with a
**variant label** `review-this:<profile>` (e.g. `review-this:codex-sol`);
plain `review-this` then runs that site's default model. Older deployments
have one fixed reviewer and know only `review-this`.

**Resolve this from the site, never from memory:** list the labels of the
org (or repo, on GitHub) before requesting.

- `review-this:<profile>` labels exist → selection is supported; the
  suffixes ARE the available profiles (they are named `<backend>-<model>`,
  e.g. `claude-fable`, `codex-astra`, `codex-sol`, `cc-sol`).
- Only `review-this` exists → the reviewer is fixed; use `review-this` and
  nothing else. Do **not** create a variant label yourself — on a site
  without the feature it is an ordinary label that triggers nothing, and the
  PR sits unreviewed.

When to use a variant, where available: the user asked for a specific model,
or the site default would repeat the model family that did the local pass
(the external review exists to be a *different* reader). Site defaults are
chosen with that in mind — on site A below the default is GPT (`codex-astra`),
so a Claude Code session's plain `review-this` already gets the other family,
while a Codex/GPT-driven session should ask for `review-this:claude-fable`.
Otherwise use plain `review-this`: every review costs a subscription or API
budget, and the default was set on purpose.

The signature line at the end of the review comment names the model that
actually ran (stamped by the site's automation from its configuration, so
it is trustworthy even where the model itself would misreport its identity).

Example sites (the two deployment styles in use; the label list is still the
authority):

| Site | Reviewer selection | Notes |
| --- | --- | --- |
| Site A: `hands` on Agent Canvas (a Gitea org + polled GitHub repos) | yes — `review-this:<profile>`, default `codex-astra` (GPT) | Agent Canvas; profiles = Canvas agent profiles: `claude-fable`, `codex-astra`, `codex-sol`, `cc-sol` |
| Site B: OpenHands classic app + original review hook | no — `review-this` only, fixed model | do not use variant labels there |

## Where it applies

- Gitea: org-wide labels; every repo of the org that hands can clone.
- GitHub: only repos hands polls (it cannot receive webhooks from GitHub).
  Labels are per repo; hands creates the full request set (`review-this`
  plus every `review-this:<profile>`) in each polled repo that has an open
  PR, within one polling interval. So on GitHub a missing variant means
  either the site has not seen the repo yet (open the PR, wait ~2 minutes,
  re-list) or the profile does not exist — not a reason to create it.

## Reading the review

Reviews use this skill's CRITICAL REVIEW OUTPUT FORMAT: taste rating,
CRITICAL ISSUES / IMPROVEMENT OPPORTUNITIES / TESTING GAPS, RISK
ASSESSMENT, VERDICT, KEY INSIGHT — findings tagged [CONFIRMED] /
[PLAUSIBLE] by the verification stage. Treat it as one more reader, not an
oracle: the local pass and the external pass have different blind spots,
which is why the merge gate requires both.
