"""The Pi emitter: booting it over USB (against a simulated Pi), knowing its
build, and the lens window."""
import ctypes
import os
import struct

from shutter3d import board, core, pi, rpiboot


class FakeUsb:
    """The Pi's side of rpiboot: each read() of the host gets the next request
    the Pi makes; what the host sends is recorded."""

    def __init__(self, requests=()):
        self.requests = list(requests)
        self.controls = []                       # (request type, value, index, length)
        self.bulks = []                          # each bulk transfer's data

    def control(self, request_type, value, index, length=0, timeout_ms=1000):
        self.controls.append((request_type, value, index, length))
        if request_type == 0xC0:                 # the host reads
            if length == 4:
                return struct.pack("<i", 0)
            if not self.requests:
                raise OSError("gone: it left USB to boot")
            return self.requests.pop(0)
        return b""

    def bulk_out(self, data, timeout_ms=5000):
        self.bulks.append(bytes(data))
        return len(data)

    def lengths(self):
        """The lengths the host announced before each write."""
        return [v | i << 16 for t, v, i, _ in self.controls if t == 0x40]


def request(command, name):
    return struct.pack("<i", command) + name.encode().ljust(256, b"\0")


def boot_dir(tmp_path):
    d = tmp_path / "boot"
    (d / "overlays").mkdir(parents=True)
    (d / "config.txt").write_bytes(b"kernel=zImage\n")
    (d / "overlays" / "dwc2.dtbo").write_bytes(b"\x01" * 40000)
    (d / "bootcode.bin").write_bytes(b"\x02" * 100)
    (tmp_path / "secret").write_bytes(b"not for the Pi")
    return str(d)


def test_the_file_server_answers_sizes_and_files(tmp_path):
    usb = FakeUsb([request(0, "config.txt"), request(1, "config.txt"),
                   request(0, "overlays/dwc2.dtbo"), request(1, "overlays/dwc2.dtbo"),
                   request(2, "")])
    served = rpiboot.serve_files(usb, boot_dir(tmp_path))
    assert served == ["config.txt", "overlays/dwc2.dtbo"]
    # size of config.txt, its data, size of dwc2.dtbo, its data in 16 KB pieces,
    # and the empty answer that ends it (an empty name, as rpiboot does)
    assert usb.lengths() == [14, 14, 40000, 40000, 0]
    assert usb.bulks[0] == b"kernel=zImage\n"
    assert [len(b) for b in usb.bulks[1:]] == [16384, 16384, 7232]


def test_missing_files_and_paths_outside_get_nothing(tmp_path):
    usb = FakeUsb([request(0, "recovery.elf"), request(1, "recovery.elf"),
                   request(0, "../secret"), request(1, "../secret"),
                   request(0, "/etc/passwd"), request(2, "")])
    assert rpiboot.serve_files(usb, boot_dir(tmp_path)) == []
    assert usb.lengths() == [0, 0, 0, 0, 0, 0] and usb.bulks == []


def test_an_empty_name_ends_it_and_metadata_is_acknowledged(tmp_path):
    usb = FakeUsb([request(0, "*serial"), request(0, ""), request(0, "config.txt")])
    assert rpiboot.serve_files(usb, boot_dir(tmp_path)) == []
    assert usb.lengths() == [0, 0] and len(usb.requests) == 1


def test_the_boot_rom_gets_the_header_then_bootcode(tmp_path, monkeypatch):
    monkeypatch.setattr(rpiboot.time, "sleep", lambda s: None)
    usb = FakeUsb()
    assert rpiboot.send_bootcode(usb, b"\x02" * 100) == 0
    assert usb.lengths() == [24, 100]
    assert usb.bulks[0] == struct.pack("<i", 100) + b"\0" * 20
    assert usb.controls[-1] == (0xC0, 4, 0, 4)          # then its 4-byte result


