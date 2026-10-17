"""Kodi side: the emitter on USB (a Raspberry Pi Zero W for Bluetooth glasses,
which the service boots with the firmware the add-on holds, or a VESA stereo
board) keeps the glasses in step whenever Kodi shows 3D frame-sequentially.
Kodi's own 3D modes switch the output; this follows. The service owns the
emitter; the screen (default.py) asks it through NotifyAll and reads its state
from a Home window property (ids.py).

The eye switch delay is Kodi's own display setting (3D glasses delay, set
while 3D plays like the audio offset), followed here. The screen's drawing
time takes a different share of the frame at each rate, so the service keeps
one value for each film rate: it puts the rate's value into Kodi's setting when
3D starts at that rate (a rate not tuned yet keeps the value in use), and
remembers what the slider is moved to while it plays."""

import json
import os
import threading
import time

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from . import board, core, pi
from .ids import ADDON, ADDON_ID, DELAY_SETTING, HOME, KIND_PROPERTY, STATE_PROPERTY, rpc

TICK_S = 0.25                    # how soon a delay change reaches the board
START_WAIT_S = 5.0               # a hold waits this long for the film to be playing
RATES = core.FILM_RATES + core.OTHER_RATES
DELAYS_FILE = "special://profile/addon_data/%s/delays.json" % ADDON_ID


def log(msg, level=xbmc.LOGINFO):
    xbmc.log("[%s] %s" % (ADDON_ID, msg), level)


class Settings:
    def __init__(self):
        a = xbmcaddon.Addon()
        self.start_pause = a.getSettingNumber("start_pause")
        self.lens_closed = a.getSettingBool("lens_closed")
        self.lens_redraw_adjust_us = a.getSettingInt("lens_redraw_adjust_us")
        self.lens_redraw_auto_us = a.getSettingInt("lens_redraw_auto_us")
        # before 0.1.12: a manual redraw time in us, made an adjustment on the next 3D film
        self.lens_redraw_manual = a.getSettingInt("lens_redraw_manual") == 1
        self.lens_redraw_us = a.getSettingInt("lens_redraw_us")


