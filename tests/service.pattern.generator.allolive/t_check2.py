"""Check round 2: a LightSpace background drawn at the patch's colex depth (its own colex,
else its 8-bit <color> scaled, not read as 10-bit codes), and the status line when no TCP
port could be bound while LightSpace listens, with DeviceControl alone, and when
LightSpace's UDP port is taken. Mocked Kodi; ports from PGPORT. Exit 1 on any failure.
"""
import os
import socket
import sys
import time

os.environ["PGPORT"] = str(int(os.environ.get("PGPORT", "12100")) + 2000)
# the rigs read PGPORT when imported: after the offset above
from classic_rig import check, END, FAILS, grey, key, last, ok, q, Rig  # noqa: E402
from status_rig import CLASSIC, DC, LS, open_ports, RPC, settings, status, TCP, wait  # noqa: E402
from patterngen import kodi  # noqa: E402

from patterngen import lightspace  # noqa: E402


def xml(bg, patch):
    return "<calibration><shapes>%s</shapes></calibration>" % "".join(
        "<rectangle>%s</rectangle>" % r for r in (bg, patch) if r is not None)


GEOM = '<geometry x="0.25" y="0.25" cx="0.5" cy="0.5"/>'
C8 = '<color red="%s" green="%s" blue="%s"/>'
CX = '<colex red="%s" green="%s" blue="%s" bits="%s"/>'


def t_ls_payload():
    p = lightspace.payload
    check("bg colex at the patch depth",
          p(xml(C8 % (128, 0, 255) + CX % (514, 1, 1023, 10), CX % (1, 2, 3, 10) + GEOM), 1280, 720),
          "1,2,3;514,1,1023;RECTANGLE;640,360;320,180;100;;;10")
    check("bg <color> scaled to 10 bits (nearest; 0 and 255 exact)",
          p(xml(C8 % (128, 0, 255), CX % (1, 2, 3, 10) + GEOM), 1280, 720),
          "1,2,3;514,0,1023;RECTANGLE;640,360;320,180;100;;;10")
    check("bg <color> scaled to 12 bits",
          p(xml(C8 % (16, 235, 255), CX % (1, 2, 3, 12) + GEOM), 1280, 720),
          "1,2,3;257,3774,4095;RECTANGLE;640,360;320,180;100;;;12")
    check("bg colex at another depth: <color> scaled",
          p(xml(C8 % (128, 128, 128) + CX % (2056, 2056, 2056, 12), CX % (1, 2, 3, 10) + GEOM),
            1280, 720), "1,2,3;514,514,514;RECTANGLE;640,360;320,180;100;;;10")
    check("8-bit patch: bg unchanged",
          p(xml(C8 % (128, 0, 255), C8 % (1, 2, 3) + GEOM), 1280, 720),
          "1,2,3;128,0,255;RECTANGLE;640,360;320,180;100;;;8")
    check("colex bits 8: bg unchanged",
          p(xml(C8 % (128, 0, 255), CX % (1, 2, 3, 8) + GEOM), 1280, 720),
          "1,2,3;128,0,255;RECTANGLE;640,360;320,180;100;;;8")
    check("one rectangle: black bg",
          p(xml(None, CX % (1, 2, 3, 10) + GEOM), 1280, 720),
          "1,2,3;0,0,0;RECTANGLE;640,360;320,180;100;;;10")
    check("non-integer bg left for the template to refuse",
          p(xml(C8 % ("1.5", 0, 0), CX % (1, 2, 3, 10) + GEOM), 1280, 720),
          "1,2,3;1.5,0,0;RECTANGLE;640,360;320,180;100;;;10")


def t_ls_drawn():
    rig = Rig(ls_timeout=2)
    pc = socket.socket()
    pc.bind(("127.0.0.1", 0))
    pc.listen(1)
    pc.settimeout(3)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.sendto(b"LS:x:%d" % pc.getsockname()[1], ("127.0.0.1", rig.ls))
    try:
        conn, _ = pc.accept()
    except socket.timeout:
        FAILS.append("LightSpace did not connect")
        rig.close()
        return
    for bg, want in ((C8 % (128, 128, 128) + CX % (514, 514, 514, 10), grey(514, 1023.0)),
                     (C8 % (64, 64, 64), grey(257, 1023.0))):
        n = len(rig.backend.plays)
        conn.sendall(xml(bg, C8 % (255, 255, 255) + CX % (1023, 1023, 1023, 10) + GEOM).encode())
        end = time.time() + 5
        while len(rig.backend.plays) == n and time.time() < end:
            time.sleep(0.02)
        check("LS 10-bit background drawn (%s)" % bg, key(last().background), want)
        check("LS 10-bit patch drawn", [(r[:4], key(r[4])) for r in last().rects],
              [((320, 180, 640, 360), (1.0,) * 3)])
    conn.close()
    u.close()
    pc.close()
    time.sleep(0.3)
    rig.close()


