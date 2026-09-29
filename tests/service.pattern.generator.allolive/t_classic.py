"""PGenerator's classic protocol on TCP 85, DeviceControl discovery (UDP 1977) and the
LightSpace client (UDP 20123 trigger, outbound TCP), byte for byte against
daemon.pm / pattern.pm / conf.pm / command.pm / client.pm / discovery.pm, and the
shapes the renderer (ofApp.cpp) would draw. Ports from PGPORT; clips rendered for real.
Exit status 1 on any mismatch.
"""
import base64
import math
import re
import socket
import sys
import time

from classic_rig import bbox, check, dvgrey, END, even, FAILS, free_port, g, grey, key, last, ok, q, Rig, SCENES, xfer
from testlib import bindable
from patterngen import templates, upgci

# ==============================================================================
def t_hcfr():
    """HCFR 3.x/4.x: probes with CMD:GET_RESOLUTION, uploads its template with
    SETCONF:HCFR:TEMPLATE (conf.pm HCFR block swap), then draws with TESTTEMPLATE."""
    rig = Rig()
    c = rig.conn()
    check("port 85 Calman framing: STATUS", xfer(c, b"STATUS\x03"), upgci.STATUS_CAPS.encode() + b"\x03")
    check("port 85 Calman key sets status", rig.srv.client(), ("127.0.0.1", "Calman"))
    c.close()
    time.sleep(0.2)
    check("Calman close clears status", rig.srv.client(), None)
    c = rig.conn()
    check("CMD:GET_RESOLUTION", q(c, "CMD:GET_RESOLUTION"), b"OK:1280x720" + END)
    check("GET_RESOLUTION marks HCFR", rig.srv.client(), ("127.0.0.1", "HCFR"))
    # the key's trailing CR LF is gone before SETCONF sees it (daemon.pm:981)
    tpl = ("DRAW=RECTANGLE\r\nDIM=DYNAMIC\r\nRGB=DYNAMIC\r\nBG=-1,-1,-1\r\nPOSITION=DYNAMIC\r\n"
           "END=1\r\nDRAW=RECTANGLE\r\nBG=-1,-1,-1\r\nEND=1\r\n")
    check("SETCONF:HCFR:TEMPLATE", q(c, "SETCONF:HCFR:TEMPLATE:" + tpl), b"OK" + END)
    want = ("DRAW=RECTANGLE\nBG=DYNAMIC\nEND=1\n"
            "DRAW=RECTANGLE\nDIM=DYNAMIC\nRGB=DYNAMIC\nBG=-1,-1,-1\nPOSITION=DYNAMIC\nEND=1\n")
    check("HCFR template: last DRAW block first, first BG=-1 dynamic, CRLF", rig.cal.templates.get("HCFR"), want)
    check("GETCONF first DIM", q(c, "GETCONF:HCFR:DIM"), b"OK:DYNAMIC" + END)
    check("GETCONF BG", q(c, "GETCONF:HCFR:BG"), b"OK:DYNAMIC" + END)
    check("GETCONF unknown key: whole file less its last newline",
          q(c, "GETCONF:HCFR:NOPE"), b"OK:" + want[:-1].encode() + END)
    check("GETCONF missing template", q(c, "GETCONF:NOPE:DIM"), b"OK:" + END)
    n, pats = len(rig.backend.plays), rig.cal.stats.get("patterns", 0)
    r = q(c, "TESTTEMPLATE:HCFR:235,235,235;16,16,16;RECTANGLE;200,100;-1,-1;100;1;Hi;10")
    check("TESTTEMPLATE:HCFR reply", r, b"OK" + END)
    check("TESTTEMPLATE drew once", (len(rig.backend.plays), rig.cal.stats.get("patterns")), (n + 1, pats + 1))
    sc = last()
    # BITS forced to 8 for HCFR (daemon.pm hcfr_template_payload); LIMITED source on a Full
    # RGB wire: codes read as full range (ofApp normalizeSourceValue)
    check("HCFR scene background = payload BG", key(sc.background), grey(16))
    check("HCFR scene draws (the clearing block has no DIM: 0x0)", [(r[:4], key(r[4])) for r in sc.rects],
          [((540, 310, 200, 100), grey(235))])
    # a Limited RGB wire: SOURCE_RANGE=LIMITED codes are expanded (limited E')
    rig.cal.conf["rgb_quant_range"] = "1"
    q(c, "RESTARTPGENERATOR:")
    # the restart's clean_files removed HCFR's template (command.pm:718-745)
    check("HCFR template gone after a restart",
          q(c, "TESTTEMPLATE:HCFR:235,235,235;16,16,16;RECTANGLE;200,100;-1,-1;100;1;Hi;10"),
          b"ERR" + END)
    q(c, "SETCONF:HCFR:TEMPLATE:" + tpl)
    q(c, "TESTTEMPLATE:HCFR:235,235,235;16,16,16;RECTANGLE;200,100;-1,-1;100;1;Hi;10")
    check("HCFR limited wire: limited E'", key(last().rects[0][4]), (1.0,) * 3)
    check("HCFR limited wire: BG 16 black", key(last().background), (0.0,) * 3)
    rig.cal.conf["rgb_quant_range"] = "2"
    q(c, "RESTARTPGENERATOR:")
    # TESTPATTERN on an HCFR connection: RECTANGLE8bit (hcfr_draw)
    check("HCFR TESTPATTERN", q(c, "TESTPATTERN:P:RECTANGLE:100,50:0:255,0,0:"), b"OK:0" + END)
    check("HCFR TESTPATTERN rect", (last().rects[0][:4], key(last().rects[0][4])),
          ((590, 335, 100, 50), (1.0, 0.0, 0.0)))
    c.close()
    rig.close()


