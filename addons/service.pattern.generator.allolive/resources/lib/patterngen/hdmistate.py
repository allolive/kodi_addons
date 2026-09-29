"""Live HDMI link state, read from the Amlogic transmitter's sysfs nodes.

What is on the wire, not what was asked for: the AVI infoframe the TV is sent
(colour format, colorimetry, quantization ranges), the deep-colour depth, the
HDR/DV state and the TV's deep-colour capabilities. Every node is optional:
a missing or unreadable one leaves its fields None, so on anything but an
Amlogic box summary() is None and details() is all None.

Formats (common_drivers, hdmitx21 = S905X5 and later, hdmitx20 = g12b/sm1):
  config           "VIC: <n> <name>", "Colour depth: <n>-bit", "Colourspace: ...",
                   "Colour range", "EOTF", "YCC colour range", "Colourimetry"
                   (hdmitx21 guesses both ranges from the format: not used for range)
  disp_mode        hdmitx21: "key: value" lines, "h/v_freq: <Hz>/<mHz>", "pi_mode: P";
                   hdmitx20: only "VIC:<n>"
  cs               AVI colour format 0 RGB, 1 4:2:2, 2 4:4:4, 3 4:2:0
  cd               hdmitx21: enum 4..7 (24/30/36/48 bit); hdmitx20: index 0..3
  hdmi_hdr_status  SDR, HDR10-GAMMA_ST2084, HDR10-GAMMA_HLG, HDR10-others,
                   HDR10Plus-VSIF, DolbyVision-Std, DolbyVision-Lowlatency
  hdmitx_pkt_dump  "GCP.color_depth: 30bit", "AVI.<field>: <value>", "DRM.eotf: ..."
  dc_cap           one "<fmt>,<n>bit" per line (rgb, 444, 422, 420)
The research notes cite the driver lines; the parser takes any of these in any
order and also accepts lines joined with " | ".
"""

from typing import Any
import re

HDMITX = "/sys/class/amhdmitx/amhdmitx0/"
DISPLAY_MODE = "/sys/class/display/mode"
NODES = ("config", "disp_mode", "cs", "cd", "hdmi_hdr_status", "hdmitx_pkt_dump", "dc_cap",
         "frac_rate_policy")

_FORMAT = {"rgb": "RGB", "422": "YCC422", "yuv422": "YCC422", "444": "YCC444",
           "yuv444": "YCC444", "420": "YCC420", "yuv420": "YCC420", "ycbcr422": "YCC422",
           "ycbcr444": "YCC444", "ycbcr420": "YCC420"}
_CS_CODE: dict[Any, str] = {0: "RGB", 1: "YCC422", 2: "YCC444", 3: "YCC420"}
_DEPTH_ENUM: dict[Any, int] = {4: 8, 5: 10, 6: 12, 7: 16}              # hdmitx21 cd / GCP CD field
_DEPTH_INDEX = {0: 8, 1: 10, 2: 12, 3: 16}             # hdmitx20 cd
_GCP = {"24bit": 8, "30bit": 10, "36bit": 12, "48bit": 16}
_EOTF_STATUS = {"sdr": "SDR", "hdr10-gamma_st2084": "PQ", "hdr10-gamma_hlg": "HLG",
                "hdr10-others": "HDR", "hdr10plus-vsif": "HDR10+", "dolbyvision-std": "DV",
                "dolbyvision-lowlatency": "DV-LL"}


def hdr_eotf(status):
    """hdmi_hdr_status in one vocabulary (SDR, PQ, HLG, HDR, HDR10+, DV, DV-LL), whatever the
    driver generation spells; an unknown Dolby Vision form is DV, anything else as it is."""
    if status is None:
        return None
    low = status.strip().lower()
    return _EOTF_STATUS.get(low) or ("DV" if low.startswith("dolbyvision") else status.strip())


_EOTF_CONFIG = {"sdr": "SDR", "hdr10": "PQ", "hlg": "HLG", "hdr": "HDR", "hdr10+": "HDR10+",
                "dv-std": "DV", "dv-ll": "DV-LL"}
_EOTF_DRM = {"sdr": "SDR", "hdr": "HDR", "st 2084": "PQ", "hlg": "HLG"}
_RANGE = {"default": "def", "limited": "lim", "full": "full", "reserved": "res"}


