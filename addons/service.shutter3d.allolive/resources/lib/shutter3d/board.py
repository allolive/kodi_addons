"""The emitter on USB, and the thread that keeps it in step with the video
layer's eye. No Kodi imports.

Two kinds speak the same line protocol: a Raspberry Pi Zero W with the
pi-bt3dsp firmware, which broadcasts to Bluetooth 3D glasses (the add-on boots
it over USB, rpiboot.py), and a USB board with the promicro-vesa-stereo
firmware, which drives a 3D emitter's VESA stereo sync input.

Kodi itself (the yacer build) runs the display side of frame-sequential 3D;
the kernel reports an eye on every vsync on /sys/class/video/fa_sync, with the
vsync's time stamped in the interrupt. This forwards each one; the board maps
the box clock onto its timer and switches the sync pin in hardware, so this
thread's own scheduling jitter does not reach the glasses.
"""

import glob
import os
import select
import termios
import threading
import time

from . import core

FA_SYNC = "/sys/class/video/fa_sync"
PROBE_S = 30.0                   # no board: ask the serial ports again this often
PORTS_S = 2.0                    # no board: look for a port coming or going this often
# The emitter's power, through the optional MOSFET on the board (its P command;
# a board without one, or older firmware, ignores it): on for a 3D film or the
# test, off this long after both have ended, so the next episode starts with
# the glasses already synced.
POWER_OFF_S = 60.0

# the board's M command
MODE_OFF = 0
MODE_FREE = 1
MODE_SYNC = 2

# the first word of the status line ("?"); older board firmware began "mode"
PI_PREFIX = b"pi-bt3dsp "
STATUS_PREFIXES = (PI_PREFIX, b"promicro-vesa-stereo ", b"mode ")


def open_serial(path, nonblock=True, flush=True):
    """A raw 115200 8N1 file descriptor on a USB CDC serial port."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | (os.O_NONBLOCK if nonblock else 0))
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0                                        # iflag
    attrs[1] = 0                                        # oflag
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0                                        # lflag: raw
    attrs[4] = attrs[5] = termios.B115200
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1                         # reads return within 0.1 s
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    if flush:
        termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def status_line(reply):
    """Our firmware's status line in what a port answered to "?", or None."""
    for line in reply.splitlines():
        if line.startswith(STATUS_PREFIXES):
            return line.decode("ascii", "replace")
    return None


def pi_build(line):
    """The build a Pi's status line names ("pi-bt3dsp 0.1.0 build a74f863 ..."),
    "" for a Pi too old to say, None for a board."""
    words = line.split()
    if not words or words[0] != PI_PREFIX.decode().strip():
        return None
    return words[3] if len(words) > 3 and words[2] == "build" else ""


def probe(path, timeout=1.0):
    """The status line our firmware answers "?" with; other serial devices do not."""
    try:
        fd = open_serial(path, flush=False)     # it may be in use: do not drop its data
    except OSError:
        return None
    try:
        os.write(fd, b"?\n")
        buf = b""
        while select.select([fd], [], [], timeout)[0]:
            chunk = os.read(fd, 256)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        return status_line(buf)
    finally:
        os.close(fd)


def ports():
    """The USB CDC serial ports, the kind the board's ATmega32U4 makes (ttyACM).
    Serial adapters (ttyUSB: FTDI, CH340, Zigbee sticks) are never asked."""
    return sorted(glob.glob("/dev/ttyACM*"))


def find():
    """(serial port, status line) of the first port that answers as our
    firmware, or (None, None)."""
    for port in ports():
        line = probe(port)
        if line:
            return port, line
    return None, None


