"""hdmistate: the Amlogic HDMI sysfs parser on the drivers' real output formats,
and its three hooks (service status line, debug overlay line, menu entry + dialog).

Samples reproduce the snprintf formats of common_drivers f9af0b41:
hdmitx21/hdmi_tx_main.c config_show/disp_mode_show/cs_show/cd_show/hdmi_hdr_status_show,
hdmitx21/hw/hdmi_tx_hw.c hdmitx21_pkt_dump, hdmitx_common/hdmitx_sysfs_common.c _dc_cap_show,
and hdmitx20's config_show/disp_mode_show/cd_show/hdmitx_pkt_dump for g12b.
"""
import os, runpy, shutil, sys, tempfile, types

from testlib import ADDON, FAILS as fails, check  # noqa: E402
from patterngen import hdmistate  # noqa: E402


ROOT = tempfile.mkdtemp()


def box(name, files, mode=None):
    d = os.path.join(ROOT, name) + "/"
    os.makedirs(d)
    for k, v in files.items():
        with open(d + k, "w") as f:
            f.write(v)
    mp = d + "display_mode"
    if mode is not None:
        with open(mp, "w") as f:
            f.write(mode + "\n")
    return d, mp


def config21(vic, name, depth, fmt, eotf, col):
    guess = "default"           # hdmitx21: input == output format -> range[0]
    return ("VIC: %d %s\nColour depth: %d-bit\nColourspace: %s\nColour range: %s\nEOTF: %s\n"
            "YCC colour range: %s\nColourimetry: %s\n" % (vic, name, depth, fmt, guess, eotf,
                                                          guess, col))


def disp_mode21(cd, cs, vic, name, v_freq, v_active, pi="P"):
    return ("cd/cs/cr: %d/%d/1\nscramble/tmds_clk_div40: 0/0\ntmds_clk: 371250\nvic: %d\n"
            "name: %s\nenc_idx: 0\npi_mode: %s\nh/v_freq: 67500/%d\npixel_freq: 148500\n"
            "h_total: 2200\nh_active: 1920\nv_total: 1125\nv_active: %d\nh/v_pol: 1/1\n"
            "name: %s\nmode: 0\next_name: \nfrac: 0\nwidth/height: 1920/1080\n"
            "hdmichecksum:\n00000000\ninfo_3d: 0\n" % (cd, cs, vic, name, pi, v_freq, v_active,
                                                       name))


def pkt21(gcp, cs, col, ext, q, yq, vic, drm=None):
    s = ("hdmitx gcp reg config\nGCP.clear_avmute: 1\nGCP.set_avmute: 0\nGCP.color_depth: %s\n"
         "GCP.dc_phase_st: 0\n\nAVI.type: 0x82\nAVI.version: 2\nAVI.length: 13\n"
         "AVI.colorspace: %s\nAVI.scan: none\nAVI.colorimetry: %s\n" % (gcp, cs, col))
    if ext:
        s += "AVI.extended_colorimetry: %s\n" % ext
    s += ("AVI.picture_aspect: 16:9\nAVI.active_aspect: Same as picture_aspect\n"
          "AVI.quantization_range: %s\nAVI.itc: disable\nAVI.nups: unknown\nAVI.video_code: %d\n"
          "AVI.ycc_quantization_range: %s\nAVI.content_type: graphics\nAVI.pixel_repetition: 0\n"
          "AVI.top_bar: 0\nAVI.bottom_bar: 0\nAVI.left_bar: 0\nAVI.right: 0\n\n\n"
          "ACR.N1=0x0\nACR.N2=0x18\nACR.N3=0x0\n\n" % (q, vic, yq))
    if drm:
        s += ("DRM.type: 0x87\nDRM.version: 1\nDRM.length: 26\nDRM.eotf: %s\n"
              "DRM.metadata_id: static metadata\ndisplay_primaries:\nx:34000, y:16000\n"
              "x:13250, y:34500\nx:7500, y:3000\nwhite_point: x:15635, y:16450\n"
              "DRM.max_lum : 1000\nDRM.min_lum : 50\nDRM.max_cll : 1000\nDRM.max_fall : 400\n"
              % drm)
    else:
        s += "DRM PKT not enable\n"
    return s + "\nAUDI.type: 0x84\nAUDI.version: 1\nAUDI.length: 10\n"


