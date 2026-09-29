import os, socket, time
PORT=int(os.environ.get('PGPORT','12100'))
import testlib  # noqa: E402,F401  the add-on on sys.path
from patterngen import upgci, generator, resolve
played=[]
class FB:
    def play(self,p,**k): played.append(p); return True
    def stop(self): played.append('STOP')
def log(m,error=False,debug=False): print(('ERR ' if error else '')+m)
g=generator.Generator(FB(),'cache2',log,duration=20)
srv=upgci.Server(g,{'name':'KodiPG','serial':'KODI-x','firmware':'fw 0.1','stop_on_disconnect':True},log,port=PORT,rpc_port=PORT+1,discovery=False)
srv.start(); time.sleep(0.2)
c=socket.create_connection(('127.0.0.1',PORT)); c.settimeout(10)
def send(cmd):
    c.sendall(b'\x02'+cmd.encode()+b'\x03'); t=time.time()
    r=c.recv(4096); return r, time.time()-t
for cmd in ['INIT:2.0','CAP','STATUS','SN','FIRMWARE','CONF_FORMAT:Resolution=3840x2160,Refresh=23.976','DSMD:HDR10',
            'CONF_HDR:ST2084,0.708,0.292,0.170,0.797,0.131,0.046,0.3127,0.3290,0.0050,1000,1000,004002.2000',
            'RGB_S:0940,0940,0940,010','10_SIZE:18','11_APL:25','CommandRGB:940,64,64,1,999','RGB_A:0512,0512,0512,0100,0100,0100,050',
            'YCC_S:0600,0400,0700,010','CONF_DV:ABSOLUTE','RGB_S:0721,0721,0721,010','DSMD:HDR10+','RGB_B:1000,1000,1000,1020','SPECIALTY:ALIGNMENT','GET_SETTINGS','BOGUS:1','TERM']:
    r,dt=send(cmd); print('%-60s -> %r (%.2fs)'%(cmd[:60],r[:120],dt))
time.sleep(0.8)
print('played:',len(played)); [print(' ',p) for p in played]
srv.stop()
print(resolve.parse('<calibration><color red="512" green="256" blue="128" bits="10"/><background red="0" green="0" blue="0" bits="10"/><geometry x="0.35" y="0.35" cx="0.3" cy="0.3"/></calibration>'))
