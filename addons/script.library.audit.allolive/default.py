"""Library Audit: find duplicate movies and orphan video files."""
import json
import os
import re
from urllib.parse import unquote

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDON_NAME = ADDON.getAddonInfo("name")

# Backed by the addon setting "dry_run" (default true). When true, all delete
# paths short-circuit and just log what would have happened.
def is_dry_run():
    try:
        return ADDON.getSettingBool("dry_run")
    except Exception:
        return True


def set_dry_run(value):
    try:
        ADDON.setSettingBool("dry_run", bool(value))
    except Exception:
        pass


SELECTED_KEY = "selected_sources"


def load_saved_selection():
    raw = ADDON.getSetting(SELECTED_KEY) or ""
    return [p for p in raw.split("|") if p]


def save_selection(chosen):
    ADDON.setSetting(SELECTED_KEY, "|".join(s["file"] for s in chosen))

VIDEO_EXTS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".vob", ".iso", ".img", ".flv", ".webm", ".divx",
    ".ogm", ".ogv", ".3gp", ".rm", ".rmvb", ".asf", ".f4v", ".mts",
}
SKIP_DIRS = {"VIDEO_TS", "BDMV", "EXTRAS", "FEATURETTES", "TRAILERS", "SAMPLE", "SAMPLES"}
SAMPLE_RE = re.compile(r"(?:^|[\W_])sample(?:[\W_]|$)", re.IGNORECASE)
TITLE_RE = re.compile(r"[^a-z0-9]+")


# ---- JSON-RPC helper ----

def rpc(method, params=None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    raw = xbmc.executeJSONRPC(json.dumps(payload))
    try:
        return json.loads(raw)
    except ValueError:
        return {}


# ---- Formatting ----

def human_size(n):
    if n is None or n < 0:
        return "?"
    if n < 1024:
        return f"{n} B"
    for u in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {u}"
    return f"{n:.1f} PB"


def file_size(path):
    if not path:
        return None
    try:
        if path.startswith("stack://"):
            total = 0
            for part in stack_parts(path):
                try:
                    total += int(xbmcvfs.Stat(part).st_size())
                except Exception:
                    pass
            return total or None
        return int(xbmcvfs.Stat(path).st_size())
    except Exception:
        return None


def stack_parts(path):
    return path[len("stack://"):].split(" , ")


_size_cache = {}


def file_size_cached(path):
    if not path:
        return None
    if path not in _size_cache:
        _size_cache[path] = file_size(path)
    return _size_cache[path]


def copy_score(m):
    """Tuple used to pick the keeper: prefer higher resolution, then size, then playcount, then newer."""
    sd = m.get("streamdetails") or {}
    videos = sd.get("video") or []
    v = videos[0] if videos else {}
    height = v.get("height") or 0
    width = v.get("width") or 0
    size = file_size_cached(m.get("file") or "") or 0
    playcount = m.get("playcount") or 0
    dateadded = m.get("dateadded") or ""
    return (height, width, size, playcount, dateadded)


def recommended_keeper_index(group):
    return max(range(len(group)), key=lambda i: copy_score(group[i]))


def stream_summary(movie):
    sd = movie.get("streamdetails") or {}
    videos = sd.get("video") or []
    if not videos:
        return ""
    v = videos[0]
    return f"{v.get('width') or 0}x{v.get('height') or 0} {v.get('codec') or ''}".strip()


# ---- Movies / duplicates ----

def get_movies():
    res = rpc("VideoLibrary.GetMovies", {"properties": [
        "title", "year", "imdbnumber", "uniqueid", "file",
        "streamdetails", "playcount", "dateadded",
    ]})
    return res.get("result", {}).get("movies", []) or []


def movie_key(m):
    uid = m.get("uniqueid") or {}
    for src in ("imdb", "tmdb", "tvdb"):
        v = uid.get(src)
        if v:
            return f"{src}:{v}"
    imdb = (m.get("imdbnumber") or "").strip()
    if imdb:
        return f"imdb:{imdb}"
    t = TITLE_RE.sub("", (m.get("title") or "").lower())
    y = m.get("year") or 0
    return f"ty:{t}:{y}" if t else None


def find_duplicates(movies):
    groups = {}
    for m in movies:
        k = movie_key(m)
        if not k:
            continue
        groups.setdefault(k, []).append(m)
    dupes = [g for g in groups.values() if len(g) > 1]
    dupes.sort(key=lambda g: (g[0].get("title") or "").lower())
    return dupes


def audio_summary(m):
    audios = (m.get("streamdetails") or {}).get("audio") or []
    if not audios:
        return ""
    a = audios[0]
    parts = [a.get("codec") or "", f"{a.get('channels') or '?'}ch"]
    if a.get("language"):
        parts.append(a["language"])
    return " ".join(p for p in parts if p).strip()


def short_date(s):
    return (s or "").split(" ", 1)[0] or "?"


def delete_file_path(path):
    # Backstop at the point of no return: callers route deletes through the
    # dry-run gate (_run_or_record / delete_orphan), but guard here too so a
    # file can never be removed in simulate mode regardless of the caller.
    if is_dry_run():
        xbmc.log(f"[{ADDON_NAME}] dry-run backstop blocked file delete: {path}", xbmc.LOGWARNING)
        return True
    if path.startswith("stack://"):
        ok = True
        for part in stack_parts(path):
            if xbmcvfs.exists(part) and not xbmcvfs.delete(part):
                ok = False
        return ok
    if not xbmcvfs.exists(path):
        return True
    return xbmcvfs.delete(path)


def _li(label, label2=""):
    return xbmcgui.ListItem(label=label, label2=label2 or "")


class WideSelect(xbmcgui.WindowXMLDialog):
    LIST_ID = 3000
    HEADING_ID = 2000

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.selected = -1
        self.heading = ""
        self.items = []

    def onInit(self):
        try:
            self.getControl(self.HEADING_ID).setLabel(self.heading)
        except Exception:
            pass
        try:
            lst = self.getControl(self.LIST_ID)
            lst.reset()
            lst.addItems(self.items)
            self.setFocusId(self.LIST_ID)
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] WideSelect onInit failed: {e}", xbmc.LOGERROR)
            self.close()

    def onClick(self, control_id):
        if control_id == self.LIST_ID:
            try:
                self.selected = self.getControl(self.LIST_ID).getSelectedPosition()
            except Exception:
                self.selected = -1
            self.close()

    def onAction(self, action):
        if action.getId() in (10, 92):
            self.selected = -1
            self.close()
        else:
            super().onAction(action)


