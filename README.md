# Review skills — portable copies for architecture and code review

Skills taken from `OpenHands/extensions` (see `UPSTREAM.txt` for the commit),
plus skills written here. The upstream skills carry an `oh-` prefix so they
never shadow built-in skills of the same name (Claude Code ships its own
`/code-review`, and the OpenHands public catalog has `code-review`,
`code-simplifier`, ...). `architecture-review` and `verify-delivery` are local
and unprefixed.
All use the common `SKILL.md` format (YAML
frontmatter with `name` and `description`, then instructions), so the same folder
works in Claude Code, OpenHands and any other agent that reads `SKILL.md`.

| Skill | Source | Use it for |
| --- | --- | --- |
| `architecture-review` | written here | Reviewing a system across repositories: goals, artifacts, data flow, consumer contracts, risk. Builds on the two below. |
| `oh-code-review` | extensions (`code-review`) | Rigorous review of a change: data structures, complexity, breaking changes, real security, test quality. Ships `references/risk-evaluation.md` and `references/supply-chain-security.md`. |
| `oh-technical-writing` | extensions (`technical-writing`) | Report style: answer first, complete arguments, prose over fragments. Named for architecture analysis. |
| `oh-code-simplifier` | extensions (`code-simplifier`) | Three checklists (`references/`) for reuse, quality and efficiency over a chosen scope. |
| `oh-agent-readiness-report` | extensions (onboarding plugin, `agent-readiness-report`) | Whole-repository assessment across five pillars, with five shell scanners. |
| `oh-improve-agent-readiness` | extensions (onboarding plugin, `improve-agent-readiness`) | Turns a readiness report's gaps into 5–10 ranked, repo-specific fixes and implements the approved ones. |
| `oh-qa-changes` | extensions (`qa-changes`) | QA a PR by RUNNING the software: env setup, exercise changed behavior as a user, PASS/FAIL report with before/after evidence. Not tests (CI's job), not reading code (review's job). |
| `oh-learn-from-code-review` | extensions (`learn-from-code-review`) | Distill merged-PR review feedback into repo skills and the `custom-codereview-guide` the reviewer reads; Gitea+GitHub; AI-reviewer comments included as signal. |
| `verify-delivery` | written here | Confirm from the forge that a PR was delivered: reviews READY at the final head, merged by a person, merged tree equal to the reviewed one, CI green on the merge commit. Read-only helper, JSON verdict and evidence. |

`prompts/` holds ready-to-fill prompts for each skill. The command stubs the
upstream ships were only "read SKILL.md, then `$ARGUMENTS`", so they were replaced
with real prompts.

The set is versioned as a whole: the current version is in `VERSION`, the
history in `CHANGELOG.md` (major = breaking rename/removal or installer flag
change, minor = new skill/prompt/installer feature, patch = content fix).
Bump both in the same change.

## Consumers

`consumers/openhands-review-hook/` holds the prompt template the hands review
receivers hand to each review conversation, versioned with the skill it
depends on, plus example site files; each deployment renders it from its own
site file. See its README for the receiver contract and the site variables.

## Quick install

No checkout needed. Linux and macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/BlackVS/aiskills/main/boot.sh | bash
```

Windows (PowerShell):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/BlackVS/aiskills/main/boot.ps1 | iex"
```

Either line fetches the archive of the latest
[release](https://github.com/BlackVS/aiskills/releases) (`main` while there is
none) into a temporary directory, runs the real installer from it, prints the
installed version and cleans up. Run it again to upgrade to a newer release. With no flags it installs user-wide with `--user -s all -p
-a`: every skill and the prompts for Claude Code (`~/.claude`) and Codex
(`~/.agents`), plus the review-gates block in the agent instructions. Any
other combination is the installer's own flags, passed with `bash -s --` on
Linux and macOS and with `AI_SKILLS_ARGS` on Windows:

