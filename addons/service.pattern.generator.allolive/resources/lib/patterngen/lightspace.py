"""LightSpace / ColourSpace network client, as PGenerator 2.12.1 runs it
(discovery.pm discovery_lightspace, client.pm lightspace_connect).

The calibration PC sends a UDP datagram to port 20123 containing "LS:" and ending
with ":<tcp port>"; the generator connects out to the sender on that port. Each
recv() is one message: an XML <calibration><shapes><rectangle>... block (one
rectangle: the patch on black; two: the background, then the patch), with colours
in <color> (8-bit) or <colex bits=".."> and the geometry as fractions of the screen.
The pattern is drawn through the PatternDynamic template at full range. Nothing is
answered, except "Command:IsAlive" after 5 s of silence; 5 s more closes the session.
On disconnect the range is released, a black full field is drawn and the UDP
listener opens again. Only one session runs at a time.
"""

import re
import select
import socket
import threading
import xml.etree.ElementTree as ET
from xml.parsers import expat

from .perl import dec_int, perl_num
from .upgci import Session
from .templates import round_val


KEY_ATTR = ("name", "key", "id")        # XML::Simple's default KeyAttr


class _Die(Exception):
    """A Perl die while reading the message (a hash lookup on an array reference)."""


def xmlin(e):
    """XML::Simple's XMLin (default options) of an element: attributes and child elements
    in one hash, a repeated name as an array, text as "content" (only text and no
    attributes: the string), an empty element {}, arrays whose items all carry a
    name/key/id attribute folded into a hash keyed by it."""
    attr = dict(e.attrib)
    items = ([("0", e.text)] if e.text else [])
    for c in e:
        items.append((c.tag, c))
        if c.tail:
            items.append(("0", c.tail))
    for i, (key, val) in enumerate(items):
        if key == "0":
            if re.fullmatch(r"\s*", val):
                continue
            if not attr and i == len(items) - 1:
                return val
            key = "content"
        else:
            val = xmlin(val)
        if key in attr:
            if isinstance(attr[key], list):
                attr[key].append(val)
            else:
                attr[key] = [attr[key], val]
        elif isinstance(val, list):
            attr[key] = [val]
        else:
            attr[key] = val
    for key, val in list(attr.items()):
        if isinstance(val, list):
            attr[key] = _fold(val)
    return attr


def _parse(raw):
    """XML::Parser without namespace processing (XMLin's default): an element keeps its name
    as written ("shapes", "a:shapes") and xmlns is an ordinary attribute."""
    tb = ET.TreeBuilder()
    p = expat.ParserCreate()
    p.StartElementHandler = tb.start
    p.EndElementHandler = tb.end
    p.CharacterDataHandler = tb.data
    p.Parse(raw, True)
    return tb.close()


def _fold(items):
    """array_to_hash with KeyAttr [name key id]."""
    out = {}
    for item in items:
        if not isinstance(item, dict):
            return items
        for k in KEY_ATTR:
            if item.get(k) is not None:
                if isinstance(item[k], (dict, list)):
                    return items
                out[item[k]] = {n: v for n, v in item.items() if n != k}
                break
        else:
            return items
    return out


def _get(v, key):
    """$v->{key}: undef stays undef, a string is a symbolic (empty) hash, an array dies."""
    if isinstance(v, list):
        raise _Die("Not a HASH reference")
    return v.get(key) if isinstance(v, dict) else None


def _str(v):
    """A value interpolated into a string: undef "", references HASH(0x..)/ARRAY(0x..)."""
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return "%s(0x%x)" % ("HASH" if isinstance(v, dict) else "ARRAY", id(v))
    return v


def _values(node, names):
    return [_str(_get(node, n)) for n in names]


