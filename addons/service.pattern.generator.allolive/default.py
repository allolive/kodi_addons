"""Programs -> Pattern Generator: status, start/stop, manual test patterns, settings."""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "lib"))

import xbmc  # noqa: E402
import xbmcaddon  # noqa: E402
import xbmcgui  # noqa: E402

# not patterngen.kodi: it loads the whole service, and this screen only needs its names
from patterngen import hdmistate  # noqa: E402
from patterngen.ids import ADDON_ID, LIVE_PATH, STATE_PROP, STATUS_PROP, cache_dir, version  # noqa: E402
from patterngen.patterns import SIGNAL_MODES, Colour  # noqa: E402

CACHE_DIR = cache_dir()

# (test_signal value, name, mode), in the settings' option order
MODES = [(int(m.setting), m.name, key) for key, m in SIGNAL_MODES.items()]


def patterns():
    items: list[tuple[str, tuple]] = []      # (name, ("patch", colour, window) or ("specialty", name))
    for pct in (100, 90, 80, 70, 60, 50, 40, 30, 20, 10, 5, 0):
        items.append(("Grey %d%% (10%% window)" % pct, ("patch", Colour.gray(pct / 100.0), 10)))
    items.append(("White full field", ("patch", Colour.gray(1.0), 100)))
    for name, rgb in (("Red", (1, 0, 0)), ("Green", (0, 1, 0)), ("Blue", (0, 0, 1)),
                      ("Cyan", (0, 1, 1)), ("Magenta", (1, 0, 1)), ("Yellow", (1, 1, 0))):
        items.append(("%s 75%% (10%% window)" % name,
                      ("patch", Colour(*[0.75 * c for c in rgb]), 10)))
    items += [("Brightness (PLUGE)", ("specialty", "BRIGHTNESS")),
              ("Contrast (white clipping)", ("specialty", "CONTRAST")),
              ("Alignment / overscan", ("specialty", "ALIGNMENT"))]
    return items


LUMINANCES = [400, 600, 1000, 1500, 2000, 4000, 10000]
HDR = ("hdr10", "hlg")   # the modes a max luminance applies to, as PGenerator's max_luma (DV: its fixed range)
HELP = "Left/Right: previous/next pattern    OK: pattern, signal, luminance    Back: exit"


def _setting_index(addon, key, values, default):
    value = addon.getSettingInt(key)
    return values.index(value) if value in values else values.index(default)


class Remote(xbmcgui.WindowDialog):
    """Transparent layer over the playing pattern that turns remote keys into steps."""
    KEYS = {xbmcgui.ACTION_MOVE_LEFT: ("pattern", -1), xbmcgui.ACTION_MOVE_RIGHT: ("pattern", 1),
            xbmcgui.ACTION_SELECT_ITEM: ("menu", 0), xbmcgui.ACTION_CONTEXT_MENU: ("menu", 0),
            xbmcgui.ACTION_PREVIOUS_MENU: ("exit", 0), xbmcgui.ACTION_NAV_BACK: ("exit", 0),
            xbmcgui.ACTION_STOP: ("exit", 0)}

    def __init__(self, caption):
        self.result = None
        # the name stays; the key help goes after a while
        self.addControl(xbmcgui.ControlLabel(20, 40, 1240, 30, caption, textColor="0xFFC0C0C0"))
        self.help = xbmcgui.ControlLabel(20, 670, 1240, 30, HELP, textColor="0xFFC0C0C0")
        self.addControl(self.help)
        self.timer = threading.Timer(4.0, lambda: self.help.setVisible(False))
        self.timer.start()

    def onAction(self, action):
        self.result = self.KEYS.get(action.getId())
        if self.result:
            self.timer.cancel()
            self.close()