def wide_select(heading, items):
    """Drop-in for Dialog().select(heading, items, useDetails=True), wider window."""
    try:
        dlg = WideSelect(
            "audit-select.xml", ADDON.getAddonInfo("path"), "Default", "720p"
        )
        dlg.heading = heading
        dlg.items = items
        dlg.doModal()
        result = dlg.selected
        del dlg
        return result
    except Exception as e:
        xbmc.log(
            f"[{ADDON_NAME}] WideSelect failed, falling back to Dialog().select: {e}",
            xbmc.LOGWARNING,
        )
        return xbmcgui.Dialog().select(heading, items, useDetails=True)


class WideConfirm(xbmcgui.WindowXMLDialog):
    HEADING_ID = 2000
    TEXT_ID = 2001
    NO_BTN_ID = 3001
    YES_BTN_ID = 3002

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.confirmed = False
        self.heading = ""
        self.body = ""
        self.yes_label = "OK"
        self.no_label = "Cancel"

    def onInit(self):
        try:
            self.getControl(self.HEADING_ID).setLabel(self.heading)
            self.getControl(self.TEXT_ID).setText(self.body)
            self.getControl(self.NO_BTN_ID).setLabel(self.no_label)
            self.getControl(self.YES_BTN_ID).setLabel(self.yes_label)
            self.setFocusId(self.YES_BTN_ID)
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] WideConfirm onInit failed: {e}", xbmc.LOGERROR)
            self.close()

    def onClick(self, control_id):
        if control_id == self.YES_BTN_ID:
            self.confirmed = True
            self.close()
        elif control_id == self.NO_BTN_ID:
            self.confirmed = False
            self.close()

    def onAction(self, action):
        if action.getId() in (10, 92):
            self.confirmed = False
            self.close()
        else:
            super().onAction(action)


class WideReview(xbmcgui.WindowXMLDialog):
    """List with 3-line rows (label / line2 / label2) for the duplicate review wizard."""
    LIST_ID = 3000
    HEADING_ID = 2000

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.selected = -1
        self.heading = ""
        self.items = []
        self.preselect = 0

    def onInit(self):
        try:
            self.getControl(self.HEADING_ID).setLabel(self.heading)
            lst = self.getControl(self.LIST_ID)
            lst.reset()
            lst.addItems(self.items)
            if 0 <= self.preselect < len(self.items):
                lst.selectItem(self.preselect)
            self.setFocusId(self.LIST_ID)
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] WideReview onInit failed: {e}", xbmc.LOGERROR)
            self.close()

    def onClick(self, control_id):
        if control_id == self.LIST_ID:
            try:
                self.selected = self.getControl(self.LIST_ID).getSelectedPosition()
            except Exception:
                self.selected = -1
            self.close()

    def onAction(self, action):
        if action.getId() in (10, 92):
            self.selected = -1
            self.close()
        else:
            super().onAction(action)


def wide_review(heading, items, preselect=0):
    """Like wide_select but uses a 3-line-per-row layout for the review wizard."""
    try:
        dlg = WideReview(
            "audit-review.xml", ADDON.getAddonInfo("path"), "Default", "720p"
        )
        dlg.heading = heading
        dlg.items = items
        dlg.preselect = preselect
        dlg.doModal()
        result = dlg.selected
        del dlg
        return result
    except Exception as e:
        xbmc.log(
            f"[{ADDON_NAME}] WideReview failed, falling back to wide_select: {e}",
            xbmc.LOGWARNING,
        )
        return wide_select(heading, items)


def wide_confirm(heading, body, yes_label="OK", no_label="Cancel"):
    """Drop-in for Dialog().yesno(...), wider window with a scrollable textbox."""
    try:
        dlg = WideConfirm(
            "audit-confirm.xml", ADDON.getAddonInfo("path"), "Default", "720p"
        )
        dlg.heading = heading
        dlg.body = body
        dlg.yes_label = yes_label
        dlg.no_label = no_label
        dlg.doModal()
        result = dlg.confirmed
        del dlg
        return result
    except Exception as e:
        xbmc.log(
            f"[{ADDON_NAME}] WideConfirm failed, falling back to Dialog().yesno: {e}",
            xbmc.LOGWARNING,
        )
        return xbmcgui.Dialog().yesno(
            heading, body, nolabel=no_label, yeslabel=yes_label
        )


def folder_name(path):
    if not path:
        return "?"
    parent = path.rsplit("/", 1)[0]
    return parent.rsplit("/", 1)[-1] or parent or path


