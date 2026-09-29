"""Signal state, colour conversion and pattern scenes."""

import contextlib
import math
from typing import NamedTuple

from .perl import perl_uc

# PGenerator's DisplayChromacityList: (R, G, B, W) CIE xy by conf "primaries" index
DISPLAY_PRIMARIES = (
    ((0.640, 0.330), (0.300, 0.600), (0.150, 0.060), (0.3127, 0.3290)),     # 0 Rec.709
    ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046), (0.3127, 0.3290)),     # 1 Rec.2020
    ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060), (0.3127, 0.3290)),     # 2 P3-D65
    ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060), (0.3140, 0.3510)),     # 3 P3-DCI
    ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060), (0.32168, 0.33767)),   # 4 P3-D60
    ((0.6703, 0.3297), (0.2606, 0.6732), (0.1442, 0.0512), (0.3127, 0.3290)),  # 5
)

# Kr, Kb of the Y'CbCr matrix for each container colorimetry
class SignalMode(NamedTuple):
    setting: str        # the test_signal / default_mode option value ("2" was HDR10+)
    name: str
    short: str          # the debug overlay's
    hdmi: tuple         # hdmistate.hdr_eotf() values that mean this signal is on the wire


SIGNAL_MODES = {"sdr": SignalMode("0", "SDR", "SDR", ("SDR",)),
                "hdr10": SignalMode("1", "HDR10", "HDR10", ("PQ",)),
                "hlg": SignalMode("3", "HLG", "HLG", ("HLG",)),
                "dv": SignalMode("4", "Dolby Vision", "DV", ("DV", "DV-LL"))}
DV_MAPS = {0: "Perceptual", 1: "Absolute", 2: "Relative"}   # dv_map_mode (L255 dm_run_mode)
DV_DEFAULT_MAP = 2   # PGenerator's default dv_map_mode (Relative)

MATRIX_K = {"bt709": (0.2126, 0.0722), "bt2020": (0.2627, 0.0593)}


class Signal:
    """Everything that shapes the encoded stream, as set by the client."""

    def __init__(self):
        self.mode = "sdr"
        self.width = 1920
        self.height = 1080
        self.fps_num = 60
        self.fps_den = 1
        self.colorimetry = "bt709"        # container primaries/matrix
        self.bits = 8                     # PGenerator max_bpc (reported, not a code depth)
        self.color_format = "RGB"         # requested HDMI format (reported)
        self.wire_range = 2               # PGenerator rgb_quant_range (1 limited, 2 full)
        self.calman_range = None          # PGenerator calman_rgb_quant_range
        self.primaries = 0                # mastering display: DISPLAY_PRIMARIES index
        self.max_luma = 1000
        self.min_luma = 0.005
        self.max_cll = 1000
        self.max_fall = 400
        self.dv_map_mode = None           # None, 0 perceptual, 1 absolute, 2 relative
        self.pattern_mode = None          # PGenerator conf mode the charts are drawn for

    def copy(self):
        s = Signal()
        s.__dict__.update(self.__dict__)
        return s

    def key(self):
        """Everything that changes the encoded stream (range/bits/format only change colours)."""
        return tuple(sorted((k, repr(v)) for k, v in self.__dict__.items()
                            if k not in ("wire_range", "calman_range", "bits", "color_format",
                                         "pattern_mode")))

    def set_mode(self, mode):
        self.mode = mode
        self.pattern_mode = None
        self.colorimetry = "bt709" if mode == "sdr" else "bt2020"
        self.primaries = 0 if mode == "sdr" else 1

    def mastering_primaries(self):
        i = self.primaries
        return DISPLAY_PRIMARIES[i] if 0 <= i < len(DISPLAY_PRIMARIES) else DISPLAY_PRIMARIES[0]

    def describe(self):
        fps = self.fps_num / self.fps_den
        return "%s %dx%d@%.3f %s" % (self.mode.upper(), self.width, self.height, fps,
                                     self.colorimetry)


_M1, _M2 = 2610 / 16384, 2523 / 4096 * 128
_C1, _C2, _C3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32


def pq_encode(nits):
    """SMPTE ST 2084 inverse EOTF, 0..1."""
    y = max(0.0, nits) / 10000.0
    ym = y ** _M1
    return ((_C1 + _C2 * ym) / (1 + _C3 * ym)) ** _M2


