"""Round-1 fix checks on the classic protocol, templates, shapes, LightSpace and the
device_info cache, each against the PGenerator source it cites. Real servers, ports from
PGPORT. Exit 1 on failure.
"""
import base64
import os
import socket
import sys
import time

os.environ["PGPORT"] = str(int(os.environ.get("PGPORT", "12100")) + 2500)
# the rig reads PGPORT when imported: after the offset above
from classic_rig import bbox, check, dvg, dvgrey, END, FAILS, g, grey, key, last, ok, q, Rig, SCENES, xfer  # noqa: E402
from patterngen import upgci  # noqa: E402
from patterngen import lightspace, sysinfo  # noqa: E402


def cmd(c, text):
    r = q(c, "CMD:" + text)
    return r[3:-2].decode("latin-1") if r.startswith(b"OK:") else r


def lit(scene):
    return [(r[:4], key(r[4])) for r in scene.rects if key(r[4]) != (0.0,) * 3]


def conn_from(rig, ip):
    c = socket.socket()
    c.bind((ip, 0))
    c.connect(("127.0.0.1", rig.port))
    c.settimeout(5)
    return c


def t_framing():
    # cf-1: an ETX client's EOF does not clear a status another IP owns (daemon.pm:890, 2767)
    rig = Rig()
    a = conn_from(rig, "127.0.0.2")
    xfer(a, b"STATUS\x03")
    b = conn_from(rig, "127.0.0.1")
    check("CLIENTNAME", q(b, "CLIENTNAME=Foo"), b"OK" + END)
    a.shutdown(socket.SHUT_WR)
    time.sleep(0.4)
    check("EOF after ETX keeps Foo", rig.srv.client(), ("127.0.0.1", "Foo"))
    a.close()
    b.close()
    # cf-2: int() below IV_MIN stays an NV (%g)
    c = rig.conn()
    check("UPLOAD_FILE -9.3e18", q(c, "UPLOAD_FILE:V:f:t:S:-9.3e18:100:x"), b"OK:-9.3e+18%" + END)
    check("UPLOAD_FILE -9.2e18", q(c, "UPLOAD_FILE:V:f:t:S:-9.2e18:100:x"),
          b"OK:-9200000000000000000%" + END)
    c.close()
    rig.close()