def t_templates():
    """get_pattern: VAR, DYNAMIC||default, MACRO, CIRCLE/TRIANGLE/TEXT, BG=-1, DIM=N%,
    frames (the first is shown), errors, TEMPLATERAMDISK, non-TEMPLATE SETCONF, TESTCMD."""
    rig = Rig()
    c = rig.conn()
    t1 = ("# a comment\nVAR=SIZE=300\nDRAW=CIRCLE\nDIM=SIZE,SIZE\nRESOLUTION=64\nRGB=DYNAMIC\n"
          "BG=DYNAMIC||10,20,30\nPOSITION=-1,-1\nEND=1\nMACRO=T2\nDRAW=TEXT\nDIM=30,30\n"
          "RGB=255,255,0\nBG=-1,-1,-1\nPOSITION=100,600\nTEXT=RGB $RGB\nEND=1\nFRAME=DYNAMIC\n"
          "DRAW=RECTANGLE\nDIM=25%\nRGB=1,2,3\nEND=1\nFRAME=5")
    t2 = "DRAW=TRIANGLE\nDIM=100,100\nRGB=0,255,0\nBG=-1,-1,-1\nPOSITION=200,200\nEND=1\n"
    check("SETCONF T1", q(c, "SETCONF:T1:TEMPLATE:" + t1), b"OK" + END)
    check("SETCONF T2", q(c, "SETCONF:T2:TEMPLATE:" + t2), b"OK" + END)
    pats = rig.cal.stats.get("patterns", 0)
    check("TESTTEMPLATE:T1", q(c, "TESTTEMPLATE:T1:255,0,0"), b"OK" + END)
    check("DeviceControl status", rig.srv.client(), ("127.0.0.1", "DeviceControl"))
    check("MACRO counts a pattern too", rig.cal.stats.get("patterns"), pats + 2)
    sc = last()
    check("T1 background: BG=DYNAMIC||10,20,30", key(sc.background), (g(10), g(20), g(30)))
    ok("T1 even geometry", even(sc))
    red = [r for r in sc.rects if key(r[4]) == (1.0, 0.0, 0.0)]
    green = [r for r in sc.rects if key(r[4]) == (0.0, 1.0, 0.0)]
    yellow = [r for r in sc.rects if key(r[4]) == (1.0, 1.0, 0.0)]
    ok("T1 circle drawn", red, "no red")
    if red:
        x0, y0, x1, y1 = bbox(red)
        ok("T1 circle: 64-gon of radius 300 at 640,360", abs(x0 - 340) <= 2 and abs(x1 - 940) <= 2
           and 60 <= y0 <= 64 and 656 <= y1 <= 660, (x0, y0, x1, y1))
        area = sum(r[2] * r[3] for r in red)
        poly = 0.5 * 64 * 300 * 300 * math.sin(2 * math.pi / 64)
        ok("T1 circle area", abs(area - poly) / poly < 0.01, (area, poly))
    ok("T1 macro triangle drawn", green, "no green")
    if green:
        x0, y0, x1, y1 = bbox(green)
        ok("T1 triangle apex 200,100 base y 300 x 100..300",
           100 <= y0 <= 102 and 298 <= y1 <= 300 and 100 <= x0 <= 104 and 296 <= x1 <= 300,
           (x0, y0, x1, y1))
        area = sum(r[2] * r[3] for r in green)
        ok("T1 triangle area", abs(area - 20000) / 20000.0 < 0.03, area)
    ok("T1 text drawn (after the macro, over it)", yellow, "no yellow")
    if yellow:
        x0, y0, x1, y1 = bbox(yellow)
        # "RGB 255,0,0" = 11 glyphs, size 30 -> cell 4: baseline 600, 28 px tall
        ok("T1 text box", x0 >= 100 and y0 >= 572 and y1 <= 600 and x1 <= 100 + 11 * 24, (x0, y0, x1, y1))
    ok("frame 1 (1,2,3) not shown", not [r for r in sc.rects if key(r[4]) == (g(1), g(2), g(3))])
    ok("order: circle, triangle, text", sc.rects.index(red[0]) < sc.rects.index(green[0])
       < sc.rects.index(yellow[0]) if red and green and yellow else False)
    # the renderer keeps DRAW/RGB/BG between files: a template without DRAW/RGB reuses them
    q(c, "SETCONF:T4:TEMPLATE:DIM=100,100\nPOSITION=0,0\nEND=1")
    check("T4 (no DRAW, no RGB) reply", q(c, "TESTTEMPLATE:T4:9,9,9"), b"OK" + END)
    sc = last()
    # T1's file ended with a RECTANGLE of 1,2,3 and no BG (frame 1, parsed though not shown)
    check("T4 keeps DRAW and RGB", sc.rects and (sc.rects[0][:4], key(sc.rects[0][4])),
          ((0, 0, 100, 100), (g(1), g(2), g(3))))
    check("T4 background: BG kept (-1: no clear, the last clear colour)", key(sc.background),
          (g(10), g(20), g(30)))
    # errors (stats errors, nothing drawn)
    n, errs = len(rig.backend.plays), rig.cal.stats.get("errors", 0)
    for name, text, reply in (
            ("missing template", "TESTTEMPLATE:NOPE:1,2,3", b"ERR"),
            ("bad DRAW", "SETCONF:B1:TEMPLATE:DRAW=HEXAGON\nEND=1", None),
            ("bad DRAW draw", "TESTTEMPLATE:B1:1,1,1", b"ERR"),
            ("DIM too big", "SETCONF:B2:TEMPLATE:DRAW=RECTANGLE\nDIM=5000,10\nEND=1", None),
            ("DIM too big draw", "TESTTEMPLATE:B2:1,1,1", b"ERR"),
            ("negative DIM%", "SETCONF:B3:TEMPLATE:DIM=-5%\nEND=1", None),
            ("negative DIM% draw", "TESTTEMPLATE:B3:1,1,1", b"ERR"),
            ("EVAL", "SETCONF:B4:TEMPLATE:EVAL=system('x')\nEND=1", None),
            ("EVAL draw", "TESTTEMPLATE:B4:1,1,1", b"eval denied"),
            ("EVALPATTERN", "SETCONF:B5:TEMPLATE:EVALPATTERN=\n$str='DRAW=RECTANGLE';", None),
            ("EVALPATTERN draw", "TESTTEMPLATE:B5:1,1,1", b"ERR"),
            ("RGB not digits", "SETCONF:B6:TEMPLATE:DRAW=RECTANGLE\nRGB=DYNAMIC\nEND=1", None),
            ("RGB not digits draw", "TESTTEMPLATE:B6:1,x,1", b"ERR")):
        r = q(c, text)
        if reply is not None:
            check("error %s" % name, r, reply + END)
    check("errors drew nothing", len(rig.backend.plays), n)
    check("errors counted", rig.cal.stats.get("errors", 0) - errs, 7)
    # non-TEMPLATE SETCONF: TYPE=val first, the old lines without TYPE= and END=1, END=1
    q(c, "SETCONF:T3:TEMPLATE:DRAW=RECTANGLE\nDIM=1,1\nRGB=1,1,1\nEND=1")
    check("SETCONF:T3:DIM", q(c, "SETCONF:T3:DIM:10,10"), b"OK" + END)
    check("SETCONF TYPE file", rig.cal.templates.get("T3"),
          "DIM=10,10\nDRAW=RECTANGLE\nRGB=1,1,1\nEND=1\n")
    n = len(rig.backend.plays)
    check("SETCONF TESTCMD: the CMD: handler takes it", q(c, "SETCONF:T3:TESTCMD:x"), b"OK:" + END)
    check("TESTCMD draws nothing", len(rig.backend.plays), n)
    # TEMPLATERAMDISK: its own directory; GETCONF reads the template directory only
    q(c, "SETCONF:R1:TEMPLATERAMDISK:DRAW=RECTANGLE\nDIM=20,20\nRGB=DYNAMIC\nPOSITION=0,0\nEND=1")
    check("TESTTEMPLATE misses a ramdisk template", q(c, "TESTTEMPLATE:R1:1,1,1"), b"ERR" + END)
    check("TESTTEMPLATERAMDISK", q(c, "TESTTEMPLATERAMDISK:R1:50,60,70"), b"OK" + END)
    check("ramdisk draw", (last().rects[0][:4], key(last().rects[0][4])),
          ((0, 0, 20, 20), (g(50), g(60), g(70))))
    check("GETCONF ramdisk", q(c, "GETCONF:R1:DIM"), b"OK:" + END)
    # shipped PatternDynamic: a bare payload is a centred 640x360 box
    check("PatternDynamic", q(c, "TESTTEMPLATE:PatternDynamic:1,2,3;4,5,6"), b"OK" + END)
    check("PatternDynamic scene", (last().rects[0][:4], key(last().background)),
          ((320, 180, 640, 360), (g(4), g(5), g(6))))
    # 10-bit payload bits (non-HCFR keeps them)
    q(c, "TESTTEMPLATE:PatternDynamic:1023,0,512;0,0,0;RECTANGLE;100,100;0,0;100;;;10")
    check("10-bit payload", key(last().rects[0][4]), (1.0, 0.0, round(512 / 1023.0, 7)))
    c.close()
    rig.close()


