"""Kodi side: playback backend, settings and wiring."""

import functools
import json
import os
import re
import socket
import threading
import time
from xml.etree import ElementTree

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from . import hdmistate, live, upgci
from .generator import Generator
from .ids import ADDON_ID, STATE_PROP, STATUS_PROP, cache_dir  # noqa: F401  (also for callers)
from .patterns import SIGNAL_MODES
from .resolve import Client as ResolveClient

_addon = xbmcaddon.Addon(ADDON_ID)
VERSION = _addon.getAddonInfo("version")
_debug = False


def reload_settings():
    """An Addon object keeps the settings it first read; take a fresh one."""
    global _addon, _debug
    _addon = xbmcaddon.Addon(ADDON_ID)
    _debug = setting_bool("debug_log")


def setting(key, default=""):
    v = _addon.getSetting(key)
    return v if v != "" else default


def setting_int(key, default):
    try:
        return int(float(setting(key, default)))
    except ValueError:
        return default


def setting_bool(key, default=False):
    v = setting(key, None)
    return default if v is None else v.lower() == "true"


def log(msg, error=False, debug=False):
    if debug and not _debug:
        return
    xbmc.log("[%s] %s" % (ADDON_ID, msg), xbmc.LOGERROR if error else xbmc.LOGINFO)


_HDR_EOTFS = ("PQ", "HLG", "HDR", "HDR10+")   # hdmitx mutes video when it enters one of these


def _sysfs(path):
    """A sysfs value without its trailing newline, None when the node is missing."""
    value = hdmistate.read(path)
    return None if value is None else value.strip()


def _hdr_status():
    """The HDR signal on the wire (hdmistate.hdr_eotf), None off Amlogic."""
    return hdmistate.hdr_eotf(_sysfs(hdmistate.HDMITX + "hdmi_hdr_status"))


def _expected_hdmi(mode):
    return SIGNAL_MODES[mode].hdmi if mode in SIGNAL_MODES else None


def _display_mode():
    return _sysfs(hdmistate.DISPLAY_MODE)


class _Events(xbmc.Player):
    """Player callbacks as flags. Kodi delivers them while some thread of this
    add-on sits in Monitor.waitForAbort, which the waiting loops below do."""

    def __init__(self):
        xbmc.Player.__init__(self)
        self.reset()
        self.last_file = None
        self.on_ended = self.on_stopped = None

    def reset(self):
        self.started = self.av_started = self.failed = False

    def onPlayBackStarted(self):
        self.started = True

    def onAVStarted(self):
        try:
            self.last_file = self.getPlayingFile()
        except RuntimeError:
            self.last_file = None
        self.av_started = True

    def onPlayBackError(self):
        self.failed = True

    def onPlayBackStopped(self):
        if self.started:            # a stop before our item started is the previous clip
            self.failed = True
            if self.on_stopped:
                self.on_stopped()

    def onPlayBackEnded(self):
        if self.started and not self.av_started:
            self.failed = True
        elif self.on_ended:
            self.on_ended()


