"""Calman control, behaving as PGenerator 2.12.1 does (daemon.pm).

Two TCP ports: 2100 (UPGCI / GCI plugin) and 2101 (RPC, used by Calman's
"Portrait Displays G1" source), plus UDP discovery (3529, answered on the
caller's port 3530). The same daemon serves the classic protocol on TCP 85
(HCFR, DeviceControl: templates, shapes, CMD: getters and setters), answers
DeviceControl discovery on UDP 1977 and runs the LightSpace client (lightspace.py).
Each 2101 recv() is one command; 2100 and 85 gather data until STX CR or ETX. Only the first STX CR or ETX of a command is removed, then
trailing CR/LF. Commands get ACK (0x06); queries get their text (+ ETX on 2100).

The state is global, as in PGenerator's single daemon: Calman keeps a mirror
of the PGenerator.conf keys it writes (with PGenerator's "dirty" semantics:
a changed key redraws at the next apply), the pattern state (window, APL,
background, last pattern) and the source range. What PGenerator would switch
on the HDMI link (mode, format, range) only changes how pattern codes are read
and what is encoded; the link itself is Kodi's. Codes become E' from their own
depth (never re-quantised to max_bpc, which is only reported): see README.md,
"Deviations from PGenerator".
"""

import base64
import math
import os
import re
import socket
import struct
import threading
import time
import urllib.parse

from . import shapes, sysinfo, templates
from .perl import c_atoi, dec_int, lead_digits, perl_int, perl_num, perl_str, perl_uc, split_perl
from .patterns import (Code, Colour, Scene, _level, calman_range, dynamic_scene, patch_scene,
                       reads_limited, specialty_scene)

ACK, ETX = b"\x06", b"\x03"
NAK = b"\x15"     # a pattern that was not displayed: never ACKed (the add-on's own)
CLASSIC_END = b"\x02\r"
RECV_SIZE = 1024

VERSION = "2.12.1"
NAME = "PGenerator+"
CAPS = ("HDR,DOLBYVISION,CONF_FORMAT,CONF_HDR,SIZE,10_SIZE,11_APL,"
        "CommandRGB,BITDEPTH,COLORSPACE,RANGE")
STATUS_CAPS = ("STATUS,CONF_FORMAT,CONF_HDR,CONF_LEVEL,CONF_DV,HDR_ENABLE,IMAGE,PUSH,"
               "RGB_S,RGB_B,RGB_A,CommandRGB,10_SIZE,11_APL,SPECIALTY,UPDATE,"
               "YCC_A,YCC_B,YCC_S")
ACKED = ("ENABLE PATTERNS", "ENABLEPATTERNS", "DISABLE PATTERNS", "DISABLEPATTERNS",
         "UPDATE", "IS_ALIVE", "ISALIVE")

STD_WIDTH = {2160: 3840, 1080: 1920, 720: 1280, 576: 720, 480: 720}
FRACTIONAL = {23.976: (24000, 1001), 23.98: (24000, 1001), 29.97: (30000, 1001),
              59.94: (60000, 1001), 47.952: (48000, 1001), 119.88: (120000, 1001)}
PERL_WS = " \t\n\r\f\v"


def _tv_modes():
    """The connector's mode list CONF_FORMAT picks from (daemon.pm:2232-2262), taken as a 4K
    TV's usual CTA-861 set (the add-on cannot read the TV's), in drm_mode_sort order:
    preferred (2160p60) first, then larger area, rounded refresh, higher pixel clock."""
    hi = ((60, 1), (60000, 1001), (50, 1), (48, 1), (48000, 1001), (30, 1), (30000, 1001),
          (25, 1), (24, 1), (24000, 1001))
    sets = [(4096, 2160, "p", hi), (3840, 2160, "p", hi), (2560, 1440, "p", ((60, 1),)),
            (1920, 1080, "p", ((120, 1), (120000, 1001), (100, 1)) + hi),
            (1920, 1080, "i", hi[:3]), (1280, 720, "p", hi[:3]), (720, 576, "p", ((50, 1),)),
            (720, 576, "i", ((50, 1),)), (720, 480, "p", hi[:2]), (720, 480, "i", hi[:2]),
            (640, 480, "p", hi[:2])]
    modes = [(w, h, scan, float("%.2f" % (n / float(d))), (n, d))
             for w, h, scan, rates in sets for n, d in rates]
    return sorted(modes, key=lambda m: ((m[0], m[1], m[2], m[4]) != (3840, 2160, "p", (60, 1)),
                                        -m[0] * m[1], -round(m[3]), -m[4][0] / m[4][1]))


TV_MODES = _tv_modes()


def _clock_mhz(w, h, scan, fps):
    """The CTA-861 pixel clock of a TV_MODES entry (fractional rates: /1.001)."""
    n, d = fps
    rate = n / float(d)
    base = {(640, 480): 25.2, (720, 480): 27.027 if scan == "p" else 13.514,
            (720, 576): 27.0 if scan == "p" else 13.5, (1280, 720): 74.25,
            (2560, 1440): 241.5}.get((w, h))
    if base is None and h == 2160:
        base = 594.0 if rate > 47 else 297.0
    elif base is None:                  # 1080 lines
        base = 74.25 if scan == "i" or rate < 47 else 297.0 if rate > 99 else 148.5
    return base / 1.001 if d == 1001 else base


def mode_line(idx):
    """A connector mode as get_hdmi_info lists it: idx[WxH R.RRHz C.CCMHz flags]."""
    if idx is None:
        return ""
    w, h, scan, _, fps = TV_MODES[idx]
    flags = ("nhsyncnvsync" if h <= 576 else "phsyncnvsync" if (w, h) == (2560, 1440)
             else "phsyncpvsync")
    return "%d[%dx%d%s %.2fHz %.2fMHz %s]" % (idx, w, h, "i" if scan == "i" else "",
                                             fps[0] / float(fps[1]),
                                             _clock_mhz(w, h, scan, fps), flags)


def mode_index(fmt, scan="p"):
    """The TV_MODES index of (w, h, (num, den)) with that scan."""
    for i, (w, h, sc, _, fps) in enumerate(TV_MODES):
        if (w, h, fps) == (fmt[0], fmt[1], tuple(fmt[2])) and sc == scan:
            return i
    return None


def find_mode(w, h, scan, rate):
    """calman_find_mode_idx / CONF_FORMAT's modetest scan (daemon.pm:275-306, 2232-2262):
    same height and scan, int(rate) equal or within 0.25 Hz, exact width preferred, the
    first listed wins -> the TV_MODES index, or None."""
    best, score = None, 0
    for i, (mw, mh, mscan, mrate, _mfps) in enumerate(TV_MODES):
        if mh != h or mscan != scan or not (int(mrate) == int(rate) or abs(mrate - rate) < 0.25):
            continue
        sc = (100 if w and mw == w else 0) + (5 if i == 0 else 0) + 1     # 0: preferred
        if sc > score:
            best, score = i, sc
    return best


def mode_by_index(text):
    """The renderer's mode for a conf mode_idx (main.cpp:160 atoi; "" is auto_select_4k_mode's
    2160p30, command.pm:23-44) -> (w, h, fps) (an interlaced mode is played as progressive
    frames at its rate), None past the list (PGenerator's renderer indexes out of bounds:
    kept)."""
    if (text or "") == "":
        return mode_by_index(str(mode_index((3840, 2160, (30, 1)))))
    i = c_atoi(text)
    if not 0 <= i < len(TV_MODES):
        return None
    w, h, scan, _, fps = TV_MODES[i]
    return (w, h, fps)


def conf_mode_index(value):
    """get_hdmi_info's selected mode: the modetest line whose index == the conf mode_idx
    (numeric ==, command.pm:1373), None when "" or no line matches."""
    if (value or "") == "":
        return None
    n = perl_num(value)
    return int(n) if n == n and 0 <= n < len(TV_MODES) and n == int(n) else None


def modes_available():
    """GET_MODES_AVAILABLE before base64: rows grouped by width, widest first."""
    rows: dict = {}
    for i, (w, *_) in enumerate(TV_MODES):
        rows.setdefault(w, []).append(i)
    out = []
    for w in sorted(rows, reverse=True):
        for i in rows[w]:
            mw, mh, scan, _, fps = TV_MODES[i]
            flags = ("nhsyncnvsync" if mh <= 576 else "phsyncnvsync" if (mw, mh) == (2560, 1440)
                     else "phsyncpvsync")
            out.append("%d[%dx%d%s %.2fHz %.2fMHz %s]" % (i, mw, mh, "i" if scan == "i" else "",
                                                         fps[0] / float(fps[1]),
                                                         _clock_mhz(mw, mh, scan, fps), flags))
    return "\n".join(out)

# PGenerator.conf as shipped (mode_idx: the index of the add-on's start-up mode)
STOCK_CONF = {"ip_pattern": "0.0.0.0", "port_pattern": "85", "rgb_quant_range": "2", "dv_color_space": "0", "dv_metadata": "0",
              "dv_transport": "standard", "eotf": "0", "max_cll": "1000", "max_fall": "400",
              "max_luma": "1000", "min_luma": "0.005", "color_format": "0", "colorimetry": "2",
              "dv_interface": "0", "dv_map_mode": "2", "dv_status": "0", "is_hdr": "0",
              "is_ll_dovi": "0", "is_sdr": "1", "is_std_dovi": "0", "max_bpc": "8",
              "primaries": "0"}
# keys a GCI (INIT:2.0) connection may write
LUMINANCE_KEYS = ("max_luma", "min_luma", "max_cll", "max_fall")
GCI_KEYS = {"primaries", "eotf", "is_hdr", "color_format", "max_bpc", "min_luma", "max_luma",
            "max_cll", "max_fall", "rgb_quant_range", "dv_map_mode", "dv_metadata"}
FORMATS = {"0": "RGB", "1": "YCC444", "2": "YCC422", "3": "YCC420"}
# clean_files keeps these templates, and those whose first line is PERMANENT=yes
KEPT_TEMPLATES = ("PatternDynamic", "MeterProfile", "MeterPosition", "ScreenSaver",
                  "PatternStart", "CalmanCustomPattern1", "CalmanCustomPattern2",
                  "CalmanCustomPattern3", "CalmanCustomPattern4")
# variables.pm:303-306: these exact keys draw the CalmanCustomPattern templates when stored
CALMAN_SPECIAL = {"\x02RGB_B:0020,0020,0020,0000": "CalmanCustomPattern1",
                  "\x02RGB_B:1000,1000,1000,1020": "CalmanCustomPattern2",
                  "\x02RGB_S:0940,0940,0940,018": "CalmanCustomPattern3",
                  "\x02RGB_S:0064,0064,0064,018": "CalmanCustomPattern4"}
INFO_PERIOD = 5.0               # $sleep_info: the device_info thread's cycle (info.pm); 0: none
# calman_mode_key_snapshot: a change of one of these restarts the renderer at the next apply
MODE_KEYS = ("mode_idx", "signal_mode", "is_hdr", "is_sdr", "eotf", "colorimetry", "color_format",
             "max_bpc", "dv_status", "is_ll_dovi", "is_std_dovi", "dv_map_mode")


# -- Perl semantics (perl.py has the parsers) -------------------------------------------
def _finite(v, lim=1e15):
    """A stored number as the renderer can use it (NaN 0, +-Inf saturated)."""
    return 0 if v != v else max(-lim, min(lim, v))


def _or(v, default):
    """Perl's `$v || default`: "" and "0" are false."""
    return v if v not in (None, "", "0") else default


def dv_flag(v):
    """int($v || 0) == 1: "01", " 1", "1.0" and "1abc" are 1 as well."""
    return perl_int(_or(v, "0")) == 1


def perl_decode_base64(s):
    """MIME::Base64 decode_base64: characters outside the alphabet are skipped, the first
    '=' ends the data, and a lone trailing character is dropped."""
    s = re.sub(r"[^A-Za-z0-9+/]", "", s.split("=", 1)[0])
    s = s[:len(s) - len(s) % 4] if len(s) % 4 == 1 else s
    return base64.b64decode(s + "=" * (-len(s) % 4))


def dv_active(c):
    """legacy_external_dv_active / normalize_signal_mode_conf's test."""
    return any(dv_flag(c.get(k)) for k in ("dv_status", "is_ll_dovi", "is_std_dovi"))


def hcfr_draw(draw):
    """legacy_external_hcfr_draw."""
    if re.search(r"\d+bit$", draw, re.I | re.A):
        return draw
    up = perl_uc(draw)
    return up + "8bit" if up in ("RECTANGLE", "CIRCLE", "TRIANGLE", "TEXT", "IMAGE") else draw


def _hcfr_triplet_range(triplet, allow_full):
    el = split_perl(triplet)
    if len(el) != 3:
        return ""
    anchor = False
    for v in el:
        n = float(v) if re.match(r"-?\d+$", v, re.A) else -1.0   # Perl numifies, never dies
        if n < 0:
            return ""
        if allow_full and (n > 255 or n < 16 or n > 235):
            return "2"
        anchor = anchor or n in (16, 235)
    return "1" if anchor else ""


def hcfr_source_range(payload, conf):
    """legacy_external_hcfr_source_range: LIMITED when the codes anchor on 16/235."""
    if dv_active(conf):
        return ""
    f = payload.split(";") + ["", ""]
    first = _hcfr_triplet_range(f[0], True)
    if first == "2":
        return ""
    return "LIMITED" if "1" in (first, _hcfr_triplet_range(f[1], False)) else ""


def _field(el, i):
    return el[i] if i < len(el) else ""


def fps_fraction(rate):
    rate = float(rate)
    for k, v in FRACTIONAL.items():
        if abs(rate - k) < 0.01:
            return v
    if rate.is_integer() and int(rate) + 1 in (24, 30, 48, 60, 120):
        # PGenerator matches an HDMI mode by int(rate): 59 is the 59.94 mode
        return (int(rate) + 1) * 1000, 1001
    return int(round(rate)), 1


def valid_bpc(value):
    v = perl_int(value) if value not in (None, "") else 0
    return str(v) if v in (8, 10, 12) else ""


def choose_bpc(explicit, current, fallback, preserve):
    for v, use in ((explicit, True), (current, preserve), (fallback, True)):
        if use and valid_bpc(v):
            return valid_bpc(v)
    return "10"