class Board:
    """The open serial port. Whole lines or nothing."""

    def __init__(self, path, log, status=""):
        self.log = log
        self.path = path
        self.fd = None
        self.lock = threading.Lock()
        self.build = pi_build(status or "")      # a Pi's build; None: a board
        self.kind = "board" if self.build is None else "pi"

    def open(self):
        self.fd = open_serial(self.path)
        self.log("%s on %s" % ("Pi" if self.kind == "pi" else "board", self.path))

    def is_open(self):
        return self.fd is not None

    def send(self, text):
        data = text.encode("ascii")
        with self.lock:
            if self.fd is None:
                return False
            try:
                n = os.write(self.fd, data)
                if n < len(data):
                    os.write(self.fd, b"\n")            # end the torn line, drop the rest
                    return False
                return True
            except BlockingIOError:
                return False                            # board busy: drop this one
            except OSError as e:
                self.log("board write: %s" % e)
                self._close()
                return False

    def configure(self, delay_us, mode=None):
        """The delay applies on the next edge; a mode change restarts the
        board's schedule and clock. Kodi swaps the eyes itself (Swap left/right
        in the video settings), so the board's own swap stays off, even when a
        saved one comes back from its EEPROM."""
        lines = "D %d\nI 0\n" % delay_us
        if mode is not None:
            lines += "M %d\n" % mode
        self.send(lines)

    def free_run(self, hz):
        self.send("F %d\n" % int(round(hz * 1000)))

    def power(self, on):
        self.send("P %d\n" % (1 if on else 0))

    def opening(self, open_after_us):
        """The lens window (the Pi; a board ignores it): each lens opens
        open_after_us into its eye's frame, until the other eye's frame."""
        self.send("O %d 0\n" % open_after_us)

    def _close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def close(self):
        with self.lock:
            self._close()


