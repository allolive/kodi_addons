"""PGenerator's renderer (ofApp.cpp) reading a pattern file: frames of draws, each a
RECTANGLE, CIRCLE, TRIANGLE, TEXT or IMAGE over a background that BG=-1 leaves
uncleared, and the shapes drawn into a Scene at even (4:2:0) pixel granularity.

The renderer keeps its values from one draw, and one file, to the next (only DIM and
SOURCE_RANGE reset at each END, so a DIM after a file's last END carries into the next
file); RendererState carries them. A value it cannot read
(boost::lexical_cast throws and the renderer dies) leaves that line unread here.
"""

import math
import re

from .overlay import _cells as glyph_runs
from .patterns import Code, Colour, Scene, calman_range
from .perl import dec_int

DRAW_NUM = {"RECTANGLE": 1, "CIRCLE": 2, "TRIANGLE": 3, "TEXT": 4, "IMAGE": 5}
MAX_SEGMENTS = 4096                 # a finer polygon is a circle at any size here
_INT = re.compile(r"[+-]?\d+$", re.A)


def c_int(text):
    """boost::lexical_cast<int>, or None where it throws."""
    if text is None or not _INT.match(text):
        return None
    v = dec_int(text)
    return v if -2 ** 31 <= v < 2 ** 31 else None


def c_ints(text, n):
    if text is None:
        return None
    el = text.split(",")
    if len(el) < n:
        return None
    vals = [c_int(v) for v in el[:n]]
    return None if None in vals else vals


class Codes(tuple):
    """An RGB=/BG= value that carries its own depth: a Dolby Vision payload triplet that
    PGenerator shifts to 12-bit, drawn at the depth it was sent in (README deviation)."""

    cmax: int

    def __new__(cls, codes, cmax):
        t = super().__new__(cls, codes)
        t.cmax = cmax
        return t


class RendererState:
    """ofApp's members that outlive a draw (a new renderer process starts afresh)."""

    def __init__(self):
        self.draw_type = ""
        self.text = ""
        self.image = ""
        self.resolution = 0
        self.rgb = (0, 0, 0)
        self.bg = (0, 0, 0)
        self.pos = (0, 0)
        self.bits = 8
        self.background = None      # the last cleared-to colour: (codes, cmax, limited)
        self.dim = (0, 0)           # dim1/dim2 and source_range: reset only at END
        self.limited = 0


class Draw:
    __slots__ = ("kind", "rgb", "bg", "dim", "pos", "res", "text", "bits", "smax", "limited")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def parse(text, st, marks=None):
    """ofApp::update: the pattern file -> [draws of frame 0, frame 1, ...] (frames past the
    last FRAME line are never shown). Updates st. marks: {"RGB"/"BG": {n: Codes}}, the n-th
    such line holding a payload value PGenerator scaled (templates.Engine.marks)."""
    frames: list[list] = [[]]
    dim, smax, limited = st.dim, 255, st.limited
    seen = {"RGB": 0, "BG": 0}
    for line in text.split("\n"):
        exact = None
        for key in seen:
            if line.startswith(key + "="):
                exact = (marks or {}).get(key, {}).get(seen[key])
                seen[key] += 1
        el = line.split("=")
        k = el[0]
        v = el[1] if len(el) > 1 else None
        if k == "DRAW" and v is not None:
            st.draw_type = v
        elif k == "TEXT" and v is not None:
            st.text = line.split("=", 1)[1]     # deviation: text after a '=' is kept
        elif k == "IMAGE" and v is not None:
            st.image = v
        elif k == "DIM":
            d = c_ints(v, 2)
            if d:
                dim = tuple(d)
        elif k == "RESOLUTION":
            r = c_int(v)
            if r is not None:
                st.resolution = r
        elif k == "RGB":
            c = c_ints(v, 3)
            if c:
                st.rgb = exact if exact is not None else tuple(c)
        elif k == "BITS":
            b = c_int(v)
            if b is not None:
                st.bits = b
        elif k == "SOURCE_MAX":
            s = c_int(v)
            if s is not None:
                smax = s if s in (255, 1023, 4095) else 255
        elif k == "POSITION":
            p = c_ints(v, 2)
            if p:
                st.pos = tuple(p)
        elif k == "BG":
            b = c_ints(v, 3)
            if b:
                st.bg = exact if exact is not None else tuple(b)
        elif k == "SOURCE_RANGE" and v is not None:
            limited = 1 if v == "LIMITED" else 0
        elif k == "FRAME":
            if c_int(v) is not None:
                frames.append([])
        elif k == "END":
            frames[-1].append(Draw(kind=DRAW_NUM.get(st.draw_type, 0), rgb=st.rgb, bg=st.bg,
                                   dim=dim, pos=st.pos, res=st.resolution, text=st.text,
                                   bits=st.bits, smax=smax, limited=limited))
            dim, limited = (0, 0), 0
    st.dim, st.limited = dim, limited
    shown = frames[:-1] if len(frames) > 1 else frames
    return shown


def cmax_of(d, dv):
    """The depth a draw's codes are read at: SOURCE_MAX in Dolby Vision, else BITS
    (deviation: BITS=11..16 outside DV are read at that depth, as RGB=/TESTPATTERN read
    them; the renderer draws anything but 10 as 8-bit)."""
    if dv:
        return d.smax
    return (1 << d.bits) - 1 if 8 < d.bits <= 16 else 255


class Paint:
    """A code triple at its depth, read in its source range when drawn."""

    __slots__ = ("codes", "cmax", "limited")

    def __init__(self, codes, cmax, limited):
        cmax = getattr(codes, "cmax", cmax)
        self.codes = tuple(max(0, min(cmax, int(v))) for v in codes)
        self.cmax, self.limited = cmax, limited

    def colour(self, sig):
        with calman_range(sig, 1 if self.limited else None):
            return Code(self.codes, self.cmax).colour(sig)