def t_shapes_simple():
    """TESTPATTERN / RGB= CIRCLE, TRIANGLE, TEXT, IMAGE (create_pattern_file simple=1)."""
    rig = Rig()
    c = rig.conn()
    check("TESTPATTERN TRIANGLE", q(c, "TESTPATTERN:P:TRIANGLE:100,100:0:255,255,255:"), b"OK:0" + END)
    x0, y0, x1, y1 = bbox(last().rects)
    ok("triangle centred: apex 360-100, base 460", 260 <= y0 <= 262 and 458 <= y1 <= 460
       and 540 <= x0 <= 544 and 736 <= x1 <= 740, (x0, y0, x1, y1))
    check("TESTPATTERN CIRCLE resolution 0: nothing", q(c, "TESTPATTERN:P:CIRCLE:100,100:0:255,255,255:"),
          b"OK:0" + END)
    check("CIRCLE res 0 rects", last().rects, [])
    q(c, "TESTPATTERN:P:CIRCLE:100,100:100:255,255,255:")
    x0, y0, x1, y1 = bbox(last().rects)
    ok("circle r100 centred", abs(x0 - 540) <= 2 and abs(x1 - 740) <= 2 and abs(y0 - 260) <= 2
       and abs(y1 - 460) <= 2, (x0, y0, x1, y1))
    ok("circle even", even(last()))
    check("RGB=TEXT", q(c, "RGB=TEXT;30,30;0;255,0,0;0,0,0;-1,-1;ABC"), b"OK" + END)
    x0, y0, x1, y1 = bbox(last().rects)
    # 3 glyphs, size 30 -> cell 4, 68 px wide, centred; baseline at 360, 28 px tall
    check("text centred", (x0, y0, x1, y1), (606, 332, 674, 360))
    check("RGB=IMAGE: background only", (q(c, "RGB=IMAGE;100,100;0;255,255,255;5,5,5;-1,-1;/x.png"),
                                         last().rects, key(last().background)),
          (b"OK" + END, [], grey(5)))
    c.close()
    rig.close()