def folder_under_source(path, sources):
    """First subfolder of the matching source root (closest to the source, not the file).
    Falls back to the immediate parent folder name when no source matches."""
    if not path or path == "?":
        return "?"
    target = stack_parts(path)[0] if path.startswith("stack://") else path
    for s in sources:
        for root in source_roots(s):
            if target.startswith(root):
                rest = target[len(root):]
                first = rest.split("/", 1)[0]
                if first:
                    return first
    return folder_name(path)


def split_path_for_display(path, sources):
    """Return (source_root, between_path, filename) for a 3-line display.
    `between_path` is whatever sits between the source and the filename.
    All three pieces have no leading/trailing slash."""
    if not path or path == "?":
        return ("?", "", path or "?")
    target = stack_parts(path)[0] if path.startswith("stack://") else path
    for s in sources:
        for root in source_roots(s):
            if target.startswith(root):
                rest = target[len(root):]
                if "/" in rest:
                    folder, fname = rest.rsplit("/", 1)
                else:
                    folder, fname = "", rest
                return (root.rstrip("/"), folder, fname)
    if "/" in target:
        folder, fname = target.rsplit("/", 1)
    else:
        folder, fname = "", target
    return ("", folder, fname)


def _effective_parent(path):
    """The 'movie folder' for grouping & deletion. Walks up past BDMV/VIDEO_TS
    so a Blu-ray rip at /movies/X/BDMV/index.bdmv is grouped under /movies/X/
    and the deletion target is the movie folder, not the disc folder."""
    if not path or path.startswith("stack://"):
        return None
    parts = path.split("/")
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].upper() in ("BDMV", "VIDEO_TS"):
            return "/".join(parts[:i]) + "/" if i > 0 else None
    return "/".join(parts[:-1]) + "/"


def _is_source_root(parent, sources):
    """True if `parent` is itself one of the configured source roots. We never
    rmdir a source folder — only the movie folders below it."""
    if not parent:
        return False
    p = _norm_src(parent)
    return any(p == r for s in sources for r in source_roots(s))


def _has_nested_video(path, count_own_files=True):
    """True if `path` (when count_own_files) or any non-skip subfolder below it
    holds a video file. Errs safe (returns True) if a folder can't be listed.
    The recursion always counts a subfolder's own files, so the
    count_own_files=False entry point catches videos nested *below* `path`
    without counting files sitting directly in it."""
    if not path.endswith("/"):
        path = path + "/"
    try:
        dirs, files = xbmcvfs.listdir(path)
    except Exception:
        return True
    if count_own_files and any(is_video(f) for f in (files or [])):
        return True
    return any(
        _has_nested_video(join_path(path, d))
        for d in (dirs or [])
        if d.upper() not in SKIP_DIRS
    )


def _has_foreign_videos_in_subdirs(parent):
    """True if a non-skip subfolder of `parent` holds a video anywhere below it
    (separate content). Files directly in `parent` (the movie, its sidecars, a
    trailer) do NOT count — only nested videos do. BDMV/VIDEO_TS/EXTRAS/etc.
    (SKIP_DIRS) are this movie's own structure and are skipped."""
    return _has_nested_video(parent, count_own_files=False)


def deletion_plan(losers, library_files, sources):
    """Group the losers by parent folder and decide file vs folder removal.

    Rule: for each parent folder, remove the whole folder (sidecars, nfo, art
    and all) ONLY when it is safe to do so:
      - no other library entry remains in the folder, AND
      - the folder is not a source root, AND
      - the folder has no subfolders containing other videos.
    Otherwise remove each file individually.

    Returns a list of plan entries: (kind, target, movies, reason).
      - kind == 'file'   -> target is the file path, movies is [one movie]
      - kind == 'folder' -> target is the parent folder, movies are all the
                            losers that lived in that folder
    """
    if library_files is None:
        library_files = set()
    deleting = {m.get("file") or "" for m in losers if m.get("file")}
    by_parent = {}
    stack_movies = []
    for m in losers:
        parent = _effective_parent(m.get("file") or "")
        if parent is None:
            stack_movies.append(m)
            continue
        by_parent.setdefault(parent, []).append(m)

    plan = []
    for parent, movies in by_parent.items():
        keeps_in_folder = False
        for f in library_files:
            if not f or f in deleting:
                continue
            cand = stack_parts(f)[0] if f.startswith("stack://") else f
            if _effective_parent(cand) == parent:
                keeps_in_folder = True
                break

        if keeps_in_folder:
            block_reason = "other library files remain in folder"
        elif _is_source_root(parent, sources):
            block_reason = "folder is a source root"
        elif _has_foreign_videos_in_subdirs(parent):
            block_reason = "subfolder(s) contain other videos"
        else:
            block_reason = None

        if block_reason:
            for m in movies:
                plan.append(("file", m.get("file") or "", [m], block_reason))
        else:
            plan.append(
                ("folder", parent, movies, "no library movie remains in folder after delete")
            )
    for m in stack_movies:
        plan.append(("file", m.get("file") or "", [m], "stack path"))
    return plan


def _run_or_record(label, fn, recorded):
    """The single gate that diverges live vs simulate mode.

    Live: run fn() and return its result.
    Simulate (dry run): append the command `label` to `recorded` (for the
    popup) and skip execution, returning True."""
    if is_dry_run():
        recorded.append(label)
        xbmc.log(f"[{ADDON_NAME}] SIMULATE (not run): {label}", xbmc.LOGINFO)
        return True
    return fn()


