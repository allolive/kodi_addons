"""Byte-level parity of the add-on's Calman server with PGenerator 2.12.1 (daemon.pm),
on the "Portrait Displays G1" path (RPC TCP 2101 + UDP discovery) and on UPGCI 2100.

Runs the real upgci.Server + generator.Generator with a recording backend on
ports from PGPORT; the clips are rendered for real. Expectations are PGenerator's,
worked out from daemon.pm / pattern.pm / webui.pm / ofApp.cpp, not from the add-on.
Exit status 1 on any mismatch.
"""
import math
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import types

import testlib
from testlib import FAILS, SCENES, Ports, capture_scenes, check, near, pq
from patterngen import generator, upgci  # noqa: E402

upgci.INFO_PERIOD = 0       # no device_info cache here (t_parity2.py tests it)

CACHE = tempfile.mkdtemp(prefix="pg_g1_")
STX, ETX, ACK = b"\x02", b"\x03", b"\x06"
_PORTS = Ports(20)


# -- recording backend + scene capture ------------------------------------------
class Backend:
    def __init__(self):
        self.plays = []
        self.stops = 0

    def play(self, path, mode=None, fps=None):
        time.sleep(0.05)                       # the ACK must wait for this
        self.plays.append((time.time(), path, mode, fps))
        return True

    def stop(self):
        self.stops += 1


capture_scenes()


def free_port(kind=socket.SOCK_STREAM):
    return _PORTS.free(kind)


class Rig:
    """Generator + Server at a given display format, like a freshly started service."""

    def __init__(self, fmt=(1920, 1080, 24000, 1001), discovery=False, rpc=True, info=None):
        self.backend = Backend()
        self.logs = []
        self.gen = generator.Generator(self.backend, CACHE, self.log, duration=2)
        self.gen.set_default_format(*fmt)
        self.info = info or {"name": "CoreELEC", "serial": "c0ffee12", "firmware": "2.12.1"}
        for _ in range(20):
            self.port, self.rpc_port = free_port(), (free_port() if rpc else 0)
            self.disc = free_port(socket.SOCK_DGRAM)
            self.reply_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.reply_sock.bind(("127.0.0.1", 0))
            self.reply_sock.settimeout(0.5)
            self.srv = upgci.Server(self.gen, self.info, self.log, port=self.port,
                                    rpc_port=self.rpc_port, discovery=discovery,
                                    discovery_port=self.disc,
                                    discovery_reply_port=self.reply_sock.getsockname()[1])
            self.srv.start()
            if len(self.srv.sockets) == (2 if rpc else 1):
                break
            self.srv.stop()
        self.cal = self.srv.calman
        time.sleep(0.05)

    def log(self, msg, error=False, debug=False):
        if error:
            self.logs.append(msg)

    def conn(self, rpc=True):
        c = socket.create_connection(("127.0.0.1", self.rpc_port if rpc else self.port))
        c.settimeout(5)
        return c

    def close(self):
        self.srv.stop()
        self.reply_sock.close()
        if self.logs:
            FAILS.append("add-on logged errors: %s" % self.logs[:3])


def xfer(c, data, idle=0.03):
    """Send, then read everything that arrives until the line is idle."""
    return testlib.xfer(c, data, idle, timeout=5)


def closed(c):
    c.settimeout(0.5)       # a close follows the reply at once; only an open one waits
    try:
        return c.recv(16) == b""
    except (socket.timeout, OSError):
        return False


def cmd(c, text):
    return xfer(c, STX + text.encode("latin-1") + ETX)


def last_scene():
    return SCENES[-1][0] if SCENES else None


def lit(scene):
    """The scene's non-black rectangles (PatternStart draws a black 640x360 box on black)."""
    return [r for r in scene.rects if (r[4].r, r[4].g, r[4].b) != (0.0, 0.0, 0.0)]


def grey(colour):
    return round(colour.r, 9) if colour.r == colour.g == colour.b else (colour.r, colour.g, colour.b)


def rgb(colour):
    return tuple(round(v, 9) for v in (colour.r, colour.g, colour.b))


def full(code, mx):
    return round(code / float(mx), 9)


def lim8(b):
    """8-bit code read limited, footroom/headroom kept (README: no limited clamp)."""
    return round((b - 16) / 219.0, 9)


def lim10(c):
    return round((c - 64) / 876.0, 9)


def r10full(e):
    """E' rounded to a whole 10-bit full-range code (README: 10-bit, not max_bpc, rounding)."""
    return round(math.floor(1023 * e + 0.5) / 1023.0, 9)


def window(scene):
    """(x, y, w, h, colour) of the single rectangle, or None for a full field."""
    return scene.rects[0] if scene.rects else None


def pg_window(W, H, pct):
    """PGenerator's window: int(sqrt*1920) scaled to the screen, centred."""
    s = math.sqrt(pct / 100.0)
    w = int(int(s * 1920) * (W / 1920.0) + 0.5)
    h = int(int(s * 1080) * (H / 1080.0) + 0.5)
    return int((W - w) / 2), int((H - h) / 2), w, h


def check_window(name, scene, pct, fg, bg):
    W, H = scene.width, scene.height
    r = window(scene)
    if pct >= 100:
        check(name + " full field", r, None)
        check(name + " colour", rgb(scene.background), fg)
        return
    if r is None:
        FAILS.append(name + ": full field, want a %s%% window" % pct)
        return
    px, py, pw, ph = pg_window(W, H, pct)
    for what, g, want in (("x", r[0], px), ("y", r[1], py), ("w", r[2], pw), ("h", r[3], ph)):
        if abs(g - want) > 2:        # the add-on rounds to even for 4:2:0 (intentional)
            FAILS.append("%s window %s %d, PGenerator %d" % (name, what, g, want))
    check(name + " fg", rgb(r[4]), fg)
    check(name + " bg", rgb(scene.background), bg)


def g3(v):
    return (v, v, v)


def dvl(code, mx):
    """PGenerator's standard-DV tunnel: the code shifted to 12 bits, E' = (c12 - 256)/3504."""
    shift = {255: 4, 1023: 2, 4095: 0}[int(mx)]
    return round(((int(code) << shift) - 256) / 3504.0, 9)


def Calman_mode(conf):
    return upgci.Calman._mode(conf)


# ==============================================================================
# 1. discovery (UDP, Calman -> 3529, answer to the caller's 3530)
# ==============================================================================
def t_discovery():
    rig = Rig(discovery=True)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def ask(data):
        s.sendto(data, ("127.0.0.1", rig.disc))
        try:
            d, addr = rig.reply_sock.recvfrom(100)
            return d, addr[1]
        except socket.timeout:
            return None, None
    port = rig.rpc_port.to_bytes(2, "little")
    d, src = ask(b"x")
    check("discovery reply", d, b"CoreELEC" + b"\0" * 16 + port + port)
    check("discovery source port", src, rig.disc)
    check("discovery empty datagram", ask(b"")[0], None)
    check("discovery '0' datagram", ask(b"0")[0], None)
    check("discovery '00' datagram", ask(b"00")[0] is not None, True)
    rig.close()
    rig = Rig(discovery=True, rpc=False)
    s.sendto(b"x", ("127.0.0.1", rig.disc))
    try:
        d = rig.reply_sock.recvfrom(100)[0]
    except socket.timeout:
        d = None
    check("no RPC port: no discovery answer", d, None)
    rig.close()
    s.close()


def t_identity():
    """kodi.py: SN from /proc/cpuinfo, FIRMWARE 2.12.1, discovery name from /etc/hostname."""
    mods = {}
    for name in ("xbmc", "xbmcaddon", "xbmcgui", "xbmcvfs"):
        mods[name] = types.ModuleType(name)
    mods["xbmc"].Player = mods["xbmc"].Monitor = object
    mods["xbmc"].LOGERROR = mods["xbmc"].LOGINFO = 0
    settings = {}

    class Addon:
        def __init__(self, *a):
            pass

        def getSetting(self, k):
            return settings.get(k, "")

        def getAddonInfo(self, k):
            return "0"
    mods["xbmcaddon"].Addon = Addon
    saved = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    try:
        from patterngen import kodi
        files = {}
        real_open = open

        def fake_open(path, *a, **k):
            if path in files:
                if files[path] is None:
                    raise OSError(path)
                import io
                data = files[path]
                return io.BytesIO(data if isinstance(data, bytes) else data.encode("utf-8")) \
                    if "b" in (a[0] if a else k.get("mode", "")) else io.StringIO(data)
            return real_open(path, *a, **k)
        kodi.open = fake_open
        files["/proc/cpuinfo"] = ("processor\t: 0\nHardware\t: Amlogic\n"
                                  "Serial\t\t: 290b1234 5678abcd\n")
        check("SN from cpuinfo", kodi.cpu_serial(), "290b1234")
        files["/proc/cpuinfo"] = "processor\t: 0\n"
        check("SN fallback", kodi.cpu_serial(), "PGenerator+")
        # awk '{print $3}' splits on blanks only, then s/\s+//g; bytes pass through
        for info, want in ((b"Serial : a\x0bb\n", "ab"), (b"Serial\t: \xff\xfe\n", "\xff\xfe"),
                           (b"Serial:abc\n", "PGenerator+")):
            files["/proc/cpuinfo"] = info
            check("SN %r" % info, kodi.cpu_serial(), want)
        for host, want in (("CoreELEC\n", "CoreELEC"), ("  pgenerator \n", "PGenerator+"),
                           ("\n", "PGenerator+"), ("a" * 30, "a" * 24)):
            files["/etc/hostname"] = host
            check("discovery name %r" % host, kodi.discovery_name(), want)
        # discovery.pm: read_from_file bytes ("" if missing), ASCII \s trim, 24 bytes
        for host, want in ((None, "PGenerator+"), (b"box\xc2\xa0", b"box\xc2\xa0"),
                           (b"box\x1c\n", b"box\x1c"), (b"\xff\xfebox", b"\xff\xfebox"),
                           (b"PGENERATOR\x0b", "PGenerator+"), ("é" * 13, "é" * 12)):
            files["/etc/hostname"] = host
            try:
                got = kodi.discovery_name()
                got = got.encode("utf-8", "surrogateescape") if isinstance(want, bytes) else got
            except Exception as exc:
                got = repr(exc)
            check("discovery name bytes %r" % (host,), got, want)
        settings["device_name"] = "Living room"
        check("device_name override", kodi.discovery_name(), "Living room")
        check("FIRMWARE", upgci.VERSION, "2.12.1")
        del kodi.open
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# ==============================================================================
# 2. queries
# ==============================================================================
CAPS = b"HDR,DOLBYVISION,CONF_FORMAT,CONF_HDR,SIZE,10_SIZE,11_APL,CommandRGB,BITDEPTH,COLORSPACE,RANGE"
STATUS = (b"STATUS,CONF_FORMAT,CONF_HDR,CONF_LEVEL,CONF_DV,HDR_ENABLE,IMAGE,PUSH,RGB_S,RGB_B,"
          b"RGB_A,CommandRGB,10_SIZE,11_APL,SPECIALTY,UPDATE,YCC_A,YCC_B,YCC_S")


def t_queries():
    rig = Rig()
    c = rig.conn()
    check("RPC SN", cmd(c, "SN"), b"c0ffee12")
    check("RPC CAP", cmd(c, "CAP"), CAPS)
    check("RPC FIRMWARE", cmd(c, "FIRMWARE"), b"2.12.1")
    check("RPC STATUS", cmd(c, "STATUS"), STATUS)
    check("RPC GET_SETTINGS fresh 23.976", cmd(c, "GET_SETTINGS"),
          b"Resolution=1920x1080,Refresh=23,1_FORMAT=RGB 8-bit,Range=Full,Bits=8,Dolby=Off")
    check("RPC  SN  (trimmed)", cmd(c, " SN "), b"c0ffee12")
    for k in ("IS_ALIVE", "ISALIVE", "ENABLE PATTERNS", "DISABLEPATTERNS", "UPDATE", "sn", "cap",
              "FOO", "FOO:bar", "HDR_WHITEPOINT:0", "UPDATE:1"):
        check("RPC %s" % k, cmd(c, k), ACK)
    check("no pattern drawn by queries", len(rig.backend.plays), 0)
    c2 = rig.conn(rpc=False)
    check("2100 CAP", cmd(c2, "CAP"), CAPS + ETX)
    check("2100 FIRMWARE", cmd(c2, "FIRMWARE"), b"2.12.1" + ETX)
    check("2100 SN", cmd(c2, "SN"), b"c0ffee12" + ETX)
    check("2100 IS_ALIVE", cmd(c2, "IS_ALIVE"), ACK)
    c.close()
    c2.close()
    rig.close()