class KodiBackend:
    """Plays a pattern clip fullscreen and returns once it is on the wire.

    Ready means: Kodi reported onAVStarted for our file, the HDMI transmitter
    signals the requested dynamic range (Amlogic sysfs, skipped elsewhere),
    the transmitter's HDR-entry video mute has run out, Kodi's own "delay after
    change of refresh rate" has passed if the output mode changed, and finally
    settle_frames more frames for the display's own processing.

    Measured on the AM9: between two clips of the same dynamic range the
    infoframe blip (ST 2084 -> SDR -> ST 2084, ~0.25 s) is over ~150 ms before
    onAVStarted; from SDR the transmitter enters HDR ~0.4 s after onAVStarted.
    """

    TOAST_MAX = 20

    SWITCH_TIMEOUT = 3.0        # a pattern not on screen by then restarts the live stream
    START_TIMEOUT = 10.0        # the live stream's first picture

    def __init__(self, settle_frames=2, timeout=20, root=None, continuous=False):
        self.events = _Events()
        self.events.on_ended = self._loop
        self.events.on_stopped = self._stopped
        self.monitor = xbmc.Monitor()
        self.settle_frames = settle_frames
        self.timeout = timeout
        self.root = root
        self.current = None
        self.notifier = None
        self.closed = False         # the service stopped or replaced this generator
        # the continuous stream, where the Amlogic sync clock can pace it
        self.live = live.Stream(log) if continuous and live.clock_available() else None

    def _playing_file(self):
        try:
            if self.events.isPlayingVideo():
                return self.events.getPlayingFile()
        except RuntimeError:
            pass
        return None

    def _wait(self, cond, seconds):
        """Poll cond every 10 ms (pumping Kodi callbacks); False on timeout/abort."""
        deadline = time.time() + seconds
        while not cond():
            if time.time() > deadline or self.monitor.waitForAbort(0.01):
                return False
        return True

    def _sleep(self, seconds):
        # waitForAbort(t) with t*1000 <= 0 has no timeout at all
        if seconds > 0:
            self.monitor.waitForAbort(seconds)

    def _stopped(self):
        self.current = None         # stopped by the user, or a failed start that play() clears anyway

    def _loop(self):
        # Our clip ran its full length while still the current pattern: keep it up.
        if self.current and self.events.last_file == self.current and self._playing_file() is None:
            log("pattern clip ended, restarting it")
            self.events.play(self.current, self._item(self.current,
                                                      stream=live.PATH in self.current))

    @staticmethod
    def _item(path, stream=False):
        item = xbmcgui.ListItem(label="Test pattern", path=path)
        item.getVideoInfoTag().setTitle("Test pattern")
        if stream:
            # a live source: Kodi starts on a short buffer instead of waiting for
            # seconds of cache that a stream made in real time never has
            item.setProperty("isrealtimestream", "true")
            item.setMimeType("video/x-matroska")
            item.setContentLookup(False)
        return item

    COVER_TIMEOUT = 5.0         # a dialog still over the video by then fails the pattern

    @property
    def continuous(self):
        """Patterns go to show() (the continuous stream) rather than play() (clips)."""
        return self.live is not None

    def play(self, path, mode=None, fps=None):
        """A clip on screen: True once the pattern is what the screen shows."""
        return self._finish(self._play(path, mode, fps))

    def show(self, enc, mode=None, fps=None):
        """The continuous stream: enc on screen, in the running stream when it has
        enc's format (the signal is never interrupted), else in a new stream."""
        return self._finish(self._show(enc, mode, fps))

    def _finish(self, shown):
        self._toast_wait()
        return shown and self._uncovered()

    def _uncovered(self):
        """The pattern is what the screen shows: full-screen video, no dialog over it (the
        add-on's own main screen steps aside within a second of a pattern starting)."""
        def clear():
            return (xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)")
                    and not xbmc.getCondVisibility("System.HasVisibleModalDialog"))
        if clear():
            return True
        self._fullscreen()
        if self._wait(lambda: self.closed or clear(), self.COVER_TIMEOUT) and not self.closed:
            return True
        log("pattern covered by another window after %.0fs: not displayed" % self.COVER_TIMEOUT,
            error=True)
        return False

    def _toast_wait(self):
        if self.notifier:       # a toast draws over the pattern: no ACK while one is up
            left = self.notifier.remaining()
            while left > 0 and not self.closed and not self.monitor.waitForAbort(left):
                left = self.notifier.remaining()
            # the skin keeps it longer: open/close animations, a long text scrolls to its end
            self._wait(lambda: self.closed or not xbmc.getCondVisibility(
                "Window.IsVisible(notification)"), self.TOAST_MAX)

    def _show(self, enc, mode, fps):
        if self.closed:
            return False
        stream, frame = self.live, 1.0 / (fps or 24.0)
        assert stream is not None, "show() is only used with the live stream"
        if (self.current and stream.matches(enc) and stream.started()
                and self._playing_file() == self.current):
            token = stream.switch(enc)
            t0 = time.time()
            if not self._wait(lambda: self.closed or stream.shown(token), self.SWITCH_TIMEOUT):
                log("pattern not on screen %.0fs into the live stream: restarting the stream"
                    % self.SWITCH_TIMEOUT, error=True)
            else:
                if self.closed:
                    return False
                expected = _expected_hdmi(mode)
                hdr = _hdr_status()
                if hdr is not None and expected and hdr not in expected:
                    # the link renegotiated under the stream (AVR or TV restart): a new
                    # stream re-checks the signal, and fails if it still is not right
                    log("HDMI reports %r in the live stream, expected %s: restarting it"
                        % (hdr, "/".join(expected)), error=True)
                else:
                    self._fullscreen()
                    self._sleep(self.settle_frames * frame)
                    log("pattern up in %.2fs (in the live stream)" % (time.time() - t0),
                        debug=True)
                    return True
        for attempt in (1, 2):
            if self.closed:
                return False
            shown = self._start_stream(stream, enc, mode, fps)
            if shown is not None:
                return shown
            log("live stream: no picture %.0fs after it started%s" % (
                self.START_TIMEOUT, ", restarting it" if attempt == 1 else ""), error=True)
        stream.stop()
        self.current = None
        return False

    def _start_stream(self, stream, enc, mode, fps):
        """A new stream showing enc: True once up, False when it cannot start, None when
        it plays but its first picture never showed (the caller tries again)."""
        try:
            url = stream.start(enc)
        except RuntimeError:        # the service closed the stream meanwhile
            return False
        begun = self._begin(url, self._item(url, stream=True), "live stream", stoppable=True)
        if begun is None:
            stream.stop()
            return False
        t0, hdr_before, mode_before = begun
        t_av = time.time()
        stream.player_started()     # the display clock is this stream's from now on
        if not self._wait(lambda: self.closed or stream.started(), self.START_TIMEOUT):
            return None
        if self.closed:             # stopped while the stream was starting
            if self._playing_file() == url:
                self.events.stop()
            stream.stop()
            return False
        self._fullscreen()
        return self._signal_ready(t0, t_av, hdr_before, mode_before, mode, fps)

    def _play(self, path, mode, fps):
        if self.closed:
            return False
        if self._playing_file() == path:
            self._fullscreen()
            return True
        begun = self._begin(path, self._item(path), "pattern " + path)
        if begun is None:
            return False
        t0, hdr_before, mode_before = begun
        self._fullscreen()
        return self._signal_ready(t0, time.time(), hdr_before, mode_before, mode, fps)

    def _begin(self, url, item, what, stoppable=False):
        """Play url until Kodi reports it started: (t0, hdr_before, mode_before), or None
        when it failed, timed out or the backend closed meanwhile (playback stopped).
        stoppable: the caller can end the source itself (the live stream), so a close
        need not wait for Kodi; a clip must, or it would start after the stop."""
        hdr_before, mode_before = _hdr_status(), _display_mode()
        self.current = url
        ev = self.events
        ev.reset()
        ev.play(url, item)
        t0 = time.time()
        started = self._wait(lambda: (stoppable and self.closed) or ev.failed or (
            ev.av_started and self._playing_file() == url), self.timeout)
        if self.closed:             # stopped while it was starting
            if self._playing_file() == url:
                ev.stop()
            self.current = None
            return None
        if not started or ev.failed:
            log("%s did not start (%s)" % (what, "error" if ev.failed else "timeout"), error=True)
            self.current = None
            return None
        return t0, hdr_before, mode_before

    def _signal_ready(self, t0, t_av, hdr_before, mode_before, mode, fps):
        """After onAVStarted: wait for the requested signal on the wire, the HDR-entry
        mute, a refresh-rate change's delay and settle_frames. False when the requested
        signal never reached the wire: the pattern is then not the one asked for."""
        frame = 1.0 / (fps or 24.0)
        expected = _expected_hdmi(mode)
        hdr_after = hdr_before
        if hdr_before is not None and expected:
            if not self._wait(lambda: self.closed or _hdr_status() in expected, 3):
                log("HDMI reports %r, expected %s: pattern not displayed as requested"
                    % (_hdr_status(), "/".join(expected)), error=True)
                return False
            hdr_after = _hdr_status() or ""
            t_flip = time.time()
            if hdr_after != hdr_before and hdr_after in _HDR_EOTFS:
                # hdmitx mutes video for hdr_mute_frame frames when the DRM infoframe mode changes
                mute = int(_sysfs(hdmistate.HDMITX + "hdr_mute_frame") or 0)
                self._sleep(t_flip + mute * frame - time.time())
        mode_after = _display_mode()
        if mode_after != mode_before:
            delay = _kodi_setting("videoscreen.delayrefreshchange", 0) / 10.0
            self._sleep(t_av + delay - time.time())
        self._sleep(self.settle_frames * frame)
        log("pattern up in %.2fs (hdmi %s -> %s, mode %s -> %s)" % (
            time.time() - t0, hdr_before, hdr_after, mode_before, mode_after), debug=True)
        return not self.closed

    @staticmethod
    def _fullscreen():
        if not xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)"):
            xbmc.executebuiltin("ActivateWindow(fullscreenvideo)")

    def stop(self):
        self.current = None
        f = self._playing_file()
        if self.live:
            # the stream first: Player.stop() waits for Kodi to close it, and Kodi's read
            # of a stream that waits for the display (frozen while it closes) never
            # returned - the stop hung (reproduced on the AM9, 2026-09-27)
            self.live.stop()
        if self.live and f and live.PATH in f:
            self.events.stop()
            return
        if not f or not os.path.basename(f).startswith("pattern-"):
            return
        if self.root and os.path.dirname(f) != os.path.normpath(self.root):
            return
        self.events.stop()