def _rmdir_force(target):
    # Backstop: never rmdir in simulate mode, regardless of caller.
    if is_dry_run():
        xbmc.log(f"[{ADDON_NAME}] dry-run backstop blocked rmdir: {target}", xbmc.LOGWARNING)
        return True
    try:
        return xbmcvfs.rmdir(target, force=True)
    except Exception as e:
        xbmc.log(f"[{ADDON_NAME}] rmdir failed for {target}: {e}", xbmc.LOGWARNING)
        return False


def _show_simulated_commands(commands):
    """Popup listing the exact commands that would have run in live mode."""
    body = (
        "[COLOR=lime][B]== SIMULATION ==[/B][/COLOR]  these commands were NOT executed:\n\n"
        + "\n".join(commands)
    )
    try:
        xbmcgui.Dialog().textviewer(f"{ADDON_NAME} - simulated commands", body)
    except Exception:
        xbmcgui.Dialog().ok(f"{ADDON_NAME} - simulated", body)


def apply_deletion_plan(plan):
    """Execute a deletion plan: RemoveMovie for every involved movie, then
    delete each file or rmdir the folder.

    Live and simulate take the SAME path; the only difference is the gate in
    _run_or_record() - in simulate each command is recorded instead of run, and
    the collected commands are shown in a popup."""
    recorded = []
    for kind, target, movies, reason in plan:
        for m in movies:
            mid = m.get("movieid")
            _run_or_record(
                f"VideoLibrary.RemoveMovie  movieid={mid}  [{m.get('file')}]",
                lambda mid=mid: rpc("VideoLibrary.RemoveMovie", {"movieid": mid}),
                recorded,
            )
        if kind == "file":
            for m in movies:
                p = m.get("file") or ""
                _run_or_record(
                    f"delete file  {p}", lambda p=p: delete_file_path(p), recorded
                )
        else:
            _run_or_record(
                f"rmdir (recursive, force)  {target}",
                lambda t=target: _rmdir_force(t),
                recorded,
            )
    if is_dry_run():
        _show_simulated_commands(recorded)


def _mode_banner():
    if is_dry_run():
        return "[COLOR=lime][B]== DRY RUN == nothing will be deleted[/B][/COLOR]\n\n"
    return "[COLOR=red][B]== LIVE == deletes are PERMANENT[/B][/COLOR]\n\n"


def _yes_label(real_label, dry_label):
    if is_dry_run():
        return f"[COLOR=lime]{dry_label}[/COLOR]"
    return f"[COLOR=red]{real_label}[/COLOR]"


def _format_keep_entry(m, sources):
    path = m.get("file") or "?"
    src, folder, fname = split_path_for_display(path, sources)
    return (
        "+ KEEP\n"
        f"    {src}/\n"
        f"    {folder}/\n"
        f"    {fname}"
    )


def split_folder_for_display(folder_path, sources):
    """Return (source_root, between_path, folder_name) for a folder path.
    Mirrors split_path_for_display but for a directory target (the final
    component is the folder being deleted, not a file)."""
    f = folder_path.rstrip("/")
    if not f or f == "?":
        return ("?", "", folder_path or "?")
    for s in sources:
        for root in source_roots(s):
            r = root.rstrip("/")
            if f == r:
                return (r, "", "")
            if f.startswith(r + "/"):
                rest = f[len(r) + 1:]
                if "/" in rest:
                    between, name = rest.rsplit("/", 1)
                else:
                    between, name = "", rest
                return (r, between, name)
    if "/" in f:
        between, name = f.rsplit("/", 1)
    else:
        between, name = "", f
    return ("", between, name)


def _format_plan_entry(entry, sources):
    kind, target, movies, reason = entry
    movies_size = sum(file_size_cached(m.get("file") or "") or 0 for m in movies)
    if kind == "folder":
        src, between, name = split_folder_for_display(target, sources)
        lines = [f"- DELETE FOLDER  (frees {human_size(movies_size)})"]
        lines.append(f"    {src}/")
        if between:
            lines.append(f"    {between}/")
        lines.append(f"    {name}/")
        names = "; ".join(m.get("title") or "?" for m in movies)
        lines.append(f"    contains {len(movies)} library movie(s): {names}")
        lines.append(f"    reason: {reason}")
        return "\n".join(lines)
    # file
    m = movies[0]
    src, folder, fname = split_path_for_display(m.get("file") or "", sources)
    lines = ["- DELETE FILE"]
    lines.append(f"    {src}/")
    if folder:
        lines.append(f"    {folder}/")
    lines.append(f"    {fname}")
    lines.append(f"    (frees {human_size(movies_size)}) - {reason}")
    return "\n".join(lines)


def confirm_clean(keepers, plan, sources):
    total_size = sum(
        file_size_cached(m.get("file") or "") or 0
        for _, _, movies, _ in plan
        for m in movies
    )
    total_actions = len(plan)
    keep_block = "\n\n".join(_format_keep_entry(m, sources) for m in keepers)
    del_block = "\n\n".join(_format_plan_entry(e, sources) for e in plan)
    heading = "Confirm clean"
    prompt = (
        f"{_mode_banner()}"
        f"KEEP ({len(keepers)}):\n\n{keep_block}\n\n"
        f"DELETE ({total_actions} action(s), frees {human_size(total_size)}):\n\n{del_block}"
    )
    return wide_confirm(
        heading, prompt,
        no_label="Cancel",
        yes_label=_yes_label("Delete", "Pretend delete"),
    )


def _review_li(label, line2, label2, line3=""):
    item = xbmcgui.ListItem(label=label, label2=label2 or "")
    item.setProperty("line2", line2 or "")
    item.setProperty("line3", line3 or "")
    return item


