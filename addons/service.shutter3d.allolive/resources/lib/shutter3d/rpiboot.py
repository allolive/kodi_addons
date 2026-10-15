"""Boots a Raspberry Pi over USB from a directory of boot files: the rpiboot
protocol (github.com/raspberrypi/usbboot, main.c), in Python, through Linux's
usbfs (/dev/bus/usb). No libusb, nothing compiled. No Kodi imports.

A Pi with no SD card waits in its boot ROM as a USB device 0a5c:2763. It gets
bootcode.bin, comes back as a second stage that asks for the rest of the files
by name (config.txt, start.elf, the kernel, ...), and then leaves USB to boot
them.

Every transfer is a vendor control request carrying a length, then the data:
a write sends the length (wValue: low 16 bits, wIndex: high 16 bits) and then
bulk-writes the data in 16 KB pieces; a read is a control-IN request of that
length.
"""

import ctypes
import fcntl
import os
import struct
import time

VENDOR = 0x0A5C
PRODUCTS = (0x2763, 0x2764, 0x2711, 0x2712)  # BCM2835/6/7 boot ROM and second stage, BCM2711/2712
SYSFS_USB = "/sys/bus/usb/devices"
CHUNK = 16 * 1024
FILE_REQUEST = 4 + 256                        # int command, char name[256]
GET_SIZE, READ_FILE, DONE = 0, 1, 2


class _CtrlTransfer(ctypes.Structure):        # struct usbdevfs_ctrltransfer
    _fields_ = [("bRequestType", ctypes.c_uint8), ("bRequest", ctypes.c_uint8),
                ("wValue", ctypes.c_uint16), ("wIndex", ctypes.c_uint16),
                ("wLength", ctypes.c_uint16), ("timeout", ctypes.c_uint32),
                ("data", ctypes.c_void_p)]


class _BulkTransfer(ctypes.Structure):        # struct usbdevfs_bulktransfer
    _fields_ = [("ep", ctypes.c_uint), ("len", ctypes.c_uint),
                ("timeout", ctypes.c_uint), ("data", ctypes.c_void_p)]


def _ioc(direction, nr, size):
    return direction << 30 | size << 16 | ord("U") << 8 | nr


USBDEVFS_CONTROL = _ioc(3, 0, ctypes.sizeof(_CtrlTransfer))
USBDEVFS_BULK = _ioc(3, 2, ctypes.sizeof(_BulkTransfer))
USBDEVFS_CLAIMINTERFACE = _ioc(2, 15, ctypes.sizeof(ctypes.c_uint))
USBDEVFS_RELEASEINTERFACE = _ioc(2, 16, ctypes.sizeof(ctypes.c_uint))


class BootDevice:
    """A Pi in its boot ROM (or second stage), as sysfs shows it."""

    def __init__(self, sysdir):
        self.sysdir = sysdir

    def _attr(self, name):
        return _attr(self.sysdir, name)

    @property
    def node(self):
        return "/dev/bus/usb/%03d/%03d" % (int(self._attr("busnum")), int(self._attr("devnum")))

    @property
    def serial_index(self):
        """iSerialNumber: 0 or 3 is a boot ROM (send bootcode.bin), anything
        else the second stage (serve files), as rpiboot decides."""
        with open(os.path.join(self.sysdir, "descriptors"), "rb") as f:
            return f.read(18)[16]

    @property
    def interfaces(self):
        return int(self._attr("bNumInterfaces"))

    def __repr__(self):
        return "BootDevice(%s)" % os.path.basename(self.sysdir)


def find(sysfs=SYSFS_USB):
    """Every Pi waiting to be booted (normally none or one)."""
    found: list[BootDevice] = []
    try:
        names = sorted(os.listdir(sysfs))
    except OSError:
        return found
    for name in names:
        if ":" in name:                          # an interface, not a device
            continue
        d = os.path.join(sysfs, name)
        try:
            vendor, product = int(_attr(d, "idVendor"), 16), int(_attr(d, "idProduct"), 16)
        except (OSError, ValueError):
            continue
        if vendor == VENDOR and product in PRODUCTS:
            found.append(BootDevice(d))
    return found


class Usb:
    """One opened device: control and bulk transfers through usbfs."""

    def __init__(self, node, interface, out_ep):
        self.fd = os.open(node, os.O_RDWR)
        self.interface = interface
        self.out_ep = out_ep
        try:
            fcntl.ioctl(self.fd, USBDEVFS_CLAIMINTERFACE, struct.pack("I", interface))
        except OSError:
            os.close(self.fd)
            raise

    def close(self):
        try:
            fcntl.ioctl(self.fd, USBDEVFS_RELEASEINTERFACE, struct.pack("I", self.interface))
        except OSError:
            pass                                        # gone already: it left to boot
        os.close(self.fd)

    def control(self, request_type, value, index, length=0, timeout_ms=1000):
        buf = ctypes.create_string_buffer(max(length, 1))
        ctl = _CtrlTransfer(request_type, 0, value, index, length, timeout_ms,
                            ctypes.cast(buf, ctypes.c_void_p) if length else None)
        n = fcntl.ioctl(self.fd, USBDEVFS_CONTROL, ctl)
        return buf.raw[:n] if length else b""

    def bulk_out(self, data, timeout_ms=5000):
        buf = ctypes.create_string_buffer(data, len(data))
        bulk = _BulkTransfer(self.out_ep, len(data), timeout_ms, ctypes.cast(buf, ctypes.c_void_p))
        return fcntl.ioctl(self.fd, USBDEVFS_BULK, bulk)


