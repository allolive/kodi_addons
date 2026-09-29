"""Kodi-side smoke test with mocked xbmc modules and a fake Amlogic sysfs.

Also: the service is idle until NotifyAll(...,start), publishes its state in Home
window properties, toasts only real changes (a config burst makes one toast) and
never ACKs a pattern while a toast is on screen; default.py's menu drives it.

The fake player fires onPlayBackStarted/onAVStarted from a thread after a delay,
flips hdmi_hdr_status to the clip's signal and changes the display mode when
the resolution changes, like the box does. Checks the ACK waits for all of it.
"""
import json, os, socket, sys, tempfile, threading, time, types

PORT = int(os.environ.get("PGPORT", "22100")) + 2
ROOT = tempfile.mkdtemp()
SYS = os.path.join(ROOT, "sys") + "/"
os.makedirs(SYS)
AV_DELAY, FLIP_DELAY, MUTE_FRAMES, REFRESH_DELAY = 0.3, 0.2, 5, 0.5


def wsys(name, value):
    with open(SYS + name, "w") as f:
        f.write(value + "\n")


wsys("hdmi_hdr_status", "SDR")
wsys("hdr_mute_frame", str(MUTE_FRAMES))
wsys("mode", "2160p60hz")

events = []                     # (time, what)
toasts = []                     # (time, message)
PROPS = {}
builtins = []
st = {"playing": None, "player": None}

xbmc = types.ModuleType("xbmc")
xbmc.LOGERROR, xbmc.LOGINFO = 4, 1
xbmc.log = lambda m, l=1: None


class Monitor:
    def waitForAbort(self, t=None):
        if t is None:
            return True
        time.sleep(t)
        return False


class Player:
    def __init__(self):
        st["player"] = self

    def isPlayingVideo(self):
        return st["playing"] is not None

    def getPlayingFile(self):
        return st["playing"]

    def play(self, path, item=None):
        events.append((time.time(), "play " + os.path.basename(path)))

        def run():
            time.sleep(0.05)
            if "fail" in path:
                self.onPlayBackError()
                return
            st["playing"] = path
            self.onPlayBackStarted()
            # Measured on the box: when the HDR mode does not change, the infoframe
            # blip is over ~150 ms before onAVStarted; on SDR -> HDR the transmitter
            # switches ~0.4 s after onAVStarted.
            if st.get("next_hdr") == "from clip":     # the signal the clip itself carries
                data = open(path, "rb").read()
                st["clip_hdr"] = ("DolbyVision-Std" if b"\x7c\x01\x19" in data else
                                  "HDR10-GAMMA_HLG" if b"\x55\xba\x81\x12" in data else
                                  "HDR10-GAMMA_ST2084" if b"\x55\xba\x81\x10" in data else "SDR")
            want = st.get("clip_hdr") if st.get("next_hdr") == "from clip" else st.get("next_hdr", "SDR")
            same = open(SYS + "hdmi_hdr_status").read().strip() == want
            time.sleep(AV_DELAY)
            if not same:
                self.onAVStarted()
                events.append((time.time(), "av"))
                time.sleep(FLIP_DELAY)
            mode = st.get("next_mode")
            if mode:
                wsys("mode", mode)
            wsys("hdmi_hdr_status", want)
            events.append((time.time(), "flip"))
            if same:
                time.sleep(0.15)
                self.onAVStarted()
                events.append((time.time(), "av"))
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        st["playing"] = None
        self.onPlayBackStopped()


def executebuiltin(c):
    if c.startswith("NotifyAll("):
        builtins.append(c)
        sender, msg = c[len("NotifyAll("):-1].split(",")
        svc.onNotification(sender, "Other." + msg, "")


xbmc.Monitor, xbmc.Player = Monitor, Player
SKIN_EXTRA = 0.5               # open/close animation and a scrolling text keep a toast up longer


def getCondVisibility(c):
    if c == "Window.IsVisible(notification)":
        return time.time() < st.get("toast_until", 0)
    if c == "System.HasVisibleModalDialog":     # a dialog over the video (0.4.8 check)
        return time.time() < st.get("dialog_until", 0)
    return c != "Window.IsActive(addonsettings)"