DC_CAP = "420,12bit\n420,10bit\n420,8bit\n444,12bit\n444,10bit\n444,8bit\n422,12bit\n" \
         "rgb,12bit\nrgb,10bit\nrgb,8bit\n"

# 1. hdmitx21 SDR 1080p60 4:4:4 10-bit, no colorimetry (what the AM9 printed)
d, m = box("sdr", {
    "config": config21(16, "1920x1080p60hz", 10, "YUV444", "SDR", "unknown"),
    "disp_mode": disp_mode21(5, 2, 16, "1920x1080p60hz", 60000, 1080),
    "cs": "2\n", "cd": "5\n", "hdmi_hdr_status": "SDR",
    "hdmitx_pkt_dump": pkt21("30bit", "444", "none", None, "limited", "limited", 16),
    "dc_cap": DC_CAP}, "1080p60hz")
info = hdmistate.details(d, m)
check("1 summary", hdmistate.summary(info=info), "1080p60 YCC444 10bit nocolorimetry SDR lim/lim")
check("1 effective", info["effective_range"], "limited")
check("1 dc_cap", info["tv_dc_cap"][:2], ["420,12bit", "420,10bit"])
check("1 config range kept apart", info["config_colour_range"], "default")
check("1 drm", info["drm_infoframe"], False)
check("1 via paths", hdmistate.summary(d, m), "1080p60 YCC444 10bit nocolorimetry SDR lim/lim")

# 2. hdmitx21 HDR10 2160p23.976 4:2:2 12-bit BT.2020 with a DRM packet
d, m = box("hdr10", {
    "config": config21(93, "3840x2160p24hz", 12, "YUV422", "HDR10", "BT.2020"),
    "disp_mode": disp_mode21(6, 1, 93, "3840x2160p24hz", 23976, 2160),
    "cs": "1\n", "cd": "6\n", "hdmi_hdr_status": "HDR10-GAMMA_ST2084",
    "hdmitx_pkt_dump": pkt21("36bit", "422", "Extended", "BT.2020", "default", "limited", 93,
                             drm="ST 2084"),
    "dc_cap": DC_CAP}, "2160p24hz")
info = hdmistate.details(d, m)
check("2 summary", hdmistate.summary(info=info), "2160p23.976 YCC422 12bit BT2020 PQ def/lim")
check("2 drm", (info["drm_infoframe"], info["drm_max_lum"], info["drm_min_lum"],
                info["drm_max_cll"], info["drm_max_fall"]), (True, 1000, 50, 1000, 400))
check("2 refresh", info["refresh_hz"], 23.976)

# 3. DV-Std tunnel: AVI RGB, full range, 8-bit; HLG and HDR10+ statuses
d, m = box("dv", {
    "config": config21(97, "3840x2160p60hz", 8, "RGB", "DV-Std", "BT.709"),
    "disp_mode": disp_mode21(4, 0, 97, "3840x2160p60hz", 60000, 2160),
    "cs": "0\n", "cd": "4\n", "hdmi_hdr_status": "DolbyVision-Std",
    "hdmitx_pkt_dump": pkt21("24bit", "RGB", "BT.709", None, "full", "limited", 97)},
    "2160p60hz")
info = hdmistate.details(d, m)
check("3 summary", hdmistate.summary(info=info), "2160p60 RGB 8bit BT709 DV full/lim")
check("3 effective", info["effective_range"], "full")
check("3 no dc_cap", info["tv_dc_cap"], None)
for status, eotf in (("HDR10-GAMMA_HLG", "HLG"), ("HDR10Plus-VSIF", "HDR10+"),
                     ("DolbyVision-Lowlatency", "DV-LL"), ("HDR10-others", "HDR")):
    with open(d + "hdmi_hdr_status", "w") as f:
        f.write(status)
    check("3 eotf %s" % status, hdmistate.details(d, m)["eotf"], eotf)