def t_commands():
    rig = Rig()
    c = rig.conn()
    # CC-1 / T1: a restart's clean_files drops the templates but PGenerator's own, PERMANENT=yes
    # and the ramdisk's (command.pm:718-745)
    rect = "DRAW=RECTANGLE\nDIM=10,10\nRGB=1,2,3\nPOSITION=0,0\nEND=1\n"
    q(c, "SETCONF:Foo:TEMPLATE:" + rect)
    q(c, "SETCONF:Keep:TEMPLATE:PERMANENT=yes\n" + rect)
    q(c, "SETCONF:Ram:TEMPLATERAMDISK:" + rect)
    q(c, "SETCONF:CalmanCustomPattern2:TEMPLATE:" + rect)
    check("GETCONF before", q(c, "GETCONF:Foo:DIM"), b"OK:10,10" + END)
    for restart in ("RESTARTPGENERATOR:", "CMD:SET_MODE:37", "VIDEO=a;b;c"):
        q(c, "SETCONF:Foo:TEMPLATE:" + rect)
        q(c, restart)
        errs = rig.cal.stats.get("errors", 0)
        check("%s: TESTTEMPLATE:Foo" % restart, q(c, "TESTTEMPLATE:Foo:1,1,1"), b"ERR" + END)
        check("%s: error counted" % restart, rig.cal.stats.get("errors", 0), errs + 1)
        check("%s: GETCONF:Foo" % restart, q(c, "GETCONF:Foo:DIM"), b"OK:" + END)
        check("%s: PERMANENT kept" % restart, q(c, "GETCONF:Keep:DIM"), b"OK:10,10" + END)
        check("%s: CalmanCustomPattern2 kept" % restart,
              q(c, "GETCONF:CalmanCustomPattern2:DIM"), b"OK:10,10" + END)
        check("%s: ramdisk kept" % restart, q(c, "TESTTEMPLATERAMDISK:Ram:1,1,1"), b"OK" + END)
    # T2: a restart draws the stored PatternStart (command.pm:642)
    q(c, "SETCONF:PatternStart:TEMPLATE:DRAW=RECTANGLE\nDIM=100,100\nRGB=255,0,0\nBG=0,0,255\n"
         "POSITION=0,0\nEND=1\n")
    q(c, "RESTARTPGENERATOR:")
    check("restart shows the stored PatternStart", (key(last().background), lit(last())),
          ((0.0, 0.0, 1.0), [((0, 0, 100, 100), (1.0, 0.0, 0.0))]))
    # CC-2: the DV flags are numeric (daemon.pm:652-657, command.pm:177-181)
    check("HCFR range with DV_STATUS 01", upgci.hcfr_source_range(
        "235,235,235;16,16,16", dict(rig.cal.conf, dv_status="01")), "")
    cmd(c, "SET_PGENERATOR_CONF_IS_STD_DOVI:01")
    q(c, "RESTARTPGENERATOR:")
    check("IS_STD_DOVI 01 + restart: DV", [cmd(c, "GET_PGENERATOR_CONF_" + k) for k in
                                           ("DV_STATUS", "SIGNAL_MODE", "IS_HDR", "EOTF",
                                            "COLORIMETRY")], ["1", "dv", "1", "2", "9"])
    check("signal DV", rig.gen.sig.mode, "dv")
    c.close()
    rig.close()

    rig = Rig()
    c = rig.conn()
    # CC-3 / CC-4: mode_idx stored as sent and reported; the renderer takes atoi()
    check("MODE_IDX of the start-up mode (1280x720p60)", cmd(c, "GET_PGENERATOR_CONF_MODE_IDX"),
          "37")
    cmd(c, "SET_PGENERATOR_CONF_MODE_IDX:24")
    check("MODE_IDX set", cmd(c, "GET_PGENERATOR_CONF_MODE_IDX"), "24")
    check("ALL lists mode_idx", "mode_idx:24" in base64.b64decode(
        cmd(c, "GET_PGENERATOR_CONF_ALL")).decode(), True)
    # get_hdmi_info rebuilds $preferred_mode from the conf mode_idx (command.pm:1373-1383)
    check("GET_MODE reports the stored mode_idx", cmd(c, "GET_MODE").split("[")[0], "24")
    check("... the renderer keeps its mode until a restart", rig.gen.sig.width, 1280)
    cmd(c, "SET_MODE:2")
    check("SET_MODE:2", (cmd(c, "GET_MODE").split("[")[0], cmd(c, "GET_PGENERATOR_CONF_MODE_IDX")),
          ("2", "2"))
    cmd(c, "SET_MODE:abc")
    check("SET_MODE:abc is mode 0", (cmd(c, "GET_MODE").split("[")[0],
                                     cmd(c, "GET_PGENERATOR_CONF_MODE_IDX")), ("0", "abc"))
    cmd(c, "SET_MODE:37x")
    check("SET_MODE:37x is mode 37", cmd(c, "GET_MODE").split("[")[0], "37")
    cmd(c, "SET_MODE:")
    check("SET_MODE: auto-selects 2160p30", (cmd(c, "GET_MODE").split("[")[0],
                                             rig.gen.sig.width, rig.gen.sig.fps_num), ("15", 3840, 30))
    cmd(c, "SET_MODE:999")
    check("SET_MODE:999 (PGenerator's renderer crashes): mode kept", cmd(c, "GET_MODE").split("[")[0],
          "15")
    # CC-5: SAVEIMAGES remembers the name (pattern.pm:222-333)
    q(c, "SAVEIMAGES:foo:")
    check("GETPATTERNIMAGE:foo", q(c, "GETPATTERNIMAGE:foo"), b"OK" + END)
    check("GETPATTERNIMAGESLIST:foo", q(c, "GETPATTERNIMAGESLIST:foo"), b"OK:Ready" + END)
    check("GETPATTERNIMAGE:bar", q(c, "GETPATTERNIMAGE:bar"), b"None" + END)
    check("GETPATTERNIMAGESLIST:bar", q(c, "GETPATTERNIMAGESLIST:bar"), b"OK:" + END)
    q(c, "RGB=RECTANGLE;10,10;100;1,1,1;0,0,0;0,0;")        # clean_pattern_files
    check("pattern.info removed by a pattern", q(c, "GETPATTERNIMAGE:foo"), b"None" + END)
    # CC-6 / CC-7 / CC-8 / CC-9
    check("GET_MAC-lo", cmd(c, "GET_MAC-lo"), "None")
    mac = sysinfo.mac_address("eth0")
    check("ETHMAC keeps get_mac's BRD", cmd(c, "ETHMAC"), mac if mac == "None" else mac + "BRD")
    check("BTMAC without hci0", cmd(c, "BTMAC"), sysinfo.bt_mac())
    try:
        gov = open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read().rstrip("\n")
    except OSError:
        gov = ""
    check("GET_SCALING_GOVERNOR", cmd(c, "GET_SCALING_GOVERNOR"), gov)
    check("GET_FREE_DISK", cmd(c, "GET_FREE_DISK"), sysinfo.free_disk())
    ok("GET_FREE_DISK non-empty", cmd(c, "GET_FREE_DISK") != "")
    check("GET_CPU_INFO", cmd(c, "GET_CPU_INFO"), sysinfo.cpu_info())
    real = sysinfo._read
    sysinfo._read = lambda path, mode="r": (b"Ugoos AM9 Pro\x00" if "device-tree" in path
                                            else real(path, mode))
    try:
        check("GET_DEVICE_MODEL keeps its NUL", q(c, "CMD:GET_DEVICE_MODEL"),
              b"OK:Ugoos AM9 Pro\x00" + END)
    finally:
        sysinfo._read = real
    # CC-10: MIN_LUMA NaN is stored and reported as NaN (command.pm:99-118)
    cmd(c, "SET_PGENERATOR_CONF_MIN_LUMA:nan")
    check("MIN_LUMA NaN", (rig.cal.conf["min_luma"], cmd(c, "GET_PGENERATOR_CONF_MIN_LUMA")),
          ("NaN", "NaN"))
    c.close()
    rig.close()


def t_info_cache():
    # CC-11: the device_info thread's cache (info.pm, command.pm:757-808, 1292-1306)
    upgci.INFO_PERIOD = 1.0
    try:
        rig = Rig()

        def next_cycle():
            t0, p = rig.cal.info_t0, upgci.INFO_PERIOD
            n = int((time.monotonic() - t0) / p) + 1
            time.sleep(max(0.0, t0 + n * p + 0.05 - time.monotonic()))
        c = rig.conn()
        check("STATSRESET right after a connection", q(c, "STATSRESET"), b"OK:0" + END)
        check("min_luma from the cache: raw nits", cmd(c, "GET_PGENERATOR_CONF_MIN_LUMA"), "0.005")
        cmd(c, "SET_PGENERATOR_CONF_MIN_LUMA:100")
        check("after a SET: wire units until the next cycle",
              cmd(c, "GET_PGENERATOR_CONF_MIN_LUMA"), "100")
        next_cycle()
        check("next cycle: raw nits again", cmd(c, "GET_PGENERATOR_CONF_MIN_LUMA"), "0.01")
        check("STATSRESET after an idle cycle", q(c, "STATSRESET"), b"OK:1" + END)
        check("STATSRESET again", q(c, "STATSRESET"), b"OK:0" + END)
        next_cycle()
        check("TESTPATTERN after an idle cycle", q(c, "TESTPATTERN:P:RECTANGLE:100,100:0:10,10,10:"),
              b"OK:1" + END)
        check("TESTPATTERN again", q(c, "TESTPATTERN:P:RECTANGLE:100,100:0:10,10,10:"),
              b"OK:0" + END)
        c.close()
        rig.close()
    finally:
        upgci.INFO_PERIOD = 0


