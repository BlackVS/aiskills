#!/bin/sh
# ai-skills one-line installer for Linux and macOS (no checkout needed).
#
#   curl -fsSL https://raw.githubusercontent.com/BlackVS/aiskills/main/boot.sh | bash
#   curl -fsSL .../boot.sh | bash -s -- --user -s core          # your own install.sh flags
#
# It downloads the repository archive of one ref into a temporary directory,
# runs install.sh from it with the flags you pass (default: --user -s all -p -a,
# i.e. every skill and the prompts for Claude Code and Codex user-wide, plus the
# review-gates block in the agent instructions), and removes the download.
# Re-running upgrades in place: install.sh replaces the selected skills.
#
# Optional environment:
#   AI_SKILLS_REF=<branch|tag|commit>   what to install (default: main)
#   AI_SKILLS_REPO=<owner/name>         the repository (default: BlackVS/aiskills)
#   AI_SKILLS_BASE=<url>                download from this Gitea server (for example a
#                                       mirror) instead of GitHub
#   AI_SKILLS_TOKEN=<token>             sent as an Authorization header when set (GitHub:
#                                       downloads through the API, for a private fork)
#   AI_SKILLS_ARCHIVE=<path>            install from a local .tar.gz instead of downloading
set -e
for t in curl tar; do
  command -v "$t" >/dev/null 2>&1 ||
    { echo "ERROR: '$t' is required but not found - install it and re-run." >&2; exit 1; }
done
REF=${AI_SKILLS_REF:-main}
BASE=${AI_SKILLS_BASE:-}
REPO=${AI_SKILLS_REPO:-BlackVS/aiskills}
TOKEN=${AI_SKILLS_TOKEN:-}
if [ -n "$BASE" ]; then
  URL="$BASE/api/v1/repos/$REPO/archive/$REF.tar.gz"; AUTH="token $TOKEN"
elif [ -n "$TOKEN" ]; then
  URL="https://api.github.com/repos/$REPO/tarball/$REF"; AUTH="Bearer $TOKEN"
else
  URL="https://github.com/$REPO/archive/$REF.tar.gz"
fi
DEST=$(mktemp -d)
trap 'rm -rf "$DEST"' EXIT
if [ -n "${AI_SKILLS_ARCHIVE:-}" ]; then
  echo "Unpacking ai-skills from $AI_SKILLS_ARCHIVE ..."
  # extract by a relative name inside the temp dir: GNU tar reads a C:/ path as a remote host
  cp "$AI_SKILLS_ARCHIVE" "$DEST/src.tar.gz" && (cd "$DEST" && tar -xzf src.tar.gz --strip-components=1 && rm -f src.tar.gz)
else
  echo "Fetching ai-skills $REF from ${BASE:-https://github.com}/$REPO ..."
  if [ -n "$TOKEN" ]; then
    curl -fsSL -H "Authorization: $AUTH" "$URL" | tar -xz -C "$DEST" --strip-components=1
  else
    curl -fsSL "$URL" | tar -xz -C "$DEST" --strip-components=1
  fi
fi
[ -f "$DEST/install.sh" ] || { echo "ERROR: the archive has no install.sh (wrong ref or repository?)" >&2; exit 1; }
if [ $# -eq 0 ]; then
  echo "No flags given: running install.sh --user -s all -p -a (version $(cat "$DEST/VERSION" 2>/dev/null || echo unknown))"
  set -- --user -s all -p -a
fi
bash "$DEST/install.sh" "$@"
