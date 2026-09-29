"""A stand-in for Kodi's xbmc, xbmcaddon, xbmcgui and xbmcvfs, enough for kodi.py and
default.py. install() puts it in sys.modules; a script changes what it needs through the
module attributes (settings, PROPS, TOASTS, xbmcgui.Dialog.select, ...). t_kodi.py has
its own, which simulates Kodi's player."""
import os
import sys
import tempfile
import time
import types

settings: dict[str, str] = {}       # the add-on's settings (strings, as Kodi stores them)
PROPS: dict[str, str] = {}          # window properties (every window shares them)
TOASTS: list[str] = []
VIEWED: list[tuple] = []            # textviewer (heading, text)
ROOT = tempfile.mkdtemp(prefix="pg_kodi_")

xbmc = types.ModuleType("xbmc")
xbmcaddon = types.ModuleType("xbmcaddon")
xbmcgui = types.ModuleType("xbmcgui")
xbmcvfs = types.ModuleType("xbmcvfs")

xbmc.LOGERROR, xbmc.LOGINFO = 4, 1
xbmc.log = lambda m, l=1: None


class Monitor:
    def __init__(self, *a):
        pass

    def waitForAbort(self, t=None):
        time.sleep(t or 0)
        return False


class Player:
    def __init__(self, *a):
        pass

    def isPlayingVideo(self):
        return False


xbmc.Monitor, xbmc.Player = Monitor, Player
xbmc.getInfoLabel = lambda l: "1280x720 @ 60.00Hz - Full Screen" if "Screen" in l else ""
xbmc.getCondVisibility = lambda c: False
xbmc.executebuiltin = lambda c, wait=False: None
xbmc.executeJSONRPC = lambda q: "{}"


class Addon:
    def __init__(self, i=None):
        pass

    def getSetting(self, k):
        return settings.get(k, "")

    def setSetting(self, k, v):
        settings[k] = v

    def getSettingInt(self, k):
        return int(settings.get(k) or 0)

    def setSettingInt(self, k, v):
        settings[k] = str(v)

    def getAddonInfo(self, k):
        return "0.3.0"


xbmcaddon.Addon = Addon


class Window:
    def __init__(self, i=None):
        pass

    def setProperty(self, k, v):
        PROPS[k] = v

    def getProperty(self, k):
        return PROPS.get(k, "")

    def clearProperty(self, k):
        PROPS.pop(k, None)


class Dialog:
    def notification(self, title, msg, *a, **k):
        TOASTS.append(msg)

    def select(self, heading, items, preselect=-1, **k):
        return -1

    def textviewer(self, heading, text):
        VIEWED.append((heading, text))


xbmcgui.Window, xbmcgui.Dialog, xbmcgui.NOTIFICATION_INFO = Window, Dialog, "info"
xbmcgui.NOTIFICATION_ERROR = "error"
# Kodi's action ids, as xbmcgui defines them
xbmcgui.ACTION_MOVE_LEFT, xbmcgui.ACTION_MOVE_RIGHT, xbmcgui.ACTION_SELECT_ITEM = 1, 2, 7
xbmcgui.ACTION_PARENT_DIR, xbmcgui.ACTION_PREVIOUS_MENU, xbmcgui.ACTION_STOP = 9, 10, 13
xbmcgui.ACTION_NAV_BACK, xbmcgui.ACTION_CONTEXT_MENU = 92, 117
xbmcgui.WindowDialog = xbmcgui.ControlLabel = xbmcgui.WindowXMLDialog = object
xbmcgui.ListItem = lambda **k: types.SimpleNamespace(
    getVideoInfoTag=lambda: types.SimpleNamespace(setTitle=lambda t: None))
xbmcvfs.translatePath = lambda p: os.path.join(ROOT, p.replace("special://", ""))


def install():
    for mod in (xbmc, xbmcaddon, xbmcgui, xbmcvfs):
        sys.modules[mod.__name__] = mod