def parse_source_format(payload, default_bpc=0):
    """calman_parse_source_format_payload -> (color_format, max_bpc, range), "" = absent."""
    cf = bpc = rng = ""
    m = (re.search(r"(?:^|[,;])\s*(?:1_)?FORMAT\s*=\s*([^,;]+)", payload, re.I | re.A)
         or re.search(r"(?:^|[,;])\s*Color\s*Format\s*=\s*([^,;]+)", payload, re.I | re.A))
    if m:
        text = m.group(1)
    elif "=" not in payload:
        text = re.sub(r"^\s*Format\s+", "", payload, count=1, flags=re.I | re.A)
    else:
        text = ""
    text = text.strip(PERL_WS)
    if text:
        norm = re.sub(r"[^A-Z0-9]", "", perl_uc(text))
        for pat, code in (("(?:YCBCR|YCC|YUV)444", "1"), ("(?:YCBCR|YCC|YUV)422", "2"),
                          ("(?:YCBCR|YCC|YUV)420", "3"), ("RGB", "0")):
            if re.search(pat, norm):
                cf = code
                break
        m = (re.search(r"(?:^|[^0-9])(8|10|12)\s*(?:[-_\s])*(?:bit|bpc)\b", text, re.I | re.A)
             or re.search(r"(?:RGB|YCC|YCBCR|YUV)[A-Z0-9:_\s-]*?(8|10|12)\s*$", text, re.I | re.A))
        if m:
            bpc = valid_bpc(m.group(1))
    m = (re.search(r"(?:^|[,;])\s*Bits?\s*=\s*(8|10|12)\b", payload, re.I | re.A)
         or re.search(r"(?:^|\s)Bits?\s+(8|10|12)\b", payload, re.I | re.A))
    if m:
        bpc = valid_bpc(m.group(1))
    if default_bpc and cf and not bpc:
        bpc = "8"
    m = (re.search(r"(?:^|[,;])\s*Range\s*=\s*([^,;]+)", payload, re.I | re.A)
         or re.search(r"(?:^|\s)Range\s+(.+)$", payload, re.I | re.A))
    if m:
        t = m.group(1).strip(PERL_WS)
        if re.search("full|pc", t, re.I | re.A):
            rng = "2"
        elif re.search("limit|video|smpte", t, re.I | re.A):
            rng = "1"
        elif re.search("default", t, re.I | re.A):
            rng = "0"
    return cf, bpc, rng


def parse_conf_format(fmt, fps):
    """CONF_FORMAT -> the TV_MODES index PGenerator switches to (interlaced for "1080i60"),
    or None (not understood, or no mode matches). fps is the current
    (num, den): int() of it is the rate when none is given above 1080 lines."""
    w = h = None
    scan, r = "p", ""
    m = re.search(r"Resolution\s*=\s*(\d+)\s*x\s*(\d+).*Refresh\s*=\s*([\d.]+)", fmt, re.I | re.A)
    m2 = re.search(r"Resolution\s*=\s*(\d+)\s*([pi])?.*Refresh\s*=\s*([\d.]+)", fmt, re.I | re.A)
    m3 = re.match(r"(\d+)\s*x\s*(\d+)\s*([pi])?\s*(?:@|/|\s)*\s*([\d.]+)?\s*(?:Hz)?", fmt, re.I | re.A)
    m4 = re.match(r"(\d+)\s*([pi])\s*(?:@|/|\s)*\s*([\d.]+)?\s*(?:Hz)?", fmt, re.I | re.A)
    m5 = re.match(r"(\d+)\s*(?:@|/|\s)*\s*([\d.]+)?\s*(?:Hz)?$", fmt, re.I | re.A)
    if m:
        w, h, r = dec_int(m.group(1)), dec_int(m.group(2)), m.group(3)
    elif m2 and dec_int(m2.group(1)) in STD_WIDTH:
        h, scan, r = dec_int(m2.group(1)), (m2.group(2) or "p").lower(), m2.group(3)
        w = STD_WIDTH[h]
    elif m3:
        w, h, scan, r = (dec_int(m3.group(1)), dec_int(m3.group(2)),
                         (m3.group(3) or "p").lower(), m3.group(4))
    elif m4:
        h, scan, r = dec_int(m4.group(1)), m4.group(2).lower(), m4.group(3)
        w = STD_WIDTH.get(h)
    elif m5 and dec_int(m5.group(1)) in STD_WIDTH:
        h, r = dec_int(m5.group(1)), m5.group(2)
        w = STD_WIDTH[h]
    else:
        return None
    rate = perl_num(r) if r else int(fps[0] / float(fps[1]))
    if not r and h <= 1080:
        rate = 60
    if not h or not rate or not math.isfinite(rate):
        return None             # an Inf rate matches no HDMI mode
    return find_mode(w, h, scan, rate)


# -- pattern codes -----------------------------------------------------------
def source_code(value, input_max):
    """calman_scale_value's clamp of a code to its own range (daemon.pm:77-84)."""
    input_max = input_max if input_max > 0 else 255
    v = perl_int(value)
    # NaN passes every clamp; shifted to the target depth it is 0 (NaN>>2, NaN<<2), and at
    # the same depth the renderer gets NaN and dies (deviation: 0)
    return 0 if v != v else max(0, min(input_max, v))


def scale_value(v, input_max, target_max):
    """calman_scale_value (daemon.pm:76-93) of a code already clamped to input_max."""
    depth = {255: 8, 1023: 10, 4095: 12}
    if input_max == target_max:
        return v
    if input_max in depth and target_max in depth:
        if v >= input_max:
            return target_max
        d = depth[target_max] - depth[input_max]
        return v << d if d > 0 else v >> -d
    return int(v / float(input_max) * target_max + 0.5)


def round10(e, limited):
    """E' -> the nearest whole 10-bit code of the source range, as E'."""
    if limited:
        return (math.floor(64 + 876 * e + 0.5) - 64) / 876.0
    return math.floor(1023 * e + 0.5) / 1023.0


BLACK = Code((0, 0, 0), 0)


class _Clear:
    """A Calman colour the renderer cleared to, read in the source range of its file."""

    def __init__(self, colour, calman_range):
        self.c, self.range = colour, calman_range

    def colour(self, sig):
        with calman_range(sig, self.range):
            return self.c.colour(sig)


class AplGrey:
    """calman_apl_bg_value's grey surround (daemon.pm:188-226), solved at draw time from
    the patch's E' in the range its codes are read in."""

    def __init__(self, fg, win, apl):
        self.fg, self.win, self.apl = fg, win, apl

    def colour(self, sig):
        c = self.fg.colour(sig)
        fg_pct = max(0.0, (0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b) * 100.0)
        wf = self.win / 100.0
        bg_pct = max(0.0, min(100.0, (self.apl - fg_pct * wf) / (1 - wf)))
        # deviates from daemon.pm:220-222/53-57: whole 10-bit code in the read range, not max_bpc
        return Colour.gray(round10(bg_pct / 100.0, reads_limited(sig)))


def byte_colour(sig):
    """An 8-bit chart level -> Colour, at its own depth in the source range; a computed
    (fractional) HDR10 level is rounded to a whole 10-bit code."""
    lim = reads_limited(sig)

    def colour(b):
        e = _level(b, 255, lim)
        return Colour.gray(e if b == int(b) else round10(e, lim))
    return colour


def conf_value(conf, var):
    """GET_PGENERATOR_CONF_<KEY>'s value: None when unknown, min_luma in 0.0001-nit units
    (pg_min_luma_cmd_nits_to_wire)."""
    if var not in conf:
        return sysinfo.NONE
    v = conf[var]
    if var == "min_luma":
        n = perl_num(v)
        w = 0 if n <= 0 else perl_int(n / 0.0001 + 0.5)
        v = perl_str(w if w != w else max(0, min(65535, w)))
    return v


def conf_lines(conf):
    """GET_PGENERATOR_CONF_ALL before base64."""
    return "".join("%s:%s\n" % (k, conf_value(conf, k)) for k in conf)