def _kodi_setting(name, default):
    try:
        reply = json.loads(xbmc.executeJSONRPC(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "Settings.GetSettingValue",
             "params": {"setting": name}})))
        return reply["result"]["value"]
    except (ValueError, KeyError, TypeError):
        return default


DEVICECONTROL_PORT, LIGHTSPACE_PORT = 1977, 20123   # fixed by the protocols (PGenerator's)
RES_CHOICES = {"1": (1920, 1080), "2": (3840, 2160), "3": (1280, 720)}
FPS_CHOICES = {"1": (24000, 1001), "2": (24, 1), "3": (25, 1), "4": (30000, 1001),
               "5": (30, 1), "6": (50, 1), "7": (60000, 1001), "8": (60, 1)}
MODE_CHOICES = {m.setting: key for key, m in SIGNAL_MODES.items()}   # option value -> mode


def current_display():
    """(width, height, fps_num, fps_den) of the current output mode."""
    label = xbmc.getInfoLabel("System.ScreenResolution")
    m = re.search(r"(\d+)x(\d+)\D+([\d.]+)\s*Hz", label)
    if not m:
        log("cannot read the output mode from %r, assuming 1080p60" % label, error=True)
        return 1920, 1080, 60, 1
    num, den = upgci.fps_fraction(float(m.group(3)))
    return int(m.group(1)), int(m.group(2)), num, den