def _review_items(group, state, sources):
    items = []
    for i, m in enumerate(group):
        path = m.get("file") or "?"
        stream = stream_summary(m)
        size = human_size(file_size_cached(path))
        audio = audio_summary(m)
        tags = " · ".join(b for b in (stream, size, audio) if b and b != "?")
        # source root (the "library folder", distinguishes copies by host/volume),
        # then the movie folder, then the filename.
        src, folder, fname = split_path_for_display(path, sources)
        marker = (
            "[COLOR=lime][B][ KEEP ][/B][/COLOR]"
            if state[i]
            else "[COLOR=red][B][ DELETE ][/B][/COLOR]"
        )
        items.append(_review_li(
            f"{marker}  {src}/",
            f"{folder}/" if folder else "",
            tags or "",
            line3=fname,
        ))
    return items


def keeper_is_confident(group):
    """True when the recommended keeper is clearly better than the runner-up."""
    if len(group) < 2:
        return False
    keeper_idx = recommended_keeper_index(group)
    keeper_score = copy_score(group[keeper_idx])
    runner = max(
        (copy_score(m) for i, m in enumerate(group) if i != keeper_idx),
        default=None,
    )
    if runner is None:
        return False
    # Higher resolution = clearly better.
    if keeper_score[0] > runner[0]:
        return True
    # Same resolution: require >=10% larger file to call it confident.
    if keeper_score[0] == runner[0] and keeper_score[2] > runner[2] * 1.1:
        return True
    return False


def review_group(group, idx, total, sources, library_files):
    """Toggle keep/delete per copy, then Clean. Returns 'acted', 'skip', or 'stop'."""
    title = group[0].get("title") or "?"
    year = group[0].get("year") or ""
    heading = f"Review {idx}/{total} - {title} ({year})  -  OK toggles keep/delete"
    keeper_idx = recommended_keeper_index(group)
    state = [i == keeper_idx for i in range(len(group))]
    n = len(group)
    confident = keeper_is_confident(group)
    first_open = True
    while True:
        delete_count = sum(1 for s in state if not s)
        keep_count = n - delete_count
        items = _review_items(group, state, sources)
        items.append(_review_li(
            "[COLOR=lime][B]>> Clean[/B][/COLOR]",
            f"Apply: keep {keep_count}, delete {delete_count}",
            "OK confirms and moves to the next group",
        ))
        items.append(_review_li(
            "Keep all copies (skip)",
            "No changes; move to the next duplicate group",
            "",
        ))
        items.append(_review_li(
            "Stop reviewing",
            "Return to the duplicates menu",
            "",
        ))
        # When the auto-pick is confident, open with focus on Clean so the user
        # can just press OK.
        preselect = n if (first_open and confident) else 0
        first_open = False
        choice = wide_review(heading, items, preselect=preselect)
        if choice == -1 or choice == n + 2:
            return "stop"
        if choice == n + 1:
            return "skip"
        if choice == n:
            if keep_count == 0:
                xbmcgui.Dialog().ok(
                    ADDON_NAME,
                    "At least one copy must be marked KEEP. Toggle one back to keep.",
                )
                continue
            if delete_count == 0:
                return "skip"
            keepers = [m for i, m in enumerate(group) if state[i]]
            losers = [m for i, m in enumerate(group) if not state[i]]
            plan = deletion_plan(losers, library_files, sources)
            if not confirm_clean(keepers, plan, sources):
                continue
            apply_deletion_plan(plan)
            return "acted"
        if 0 <= choice < n:
            state[choice] = not state[choice]
            if n == 2:
                state[1 - choice] = not state[1 - choice]


def review_duplicates(dupes, sources, library_files):
    i = 0
    acted = skipped = 0
    while i < len(dupes):
        result = review_group(dupes[i], i + 1, len(dupes), sources, library_files)
        if result == "stop":
            break
        if result == "acted":
            acted += 1
        else:
            skipped += 1
        i += 1
    xbmcgui.Dialog().notification(
        ADDON_NAME,
        f"Reviewed: {acted} resolved, {skipped} skipped",
        xbmcgui.NOTIFICATION_INFO,
    )


def duplicates_menu(sources):
    progress = xbmcgui.DialogProgress()
    progress.create(ADDON_NAME, "Loading movie library...")
    try:
        movies = get_movies()
        scoped = [m for m in movies if path_under_sources(m.get("file") or "", sources)]
        progress.update(
            50, f"{len(scoped)}/{len(movies)} movies in selected sources. Grouping..."
        )
        dupes = find_duplicates(scoped)
    finally:
        progress.close()

    if not dupes:
        xbmcgui.Dialog().ok(
            ADDON_NAME, "No duplicate movies found in the selected sources."
        )
        return

    library_files = get_library_files()
    prefetch_sizes(dupes)
    review_duplicates(dupes, sources, library_files)


def prefetch_sizes(dupes):
    """Pre-populate the file_size cache so per-group rendering is snappy.

    Without this, opening each new duplicate group triggers fresh xbmcvfs.Stat
    calls over SMB/NFS for every copy in that group, which can stall the UI for
    several seconds per group on slow shares."""
    paths = []
    seen = set()
    for g in dupes:
        for m in g:
            p = m.get("file") or ""
            if p and p not in seen:
                seen.add(p)
                paths.append(p)
    if not paths:
        return
    progress = xbmcgui.DialogProgress()
    progress.create(ADDON_NAME, "Pre-fetching file sizes...")
    try:
        total = len(paths)
        for i, p in enumerate(paths):
            if progress.iscanceled():
                break
            progress.update(int((i / total) * 100), p)
            file_size_cached(p)
    finally:
        progress.close()