def sysfs_device(root, name, product, serial_index, interfaces=1, busnum=3, devnum=7):
    d = root / name
    d.mkdir(parents=True)
    (d / "idVendor").write_text("0a5c\n")
    (d / "idProduct").write_text("%04x\n" % product)
    (d / "busnum").write_text("%d\n" % busnum)
    (d / "devnum").write_text("%d\n" % devnum)
    (d / "bNumInterfaces").write_text(" %d\n" % interfaces)
    (d / "descriptors").write_bytes(bytes(16) + bytes([serial_index, 1]))


def test_finds_a_pi_waiting_on_usb(tmp_path):
    sysfs_device(tmp_path, "3-2", 0x2763, 0)
    other = tmp_path / "3-1"
    other.mkdir()
    (other / "idVendor").write_text("8087\n")
    (other / "idProduct").write_text("0037\n")
    (tmp_path / "usb3").mkdir()                          # a hub without those files
    found = rpiboot.find(str(tmp_path))
    assert len(found) == 1
    dev = found[0]
    assert dev.node == "/dev/bus/usb/003/007" and dev.serial_index == 0 and dev.interfaces == 1


def test_usbfs_request_numbers_are_linux_ones():
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert rpiboot.USBDEVFS_CONTROL == 0xC0185500
        assert rpiboot.USBDEVFS_BULK == 0xC0185502
    else:
        assert rpiboot.USBDEVFS_CONTROL == 0xC0105500
        assert rpiboot.USBDEVFS_BULK == 0xC0105502
    assert rpiboot.USBDEVFS_CLAIMINTERFACE == 0x8004550F


def test_the_bundled_build(tmp_path):
    assert pi.bundled_build(str(tmp_path)) is None
    (tmp_path / "build.txt").write_text("a74f863\n")
    assert pi.bundled_build(str(tmp_path)) == "a74f863"


def test_a_pi_knows_its_build():
    assert board.pi_build("pi-bt3dsp 0.1.0 build a74f863 mode 2 delay_us 0") == "a74f863"
    assert board.pi_build("pi-bt3dsp 0.1.0 mode 2") == ""            # too old to say
    assert board.pi_build("promicro-vesa-stereo 0.2.0 mode 2") is None
    assert board.Board("/dev/ttyACM0", None, "pi-bt3dsp 0.1.0 build x").kind == "pi"
    assert board.Board("/dev/ttyACM0", None, "promicro-vesa-stereo 0.2.0").kind == "board"


class Port:
    """The serial port a Board opens: what is written to it."""
    written = []


def connect(monkeypatch, status, expected):
    Port.written = []
    monkeypatch.setattr(board, "find", lambda: ("/dev/ttyACM0", status))
    monkeypatch.setattr(board, "open_serial", lambda path: 5)
    monkeypatch.setattr(board.os, "write", lambda fd, data: Port.written.append(data) or len(data))
    monkeypatch.setattr(board.os, "close", lambda fd: None)
    link = board.BoardLink(lambda m: None, opening=lambda: 8000, expected_build=expected)
    return link, link._connect()


def test_a_pi_on_another_build_is_sent_back_to_be_booted(monkeypatch):
    link, connected = connect(monkeypatch, "pi-bt3dsp 0.1.0 build old1234 mode 2", "new5678")
    assert not connected and link.board is None
    assert Port.written == [b"B\n"]


def test_a_pi_on_the_bundled_build_gets_its_lens_window(monkeypatch):
    link, connected = connect(monkeypatch, "pi-bt3dsp 0.1.0 build new5678 mode 2", "new5678")
    assert connected and link.board.kind == "pi"
    assert Port.written == [b"O 8000 0\n"]
    assert link.state()["kind"] == "pi" and link.state()["build"] == "new5678"


def test_without_a_bundled_image_any_pi_is_used(monkeypatch):
    link, connected = connect(monkeypatch, "pi-bt3dsp 0.1.0 build old1234 mode 2", None)
    assert connected