def t_cmd():
    rig = Rig()
    c = rig.conn()

    def cmd(k):
        r = q(c, "CMD:" + k)
        return r[3:-2].decode("latin-1") if r.startswith(b"OK:") and r.endswith(END) else r
    check("GET_HDMI_INFO", cmd("GET_HDMI_INFO"),
          "MODETEST (37) RGB full 16:9, 1280x720 @ 60.00Hz, progressive")
    check("GET_MODE", cmd("GET_MODE"), "37[1280x720 60.00Hz 74.25MHz phsyncpvsync]")
    modes = base64.b64decode(cmd("GET_MODES_AVAILABLE")).decode().split("\n")
    check("GET_MODES_AVAILABLE first rows", modes[:2], ["1[4096x2160 60.00Hz 594.00MHz phsyncpvsync]",
                                                         "2[4096x2160 59.94Hz 593.41MHz phsyncpvsync]"])
    check("GET_MODES_AVAILABLE count", len(modes), len(upgci.TV_MODES))
    check("GET_REFRESH", cmd("GET_REFRESH"), "60.00")
    check("GET_OUTPUT_RANGE", cmd("GET_OUTPUT_RANGE"), "RGB full")
    check("GET_EDID_INFO", base64.b64decode(cmd("GET_EDID_INFO")),
          b"HDMI-A-1\nNo info available\n\nHDMI-A-2\nNo info available")
    check("GET_IP-lo", cmd("GET_IP-lo"), "127.0.0.1")
    check("GET_MAC-lo: no link/ether line", cmd("GET_MAC-lo"), "None")
    check("GET_IP-none", cmd("GET_IP-nosuchif0"), "None")
    ok("GET_UP_FROM", re.match(r"\d+  (seconds|minutes|hours|days)$", cmd("GET_UP_FROM")))
    ok("GET_LA", re.match(r"\d+\.\d+$", cmd("GET_LA")))
    ok("GET_FREE_MEM", re.match(r"\d+M$", cmd("GET_FREE_MEM")))
    ok("GET_TEMPERATURE", re.match(r"-?\d+$", cmd("GET_TEMPERATURE")))
    check("GET_STATUS", cmd("GET_STATUS"), "Alive")
    check("MULTIPLE", q(c, "CMD:MULTIPLE:GET_REFRESH:GET_RESOLUTION"),
          b"OK:\nGET_REFRESH:60.00\nGET_RESOLUTION:1280x720" + END)
    check("MULTIPLE GET_RESOLUTION marks HCFR", rig.srv.client(), ("127.0.0.1", "HCFR"))
    check("unknown CMD", cmd("GET_WHATEVER"), "")
    check("SET_REFRESH on KMS (command.pm:878)", cmd("SET_REFRESH:5"), "ERR:Error with tvservice")
    # setters: the conf now, the signal at the next renderer restart
    check("SET IS_HDR", cmd("SET_PGENERATOR_CONF_IS_HDR:1"), "")
    cmd("SET_PGENERATOR_CONF_IS_SDR:0")
    cmd("SET_PGENERATOR_CONF_EOTF:2")
    check("conf mirrored", cmd("GET_PGENERATOR_CONF_IS_HDR"), "1")
    check("not yet restarted", rig.gen.sig.mode, "sdr")
    n = len(rig.backend.plays)
    check("RESTARTPGENERATOR", q(c, "RESTARTPGENERATOR:"), b"OK" + END)
    check("restart: HDR10, PatternStart shown", (rig.gen.sig.mode, rig.cal.conf["signal_mode"],
                                                 len(rig.backend.plays),
                                                 [r for r in last().rects if key(r[4]) != (0.0,) * 3],
                                                 key(last().background)),
          ("hdr10", "hdr10", n + 1, [], (0.0,) * 3))
    n = len(rig.backend.plays)
    cmd("SET_PGENERATOR_CONF_MAX_LUMA:4000")
    check("MAX_LUMA in HDR10: the shown pattern re-encoded",
          (len(rig.backend.plays), SCENES[-1][1].max_luma), (n + 1, 4000))
    cmd("SET_PGENERATOR_CONF_MIN_LUMA:50")
    check("MIN_LUMA wire units", (rig.cal.conf["min_luma"], cmd("GET_PGENERATOR_CONF_MIN_LUMA")),
          ("0.005", "50"))
    cmd("SET_PGENERATOR_CONF_DV_STATUS:1")
    check("DV_STATUS normalises the DV transport", tuple(rig.cal.conf[k] for k in (
        "is_std_dovi", "is_ll_dovi", "color_format", "rgb_quant_range", "max_bpc", "colorimetry")),
          ("1", "0", "0", "2", "8", "9"))
    cmd("SET_PGENERATOR_CONF_DV_MAP_MODE:1")
    check("DV_MAP_MODE -> DV_METADATA", rig.cal.conf["dv_metadata"], "3")
    cmd("SET_PGENERATOR_CONF_DV_METADATA:4")
    check("DV_METADATA -> DV_MAP_MODE", rig.cal.conf["dv_map_mode"], "2")
    q(c, "RESTARTPGENERATOR:")
    check("restart: Dolby Vision, relative", (rig.gen.sig.mode, rig.gen.sig.dv_map_mode), ("dv", 2))
    # DV: codes at their own depth, shown as PGenerator's DV tunnel shows them (limited)
    q(c, "TESTTEMPLATE:PatternDynamic:128,128,128;0,0,0;RECTANGLE;100,100;0,0;100;;;8")
    check("DV template: 8-bit 128 read at 8 bits", key(last().rects[0][4]), dvgrey(128))
    q(c, "TESTTEMPLATE:PatternDynamic:512,512,512;0,0,0;RECTANGLE10bit;100,100;0,0;100;;;")
    check("DV template: RECTANGLE10bit", key(last().rects[0][4]), dvgrey(512, 1023.0))
    for k in ("DV_STATUS:0", "IS_STD_DOVI:0", "IS_LL_DOVI:0", "IS_HDR:0", "IS_SDR:1", "EOTF:0"):
        cmd("SET_PGENERATOR_CONF_" + k)
    cmd("SET_PGENERATOR_CONF_MODE_IDX:24")
    check("MODE_IDX waits for the restart", (rig.gen.sig.width, rig.gen.sig.fps_num), (1280, 60))
    q(c, "RESTARTPGENERATOR:")
    check("restart: SDR 1080p60", (rig.gen.sig.mode, rig.gen.sig.width, rig.gen.sig.height,
                                   rig.gen.sig.fps_num), ("sdr", 1920, 1080, 60))
    check("SET_MODE", cmd("SET_MODE:37"), "")
    check("SET_MODE 720p60 now", (rig.gen.sig.width, rig.gen.sig.height), (1280, 720))
    cmd("SET_MODE:25")
    check("SET_MODE interlaced: its size and rate, progressive frames",
          (rig.gen.sig.width, rig.gen.sig.height, rig.gen.sig.fps_num), (1920, 1080, 60))
    check("GET_DISCOVERABLE", cmd("GET_DISCOVERABLE"), "1")
    cmd("SET_DISCOVERABLE:0")
    check("SET_DISCOVERABLE:0", cmd("GET_DISCOVERABLE"), "0")
    cmd("SET_DISCOVERABLE:1")
    # client identity (c is HCFR: MULTIPLE probed GET_RESOLUTION)
    c.close()
    for _ in range(250):        # its close clears the status: let it land first
        if rig.srv.client() is None:
            break
        time.sleep(0.02)
    c = rig.conn()
    check("GETSTATUS:CLIENTNAME", q(c, "GETSTATUS:CLIENTNAME=Perceptual Pro!"), b"OK:Alive" + END)
    check("client name", rig.srv.client(), ("127.0.0.1", "Perceptual Pro"))
    q(c, "RGB=RECTANGLE;10,10;0;1,1,1;0,0,0;0,0;x")
    check("a named client stays named (DeviceControl -> name)", rig.srv.client(),
          ("127.0.0.1", "Perceptual Pro"))
    d = rig.conn()
    q(d, "RGB=RECTANGLE;10,10;0;1,1,1;0,0,0;0,0;x")
    check("unnamed client: DeviceControl", rig.srv.client(), ("127.0.0.1", "DeviceControl"))
    d.close()
    c.close()
    time.sleep(0.2)
    check("close clears the status", rig.srv.client(), None)
    rig.close()