def _norm_colorimetry(text):
    t = (text or "").strip().lower().replace(" ", "").replace("_", "")
    if not t:
        return None
    if t in ("none", "disable", "default", "unknown", "extended"):
        return "none" if t != "extended" else None
    if t.startswith("bt.2020") or t.startswith("bt2020"):
        return "BT2020CL" if t.endswith("cl") or t.endswith("2020c") else "BT2020"
    return {"bt.601": "BT601", "bt.709": "BT709", "xvycc601": "xvYCC601",
            "xvycc709": "xvYCC709", "sycc601": "sYCC601", "adobeycc601": "opYCC601",
            "opycc601": "opYCC601", "adobergb": "opRGB", "oprgb": "opRGB",
            "p3d65": "P3D65", "p3dci": "P3DCI"}.get(t, text.strip())


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read(65536).decode("latin-1")
    except (OSError, ValueError):
        return None


def _pairs(text):
    """'key: value' pairs, first occurrence wins; lines may be joined with ' | '."""
    out: dict[str, str] = {}
    for line in re.split(r"\n| \| ", text or ""):
        key, sep, value = line.partition(":")
        if sep and key.strip():
            out.setdefault(key.strip(), value.strip())
    return out


def _int(text):
    m = re.match(r"\s*(-?\d+)", text or "")
    return int(m.group(1)) if m else None


def parse_config(text):
    p = _pairs(text)
    out: dict[str, Any] = {}
    m = re.match(r"(\d+)\s*(\S*)", p.get("VIC", ""))
    if m:
        out["vic"] = int(m.group(1))
        out["name"] = m.group(2) or None
    depth = _int(p.get("Colour depth"))
    if depth in (8, 10, 12, 16):
        out["depth"] = depth
    fmt = _FORMAT.get(p.get("Colourspace", "").lower())
    if fmt:
        out["format"] = fmt
    for key, name in (("Colour range", "range"), ("YCC colour range", "ycc_range")):
        if p.get(key, "").lower() in _RANGE:
            out[name] = p[key].lower()
    eotf = _EOTF_CONFIG.get(p.get("EOTF", "").lower())
    if eotf:
        out["eotf"] = eotf
    col = _norm_colorimetry(p.get("Colourimetry") or p.get("Colorimetry"))
    if col:
        out["colorimetry"] = col
    return out


def parse_disp_mode(text):
    p = _pairs(text)
    out: dict[str, Any] = {}
    vic = _int(p.get("vic", p.get("VIC")))
    if vic is not None:
        out["vic"] = vic
    m = re.match(r"(\d+)/(\d+)/(\d+)", p.get("cd/cs/cr", ""))
    if m:
        out["cd"], out["cs"] = int(m.group(1)), int(m.group(2))
    m = re.match(r"(\d+)/(\d+)", p.get("h/v_freq", ""))
    if m and int(m.group(2)) > 0:
        out["v_freq_mhz"] = int(m.group(2))
    for key in ("h_active", "v_active", "tmds_clk", "pixel_freq"):
        v = _int(p.get(key))
        if v is not None:
            out[key] = v
    if p.get("pi_mode", "")[:1] in ("P", "I"):
        out["scan"] = p["pi_mode"][:1].lower()
    if p.get("name"):
        out["name"] = p["name"]
    return out


def parse_pkt_dump(text):
    """AVI/GCP/DRM fields; an 'AVI PKT not enable' dump gives avi_enabled False."""
    p = _pairs(text)
    out: dict[str, Any] = {}
    depth = _GCP.get(p.get("GCP.color_depth", "").lower())
    if depth:
        out["gcp_depth"] = depth
    if re.search(r"^AVI PKT not enable", text or "", re.M):
        out["avi_enabled"] = False
    if "AVI.colorspace" in p:
        out["avi_enabled"] = True
        fmt = _FORMAT.get(p["AVI.colorspace"].lower())
        if fmt:
            out["format"] = fmt
    col = p.get("AVI.colorimetry", "")
    if col:
        c = _norm_colorimetry(col)
        if col.strip().lower() == "extended":
            c = _norm_colorimetry(p.get("AVI.extended_colorimetry")) or "extended"
        out["colorimetry"] = c
    q = p.get("AVI.quantization_range", "").lower()
    if q in _RANGE:
        out["quant"] = q
    yq = p.get("AVI.ycc_quantization_range", "").lower()
    if yq in ("limited", "full"):
        out["ycc_quant"] = yq
    vic = _int(p.get("AVI.video_code"))
    if vic is not None:
        out["avi_vic"] = vic
    if re.search(r"^DRM PKT not enable", text or "", re.M):
        out["drm_enabled"] = False
    eotf = p.get("DRM.eotf", "").lower()
    if eotf:
        out["drm_enabled"] = True
        out["drm_eotf"] = _EOTF_DRM.get(eotf, eotf)
    for key, name in (("DRM.max_lum", "drm_max_lum"), ("DRM.min_lum", "drm_min_lum"),
                      ("DRM.max_cll", "drm_max_cll"), ("DRM.max_fall", "drm_max_fall")):
        v = _int(p.get(key))
        if v is not None:
            out[name] = v
    return out


