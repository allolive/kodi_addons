# Allolive Kodi Addons

Personal Kodi addon repository: our own addons, and patched versions of upstream ones.

## Structure

```
addons/<id>/                      Our own addons, zipped as they are
  repository.allolive/              the repo addon itself (its <dir> entries come from releases.conf)
  script.library.audit.allolive/    finds duplicate movies and orphan video files
forks/<id>/                       Upstream addons we patch - no upstream code is stored here
  skin.estuary.allolive/                            Estuary as CoreELEC's kodi ships it
  metadata.themoviedb.org.python.allolive/          TMDB movies: adult titles, original-language art
  metadata.tvshows.themoviedb.org.python.allolive/  TMDB TV shows: original-language art
    fork.conf                       upstream repo + path (. = the repository is the addon),
                                    our name/provider, whether to pack textures
    <release>.pin                   upstream commit (PIN), our REVISION, and what to follow
    patches/*.patch                 our changes, one patch per feature (git format-patch)
    patches-<release>/              only if a release needs a different series
releases.conf                     CoreELEC releases we publish for (ce22; ce23 once it exists)
scripts/                          lib.sh (shared helpers), fork.sh, check-versions.sh,
                                  follow-upstream.sh, check-skin.py
```

A fork follows what its pin says: `UPSTREAM_TAGS=<pattern>` follows the newest matching
tag (the movie scraper's releases), `UPSTREAM_BRANCH=<branch>` the tip of a branch (the TV
scraper's `piers`, its Kodi 22 line), and neither the kodi commit its CoreELEC release builds
(CoreELEC/xbmc at the `PKG_VERSION` of `projects/Amlogic-ce/packages/mediacenter/kodi/package.mk`),
so the skin always matches the kodi on the box. The tree is taken with `git archive`, so the
upstream's `export-ignore` rules apply, and without dot-files. Its version is upstream's plus `_<REVISION>`, e.g.
`3.2.2_03`; our id, name and provider replace upstream's at build time, and a skin's media
is packed into `Textures.xbt` with the TexturePacker from the same kodi commit, the way
kodi's own build installs Estuary.

## What CI does

- **Every push to `main`** (`.github/workflows/release.yml`): builds every addon for every
  release, and publishes
  - **GitHub Pages** at [allolive.github.io/kodi_addons](https://allolive.github.io/kodi_addons/),
    one directory per release (`ce22/`, ...) - what Kodi reads;
  - **a GitHub Release per addon version**, tagged `<release>/<id>-<version>`, zip attached -
    the history, and where to grab an older version to roll back.

  It refuses a push that changes an addon (or a fork's pin or patches) without a version
  bump, and checks that every XML file parses and every `.py` compiles.
- **Daily** (`.github/workflows/follow-upstream.yml`): finds each fork's upstream commit - its
  newest tag, its branch tip, or the kodi commit its CoreELEC release builds (`PKG_URL` /
  `PKG_VERSION` of its kodi `package.mk`). When that commit changes the fork's upstream
  directory and our patches still apply, CI moves the pin,
  bumps REVISION, commits to `main` and releases - boxes get upstream fixes on their own.
  When the patches no longer apply it opens an issue saying how to rebase them. A fork whose
  `UPSTREAM_REPO` is not the repository CoreELEC builds kodi from is left alone.

Nothing built is committed. The repo addon picks the release directory by the kodi it runs on.

## Build locally

```bash
./build.sh            # -> dist/ (and the assembled addons in build/); all gitignored
```

Packing a skin needs `cmake liblzo2-dev libpng-dev libgif-dev libjpeg-dev` (TexturePacker is
built from source once per kodi commit, in `.cache/`). Use the result to `scp` a zip onto a
box for testing before pushing.

After changing a skin, `scripts/check-skin.py work/<id> <unpatched-upstream-dir>`
lists variables, includes, strings, fonts and textures it references but no longer defines
(a manual check; CI does not run it).

## Tests and checks

Every push and pull request runs, before anything is built:

- **Static checks** (`scripts/check-code.sh`): ruff (lint), mypy (types, each addon on its
  own) and shellcheck on the scripts. Settings are in `pyproject.toml`.
- **Tests**: `tests/<addon-id>/` holds an addon's pytest suite; CI runs each folder as its own
  job, on the Python CoreELEC runs (3.14). A folder's `apt-packages.txt` lists the system
  packages its tests need.

Nothing is released unless both pass. Locally:

```bash
scripts/check-code.sh                 # needs ruff, mypy, shellcheck
pytest                                # every addon's tests
pytest tests/script.library.audit.allolive
```

## Changing a forked addon

```bash
scripts/fork.sh edit skin.estuary.allolive      # work/skin.estuary.allolive: upstream + our patches as commits
# edit, test, git commit in work/skin.estuary.allolive (one commit per feature; amend or
# git rebase -i to change an existing feature)
scripts/fork.sh export skin.estuary.allolive    # rewrites forks/skin.estuary.allolive/patches/
# bump REVISION in forks/skin.estuary.allolive/ce22.pin, commit, push
```

When CI reports that the patches no longer apply:

```bash
scripts/fork.sh edit <id> <release>
scripts/fork.sh rebase <id> <new-kodi-commit> <release>   # resolve, git rebase --continue
scripts/fork.sh export <id>
# bump REVISION, commit, push
```

## Releasing one of our own addons

1. Bump `version="..."` in `addons/<id>/addon.xml`.
2. `git commit -m "<id> <version>: <change>"` and `git push`.
3. CI tags the release and updates Pages (a few minutes). On each device:
   **Settings → Add-ons → Check for updates** (or wait for the periodic check).

## Adding an addon

- Our own: create `addons/<id>/` with a valid `addon.xml` and push.
- A fork: create `forks/<id>/fork.conf` and `forks/<id>/<release>.pin` (see an existing one),
  then `scripts/fork.sh edit <id>`, commit the changes there, `scripts/fork.sh export <id>`.

## Adding a CoreELEC release

Add a line to `releases.conf` and bump the version of `addons/repository.allolive` (the
build writes its `<dir>` entries from `releases.conf`; CI insists on the bump), then add a
`<release>.pin` to each fork that should be built for it. Our own addons are published to
every release.

Our CoreELEC builds ship a copy of the repo addon (allolive/CoreELEC, branch `yacer`,
`patches-yacer/99-yacer-feed/kodi/`). Boxes update it from Pages on their own, but refresh
that copy too so a fresh install already knows the new release.

## First-time install on a Kodi box

Our CoreELEC builds already have the repository installed, enabled and trusted (listed in
kodi's `ADDON_REPOS` like Kodi's and CoreELEC's own). Anywhere else:

1. Download `repository.allolive-<version>.zip` from [allolive.github.io/kodi_addons](https://allolive.github.io/kodi_addons/).
2. In Kodi: **Settings → Add-ons → Install from zip file** → pick the zip.
3. After install, Kodi fetches all subsequent addon updates automatically from the repository.

Kodi checks the repository at startup and every 24 hours (or on **Check for updates**): it
compares `<release>/addons.xml.md5`, and when that changed, reads `addons.xml` and installs
any higher version of an addon it has.
