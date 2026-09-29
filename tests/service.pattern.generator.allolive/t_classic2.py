"""Check round 1 on the classic protocol: the template store and engine against
PGenerator's own Perl (pattern.pm get_pattern, conf.pm set/get_conf_pattern, run with
stubs), draw-suffix depths, BITS above 10, BG=-1 on RGB=, CMD:SET_REFRESH and the
anchored GET_HDMI_INFO family, the HCFR range owner. Ports from PGPORT. Exit 1 on failure.
"""
import time
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

os.environ["PGPORT"] = str(int(os.environ.get("PGPORT", "12100")) + 1500)
# the rig reads PGPORT when imported: after the offset above
from classic_rig import check, END, FAILS, grey, key, last, ok, q, Rig, SCENES  # noqa: E402
from patterngen import templates  # noqa: E402
from testlib import PGREF  # noqa: E402
PG = PGREF

HARNESS = r'''
my $PG = shift; our $dir = shift;
$var_dir = "$dir/var"; $pattern_templates = "$var_dir/tmp"; $command_file = "$var_dir/running/operations.txt";
$bg_default="0,0,0"; $res_default="100"; $bits_default=8; $draw_default="RECTANGLE";
$rgb_default="16,16,16"; $dim_default="640,360"; $position_default="-1,-1"; $text_default="No Text";
$frame_default="1"; $w_s=1280; $h_s=720; $max_x=1280; $max_y=720; $ok_response="OK"; $error_response="ERR";
$test_template_ramdisk_command="TESTTEMPLATERAMDISK"; $lut_file="$PG/../../../etc/PGenerator/lut.txt";
$eth_interface="eth0";
sub stats { } sub get_ip { return "None"; }
sub error { my $m = shift; $m = $error_response if($m eq ""); return $m; }
{ local $SIG{__WARN__} = sub { }; require "$PG/pattern.pm"; require "$PG/conf.pm"; }
{ no warnings; *load_new_pattern_file = sub { }; *log = sub { }; }
sub hx { return unpack("H*", shift // ""); }
while (my $line = <STDIN>) {
  chomp $line;
  my @f = map { pack("H*", $_) } split(/\t/, $line, -1);
  my $op = shift @f; my $out;
  if ($op eq "SET") { eval { set_conf_pattern(@f); }; $out = $@ ? "DIE" : "OK"; }
  elsif ($op eq "GET") { $out = eval { get_conf_pattern(@f) }; $out = "DIE" if $@; }
  else {
    unlink $command_file;
    my $r = eval { get_pattern($f[0], $f[1], $f[2], "x", $f[3], $f[4]) };
    if ($@) { $out = "DIE"; }
    else { my $t = ""; if (open(my $fh, "<", $command_file)) { local $/; $t = <$fh>; close $fh; }
           $out = "$r\x00$t"; }
  }
  print hx($out), "\n";
}
'''

KEYS = ["DRAW=", "DIM=", "RGB=", "BG=", "POSITION=", "RESOLUTION=", "BITS=", "FRAME=", "TEXT=", "IMAGE=",
        "END=1", "# SCALING=DISABLED", "PERMANENT=1", "VAR=K=", "EVAL=x", "PATTERN_NAME=p",
        "SOURCE_MAX=4095", "FOO=1", ""]
BARE = ("END=1", "# SCALING=DISABLED", "PERMANENT=1", "EVAL=x", "PATTERN_NAME=p", "SOURCE_MAX=4095", "")
VALS = ["DYNAMIC", "DYNAMIC||", "RECTANGLE", "CIRCLE", "TRIANGLE", "TEXT", "IMAGE", "BAD", "10,20",
        "640,360", "50%", "0%", "1e3%", "abc%", "-1,-1", "5,6,7,8", "1280,720", "2000,10", "235,235,235",
        "1,2", "a,b,c", "K", "$RGB", "8", "10", "NATIVE", "", "1.5,2.5", "-1", " 1", "3,4", "1,2,3,4",
        "nan%", "inf%", "1e400%", "0x10,5", "1e30,5", "99999999999999999999,1", "-1,-1,5,5", "4,-3",
        "+5,5", "1_0,2", " 5,6", "5 ,6", "10,20,"]