# 4. hdmitx20 (g12b): disp_mode is only VIC, cd is an index, ranges in AVICONF registers;
#    frac policy on for 2160p24 -> 23.976
d, m = box("g12b", {
    "config": "VIC: 93 3840x2160p24hz\nColour depth: 10-bit\nColourspace: YUV420\n"
              "Colour range: limited\nEOTF: HDR10\nYCC colour range: limited\n"
              "Colourimetry: BT.2020nc\n",
    "disp_mode": "VIC:93\n", "cs": "3\n", "cd": "1\n", "frac_rate_policy": "1\n",
    "hdmi_hdr_status": "HDR10-GAMMA_ST2084",
    "hdmitx_pkt_dump": "hdmitx gcp reg config\nGCP.clear_avmute: 0\nGCP.set_avmute: 0\n"
                       "GCP.default_phase: 1\nGCP.packing_phase: 0\nGCP.color_depth: 30bit\n"
                       "hdmitx avi info reg config\nAVI.colorspace: 420\nAVI.active_aspect: "
                       "enable\nAVI.bar: disable\nAVI.scan: disable\nAVI.colorimetry: Extended\n"
                       "AVI.picture_aspect: 16:9\nAVI.active_aspect: Same as picture_aspect\n"
                       "AVI.itc: disable\nAVI.extended_colorimetry: BT.2020\n"
                       "AVI.quantization_range: limited\nAVI.nups: unknown\n"
                       "AVI.video_code: 93\nAVI.ycc_quantization_range: limited\n"},
    "2160p24hz")
info = hdmistate.details(d, m)
check("4 summary", hdmistate.summary(info=info), "2160p23.976 YCC420 10bit BT2020 PQ lim/lim")
check("4 vic", info["vic"], 93)

# 5. the pipe-joined form the box was quoted in, config only
d, m = box("pipes", {"config": "VIC: 16 1920x1080p60hz | Colour depth: 10-bit | Colourspace: "
                               "YUV444 | Colour range: default | EOTF: SDR | YCC colour range: "
                               "default | Colourimetry: BT.2020 CL"})
info = hdmistate.details(d, m)
check("5 summary", hdmistate.summary(info=info), "1080p60 YCC444 10bit BT2020CL SDR")
check("5 range from config not trusted", info["effective_range"], None)

# 6. nothing there (not Amlogic): all None, no summary, no exception
d, m = os.path.join(ROOT, "none") + "/", os.path.join(ROOT, "none", "mode")
info = hdmistate.details(d, m)
check("6 all None", [k for k, v in info.items() if v is not None], [])
check("6 summary", hdmistate.summary(d, m), None)
check("6 details text", hdmistate.format_details(info).splitlines()[0], "Summary: not available")

# 7. garbage, empty, binary and unreadable nodes never raise
d, m = box("junk", {"config": "VIC: x\nColour depth: ?-bit\n\x00\xff", "disp_mode": "cd/cs/cr: a/b",
                    "cs": "", "cd": "99\n", "hdmi_hdr_status": "\n",
                    "hdmitx_pkt_dump": "AVI PKT not enable\nDRM body get error\n",
                    "dc_cap": "rgb,8bit\nnonsense\n"}, "null")
os.makedirs(d + "frac_rate_policy")          # a directory: open() fails
info = hdmistate.details(d, m)
check("7 junk summary", hdmistate.summary(info=info), None)
check("7 junk dc_cap", info["tv_dc_cap"], ["rgb,8bit"])
check("7 junk mode", info["display_mode"], "null")
text = hdmistate.format_details(info)
check("7 details lists every field", len(text.splitlines()), 1 + len(hdmistate._LABELS))