class Browser:
    def __init__(self, addon):
        self.addon = addon
        from patterngen.kodi import make_generator     # the generator, only for patterns
        self.gen = make_generator()
        self.items = patterns()
        self.pattern = 0
        self.signal = _setting_index(addon, "test_signal", [m[0] for m in MODES], 0)
        self.luma = _setting_index(addon, "test_max_luma", LUMINANCES, 4000)
        self.lock = threading.Lock()

    def apply(self):
        self.gen.set_mode(MODES[self.signal][2])
        s = self.gen.sig
        s.max_luma = s.max_cll = LUMINANCES[self.luma]

    def save(self):
        """Remember the signal and luminance for next time, once, when the screen closes."""
        for key, value in (("test_signal", MODES[self.signal][0]),
                           ("test_max_luma", LUMINANCES[self.luma])):
            if self.addon.getSettingInt(key) != value:
                self.addon.setSettingInt(key, value)

    def describe(self):
        _, name, mode = MODES[self.signal]
        return "%s  |  %s%s" % (self.items[self.pattern][0], name,
                                "  |  %d nits" % LUMINANCES[self.luma] if mode in HDR else "")

    def draw(self, index, play=True):
        name, kind = self.items[index]
        if play:
            self.gen.pattern_request = "MANUAL " + name
        if kind[0] == "patch":
            return self.gen.patch(kind[1], kind[2], play=play)
        return self.gen.specialty(kind[1], play=play)

    def prepare_neighbours(self):
        """Encode the previous and next pattern now, so stepping to them only switches clips."""
        n = len(self.items)
        for index in ((self.pattern + 1) % n, (self.pattern - 1) % n):
            self.draw(index, play=False)

    def menu(self, dialog):
        """OK while a pattern plays: pick pattern, signal or luminance. False = leave."""
        while True:
            entries = ["Pattern: %s" % self.items[self.pattern][0],
                       "Signal: %s" % MODES[self.signal][1]]
            if MODES[self.signal][2] in HDR:
                entries.append("Max luminance: %d nits" % LUMINANCES[self.luma])
            entries.append("Exit test patterns")
            choice = dialog.select("Test pattern", entries)
            if choice < 0:
                return True
            if choice == len(entries) - 1:
                return False
            title, labels, attr = (("Pattern", [i[0] for i in self.items], "pattern"),
                                   ("Signal", [m[1] for m in MODES], "signal"),
                                   ("Max luminance", ["%d nits" % v for v in LUMINANCES],
                                    "luma"))[choice]
            pick = dialog.select(title, labels, preselect=getattr(self, attr))
            if pick >= 0:
                setattr(self, attr, pick)
                return True

    def run(self):
        try:
            self._run()
        finally:
            self.save()

    def _run(self):
        dialog = xbmcgui.Dialog()
        pick = dialog.select("Test pattern", [i[0] for i in self.items])
        if pick < 0:
            return
        self.pattern = pick
        while True:
            self.apply()
            if not self.draw(self.pattern):
                dialog.notification("Pattern generator", "Pattern failed, see the Kodi log",
                                    xbmcgui.NOTIFICATION_ERROR)
                return
            threading.Thread(target=self.prepare_neighbours, daemon=True).start()
            remote = Remote(self.describe())
            remote.doModal()
            remote.timer.cancel()
            action = remote.result
            del remote
            if action is None:          # closed by Kodi, not by a key
                if not xbmc.getCondVisibility("Player.HasVideo"):
                    return
                continue
            what, step = action
            if what == "pattern":
                self.pattern = (self.pattern + step) % len(self.items)
            elif what == "menu":
                if not self.menu(dialog):
                    what = "exit"
            if what == "exit":
                self.gen.backend.stop()
                return


def show_pattern():
    Browser(xbmcaddon.Addon(ADDON_ID)).run()


HOME = xbmcgui.Window(10000)
MAIN_OWNER = ADDON_ID + ".main"     # the main screen instance that may run, newest wins


def _state():
    return HOME.getProperty(STATE_PROP) or "stopped", HOME.getProperty(STATUS_PROP) or "Stopped"


def _send(cmd):
    """start/stop the service, and wait until it reports the new state."""
    want = "running" if cmd == "start" else "stopped"
    xbmc.executebuiltin("NotifyAll(%s,%s)" % (ADDON_ID, cmd))
    monitor = xbmc.Monitor()
    for _ in range(50):         # the service answers within a few 100 ms
        if _state()[0] == want or monitor.waitForAbort(0.1):
            return


def network_pattern_playing():
    """Whether the player shows one of the generator's clips (the window is closed
    before a manual pattern plays, so while it is up that is a network pattern).
    Info labels, not xbmc.Player(): Kodi keeps every Player object a script creates, and
    this runs every second for as long as Calman sends patterns."""
    playing = xbmc.getInfoLabel("Player.FilenameAndPath")
    return (xbmc.getCondVisibility("Player.HasVideo")
            and (playing.startswith(CACHE_DIR) or LIVE_PATH in playing))


def superseded(token):
    """A newer main screen was opened: this instance leaves."""
    return HOME.getProperty(MAIN_OWNER) != token