def t_perl_differential(seed, n):
    """Random SETCONF / GETCONF / get_pattern sequences through both implementations."""
    R = random.Random(seed)
    d = tempfile.mkdtemp(prefix="pg_perl_")
    for sub in ("var/tmp", "var/running/tmp"):
        os.makedirs(os.path.join(d, sub))
    for k, v in templates.SHIPPED.items():
        with open(os.path.join(d, "var/tmp", k), "w") as f:
            f.write(v)
    harness = os.path.join(d, "h.pl")
    with open(harness, "w") as f:
        f.write(HARNESS)
    store = templates.Store()
    eng = templates.Engine(store, lambda k: None)

    def line():
        k = R.choice(KEYS)
        if k in BARE:
            return k
        v = R.choice(VALS)
        return k + (v + R.choice(VALS) if v == "DYNAMIC||" else v)

    def template():
        eol = R.choice(["\n", "\r\n"])
        return eol.join(line() for _ in range(R.randint(0, 12))) + (eol if R.random() < 0.8 else "")

    def payload():
        if R.random() < 0.3:
            return R.choice(["", "235,235,235", "1,2,3", "x"])
        return ";".join(R.choice(VALS + [""]) for _ in range(R.randint(1, 10)))

    ops = []
    for _ in range(n):
        r = R.random()
        if r < 0.35:
            name = R.choice(["T", "U", "HCFR"])
            ctype = R.choice(["TEMPLATE", "TEMPLATE", "TEMPLATERAMDISK", "DIM", "RGB", "BG", "TESTCMD"])
            val = template() if ctype.startswith("TEMPLATE") else R.choice(VALS)
            if name == "HCFR" and not re.search(r"(.*)(DRAW=.*)(DRAW=.*)", val.replace("\r\n", "\n"), re.S):
                continue        # README deviation: PGenerator splices stale captures
            ops.append(("SET", name, ctype, val))
        elif r < 0.45:
            ops.append(("GET", R.choice(["T", "U", "HCFR", "PatternStart", "X"]),
                        R.choice(["DIM", "RGB", "BG", "DRAW", "NOPE"])))
        else:
            ops.append(("PAT", R.choice(["TESTTEMPLATE", "TESTTEMPLATE", "TESTTEMPLATERAMDISK"]),
                        R.choice(["T", "U", "HCFR", "PatternDynamic", "PatternStart", "X"]), payload(),
                        R.choice(["", "", "LIMITED"]), R.choice(["0", "255", "1023", "4095", "7"])))
    inp = "".join("\t".join(x.encode("latin-1").hex() for x in op) + "\n" for op in ops)
    out = subprocess.run(["perl", harness, PG, d], input=inp.encode(), capture_output=True)
    shutil.rmtree(d, ignore_errors=True)
    res = [bytes.fromhex(v).decode("latin-1") for v in out.stdout.decode().split("\n")[:-1]]
    check("perl harness answered every op (%s)" % out.stderr.decode()[:200], len(res), len(ops))
    died = bad = 0
    for op, want in zip(ops, res, strict=True):
        if want == "DIE":           # DIM=<negative>%: Perl dies (README: ERR here)
            died += 1
            continue
        if op[0] == "SET":
            store.set_conf(op[1], op[2], op[3])
            got = "OK"
        elif op[0] == "GET":
            got = store.get_conf(op[1], op[2])
        else:
            rep, text = eng.get_pattern(op[1], op[2], op[3], 1280, 720, 8, op[4], int(op[5]))
            got = rep + "\x00" + (text or "")
        if got != want:
            bad += 1
            if bad <= 3:
                FAILS.append("perl differential seed %d %r:\n perl %r\n py   %r" % (seed, op, want, got))
    ok("perl differential exercised", len(ops) - died > n // 2, "(%d ops, %d died)" % (len(ops), died))


def t_depths_and_bg():
    rig = Rig()
    c = rig.conn()
    # a draw suffix below 9 bits: PGenerator accepts 0..255 and draws 8-bit
    for draw in ("RECTANGLE6bit", "RECTANGLE0bit", "RECTANGLE8bit"):
        check("RGB=%s reply" % draw, q(c, "RGB=%s;200,100;0;200,200,200;0,0,0;-1,-1;x" % draw),
              b"OK" + END)
        check("RGB=%s: 8-bit codes" % draw, [key(r[4]) for r in last().rects], [grey(200)])
    n = len(SCENES)
    q(c, "RGB=RECTANGLE6bit;200,100;0;256,0,0;0,0,0;-1,-1;x")
    check("RGB=RECTANGLE6bit 256 rejected (above 255)", len(SCENES), n)
    # BITS 11..16 read at their depth, as RGB=RECTANGLE16bit is
    q(c, "RGB=RECTANGLE16bit;200,100;0;60000,60000,60000;0,0,0;-1,-1;x")
    want = [key(r[4]) for r in last().rects]
    check("TESTTEMPLATE BITS=16", q(c, "TESTTEMPLATE:PatternDynamic:60000,60000,60000;0,0,0;RECTANGLE;"
                                        "200,100;-1,-1;100;1;;16"), b"OK" + END)
    check("BITS=16 = RGB=...16bit", [key(r[4]) for r in last().rects], want)
    check("16-bit code", want, [grey(60000, 65535.0)])
    # RGB= with BG=-1: the renderer does not clear; the last background colour stays
    q(c, "RGB=RECTANGLE;200,100;0;235,235,235;50,50,50;-1,-1;x")
    q(c, "RGB=RECTANGLE;200,100;0;100,100,100;-1,-1,-1;-1,-1;x")
    check("RGB= BG=-1 keeps the background", key(last().background), grey(50))
    check("RGB= BG=-1 draws its shape", [(r[:4], key(r[4])) for r in last().rects],
          [((540, 310, 200, 100), grey(100))])
    q(c, "RGB=RECTANGLE;200,100;0;100,100,100;7,7,7;-1,-1;x")
    check("RGB= BG clears again", key(last().background), grey(7))
    # command.pm:863-880 on KMS; get_cmd_generic's anchored HDMI getters
    check("CMD:SET_REFRESH", q(c, "CMD:SET_REFRESH:60"), b"OK:ERR:Error with tvservice" + END)
    check("CMD:GET_HDMI_INFO_X", q(c, "CMD:GET_HDMI_INFO_X"), b"OK:" + END)
    check("CMD:GET_OUTPUT_RANGE", q(c, "CMD:GET_OUTPUT_RANGE"), b"OK:RGB full" + END)
    c.close()
    # an HCFR client's close releases the range as owner "hcfr" (daemon.pm:2771)
    calls = []
    real = rig.cal.release
    rig.cal.release = lambda owner="calman": (calls.append(owner), real(owner))[1]
    c = rig.conn()
    q(c, "CMD:GET_RESOLUTION")
    c.close()
    for _ in range(100):
        if calls:
            break
        time.sleep(0.02)
    check("HCFR close releases as hcfr", calls, ["hcfr"])
    rig.close()


for t in (lambda: t_perl_differential(1, 2500), lambda: t_perl_differential(7, 2500), t_depths_and_bg):
    try:
        t()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        FAILS.append("raised %r" % exc)

for f in FAILS:
    print("FAIL", f)
print("classic check round 1: %d failures" % len(FAILS))
sys.exit(1 if FAILS else 0)