```bash
B=https://raw.githubusercontent.com/BlackVS/aiskills/main/boot.sh
curl -fsSL $B | bash                                         # user level, everything (the default)
curl -fsSL $B | bash -s -- --user -s core                    # user level, the three core skills only
curl -fsSL $B | bash -s -- /path/to/project --agents-md      # project level: <project>/.claude/skills,
                                                             #   <project>/.agents/skills, gates block in AGENTS.md
curl -fsSL $B | bash -s -- /path/to/project -t codex -s all  # project level, Codex only, every skill
```

```powershell
$B = 'https://raw.githubusercontent.com/BlackVS/aiskills/main/boot.ps1'
irm $B | iex                                                               # user level, everything (the default)
$env:AI_SKILLS_ARGS = '-User -Skills core'; irm $B | iex                   # user level, core skills only
$env:AI_SKILLS_ARGS = 'C:\path\to\project -AgentsMd'; irm $B | iex        # project level, both clients, gates block
$env:AI_SKILLS_ARGS = 'C:\path\to\project -Tool codex -Skills all'; irm $B | iex   # project level, Codex only
```

(On a fresh Windows machine wrap the line as `powershell -NoProfile
-ExecutionPolicy Bypass -Command "..."` as shown above; quote a project path
that contains spaces inside `AI_SKILLS_ARGS`.) Project-level installs are
committed with the project, so every clone and every client on it sees the
same skills; user-level installs follow the person across projects.
`AI_SKILLS_REF=<tag or commit>` pins a version, and `AI_SKILLS_REF=main`
installs unreleased work; `AI_SKILLS_REPO=<owner/name>`
installs from a fork, with `AI_SKILLS_TOKEN` for a private one;
`AI_SKILLS_BASE=<url>` downloads from a Gitea mirror instead of GitHub. If the
Windows form fails with an empty-string error for `Command`, the download
returned an empty body: retry, or install from a checkout as below.

From a checkout:

```bash
git clone https://github.com/BlackVS/aiskills.git && cd aiskills
./install.sh /path/to/project                 # core skills -> <project>/.claude/skills (Claude Code, OpenCode)
                                              #             and <project>/.agents/skills (Codex, Gemini CLI)
./install.sh /path/to/project -t claude       # one client only
./install.sh /path/to/project -t openhands    # -> <project>/.openhands/skills
./install.sh /path/to/project -t all -s all -p   # every tool dir, every skill, plus prompts/
./install.sh --user                           # ~/.claude/skills and ~/.agents/skills for all projects
./install.sh /path/to/project --agents-md     # + "Code review gates" block into <project>/AGENTS.md
./install.sh --user --agents-md               # + the block into ~/.claude/CLAUDE.md (global, both tools)
./install.sh --help
```

On Windows, `install.ps1` is the same installer for PowerShell:

```powershell
git clone https://github.com/BlackVS/aiskills.git; cd aiskills
.\install.ps1 C:\path\to\project                    # core skills -> <project>\.claude\skills and <project>\.agents\skills
.\install.ps1 C:\path\to\project -Tool openhands    # -> <project>\.openhands\skills
.\install.ps1 C:\path\to\project -Tool all -Skills all -Prompts
.\install.ps1 -User                                 # ~\.claude\skills and ~\.agents\skills for all projects
Get-Help .\install.ps1 -Detailed
```

The default tool set is `claude,codex`: Claude Code reads `.claude/skills`,
Codex reads `.agents/skills`, and neither reads the other's directory, so a
skill is copied to both. `-t agents` is the older name for `codex` and still
works. Re-running replaces the selected skills, supporting files included, and
leaves anything else alone; an upgrade is the same command again after `git
pull`. Selecting `architecture-review` automatically adds the two skills it
links to.