def t_templates():
    rig = Rig()
    c = rig.conn()
    # T3: a payload of "0" is false (daemon.pm:715, 776)
    q(c, "SETCONF:HCFR:TEMPLATE:DRAW=RECTANGLE\nDIM=DYNAMIC\nRGB=DYNAMIC\nBG=DYNAMIC\n"
         "POSITION=DYNAMIC\nEND=1")
    q(c, "TESTTEMPLATE:HCFR:0")
    check("TESTTEMPLATE:HCFR:0 draws RGB 16,16,16", lit(last()),
          [((320, 180, 640, 360), grey(16))])
    # T4: $bits_default is a latch the classic setters and restarts leave alone (conf.pm:88-98)
    q(c, "SETCONF:A:TEMPLATE:DRAW=RECTANGLE\nDIM=10,10\nRGB=DYNAMIC\nPOSITION=0,0\nEND=1\n")
    for step in ("CMD:SET_PGENERATOR_CONF_MAX_BPC:10", "RESTARTPGENERATOR:",
                 "CMD:SET_PGENERATOR_CONF_MAX_BPC:12"):
        q(c, step)
        if step.startswith("RESTART"):
            q(c, "SETCONF:A:TEMPLATE:DRAW=RECTANGLE\nDIM=10,10\nRGB=DYNAMIC\nPOSITION=0,0\nEND=1\n")
        q(c, "TESTTEMPLATE:A:255,255,255")
        check("after %s: 255 is still 8-bit white" % step, lit(last()), [((0, 0, 10, 10), (1.0,) * 3)])
    rig.close()


def t_shapes():
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    # shapes-unknown-draw-rgb: the renderer exits (ofApp.cpp:268): black, then a fresh renderer
    q(c, "RGB=FOO;100,100;100;200,200,200;50,50,50;500,500;")
    check("unknown DRAW: black", (key(last().background), last().rects), ((0.0,) * 3, []))
    q(c, "SETCONF:TP:TEMPLATE:DRAW=RECTANGLE\nDIM=100,100\nRGB=200,200,200\nBG=0,0,0\nEND=1\n")
    q(c, "TESTTEMPLATE:TP:1,2,3")
    check("the next renderer starts at 0,0", lit(last()), [((0, 0, 100, 100), grey(200))])
    # shapes-rgb-rect-x-minus1-recentre (ofApp.cpp:412-417)
    q(c, "RGB=RECTANGLE;100,100;100;200,200,200;0,0,0;5,200,-6,0;")
    check("x -1 after d_x centres both axes", lit(last()), [((910, 490, 100, 100), grey(200))])
    # shapes-rgb-rect-negative-dim: the quad spans backwards
    q(c, "RGB=RECTANGLE;-200,-200;100;200,200,200;0,0,0;-1,-1;")
    check("negative DIM", lit(last()), [((860, 440, 200, 200), grey(200))])
    # shapes-circle-negative-radius: the polygon is reflected
    q(c, "RGB=CIRCLE;-200,0;3;200,200,200;0,0,0;-1,-1;")
    xs = [r[0] for r in last().rects] + [r[0] + r[2] for r in last().rects]
    check("negative radius triangle points left (vertices 760..1060, even-pixel sampling)",
          (760 <= min(xs) <= 764, max(xs)), (True, 1060))
    # shapes-trailing-dim-carries: dim1/dim2 reset only at END (ofApp.cpp:98-183)
    q(c, "SETCONF:TG:TEMPLATE:DRAW=RECTANGLE\nRGB=200,200,200\nBG=0,0,0\nPOSITION=0,0\nEND=1\n"
         "DIM=300,300\n")
    q(c, "TESTTEMPLATE:TG:1,1,1")
    check("first: no DIM", lit(last()), [])
    q(c, "TESTTEMPLATE:TG:1,1,1")
    check("second: the trailing DIM carried", lit(last()), [((0, 0, 300, 300), grey(200))])
    # shapes-bits-suffix-overflow: (1<<64)-1 is -1 on a 64-bit Perl
    n = len(rig.backend.plays)
    check("RECTANGLE64bit", q(c, "TESTPATTERN:P:RECTANGLE64bit:100,100:0:10,10,10:"), b"OK:" + END)
    check("RECTANGLE64bit draws nothing", len(rig.backend.plays), n)
    check("RECTANGLE63bit", q(c, "TESTPATTERN:P:RECTANGLE63bit:100,100:0:10,10,10:"), b"OK:0" + END)
    rig.close()


GEOM = '<geometry x="0.25" y="0.25" cx="0.5" cy="0.5"/>'


