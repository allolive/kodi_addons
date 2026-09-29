"""Static per-session Dolby Vision metadata, and every requested mode sent as itself.

Drives upgci.Session in-process with a recording backend; clips are rendered for
real and searched for the exact RPU / SEI NAL units. Expected values are worked
out here from ST 2084 directly. Exit status 1 on any mismatch.
"""
import sys
import tempfile

import testlib  # noqa: E402,F401  the add-on on sys.path
from patterngen import dovi, generator, hevc, patterns, upgci  # noqa: E402

from testlib import FAILS, SCENES, RecordingBackend as Backend, capture_scenes, check, eotf, pq, pq12  # noqa: E402


def ramp_avg(peak):
    n = 4096
    return sum(eotf((i + 0.5) / n * pq(peak)) for i in range(n)) / n


capture_scenes()
CACHE = tempfile.mkdtemp(prefix="pg_meta_")


def session():
    gen = generator.Generator(Backend(), CACHE, lambda *a, **k: None, duration=1)
    gen.set_default_format(1280, 720, 24, 1)
    cal = upgci.Calman(gen, lambda *a, **k: None)
    s = upgci.Session(cal, {"name": "t", "serial": "T", "firmware": "t"}, lambda *a, **k: None)
    return gen, s


def run(s, *cmds):
    for c in cmds:
        s.handle(c)


def clip(gen):
    return open(gen.backend.plays[-1][0], "rb").read()


def expected_rpu(lo, hi, cll, fall, run_mode, first=True):
    """PGenerator's blocks: L1 + L255 only, L255 Relative until Calman picks a mapping
    (cll/fall kept in the signature: no longer in the RPU)."""
    a, b = pq12(lo), pq12(hi)
    return dovi.rpu_nal(dovi.rpu((a, b, (a + b) // 2), a, b, None,
                                 2 if run_mode is None else run_mode, first))


# -- session defaults ---------------------------------------------------------------
lum = (patterns.DV_PEAK, patterns.DV_MIN)
check("default session luminance = PGenerator's DV range", lum, (4000.0, 0.005))

# -- DV: static across patterns, Calman's values once sent ---------------------------
gen, s = session()
run(s, "DSMD:DV", "RGB_S:0940,0940,0940,0100")
a = clip(gen)
run(s, "RGB_S:0064,0064,0064,0010")
b = clip(gen)
rpu0 = expected_rpu(0.005, 4000, 0, 0, None)
check("DV default RPU in white clip", rpu0 in a, True)
check("DV default RPU in black clip", rpu0 in b, True)
check("DV default next-frame RPU", expected_rpu(0.005, 4000, 0, 0, None, False) in b, True)
check("DV source max = PGenerator's 3696", pq12(4000), 3696)
# 0.4.16: profile 5 has no HDR10 base layer, so no mastering SEI in a DV clip
check("DV clip carries no HDR10 mastering SEI", hevc.sei_hdr10(
    (*patterns.DISPLAY_PRIMARIES[1][:4], 1000.0, 0.005), 1000, 400) in b, False)
run(s, "MAXL:1000")                               # same value as the default conf
run(s, "RGB_S:0512,0512,0512,0050")
fall1000 = int(round(ramp_avg(1000)))
# 0.4.12: PGenerator never takes the DV range from Calman: L1 stays 62/3696 (0.005-4000)
check("DV ignores Calman MaxL 1000 (PGenerator's 4000)", expected_rpu(0.005, 4000, 0, 0, None) in clip(gen), True)
run(s, "MINL:0.0001", "MAXCLL:900", "MAXFALL:300", "CONF_DV:ABSOLUTE", "RGB_S:0100,0200,0300,0020")
rpu1 = expected_rpu(0.005, 4000, 0, 0, 1)
check("DV ignores Calman MinL; L255 absolute (L1 + L255 only)", rpu1 in clip(gen), True)
run(s, "RGB_S:0940,0064,0064,0100")
check("DV static across patterns", rpu1 in clip(gen), True)
check("DV L1 = PGenerator's dv_minpq/dv_maxpq", (pq12(0.005), pq12(4000), (pq12(0.005) + pq12(4000)) // 2), (62, 3696, 1879))


gen, s = session()
run(s, "CONF_HDR:ST2084,0.708,0.292,0.170,0.797,0.131,0.046,0.3127,0.3290,0.0050,4000,2000,000800.2200",
    "21_HDR_MetadataMode:4", "RGB_S:0940,0940,0940,0010")
sg = gen.sig
check("CONF_HDR state", (sg.mode, sg.max_luma, sg.min_luma, sg.max_cll, sg.max_fall, sg.dv_map_mode),
      ("dv", 4000, 0.005, 2000, 80, 2))                  # MaxFALL is f[12][:5], as daemon.pm
check("CONF_HDR -> DV relative, PGenerator's range", expected_rpu(0.005, 4000, 0, 0, 2) in clip(gen), True)

gen, s = session()
run(s, "INIT:2.0", "MAXL:2000", "DSMD:DV", "RGB_S:0940,0940,0940,0010")
check("GCI MAXL 2000 -> DV range still PGenerator's 4000", expected_rpu(0.005, 4000, 0, 0, None)
      in clip(gen), True)

# -- every request is sent as the mode it asks for ---------------------------------------
RPU = b"\x7c\x01\x19"
for dsmd, mode in (("SDR", "sdr"), ("HDR10", "hdr10"), ("HLG", "hlg"), ("DV", "dv")):
    gen, s = session()
    run(s, "DSMD:" + dsmd, "RGB_S:0940,0940,0940,0010")
    check("%s sent as %s" % (dsmd, mode), {m for _, m in gen.backend.plays}, {mode})
    check("%s: signal rendered" % dsmd, SCENES[-1][1].mode, mode)
    check("%s: Dolby Vision RPU only for DV" % dsmd, RPU in clip(gen), mode == "dv")

if FAILS:
    print("\n".join(FAILS))
    sys.exit(1)
print("meta OK")