def pq_decode(e):
    """SMPTE ST 2084 EOTF, normalised code -> nits."""
    ep = max(0.0, min(1.0, e)) ** (1 / _M2)
    return 10000.0 * (max(ep - _C1, 0.0) / (_C2 - _C3 * ep)) ** (1 / _M1)


def pq12(nits):
    return max(0, min(4095, int(round(pq_encode(nits) * 4095))))


# Peak and min nits of the static Dolby Vision metadata: always PGenerator's dv_maxpq/
# dv_minpq (3696/62), whatever luminance the client sends - PGenerator takes them from its
# own config, never from Calman, and Calman's DV workflow is built around that (following
# its MaxL changed what Relative mapping measures against).
DV_PEAK, DV_MIN = 4000.0, 0.005


class Colour:
    """A pattern colour as normalised non-linear R'G'B' (0..1, may exceed)."""

    __slots__ = ("r", "g", "b")

    def __init__(self, r, g, b):
        self.r, self.g, self.b = r, g, b

    @classmethod
    def from_code(cls, r, g, b, bits, rng):
        return cls(*(_level(v, (1 << bits) - 1, rng != "full") for v in (r, g, b)))

    @classmethod
    def gray(cls, v):
        return cls(v, v, v)

    def ycc(self, colorimetry):
        """10-bit limited-range Y'CbCr code values."""
        kr, kb = MATRIX_K[colorimetry]
        y = kr * self.r + (1 - kr - kb) * self.g + kb * self.b
        cb = (self.b - y) / (2 * (1 - kb))
        cr = (self.r - y) / (2 * (1 - kr))
        return (_clip(64 + 876 * y), _clip(512 + 896 * cb), _clip(512 + 896 * cr))

    def key(self):
        return (round(self.r, 7), round(self.g, 7), round(self.b, 7))


class YccColour(Colour):
    """A colour given as normalised Y'CbCr (YCC_* commands): encoded as those values,
    with R'G'B' derived through the container matrix."""

    __slots__ = ("y", "cb", "cr")

    def __init__(self, y, cb, cr, colorimetry):
        kr, kb = MATRIX_K[colorimetry]
        r = y + 2 * (1 - kr) * cr
        b = y + 2 * (1 - kb) * cb
        Colour.__init__(self, r, (y - kr * r - kb * b) / (1 - kr - kb), b)
        self.y, self.cb, self.cr = y, cb, cr

    def ycc(self, colorimetry):
        return (_clip(64 + 876 * self.y), _clip(512 + 896 * self.cb), _clip(512 + 896 * self.cr))

    def key(self):
        return ("ycc", round(self.y, 7), round(self.cb, 7), round(self.cr, 7))


def reads_limited(sig):
    """Whether pattern codes are limited-range codes, as ofApp.cpp draws them: always in
    Dolby Vision; in RGB only with a Limited wire AND a Limited Calman source range
    (normalizeSourceValue); in Y'CbCr formats the codes are wire codes (the YCbCr shader),
    so limited with a Limited wire (4:2:0 taken like 4:4:4/4:2:2, unverified).

    Dolby Vision: normalizeSourceValue leaves the codes alone, but the standard-DV shader
    packs them unscaled as 12-bit Y (Y = Kr.R + Kg.G + Kb.B of code<<2) and the DM metadata
    tells the TV the signal is Y'CbCr with a 1/16 black offset and a 4095/3504 gain: on
    screen E' = (code - 64)/876, so 64 is black and 940 peak white, whatever the range
    setting. Calman sends limited codes in DV (its guide sets Limited; a 2026-09-27
    session's patterns carried 64..940). Reading them as full range raised black to
    ~6% PQ (fixed 0.4.13)."""
    if sig.mode == "dv":
        return True
    if sig.color_format == "RGB":
        return sig.wire_range == 1 and sig.calman_range == 1
    return sig.wire_range == 1


def _level(code, cmax, limited):
    """A code at its own depth (cmax = 2^b - 1) -> E' in the source range."""
    s = (cmax + 1) // 256
    if limited:
        # deviates from ofApp.cpp:648-651 (clamp): BTB/WTW kept, Colour.ycc clips to 4..1019
        return (code - 16 * s) / float(219 * s)
    return code / float(cmax)



