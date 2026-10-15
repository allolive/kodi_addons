"""What the service and the screen share."""

import json

import xbmc
import xbmcaddon
import xbmcgui

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
DELAY_SETTING = "videoscreen.fs3ddelay"   # Kodi's 3D glasses delay
STATE_PROPERTY = ADDON_ID + ".state"     # the board link's state(), as JSON, on the Home window
KIND_PROPERTY = ADDON_ID + ".kind"       # the emitter connected: "pi", "board" or "" (settings.xml)
HOME = 10000


def tell_service(method):
    """test_<mHz>, test_off, delay_<rate>_<us>, lens (the service's onNotification)."""
    xbmc.executebuiltin("NotifyAll(%s,%s)" % (ADDON_ID, method))


def rpc(method, **params):
    """A Kodi JSON-RPC call's result, or None."""
    reply = json.loads(xbmc.executeJSONRPC(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})))
    return reply.get("result")


def read_state():
    try:
        state = json.loads(xbmcgui.Window(HOME).getProperty(STATE_PROPERTY) or "{}")
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}