def t_renderer_state():
    """RGB= and TESTPATTERN write RGB, BG, POSITION and BITS lines the renderer keeps: a
    later template without them draws with those values (ofApp.cpp update)."""
    rig = Rig()
    c = rig.conn()
    check("SETCONF A", q(c, "SETCONF:A:TEMPLATE:DRAW=RECTANGLE\nDIM=100,100\nRGB=10,10,10\n"
                            "BG=5,5,5\nPOSITION=0,0\nEND=1"), b"OK" + END)
    check("SETCONF B", q(c, "SETCONF:B:TEMPLATE:DRAW=RECTANGLE\nDIM=100,100\nEND=1"), b"OK" + END)
    q(c, "TESTTEMPLATE:A:")
    q(c, "TESTTEMPLATE:B:")
    check("B after A", ([(r[:4], key(r[4])) for r in last().rects], key(last().background)),
          ([((0, 0, 100, 100), grey(10))], grey(5)))
    q(c, "RGB=RECTANGLE;200,100;0;200,200,200;50,50,50;300,300;x")
    q(c, "TESTTEMPLATE:B:")
    check("B after RGB=", ([(r[:4], key(r[4])) for r in last().rects], key(last().background)),
          ([((300, 300, 100, 100), grey(200))], grey(50)))
    q(c, "TESTPATTERN:p:RECTANGLE10bit:100,50:0:1000,1000,1000:")
    q(c, "SETCONF:C:TEMPLATE:DRAW=RECTANGLE\nDIM=100,100\nBITS=10\nEND=1")
    q(c, "TESTTEMPLATE:C:")
    check("C after TESTPATTERN 10bit: its codes, centred position",
          ([(r[:4], key(r[4])) for r in last().rects], key(last().background)),
          ([((590, 335, 100, 100), grey(1000, 1023.0))], grey(0)))
    q(c, "RGB=RECTANGLE;200,100;0;7,7,7;-1,-1,-1;10,10;x")
    q(c, "TESTTEMPLATE:B:")
    check("B after RGB= with BG=-1: not cleared", key(last().background), grey(0))
    c.close()
    rig.close()


def run_status(name, want, hold=(), **conf):
    """Start with these settings while the test holds the ports in hold (no SO_REUSEADDR,
    so the add-on cannot share them); the status line, then Stop."""
    held = []
    for p, kind in hold:
        s = socket.socket(socket.AF_INET, kind)
        s.bind(("", p))
        if kind == socket.SOCK_STREAM:
            s.listen(1)
        held.append(s)
    settings.update(upgci_enabled="true", classic_enabled="true", devicecontrol_discovery="true",
                    lightspace_enabled="true")
    settings.update(conf)
    svc = kodi.Service()
    svc.onNotification(kodi.ADDON_ID, "Other.start", "")
    wait(lambda: status(svc) == want, 3.0)
    check(name, status(svc), want)
    svc.onNotification(kodi.ADDON_ID, "Other.stop", "")
    for s in held:
        s.close()
    ok("%s: every port closed at Stop" % name, wait(lambda: open_ports() == [], 3.0),
       open_ports())


def t_status_lines():
    run_status("TCP ports taken, LightSpace listening", "Not listening (port in use?)",
               ((TCP, socket.SOCK_STREAM), (RPC, socket.SOCK_STREAM),
                (CLASSIC, socket.SOCK_STREAM)))
    run_status("DeviceControl alone", "Answering DeviceControl discovery on UDP %d" % DC,
               upgci_enabled="false", classic_enabled="false", lightspace_enabled="false")
    run_status("LightSpace port taken", "Not listening (UDP %d in use?)" % LS,
               ((LS, socket.SOCK_DGRAM),), upgci_enabled="false", classic_enabled="false",
               devicecontrol_discovery="false")
    run_status("LightSpace alone", "Waiting for LightSpace on UDP %d" % LS,
               upgci_enabled="false", classic_enabled="false", devicecontrol_discovery="false")
    run_status("all on", "Listening on %s:%d/%d/%d" % (kodi.local_ip(), TCP, RPC, CLASSIC))


for t in (t_ls_payload, t_ls_drawn, t_renderer_state, t_status_lines):
    try:
        t()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        FAILS.append("raised %r" % exc)

for f in FAILS:
    print("FAIL", f)
print("check round 2: %d failures" % len(FAILS))
sys.exit(1 if FAILS else 0)
