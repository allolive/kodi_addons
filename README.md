# Allolive Kodi Addons

Personal Kodi addon repository: our own addons, and patched versions of upstream ones.

## Structure

```
addons/<id>/                      Our own addons, zipped as they are
  repository.allolive/              the repo addon itself (its <dir> entries come from releases.conf)
  script.library.audit/
forks/<id>/                       Upstream addons we patch - no upstream code is stored here
  skin.estuary.custom/              Estuary as CoreELEC's kodi ships it
  metadata.themoviedb.org.python.adult/   the TMDB movie scraper, with adult titles
    fork.conf                       upstream repo + path, our name/provider, whether to pack textures
    <release>.pin                   upstream commit (PIN) and our REVISION, per CoreELEC release
    patches/*.patch                 our changes, one patch per feature (git format-patch)
    patches-<release>/              only if a release needs a different series
releases.conf                     CoreELEC releases we publish for (ce22; ce23 once it exists)
scripts/                          lib.sh (shared helpers), fork.sh, check-versions.sh,
                                  follow-upstream.sh, check-skin.py
```

A fork's upstream is the kodi commit its CoreELEC release builds (CoreELEC/xbmc at the
`PKG_VERSION` of `projects/Amlogic-ce/packages/mediacenter/kodi/package.mk`), so the skin
always matches the kodi on the box. Its version is upstream's plus `_<REVISION>`, e.g.
`3.2.2_03`; our id, name and provider replace upstream's at build time, and a skin's media
is packed into `Textures.xbt` with the TexturePacker from the same kodi commit, the way
kodi's own build installs Estuary.

## What CI does

- **Every push to `main`** (`.github/workflows/release.yml`): builds every addon for every
  release, and publishes
  - **GitHub Pages** at `https://allolive.github.io/kodi_addons/<release>/` - what Kodi reads;
  - **a GitHub Release per addon version**, tagged `<release>/<id>-<version>`, zip attached -
    the history, and where to grab an older version to roll back.

  It refuses a push that changes an addon (or a fork's pin or patches) without a version
  bump, and checks that every XML file parses and every `.py` compiles.
- **Daily** (`.github/workflows/follow-upstream.yml`): reads the kodi repository and commit
  each CoreELEC release builds (`PKG_URL` / `PKG_VERSION` of its kodi `package.mk`). When that
  commit changes a fork's upstream directory and our patches still apply, CI moves the pin,
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

## Changing a forked addon

```bash
scripts/fork.sh edit skin.estuary.custom      # work/skin.estuary.custom: upstream + our patches as commits
# edit, test, git commit in work/skin.estuary.custom (one commit per feature; amend or
# git rebase -i to change an existing feature)
scripts/fork.sh export skin.estuary.custom    # rewrites forks/skin.estuary.custom/patches/
# bump REVISION in forks/skin.estuary.custom/ce22.pin, commit, push
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

1. Download `repository.allolive-<version>.zip` from `https://allolive.github.io/kodi_addons/`.
2. In Kodi: **Settings → Add-ons → Install from zip file** → pick the zip.
3. After install, Kodi fetches all subsequent addon updates automatically from the repository.

Kodi checks the repository at startup and every 24 hours (or on **Check for updates**): it
compares `<release>/addons.xml.md5`, and when that changed, reads `addons.xml` and installs
any higher version of an addon it has.
