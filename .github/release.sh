#!/usr/bin/env bash
# The steps of .github/workflows/release.yml, in a script so that
# tests/test_release.py can run them against scratch repositories.
#
#   release.sh resolve
#       From the workflow's event (GITHUB_EVENT_NAME, GITHUB_REF,
#       GITHUB_REF_NAME, INPUT_VERSION), append TAG and CREATE to $GITHUB_ENV.
#       A manual run must come from main, and its version input must be one
#       MAJOR.MINOR.PATCH value and nothing else.
#   release.sh prepare vX.Y.Z [--create]
#       Check out the commit to release and write release_notes.md. The commit
#       is the existing tag, or with --create the tip of origin/main (the tag
#       is created by "publish"). Refuses a commit not on main, a VERSION that
#       differs from the tag, a missing CHANGELOG section and a release that
#       already exists.
#   release.sh publish vX.Y.Z [--create]
#       Create and push the tag if needed (--create), build the archives, and
#       publish the release; it is marked latest only when no higher vX.Y.Z tag
#       exists on origin at that moment.
#
# The workflow runs one release at a time (a single concurrency group), and
# publish re-reads the tags from origin, so the latest decision never rests on
# the snapshot a run took when it started.
set -euo pipefail
cmd=${1:-}; TAG=${2:-}; create=${3:-}
fail() { echo "::error::$*" >&2; exit 1; }
# [[ =~ ]] matches the whole string; grep -x would accept any one line of a
# multi-line value, and a newline reaching $GITHUB_ENV sets further variables.
semver='^[0-9]+\.[0-9]+\.[0-9]+$'

if [ "$cmd" = resolve ]; then
  if [ "${GITHUB_EVENT_NAME:-}" = workflow_dispatch ]; then
    # a manual release runs main's own workflow and script, never a branch's
    [ "${GITHUB_REF:-}" = refs/heads/main ] || fail "run the release from main, not ${GITHUB_REF:-}"
    v=${INPUT_VERSION:-}; v=${v#v}
    [[ $v =~ $semver ]] || fail "version '${INPUT_VERSION:-}' is not MAJOR.MINOR.PATCH"
    printf 'TAG=v%s\nCREATE=--create\n' "$v" >> "$GITHUB_ENV"
  else
    t=${GITHUB_REF_NAME:-}
    [[ ${t#v} =~ $semver && $t = v* ]] || fail "tag '$t' is not vMAJOR.MINOR.PATCH"
    printf 'TAG=%s\nCREATE=\n' "$t" >> "$GITHUB_ENV"
  fi
  exit 0
fi

[[ ${TAG#v} =~ $semver && $TAG = v* ]] || fail "tag '$TAG' is not vMAJOR.MINOR.PATCH"
ver=${TAG#v}
has_tag() { git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; }
sha256() { if command -v sha256sum >/dev/null; then sha256sum -- "$@"; else shasum -a 256 -- "$@"; fi; }

case "$cmd" in
prepare)
  git fetch -q --force origin '+refs/heads/main:refs/remotes/origin/main' '+refs/tags/*:refs/tags/*'
  if has_tag; then
    commit=$(git rev-parse "$TAG^{commit}")
  elif [ "$create" = --create ]; then
    commit=$(git rev-parse origin/main)
  else
    fail "tag $TAG does not exist"
  fi
  git merge-base --is-ancestor "$commit" origin/main ||
    fail "$TAG points at $(git rev-parse --short "$commit"), which is not on main; merge first, then release"
  git checkout -q --detach "$commit"
  file_ver=$(tr -d '[:space:]' < VERSION)
  [ "$file_ver" = "$ver" ] || fail "VERSION at $(git rev-parse --short HEAD) says $file_ver, the release is $TAG"
  # The release body is the version's CHANGELOG section: roll [Unreleased]
  # into the version, in the PR that sets VERSION, before releasing.
  awk -v ver="$ver" '
    index($0, "## [" ver "]") == 1 {on=1; next}
    on && /^## \[/ {exit}
    on {print}
  ' CHANGELOG.md > release_notes.md
  grep -q '[^[:space:]]' release_notes.md || fail "CHANGELOG.md has no section for $ver"
  if gh release view "$TAG" >/dev/null 2>&1; then fail "release $TAG already exists"; fi
  echo "release $TAG: commit $(git rev-parse --short HEAD) checked"
  ;;
publish)
  if ! has_tag; then
    [ "$create" = --create ] || fail "tag $TAG does not exist"
    git -c user.name='github-actions[bot]' -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
      tag -a "$TAG" HEAD -m "ai-skills $ver"
    git push -q origin "refs/tags/$TAG"
    echo "created tag $TAG at $(git rev-parse --short HEAD)"
  fi
  [ "$(git rev-parse "$TAG^{commit}")" = "$(git rev-parse HEAD)" ] || fail "tag $TAG does not point at the checked commit"
  rm -rf dist; mkdir dist
  git archive --format=tar.gz --prefix="aiskills-$ver/" -o "dist/aiskills-$ver.tar.gz" "$TAG"
  git archive --format=zip --prefix="aiskills-$ver/" -o "dist/aiskills-$ver.zip" "$TAG"
  (cd dist && sha256 * > SHA256SUMS)
  # "Latest" is what the one-liners install: only the highest version gets it,
  # judged against the tags on origin now, not when this run started.
  git fetch -q --force origin '+refs/tags/*:refs/tags/*'
  newest=$(git tag -l 'v[0-9]*' --sort=-v:refname | grep -Ex 'v[0-9]+\.[0-9]+\.[0-9]+' | head -n 1)
  latest=false; [ "$newest" = "$TAG" ] && latest=true
  echo "newest tag on origin: $newest; $TAG latest: $latest"
  gh release create "$TAG" dist/* --verify-tag --latest="$latest" \
    --title "ai-skills $ver" --notes-file release_notes.md
  ;;
*)
  fail "usage: release.sh resolve | prepare|publish vX.Y.Z [--create]"
  ;;
esac