def parse_dc_cap(text):
    caps = []
    for line in (text or "").splitlines():
        m = re.match(r"\s*(rgb|444|422|420),(\d+)bit\s*$", line, re.I)
        if m:
            caps.append("%s,%sbit" % (m.group(1).lower(), m.group(2)))
    return caps or None


def parse_mode(text):
    """/sys/class/display/mode, e.g. 2160p24hz, 1080i50hz, 2160p59.94hz."""
    m = re.match(r"\s*(?:\d+x)?(\d+)([pi])([\d.]+)hz", text or "", re.I)
    if not m:
        return {}
    return {"lines": int(m.group(1)), "scan": m.group(2).lower(), "rate": float(m.group(3))}


def _rate_text(hz):
    return ("%.3f" % hz).rstrip("0").rstrip(".")


def _depth_from_cd(cd):
    """cd node: 4..7 on hdmitx21 (enum), 0..3 on hdmitx20 (index)."""
    if cd in _DEPTH_ENUM:
        return _DEPTH_ENUM[cd]
    return _DEPTH_INDEX.get(cd)


def details(hdmitx=None, display_mode=None):
    """Everything readable about the live link; missing nodes give None fields."""
    base = hdmitx or HDMITX
    raw = {n: read(base + n) for n in NODES}
    mode_text = read(display_mode or DISPLAY_MODE)
    cfg: dict[str, Any] = parse_config(raw["config"]) if raw["config"] else {}
    dm: dict[str, Any] = parse_disp_mode(raw["disp_mode"]) if raw["disp_mode"] else {}
    pkt: dict[str, Any] = parse_pkt_dump(raw["hdmitx_pkt_dump"]) if raw["hdmitx_pkt_dump"] else {}
    mode = parse_mode(mode_text)
    status = (raw["hdmi_hdr_status"] or "").strip() or None

    # resolution from the transmitter's format name, else the display mode, else the
    # timing; refresh from the timing (exact 1000/1001), else the names
    named = parse_mode(cfg.get("name")) or parse_mode(dm.get("name")) or mode
    lines = named.get("lines") or dm.get("v_active")
    scan = named.get("scan") or dm.get("scan")
    rate = dm["v_freq_mhz"] / 1000.0 if dm.get("v_freq_mhz") else None
    if rate is None:
        rate = named.get("rate")
        # hdmitx20 has no live timing: its frac policy applies to rates divisible by 6
        if rate and rate == int(rate) and int(rate) % 6 == 0 and \
                _int(raw["frac_rate_policy"]) == 1:
            rate = rate * 1000 / 1001.0

    cs = _int(raw["cs"])
    fmt = _CS_CODE.get(cs) if cs is not None else None
    fmt = fmt or pkt.get("format") or _CS_CODE.get(dm.get("cs")) or cfg.get("format")
    depth = _depth_from_cd(_int(raw["cd"]))
    depth = depth or _DEPTH_ENUM.get(dm.get("cd")) or pkt.get("gcp_depth") or cfg.get("depth")

    eotf = hdr_eotf(status) if status else None
    eotf = eotf or cfg.get("eotf") or pkt.get("drm_eotf")
    col = pkt.get("colorimetry") or cfg.get("colorimetry")
    vic = dm.get("vic") or cfg.get("vic") or pkt.get("avi_vic")

    quant, ycc_quant = pkt.get("quant"), pkt.get("ycc_quant")
    effective = None
    if fmt == "RGB" and quant:
        # CTA-861: default = limited for CE formats (VIC > 1), full for IT (VGA, VIC 1)
        effective = quant if quant in ("limited", "full") else \
            ("full" if vic in (0, 1) else "limited") if quant == "default" else None
    elif fmt and fmt != "RGB" and ycc_quant:
        effective = ycc_quant
    return {
        "display_mode": (mode_text or "").strip() or None,
        "vic": vic,
        "lines": lines,
        "scan": scan,
        "refresh_hz": rate,
        "colour_format": fmt,
        "colour_depth": depth,
        "colorimetry": col,
        "eotf": eotf,
        "hdr_status": status,
        "avi_quantization_range": quant,
        "avi_ycc_quantization_range": ycc_quant,
        "effective_range": effective,
        "config_colour_range": cfg.get("range"),
        "drm_infoframe": pkt.get("drm_enabled"),
        "drm_max_lum": pkt.get("drm_max_lum"),
        "drm_min_lum": pkt.get("drm_min_lum"),
        "drm_max_cll": pkt.get("drm_max_cll"),
        "drm_max_fall": pkt.get("drm_max_fall"),
        "tmds_clk_khz": dm.get("tmds_clk"),
        "tv_dc_cap": parse_dc_cap(raw["dc_cap"]) if raw["dc_cap"] else None,
    }


