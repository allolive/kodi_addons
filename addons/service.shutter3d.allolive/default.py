"""Programs -> Active 3D for 120 Hz VRR displays: the emitter's status, the glasses test and the
test pattern. The window stays up; the service owns the emitter and does what
the buttons ask. The eye switch delay is Kodi's 3D glasses delay, set while
3D plays (over the test pattern, the skin's video stream dialog, or the
player's video settings)."""

import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "lib"))

import xbmc  # noqa: E402
import xbmcgui  # noqa: E402

from shutter3d import core  # noqa: E402
from shutter3d.ids import ADDON, DELAY_SETTING, read_state, rpc, tell_service  # noqa: E402
from shutter3d.status import GLASSES, summary  # noqa: E402

NAME = ADDON.getAddonInfo("name").replace(" (allolive)", "")
MEDIA = os.path.join(ADDON.getAddonInfo("path"), "resources", "media")
# film rate -> the refresh the box shows it at in 3D, in mHz; a test pattern for
# each, crosstalk-<rate>.3D.SBS.mkv
RATES = {core.rate_name(fps): core.display_mhz(fps) for fps in core.FILM_RATES}
BACK = (xbmcgui.ACTION_PARENT_DIR, xbmcgui.ACTION_PREVIOUS_MENU, xbmcgui.ACTION_NAV_BACK)


class Main(xbmcgui.WindowXMLDialog):
    """Status on the left, the buttons on the right."""
    TEST, PATTERN_BUTTON, SETTINGS, CLOSE = 11, 12, 13, 14
    LABELS = (28, 30, TEST)                  # the rest are textboxes
    WINDOW = "Window.IsActive(script-shutter3d-main.xml)"

    def __init__(self, *args, **kwargs):
        self.next = None
        self.open = False
        self.header = "3D glasses  -  version %s" % \
            ADDON.getAddonInfo("version")
        self.texts = {}                      # what each control shows, sent only on change
        self.state = {}

    def onInit(self):
        self.open = True
        self.texts = {}                      # the controls start from the window's XML
        self.refresh()
        threading.Thread(target=self.follow, daemon=True).start()

    def onAction(self, action):
        if action.getId() in BACK + (xbmcgui.ACTION_STOP,):
            self.close()

    def show(self, texts):
        for control, value in texts.items():
            if self.texts.get(control) == value:
                continue
            self.texts[control] = value
            if control in self.LABELS:
                self.getControl(control).setLabel(value)
            else:
                self.getControl(control).setText(value)

    def test_label(self, on):
        return "Stop glasses test" if on else "Test glasses"

    def refresh(self):
        self.state = read_state()
        colour, title, detail = summary(self.state)
        self.show({28: self.header,
                   30: "[COLOR %s]●[/COLOR]  %s" % (colour, title),
                   31: detail,
                   40: GLASSES,
                   self.TEST: self.test_label(self.state.get("test_hz"))})

    def follow(self):
        """The board comes and goes and 3D starts while the window is up."""
        monitor, gone = xbmc.Monitor(), 0
        while self.open and not monitor.waitForAbort(1):
            gone = 0 if xbmc.getCondVisibility(self.WINDOW) else gone + 1
            if gone >= 3:
                self.close()
            elif self.open:
                self.refresh()

    def onClick(self, control):
        if control == self.TEST:
            if self.state.get("test_hz"):
                tell_service("test_off")
            else:
                rate = pick_rate("Test the glasses at which rate?")
                if rate is None:
                    return
                tell_service("test_%d" % RATES[rate])
            want = not self.state.get("test_hz")
            self.state["test_hz"] = want
            self.show({self.TEST: self.test_label(want)})   # follow() confirms within a second
        elif control in (self.PATTERN_BUTTON, self.SETTINGS):
            self.next = "pattern" if control == self.PATTERN_BUTTON else "settings"
            self.close()
        elif control == self.CLOSE:
            self.close()

    def close(self):
        self.open = False
        super().close()


