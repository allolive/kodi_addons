"""Debug overlay: what was requested and what is sent, in light grey at the bottom right.

Drawn into the picture itself, so it slightly changes full-field and APL patterns;
it is off by default and meant for debugging only.
"""

from .patterns import DV_DEFAULT_MAP, DV_MAPS, DV_MIN, DV_PEAK, SIGNAL_MODES, Colour

# 5x7 glyphs, one string of 5 bits per row
_FONT = {
    " ": "00000" * 7,
    "0": "01110100011001110101110011000101110", "1": "00100011000010000100001000010001110",
    "2": "01110100010000100010001000100011111", "3": "11111000100010000010000011000101110",
    "4": "00010001100101010010111110001000010", "5": "11111100001111000001000011000101110",
    "6": "00110010001000011110100011000101110", "7": "11111000010001000100010000100001000",
    "8": "01110100011000101110100011000101110", "9": "01110100011000101111000010001001100",
    "A": "01110100011000111111100011000110001", "B": "11110100011000111110100011000111110",
    "C": "01110100011000010000100001000101110", "D": "11100100101000110001100011001011100",
    "E": "11111100001000011110100001000011111", "F": "11111100001000011110100001000010000",
    "G": "01110100011000010111100011000101111", "H": "10001100011000111111100011000110001",
    "I": "01110001000010000100001000010001110", "J": "00111000100001000010000101001001100",
    "K": "10001100101010011000101001001010001", "L": "10000100001000010000100001000011111",
    "M": "10001110111010110101100011000110001", "N": "10001100011100110101100111000110001",
    "O": "01110100011000110001100011000101110", "P": "11110100011000111110100001000010000",
    "Q": "01110100011000110001101011001001101", "R": "11110100011000111110101001001010001",
    "S": "01111100001000001110000010000111110", "T": "11111001000010000100001000010000100",
    "U": "10001100011000110001100011000101110", "V": "10001100011000110001100010101000100",
    "W": "10001100011000110101101011010101010", "X": "10001100010101000100010101000110001",
    "Y": "10001100011000101010001000010000100", "Z": "11111000010001000100010001000011111",
    ".": "00000000000000000000000000110001100", ",": "00000000000000000000011000010001000",
    ":": "00000011000110000000011000110000000", ";": "00000011000110000000011000010001000",
    "-": "00000000000000011111000000000000000", "+": "00000001000010011111001000010000000",
    "/": "00000000010001000100010001000000000", "%": "11000110010001000100010001001100011",
    ">": "10000010000010000010001000100010000", "<": "00010001000100010000010000010000010",
    "(": "00010001000100001000010000010000010", ")": "01000001000001000010000100010001000",
    "=": "00000000001111100000111110000000000", "_": "00000000000000000000000000000011111",
    "'": "00100001000100000000000000000000000", "|": "00100001000010000100001000010000100",
    "#": "01010010101111101010111110101001010", "*": "00000001001010101110101010010000000",
    "?": "01110100010000100010001000000000100", "!": "00100001000010000100001000000000100",
}
_UNKNOWN = _FONT["?"]
MAX_CHARS = 60


def _cells(ch):
    bits = _FONT.get(ch.upper(), _UNKNOWN)
    for row in range(7):
        col = 0
        while col < 5:          # one rectangle per run of lit cells
            if bits[row * 5 + col] != "1":
                col += 1
                continue
            end = col
            while end < 5 and bits[row * 5 + end] == "1":
                end += 1
            yield row, col, end - col
            col = end


def add(scene, lines, hdr):
    """Draw lines (str) into a black box at the bottom right of scene."""
    lines = [str(line)[:MAX_CHARS] for line in lines if line]
    if not lines:
        return scene
    s = max(2, (scene.height // 540) & ~1)             # pixel size, even for 4:2:0
    cw, lh, pad = 6 * s, 9 * s, 2 * s
    w = max(len(line) for line in lines) * cw + 2 * pad
    h = len(lines) * lh + 2 * pad
    x0, y0 = (scene.width - w - 4 * s) & ~1, (scene.height - h - 4 * s) & ~1
    ink = Colour.gray(0.5 if hdr else 0.7)
    scene.add(x0, y0, w, h, Colour.gray(0.0))
    for i, line in enumerate(lines):
        y = y0 + pad + i * lh
        for j, ch in enumerate(line):
            x = x0 + pad + j * cw
            for row, col, run in _cells(ch):
                scene.add(x + col * s, y + row * s, run * s, s, ink)
    return scene




def _ycc(colour, colorimetry):
    try:
        return "%d %d %d" % tuple(colour.ycc(colorimetry))
    except (AttributeError, TypeError):     # a colour kind without direct codes
        return "?"


def _short(mode):
    return SIGNAL_MODES[mode].short if mode in SIGNAL_MODES else mode


def describe(request, sig, scene):
    """The overlay lines for a pattern: only what the request itself decides, so the same
    request draws the same clip and the cache can reuse it."""
    fps = sig.fps_num / float(sig.fps_den)
    sig_line = "SIG %s" % _short(sig.mode)
    if sig.mode == "dv":
        m = sig.dv_map_mode if sig.dv_map_mode is not None else DV_DEFAULT_MAP
        sig_line += " MAP %s" % DV_MAPS.get(m, str(m)).upper()
    lines = ["REQ %s" % (request or "-")]
    lines += [
        sig_line,
        "%dX%d %.3f %s" % (sig.width, sig.height, fps,
                           sig.colorimetry.upper()),
    ]
    if sig.mode in ("hdr10", "hlg"):     # the clips with HDR10 metadata
        lines.append("MAXL %g MINL %g CLL %d FALL %d" % (sig.max_luma, sig.min_luma,
                                                         sig.max_cll, sig.max_fall))
    if sig.mode == "dv":
        lines.append("STATIC %s PEAK %g MIN %g" % (_short(sig.mode), DV_PEAK, DV_MIN))
    fg = scene.rects[0][4] if scene.rects else scene.background
    lines.append("FG YCC %s BG %s" % (_ycc(fg, sig.colorimetry),
                                      _ycc(scene.background, sig.colorimetry)))
    if scene.rects:
        x, y, w, h = scene.rects[0][:4]
        lines.append("WIN %dX%d AT %d,%d %.1f%%" % (w, h, x, y,
                                                   100.0 * w * h / (scene.width * scene.height)))
    return lines
