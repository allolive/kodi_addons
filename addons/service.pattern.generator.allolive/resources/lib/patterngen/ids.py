"""Names the service and the main screen share, without loading the service (the screen
opens faster when it does not import the protocol stack)."""

ADDON_ID = "service.pattern.generator.allolive"
STATE_PROP, STATUS_PROP = ADDON_ID + ".state", ADDON_ID + ".status"   # Home window
LIVE_PATH = "/patterngen-live/"     # the continuous stream's URL path


def cache_dir():
    import xbmcvfs
    return xbmcvfs.translatePath("special://temp/patterngen")


def version():
    import xbmcaddon
    return xbmcaddon.Addon(ADDON_ID).getAddonInfo("version")