# ---- Orphan files on disk ----

def get_video_sources():
    res = rpc("Files.GetSources", {"media": "video"})
    out = []
    for s in res.get("result", {}).get("sources", []) or []:
        path = s.get("file")
        if path:
            out.append({"label": s.get("label") or path, "file": path})
    return out


def get_movie_file_paths():
    res = rpc("VideoLibrary.GetMovies", {"properties": ["file"]})
    paths = []
    for m in res.get("result", {}).get("movies", []) or []:
        p = m.get("file") or ""
        if not p:
            continue
        if p.startswith("stack://"):
            paths.extend(stack_parts(p))
        else:
            paths.append(p)
    return paths


def expand_source(path):
    """Expand a multipath:// source into its component paths."""
    if not path or not path.startswith("multipath://"):
        return [path] if path else []
    body = path[len("multipath://"):]
    parts = [unquote(p) for p in body.split("/") if p]
    return parts or [path]


def source_roots(s):
    return [_norm_src(p) for p in expand_source(s["file"])]


def filter_movie_sources(sources):
    """Keep only sources that contain at least one library movie."""
    movie_paths = get_movie_file_paths()
    if not movie_paths:
        return sources
    kept, dropped = [], []
    for s in sources:
        roots = source_roots(s)
        if any(p.startswith(r) for r in roots for p in movie_paths):
            kept.append(s)
        else:
            dropped.append(s)
    if dropped:
        xbmc.log(
            f"[{ADDON_NAME}] filter_movie_sources dropped {len(dropped)} source(s): "
            + " | ".join(f"{d['label']}={d['file']}" for d in dropped),
            xbmc.LOGINFO,
        )
    return kept


def select_sources(sources, preselect_files=None):
    """Multi-select prompt. Returns chosen list, or None on cancel."""
    if len(sources) <= 1:
        return sources
    labels = [f"{s['label']}  [{s['file']}]" for s in sources]
    if preselect_files is None:
        preselect = list(range(len(sources)))
    else:
        wanted = set(preselect_files)
        preselect = [i for i, s in enumerate(sources) if s["file"] in wanted]
        if not preselect:
            preselect = list(range(len(sources)))
    selected = xbmcgui.Dialog().multiselect(
        "Choose sources to use", labels, preselect=preselect
    )
    if selected is None:
        return None
    return [sources[i] for i in selected]


def _norm_src(path):
    return path if path.endswith("/") or path.endswith("\\") else path + "/"


def path_under_sources(path, sources):
    if not path or not sources:
        return False
    roots = [r for s in sources for r in source_roots(s)]
    candidates = stack_parts(path) if path.startswith("stack://") else [path]
    for c in candidates:
        for r in roots:
            if c.startswith(r):
                return True
    return False


def get_library_files():
    """All file paths referenced by the video library, with stack parts expanded."""
    files = set()
    for method, key in (
        ("VideoLibrary.GetMovies", "movies"),
        ("VideoLibrary.GetEpisodes", "episodes"),
        ("VideoLibrary.GetMusicVideos", "musicvideos"),
    ):
        res = rpc(method, {"properties": ["file"]})
        for item in res.get("result", {}).get(key, []) or []:
            p = item.get("file")
            if not p:
                continue
            if p.startswith("stack://"):
                for part in stack_parts(p):
                    files.add(part)
            else:
                files.add(p)
    return files


def is_video(name):
    return os.path.splitext(name)[1].lower() in VIDEO_EXTS


def join_path(base, name):
    if base.endswith("/") or base.endswith("\\"):
        return base + name
    return base + "/" + name


def walk_source(path, found, progress, visited):
    """Recursively collect video files under a Kodi path via xbmcvfs."""
    if not path.endswith("/"):
        path = path + "/"
    if path in visited:
        return
    visited.add(path)
    if progress.iscanceled():
        return
    progress.update(50, f"Scanning:\n{path}")
    try:
        dirs, files = xbmcvfs.listdir(path)
    except Exception:
        return
    for f in files or []:
        if SAMPLE_RE.search(f) or not is_video(f):
            continue
        found.add(join_path(path, f))
    for d in dirs or []:
        if d.upper() in SKIP_DIRS:
            continue
        if progress.iscanceled():
            return
        walk_source(join_path(path, d), found, progress, visited)


def _log_orphan_reasons(orphans, found, library_files):
    """Debug: explain why each flagged file counts as missing. The orphan test
    is exact-path set subtraction (files found on disk MINUS library file
    paths), so a file is flagged either because it is genuinely unscanned, or
    because the library stored the SAME file under a different path string
    (different host / volume / multipath leg — e.g. source nfs://192.168.1.16/...
    vs library nfs://192.168.1.18/...). Index the library by basename to tell
    the two apart and log a reason per file."""
    xbmc.log(
        f"[{ADDON_NAME}] orphan scan: walked {len(found)} video file(s) on disk, "
        f"library references {len(library_files)} path(s), "
        f"{len(orphans)} flagged as not-in-library",
        xbmc.LOGINFO,
    )
    if not orphans:
        return
    by_base = {}
    for f in library_files:
        by_base.setdefault(f.rsplit("/", 1)[-1], []).append(f)
    mismatch = genuine = 0
    for o in orphans:
        same = by_base.get(o.rsplit("/", 1)[-1])
        if same:
            mismatch += 1
            extra = f" (+{len(same) - 1} more)" if len(same) > 1 else ""
            xbmc.log(
                f"[{ADDON_NAME}] missing? PATH-MISMATCH: {o} -- not in library by "
                f"exact path, but the same filename IS in library at: {same[0]}{extra}",
                xbmc.LOGWARNING,
            )
        else:
            genuine += 1
            xbmc.log(
                f"[{ADDON_NAME}] missing: ABSENT: {o} -- filename not in library at "
                f"all (unscanned, or the scraper could not match it)",
                xbmc.LOGINFO,
            )
    xbmc.log(
        f"[{ADDON_NAME}] orphan reasons: {mismatch} path-mismatch (same file "
        f"already in library under a different path), {genuine} genuinely absent",
        xbmc.LOGINFO,
    )


