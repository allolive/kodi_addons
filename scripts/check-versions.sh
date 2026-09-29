#!/bin/bash
# Refuse an addon whose sources changed since its released version without a version bump.
# Run after ./build.sh: versions are read from the assembled build/<release>/<id>/addon.xml,
# because a fork's version is its upstream's plus our REVISION.
# A release is the tag <release>/<id>-<version>; an addon with no such tag is new and passes.
# Prints "<release> <id> <version>" for every addon whose version has not been released yet.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
cd "$ROOT"

fail=0
while read -r release _; do
  [ -d "build/$release" ] || die "no build/$release; run ./build.sh first"
  for addon_xml in build/"$release"/*/addon.xml; do
    addon_id="$(basename "$(dirname "$addon_xml")")"
    version="$(addon_attrs "$addon_xml" version)"
    tag="$(release_tag "$release" "$addon_id" "$version")"
    if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
      mapfile -t sources < <(addon_sources "$release" "$addon_id")
      if ! git diff --quiet "$tag" HEAD -- "${sources[@]}"; then
        echo "error: $addon_id changed since $tag but its version is still $version - bump $(bump_hint "$release" "$addon_id")" >&2
        fail=1
      fi
    else
      echo "$release $addon_id $version"
    fi
  done
done < <(releases)
exit $fail