class Calman:
    """PGenerator's Calman state, shared by every connection."""

    def __init__(self, gen, log, format_file=None):
        self.gen = gen
        self.log = log
        self.format_file = format_file
        self.stats = {}               # command.pm stats(): connections, patterns, errors
        self.templates = templates.Store()
        self.engine = templates.Engine(self.templates, self.stat)
        self.rstate = shapes.RendererState()
        self.client_ip = self.client_software = ""   # calibration_client_ip / _software
        # DISCOVERABLE.disabled (variables.pm:346), kept next to the remembered format
        self.discoverable_file = (os.path.join(os.path.dirname(format_file),
                                               "DISCOVERABLE.disabled") if format_file else None)
        self.discoverable = not (self.discoverable_file and os.path.exists(self.discoverable_file))
        self.pending_mode = False     # a CMD mode_idx, taken at the next restart
        self.images_name = ""         # frames/pattern.info (SAVEIMAGES)
        self.upload_buf = b""         # /tmp/UPLOAD_FILE.<time>.<pid> ($uploading_file)
        self.bits = 8                 # $bits_default (sync_pattern_bits_default)
        s = gen.sig
        self.conf = dict(STOCK_CONF)
        self.conf.update(max_luma=perl_str(s.max_luma), min_luma=perl_str(s.min_luma),
                         max_cll=perl_str(s.max_cll), max_fall=perl_str(s.max_fall),
                         dv_map_mode="2" if s.dv_map_mode is None else str(s.dv_map_mode),
                         mode_idx=self._mode_idx((s.width, s.height, (s.fps_num, s.fps_den))))
        self.rend = None              # the conf the renderer was started with
        self.map_chosen = s.dv_map_mode is not None     # README: no L255 until Calman sets one
        self.applied = {}             # the serving connection's calman_applied_mode_keys
        self.shown = None             # the renderer's pattern file (a scene function)
        self.stale = False            # the renderer restarted since it was drawn
        self.image = None             # the renderer's loaded chart texture: (file, scene)
        self.cpu_prev = (0, 0)
        self.gci = False              # the connection being served is GCI
        self.dv_transition = False
        self.explicit_bpc = ""
        self.range = None             # calman_rgb_quant_range, undef at start
        self.preferred = ""           # webui_rgb_quant_range_preferred
        self.range_owner = ""
        self.external = False
        self.replaying = False
        saved = self._load_mode_idx()     # PGenerator.conf's calman_mode_idx lasts
        if saved is not None:
            self.conf["calman_mode_idx"] = saved
        if s.mode == "dv":
            self.set_dv("", "")
        elif s.mode != "sdr":
            self.set_non_dv(3 if s.mode == "hlg" else 2, s.mode)
        self.range = None             # the start-up mode is configuration, not Calman's
        self.preferred = self.range_owner = ""
        self.external = self.dirty = False
        self.apl, self.apl_enabled, self.bg, self.win = 18.0, False, BLACK, 10
        self.last = None              # (kind, type, payload)
        self.sync_bits()              # get_conf at the daemon's start
        self.restart()                # the daemon's start (daemon.pm:31), shown by nothing
        self.stale = False
        # the device_info thread (info.pm), started right after: every INFO_PERIOD it caches
        # get_cmd_generic's answers and each (shared) conf key's raw value
        self.info_t0, self.info_gone = time.monotonic(), {}
        self.info_cycle, self.info_conf = 0, dict(self.conf)

    # -- conf mirror ----------------------------------------------------------
    @staticmethod
    def _mode(c, renderer=False):
        """renderer: the renderer's own test, isHDR=atoi(is_hdr) as a truth value
        (main.cpp:150, ofxRPI4Window.cpp:1550); the conf's is int()==1."""
        if dv_active(c):
            return "dv"
        if (c_atoi(c["is_hdr"]) != 0) if renderer else (perl_int(c["is_hdr"]) == 1):
            return "hlg" if perl_int(c["eotf"]) == 3 else "hdr10"
        return "sdr"

    @staticmethod
    def _mode_idx(fmt):
        """mode_idx: the mode's index in the assumed TV list (else the format itself)."""
        idx = mode_index(fmt)
        return str(idx) if idx is not None else "%dx%d@%d/%d" % (fmt[0], fmt[1], fmt[2][0],
                                                                 fmt[2][1])

    def sync(self):
        """The add-on's signal from the conf mirror. The renderer reads the mode, format,
        range, primaries and DV mapping only when it starts (main.cpp:115-154,
        drm_override.c), the luminance keys on every frame (updateHDR_Infoframe)."""
        c, s = self.conf, self.gen.sig
        r = self.rend if self.rend is not None else c
        s.mode = self._mode(r, self.rend is not None)
        s.pattern_mode = self._mode(c)      # webui_pattern_signal_mode reads the conf
        # drm_override.so puts the conf colorimetry on the wire in every mode
        s.colorimetry = "bt2020" if lead_digits(r["colorimetry"]) in (9, 10) else "bt709"
        s.primaries = c_atoi(r["primaries"])          # main.cpp:154 atoi
        s.bits = perl_int(valid_bpc(r["max_bpc"]) or 8)
        # the HDMI format is the box's own: Calman's is reported back but its codes are
        # always read as on an RGB wire (Calman's range still decides 0-255 or 16-235)
        s.color_format = "RGB"
        s.wire_range = c_atoi(r["rgb_quant_range"])     # main.cpp:123,143 atoi
        s.calman_range = self.range
        s.max_luma = _finite(perl_num(c["max_luma"]))
        s.min_luma = _finite(perl_num(c["min_luma"]))
        s.max_cll = int(_finite(perl_int(c["max_cll"])))
        s.max_fall = int(_finite(perl_int(c["max_fall"])))
        s.dv_map_mode = (perl_int(r["dv_map_mode"]) if self.map_chosen and r["dv_map_mode"] != ""
                         else None)

    def restart(self):
        """pattern_generator_stop + start: clean_files, then the renderer takes the conf as it
        is now, after normalize_signal_mode_conf (command.pm:135-191, written without the dirty
        flag), and shows PatternStart (command.pm:525/642; as shipped a black full field),
        until a pattern is drawn."""
        c = self.conf
        self.clean_files()
        self.normalize_dv()
        c["signal_mode"] = self._mode(c)
        if self.pending_mode or c.get("mode_idx", "") == "":
            # a CMD mode_idx: the renderer's new mode ("" is auto_select_4k_mode's 2160p30)
            fmt = mode_by_index(c.get("mode_idx", ""))
            if c.get("mode_idx", "") == "":
                c["mode_idx"] = self._mode_idx(fmt)
            if fmt is not None:
                s = self.gen.sig
                s.width, s.height, (s.fps_num, s.fps_den) = fmt
            self.pending_mode = False
        self.rend = dict(c)
        self.image = None             # a new renderer process loads its textures again
        self.rstate = shapes.RendererState()
        self.stale = True
        self.sync()
        s, dv = self.gen.sig, self.std_dv()
        _, text = self.engine.get_pattern("TESTTEMPLATE", "PatternStart", "", s.width, s.height,
                                          self.bits_default())
        draws = shapes.parse(text, self.rstate)[0] if text is not None else []
        _, after, exited = shapes.plan(draws, None, dv)
        if exited:
            self.rstate = shapes.RendererState()
        else:
            self.rstate.background = after
        self.shown = lambda sig: shapes.frame_scene(sig, draws, None, dv)

    def clean_files(self):
        """clean_files (command.pm:718-745): the frames' pattern.info, the files of $var_dir,
        frames and running, and every template in $var_dir/tmp but PGenerator's own and
        PERMANENT=yes ones (not the ramdisk's)."""
        self.images_name = ""
        files = self.templates.files
        for d in (templates.VAR_DIR, templates.VAR_DIR + "/frames", templates.VAR_DIR + "/running"):
            files[d].clear()
        tmp = self.templates.dirs["tmp"]
        for name in list(tmp):
            if name not in KEPT_TEMPLATES and not tmp[name].startswith("PERMANENT=yes"):
                del tmp[name]

    def clean_pattern_files(self):
        """clean_pattern_files (pattern.pm:740-746): frames/pattern.info and the templates
        whose name matches /.jpg$/ or /.png$/."""
        self.images_name = ""
        self.templates.clean_pattern_files()

    def normalize_dv(self):
        """normalize_dv_transport_conf, "standard" transport (the only one the add-on sends)."""
        c = self.conf
        if dv_active(c):
            c.update(signal_mode="dv", is_sdr="0", is_hdr="1", eotf="2", dv_transport="standard",
                     is_ll_dovi="0", is_std_dovi="1", dv_status="1", dv_interface="0",
                     dv_profile="1", dv_metadata=self._dv_metadata_for(c.get("dv_map_mode", "")),
                     dv_color_space="0", color_format="0", colorimetry="9", primaries="1",
                     max_bpc=self._dv_transport_bpc(), rgb_quant_range="2")

    def refresh(self):
        """A restarted renderer that nothing drew on since shows PatternStart."""
        if self.stale and self.shown is not None:
            self.replaying = True     # not a Calman pattern: announces nothing
            try:
                self.gen.show_scene(self.shown)
            finally:
                self.replaying = False
        self.stale = False

    def stat(self, key):
        """stats(key, 1): -> what its unlink of the cached GET_STATS answer returns."""
        self.stats[key] = self.stats.get(key, 0) + 1
        return self.info_forget("GET_STATS")

    def info_cached(self, name):
        """Whether the device_info thread's cache holds $info_dir/<name>.info now: it is
        written at every cycle and removed by unlink (info_forget)."""
        if not hasattr(self, "info_t0") or INFO_PERIOD <= 0:
            return False
        self.info_tick()
        return self.info_t0 + INFO_PERIOD * self.info_cycle > self.info_gone.get(name, -1.0)

    def info_tick(self):
        """A cycle since the last look: the conf as it is now is what that cycle wrote (the
        conf only changes while a command is served, and every command looks first)."""
        if not hasattr(self, "info_t0") or INFO_PERIOD <= 0:
            return
        n = int(math.floor((time.monotonic() - self.info_t0) / INFO_PERIOD))
        if n != self.info_cycle:
            self.info_cycle, self.info_conf = n, dict(self.conf)

    def info_forget(self, name):
        """unlink("$info_dir/<name>.info"): 1 when it was there."""
        was = self.info_cached(name)
        if hasattr(self, "info_t0"):
            self.info_gone[name] = time.monotonic()
        return 1 if was else 0

    def sync_bits(self):
        """sync_pattern_bits_default (conf.pm:88-98): a latch, set at the daemon's start and
        by Calman's saves only; an invalid max_bpc keeps the previous value."""
        if dv_flag(self.conf.get("dv_status")):
            self.bits = 8
            return
        b = perl_int(_or(self.conf.get("max_bpc"), "8"))
        if b in (8, 10, 12):
            self.bits = b

    def save(self, key, value):
        """calman_save_setting."""
        value = perl_str(value)
        if self.gci and key not in GCI_KEYS:
            return
        if key == "dv_map_mode" and not self.map_chosen:
            self.map_chosen = True
            self.sync()
        if self.conf.get(key, "") == value:
            return
        self.conf[key] = value
        self.sync_bits()
        self.dirty = True
        self.sync()
        if self.gci and key in ("eotf", "is_hdr", "primaries"):
            self.coherence()

    def coherence(self):
        """GCI: recompute the mode keys the gate suppressed."""
        if not self.gci:
            return
        eotf, prim = perl_int(self.conf["eotf"]), perl_int(self.conf["primaries"])
        mode = ("hlg" if eotf == 3 else "hdr10") if eotf >= 2 else "hdr10" if eotf == 1 else "sdr"
        if self.dv_transition:
            mode = "dv"
        writes = [("signal_mode", mode), ("is_sdr", "0" if eotf >= 2 else "1"),
                  ("is_hdr", "1" if eotf >= 2 else "0"),
                  ("colorimetry", ("2" if prim == 0 else "9") if eotf >= 2 else "2")]
        dv = "1" if self.dv_transition else "0" if 0 <= eotf <= 3 else None
        if dv is not None:
            writes += [("dv_status", dv), ("is_ll_dovi", "0"), ("is_std_dovi", dv)]
        for k, v in writes:
            if self.conf.get(k, "") != v:
                self.conf[k] = v
                self.dirty = True
        self.sync()

    def preferred_bpc(self, fallback, preserve):
        return choose_bpc(self.explicit_bpc, self.conf["max_bpc"], fallback, preserve)

    def note_explicit_bpc(self, value):
        bpc = valid_bpc(value)
        if bpc:
            self.explicit_bpc = bpc
        return bpc

    # calman_set_non_dv's arguments by EOTF: (signal_mode, colorimetry, max_bpc)
    NON_DV = {0: ("sdr", "2", "8"), 2: ("hdr", "9", "10"), 3: ("hlg", "9", "10")}

    def set_non_dv(self, eotf, mode=None):
        """calman_set_non_dv for EOTF 0 (SDR), 2 (PQ) or 3 (HLG); `mode` overrides the
        signal_mode written (the start-up signal's own name)."""
        default_mode, colorimetry, bpc = self.NON_DV[eotf]
        mode = mode or default_mode
        bpc = self.preferred_bpc(bpc, eotf >= 2)
        hdr = eotf >= 2
        for k, v in (("signal_mode", mode.lower()), ("is_sdr", "0" if hdr else "1"),
                     ("is_hdr", "1" if hdr else "0"), ("is_ll_dovi", "0"),
                     ("is_std_dovi", "0"), ("dv_status", "0"), ("eotf", str(eotf)),
                     ("colorimetry", colorimetry), ("max_bpc", bpc),
                     ("primaries", "1" if hdr else "0")):
            self.save(k, v)

    def _dv_transport_bpc(self):
        return "10" if perl_int(self.conf["max_bpc"]) == 10 else "8"

    def _dv_metadata_for(self, map_mode):
        return {"0": "2", "1": "3", "2": "4"}.get(map_mode, "2")

    def _save_dv_transport(self):
        """The keys calman_set_dv_rgb and calman_force_dv_rgb both assert, in their order."""
        for k, v in (("is_sdr", "0"), ("is_hdr", "1"), ("eotf", "2"),
                     ("dv_transport", "standard"), ("is_ll_dovi", "0"), ("is_std_dovi", "1"),
                     ("dv_status", "1"), ("dv_interface", "0"), ("dv_color_space", "0"),
                     ("color_format", "0"), ("colorimetry", "9"), ("primaries", "1"),
                     ("max_bpc", self.preferred_bpc(self._dv_transport_bpc(), False)),
                     ("rgb_quant_range", "2")):
            self.save(k, v)

    def _full_range_source(self):
        if not self.gci:
            self.set_range(2)
            self.apply_source(2)

    def set_dv(self, map_mode, metadata):
        """calman_set_dv_rgb: standard Dolby Vision, RGB tunnel, full range."""
        self.dv_transition = True
        self.save("signal_mode", "dv")
        self._save_dv_transport()
        self._full_range_source()
        if map_mode != "":
            self.save("dv_map_mode", map_mode)
        if metadata == "":
            metadata = self._dv_metadata_for(map_mode if map_mode != "" else
                                             self.conf["dv_map_mode"])
        self.save("dv_metadata", metadata)
        self.coherence()
        self.dv_transition = False

    def force_dv(self):
        """calman_force_dv_rgb: an apply in DV re-asserts the DV transport keys."""
        if not dv_active(self.conf):
            return
        self.dv_transition = True
        self._save_dv_transport()
        self.save("dv_metadata", self._dv_metadata_for(self.conf["dv_map_mode"]))
        self._full_range_source()
        self.coherence()
        self.dv_transition = False

    def apply(self, replay=True):
        """calman_apply: settings changed since the last apply redraw the pattern."""
        did = False
        while self.dirty:
            self.dirty = False
            self.force_dv()
            now = {k: self.conf.get(k, "") for k in MODE_KEYS}
            if any(self.applied[k] != v if k in self.applied else v != "" for k, v in now.items()):
                self.restart()
            self.applied.clear()
            self.applied.update(now)
            if replay:
                self.replay_last()
            did = True
        return did

    # -- source range (command.pm) --------------------------------------------
    def webui_preferred(self):
        q = self.preferred if self.preferred != "" else self.conf["rgb_quant_range"]
        return perl_int(q) if q in ("1", "2") else 2

    def _set_wire(self, q, owner):
        """command.pm: a new rgb_quant_range restarts the renderer; a new owner does not."""
        changed = self.conf["rgb_quant_range"] != str(q)
        self.conf["rgb_quant_range"] = str(q)
        self.range_owner = owner
        if changed:
            self.restart()

    def apply_source(self, q, owner="calman"):
        """apply_source_rgb_quant_range(owner, q)."""
        q = q if q in (1, 2) else 2
        if self.preferred == "":
            self.preferred = self.conf["rgb_quant_range"]
        self.external = True
        self._set_wire(q, owner)

    def release(self, owner="calman"):
        """release_source_rgb_quant_range(owner)."""
        if self.range_owner != owner and not self.external:
            return
        q = self.webui_preferred()
        self.preferred = str(q)
        self.external = False
        self._set_wire(q, "webui")

    def set_range(self, q):
        self.range = q
        self.sync()

    def release_range(self):
        self.release()
        self.set_range(self.webui_preferred())

    # -- pattern state --------------------------------------------------------
    def reset(self):
        """calman_reset_pattern_state."""
        self.apl = 18.0
        self.apl_enabled = False
        self.bg = BLACK
        self.win = 10
        self.explicit_bpc = ""
        # ($conf || "2") + 0: numeric, not int() ("1.5" stays 1.5: not a Limited source)
        q = perl_num(_or(self.conf["rgb_quant_range"], "2"))
        self.set_range(int(q) if math.isfinite(q) and q == int(q) else q)
        self.last = None

    def remember(self, kind, ctype, payload):
        if not self.replaying:
            self.last = (kind, ctype, payload)

    def reencode(self):
        """The picture shown, encoded again (new static metadata); nothing is redrawn."""
        if self.replaying or self.shown is None:
            return
        self.replaying = True
        try:
            self.gen.show_scene(self.shown)
        finally:
            self.replaying = False
        self.stale = False

    def replay_last(self):
        if self.replaying or self.last is None:
            return False
        self.replaying = True
        try:
            kind, ctype, payload = self.last
            self.log("redrawing last pattern (%s) for %s" % (ctype, self.gen.sig.describe()))
            if kind == "CommandRGB":
                self.render_commandrgb(payload)
            elif kind in ("RGB", "YCC"):
                self.render_rgb(ctype, payload)
            else:
                self.render_specialty(payload)
        finally:
            self.replaying = False
        return True

    def _load_mode_idx(self):
        """The saved conf calman_mode_idx (an older add-on saved "w h num den")."""
        try:
            with open(self.format_file, encoding="latin-1") as f:
                text = f.read()
        except (OSError, TypeError):
            return None
        m = re.match(r"(\d+) (\d+) (\d+) (\d+)\n\Z", text, re.A)
        if m:
            idx = mode_index((int(m.group(1)), int(m.group(2)), (int(m.group(3)),
                                                                  int(m.group(4)))))
            return None if idx is None else str(idx)
        return text

    def save_mode_idx(self):
        """sudo SET_PGENERATOR_CONF calman_mode_idx: written to the conf file."""
        if not self.format_file or "calman_mode_idx" not in self.conf:
            return
        try:
            os.makedirs(os.path.dirname(self.format_file), exist_ok=True)
            with open(self.format_file, "w", encoding="latin-1", errors="replace") as f:
                f.write(self.conf["calman_mode_idx"])
        except OSError as exc:
            self.log("cannot remember the Calman format: %s" % exc, error=True)

    def remember_mode_idx(self, idx):
        self.conf["calman_mode_idx"] = str(idx)
        self.save_mode_idx()

    def set_mode(self, idx):
        """pgenerator_cmd SET_MODE: mode_idx stored as sent, and the renderer restarts."""
        self.conf["mode_idx"] = idx
        self.pending_mode = True
        self.restart()

    def init_mode(self):
        """calman_apply_init_mode (daemon.pm:308-321): the conf calman_mode_idx, 1080p24's
        index when it is not digits ("" and "0" count as unset), if not current."""
        idx = _or(self.conf.get("calman_mode_idx"), "")
        if not re.match(r"\d+\n?\Z", idx, re.A):
            default = find_mode(1920, 1080, "p", 24)
            if default is not None:
                idx = str(default)
                self.remember_mode_idx(idx)
        if not re.match(r"\d+\n?\Z", idx, re.A):
            return
        if _or(self.conf.get("mode_idx"), "") != idx:
            self.set_mode(idx)

    def reported_mode(self):
        """get_hdmi_info's mode (the device_info thread rebuilds it every cycle): the
        modetest line of the conf mode_idx, else the mode shown -> (idx, w, h, scan, fps)."""
        idx = conf_mode_index(self.conf.get("mode_idx"))
        if idx is not None:
            w, h, scan, _, fps = TV_MODES[idx]
            return idx, w, h, scan, fps
        s = self.gen.sig
        fps = (s.fps_num, s.fps_den)
        return mode_index((s.width, s.height, fps)), s.width, s.height, "p", fps

    # -- drawing ------------------------------------------------------------
    def _draw(self, scene_fn, count=True, image=None):
        """A new pattern file (create_pattern_file / get_pattern count it in STATS). Its
        source range and chart levels are written into it, so a restart keeps them.
        image: the chart's image file (webui_pattern_diag_image_file), for DRAW=IMAGE."""
        s = self.gen.sig
        frozen = (s.calman_range, s.pattern_mode, s.max_luma)

        def file_fn(sig):
            sig.calman_range, sig.pattern_mode, sig.max_luma = frozen
            return scene_fn(sig)
        if image is not None:
            # ofApp.cpp:475/491: the same image file is not loaded again until a non-IMAGE
            # pattern or a renderer restart (ofApp.cpp:189-195); the old texture stays
            if self.image is not None and self.image[0] == image:
                file_fn = self.image[1]
            self.image = (image, file_fn)
        else:
            self.image = None
        self.shown, self.stale = file_fn, False
        if count:
            self.stat("patterns")
        return self.gen.show_scene(file_fn)

    def draw_window(self, fg, win, bg, count=True):
        if not fg.ycc:
            s = self.gen.sig
            w, h = (s.width, s.height) if win >= 100 else (
                int(math.sqrt(win / 100.0) * s.width), int(math.sqrt(win / 100.0) * s.height))
            self.renderer_reads(self.simple_file(w, h, fg, bg), bg)
        return self._draw(lambda sig: patch_scene(sig, fg.colour(sig), win, bg.colour(sig)), count)

    def simple_file(self, w, h, fg, bg):
        """create_pattern_file(simple=1) of a Calman RECTANGLE (pattern.pm:75-140)."""
        s, tmax = self.gen.sig, self.target_max()
        return ("MOVIE_NAME=TestPattern\nBITS=%d\n%sDRAW=RECTANGLE\nDIM=%d,%d\nRESOLUTION=100\n"
                "RGB=%s\nBG=%s\nPOSITION=%d,%d\n%sTEXT=\nEND=1\nFRAME_NAME=TestPattern\nFRAME=1\n"
                % (self.bits_default(), "SOURCE_MAX=4095\n" if self.std_dv() else "", w, h,
                   self.target_codes(fg, tmax), self.target_codes(bg, tmax),
                   int((s.width - w) / 2.0), int((s.height - h) / 2.0),
                   "SOURCE_RANGE=LIMITED\n" if self.range == 1 else ""))

    def renderer_reads(self, text, clear):
        """The renderer reading the file PGenerator writes for a Calman pattern: its values
        (RGB, BG, POSITION, RESOLUTION, BITS, DRAW, TEXT) carry into later files that lack
        them, and a later BG=-1 leaves clear, the colour it cleared to, on screen. The picture
        itself is drawn from the codes at their own depth (_draw)."""
        shapes.parse(text, self.rstate)
        self.rstate.background = _Clear(clear, self.gen.sig.calman_range)

    def draw_dynamic(self, fg, bg, count=True):
        """get_pattern(PatternDynamic, "r,g,b;bg"): as shipped a centred 640x360 box; a
        PatternDynamic the client stored (SETCONF) is drawn as a template."""
        stored = self.templates.get("PatternDynamic")
        if not fg.ycc and stored != templates.SHIPPED["PatternDynamic"]:
            return self.dynamic_template(fg, bg)
        if not fg.ycc:
            s, tmax = self.gen.sig, self.target_max()
            _, text = self.engine.get_pattern(
                "TESTTEMPLATE", "PatternDynamic", "%s;%s" % (self.target_codes(fg, tmax),
                                                             self.target_codes(bg, tmax)),
                s.width, s.height, self.bits_default(), "LIMITED" if self.range == 1 else "",
                4095 if self.std_dv() else 0, count=False)
            if text is not None:
                self.renderer_reads(text, bg)
        return self._draw(lambda sig: dynamic_scene(sig, fg.colour(sig), bg.colour(sig)), count)

    def dynamic_template(self, fg, bg):
        """The stored PatternDynamic. A template whose only colours and depth are DYNAMIC
        gets the codes at their own depth (10-bit, BITS=10 or SOURCE_MAX=1023 in Dolby
        Vision), the background as whole 10-bit codes of the range they are read in
        (PGenerator's internal black stays black). One with its own RGB=/BG=/BITS=/
        SOURCE_MAX=/VAR=/MACRO= lines gets PGenerator's file (daemon.pm:483, 537): the codes
        scaled to the pattern depth, which its own values are read at."""
        sig = self.gen.sig
        lim = reads_limited(sig)
        rng = "LIMITED" if self.range == 1 else ""
        if not templates.dynamic_only(self.templates.get("PatternDynamic")):
            tmax = self.target_max()
            payload = "%s;%s" % (self.target_codes(fg, tmax), self.target_codes(bg, tmax))
            return self.template_pattern("TESTTEMPLATE", "PatternDynamic", payload, rng,
                                         12 if self.std_dv() else 0)
        if isinstance(bg, Code) and bg.cmax:
            bgc = bg.codes
        else:
            e = 0.0 if bg is BLACK else bg.colour(sig).r
            bgc = (int(math.floor((64 + 876 * e if lim else 1023 * e) + 0.5)),) * 3
        payload = "%d,%d,%d;%d,%d,%d;;;;;;;10" % (tuple(fg.codes) + tuple(bgc))
        return self.template_pattern("TESTTEMPLATE", "PatternDynamic", payload, rng,
                                     10 if self.std_dv() else 0)

    def target_max(self):
        """calman_target_max: 4095 in standard Dolby Vision, else $bits_default's."""
        if self.std_dv():
            return 4095
        return {10: 1023, 12: 4095}.get(self.bits_default(), 255)

    def target_codes(self, colour, tmax):
        """A pattern colour as PGenerator writes it into the file: codes scaled to tmax
        (calman_scale_value), an APL grey as calman_apl_bg_value's code."""
        if isinstance(colour, Code):
            if not colour.cmax:
                return "0,0,0"
            return ",".join(str(scale_value(v, colour.cmax, tmax)) for v in colour.codes)
        e = colour.colour(self.gen.sig).r
        if perl_int(_or(self.conf["rgb_quant_range"], "0")) == 1:     # calman_apl_levels
            k = tmax // 255
            v = 16 * k + e * 219 * k
        else:
            v = e * tmax
        return ",".join([str(int(math.floor(v + 0.5)))] * 3)

    def apl_bg_value(self, fg, win, apl, fallback):
        """calman_apl_bg_value (daemon.pm:188-226), PGenerator's arithmetic exactly (0.4.14):
        the patch codes scaled to the target depth (calman_scale_value), their Rec.709 luma
        placed in the levels of the conf's wire range (calman_apl_levels: limited only when
        rgb_quant_range is 1, so full in Dolby Vision, whose tunnel then shows the codes
        as limited - PGenerator lifts that surround, and so does this), the surround solved
        and rounded to a whole code at that depth, then drawn like any other code. On an
        OLED the surround sets the brightness limiter, so it changes the measured patch."""
        win = win if win > 0 else self.win
        win = min(100, win)
        if win >= 100:
            return fallback
        apl = max(0.0, min(100.0, float(apl)))
        if fg.ycc:                      # YCC_* is not drawn by PGenerator: no reference
            return AplGrey(fg, win, apl)
        target = self.target_max()
        bits = {255: 8, 1023: 10, 4095: 12}.get(target) or int(self.bits_default() or 8)
        shift = bits - 8
        if perl_int(_or(self.conf.get("rgb_quant_range"), "0")) == 1:
            lo, span = 16 << shift, 219 << shift
        else:
            lo, span = 0, target
        hi = lo + span
        cmax = fg.cmax or target        # PGenerator's internal black "0,0,0"
        r, g, b = (scale_value(max(0, min(cmax, int(c))), cmax, target) for c in fg.codes)
        fg_pct = max(0.0, ((0.2126 * r + 0.7152 * g + 0.0722 * b) - lo) * 100 / span)
        wf = win / 100.0
        bg_pct = max(0.0, min(100.0, (apl - fg_pct * wf) / (1 - wf)))
        bg_y = int(lo + bg_pct * span / 100 + 0.5)
        bg_y = max(0, min(hi, bg_y))
        return Code((bg_y, bg_y, bg_y), target)

    def render_rgb(self, ctype, payload, full_key=""):
        """calman_render_rgb_pattern: RGB_S/RGB_A/RGB_B and other RGB_ types, 10-bit codes;
        YCC_S/YCC_A/YCC_B/YCC_<other> alike with 10-bit Y'CbCr codes. full_key: the key as
        received ("" on a replay: STX + type + payload)."""
        ycc = "RGB_" not in ctype
        pre = "YCC_" if ycc else "RGB_"
        el = split_perl(payload)
        fg = Code([source_code(_field(el, i), 1023) for i in range(3)], ycc=ycc)
        if not ycc:                   # YCC_* is not drawn by PGenerator (daemon.pm:2393)
            self.clean_pattern_files()
        special = CALMAN_SPECIAL.get(full_key or "\x02%s:%s" % (ctype, payload))
        if special and self.templates.get(special) is not None:
            return self.template_pattern("TESTTEMPLATE", special, "")
        if pre + "B" in ctype:
            v = source_code(_field(el, 3), 1023)
            self.apl_enabled = False
            # deviates from daemon.pm:481 (target-depth codes): kept with their own depth
            self.bg = Code((v, 512, 512) if ycc else (v, v, v), ycc=ycc)
            return self.draw_dynamic(fg, self.bg, not ycc)
        if pre + "S" in ctype or pre + "A" in ctype:
            bg = self.bg
            if pre + "S" in ctype:
                win = perl_int(_field(el, 3))
            elif len(el) >= 7:
                bg = Code([source_code(el[i], 1023) for i in (3, 4, 5)], ycc=ycc)
                win = perl_int(el[6])
            elif len(el) >= 4:
                win = perl_int(el[3])
            else:
                win = self.win
            if win != win:
                # NaN: the window size is kept and this draw's geometry is NaN (the renderer
                # dies; deviation: drawn at the kept size)
                win = self.win
            win = self.win if win < 1 else min(100, win)
            self.win = win
            return self.draw_window(fg, win, bg, not ycc)
        bg = self.apl_bg_value(fg, self.win, self.apl, self.bg) if self.apl_enabled else self.bg
        return self.draw_dynamic(fg, bg, not ycc)

    def commandrgb_window(self, fg, token):
        if 1 <= token <= 100:
            return token, BLACK
        if 101 <= token <= 998:
            return 10, self.apl_bg_value(fg, 10, token - 100, BLACK)
        win = self.win if self.win > 0 else 10
        win = 10 if win < 1 else min(100, win)
        if token == 999:
            return win, self.apl_bg_value(fg, win, self.apl, BLACK)
        return win, BLACK

    def render_commandrgb(self, payload):
        """calman_render_commandrgb_pattern: R,G,B,tenBit,size (10 or 8-bit codes)."""
        el = split_perl(payload)
        in_max = 1023 if perl_int(_field(el, 3)) else 255
        fg = Code([source_code(_field(el, i), in_max) for i in range(3)], in_max)
        self.clean_pattern_files()
        win, bg = self.commandrgb_window(fg, perl_int(_field(el, 4)))
        self.apply_source(self.range)
        return self.draw_window(fg, win, bg)

    def render_specialty(self, payload):
        """calman_render_specialty_pattern; unknown names are a full-field 8-bit grey 128."""
        name = perl_uc(payload).strip(PERL_WS)
        self.clean_pattern_files()

        def scene_fn(sig):
            byte = byte_colour(sig)
            scene = specialty_scene(sig, name, byte)
            if scene is None:
                # deviates from daemon.pm:551 (128 shifted to max_bpc): read as an 8-bit code
                scene = patch_scene(sig, byte(128), 100, None)
            return scene
        # webui.pm's chart images are not pattern files (no stats); one file per chart
        image = {"BRIGHTNESS": "brightness", "CONTRAST": "contrast", "ALIGNMENT": "alignment",
                 "OVERSCAN": "alignment"}.get(name)
        s = self.gen.sig
        if image is None:
            self.renderer_reads(self.simple_file(s.width, s.height, Code((128,) * 3, 255),
                                                 self.bg), self.bg)
        else:               # webui_pattern_image_pattern (webui.pm:12074-12079)
            self.renderer_reads("MOVIE_NAME=TestPattern\nBITS=8\nDRAW=IMAGE\nDIM=%d,%d\n"
                                "RGB=0,0,0\nBG=0,0,0\nPOSITION=0,0\nIMAGE=/var/lib/PGenerator//running/"
                                "webui_pattern_calman_%s.png\nEND=1\n"
                                "FRAME_NAME=TestPattern\nFRAME=1\n" % (s.width, s.height, image),
                                BLACK)
        return self._draw(scene_fn, image is None, image)

    # -- queries ----------------------------------------------------------------
    def settings_string(self):
        c = self.conf
        _, w, h, _, (n, d) = self.reported_mode()      # ${w_s}x${h_s}, $preferred_mode
        bits = _or(c["max_bpc"], "8")
        fmt = {"0": "RGB", "1": "YCbCr 444", "2": "YCbCr 422", "3": "YCbCr 420"}.get(
            _or(c["color_format"], "0"))
        fmt = "%s %s-bit" % (fmt, bits) if fmt else "RGB 8-bit"
        return ("Resolution=%dx%d,Refresh=%d,1_FORMAT=%s,Range=%s,Bits=%s,Dolby=%s"
                % (w, h, int(n / float(d)), fmt,
                   "Limited" if _or(c["rgb_quant_range"], "0") == "1" else "Full", bits,
                   "On" if c["dv_status"] == "1" else "Off"))

    # -- classic protocol: templates and the renderer's pattern file -------------------
    def bits_default(self):
        """$bits_default: max_bpc, 8 in Dolby Vision, as last latched (sync_bits)."""
        return self.bits

    def std_dv(self):
        """legacy_external_dv_source_max: standard Dolby Vision."""
        return dv_flag(self.conf["dv_status"]) and dv_flag(self.conf["is_std_dovi"])

    def template_pattern(self, ptype, template, payload, source_range="", depth=0, exact=None):
        """get_pattern(ptype, template, payload) drawn; depth: the codes' own depth in
        Dolby Vision (0: none given); exact: templates.prepare_payload's own-depth triplets.
        Returns get_pattern's reply."""
        s = self.gen.sig
        reply, text = self.engine.get_pattern(ptype, template, payload, s.width, s.height,
                                              self.bits_default(), source_range,
                                              (1 << depth) - 1 if depth else 0, exact=exact)
        if text is not None:
            self.draw_file(text, self.engine.marks)
        return reply

    def draw_file(self, text, marks=None):
        """The renderer reading a pattern file (shapes.py); a file with several frames
        shows its first."""
        st, dv = self.rstate, self.std_dv()
        before = st.background
        draws = shapes.parse(text, st, marks)[0]
        start, after, exited = shapes.plan(draws, before, dv)
        if exited:                  # the renderer quits: black until the next pattern
            self.rstate = shapes.RendererState()
        else:
            st.background = after
        return self._draw(lambda sig: shapes.frame_scene(sig, draws, before, dv), False)

    def set_client(self, ip, software):
        self.client_ip, self.client_software = ip, software

    def set_discoverable(self, on):
        """set_discoverable: DISCOVERABLE.disabled written or removed, so it lasts across
        restarts (in memory only without a format file)."""
        self.discoverable = on
        self.info_forget("GET_DISCOVERABLE")
        if not self.discoverable_file:
            return
        try:
            if on:
                if os.path.exists(self.discoverable_file):
                    os.unlink(self.discoverable_file)
            else:
                os.makedirs(os.path.dirname(self.discoverable_file), exist_ok=True)
                with open(self.discoverable_file, "w") as f:
                    f.write("DISABLED")
        except OSError as exc:
            self.log("cannot save the discoverable state: %s" % exc, error=True)