# /sys/class/display/vinfo as the kernel writes it (trimmed)
VINFO = """current vinfo:
    name:                  %s
    width:                 1920
    height:                %d
    htotal:                %d
    vtotal:                %d
    video_clk:             %d
    cs:                    2
"""

VINFO_165 = VINFO % ("1920x1080p165hzV", 1080, 2080, 1169, 401000)   # an LG 2025 OLED's DisplayID mode
VINFO_144 = VINFO % ("1920x1080p144hzV", 1080, 2080, 1157, 346500)   # a 144 Hz sink's, as one lists it
VINFO_120 = VINFO % ("1080p120hz", 1080, 2200, 1125, 297000)
VINFO_GUI = VINFO % ("2160p60hz", 2160, 4400, 2250, 594000)


def test_the_picture_time_of_a_mode():
    # from the timing the kernel sends, rounded up to 100 us
    assert core.picture_us(VINFO_120) == 8000          # 1080 lines of 2200 pixels at 297 MHz: 8000.0
    assert core.picture_us(VINFO_165) == 5700          # 5602.0
    assert core.picture_us(VINFO_144) == 6500          # 6483.1
    # without vinfo, from a CEA mode's name
    assert core.picture_us(mode="1080p120hz") == 8000
    assert core.picture_us(mode="1080p60hz") == 16000
    assert core.picture_us(mode="720p50hz") == 19200
    assert core.picture_us(mode="1920x1080p120hz") == 8000     # the name HFR VRR's mode has
    assert core.picture_us(mode="1920x1080p165hzV") is None    # a DisplayID mode: its timing is the sink's
    assert core.picture_us(mode="smpte24hz") is None
    assert core.picture_us("no timing here", None) is None
    assert core.picture_us(None, None) is None


def test_a_3d_base_is_a_fast_mode():
    assert core.is_3d_base(VINFO_120)
    assert core.is_3d_base(VINFO_144)
    assert core.is_3d_base(VINFO_165)
    assert not core.is_3d_base(VINFO_GUI)
    assert not core.is_3d_base(None)


def test_the_lens_window():
    # closed for the redraw time, then open until the next frame
    assert core.opening(True, False, 0, VINFO_120) == 8000          # automatic
    assert core.opening(True, False, 0, VINFO_165) == 5700          # the 165 Hz base
    assert core.opening(True, False, 0, None, "1080p120hz") == 8000
    assert core.opening(True, False, 0) == 8000                     # 3D's own base if unknown
    assert core.opening(True, True, 8200, VINFO_165) == 8200        # manual
    assert core.opening(True, True, -5, VINFO_165) == 0
    assert core.opening(False, True, 8200, VINFO_165) == 0          # open the whole frame


def test_the_booter_waits_without_firmware(tmp_path, monkeypatch):
    logs = []
    b = pi.Booter(str(tmp_path), logs.append)
    monkeypatch.setattr(pi.rpiboot, "find", lambda: [object()])
    calls = iter([False, False, True])
    monkeypatch.setattr(b.stopping, "wait", lambda s: next(calls))
    b.run()
    assert logs == ["a Pi waits on USB, but this add-on was built without its firmware"]
    assert os.listdir(str(tmp_path)) == []


def test_a_value_snaps_to_its_setting():
    redraw = (core.REDRAW_STEP_US, core.REDRAW_MIN_US, core.REDRAW_MAX_US)
    assert core.snap(8100, *redraw) == 8100
    assert core.snap(-300, *redraw) == 0                         # never below 0
    assert core.snap(20300, *redraw) == 20000                    # the setting's maximum
    assert core.snap(8140, *redraw) == 8100                      # back on its 100 us steps


def test_the_film_rates():
    assert [core.rate_name(r) for r in core.FILM_RATES] == ["23.976", "24", "25", "29.97", "30"]
    assert [core.display_mhz(r) for r in core.FILM_RATES] == [95904, 96000, 100000, 119880, 120000]
