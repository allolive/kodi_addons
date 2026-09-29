"""Box facts PGenerator's CMD: getters read (command.pm get_cmd_generic), from /proc,
/sys and the network interfaces. Every reader returns PGenerator's fallback when the
source is missing."""

import fcntl
import os
import re
import shutil
import socket
import struct
import subprocess

NONE = "None"


def _read(path, mode="r"):
    try:
        with open(path, mode) as f:
            return f.read()
    except OSError:
        return b"" if "b" in mode else ""


def ip_address(ifname):
    """get_ip: the interface's IPv4 address, else None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            raw = fcntl.ioctl(s.fileno(), 0x8915,       # SIOCGIFADDR
                              struct.pack("256s", ifname.encode("latin-1", "replace")[:15]))
        return socket.inet_ntoa(raw[20:24])
    except (OSError, ValueError):
        return NONE


def mac_address(ifname):
    """The MAC `ip a` shows on a link/ether line (ARPHRD_ETHER), else None (lo, tunnels)."""
    if _read("/sys/class/net/%s/type" % ifname).strip() != "1":
        return NONE
    mac = _read("/sys/class/net/%s/address" % ifname).strip()
    return mac.upper() if mac else NONE


def _run(argv):
    """A command's output ("" when it is missing or fails), as PGenerator's pipes read it."""
    if not shutil.which(argv[0]):
        return b""
    try:
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return b""


def bt_mac():
    """get_mac($bt_interface): hci0's address from `hcitool dev`, else None."""
    for line in _run(["hcitool", "dev"]).decode("latin-1").splitlines():
        if "hci0" in line:
            f = line.split()
            return f[1] if len(f) > 1 else ""
    return NONE


def cpufreq(cmd):
    """GET_SCALING_GOVERNOR / _AVAILABLE / _CUR_FREQ: cpu0's cpufreq file, chomped."""
    name = {"GET_SCALING_GOVERNOR": "scaling_governor",
            "GET_SCALING_GOVERNOR_AVAILABLE": "scaling_available_governors",
            "GET_SCALING_GOVERNOR_CUR_FREQ": "scaling_cur_freq"}[cmd]
    text = _read("/sys/devices/system/cpu/cpu0/cpufreq/" + name, "rb").decode("latin-1")
    return text[:-1] if text.endswith("\n") else text


def cpu_info():
    """GET_CPU_INFO and friends: the Hardware, Revision and Serial values of /proc/cpuinfo."""
    out = ""
    for line in _read("/proc/cpuinfo", "rb").decode("latin-1").split("\n"):
        for key in ("Hardware", "Revision", "Serial"):
            m = re.search(key + r".*: (.*)", line)
            if m:
                out += m.group(1) + " "
    out = out[:-1] if out.endswith(" ") else out
    return out[:-1] if out.endswith("\n") else out


def free_disk():
    """FREE_DISK: the Avail column of `df -kh` for the row ending with "/"."""
    resp = ""
    for line in _run(["df", "-kh"]).decode("latin-1").splitlines(True):
        f = line.split()
        if re.search(r"/$", line):
            resp = f[3] if len(f) > 3 else ""
    return resp


def interfaces():
    try:
        return sorted(os.listdir("/sys/class/net"))
    except OSError:
        return []


def device_model():
    """$device_model: /proc/device-tree/model as read, its NUL terminator included."""
    return _read("/proc/device-tree/model", "rb").decode("latin-1")


def temperature():
    v = _read("/sys/class/thermal/thermal_zone0/temp").strip()
    try:
        return str(int(int(v) / 1000))
    except ValueError:
        return "0"


def up_from():
    """GET_UP_FROM, with PGenerator's double space ("5  minutes")."""
    try:
        t = int(float(_read("/proc/uptime").split()[0]))
    except (IndexError, ValueError):
        t = 0
    final = " seconds"
    if 60 <= t < 3600:
        t, final = t // 60, " minutes"
    elif 3600 <= t < 86400:
        t, final = t // 3600, " hours"
    elif t >= 86400:
        t, final = t // 86400, " days"
    return "%d %s" % (t, final)


def load_average():
    f = _read("/proc/loadavg").split()
    return f[0] if f else ""


def free_mem():
    for line in _read("/proc/meminfo").splitlines():
        if "MemFree:" in line:
            try:
                return "%dM" % (int(line.split()[1]) // 1024)
            except (IndexError, ValueError):
                break
    return "M"


def edid_info():
    """GET_EDID_INFO on KMS without edidparser: both connectors, no info."""
    return "HDMI-A-1\nNo info available\n\nHDMI-A-2\nNo info available"


def hostname():
    text = _read("/etc/hostname", "rb").decode("latin-1")
    return text[:-1] if text.endswith("\n") else text


def is_valid_ifname(name):
    return bool(re.match(r"[\w.-]{1,15}$", name, re.A))