class Service(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.entered_3d = threading.Event()      # set on the board's thread, acted on here
        boot_dir = os.path.join(xbmcvfs.translatePath(ADDON.getAddonInfo("path")), "resources", "firmware", "boot")
        self.booter = pi.Booter(boot_dir, log)
        self.link = board.BoardLink(log, self.entered_3d.set, opening=self._opening,
                                    expected_build=pi.bundled_build(boot_dir))
        self.published = None
        self.delay = None                        # Kodi's 3D glasses delay, as last applied
        self.delay_misses = 0
        self.item_rate = None                    # the playing item's film rate, once known
        self.rate_seen = None                    # what Kodi said it was on the last tick
        self.rate = None                         # the rate whose delay Kodi's setting holds, in 3D
        self.delays = load_delays()              # film rate -> delay, with a delay per frame rate
        self.armed = False                       # the playing item may still get its start pause
        self.hold_cancel = None                  # the running start pause's cancel
        self.adjust_from_manual = None           # the adjustment an old manual redraw time makes
        self.auto_seen = None                    # the automatic redraw time of the 3D base in use

    def onSettingsChanged(self):
        self.settings = Settings()
        self.link.set_opening()

    def _opening(self):
        """The lens window for the emitter, from the settings and the HDMI
        mode in use (read each time: 3D switches the mode)."""
        s = self.settings
        timing = core.parse_vinfo(core.read_sysfs(core.DISPLAY_VINFO))
        base = core.base_picture_us(timing)
        if base is not None:
            self.auto_seen = base
        if s.lens_redraw_manual and base is not None:
            # an old manual time, on the base it was set for: the same opening now
            self.adjust_from_manual = core.snap(
                s.lens_redraw_us - base, core.ADJUST_STEP_US,
                core.ADJUST_MIN_US, core.ADJUST_MAX_US)
            return core.opening(s.lens_closed, self.adjust_from_manual, timing)
        return core.opening(s.lens_closed, s.lens_redraw_adjust_us, timing)

    def _keep_redraw_settings(self):
        """Written here, after the board has its window, not while sending it:
        the automatic redraw time of the 3D base in use, shown in the settings
        beside the adjustment, and the adjustment an old manual redraw time
        makes (the same opening)."""
        auto, self.auto_seen = self.auto_seen, None
        if auto is not None and auto != self.settings.lens_redraw_auto_us:
            ADDON.setSettingInt("lens_redraw_auto_us", auto)
        us, self.adjust_from_manual = self.adjust_from_manual, None
        if us is not None:
            ADDON.setSettingInt("lens_redraw_adjust_us", us)
            ADDON.setSettingInt("lens_redraw_manual", 0)
            log("the manual redraw time is now an adjustment of %+d us" % us)

    def onNotification(self, sender, method, data):
        if sender == "xbmc":
            self._player_event(method)
            return
        if sender != ADDON_ID:
            return
        delay = core.parse_delay_message(method[6:]) if method.startswith("Other.") else None
        if delay:
            self._set_rate_delay(*delay)
        elif method == "Other.lens":                     # the tuning panel changed them
            self.onSettingsChanged()
        elif method == "Other.test_off":
            self.link.test(0.0)
        elif method.startswith("Other.test_") and method[11:].isdigit():
            self.link.test(int(method[11:]) / 1000.0)    # test_<mHz>
        else:
            return
        self._publish()                                  # the screen sees it at once

    def _set_rate_delay(self, rate, us):
        """A delay for a film rate, from the settings or the tuning panel: kept,
        and put into Kodi's setting at once if 3D plays at that rate
        (_follow_delay passes it on)."""
        us = core.clamp_delay(us)
        self.delays[rate] = us
        save_delays(self.delays)
        log("%s fps: 3D glasses delay set to %d us" % (rate, us))
        if self.rate == rate:
            rpc("Settings.SetSettingValue", setting=DELAY_SETTING, value=us)

    def _player_event(self, method):
        """One start pause per item. Anything the viewer (or Kodi) does to the
        player while it holds ends the hold without a resume of ours."""
        if method in ("Player.OnPlay", "Player.OnStop", "Player.OnResume"):
            self._cancel_hold()
        if method in ("Player.OnPlay", "Player.OnStop"):
            self.item_rate = self.rate_seen = None
            self.armed = method == "Player.OnPlay"

    def _cancel_hold(self):
        if self.hold_cancel is not None:
            self.hold_cancel.set()
            self.hold_cancel = None

    def _start_hold(self):
        """The box started reporting eyes: hold the picture while the glasses
        find the emitter. The eyes keep alternating on the paused picture, so
        the glasses sync and no part of the film is missed."""
        seconds = self.settings.start_pause
        if seconds <= 0 or not self.armed:
            return
        self.armed = False
        self.hold_cancel = cancel = threading.Event()
        threading.Thread(target=hold, args=(seconds, cancel), daemon=True).start()

    def _follow_delay(self):
        try:
            value = (rpc("Settings.GetSettingValue", setting=DELAY_SETTING) or {}).get("value")
        except ValueError:
            value = None
        if not isinstance(value, int):
            self.delay_misses += 1
            if self.delay is None and self.delay_misses == 4:    # a second of it, not a hiccup
                log("no %s in this Kodi: the glasses delay stays 0" % DELAY_SETTING, xbmc.LOGWARNING)
                self.delay = 0
                self.link.set_delay(0)
            return
        self.delay_misses = 0
        if value != self.delay:
            self.delay = value
            self.link.set_delay(value)
        if self.rate is not None and self.delays.get(self.rate) != value:
            self.delays[self.rate] = value       # tuned at this rate (or its first time)
            save_delays(self.delays)

    def _follow_rate(self):
        """On 3D, give Kodi's setting the film rate's value before the delay is followed. The rate is read once per
        item, when Kodi gives the same one twice in a row: it can start at 0,
        and be replaced by a measured one during the film."""
        if not self.link.in_3d:
            self.rate = None
            return
        if self.rate is not None:
            return
        if self.item_rate is None:
            rate = film_rate(xbmc.getInfoLabel("Player.Process(VideoFPS)"))
            if rate is None or rate != self.rate_seen:
                self.rate_seen = rate
                return
            self.item_rate = rate
        rate = self.item_rate
        if rate in self.delays:
            if rpc("Settings.SetSettingValue", setting=DELAY_SETTING, value=self.delays[rate]) is True:
                log("%s fps: 3D glasses delay %d us" % (rate, self.delays[rate]))
            else:                                # refused: start this rate again from Kodi's value
                log("Kodi refused %d us for %s fps: dropped" % (self.delays.pop(rate), rate),
                    xbmc.LOGWARNING)
                save_delays(self.delays)
        self.rate = rate

    def _publish(self):
        state = self.link.state()
        state["delays"] = self.delays
        kind = state.get("kind", "")
        state = json.dumps(state, sort_keys=True)
        if state != self.published:
            home = xbmcgui.Window(HOME)
            home.setProperty(STATE_PROPERTY, state)
            home.setProperty(KIND_PROPERTY, kind)    # the settings grey out what this emitter ignores
            self.published = state

    def run(self):
        log("started")
        self._follow_delay()
        self.booter.start()
        self.link.start()
        while not self.waitForAbort(TICK_S):
            if self.entered_3d.is_set():
                self.entered_3d.clear()
                self._start_hold()
            self._follow_rate()
            self._follow_delay()
            self._keep_redraw_settings()
            self._publish()
        # a running hold sees the abort too and gives the film back
        self.link.stop()
        self.booter.stop()
        xbmcgui.Window(HOME).clearProperty(STATE_PROPERTY)
        xbmcgui.Window(HOME).clearProperty(KIND_PROPERTY)
        log("stopped")


def film_rate(label):
    """The film rate Kodi reports, as the name of the nearest common rate
    ("23.976", "25"), or None."""
    try:
        fps = float(label)
    except ValueError:
        return None
    if fps <= 0:
        return None
    near = min(RATES, key=lambda r: abs(r - fps))
    return core.rate_name(near if abs(near - fps) < 0.003 * near else round(fps, 3))


def load_delays():
    try:
        with open(xbmcvfs.translatePath(DELAYS_FILE)) as f:
            delays = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(delays, dict):
        return {}
    return {k: v for k, v in delays.items() if isinstance(k, str) and isinstance(v, int)}


def save_delays(delays):
    path = xbmcvfs.translatePath(DELAYS_FILE)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".new", "w") as f:
            json.dump(delays, f, sort_keys=True)
        os.replace(path + ".new", path)
    except OSError as e:
        log("could not save the delays per frame rate: %s" % e, xbmc.LOGWARNING)


