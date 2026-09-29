#!/bin/bash
# Work on a forked addon as git commits on top of its upstream.
#
#   fork.sh edit   <id> [release]   check out upstream at the pin into work/<id>, our patches as commits
#   fork.sh export <id>             write work/<id>'s commits back to forks/<id>/patches/
#   fork.sh rebase <id> <sha> [release]
#                                   move work/<id> onto upstream <sha> and pin it (resolve any
#                                   conflict with git rebase --continue, then export)
#
# release defaults to the first one in releases.conf. work/ is gitignored.
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(dirname "$0")/lib.sh"

cmd=${1:-}; id=${2:-}
[ -n "$cmd" ] && [ -n "$id" ] || { sed -n '2,10p' "$0"; exit 1; }
work="$ROOT/work/$id"
git_w() { git -C "$work" "$@"; }

# Commit the upstream tree at <sha> in work/<id>, on top of the current upstream commit if any.
commit_upstream() { # <sha>
  git_w rm -rq --cached --ignore-unmatch .
  find "$work" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
  upstream_export "$1" "$work"
  git_w add -A
  git_w commit -q --allow-empty -m "upstream: $UPSTREAM_REPO@$1 $UPSTREAM_PATH"
  git_w tag -f upstream >/dev/null
}

case "$cmd" in
edit)
  release=${3:-$(releases | awk 'NR == 1 { print $1 }')}
  load_fork "$id" "$release"
  [ ! -e "$work" ] || die "$work exists; export or remove it first"
  mkdir -p "$work"
  git_w init -q
  git_w config user.name "$(git -C "$ROOT" config user.name)"
  git_w config user.email "$(git -C "$ROOT" config user.email)"
  echo "$release" > "$work/.git/fork-release"
  git_w checkout -q -b "$id"
  commit_upstream "$PIN"
  patches=("$PATCH_DIR"/*.patch)
  [ -e "${patches[0]}" ] && git_w am -q --whitespace=nowarn "${patches[@]}"
  echo "work/$id: upstream $UPSTREAM_REPO@${PIN:0:10} + $(git_w rev-list --count upstream..HEAD) patches"
  ;;
export)
  [ -d "$work/.git" ] || die "no work/$id; run: fork.sh edit $id"
  release="$(cat "$work/.git/fork-release")"
  load_fork "$id" "$release"
  [ -z "$(git_w status --porcelain)" ] || die "work/$id has uncommitted changes"
  ! git_w rev-parse -q --verify REBASE_HEAD >/dev/null || die "work/$id is mid-rebase"
  mkdir -p "$PATCH_DIR"
  rm -f "$PATCH_DIR"/*.patch
  git_w format-patch -q --zero-commit --no-signature --binary -o "$PATCH_DIR" upstream..HEAD
  patches=("$PATCH_DIR"/*.patch)
  [ -e "${patches[0]}" ] || patches=()
  echo "forks/$id: ${#patches[@]} patches in ${PATCH_DIR#"$ROOT"/}"
  echo "bump REVISION in forks/$id/$release.pin before committing"
  ;;
rebase)
  sha=${3:-}; [ -n "$sha" ] || die "usage: fork.sh rebase <id> <sha> [release]"
  [ -d "$work/.git" ] || die "no work/$id; run: fork.sh edit $id"
  release=${4:-$(cat "$work/.git/fork-release")}
  load_fork "$id" "$release"
  [ -z "$(git_w status --porcelain)" ] || die "work/$id has uncommitted changes"
  old="$(git_w rev-parse upstream)"
  git_w checkout -q --detach "$old"
  commit_upstream "$sha"
  git_w checkout -q "$id"
  write_pin "$id" "$release" "$sha" "$REVISION"
  git_w rebase -q --onto upstream "$old" "$id" \
    || die "conflicts in work/$id: resolve, git rebase --continue, then fork.sh export $id"
  echo "work/$id rebased onto ${sha:0:10} ($(git_w rev-list --count upstream..HEAD) patches); now fork.sh export $id"
  ;;
*) die "unknown command $cmd" ;;
esac