class Tune(xbmcgui.WindowXMLDialog):
    """Over the playing test pattern: the Settings page's lens settings and
    the 3D glasses delay for the pattern's rate, with their labels, ranges,
    steps and help, applied as they change. Left/Right change the focused
    value, Select toggles, Back or Stop ends the pattern."""
    DELAY, CLOSED, MANUAL, REDRAW, BOARD_INFO = 41, 42, 43, 44, 45
    TITLE, HELP = 50, 60
    HELP_IDS = {DELAY: 30031, CLOSED: 30022, MANUAL: 30024, REDRAW: 30028, BOARD_INFO: 30022}

    def __init__(self, *args, **kwargs):
        self.rate = kwargs.get("rate")
        self.pattern = pattern_file(self.rate)
        self.player = xbmc.Player()
        self.open = False
        self.texts = {}
        self.focused = None

    def onInit(self):
        self.open = True
        self.texts = {}
        self.focused = None
        self.getControl(self.TITLE).setLabel("%s  -  %s fps test pattern" % (
            ADDON.getLocalizedString(30020), self.rate))
        self.getControl(self.BOARD_INFO).setEnabled(False)
        self.refresh()
        threading.Thread(target=self.follow, daemon=True).start()

    def row(self, control, label, value=""):
        if self.texts.get(control) != (label, value):
            self.texts[control] = (label, value)
            self.getControl(control).setLabel(label, label2=value)

    def refresh(self, delay=None):
        """The rows as the settings are now, shown as the Settings page shows
        them (the redraw rows only where it shows them)."""
        pi = read_state().get("kind") != "board"
        closed = ADDON.getSettingBool("lens_closed")
        manual = ADDON.getSettingInt("lens_redraw_manual") == 1
        self.row(self.DELAY, "%s (%s fps)" % (ADDON.getLocalizedString(30030).rstrip("…."), self.rate),
                 "%d µs" % (self.delay() if delay is None else delay))
        self.row(self.BOARD_INFO, ADDON.getLocalizedString(30029))
        self.row(self.CLOSED, ADDON.getLocalizedString(30021), "On" if closed else "Off")
        self.row(self.MANUAL, ADDON.getLocalizedString(30023), ADDON.getLocalizedString(30026 if manual else 30025))
        self.row(self.REDRAW, ADDON.getLocalizedString(30027), str(ADDON.getSettingInt("lens_redraw_us")))
        for control, visible in ((self.BOARD_INFO, not pi), (self.CLOSED, pi),
                                 (self.MANUAL, pi and closed), (self.REDRAW, pi and closed and manual)):
            if self.texts.get(("visible", control)) != visible:
                self.texts[("visible", control)] = visible
                self.getControl(control).setVisible(visible)
        focused = self.getFocusId()
        if focused != self.focused and focused in self.HELP_IDS:
            self.focused = focused
            self.getControl(self.HELP).setText(ADDON.getLocalizedString(self.HELP_IDS[focused]))

    def follow(self):
        """Closes when the pattern stops; shows changes made elsewhere."""
        monitor = xbmc.Monitor()
        while self.open and not monitor.waitForAbort(0.5):
            if not playing_pattern(self.player, self.pattern):
                self.open = False
                self.close()
            elif self.open:
                self.refresh()

    def delay(self):
        """The delay kept for the pattern's rate, or Kodi's where it starts."""
        return (read_state().get("delays") or {}).get(self.rate, kodi_delay())

    def change(self, control, step):
        """step: -1 (Left), +1 (Right) or 0 (Select)."""
        if control == self.DELAY and step:
            us = core.clamp_delay(self.delay() + step * core.DELAY_STEP_US)
            tell_service(core.delay_message(self.rate, us))     # the service keeps and applies it
            self.refresh(us)
            return
        if control == self.CLOSED:
            ADDON.setSettingBool("lens_closed", not ADDON.getSettingBool("lens_closed"))
        elif control == self.MANUAL:
            ADDON.setSettingInt("lens_redraw_manual", 1 - ADDON.getSettingInt("lens_redraw_manual"))
        elif control == self.REDRAW and step:
            ADDON.setSettingInt("lens_redraw_us", core.snap(
                ADDON.getSettingInt("lens_redraw_us") + step * core.REDRAW_STEP_US,
                core.REDRAW_STEP_US, core.REDRAW_MIN_US, core.REDRAW_MAX_US))
        else:
            return
        tell_service("lens")                     # the service rereads them at once
        self.refresh()

    def onClick(self, control):
        self.change(control, 0)

    def onAction(self, action):
        action_id = action.getId()
        if action_id in BACK + (xbmcgui.ACTION_STOP,):
            self.finish()
        elif action_id in (xbmcgui.ACTION_MOVE_LEFT, xbmcgui.ACTION_MOVE_RIGHT):
            self.change(self.getFocusId(), -1 if action_id == xbmcgui.ACTION_MOVE_LEFT else 1)
        elif action_id in (xbmcgui.ACTION_MOVE_UP, xbmcgui.ACTION_MOVE_DOWN):
            self.refresh()                       # the help follows the focus

    def finish(self):
        self.open = False
        if playing_pattern(self.player, self.pattern):
            self.player.stop()
        self.close()


