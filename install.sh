#!/usr/bin/env bash
# Install the review skills into a project (or user-level) skills directory.
#
# Usage: ./install.sh <target-repo> [options]
#        ./install.sh --user [options]
#
# Options:
#   -t, --tool <name>     Where to install. Repeatable. One of:
#                           claude    -> <repo>/.claude/skills      (Claude Code; also read by OpenCode)
#                           codex     -> <repo>/.agents/skills      (Codex CLI/IDE; also Gemini CLI, OpenCode)
#                           agents    -> the same directory as codex (older name, still accepted)
#                           opencode  -> <repo>/.opencode/skills
#                           openhands -> <repo>/.openhands/skills
#                           all       -> claude, codex, opencode, openhands
#                         Default: claude,codex. Claude Code and Codex read different
#                         directories, so a skill has to be copied to both.
#   -s, --skills <list>   Comma-separated skill names, or "core" (default) or "all".
#                           core = architecture-review,oh-code-review,oh-technical-writing
#   -p, --prompts         Also copy prompts/ to <repo>/.claude/prompts (or the tool's dir;
#                           for opencode the commands dir, so each prompt is a /<name> command).
#                           With --user, also to ~/.codex/prompts when ~/.codex exists
#                           (Codex custom prompts: /prompts:<name> in the CLI) and to
#                           ~/.config/opencode/commands when ~/.config/opencode exists.
#   -a, --agents-md       Also write the "Code review gates" block (agents/review-gates.md)
#                         into the agent instructions, between markers, replacing any
#                         previous copy:
#                           project: <repo>/AGENTS.md (created if absent); <repo>/CLAUDE.md
#                                    and <repo>/GEMINI.md get an "@AGENTS.md" import (each created
#                                    with just that import if absent), so Claude Code and Gemini
#                                    CLI read the block too.
#                           --user:  ~/.claude/CLAUDE.md (Claude Code), plus, each created if
#                                    absent: ~/.config/opencode/AGENTS.md when ~/.config/opencode
#                                    exists (OpenCode v2 reads only AGENTS.md), ~/.codex/AGENTS.md
#                                    when ~/.codex exists (Codex global instructions) and
#                                    ~/.gemini/GEMINI.md when ~/.gemini exists (Gemini CLI).
#   -u, --user            Install user-level instead of into a repo:
#                           claude -> ~/.claude/skills, codex -> ~/.agents/skills,
#                           opencode -> ~/.config/opencode/skills, openhands -> ~/.openhands/skills
#                           (OpenCode's ~/.config/opencode is $XDG_CONFIG_HOME/opencode when that is set)
#                           (~ is $HOME; on Windows, run install.ps1, which uses the user profile)
#   -n, --dry-run         Show what would be done.
#   -h, --help
#
# Re-running replaces previously installed copies of the same skills (idempotent):
# each selected skill directory is removed and copied again, supporting files
# (references/, scripts/) included, so an upgrade is the same command again.
# Skills not in the selected set are left untouched.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/skills"
CORE="architecture-review,oh-code-review,oh-technical-writing"

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
die() { echo "install.sh: $*" >&2; exit 1; }

# OpenCode reads its config from $XDG_CONFIG_HOME/opencode, ~/.config/opencode when unset
OC_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
repo=""; tools=(); skills="core"; prompts=0; user=0; dry=0; agentsmd=0; pdests=""
while [ $# -gt 0 ]; do
  case "$1" in
    -t|--tool) tools+=("$2"); shift 2;;
    -s|--skills) skills="$2"; shift 2;;
    -p|--prompts) prompts=1; shift;;
    -a|--agents-md) agentsmd=1; shift;;
    -u|--user) user=1; shift;;
    -n|--dry-run) dry=1; shift;;
    -h|--help) usage 0;;
    -*) die "unknown option: $1 (see --help)";;
    *) [ -z "$repo" ] || die "unexpected argument: $1"; repo="$1"; shift;;
  esac
done
[ ${#tools[@]} -gt 0 ] || tools=(claude codex)
# normalize: comma lists, the older name, "all", duplicates (codex and agents share a directory)
resolved=()
for t in "${tools[@]}"; do
  IFS=, read -r -a parts <<<"$t"
  for part in ${parts[@]+"${parts[@]}"}; do
    case "$part" in
      all) resolved+=(claude codex opencode openhands);;
      agents) resolved+=(codex);;
      claude|codex|opencode|openhands) resolved+=("$part");;
      '') ;;
      *) die "unknown tool: $part (see --help)";;
    esac
  done