# 8. interlaced mode name, parse helpers
check("8 1080i", hdmistate.parse_mode("1080i50hz"), {"lines": 1080, "scan": "i", "rate": 50.0})
check("8 name", hdmistate.parse_mode("1920x1080p59.94hz")["rate"], 59.94)

# ---- hooks: overlay line, service status, menu entry ----
from patterngen import overlay  # noqa: E402
from patterngen.patterns import Colour, patch_scene  # noqa: E402
from patterngen import generator  # noqa: E402

d2, m2 = os.path.join(ROOT, "hdr10") + "/", os.path.join(ROOT, "hdr10", "display_mode")
hdmistate.HDMITX, hdmistate.DISPLAY_MODE = d2, m2
gen = generator.Generator(types.SimpleNamespace(play=lambda *a, **k: True, stop=lambda: 0),
                          tempfile.mkdtemp(), lambda *a, **k: None, duration=1)
gen.set_default_format(1920, 1080, 24, 1)
lines = overlay.describe("R", gen.sig,
                         patch_scene(gen.sig.copy(), Colour.gray(0.5), 10, None))
check("overlay has no HDMI line (history-free, 0.4.7)", any(l.startswith("HDMI ") for l in lines), False)
check("overlay drawable", [c for c in lines[-1] if c.upper() not in overlay._FONT], [])
check("overlay fits", len(lines[-1]) <= overlay.MAX_CHARS, True)
hdmistate.HDMITX = os.path.join(ROOT, "none") + "/"
hdmistate.DISPLAY_MODE = os.path.join(ROOT, "none", "mode")
lines = overlay.describe("R", gen.sig,
                         patch_scene(gen.sig.copy(), Colour.gray(0.5), 10, None))
check("overlay no HDMI line off Amlogic", [l for l in lines if l.startswith("HDMI")], [])

# the fake Kodi, with scripted menu choices for default.py
import fake_kodi  # noqa: E402
PROPS, viewed, shown, script = fake_kodi.PROPS, fake_kodi.VIEWED, [], []


def _select(self, heading, items, preselect=-1, **k):
    if heading == "Pattern Generator":
        shown.append(list(items))
        if not script:
            return -1
        want = script.pop(0)
        return [i for i, t in enumerate(items) if t.startswith(want)][0]
    return -1


fake_kodi.Dialog.select = _select
fake_kodi.xbmc.getInfoLabel = lambda l: ""
fake_kodi.xbmcvfs.translatePath = lambda p: ROOT
fake_kodi.install()
from patterngen import kodi  # noqa: E402

svc = kodi.Service()
check("status stopped: no HDMI", PROPS.get(kodi.ADDON_ID + ".status"), "Stopped")
svc.running = True
check("status running off Amlogic", svc.describe(), "Running, no connection enabled")
hdmistate.HDMITX, hdmistate.DISPLAY_MODE = d2, m2
check("status running", svc.describe(),
      "Running, no connection enabled | HDMI 2160p23.976 YCC422 12bit BT2020 PQ def/lim")
svc.running = False
check("status stopped stays plain", svc.describe(), "Stopped")

sys.argv = [os.path.join(ADDON, "default.py"), "none"]
ns = runpy.run_path(sys.argv[0], run_name="default")      # load only, main() not run
text = ns["panels"]()
check("status panels", (text[30][-7:], text[34]),
      ("Stopped", "2160p23.976 YCC422 12bit BT2020 PQ def/lim"))
check("no request mapping card: requests are sent as they are", 33 in text, False)
check("details split in two columns", (text[40].count("\n") + 1, text[41].count("\n") + 1), (10, 9))
check("status has AVI ranges", "AVI YCC quantization (YQ)[/COLOR]  limited" in text[40] + text[41], True)

shutil.rmtree(ROOT, ignore_errors=True)
print("\n".join(fails) if fails else "hdmistate OK")
sys.exit(1 if fails else 0)