# ==============================================================================
# 3. patterns, window/background/APL state, own-depth codes
# ==============================================================================
def t_patterns():
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    n = len(rig.backend.plays)
    t0 = time.time()
    check("RGB_S reply", cmd(c, "RGB_S:0512,0512,0512,010"), ACK)
    if len(rig.backend.plays) != n + 1 or rig.backend.plays[-1][0] > time.time() or \
            rig.backend.plays[-1][0] < t0:
        FAILS.append("RGB_S: ACK did not follow the pattern")
    # SDR, 8 bpc, full range: read at the code's own depth, 512/1023 (README deviation)
    check_window("RGB_S 512 10%", last_scene(), 10, g3(full(512, 1023)), g3(0.0))
    check("calman_win_size", rig.cal.win, 10)
    check("last pattern", rig.cal.last, ("RGB", "RGB_S", "0512,0512,0512,010"))
    cmd(c, "RGB_S:0064,0064,0064,100")
    check_window("RGB_S 64 full field (full range)", last_scene(), 100, g3(full(64, 1023)), None)
    # RGB_B: sticky background, PatternDynamic 640x360 centred, window size untouched
    cmd(c, "RGB_B:0512,0512,0512,0100")
    sc = last_scene()
    check("RGB_B rect", window(sc)[:4], (640, 360, 640, 360))
    check("RGB_B fg", rgb(window(sc)[4]), g3(full(512, 1023)))
    check("RGB_B bg", rgb(sc.background), g3(full(100, 1023)))
    check("RGB_B calman_bg", rig.cal.bg, upgci.Code(g3(100)))
    check("RGB_B leaves calman_win_size", rig.cal.win, 100)
    cmd(c, "RGB_S:0940,0940,0940,010")
    check_window("RGB_S after RGB_B", last_scene(), 10, g3(full(940, 1023)), g3(full(100, 1023)))
    cmd(c, "RGB_A:0512,0512,0512,0000,0000,0000,025")
    check_window("RGB_A 7 values", last_scene(), 25, g3(full(512, 1023)), g3(0.0))
    check("RGB_A win", rig.cal.win, 25)
    check("RGB_A keeps calman_bg", rig.cal.bg, upgci.Code(g3(100)))
    cmd(c, "RGB_A:0256,0512,0768,050")
    check_window("RGB_A 4 values", last_scene(), 50,
                 (full(256, 1023), full(512, 1023), full(768, 1023)), g3(full(100, 1023)))
    # RGB_<other>: PatternDynamic with calman_bg (APL off)
    cmd(c, "RGB_X:0512,0512,0512")
    check("RGB_X rect", window(last_scene())[:4], (640, 360, 640, 360))
    check("RGB_X bg", rgb(last_scene().background), g3(full(100, 1023)))
    check("RGB_BX is RGB_B", (cmd(c, "RGB_BX:0000,0000,0000,0200"), rig.cal.bg),
          (ACK, upgci.Code(g3(200))))
    # 10_SIZE: replayed before the ACK
    cmd(c, "RGB_S:0512,0512,0512")
    n = len(rig.backend.plays)
    check("10_SIZE:-5", (cmd(c, "10_SIZE:-5"), rig.cal.win), (ACK, 10))
    check("10_SIZE replay before ACK", len(rig.backend.plays), n + 1)
    check_window("replayed at 10", last_scene(), 10, g3(full(512, 1023)), g3(full(200, 1023)))
    cmd(c, "10_SIZE:250")
    check("10_SIZE:250", rig.cal.win, 100)
    cmd(c, "10_SIZE:33x")
    check("10_SIZE:33x (Perl int)", rig.cal.win, 33)
    cmd(c, "10_SIZE:10")
    # APL + CommandRGB (8 bpc, full)
    cmd(c, "11_APL:30")
    check("11_APL", (rig.cal.apl, rig.cal.apl_enabled), (30.0, True))
    # APL grey rounded to a whole 10-bit code (README): (30-10)/0.9 = 22.22% -> 227/1023
    cmd(c, "CommandRGB:1023,1023,1023,1,999")
    # 0.4.14: the surround is PGenerator's whole code at the 8-bit target depth (t_apl
    # checks the arithmetic against its Perl)
    check_window("CommandRGB 999", last_scene(), 10, g3(1.0), g3(full(57, 255)))
    cmd(c, "CommandRGB:1023,1023,1023,1,150")
    check_window("CommandRGB 150", last_scene(), 10, g3(1.0), g3(full(113, 255)))
    cmd(c, "CommandRGB:128,128,128,0,50")
    check_window("CommandRGB 50%", last_scene(), 50, g3(full(128, 255)), g3(0.0))
    cmd(c, "CommandRGB:128,128,128,0,0")
    check_window("CommandRGB legacy token", last_scene(), 10, g3(full(128, 255)), g3(0.0))
    cmd(c, "11_APL:abc")
    check("11_APL:abc (Perl numeric)", rig.cal.apl, 0.0)
    cmd(c, "11_APL:25x")
    check("11_APL:25x", rig.cal.apl, 25.0)
    # RGB_<other> with APL on: surround from the APL
    cmd(c, "RGB_Q:1023,1023,1023")
    # fg 100%, win 10, APL 25: (25-10)/0.9 = 16.67% -> PGenerator's 8-bit 43 (0.4.14)
    check("RGB_Q APL surround", rgb(last_scene().background), g3(full(43, 255)))
    # BITD:10 replays; 8-bit CommandRGB 128 stays 128/255 (own depth, README)
    n = len(rig.backend.plays)
    cmd(c, "BITD:10")
    check("BITD replays (changed)", len(rig.backend.plays), n + 1)
    cmd(c, "CommandRGB:128,128,128,0,100")
    check_window("CommandRGB 8-bit at 10 bpc", last_scene(), 100, g3(full(128, 255)), None)
    cmd(c, "RGB_S:0513,0513,0513,100")
    check_window("RGB_S at 10 bpc", last_scene(), 100, g3(full(513, 1023)), None)
    n = len(rig.backend.plays)
    cmd(c, "BITD:10")
    check("BITD unchanged: no replay", len(rig.backend.plays), n)
    cmd(c, "BITD:8")
    cmd(c, "RGB_S:0513,0513,0513,100")
    check_window("RGB_S 513 at 8 bpc", last_scene(), 100, g3(full(513, 1023)), None)
    cmd(c, "RGB_S:1023,1023,1023,100")
    check_window("RGB_S 1023 at 8 bpc", last_scene(), 100, g3(1.0), None)
    # YCC_*: drawn like RGB_ (README: PGenerator only ACKs), full range read here
    n = len(rig.backend.plays)
    check("YCC_S reply", cmd(c, "YCC_S:0512,0512,0512,010"), ACK)
    check("YCC_S drawn and remembered", (len(rig.backend.plays), rig.cal.last),
          (n + 1, ("YCC", "YCC_S", "0512,0512,0512,010")))
    check_window("YCC_S grey on calman_bg", last_scene(), 10, g3(full(512, 1023)), g3(full(200, 1023)))
    cmd(c, "YCC_B:0600,0400,0700,0100")
    check("YCC_B calman_bg", (rig.cal.bg, rig.cal.apl_enabled),
          (upgci.Code((100, 512, 512), ycc=True), False))
    check("YCC_B rect", window(last_scene())[:4], (640, 360, 640, 360))
    c.close()
    rig.close()


# ==============================================================================
# 4. framing
# ==============================================================================
def t_framing():
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    n = len(rig.backend.plays)
    r = xfer(c, b"\x02RGB_S:0100,0100,0100,010\x03\x02RGB_S:0200,0200,0200,010\x03")
    check("coalesced RPC frames: one reply", r, ACK)
    check("coalesced RPC frames: one draw", len(rig.backend.plays), n + 1)
    check_window("coalesced: first patch at int('010\\x02RGB_S:0200')", last_scene(), 10,
                 g3(full(100, 1023)), g3(0.0))
    # RPC: no reassembly
    check("RPC split 1", xfer(c, b"\x02RGB_S:0512,0512"), ACK)
    check_window("RPC split 1 draws R,G,0", last_scene(), 10,
                 (full(512, 1023), full(512, 1023), 0.0), g3(0.0))
    n = len(rig.backend.plays)
    check("RPC split 2", xfer(c, b",0512,010\x03"), ACK)
    check("RPC split 2 draws nothing", len(rig.backend.plays), n)
    check("RPC trailing CRLF: one reply", xfer(c, b"\x02CAP\x03\r\n"), CAPS)
    # STX CR also ends a command; only the first terminator is removed
    check("RPC STX-CR terminated", xfer(c, b"\x02FIRMWARE\x02\r"), b"2.12.1")
    c.close()
    # 2100: reassembly until STX CR / ETX
    c = rig.conn(rpc=False)
    check("2100 split 1: no reply", xfer(c, b"\x02RGB_S:0512,0512"), b"")
    check("2100 split 2: one ACK", xfer(c, b",0512,010\x03"), ACK)
    check_window("2100 reassembled", last_scene(), 10, g3(full(512, 1023)), g3(0.0))
    n = len(rig.backend.plays)
    check("2100 ETX CR LF: classic ERR", xfer(c, b"\x02RGB_S:0512,0512,0512,010\x03\r\n"),
          b"ERR\x02\r")
    check("2100 ETX CR LF: nothing drawn", len(rig.backend.plays), n)
    check("2100 ETX LF: Calman (Perl $)", xfer(c, b"\x02CAP\x03\n"), CAPS + ETX)
    c.close()
    # close without a reply, state kept
    rig.cal.win = 42
    for data in (b"\x03", b"\r\n", b"\x02\r", b"QUIT", b"QUIT:now\x03"):
        c = rig.conn()
        c.sendall(data)
        check("close on %r" % data, closed(c), True)
        c.close()
    check("silent close keeps state", (rig.cal.win, rig.cal.last is not None), (42, True))
    rig.close()


# ==============================================================================
# 5. TERM / QUIT / SHUTDOWN / disconnect
# ==============================================================================
def t_terminate():
    for word, close in (("QUIT", True), ("TERM", True), ("SHUTDOWN", True), ("xTERMx", True),
                        ("term", False), ("TERM:1", True)):
        rig = Rig(fmt=(1920, 1080, 60, 1))
        c = rig.conn()
        cmd(c, "SetRange:1")
        cmd(c, "11_APL:40")
        cmd(c, "BITD:10")
        cmd(c, "RGB_B:0512,0512,0512,0100")
        cmd(c, "10_SIZE:30")
        plays, stops = len(rig.backend.plays), rig.backend.stops
        check("%s reply" % word, cmd(c, word), ACK)
        check("%s closes" % word, closed(c), close)
        cal = rig.cal
        if close:
            check("%s reset" % word, (cal.win, cal.apl, cal.apl_enabled, cal.bg, cal.last,
                                      cal.explicit_bpc),
                  (10, 18.0, False, upgci.BLACK, None, ""))
            check("%s calman range = conf range before the release" % word, cal.range, 1)
            # SetRange:1 saved the conf before the first apply_source, so PGenerator's
            # "WebUI preferred" range was captured as Limited: the release keeps 1
            check("%s releases to the preferred range" % word, cal.conf["rgb_quant_range"], "1")
            check("%s keeps the picture" % word, (len(rig.backend.plays), rig.backend.stops),
                  (plays, stops))
        else:
            check("lower-case term is not TERM", cal.win, 30)
        c.close()
        rig.close()
    # EOF: pattern and state kept, the next connection redraws the last pattern
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    cmd(c, "RGB_S:0512,0512,0512,010")
    c.shutdown(socket.SHUT_WR)
    closed(c)
    c.close()
    time.sleep(0.2)
    check("EOF keeps the picture", rig.backend.stops, 0)
    c = rig.conn()
    n = len(rig.backend.plays)
    check("next connection 10_SIZE", cmd(c, "10_SIZE:50"), ACK)
    check("next connection redraws", len(rig.backend.plays), n + 1)
    # RGB_S replay re-applies its own size (PGenerator replays the command)
    check_window("replay of RGB_S ,010", last_scene(), 10, g3(full(512, 1023)), g3(0.0))
    c.close()
    rig.close()


# ==============================================================================
# 6. range model
# ==============================================================================
def settings(c):
    return cmd(c, "GET_SETTINGS").decode()


def t_range():
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    cmd(c, "RGB_S:0064,0064,0064,100")
    check("default reads full", rgb(last_scene().background), g3(full(64, 1023)))
    cmd(c, "QRNG:LIMITED")
    # QRNG saves rgb_quant_range before apply_source, so the renderer is not restarted:
    # it keeps the Full range it started with and reads 64 as a full-range code
    check("QRNG:LIMITED replays, renderer still Full", rgb(last_scene().background),
          g3(full(64, 1023)))
    check("QRNG:LIMITED settings", "Range=Limited" in settings(c), True)
    cmd(c, "QRNG:DEFAULT")
    # PGenerator's preferred range is the conf value at the first Calman apply_source,
    # and QRNG saves the conf first: the release goes back to Limited
    check("QRNG:DEFAULT after a first QRNG:LIMITED", "Range=Limited" in settings(c), True)
    rig.close()
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    cmd(c, "CommandRGB:16,16,16,0,100")      # apply_source(undef) captures Full as preferred
    check("CommandRGB takes the wire to Full", (rig.cal.conf["rgb_quant_range"],
                                                rig.cal.preferred), ("2", "2"))
    cmd(c, "QRNG:LIMITED")
    cmd(c, "QRNG:DEFAULT")
    check("QRNG:DEFAULT releases to Full", "Range=Full" in settings(c), True)
    check("QRNG:DEFAULT full read", rgb(last_scene().background), g3(full(16, 255)))
    cmd(c, "CONF_LEVEL:Range Limited")
    cmd(c, "CONF_LEVEL:Range Video")
    check("CONF_LEVEL Video releases", (rig.cal.conf["rgb_quant_range"], rig.cal.range), ("2", 2))
    cmd(c, "SetRange:1")
    cmd(c, "setrange:0")
    check("setrange lower case ignored", (rig.cal.conf["rgb_quant_range"], rig.cal.range), ("1", 1))
    cmd(c, "RGB_S:0940,0940,0940,100")
    check("limited 940 -> white", rgb(last_scene().background), g3(1.0))
    cmd(c, "RGB_S:1023,1023,1023,100")
    check("limited above white kept (README)", rgb(last_scene().background), g3(lim10(1023)))
    cmd(c, "RGB_S:0060,0060,0060,100")
    check("limited below black kept", last_scene().background.ycc("bt709"), (60, 512, 512))
    cmd(c, "YCC_S:0600,0400,0700,100")
    check("YCC_S limited: exact codes", last_scene().background.ycc("bt709"), (600, 400, 700))
    cmd(c, "COLF:YCbCr444")
    cmd(c, "RGB_S:0064,0064,0064,100")
    # ofApp.cpp:626-627 + the YCbCr shader: codes are wire codes in Y'CbCr formats
    check("YCbCr output: codes read in the wire range", rgb(last_scene().background), g3(0.0))
    cmd(c, "COLF:RGB")
    check("RPC RANGE:Full", cmd(c, "RANGE:Full"), ACK)
    check("RPC RANGE:Full effect", (rig.cal.conf["rgb_quant_range"], rig.cal.range), ("2", 2))
    cmd(c, "SetRange:1")
    c2 = rig.conn(rpc=False)
    check("2100 RANGE:Full reply", cmd(c2, "RANGE:Full"), ACK)
    check("2100 RANGE:Full no effect", (rig.cal.conf["rgb_quant_range"], rig.cal.range), ("1", 1))
    cmd(c, "CMD:SET_PGENERATOR_CONF_RGB_QUANT_RANGE:0")
    check("RPC CMD range 0 releases", (rig.cal.conf["rgb_quant_range"], rig.cal.range), ("2", 2))
    cmd(c, "SetRange:0")
    # APL surround in limited wire range with a full source (CommandRGB sets the wire)
    cmd(c, "CONF_LEVEL:Range Limited")
    cmd(c, "11_APL:18")
    cmd(c, "10_SIZE:10")
    check("10_SIZE undone by the replay of RGB_S ,100", rig.cal.win, 100)
    cmd(c, "RGB_S:0064,0064,0064,010")
    cmd(c, "CommandRGB:235,235,235,0,999")
    # the renderer was last restarted Full (CMD range 0), CONF_LEVEL saved Limited without a
    # restart: 8-bit 235 is drawn full; PGenerator solves the surround in the conf's saved
    # Limited levels (it "lifts" it when the two differ) -> its 8-bit 35 (0.4.14)
    check_window("APL, renderer still Full", last_scene(), 10, g3(full(235, 255)),
                 g3(full(35, 255)))
    c.close()
    c2.close()
    rig.close()


