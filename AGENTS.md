# Working on this repository (for coding agents)

This repository is **public**. Read this before changing anything.

## What it is

A versioned set of agent skills (`skills/`), ready-to-fill prompts
(`prompts/`), the review-gates block the installers write into agent
instructions (`agents/review-gates.md`), the installers (`install.sh`,
`install.ps1`, and the one-line `boot.sh`, `boot.ps1`), and the review-hook
consumer for OpenHands deployments (`consumers/openhands-review-hook/`). The
`README.md` explains each part; `NOTICE.md` says which license covers which
files.

## Nothing private goes into the repository

Everything committed or posted here is published. That covers files, file
names, commit messages, PR descriptions, review comments, changelog entries
and quoted logs or commands. Never include:

- private hostnames, domains or IP addresses, or the names of private
  organizations, bots, machines, hubs, deployments or backends;
- credential names, token variable names that identify a private setup, or
  credential file paths;
- session or conversation URLs.

Describe environments generically instead: "the private Gitea", "a hands
deployment", "site A". The files in `consumers/openhands-review-hook/sites/`
are neutral examples; a real deployment's site file, with its real hosts and
bot account, stays in that deployment's private configuration. When unsure
whether a name is public, leave it out.

## Versioning

The set is versioned as a whole. Every change that ships bumps `VERSION` and
adds a `CHANGELOG.md` entry in the same commit (major: a skill renamed or
removed, or installer flags changed; minor: a new skill, prompt or installer
feature; patch: a content fix). Unreleased work goes under `## [Unreleased]`.
A change a hands site must act on also gets a row in
`consumers/openhands-review-hook/UPDATING.md`.

## Checks before every commit

```sh
python3 -m unittest discover -s . -p "test_*.py" -t .              # installers, boot, changelog, skills
python3 -m unittest discover -s consumers/openhands-review-hook/runtime -p "test_*.py"
python3 consumers/openhands-review-hook/render.py --check          # every example site renders
```

The boot tests install from a local archive (`AI_SKILLS_ARCHIVE`) and run
under both bash and PowerShell when both are present. `.gitattributes` forces
LF for shell scripts and Markdown, and PowerShell scripts are stored with LF
too; keep them that way.

## Upstream skills

`skills/oh-*/` are derived from OpenHands/extensions (MIT, see `UPSTREAM.txt`
and `NOTICE.md`). Keep the `oh-` prefix, keep the MIT notice in
`LICENSES/MIT-OpenHands.txt`, and record a new upstream sync in
`UPSTREAM.txt`. A new file added under an `oh-*` skill is local work; list it
in the `NOTICE.md` table.

## Review and merge

Changes go through pull requests on GitHub. The review gates are the ones the
set itself defines in `agents/review-gates.md`, with the `oh-code-review`
skill:

- before every push: a review at level **medium**, every finding fixed or
  explicitly waived before the push;
- before merge: a review at level **high**, posted to the PR, plus the
  external reviewer where one is configured (the `review-this` label). A new
  commit or a rebase makes both stale.

Open a PR as a draft and mark it ready only when both gates are green at the
current head. The merge stays with the maintainer. Repository-specific review
context lives in `.agents/skills/custom-codereview-guide/SKILL.md`; the
reviewer reads it before every review.
