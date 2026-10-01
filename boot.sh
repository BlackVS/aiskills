#!/bin/sh
# aiskills one-line installer for Linux and macOS (no checkout needed).
#
#   curl -fsSL https://raw.githubusercontent.com/BlackVS/aiskills/main/boot.sh | bash
#   curl -fsSL .../boot.sh | bash -s -- --user -s core          # your own install.sh flags
#
# It downloads one release's archive into a temporary directory, checks it
# against the release's SHA256SUMS, runs install.sh from it with the flags you
# pass (default: --user -s all -p -a, i.e. every skill and the prompts for
# Claude Code and Codex user-wide, plus the review-gates block in the agent
# instructions), and removes the download. Re-running upgrades in place:
# install.sh replaces the selected skills. The archive's digest is recorded in
# the installed manifest (<dest>/.ai-skills.json, "archive_sha256").
#
# A release (vX.Y.Z) is installed only from its release asset
# aiskills-X.Y.Z.tar.gz whose SHA-256 matches the release's SHA256SUMS: a
# missing SHA256SUMS or a mismatch stops before anything is installed. Any
# other ref (a branch such as main, a commit) has no such check and is refused
# unless AI_SKILLS_UNVERIFIED=1 is set; it is then installed from the source
# archive and reported as unverified.
#
# Optional environment:
#   AI_SKILLS_REF=<vX.Y.Z|branch|commit> what to install (default: the latest GitHub release;
#                                       main, unverified, with AI_SKILLS_BASE or while the
#                                       repository has no release)
#   AI_SKILLS_UNVERIFIED=1              allow a ref that is not a release, without verification
#   AI_SKILLS_REPO=<owner/name>         the repository (default: BlackVS/aiskills)
#   AI_SKILLS_BASE=<url>                download from this Gitea server (for example a
#                                       mirror) instead of GitHub
#   AI_SKILLS_TOKEN=<token>             sent as an Authorization header when set (GitHub:
#                                       downloads through the API, for a private fork)
#   AI_SKILLS_ARCHIVE=<path>            install from a local .tar.gz instead of downloading
#                                       (your own file: no release check)
set -e
for t in curl tar; do
  command -v "$t" >/dev/null 2>&1 ||
    { echo "ERROR: '$t' is required but not found - install it and re-run." >&2; exit 1; }
done
REF=${AI_SKILLS_REF:-}
BASE=${AI_SKILLS_BASE:-}
REPO=${AI_SKILLS_REPO:-BlackVS/aiskills}
TOKEN=${AI_SKILLS_TOKEN:-}
fail() { echo "ERROR: $*" >&2; exit 1; }
if [ -z "$REF" ] && [ -z "${AI_SKILLS_ARCHIVE:-}" ]; then
  if [ -n "$BASE" ]; then
    REF=main
  else
    # The latest release, not the tip of main: main can carry unreleased work.
    # Only a clear "no release" answer falls back to main; any other failure
    # stops here, so a lookup error never installs unreleased work.
    lookup_failed() {
      echo "ERROR: could not look up the latest release of $REPO ($1); set AI_SKILLS_REF to skip the lookup" >&2
      exit 1
    }
    if [ -n "$TOKEN" ]; then
      # a private fork: the API with the token; 404 means no release
      BODY=$(mktemp)
      CODE=$(curl -sSL -o "$BODY" -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
        -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$REPO/releases/latest") || CODE=000
      REF=$(sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' "$BODY" | head -n 1)
      rm -f "$BODY"
      case "$CODE" in
        200) [ -n "$REF" ] || lookup_failed "no tag_name in the API answer" ;;
        404) REF= ;;
        *) lookup_failed "HTTP $CODE" ;;
      esac
    else
      # A public repository answers /releases/latest with a redirect (no API
      # rate limit): to /releases/tag/<tag>, or to /releases while it has none.
      HEAD=$(curl -fsSI "https://github.com/$REPO/releases/latest") || lookup_failed "request failed"
      LOC=$(printf '%s\n' "$HEAD" | tr -d '\r' | sed -n 's/^[Ll]ocation: *//p' | head -n 1)
      case "$LOC" in
        */releases/tag/*) REF=$(printf '%s\n' "$LOC" | sed 's|.*/releases/tag/\([^/?#]*\).*|\1|') ;;
        */releases) REF= ;;
        *) lookup_failed "unexpected answer: ${LOC:-no redirect}" ;;
      esac
    fi
    if [ -z "$REF" ]; then
      echo "No release of $REPO found: falling back to main."
      REF=main
    fi
  fi
fi

# sha256 FILE: the file's SHA-256 in lowercase hex (empty when no tool is found)
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  fi
}
# a release is exactly vMAJOR.MINOR.PATCH, as the release workflow tags it (the whole value)
RELEASE=0
[ -z "$REF" ] || [ "$(printf '%s' "$REF" | grep -Ex 'v[0-9]+\.[0-9]+\.[0-9]+' || true)" != "$REF" ] || RELEASE=1

DEST=$(mktemp -d)
trap 'rm -rf "$DEST"' EXIT
TGZ="$DEST/src.tar.gz"
if [ -n "${AI_SKILLS_ARCHIVE:-}" ]; then
  echo "Unpacking aiskills from $AI_SKILLS_ARCHIVE ..."
  cp "$AI_SKILLS_ARCHIVE" "$TGZ"