def t_lightspace():
    w, h = 1280, 720
    p = lightspace.payload
    x = "<calibration><shapes><rectangle>%s</rectangle></shapes></calibration>"
    # LS-1: XMLin's hashes and arrays interpolate as HASH(0x..)/ARRAY(0x..) (client.pm:69-98)
    v = p(x % ('<color><red v="1">200</red><green>1</green><blue>1</blue></color>' + GEOM), w, h)
    ok("attributed child is a HASH", v.startswith("HASH(0x"), v)
    v = p(x % ('<color red="31" green="1" blue="1"><red>40</red></color>' + GEOM), w, h)
    ok("attribute and child of one name: ARRAY", v.startswith("ARRAY(0x"), v)
    v = p(x % ('<color red="30" green="1" blue="1"/><geometry cx="0.5" cy="0.5"><x u="f">0.25</x>'
               '<y>0.25</y></geometry>'), w, h)
    check("attributed x reads 0", v.split(";")[4], "0,180")
    # LS-4: invalid UTF-8 is a parse error (skipped)
    check("0xFF in a comment", p('<calibration><!-- \xff --><shapes><rectangle>'
                                 '<color red="20" green="1" blue="1"/>' + GEOM +
                                 '</rectangle></shapes></calibration>', w, h), None)
    check("UTF-8 text parses", p(x % ('<color red="20" green="1" blue="1"/>' + GEOM)
                                 + '<!-- \xc3\xa9 -->', w, h) is not None, True)
    # LS-3: PeerPort as IO::Socket::INET reads it
    check("PeerPort", [lightspace.peer_port(t) for t in ("foo(23599)", "89135", "0023599", "http")],
          [23599, 23599, 23599, 80])
    # LS-1 / LS-2 over a session: nothing drawn, but the range is taken first
    rig = Rig(ls_timeout=2)
    rig.cal.conf["rgb_quant_range"] = "1"
    pc = socket.socket()
    pc.bind(("127.0.0.1", 0))
    pc.listen(1)
    pc.settimeout(3)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.sendto(b"LS:x:foo(%d)" % pc.getsockname()[1], ("127.0.0.1", rig.ls))
    try:
        conn, _ = pc.accept()
    except socket.timeout:
        FAILS.append("LightSpace did not connect to foo(port)")
        rig.close()
        return
    pats = rig.cal.stats.get("patterns", 0)
    conn.sendall((x % ('<color><red/><green>1</green><blue>1</blue></color>' + GEOM)).encode())
    time.sleep(0.5)
    check("empty colour element: range taken, nothing drawn",
          (rig.cal.range_owner, rig.cal.conf["rgb_quant_range"], rig.cal.stats.get("patterns", 0)),
          ("lightspace", "2", pats + 1))    # + PatternStart of the range restart
    conn.close()
    pc.close()
    time.sleep(0.3)
    rig.close()