class Session:
    """One client connection. handle() -> (reply bytes, close the connection)."""

    def __init__(self, calman, info, log, rpc=False, peer=""):
        self.cal = calman
        self.info = info          # dict: name, serial, firmware
        self.log = log
        self.rpc = rpc
        self.peer = peer          # client_ip{$connection}
        self.client_name = ""     # legacy_external_client_name{$connection}
        self.gci = False
        self.closed = False
        self.drew = False
        self.hcfr = False         # hcfr_client: the classic protocol from HCFR
        self.calman_seen = False  # calman{$connection}: the last recv ended with ETX
        self.applied = {}         # calman_applied_mode_keys of this connection

    def payload(self, text):
        data = text.encode("latin-1", "replace")
        return data if self.rpc else data + ETX

    @staticmethod
    def reply(text, calman):
        """send_key_to_client: ACK on a Calman connection, else the text + STX CR."""
        return ACK if calman else text.encode("latin-1", "replace") + CLASSIC_END

    def handle(self, key, calman=True):
        """key: one command after PGenerator's normalisation. calman: the chunk
        ended with ETX (always on the RPC port).

        Deviation from PGenerator, which draws asynchronously and ACKs regardless: a
        command during which a pattern failed to reach the screen (not displayed, the
        requested signal not on the wire, an error) is answered NAK (ERR on the classic
        port) instead, so the client never measures a pattern that is not the one it asked
        for. Commands are served one at a time under the generator lock, so the failure
        count only moves for this command."""
        gen = self.cal.gen
        failures = gen.failures
        self.cal.info_tick()
        try:
            reply, close = self._handle(key, calman)
        except Exception as exc:        # any path, not only dispatch: answered, not dropped
            self.log("%r failed: %r" % (key[:60], exc), error=True)
            gen.failures += 1
            reply, close = ACK, False
        finally:
            try:
                self.cal.refresh()
            except Exception as exc:
                self.log("redraw after %r failed: %r" % (key[:60], exc), error=True)
                gen.failures += 1
        if gen.failures != failures and reply:
            self.log("pattern for %r not displayed: answered %s"
                     % (key[:60], "NAK" if calman else "ERR"), error=True)
            self.cal.stat("errors")
            reply = NAK if calman else b"ERR" + CLASSIC_END
        return reply, close

    def _handle(self, key, calman):
        if key == "" or key.startswith("QUIT"):
            self.closed = True
            return b"", True
        cal = self.cal
        cal.gci, cal.applied = self.gci, self.applied
        if key.startswith("UPLOAD_FILE:"):        # daemon.pm:993, before TERM
            return self.reply(self.upload(key[12:]), calman), False
        if not calman:
            return self.classic(key, False)
        self.log("Calman < %r" % key, debug=True)
        req = key.lstrip("\x02")
        if req.startswith(("RGB_", "YCC_", "CommandRGB", "SPECIALTY")):
            self.cal.gen.pattern_request = req
        if "TERM" in key:
            return self.terminate()
        clean = key[1:] if key.startswith("\x02") else key
        clean = (clean[:-1] if clean.endswith("\x03") else clean).strip(PERL_WS)
        queries = {"SN": lambda: self.info["serial"], "CAP": lambda: CAPS,
                   "FIRMWARE": lambda: self.info["firmware"], "STATUS": lambda: STATUS_CAPS,
                   "GET_SETTINGS": cal.settings_string}
        if clean in queries:
            return self.payload(queries[clean]()), False
        if clean in ACKED:
            return ACK, False
        if clean in ("SHUTDOWN", "QUIT"):
            return self.terminate()
        if ":" not in key:
            return self.classic(key, True)     # the classic handlers run, ACKed on a Calman socket
        ctype, payload = key.split(":", 1)
        if ctype.startswith("\x02"):
            ctype = ctype[1:]
        try:
            self.dispatch(key, ctype, payload)
        except Exception as exc:
            self.log("Calman: %r failed: %r" % (key, exc), error=True)
            # whatever it was meant to change or redraw did not happen: never ACKed
            self.cal.gen.failures += 1
        return ACK, False

    def error(self, text="ERR"):
        self.cal.stat("errors")
        return text

    def upload(self, packet):
        """UPLOAD_FILE:where:name:type:status:start:size:content. The chunks are appended to
        the daemon's one upload file ($uploading_file, shared by every connection); END_UPLOAD
        stores it in the template directories (templates.Store.upload)."""
        f = packet.split(":", 6)
        f += [""] * (7 - len(f))
        size = perl_num(f[5])
        if f[5] in ("", "0"):
            return self.error()
        if size == 0:                   # "0.0": PGenerator dies dividing by it (deviation: ERR)
            return self.error()
        cal = self.cal
        cal.upload_buf += perl_decode_base64(re.sub(r".*;base64,", "", f[6]))
        perc = "100" if f[3] == "END_UPLOAD" else perl_str(perl_int(100 * perl_num(f[4]) / size))
        if f[3] == "END_UPLOAD":
            data, cal.upload_buf = cal.upload_buf, b""
            if f[0] == "PLUGINS":
                # set_plugin (PGenerator_cmd.pl:1172-1183) prints ERR for any archive but its
                # one permitted digest; plugins are never installed here
                return "ERR:%s%%" % perc
            cal.templates.upload(f[0], f[1], data)
        return "OK:%s%%" % perc

    def http_get(self, chunk, calman):
        """daemon.pm:900-956, on every raw recv before any framing: a GET is answered,
        then the connection is closed. There are no frame images to list or send."""
        m = re.search(r"GET /(.*)/index.html ", chunk)
        prefix, disabled = "", False
        for part in split_perl(urllib.parse.unquote(m.group(1), "latin-1") if m else "", "/"):
            prefix += part + "/"
            disabled = disabled or self.cal.templates.read(
                templates.VAR_DIR, prefix + "HTMLIMAGELIST.disabled") is not None
        if disabled:
            body = "HTTP/1.0 404 Not Found\r\n"
        elif m:
            body = ("HTTP/1.0 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n"
                    "<body style='background-color:#adadad;'>\n<center>\n</center>\n</body>\n")
        elif img := re.search(r"GET /(.*)/(.*)\.(jpg|png)-.* ", chunk):
            ext = img.group(3)
            ctype, content = "image/" + ext, ""
            if "&inline=1" in chunk:
                ctype, content = "text/html", '<img src="data:image/%s;base64," />' % ext
            body = ("HTTP/1.0 200 OK\r\nAccess-Control-Allow-Origin:*\r\nContent-Type: %s\r\n"
                    "Connection: close\r\n\r\n%s" % (ctype, content))
        else:
            return None
        self.closed = True
        return self.reply(body, calman), True

    # -- daemon.pm's classic protocol (2396-2692) -----------------------------------
    def classic(self, key, calman):
        """A Calman key without a colon, or any 2100 command not ending with ETX."""
        cal = self.cal

        def ok(text="OK"):
            return self.reply(text, calman), False
        if "RESTARTPGENERATOR:" in key:
            cal.restart()
            return ok()
        if "IS_ALIVE" in key:
            return ok("ALIVE")
        if "GETSTATUS" in key:
            m = re.match(r"GETSTATUS\s*[:;]\s*(.*)$", key, re.I | re.A)
            if m:           # legacy_external_client_name_from_marker
                m = re.match(r"\s*(?:CLIENTNAME|CLIENT_NAME|SOFTWARE)\s*[:=]\s*(.*?)\s*$",
                             m.group(1), re.I | re.A)
                if m:
                    self.set_client_name(m.group(1))
            return ok("OK:Alive")
        m = re.match(r"(?:CLIENTNAME|CLIENT_NAME|SOFTWARE)\s*[:=]\s*(.*)$", key, re.I | re.A)
        if m:
            name = self.set_client_name(m.group(1))
            return ok("OK" if name else self.error("invalid client name"))
        m = re.search(r"CMD:(.*)", key)
        if m:
            el = split_perl(m.group(1), ":")
            if el and (el[0] in ("GET_RESOLUTION", "GET_GPU_MEMORY") or (el[0] == "MULTIPLE" and (
                    "GET_RESOLUTION" in el[1:] or "GET_GPU_MEMORY" in el[1:]))):
                self.mark_hcfr()
            if el and el[0] == "MULTIPLE":
                resp = "\n" + "".join("%s:%s\n" % (c, self.pgenerator_cmd(c)) for c in el[1:])
            else:
                resp = self.pgenerator_cmd(m.group(1))
            return ok("OK:" + (resp[:-1] if resp.endswith("\n") else resp))
        if "STATS" in key:
            if "STATSRESET" in key:
                cal.stats.clear()      # stats("RESET"): its unlink of the cached GET_STATS
                return ok("OK:%d" % cal.info_forget("GET_STATS"))
            return ok("OK:" + self.stats_text())
        m = re.search(r"GETFILELIST:(.*)", key)
        if m:                           # the template directories are the ones modelled
            return ok("OK:" + cal.templates.listing(m.group(1)))
        m = re.match(r"DELETE:(.*)", key)
        if m:
            el = split_perl(m.group(1), ":") + ["", ""]
            if el[1] != "":
                cal.templates.delete(el[0], el[1])
            if el[0] == "PLUGINS":
                # the reply is set_plugin's output (PGenerator_cmd.pl:1172-1182): with no such
                # archive (none is ever installed) Digest::MD5 dies before printing anything
                return ok("")
            return ok()
        if re.search(r"VIDEO=(.*)", key):
            cal.clean_files()           # play_video stops the renderer (pattern.pm:176)
            return ok()
        m = re.search(r"SAVEIMAGES:(.*):", key)
        if m:                           # frames/pattern.info; no frames are ever saved
            cal.images_name = m.group(1)
            return ok()
        m = re.search(r"GETPATTERNIMAGE:(.*)", key)
        if m:
            return ok("OK" if m.group(1) == cal.images_name else "None")
        m = re.search(r"GETPATTERNIMAGESLIST:(.*)", key)
        if m:
            return ok("OK:" + ("Ready" if m.group(1) == cal.images_name != "" else ""))
        m = re.search(r"(TESTTEMPLATE.*):(.*):(.*)", key)
        if m:
            ptype, template, payload = m.groups()
            rng = ""
            if template == "HCFR":
                self.mark_hcfr()
                rng = hcfr_source_range(payload, cal.conf)
            else:
                self.set_status("DeviceControl")
            payload, exact = templates.prepare_payload(payload, cal.std_dv(), template == "HCFR")
            reply = cal.template_pattern(ptype, template, payload, rng,
                                         12 if exact is not None else 0, exact)
            cal.clean_pattern_files()
            self.drew = self.drew or reply == "OK"
            return ok(reply)
        m = re.search(r"TESTPATTERN:(.*):(.*):(.*):(.*):(.*):", key)
        if m:
            draw, rgb, rng = m.group(2), m.group(5), ""
            if self.hcfr:
                self.set_status("HCFR")
                draw, rng = hcfr_draw(draw), hcfr_source_range(rgb, cal.conf)
            else:
                self.set_status("DeviceControl")
            cal.clean_pattern_files()
            return ok("OK:" + self.create_pattern(draw, m.group(3), rgb, "", "", rng, True,
                                                  m.group(4)))
        m = re.search(r"GETCONF:(.*):(.*)", key)
        if m:
            return ok("OK:" + cal.templates.get_conf(m.group(1), m.group(2)))
        m = re.search(r"SETCONF:(.*):(.*):(.*)", key, re.S | re.M)
        if m:
            el = key.split(":", 3)
            if el[1] == "HCFR":
                self.mark_hcfr()
            cal.templates.set_conf(el[1], el[2], el[3])
            return ok()
        if re.search(r"FUNCTIONS=(.*)", key):     # execute_functions: no *.server files
            self.set_status("HCFR" if self.hcfr else "DeviceControl")
            return ok()
        m = re.search(r"RGB=(.*)", key)
        if m:
            self.classic_rgb(m.group(1))
            return ok()
        if re.search(r"PGENERATORISEXECUTED:(.*)", key):
            return ok("Pid %d" % os.getpid())
        return ok(self.error())

    def classic_rgb(self, payload):
        """RGB=DRAW;DIMENSIONS;RESOLUTION;RGB;BACKGROUND;POSITION;TEXT (unscaled pixels)."""
        draw, dim, _res, rgb, bg, pos, text = (payload.split(";") + [""] * 7)[:7]
        marker = ((perl_uc(draw) == "IMAGE" and re.match(r"/var/lib/PGenerator/images-HCFR/",
                                                          text, re.I | re.A))
                  or (perl_uc(draw) == "TEXT" and re.match(
                      r"(Init|End of sequence|Initializing PGenerator at:)", text)))
        rng = ""
        if marker:
            self.mark_hcfr()
        if self.hcfr:
            self.set_status("HCFR")
            draw = hcfr_draw(draw)
            if not marker:
                rng = hcfr_source_range("%s;%s" % (rgb, bg), self.cal.conf)
        else:
            self.set_status("DeviceControl")
        if marker and perl_uc(draw).startswith("TEXT"):
            return
        self.cal.clean_pattern_files()
        self.create_pattern(draw, dim, rgb, bg, pos, rng, False, _res, text)

    # -- client identity (legacy_external_set_status / _set_client_name) -----------
    def set_status(self, software):
        if software == "DeviceControl" and self.client_name not in ("", "0"):   # Perl-false
            software = self.client_name
        self.cal.set_client(self.peer, software)

    def mark_hcfr(self):
        self.hcfr = True
        self.set_status("HCFR")

    def set_client_name(self, name):
        name = re.sub(r"[^A-Za-z0-9 ._+()/-]", "", _or(name, "").strip(PERL_WS))[:64]
        if name:
            self.client_name = name
            self.set_status(name)
        return name

    def create_pattern(self, draw, dim, rgb, bg, pos, source_range, scaled, res="", text=""):
        """legacy_external_prepare_dv_pattern + pattern.pm create_pattern_file(simple=1) and
        the renderer's shapes. Returns what create_pattern_file returns (stats(): "1" or "0"
        drawn, "" rejected, "ERR" bad position)."""
        cal, sig = self.cal, self.cal.gen.sig
        el = split_perl(rgb)
        depth = 0                   # DV: the codes' own depth (README: no <<2 to 12-bit)
        bg_exact = None             # DV: the background's own-depth codes, when scaled
        if cal.std_dv():
            depth = templates.source_bits("", draw)
            # legacy_external_prepare_dv_pattern: rgb and bg each scaled on their own form;
            # one passed through unscaled is read as 12-bit codes
            _, bg_exact = templates.dv_triplet(bg, depth)
            _, rgb_exact = templates.dv_triplet(rgb, depth)
            if rgb_exact is not None:
                el = [str(v) for v in rgb_exact]
            else:
                depth = 12
            draw = re.sub(r"(?:8|10|12)bit$", "", draw, flags=re.I | re.A)
        if len(el) != 3:
            return ""
        m = re.search(r"([A-Z]+)(\d+)bit", draw)
        bits = dec_int(m.group(2)) if m and not depth else cal.bits_default()   # bits_default
        draw = m.group(1) if m else draw
        # (1 << bits) - 1: a shift of the word size or more is 0 on a 64-bit Perl, so -1
        hi = 4095 if depth else (-1 if bits >= 64 else (1 << bits) - 1) if bits > 8 else 255
        codes = [perl_num(v) for v in el]
        if any(not 0 <= v <= hi for v in codes):
            return ""
        cmax = (1 << depth) - 1 if depth else hi        # 0..8 bit: 8-bit codes, as accepted
        dims = split_perl(dim) + ["", ""]
        w, h = perl_num(dims[0]), perl_num(dims[1])
        if scaled:          # TESTPATTERN: DIM x w_s/max_x, 1 once PGenerator has read the mode
            w, h = w + 0.5, h + 0.5
            w, h = (math.trunc(v) if math.isfinite(v) else v for v in (w, h))   # round_val
        p = (pos if pos != "" else "-1,-1").split(",") + ["", "", "", ""]
        x, y, dx, dy = (perl_num(v) for v in p[:4])
        dx, dy = (0 if w == sig.width else dx), (0 if h == sig.height else dy)
        if draw == "RECTANGLE":
            x = (sig.width - w) / 2 if x == -1 else x
            y = (sig.height - h) / 2 if y == -1 else y
        elif draw in ("CIRCLE", "TRIANGLE"):
            x = sig.width / 2 if x == -1 and w != sig.width else x
            y = sig.height / 2 if y == -1 and h != sig.height else y
        # int() leaves NaN, Inf and anything outside IV/UV an NV ("3e+19"): pattern.pm:112
        if not all(math.isfinite(v) and -2 ** 63 < math.trunc(v) < 2 ** 64
                   for v in (x + dx, y + dy)) or (draw == "RECTANGLE" and not all(
                       math.isfinite(v) for v in (w, h))):
            return self.error()
        x, y = int(x + dx), int(y + dy)
        w, h = (int(v) if math.isfinite(v) else 0 for v in (w, h))    # used by RECTANGLE only
        fg = Code([int(v) for v in codes], cmax)
        limited = source_range == "LIMITED"
        # the renderer's values: a line it cannot read keeps the previous one (shapes.py)
        st = cal.rstate
        st.draw_type = draw
        r = shapes.c_int(res)
        st.resolution = st.resolution if r is None else r
        if draw == "IMAGE":         # the file's IMAGE= line (pattern.pm:115), else TEXT=
            st.image = text
        else:
            st.text = text
        # the file's RGB=, BG=, POSITION= and BITS= lines, which a later template without
        # them draws with
        st.rgb = shapes.Codes(fg.codes, cmax) if depth and depth != 12 else tuple(fg.codes)
        st.bg = bg_exact or tuple(shapes.c_ints(bg if bg != "" else "0,0,0", 3) or st.bg)
        st.pos = tuple(shapes.c_ints("%d,%d" % (x, y), 2) or st.pos)
        st.bits = bits
        st.dim, st.limited = (0, 0), 0      # the file's END resets them (ofApp.cpp:180-184)
        if st.bg[0] == -1:
            background = st.background      # BG=-1: not cleared, the last colour stays (ofApp)
        else:                               # an unreadable BG keeps the renderer's last one
            background = st.background = shapes.Paint(st.bg, 4095 if depth else cmax,
                                                      limited)
        kind = shapes.DRAW_NUM.get(draw, 0)
        radius = w if all(-2 ** 31 <= v < 2 ** 31 for v in (w, h)) else 0   # DIM unread: 0
        res_n = st.resolution

        if kind == 0:               # ofApp::draw exit(0) on an unknown DRAW: black, and the next
            cal.rstate = shapes.RendererState()     # pattern starts a new renderer
            background = None

        def scene_fn(s):
            s.calman_range = 1 if limited else None
            scene = Scene(s.width, s.height,
                          background.colour(s) if background else Colour.gray(0.0), "patch")
            if kind == 0:
                return scene
            if draw == "RECTANGLE" and shapes._fits(x, y, w, h):
                # ofApp::rectangle: x -1 centres both axes, a negative size spans backwards
                shapes.draw_shape(scene, 1, x, y, w, h, res_n, text, fg.colour(s))
            elif draw == "RECTANGLE":
                x1, y1 = max(0, x), max(0, y)
                x2, y2 = min(s.width, x + w), min(s.height, y + h)
                if x2 > x1 and y2 > y1:
                    scene.add(x1, y1, x2 - x1, y2 - y1, fg.colour(s))
            elif kind in (2, 3, 4):         # IMAGE: no image files, the background only
                shapes.draw_shape(scene, kind, x, y, radius, 0, res_n, text, fg.colour(s))
            return scene
        self.drew = True
        counted = cal.stat("patterns")
        cal._draw(scene_fn, False)
        return str(counted)

    def stats_text(self):
        return ",".join("%s: %s" % kv for kv in self.cal.stats.items())

    def pgenerator_cmd(self, cmd):
        """command.pm pgenerator_cmd: the device_info thread's cached answer, else
        get_cmd_generic's, then the SET_ side effects."""
        cached = self.info_answer(cmd)
        if cached is not None:
            return cached
        resp = self.cmd_generic(cmd)
        self.cmd_set(cmd)
        if re.search(r"SET_REFRESH:(.*)", cmd):     # command.pm:863-880, the KMS branch
            resp = "ERR:Error with tvservice"
        return resp

    def info_answer(self, cmd):
        """$info_dir/<cmd>.info while the device_info thread's last cycle wrote it and nothing
        unlinked it since: each conf key's raw value then (min_luma in nits, info.pm:45-48)
        and GET_PGENERATOR_CONF_ALL. Only these are modelled."""
        cal = self.cal
        if not cmd.startswith("GET_PGENERATOR_CONF_") or not cal.info_cached(cmd):
            return None
        if cmd == "GET_PGENERATOR_CONF_ALL":
            return base64.b64encode(conf_lines(cal.info_conf).encode("latin-1", "replace")).decode()
        for k, v in cal.info_conf.items():
            if cmd == "GET_PGENERATOR_CONF_" + perl_uc(k):
                return v
        return None

    def cmd_generic(self, cmd):
        """command.pm get_cmd_generic, for what this box can answer ("" otherwise)."""
        cal = self.cal
        if re.search(r"(^GET_ALL_IPMAC|^WIFI^BT|^ETH|^GET_IP|^GET_MAC)", cmd):
            resp = sysinfo.NONE
        else:
            resp = ""
        if cmd == "GET_STATUS":
            return "Alive"
        if cmd == "GET_HOSTNAME":
            return sysinfo.hostname()
        if cmd == "GET_DEVICE_MODEL":
            return sysinfo.device_model()
        if cmd in ("GET_SCALING_GOVERNOR", "GET_SCALING_GOVERNOR_AVAILABLE",
                   "GET_SCALING_GOVERNOR_CUR_FREQ"):
            return sysinfo.cpufreq(cmd)
        if re.match(r"(GET_CPU_INFO|GET_CPU_HARDWARE|GET_CPU_REVISION|GET_CPU_SERIAL)", cmd):
            info = sysinfo.cpu_info()
            i = ("GET_CPU_HARDWARE", "GET_CPU_REVISION", "GET_CPU_SERIAL").index(cmd) \
                if cmd in ("GET_CPU_HARDWARE", "GET_CPU_REVISION", "GET_CPU_SERIAL") else None
            if i is None:
                return info
            el = re.split(r"[ \t\n\r\f\v]+", info.strip(PERL_WS))     # split(" ",$response)
            return el[i] if i < len(el) else ""
        if cmd in ("FREE_DISK", "GET_FREE_DISK"):
            return sysinfo.free_disk()
        if cmd.startswith("GET_DMESG"):
            return ""       # the box's kernel log is not handed to whoever asks on the network
        if cmd == "BTMAC":
            return sysinfo.bt_mac()
        if cmd in ("STATSGET", "GET_STATS"):
            return self.stats_text()
        if cmd in ("PGENERATORISEXECUTED", "GET_PGENERATOR_IS_EXECUTED"):
            return "Pid %d" % os.getpid()
        if cmd == "GET_PGENERATOR_VERSION":
            return VERSION
        if cmd == "GET_TEMPERATURE":
            return sysinfo.temperature()
        if cmd == "GET_CPU":
            return self.cpu_usage()
        if cmd in ("ETH", "WIFI", "BT"):
            return sysinfo.ip_address({"ETH": "eth0", "WIFI": "wlan0", "BT": "bnep0"}[cmd])
        if cmd in ("ETHMAC", "WIFIMAC"):
            # get_mac's greedy /link\/ether (.*) / takes " brd" in: "AA:BB:..:FFBRD"
            mac = sysinfo.mac_address("eth0" if cmd == "ETHMAC" else "wlan0")
            return mac if mac == sysinfo.NONE else mac + "BRD"
        if re.match(r"(GET_MODES_AVAILABLE|GET_CEA_DMT_AVAILABLE)", cmd):
            return base64.b64encode(modes_available().encode("latin-1")).decode()
        if cmd in ("GET_MODE", "GET_CEA_DMT"):
            return mode_line(cal.reported_mode()[0])
        m = re.search(r"GET_PGENERATOR_CONF_(.*)", cmd)
        if m:
            var = m.group(1).lower()
            if var == "all":
                return base64.b64encode(conf_lines(cal.conf).encode("latin-1", "replace")).decode()
            return conf_value(cal.conf, var)
        if re.match(r"GET_EDID_INFO", cmd):
            return base64.b64encode(sysinfo.edid_info().encode()).decode()
        if re.match(r"(GET_HDMI_INFO|GET_REFRESH|GET_OUTPUT_RANGE)$", cmd):
            resp = self.hdmi_info()
        if cmd == "GET_REFRESH":
            # deviates from command.pm:1206 (word 4 of the tvservice-style line, which on KMS
            # is the mode index): the refresh rate
            fps = cal.reported_mode()[4]
            return "%.2f" % (fps[0] / float(fps[1]))
        if cmd == "GET_RESOLUTION":
            return "%dx%d" % cal.reported_mode()[1:3]
        if cmd == "GET_OUTPUT_RANGE":
            return " ".join(self.output_format())
        if cmd == "GET_DISCOVERABLE":
            return "1" if cal.discoverable else "0"
        m = re.match(r"(GET_IP|GET_MAC)-(.*)", cmd)
        if m:
            name = m.group(2)
            if not sysinfo.is_valid_ifname(name) or name not in sysinfo.interfaces():
                return sysinfo.NONE
            return (sysinfo.ip_address(name) if m.group(1) == "GET_IP"
                    else sysinfo.mac_address(name))
        if cmd in ("GET_UP_FROM", "UP_FROM"):
            return sysinfo.up_from()
        if cmd in ("GET_LA", "LA"):
            return sysinfo.load_average()
        if cmd in ("FREE_MEM", "GET_FREE_MEM"):
            return sysinfo.free_mem()
        return resp

    def cmd_set(self, cmd):
        """pgenerator_cmd's setters. Conf writes take effect at the next renderer restart
        (RESTARTPGENERATOR), as in PGenerator; REBOOT/HALT and the system settings
        (hostname, Wi-Fi, boot config) are not the add-on's to change."""
        cal = self.cal
        m = re.search(r"SET_DISCOVERABLE:(.*)", cmd)
        if m:
            cal.set_discoverable(m.group(1) not in ("", "0"))
        m = re.search(r"SET_PGENERATOR_CONF_(IS_SDR|IS_HDR|IS_LL_DOVI|IS_STD_DOVI|EOTF|PRIMARIES|"
                      r"MAX_LUMA|MIN_LUMA|MAX_CLL|MAX_FALL|COLOR_FORMAT|COLORIMETRY|"
                      r"RGB_QUANT_RANGE|MAX_BPC|DV_STATUS|DV_INTERFACE|DV_PROFILE|DV_MAP_MODE|"
                      r"DV_MINPQ|DV_MAXPQ|DV_DIAGONAL|MODE_IDX|DV_METADATA|DV_COLOR_SPACE|"
                      r"DV_TRANSPORT|SIGNAL_MODE|CALMAN_MODE_IDX):(.*)", cmd)
        if m:
            self.set_conf(m.group(1).lower(), m.group(2))
        m = re.search(r"(SET_MODE|SET_CEA_DMT):(.*)", cmd)
        if m:                   # stored as sent; the restarted renderer takes atoi() of it
            cal.set_mode(m.group(2))

    def set_conf(self, key, value):
        """SET_PGENERATOR_CONF_<KEY>: the conf key, with command.pm's couplings."""
        cal, c = self.cal, self.cal.conf
        if key == "min_luma":           # pg_min_luma_cmd_wire_to_nits: 0.0001-nit units
            v = perl_num(value)
            value = "NaN" if v != v else "%.10g" % (max(0, min(65535, v)) * 0.0001)
        if key == "mode_idx":           # stored; the renderer takes it at the next restart
            cal.pending_mode = True
        cal.info_forget("GET_PGENERATOR_CONF_" + perl_uc(key))
        cal.info_forget("GET_PGENERATOR_CONF_ALL")
        before = c.get(key)
        c[key] = value
        if key == "dv_metadata":
            mm = {"2": "0", "3": "1", "4": "2"}.get(value, "")
            if mm != "" and c.get("dv_map_mode", "") != mm:
                c["dv_map_mode"] = mm
        elif key == "dv_map_mode":
            md = cal._dv_metadata_for(value)
            if c.get("dv_metadata", "") != md:
                c["dv_metadata"] = md
        if key in ("dv_map_mode", "dv_metadata"):
            cal.map_chosen = True
        if ((key == "dv_status" and value == "1") or
                (key in ("is_ll_dovi", "is_std_dovi") and value == "1") or
                (key in ("max_bpc", "color_format", "rgb_quant_range", "dv_interface",
                         "dv_transport") and perl_int(c.get("dv_status") or 0) == 1)):
            cal.normalize_dv()
        if key == "calman_mode_idx":
            cal.save_mode_idx()
        cal.sync()
        if key in LUMINANCE_KEYS and before != value and cal.shown is not None and \
                cal.gen.sig.mode in ("hdr10", "dv"):
            # the renderer reads the luminance keys on every frame: the clip carries them
            cal.stale = True

    def output_format(self):
        r = self.cal.rend or self.cal.conf
        # the connector's numeric values, which the renderer sets with atoi (main.cpp:122-123)
        return ({1: "YCbCr444", 2: "YCbCr422", 3: "YCbCr420"}.get(c_atoi(r["color_format"]), "RGB"),
                {1: "limited", 2: "full"}.get(c_atoi(r["rgb_quant_range"]), "default"))

    def hdmi_info(self):
        """GET_HDMI_INFO: the tvservice-style line without "state 0xa [HDMI " and "]"."""
        idx, w, h, scan, (n, d) = self.cal.reported_mode()
        fmt, rng = self.output_format()
        ratio = "4:3" if h and w * 3 == h * 4 else "16:9"
        return "MODETEST (%d) %s %s %s, %dx%d @ %.2fHz, %s" % (
            idx if idx is not None else 0, fmt, rng, ratio, w, h, n / float(d),
            "interlaced" if scan == "i" else "progressive")

    def cpu_usage(self):
        """get_cpu: busy share of /proc/stat since the previous call."""
        usage = 0
        try:
            with open("/proc/stat") as f:
                for line in f:
                    if re.match(r"cpu\s+", line, re.A):
                        t = [int(v) for v in line.split()[1:]]
                        total, idle = sum(t), t[3]
                        dt, di = total - self.cal.cpu_prev[0], idle - self.cal.cpu_prev[1]
                        usage = 100.0 * (dt - di) / dt if dt > 0 else 0
                        self.cal.cpu_prev = (total, idle)
        except (OSError, ValueError, IndexError):
            pass
        return "%d%%" % int(usage)

    def terminate(self):
        """TERM/SHUTDOWN/QUIT: reset the pattern state, release the range; the picture
        stays unless the release restarts the renderer (PatternStart)."""
        self.cal.reset()
        self.cal.release()
        self.clear_gci()
        self.closed = True
        return ACK, True

    def clear_gci(self):
        """calman_clear_gci_connection: the conf's calman_gci drops to 0."""
        if self.gci:
            self.gci = False
            self.cal.conf["calman_gci"] = "0"

    def close(self):
        """close_connection (daemon.pm:2765-2778): an HCFR-marked client releases the source
        range (command.pm:83-87, also Calman's), a GCI one clears calman_gci."""
        if self.hcfr:
            self.hcfr = False
            self.cal.release("hcfr")
        self.clear_gci()
        self.cal.refresh()

    def dispatch(self, key, ctype, p):
        cal = self.cal
        handler = getattr(self, "c_" + ctype, None) if re.match(r"\w+$", ctype, re.A) else None
        if ctype == "INIT":
            cal.reset()
            cal.init_mode()
            if re.search(r"\s*2\.0", p, re.A):
                self.gci = cal.gci = True
                cal.conf["calman_gci"] = "1"
            return
        if self.rpc and self.rpc_alias(perl_uc(ctype), p):
            return
        m = re.match(r"^\x02?SPECIALTY:([^\x02\x03]+)\x02CONF_LEVEL:Range\s+([^\x03]+)\x03?$",
                     key, re.I | re.A)
        if m:
            return self.specialty_combo(m.group(1).strip(PERL_WS), m.group(2).strip(PERL_WS))
        if handler is not None and ctype in EARLY:
            return handler(p)
        if ctype == "CommandRGB":
            cal.apply(False)
            self.drew = True
            cal.render_commandrgb(p)
            return cal.remember("CommandRGB", ctype, p)
        if "RGB_" in ctype:
            cal.apply(False)
            self.drew = True
            cal.render_rgb(ctype, p, key)
            return cal.remember("RGB", ctype, p)
        if "YCC_" in ctype:             # handled like RGB_ (daemon.pm:2171-2176); see Code.colour
            cal.apply(False)
            cal.apply_source(cal.range)  # daemon.pm:2182, which YCC_ reaches in PGenerator
            self.drew = True
            cal.render_rgb(ctype, p)
            return cal.remember("YCC", ctype, p)
        cal.apply_source(cal.range)      # daemon.pm runs this for every later command
        if handler is not None:
            handler(p)
        else:
            self.log("Calman: unhandled command %s ignored" % ctype)

    # -- RPC-only aliases -------------------------------------------------------
    def rpc_alias(self, alias, p):
        cal = self.cal
        if alias in ("BITDEPTH", "BITS"):
            bpc = cal.note_explicit_bpc(p)
            if bpc:
                cal.save("max_bpc", bpc)
                cal.apply()
            return True
        if alias in ("COLORSPACE", "COLOR_FORMAT", "FORMAT"):
            self.source_payload(p, 0)
            cal.apply()
            return True
        if alias == "RANGE":
            rng = parse_source_format("Range=" + p)[2]
            if rng in ("1", "2"):
                cal.set_range(int(rng))
                cal.apply_source(cal.range)
            else:
                cal.release_range()
            cal.replay_last()
            return True
        if alias == "CMD":
            m = re.match(r"^SET_PGENERATOR_CONF_MAX_BPC:(8|10|12)$", p, re.I | re.A)
            if m:
                if cal.note_explicit_bpc(m.group(1)):
                    cal.save("max_bpc", m.group(1))
                    cal.apply()
                return True
            m = re.match(r"^SET_PGENERATOR_CONF_COLOR_FORMAT:([0-3])$", p, re.I | re.A)
            if m:
                cal.save("color_format", m.group(1))
                cal.apply()
                return True
            m = re.match(r"^SET_PGENERATOR_CONF_RGB_QUANT_RANGE:([012])$", p, re.I | re.A)
            if m:
                cal.save("rgb_quant_range", m.group(1))
                if not self.gci:
                    if m.group(1) in ("1", "2"):
                        cal.set_range(int(m.group(1)))
                        cal.apply_source(cal.range)
                    else:
                        cal.release_range()
                cal.replay_last()
                return True
        return False

    def source_payload(self, payload, default_bpc):
        """calman_apply_source_payload."""
        cal = self.cal
        cf, bpc, rng = parse_source_format(payload, default_bpc)
        if cf:
            cal.save("color_format", cf)
        if bpc and cal.note_explicit_bpc(bpc):
            cal.save("max_bpc", bpc)
        if rng and not self.gci:
            if rng in ("1", "2"):
                cal.set_range(int(rng))
                cal.apply_source(cal.range)
            else:
                cal.release_range()

    def _range_command(self, q, save=True):
        """QRNG/SetRange/CONF_LEVEL Range: 1 limited, 2 full, None releases."""
        cal = self.cal
        if q in (1, 2):
            cal.set_range(q)
            cal.save("rgb_quant_range", q)
            cal.apply_source(q)
        else:
            cal.release_range()
            if save:
                cal.save("rgb_quant_range", cal.range)

    def specialty_combo(self, name, value):
        """SPECIALTY:<name> STX CONF_LEVEL:Range <value> in one frame: one ACK."""
        cal = self.cal
        q = 2 if re.search("full", value, re.I | re.A) else 1 if re.search("limit", value, re.I | re.A) else None
        self._range_command(q)
        if not self.gci:
            cal.apply(False)
        self.drew = True
        cal.render_specialty(name)
        cal.remember("SPECIALTY", "SPECIALTY", name)

    # -- commands handled before the pattern commands -----------------------------
    def c_HDR_ENABLE(self, p):
        if re.match(r"^False$", p, re.I | re.A):
            self.cal.set_non_dv(0)
            self.cal.apply()
        if re.match(r"^True$", p, re.I | re.A):
            self.cal.set_non_dv(2)
            self.cal.apply()

    def c_CONF_HDR(self, p):
        cal = self.cal
        f = split_perl(p)
        num = [perl_num(_field(f, i)) for i in range(9)]
        present = [_field(f, i) != "" for i in range(13)]
        eotf = 2
        if re.match(r"^SDR$", _field(f, 0), re.I | re.A) or re.match(r"^Traditional$", _field(f, 0), re.I | re.A):
            eotf = 0
        if re.match(r"^HLG$", _field(f, 0), re.I | re.A):
            eotf = 3
        cal.save("eotf", eotf)
        if eotf >= 2:
            cal.set_non_dv(eotf)
        else:
            cal.set_non_dv(0)
        rx = num[1]
        if abs(rx - 0.708) < 0.01:
            prim, clr = "1", "9"
        elif abs(rx - 0.680) < 0.01:
            prim, clr = "2", "9"
        else:
            prim, clr = "0", "2"
        cal.save("colorimetry", clr)
        cal.save("primaries", prim)
        cal.save("max_bpc", cal.preferred_bpc("10" if eotf >= 2 else "8", eotf >= 2))
        # NaN values are ignored (deviation, see _luminance)
        if present[9] and perl_num(f[9]) == perl_num(f[9]):
            cal.save("min_luma", perl_num(f[9]))
        if present[10] and perl_num(f[10]) == perl_num(f[10]) and perl_int(f[10]) > 0:
            cal.save("max_luma", perl_int(f[10]))
        if present[11] and perl_num(f[11]) == perl_num(f[11]):
            cal.save("max_cll", perl_int(f[11]))
        if present[12] and perl_num(f[12][:5]) == perl_num(f[12][:5]):
            cal.save("max_fall", perl_int(f[12][:5]))
        cal.apply()

    def c_DSMD(self, p):
        cal = self.cal
        restart = False
        for pat, eotf in ((r"^SDR$", 0), (r"^HDR10$", 2), (r"^HLG$", 3)):
            if re.match(pat, p, re.I | re.A):
                cal.set_non_dv(eotf)
                restart = True
        if re.match(r"^DOLBYVISION$", p, re.I | re.A) or re.match(r"^DV$", p, re.I | re.A):
            cal.set_dv("", "1")
            restart = True
        if restart:
            cal.apply()

    def c_21_HDR_MetadataMode(self, p):
        cal = self.cal
        v = perl_int(p)
        if v == 0:
            cal.set_non_dv(0)
        elif v in (2, 3, 4):
            cal.set_dv(str(v - 2), str(v))
        elif v == 1:
            cal.set_dv("", "1")
        cal.apply()

    def c_EOTF(self, p):
        cal = self.cal
        v = perl_int(p)
        if 0 <= v <= 3:
            cal.save("eotf", v)
            if v >= 2:
                cal.set_non_dv(v)
            else:
                cal.set_non_dv(0)
            cal.apply()

    c_HDR_EOTF = c_EOTF

    def c_PRIM(self, p):
        prim = p
        if re.match(r"^\d+$", p, re.A):
            prim = {1: "0", 2: "1", 0: "2"}.get(dec_int(p), p)
        if re.match(r"^BT709$", p, re.I | re.A):
            prim = "0"
        if re.match(r"^BT2020$", p, re.I | re.A):
            prim = "1"
        if re.match(r"^P3$", p, re.I | re.A) or re.match(r"^DCI.?P3$", p, re.I | re.A):
            prim = "2"
        self.cal.save("primaries", prim)
        self.cal.save("colorimetry", "2" if prim == "0" else "9")

    c_HDR_PRIMARIES = c_PRIM

    def c_CLSP(self, p):
        self.cal.save("colorimetry", "9" if re.match(r"^(?:BT)?2020$", p, re.I | re.A) else "2")

    def c_BITD(self, p):
        bpc = self.cal.note_explicit_bpc(p)
        if bpc:
            self.cal.save("max_bpc", bpc)
            self.cal.apply()

    def c_COLF(self, p):
        self.source_payload(p, 0)
        self.cal.apply()

    def c_QRNG(self, p):
        q = 2 if re.match(r"^FULL$", p, re.I | re.A) else 1 if re.match(r"^LIMITED$", p, re.I | re.A) else None
        self._range_command(q)
        self.cal.replay_last()

    def _luminance(self, key, p):
        cal = self.cal
        if perl_num(p) != perl_num(p):
            # deviates from daemon.pm:2058-2089: PGenerator stores NaN and later dies drawing
            self.log("Calman: ignoring NaN %s %r" % (key, p))
            return
        before = cal.conf.get(key)
        cal.save(key, p)
        # deviates from daemon.pm:2058-2089 (saved, read per frame by the renderer): the picture
        # shown is re-encoded
        if cal.conf.get(key) != before and cal.gen.sig.mode in ("hdr10", "dv"):
            cal.reencode()

    def c_MAXL(self, p):
        self._luminance("max_luma", p)

    def c_MINL(self, p):
        self._luminance("min_luma", p)

    def c_MAXCLL(self, p):
        self._luminance("max_cll", p)

    def c_MAXFALL(self, p):
        self._luminance("max_fall", p)

    c_HDR_MAXL, c_HDR_MINL, c_HDR_MAXCLL, c_HDR_MAXFALL = c_MAXL, c_MINL, c_MAXCLL, c_MAXFALL

    def c_HDR_WHITEPOINT(self, p):
        pass

    def c_SetRange(self, p):
        q = 1 if perl_int(p) == 1 else 2
        self._range_command(q)
        self.cal.replay_last()

    def c_10_SIZE(self, p):
        v = perl_int(p)
        if v == v:      # NaN: PGenerator stores it and dies drawing (deviation: ignored)
            self.cal.win = 10 if v < 1 else min(100, v)
        self.cal.replay_last()

    def c_11_APL(self, p):
        v = float(perl_num(p))
        if v == v:      # NaN: PGenerator stores it and dies drawing (deviation: ignored)
            self.cal.apl = max(0.0, min(100.0, v))
        self.cal.apl_enabled = True
        self.cal.replay_last()

    def c_303_UPDATE(self, p):
        if not self.cal.apply():
            self.cal.replay_last()

    c_APPLY = c_303_UPDATE

    # -- commands after daemon.pm's stray apply_source_rgb_quant_range -----------
    def c_CONF_FORMAT(self, p):
        cal = self.cal
        fmt = p.strip(PERL_WS)
        self.source_payload(fmt, 0)
        req = parse_conf_format(fmt, cal.reported_mode()[4])     # $preferred_mode's rate
        if req is not None:
            cal.set_mode(str(req))
            cal.remember_mode_idx(req)
        else:
            self.log("Calman: CONF_FORMAT not understood: %s" % fmt)
        cal.apply(False)
        cal.replay_last()

    def c_CONF_LEVEL(self, p):
        cal = self.cal
        cl = p.strip(PERL_WS)
        m = re.match(r"^Bits\s+(\d+)", cl, re.I | re.A)
        if m:
            bpc = cal.note_explicit_bpc(m.group(1))
            if bpc:
                cal.save("max_bpc", bpc)
            cal.apply()
            return
        m = re.match(r"^Range\s+(.*)", cl, re.I | re.A)
        if m:
            rv = m.group(1).lower()
            q = 2 if "full" in rv else 1 if "limit" in rv else None
            self._range_command(q, save=False)
            cal.replay_last()
            return
        m = re.match(r"^Format\s+(.*)", cl, re.I | re.A)
        if m:
            self.source_payload(m.group(1), 1)
            cal.apply()
        elif re.match(r"^Gamma-HDR$", cl, re.I | re.A):
            cal.set_non_dv(2)
        elif re.match(r"^Gamma-SDR$", cl, re.I | re.A):
            cal.set_non_dv(0)

    def c_CONF_DV(self, p):
        v = {"PERCEPTUAL": ("0", "2"), "ABSOLUTE": ("1", "3"),
             "RELATIVE": ("2", "4")}.get(perl_uc(p).strip(PERL_WS))
        if v:
            self.cal.set_dv(*v)
            self.cal.apply()

    def c_SPECIALTY(self, p):
        self.cal.apply(False)
        self.drew = True
        self.cal.render_specialty(p)
        self.cal.remember("SPECIALTY", "SPECIALTY", p)

    def c_UPDATE(self, p):
        pass