def find_orphan_files(sources):
    library_files = get_library_files()
    found = set()
    visited = set()
    progress = xbmcgui.DialogProgress()
    progress.create(ADDON_NAME, "Scanning video sources...")
    try:
        for s in sources:
            if progress.iscanceled():
                break
            for sub in expand_source(s["file"]):
                if progress.iscanceled():
                    break
                walk_source(sub, found, progress, visited)
    finally:
        progress.close()
    orphans = sorted(found - library_files)
    _log_orphan_reasons(orphans, found, library_files)
    return orphans


def delete_orphan(path):
    heading = "Delete orphan file?"
    prompt = f"{_mode_banner()}Path:\n{path}"
    ok = wide_confirm(
        heading, prompt, no_label="Cancel",
        yes_label=_yes_label("Delete", "Pretend delete"),
    )
    if not ok:
        return False
    if is_dry_run():
        _show_simulated_commands([f"delete file  {path}"])
        return True
    if delete_file_path(path):
        xbmcgui.Dialog().notification(ADDON_NAME, "Deleted", xbmcgui.NOTIFICATION_INFO)
        return True
    xbmcgui.Dialog().notification(
        ADDON_NAME, "File deletion failed", xbmcgui.NOTIFICATION_WARNING
    )
    return False


def _trigger_info_scan(filename):
    """After navigating to the file's folder, wait for the Videos listing to
    populate (NFS can be slow), focus the target file, then fire Action(Info) —
    Kodi's native scrape, which shows the "Locally stored information found.
    Ignore and refresh from Internet?" prompt when an NFO is present."""
    monitor = xbmc.Monitor()
    loaded = False
    for _ in range(60):  # up to ~15s for the folder to open
        if (xbmc.getCondVisibility("Window.IsActive(videos)")
                and int(xbmc.getInfoLabel("Container.NumItems") or "0") > 0):
            loaded = True
            break
        if monitor.waitForAbort(0.25):
            return
    count = int(xbmc.getInfoLabel("Container.NumItems") or "0")
    focused = False
    for _ in range(count + 1):  # step the list until the target file is focused
        if xbmc.getInfoLabel("ListItem.FileName") == filename:
            focused = True
            break
        xbmc.executebuiltin("Action(Down)")
        monitor.waitForAbort(0.15)
    xbmc.log(
        f"[{ADDON_NAME}] scan: window-loaded={loaded}, {count} item(s) in folder, "
        f"focused '{filename}'={focused}; firing Action(Info)",
        xbmc.LOGINFO,
    )
    xbmc.executebuiltin("Action(Info)")
    monitor.waitForAbort(0.5)  # let the action register before the script exits


def open_files_view(path, scan_hint=False):
    """Open the file's folder in the Videos window. 'Open containing folder'
    (scan_hint=False) just navigates there. 'Scan this file' (scan_hint=True)
    also focuses the file and fires Action(Info), running Kodi's native scrape
    (with the 'Ignore and refresh from Internet?' prompt). Either way we close
    our modal first so the Videos window is actually in front."""
    if not path or path.startswith("stack://"):
        xbmcgui.Dialog().ok(ADDON_NAME, "Not supported for this path type.")
        return False
    _close_backdrop()  # reveal the Videos window we're about to open
    parent = path.rsplit("/", 1)[0] + "/"
    filename = path.rsplit("/", 1)[-1] or path
    xbmc.log(
        f"[{ADDON_NAME}] open_files_view: navigate {parent} (scan={scan_hint})",
        xbmc.LOGINFO,
    )
    xbmc.executebuiltin(f'ActivateWindow(videos,"{parent}",return)')
    if scan_hint:
        xbmcgui.Dialog().notification(
            ADDON_NAME, f"Scanning '{filename}'...", xbmcgui.NOTIFICATION_INFO, 4000
        )
        _trigger_info_scan(filename)
    raise SystemExit


def orphan_action(path):
    size = human_size(file_size_cached(path))
    name = path.rsplit("/", 1)[-1] or path
    heading = f"{name}  -  {size}" if size != "?" else name
    delete_label = "Pretend delete (DRY RUN)" if is_dry_run() else "Delete file"

    while True:
        items = [
            _li("Play this file", path),
            _li("Open containing folder",
                "Drops you on the file's folder in the Videos window"),
            _li("Scan this file",
                "Opens the folder; press Info on the file to scrape it into the library"),
            _li(delete_label, path),
            _li("Back"),
        ]
        choice = wide_select(heading, items)
        if choice in (-1, 4):
            return False
        if choice == 0:
            xbmc.Player().play(path)
            return False
        if choice == 1:
            open_files_view(path, scan_hint=False)
            return False
        if choice == 2:
            open_files_view(path, scan_hint=True)
            return False
        if choice == 3 and delete_orphan(path):
            return True


