"""live.py (0.4.8): the continuous stream, against a simulated display clock and a real
decoder (ffmpeg): priming, pacing, in-stream switches, shown(), stop()."""
import subprocess, sys, threading, time
import testlib  # noqa: E402,F401  the add-on on sys.path
from patterngen import live, patterns, render
from testlib import FAILS as fails, check  # noqa: E402

def enc_for(level, mode="dv", w=1280, h=720):
    sig = patterns.Signal(); sig.set_mode(mode); sig.width, sig.height = w, h
    sig.fps_num, sig.fps_den = 24, 1
    sc = patterns.patch_scene(sig, patterns.Colour.gray(level), 10, patterns.Colour.gray(0.0))
    return render.encode(sc, sig)

class Display:
    """The picture on screen: starts `delay` s after the first byte is read, then real time."""
    def __init__(self, delay=0.3): self.t0, self.delay = None, delay
    def __call__(self):
        if self.t0 is None: return None
        t = time.time() - self.t0 - self.delay
        return t if t > 0 else None

A, B = enc_for(0.45), enc_for(0.60)
disp = Display()
s = live.Stream(clock=disp, lead=0.4, start_lead=0.4, prime=64 * 1024)
url = s.start(A)
check("url", url.startswith("http://127.0.0.1:") and "/patterngen-live/" in url, True)
check("matches same format", s.matches(B), True)
check("other format differs", s.matches(enc_for(0.45, mode="sdr")), False)
check("other size differs", s.matches(enc_for(0.45, w=1920, h=1080)), False)

p = subprocess.Popen(["ffmpeg", "-hide_banner", "-i", url, "-t", "4", "-vf",
                      "crop=64:64:608:328,signalstats,metadata=print:key=lavfi.signalstats.YAVG",
                      "-f", "null", "-"], stderr=subprocess.PIPE, text=True)
disp.t0 = time.time()
time.sleep(1.5)
check("connected", s.connected(), True)
check("started once the display runs", s.started(), True)
tok = s.switch(B); t_sw = time.time()
while not s.shown(tok) and time.time() - t_sw < 3: time.sleep(0.01)
lat = time.time() - t_sw
check("switch shown within lead + a frame", 0.2 < lat < 0.55, True)
err = p.communicate(timeout=30)[1]
ys = [round(float(l.split("=")[1])) for l in err.splitlines() if "YAVG" in l]
changes = [i for i in range(1, len(ys)) if ys[i] != ys[i - 1]]
check("decoded both greys", len(set(ys)), 2)
check("one switch in the stream", len(changes), 1)
# the switch lands ~lead after the display time of the request: frame ~ (1.5 - 0.3 + 0.4) * 24
check("switch frame near display+lead", abs(changes[0] - int((1.5 - 0.3 + 0.4) * 24)) <= 3 if changes else False, True)
check("decoded ~4 s", 90 <= len(ys) <= 100, True)

# a new stream does not end the old one at once (Kodi closes it when it switches items;
# ending it first made Kodi stop the new item too), but it ends once superseded
url_old = s.start(A); disp.t0 = None
p_old = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", url_old, "-f", "null", "-"])
time.sleep(0.5); disp.t0 = time.time(); time.sleep(1.0)
live.SUPERSEDED = 1.5
s.start(B)
time.sleep(0.5)
check("old stream still served just after a new start", p_old.poll(), None)
try:
    p_old.wait(timeout=6); old_ended = True
except subprocess.TimeoutExpired:
    p_old.kill(); old_ended = False
check("old stream ends once superseded", old_ended, True)

# pacing: frames produced stay ~lead ahead of the display (not free-running)
url = s.start(A); disp.t0 = None
p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", url, "-f", "null", "-"])
time.sleep(0.5); disp.t0 = time.time(); time.sleep(2.0)
conn = next(c for c in s.conns if c.generation == s.generation)
ahead = conn.frames / conn.fps - disp()
check("paced ~lead ahead", 0.3 < ahead < 0.5, True)
s.stop()
t = time.time()
try:
    p.wait(timeout=5); ended = True
except subprocess.TimeoutExpired:
    p.kill(); ended = False
check("stop ends the player's stream", ended, True)
check("stopped: nothing connected", s.connected(), False)
# a stale URL (an old generation) is refused
import urllib.request, urllib.error
try:
    urllib.request.urlopen(url, timeout=3); code = 200
except urllib.error.HTTPError as e:
    code = e.code
check("old stream URL refused", code, 404)
# priming: frames before the first picture carry filler, after it they are small
url = s.start(A); disp.t0 = None
import urllib.request
r = urllib.request.urlopen(url, timeout=5)
first = r.read(200 * 1024)
check("first frames primed (>= 64 KB)", len(first) >= 128 * 1024, True)
r.close(); s.close()
# -- hardening (0.4.8 review) ---------------------------------------------------------
W = live.CLOCK_WRAP
check("behind: plain", round(live.behind(10.0, 9.6), 3), 0.4)
check("behind: across the wrap", round(live.behind(0.2, W - 0.2), 3), 0.4)
check("behind: display ahead across the wrap", round(live.behind(W - 0.2, 0.2), 3), -0.4)

# HEAD (Kodi stats the URL) and a query string on the path
s2 = live.Stream(clock=lambda: None, lead=0.4, prime=64 * 1024)
url = s2.start(A)
import http.client, urllib.parse
u = urllib.parse.urlparse(url)
h = http.client.HTTPConnection(u.hostname, u.port, timeout=5); h.request("HEAD", u.path)
r = h.getresponse(); check("HEAD answered 200", (r.status, r.read()), (200, b"")); h.close()
h = http.client.HTTPConnection(u.hostname, u.port, timeout=5); h.request("GET", u.path + "?mimetype=video%2fx-matroska")
r = h.getresponse(); check("GET with a query served", (r.status, len(r.read(1000)) > 0), (200, True)); h.close()