xbmc.getCondVisibility = getCondVisibility
xbmc.executebuiltin = executebuiltin
xbmc.getInfoLabel = lambda l: ("192.168.1.94" if l == "Network.IPAddress"
                               else "3840x2160 @ 23.98Hz - Full Screen")
xbmc.executeJSONRPC = lambda q: json.dumps({"result": {"value": int(REFRESH_DELAY * 10)}})
xbmcaddon = types.ModuleType("xbmcaddon")
SETTINGS = {"upgci_port": str(PORT), "rpc_port": str(PORT + 1), "discovery": "false",
            "settle_frames": "2",
            # port 85, UDP 1977 and 20123 are fixed: t_classic.py covers them on free ports
            "classic_enabled": "false", "devicecontrol_discovery": "false",
            "lightspace_enabled": "false"}


class Addon:
    def __init__(self, i=None):
        pass

    def getSetting(self, k):
        return SETTINGS.get(k, "")

    def setSetting(self, k, v):
        SETTINGS[k] = v

    def getSettingInt(self, k):
        return int(SETTINGS.get(k) or 0)

    def setSettingInt(self, k, v):
        SETTINGS[k] = str(v)

    def getAddonInfo(self, k):
        return "0.1.0"

    def openSettings(self):
        OPENED.append(1)


OPENED = []


xbmcaddon.Addon = Addon
xbmcgui = types.ModuleType("xbmcgui")
# Kodi's action ids, as xbmcgui defines them
xbmcgui.ACTION_MOVE_LEFT, xbmcgui.ACTION_MOVE_RIGHT, xbmcgui.ACTION_SELECT_ITEM = 1, 2, 7
xbmcgui.ACTION_PARENT_DIR, xbmcgui.ACTION_PREVIOUS_MENU, xbmcgui.ACTION_STOP = 9, 10, 13
xbmcgui.ACTION_NAV_BACK, xbmcgui.ACTION_CONTEXT_MENU = 92, 117


class ListItem:
    def __init__(self, **k):
        pass

    def getVideoInfoTag(self):
        return types.SimpleNamespace(setTitle=lambda t: None)


class Dialog:
    script = []                 # select() answers, in order
    shown = []                  # the main menu lists

    def notification(self, heading, msg, icon=None, ms=5000, sound=True):
        now = time.time()
        toasts.append((now, msg))
        st["toast_until"] = max(st.get("toast_until", 0), now) + ms / 1000.0 + SKIN_EXTRA

    def select(self, heading, items, preselect=-1, **k):
        if heading == "Pattern Generator":
            Dialog.shown.append(list(items))
        return Dialog.script.pop(0)

    def input(self, heading, default=""):
        return default


class DialogProgressBG:
    def create(self, *a):
        pass

    def close(self):
        pass


class Window:
    def __init__(self, wid):
        assert wid == 10000

    def setProperty(self, k, v):
        PROPS[k] = v

    def getProperty(self, k):
        return PROPS.get(k, "")

    def clearProperty(self, k):
        PROPS.pop(k, None)


xbmcgui.ListItem, xbmcgui.Dialog, xbmcgui.Window = ListItem, Dialog, Window
xbmcgui.DialogProgressBG = DialogProgressBG
KEYS, SEEN = [], []
CLICKS, LABELS = [], []


class Control:
    def setLabel(self, t):
        LABELS.append(t)

    def setText(self, t):
        LABELS.append(t)


class WindowXMLDialog:
    """Runs the scripted button presses in CLICKS, one window per list."""
    def __init__(self, *a, **k):
        pass

    def getControl(self, i):
        return Control()

    def doModal(self):
        self.closed = False
        self.onInit()
        for c in CLICKS.pop(0):
            self.onClick(c)
        assert self.closed

    def close(self):
        self.closed = True


xbmcgui.WindowXMLDialog = WindowXMLDialog


class WindowDialog:
    def addControl(self, c):
        pass

    def doModal(self):
        SEEN.append(st["playing"])
        self.result = KEYS.pop(0)

    def close(self):
        pass


class ControlLabel:
    def __init__(self, *a, **k):
        pass

    def setVisible(self, v):
        pass


