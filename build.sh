#!/bin/bash
# Build the Kodi addon repository under ./repository/ from ./addons/.
# Each addon dir's addon.xml is parsed for id+version; the dir is zipped as
# <id>-<version>.zip, and all addon.xml files are concatenated into addons.xml.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
ADDONS_DIR="$ROOT/addons"
REPO_DIR="$ROOT/repository"

rm -rf "$REPO_DIR"
mkdir -p "$REPO_DIR"

ADDONS_XML="$REPO_DIR/addons.xml"
echo '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' > "$ADDONS_XML"
echo '<addons>' >> "$ADDONS_XML"

for addon_path in "$ADDONS_DIR"/*/; do
  addon_id="$(basename "$addon_path")"
  addon_xml="$addon_path/addon.xml"
  [ -f "$addon_xml" ] || { echo "skip $addon_id (no addon.xml)"; continue; }

  version="$(python3 -c "import xml.etree.ElementTree as ET; print(ET.parse('$addon_xml').getroot().get('version'))")"
  echo "==> $addon_id $version"

  mkdir -p "$REPO_DIR/$addon_id"
  (cd "$ADDONS_DIR" && zip -rq "$REPO_DIR/$addon_id/${addon_id}-${version}.zip" "$addon_id" \
    -x "${addon_id}/.git/*" "*.DS_Store")

  # Strip XML declaration from per-addon addon.xml before appending.
  grep -v '<?xml' "$addon_xml" >> "$ADDONS_XML"
done

echo '</addons>' >> "$ADDONS_XML"

# md5 sum file (Kodi checks this to know when addons.xml changed)
md5 -q "$ADDONS_XML" > "$ADDONS_XML.md5" 2>/dev/null || md5sum "$ADDONS_XML" | awk '{print $1}' > "$ADDONS_XML.md5"

echo
echo "Built repository at $REPO_DIR"
ls -la "$REPO_DIR"
