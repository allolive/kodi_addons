import sys, random
import testlib  # noqa: E402,F401  the add-on on sys.path
from patterngen.cabac import CabacEncoder, Context, RANGE_LPS, TRANS_LPS
from patterngen.bitstream import BitWriter
class Dec:
    def __init__(s,data):
        s.d=data; s.pos=0
        s.range=510; s.off=s.bits(9)
    def bit(s):
        b=(s.d[s.pos>>3]>>(7-(s.pos&7)))&1 if (s.pos>>3)<len(s.d) else 0
        s.pos+=1; return b
    def bits(s,n):
        v=0
        for _ in range(n): v=(v<<1)|s.bit()
        return v
    def dec(s,ctx):
        lps=RANGE_LPS[ctx.state][(s.range>>6)&3]; s.range-=lps
        if s.off>=s.range:
            b=1-ctx.mps; s.off-=s.range; s.range=lps
            if ctx.state==0: ctx.mps=1-ctx.mps
            ctx.state=TRANS_LPS[ctx.state]
        else:
            b=ctx.mps; ctx.state=min(ctx.state+1,62)
        while s.range<256: s.range<<=1; s.off=(s.off<<1)|s.bit()
        return b
    def byp(s):
        s.off=(s.off<<1)|s.bit()
        if s.off>=s.range: s.off-=s.range; return 1
        return 0
    def term(s):
        s.range-=2
        if s.off>=s.range: return 1
        while s.range<256: s.range<<=1; s.off=(s.off<<1)|s.bit()
        return 0
random.seed(int(sys.argv[1]) if len(sys.argv)>1 else 0)
for trial in range(300):
    ops=[]
    for _ in range(random.randrange(1,400)):
        k=random.random()
        if k<0.6: ops.append(('c',random.randrange(4),1 if random.random()<random.choice((0.05,0.5,0.95)) else 0))
        elif k<0.9: ops.append(('b',random.randrange(2)))
        else: ops.append(('t',0))
    ops.append(('t',1))
    w=BitWriter(); e=CabacEncoder(w); cs=[Context(v,26) for v in (184,63,141,197)]
    for o in ops:
        if o[0]=='c': e.encode(cs[o[1]],o[2])
        elif o[0]=='b': e.encode_bypass(o[1])
        else: e.encode_terminate(o[1])
    e.finish(); w.trailing_bits(); data=w.getvalue()
    d=Dec(data); cs=[Context(v,26) for v in (184,63,141,197)]
    for i,o in enumerate(ops):
        if o[0]=='c': r=d.dec(cs[o[1]]); exp=o[2]
        elif o[0]=='b': r=d.byp(); exp=o[1]
        else: r=d.term(); exp=o[1]
        if r!=exp: print('trial',trial,'MISMATCH at',i,o); sys.exit(1)
print('cabac roundtrip OK')