def t_r2fix():
    """Round-2 parity fixes: classic commands, the template store, shapes, LightSpace."""
    from patterngen import templates
    rig = Rig()
    c = rig.conn()
    # cf-r2-1: a plugin upload's last chunk: set_plugin prints ERR (PGenerator_cmd.pl:1172-1183)
    check("PLUGINS END_UPLOAD", q(c, "UPLOAD_FILE:PLUGINS:PGenerator-plugin-x.tar.gz:t:END_UPLOAD:0:10:"
                                     "eA=="), b"ERR:100%" + END)
    check("PLUGINS chunk", q(c, "UPLOAD_FILE:PLUGINS:a:t:X:0:10:eA=="), b"OK:0%" + END)
    check("VIDEO END_UPLOAD", q(c, "UPLOAD_FILE:VIDEO:a:t:END_UPLOAD:0:10:eA=="), b"OK:100%" + END)
    # cc2-cpu-fields: one field each (command.pm:1228-1230)
    real = sysinfo._read

    def fake(path, mode="r"):
        if path == "/proc/cpuinfo":
            t = "processor\t: 0\nHardware\t: Amlogic\nRevision\t: 0400\nSerial\t\t: 2a0c1234abcd\n"
            return t.encode() if "b" in mode else t
        return real(path, mode)
    sysinfo._read = fake
    try:
        check("CPU getters", [cmd(c, k) for k in ("GET_CPU_INFO", "GET_CPU_HARDWARE",
                                                   "GET_CPU_REVISION", "GET_CPU_SERIAL")],
              ["Amlogic 0400 2a0c1234abcd", "Amlogic", "0400", "2a0c1234abcd"])
        check("CPU MULTIPLE", q(c, "CMD:MULTIPLE:GET_CPU_HARDWARE:GET_CPU_SERIAL"),
              b"OK:\nGET_CPU_HARDWARE:Amlogic\nGET_CPU_SERIAL:2a0c1234abcd" + END)
    finally:
        sysinfo._read = real
    # cc2-clientname-zero: a stored name "0" is Perl-false (daemon.pm:620)
    check("CLIENTNAME:#0", q(c, "CLIENTNAME:#0"), b"OK" + END)
    q(c, "TESTTEMPLATE:PatternDynamic:16,16,16")
    check("name 0: DeviceControl", rig.srv.client(), ("127.0.0.1", "DeviceControl"))
    # cc2-mode-report-follows-mode_idx: get_hdmi_info reports the conf mode_idx
    cmd(c, "SET_PGENERATOR_CONF_MODE_IDX:24")
    check("stored mode reported", [cmd(c, k) for k in ("GET_MODE", "GET_HDMI_INFO", "GET_RESOLUTION")],
          ["24[1920x1080 60.00Hz 148.50MHz phsyncpvsync]",
           "MODETEST (24) RGB full 16:9, 1920x1080 @ 60.00Hz, progressive", "1920x1080"])
    check("... the renderer's mode until a restart", rig.gen.sig.width, 1280)
    c.close()
    rig.close()
    # cc2-filelist-delete-template-store: the template directories are $var_dir/tmp and
    # running/tmp (get_destination, file.pm:64-71)
    rig = Rig()
    c = rig.conn()
    rect = "DRAW=RECTANGLE\nDIM=10,10\nRGB=1,2,3\nPOSITION=0,0\nEND=1\n"
    q(c, "SETCONF:Foo:TEMPLATE:" + rect)
    q(c, "SETCONF:Bar:TEMPLATERAMDISK:" + rect)
    check("GETFILELIST:tmp", q(c, "GETFILELIST:tmp"), b"OK:Foo\nPatternDynamic\nPatternStart" + END)
    check("GETFILELIST:running/tmp", q(c, "GETFILELIST:running/tmp"), b"OK:Bar" + END)
    check("GETFILELIST:VIDEO", q(c, "GETFILELIST:VIDEO"), b"OK:" + END)
    check("DELETE:tmp:Foo", q(c, "DELETE:tmp:Foo"), b"OK" + END)
    check("deleted: TESTTEMPLATE", q(c, "TESTTEMPLATE:Foo:100,100,100"), b"ERR" + END)
    check("deleted: GETCONF", q(c, "GETCONF:Foo:DRAW"), b"OK:" + END)
    # templates/T5-fs-names: a template name is a path (conf.pm:139-173, pattern.pm:417-440)
    n = len(SCENES)
    check("../tmp/PatternStart", q(c, "TESTTEMPLATE:../tmp/PatternStart:"), b"OK" + END)
    check("... drawn", len(SCENES), n + 1)
    q(c, "SETCONF:a/b:TEMPLATE:" + rect)
    check("a/b not stored", (q(c, "GETCONF:a/b:DIM"), q(c, "TESTTEMPLATE:a/b:")),
          (b"OK:" + END, b"ERR" + END))
    for name in ("", ".", ".."):
        q(c, "SETCONF:%s:TEMPLATE:%s" % (name, rect))
        check("%r not stored" % name, q(c, "TESTTEMPLATE:%s:" % name), b"ERR" + END)
    check("'' leaves .tmp, '.' ..tmp, '..' ...tmp", q(c, "GETFILELIST:tmp"),
          b"OK:...tmp\n..tmp\n.tmp\nPatternDynamic\nPatternStart" + END)
    q(c, "SETCONF:../running/tmp/Z:TEMPLATE:" + rect)
    check("../running/tmp/Z is a ramdisk template", q(c, "TESTTEMPLATERAMDISK:Z:"), b"OK" + END)
    q(c, "SETCONF:A.tmp:TEMPLATE:" + rect)
    q(c, "SETCONF:A:TEMPLATE:" + rect)
    check("SETCONF:A renames A.tmp away", (q(c, "GETCONF:A.tmp:DIM"), q(c, "TESTTEMPLATE:A.tmp:")),
          (b"OK:" + END, b"ERR" + END))
    # templates/T6: clean_pattern_files after TESTTEMPLATE unlinks /.jpg$/ and /.png$/
    q(c, "SETCONF:bars.png:TEMPLATE:" + rect)
    q(c, "SETCONF:Xjpg:TEMPLATE:" + rect)
    check("bars.png drawn once", (q(c, "TESTTEMPLATE:bars.png:"), q(c, "TESTTEMPLATE:bars.png:"),
                                  q(c, "GETCONF:Xjpg:DIM")),
          (b"OK" + END, b"ERR" + END, b"OK:" + END))
    # templates/T7: a VAR= key is a Perl regex (pattern.pm:467-473)
    q(c, "SETCONF:V:TEMPLATE:VAR=\\h=,\nDRAW=RECTANGLE\nDIM=100 100\nRGB=200,200,200\n"
         "POSITION=0,0\nEND=1")
    q(c, "TESTTEMPLATE:V:")
    check("VAR=\\h", lit(last()), [((0, 0, 100, 100), (round(200 / 255.0, 7),) * 3)])
    # templates/T8: a template named LUT ends at its first RGB= line (lut() closes LUT)
    q(c, "SETCONF:LUT:TEMPLATE:DRAW=RECTANGLE\nDIM=10,10\nRGB=1,2,3\nBG=4,5,6\nRGB=7,8,9\n"
         "POSITION=1,1\nEND=1")
    c.close()
    rig.close()
    st = templates.Store()
    eng = templates.Engine(st, lambda k: None)
    st.set_conf("LUT", "TEMPLATE", "DRAW=RECTANGLE\nDIM=10,10\nRGB=1,2,3\nBG=4,5,6\nRGB=7,8,9\n"
                "POSITION=1,1\nEND=1")
    check("LUT truncated", eng.get_pattern("TESTTEMPLATE", "LUT", "", 1280, 720, 8),
          ("OK", "PATTERN_NAME=LUT\nBITS=8\nDRAW=RECTANGLE\nDIM=10,10\nRGB=1,2,3\nFRAME=1\n"))
    for k, text, want in (("\\h", "a b\tc", "a_b_c"), ("\\K", "ab", "_a_b_"), ("a\\Kb", "aab", "aa_"),
                          ("\\v", "a\n", "a_"), ("\\N", "ab", "__"), ("\\p{L}", "a1", "_1"),
                          ("\\G.", "ab", "__"), ("[[:digit:]]", "a1", "a_"), ("\\Z", "ab\n", "ab_\n_"),
                          ("\\q", "q", "_"), ("\\x{41}", "A", "_"), ("(?<n>a)\\k<n>", "aa", "_"),
                          ("[\\d-z]", "-5", "__")):
        rx = templates.perl_regex(k)
        check("VAR key %r" % k, rx and templates.perl_sub(rx[0], rx[1], "_", text), want)
    # shapes-text-size-nonpositive: FreeType's 1 pt floor (not a mirrored size)
    rig = Rig()
    c = rig.conn()
    boxes = []
    for sz in ("-100", "0", "1", "100"):
        q(c, "RGB=TEXT;%s,0;0;255,255,255;0,0,0;100,300;HELLO" % sz)
        boxes.append(bbox(last().rects))
    check("TEXT size -100 and 0 as 1 pt", (boxes[0] == boxes[2], boxes[1] == boxes[2],
                                          boxes[3] == boxes[2]), (True, True, False))
    c.close()
    rig.close()
    # lightspace/LS-5: XMLin does not expand namespaces (client.pm:69, NSExpand off)
    g = '<geometry x="0.25" y="0.25" cx="0.5" cy="0.5"/>'
    for name, x in (
            ("shapes", '<calibration><shapes xmlns="urn:x"><rectangle><color red="1" green="2" '
                       'blue="3"/>' + g + '</rectangle></shapes></calibration>'),
            ("rectangle", '<calibration><shapes><rectangle xmlns="urn:x"><color red="1" green="2" '
                          'blue="3"/>' + g + '</rectangle></shapes></calibration>'),
            ("geometry", '<calibration><shapes><rectangle><color red="1" green="2" blue="3"/>'
                         '<geometry xmlns="urn:x" x="0.25" y="0.25" cx="0.5" cy="0.5"/>'
                         '</rectangle></shapes></calibration>')):
        check("LS xmlns on %s" % name, lightspace.payload(x, 1920, 1080),
              "1,2,3;0,0,0;RECTANGLE;960,540;480,270;100;;;8")
    check("LS a:shapes is another name", lightspace.payload(
        '<calibration><a:shapes xmlns:a="urn:x"><rectangle><color red="1" green="2" blue="3"/>' + g +
        '</rectangle></a:shapes></calibration>', 1920, 1080), ",,;0,0,0;RECTANGLE;0,0;0,0;100;;;8")