def t_devicecontrol():
    rig = Rig()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(1.0)

    def ask(msg):
        s.sendto(msg, ("127.0.0.1", rig.dc))
        try:
            return s.recvfrom(1024)
        except socket.timeout:
            return None
    r = ask(b"xx Who is a PGenerator? yy")
    check("DeviceControl reply", r and r[0], b"I am a PGenerator box")
    check("reply from the DeviceControl port", r and r[1][1], rig.dc)
    check("other text: no reply", ask(b"hello"), None)
    check("'0': no reply", ask(b"0"), None)
    rig.info["name"] = "x" * 30
    check("name cut to 24", (ask(b"Who is a PGenerator") or (b"",))[0],
          b"I am a PGenerator " + b"x" * 24)
    rig.cal.discoverable = False
    check("not discoverable: no reply", ask(b"Who is a PGenerator"), None)
    rig.cal.discoverable = True
    s.close()
    rig.close()


def t_lightspace():
    rig = Rig(ls_timeout=0.4)
    pc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    pc.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    pc.bind(("127.0.0.1", 0))
    pc.listen(2)
    pc.settimeout(3)
    tport = pc.getsockname()[1]
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def trigger(text):
        u.sendto(text.encode(), ("127.0.0.1", rig.ls))
        try:
            conn, _ = pc.accept()
            conn.settimeout(3)
            return conn
        except socket.timeout:
            return None

    def wait_plays(n, t=5):
        end = time.time() + t
        while len(rig.backend.plays) < n and time.time() < end:
            time.sleep(0.02)
        return len(rig.backend.plays)
    u.sendto(b"no trigger here:%d" % tport, ("127.0.0.1", rig.ls))
    pc.settimeout(0.5)
    try:
        pc.accept()
        FAILS.append("LightSpace connected without LS:")
    except socket.timeout:
        pass
    pc.settimeout(3)
    conn = trigger("LS:127.0.0.1:%d" % tport)
    ok("LightSpace connects out on the trigger", conn is not None)
    if conn is None:
        rig.close()
        return
    check("LightSpace status", rig.srv.client(), ("127.0.0.1", "LightSpace"))
    two = ('<?xml version="1.0"?><calibration><shapes>'
           '<rectangle><color red="16" green="16" blue="16"/></rectangle>'
           '<rectangle><color red="235" green="128" blue="16"/>'
           '<geometry x="0.25" y="0.25" cx="0.5" cy="0.5"/></rectangle></shapes></calibration>')
    n = len(rig.backend.plays)
    conn.sendall(two.encode())
    check("two rectangles drawn", wait_plays(n + 1), n + 1)
    sc = last()
    check("LS background", key(sc.background), grey(16))
    check("LS patch", (sc.rects[0][:4], key(sc.rects[0][4])),
          ((320, 180, 640, 360), (g(235), g(128), g(16))))
    check("LS full range owner", (rig.cal.range_owner, rig.cal.conf["rgb_quant_range"]),
          ("lightspace", "2"))
    conn.sendall(two.encode())
    time.sleep(0.3)
    check("same message: not drawn again", len(rig.backend.plays), n + 1)
    conn.sendall(b"<calibration><shapes><rectangle>")
    time.sleep(0.2)
    one = ('<calibration><shapes><rectangle><color red="255" green="255" blue="255"/>'
           '<colex red="1023" green="512" blue="0" bits="10"/>'
           '<geometry x="0,1" y="0,1" cx="0,2" cy="0,2"/></rectangle></shapes></calibration>')
    conn.sendall(one.encode())
    check("bad XML skipped, session alive; colex drawn", wait_plays(n + 2), n + 2)
    sc = last()
    check("LS colex 10-bit", (sc.rects[0][:4], key(sc.rects[0][4]), key(sc.background)),
          ((128, 72, 256, 144), (1.0, round(512 / 1023.0, 7), 0.0), (0.0,) * 3))
    conn.settimeout(2)
    check("keepalive after silence", conn.recv(64), b"Command:IsAlive")
    conn.sendall(b"pong")
    check("keepalive again", conn.recv(64), b"Command:IsAlive")
    n = len(rig.backend.plays)
    t0 = time.time()
    check("silence after IsAlive closes", conn.recv(64), b"")
    ok("closed after ~2 timeouts", time.time() - t0 < 1.5, time.time() - t0)
    check("black full field on disconnect", wait_plays(n + 1), n + 1)
    check("black", ([(r[:4], key(r[4])) for r in last().rects], key(last().background)),
          ([((0, 0, 1280, 720), (0.0,) * 3)], (0.0,) * 3))
    check("status cleared", rig.srv.client(), None)
    conn.close()
    conn = trigger("LS:x:%d" % tport)
    ok("listening again after the session", conn is not None)
    if conn:
        n = len(rig.backend.plays)
        conn.sendall(two.encode())
        check("same pattern after a reconnect is drawn", wait_plays(n + 1), n + 1)
        n = len(rig.backend.plays)
        conn.close()
        check("PC closes: black", wait_plays(n + 1), n + 1)
    u.close()
    pc.close()
    rig.close()


