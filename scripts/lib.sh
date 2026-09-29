# Shared helpers, sourced by the other scripts.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="${CACHE:-$ROOT/.cache}"
PAGES_URL="https://allolive.github.io/kodi_addons"

die() { echo "error: $*" >&2; exit 1; }

# Print "<name> <min> <max> <coreelec-branch>" for every release.
releases() { grep -vE '^\s*(#|$)' "$ROOT/releases.conf"; }

# Fork ids; with <release>, only those pinned for it.
forks() { # [release]
  for d in "$ROOT"/forks/*/; do
    [ -f "$d/fork.conf" ] && { [ -z "${1:-}" ] || [ -f "$d/$1.pin" ]; } && basename "$d"
  done
  return 0
}

# Load forks/<id>/fork.conf and forks/<id>/<release>.pin into the caller's scope.
load_fork() { # <id> <release>
  local dir="$ROOT/forks/$1"
  [ -f "$dir/fork.conf" ] || die "no fork $1"
  [ -f "$dir/$2.pin" ] || die "fork $1 has no pin for $2 ($dir/$2.pin)"
  UPSTREAM_REPO= UPSTREAM_PATH= UPSTREAM_BRANCH= UPSTREAM_TAGS= NAME= PROVIDER= PACK_TEXTURES=0 PIN= REVISION=
  # shellcheck source=/dev/null
  . "$dir/fork.conf"; . "$dir/$2.pin"
  [ -n "$UPSTREAM_REPO" ] && [ -n "$UPSTREAM_PATH" ] && [ -n "$PIN" ] && [ -n "$REVISION" ] \
    || die "fork $1: UPSTREAM_REPO, UPSTREAM_PATH, PIN and REVISION must be set"
  PIN_FILE="$dir/$2.pin"
  PATCH_DIR="$dir/patches"
  [ -d "$dir/patches-$2" ] && PATCH_DIR="$dir/patches-$2"
  return 0
}

# Set PIN and REVISION in forks/<id>/<release>.pin, keeping its other lines.
write_pin() { # <id> <release> <pin> <revision>
  local f="$ROOT/forks/$1/$2.pin"
  sed -i -e "s/^PIN=.*/PIN=$3/" -e "s/^REVISION=.*/REVISION=$4/" "$f"
}

# The files an addon of <release> is built from, relative to ROOT: a version bump is due
# when any of them changed since the addon's last release.
addon_sources() { # <release> <id>
  if [ -d "$ROOT/forks/$2" ]; then
    load_fork "$2" "$1"
    printf '%s\n' "forks/$2/fork.conf" "forks/$2/$1.pin" "${PATCH_DIR#$ROOT/}"
  else
    echo "addons/$2"
    # a repository addon's <dir> entries are written from releases.conf
    grep -q 'xbmc.addon.repository' "$ROOT/addons/$2/addon.xml" && echo releases.conf
  fi
  return 0
}

bump_hint() { # <release> <id>
  if [ -d "$ROOT/forks/$2" ]; then echo "REVISION in forks/$2/$1.pin"; else echo "the version in addons/$2/addon.xml"; fi
}

release_tag() { echo "$1/$2-$3"; } # <release> <id> <version>

# Print the given attributes of the <addon> element, space-separated.
addon_attrs() { # <addon.xml> <attr>...
  python3 -c "import sys, xml.etree.ElementTree as ET; r = ET.parse(sys.argv[1]).getroot(); print(*(r.get(a) for a in sys.argv[2:]))" "$@"
}

