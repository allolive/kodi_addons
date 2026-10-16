"""Frame-sequential 3D, free of Kodi and of the box. Kodi shows the eyes
alternately on every vsync; the kernel reports each one on
/sys/class/video/fa_sync as "<vsync> <ns> <L|R|->"."""

import re


def parse_fa_sync(text):
    """/sys/class/video/fa_sync: (vsync, ns, eye) or None for "off"."""
    parts = text.split()
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), parts[2]
    except ValueError:
        return None


def sync_line(vsync, ns, eye):
    """The board's sync message for one vsync: its number and time in
    microseconds, both mod 2^32."""
    return "S %d %d %s\n" % (vsync & 0xFFFFFFFF, (ns // 1000) & 0xFFFFFFFF, eye)


# ---- the lens window ---------------------------------------------------------

# total lines (active and blanking) of the CEA modes, by active lines
TOTAL_LINES = {480: 525, 576: 625, 720: 750, 1080: 1125, 2160: 2250}

# the modes a sink lists in its DisplayID ("...hzV"), by active lines and rate:
# their blanking is not the CEA one (an LG 2025 OLED's 1080p165 has 1169 lines)
TOTAL_LINES_V = {(1080, 165): 1169}


def picture_us(mode):
    """How long the box takes to send one picture in this HDMI mode
    ("1080p120hz", "1920x1080p165hzV"): the active lines of a frame at the
    mode's own rate. VRR slows the refresh by lengthening the blanking after the
    picture, so this does not change with the film rate. None for a mode it
    cannot read."""
    m = re.search(r"(\d+)[pi](\d+)hz(V?)", mode or "")
    if not m:
        return None
    lines, hz = int(m.group(1)), int(m.group(2))
    total = TOTAL_LINES_V.get((lines, hz)) if m.group(3) else TOTAL_LINES.get(lines)
    if not total or hz <= 0:
        return None
    return int(round(1e6 / hz * lines / total))


PICTURE_US_1080P120 = picture_us("1080p120hz")   # 3D always plays on a 1080p 120 Hz base


def opening(closed, manual, redraw_us, display_mode):
    """How long into each eye's frame its lens opens, in us (it stays open
    until the other eye's frame starts). Closed during the redraw: both lenses stay
    closed for the redraw time at the start of each eye's frame, then that
    eye's lens opens until the next; the redraw time is the time the box takes
    to send a picture in this HDMI mode, or the manual value. Open: the whole
    frame (brightest, some crosstalk). The 3D glasses delay moves the window
    in time, not this."""
    if not closed:
        return 0
    if manual:
        return max(0, redraw_us)
    return picture_us(display_mode) or PICTURE_US_1080P120


def snap(us, step, lo, hi):
    """us on a setting's steps, inside its range."""
    return max(lo, min(hi, int(round(us / float(step))) * step))


# the lens_redraw_us setting's range and step (settings.xml)
REDRAW_MIN_US, REDRAW_MAX_US, REDRAW_STEP_US = 0, 20000, 100


# ---- the delay per film rate, set from the settings ----------------------------

DELAY_MIN_US, DELAY_MAX_US, DELAY_STEP_US = -10500, 10500, 100   # Kodi's 3D glasses delay
FILM_RATES = (23.976, 24.0, 25.0, 29.97, 30.0)     # 3D film rates, a test pattern each
OTHER_RATES = (50.0, 59.94, 60.0)


def rate_name(fps):
    """A film rate as the delays are kept under ("23.976", "24")."""
    return "%g" % fps


def display_mhz(fps):
    """The refresh frame-sequential 3D shows a film rate at: each eye twice."""
    return int(round(fps * 4000))


def delay_message(rate, us):
    """What the screen tells the service: the delay for a film rate
    ("23.976" at -400 us -> "delay_23p976_-400")."""
    return "delay_%s_%d" % (rate.replace(".", "p"), us)


def parse_delay_message(method):
    """(rate, us) from delay_message(), or None."""
    parts = method.split("_")
    if len(parts) != 3 or parts[0] != "delay":
        return None
    try:
        return parts[1].replace("p", "."), int(parts[2])
    except ValueError:
        return None


def clamp_delay(us):
    """Kodi's range and 100 us steps."""
    return snap(us, DELAY_STEP_US, DELAY_MIN_US, DELAY_MAX_US)