def t_payload_unit():
    """Template engine details with no socket (pattern.pm)."""
    st = templates.Store()
    eng = templates.Engine(st, lambda k: None)
    st.set_conf("A", "TEMPLATE", "PERMANENT=1\nDRAW=DYNAMIC||CIRCLE\nDIM=DYNAMIC\nRGB=DYNAMIC||1,2,3\n"
                "POSITION=DYNAMIC\nBITS=DYNAMIC||10\nEND=1")
    r, text = eng.get_pattern("TESTTEMPLATE", "A", "", 1920, 1080, 8)
    check("DYNAMIC||defaults file", (r, text),
          ("OK", "PATTERN_NAME=A\nPERMANENT=1\nDRAW=CIRCLE\nDIM=640,360\nRGB=1,2,3\n"
                 "POSITION=960,540\nBITS=10\nEND=1\nFRAME=1\n"))
    r, text = eng.get_pattern("TESTTEMPLATE", "A", "9,9,9;;;;;;;;", 1920, 1080, 8, "LIMITED", 4095)
    check("source max/range before END", text.split("\n")[-5:],
          ["SOURCE_MAX=4095", "SOURCE_RANGE=LIMITED", "END=1", "FRAME=1", ""])
    # daemon.pm:707-722: the payload PGenerator's get_pattern gets (v<<2 at 10-bit), and the
    # triplets it scaled with their own-depth codes (drawn losslessly)
    p, ex = templates.prepare_payload("1,2,3;4,5,6;RECTANGLE10bit", True, False)
    check("prepare_payload DV", (p, {k: (tuple(v), v.cmax) for k, v in ex.items()}),
          ("4,8,12;16,20,24;RECTANGLE;;;;;;8", {0: ((1, 2, 3), 1023), 1: ((4, 5, 6), 1023)}))
    check("prepare_payload HCFR", templates.prepare_payload("1,2,3;;;;;;;;10;x", False, True),
          ("1,2,3;;;;;;;;8", None))
    check("get_position scaled round", templates.get_position("100,100", "RECTANGLE", "10.6,3", False,
                                                               1920, 1080), "10,3")
    check("perl_int_str big", templates.perl_int_str(3e19), "3e+19")