def panels(addon=None):
    """The window's texts by control id; [COLOR] tags do the styling."""
    addon = addon or xbmcaddon.Addon(ADDON_ID)
    state, status = _state()
    running = state == "running"
    parts = [p for p in status.split(" | ") if not p.startswith("HDMI ")]
    lines = hdmistate.format_details().splitlines()
    summary = lines.pop(0).partition(": ")[2] if lines else "not available"
    rows = ["[COLOR grey]%s[/COLOR]  %s" % tuple(line.split(": ", 1)) if ": " in line else line
            for line in lines]
    half = (len(rows) + 1) // 2
    return {
        28: "Calman, HCFR, LightSpace, DeviceControl and Resolve  -  version %s"
            % version(),
        30: "[COLOR FF3FD37A]\u25cf[/COLOR]  Running" if running
            else "[COLOR grey]\u25cf[/COLOR]  Stopped",
        31: "\n".join(parts) if running else
            "Start it for the calibration software to connect. It picks each pattern "
            "and the signal.",
        34: summary,
        40: "\n".join(rows[:half]),
        41: "\n".join(rows[half:]),
    }


class Main(xbmcgui.WindowXMLDialog):
    """The add-on's screen: status on the left, the actions as buttons on the right."""
    START_STOP, PATTERN, SETTINGS, CLOSE = 10, 11, 12, 13
    LABELS = (28, 30, 34)

    BACK = (xbmcgui.ACTION_PARENT_DIR, xbmcgui.ACTION_PREVIOUS_MENU,      # Kodi would close us
            xbmcgui.ACTION_NAV_BACK, xbmcgui.ACTION_STOP)
    WINDOW = "Window.IsActive(script-patterngen-main.xml)"

    def __init__(self, *args, **kwargs):
        self.next = None
        self.open = False
        self.addon = self.token = None       # set by main() after construction
        self.texts = {}                      # what each control shows, sent only on change

    def onInit(self):
        self.open = True
        self.texts = {}                      # the controls start from the window's XML
        self.refresh()
        threading.Thread(target=self.follow, daemon=True).start()

    def onAction(self, action):
        if action.getId() in self.BACK:
            self.close()            # ours, so the refresh thread sees it

    def refresh(self):
        running = _state()[0] == "running"
        texts = panels(self.addon)
        texts[self.START_STOP] = "Stop generator" if running else "Start generator"
        for control, text in texts.items():
            if self.texts.get(control) == text:
                continue
            self.texts[control] = text
            if control in self.LABELS or control == self.START_STOP:
                self.getControl(control).setLabel(text)
            else:
                self.getControl(control).setText(text)

    def follow(self):
        """Clients connect and the HDMI mode changes while the window is up; a network
        pattern sends it behind the video until playback stops. Ends with the window,
        however it was closed, and when a newer main screen takes over."""
        monitor, gone = xbmc.Monitor(), 0
        while self.open and not monitor.waitForAbort(1):
            gone = 0 if xbmc.getCondVisibility(self.WINDOW) else gone + 1
            if superseded(self.token) or gone >= 3:
                self.next = "quit"
                self.close()
            elif network_pattern_playing():
                self.next = "network"
                self.close()
            elif self.open:
                self.refresh()

    def onClick(self, control):
        if control == self.START_STOP:
            action = "stop" if _state()[0] == "running" else "start"
            _send(action)
            self.refresh()
            return
        self.next = {self.PATTERN: "pattern", self.SETTINGS: "settings"}.get(control)
        self.close()

    def close(self):
        self.open = False
        super().close()


def main(action):
    """Programs -> Pattern Generator. An argument runs that action alone (for keymaps)."""
    addon = xbmcaddon.Addon(ADDON_ID)
    if action in ("start", "stop"):
        _send(action)
        return
    if action == "pattern":
        show_pattern()
        return
    token = "%d.%f" % (os.getpid(), time.time())
    HOME.setProperty(MAIN_OWNER, token)     # an older main screen still waiting leaves
    monitor = xbmc.Monitor()
    while not superseded(token):
        window = Main("script-patterngen-main.xml", addon.getAddonInfo("path"), "Default", "1080i")
        window.addon, window.token = addon, token
        window.doModal()
        window.open = False             # however it closed, its refresh thread ends
        chosen = window.next
        if chosen is None and network_pattern_playing():
            chosen = "network"          # Kodi closed it for the pattern's full-screen video
        del window
        if chosen == "network":
            # back once nothing played for 1.5 s: a clip switch leaves the player
            # stopped for a moment between two patterns
            idle = 0
            while idle < 3:
                idle = 0 if network_pattern_playing() else idle + 1
                if monitor.waitForAbort(0.5) or superseded(token):
                    return
        elif chosen == "pattern":
            show_pattern()
        elif chosen == "settings":
            addon.openSettings()
        else:
            return


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
