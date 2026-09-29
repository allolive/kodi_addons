"""Pattern generator core: signal state, rendering and display, no Kodi imports.

The backend is any object with play(path, mode=, fps=) (blocking until the
picture is up, returning False if it never came up) and stop(); one with a `live`
stream (not None) gets show(encoded, mode=, fps=) instead of clips. All public methods are thread
safe; pattern requests are serialised so a client always gets its ACK after its
own pattern is shown.
"""

import threading
import time

from . import overlay, render
from .patterns import (DV_MAPS, SIGNAL_MODES, Colour, Scene, Signal, patch_scene,
                       specialty_scene)



class Generator:
    """Draws the patterns the protocol ports ask for and puts them on screen through the
    backend: play(path, mode, fps) plays a clip and show(enc, mode, fps) an encoded picture
    in the continuous stream (used when backend.continuous is true), each True only once
    the pattern is on screen; stop() ends playback."""

    def __init__(self, backend, cache_dir, log, duration=300, cache_keep=1000):
        self.backend = backend
        self.cache_dir = cache_dir
        self.log = log
        self.duration = duration
        self.cache_keep = cache_keep
        self.sig = Signal()
        self.lock = threading.RLock()
        self.last = None
        self.notify = None          # called under the lock before each pattern is drawn
        self.announced = None
        self.overlay = False        # debug text drawn into the picture
        self.pattern_request = None
        self._pruned = 0.0
        self._pruning = False
        self.failures = 0           # patterns that did not reach the screen

    # -- state -------------------------------------------------------------
    def set_default_format(self, width, height, fps_num, fps_den):
        with self.lock:
            self.sig.width, self.sig.height = width, height
            self.sig.fps_num, self.sig.fps_den = fps_num, fps_den

    def set_mode(self, mode):
        """Start-up / manual signal mode."""
        with self.lock:
            self.sig.set_mode(mode)

    def close(self):
        """The generator is being replaced. No lock: an in-flight play() holds it
        until Kodi delivers its callbacks, which needs the caller (the service main
        thread) back in waitForAbort."""
        self.last = None

    # -- drawing -----------------------------------------------------------
    def signal_changes(self):
        """Messages for what changed in the signal or the Dolby Vision mapping since the
        last call (none on the first call)."""
        with self.lock:
            mode = self.sig.mode
            state = (mode, self.sig.dv_map_mode if mode == "dv" else None)
            prev, self.announced = self.announced, state
        if prev is None or prev == state:
            return []
        msgs = []
        if prev[0] != state[0]:
            msgs.append("Signal: %s" % (SIGNAL_MODES[mode].name if mode in SIGNAL_MODES else mode))
        if state[1] is not None and prev[1] != state[1]:
            msgs.append("Dolby Vision mapping: %s" % DV_MAPS.get(state[1], state[1]))
        return msgs

    def show_scene(self, scene_fn, remember=None, play=True):
        """scene_fn(sig) -> Scene (None: nothing to draw). Renders, plays, and
        blocks until shown. play=False only renders it into the cache.

        remember = (method name, args) redraws the same request on replay().
        """
        if not play:
            # a pre-render (the manual screen's neighbours) reads the signal under the lock
            # and renders outside it, so a pattern being shown never waits behind one
            with self.lock:
                sig, request = self.sig.copy(), self.pattern_request
            try:
                return self._prepare(scene_fn, sig, request) is not None
            except Exception as exc:
                self.log("pre-rendering failed: %r" % (exc,), error=True)
                return False
        with self.lock:
            if remember is not None:
                self.last = remember
            if self.notify:
                try:
                    self.notify()
                except Exception as exc:
                    self.log("notification failed: %r" % (exc,), error=True)
            sig = self.sig.copy()
            try:
                # None: nothing drawn, the old picture is still up (PGenerator ACKs this;
                # the add-on does not acknowledge a pattern it did not show)
                ready = self._prepare(scene_fn, sig, self.pattern_request)
                fps = sig.fps_num / float(sig.fps_den)
                if ready is None:
                    shown = False
                elif getattr(self.backend, "continuous", False):
                    shown = self.backend.show(ready, mode=sig.mode, fps=fps) is True
                else:
                    shown = self.backend.play(ready, mode=sig.mode, fps=fps) is True
            except Exception as exc:     # never leave the client without an answer
                self.log("pattern failed: %r" % (exc,), error=True)
                shown = False
            if not shown:
                self.failures += 1
                return False
            self._prune()
            return True

    def _prepare(self, scene_fn, sig, request):
        """The scene for sig, rendered for the backend: the encoded picture for the
        continuous stream, else a clip's path. None when there is nothing to draw."""
        scene = scene_fn(sig.copy())
        if scene is None:
            return None
        if self.overlay:
            overlay.add(scene, overlay.describe(request, sig, scene), sig.mode != "sdr")
        if getattr(self.backend, "continuous", False):
            return render.encoded(scene, sig, self.cache_dir, self.log)
        return render.render(scene, sig, self.cache_dir, self.duration, self.log)

    PRUNE_EVERY = 30.0

    def _prune(self):
        """Trim the cache now and then, off this thread: listing and dating up to two
        thousand files after every pattern delayed each acknowledgement."""
        now = time.time()
        if self._pruning or now - self._pruned < self.PRUNE_EVERY:
            return
        self._pruned, self._pruning = now, True

        def run():
            try:
                render.prune(self.cache_dir, self.cache_keep)
            finally:
                self._pruning = False
        threading.Thread(target=run, daemon=True, name="patterngen-prune").start()

    def patch(self, fg, window=10, background=None, play=True):
        """Show colour fg in a window (percent of area, 100 = full field)."""
        win = max(1.0, min(100.0, float(window)))
        return self.show_scene(lambda sig: patch_scene(sig, fg, win, background),
                               ("patch", (fg, window, background)), play)

    def rect(self, fg, bg, x, y, cx, cy):
        """Rectangle given in fractions of the screen (Resolve geometry)."""
        def scene_fn(sig):
            scene = Scene(sig.width, sig.height, bg, "patch")
            if cx >= 1 and cy >= 1:
                scene.background = fg
            else:
                scene.add(int(x * sig.width) & ~1, int(y * sig.height) & ~1,
                          int(round(cx * sig.width / 2)) * 2,
                          int(round(cy * sig.height / 2)) * 2, fg)
            return scene
        return self.show_scene(scene_fn, ("rect", (fg, bg, x, y, cx, cy)))

    def specialty(self, name, play=True):
        """A PGenerator SPECIALTY chart; any other name is a full-field mid grey."""
        return self.show_scene(lambda sig: specialty_scene(sig, name)
                               or patch_scene(sig, Colour.gray(128 / 255.0), 100, None),
                               ("specialty", (name,)), play)

    def replay(self):
        with self.lock:
            if self.last is None:
                return False
            kind, args = self.last
            self.log("redrawing last pattern (%s) for %s" % (kind, self.sig.describe()))
            return getattr(self, kind)(*args)

    def stop(self):
        with self.lock:
            self.last = None
            self.backend.stop()
