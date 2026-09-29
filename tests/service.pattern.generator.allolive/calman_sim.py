import socket, struct, sys, time
host = sys.argv[1]
# UDP discovery
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
r = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); r.bind(("", 3530)); r.settimeout(3)
u.sendto(b"discover", (host, 3529))
try:
    data, addr = r.recvfrom(64); name, p1, p2 = struct.unpack("<24sHH", data)
    print("discovery:", addr[0], name.rstrip(b"\0"), p1, p2)
except socket.timeout:
    print("discovery: no answer")
c = socket.create_connection((host, 2100)); c.settimeout(60)
def send(cmd):
    t = time.time(); c.sendall(b"\x02" + cmd.encode() + b"\x03"); r = c.recv(4096)
    print("%-70s %-10r %.2fs" % (cmd[:70], r[:40], time.time() - t)); return r
for cmd in sys.argv[2:] if len(sys.argv) > 2 else []:
    send(cmd)
