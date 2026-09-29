"""A fake Kodi for script.library.audit.allolive: file operations on a real temporary
directory, JSON-RPC answered by the test, settings in a dict. The add-on's default.py is
loaded fresh for every test (it reads its Addon object at import)."""
import importlib.util
import json
import os
import shutil
import sys
import types
from pathlib import Path

import pytest

ADDON_DIR = Path(__file__).resolve().parents[2] / "addons" / "script.library.audit.allolive"


class FakeKodi:
    def __init__(self):
        self.settings = {"dry_run": True}
        self.rpc_calls = []
        self.movies = []            # what VideoLibrary.GetMovies answers
        self.episodes = []
        self.sources = []           # Files.GetSources

    def rpc(self, request):
        req = json.loads(request)
        method, params = req["method"], req.get("params", {})
        self.rpc_calls.append((method, params))
        if method == "VideoLibrary.GetMovies":
            return {"result": {"movies": self.movies}}
        if method == "VideoLibrary.GetEpisodes":
            return {"result": {"episodes": self.episodes}}
        if method == "VideoLibrary.GetMusicVideos":
            return {"result": {"musicvideos": []}}
        if method == "Files.GetSources":
            return {"result": {"sources": self.sources}}
        return {"result": "OK"}

    def calls(self, method):
        return [p for m, p in self.rpc_calls if m == method]


def _modules(fake):
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG, xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR = 0, 1, 2, 3
    xbmc.log = lambda msg, level=0: None
    xbmc.executeJSONRPC = lambda request: json.dumps(fake.rpc(request))
    xbmc.executebuiltin = lambda cmd, wait=False: None
    xbmc.getCondVisibility = lambda cond: False
    xbmc.sleep = lambda ms: None

    xbmcaddon = types.ModuleType("xbmcaddon")

    class Addon:
        def __init__(self, addon_id=None):
            pass

        def getAddonInfo(self, key):
            return {"name": "Library Audit (allolive)", "path": str(ADDON_DIR)}.get(key, "")

        def getSetting(self, key):
            return str(fake.settings.get(key, ""))

        def setSetting(self, key, value):
            fake.settings[key] = value

        def getSettingBool(self, key):
            return bool(fake.settings.get(key, False))

        def setSettingBool(self, key, value):
            fake.settings[key] = bool(value)

    xbmcaddon.Addon = Addon

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.NOTIFICATION_INFO, xbmcgui.NOTIFICATION_WARNING, xbmcgui.NOTIFICATION_ERROR = (
        "info", "warning", "error")

    class Dialog:
        shown = []              # textviewer / ok bodies, for the dry-run command list

        def textviewer(self, heading, text):
            Dialog.shown.append(text)

        def ok(self, heading, text):
            Dialog.shown.append(text)

        def notification(self, *args, **kwargs):
            pass

    class DialogProgress:
        def create(self, *args):
            pass

        def update(self, *args):
            pass

        def iscanceled(self):
            return False

        def close(self):
            pass

    class WindowXMLDialog:
        def __init__(self, *args, **kwargs):
            pass

    xbmcgui.Dialog, xbmcgui.DialogProgress, xbmcgui.WindowXMLDialog = (
        Dialog, DialogProgress, WindowXMLDialog)
    xbmcgui.ListItem = lambda *args, **kwargs: types.SimpleNamespace(
        setProperty=lambda *a: None, setLabel2=lambda *a: None)

    xbmcvfs = types.ModuleType("xbmcvfs")

    def listdir(path):
        entries = sorted(os.listdir(path))
        return ([e for e in entries if os.path.isdir(os.path.join(path, e))],
                [e for e in entries if os.path.isfile(os.path.join(path, e))])

    def delete(path):
        os.remove(path)
        return True

    def rmdir(path, force=False):
        shutil.rmtree(path) if force else os.rmdir(path)
        return True

    class Stat:
        def __init__(self, path):
            self._size = os.path.getsize(path)

        def st_size(self):
            return self._size

    xbmcvfs.listdir, xbmcvfs.delete, xbmcvfs.rmdir, xbmcvfs.Stat = listdir, delete, rmdir, Stat
    xbmcvfs.exists = os.path.exists
    xbmcvfs.translatePath = lambda p: p
    return {"xbmc": xbmc, "xbmcaddon": xbmcaddon, "xbmcgui": xbmcgui, "xbmcvfs": xbmcvfs}


@pytest.fixture
def kodi(monkeypatch):
    fake = FakeKodi()
    for name, module in _modules(fake).items():
        monkeypatch.setitem(sys.modules, name, module)
    return fake


@pytest.fixture
def audit(kodi):
    """default.py, freshly imported against the fake Kodi."""
    spec = importlib.util.spec_from_file_location("library_audit", ADDON_DIR / "default.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tree(tmp_path):
    """tree({"Movies/A (2001)/a.mkv": 5, ...}): files of those sizes under tmp_path; the
    root as a Kodi-style path with a trailing slash."""
    def make(files):
        for rel, size in files.items():
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\0" * size)
        return str(tmp_path) + "/"
    return make
