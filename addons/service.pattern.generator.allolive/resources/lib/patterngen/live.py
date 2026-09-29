"""Continuous pattern stream: one endless Matroska stream that Kodi plays once.

A new pattern is spliced into the running stream at its next frame instead of starting
a new clip, so the video decoder never closes and a Dolby Vision (or HDR) signal is
never torn down and re-negotiated between patterns. Measured on the AM9 (2026-09-27):
DV held for 10 minutes and 209 switches, each on screen ~0.4 s after it was requested.

Two Amlogic facts shape it:
- the codec shows no first picture until its compressed buffer is 5% full by bytes, and
  a decoded frame leaves the buffer, so until the first picture is up every frame
  carries filler data (PRIME bytes);
- frames are only made as the display consumes them (the Amlogic sync clock gives the
  stream time on screen), LEAD seconds ahead, so a new pattern waits behind LEAD of the
  old one and not behind whatever piled up while the player started. Under ~0.25 s the
  player runs dry and slows down (0.55x measured at 0.15 s).

The stream is served on 127.0.0.1 only. Kodi opens it once or twice to probe before it
plays, and nothing says which connection is the player: every open connection of the
stream gets every switch, and a pattern counts as shown only once all of them are past
it (an unread connection holding the switch would otherwise let the old pattern be
acknowledged). A new stream does
not end the old one: Kodi closes the old item itself when it opens the new one, and an
old item that ended on its own at that moment made Kodi stop the new one as well (the
next pattern timed out after every change of format).
"""

from typing import Any
import http.server
import socketserver
import sys
import threading
import time

from . import mkv
from .ids import LIVE_PATH

PATH = LIVE_PATH
CLOCK = "/sys/class/tsync/pts_video"
LEAD = 0.4                  # seconds of stream kept ahead of the picture on screen
START_LEAD = 0.4            # sent before the first picture; grows if the player waits longer
START_LEAD_MAX = 3.0
PRIME = 256 * 1024          # filler per frame until the first picture is up: 2 MB frames
                            # never fit the 1080p codec buffer (nothing was ever decoded)
POLL = 0.005
SUPERSEDED = 5.0            # an old stream's connection, once a new one exists, then ends
SEND_TIMEOUT = 10.0         # a client that stops reading does not hold a thread forever
CLOCK_WRAP = (1 << 32) / 90000.0   # the 90 kHz clock is 32-bit: it wraps every 13.25 h
APPLIED_KEEP = 32           # pattern switches remembered per connection
KEY_REPEAT = 3              # a new pattern's key frame is sent this many times: one lost
                            # frame must not leave the old picture up when it is ACKed
FROZEN = 1.0                # a display clock that has not moved this long (Kodi paused or
                            # closing the stream): frames go out in real time, so a read of
                            # the player never waits on a display that waits on that read
CLOCK_LOST = 0.5            # a started stream whose clock reads nothing this long (a decoder
                            # reset) primes again: it needs its 5% buffer to show anything


def tsync_clock():
    """Stream time (s) of the picture on screen from the Amlogic sync clock, None
    before the first picture or where there is no such clock."""
    try:
        with open(CLOCK) as f:
            v = int(f.read().strip(), 16)
    except (OSError, ValueError):
        return None
    return v / 90000.0 if v else None


def behind(stream_time, now):
    """stream_time - now across a wrap of the 32-bit display clock."""
    d = (stream_time - now) % CLOCK_WRAP
    return d - CLOCK_WRAP if d > CLOCK_WRAP / 2 else d


def clock_available():
    try:
        with open(CLOCK):
            return True
    except OSError:
        return False


_fillers: dict[Any, bytes] = {}


def filler(size):
    """A length-prefixed HEVC filler data NAL unit (FD_NUT) of about `size` bytes."""
    if size <= 0:
        return b""
    f = _fillers.get(size)
    if f is None:
        nal = b"\x4c\x01" + b"\xff" * max(0, size - 7) + b"\x80"
        f = _fillers[size] = len(nal).to_bytes(4, "big") + nal
    return f


class _Conn:
    """One player connection: its frame counter, when each pattern entered it, and
    whether its first picture is on screen."""

    def __init__(self, generation, seq, fps):
        self.generation = generation
        self.seq = seq
        self.fps = fps
        self.applied = {seq: 0.0}   # pattern sequence -> stream time of its first frame
        self.started = False
        self.frames = 0
        self.superseded = None      # when a newer stream replaced this one
        self.frames_at_supersede = 0
        # the display clock was idle (no picture) since this connection began: only then
        # is a running clock this stream's, not the previous one still on screen
        self.fresh = False
        self.prime_from = 0         # the frame priming (re)started at
        self.superseded_like = None     # (time, frame) real-time sending began while frozen

    def note_switch(self, seq):
        self.applied[seq] = self.frames / self.fps
        if len(self.applied) > APPLIED_KEEP:
            del self.applied[min(self.applied)]