def wait_until(done, seconds=10.0):
    """Polls done() every 0.1 s: True once it is, False on timeout or abort."""
    monitor = xbmc.Monitor()
    for _ in range(int(seconds * 10)):
        if done():
            return True
        if monitor.waitForAbort(0.1):
            return False
    return False


def tune(rate):
    """The tuning panel once the pattern is on screen."""
    wait_until(lambda: xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)"))
    if xbmc.Monitor().abortRequested() or not playing_pattern(xbmc.Player(), pattern_file(rate)):
        return
    window = Tune("script-shutter3d-tune.xml", ADDON.getAddonInfo("path"), "Default", "1080i",
                  rate=rate)
    window.doModal()
    del window


def pick_rate(heading):
    """The film rate the viewer picks, or None."""
    rates = list(RATES)
    chosen = xbmcgui.Dialog().select(
        heading, ["%s fps  (%g Hz)" % (rate, round(RATES[rate] / 1000.0, 1)) for rate in rates])
    return rates[chosen] if chosen >= 0 else None


def pattern_file(rate):
    return os.path.join(MEDIA, "crosstalk-%s.3D.SBS.mkv" % rate)


def play_pattern(pattern):
    """The test pattern in HFR VRR, the mode chosen as if by hand before it
    starts, so Kodi does not ask. A film already playing is stopped first, if
    the viewer agrees."""
    player = Stops()
    if player.isPlaying():
        if not xbmcgui.Dialog().yesno(NAME, "Stop what is playing and play the test pattern?"):
            return False
        # Kodi forgets a chosen 3D mode when it handles the stop, which comes
        # after the player has closed: wait for it, not for the player
        player.stop()
        wait_until(player.stopped.is_set)
    rpc("GUI.SetStereoscopicMode", mode="hfr_vrr")
    player.play(pattern)
    if wait_until(lambda: playing_pattern(player, pattern)):
        return True
    if xbmc.Monitor().abortRequested():
        return False
    xbmcgui.Dialog().notification(NAME, "The test pattern did not start",
                                  xbmcgui.NOTIFICATION_ERROR, 5000)
    return False


class Stops(xbmc.Player):
    """A player that sees playback stop (called back while this thread waits
    in waitForAbort)."""

    def __init__(self):
        super().__init__()
        self.stopped = threading.Event()

    def onPlayBackStopped(self):
        self.stopped.set()

    def onPlayBackEnded(self):
        self.stopped.set()


def playing_pattern(player, pattern):
    try:
        return player.isPlayingVideo() and player.getPlayingFile() == pattern
    except RuntimeError:                         # it stopped in between
        return False


def kodi_delay():
    """Kodi's 3D glasses delay as it is now (where a rate not tuned yet starts)."""
    value = (rpc("Settings.GetSettingValue", setting=DELAY_SETTING) or {}).get("value")
    return value if isinstance(value, int) else 0


def edit_delays():
    """Settings -> 3D glasses delay: the delay of each film rate, set by hand.
    The service keeps them and applies one at once if 3D plays at its rate."""
    delays = dict(read_state().get("delays") or {})
    start = kodi_delay()
    dialog = xbmcgui.Dialog()
    while True:
        rates = list(RATES)
        items = ["%s fps:  %d µs" % (r, delays[r]) if r in delays
                 else "%s fps:  not set yet (starts at %d µs)" % (r, start) for r in rates]
        chosen = dialog.select("3D glasses delay by film rate", items)
        if chosen < 0:
            return
        rate = rates[chosen]
        text = dialog.input("3D glasses delay at %s fps, in µs (%d to %d)"
                            % (rate, core.DELAY_MIN_US, core.DELAY_MAX_US),
                            defaultt=str(delays.get(rate, start)))
        if not text:
            continue
        try:
            us = core.clamp_delay(int(text.strip()))
        except ValueError:
            dialog.notification(NAME, "Not a number: %s" % text, xbmcgui.NOTIFICATION_WARNING, 3000)
            continue
        delays[rate] = us
        tell_service(core.delay_message(rate, us))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "delay":
        edit_delays()
        return
    while True:
        window = Main("script-shutter3d-main.xml", ADDON.getAddonInfo("path"), "Default", "1080i")
        window.doModal()
        chosen = window.next
        del window
        if chosen == "settings":
            ADDON.openSettings()                 # the service rereads them itself
        elif chosen == "pattern":
            rate = pick_rate("Test pattern at which film rate?")
            if rate and play_pattern(pattern_file(rate)):
                tune(rate)
                return
        else:
            return


if __name__ == "__main__":
    main()
