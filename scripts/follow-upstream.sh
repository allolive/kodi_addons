#!/bin/bash
# Move every fork onto the kodi commit its CoreELEC release currently builds.
#
# A fork whose upstream directory did not change keeps its pin. One that changed and still
# takes our patches gets the new PIN and REVISION+1 in forks/<id>/<release>.pin and a commit.
# One whose patches no longer apply is left alone and listed in build/follow-failed.txt as
# "<release> <id> <old-pin> <new-pin>"; the exit status is 0 either way.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
cd "$ROOT"

failed="$ROOT/build/follow-failed.txt"
mkdir -p "$(dirname "$failed")"; : > "$failed"

while read -r release _ _ branch; do
  kodi="$(coreelec_kodi "$branch")"
  read -r kodi_repo target <<< "$kodi"
  for fork in $(forks "$release"); do
    load_fork "$fork" "$release"
    [ "$PIN" = "$target" ] && continue
    [ "$UPSTREAM_REPO" = "$kodi_repo" ] || { echo "$release $fork: follows $UPSTREAM_REPO, CoreELEC builds $kodi_repo; skipped"; continue; }

    src="$(upstream_fetch "$UPSTREAM_REPO" "$PIN" "$target")"
    if git -C "$src" diff --quiet "$PIN" "$target" -- "$UPSTREAM_PATH"; then
      echo "$release $fork: $UPSTREAM_PATH unchanged ${PIN:0:10}..${target:0:10}, pin kept"
      continue
    fi

    revision="$(printf "%0${#REVISION}d" $((10#$REVISION + 1)))"
    write_pin "$fork" "$release" "$target" "$revision"
    # Whether the patches still apply is all that is decided here; the release build packs.
    if (fork_source "$fork" "$release" "$ROOT/build/follow/$fork") >&2; then
      git add "$PIN_FILE"
      git commit -q -m "$fork: follow CoreELEC $branch kodi ${target:0:10}" \
        -m "$UPSTREAM_REPO $UPSTREAM_PATH ${PIN:0:10}..${target:0:10}; our patches apply unchanged." \
        -m "https://github.com/$UPSTREAM_REPO/compare/$PIN...$target"
      echo "$release $fork: moved to ${target:0:10}, revision $REVISION -> $revision"
    else
      git checkout -- "$PIN_FILE"
      echo "$release $fork $PIN $target" >> "$failed"
      echo "$release $fork: patches do not apply to ${target:0:10}, pin kept"
    fi
  done
done < <(releases)
rm -rf "$ROOT/build/follow"
