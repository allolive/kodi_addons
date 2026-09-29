"""APL surround (0.4.14): the add-on against PGenerator's own Perl (daemon.pm's
calman_apl_levels / calman_target_max / calman_scale_value / calman_apl_bg_value, run as
they ship) over bit depths, wire ranges, Dolby Vision, patch levels, windows and APLs."""
import itertools, os, re, subprocess, sys, tempfile
from testlib import PGREF  # noqa: E402  (puts the add-on on sys.path)
from patterngen import upgci

PG = PGREF
src = open(os.path.join(PG, "daemon.pm"), encoding="latin-1").read()
subs = []
for name in ("calman_apl_levels", "calman_dv_source_max", "calman_target_max",
             "calman_scale_value", "calman_apl_bg_value"):
    m = re.search(r"^sub %s \(@\) \{.*?^\}\n" % name, src, re.S | re.M)
    assert m, name
    subs.append(m.group(0))

cases = []
for bits, rng, dv in itertools.product((8, 10, 12), (1, 2), (0, 1)):
    for codes, cmax in (((64, 64, 64), 1023), ((512, 512, 512), 1023), ((940, 940, 940), 1023),
                        ((1023, 0, 0), 1023), ((300, 700, 100), 1023), ((128, 128, 128), 255),
                        ((0, 0, 0), 1023)):
        for win, apl in ((10, 18), (10, 50), (25, 30), (50, 10), (1, 99)):
            cases.append((bits, rng, dv, codes, cmax, win, apl))

perl = ["our %pgenerator_conf; our $bits_default; our $calman_win_size = 10;",
        "sub log {}"] + subs + ["""
while (my $l = <STDIN>) {
  chomp $l; my ($bits, $rng, $dv, $r, $g, $b, $cmax, $win, $apl) = split / /, $l;
  $bits_default = $bits;
  %pgenerator_conf = (rgb_quant_range => $rng, dv_status => $dv, is_std_dovi => $dv);
  my $rgb = join(",", map { &calman_scale_value($_, $cmax) } ($r, $g, $b));
  print &calman_apl_bg_value($rgb, $win, $apl, "t", "0,0,0"), "\\n";
}"""]
with tempfile.NamedTemporaryFile("w", suffix=".pl", delete=False) as f:
    f.write("\n".join(perl))
inp = "".join("%d %d %d %d %d %d %d %d %s\n" % (b, r, d, *c, m, w, a) for b, r, d, c, m, w, a in cases)
pg_out = subprocess.run(["perl", f.name], input=inp, capture_output=True, text=True)
os.unlink(f.name)
assert pg_out.returncode == 0, pg_out.stderr
pg = pg_out.stdout.split("\n")

class Stub:
    def __init__(self, bits, rng, dv):
        self.bits, self.dv, self.win = bits, dv, 10
        self.conf = {"rgb_quant_range": str(rng)}
    def target_max(self):
        return 4095 if self.dv else {10: 1023, 12: 4095}.get(self.bits, 255)
    def bits_default(self):
        return self.bits

fails = []
for i, (bits, rng, dv, codes, cmax, win, apl) in enumerate(cases):
    got = upgci.Calman.apl_bg_value(Stub(bits, rng, dv), upgci.Code(codes, cmax), win, apl, None)
    ours = "%d,%d,%d" % tuple(got.codes)
    if ours != pg[i]:
        fails.append("bits %d range %d dv %d fg %r/%d win %d apl %s: PGenerator %s, add-on %s"
                     % (bits, rng, dv, codes, cmax, win, apl, pg[i], ours))
print("\n".join(fails[:10]) if fails else "APL surround identical to PGenerator's Perl in %d cases" % len(cases))
sys.exit(1 if fails else 0)
