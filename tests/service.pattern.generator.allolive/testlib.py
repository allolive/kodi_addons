"""What the check scripts share. Importing it only puts the add-on on sys.path; the render
capture is installed by capture_scenes(), not on import.

  FAILS, check, ok, near    collect failures; a script ends with sys.exit(1 if FAILS else 0)
  Ports(offset)             free ports from PGPORT + offset (each script its own range)
  wait, xfer                poll a condition; send and read until the line is idle
  pq, eotf, pq12            ST 2084, written here and not taken from the add-on
  RecordingBackend          a backend that records the clips it is asked to play
  SCENES, capture_scenes    every scene the generator renders, with its signal
"""
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.environ.get("PGADDON") or os.path.join(HERE, "../../addons/service.pattern.generator.allolive")
LIB = os.path.join(ADDON, "resources", "lib")
PGREF = os.environ.get("PGREF") or os.path.join(HERE, "pgenerator-ref", "usr", "share", "PGenerator")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

FAILS: list[str] = []


def check(name, got, want):
    if got != want:
        FAILS.append("%s: got %r, want %r" % (name, got, want))


def ok(name, cond, detail=""):
    if not cond:
        FAILS.append("%s %s" % (name, detail))


def near(name, got, want, tol=1e-6):
    if got is None or abs(got - want) > tol:
        FAILS.append("%s: got %r, want %r" % (name, got, want))


def wait(cond, timeout=3.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(step)
    return cond()


def bindable(port, kind):
    s = socket.socket(socket.AF_INET, kind)
    if kind == socket.SOCK_STREAM:      # TIME_WAIT is not a listener; on UDP it would share
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class Ports:
    """Free ports from PGPORT + offset upwards (400 of them), then any the kernel gives."""

    SPAN = 400

    def __init__(self, offset):
        self.base = int(os.environ.get("PGPORT", "12100")) + offset
        self.next = self.base

    def free(self, *kinds):
        """A port free for every kind given (default TCP)."""
        kinds = kinds or (socket.SOCK_STREAM,)
        while self.next < self.base + self.SPAN:
            p = self.next
            self.next += 1
            if all(bindable(p, k) for k in kinds):
                return p
        s = socket.socket(socket.AF_INET, kinds[0])
        s.bind(("", 0))
        p = s.getsockname()[1]
        s.close()
        return p


def xfer(c, data, idle=0.05, timeout=10):
    """Send data (if any), then read everything that arrives until the line is idle."""
    if data:
        c.sendall(data)
    out = b""
    c.settimeout(timeout)
    try:
        out = c.recv(65536)
        c.settimeout(idle)
        while out:
            d = c.recv(65536)
            if not d:
                break
            out += d
    except socket.timeout:
        pass
    return out


_M1, _M2, _C1, _C2, _C3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875


def pq(nits):
    """ST 2084 inverse EOTF: nits -> E'."""
    y = (nits / 10000.0) ** _M1
    return ((_C1 + _C2 * y) / (1 + _C3 * y)) ** _M2


def eotf(e):
    p = e ** (1 / _M2)
    return 10000.0 * (max(p - _C1, 0) / (_C2 - _C3 * p)) ** (1 / _M1)


def pq12(nits):
    return int(round(pq(nits) * 4095))


class RecordingBackend:
    def __init__(self):
        self.plays = []

    def play(self, path, mode=None, fps=None):
        self.plays.append((path, mode))
        return True

    def stop(self):
        pass


SCENES: list = []


def capture_scenes():
    """Record (scene, signal) of every clip the generator renders from now on."""
    from patterngen import generator
    real = generator.render.render
    if getattr(real, "captures", False):
        return

    def capture(scene, sig, *a, **k):
        SCENES.append((scene, sig.copy()))
        return real(scene, sig, *a, **k)
    capture.captures = True             # type: ignore[attr-defined]
    generator.render.render = capture