# handled before CommandRGB / RGB_ and the stray range line in daemon.pm
EARLY = {"HDR_ENABLE", "CONF_HDR", "DSMD", "21_HDR_MetadataMode", "EOTF", "HDR_EOTF", "PRIM",
         "HDR_PRIMARIES", "CLSP", "BITD", "COLF", "QRNG", "MAXL", "HDR_MAXL", "MINL",
         "HDR_MINL", "MAXCLL", "HDR_MAXCLL", "MAXFALL", "HDR_MAXFALL", "HDR_WHITEPOINT",
         "SetRange", "10_SIZE", "11_APL", "303_UPDATE", "APPLY"}


def normalise(chunk):
    """daemon.pm: drop the first STX CR or ETX, then trailing CR/LF."""
    key = re.sub("\x02\r|\x03", "", chunk, count=1)
    return re.sub(r"[\r\n]+$", "", key, count=1)


def tune_client_socket(conn, idle=60, interval=30, count=10):
    """A calibration PC on poor Wi-Fi: acknowledgements go out at once (no Nagle delay).
    The server never closes a connection itself; keepalive only reaps a peer silent for
    ~6 min by default (60 s idle, then 10 probes 30 s apart), so a Wi-Fi outage of minutes
    still finds the connection alive, while one Calman abandoned is eventually freed."""
    for level, opt, value in ((socket.IPPROTO_TCP, "TCP_NODELAY", 1),
                              (socket.SOL_SOCKET, "SO_KEEPALIVE", 1),
                              (socket.IPPROTO_TCP, "TCP_KEEPIDLE", idle),
                              (socket.IPPROTO_TCP, "TCP_KEEPINTVL", interval),
                              (socket.IPPROTO_TCP, "TCP_KEEPCNT", count)):
        if hasattr(socket, opt):
            try:
                conn.setsockopt(level, getattr(socket, opt), value)
            except OSError:
                pass


