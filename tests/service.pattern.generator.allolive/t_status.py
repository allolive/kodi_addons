"""kodi.Service wiring across both feature sets: idle at boot (no socket on the classic,
DeviceControl or LightSpace ports until Start), status line and toasts naming the
client software (Calman, HCFR, a CLIENTNAME=, LightSpace), "Waiting for LightSpace"
when only UDP listeners are on, a settings change reopening exactly the enabled ports,
and the menu's Connection line. Mocked Kodi; ports from PGPORT. Exit 1 on any failure.
"""
import socket
import sys
import time

from status_rig import check, CLASSIC, DC, END, FAILS, LS, open_ports, PORTS, RPC, settings, status, TCP, TOASTS, wait
from patterngen import kodi

# -- idle at boot ----------------------------------------------------------------------
svc = kodi.Service()
time.sleep(0.2)
check("boot: no port open", open_ports(), [])
check("boot: stopped", (svc.window.getProperty(kodi.ADDON_ID + ".state"), status(svc)),
      ("stopped", "Stopped"))

# -- Start: everything the settings enable ----------------------------------------------
svc.onNotification(kodi.ADDON_ID, "Other." + "start", "")
wait(lambda: len(open_ports()) == 5)
check("start: all five ports", sorted(open_ports()), sorted(p for p, _ in PORTS))
addr = svc.address()
check("start: address lists the TCP ports", addr.split(":", 1)[1], "%d/%d/%d" % (TCP, RPC, CLASSIC))
check("start toast", TOASTS[-1:], ["Generator started on %s" % addr])
check("listening status", status(svc), "Listening on " + addr)

# -- HCFR on the classic port: named in the status and the toast --------------------------
c = socket.create_connection(("127.0.0.1", CLASSIC))
c.settimeout(3)
c.sendall(b"CLIENTNAME=HCFR" + END)
c.recv(64)
wait(lambda: svc.server.client() is not None)
check("HCFR status", status(svc), "HCFR connected from 127.0.0.1")
n = len(TOASTS)
svc.check(signal=True)
check("HCFR toast", TOASTS[n:], ["HCFR connected (127.0.0.1)"])
c.close()
wait(lambda: not kodi._peers(svc.server))
n = len(TOASTS)
svc.check(signal=False)
check("HCFR disconnect toast", TOASTS[n:], ["HCFR disconnected"])
check("status after disconnect", status(svc), "Listening on " + addr)

# -- unnamed client (no data yet) --------------------------------------------------------
c = socket.create_connection(("127.0.0.1", CLASSIC))
wait(lambda: kodi._peers(svc.server))
check("unnamed client status", status(svc), "Client connected from 127.0.0.1")
c.close()
wait(lambda: not kodi._peers(svc.server))

# -- LightSpace: the outbound connection counts as a peer ---------------------------------
pc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
pc.bind(("127.0.0.1", 0))
pc.listen(1)
pc.settimeout(3)
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
u.sendto(b"LS:127.0.0.1:%d" % pc.getsockname()[1], ("127.0.0.1", LS))
try:
    conn, _ = pc.accept()
except socket.timeout:
    conn = None
    FAILS.append("LightSpace did not connect out")
if conn:
    wait(lambda: svc.server.lightspace.peer is not None)
    check("LightSpace status", status(svc), "LightSpace connected from 127.0.0.1")
    n = len(TOASTS)
    svc.check(signal=True)
    check("LightSpace toast", [t for t in TOASTS[n:] if "connected" in t],
          ["LightSpace connected (127.0.0.1)"])
    svc.shutdown()          # ends the session without drawing through the mocked player
    conn.close()
u.close()
pc.close()
time.sleep(1.3)
check("shutdown: all ports closed", open_ports(), [])

# -- only UDP listeners: "Waiting for LightSpace" -----------------------------------------
settings.update(upgci_enabled="false", classic_enabled="false")
svc.start()
wait(lambda: len(open_ports()) == 2)
check("UDP only: DeviceControl + LightSpace open", sorted(open_ports()), sorted([DC, LS]))
check("UDP only status", status(svc), "Waiting for LightSpace on UDP %d" % LS)

# -- settings change: restart opens exactly what is enabled --------------------------------
settings.update(classic_enabled="true", devicecontrol_discovery="false", lightspace_enabled="false")
svc.onSettingsChanged()
svc.tick()                  # the service loop does the restart the callback asked for
time.sleep(1.3)
check("after settings change", sorted(open_ports()), [CLASSIC])

# -- the manual-pattern screen saving its choices must not restart the generator --------
server_before = svc.server
settings.update(test_signal="3", test_max_luma="2000")
svc.onSettingsChanged()
svc.tick()                  # the service loop does the restart the callback asked for
time.sleep(1.3)
check("manual-only settings saved: generator not restarted", svc.server is server_before, True)
settings.update(classic_enabled="false")
svc.onSettingsChanged()
svc.tick()                  # the service loop does the restart the callback asked for
time.sleep(1.3)
check("nothing enabled: no server", (svc.server, open_ports()), (None, []))
check("nothing enabled: status", status(svc), "Running, no connection enabled")

# -- Stop ---------------------------------------------------------------------------------
settings.update(classic_enabled="true", devicecontrol_discovery="true", lightspace_enabled="true")
svc.onSettingsChanged()
svc.tick()                  # the service loop does the restart the callback asked for
time.sleep(0.3)
svc.onNotification(kodi.ADDON_ID, "Other.stop", "")
time.sleep(1.3)
check("stop: ports closed, stopped", (open_ports(), status(svc)), ([], "Stopped"))

for f in FAILS:
    print("FAIL", f)
print("service status/wiring: %d failures" % len(FAILS))
sys.exit(1 if FAILS else 0)
