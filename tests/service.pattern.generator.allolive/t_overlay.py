"""Debug overlay: glyphs, content, placement, and that it never touches the pattern."""
import os, socket, subprocess, sys, tempfile, time
from array import array

import testlib  # noqa: E402,F401  the add-on on sys.path
from patterngen import overlay, upgci, generator  # noqa: E402
from patterngen.patterns import Colour, patch_scene  # noqa: E402

from testlib import FAILS as fails, check  # noqa: E402


for ch, bits in overlay._FONT.items():
    check("glyph %r size" % ch, (len(bits), set(bits) <= {"0", "1"}), (35, True))

played = []


class FB:
    def play(self, p, **k):
        played.append(p)
        return True

    def stop(self):
        pass


def decode(path, w, h):
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "yuv420p10le", "-"], capture_output=True).stdout
    a = array("H")
    a.frombytes(out[:w * h * 2])
    return a


cache = tempfile.mkdtemp()
for mode, w, h in (("sdr", 1920, 1080), ("hdr10", 3840, 2160), ("dv", 1920, 1080)):
    gen = generator.Generator(FB(), cache, lambda *a, **k: None, duration=1)
    gen.set_default_format(w, h, 24, 1)
    gen.set_mode(mode)
    fg = Colour.gray(0.6)
    ref = patch_scene(gen.sig.copy(), fg, 10, None)
    # overlay off: the scene is exactly the pattern
    gen.patch(fg, 10)
    y_off = decode(played[-1], w, h)
    gen.overlay = True
    gen.pattern_request = "RGB_S:0512,0512,0512,010"
    gen.patch(fg, 10)
    y_on = decode(played[-1], w, h)
    check("%s: overlay makes a different clip" % mode, played[-1] != played[-2], True)
    # the window centre and the top-left quadrant are untouched
    for x, y in ((w // 2, h // 2), (10, 10), (w // 4, h // 4)):
        check("%s pixel %d,%d unchanged" % (mode, x, y), y_on[y * w + x], y_off[y * w + x])
    # something light and something black near the bottom right
    region = [y_on[yy * w + xx] for yy in range(h * 3 // 4, h) for xx in range(w // 2, w)]
    ink = round(64 + 876 * (0.5 if mode != "sdr" else 0.7))
    check("%s overlay ink present" % mode, ink in region, True)
    check("%s overlay box black" % mode, 64 in region, True)
    lines = overlay.describe(gen.pattern_request, gen.sig, ref)
    check("%s first line" % mode, lines[0], "REQ RGB_S:0512,0512,0512,010")
    check("%s no LAST/HDMI line (history-free, 0.4.7)" % mode,
          [l for l in lines if l.startswith(("LAST ", "HDMI "))], [])
    if mode == "dv":
        check("dv static line", any(l.startswith("STATIC DV PEAK 4000 MIN 0.005") for l in lines), True)
    check("%s MAXL line only with HDR10 metadata" % mode, any(l.startswith("MAXL ") for l in lines),
          mode in ("hdr10", "hlg"))
    check("%s all characters drawable" % mode,
          [c for l in lines for c in l if c.upper() not in overlay._FONT], [])
    check("%s lines fit" % mode, max(len(l) for l in lines) <= overlay.MAX_CHARS, True)

# the signal line is the requested mode: nothing is sent in its place
gen = generator.Generator(FB(), cache, lambda *a, **k: None, duration=1)
gen.set_default_format(1920, 1080, 24, 1)
gen.set_mode("dv")
lines = overlay.describe("X", gen.sig,
                         patch_scene(gen.sig.copy(), Colour.gray(0.5), 10, None))
check("signal line", lines[1], "SIG DV MAP RELATIVE")

# the protocol records the request
port = int(os.environ.get("PGPORT", "24000")) + 7
gen = generator.Generator(FB(), cache, lambda *a, **k: None, duration=1)
gen.set_default_format(1920, 1080, 24, 1)
gen.overlay = True
srv = upgci.Server(gen, {"name": "t", "serial": "s", "firmware": "f"}, lambda *a, **k: None,
                   port=port, rpc_port=0, discovery=False)
srv.start()
time.sleep(0.2)
c = socket.create_connection(("127.0.0.1", port))
c.settimeout(30)
for cmd in ("RGB_S:0940,0940,0940,010", "10_SIZE:18"):
    c.sendall(b"\x02" + cmd.encode() + b"\x03")
    c.recv(16)
check("pattern_request recorded", gen.pattern_request, "RGB_S:0940,0940,0940,010")
c.close()
srv.stop()

print("\n".join(fails) if fails else "overlay OK")
sys.exit(1 if fails else 0)