class Stream:
    """The live stream. start(enc) opens a stream for enc's format and returns its URL;
    switch(enc) puts another pattern of the same format on screen and returns a token
    for shown(). Thread safe."""

    def __init__(self, log=None, clock=tsync_clock, lead=LEAD, start_lead=START_LEAD,
                 prime=PRIME, host="127.0.0.1"):
        self.log = log or (lambda *a, **k: None)
        self.clock = clock
        self.lead = lead
        self.start_lead = start_lead
        self.prime = prime
        self.host = host
        self.cond = threading.Condition()
        self.server = None
        self.generation = 0
        self.ended = 0              # connections of this generation and older stop
        self.seq = 0
        self.enc = None
        self.key = None
        self.conns = set()          # open connections of any generation
        self.playing = 0            # the generation Kodi reported started (onAVStarted)
        self.closed = False

    # -- control (the backend's thread) -------------------------------------------
    @property
    def url(self):
        assert self.server is not None, "the stream's server is started before its URL is used"
        return "http://%s:%d%s%d.mkv" % (self.host, self.server.server_address[1], PATH,
                                         self.generation)

    def start(self, enc):
        """A new stream for enc's format, showing enc: returns its URL."""
        with self.cond:
            if self.closed:
                raise RuntimeError("live stream closed")
            self._ensure_server()
            self.generation += 1
            self.seq += 1
            self.enc, self.key = enc, enc.stream_key
            self.cond.notify_all()
            return self.url

    def matches(self, enc):
        with self.cond:
            return self.enc is not None and self.key == enc.stream_key

    def switch(self, enc):
        """Show enc (same format) from the next frame: a token for shown()."""
        with self.cond:
            self.seq += 1
            self.enc = enc
            self.cond.notify_all()
            return self.seq

    def player_started(self):
        """Kodi reported the current stream playing: its clock is this stream's."""
        with self.cond:
            self.playing = self.generation

    def _current(self):
        """Under the lock: the open connections of the current stream."""
        return [c for c in self.conns if c.generation == self.generation]

    def connected(self):
        with self.cond:
            return bool(self._current())

    def started(self):
        """The current stream's first picture is on screen."""
        with self.cond:
            return any(c.started for c in self._current())

    def shown(self, token):
        """The pattern switch() returned token for is on screen (to half a frame): every
        open connection of the stream has switched and the display is past it on all."""
        with self.cond:
            conns = self._current()
            ats = [c.applied.get(token) for c in conns]
            fps = conns[0].fps if conns else 0
            started = conns and all(c.started for c in conns)
        if not started or None in ats:
            return False
        now = self.clock()
        return now is not None and all(behind(at, now) <= 0.5 / fps for at in ats)

    def stop(self):
        """End the stream: its connection closes, the player reaches its end."""
        with self.cond:
            self.ended = self.generation
            self.enc = self.key = None
            self.cond.notify_all()

    def close(self):
        self.stop()
        with self.cond:
            self.closed = True
            server, self.server = self.server, None
        if server:
            server.shutdown()
            server.server_close()

    # -- serving (one thread per player connection) ----------------------------------
    def _ensure_server(self):
        if self.server:
            return
        stream = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_GET(self):
                stream._serve(self)

            def do_HEAD(self):          # Kodi stats the URL before it opens it
                stream._serve(self, head=True)

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

            def handle_error(self, request, client_address):
                # a player that hung up mid-reply: one line, not a traceback in Kodi's log
                stream.log("live stream: request from %s failed: %r"
                           % (client_address, sys.exc_info()[1]), debug=True)

        self.server = Server((self.host, 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True,
                         name="patterngen-live").start()

    def _serve(self, req, head=False):
        with self.cond:
            path = req.path.split("?", 1)[0]
            current = path == "%s%d.mkv" % (PATH, self.generation) and self.enc is not None
        if not current or head:
            try:
                if not current:
                    req.send_error(404)
                else:
                    req.send_response(200)
                    req.send_header("Content-Type", "video/x-matroska")
                    req.end_headers()
            except OSError:
                pass
            return
        with self.cond:
            if path != "%s%d.mkv" % (PATH, self.generation) or self.enc is None:
                return                  # replaced in between
            enc, generation, seq = self.enc, self.generation, self.seq
            conn = _Conn(generation, seq, enc.fps_num / float(enc.fps_den))
            self.conns.add(conn)
        req.send_response(200)
        req.send_header("Content-Type", "video/x-matroska")
        req.end_headers()
        try:
            req.connection.settimeout(SEND_TIMEOUT)
            req.wfile.write(mkv.live_header(enc.width, enc.height, enc.fps_num, enc.fps_den,
                                            enc.codec_private, enc.container_colour(),
                                            enc.dovi_record, title="Pattern generator"))
            self._frames(req.wfile, conn, enc)
        except OSError:                 # the player closed it
            pass
        finally:
            with self.cond:
                self.conns.discard(conn)
            self.log("live stream %d: connection closed after %d frames"
                     % (generation, conn.frames), debug=True)

    def _over(self, conn):
        """Under the lock: this connection should end. A superseded one keeps sending its
        last pattern until the player drops it, at most SUPERSEDED seconds."""
        if conn.generation <= self.ended:
            return True
        if conn.generation != self.generation:
            if conn.superseded is None:
                conn.superseded = time.time()
                conn.frames_at_supersede = conn.frames
            return time.time() - conn.superseded > SUPERSEDED
        return False

    def _frames(self, out, conn, enc):
        before = self.clock()           # the clock as a previous stream left it
        conn.fresh = before is None
        seen = None                     # last display time read once started
        lost = None                     # since when a started stream's clock reads nothing
        moved, last = time.time(), None     # when the display clock last changed, its value
        waited = 0.0
        since = 0
        while True:
            with self.cond:
                if self._over(conn):
                    return
                if self.seq != conn.seq and conn.generation == self.generation:
                    conn.seq, enc, since = self.seq, self.enc, 0
                    conn.note_switch(conn.seq)
            fill = b"" if conn.started else filler(self.prime)
            # the key frame KEY_REPEAT times, then P frames numbered from the last of them
            key = since < KEY_REPEAT
            payload = (enc.key_frame(fill) if key
                       else enc.repeat_frame(since - KEY_REPEAT + 1, fill))
            ms = int(round(conn.frames * 1000.0 / conn.fps))
            out.write(mkv.live_frame(ms, payload, key))
            out.flush()
            conn.frames += 1
            since += 1
            # the next frame waits until the display is less than the lead behind it
            while True:
                now = self.clock()
                if now is None:
                    conn.fresh = True
                    if conn.started and conn.superseded is None:   # an old stream's decoder
                        # closing is not a reset of this one
                        lost = lost or time.time()
                        if time.time() - lost > CLOCK_LOST:
                            conn.started, seen, lost = False, None, None
                            conn.prime_from, waited = conn.frames, 0.0
                            self.log("live stream %d: display clock lost, priming again"
                                     % conn.generation)
                else:
                    lost = None
                if now != last:
                    moved, last = time.time(), now
                frozen = conn.started and time.time() - moved > FROZEN
                # the first picture: a clock that moved, near this stream's start, and not
                # the previous stream's still running: it went idle in between, or it
                # jumped back to this stream's start after Kodi said this one is playing
                if (not conn.started and now is not None and now != before
                        and behind(conn.frames / conn.fps + 1.0, now) >= 0
                        and (conn.fresh or (self.playing == conn.generation
                                            and behind(before, now) > 0))):
                    conn.started = True
                    self.log("live stream %d: first picture after %d frames"
                             % (conn.generation, conn.frames), debug=True)
                if conn.superseded is not None or frozen:
                    # Kodi is switching to a newer stream, stopping or paused: it closes
                    # this one once a read returns, and the display it paced on has stopped
                    # - frames go out in plain real time instead (waiting on the display
                    # delayed every format change 5 s, and hung Player.stop() for good)
                    if conn.superseded is None:     # frozen: real time from the freeze on
                        conn.superseded_like = conn.superseded_like or (time.time(), conn.frames)
                        t_from, f_from = conn.superseded_like
                    else:
                        t_from, f_from = conn.superseded, conn.frames_at_supersede
                    ahead = conn.frames / conn.fps - (time.time() - t_from + f_from / conn.fps)
                    limit = 0.0
                elif conn.started:
                    # a clock that reads nothing for a moment (a decoder reset) holds the
                    # stream where the display last was
                    conn.superseded_like = None
                    seen = now if now is not None else seen
                    ahead, limit = behind(conn.frames / conn.fps, seen or 0.0), self.lead
                else:
                    # measured from where priming began, not from the stream's start
                    ahead = (conn.frames - conn.prime_from) / conn.fps
                    limit = min(START_LEAD_MAX, self.start_lead + waited / 5.0)
                if ahead < limit:
                    break
                # the display runs in real time: sleep until it should have caught up
                # (about one wake-up per frame), but poll fast for the first picture
                pause = POLL if not conn.started else min(0.05, max(POLL, ahead - limit))
                with self.cond:
                    if self._over(conn):
                        return
                    self.cond.wait(pause)
                if not conn.started:
                    waited += pause
