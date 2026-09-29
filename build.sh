#!/bin/bash
# Build the Kodi repository for every release in releases.conf into an output dir (default ./dist/).
#
#   dist/<release>/addons.xml, addons.xml.md5, <id>/<id>-<version>.zip
#   dist/index.html, dist/repository.allolive-<version>.zip     (first-time install)
#
# addons/<id>/ is our own addon, zipped as it is. forks/<id>/ is an upstream addon: its pinned
# upstream tree with our patches applied, our identity and packed textures (scripts/lib.sh).
# The assembled addon directories are kept in build/<release>/ for scripts/check-versions.sh.
# CI runs this and publishes the result; the output is never committed.
set -euo pipefail
. "$(dirname "$0")/scripts/lib.sh"

OUT_DIR="$(realpath -m "${1:-$ROOT/dist}")"
BUILD_DIR="$ROOT/build"
rm -rf "$OUT_DIR" "$BUILD_DIR"
mkdir -p "$OUT_DIR"

while read -r release _; do
  tree="$BUILD_DIR/$release"
  mkdir -p "$tree"

  for addon_path in "$ROOT"/addons/*/; do
    [ -f "$addon_path/addon.xml" ] || { echo "skip $(basename "$addon_path") (no addon.xml)"; continue; }
    rsync -a --exclude=.git --exclude=__pycache__ --exclude='*.pyc' --exclude=.DS_Store "$addon_path" "$tree/$(basename "$addon_path")/"
  done
  for fork in $(forks "$release"); do
    [ ! -e "$tree/$fork" ] || die "$fork is both in addons/ and forks/"
    fork_source "$fork" "$release" "$tree/$fork"
    fork_finish "$fork" "$tree/$fork"
  done
  for repo_xml in $(grep -l 'xbmc.addon.repository' "$tree"/*/addon.xml); do
    repository_dirs "$repo_xml"
  done

  repo="$OUT_DIR/$release"
  mkdir -p "$repo"
  ADDONS_XML="$repo/addons.xml"
  echo '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' > "$ADDONS_XML"
  echo '<addons>' >> "$ADDONS_XML"
  for addon_path in "$tree"/*/; do
    addon_id="$(basename "$addon_path")"
    read -r xml_id version < <(addon_attrs "$addon_path/addon.xml" id version)
    [ "$xml_id" = "$addon_id" ] || die "$addon_id/addon.xml declares id=$xml_id"
    echo "==> $release $addon_id $version"
    mkdir -p "$repo/$addon_id"
    (cd "$tree" && zip -rqX "$repo/$addon_id/${addon_id}-${version}.zip" "$addon_id")
    # Strip XML declaration from per-addon addon.xml before appending.
    grep -v '<?xml' "$addon_path/addon.xml" >> "$ADDONS_XML"
  done
  echo '</addons>' >> "$ADDONS_XML"
  # md5 sum file (Kodi checks this to know when addons.xml changed)
  md5sum "$ADDONS_XML" | awk '{print $1}' > "$ADDONS_XML.md5"
done < <(releases)

# The repository addon is the same for every release: offer it at the top for first-time install.
repo_zip="$(cd "$OUT_DIR" && ls -1 */repository.allolive/repository.allolive-*.zip | head -1)"
cp "$OUT_DIR/$repo_zip" "$OUT_DIR/"
repo_zip="$(basename "$repo_zip")"
cat > "$OUT_DIR/index.html" <<EOF
<!doctype html><meta charset="utf-8"><title>Allolive Kodi repository</title>
<p>Kodi add-on repository. Install <a href="$repo_zip">$repo_zip</a>
via Settings &rarr; Add-ons &rarr; Install from zip file.</p>
EOF

echo
echo "Built repository at $OUT_DIR"
find "$OUT_DIR" -name '*.zip' | sed "s|$OUT_DIR/||" | sort