def default_format():
    w, h, n, d = current_display()
    w, h = RES_CHOICES.get(setting("resolution", "0"), (w, h))
    n, d = FPS_CHOICES.get(setting("framerate", "0"), (n, d))
    return w, h, n, d


@functools.cache
def _setting_ids():
    """Every setting id the add-on declares (resources/settings.xml)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "settings.xml")
    return tuple(sorted({s.get("id") for s in ElementTree.parse(path).iter("setting")}))


def make_generator(backend=None, continuous=False):
    reload_settings()
    root = cache_dir()
    gen = Generator(backend or KodiBackend(settle_frames=setting_int("settle_frames", 2), root=root,
                                           continuous=continuous),
                    root, log,
                    duration=setting_int("duration", 300),
                    cache_keep=setting_int("cache_keep", 1000))
    gen.set_default_format(*default_format())
    gen.set_mode(MODE_CHOICES.get(setting("default_mode", "0"), "sdr"))
    gen.overlay = setting_bool("debug_overlay")
    return gen


def cpu_serial():
    """PGenerator's SN: field 3 of the /proc/cpuinfo Serial line(s), whitespace removed."""
    try:            # grep Serial | awk '{print $3}' (fields split on blanks), then s/\s+//g
        with open("/proc/cpuinfo", "rb") as f:
            fields = [re.split(rb"[ \t\n]+", line.strip(b" \t\n")) for line in f if b"Serial" in line]
    except OSError:
        fields = []
    serial = re.sub(rb"[ \t\n\r\f\v]+", b"", b"".join(b"".join(f[2:3]) for f in fields))
    return serial.decode("latin-1") or upgci.NAME


