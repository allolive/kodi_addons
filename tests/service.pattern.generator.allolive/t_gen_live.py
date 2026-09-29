"""Generator routing (0.4.8): with a live backend, patterns go to show() as encoded
patterns (no clip files) and a repeated pattern is reused from the cache."""
import os, sys, tempfile
import testlib  # noqa: E402,F401  the add-on on sys.path
from patterngen import generator, render
from patterngen.patterns import Colour
from testlib import FAILS as fails, check  # noqa: E402
class LiveBackend:
    continuous = True
    def __init__(self): self.shown = []
    def show(self, enc, mode=None, fps=None): self.shown.append((enc, mode, fps)); return True
    def play(self, path, **k): raise AssertionError("clip played with a live backend")
    def stop(self): pass
cache = tempfile.mkdtemp()
logs = []
g = generator.Generator(LiveBackend(), cache, lambda m, **k: logs.append(m), duration=2)
g.set_default_format(1280, 720, 24, 1); g.set_mode("dv")
check("pattern shown", g.patch(Colour.gray(0.5), 10), True)
enc, mode, fps = g.backend.shown[-1]
check("an Encoded", isinstance(enc, render.Encoded), True)
check("mode / fps", (mode, fps), ("dv", 24.0))
check("no clip file", [f for f in os.listdir(cache) if f.endswith(".mkv")], [])
check("encoded file cached", len([f for f in os.listdir(cache) if f.startswith("enc-")]), 1)
n = sum("encoded" in m for m in logs)
g.patch(Colour.gray(0.5), 10)
check("repeat: not encoded again", sum("encoded" in m for m in logs), n)
check("repeat: same pattern", g.backend.shown[-1][0].as_tuple(), enc.as_tuple())
g.patch(Colour.gray(0.6), 10)
check("new pattern: same stream format", g.backend.shown[-1][0].stream_key, enc.stream_key)
g.set_mode("sdr"); g.patch(Colour.gray(0.5), 10)
check("other mode: other stream format", g.backend.shown[-1][0].stream_key != enc.stream_key, True)
# clip backend (no live) still renders clips
class ClipBackend:
    def __init__(self): self.played = []
    def play(self, path, **k): self.played.append(path); return True
    def stop(self): pass
g2 = generator.Generator(ClipBackend(), cache, lambda m, **k: None, duration=2)
g2.set_default_format(1280, 720, 24, 1); g2.set_mode("dv")
g2.patch(Colour.gray(0.5), 10)
check("clip backend plays a clip", g2.backend.played[-1].endswith(".mkv"), True)
# prune keeps both kinds within the limit
for i in range(6):
    g.patch(Colour.gray(0.1 + i / 20.0), 10)
render.prune(cache, 3)
check("prune: 3 encoded kept", len([f for f in os.listdir(cache) if f.startswith("enc-")]), 3)
check("prune: clip kept", len([f for f in os.listdir(cache) if f.endswith(".mkv")]), 1)
# nothing drawn (a scene that does not fit): counted as a failure, so it is NAKed
f0 = g.failures
g.show_scene(lambda sig: None)
check("nothing drawn counts as a failure", g.failures, f0 + 1)

# a damaged cached pattern is never shown: detected by its checksum and encoded again
f = sorted((os.path.getmtime(os.path.join(cache, x)), os.path.join(cache, x))
           for x in os.listdir(cache) if x.startswith("enc-"))[-1][1]
b = bytearray(open(f, "rb").read()); b[-3] ^= 0x55; open(f, "wb").write(b)
logs.clear()
g.patch(Colour.gray(0.1 + 5 / 20.0), 10)
check("damaged cache file re-encoded", any("unusable" in m for m in logs) and any("encoded" in m for m in logs), True)
print("\n".join(fails) if fails else "generator live routing OK")
sys.exit(1 if fails else 0)