def t_r3fix():
    """Round-3 fixes: classic protocol, templates, shapes and LightSpace."""
    import base64 as b64
    import io
    import zipfile

    def up(c, where, name, data, status="END_UPLOAD", start="0"):
        return q(c, "UPLOAD_FILE:%s:%s:text/plain:%s:%s:10:%s" % (
            where, name, status, start, b64.b64encode(data).decode()))
    rect = b"DRAW=RECTANGLE\nDIM=100,100\nRGB=255,0,0\nPOSITION=0,0\nEND=1\n"
    # cf-r3-1: END_UPLOAD renames the upload into "$var_dir/$where" (daemon.pm:993-1030)
    rig = Rig()
    c = rig.conn()
    check("upload tmp", up(c, "tmp", "UPL", rect), b"OK:100%" + END)
    check("upload listed", q(c, "GETFILELIST:tmp"), b"OK:PatternDynamic\nPatternStart\nUPL" + END)
    check("upload GETCONF", q(c, "GETCONF:UPL:RGB"), b"OK:255,0,0" + END)
    check("upload TESTTEMPLATE", q(c, "TESTTEMPLATE:UPL:1,1,1"), b"OK" + END)
    check("upload drawn", [(r[:4], key(r[4])) for r in last().rects],
          [((0, 0, 100, 100), (1.0, 0.0, 0.0))])
    up(c, "tmp", "M2", rect[:20], "UPLOADING", "0")
    up(c, "tmp", "M2", rect[20:], "END_UPLOAD", "5")
    check("two-chunk upload", q(c, "GETCONF:M2:DIM"), b"OK:100,100" + END)
    up(c, "running/tmp", "RAM", rect)             # a tmpfs: rename(2) from /tmp fails
    check("upload to the ramdisk not stored", q(c, "TESTTEMPLATERAMDISK:RAM:1,1,1"), b"ERR" + END)
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Z1", rect.decode())
        zf.writestr("Z2", "DRAW=CIRCLE\nEND=1\n")
    up(c, "tmp", "whatever.zip", z.getvalue())
    check("Zip unzipped", (q(c, "GETCONF:Z1:DIM"), q(c, "GETCONF:Z2:DRAW")),
          (b"OK:100,100" + END, b"OK:CIRCLE" + END))
    # cf-r3-2: HTMLIMAGELIST.disabled in a directory of the path answers 404 (daemon.pm:900-911)
    check("SETCONF HTMLIMAGELIST.disabled", q(c, "SETCONF:HTMLIMAGELIST.disabled:TEMPLATE:x"),
          b"OK" + END)
    h = rig.conn()
    got = xfer(h, b"GET /tmp/index.html HTTP/1.1\r\n", idle=0.5)
    check("GET listing disabled", got, b"HTTP/1.0 404 Not Found\r\n" + END)
    h.close()
    h = rig.conn()
    check("GET /frames still listed", xfer(h, b"GET /frames/index.html HTTP/1.1\r\n",
                                           idle=0.5)[:17], b"HTTP/1.0 200 OK\r\n")
    h.close()
    # cc3-delete-plugins-reply: set_plugin dies on a missing archive, printing nothing
    check("DELETE:PLUGINS:foo", q(c, "DELETE:PLUGINS:foo"), END)
    check("DELETE:PLUGINS", q(c, "DELETE:PLUGINS"), END)
    check("DELETE:VIDEO:foo", q(c, "DELETE:VIDEO:foo"), b"OK" + END)
    # cc3-output-range-string-not-numeric: get_hdmi_info compares the connector's numbers
    cmd(c, "SET_PGENERATOR_CONF_RGB_QUANT_RANGE:02")
    cmd(c, "SET_PGENERATOR_CONF_COLOR_FORMAT:01")
    q(c, "RESTARTPGENERATOR:")
    check("GET_OUTPUT_RANGE 02/01", cmd(c, "GET_OUTPUT_RANGE"), "YCbCr444 full")
    check("GET_HDMI_INFO 02/01", " YCbCr444 full 16:9" in cmd(c, "GET_HDMI_INFO"), True)
    cmd(c, "SET_PGENERATOR_CONF_RGB_QUANT_RANGE:1.0")
    q(c, "RESTARTPGENERATOR:")
    check("GET_OUTPUT_RANGE 1.0", cmd(c, "GET_OUTPUT_RANGE"), "YCbCr444 limited")
    # templates/T9: the GETCONF/SETCONF key is a Perl regex (conf.pm:125, 152)
    q(c, "SETCONF:Q:TEMPLATE:DRAW=RECTANGLE\nDIM=10,10\nRGB=1,2,3\nEND=1\n")
    for k, want in (("D.M", "10,10"), ("RGB|DIM", "10,10"), ("^DRAW", "RECTANGLE"),
                    ("(?i)rgb", "1,2,3"), ("(R)GB", "R"), ("RGB", "1,2,3")):
        check("GETCONF key %r" % k, q(c, "GETCONF:Q:" + k), b"OK:" + want.encode() + END)
    q(c, "SETCONF:Q:D[I]M:7,7")
    check("SETCONF D[I]M drops DIM=", "DIM=10,10" in rig.cal.templates.get("Q"), False)
    q(c, "SETCONF:Q:RGB|DIM:7,7")
    check("SETCONF RGB|DIM drops RGB=", "RGB=1,2,3" in rig.cal.templates.get("Q"), False)
    check("GETCONF PatternStart DR.W", q(c, "GETCONF:PatternStart:DR.W"), b"OK:RECTANGLE" + END)
    # templates/T11: a template named PATTERN or IDENTIFY loses its handle (pattern.pm:441,
    # 531-535, 639-642)
    q(c, "SETCONF:A:TEMPLATE:DRAW=CIRCLE\nDIM=5,5\nEND=1\n")
    body = "DRAW=RECTANGLE\nDIM=10,10\nMACRO=A\nRGB=1,1,1\nDIM=20,20\nEND=1\n"
    for name, cut in (("PATTERN", True), ("G", False)):
        q(c, "SETCONF:%s:TEMPLATE:%s" % (name, body))
        _, text = rig.cal.engine.get_pattern("TESTTEMPLATE", name, "9,9,9", 1920, 1080, 8)
        check("template %s after MACRO" % name, "DIM=20,20" in text, not cut)
    img = "DRAW=IMAGE\nIMAGE=/nonexistent.png\nDIM=NATIVE\nRGB=1,1,1\nEND=1\n"
    for name, cut in (("IDENTIFY", True), ("I2", False)):
        q(c, "SETCONF:%s:TEMPLATE:%s" % (name, img))
        _, text = rig.cal.engine.get_pattern("TESTTEMPLATE", name, "9,9,9", 1920, 1080, 8)
        check("template %s after DIM=NATIVE" % name, "END=1" in text, not cut)
    c.close()
    rig.close()

    # shapes-r3-rgb-file-dim-not-reset: the RGB=/TESTPATTERN file's END resets DIM
    for between in ("RGB=RECTANGLE;100,100;0;255,0,0;0,0,0;0,0;",
                    "TESTPATTERN:X:RECTANGLE:100,100:0:255,0,0:"):
        rig = Rig(fmt=(1920, 1080, 60, 1))
        c = rig.conn()
        q(c, "SETCONF:TG:TEMPLATE:DRAW=RECTANGLE\nRGB=200,200,200\nBG=0,0,0\nPOSITION=0,0\n"
             "END=1\nDIM=300,300\nSOURCE_RANGE=LIMITED\n")
        q(c, "SETCONF:TB:TEMPLATE:DRAW=RECTANGLE\nRGB=100,100,100\nBG=0,0,0\nPOSITION=0,0\n"
             "END=1\n")
        q(c, "TESTTEMPLATE:TG:1,1,1")
        q(c, between)
        q(c, "TESTTEMPLATE:TB:1,1,1")
        check("DIM reset by %s" % between.split(":")[0].split("=")[0],
              [r[:4] for r in last().rects if key(r[4]) != (0.0,) * 3], [])
        c.close()
        rig.close()
    # shapes-r3-rgb-unreadable-bg-clears: an unreadable BG keeps the renderer's last one
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    q(c, "RGB=RECTANGLE;100,100;0;255,0,0;0,0,255;0,0;")
    for bad in ("abc,0,0", "1.5,0,0"):
        q(c, "RGB=RECTANGLE;100,100;0;255,0,0;%s;0,0;" % bad)
        check("BG %s keeps blue" % bad, key(last().background), (0.0, 0.0, 1.0))
    c.close()
    rig.close()

    # templates/T10 + shapes-r3-dv-bg-depth-per-triplet: in standard Dolby Vision only the
    # triplets PGenerator scales keep their own depth; the rest is read as 12-bit codes
    rig = Rig(fmt=(1920, 1080, 60, 1))
    c = rig.conn()
    cmd(c, "SET_PGENERATOR_CONF_DV_STATUS:1")
    q(c, "RESTARTPGENERATOR:")
    check("DV on", rig.cal.std_dv(), True)

    def fg_bg():
        sc = last()
        return [key(r[4]) for r in sc.rects if key(r[4]) != key(sc.background)], key(sc.background)
    q(c, "SETCONF:Y:TEMPLATE:DRAW=RECTANGLE\nDIM=200,200\nRGB=2048,2048,2048\nBG=0,0,0\n"
         "POSITION=0,0\nEND=1\n")
    q(c, "TESTTEMPLATE:Y:1,1,1")
    check("DV template's own RGB 12-bit", fg_bg(), ([dvgrey(2048, 4095)], dvgrey(0, 4095)))
    q(c, "TESTTEMPLATE:PatternDynamic:")
    check("DV rgb_default 16/4095", fg_bg()[0], [dvgrey(16, 4095)])
    q(c, "SETCONF:N:TEMPLATE:DRAW=RECTANGLE\nDIM=200,200\nRGB=DYNAMIC\nBG=DYNAMIC\n"
         "POSITION=0,0\nEND=1\n")
    q(c, "TESTTEMPLATE:N:2048,2048,2048,0")
    check("DV 4-field patch 12-bit", fg_bg()[0], [dvgrey(2048, 4095)])
    q(c, "TESTTEMPLATE:N:1,1,1;2048,2048,2048,0")
    check("DV 4-field bg 12-bit", fg_bg(), ([dvgrey(1)], dvgrey(2048, 4095)))
    q(c, "TESTTEMPLATE:N:255,0,0;0,0,255")
    check("DV scaled triplets at their own depth", fg_bg(), ([(dvg(255), dvg(0), dvg(0))], (dvg(0), dvg(0), dvg(255))))
    q(c, "TESTTEMPLATE:N:512,0,0;0,0,1023;RECTANGLE10bit")
    check("DV 10-bit triplets", fg_bg(), ([(dvg(512, 1023), dvg(0), dvg(0))], (dvg(0), dvg(0), dvg(1023, 1023))))
    q(c, "RGB=RECTANGLE;100,100;0;1e2,0,0;255,255,255;0,0;")
    check("DV RGB= bg scaled, rgb not", fg_bg(), ([(dvg(100, 4095), dvg(0), dvg(0))], dvgrey(255)))
    q(c, "RGB=RECTANGLE;100,100;0;255,0,0;+255,0,0;0,0;")
    check("DV RGB= rgb scaled, bg not", fg_bg(), ([(dvg(255), dvg(0), dvg(0))], (dvg(255, 4095), dvg(0), dvg(0))))
    q(c, "TESTTEMPLATE:PatternDynamic:255,0,0;+255,0,0;RECTANGLE;100,100;0,0;100;;;8")
    check("DV template bg +255 unscaled", fg_bg(), ([(dvg(255), dvg(0), dvg(0))], (dvg(255, 4095), dvg(0), dvg(0))))
    q(c, "TESTTEMPLATE:PatternDynamic:255,0,0;255,255,255,7;RECTANGLE;100,100;0,0;100;;;8")
    check("DV template 4-field bg unscaled", fg_bg()[1], dvgrey(255, 4095))
    c.close()
    rig.close()

    # lightspace/LS-r3-1: a trailing empty colour field is drawn (the renderer reads 0-2)
    rig = Rig(fmt=(1920, 1080, 60, 1), ls_timeout=3.0)
    pc = socket.socket()
    pc.bind(("127.0.0.1", 0))
    pc.listen(1)
    pc.settimeout(3)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.sendto(b"LS:x:%d" % pc.getsockname()[1], ("127.0.0.1", rig.ls))
    ls, _ = pc.accept()

    def ls_send(x):
        n = len(SCENES)
        ls.sendall(x.encode())
        t = time.time() + 1.5
        while len(SCENES) == n and time.time() < t:
            time.sleep(0.02)
        return len(SCENES) > n
    one = ('<calibration><shapes><rectangle><color red="%s" green="%s" blue="%s"/>'
           '<geometry x="0.25" y="0.25" cx="0.5" cy="0.5"/></rectangle></shapes></calibration>')
    two = ('<calibration><shapes><rectangle><color red="%s" green="%s" blue="%s"/></rectangle>'
           '<rectangle><color red="50" green="50" blue="50"/><geometry x="0.25" y="0.25" '
           'cx="0.5" cy="0.5"/></rectangle></shapes></calibration>')
    check("LS patch blue '9,' drawn", ls_send(one % (21, 7, "9,")), True)
    check("LS patch colour", fg_bg()[0], [(g(21), g(7), g(9))])
    check("LS bg blue '9,' drawn", ls_send(two % (0, 0, "9,")), True)
    check("LS bg colour", fg_bg()[1], (0.0, 0.0, g(9)))
    check("LS patch blue '9,,' drawn", ls_send(one % (22, 7, "9,,")), True)
    check("LS empty first field not drawn", ls_send(one % ("", 7, 9)), False)
    # lightspace/LS-r3-2: the session's end clears the status whoever set it (discovery.pm:76-79)
    cc = rig.conn()
    q(cc, "CLIENTNAME=Foo")
    check("status Foo", rig.srv.client(), ("127.0.0.1", "Foo"))
    ls.close()
    time.sleep(0.8)
    check("LS end clears Foo", rig.srv.client(), None)
    cc.close()
    pc.close()
    u.close()
    rig.close()


for t in (t_framing, t_commands, t_info_cache, t_templates, t_shapes, t_lightspace, t_r2fix, t_r3fix):
    n = len(FAILS)
    try:
        t()
    except Exception as exc:
        import traceback
        FAILS.append("%s crashed: %r\n%s" % (t.__name__, exc, traceback.format_exc()[-800:]))
    print("%-14s %s" % (t.__name__, "ok" if len(FAILS) == n else "%d FAIL" % (len(FAILS) - n)))
for f in FAILS:
    print("FAIL", f)
print("parity round-1 fixes: %d failures" % len(FAILS))
sys.exit(1 if FAILS else 0)