def discovery_name():
    """PGenerator's discovery name: the host name, or PGenerator+ for an empty or
    default one; the device_name setting overrides it."""
    name = setting("device_name", "").strip()      # opt-in, not in discovery.pm:89-112
    if name:
        return name[:24]
    try:            # read_from_file: the bytes, "" when missing; \s is ASCII whitespace
        with open("/etc/hostname", "rb") as f:
            raw = f.read().strip(b" \t\n\r\f\v")
    except OSError:
        raw = b""
    if not raw or raw.lower() == b"pgenerator":
        return upgci.NAME
    return raw[:24].decode("utf-8", "surrogateescape")     # sent as these bytes


def local_ip():
    ip = xbmc.getInfoLabel("Network.IPAddress")
    if re.match(r"\d+\.\d+\.\d+\.\d+$", ip or "") and ip != "0.0.0.0":
        return ip
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))         # no packet: only picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"


def _peers(server):
    with server.lock:
        conns = list(server.clients)
    peers = set()
    for c in conns:
        try:
            peers.add(c.getpeername()[0])
        except OSError:
            pass
    ls = server.lightspace and server.lightspace.peer     # an outbound connection
    if ls:
        peers.add(ls[0])
    return peers


def _software(server, ip):
    """What the client at ip calls itself (Calman, HCFR, LightSpace, a CLIENTNAME=...)."""
    c = server.client()
    return c[1] if c and c[0] == ip else "Client"


def _named(names):
    """{ip: software} -> "Calman connected from a" / "HCFR connected from b, Calman ... c"."""
    by: dict[str, list[str]] = {}
    for ip in sorted(names):
        by.setdefault(names[ip], []).append(ip)
    return ", ".join("%s connected from %s" % (sw, ", ".join(ips)) for sw, ips in sorted(by.items()))


class Notifier:
    """Kodi toasts. Kodi queues them, each on screen for TIME plus its fade."""

    TIME, FADE = 2.0, 0.5

    def __init__(self):
        self.enabled = True
        self.until = 0.0
        self.lock = threading.Lock()

    def __call__(self, msg):
        log("notification: %s" % msg)
        if not self.enabled:
            return
        with self.lock:
            self.until = max(self.until, time.time()) + self.TIME + self.FADE
        xbmcgui.Dialog().notification("Pattern Generator", msg, xbmcgui.NOTIFICATION_INFO,
                                      int(self.TIME * 1000), False)

    def remaining(self):
        with self.lock:
            return self.until - time.time()