# Fetch <sha>s of <repo> into the shared cache (shallow, no blobs) and print its directory.
upstream_fetch() { # <owner/repo> <sha>...
  local repo=$1; shift
  local dir="$CACHE/src/${repo//\//_}" missing=() sha
  if [ ! -d "$dir/.git" ]; then
    git init -q "$dir"
    git -C "$dir" remote add origin "https://github.com/$repo.git"
    git -C "$dir" sparse-checkout init --no-cone
  fi
  for sha; do git -C "$dir" cat-file -e "$sha^{commit}" 2>/dev/null || missing+=("$sha"); done
  [ ${#missing[@]} = 0 ] || git -C "$dir" fetch -q --depth 1 --filter=blob:none origin "${missing[@]}" >&2 \
    || die "cannot fetch $repo ${missing[*]}"
  echo "$dir"
}

# Check out <paths> of <repo> at <sha> and print the checkout directory. Paths only ever
# get added to the sparse set, so switching between them rewrites nothing.
upstream_checkout() { # <owner/repo> <sha> <path>...
  local repo=$1 sha=$2; shift 2
  local dir; dir="$(upstream_fetch "$repo" "$sha")"
  git -C "$dir" sparse-checkout add "$@" >&2
  git -C "$dir" -c advice.detachedHead=false checkout -q --force "$sha" >&2
  echo "$dir"
}

# After load_fork: extract the fork's upstream addon at <sha> into the existing directory <dest>,
# as upstream packages it - git archive, so its export-ignore rules apply - without top-level
# dot-files. UPSTREAM_PATH=. is an upstream repository that is the addon itself.
upstream_export() { # <sha> <dest>
  local pattern="/$UPSTREAM_PATH/" treeish="$1:$UPSTREAM_PATH"
  [ "$UPSTREAM_PATH" = . ] && pattern='/*' treeish="$1"
  # the sparse checkout fetches the path's blobs in one batch for git archive
  local dir; dir="$(upstream_checkout "$UPSTREAM_REPO" "$1" "$pattern")"
  git -C "$dir" archive --format=tar "$treeish" | tar -x --anchored --exclude='.*' -C "$2"
  [ -f "$2/addon.xml" ] || die "$UPSTREAM_REPO@$1 has no $UPSTREAM_PATH/addon.xml"
}

# The commit of the newest tag of <repo> matching <glob>, by version order.
latest_tag_commit() { # <owner/repo> <glob>
  # sort -V puts an annotated tag's peeled "^{}" line right after it, so the last line is a commit
  git ls-remote --tags "https://github.com/$1.git" "refs/tags/$2" | sort -k2,2V | tail -1 | cut -f1
}

# "<owner/repo> <sha>" of the kodi a CoreELEC release branch builds.
coreelec_kodi() { # <coreelec-branch>
  local mk; mk="$(curl -fsSL "https://raw.githubusercontent.com/CoreELEC/CoreELEC/$1/projects/Amlogic-ce/packages/mediacenter/kodi/package.mk")" \
    || die "cannot read CoreELEC $1 kodi package.mk"
  local repo sha
  repo="$(sed -n 's|^PKG_URL="https://github.com/\([^/]*/[^/]*\)/archive/.*|\1|p' <<< "$mk")"
  sha="$(sed -n 's/^PKG_VERSION="\([0-9a-f]\{40\}\)"$/\1/p' <<< "$mk")"
  [ -n "$repo" ] && [ -n "$sha" ] || die "no kodi PKG_URL/PKG_VERSION on CoreELEC $1"
  echo "$repo $sha"
}

# After load_fork: "<sha> <description>" of the commit the fork should be on - the newest tag
# matching UPSTREAM_TAGS, the tip of UPSTREAM_BRANCH, or the kodi commit <coreelec-branch> builds.
# Fails (and prints why) when the fork follows a kodi other than the one CoreELEC builds.
upstream_target() { # <coreelec-branch>
  local sha
  if [ -n "$UPSTREAM_TAGS" ]; then
    sha="$(latest_tag_commit "$UPSTREAM_REPO" "$UPSTREAM_TAGS")"
    [ -n "$sha" ] || die "no tag $UPSTREAM_TAGS in $UPSTREAM_REPO"
    echo "$sha $UPSTREAM_REPO newest tag $UPSTREAM_TAGS"
  elif [ -n "$UPSTREAM_BRANCH" ]; then
    sha="$(git ls-remote "https://github.com/$UPSTREAM_REPO.git" "refs/heads/$UPSTREAM_BRANCH" | cut -f1)"
    [ -n "$sha" ] || die "no branch $UPSTREAM_BRANCH in $UPSTREAM_REPO"
    echo "$sha $UPSTREAM_REPO $UPSTREAM_BRANCH"
  else
    local repo; read -r repo sha <<< "$(coreelec_kodi "$1")"
    [ -n "$sha" ] || die "no kodi commit for CoreELEC $1"
    [ "$UPSTREAM_REPO" = "$repo" ] || { echo "follows $UPSTREAM_REPO, CoreELEC builds $repo" >&2; return 1; }
    echo "$sha CoreELEC $1 kodi"
  fi
}

# Build (once per kodi commit) the TexturePacker that kodi commit ships and print its path.
texturepacker() { # <owner/repo> <sha>
  [ -n "${TEXTUREPACKER:-}" ] && { echo "$TEXTUREPACKER"; return; }
  local out="$CACHE/texturepacker/$2"
  if [ ! -x "$out/TexturePacker" ]; then
    local src; src="$(upstream_checkout "$1" "$2" /tools/depends/native/TexturePacker/src/ \
      '/xbmc/guilib/XBTF*' /xbmc/guilib/TextureFormats.h /xbmc/utils/EndianSwap.h)"
    cmake -S "$src/tools/depends/native/TexturePacker/src" -B "$out/build" \
      -DKODI_SOURCE_DIR="$src" -DARCH_DEFINES="TARGET_POSIX;TARGET_LINUX" -DCMAKE_BUILD_TYPE=Release ${CMAKE_ARGS:-} >&2 \
      && cmake --build "$out/build" -j"$(nproc)" >&2 \
      || die "TexturePacker build failed (needs cmake, liblzo2-dev, libpng-dev, libgif-dev, libjpeg-dev)"
    cp "$out/build/TexturePacker" "$out/TexturePacker"
    rm -rf "$out/build"
  fi
  echo "$out/TexturePacker"
}

# Write the upstream tree of fork <id> with our patches applied to <dest> (unpacked media).
# Leaves the fork loaded for fork_finish.
fork_source() { # <id> <release> <dest>
  load_fork "$1" "$2"
  rm -rf "$3"; mkdir -p "$3"
  upstream_export "$PIN" "$3"
  local patches=("$PATCH_DIR"/*.patch)
  [ -e "${patches[0]}" ] || return 0
  # A repository of its own, so git apply resolves paths against the addon, not an enclosing repo.
  git -C "$3" init -q
  git -C "$3" apply --whitespace=nowarn "${patches[@]}" || { rm -rf "$3/.git"; die "$1: patches do not apply to $UPSTREAM_REPO@$PIN"; }
  rm -rf "$3/.git"
}

# After fork_source: turn the patched tree into the addon Kodi installs - our identity,
# packed textures.
fork_finish() { # <id> <dir>
  local upstream_version; upstream_version="$(addon_attrs "$2/addon.xml" version)"
  python3 - "$2/addon.xml" "$1" "$NAME" "$PROVIDER" "${upstream_version}_$REVISION" <<'PY'
import re, sys
from xml.sax.saxutils import quoteattr
path, *values = sys.argv[1:]
s = open(path, encoding='utf-8').read()
m = re.search(r'<addon\b[^>]*>', s)
tag = m.group(0)
for attr, value in zip(('id', 'name', 'provider-name', 'version'), values):
    tag, n = re.subn(r'(\s%s=)("[^"]*"|\'[^\']*\')' % re.escape(attr), lambda m: m.group(1) + quoteattr(value), tag)
    if n != 1:
        sys.exit('%s: <addon> has %d %s attributes' % (path, n, attr))
open(path, 'w', encoding='utf-8').write(s[:m.start()] + tag + s[m.end():])
PY
  [ "$PACK_TEXTURES" = 1 ] || return 0
  # Same layout kodi's own build installs (cmake/scripts/common/ProjectMacros.cmake).
  local tp; tp="$(texturepacker "$UPSTREAM_REPO" "$PIN")"
  local packed; packed="$(mktemp -d)"
  "$tp" -input "$2/media" -output "$packed/Textures.xbt" -dupecheck >/dev/null
  for theme in "$2"/themes/*/; do
    [ -d "$theme" ] && "$tp" -input "$theme" -output "$packed/$(basename "$theme").xbt" -dupecheck >/dev/null
  done
  rm -rf "$2/media"; mv "$packed" "$2/media"; chmod 755 "$2/media"
}

# Give a repository addon one <dir> per release in releases.conf, chosen by kodi's xbmc.addon
# API version, pointing at that release's directory on Pages.
repository_dirs() { # <addon.xml>
  python3 - "$1" "$PAGES_URL" "$(releases)" <<'PY'
import re, sys
path, url, conf = sys.argv[1:]
releases = [l.split() for l in conf.splitlines()]
dirs = ''.join(f'''    <dir minversion="{lo}" maxversion="{hi}">
      <info compressed="false">{url}/{name}/addons.xml</info>
      <checksum>{url}/{name}/addons.xml.md5</checksum>
      <datadir zip="true">{url}/{name}/</datadir>
    </dir>
''' for name, lo, hi, _ in releases)
s = open(path, encoding='utf-8').read()
s, n = re.subn(r'(<extension point="xbmc.addon.repository"[^>]*>\n)(.*?)(\s*</extension>)', lambda m: m.group(1) + dirs.rstrip('\n') + m.group(3), s, flags=re.S)
if n != 1:
    sys.exit(f'{path}: no xbmc.addon.repository extension')
open(path, 'w', encoding='utf-8').write(s)
PY
}