def payload(xml_text, width, height):
    """A LightSpace message -> the PatternDynamic payload (lightspace_connect), or None when
    it holds no <calibration>...</calibration> or XMLin dies on it (bad XML or UTF-8;
    deviation: skipped, PGenerator's session thread dies). xml_text: the bytes as latin-1."""
    i = xml_text.rfind("<calibration>")
    if i < 0:
        return None
    xml_text = xml_text[i:]
    j = xml_text.find("</calibration>")
    if j < 0:
        return None
    try:
        raw = xml_text[:j + len("</calibration>")]
        try:
            raw = raw.encode("latin-1")
        except UnicodeEncodeError:
            raw = raw.encode("utf-8")
        root = xmlin(_parse(raw))               # expat: UTF-8, as XMLin reads it
        rect = _get(_get(root, "shapes"), "rectangle")
        two = isinstance(rect, list)
        patch = (rect[1] if len(rect) > 1 else None) if two else rect
        rgb = _values(_get(patch, "color"), ("red", "green", "blue"))
        cx_r, cx_g, cx_b, bits = _values(_get(patch, "colex"), ("red", "green", "blue", "bits"))
        bgv = _values(_get(rect[0], "color"), ("red", "green", "blue")) if two \
            else ["0", "0", "0"]
        bx = _values(_get(rect[0], "colex"), ("red", "green", "blue", "bits")) if two else None
        x, y, cx, cy = (v.replace(",", ".") for v in
                        _values(_get(patch, "geometry"), ("x", "y", "cx", "cy")))
    except (expat.ExpatError, _Die, RecursionError):
        return None
    # PGenerator on a Pi 4 uses colex when it has bits; <color> is 8-bit: declared as such
    # (deviation: PGenerator reads it at max_bpc)
    if bits != "":
        rgb = [cx_r, cx_g, cx_b]
    else:
        bits = "8"
    if bx is not None and re.match(r"\d+$", bits, re.A) and 8 < dec_int(bits) <= 16:
        # the background shares the patch's BITS (deviation: PGenerator reads the 8-bit
        # <color> at that depth): its own colex at that depth, else <color> scaled to it
        if bx[3] == bits and "" not in bx[:3]:
            bgv = bx[:3]
        elif all(re.match(r"\d+$", v, re.A) for v in bgv):
            top = (1 << dec_int(bits)) - 1
            bgv = [str((dec_int(v) * top * 2 + 255) // 510) for v in bgv]
    dim = "%s,%s" % (round_val(perl_num(cx) * width), round_val(perl_num(cy) * height))
    pos = "%s,%s" % (round_val(perl_num(x) * width), round_val(perl_num(y) * height))
    return "%s;%s;RECTANGLE;%s;%s;100;;;%s" % (",".join(rgb), ",".join(bgv), dim, pos, bits)


def drawable(p):
    """An empty colour code reaches the renderer as "RGB=,1,1" or "BG=,0,0", which it cannot
    read: it dies (deviation: not drawn). The renderer reads only the first three fields,
    so a trailing empty one ("9,") is drawn."""
    rgb, bg = p.split(";")[:2]
    return all("" not in (v.split(",") + [""] * 3)[:3] for v in (rgb, bg))


def peer_port(text):
    """IO::Socket::INET's PeerPort: "name(NNN)" falls back to NNN, a service name is looked
    up, a number above 65535 is truncated (pack_sockaddr_in); OSError when none."""
    m = re.search(r"\((\d+)\)\n?$", text, re.A)
    default = dec_int(m.group(1)) if m else None
    text = text[:m.start()] if m else text
    num = re.match(r"(\d+)\n?$", text, re.A)
    port = None
    if re.search(r"\D", text, re.A):
        try:
            port = socket.getservbyname(text, "tcp")
        except (OSError, ValueError, UnicodeError):
            port = None
    port = port or default or (dec_int(num.group(1)) if num else None)
    if not port:
        raise OSError("bad port %r" % text)
    return port % 65536


class LightSpace:
    def __init__(self, server, port=20123, timeout=5.0):
        self.server = server
        self.cal = server.calman
        self.gen = server.gen
        self.log = server.log
        self.port = port
        self.timeout = timeout
        self.stopping = threading.Event()
        self.socks = set()
        self.lock = threading.Lock()
        self.peer = None            # (ip, port) of the connected PC
        self.bind_error = None      # UDP port could not be bound: not listening

    def start(self):
        threading.Thread(target=self._listen, daemon=True, name="lightspace").start()

    def stop(self):
        self.stopping.set()
        with self.lock:
            socks = list(self.socks)
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def _track(self, s, add=True):
        with self.lock:
            (self.socks.add if add else self.socks.discard)(s)

    def _bind(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("", self.port))
            s.settimeout(1.0)
        except OSError:
            s.close()
            raise
        self._track(s)
        return s

    def _listen(self):
        while not self.stopping.is_set():
            try:
                s = self._bind()
            except OSError as exc:
                self.log("LightSpace disabled, UDP %d: %s" % (self.port, exc), error=True)
                self.bind_error = exc
                return
            target = None
            with s:
                while not self.stopping.is_set():
                    try:
                        data, addr = s.recvfrom(1024)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not data or data == b"0":
                        continue
                    text = data.decode("latin-1")
                    m = re.search(r"(.*):(.*)", text)
                    if m and m.group(2) != "" and "LS:" in text and self.cal.discoverable:
                        target = (addr[0], m.group(2))
                        break
            self._track(s, False)
            if target and not self.stopping.is_set():
                self._session(*target)

    def _session(self, ip, port):
        self.log("LightSpace trigger from %s: connecting to port %s" % (ip, port))
        with self.gen.lock:
            self.cal.stat("connections")
            self.cal.set_client(ip, "LightSpace")
        sock = None
        try:
            port = peer_port(port)
            sock = socket.create_connection((ip, port), timeout=self.timeout)
            self._track(sock)
            self.peer = (ip, port)
            self.log("LightSpace connected to %s:%s" % (ip, port))
            self._serve(sock)
        except OSError as exc:
            if not self.stopping.is_set():
                self.log("LightSpace session with %s:%s ended: %s" % (ip, port, exc))
        finally:
            self.peer = None
            if sock is not None:
                self._track(sock, False)
                try:
                    sock.close()
                except OSError:
                    pass
            if not self.stopping.is_set():
                with self.gen.lock:
                    self.cal.release("lightspace")
                    self.cal.set_client("", "")     # discovery.pm:76-79, whoever set it
                    s = self.gen.sig
                    # discovery.pm:80: a black pattern when LightSpace disconnects
                    Session(self.cal, self.server.info, self.log).create_pattern(
                        "RECTANGLE", "%d,%d" % (s.width, s.height), "0,0,0", "", "", "", True,
                        "100")
                    self.cal.refresh()

    def _readable(self, sock):
        """select() for up to timeout, woken by stop()."""
        left = self.timeout
        while left > 0 and not self.stopping.is_set():
            step = min(0.25, left)
            r, _, _ = select.select([sock], [], [], step)
            if r:
                return True
            left -= step
        return False

    def _serve(self, sock):
        last = None                 # deviation: per session (a reconnect after the black
        while not self.stopping.is_set():       # field would skip the same pattern)
            if not self._readable(sock):
                if self.stopping.is_set():
                    return
                sock.sendall(b"Command:IsAlive")
                if not self._readable(sock):
                    return
            data = sock.recv(1024)
            if not data:
                return
            text = data.decode("latin-1")         # bytes as sent: XMLin reads UTF-8
            if "<calibration>" not in text:
                continue
            with self.gen.lock:
                self.cal.info_tick()
                s = self.gen.sig
                p = payload(text, s.width, s.height)
                if p is None:
                    self.log("LightSpace: message not understood: %r" % text[:200])
                    continue
                if p == last:
                    continue
                last = p
                self.gen.pattern_request = "LIGHTSPACE " + p
                self.cal.apply_source(2, "lightspace")
                if not drawable(p):
                    self.cal.refresh()
                    continue
                bits = p.rsplit(";", 1)[1]
                depth = int(bits) if self.cal.std_dv() and bits in ("8", "10", "12") else 0
                self.cal.template_pattern("TESTTEMPLATE", "PatternDynamic", p, "", depth)
                self.cal.refresh()
