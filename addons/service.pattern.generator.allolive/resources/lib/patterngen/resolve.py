"""Resolve calibration protocol client (Calman, HCFR, DisplayCAL, ColourSpace).

The generator connects out to the calibration software (TCP 20002 by
default) and receives length-prefixed XML messages such as

  <calibration><color red="512" green="512" blue="512" bits="10"/>
  <background red="0" green="0" blue="0" bits="10"/>
  <geometry x="0.35" y="0.35" cx="0.3" cy="0.3"/></calibration>

Colours are full-range RGB. Nothing is sent back.
"""

import socket
import struct
import threading
import xml.etree.ElementTree as ET

from .patterns import Colour
from .upgci import tune_client_socket


def _colour(node):
    if node is None:
        return None
    bits = int(float(node.get("bits", "8")))
    return Colour.from_code(float(node.get("red", 0)), float(node.get("green", 0)),
                            float(node.get("blue", 0)), bits, "full")


def _find(node, *names):
    # Element truthiness is "has children", so `a or b` cannot pick a node.
    for n in names:
        found = node.find(n)
        if found is not None:
            return found
    return None


def parse(xml_text):
    """-> (fg, bg, x, y, cx, cy) or None."""
    root = ET.fromstring(xml_text)
    fg = _colour(root.find("color"))
    bg = _colour(root.find("background"))
    geom = root.find("geometry")
    if fg is None:
        rects = root.findall("./shapes/rectangle")
        if rects:
            fg = _colour(_find(rects[-1], "color", "colex"))
            geom = rects[-1].find("geometry")
            if len(rects) > 1:
                bg = _colour(_find(rects[0], "color", "colex"))
    if fg is None:
        return None
    if geom is not None:
        x, y = float(geom.get("x", 0)), float(geom.get("y", 0))
        cx, cy = float(geom.get("cx", 1)), float(geom.get("cy", 1))
    else:
        x = y = 0.0
        cx = cy = 1.0
    return fg, bg or Colour.gray(0.0), x, y, cx, cy


class Client:
    def __init__(self, gen, host, port, log):
        self.gen = gen
        self.host = host
        self.port = port
        self.log = log
        self.stopping = threading.Event()
        self.sock = None

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="resolve").start()

    def stop(self):
        self.stopping.set()
        sock = self.sock
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def _recv_exact(self, sock, n):
        data = b""
        while len(data) < n and not self.stopping.is_set():
            try:
                chunk = sock.recv(n - len(data))
            except socket.timeout:
                continue
            if not chunk:
                return None
            data += chunk
        return data if len(data) == n else None

    def _connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=10)
        sock.settimeout(1.0)
        # notice a calibration PC that went away without closing the socket (~1 min)
        tune_client_socket(sock, idle=30, interval=10, count=3)
        return sock

    def _run(self):
        backoff = 2
        while not self.stopping.is_set():
            try:
                sock = self.sock = self._connect()
            except OSError:
                self.stopping.wait(backoff)
                backoff = min(30, backoff * 2)
                continue
            backoff = 2
            self.log("connected to Resolve target %s:%d" % (self.host, self.port))
            with sock:
                self._serve(sock)
            self.sock = None
            self.log("Resolve target disconnected")
            self.stopping.wait(2)

    def _serve(self, sock):
        try:
            while not self.stopping.is_set():
                head = self._recv_exact(sock, 4)
                if head is None:
                    return
                (length,) = struct.unpack(">I", head)
                if not 0 < length <= 65536:
                    self.log("Resolve: bad message length %d" % length, error=True)
                    return
                body = self._recv_exact(sock, length)
                if body is None:
                    return
                text = body.decode("utf-8", "replace")
                try:
                    pattern = parse(text)
                    if pattern:
                        col = ET.fromstring(text).find(".//color")
                        self.gen.pattern_request = (
                            "RESOLVE %s,%s,%s/%s" % tuple(col.get(k, "?") for k in (
                                "red", "green", "blue", "bits")) if col is not None else "RESOLVE")
                        self.gen.rect(*pattern)
                except Exception as exc:
                    self.log("Resolve: bad message %r (%r)" % (text[:200], exc), error=True)
        except OSError as exc:
            if not self.stopping.is_set():
                self.log("Resolve connection error: %s" % exc)