def t_kodi_wiring():
    """kodi.Service.start/shutdown with the classic, DeviceControl and LightSpace settings."""
    import fake_kodi
    fake_kodi.install()
    settings = fake_kodi.settings
    try:
        from patterngen import kodi
    except Exception as exc:        # kodi.py needs what another module provides
        FAILS.append("kodi import: %r" % (exc,))
        return
    tcp, dc, ls = free_port(), free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
    kodi.DEVICECONTROL_PORT, kodi.LIGHTSPACE_PORT = dc, ls
    settings.update(upgci_enabled="false", classic_enabled="true", classic_port=str(tcp),
                    devicecontrol_discovery="true", lightspace_enabled="true",
                    notifications="false")
    svc = kodi.Service()
    svc.start()
    time.sleep(0.2)
    srv = svc.server
    ok("server started", srv is not None)
    if srv:
        check("only the classic TCP port", sorted(s.getsockname()[1] for s in srv.sockets
                                                   if s.type == socket.SOCK_STREAM), [tcp])
        c = socket.create_connection(("127.0.0.1", tcp))
        c.settimeout(3)
        check("classic port answers", q(c, "IS_ALIVE"), b"ALIVE" + END)
        c.close()
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.settimeout(1)
        u.sendto(b"Who is a PGenerator", ("127.0.0.1", dc))
        try:
            r = u.recv(64)
        except socket.timeout:
            r = None
        ok("DeviceControl answers", r and r.startswith(b"I am a PGenerator "), r)
        u.close()
        ok("LightSpace listening", srv.lightspace is not None)
    svc.shutdown()
    time.sleep(1.3)
    for p, kind in ((tcp, socket.SOCK_STREAM), (dc, socket.SOCK_DGRAM), (ls, socket.SOCK_DGRAM)):
        ok("port %d closed after shutdown" % p, bindable(p, kind))
    settings.update(classic_enabled="false", devicecontrol_discovery="false",
                    lightspace_enabled="false")
    svc.start()
    check("nothing enabled: no server", svc.server, None)
    svc.shutdown()


for t in (t_payload_unit, t_hcfr, t_templates, t_shapes_simple, t_cmd, t_devicecontrol,
          t_lightspace, t_kodi_wiring):
    try:
        t()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        FAILS.append("%s raised %r" % (t.__name__, exc))

for f in FAILS:
    print("FAIL", f)
print("classic/DeviceControl/LightSpace: %d failures" % len(FAILS))
sys.exit(1 if FAILS else 0)