class Code:
    """A pattern colour as Calman sent it: R'G'B' codes at their own depth (cmax 1023 or
    255) or 10-bit Y'CbCr codes, read in the source range when drawn. cmax 0 is
    PGenerator's "0,0,0" black."""

    __slots__ = ("codes", "cmax", "ycc")

    def __init__(self, codes, cmax=1023, ycc=False):
        self.codes, self.cmax, self.ycc = tuple(codes), cmax, ycc

    def colour(self, sig):
        # deviates from daemon.pm:76-93/61-74 (shift to max_bpc, <<2 in DV): E' from own depth
        if not self.cmax:
            return Colour.gray(0.0)
        lim = reads_limited(sig)
        if not self.ycc:
            return Colour(*[_level(c, self.cmax, lim) for c in self.codes])
        # daemon.pm:2393 ACKs YCC_* undrawn; drawn because STATUS (daemon.pm:1095) advertises it
        y, cb, cr = self.codes
        if lim:
            return YccColour((y - 64) / 876.0, (cb - 512) / 896.0, (cr - 512) / 896.0,
                             sig.colorimetry)
        return YccColour(y / 1023.0, (cb - 512) / 1023.0, (cr - 512) / 1023.0, sig.colorimetry)

    def __eq__(self, other):
        return isinstance(other, Code) and (self.codes, self.cmax, self.ycc) == \
            (other.codes, other.cmax, other.ycc)

    def __hash__(self):
        return hash((self.codes, self.cmax, self.ycc))

    def __repr__(self):
        return "Code(%r, %d%s)" % (self.codes, self.cmax, ", ycc" if self.ycc else "")


@contextlib.contextmanager
def calman_range(sig, value):
    """sig.calman_range set to `value` for the duration (a colour read in another range)."""
    saved = sig.calman_range
    sig.calman_range = value
    try:
        yield
    finally:
        sig.calman_range = saved


def _clip(v):
    return max(4, min(1019, int(math.floor(v + 0.5))))


class Scene:
    """Background colour plus rectangles painted in order."""

    def __init__(self, width, height, background, name="pattern"):
        self.width = width
        self.height = height
        self.background = background
        self.rects = []
        self.name = name

    def add(self, x, y, w, h, colour):
        self.rects.append((int(x), int(y), int(w), int(h), colour))

    def key(self):
        return (self.width, self.height, self.background.key(),
                tuple((x, y, w, h, c.key()) for x, y, w, h, c in self.rects))