# ==============================================================================
# 7. modes, bit depth, format, metadata
# ==============================================================================
def t_modes():
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    sig = rig.gen.sig
    cmd(c, "RGB_S:0512,0512,0512,010")
    n = len(rig.backend.plays)
    cmd(c, "DSMD:HDR10")
    check("DSMD:HDR10 replays before ACK", len(rig.backend.plays), n + 1)
    check("DSMD:HDR10 settings", settings(c),
          "Resolution=1920x1080,Refresh=60,1_FORMAT=RGB 8-bit,Range=Full,Bits=8,Dolby=Off")
    check("HDR10 mode/primaries", (sig.mode, sig.primaries, sig.colorimetry), ("hdr10", 1, "bt2020"))
    for p in ("HDR10+", "HDR", "Dolby Vision", "SDR "):
        cmd(c, "DSMD:" + p)
        check("DSMD:%s ignored" % p, sig.mode, "hdr10")
    # Calman saves signal_mode "hdr", the restart normalises it to "hdr10" (command.pm:177-191)
    check("restart normalises signal_mode", rig.cal.conf["signal_mode"], "hdr10")
    n = len(rig.backend.plays)
    cmd(c, "DSMD:HDR10")
    check("DSMD:HDR10 again: signal_mode hdr is a change, replay without restart",
          (len(rig.backend.plays), rig.cal.conf["signal_mode"]), (n + 1, "hdr"))
    n = len(rig.backend.plays)
    cmd(c, "DSMD:HDR10")
    check("DSMD unchanged: no replay", len(rig.backend.plays), n)
    cmd(c, "DSMD:DV")
    check("DSMD:DV settings", settings(c),
          "Resolution=1920x1080,Refresh=60,1_FORMAT=RGB 8-bit,Range=Full,Bits=8,Dolby=On")
    check("DV", (sig.mode, rig.cal.range), ("dv", 2))
    cmd(c, "RGB_S:0512,0512,0512,100")
    check_window("DV codes as PGenerator's DV tunnel shows them (limited)", last_scene(), 100,
                 g3(dvl(512, 1023)), None)
    cmd(c, "SetRange:1")
    cmd(c, "RGB_S:0064,0064,0064,100")
    check_window("DV: 64 is black (0.4.13)", last_scene(), 100, g3(dvl(64, 1023)), None)
    cmd(c, "CONF_HDR:ST2084,0.6400,0.3300,0.3000,0.6000,0.1500,0.0600,0.3127,0.3290,0.0050,"
           "1000,1000,004002.2000")
    check("CONF_HDR leaves DV", (sig.mode, sig.primaries, sig.colorimetry), ("hdr10", 0, "bt709"))
    check("CONF_HDR metadata", (sig.max_luma, sig.min_luma, sig.max_cll, sig.max_fall),
          (1000, 0.005, 1000, 400))
    check("CONF_HDR mastering table", sig.mastering_primaries()[0], (0.64, 0.33))
    check("CONF_HDR Dolby=Off", "Dolby=Off" in settings(c), True)
    cmd(c, "CONF_HDR:PQ,0.681,0.32,0.265,0.69,0.15,0.06,0.3127,0.329,0.0001,4000.9,,")
    check("CONF_HDR P3/int MaxL", (sig.primaries, sig.max_luma, sig.min_luma, sig.max_cll),
          (2, 4000, 0.0001, 1000))
    mode = sig.mode
    for k in ("HDR_ENABLE:1", "EOTF:7", "HDR_ENABLE:yes"):
        cmd(c, k)
        check("%s: no change" % k, sig.mode, mode)
    n = len(rig.backend.plays)
    conf = rig.cal.conf
    # CLSP/PRIM only save: the renderer keeps what it started with (bt2020, P3 from CONF_HDR)
    cmd(c, "CLSP:BT.2020")
    check("CLSP:BT.2020 -> 709 saved, no redraw", (conf["colorimetry"], sig.colorimetry,
                                                   len(rig.backend.plays)), ("2", "bt2020", n))
    cmd(c, "CLSP:2020")
    check("CLSP:2020", conf["colorimetry"], "9")
    cmd(c, "PRIM:3")
    check("PRIM:3 P3-DCI saved, no redraw", (conf["primaries"], sig.primaries,
                                              len(rig.backend.plays)), ("3", 2, n))
    cmd(c, "PRIM:0")
    check("PRIM:0 -> P3-D65", (conf["primaries"], conf["colorimetry"]), ("2", "9"))
    cmd(c, "PRIM:1")
    check("PRIM:1 -> 709", (conf["primaries"], conf["colorimetry"]), ("0", "2"))
    cmd(c, "PRIM:DCI-P3")
    check("PRIM:DCI-P3", conf["primaries"], "2")
    n = len(rig.backend.plays)
    cmd(c, "MAXL:600")
    check("MAXL in HDR10: re-encoded", (sig.max_luma, len(rig.backend.plays)), (600, n + 1))
    n = len(rig.backend.plays)
    cmd(c, "HDR_MAXL:600")
    check("HDR_MAXL unchanged: no redraw", len(rig.backend.plays), n)
    cmd(c, "APPLY:1")
    check("APPLY after PRIM/MAXL saves: replays once", len(rig.backend.plays), n + 1)
    cmd(c, "21_HDR_MetadataMode:3")
    check("21_HDR_MetadataMode:3", (sig.mode, sig.dv_map_mode), ("dv", 1))
    cmd(c, "CONF_DV:relative ")
    check("CONF_DV relative", sig.dv_map_mode, 2)
    cmd(c, "DSMD:SDR")
    check("DSMD:SDR", (sig.mode, sig.colorimetry, sig.primaries, sig.bits), ("sdr", "bt709", 0, 8))
    cmd(c, "BITDEPTH:10")
    check("BITDEPTH:10", settings(c),
          "Resolution=1920x1080,Refresh=60,1_FORMAT=RGB 10-bit,Range=Full,Bits=10,Dolby=Off")
    cmd(c, "DSMD:HLG")
    check("HLG keeps explicit 10", (sig.mode, sig.bits), ("hlg", 10))
    cmd(c, "CMD:SET_PGENERATOR_CONF_COLOR_FORMAT:1")
    check("CMD color format", "1_FORMAT=YCbCr 444 10-bit" in settings(c), True)
    cmd(c, "COLORSPACE:RGB 12-bit")
    check("RPC COLORSPACE", "1_FORMAT=RGB 12-bit" in settings(c), True)
    cmd(c, "CONF_LEVEL:Bits 9")
    check("CONF_LEVEL Bits 9 invalid", sig.bits, 12)
    cmd(c, "CONF_LEVEL:Format YCbCr 422")
    check("CONF_LEVEL Format default 8 bpc", "1_FORMAT=YCbCr 422 8-bit" in settings(c), True)
    n = len(rig.backend.plays)
    cmd(c, "CONF_LEVEL:Gamma-HDR")
    check("Gamma-HDR: saved, no redraw, renderer still HLG",
          (Calman_mode(rig.cal.conf), sig.mode, len(rig.backend.plays)), ("hdr10", "hlg", n))
    cmd(c, "CONF_FORMAT:1080p")
    check("CONF_FORMAT (SET_MODE) restarts the renderer: HDR10", sig.mode, "hdr10")
    check("CONF_FORMAT:1080p -> 60", (sig.width, sig.height, sig.fps_num, sig.fps_den),
          (1920, 1080, 60, 1))
    check("CONF_FORMAT always replays", len(rig.backend.plays), n + 1)
    cmd(c, "CONF_FORMAT:2160p23.976")
    # daemon.pm:2248-2256: int(rate) or +-0.25 Hz, first listed wins: 24.00 before 23.98
    check("CONF_FORMAT 2160p23.976", (sig.width, sig.fps_num, sig.fps_den), (3840, 24, 1))
    cmd(c, "CONF_FORMAT:720p")
    check("CONF_FORMAT 720p -> 60", (sig.width, sig.fps_num), (1280, 60))
    cmd(c, "CONF_FORMAT:Resolution=1440p,Refresh=60")
    check("CONF_FORMAT 1440 not standard", sig.width, 1280)
    cmd(c, "CONF_FORMAT:Resolution=3840x2160")
    check("CONF_FORMAT WxH without Refresh", sig.width, 1280)
    cmd(c, "CONF_FORMAT:3840x2160")
    check("CONF_FORMAT 3840x2160 keeps the rate", (sig.width, sig.fps_num), (3840, 60))
    c.close()
    rig.close()


# ==============================================================================
# 8. specialty charts
# ==============================================================================
def t_specialty():
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    check("combo reply", cmd(c, "SPECIALTY:BRIGHTNESS\x02CONF_LEVEL:Range Limited"), ACK)
    check("combo range", (rig.cal.conf["rgb_quant_range"], rig.cal.range), ("1", 1))
    check("combo remembered", rig.cal.last, ("SPECIALTY", "SPECIALTY", "BRIGHTNESS"))
    sc = last_scene()
    W, H = 1920, 1080
    px, py, pw, ph = int(W * 0.06), int(H * 0.18), W - 2 * int(W * 0.06), int(H * 0.48)
    gap = max(2, int(pw / 130))
    by1, by2 = py + int(ph * 0.08), py + ph - int(ph * 0.08) - 1
    bars = [r for r in sc.rects if r[1] == by1 and r[3] == by2 - by1 + 1]
    check("BRIGHTNESS bars", len(bars), 13)
    if len(bars) == 13:
        check("bar 0 x", bars[0][0], px + gap)
        check("bar 12 right", bars[12][0] + bars[12][2] - 1, px + pw - 1 - gap)
        check("bar levels (limited)", [grey(b[4]) for b in bars],
              [lim8(v) for v in (2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 25)])
    frame = [r for r in sc.rects if (r[0], r[1]) == (px, py)]
    check("frame 56", grey(frame[0][4]) if frame else None, lim8(56))
    check("background byte 0 (below black kept)", grey(sc.background), lim8(0))
    cmd(c, "SetRange:0")
    sc = last_scene()
    bars = [r for r in sc.rects if r[1] == by1 and r[3] == by2 - by1 + 1]
    # the wire range does not change (the combo saved it), so no restart: the replay writes
    # the same image file and the renderer keeps its loaded texture (ofApp.cpp:491)
    check("BRIGHTNESS replay, same file: old texture", [grey(b[4]) for b in bars][:2],
          [lim8(2), lim8(4)])
    cmd(c, "SPECIALTY:ALIGNMENT")
    cmd(c, "SPECIALTY:BRIGHTNESS")
    sc = last_scene()
    bars = [r for r in sc.rects if r[1] == by1 and r[3] == by2 - by1 + 1]
    check("BRIGHTNESS after another chart, full range", [grey(b[4]) for b in bars][:2],
          [full(2, 255), full(4, 255)])
    cmd(c, "SPECIALTY:CONTRAST")
    sc = last_scene()
    py2, ph2 = int(H * 0.15), int(H * 0.52)
    by1 = py2 + int(ph2 * 0.10)
    bars = [r for r in sc.rects if r[1] == by1]
    check("CONTRAST levels (SDR)", [grey(b[4]) for b in bars],
          [full(v, 255) for v in (232, 234, 236, 238, 240, 242, 244, 246, 248, 250, 252, 254, 255)])
    panel = [r for r in sc.rects if (r[0], r[1]) == (px, py2)]
    check("CONTRAST panel 235", grey(panel[0][4]) if panel else None, full(235, 255))
    cmd(c, "DSMD:HDR10")
    sc = last_scene()
    peak = pq(1000)
    bars = [r for r in sc.rects if r[1] == by1]
    # webui.pm scaling kept, rounded to a 10-bit code instead of 8-bit (README)
    check("CONTRAST PQ peak-scaled", grey(bars[-1][4]) if bars else None, r10full(peak))
    check("CONTRAST PQ bar 232", grey(bars[0][4]) if bars else None, r10full(232 * peak / 255.0))
    cmd(c, "DSMD:SDR")
    cmd(c, "SPECIALTY:overscan ")
    sc = last_scene()
    check("ALIGNMENT edge box", [r[:4] for r in sc.rects[:4]],
          [(0, 0, 1920, 3), (0, 1077, 1920, 3), (0, 0, 3, 1080), (1917, 0, 3, 1080)])
    check("ALIGNMENT greys", sorted({grey(r[4]) for r in sc.rects}),
          sorted({full(255, 255), full(160, 255), full(96, 255)}))
    cmd(c, "SPECIALTY:FOO")
    sc = last_scene()
    check("unknown specialty grey 128", (sc.rects, grey(sc.background)), ([], full(128, 255)))
    cmd(c, "BITD:10")
    check("unknown specialty 10 bpc: 8-bit 128", grey(last_scene().background), full(128, 255))
    cmd(c, "SPECIALTY:PLUGE")
    check("no PLUGE alias", (last_scene().rects, grey(last_scene().background)), ([], full(128, 255)))
    # 4K render of every chart in DV (statistics over many rectangles)
    cmd(c, "CONF_FORMAT:2160p24")
    cmd(c, "DSMD:DV")
    t = time.time()
    for name in ("BRIGHTNESS", "CONTRAST", "ALIGNMENT"):
        check("4K DV %s" % name, cmd(c, "SPECIALTY:" + name), ACK)
    if time.time() - t > 30:
        FAILS.append("4K DV specialty charts took %.1fs" % (time.time() - t))
    c.close()
    rig.close()


