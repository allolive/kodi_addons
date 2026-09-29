"""Rig shared by t_status and t_check2: the fake Kodi, the service, free ports, status()."""
import socket

import fake_kodi
from fake_kodi import TOASTS, settings  # noqa: F401
from testlib import FAILS, Ports, bindable, check, wait  # noqa: F401

fake_kodi.install()
from patterngen import kodi  # noqa: E402

END = b"\x02\r"
kodi.Notifier.TIME = kodi.Notifier.FADE = 0.0

_ports = Ports(1000)
# free for both TCP and UDP: DeviceControl and LightSpace are UDP
TCP, RPC, CLASSIC, DC, LS = (_ports.free(socket.SOCK_STREAM, socket.SOCK_DGRAM) for _ in range(5))
kodi.DEVICECONTROL_PORT, kodi.LIGHTSPACE_PORT = DC, LS
PORTS = ((TCP, socket.SOCK_STREAM), (RPC, socket.SOCK_STREAM), (CLASSIC, socket.SOCK_STREAM),
         (DC, socket.SOCK_DGRAM), (LS, socket.SOCK_DGRAM))
settings.update(upgci_enabled="true", upgci_port=str(TCP), rpc_port=str(RPC), discovery="false",
                classic_enabled="true", classic_port=str(CLASSIC),
                devicecontrol_discovery="true", lightspace_enabled="true")


def open_ports():
    return [p for p, k in PORTS if not bindable(p, k)]


def status(svc):
    svc.publish()
    return svc.window.getProperty(kodi.STATUS_PROP)
