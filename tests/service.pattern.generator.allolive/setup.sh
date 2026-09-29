#!/bin/bash
# Fetch what these tests need beyond apt-packages.txt: dovi_tool (MIT), pinned and checked.
set -euo pipefail
VERSION=2.3.4
SHA256=1844258e13c26607b32224bf1fa82b595d3b35949f5467405fda560daad32b3f
dir="$(dirname "$0")/dtbin"
[ -x "$dir/dovi_tool" ] && exit 0
mkdir -p "$dir"
tgz="$dir/dovi_tool.tar.gz"
curl -fsSL -o "$tgz" \
  "https://github.com/quietvoid/dovi_tool/releases/download/$VERSION/dovi_tool-$VERSION-x86_64-unknown-linux-musl.tar.gz"
echo "$SHA256  $tgz" | sha256sum -c --quiet
tar -xzf "$tgz" -C "$dir" ./dovi_tool
rm "$tgz"