done
tools=()
for t in ${resolved[@]+"${resolved[@]}"}; do printf '%s\n' "${tools[@]:-}" | grep -qx "$t" || tools+=("$t"); done
[ ${#tools[@]} -gt 0 ] || die "no tool selected (see --help)"
if [ $user -eq 0 ]; then
  [ -n "$repo" ] || usage 1
  [ -d "$repo" ] || die "target is not a directory: $repo"
  repo="$(cd "$repo" && pwd)"
else
  [ -z "$repo" ] || die "--user takes no repo argument"
fi

case "$skills" in
  core) skills="$CORE";;
  all)  skills="$(ls "$SRC" | paste -sd, -)";;
esac
IFS=, read -r -a want <<<"$skills"
for s in ${want[@]+"${want[@]}"}; do [ -f "$SRC/$s/SKILL.md" ] || die "unknown skill '$s' (available: $(ls "$SRC" | paste -sd' ' -))"; done
# architecture-review references its two siblings by relative path
if printf '%s\n' ${want[@]+"${want[@]}"} | grep -qx architecture-review; then
  for dep in oh-code-review oh-technical-writing; do
    printf '%s\n' ${want[@]+"${want[@]}"} | grep -qx "$dep" || { echo "note: architecture-review needs $dep; adding it"; want+=("$dep"); }
  done
fi

dest_for() {  # tool -> destination skills dir
  case "$1" in
    claude)    [ $user -eq 1 ] && echo "$HOME/.claude/skills"          || echo "$repo/.claude/skills";;
    codex)     [ $user -eq 1 ] && echo "$HOME/.agents/skills"          || echo "$repo/.agents/skills";;
    opencode)  [ $user -eq 1 ] && echo "$OC_DIR/skills" || echo "$repo/.opencode/skills";;
    openhands) [ $user -eq 1 ] && echo "$HOME/.openhands/skills"       || echo "$repo/.openhands/skills";;
    *) die "unknown tool: $1";;
  esac
}
run() { if [ $dry -eq 1 ]; then echo "  [dry-run] $*"; else "$@"; fi; }