class Server:
    """TCP listeners for the UPGCI (2100), RPC (2101) and classic (85) ports, RPC discovery
    (3529), DeviceControl discovery (1977) and the LightSpace client (20123). A port of 0
    is not opened."""

    def __init__(self, gen, info, log, port=2100, rpc_port=2101, discovery=True,
                 discovery_port=3529, discovery_reply_port=3530, format_file=None,
                 classic_port=0, devicecontrol_port=0, lightspace_port=0, lightspace_timeout=5.0):
        self.gen = gen
        self.info = info
        self.log = log
        self.ports = (([(port, False)] if port else []) + ([(rpc_port, True)] if rpc_port else [])
                      + ([(classic_port, False)] if classic_port else []))
        self.rpc_port = rpc_port
        self.classic_port = classic_port
        self.discovery = discovery and bool(rpc_port)
        self.discovery_port = discovery_port
        self.discovery_reply_port = discovery_reply_port
        self.devicecontrol_port = devicecontrol_port
        with gen.lock:
            self.calman = Calman(gen, log, format_file)
        self.stopping = threading.Event()
        self.sockets = []
        self.clients = set()
        self.origin = {}            # client socket -> (peer ip, local port, accept order)
        self.accepted = 0
        self.lock = threading.Lock()
        self.lightspace = None
        if lightspace_port:
            from .lightspace import LightSpace
            self.lightspace = LightSpace(self, lightspace_port, lightspace_timeout)

    def client(self):
        """(ip, software) of the calibration client PGenerator's status shows, or None:
        Calman, HCFR, DeviceControl (or the name it gave), LightSpace."""
        c = self.calman         # no lock: a pattern being shown holds it for seconds
        ip, software = c.client_ip, c.client_software
        return (ip, software) if software else None

    def start(self):
        for port, rpc in self.ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("", port))
                s.listen(4)
                s.settimeout(1.0)
            except OSError as exc:
                self.log("cannot listen on TCP %d: %s" % (port, exc), error=True)
                continue
            with self.lock:
                self.sockets.append(s)
            threading.Thread(target=self._accept, args=(s, rpc), daemon=True,
                             name="upgci-%d" % port).start()
            self.log("listening on TCP %d (%s)" % (
                port, "Calman RPC" if rpc else "classic, HCFR" if port == self.classic_port
                else "Calman"))
        if self.discovery:
            threading.Thread(target=self._discovery, daemon=True, name="upgci-disc").start()
        if self.devicecontrol_port:
            threading.Thread(target=self._devicecontrol, daemon=True, name="upgci-dc").start()
        if self.lightspace:
            self.lightspace.start()

    def stop(self):
        self.stopping.set()
        if self.lightspace:
            self.lightspace.stop()
        with self.lock:
            socks = self.sockets + list(self.clients)
        for s in socks:
            try:        # wakes a thread blocked on it, so the port is free for a restart
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def _accept(self, srv, rpc):
        while not self.stopping.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.log("client connected from %s:%d on TCP %d" % (addr[0], addr[1],
                                                                  srv.getsockname()[1]))
            tune_client_socket(conn)
            with self.gen.lock:
                self.calman.stat("connections")
            threading.Thread(target=self._client, args=(conn, addr, rpc), daemon=True).start()

    def _reconnected(self, conn):
        """Whether the same PC opened a newer connection to the same port: this one is
        the dead one Calman left behind when it reconnected."""
        with self.lock:
            mine = self.origin.get(conn)
            return mine is not None and any(
                c is not conn and o[:2] == mine[:2] and o[2] > mine[2]
                for c, o in self.origin.items())

    def _client(self, conn, addr, rpc):
        with self.lock:
            self.clients.add(conn)
            self.accepted += 1
            try:
                self.origin[conn] = (addr[0], conn.getsockname()[1], self.accepted)
            except OSError:
                pass
        session = Session(self.calman, self.info, self.log, rpc, addr[0])
        buf = bytearray()           # daemon.pm keeps the whole command, however long
        eof = False
        conn.settimeout(1.0)
        try:
            while not self.stopping.is_set() and not session.closed:
                try:
                    data = conn.recv(RECV_SIZE)
                except socket.timeout:
                    continue
                if not data:
                    eof = True
                    session.calman_seen = rpc   # daemon.pm:890 resets calman{} for "" too
                    break
                calman = rpc or data.endswith(ETX) or data.endswith(ETX + b"\n")
                session.calman_seen = calman
                with self.gen.lock:
                    if calman:
                        self.calman.set_client(addr[0], "Calman")
                    http = session.http_get(data.decode("latin-1"), calman)
                if http:
                    conn.sendall(http[0])
                    break
                if rpc:
                    chunk = data            # one recv is one command, as in PGenerator
                else:
                    tail = bytes(buf[-1:]) + data
                    buf += data
                    if CLASSIC_END not in tail and ETX not in data:
                        continue
                    chunk, buf = bytes(buf), bytearray()
                with self.gen.lock:         # PGenerator serves one command at a time
                    reply, close = session.handle(normalise(chunk.decode("latin-1")), calman)
                if reply:
                    conn.sendall(reply)
                if close:
                    break
        except OSError as exc:
            if not self.stopping.is_set():
                self.log("client %s: connection error: %s" % (addr[0], exc))
        finally:
            replaced = self._reconnected(conn)
            with self.lock:
                self.clients.discard(conn)
                self.origin.pop(conn, None)
            try:
                conn.close()
            except OSError:
                pass
            self.log("client %s disconnected" % addr[0])
            # a dead connection (reaped by keepalive) after Calman reconnected from the same
            # PC must not reset the live one
            if not self.stopping.is_set():
                with self.gen.lock:
                    if replaced:                            # its flags only, nothing shared
                        session.hcfr = session.gci = False
                    else:
                        if session.calman_seen or (addr[0] and addr[0] == self.calman.client_ip):
                            self.calman.set_client("", "")  # close_connection
                        session.close()
            # PGenerator keeps the pattern (daemon.pm:2768); blanking is an opt-in of the add-on
            if (eof and session.drew and not self.stopping.is_set() and not replaced
                    and self.info.get("stop_on_disconnect")):
                with self.gen.lock:
                    self.calman.last = self.calman.shown = None
                    self.gen.stop()

    def _discovery(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("", self.discovery_port))
            s.settimeout(1.0)
        except OSError as exc:
            self.log("discovery disabled, UDP %d: %s" % (self.discovery_port, exc), error=True)
            return
        with self.lock:
            self.sockets.append(s)
        with s:
            while not self.stopping.is_set():
                try:
                    data, addr = s.recvfrom(1024)
                    if data and data != b"0" and self.calman.discoverable:   # "0" is false
                        # discovery.pm:108: the name is read again for every answer
                        name = self.info["name"]
                        name = (name() if callable(name) else name).encode(
                            "utf-8", "surrogateescape")[:24]
                        s.sendto(struct.pack("<24sHH", name, self.rpc_port, self.rpc_port),
                                 (addr[0], self.discovery_reply_port))
                except socket.timeout:
                    continue
                except OSError:
                    if s.fileno() < 0:
                        return

    def _devicecontrol(self):
        """discovery_devicecontrol (discovery.pm:35-51): "Who is a PGenerator" anywhere in a
        datagram -> "I am a PGenerator <name>" to the sender."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("", self.devicecontrol_port))
            s.settimeout(1.0)
        except OSError as exc:
            self.log("DeviceControl discovery disabled, UDP %d: %s" % (self.devicecontrol_port,
                                                                      exc), error=True)
            return
        with self.lock:
            self.sockets.append(s)
        with s:
            while not self.stopping.is_set():
                try:
                    data, addr = s.recvfrom(1024)
                    if data and data != b"0" and b"Who is a PGenerator" in data \
                            and self.calman.discoverable:
                        name = self.info["name"]
                        name = (name() if callable(name) else name).encode(
                            "utf-8", "surrogateescape")[:24]
                        s.sendto(b"I am a PGenerator " + name, addr)
                except socket.timeout:
                    continue
                except OSError:
                    if s.fileno() < 0:
                        return