# ==============================================================================
# 9. UPGCI 2100 / GCI (INIT:2.0)
# ==============================================================================
def t_gci():
    rig = Rig(fmt=(3840, 2160, 60000, 1001))
    c = rig.conn(rpc=False)
    check("INIT:2.0", cmd(c, "INIT:2.0"), ACK)
    check("INIT -> 1080p24", (rig.gen.sig.width, rig.gen.sig.fps_num, rig.gen.sig.fps_den),
          (1920, 24, 1))
    check("CONF_FORMAT", cmd(c, "CONF_FORMAT:Resolution=1920x1080,Refresh=24,1_FORMAT=RGB 10-bit,"
                                "Range=Limited"), ACK)
    check("GCI GET_SETTINGS", cmd(c, "GET_SETTINGS"),
          b"Resolution=1920x1080,Refresh=24,1_FORMAT=RGB 10-bit,Range=Full,Bits=10,Dolby=Off" + ETX)
    cmd(c, "CLSP:BT2020")
    check("GCI: CLSP suppressed", rig.gen.sig.colorimetry, "bt709")
    cmd(c, "QRNG:LIMITED")
    check("GCI: QRNG still applies", (rig.cal.conf["rgb_quant_range"], rig.cal.range), ("1", 1))
    cmd(c, "DSMD:DV")
    check("GCI: DV keeps the Calman range", (rig.gen.sig.mode, rig.cal.range), ("dv", 1))
    check("GCI DV: wire Full", b"Range=Full" in cmd(c, "GET_SETTINGS"), True)
    # YCC_* reaches daemon.pm:2182's stray apply_source_rgb_quant_range, RGB_* does not
    cmd(c, "RGB_S:0512,0512,0512,010")
    check("GCI DV: RGB_S keeps wire Full", b"Range=Full" in cmd(c, "GET_SETTINGS"), True)
    check("GCI DV: YCC_S", cmd(c, "YCC_S:0512,0512,0512,010"), ACK)
    # ... which writes conf 1 and restarts; the restart's normalize_dv_transport_conf
    # puts rgb_quant_range back to 2 (command.pm:74-79, 135-167)
    check("GCI DV: YCC_S restart normalises the wire back to Full",
          (b"Range=Full" in cmd(c, "GET_SETTINGS"), rig.cal.rend["rgb_quant_range"]), (True, "2"))
    cmd(c, "DSMD:HDR10")
    check("GCI: DSMD:HDR10 changes no allowed key, stays DV", rig.gen.sig.mode, "dv")
    cmd(c, "DSMD:SDR")
    check("GCI: coherence to SDR", (rig.gen.sig.mode, rig.gen.sig.colorimetry), ("sdr", "bt709"))
    cmd(c, "DSMD:HDR10")
    check("GCI: coherence to HDR10", (rig.gen.sig.mode, rig.gen.sig.colorimetry), ("hdr10", "bt2020"))
    cmd(c, "CONF_FORMAT:Resolution=3840x2160,Refresh=23.976")
    c.close()
    c = rig.conn(rpc=False)
    cmd(c, "INIT:1.2")
    check("INIT restores the last CONF_FORMAT", (rig.gen.sig.width, rig.gen.sig.fps_num),
          (3840, 24))
    cmd(c, "CLSP:BT709")
    check("INIT:1.2 is not GCI", rig.cal.conf["colorimetry"], "2")
    c.close()
    rig.close()


# ==============================================================================
# 10. round-1 findings: classic handlers, HTTP GET, UPLOAD_FILE, framing, Perl numbers,
#     ASCII regexes, the renderer's start-up latch
# ==============================================================================
def sig_of_last():
    return SCENES[-1][1] if SCENES else None


def t_classic():
    """daemon.pm:2396-2692: a Calman key without a colon still runs the classic handlers
    (ACKed); a 2100 command not ending with ETX gets their real replies + STX CR."""
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    n = len(rig.backend.plays)
    check("F1 RPC RGB= reply", cmd(c, "RGB=RECTANGLE;500,500;0;235,235,235;16,16,16;50,50;Testo"), ACK)
    check("F1 RPC RGB= drawn", len(rig.backend.plays), n + 1)
    sc = last_scene()
    check("F1 RGB= rectangle", (sc.rects[0][:4], rgb(sc.rects[0][4]), rgb(sc.background)),
          ((50, 50, 500, 500), g3(full(235, 255)), g3(full(16, 255))))
    check("F1 RGB= not a Calman pattern", rig.cal.last, None)
    n = len(rig.backend.plays)
    for k in ("FUNCTIONS=RECTANGLE;500,500;0;GREY10", "VIDEO=a;b;5s", "FOO"):
        check("F1 RPC %s" % k, cmd(c, k), ACK)
    check("F1 FUNCTIONS/VIDEO/unknown: nothing drawn (no *.server files)",
          len(rig.backend.plays), n)
    check("F1 connection open", closed(c), False)
    cmd(c, "RGB=RECTANGLE10bit;100,100;0;1023,0,0;0,0,0;-1,-1;x")
    check("RGB= 10bit centred", (last_scene().rects[0][:4], rgb(last_scene().rects[0][4])),
          ((910, 490, 100, 100), (1.0, 0.0, 0.0)))
    n = len(rig.backend.plays)
    cmd(c, "RGB=RECTANGLE;100,100;0;256,0,0;0,0,0;-1,-1;x")
    check("RGB= 8-bit 256 rejected, not drawn", len(rig.backend.plays), n)
    c.close()
    c2 = rig.conn(rpc=False)

    def classic(text):
        return xfer(c2, text.encode("latin-1") + b"\x02\r")
    check("F3 GETSTATUS", classic("GETSTATUS"), b"OK:Alive\x02\r")
    check("F3 IS_ALIVE", classic("IS_ALIVE"), b"ALIVE\x02\r")
    check("F3 CMD:GET_RESOLUTION", classic("CMD:GET_RESOLUTION"), b"OK:1920x1080\x02\r")
    check("F3 CMD:GET_PGENERATOR_VERSION", classic("CMD:GET_PGENERATOR_VERSION"), b"OK:2.12.1\x02\r")
    check("F3 CMD:MULTIPLE", classic("CMD:MULTIPLE:GET_STATUS:GET_PGENERATOR_CONF_MAX_BPC"),
          b"OK:\nGET_STATUS:Alive\nGET_PGENERATOR_CONF_MAX_BPC:8\x02\r")
    r = classic("CMD:GET_CPU")
    check("F3 CMD:GET_CPU", bool(__import__("re").match(rb"OK:\d+%\x02\r$", r)), True)
    r = classic("STATS")
    check("F3 STATS", (r.startswith(b"OK:"), b"connections: 2" in r, b"patterns: 3" in r,
                       b"errors: 1" in r), (True, True, True, True))
    check("F3 STATSRESET", classic("STATSRESET"), b"OK:0\x02\r")
    check("F3 STATS after reset", classic("STATS"), b"OK:\x02\r")
    check("F3 PGENERATORISEXECUTED", classic("PGENERATORISEXECUTED:"),
          b"Pid %d\x02\r" % os.getpid())
    check("F3 CLIENTNAME", classic("clientname = Perceptual Pro!"), b"OK\x02\r")
    check("F3 CLIENTNAME invalid", classic("SOFTWARE:@@"), b"invalid client name\x02\r")
    check("F3 unknown", classic("FOO"), b"ERR\x02\r")
    check("F3 GETPATTERNIMAGE", classic("GETPATTERNIMAGE:x"), b"None\x02\r")
    n = len(rig.backend.plays)
    check("F3 RGB=", classic("RGB=RECTANGLE;500,500;0;235,235,235;16,16,16;50,50;Testo"),
          b"OK\x02\r")
    check("F3 RGB= drawn", len(rig.backend.plays), n + 1)
    check("F3 ETX CR LF is classic", xfer(c2, b"\x02CMD:GET_RESOLUTION\x03\r\n"),
          b"OK:1920x1080\x02\r")
    # CMD:GET_RESOLUTION marked this connection HCFR: 16/235 anchors are a LIMITED source,
    # expanded once the renderer runs Limited (RPC RANGE restarts it)
    cmd_ = "RGB=rectangle;200,200;0;235,235,235;16,16,16;-1,-1;x"
    classic(cmd_)
    check("HCFR RGB= RECTANGLE8bit, renderer Full", (rgb(last_scene().rects[0][4]),
          rgb(last_scene().background)), (g3(full(235, 255)), g3(full(16, 255))))
    c = rig.conn()
    cmd(c, "RANGE:Limited")
    classic(cmd_)
    check("HCFR RGB= limited", (rgb(last_scene().rects[0][4]), rgb(last_scene().background)),
          (g3(1.0), g3(0.0)))
    c3 = rig.conn(rpc=False)
    c3.sendall(b"RGB=RECTANGLE;200,200;0;235,235,235;16,16,16;-1,-1;x\x02\r")
    xfer(c3, b"")
    check("non-HCFR RGB= full source on a Limited renderer", rgb(last_scene().background),
          g3(full(16, 255)))
    c.close()
    c3.close()
    c2.close()
    rig.close()


def t_http():
    """F2/identity-1: daemon.pm:900-956 answers a GET on any raw recv, then closes."""
    rig = Rig(fmt=(1920, 1080, 60, 1))
    for rpc, data, want in (
            (True, b"GET /frames/index.html HTTP/1.1\r\n\r\n", ACK),
            (True, b"GET /frames/a.png-1 HTTP/1.1\r\nHost: x\r\n\r\n", ACK),
            (True, b"\x02GET /x/index.html HTTP/1.1\x03", ACK),
            (False, b"\x02GET /x/index.html HTTP/1.1\x03", ACK),
            (False, b"GET /frames/index.html HTTP/1.1\r\n\r\n",
             b"HTTP/1.0 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n"
             b"<body style='background-color:#adadad;'>\n<center>\n</center>\n</body>\n\x02\r"),
            (False, b"GET /f/b.jpg-7&inline=1 HTTP/1.1\r\n\r\n",
             b"HTTP/1.0 200 OK\r\nAccess-Control-Allow-Origin:*\r\nContent-Type: text/html\r\n"
             b"Connection: close\r\n\r\n<img src=\"data:image/jpg;base64,\" />\x02\r")):
        c = rig.conn(rpc)
        check("HTTP %r reply" % data[:24], xfer(c, data), want)
        check("HTTP %r closes" % data[:24], closed(c), True)
        c.close()
    rig.close()


def t_upload_buffer():
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    cmd(c, "RGB_S:0512,0512,0512,010")
    rig.cal.win = 42
    check("F5 UPLOAD_FILE before TERM", xfer(c, b"UPLOAD_FILE:VIDEO:TERM.txt:text/plain:END_UPLOAD:0:10:abc\x03"),
          ACK)
    check("F5 open, state kept", (closed(c), rig.cal.win), (False, 42))
    c.close()
    c = rig.conn(rpc=False)
    check("UPLOAD_FILE 2100 progress",
          xfer(c, b"UPLOAD_FILE:VIDEO:a.txt:text/plain:UPLOADING:5:10:abc\x02\r"), b"OK:50%\x02\r")
    check("UPLOAD_FILE 2100 size 0", xfer(c, b"UPLOAD_FILE:VIDEO:a.txt:text/plain:X:5:0:abc\x02\r"),
          b"ERR\x02\r")
    check("UPLOAD_FILE 2100 END", xfer(c, b"UPLOAD_FILE:VIDEO:a.txt:t:END_UPLOAD:5:10:\x02\r"),
          b"OK:100%\x02\r")
    # F4: no cap on a command being gathered
    c.sendall(b"\x02SN" + b" " * (1 << 20))
    check("F4 >1 MiB command", xfer(c, b"\x03"), b"c0ffee12" + ETX)
    c.close()
    rig.close()


def t_numbers():
    """Perl numification: Inf/NaN strings and overflows (1e999) clamp, never abort."""
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    cmd(c, "RGB_S:0512,0512,0512,010")
    cmd(c, "RGB_S:inf,0,0,050")
    check_window("RGB_S:inf -> max code", last_scene(), 50, (1.0, 0.0, 0.0), g3(0.0))
    cmd(c, "10_SIZE:inf")
    check("10_SIZE:inf, then the RGB_S replay sets its own 50", rig.cal.win, 50)
    cmd(c, "CommandRGB:1023,1023,1023,1,0")
    cmd(c, "10_SIZE:inf")
    check("10_SIZE:inf -> 100", rig.cal.win, 100)
    check("CommandRGB legacy token replayed at 100: full field", window(last_scene()), None)
    cmd(c, "11_APL:Infinity")
    check("11_APL:Infinity", rig.cal.apl, 100.0)
    cmd(c, "10_SIZE:10")
    n = len(rig.backend.plays)
    cmd(c, "SetRange:1e999")
    check("SetRange:1e999 -> Full, replayed", (rig.cal.range, rig.cal.conf["rgb_quant_range"],
                                               len(rig.backend.plays)), (2, "2", n + 1))
    cmd(c, "CommandRGB:1e999,0,0,1,100")
    check_window("CommandRGB:1e999", last_scene(), 100, (1.0, 0.0, 0.0), None)
    cmd(c, "RGB_S:0512,0512,0512,1e400")
    check("RGB_S win 1e400", (rig.cal.win, rig.cal.last), (100, ("RGB", "RGB_S", "0512,0512,0512,1e400")))
    cmd(c, "10_SIZE:1e400")
    check("10_SIZE:1e400", rig.cal.win, 100)
    cmd(c, "RGB_A:0,0,0,1e999")
    check("RGB_A win 1e999", rig.cal.win, 100)
    sig = rig.gen.sig
    cmd(c, "RGB_S:0512,0512,0512,010")
    n = len(rig.backend.plays)
    check("modes-2 CONF_HDR MaxL 1e999", cmd(c, "CONF_HDR:ST2084,0.708,0.292,0.17,0.797,0.131,"
                                                "0.046,0.3127,0.329,0.005,1e999,1000,00400"), ACK)
    check("modes-2 applied before the ACK", (sig.mode, len(rig.backend.plays), rig.cal.dirty,
                                             rig.cal.conf["max_luma"]), ("hdr10", n + 1, False, "Inf"))   # int("1e999") > 0: stored
    cmd(c, "DSMD:SDR")
    n = len(rig.backend.plays)
    cmd(c, "CONF_HDR:HLG,0.708,0.292,0.17,0.797,0.131,0.046,0.3127,0.329,0.005,1000,1e400,00400")
    check("modes-2 CONF_HDR MaxCLL 1e400", (sig.mode, len(rig.backend.plays), rig.cal.conf["max_cll"]),
          ("hlg", n + 1, "Inf"))                               # int("1e400"): Inf, stored
    cmd(c, "DSMD:HDR10")
    n, base = len(rig.backend.plays), settings(c)
    for k in ("EOTF:inf", "HDR_EOTF:nan", "EOTF:Infinity"):
        cmd(c, k)
        check("modes-3 %s is a no-op" % k, (sig.mode, len(rig.backend.plays), settings(c)),
              ("hdr10", n, base))
    cmd(c, "DSMD:DV")
    cmd(c, "21_HDR_MetadataMode:inf")
    check("modes-3 21_HDR_MetadataMode:inf stays DV", sig.mode, "dv")
    cmd(c, "CONF_HDR:ST2084,0.708,0.292,0.17,0.797,0.131,0.046,0.3127,0.329,inf,1000,1000,00400")
    check("modes-3 CONF_HDR MinL inf stored", rig.cal.conf["min_luma"], "Inf")
    # MAXL:inf is stored raw; webui_pattern_max_luma clamps it to 10000 (webui.pm:11635-11641)
    cmd(c, "SPECIALTY:CONTRAST")
    cmd(c, "MAXL:inf")
    check("MAXL:inf stored raw", rig.cal.conf["max_luma"], "inf")
    cmd(c, "SPECIALTY:ALIGNMENT")                  # another image: the next one is loaded
    cmd(c, "SPECIALTY:CONTRAST")
    check("MAXL:inf: the chart at a 10000-nit peak (panel byte 235 unscaled)",
          grey(last_scene().rects[0][4]), round(235 / 255.0, 9))
    c.close()
    rig.close()