| Platform | Installer | User-level destinations | Status |
| --- | --- | --- | --- |
| Linux | `install.sh` | `~/.claude/skills`, `~/.agents/skills` | exercised (tests in `tests/test_install.py`, CI on `ubuntu-latest`) |
| Windows | `install.ps1` (PowerShell 5.1+) or `install.sh` under Git Bash | `%USERPROFILE%\.claude\skills`, `%USERPROFILE%\.agents\skills` | exercised (CI on `windows-latest`, one-liner under Windows PowerShell 5.1); Codex 0.156 lists the installed skills |
| macOS | `install.sh` | same as Linux | exercised in CI (tests and the one-liner on `macos-latest`) |

Clients, checked by `.github/check-clients.py` (the Clients workflow): each
client lists what it discovered, without a model call, after a user-level
install, a Claude Code only install, a user-level install with
`XDG_CONFIG_HOME` set, and a project install.

| Client | Version checked | Skills | Review-gates block |
| --- | --- | --- | --- |
| Claude Code | 2.1.283 | `~/.claude/skills`, `<repo>/.claude/skills` | `~/.claude/CLAUDE.md`; `CLAUDE.md` → `@AGENTS.md` |
| Codex | 0.157.1 | `~/.agents/skills`, `<repo>/.agents/skills` | `~/.codex/AGENTS.md`; `AGENTS.md` |
| OpenCode 1.x | 1.18.32 | all of the above; prompts as `/` commands | `~/.config/opencode/AGENTS.md`; `AGENTS.md` |
| OpenCode v2 | 2.0.18 | all of the above; prompts as `/` commands | `~/.config/opencode/AGENTS.md`; `AGENTS.md` |
| Gemini CLI | 0.61.0 | `~/.agents/skills`, `<repo>/.agents/skills` (project skills in trusted folders only) | `~/.gemini/GEMINI.md`; `GEMINI.md` → `@AGENTS.md` |

### Cloud agents (Claude Code on the web, other hosted sessions)

A cloud session starts from a fresh container, so a user-level install from
an earlier session is gone. Put the one-liner in the environment's setup
script (for Claude Code on the web: the environment's settings, *Setup
script*), which runs before the session starts, so the skills are in
`~/.claude/skills` and `~/.agents/skills` when the agent loads them:

```bash
curl -fsSL https://raw.githubusercontent.com/BlackVS/aiskills/main/boot.sh | bash
```

Every session in that environment then has the skills, whichever repository it
works on. The alternative is a project-level install committed to the
repository (`curl -fsSL $B | bash -s -- . --agents-md` from its root), which also serves
everyone who clones it.

### Releases

`main` only changes through reviewed pull requests, and a release is a
`vX.Y.Z` tag on a commit of `main`. A release starts as a PR that rolls
`## [Unreleased]` into the new version and bumps `VERSION`. After it merges,
run **Actions → Release → Run workflow** on `main` with the version (for
example `1.22.1`). The workflow checks that `VERSION` and a `CHANGELOG.md`
section match, runs the checks, tags the tip of `main`, and publishes the
GitHub release with that section as its notes and the tree as `.tar.gz` and
`.zip` with `SHA256SUMS`. Pushing the tag yourself does the same:

```bash
git fetch origin && git tag -a v1.22.1 origin/main -m "aiskills 1.22.1" && git push origin v1.22.1
```

Releases run one at a time, and only the highest version is marked latest,
so a patch for an older line never becomes what the one-liners install. To
publish an existing tag again after a failed or cancelled run, run the
workflow with its version.

The sections below explain what each tool does with the files.

## Review gates

`agents/review-gates.md` says which review level each gate uses (pre-push
medium, pre-merge max, sensitive surfaces ultra, docs-only high) and the rules
around it. `--agents-md` / `-AgentsMd` writes it into the agent instructions as
a managed block between `<!-- aiskills:review-gates start -->` and `end`
markers; re-running replaces the block and leaves the rest of the file alone.
A block written before 1.27.0, when the set was named `ai-skills`, carries the
`<!-- ai-skills:review-gates start -->` marker and is replaced the same way.