def playing_video():
    """(player id, file, speed) of the video player, or None."""
    for p in rpc("Player.GetActivePlayers") or []:
        if p.get("type") == "video":
            pid = p["playerid"]
            speed = (rpc("Player.GetProperties", playerid=pid, properties=["speed"]) or {}).get("speed")
            item = (rpc("Player.GetItem", playerid=pid, properties=["file"]) or {}).get("item") or {}
            return pid, item.get("file"), speed
    return None


def hold(seconds, cancel):
    """Pause for <seconds>, then play the same item again, unless the hold was
    cancelled meanwhile (stop, another item, the viewer's own play). It waits
    first for the film to be playing: Kodi pauses it while it asks for the 3D
    mode, and a film the viewer paused is left alone."""
    monitor = xbmc.Monitor()
    deadline = time.monotonic() + START_WAIT_S
    while True:
        if cancel.is_set():
            return
        now = playing_video()
        if now and now[2] == 1:
            break
        if time.monotonic() > deadline or monitor.waitForAbort(0.1):
            return
    player, file, _ = now
    rpc("Player.PlayPause", playerid=player, play=False)
    log("3D start: paused %.1f s for the glasses" % seconds)
    end = time.monotonic() + seconds
    while not cancel.is_set() and time.monotonic() < end:
        if monitor.waitForAbort(min(0.1, max(0.0, end - time.monotonic()))):
            break                                # shutting down: still give the film back
    if cancel.is_set():
        return
    now = playing_video()
    if now and now[0] == player and now[1] == file and now[2] == 0:
        rpc("Player.PlayPause", playerid=player, play=True)