xbmcgui.WindowDialog, xbmcgui.ControlLabel = WindowDialog, ControlLabel
xbmcgui.NOTIFICATION_INFO = xbmcgui.NOTIFICATION_ERROR = "info"
xbmcvfs = types.ModuleType("xbmcvfs")
xbmcvfs.translatePath = lambda p: os.path.join(ROOT, "cache")
for m in (xbmc, xbmcaddon, xbmcgui, xbmcvfs):
    sys.modules[m.__name__] = m
import testlib  # noqa: E402,F401  the add-on on sys.path
from patterngen import kodi  # noqa: E402

kodi.hdmistate.HDMITX, kodi.hdmistate.DISPLAY_MODE = SYS, SYS + "mode"
kodi.Notifier.TIME, kodi.Notifier.FADE = 0.6, 0.2
TOAST = 0.6 + SKIN_EXTRA         # what the mock skin shows, longer than Notifier's 0.8
AID = kodi.ADDON_ID
fails = []


def listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def state():
    """(state, status) without the status's HDMI part, which t_hdmi and t_status check."""
    status = PROPS.get(AID + ".status")
    return PROPS.get(AID + ".state"), status and status.split(" | HDMI ")[0]


def new_toasts(n):
    return [m for _, m in toasts[n:]]


# 0. loaded idle: nothing listening, state published; start/stop over NotifyAll
svc = kodi.Service()
if listening(PORT) or listening(PORT + 1) or state() != ("stopped", "Stopped"):
    fails.append("0: not idle at load: %r" % (state(),))
svc.onNotification("xbmc", "Other.start", "")
svc.onNotification(AID, "Other.start", "")
svc.onNotification(AID, "Other.start", "")
addr = "192.168.1.94:%d/%d" % (PORT, PORT + 1)
if not (listening(PORT) and listening(PORT + 1)):
    fails.append("0: start did not open %d/%d" % (PORT, PORT + 1))
if state() != ("running", "Listening on " + addr):
    fails.append("0: running state %r" % (state(),))
if new_toasts(0) != ["Generator started on " + addr]:
    fails.append("0: start toasts %r" % new_toasts(0))
c = socket.create_connection(("127.0.0.1", PORT))
c.settimeout(60)


def cmd(x):
    c.sendall(b"\x02" + x.encode() + b"\x03")
    r = c.recv(4096)
    events.append((time.time(), "ack " + x[:12]))
    return r, time.time()


def last(what):
    return [t for t, e in events if e == what][-1]


# 1. HDR10 from the SDR menu at a new refresh rate: av, flip, HDR mute, refresh delay, settle
st.update(next_hdr="HDR10-GAMMA_ST2084", next_mode="2160p24hz")
# each restart shows PatternStart (black, command.pm:642): CONF_FORMAT's is the first clip,
# the one the fake display switches on, and its ACK waits for it; the connection is
# announced there, the signal (a change no Calman pattern showed yet) at the next pattern
r, t_ack = cmd("CONF_FORMAT:Resolution=3840x2160,Refresh=23.976")
frame = 1001 / 24000.0
need = max(last("flip") + MUTE_FRAMES * frame, last("av") + REFRESH_DELAY) + 2 * frame
if r != b"\x06" or t_ack < need - 0.02:
    fails.append("1: ack %r at +%.3f, needed +%.3f" % (r, t_ack - last("av"), need - last("av")))
cmd("DSMD:HDR10")
r, t_ack = cmd("RGB_S:0512,0512,0512,010")
n_toasts = len(toasts)
if new_toasts(1) != ["Calman connected (127.0.0.1)", "Signal: HDR10"]:
    fails.append("1: toasts %r" % new_toasts(1))
elif t_ack < toasts[-1][0] + TOAST - 0.02:
    fails.append("1: ack %.3f s after the toast, it shows %.1f s" % (t_ack - toasts[-1][0], TOAST))
# 2. next HDR10 pattern, same mode: no mute, no refresh delay, only av + settle
st.update(next_mode=None)
r, t_ack = cmd("RGB_S:0600,0600,0600,010")
if r != b"\x06" or t_ack < last("av") + 2 * frame - 0.02 or t_ack > last("av") + 2 * frame + 0.15:
    fails.append("2: ack at +%.3f after av" % (t_ack - last("av")))