def write(usb, data):
    """rpiboot's ep_write: the length in a vendor request, then the data."""
    n = len(data)
    usb.control(0x40, n & 0xFFFF, n >> 16)
    sent = 0
    while sent < n:
        sent += usb.bulk_out(data[sent:sent + CHUNK])
    return sent


def read(usb, n, timeout_ms=20000):
    """rpiboot's ep_read: a vendor control-IN request of n bytes."""
    return usb.control(0xC0, n & 0xFFFF, n >> 16, n, timeout_ms)


def send_bootcode(usb, bootcode):
    """The boot ROM's part: a 24-byte header (length, no signature), the
    second stage itself, then its 4-byte result (0: started)."""
    write(usb, struct.pack("<i20s", len(bootcode), b""))
    write(usb, bootcode)
    time.sleep(1)
    return struct.unpack("<i", read(usb, 4)[:4].ljust(4, b"\0"))[0]


def resolve(boot_dir, name):
    """The file the Pi asks for, inside boot_dir and nowhere else, or None."""
    name = name.lstrip("/")
    path = os.path.realpath(os.path.join(boot_dir, name))
    root = os.path.realpath(boot_dir)
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return None
    return path


def serve_files(usb, boot_dir, log=lambda msg: None):
    """The second stage's part: answer its requests until it is done. Returns
    the names it was given."""
    served = []
    size_for = None
    while True:
        try:
            req = read(usb, FILE_REQUEST)
        except OSError:
            break                                       # it left USB to boot
        command = struct.unpack("<i", req[:4].ljust(4, b"\0"))[0]
        name = req[4:].split(b"\0", 1)[0].decode("ascii", "replace")
        if not name or command == DONE:
            if not name:
                write(usb, b"")
            break
        if name.startswith("*"):                        # metadata (BCM2711/2712 only)
            write(usb, b"")
            continue
        path = resolve(boot_dir, name)
        if command == GET_SIZE:
            size_for = path
            if path:
                size = os.path.getsize(path)
                usb.control(0x40, size & 0xFFFF, size >> 16)
            else:
                write(usb, b"")
        elif command == READ_FILE:
            if path and path == size_for:
                with open(path, "rb") as f:
                    write(usb, f.read())
                served.append(name)
                log("rpiboot: sent %s" % name)
            else:
                write(usb, b"")
            size_for = None
        else:
            raise ValueError("rpiboot: unknown request %d" % command)
    return served


def open_device(dev):
    """rpiboot's choice: one interface means interface 0 with bulk endpoint 1;
    two (a 2837 that also offers mass storage) interface 1 with endpoint 3."""
    if dev.interfaces == 1:
        return Usb(dev.node, 0, 1)
    return Usb(dev.node, 1, 3)


def boot(boot_dir, log=lambda msg: None, timeout_s=30.0):
    """Boot the Pi waiting on USB with the files in boot_dir. True once the
    second stage has taken them and left; False if no Pi came, or it stopped
    half way."""
    deadline = time.monotonic() + timeout_s
    last_serial = None
    with open(os.path.join(boot_dir, "bootcode.bin"), "rb") as f:
        bootcode = f.read()
    while time.monotonic() < deadline:
        devices = [(d, s) for d in find() for s in [_serial(d)] if s is not None and s != last_serial]
        if not devices:
            time.sleep(0.2)
            continue
        dev, serial = devices[0]
        try:
            usb = open_device(dev)
        except OSError as e:
            log("rpiboot: cannot open %s: %s" % (dev.node, e))
            time.sleep(0.5)
            continue
        try:
            if serial in (0, 3):
                result = send_bootcode(usb, bootcode)
                log("rpiboot: bootcode.bin sent (%d)" % result)
            else:
                served = serve_files(usb, boot_dir, log)
                log("rpiboot: %d files served" % len(served))
                return bool(served)
        except OSError as e:
            log("rpiboot: %s" % e)
        finally:
            usb.close()
        last_serial = serial
        time.sleep(1.0)
    return False


def _attr(sysdir, name):
    with open(os.path.join(sysdir, name)) as f:
        return f.read().strip()


def _serial(dev):
    try:
        return dev.serial_index
    except (OSError, IndexError):
        return None