class BoardLink(threading.Thread):
    """Runs for the life of the service: finds the board, forwards every vsync
    fa_sync reports, and runs its free-running test. Each 3D session starts the
    board in sync mode, so a test or a stale clock never carries over.

    The eye switch delay comes with set_delay, the lens window with
    set_opening (opening: a callable giving open_after_us, asked
    each time it is sent, since it may follow the display mode).
    on_3d: called from this thread when the box starts reporting eyes, before
    the first one goes out; it must not block.
    expected_build: the build of the Pi image the add-on holds; a Pi running
    another is sent back to its boot ROM, where the add-on boots it again.
    """

    def __init__(self, log, on_3d=lambda: None, opening=lambda: 0, expected_build=None):
        super().__init__(name="shutter3d-board", daemon=True)
        self.log = log
        self.stopping = threading.Event()
        self.changed = threading.Event()
        self.board = None
        self.test_hz = 0.0
        self.applied_test = 0.0          # what the board was last told
        self.in_3d = False
        self.delay_us = 0
        self.on_3d = on_3d
        self.configuring = threading.Lock()  # the delay read and sent as one, against set_delay
        self.clock = time.monotonic
        self.power = None                # what the board was last told; None: not yet
        self.power_off_at = None         # when to switch the emitter off; None: never
        self.opening = opening
        self.expected_build = expected_build

    # -- from the Kodi thread

    def set_delay(self, delay_us):
        """Applies on the board's next edge."""
        with self.configuring:
            self.delay_us = delay_us
            board = self.board
            if board and board.is_open():
                board.configure(delay_us)

    def set_opening(self):
        """The lens window changed (a setting, or the display mode): send it."""
        board = self.board
        if board and board.is_open():
            board.opening(self.opening())

    def test(self, hz):
        self.test_hz = hz
        self.changed.set()

    def stop(self):
        self.stopping.set()
        self.changed.set()
        self.join(timeout=2.0)

    def state(self):
        board = self.board
        if board is None or not board.is_open():
            path = kind = build = ""
        else:
            path, kind, build = board.path, board.kind, board.build or ""
        return {"board": path, "kind": kind, "build": build,
                "in_3d": self.in_3d, "test_hz": self.test_hz,
                "delay_us": self.delay_us}

    # -- the loop

    def _drop(self):
        board, self.board = self.board, None
        if board:
            board.send("M %d\n" % MODE_SYNC)
            board.power(True)            # the board's own default, without us
            board.close()

    def _power(self, board, on):
        if self.power != on:
            board.power(on)
            self.power = on
            self.log("emitter %s" % ("on" if on else "off"))

    def _power_on(self, board):
        self._power(board, True)
        self.power_off_at = None

    def _power_off_later(self):
        if self.power_off_at is None:
            self.power_off_at = self.clock() + POWER_OFF_S

    def _follow_power(self, board):
        """Off once neither a film nor the test has run for POWER_OFF_S."""
        if self.in_3d or self.test_hz:
            return
        if self.power_off_at is not None and self.clock() >= self.power_off_at:
            self._power(board, False)
            self.power_off_at = None

    def _wait_for_ports(self):
        """No board: until a serial port comes or goes, a setting or the test
        changes, or PROBE_S passes."""
        seen = ports()
        for _ in range(int(PROBE_S / PORTS_S)):
            if self.changed.wait(PORTS_S) or ports() != seen:
                break
        self.changed.clear()

    def _connect(self):
        path, line = find()
        if not path:
            return False
        board = Board(path, self.log, line)
        try:
            board.open()
        except OSError as e:
            self.log("board %s: %s" % (path, e))
            return False
        if board.kind == "pi" and self.expected_build and board.build != self.expected_build:
            # back to its boot ROM: the booter gives it the image the add-on holds
            self.log("Pi runs build %s, the add-on has %s: rebooting it"
                     % (board.build or "unknown", self.expected_build))
            board.send("B\n")
            board.close()
            return False
        self.board = board
        board.opening(self.opening())
        self.applied_test = -1.0         # tell the board its mode afresh
        self.power = None                # and its power: off, unless 3D or the test wants it
        self.power_off_at = self.clock()
        return True

    def _apply_test(self, board):
        """Free-run at the test rate, or back to following the box."""
        if self.test_hz:
            self._power_on(board)
        else:
            self._power_off_later()
        with self.configuring:
            if self.test_hz:
                board.free_run(self.test_hz)
                board.configure(self.delay_us, MODE_FREE)
            else:
                board.configure(self.delay_us, MODE_SYNC)
        self.applied_test = self.test_hz

    def _enter_3d(self, board):
        if self.test_hz:
            self.log("board test off: a 3D film started")
            self.test_hz = 0.0
        self._power_on(board)
        with self.configuring:
            board.configure(self.delay_us, MODE_SYNC)
        board.opening(self.opening())    # 3D has just set its HDMI mode
        self.applied_test = 0.0
        self.in_3d = True
        try:
            self.on_3d()
        except Exception as e:           # never let it stop the glasses
            self.log("on 3D start: %s" % e)

    def run(self):
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(10))
        except (AttributeError, OSError):
            pass
        try:
            fd = os.open(FA_SYNC, os.O_RDONLY)
        except OSError as e:
            self.log("no %s (kernel without the frame-sequential patches?): %s" % (FA_SYNC, e))
            return
        try:
            while not self.stopping.is_set():
                board = self.board
                if board is None or not board.is_open():
                    self.in_3d = False
                    if not self._connect():
                        self._wait_for_ports()
                        continue
                    board = self.board
                    if board is None:
                        continue
                try:
                    # blocks until the next 3D vsync, or 100 ms and "off"
                    sample = core.parse_fa_sync(os.pread(fd, 64, 0).decode("ascii", "replace"))
                except InterruptedError:
                    continue
                if sample is None:
                    if self.in_3d:
                        self._power_off_later()
                    self.in_3d = False
                    if not os.path.exists(board.path):
                        # unplugged while idle: nothing is written to notice it
                        self.log("board gone from %s" % board.path)
                        self.board = None
                        board.close()
                        continue
                    if self.applied_test != self.test_hz:
                        self._apply_test(board)
                    self._follow_power(board)
                    continue
                if not self.in_3d:
                    self._enter_3d(board)
                board.send(core.sync_line(*sample))
        finally:
            os.close(fd)
            self._drop()
