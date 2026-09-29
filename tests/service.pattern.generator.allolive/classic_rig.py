"""Rig shared by the classic-protocol checks (t_classic, t_classic2, t_parity2, t_check2):
a generator on free ports, a recording backend, a client, colour helpers."""
import socket
import tempfile
import time

from testlib import (FAILS, Ports, SCENES, RecordingBackend as Backend, capture_scenes, check,  # noqa: F401
                     ok, xfer)
from patterngen import generator, upgci  # noqa: E402

upgci.INFO_PERIOD = 0       # no device_info cache here (t_parity2.py tests it)
capture_scenes()

_PORTS = Ports(500)
CACHE = tempfile.mkdtemp(prefix="pg_classic_")
END = b"\x02\r"


def free_port(kind=socket.SOCK_STREAM):
    return _PORTS.free(kind)


class Rig:
    def __init__(self, fmt=(1280, 720, 60, 1), ls_timeout=0.4):
        self.backend = Backend()
        self.logs = []
        self.gen = generator.Generator(self.backend, CACHE, self.log, duration=1)
        self.gen.set_default_format(*fmt)
        self.info = {"name": "box", "serial": "c0ffee", "firmware": "2.12.1"}
        for _ in range(20):
            self.port = free_port()
            self.dc = free_port(socket.SOCK_DGRAM)
            self.ls = free_port(socket.SOCK_DGRAM)
            self.srv = upgci.Server(self.gen, self.info, self.log, port=0, rpc_port=0,
                                    discovery=False, classic_port=self.port,
                                    devicecontrol_port=self.dc, lightspace_port=self.ls,
                                    lightspace_timeout=ls_timeout)
            self.srv.start()
            time.sleep(0.1)
            if len(self.srv.sockets) == 2:     # TCP classic + UDP DeviceControl
                break
            self.srv.stop()
        self.cal = self.srv.calman

    def log(self, msg, error=False, debug=False):
        if error:
            self.logs.append(msg)

    def conn(self):
        c = socket.create_connection(("127.0.0.1", self.port))
        c.settimeout(5)
        return c

    def close(self):
        self.srv.stop()
        if self.logs:
            FAILS.append("add-on logged errors: %s" % self.logs[:3])


def q(c, text):
    return xfer(c, text.encode("latin-1") + END)


def last():
    return SCENES[-1][0]


def g(v, m=255.0):
    return round(v / m, 7)


def key(colour):
    return colour.key()


def grey(v, m=255.0):
    return (g(v, m),) * 3


def dvg(v, m=255.0):
    """PGenerator's standard-DV tunnel (0.4.13): the code shifted to 12 bits, shown as
    E' = (c12 - 256) / 3504 (the DM metadata's 1/16 black offset and 4095/3504 gain)."""
    shift = {255: 4, 1023: 2, 4095: 0}[int(m)]
    return round(((int(v) << shift) - 256) / 3504.0, 7)


def dvgrey(v, m=255.0):
    return (dvg(v, m),) * 3


def even(scene):
    return all(v % 2 == 0 for r in scene.rects for v in r[:4])


def bbox(rects):
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects))


