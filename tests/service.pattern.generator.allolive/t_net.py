"""Network robustness (0.4.7): keepalive/no-delay on client sockets, and a dead connection
that ends after Calman reconnected from the same PC does not reset the live session."""
import os, sys, socket, time
import testlib  # noqa: E402,F401  the add-on on sys.path
from patterngen import upgci, generator
PORT = int(os.environ.get('PGPORT', '12100')) + 3000
from testlib import FAILS as fails, check, wait  # noqa: E402
class FB:
    def play(self, p, **k): return True
    def stop(self): pass
g = generator.Generator(FB(), 'cache_net', lambda *a, **k: None, duration=20)
srv = upgci.Server(g, {'name': 'KodiPG', 'serial': 'x', 'firmware': 'fw'}, lambda *a, **k: None,
                   port=PORT, rpc_port=0, discovery=False)
srv.start(); time.sleep(0.2)
def conn():
    c = socket.create_connection(('127.0.0.1', PORT)); c.settimeout(10); return c
def send(c, cmd):
    c.sendall(b'\x02' + cmd.encode() + b'\x03'); return c.recv(4096)

a = conn(); send(a, 'INIT:2.0')
wait(lambda: srv.clients)
s = next(iter(srv.clients))
check("keepalive on", s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 1)
check("no delay on", s.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 1)
check("keepalive idle 60 s", s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE), 60)
check("calman client set", srv.calman.client_ip, '127.0.0.1')

b = conn(); send(b, 'INIT:2.0')                 # Calman reconnected; a is the dead one
a.close()
check("a's end seen", wait(lambda: len(srv.clients) == 1), True)
time.sleep(0.2)
check("reconnect: live session keeps its client", srv.calman.client_ip, '127.0.0.1')
check("reconnect: live connection still answers", send(b, 'STATUS')[:1] != b'', True)

b.close()                                       # the last one: reset as before
check("plain close resets the client", wait(lambda: srv.calman.client_ip == ''), True)
srv.stop()
print("\n".join(fails) if fails else "network OK")
sys.exit(1 if fails else 0)
