import sys, time, subprocess, random
import testlib  # noqa: E402,F401  the add-on on sys.path
from patterngen import hevc
from array import array
W,H=int(sys.argv[1]),int(sys.argv[2])
pic=hevc.Picture(W,H,(64,512,512))
pic.fill_rect(W//4*1+6, H//4+2, W//3, H//3, (700,300,800))
pic.fill_rect(10, 10, 100, 36, (940,512,512))
if len(sys.argv)>3:  # random noise block to exercise PCM heavily
    random.seed(1)
    for _ in range(3000):
        x=random.randrange(0,W-8)&~1; y=random.randrange(0,H-8)&~1
        pic.fill_rect(x,y,4,4,(random.randrange(4,1020),random.randrange(64,960),random.randrange(64,960)))
p=hevc.make_params(W,H,24000,1001,(1,1,1))
t=time.time()
idr,npcm=hevc.encode_idr(p,pic)
t1=time.time()
data=hevc.p_slice_data(p)
sc=b'\0\0\0\1'
out=sc+hevc.vps(p)+sc+hevc.sps(p)+sc+hevc.pps()+sc+idr
for i in range(1,5): out+=sc+hevc.p_slice_nal(i,data)
open('t.hevc','wb').write(out)
print('idr bytes',len(idr),'pcm cus',npcm,'enc %.2fs'%(t1-t),'p bytes',len(data))
r=subprocess.run(['ffmpeg','-v','error','-i','t.hevc','-f','rawvideo','-pix_fmt','yuv420p10le','-'],capture_output=True)
print('ffmpeg err:',r.stderr.decode()[:500])
dec=r.stdout; fs=W*H*3//2*2
print('frames',len(dec)/fs)
for f in range(len(dec)//fs):
    fr=array('H'); fr.frombytes(dec[f*fs:(f+1)*fs])
    bad=0; off=0
    for pl,(pw,ph,st) in enumerate(((W,H,pic.cw),(W//2,H//2,pic.cw//2),(W//2,H//2,pic.cw//2))):
        src=pic.planes[pl]
        for y in range(ph):
            if fr[off+y*pw:off+(y+1)*pw]!=src[y*st:y*st+pw]: bad+=1
        off+=pw*ph
    print('frame',f,'mismatched rows',bad)