Where it goes, and why:

- **Project**: `<repo>/AGENTS.md`. OpenCode and Codex read `AGENTS.md`
  (OpenCode v2 reads nothing else; v1 fell back to `CLAUDE.md`). Claude Code
  reads `CLAUDE.md` and Gemini CLI `GEMINI.md`, and both resolve `@file`
  imports, so the installer also makes sure `<repo>/CLAUDE.md` and
  `<repo>/GEMINI.md` each contain an `@AGENTS.md` import, creating a one-line
  file where there is none.
- **User (`--user`)**: `~/.claude/CLAUDE.md` for Claude Code, always. Each
  other client gets its own global file when its directory exists (that is,
  the client has run on this machine), created if absent, and nothing is
  created for a client that is not there:
  - OpenCode: `~/.config/opencode/AGENTS.md`. OpenCode v2 reads only
    `AGENTS.md`, the global one first and then every `AGENTS.md` from the
    working directory up to home, and no longer falls back to
    `~/.claude/CLAUDE.md` as v1 did.
  - Codex: `~/.codex/AGENTS.md`, its global instructions, with no fallback to
    the Claude or OpenCode files.
  - Gemini CLI: `~/.gemini/GEMINI.md`, its global context file.

Edit the text in `agents/review-gates.md` and re-run the installer; never edit
inside the markers.