# the previous stream's clock still running: not this stream's first picture until the
# clock went idle or Kodi reports this stream playing
class Running:
    t0 = time.time()
    def __call__(self): return 0.2 + (time.time() - self.t0)   # an old stream just started
old = Running()
s3 = live.Stream(clock=old, lead=0.4, start_lead=0.4, prime=1024)
url = s3.start(A)
r = urllib.request.urlopen(url, timeout=5)
reader = threading.Thread(target=lambda: [r.read(4096) for _ in range(200)], daemon=True); reader.start()
time.sleep(0.8)
check("old clock running: not started", s3.started(), False)
s3.player_started()
time.sleep(0.5)
check("Kodi says playing, but the old clock still runs on: not started", s3.started(), False)
# the new stream's clock takes over: it restarts near 0, behind where the old one was
old.t0 = time.time() + 0.19
time.sleep(0.5)
check("clock jumped back to this stream's start: started", s3.started(), True)
r.close(); s3.close()

# every open connection gets the switch; shown only once all are past it
disp2 = Display(0.2)
s5 = live.Stream(clock=disp2, lead=0.4, start_lead=0.4, prime=1024)
url = s5.start(A)
player = urllib.request.urlopen(url, timeout=5)
disp2.t0 = time.time()
threading.Thread(target=lambda: [player.read(4096) for _ in range(400)], daemon=True).start()
time.sleep(1.0)
late = urllib.request.urlopen(url, timeout=5)      # a second connection, never read
time.sleep(0.3)
tok = s5.switch(B)
time.sleep(1.0)
conns = [c for c in s5.conns if c.generation == s5.generation]
check("two connections open", len(conns), 2)
check("both got the switch", all(tok in c.applied for c in conns), True)
late.close()
t = time.time()
while not s5.shown(tok) and time.time() - t < 3: time.sleep(0.02)
check("shown once the unread one is gone", s5.shown(tok), True)
player.close(); s5.close()

# a switch record that stays bounded
c = live._Conn(1, 1, 24.0)
for i in range(2, 200):
    c.note_switch(i)
check("switch record bounded", len(c.applied) <= live.APPLIED_KEEP, True)
check("switch record keeps the newest", 199 in c.applied, True)

# a client that stops reading does not hold its thread forever
live.SEND_TIMEOUT = 1.0
s4 = live.Stream(clock=lambda: None, lead=0.4, start_lead=3.0, prime=512 * 1024)
url = s4.start(A)
u = urllib.parse.urlparse(url)
import socket as _s
stuck = _s.create_connection((u.hostname, u.port)); stuck.sendall(("GET %s HTTP/1.0\r\n\r\n" % u.path).encode())
t = time.time()
while s4.connected() and time.time() - t < 15: time.sleep(0.1)
check("a client that stops reading is dropped", s4.connected(), False)
stuck.close(); s4.close(); s2.close()

# a decoder reset hours in (clock reads nothing): priming resumes and frames keep flowing
class Resettable:                   # idle until the first picture, like the real clock
    def __init__(self): self.t0, self.off = time.time(), True
    def __call__(self): return None if self.off else time.time() - self.t0
rc = Resettable()
s6 = live.Stream(clock=rc, lead=0.4, start_lead=0.4, prime=1024)
live.CLOCK_LOST = 0.3
url = s6.start(A)
r6 = urllib.request.urlopen(url, timeout=5)
time.sleep(0.2); rc.t0, rc.off = time.time() - 0.05, False
got = []
threading.Thread(target=lambda: [got.append(len(r6.read(1024))) for _ in range(100000)],
                 daemon=True).start()
time.sleep(4.0)                     # well past START_LEAD_MAX of stream time
check("stream started", s6.started(), True)
rc.off = True; time.sleep(0.8)
check("clock lost: priming again (not started)", s6.started(), False)
n0 = sum(got); time.sleep(0.5)
check("priming sends frames after a reset", sum(got) > n0, True)
rc.off = False; time.sleep(0.8)
check("picture back: started again", s6.started(), True)
r6.close(); s6.close()

# a frozen display (Kodi paused, or closing the stream): frames keep coming in real time,
# so the player's read never waits on a display that waits on it (Player.stop() hung)
class Freezable:
    def __init__(self): self.t0, self.at = None, None
    def __call__(self):
        if self.t0 is None: return None
        return self.at if self.at is not None else time.time() - self.t0
fz = Freezable()
s7 = live.Stream(clock=fz, lead=0.4, start_lead=0.4, prime=1024)
url = s7.start(A)
r7 = urllib.request.urlopen(url, timeout=5)
got7 = []
threading.Thread(target=lambda: [got7.append(len(r7.read(256))) for _ in range(100000)],
                 daemon=True).start()
time.sleep(0.2); fz.t0 = time.time() - 0.05
time.sleep(2.0)
check("frozen test: started", s7.started(), True)
fz.at = fz()                          # the display stops moving
time.sleep(1.5)                       # past FROZEN
n0 = len(got7); time.sleep(1.0)
check("display frozen: frames still flow", len(got7) > n0, True)
r7.close(); s7.close()

print("\n".join(fails) if fails else "live OK")
sys.exit(1 if fails else 0)