def plan(draws, background, dv):
    """-> (first draw left on screen, the colour cleared to, whether the renderer exits).
    Each draw with a BG clears the screen (ofBackground) before drawing; BG=-1 does not.
    background: RendererState.background before this file (a Paint, None = black)."""
    start = 0
    for i, d in enumerate(draws):
        if d.bg[0] != -1:
            start, background = i, Paint(d.bg, cmax_of(d, dv), d.limited)
    return start, background, any(d.kind == 0 for d in draws)


def frame_scene(sig, draws, background, dv):
    """ofApp::draw of one frame, as it stays on screen."""
    w, h = sig.width, sig.height
    start, background, exited = plan(draws, background, dv)
    if exited:                      # ofApp::draw exit(0) on an unknown DRAW: no renderer
        return Scene(w, h, Colour.gray(0.0), "patch")
    scene = Scene(w, h, background.colour(sig) if background else Colour.gray(0.0), "patch")
    for d in draws[start:]:
        fg = Paint(d.rgb, cmax_of(d, dv), d.limited).colour(sig)
        draw_shape(scene, d.kind, d.pos[0], d.pos[1], d.dim[0], d.dim[1], d.res, d.text, fg)
    return scene


# -- rasterising ---------------------------------------------------------------
def fill_rect(scene, x, y, w, h, colour):
    if w < 0:
        x, w = x + w, -w
    if h < 0:
        y, h = y + h, -h
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(scene.width, x + w), min(scene.height, y + h)
    if x2 > x1 and y2 > y1:
        scene.add(x1, y1, x2 - x1, y2 - y1, colour)


def fill_polygon(scene, pts, colour):
    """A convex polygon, sampled once per 2x2 block at its centre (4:2:0 granularity);
    blocks with the same span on consecutive rows merge into one rectangle."""
    if len(pts) < 3 or not all(math.isfinite(c) for p in pts for c in p):
        return
    ys = [p[1] for p in pts]
    y_lo = max(0, int(math.floor(min(ys))) & ~1)
    y_hi = min(scene.height, int(math.ceil(max(ys))) + 2)
    edges = list(zip(pts, pts[1:] + pts[:1], strict=True))
    run = None                     # (y, x, w, h) of the rectangle being extended
    for y in range(y_lo, y_hi, 2):
        yc = y + 1.0
        xs = []
        for (ax, ay), (bx, by) in edges:
            if (ay <= yc < by) or (by <= yc < ay):
                xs.append(ax + (yc - ay) * (bx - ax) / (by - ay))
        span = None
        if xs:
            j0 = int(math.ceil((min(xs) - 1) / 2.0))
            j1 = int(math.ceil((max(xs) - 1) / 2.0))
            x0, x1 = max(0, 2 * j0), min(scene.width, 2 * j1)
            if x1 > x0:
                span = (x0, x1 - x0)
        if run and span == run[1:3] and y == run[0] + run[3]:
            run = (run[0], run[1], run[2], run[3] + 2)
            continue
        if run:
            scene.add(run[1], run[0], run[2], run[3], colour)
        run = (y, span[0], span[1], 2) if span else None
    if run:
        scene.add(run[1], run[0], run[2], run[3], colour)


def text_metrics(text, size):
    """The 5x7 font scaled to a TrueType size (cap height ~ 0.93 size px): cell, width. A
    size below 1 is FreeType's 1 pt floor (FT_Set_Char_Size)."""
    cell = max(2, 2 * int(round(max(1, size) / 15.0)))
    return cell, (len(text) * 6 - 1) * cell if text else 0


def draw_text(scene, text, x, y, size, colour):
    """ofTrueTypeFont::drawString at baseline y, in the add-on's 5x7 font (the TTF is
    not here)."""
    cell, _ = text_metrics(text, size)
    top = y - 7 * cell
    for i, ch in enumerate(text):
        gx = x + i * 6 * cell
        if gx >= scene.width:
            break
        for row, col, run in glyph_runs(ch):
            fill_rect(scene, gx + col * cell, top + row * cell, run * cell, cell, colour)


def _fits(*v):
    return all(isinstance(n, int) and -2 ** 31 <= n < 2 ** 31 for n in v)


def draw_shape(scene, kind, x, y, d1, d2, res, text, colour):
    """ofApp::rectangle/circle/triangle/text (IMAGE: no image files here, nothing)."""
    w, h = scene.width, scene.height
    if not _fits(x, y, d1, d2):
        return
    if kind == 1:
        if x == -1:
            x, y = (w - d1) // 2 if w >= d1 else -((d1 - w) // 2), \
                   (h - d2) // 2 if h >= d2 else -((d2 - h) // 2)
        fill_rect(scene, x, y, d1, d2, colour)
    elif kind == 2:
        if x == -1:
            x, y = w // 2, h // 2
        # ofDrawCircle scales the unit polygon by the radius: a negative one reflects it
        n, r = min(res, MAX_SEGMENTS), d1
        if n >= 3 and r != 0:
            fill_polygon(scene, [(x + r * math.cos(2 * math.pi * i / n),
                                  y + r * math.sin(2 * math.pi * i / n)) for i in range(n)],
                         colour)
    elif kind == 3:
        if x == -1:
            x, y = w // 2, h // 2
        fill_polygon(scene, [(float(x), float(y - d1)), (float(x - d1), float(y + d1)),
                             (float(x + d1), float(y + d1))], colour)
    elif kind == 4:
        if x == -1:
            _, width = text_metrics(text, d1)
            x = int((w - width) / 2)
            y = h // 2
        draw_text(scene, text, x, y, d1, colour)