The external-reviewer label protocol (`review-this` → `hands-reviewing` →
`hands-reviewed`, re-triggering, and per-PR model selection with
`review-this:<profile>` on sites that support it) is documented once, in
`skills/oh-code-review/references/external-review-labels.md`; the managed
block only carries the short rules and points there. The reference includes
the rule for telling a selection-capable site from a fixed-reviewer one (the
site's label list decides) and a known-sites table — update that table when a
deployment changes.

## Enabling in a Claude Code project

Project-scoped (only this repo sees them):

```bash
mkdir -p /path/to/project/.claude/skills
cp -r skills/architecture-review skills/oh-code-review skills/oh-technical-writing /path/to/project/.claude/skills/
```

User-scoped (every project on this machine):

```bash
mkdir -p ~/.claude/skills
cp -r skills/* ~/.claude/skills/
```

Claude Code lists them as `/architecture-review`, `/oh-code-review`, and so on, and
also loads a skill on its own when the request matches the `description`.
The same `.claude/skills` and `~/.claude/skills` paths are also read by
OpenCode, see below. The
`triggers:` key in the frontmatter is OpenHands-specific and is ignored here.
The `oh-` prefix keeps the built-in `/code-review` available alongside.

Keep `references/` and `scripts/` next to each `SKILL.md`: the skills refer to
them by relative path. `architecture-review` refers to its siblings as
`../oh-code-review/...` and `../oh-technical-writing/...`, so install those three
together.

## Enabling in OpenHands (Agent Canvas / agent-server)

Project skills, loaded for conversations whose workspace is that repository:

```bash
mkdir -p /path/to/repo/.openhands/skills
cp -r skills/architecture-review skills/oh-code-review skills/oh-technical-writing /path/to/repo/.openhands/skills/
```

User skills on the OpenHands host, loaded for every conversation:

```bash
mkdir -p ~/.openhands/skills
cp -r skills/* ~/.openhands/skills/
```

In the agent profile the corresponding loader must be on: `load_project_skills`
or `load_user_skills` in Settings → Agent (both were `false` on a test host on
2026-09-05; the public catalog was still reaching conversations through the GUI).
Because of the prefix these do not override the public catalog copies; both
stay available. The `triggers:` lines make `/archreview`, `/oh-codereview`,
`/oh-simplify` work as slash commands in the chat.

## Enabling in OpenCode

OpenCode (v1 and v2) discovers skills from several directories, and two of them
are the Claude Code and Codex paths, so the default install already serves it.

Project-level, any of:

```
.opencode/skills/<name>/SKILL.md
.claude/skills/<name>/SKILL.md      # shared with Claude Code
.agents/skills/<name>/SKILL.md      # shared with Codex / Gemini CLI
```

User-level, any of:

```
~/.config/opencode/skills/<name>/SKILL.md
~/.claude/skills/<name>/SKILL.md    # shared with Claude Code
~/.agents/skills/<name>/SKILL.md
```

So for a project that is used with both Claude Code and OpenCode, the Claude
Code install above is enough. For OpenCode only:

```bash
mkdir -p /path/to/project/.opencode/skills
cp -r skills/architecture-review skills/oh-code-review skills/oh-technical-writing /path/to/project/.opencode/skills/
```

Rules that matter here:

- The directory name is the skill's ID. v1 requires it to match
  `^[a-z0-9]+(-[a-z0-9]+)*$` and equal the frontmatter `name`, with a
  `description` of 1 to 1024 characters; v2 recommends the same pattern at 1 to
  64 characters for portable skills, treats `name` as a display label and
  enforces neither the pattern nor a description length. Every skill here
  satisfies the stricter v1 rules. `triggers:` is ignored, as in Claude Code.
- The agent loads a skill on demand through its native `skill` tool, choosing
  it from the `description`: v1 takes `skill({ name: "oh-code-review" })`, v2
  `skill({ id: "oh-code-review" })`.
  In v2 the same skills also appear in the slash-command catalog, so
  `/oh-code-review` works as a user command (frontmatter `slash: false` hides
  one; `metadata: {opencode/autoinvoke: false}` keeps it out of the model's
  list). v1 has no slash form: just ask, or name the skill in the prompt.
- When two sources define the same ID, the later-registered one wins: v2
  registers `.claude/skills` first, then `.agents/skills`, then
  `~/.config/opencode/skills`, then the project's `.opencode/skills`. Installing
  the same skill in more than one of them is harmless; they are identical
  copies.
- **Prompts as commands**: with `-p`, the `opencode` tool copies `prompts/*.md`
  into `.opencode/commands/` (project) or `~/.config/opencode/commands/`
  (user), where each file is a `/<name>` command (`/code-review`, ...). A
  user-level `-p` run also does this without `-t opencode` whenever
  `~/.config/opencode` exists. v2's preferred directory is `commands/`; both
  versions read it (v1 also read the singular `command/`).
- **Instructions**: v2 reads `AGENTS.md` only (global
  `~/.config/opencode/AGENTS.md`, then every `AGENTS.md` from the working
  directory up to home); the `--agents-md` rules above put the gates block
  there.
- Skills can be gated per name in `opencode.json`:

  ```json
  {
    "permission": {
      "skill": { "*": "allow", "oh-agent-readiness-report": "ask" }
    }
  }
  ```

  Built-in agents can override this under `agent.<name>.permission.skill`, or
  turn skills off entirely with `"tools": { "skill": false }`.
- Only `SKILL.md` is injected when loaded. `references/` and `scripts/` are read
  by the agent with its normal file tools, so keep them in place next to
  `SKILL.md` and keep the three sibling skills together as noted above.

Docs: https://opencode.ai/v2/docs/skills/, https://opencode.ai/v2/docs/instructions/,
https://opencode.ai/v2/docs/commands/ and https://opencode.ai/v2/docs/migrate-v1/
(checked 2026-09-24); v1: https://opencode.ai/docs/skills/ (checked 2026-09-05).
Exercised with OpenCode 1.18.3 on Windows (`opencode debug skill` lists the
skills from all three project directories and from the user-level ones), and
on Linux by the Clients workflow with 1.18.32 and v2.0.18: both list the
skills from `.agents/skills` and, when that is the only copy, from
`~/.claude/skills`, and both register the prompts in
`~/.config/opencode/commands` as `/` commands. v2 installs separately from
1.x (`npm install -g @opencode/cli`, or `curl -fsSL https://opencode.ai/v2/install | bash`;
see https://opencode.ai/download). Its server API lists skills (`/api/skill`)
and commands (`/api/command`) as separate catalogs, so whether skills also
appear among the `/` commands is taken from the v2 documentation, not
observed. 1.x and v2 keep their data in the same place, and 1.x refuses to
start on data v2 has written ("Database is not empty and has no session
table"): use one version per home directory. Both read their config from
`$XDG_CONFIG_HOME/opencode` when `XDG_CONFIG_HOME` is set, and the installers
write there too; `~/.config/opencode` below stands for that directory.

## Enabling in Codex CLI

Codex discovers skills, in this order, from `.agents/skills/` in the working
directory, its parent folders and the repository root, then `$HOME/.agents/skills/`
(`%USERPROFILE%\.agents\skills` on Windows), then `/etc/codex/skills`, then its
own built-ins (`~/.codex/skills/.system`, not a place for ours). The `codex`
tool, part of the default set, installs there:

```bash
./install.sh /path/to/project                # default set: Claude Code and Codex
./install.sh --user -t codex                 # Codex only, every repo on this machine
```

Docs: https://developers.openai.com/codex/skills (checked 2026-09-24). Verify
after installing: restart Codex, type `/skills`; headless,
`codex exec --skip-git-repo-check "List the skills available to you, one per line"`
names them. A skill whose frontmatter is not strict YAML (an unquoted `: ` in
`description`, say) is silently skipped by Codex while Claude Code still loads
it; `tests/test_skill_frontmatter.py` catches that.

How the pieces land in Codex:

- **Skills**: picked implicitly from the `description`, or explicitly — type
  `$` to mention a skill by name, `/skills` to list what's loaded. Frontmatter
  needs `name` + `description`, which every skill here has; `triggers:` is
  ignored. Skills load at session start, so restart Codex after installing.
- **Review gates block**: project-level, Codex natively reads `<repo>/AGENTS.md`,
  which `--agents-md` writes — nothing extra needed. User-level it reads only
  `~/.codex/AGENTS.md`; `--user --agents-md` writes the block there, creating
  the file, whenever `~/.codex` exists (see "Review gates" above).
- **Prompts**: `--user --prompts` also copies `prompts/` to `~/.codex/prompts/`
  when `~/.codex` exists; each file becomes a `/prompts:<name>` command in the
  CLI. Codex has deprecated custom prompts in favor of skills, but they still
  work and are the closest match for our fill-in prompt texts.

Docs: https://developers.openai.com/codex/skills (checked 2026-09-11).

## Other agents

Gemini CLI reads the same `SKILL.md` layout from `.agents/skills/` (repository)
and `~/.agents/skills/` (user), which the default tool set already fills; list
them with `gemini skills list`. It loads project skills and project context
only in a folder you have trusted. Its instructions file is `GEMINI.md`, not
`AGENTS.md`: `--agents-md` gives `<repo>/GEMINI.md` an `@AGENTS.md` import,
and with `--user` writes the block into `~/.gemini/GEMINI.md` when `~/.gemini`
exists. Anything else: paste the `SKILL.md` body into the system prompt or
`AGENTS.md`.

## Verifying delivery

`verify-delivery` is for an agent that has to record a pull request as
delivered, typically a team coordinator writing evidence into its team
system. It asks the forge, read-only, and confirms four things: the required
reviews are `READY_FOR_HUMAN_MERGE` at exactly the final head (by default the
local `oh-code-review` pre-merge review and an external reviewer's, in the
`[<reviewer> review] reviewed at head <sha>` format, posted by an author the
repository trusts: a listed account, or on GitHub an owner, member or
collaborator), the PR was merged by a
person, the merged content is the reviewed content (the same tree, or after a
base-only update the same patch identity), and CI on the merge commit is green. Its helper, `skills/verify-delivery/verify_delivery.py`,
uses only the Python standard library and works against GitHub and Gitea:

```bash
GITHUB_TOKEN_FILE=/path/to/read-only-token python3 skills/verify-delivery/verify_delivery.py \
  --pr https://github.com/OWNER/REPO/pull/123
```

It prints one JSON document (`verdict`, `checks`, and `evidence` only when
confirmed) and exits 0 confirmed, 3 not confirmed, 4 pending or retryable, 2
usage error. The token is read from the file an environment variable names,
never from the command line, and never appears in the output. Review patterns,
review author allowlists, the required count, a merger allowlist, bot accounts and known-flaky checks are
options; `SKILL.md` documents them and how to read the result. The helper is
tested offline against recorded API responses for both forges
(`tests/test_verify_delivery.py`).

## Compatibility rules

The set is used from Linux, Windows and macOS (all three in CI), in
Claude Code, OpenCode and Codex. Every change must keep working in all
combinations (`python3 -m unittest discover tests` runs the checks):

- **No symlinks.** Copy shared files instead (see the criteria.md note below).
  Symlinks do not survive a Windows checkout or `cp` on some filesystems.
- **LF line endings** for `*.sh` and every `*.md`, enforced by `.gitattributes`.
  Bash will not run a CRLF script.
- **Executable bit** on `install.sh` and every `scripts/*.sh`, committed with
  `git update-index --chmod=+x <file>` so it survives a fresh clone.
- **Two installers, one feature set.** Any flag added to `install.sh` is added to
  `install.ps1` in the same change, and both are tried on a scratch repo.
- **Scripts shipped inside a skill** are bash today; the skill text must say
  how to run them on Windows (Git Bash or WSL) until a PowerShell port exists.
- **Tool-neutral frontmatter.** `name` matches `^[a-z0-9]+(-[a-z0-9]+)*$` and
  equals the directory name, `description` is 1 to 1024 characters. Those are
  OpenCode's documented limits, and every skill here stays within them, which
  Claude Code loads without complaint. `triggers:` is OpenHands-only and
  ignored elsewhere, so nothing may depend on it. The frontmatter must be
  strict YAML: a `description` containing `: ` is quoted, because Codex drops a
  skill it cannot parse while Claude Code loads it anyway.
- **Relative paths only** inside skills; cross-skill references use `../<name>/`.
- **Trigger by description, not by slash command.** OpenCode has no slash
  commands, so each `description` must be enough for the agent to pick the
  skill on its own.

## Keeping in sync with upstream

```bash
git clone --depth 1 https://github.com/OpenHands/extensions /tmp/ext
diff -ru /tmp/ext/skills/code-review skills/oh-code-review
```

Upstream locations: `skills/code-review`, `skills/code-simplifier` and
`skills/technical-writing` at the repo root; `agent-readiness-report` and
`improve-agent-readiness` under `plugins/onboarding/skills/`. In those two,
upstream's `references/criteria.md` is a symlink to the shared
`plugins/onboarding/references/criteria.md`; this repo stores it as a real file
in each skill (symlinks do not survive on Windows), so diff against the shared
file. Expect small diffs from the `oh-` renames and cross-reference updates.

`architecture-review` and `prompts/` are local and have no upstream.

## Process sets

[The aimem process set](processes/aimem/README.md) contains a Git-pinned handbook,
five-item READY/DONE checklists and task templates for aimem session bootstrap.
Selecting a process does not install or update skills.

## License

Two licenses apply. The upstream-derived skills under `skills/oh-*/` keep the
MIT license of [OpenHands/extensions](https://github.com/OpenHands/extensions)
for their upstream portions; everything else, including the changes made here,
is under the [PolyForm Noncommercial License 1.0.0](LICENSE). See
[`NOTICE.md`](NOTICE.md) for which license covers which files, and
[`LICENSES/MIT-OpenHands.txt`](LICENSES/MIT-OpenHands.txt) for the upstream
notice.
