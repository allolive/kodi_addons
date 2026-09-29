import sys, time
import testlib  # noqa: E402,F401  the add-on on sys.path
from patterngen import patterns, render
for mode in (sys.argv[1:] or ['sdr','hdr10','dv']):
    sig=patterns.Signal(); sig.set_mode(mode); sig.width,sig.height=3840,2160; sig.fps_num,sig.fps_den=24000,1001
    sig.dv_map_mode = 1 if mode=='dv' else None
    fg=patterns.Colour.from_code(700,500,300,10,'limited')
    sc=patterns.patch_scene(sig,fg,10,patterns.Colour.gray(0.3))
    t=time.time(); p=render.render(sc,sig,'cache',duration=300,log=print); print(mode,p)