elif [ $RELEASE -eq 1 ]; then
  VER=${REF#v}; NAME="aiskills-$VER.tar.gz"
  # fetch NAME OUT: one asset of release REF
  if [ -n "$BASE" ]; then
    fetch() {
      if [ -n "$TOKEN" ]; then curl -fsSL -H "Authorization: token $TOKEN" -o "$2" "$BASE/$REPO/releases/download/$REF/$1"
      else curl -fsSL -o "$2" "$BASE/$REPO/releases/download/$REF/$1"; fi
    }
  elif [ -n "$TOKEN" ]; then
    # a private fork: assets are downloaded through the API, by the id the release lists for the name
    curl -fsSL -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" \
      -o "$DEST/release.json" "https://api.github.com/repos/$REPO/releases/tags/$REF" ||
      fail "could not read release $REF of $REPO; nothing was installed"
    fetch() {
      # each asset object lists its API url before its name
      u=$(grep -Eo '"url": *"[^"]*/releases/assets/[0-9]+"|"name": *"[^"]*"' "$DEST/release.json" | awk -v n="$1" '
        /^"url"/ {u=$0; sub(/^"url": *"/, "", u); sub(/"$/, "", u); next}
        u != "" {x=$0; sub(/^"name": *"/, "", x); sub(/"$/, "", x); if (x == n) {print u; exit} u=""}')
      [ -n "$u" ] || return 1
      curl -fsSL -H "Authorization: Bearer $TOKEN" -H "Accept: application/octet-stream" -o "$2" "$u"
    }
  else
    fetch() { curl -fsSL -o "$2" "https://github.com/$REPO/releases/download/$REF/$1"; }
  fi
  echo "Fetching aiskills $REF ($NAME and SHA256SUMS) from ${BASE:-https://github.com}/$REPO ..."
  fetch "$NAME" "$TGZ" || fail "release $REF of $REPO has no $NAME asset; nothing was installed"
  fetch SHA256SUMS "$DEST/SHA256SUMS" || fail "release $REF of $REPO has no SHA256SUMS; nothing was installed"
  # the line for NAME: "<digest>  NAME" (or " *NAME", binary mode); exactly one, and a digest
  WANT=$(awk -v n="$NAME" 'NF == 2 { f = $2; sub(/^\*/, "", f); if (f == n) print tolower($1) }' "$DEST/SHA256SUMS")
  [ "$(printf '%s\n' "$WANT" | grep -Exc '[0-9a-f]{64}')" = 1 ] && [ "$(printf '%s\n' "$WANT" | wc -l | tr -d ' ')" = 1 ] ||
    fail "SHA256SUMS of release $REF lists no single digest for $NAME; nothing was installed"
  GOT=$(sha256 "$TGZ")
  [ -n "$GOT" ] || fail "'sha256sum' or 'shasum' is required to verify the download - install one and re-run."
  [ "$GOT" = "$WANT" ] ||
    fail "$NAME does not match SHA256SUMS of release $REF (expected $WANT, got $GOT); nothing was installed"
  echo "Verified $NAME against SHA256SUMS: $GOT"
else
  [ "${AI_SKILLS_UNVERIFIED:-}" = 1 ] ||
    fail "$REF is not a release (vX.Y.Z): only a release archive can be verified against its SHA256SUMS. Set AI_SKILLS_UNVERIFIED=1 to install $REF without verification."
  if [ -n "$BASE" ]; then
    URL="$BASE/api/v1/repos/$REPO/archive/$REF.tar.gz"; AUTH="token $TOKEN"
  elif [ -n "$TOKEN" ]; then
    URL="https://api.github.com/repos/$REPO/tarball/$REF"; AUTH="Bearer $TOKEN"
  else
    URL="https://github.com/$REPO/archive/$REF.tar.gz"
  fi
  echo "WARNING: installing $REF UNVERIFIED (not a release; AI_SKILLS_UNVERIFIED=1) from ${BASE:-https://github.com}/$REPO ..."
  if [ -n "$TOKEN" ]; then
    curl -fsSL -H "Authorization: $AUTH" -o "$TGZ" "$URL"
  else
    curl -fsSL -o "$TGZ" "$URL"
  fi
fi
# the digest of what is installed, for the manifest (install.sh validates it)
AI_SKILLS_ARCHIVE_SHA256=$(sha256 "$TGZ"); export AI_SKILLS_ARCHIVE_SHA256
# extract by a relative name inside the temp dir: GNU tar reads a C:/ path as a remote host
(cd "$DEST" && tar -xzf src.tar.gz --strip-components=1 && rm -f src.tar.gz)
[ -f "$DEST/install.sh" ] || { echo "ERROR: the archive has no install.sh (wrong ref or repository?)" >&2; exit 1; }
if [ $# -eq 0 ]; then
  echo "No flags given: running install.sh --user -s all -p -a (version $(cat "$DEST/VERSION" 2>/dev/null || echo unknown))"
  set -- --user -s all -p -a
fi
bash "$DEST/install.sh" "$@"