# 3. a clip that fails to play is answered NAK, quickly (0.4.8: never ACK a pattern that
# is not on screen)
import patterngen.render as render  # noqa: E402
real = render.render


def failing(*a, **k):
    p = real(*a, **k)
    q = p.replace("pattern-", "pattern-fail-")
    os.replace(p, q)
    return q


render.render = failing
t0 = time.time()
r, t_ack = cmd("RGB_S:0700,0700,0700,010")
if r != b"\x15" or t_ack - t0 > 2:
    fails.append("3: failing clip answered %r after %.2fs, want NAK" % (r, t_ack - t0))
render.render = real
# 4. clip ends naturally while current: restarted
st.update(next_hdr="HDR10-GAMMA_ST2084")
cmd("RGB_S:0800,0800,0800,010")
cur = st["playing"]
n = len([e for e in events if e[1].startswith("play")])
st["playing"] = None
st["player"].onPlayBackEnded()
time.sleep(0.2)
if len([e for e in events if e[1].startswith("play")]) != n + 1:
    fails.append("4: clip not restarted after end")
# 6. a config burst (each command redraws the last pattern) makes one toast, at the next
# pattern, and that pattern's ACK waits until the toast is gone
st.update(next_hdr="DolbyVision-Std")
cmd("DSMD:DV")
cmd("CONF_DV:ABSOLUTE")
cmd("MAXL:1000")
if len(toasts) != n_toasts:
    fails.append("6: toast during the burst %r" % new_toasts(n_toasts))
r, t_ack = cmd("RGB_S:0500,0500,0500,010")
want = ["Signal: Dolby Vision; Dolby Vision mapping: Absolute"]
if new_toasts(n_toasts) != want:
    fails.append("6: toasts %r" % new_toasts(n_toasts))
elif r != b"\x06" or t_ack < toasts[-1][0] + TOAST - 0.02:
    fails.append("6: ack %.3f s after the toast" % (t_ack - toasts[-1][0]))
cmd("CONF_DV:PERCEPTUAL")
cmd("CONF_DV:ABSOLUTE")                  # back where it was: no change, no toast
n_toasts = len(toasts)
r, t_ack = cmd("RGB_S:0510,0510,0510,010")
if new_toasts(n_toasts) or t_ack > last("av") + 2 * frame + 0.3:
    fails.append("6: unchanged signal: toasts %r, ack %.3f after av"
                 % (new_toasts(n_toasts), t_ack - last("av")))
cmd("CONF_DV:RELATIVE")
r, t_ack = cmd("RGB_S:0520,0520,0520,010")
if new_toasts(n_toasts) != ["Dolby Vision mapping: Relative"]:
    fails.append("6: mapping toasts %r" % new_toasts(n_toasts))
st.update(next_hdr="HDR10-GAMMA_ST2084")
cmd("DSMD:HDR10")
cmd("RGB_S:0800,0800,0800,010")
cur = st["playing"]
# 5. TERM closes the connection and keeps the pattern up (PGenerator)
c.sendall(b"\x02TERM\x03")
r = c.recv(10)
time.sleep(0.2)
if r != b"\x06" or c.recv(10) != b"" or st["playing"] != cur:
    fails.append("5: TERM reply %r, playing %r" % (r, st["playing"]))
n_toasts = len(toasts)
svc.check(signal=False)                 # the service's poll
svc.check(signal=False)
if new_toasts(n_toasts) != ["Calman disconnected"] or state()[1] != "Listening on " + addr:
    fails.append("5: disconnect toasts %r, state %r" % (new_toasts(n_toasts), state()))
# 7. a settings change restarts silently, on the service's next tick (never inside the
# callback); a Dolby Vision request is sent as Dolby Vision
SETTINGS["settle_frames"] = "3"
n_toasts = len(toasts)
svc.onSettingsChanged()
if not svc.restart_due or not listening(PORT):
    fails.append("7: the callback restarted, or did not ask for a restart")
svc.tick()
if (new_toasts(n_toasts) or svc.restart_due or state() != ("running", "Listening on " + addr)
        or not listening(PORT)):
    fails.append("7: restart toasts %r, state %r" % (new_toasts(n_toasts), state()))
