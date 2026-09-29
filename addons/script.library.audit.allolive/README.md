# Library Audit (allolive)

Kodi script (Programs → Add-ons) that cleans a video library: **duplicate movies** and **video files the library does not know**. Everything goes through Kodi (JSON-RPC, `xbmcvfs`), so it works on NFS/SMB sources as well as local ones.

**Dry run is on by default**: every delete is simulated and the exact commands that would have run are listed at the end. Turn it off from the main menu (asks for confirmation) or in the add-on settings.

## Main menu

| Entry | Does |
|---|---|
| Find duplicate movies | groups library movies from the selected sources, then one review screen per group |
| Find video files not in library | walks the selected sources and lists video files no library entry points at |
| Sources | which video sources to audit (only sources holding library movies are offered); the choice is remembered |
| Dry run | toggles simulate / live |

## Duplicates

- **Same movie** = same IMDb id, else same TMDB id, else same title (letters and digits only, case-folded) + year.
- **Keeper** (pre-selected) = highest resolution, then largest file, then most played, then most recently added. The screen opens on *Clean* when the pick is clear-cut (higher resolution, or same resolution and ≥10% larger); otherwise you decide.
- Per copy: path, size, video and audio streams. Toggle keep/delete, then *Clean*, *Keep all copies (skip)*, or *Stop reviewing*.
- **Clean** removes each deleted copy's library entry (`VideoLibrary.RemoveMovie`), then its files:
  - the **whole movie folder** (nfo, art, subtitles, extras) only when no other library entry lives in it, it is not a source root, and no subfolder holds another video;
  - otherwise **just the video file**. Stacked movies (`stack://`) are always deleted part by part.
  - A Blu-ray/DVD rip (`BDMV/`, `VIDEO_TS/`) counts as its movie folder, not the disc folder.

## Files not in library

- Compared against every movie, episode and music-video path (stack parts expanded). Multipath sources are expanded.
- Ignored: samples (`sample` as a word in the name) and anything under `Extras`, `Featurettes`, `Trailers`, `Sample(s)`, `BDMV`, `VIDEO_TS`. A folder that cannot be listed counts as holding videos, so it is never deleted.
- Per file: play it, open its folder in Videos, scan it into the library, or delete it (the file only). *Rescan all parent folders* runs a library scan on every folder with an orphan; it only picks up files the scraper can name.

## Safety

- One gate decides live vs dry run for every action; dry run and live walk the same code path.
- Never deletes a source root, never deletes a folder another library entry or another video still needs, and always confirms before a live delete.

## Development

Tests: `pytest tests/script.library.audit.allolive` (fake Kodi over a real temporary tree). CI runs them, with ruff and mypy, before every release. Icon: *movie-search* from Material Design Icons (Pictogrammers, Apache-2.0); source in `resources/icon.svg`.