def t_ascii():
    """PGenerator's regexes run on bytes: \\s \\w \\b are ASCII, uc() is ASCII."""
    rig = Rig(fmt=(3840, 2160, 60, 1))
    c = rig.conn()
    sig = rig.gen.sig
    cmd(c, "CONF_FORMAT:2160p\xa024")
    check("modes-4 2160p<A0>24: no rate", (sig.width, sig.fps_num, sig.fps_den), (3840, 60, 1))
    cmd(c, "CONF_FORMAT:\x0b1080\x1c")
    check("modes-4 1080<1C> not understood", sig.width, 3840)
    cmd(c, "COLF:RGB 10bit\xe9")
    check("modes-4 10bit<E9>", "Bits=10" in settings(c), True)
    cmd(c, "COLF:YCbCr444 12-bit\xa0Range Limited")
    check("modes-4 <A0>Range: no range", ("Range=Full" in settings(c), rig.cal.range), (True, None))
    base = settings(c)
    for k in ("CONF_LEVEL:Range\x1fLimited", "CONF_LEVEL:Range\x85Limited", "CONF_LEVEL:Bits\xa08",
              "CONF_LEVEL:Format\xa0RGB 8-bit"):
        cmd(c, k)
        check("RB1 %r unknown CONF_LEVEL" % k, settings(c), base)
    cmd(c, "CONF_LEVEL:Range Limited")
    for sep in ("\x1c", "\x1f", "\x85", "\xa0"):
        payload = "BRIGHTNESS\x02CONF_LEVEL:Range%sFull" % sep
        cmd(c, "SPECIALTY:" + payload)
        sc = last_scene()
        check("combo sep %r: not a combo" % sep, (rig.cal.range, sc.rects, grey(sc.background),
                                                  rig.cal.last),
              (1, [], full(128, 255), ("SPECIALTY", "SPECIALTY", payload)))
    cmd(c, "SetRange:0")
    for name in ("\x1cBRIGHTNESS", "BRIGHTNESS\xa0", "BRIGHTNE\xdf", "\x85CONTRAST"):
        cmd(c, "SPECIALTY:" + name)
        check("SPECIALTY %r unknown grey" % name, (last_scene().rects, grey(last_scene().background)),
              ([], full(128, 255)))
    cmd(c, "SPECIALTY:brightness\x0b")
    check("SPECIALTY brightness<VT> is the chart", len(last_scene().rects) > 100, True)
    c.close()
    rig.close()


def t_latch():
    """The renderer takes mode, colorimetry, primaries, format and range from the conf
    only when it starts (main.cpp:115-154): saves without a restart keep the old signal."""
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    cmd(c, "RGB_S:0512,0512,0512,010")
    cmd(c, "CLSP:BT2020")
    cmd(c, "10_SIZE:20")
    check("modes-1 CLSP then 10_SIZE: replay still BT.709", sig_of_last().colorimetry, "bt709")
    cmd(c, "RGB_S:0512,0512,0512,010")
    check("modes-1 next pattern restarts: BT.2020", sig_of_last().colorimetry, "bt2020")
    cmd(c, "DSMD:HDR10")
    cmd(c, "DSMD:HDR10")        # signal_mode back to Calman's "hdr": the next apply keeps the renderer
    cmd(c, "PRIM:0")
    cmd(c, "11_APL:20")
    check("modes-1 PRIM:0 then 11_APL: replay keeps BT.2020 mastering", sig_of_last().primaries, 1)
    cmd(c, "RGB_S:0512,0512,0512,010")
    check("primaries are not a mode key: still BT.2020 after the next pattern",
          sig_of_last().primaries, 1)
    cmd(c, "DSMD:SDR")
    cmd(c, "RGB_S:0512,0512,0512,010")
    cmd(c, "CONF_LEVEL:Gamma-HDR")
    cmd(c, "10_SIZE:30")
    check("modes-1 Gamma-HDR then 10_SIZE: replay still SDR", sig_of_last().mode, "sdr")
    cmd(c, "RGB_S:0512,0512,0512,010")
    check("modes-1 next pattern: HDR10", sig_of_last().mode, "hdr10")
    c.close()
    rig.close()
    # RB1: GCI, the range saved before apply_source does not restart the renderer
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn(rpc=False)
    for k in ("INIT:2.0", "BITD:10", "RGB_S:0064,0064,0064,100", "QRNG:LIMITED"):
        cmd(c, k)
    check("RB1 QRNG replay: renderer Full, 64 not expanded", rgb(last_scene().background),
          g3(full(64, 1023)))
    cmd(c, "RGB_S:0064,0064,0064,100")
    check("RB1 next RGB_S: still Full", rgb(last_scene().background), g3(full(64, 1023)))
    for k in ("CONF_LEVEL:Range Full", "INIT:2.0", "BITD:8", "CommandRGB:128,128,128,0,100",
              "CONF_LEVEL:Gamma-HDR"):
        cmd(c, k)
    for k in ("SPECIALTY:BLACK\x02CONF_LEVEL:Range Full", "QRNG:FULL", "10_SIZE:50"):
        cmd(c, k)
        check("RB1 GCI %r drawn SDR" % k, rig.backend.plays[-1][2], "sdr")
    cmd(c, "CommandRGB:128,128,128,0,100")
    check("RB1 GCI next CommandRGB: HDR10", rig.backend.plays[-1][2], "hdr10")
    c.close()
    rig.close()
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    for k in ("INIT:2.0", "QRNG:LIMITED", "BITD:10", "RGB_S:0064,0064,0064,100",
              "CMD:SET_PGENERATOR_CONF_RGB_QUANT_RANGE:2"):
        cmd(c, k)
    check("RB1 RPC CMD range 2 saved: renderer still Limited", rgb(last_scene().background), g3(0.0))
    # a restart with nothing replayed shows the renderer's pattern file again
    c.close()
    c = rig.conn()
    n = len(rig.backend.plays)
    cmd(c, "INIT:1.2")         # the pattern is forgotten, the mode is already 1080p24
    check("INIT at the remembered mode: no restart, no redraw", len(rig.backend.plays), n)
    cmd(c, "CONF_FORMAT:720p60")
    check("CONF_FORMAT restart with no last pattern: file shown again at 720p",
          (len(rig.backend.plays), sig_of_last().height), (n + 1, 720))
    c.close()
    rig.close()


def t_concurrency():
    """Commands from two clients are served one at a time (PGenerator's select loop)."""
    rig = Rig(fmt=(1920, 1080, 60, 1))
    a, b = rig.conn(), rig.conn(rpc=False)
    res = []

    def run(c, cmds):
        for k in cmds:
            res.append(cmd(c, k))
    ta = threading.Thread(target=run, args=(a, ["RGB_S:%04d,0512,0512,010" % i for i in range(5)]))
    tb = threading.Thread(target=run, args=(b, ["11_APL:%d" % i for i in range(5)]))
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    check("concurrent replies", sorted(res), sorted([ACK] * 10))
    a.close()
    b.close()
    rig.close()


# ==============================================================================
# round 2: close semantics, mode matching, conf mirror, malformed payloads
# ==============================================================================
def _range_after(rig, a):
    time.sleep(0.3)
    return settings(a).split(",")[3]


def t_hcfr_close():
    """daemon.pm:2765-2778: closing an HCFR-marked connection runs
    release_source_rgb_quant_range("hcfr"), which also drops Calman's external range
    (command.pm:83-87) and restarts the renderer on the WebUI-preferred range."""
    def classic(c, text):
        return xfer(c, text.encode("latin-1") + b"\x02\r")
    for label, rpc, lines in (("CMD:GET_RESOLUTION then EOF", False, ["CMD:GET_RESOLUTION"]),
                              ("SETCONF:HCFR then QUIT", False, ["SETCONF:HCFR:X:1", "QUIT"]),
                              ("RPC RGB=TEXT Init then EOF", True, [])):
        rig = Rig(fmt=(1920, 1080, 60, 1))
        a = rig.conn()
        for k in ("QRNG:LIMITED", "QRNG:FULL", "RGB_S:0512,0512,0512,010"):
            cmd(a, k)
        check("HCFR %s: Calman range Full before" % label, settings(a).split(",")[3], "Range=Full")
        n = len(rig.backend.plays)
        b = rig.conn(rpc=rpc)
        if rpc:
            check("HCFR RPC marker ACK", cmd(b, "RGB=TEXT;;;;;;Init"), ACK)
        for k in lines:
            classic(b, k)
        b.close()
        check("HCFR %s: close releases to the WebUI range" % label, _range_after(rig, a),
              "Range=Limited")
        check("HCFR %s: owner webui, not external" % label,
              (rig.cal.range_owner, rig.cal.external), ("webui", False))
        check("HCFR %s: the restarted renderer shows the pattern again" % label,
              len(rig.backend.plays), n + 1)
        a.close()
        rig.close()
    rig = Rig(fmt=(1920, 1080, 60, 1))
    a = rig.conn()
    cmd(a, "QRNG:LIMITED")
    cmd(a, "QRNG:FULL")
    b = rig.conn(rpc=False)
    classic(b, "CMD:GET_STATUS")
    b.close()
    check("unmarked classic close keeps Calman's range", _range_after(rig, a), "Range=Full")
    b = rig.conn()
    cmd(b, "IS_ALIVE")
    b.close()
    check("unmarked RPC close keeps Calman's range", _range_after(rig, a), "Range=Full")
    a.close()
    rig.close()


def t_conf_format_modes():
    """daemon.pm:2232-2275 against the connector's modes (a 4K TV's CTA-861 set, kernel
    order): int(rate) or +-0.25 Hz, width +100, first listed wins; no match: no change."""
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    sig = rig.gen.sig
    for fmt, want, refresh in (
            ("Resolution=3840x2160,Refresh=23.976", (3840, 2160, 24, 1), 24),
            ("2160p59.94", (3840, 2160, 60, 1), 60), ("1080p29.97", (1920, 1080, 30, 1), 30),
            ("1080p24.6", (1920, 1080, 24, 1), 24), ("2160p59.7", (3840, 2160, 60000, 1001), 59),
            ("2160p59.5", (3840, 2160, 60000, 1001), 59),
            ("1080p23.6", (1920, 1080, 24000, 1001), 23), ("1080p50.6", (1920, 1080, 50, 1), 50),
            ("2160p29.6", (3840, 2160, 30000, 1001), 29), ("1080p23.98", (1920, 1080, 24, 1), 24),
            ("Resolution=4096x2160,Refresh=24", (4096, 2160, 24, 1), 24),
            ("1234x2160p60", (3840, 2160, 60, 1), 60)):
        cmd(c, "CONF_FORMAT:" + fmt)
        check("CONF_FORMAT %s mode" % fmt, (sig.width, sig.height, sig.fps_num, sig.fps_den), want)
        check("CONF_FORMAT %s Refresh" % fmt, settings(c).split(",")[1], "Refresh=%d" % refresh)
    cmd(c, "CONF_FORMAT:1080p60")
    cmd(c, "RGB_S:0512,0512,0512,010")
    for fmt in ("1234x567p60", "1080p7", "1080p1000", "Resolution=1080,Refresh=13"):
        n = len(rig.backend.plays)
        check("CONF_FORMAT %s ACK" % fmt, cmd(c, "CONF_FORMAT:" + fmt), ACK)
        check("CONF_FORMAT %s: no mode, unchanged" % fmt, settings(c).split(",")[:2],
              ["Resolution=1920x1080", "Refresh=60"])
        check("CONF_FORMAT %s: still replays the last pattern" % fmt,
              (len(rig.backend.plays), (sig_of_last().width, sig_of_last().height)),
              (n + 1, (1920, 1080)))
    c.close()
    c = rig.conn(rpc=False)
    cmd(c, "INIT:1.2")
    check("INIT after unmatched CONF_FORMATs restores the last matched one",
          (sig.width, sig.height, sig.fps_num), (1920, 1080, 60))
    c.close()
    rig.close()


def t_prim_atoi():
    """PRIM keeps a non-numeric payload; the renderer reads it with atoi (main.cpp:154)."""
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    cmd(c, "DSMD:HDR10")
    cmd(c, "RGB_S:0512,0512,0512,010")
    for i, (p, want) in enumerate((("1e1", 1), ("5e-1", 5), ("3.7e-1", 3), (" 2x", 2), ("x", 0))):
        cmd(c, "PRIM:" + p)
        cmd(c, "BITD:%d" % (12 if i % 2 == 0 else 10))      # a mode key: the renderer restarts
        check("PRIM:%s renderer primaries" % p, (rig.cal.conf["primaries"], sig_of_last().primaries),
              (p, want))
    c.close()
    rig.close()


def t_conf_mirror():
    """PGenerator.conf as shipped + what the daemon writes: GET_PGENERATOR_CONF_<k>."""
    import base64
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn(rpc=False)

    def get(k):
        return xfer(c, ("CMD:GET_PGENERATOR_CONF_%s" % k).encode() + b"\x02\r")
    for k, want in (("DV_MAP_MODE", "2"), ("PORT_PATTERN", "85"), ("IP_PATTERN", "0.0.0.0"),
                    ("SIGNAL_MODE", "sdr"), ("CALMAN_GCI", "None"), ("DV_METADATA", "0")):
        check("fresh conf %s" % k, get(k), ("OK:%s" % want).encode() + b"\x02\r")
    g = rig.conn(rpc=False)
    cmd(g, "INIT:2.0")
    check("INIT:2.0 sets calman_gci", get("CALMAN_GCI"), b"OK:1\x02\r")
    cmd(g, "TERM")
    check("TERM clears calman_gci", get("CALMAN_GCI"), b"OK:0\x02\r")
    g.close()
    g = rig.conn(rpc=False)
    cmd(g, "INIT:2.0")
    g.close()
    time.sleep(0.3)
    check("GCI close clears calman_gci", get("CALMAN_GCI"), b"OK:0\x02\r")
    r = rig.conn()
    cmd(r, "DSMD:DOLBYVISION")
    check("DSMD:DOLBYVISION: dv_metadata from the stock map mode 2", get("DV_METADATA"),
          b"OK:4\x02\r")
    check("DSMD:DOLBYVISION: dv_map_mode still 2", get("DV_MAP_MODE"), b"OK:2\x02\r")
    check("L255 absent until Calman chooses (README)", rig.gen.sig.dv_map_mode, None)
    cmd(r, "CONF_DV:RELATIVE")
    check("CONF_DV: L255 now Calman's", rig.gen.sig.dv_map_mode is not None, True)
    lines = base64.b64decode(get("ALL")[3:-2]).decode().splitlines()
    keys = [ln.split(":", 1)[0] for ln in lines]
    for k in ("ip_pattern", "port_pattern", "calman_gci", "dv_map_mode", "signal_mode"):
        check("ALL lists %s" % k, k in keys, True)
    r.close()
    c.close()
    rig.close()