c = socket.create_connection(("127.0.0.1", PORT))
c.settimeout(60)
st.update(next_hdr="DolbyVision-Std")
cmd("DSMD:DV")
cmd("RGB_S:0500,0500,0500,010")
if new_toasts(n_toasts) != ["Calman connected (127.0.0.1)",      # at DSMD:DV's PatternStart
                            "Signal: Dolby Vision"]:
    fails.append("7: toasts %r" % new_toasts(n_toasts))
RPU = b"\x7c\x01\x19"
if svc.gen.sig.mode != "dv" or RPU not in open(st["playing"], "rb").read():
    fails.append("7: DV request not sent as DV (%r)" % svc.gen.sig.mode)
n_toasts = len(toasts)
cmd("RGB_S:0510,0510,0510,010")
if RPU not in open(st["playing"], "rb").read() or new_toasts(n_toasts):
    fails.append("7: next DV pattern lacks its RPU, toasts %r" % new_toasts(n_toasts))
c.close()
# 8. stop closes the listeners and the pattern
n_toasts = len(toasts)
svc.onNotification(AID, "Other.stop", "")
svc.onNotification(AID, "Other.stop", "")
time.sleep(0.2)
if listening(PORT) or listening(PORT + 1) or state() != ("stopped", "Stopped"):
    fails.append("8: after stop: %r" % (state(),))
if new_toasts(n_toasts) != ["Generator stopped"] or st["playing"] is not None:
    fails.append("8: stop toasts %r, playing %r" % (new_toasts(n_toasts), st["playing"]))
svc.onSettingsChanged()
svc.tick()
if listening(PORT):
    fails.append("8: a settings change started a stopped generator")
# 9. notifications off: no toast, no ACK delay (once test 8's "Generator stopped" is gone)
time.sleep(max(0, st["toast_until"] - time.time()))
SETTINGS.update(notifications="false")
n_toasts = len(toasts)
svc.onNotification(AID, "Other.start", "")
c = socket.create_connection(("127.0.0.1", PORT))
c.settimeout(60)
st.update(next_hdr="DolbyVision-Std")
cmd("DSMD:DV")
r, t_ack = cmd("RGB_S:0500,0500,0500,010")
if new_toasts(n_toasts) or t_ack > last("av") + 2 * frame + 0.3:
    fails.append("9: toasts %r, ack %.3f after av" % (new_toasts(n_toasts), t_ack - last("av")))
# 9a. any toast on screen (Kodi's own too) holds the ACK back until it has gone
xbmcgui.Dialog().notification("Kodi", "Library updated", ms=600)
r, t_ack = cmd("RGB_S:0520,0520,0520,010")
if r != b"\x06" or t_ack < st["toast_until"] - 0.02:
    fails.append("9a: ack %.3f s before a visible toast closed" % (st["toast_until"] - t_ack))
# 9b. Stop while a clip is starting: the clip is stopped once it is up, no ACK wait
n_play = len([e for e in events if e[1].startswith("play")])
threading.Thread(target=lambda: c.sendall(b"RGB_S:0530,0530,0530,010"), daemon=True).start()
deadline = time.time() + 30
while len([e for e in events if e[1].startswith("play")]) == n_play and time.time() < deadline:
    time.sleep(0.005)
svc.onNotification(AID, "Other.stop", "")
time.sleep(AV_DELAY + FLIP_DELAY + 0.5)
if st["playing"] is not None:
    fails.append("9b: clip started after Stop still playing: %r" % st["playing"])
c.close()
SETTINGS["notifications"] = "true"
# 10. the add-on's window: Start, Stop, Settings (reopens the window), Close
import runpy  # noqa: E402
DEFAULT = kodi.__file__.replace("resources/lib/patterngen/kodi.py", "default.py")


def run_default(*args):
    sys.argv = [DEFAULT] + list(args)
    runpy.run_path(DEFAULT, run_name="__main__")


CLICKS[:] = [[10, 10, 12], [13]]
run_default()
if builtins != ["NotifyAll(%s,start)" % AID, "NotifyAll(%s,stop)" % AID]:
    fails.append("10: builtins %r" % builtins)