def window_rect(width, height, percent):
    """Centred window covering `percent` of the screen area, even-aligned."""
    # deviates from daemon.pm:500-501 (int(sqrt(p)*max)): even size and offset for 4:2:0
    percent = max(1.0, min(100.0, float(percent)))
    if percent >= 100:
        return 0, 0, width, height
    s = math.sqrt(percent / 100.0)
    w = int(round(width * s / 2)) * 2
    h = int(round(height * s / 2)) * 2
    x = ((width - w) // 2) & ~1
    y = ((height - h) // 2) & ~1
    return x, y, w, h


def patch_scene(sig, fg, window_pct, background):
    scene = Scene(sig.width, sig.height, background or Colour.gray(0.0), "patch")
    if window_pct < 100:
        x, y, w, h = window_rect(sig.width, sig.height, window_pct)
        scene.add(x, y, w, h, fg)
    else:
        scene.background = fg
    return scene


# PatternDynamic template not in the snapshot: DIM/POSITION=DYNAMIC assumed (daemon.pm:483/537,
# variables.pm:270-271)
DYNAMIC_SIZE = (640, 360)    # PGenerator dim_default: PatternDynamic's unscaled rectangle


def dynamic_scene(sig, fg, background):
    """PGenerator's PatternDynamic template: a centred 640x360 pixel rectangle at
    every resolution, or None where it does not fit (PGenerator draws nothing)."""
    w, h = DYNAMIC_SIZE
    if w > sig.width or h > sig.height:
        return None
    scene = Scene(sig.width, sig.height, background, "patch")
    scene.add(int((sig.width - w) / 2), int((sig.height - h) / 2), w, h, fg)
    return scene


# -- PGenerator's diagnostic IMAGE charts (webui.pm webui_pattern_render_*) --------
_DIGITS = {"0": ("111", "101", "101", "101", "111"), "1": ("010", "110", "010", "010", "111"),
           "2": ("111", "001", "111", "100", "111"), "3": ("111", "001", "111", "001", "111"),
           "4": ("101", "101", "111", "001", "001"), "5": ("111", "100", "111", "001", "111"),
           "6": ("111", "100", "111", "101", "111"), "7": ("111", "001", "001", "001", "001"),
           "8": ("111", "101", "111", "101", "111"), "9": ("111", "101", "111", "001", "111"),
           "-": ("000", "000", "111", "000", "000")}


def signal_mode(sig):
    """webui_pattern_signal_mode: the conf's mode (sig.pattern_mode, else the signal's)."""
    return sig.pattern_mode or sig.mode


def chart_peak_luma(sig):
    """webui_pattern_max_luma."""
    v = sig.max_luma if sig.max_luma > 0 else 1000
    return min(10000, v)


def legacy_byte(value, smode, max_luma):
    """webui_pattern_legacy_byte: 8-bit chart level, scaled to the PQ peak in HDR10
    (a real level on the 8-bit scale; the reader rounds it to a 10-bit code)."""
    value = max(0, min(255, int(value)))
    if smode != "hdr10":        # SDR/HLG keep the byte; DV keeps its authored tunnel codes
        return value
    # deviates from webui.pm:11817/11838 (peak and level rounded to 8-bit codes): not rounded here
    peak = pq_encode(min(10000.0, max_luma)) * 255 if max_luma > 0 else 0.0
    return value * peak / 255.0


class _Chart:
    """An 8-bit IMAGE drawn as rectangles; byte_colour(b) reads a byte as a Colour."""

    def __init__(self, w, h, name, byte_colour):
        self.w, self.h = w, h
        self.colour = byte_colour
        self.scene = Scene(w, h, byte_colour(0), name)

    def rect(self, x1, y1, x2, y2, v):
        x1, y1 = max(0, int(x1)), max(0, int(y1))
        x2, y2 = min(self.w - 1, int(x2)), min(self.h - 1, int(y2))
        if x2 >= x1 and y2 >= y1:
            self.scene.add(x1, y1, x2 - x1 + 1, y2 - y1 + 1, self.colour(v))

    def box(self, x1, y1, x2, y2, t, v):
        t = max(1, int(t))
        self.rect(x1, y1, x2, y1 + t - 1, v)
        self.rect(x1, y2 - t + 1, x2, y2, v)
        self.rect(x1, y1, x1 + t - 1, y2, v)
        self.rect(x2 - t + 1, y1, x2, y2, v)

    def text_center(self, text, x1, x2, y, scale, v):
        text = str(text)
        width = len(text) * 3 * scale + (len(text) - 1) * scale if text else 0
        x = max(x1, x1 + int(((x2 - x1 + 1) - width) / 2))
        for ch in text:
            for row, bits in enumerate(_DIGITS.get(ch, ("000",) * 5)):
                col = 0
                while col < 3:       # one rectangle per run of lit cells
                    if bits[col] != "1":
                        col += 1
                        continue
                    end = col
                    while end < 3 and bits[end] == "1":
                        end += 1
                    self.rect(x + col * scale, y + row * scale, x + end * scale - 1,
                              y + (row + 1) * scale - 1, v)
                    col = end
            x += 4 * scale


def _label_scale(h):
    return max(2, min(7, int(h / 270)))


def _clipping_chart(chart, levels, labels, panel_top, panel_frac, bar_frac, footer_gap,
                    frame, label_level, panel_fill=None):
    w, h = chart.w, chart.h
    panel_x = int(w * 0.06)
    panel_y = int(h * panel_top)
    panel_w = w - panel_x * 2
    panel_h = int(h * panel_frac)
    footer_y = panel_y + panel_h + int(h * footer_gap)
    footer_h = int(h * 0.11)
    frame_t = max(2, int(h / 360))
    gap = max(2, int(panel_w / (len(levels) * 10)))
    scale = _label_scale(h)
    bar_y1 = panel_y + int(panel_h * bar_frac)
    bar_y2 = panel_y + panel_h - int(panel_h * bar_frac) - 1
    x2p, y2p = panel_x + panel_w - 1, panel_y + panel_h - 1
    if panel_fill is not None:
        chart.rect(panel_x, panel_y, x2p, y2p, panel_fill)
    chart.box(panel_x, panel_y, x2p, y2p, frame_t, frame)
    chart.rect(panel_x, footer_y, x2p, footer_y + footer_h - 1, 0)
    chart.box(panel_x, footer_y, x2p, footer_y + footer_h - 1, frame_t, 48)
    n = len(levels)
    for i, (v, label) in enumerate(zip(levels, labels, strict=True)):
        slot_x1 = panel_x + int(i * panel_w / n)
        slot_x2 = panel_x + int((i + 1) * panel_w / n) - 1 if i < n - 1 else x2p
        x1, x2 = slot_x1 + gap, slot_x2 - gap
        if x1 > x2:
            x1 = slot_x1
        if x2 < x1:
            x2 = slot_x2
        chart.rect(x1, bar_y1, x2, bar_y2, v)
        chart.text_center(label, slot_x1, slot_x2, footer_y + int((footer_h - 5 * scale) / 2),
                          scale, label_level)


def specialty_scene(sig, name, byte_colour=None):
    """PGenerator's SPECIALTY charts (BRIGHTNESS, CONTRAST, ALIGNMENT/OVERSCAN) as the
    8-bit images it draws, or None for any other name. byte_colour reads an image
    byte (default: full range)."""
    name = perl_uc(name.strip(" \t\n\r\f\v"))       # Perl: ASCII only
    w, h = sig.width, sig.height
    byte_colour = byte_colour or (lambda b: Colour.gray(b / 255.0))
    smode, peak = signal_mode(sig), chart_peak_luma(sig)
    if name == "BRIGHTNESS":
        chart = _Chart(w, h, "brightness", byte_colour)
        labels = (2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 25)
        _clipping_chart(chart, [legacy_byte(v, smode, peak) for v in labels], labels,
                        0.18, 0.48, 0.08, 0.05, 56, 220)
        return chart.scene
    if name == "CONTRAST":
        chart = _Chart(w, h, "contrast", byte_colour)
        labels = (232, 234, 236, 238, 240, 242, 244, 246, 248, 250, 252, 254, 255)
        _clipping_chart(chart, [legacy_byte(v, smode, peak) for v in labels], labels,
                        0.15, 0.52, 0.10, 0.045, 96, 255, legacy_byte(235, smode, peak))
        return chart.scene
    if name in ("ALIGNMENT", "OVERSCAN"):
        chart = _Chart(w, h, "alignment", byte_colour)
        bw = max(2, int(h / 360))
        m25x, m25y, m5x, m5y = int(w * 0.025), int(h * 0.025), int(w * 0.05), int(h * 0.05)
        cross = int(min(w, h) * 0.08)
        br = max(20, int(min(w, h) * 0.04))
        cx, cy = int(w / 2), int(h / 2)
        chart.box(0, 0, w - 1, h - 1, bw, 255)
        chart.box(m25x, m25y, w - m25x - 1, h - m25y - 1, bw, 160)
        chart.box(m5x, m5y, w - m5x - 1, h - m5y - 1, bw, 96)
        chart.rect(cx - cross, cy - int(bw / 2), cx + cross - 1, cy + int((bw - 1) / 2), 255)
        chart.rect(cx - int(bw / 2), cy - cross, cx + int((bw - 1) / 2), cy + cross - 1, 255)
        for x1, x2, y1, y2 in ((m5x, m5x + br - 1, m5y, m5y + bw - 1),
                               (m5x, m5x + bw - 1, m5y, m5y + br - 1),
                               (w - m5x - br, w - m5x - 1, m5y, m5y + bw - 1),
                               (w - m5x - bw, w - m5x - 1, m5y, m5y + br - 1),
                               (m5x, m5x + br - 1, h - m5y - bw, h - m5y - 1),
                               (m5x, m5x + bw - 1, h - m5y - br, h - m5y - 1),
                               (w - m5x - br, w - m5x - 1, h - m5y - bw, h - m5y - 1),
                               (w - m5x - bw, w - m5x - 1, h - m5y - br, h - m5y - 1)):
            chart.rect(x1, y1, x2, y2, 255)
        return chart.scene
    return None