def t_maxl_nan():
    """Deviation: NaN luminance values are ignored (PGenerator stores them and its daemon
    later dies drawing an HDR10 chart); +-Inf is stored as PGenerator stores it (its charts
    clamp it, webui.pm:11635-11641); nothing crashes, charts draw."""
    for name in ("CONTRAST", "BRIGHTNESS"):
        for v in ("nan", "NaN", "-nan", "nanq", "inf", "1e999"):
            rig = Rig(fmt=(1920, 1080, 60, 1))
            c = rig.conn()
            cmd(c, "DSMD:HDR10")
            check("MAXL:%s ACK" % v, cmd(c, "MAXL:" + v), ACK)
            check("MAXL:%s" % v, rig.cal.conf["max_luma"], "1000" if "n" in v.lower().replace(
                "inf", "") else v)
            check("HDR10 SPECIALTY:%s after MAXL %s: drawn, ACK" % (name, v),
                  cmd(c, "SPECIALTY:" + name), ACK)
            check("... connection open", closed(c), False)
            rig.close()
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    cmd(c, "DSMD:HDR10")
    cmd(c, "CONF_HDR:ST2084,0.708,0.292,0.170,0.797,0.131,0.046,0.3127,0.3290,nan,inf,nan,nan")
    check("CONF_HDR: NaN ignored, Inf stored", (rig.cal.conf["min_luma"], rig.cal.conf["max_luma"],
                                                rig.cal.conf["max_cll"]), ("0.005", "Inf", "1000"))
    check("SPECIALTY:CONTRAST drawn", cmd(c, "SPECIALTY:CONTRAST"), ACK)
    rig.close()


# ==============================================================================
# 11. round-3 findings: live discovery name, the restart's DV transport normalisation,
#     PatternStart after a restart, the renderer's chart texture latch
# ==============================================================================
def t_round3():
    def q(k, text):             # a classic 2100 command (no ETX): the real CMD: reply
        return xfer(k, text.encode("latin-1") + b"\x02\r")
    # identity-R3-discovery-name-frozen: discovery_rpc calls discovery_name() per datagram
    host = [b"livingroom"]
    rig = Rig(discovery=True, info={"name": lambda: host[0].decode(), "serial": "x",
                                    "firmware": "2.12.1"})
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def ask():
        s.sendto(b"x", ("127.0.0.1", rig.disc))
        try:
            return rig.reply_sock.recvfrom(100)[0][:24].rstrip(b"\0")
        except socket.timeout:
            return None
    check("discovery name", ask(), b"livingroom")
    host[0] = b"studio"
    check("discovery name follows a host name change", ask(), b"studio")
    rig.close()
    s.close()
    src = open(os.path.join(os.path.dirname(upgci.__file__), "kodi.py")).read()
    check("Service.start passes discovery_name itself", '"name": discovery_name,' in src, True)

    # modes-R3/RB3-dv-restart-normalize-max_bpc: the DV restart writes max_bpc 8 and dv_profile 1
    # BITD:12 then DSMD:DOLBYVISION: set_dv saves dv_metadata 1, force_dv's 4 re-dirties
    # the apply, whose second pass writes the explicit 12 back without a restart
    for seq, bits in ((("DSMD:DOLBYVISION", "BITD:12"), 8), (("BITD:12", "DSMD:DOLBYVISION"), 12),
                      (("BITD:12", "CONF_DV:ABSOLUTE"), 8),
                      (("CONF_DV:PERCEPTUAL", "CONF_LEVEL:Bits 12"), 8),
                      (("BITDEPTH:12", "21_HDR_MetadataMode:3"), 8)):
        rig = Rig(fmt=(1920, 1080, 60, 1))
        c, k = rig.conn(), rig.conn(rpc=False)
        for x in seq:
            cmd(c, x)
        check("%s: GET_SETTINGS" % (seq,), settings(c),
              "Resolution=1920x1080,Refresh=60,1_FORMAT=RGB %d-bit,Range=Full,Bits=%d,Dolby=On"
              % (bits, bits))
        check("%s: conf max_bpc / dv_profile, renderer 8" % (seq,),
              (q(k, "CMD:GET_PGENERATOR_CONF_MAX_BPC"), q(k, "CMD:GET_PGENERATOR_CONF_DV_PROFILE"),
               rig.cal.rend["max_bpc"]), (b"OK:%d\x02\r" % bits, b"OK:1\x02\r", "8"))
        if seq[0] == "DSMD:DOLBYVISION":
            n = len(rig.backend.plays)
            cmd(c, "PRIM:BT709")
            cmd(c, "APPLY:1")    # a dirty apply: calman_force_dv_rgb writes the explicit 12 back
            check("later dirty apply restores the explicit 12, no restart",
                  ("Bits=12" in settings(c), rig.cal.rend["max_bpc"], len(rig.backend.plays)),
                  (True, "8", n))
        rig.close()

    # modes-R3/RB3-dv-restart-normalize-range: a Limited wire in DV is normalised back to Full
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c, k = rig.conn(), rig.conn(rpc=False)
    cmd(c, "DSMD:DOLBYVISION")
    cmd(c, "RANGE:Limited")
    check("DV RANGE:Limited: Range=Full, conf 2",
          ("Range=Full" in settings(c), q(k, "CMD:GET_PGENERATOR_CONF_RGB_QUANT_RANGE"),
           rig.cal.rend["rgb_quant_range"], rig.cal.range), (True, b"OK:2\x02\r", "2", 1))
    cmd(c, "DSMD:SDR")
    cmd(c, "RGB_S:0064,0064,0064,100")
    check("... then SDR: wire Full, a Limited-source 64 reads full range",
          (rig.cal.rend["rgb_quant_range"], grey(last_scene().background)), ("2", full(64, 1023)))
    rig.close()
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn(rpc=False)
    for x in ("INIT:2.0", "QRNG:LIMITED", "CONF_DV:ABSOLUTE", "RGB_S:0512,0512,0512,010"):
        cmd(c, x)
    for x in ("UPDATE:1", "SPECIALTY:WHITE", "CommandRGB:255,255,255,0,100"):
        n = len(rig.backend.plays)
        cmd(c, x)
        check("GCI DV after QRNG:LIMITED, %s: Range=Full, renderer Full" % x,
              ("Range=Full" in settings(c), rig.cal.conf["rgb_quant_range"],
               rig.cal.rend["rgb_quant_range"]), (True, "2", "2"))
        if x == "UPDATE:1":     # the stray range line restarts, UPDATE replays nothing
            sc = last_scene()          # black, or below black as PGenerator's DV shows it
            check("GCI DV UPDATE:1 restart shows PatternStart (nothing above black)",
                  (len(rig.backend.plays), all(r[4].r <= 0 for r in sc.rects),
                   sc.background.r <= 0), (n + 1, True, True))
    rig.close()

    # modes-R3-restart-shows-patternstart: a restart nothing redraws after shows black
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn(rpc=False)
    cmd(c, "RGB_S:1023,0000,0000,010")
    n = len(rig.backend.plays)
    cmd(c, "INIT:1")
    sc, sig = SCENES[-1]
    check("INIT's SET_MODE shows PatternStart, not the old pattern",
          (len(rig.backend.plays), lit(sc), grey(sc.background), sig.fps_num), (n + 1, [], 0.0, 24))
    c.close()
    c = rig.conn(rpc=False)
    cmd(c, "RGB_S:1023,0000,0000,010")
    c.close()
    time.sleep(0.1)
    c = rig.conn(rpc=False)
    n = len(rig.backend.plays)
    cmd(c, "INIT:1")
    check("INIT at the remembered mode: nothing drawn", len(rig.backend.plays), n)
    cmd(c, "CONF_FORMAT:2160p60")
    sc, sig = SCENES[-1]
    check("CONF_FORMAT after INIT: PatternStart at 3840",
          (len(rig.backend.plays), lit(sc), grey(sc.background), sig.width), (n + 1, [], 0.0, 3840))
    c.close()
    rig.close()

    # charts-same-chart-texture-not-reloaded: the same chart file keeps its loaded texture
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    H = 1080
    by1 = int(H * 0.15) + int(int(H * 0.52) * 0.10)

    def bar255():
        bars = [r for r in last_scene().rects if r[1] == by1]
        return grey(bars[-1][4]) if bars else None
    cmd(c, "DSMD:HDR10")
    cmd(c, "SPECIALTY:CONTRAST")
    cmd(c, "MAXL:4000")
    cmd(c, "SPECIALTY:CONTRAST")    # restarts (signal_mode hdr -> hdr10): a new texture
    check("CONTRAST after a restart: 4000-nit levels", bar255(), r10full(pq(4000)))
    for peak in (600, 10000):
        n = len(rig.backend.plays)
        cmd(c, "MAXL:%d" % peak)
        cmd(c, "SPECIALTY:CONTRAST")
        check("MAXL:%d + CONTRAST: the loaded 4000-nit texture" % peak,
              (bar255(), len(rig.backend.plays)), (r10full(pq(4000)), n + 2))
    cmd(c, "SPECIALTY:ALIGNMENT")   # another image file: loaded
    cmd(c, "SPECIALTY:CONTRAST")
    check("CONTRAST after ALIGNMENT: 10000-nit levels", bar255(), 1.0)
    cmd(c, "MAXL:600")
    cmd(c, "RGB_S:0512,0512,0512,010")      # a non-IMAGE pattern resets the latch
    cmd(c, "SPECIALTY:CONTRAST")
    check("CONTRAST after a patch: 600-nit levels", bar255(), r10full(pq(600)))
    cmd(c, "SPECIALTY:OVERSCAN")
    sc = last_scene()
    cmd(c, "SPECIALTY:ALIGNMENT")           # the same file as OVERSCAN
    check("ALIGNMENT after OVERSCAN: same file, same texture", last_scene() is sc
          or [r[:4] for r in last_scene().rects] == [r[:4] for r in sc.rects], True)
    rig.close()


def t_round4():
    """Round-4 findings: PGenerator dies, never crashes the client thread, and redraws
    nothing on a position Perl's int() leaves an NV."""
    rig = Rig(fmt=(1920, 1080, 60, 1))

    def classic(text, idle=0.2):
        c = rig.conn(rpc=False)
        n = len(rig.backend.plays)
        r = xfer(c, text.encode("latin-1") + b"\x02\r", idle=idle)
        out = (r, closed(c), len(rig.backend.plays) - n)
        c.close()
        return out
    # R4-framing-upload-zero-size-dies: "0.0" is true in Perl, then int(100*start/0) dies
    for size in ("0.0", "-0", "00", "0x", " 0", "0", ""):     # deviation: PGenerator dies on "0.0"
        check("2100 UPLOAD_FILE size %r: ERR, still open" % size,
              classic("UPLOAD_FILE:VIDEO:f.txt:text/plain:X:0:%s:abc" % size)[:2],
              (b"ERR\x02\r", False))
    # R4-framing-classic-testpattern-nonfinite-dim-crash (round_val(Inf) stays Inf)
    for dim in ("inf,100", "1e999,100", "nan,100", "-inf,100"):
        check("TESTPATTERN RECTANGLE %s: position ERR" % dim,
              classic("TESTPATTERN:P:RECTANGLE:%s:0:255,255,255:" % dim),
              (b"OK:ERR\x02\r", False, 0))
        check("TESTPATTERN CIRCLE %s: drawn at 960,540" % dim,
              classic("TESTPATTERN:P:CIRCLE:%s:0:255,255,255:" % dim), (b"OK:0\x02\r", False, 1))
    # R4-framing-classic-position-beyond-64bit-drawn: x/y outside IV/UV print as "3e+19"
    check("TESTPATTERN RECTANGLE 3e19,100: x -1.5e+19 ERR",
          classic("TESTPATTERN:P:RECTANGLE:3e19,100:0:255,255,255:"), (b"OK:ERR\x02\r", False, 0))
    check("TESTPATTERN RECTANGLE 100,-3e19: y 1.5e19 fits a UV, drawn",
          classic("TESTPATTERN:P:RECTANGLE:100,-3e19:0:255,255,255:"), (b"OK:0\x02\r", False, 1))
    check("TESTPATTERN CIRCLE 3e19,100: drawn",
          classic("TESTPATTERN:P:CIRCLE:3e19,100:0:255,255,255:"), (b"OK:0\x02\r", False, 1))
    for pos, drawn in (("3e19,5", 0), ("5,-1e19", 0), ("1.9e19,5", 0), ("1e19,5", 1),
                       ("5,-9e18", 1)):
        check("RGB= pos %s" % pos, classic("RGB=RECTANGLE;100,100;0;255,255,255;0,0,0;%s" % pos),
              (b"OK\x02\r", False, drawn))
    # R4-framing-hcfr-long-digit-crash: Perl numifies 5000 digits to Inf, no ValueError
    big = "9" * 5000
    for text, want in (("TESTPATTERN:P:RECTANGLE:100,100:0:%s,1,1:" % big, b"OK:\x02\r"),
                       ("TESTPATTERN:P:RECTANGLE10bit:100,100:0:%s,1,1:" % big, b"OK:\x02\r"),
                       ("RGB=CIRCLE;%s,100;0;%s,1,1;;;" % (big, big), b"OK\x02\r")):
        c = rig.conn(rpc=False)
        xfer(c, b"CMD:GET_RESOLUTION\x02\r")
        check("HCFR %s...: answered, open" % text[:24],
              (xfer(c, text.encode() + b"\x02\r", idle=0.2), closed(c)), (want, False))
        c.close()
    # identity-R4-clientname-zero: `shift || ""` makes the name "0" empty
    for text in ("CLIENTNAME=0", "CLIENTNAME= 0", "SOFTWARE:0"):
        check("%s: invalid client name" % text, classic(text)[:2],
              (b"invalid client name\x02\r", False))
    check("CLIENTNAME= 0 1: accepted", classic("CLIENTNAME=0 1")[:2], (b"OK\x02\r", False))
    rig.close()
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn(rpc=False)
    check("Calman CLIENTNAME=0: ACK", xfer(c, b"CLIENTNAME=0\x03"), ACK)
    check("Calman CLIENTNAME=0: counted as an error", b"errors: 1" in xfer(c, b"STATS\x02\r"), True)
    c.close()
    rig.close()

    # RB4-set-dv-stale-calman-range: CONF_DV sets calman_rgb_quant_range=2 at once
    rig = Rig(fmt=(1920, 1080, 60, 1))
    a, b = rig.conn(), rig.conn()
    cmd(a, "CONF_DV:PERCEPTUAL")
    cmd(b, "DSMD:SDR")
    cmd(b, "RANGE:Limited")
    cmd(a, "CONF_DV:PERCEPTUAL")        # a's snapshot is DV already: no restart
    check("CONF_DV again: sig.calman_range", rig.gen.sig.calman_range, 2)
    cmd(a, "RGB_S:0064,0064,0064,100")
    near("CONF_DV again, RGB_S:0064: read full (no LIMITED source range)",
         grey(last_scene().background), full(64, 1023))
    rig.close()

    # modes-R4-hlg-no-static-metadata: EOTF 3 infoframe carries the conf luminance
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    cmd(c, "INIT:1")
    for mode in ("HDR10", "HLG", "SDR"):
        for t in ("DSMD:" + mode, "MAXL:600", "MAXCLL:500", "RGB_S:0512,0512,0512,010"):
            cmd(c, t)
        with open(rig.backend.plays[-1][1], "rb") as f:
            head = f.read(4096)
        want = mode != "SDR"
        check("%s clip MasteringMetadata" % mode, b"\x55\xd0" in head, want)
        check("%s clip MaxCLL 500" % mode, b"\x55\xbc\x82\x01\xf4" in head, want)
        check("%s clip MaxFALL" % mode, b"\x55\xbd" in head, want)
        if mode == "HLG":
            check("HLG clip: zero primaries", b"\x55\xd1\x88" + bytes(8) in head, True)
            check("HLG clip: max 600", b"\x55\xd9\x88" + __import__("struct").pack(">d", 600.0)
                  in head, True)
    rig.close()