for tool in "${tools[@]}"; do
  dest="$(dest_for "$tool")"
  echo "==> $tool: $dest"
  run mkdir -p "$dest"
  for s in ${want[@]+"${want[@]}"}; do
    verb=installed; [ -d "$dest/$s" ] && verb=replaced
    run rm -rf "$dest/$s"
    run cp -R "$SRC/$s" "$dest/$s"
    echo "    $verb $s"
  done
  if [ $prompts -eq 1 ]; then
    case "$tool" in opencode) pdest="$(dirname "$dest")/commands";; *) pdest="$(dirname "$dest")/prompts";; esac
    run mkdir -p "$pdest"; run cp "$HERE"/prompts/*.md "$pdest"/
    echo "    prompts -> $pdest"; pdests="$pdests $pdest"
  fi
done

# ---- Codex extras (skills reach Codex via --tool codex; prompts and global
# ---- AGENTS.md live in ~/.codex, touched only when ~/.codex already exists) ----
if [ $user -eq 1 ] && [ $prompts -eq 1 ] && [ -d "$HOME/.codex" ]; then
  echo "==> codex: $HOME/.codex/prompts"
  run mkdir -p "$HOME/.codex/prompts"
  run cp "$HERE"/prompts/*.md "$HOME/.codex/prompts"/
  echo "    prompts -> $HOME/.codex/prompts (Codex: /prompts:<name>)"; pdests="$pdests $HOME/.codex/prompts"
fi

# ---- OpenCode extras (skills reach OpenCode through .claude/skills and .agents/skills;
# ---- its commands and global AGENTS.md live in ~/.config/opencode, touched only when it exists) ----
if [ $user -eq 1 ] && [ $prompts -eq 1 ] && [ -d "$OC_DIR" ] && ! printf '%s\n' "${tools[@]}" | grep -qx opencode; then
  echo "==> opencode: $OC_DIR/commands"
  run mkdir -p "$OC_DIR/commands"
  run cp "$HERE"/prompts/*.md "$OC_DIR/commands"/
  echo "    prompts -> $OC_DIR/commands (OpenCode: /<name>)"; pdests="$pdests $OC_DIR/commands"
fi

# ---- managed "Code review gates" block in agent instruction files ----
BLOCK_START="<!-- ai-skills:review-gates start (managed by install.sh, do not edit inside) -->"
BLOCK_END="<!-- ai-skills:review-gates end -->"
write_block() {  # file
  local f="$1"
  if [ $dry -eq 1 ]; then echo "  [dry-run] write review-gates block into $f"; return; fi
  mkdir -p "$(dirname "$f")"; [ -f "$f" ] || : > "$f"
  if grep -qF "$BLOCK_START" "$f"; then
    awk -v s="$BLOCK_START" -v e="$BLOCK_END" '$0==s{skip=1} !skip{print} $0==e{skip=0}' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  fi
  # trim trailing blank lines so re-runs do not grow the file (awk, not sed -i: BSD sed on macOS differs)
  awk '{l[NR]=$0} $0!=""{n=NR} END{for(i=1;i<=n;i++)print l[i]}' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  [ -s "$f" ] && printf '\n' >> "$f"
  { printf '%s\n' "$BLOCK_START"; cat "$HERE/agents/review-gates.md"; printf '%s\n' "$BLOCK_END"; } >> "$f"
}
ensure_import() {  # <repo>/<file> gets "@AGENTS.md" so Claude Code (CLAUDE.md) or Gemini CLI (GEMINI.md) reads AGENTS.md
  local f="$1/$2"
  if [ $dry -eq 1 ]; then echo "  [dry-run] ensure @AGENTS.md import in $f"; return; fi
  if [ -f "$f" ]; then
    grep -qE '^@AGENTS\.md\s*$' "$f" || { [ -s "$f" ] && printf '\n' >> "$f"; printf '@AGENTS.md\n' >> "$f"; echo "    added @AGENTS.md import to $f"; }
  else
    printf '@AGENTS.md\n' > "$f"; echo "    created $f with @AGENTS.md import"
  fi
}
if [ $agentsmd -eq 1 ]; then
  echo "==> agent instructions: review-gates block"
  if [ $user -eq 1 ]; then
    write_block "$HOME/.claude/CLAUDE.md"; echo "    block -> $HOME/.claude/CLAUDE.md"
    if [ -d "$OC_DIR" ]; then write_block "$OC_DIR/AGENTS.md"; echo "    block -> $OC_DIR/AGENTS.md"; fi
    if [ -d "$HOME/.codex" ]; then write_block "$HOME/.codex/AGENTS.md"; echo "    block -> $HOME/.codex/AGENTS.md"; fi
    if [ -d "$HOME/.gemini" ]; then write_block "$HOME/.gemini/GEMINI.md"; echo "    block -> $HOME/.gemini/GEMINI.md"; fi
  else
    write_block "$repo/AGENTS.md"; echo "    block -> $repo/AGENTS.md"
    ensure_import "$repo" CLAUDE.md
    ensure_import "$repo" GEMINI.md
  fi
fi

# The source may be a temporary download (boot.sh): point at the copies when there are any.
if [ -n "$pdests" ]; then prompt_note="Prompts installed to:$pdests"; else prompt_note="Prompts to start from: $HERE/prompts/ (re-run with -p to copy them next to the skills)"; fi
cat <<NOTE

Installed from: $HERE, version $(cat "$HERE/VERSION" 2>/dev/null || echo "unknown") (see CHANGELOG.md)
Upstream: $(cat "$HERE/UPSTREAM.txt" 2>/dev/null || echo "unknown")
Next:
  - Claude Code: skills appear as /<name>; the oh- prefix keeps them clear of built-in skills such as /code-review.
  - OpenCode: reads .claude/skills, .agents/skills and .opencode/skills; ask for one by name (v2 also documents /<name>).
  - OpenHands: turn on load_project_skills (or load_user_skills for --user) in Settings -> Agent.
  - Codex: reads .agents/skills in the repo (and its parent folders) and ~/.agents/skills (--tool codex, in the default set);
    restart Codex, then /skills lists them and \$<name> mentions one. Gemini CLI reads the same directory.
$prompt_note
NOTE
