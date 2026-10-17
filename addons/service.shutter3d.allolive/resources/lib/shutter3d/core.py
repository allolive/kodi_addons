"""Frame-sequential 3D, free of Kodi and of the box. Kodi shows the eyes
alternately on every vsync; the kernel reports each one on
/sys/class/video/fa_sync as "<vsync> <ns> <L|R|->"."""


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

PICTURE_STEP_US = 100           # the send time is rounded up to this
BASE_MIN_HZ = 96                # a mode this fast is a 3D base (120, 144, 165), not the GUI's
PICTURE_US_1080P120 = 8000      # 1080 of 1125 lines at 120 Hz: the base without a faster one


def parse_vinfo(text):
    """/sys/class/display/vinfo: (active lines, line length in pixels, lines
    per frame, pixel clock in kHz) of the mode being sent, or None."""
    fields = {}
    for line in (text or "").splitlines():
        key, _, value = line.partition(":")
        fields.setdefault(key.strip(), value.strip())
    try:
        timing = tuple(int(fields[k]) for k in ("height", "htotal", "vtotal", "video_clk"))
    except (KeyError, ValueError):
        return None
    return timing if all(v > 0 for v in timing) else None


def is_3d_base(timing):
    """The mode (parse_vinfo's timing) is fast enough to carry frame-sequential 3D."""
    if not timing:
        return False
    _, htotal, vtotal, clock_khz = timing
    return clock_khz * 1000 >= BASE_MIN_HZ * htotal * vtotal


def picture_us(timing):
    """How long the box takes to send one picture of the mode (parse_vinfo's
    timing), in us rounded up to 100: its active lines at its pixel clock, so
    any mode a sink lists is read as it is. VRR slows the refresh by lengthening
    the blanking after the picture, so this does not change with the film
    rate. None without a timing."""
    if not timing:
        return None
    lines, htotal, _, clock_khz = timing
    step = clock_khz * PICTURE_STEP_US
    return -(-lines * htotal * 1000 // step) * PICTURE_STEP_US


def opening(closed, adjust_us, timing=None):
    """How long into each eye's frame its lens opens, in us (it stays open
    until the other eye's frame starts). Closed during the redraw: both lenses stay
    closed for the redraw time at the start of each eye's frame, then that
    eye's lens opens until the next; the redraw time is the time the box takes
    to send a picture in this HDMI mode, plus the adjustment. Open: the whole
    frame (brightest, some crosstalk). The 3D glasses delay moves the window
    in time, not this."""
    if not closed:
        return 0
    return max(0, (picture_us(timing) or PICTURE_US_1080P120) + adjust_us)


def snap(us, step, lo, hi):
    """us on a setting's steps, inside its range."""
    return max(lo, min(hi, int(round(us / float(step))) * step))


# the lens_redraw_adjust_us setting's range and step (settings.xml)
ADJUST_MIN_US, ADJUST_MAX_US, ADJUST_STEP_US = -3000, 8000, 100


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