def t_r1fix():
    """Round-1 fix checks (Calman slices), each against the PGenerator source cited."""
    def classic(c, text):
        return xfer(c, text.encode("latin-1") + b"\x02\r")

    def conn_from(rig, ip):
        c = socket.socket()
        c.bind((ip, 0))
        c.connect(("127.0.0.1", rig.port))
        c.settimeout(5)
        return c

    # calman-framing-eof-clears-other-client: the EOF recv resets calman{} (daemon.pm:890)
    rig = Rig(fmt=(1920, 1080, 60, 1))
    a, b = conn_from(rig, "127.0.0.1"), conn_from(rig, "127.0.0.2")
    cmd(a, "SN")
    cmd(b, "SN")
    a.close()
    time.sleep(0.4)
    check("ETX socket EOF keeps another IP's Calman status", rig.srv.client(), ("127.0.0.2", "Calman"))
    b.close()
    time.sleep(0.4)
    check("own EOF clears it (peer IP)", rig.srv.client(), None)
    rig.close()

    # calman-identity-1: SET_DISCOVERABLE:0 lasts across a restart (DISCOVERABLE.disabled)
    tmp = tempfile.mkdtemp(prefix="pg_disc_")
    ffile = os.path.join(tmp, "calman_format")
    rig = Rig(fmt=(1920, 1080, 60, 1))
    rig.srv.stop()
    reply = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    reply.bind(("127.0.0.1", 0))
    reply.settimeout(0.5)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def server():
        srv = upgci.Server(rig.gen, rig.info, rig.log, port=rig.port, rpc_port=rig.rpc_port,
                           discovery=True, discovery_port=rig.disc,
                           discovery_reply_port=reply.getsockname()[1], format_file=ffile)
        srv.start()
        time.sleep(0.2)
        return srv

    def answered():
        u.sendto(b"x", ("127.0.0.1", rig.disc))
        try:
            return bool(reply.recvfrom(100))
        except socket.timeout:
            return False
    srv = server()
    c = rig.conn(rpc=False)
    check("SET_DISCOVERABLE:0", classic(c, "CMD:SET_DISCOVERABLE:0"), b"OK:\x02\r")
    c.close()
    check("not discoverable", answered(), False)
    srv.stop()
    time.sleep(1.2)
    srv = server()
    check("not discoverable after a restart", answered(), False)
    c = rig.conn(rpc=False)
    check("GET_DISCOVERABLE after a restart", classic(c, "CMD:GET_DISCOVERABLE"), b"OK:0\x02\r")
    classic(c, "CMD:SET_DISCOVERABLE:1")
    c.close()
    srv.stop()
    time.sleep(1.2)
    srv = server()
    check("discoverable again after SET_DISCOVERABLE:1 and a restart", answered(), True)
    srv.stop()
    reply.close()
    shutil.rmtree(tmp, ignore_errors=True)

    # calman-modes-1 / crg-2 / calman-charts-dvflag-int: DV flags are int(x || 0) == 1
    for v in ("01", " 1", "1.0", "+1", "1abc"):
        rig = Rig(fmt=(1920, 1080, 60, 1))
        c = rig.conn(rpc=False)
        classic(c, "CMD:SET_PGENERATOR_CONF_DV_STATUS:" + v)
        classic(c, "CMD:SET_PGENERATOR_CONF_MAX_BPC:12")       # command.pm:801-803
        check("DV_STATUS %r: MAX_BPC normalised" % v, rig.cal.conf["max_bpc"], "8")
        classic(c, "RESTARTPGENERATOR:")
        check("DV_STATUS %r: restart normalises to DV" % v,
              (rig.gen.sig.mode, rig.cal.conf["dv_status"], rig.cal.conf["signal_mode"],
               rig.cal.conf["is_std_dovi"]), ("dv", "1", "dv", "1"))
        check("DV_STATUS %r: GET_SETTINGS" % v, settings(c).endswith("Bits=8,Dolby=On\x03"), True)
        c.close()
        rig.close()
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn(rpc=False)
    for k in ("IS_HDR:1", "EOTF:2", "IS_STD_DOVI:1abc"):
        classic(c, "CMD:SET_PGENERATOR_CONF_" + k)
    cmd(c, "SPECIALTY:CONTRAST")
    check("IS_STD_DOVI 1abc: chart in DV (authored bytes)",
          (rig.gen.sig.pattern_mode, {round(232 / 255.0, 9), round(234 / 255.0, 9)}
           <= {grey(r[4]) for r in last_scene().rects}), ("dv", True))
    c.close()
    rig.close()

    # crg-3: Calman's color_format no longer changes how codes are read (0.4.0): a
    # Y'CbCr 4:4:4 request with only the wire range limited reads as on an RGB wire
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn(rpc=False)
    classic(c, "CMD:SET_PGENERATOR_CONF_COLOR_FORMAT:01")
    classic(c, "CMD:SET_PGENERATOR_CONF_RGB_QUANT_RANGE:1")
    classic(c, "RESTARTPGENERATOR:")
    r = rig.conn()
    cmd(r, "RGB_S:0940,0940,0940,100")
    check("COLOR_FORMAT 01 ignored: 940 read as a full-range code",
          (rig.gen.sig.color_format, grey(last_scene().background)),
          ("RGB", round(940 / 1023.0, 9)))
    r.close()
    c.close()
    rig.close()

    # calman-modes-2 / -3: any number of leading zeros (Perl int(), C atoi)
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn(rpc=False)
    z = "0" * 5000
    cmd(c, "PRIM:" + z + "2")
    check("PRIM:000..2", (rig.cal.conf["primaries"], rig.cal.conf["colorimetry"]), ("1", "9"))
    cmd(c, "CONF_FORMAT:" + z + "1080p60")
    s = rig.gen.sig
    check("CONF_FORMAT:000..1080p60", (s.width, s.height, s.fps_num, s.fps_den), (1920, 1080, 60, 1))
    cmd(c, "CONF_FORMAT:%s1280x%s720@50" % (z, z))
    check("CONF_FORMAT:000..1280x000..720@50", (s.width, s.height, s.fps_num), (1280, 720, 50))
    classic(c, "CMD:SET_PGENERATOR_CONF_PRIMARIES:" + z + "2")
    classic(c, "CMD:SET_PGENERATOR_CONF_IS_HDR:1")
    classic(c, "CMD:SET_PGENERATOR_CONF_EOTF:2")
    check("RESTARTPGENERATOR with a 5001-digit primaries", classic(c, "RESTARTPGENERATOR:"),
          b"OK\x02\r")
    check("atoi(000..2) = P3-D65", rig.gen.sig.primaries, 2)
    check("DSMD:HDR10 after it", cmd(c, "DSMD:HDR10"), ACK)
    c.close()
    rig.close()

    # calman-special-keys: a stored CalmanCustomPatternN draws for its exact key (daemon.pm:469-476)
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn(rpc=False)
    cmd(c, "INIT:1")
    cmd(c, "RGB_B:0000,0000,0000,0100")
    bg = rig.cal.bg
    classic(c, "SETCONF:CalmanCustomPattern1:TEMPLATE:DRAW=RECTANGLE\nDIM=200,200\nRGB=255,0,0\n"
               "BG=0,0,255\nPOSITION=0,0\nEND=1\n")
    xfer(c, b"\x02RGB_B:0020,0020,0020,0000\x03")
    sc = last_scene()
    check("CalmanCustomPattern1 drawn", (rgb(sc.background), [(r[:4], rgb(r[4])) for r in sc.rects]),
          ((0.0, 0.0, 1.0), [((0, 0, 200, 200), (1.0, 0.0, 0.0))]))
    check("calman_bg untouched", rig.cal.bg, bg)
    xfer(c, b"RGB_B:0020,0020,0020,0000\x03")     # no STX: not the special key
    check("no STX: the generic RGB_B", [r[:4] for r in last_scene().rects], [(640, 360, 640, 360)])
    cmd(c, "10_SIZE:20")                             # the replay rebuilds STX + key
    check("replay draws the template", [r[:4] for r in last_scene().rects], [(0, 0, 200, 200)])
    # calman-patterndynamic-template-ignored: a stored PatternDynamic decides the geometry
    classic(c, "SETCONF:PatternDynamic:TEMPLATE:DRAW=RECTANGLE\nDIM=100,100\nRGB=DYNAMIC\n"
               "BG=DYNAMIC\nPOSITION=0,0\nEND=1\n")
    cmd(c, "RGB_B:0512,0512,0512,0000")
    check("PatternDynamic stored: RGB_B", [(r[:4], grey(r[4])) for r in last_scene().rects],
          [((0, 0, 100, 100), round(512 / 1023.0, 9))])
    cmd(c, "RGB_X:1023,0000,0000")
    check("PatternDynamic stored: RGB_X", [(r[:4], rgb(r[4])) for r in last_scene().rects],
          [((0, 0, 100, 100), (1.0, 0.0, 0.0))])
    c.close()
    rig.close()

    # calman-nan-code-drawn-white / calman-nan-window-poisons-win-size (daemon.pm:76-93, 486-520)
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn(rpc=False)
    cmd(c, "INIT:1")
    cmd(c, "RGB_S:nan,0512,0512,100")
    check("RGB_S NaN code is 0", rgb(last_scene().background), (0.0, round(512 / 1023.0, 9),
                                                                round(512 / 1023.0, 9)))
    cmd(c, "CommandRGB:-nan,0,0,1,100")
    check("CommandRGB -NaN code is 0", rgb(last_scene().background), (0.0, 0.0, 0.0))
    cmd(c, "10_SIZE:25")
    for t in ("RGB_S:0512,0512,0512,nan", "RGB_A:0512,0512,0512,nan", "10_SIZE:nan"):
        cmd(c, t)
        check("%s keeps the window size" % t, rig.cal.win, 25)
    cmd(c, "11_APL:30")
    cmd(c, "11_APL:nan")
    check("11_APL:nan keeps the APL", rig.cal.apl, 30.0)
    c.close()
    rig.close()