def rescan_parents(orphans):
    parents = sorted({p.rsplit("/", 1)[0] + "/" for p in orphans})
    if not parents:
        return
    ok = xbmcgui.Dialog().yesno(
        ADDON_NAME,
        f"Trigger a library scan on {len(parents)} parent folder(s)?",
        nolabel="Cancel", yeslabel="Scan",
    )
    if not ok:
        return
    for parent in parents:
        rpc("VideoLibrary.Scan", {"directory": parent})
    xbmcgui.Dialog().notification(
        ADDON_NAME, f"Scans started ({len(parents)} folders)", xbmcgui.NOTIFICATION_INFO
    )


def orphans_menu(sources):
    orphans = find_orphan_files(sources)
    if orphans is None:
        return
    if not orphans:
        xbmcgui.Dialog().ok(ADDON_NAME, "No orphan video files found in the selected sources.")
        return
    while True:
        parents_count = len({p.rsplit("/", 1)[0] for p in orphans})
        items = [
            _li("Rescan all parent folders (library scan)",
                f"VideoLibrary.Scan on {parents_count} folder(s); only picks up well-named files"),
        ]
        for p in orphans:
            size = human_size(file_size_cached(p))
            items.append(_li(folder_name(p), f"{size}  ·  {p}"))
        items.append(_li("Back"))
        choice = wide_select(f"{len(orphans)} file(s) not in library", items)
        if choice in (-1, len(items) - 1):
            return
        if choice == 0:
            rescan_parents(orphans)
            return
        idx = choice - 1
        if orphan_action(orphans[idx]):
            del orphans[idx]
            if not orphans:
                xbmcgui.Dialog().ok(ADDON_NAME, "No more orphan files.")
                return


# ---- Main ----

class Backdrop(xbmcgui.WindowXMLDialog):
    """Passive full-screen backdrop kept up for the whole run so there is always
    a 'Library Audit / working' indication between the modal dialogs — otherwise
    library queries, deletes and reloads leave a blank screen for a few seconds.
    The interactive dialogs layer on top of it."""


_backdrop = None


def _show_backdrop():
    global _backdrop
    if _backdrop is not None:
        return
    try:
        _backdrop = Backdrop(
            "audit-background.xml", ADDON.getAddonInfo("path"), "Default", "720p"
        )
        _backdrop.show()
    except Exception as e:
        xbmc.log(f"[{ADDON_NAME}] backdrop show failed: {e}", xbmc.LOGWARNING)
        _backdrop = None


def _close_backdrop():
    global _backdrop
    if _backdrop is None:
        return
    try:
        _backdrop.close()
    except Exception:
        pass
    _backdrop = None


def main():
    _show_backdrop()
    try:
        _main()
    finally:
        _close_backdrop()


def _main():
    all_sources = get_video_sources()
    if not all_sources:
        xbmcgui.Dialog().ok(ADDON_NAME, "No video sources are configured.")
        return
    movie_sources = filter_movie_sources(all_sources)
    if not movie_sources:
        xbmcgui.Dialog().ok(
            ADDON_NAME,
            "None of your video sources contain library movies. "
            "Add a source with movie content or scan some movies in first.",
        )
        return

    saved = set(load_saved_selection())
    chosen = (
        [s for s in movie_sources if s["file"] in saved]
        if saved
        else list(movie_sources)
    )
    if not chosen:
        chosen = list(movie_sources)

    while True:
        dry = is_dry_run()
        base_heading = f"{ADDON_NAME} [DRY RUN]" if dry else ADDON_NAME
        heading = f"{base_heading} - {len(chosen)}/{len(movie_sources)} source(s) selected"
        dry_row_label = (
            "[COLOR=lime]Dry run: ON[/COLOR]  (deletes are simulated)"
            if dry
            else "[COLOR=orange]Dry run: OFF[/COLOR]  (deletes are real)"
        )
        if chosen:
            sources_row_label = (
                f"[B]Sources:[/B] {len(chosen)} of {len(movie_sources)} selected"
            )
            sources_row_sub = " · ".join(s["label"] for s in chosen)
        else:
            sources_row_label = (
                "[COLOR=orange]Sources: none selected[/COLOR]  (scans will be empty)"
            )
            sources_row_sub = "Click to pick sources"
        items = [
            _li("Find duplicate movies",
                "Movies in the selected sources, grouped by uniqueid/title+year"),
            _li("Find video files not in library",
                "Walk the selected sources and flag files not in the library"),
            _li(sources_row_label, sources_row_sub),
            _li(dry_row_label, "Click to toggle - affects all delete actions"),
            _li("Exit"),
        ]
        choice = wide_select(heading, items)
        if choice in (-1, 4):
            return
        if choice == 0:
            if not chosen:
                xbmcgui.Dialog().ok(ADDON_NAME, "Pick at least one source first.")
                continue
            duplicates_menu(chosen)
        elif choice == 1:
            if not chosen:
                xbmcgui.Dialog().ok(ADDON_NAME, "Pick at least one source first.")
                continue
            orphans_menu(chosen)
        elif choice == 2:
            new_chosen = select_sources(
                movie_sources, preselect_files=[s["file"] for s in chosen]
            )
            if new_chosen is not None:
                chosen = new_chosen
                save_selection(chosen)
        elif choice == 3:
            new_state = not dry
            if not new_state:
                ok = xbmcgui.Dialog().yesno(
                    "Disable dry run?",
                    "From now on Delete actions will permanently remove files from disk and library entries. Continue?",
                    nolabel="Cancel", yeslabel="Disable dry run",
                )
                if not ok:
                    continue
            set_dry_run(new_state)


if __name__ == "__main__":
    main()