class Service(xbmc.Monitor):
    """Idle until told to start (NotifyAll(service.pattern.generator.allolive,start|stop));
    publishes its state in Home window properties PROP + state/status."""

    def __init__(self):
        xbmc.Monitor.__init__(self)
        self.gen = self.server = self.resolve = None
        self.running = False
        self.hdmi = None            # the idle loop's last HDMI summary (None: read it)
        self.notifier = Notifier()
        self.lock = threading.Lock()
        self.peers, self.resolve_up, self.status = {}, False, None     # peers: {ip: software}
        self.restart_due = False
        self.window = xbmcgui.Window(10000)
        self.publish()

    def start(self):
        self.settings_used = self._settings_snapshot()
        self.gen = make_generator(continuous=setting("pattern_playback", "0") == "0")
        self.notifier.enabled = setting_bool("notifications", True)
        self.gen.backend.notifier = self.notifier
        info = {
            "name": discovery_name,         # read per datagram (discovery.pm:108)
            "serial": cpu_serial(),
            "firmware": upgci.VERSION,
            "stop_on_disconnect": setting_bool("stop_on_disconnect", False),
        }
        calman = setting_bool("upgci_enabled", True)
        classic = setting_bool("classic_enabled", True)
        devicecontrol = setting_bool("devicecontrol_discovery", True)
        lightspace = setting_bool("lightspace_enabled", True)
        if calman or classic or devicecontrol or lightspace:
            port = setting_int("classic_port", 85) if classic else 0
            self.server = upgci.Server(self.gen, info, log,
                                       port=setting_int("upgci_port", 2100) if calman else 0,
                                       rpc_port=setting_int("rpc_port", 2101) if calman else 0,
                                       discovery=setting_bool("discovery", True),
                                       classic_port=port,
                                       devicecontrol_port=devicecontrol and DEVICECONTROL_PORT,
                                       lightspace_port=lightspace and LIGHTSPACE_PORT,
                                       format_file=os.path.join(xbmcvfs.translatePath(
                                           "special://profile/addon_data/%s" % ADDON_ID),
                                           "calman_format"))
            self.server.start()
        host = setting("resolve_host", "").strip()
        if setting_bool("resolve_enabled") and host:
            self.resolve = ResolveClient(self.gen, host, setting_int("resolve_port", 20002), log)
            self.resolve.start()
        with self.lock:
            self.peers, self.resolve_up = {}, False
        self.gen.signal_changes()           # only later changes are announced
        self.gen.notify = self.check
        log("started (%s)" % self.gen.sig.describe())
        self.publish()

    def shutdown(self):
        for part in (self.server, self.resolve):
            if part:
                part.stop()
        if self.gen:
            self.gen.notify = None
            self.gen.close()
            self.gen.backend.closed = True
            # its player keeps getting callbacks while a play() is still in flight: detach them
            self.gen.backend.events.on_ended = self.gen.backend.events.on_stopped = None
            if self.gen.backend.live:
                self.gen.backend.live.close()
        self.gen = self.server = self.resolve = None

    def address(self):
        server = self.server
        if not server:
            return ""
        with server.lock:
            ports = [s.getsockname()[1] for s in server.sockets
                     if s.type == socket.SOCK_STREAM and s.fileno() >= 0]
        return "%s:%s" % (local_ip(), "/".join(map(str, ports))) if ports else ""

    def describe(self, hdmi=None):
        """The status line; hdmi: the HDMI summary to show, read now when None."""
        if not self.running:
            return "Stopped"
        parts = []
        server, resolve = self.server, self.resolve
        if server:
            peers = _peers(server)
            addr = self.address()
            ls = server.lightspace
            if peers:
                parts.append(_named({ip: _software(server, ip) for ip in peers}))
            elif addr:
                parts.append("Listening on %s" % addr)
            elif server.ports or not (ls or server.devicecontrol_port):
                parts.append("Not listening (port in use?)")      # no TCP port could be bound
            elif ls:
                parts.append(("Not listening (UDP %d in use?)" if ls.bind_error
                              else "Waiting for LightSpace on UDP %d") % ls.port)
            else:
                parts.append("Answering DeviceControl discovery on UDP %d"
                             % server.devicecontrol_port)
        if resolve:
            parts.append(("Resolve connected to %s:%d" if resolve.sock is not None
                          else "Connecting to Resolve at %s:%d") % (resolve.host, resolve.port))
        status = " | ".join(parts) or "Running, no connection enabled"
        if hdmi is None:
            hdmi = hdmistate.summary()
        return status + (" | HDMI " + hdmi if hdmi else "")

    def publish(self, hdmi=None):
        status = self.describe(hdmi)
        if status != self.status:
            self.status = status
            self.window.setProperty(STATE_PROP, "running" if self.running else "stopped")
            self.window.setProperty(STATUS_PROP, status)

    def check(self, signal=True):
        """Connection (and, before a new pattern, signal) changes -> one toast; status.
        Calman's config commands redraw the last pattern; those redraws announce nothing,
        so a burst makes one toast at the next pattern, whose ACK waits for it."""
        with self.lock:
            gen, server, resolve = self.gen, self.server, self.resolve
            if gen is None:
                return
            msgs = []
            peers = _peers(server) if server else set()
            if self.peers and not peers:
                msgs.append("%s disconnected" % "/".join(sorted(set(self.peers.values()))))
            if signal:
                # Connections are only announced before a pattern, whose ACK waits for
                # the toast; from the idle loop it could land on a measured patch.
                new = {p: _software(server, p) for p in sorted(peers - set(self.peers))}
                msgs += ["%s connected (%s)" % (sw, p) for p, sw in new.items()]
                self.peers = {p: self.peers.get(p) or new[p] for p in peers}
            else:
                self.peers = {p: sw for p, sw in self.peers.items() if p in peers}
            up = resolve is not None and resolve.sock is not None
            # a Resolve drop is told from the idle loop only when no client could be
            # measuring a patch the toast would cover; else at the next pattern
            if up != self.resolve_up and (signal or (not up and not peers)):
                if resolve is not None and up:
                    msgs.append("Resolve connected to %s:%d" % (resolve.host, resolve.port))
                else:
                    msgs.append("Resolve disconnected")
                self.resolve_up = up
            if signal and not (server and server.calman.replaying):
                msgs += gen.signal_changes()
            if msgs:
                self.notifier("; ".join(msgs))
            # the HDMI link is read by the idle loop; before a pattern (whose ACK waits
            # on this, under the generator's lock) the last reading is shown again
            if not signal:
                self.hdmi = hdmistate.summary()
            self.publish(self.hdmi)

    def onNotification(self, sender, method, data):
        if sender != ADDON_ID:
            return
        cmd = method.split(".")[-1]
        if cmd == "start" and not self.running:
            self.running = True
            self.start()
            where = self.address()
            if self.resolve:
                where = ", ".join(filter(None, (where, "Resolve %s:%d" % (
                    self.resolve.host, self.resolve.port))))
            self.notifier("Generator started" + (" on %s" % where if where else ""))
        elif cmd == "stop" and self.running:
            self.running = False
            gen = self.gen
            self.shutdown()
            if gen:
                gen.backend.stop()
            self.notifier("Generator stopped")
            self.publish()

    # settings only the manual-pattern screen reads: the screen saving them when it closes
    # must not restart the network generator and drop the calibration software
    MANUAL_ONLY = ("test_signal", "test_max_luma")

    def _settings_snapshot(self):
        reload_settings()
        return {k: setting(k) for k in _setting_ids() if k not in self.MANUAL_ONLY}

    def onSettingsChanged(self):
        # only noted here: run() restarts, so the restart never nests inside a callback
        if self.running and self._settings_snapshot() != getattr(self, "settings_used", None):
            self.restart_due = True

    def tick(self):
        """One pass of the service loop: a restart the settings asked for, then the status."""
        if self.restart_due:
            self.restart_due = False
            if self.running:
                log("settings changed, restarting")
                self.shutdown()
                self.start()
        if self.running:
            self.check(signal=False)

    def run(self):
        log("loaded, generator off")
        while not self.waitForAbort(0.5):
            self.tick()
        self.running = False
        self.shutdown()
        self.window.clearProperty(STATE_PROP)
        self.window.clearProperty(STATUS_PROP)