def t_r2fix():
    """Round-2 parity fixes on the Calman side (daemon.pm / command.pm / pattern.pm)."""
    def classic(c, text):
        return xfer(c, text.encode("latin-1") + b"\x02\r")

    def conf_get(c, k):
        r = classic(c, "CMD:GET_PGENERATOR_CONF_" + k)
        return r[3:-2].decode("latin-1") if r.startswith(b"OK:") else r

    def lastbar(sc):
        py, ph = int(sc.height * 0.15), int(sc.height * 0.52)
        b = [grey(r[4]) for r in sc.rects if r[1] == py + int(ph * 0.10)]
        return b[-1] if b else None
    i1080p24 = upgci.find_mode(1920, 1080, "p", 24)
    # calman-modes-r2-1 / crg-r2-1: ($conf || "2") + 0 keeps 1.5 (daemon.pm:270); ==1 is false,
    # and apply_source's /^[12]$/ makes it 2 (command.pm:62)
    rig = Rig(fmt=(1920, 1080, 24, 1))
    c = rig.conn(rpc=False)
    classic(c, "CMD:SET_PGENERATOR_CONF_RGB_QUANT_RANGE:1.5")
    cmd(c, "INIT:1")
    check("r2-1 INIT: range 1.5", rig.cal.range, 1.5)
    cmd(c, "RGB_S:0064,0064,0064,100")
    check("r2-1 RGB_S code 64 read full", grey(last_scene().background), full(64, 1023))
    cmd(c, "CONF_DV:x")
    check("r2-1 stray range line: conf 2", rig.cal.conf["rgb_quant_range"], "2")
    check("r2-1 GET_SETTINGS Range=Full", "Range=Full" in settings(c), True)
    c.close()
    rig.close()
    # crg-r2-2: the renderer reads rgb_quant_range with atoi (main.cpp:123,143)
    for v, want in (("1e1", 0.0), ("0.1e1", full(64, 1023)), ("1", 0.0), ("2", full(64, 1023))):
        rig = Rig(fmt=(1920, 1080, 24, 1))
        c = rig.conn(rpc=False)
        classic(c, "CMD:SET_PGENERATOR_CONF_RGB_QUANT_RANGE:" + v)
        classic(c, "RESTARTPGENERATOR:")
        for k in ("BITD:10", "QRNG:LIMITED", "RGB_S:0064,0064,0064,100"):
            cmd(c, k)
        check("crg-r2-2 rgb_quant_range %s: code 64" % v, grey(last_scene().background), want)
        c.close()
        rig.close()
    # crg-r2-3: MAXL only saves (daemon.pm:2058-2089): the range and its owner stay, nothing
    # is drawn over PatternStart (the picture shown is re-encoded)
    rig = Rig(fmt=(1920, 1080, 24, 1))
    c = rig.conn(rpc=False)
    for k in ("INIT:1.2", "DSMD:HDR10", "QRNG:LIMITED", "CommandRGB:512,512,512,1,100"):
        cmd(c, k)
    classic(c, "CMD:SET_PGENERATOR_CONF_RGB_QUANT_RANGE:2")
    classic(c, "RESTARTPGENERATOR:")
    owner = rig.cal.range_owner
    cmd(c, "MAXL:500")
    check("crg-r2-3 MAXL keeps the range", (rig.cal.conf["rgb_quant_range"], rig.cal.range_owner,
                                            "Range=Full" in settings(c)), ("2", owner, True))
    check("crg-r2-3 PatternStart still shown (re-encoded)", lit(last_scene()), [])
    check("crg-r2-3 MAXL stored", rig.cal.conf["max_luma"], "500")
    c.close()
    rig.close()
    # calman-modes-r2-2: INIT restores the conf calman_mode_idx (daemon.pm:308-321), CONF_FORMAT
    # writes it (2268-2269)
    tmp = tempfile.mkdtemp(prefix="pg_cmi_")
    ffile = os.path.join(tmp, "calman_format")
    rig = Rig(fmt=(1920, 1080, 24, 1))
    rig.srv.stop()
    rig.srv = upgci.Server(rig.gen, rig.info, rig.log, port=rig.port, rpc_port=rig.rpc_port,
                           discovery=False, format_file=ffile)
    rig.srv.start()
    rig.cal = rig.srv.calman
    time.sleep(0.2)
    c = rig.conn(rpc=False)
    check("r2-2 no calman_mode_idx before INIT", conf_get(c, "CALMAN_MODE_IDX"), "None")
    cmd(c, "INIT:1")
    check("r2-2 INIT writes 1080p24's index", conf_get(c, "CALMAN_MODE_IDX"), str(i1080p24))
    cmd(c, "CONF_FORMAT:2160p24")
    check("r2-2 CONF_FORMAT writes it", conf_get(c, "CALMAN_MODE_IDX"), rig.cal.conf["mode_idx"])
    i720 = upgci.mode_index((1280, 720, (60, 1)))
    classic(c, "CMD:SET_PGENERATOR_CONF_CALMAN_MODE_IDX:%d" % i720)
    cmd(c, "INIT:1")
    check("r2-2 INIT takes the client's calman_mode_idx",
          (rig.gen.sig.width, rig.gen.sig.height, rig.gen.sig.fps_num, rig.cal.conf["mode_idx"]),
          (1280, 720, 60, str(i720)))
    c.close()
    rig.srv.stop()
    time.sleep(1.2)
    rig.srv = upgci.Server(rig.gen, rig.info, rig.log, port=rig.port, rpc_port=rig.rpc_port,
                           discovery=False, format_file=ffile)
    rig.srv.start()
    rig.cal = rig.srv.calman
    time.sleep(0.2)
    c = rig.conn(rpc=False)
    check("r2-2 calman_mode_idx lasts (PGenerator.conf)", conf_get(c, "CALMAN_MODE_IDX"), str(i720))
    classic(c, "CMD:SET_PGENERATOR_CONF_CALMAN_MODE_IDX:abc")
    cmd(c, "INIT:1")
    check("r2-2 abc: 1080p24 restored and written",
          (rig.gen.sig.width, rig.gen.sig.fps_num, rig.cal.conf["calman_mode_idx"]),
          (1920, 24, str(i1080p24)))
    # calman-modes-r2-3: CONF_FORMAT keeps the scan (daemon.pm:2214-2219, 2243-2244)
    cmd(c, "CONF_FORMAT:1080i60")
    i = upgci.mode_index((1920, 1080, (60, 1)), "i")
    check("r2-3 1080i60 stored interlaced", (rig.cal.conf["mode_idx"], conf_get(c, "CALMAN_MODE_IDX")),
          (str(i), str(i)))
    check("r2-3 GET_MODE", classic(c, "CMD:GET_MODE"),
          b"OK:%d[1920x1080i 60.00Hz 74.25MHz phsyncpvsync]\x02\r" % i)
    check("r2-3 GET_HDMI_INFO interlaced", classic(c, "CMD:GET_HDMI_INFO").endswith(
        b"1920x1080 @ 60.00Hz, interlaced\x02\r"), True)
    cmd(c, "CONF_FORMAT:576i50")
    check("r2-3 576i50", rig.cal.conf["mode_idx"], str(upgci.mode_index((720, 576, (50, 1)), "i")))
    # calman-modes-r2-4: int() of a decimal string is exact within IV/UV
    cmd(c, "CONF_HDR:ST2084,0.708,0.292,0.170,0.797,0.131,0.046,0.3127,0.3290,0.0050,"
           "12345678901234567,9007199254740993,004002.2000")
    check("r2-4 CONF_HDR big integers", (conf_get(c, "MAX_LUMA"), conf_get(c, "MAX_CLL")),
          ("12345678901234567", "9007199254740993"))
    c.close()
    rig.close()
    shutil.rmtree(tmp, ignore_errors=True)
    # calman-patterndynamic-forced-bits10: a stored PatternDynamic with its own BITS/values gets
    # PGenerator's file: codes scaled to the pattern depth (daemon.pm:483, 537)
    for tpl, key, fg, bg in (
            ("RGB=DYNAMIC\nBG=DYNAMIC\nBITS=8", "RGB_B:0512,0512,0512,0400",
             (full(128, 255),) * 3, full(100, 255)),
            ("RGB=255,0,0\nBG=DYNAMIC", "RGB_B:0512,0512,0512,0000", (1.0, 0.0, 0.0), 0.0),
            ("RGB=DYNAMIC\nBG=DYNAMIC", "RGB_B:0513,0513,0513,0000", (full(513, 1023),) * 3, 0.0)):
        rig = Rig(fmt=(1920, 1080, 60, 1))
        c = rig.conn(rpc=False)
        cmd(c, "INIT:1")
        classic(c, "SETCONF:PatternDynamic:TEMPLATE:DRAW=RECTANGLE\nDIM=100,100\n%s\n"
                   "POSITION=0,0\nEND=1\n" % tpl)
        cmd(c, key)
        sc = last_scene()
        check("PatternDynamic %r %s" % (tpl, key), ([(r[:4], rgb(r[4])) for r in sc.rects],
                                                 grey(sc.background)),
              ([((0, 0, 100, 100), fg)], bg))
        c.close()
        rig.close()
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn(rpc=False)
    cmd(c, "INIT:1")
    cmd(c, "DSMD:DOLBYVISION")
    classic(c, "SETCONF:PatternDynamic:TEMPLATE:DRAW=RECTANGLE\nDIM=100,100\nRGB=DYNAMIC\n"
               "BG=2048,2048,2048\nPOSITION=0,0\nEND=1\n")
    cmd(c, "RGB_B:1023,1023,1023,0000")
    check("PatternDynamic DV: literal BG at SOURCE_MAX=4095", round(last_scene().background.r, 4),
          round(dvl(2048, 4095), 4))
    c.close()
    rig.close()
    # calman-clean-pattern-files: every draw's clean_pattern_files unlinks /.jpg$/ and /.png$/
    # from the template directory (pattern.pm:740-746)
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn(rpc=False)
    rect = "DRAW=RECTANGLE\nDIM=100,100\nRGB=255,0,0\nEND=1\n"
    for n in ("grey.png", "Xjpg", "keep"):
        classic(c, "SETCONF:%s:TEMPLATE:PERMANENT=yes\n%s" % (n, rect))
    cmd(c, "CommandRGB:128,128,128,0,50")
    check("clean_pattern_files after CommandRGB",
          [classic(c, "GETCONF:%s:DIM" % n) for n in ("grey.png", "Xjpg", "keep")],
          [b"OK:\x02\r", b"OK:\x02\r", b"OK:100,100\x02\r"])
    c.close()
    rig.close()
    # calman-charts-maxl-inf-ignored: +-Inf is stored; webui_pattern_max_luma clamps it
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    cmd(c, "DSMD:HDR10")
    for v, peak in (("inf", 10000), ("Infinity", 10000), ("1e999", 10000), ("-inf", 1000),
                    ("abc", 1000), ("20000", 10000)):
        cmd(c, "MAXL:4000")
        cmd(c, "SPECIALTY:ALIGNMENT")
        cmd(c, "MAXL:" + v)
        cmd(c, "SPECIALTY:CONTRAST")
        check("MAXL:%s: conf and chart peak" % v, (rig.cal.conf["max_luma"], lastbar(last_scene())),
              (v, r10full(pq(peak))))
    cmd(c, "CONF_HDR:ST2084,0.708,0.292,0.170,0.797,0.131,0.046,0.3127,0.3290,0.005,inf,1000,00400")
    cmd(c, "SPECIALTY:ALIGNMENT")
    cmd(c, "SPECIALTY:CONTRAST")
    check("CONF_HDR maxL inf", (rig.cal.conf["max_luma"], lastbar(last_scene())), ("Inf", 1.0))
    c.close()
    rig.close()
    # shapes-calman-draws-skip-renderer-state: a Calman pattern file sets the renderer's values
    rig = Rig(fmt=(1280, 720, 60, 1))
    c = rig.conn(rpc=False)
    cmd(c, "RGB_B:1023,0000,0000,0512")
    classic(c, "RGB=RECTANGLE;100,100;0;0,255,0;-1,-1,-1;0,0;")
    check("RGB= BG=-1 after RGB_B: Calman's background", grey(last_scene().background),
          full(512, 1023))
    cmd(c, "RGB_S:1023,0000,0000,010")
    classic(c, "SETCONF:NORGB:TEMPLATE:DRAW=RECTANGLE\nDIM=100,100\nPOSITION=0,0\nEND=1\n")
    classic(c, "TESTTEMPLATE:NORGB:1,1,1")
    sc = last_scene()
    check("template without RGB/BG after RGB_S: RGB_S's colours at BITS=8",
          ([(r[:4], rgb(r[4])) for r in sc.rects], grey(sc.background)),
          ([((0, 0, 100, 100), (1.0, 0.0, 0.0))], full(128, 255)))
    c.close()
    rig.close()

def t_r3fix():
    """Round-3 parity fixes on the Calman side."""
    def classic(c, text):
        return xfer(c, text.encode("latin-1") + b"\x02\r")
    # calman-modes-r3-1: CONF_FORMAT's default rate is $preferred_mode's, the conf mode_idx's
    # mode that GET_MODE reports (daemon.pm:2195, command.pm:1385-1390), not the one shown
    rig = Rig(fmt=(1920, 1080, 24000, 1001))
    c = rig.conn(rpc=False)
    cmd(c, "INIT:1")
    i60 = upgci.find_mode(3840, 2160, "p", 60)
    check("r3 SET MODE_IDX", classic(c, "CMD:SET_PGENERATOR_CONF_MODE_IDX:%d" % i60), b"OK:\x02\r")
    check("r3 GET_MODE", classic(c, "CMD:GET_MODE").startswith(b"OK:%d[3840x2160 60." % i60), True)
    cmd(c, "CONF_FORMAT:2160p")
    check("r3 CONF_FORMAT:2160p takes GET_MODE's 60 Hz", rig.cal.conf["mode_idx"], str(i60))
    c.close()
    rig.close()
    # crg-r3-1: the renderer's isHDR is atoi(is_hdr) as a truth value (main.cpp:150,
    # ofxRPI4Window.cpp:1550); normalize_signal_mode_conf's int()==1 only writes signal_mode
    for v, want in (("1", ("hdr10", "hdr10")), ("2", ("hdr10", "sdr")), ("-1", ("hdr10", "sdr")),
                    ("0", ("sdr", "sdr")), ("x", ("sdr", "sdr"))):
        rig = Rig(fmt=(1920, 1080, 24, 1))
        c = rig.conn(rpc=False)
        classic(c, "CMD:SET_PGENERATOR_CONF_EOTF:2")
        classic(c, "CMD:SET_PGENERATOR_CONF_IS_HDR:" + v)
        classic(c, "CMD:SET_PGENERATOR_CONF_PRIMARIES:1")
        classic(c, "RESTARTPGENERATOR:")
        check("r3 is_hdr %r: signal, signal_mode" % v,
              (rig.cal.gen.sig.mode, rig.cal.conf["signal_mode"]), want)
        c.close()
        rig.close()
    # crg-r3-2: drm_override.c read_config takes the leading ASCII digits of colorimetry only
    for v, want in (("9", "bt2020"), ("10", "bt2020"), ("+9", "bt709"), (" 9", "bt709"),
                    ("1e1", "bt709"), ("0x9", "bt709"), ("9x", "bt2020")):
        rig = Rig(fmt=(1920, 1080, 24, 1))
        c = rig.conn(rpc=False)
        classic(c, "CMD:SET_PGENERATOR_CONF_COLORIMETRY:" + v)
        classic(c, "RESTARTPGENERATOR:")
        check("r3 colorimetry %r" % v, rig.cal.gen.sig.colorimetry, want)
        c.close()
        rig.close()
    # calman-patterndynamic-bits-fallback-ignored: BITS=DYNAMIC||10 takes 10 when daemon.pm's
    # 2-field payload leaves $bits empty (pattern.pm:603-607): target codes read at 10 bits
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    rig.cal.templates.dirs["tmp"]["PatternDynamic"] = (
        "DRAW=RECTANGLE\nDIM=DYNAMIC\nRGB=DYNAMIC\nBG=DYNAMIC\nPOSITION=DYNAMIC\n"
        "BITS=DYNAMIC||10\nEND=1\nFRAME=DYNAMIC\n")
    for k, want in (("RGB_B:1023,1023,1023,0000", full(255, 1023)),
                    ("RGB_X:0512,0512,0512", full(128, 1023))):
        cmd(c, k)
        check("r3 BITS=DYNAMIC||10 %s" % k, [grey(r[4]) for r in lit(last_scene())], [want])
    # calman-patterndynamic-rgb-text-own-depth: $RGB in TEXT gets daemon.pm's target codes
    rig.cal.templates.dirs["tmp"]["PatternDynamic"] = (
        "DRAW=TEXT\nDIM=40,40\nRGB=DYNAMIC\nBG=DYNAMIC\nPOSITION=10,10\nBITS=DYNAMIC\n"
        "TEXT=Code $RGB\nEND=1\nFRAME=DYNAMIC\n")
    got = []
    orig = rig.cal.engine.get_pattern
    rig.cal.engine.get_pattern = lambda *a, **k: got.append(orig(*a, **k)) or got[-1]
    cmd(c, "RGB_X:0512,0512,0512")
    check("r3 TEXT $RGB at the target depth", [ln for ln in (got[-1][1] or "").split("\n")
                                              if ln.startswith(("TEXT=", "RGB="))],
          ["RGB=128,128,128", "TEXT=Code 128,128,128"])
    c.close()
    rig.close()


for t in (t_discovery, t_identity, t_queries, t_patterns, t_framing, t_terminate, t_range,
          t_modes, t_specialty, t_gci, t_classic, t_http, t_upload_buffer, t_numbers, t_ascii,
          t_latch, t_concurrency, t_hcfr_close, t_conf_format_modes, t_prim_atoi,
          t_conf_mirror, t_maxl_nan, t_round3, t_round4, t_r1fix, t_r2fix, t_r3fix):
    n = len(FAILS)
    try:
        t()
    except Exception as exc:
        import traceback
        FAILS.append("%s crashed: %r\n%s" % (t.__name__, exc, traceback.format_exc()[-600:]))
    print("%-16s %s" % (t.__name__, "ok" if len(FAILS) == n else "%d FAIL" % (len(FAILS) - n)))
print("\n".join(FAILS) if FAILS else "G1 parity OK")
sys.exit(1 if FAILS else 0)