buttons = [l for l in LABELS if l.endswith(" generator")]
if len(OPENED) != 1 or CLICKS or buttons[:3] != ["Start generator", "Stop generator",
                                                 "Start generator"]:
    fails.append("10: settings opened %d times, buttons %r" % (len(OPENED), buttons))
if not any(l.endswith("  Running") for l in LABELS) or not any(
        l.startswith("Listening on ") for l in LABELS):
    fails.append("10: no status text")
if listening(PORT):
    fails.append("10: still listening after the Stop button")
# 11. manual test patterns: the signal and luminance picked here; Right steps, OK's menu
# changes the signal, Back ends it; the choice is saved once, when the screen closes
SETTINGS.update(test_signal="4", test_max_luma="4000")
st.update(next_hdr="from clip")
KEYS[:] = [("pattern", 1), ("menu", 0), ("exit", 0)]
Dialog.script = [0, 1, 2]   # first pattern; OK menu: Signal -> HLG (SDR, HDR10, HLG, DV)


CLICKS[:] = [[11], [13]]        # Show test pattern, then Close when back in the window
run_default()
data = [open(p, "rb").read() for p in SEEN]
if (len(data) != 3 or RPU not in data[0]
        or RPU not in data[1] or data[0] == data[1] or RPU in data[2] or KEYS or Dialog.script or CLICKS):
    fails.append("11: manual patterns not sent as picked (%d clips)" % len(data))
if (SETTINGS.get("test_max_luma"), SETTINGS.get("test_signal")) != ("4000", "3"):
    fails.append("11: signal/luminance not remembered: %r %r"
                 % (SETTINGS.get("test_max_luma"), SETTINGS.get("test_signal")))
# 12. start-up signal and luminance: no add-on luminance settings; the old HDR10+ value (2)
# is not turned into another mode, it falls back to the default
SETTINGS.update(default_mode="2", max_luma="500", max_cll="1", max_fall="1")
g = kodi.make_generator(backend=types.SimpleNamespace(play=lambda *a, **k: True, stop=lambda: 0))
if g.sig.mode != "sdr":
    fails.append("12: old HDR10+ start-up value gives %r" % g.sig.mode)
SETTINGS.update(default_mode="1")
g = kodi.make_generator(backend=types.SimpleNamespace(play=lambda *a, **k: True, stop=lambda: 0))
if g.sig.mode != "hdr10":
    fails.append("12: HDR10 start-up gives %r" % g.sig.mode)
if (g.sig.max_luma, g.sig.min_luma, g.sig.max_cll, g.sig.max_fall) != (1000, 0.005, 1000, 400):
    fails.append("12: luminance %r" % ((g.sig.max_luma, g.sig.max_cll, g.sig.max_fall),))
# 13. settings.xml: removed settings gone, every label has a string, every read setting exists
import re  # noqa: E402
base = kodi.__file__.replace("lib/patterngen/kodi.py", "")
xml = open(base + "settings.xml").read()
po = set(re.findall(r'msgctxt "#(\d+)"', open(base + "language/resource.language.en_gb/strings.po").read()))
ids = set(re.findall(r'<setting id="([^"]+)"', xml))
gone = ids & {"hdr10_as_hdr10plus", "dv_map_mode", "max_luma", "max_cll", "max_fall",
              "dv_output", "hdr10_output"}
labels = set(re.findall(r'(?:label|help)="(\d+)"', xml)) - po
read = set(re.findall(r'setting(?:_int|_bool)?\("(\w+)"', open(kodi.__file__).read()))
read |= set(re.findall(r'etSetting(?:Int)?\("(\w+)"',
                      open(base.replace("resources/", "") + "default.py").read()))
if gone or labels or read - ids:
    fails.append("13: removed %r, labels without string %r, unknown settings read %r"
                 % (gone, labels, read - ids))
svc.shutdown()
t00 = events[0][0]
if fails: print(*["%.3f %s" % (t - t00, e) for t, e in events], sep="\n")
if fails: print(*["%.3f toast %s" % (t - t00, m) for t, m in toasts], sep="\n")
print("\n".join(fails) if fails else "kodi backend OK")
sys.exit(1 if fails else 0)