def summary(hdmitx=None, display_mode=None, info=None):
    """'2160p24 YCC444 10bit BT2020 PQ lim/lim' (AVI Q / YQ ranges); None when
    no node is readable (not an Amlogic transmitter, or HDMI unplugged)."""
    d = info if info is not None else details(hdmitx, display_mode)
    parts = []
    if d["lines"]:
        parts.append("%d%s%s" % (d["lines"], d["scan"] or "p",
                                 _rate_text(d["refresh_hz"]) if d["refresh_hz"] else ""))
    if d["colour_format"]:
        parts.append(d["colour_format"])
    if d["colour_depth"]:
        parts.append("%dbit" % d["colour_depth"])
    if d["colorimetry"]:
        parts.append("nocolorimetry" if d["colorimetry"] == "none" else d["colorimetry"])
    if d["eotf"]:
        parts.append(d["eotf"])
    if d["avi_quantization_range"] or d["avi_ycc_quantization_range"]:
        parts.append("%s/%s" % (_RANGE.get(d["avi_quantization_range"], "?"),
                                _RANGE.get(d["avi_ycc_quantization_range"], "?")))
    return " ".join(parts) or None


_LABELS = (
    ("display_mode", "Display mode"), ("vic", "VIC"), ("refresh_hz", "Refresh (Hz)"),
    ("colour_format", "Colour format (AVI)"), ("colour_depth", "Colour depth (bit)"),
    ("colorimetry", "Colorimetry (AVI)"), ("eotf", "EOTF"), ("hdr_status", "HDR status"),
    ("avi_quantization_range", "AVI RGB quantization (Q)"),
    ("avi_ycc_quantization_range", "AVI YCC quantization (YQ)"),
    ("effective_range", "Range the TV applies"),
    ("config_colour_range", "Driver 'config' range (guessed on hdmitx21)"),
    ("drm_infoframe", "HDR (DRM) infoframe sent"), ("drm_max_lum", "DRM max luminance"),
    ("drm_min_lum", "DRM min luminance (0.0001 nit)"), ("drm_max_cll", "DRM MaxCLL"),
    ("drm_max_fall", "DRM MaxFALL"), ("tmds_clk_khz", "TMDS clock (kHz)"),
    ("tv_dc_cap", "TV deep colour (dc_cap)"),
)


def format_details(info=None):
    """details() as text lines for a dialog."""
    d = info if info is not None else details()
    out = ["Summary: %s" % (summary(info=d) or "not available")]
    for key, label in _LABELS:
        v = d.get(key)
        if v is None:
            v = "-"
        elif isinstance(v, bool):
            v = "yes" if v else "no"
        elif isinstance(v, list):
            v = " ".join(v)
        elif isinstance(v, float):
            v = _rate_text(v)
        out.append("%s: %s" % (label, v))
    return "\n".join(out)
