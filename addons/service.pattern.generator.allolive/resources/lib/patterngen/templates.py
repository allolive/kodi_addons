"""PGenerator's pattern templates: the template store (conf.pm set_conf_pattern /
get_conf_pattern) and the template engine (pattern.pm get_pattern), which turns a
template and a DYNAMIC payload into the renderer's pattern file.

The pattern file is produced as PGenerator writes it; shapes.py reads it as the
renderer does. Deviations (README.md): EVALPATTERN templates are refused (PGenerator
runs them as Perl), MACRO= inlines the other template's shapes (PGenerator draws it
and then overwrites it), a negative DIM=N% is ERR (PGenerator dies), and SETCONF's
HCFR block swap needs two DRAW= blocks (PGenerator splices stale captures).
"""

import io
import math
import posixpath
import re
import time
import unicodedata
import zipfile

from . import sysinfo
from .perl import dec_int, perl_int_str, perl_num, split_perl
from .shapes import Codes

# var/lib/PGenerator/tmp as shipped; PatternDynamic is not in the snapshot (inferred from
# its callers: every field DYNAMIC, so a bare "r,g,b;bg" payload is a centred 640x360 box)
SHIPPED = {
    "PatternStart": "DRAW=RECTANGLE\nDIM=DYNAMIC\nRGB=DYNAMIC||0,0,0\nBG=DYNAMIC||0,0,0\n"
                    "POSITION=0,0\nEND=1\nFRAME=DYNAMIC\n",
    "PatternDynamic": "DRAW=DYNAMIC\nDIM=DYNAMIC\nRESOLUTION=DYNAMIC\nRGB=DYNAMIC\nBG=DYNAMIC\n"
                      "POSITION=DYNAMIC\nBITS=DYNAMIC\nTEXT=DYNAMIC\nEND=1\nFRAME=DYNAMIC\n",
}
# variables.pm:265-273
DEFAULTS = {"bg": "0,0,0", "res": "100", "draw": "RECTANGLE", "rgb": "16,16,16",
            "dim": "640,360", "pos": "-1,-1", "text": "No Text", "frame": "1"}
DRAWS = ("RECTANGLE", "CIRCLE", "TRIANGLE", "TEXT", "IMAGE")
MACRO_DEPTH = 8


def round_val(v):
    """pattern.pm round_val: int($v + 0.5)."""
    return perl_int_str(perl_num(v) + 0.5)


def _count(key, out):
    """The RGB=/BG= lines already in a pattern file."""
    return len(re.findall(r"^%s=" % key, out, re.M))


def lines_of(text):
    """Perl's while(<FH>): lines with their newline, a last one without."""
    return re.findall(r"[^\n]*\n|[^\n]+$", text)


def _key_re(key):
    """A key interpolated into a Perl regex; taken literally (a pattern Perl rejects would
    kill the daemon)."""
    return re.escape(key)


def _conf_match(key):
    """conf.pm's /^$type=(.*)/ (get_conf_pattern, set_conf_pattern): the key is a Perl regex
    -> a function line -> ($1 or None) when it matches, else False. A key Perl rejects (or
    one not translated, see perl_regex) is taken literally."""
    m = re.match(r"\(\?[a-z]*(?:-[a-z]*)?\)", key)
    flags, rest = (m.group(0), key[m.end():]) if m else ("", key)     # (?i) before the ^
    rx = perl_regex(flags + "^" + rest + "=(.*)")
    if rx is None or rx[1]:
        lit = re.compile(_key_re(key) + "=(.*)")
        return lambda line: (lambda x: x.group(1) if x else False)(lit.match(line))
    rx = rx[0]
    nums = sorted(i for n, i in rx.groupindex.items() if not n.startswith("_K"))

    def match(line):
        x = rx.search(line)
        return False if x is None else x.group(nums[0]) if nums else None
    return match


# -- a VAR= key as Perl compiles it (native 8-bit string, /d rules) --------------------
_LATIN = [chr(i) for i in range(256)]
_ASCII_CLASSES = {
    "alpha": "A-Za-z", "digit": "0-9", "alnum": "0-9A-Za-z", "upper": "A-Z", "lower": "a-z",
    "space": " \t\n\r\f\v", "blank": " \t", "punct": "!-/:-@\\[-`{-~", "xdigit": "0-9A-Fa-f",
    "word": "0-9A-Za-z_", "cntrl": "\x00-\x1f\x7f", "graph": "!-~", "print": " -~",
    "ascii": "\x00-\x7f"}


def _cat(*cats):
    return lambda c: unicodedata.category(c) in cats or unicodedata.category(c)[0] in cats


def _unicode_prop(name):
    """\\p{name} over the 256 byte values -> a set of characters, None if Perl knows no such
    property (it dies)."""
    n = re.sub(r"[\s_-]", "", name).lower()
    neg = n.startswith("^")
    n = n.lstrip("^")
    for pre in ("is", "xposix"):
        if n.startswith(pre) and len(n) > len(pre) and n != "xposixpunct":
            n = n[len(pre):]
    letter = _cat("L")
    lower = lambda c: unicodedata.category(c) == "Ll" or c in "\xaa\xba"     # noqa: E731
    upper = lambda c: unicodedata.category(c) == "Lu"                          # noqa: E731
    space = lambda c: c in "\t\n\x0b\x0c\r \x85\xa0"                          # noqa: E731
    props = {
        "l": letter, "letter": letter, "alpha": letter, "alphabetic": letter,
        "l&": _cat("Lu", "Ll", "Lt"), "lc": _cat("Lu", "Ll", "Lt"),
        "lower": lower, "lowercase": lower, "upper": upper, "uppercase": upper,
        "digit": _cat("Nd"), "alnum": lambda c: letter(c) or unicodedata.category(c) == "Nd",
        "space": space, "spaceperl": space, "blank": lambda c: c in "\t \xa0",
        "word": lambda c: letter(c) or unicodedata.category(c) in ("Nd", "Pc", "Mn"),
        "punct": _cat("P"), "cntrl": _cat("Cc"),
        "xposixpunct": lambda c: unicodedata.category(c)[0] == "P" or (
            c < "\x80" and unicodedata.category(c)[0] == "S"), "xdigit": lambda c: c in "0123456789ABCDEFabcdef",
        "hex": lambda c: c in "0123456789ABCDEFabcdef", "ascii": lambda c: c < "\x80",
        "any": lambda c: True, "latin": lambda c: letter(c) and c not in "\xaa\xb5\xba"
                                          or c in "\xaa\xba",
        "graph": lambda c: not space(c) and unicodedata.category(c) not in ("Cc", "Zs"),
        "print": lambda c: c in " \xa0" or (not space(c) and
                                            unicodedata.category(c) not in ("Cc", "Zs")),
    }
    for posix in ("alpha", "digit", "alnum", "upper", "lower", "space", "blank", "punct",
                  "xdigit", "word", "cntrl", "graph", "print"):
        cls = re.compile("[" + _ASCII_CLASSES[posix] + "]")
        props["posix" + posix] = lambda c, cls=cls: bool(cls.match(c))
    cats = {"l", "lu", "ll", "lt", "lm", "lo", "m", "mn", "mc", "me", "n", "nd", "nl", "no",
            "p", "pc", "pd", "ps", "pe", "pi", "pf", "po", "s", "sm", "sc", "sk", "so", "z",
            "zs", "zl", "zp", "c", "cc", "cf", "cs", "co", "cn"}
    if n in props:
        test = props[n]
    elif n in cats:
        cat = n[0].upper() + n[1:]
        test = _cat(cat) if len(cat) == 1 else (lambda c, cat=cat: unicodedata.category(c) == cat)
    else:
        return None
    members = {c for c in _LATIN if test(c)}
    return set(_LATIN) - members if neg else members


def _members(chars):
    """Class members; none: a character no byte string holds."""
    return "".join("\\x%02x" % ord(c) for c in sorted(chars)) or "\\uffff"


_HSPACE, _VSPACE = set("\t \xa0"), set("\n\x0b\x0c\r\x85")


class _NoRegex(Exception):
    """A construct the key cannot be translated with (Perl dies, or unsupported)."""


def perl_regex(key):
    """The key as Perl's s/$key/../g compiles it -> (Python regex, anchored at \\G), or
    None (Perl dies on it, or \\G past the start, \\b{..}, a mid-pattern (?i)...: the key is
    then taken literally). \\K becomes empty groups named _K<n> (see perl_sub)."""
    try:
        out, g_start, groups, refs = _translate(key)
        text = "".join(out)
        for i, (kind, val) in refs:
            if kind == "num":
                if val < 1 or val > len(groups):
                    raise _NoRegex()
                text_ref = "(?P=%s)" % groups[val - 1]
            else:
                text_ref = "(?P=%s)" % val
            text = text.replace("\x00REF%d\x00" % i, text_ref)
        return re.compile(text, re.A), g_start
    except (_NoRegex, re.error, OverflowError, ValueError, RecursionError):
        return None


def _translate(key):
    out: list = []
    groups: list = []
    refs: list = []
    i, n, kcount, g_start = 0, len(key), 0, False
    in_class = False
    class_start = 0

    def ref(kind, val):
        refs.append((len(refs), (kind, val)))
        return "\x00REF%d\x00" % (len(refs) - 1)

    def char(c):
        return "\\x%02x" % ord(c) if ord(c) < 256 else "\\u%04x" % ord(c)

    def cls(members, negate):
        if in_class:
            if negate:
                members = set(_LATIN) - set(members)
            return _members(members)
        return "[%s%s]" % ("^" if negate else "", _members(members))
    while i < n:
        c = key[i]
        if in_class:
            if c == "]" and i > class_start:
                in_class = False
                out.append("]")
                i += 1
                continue
            if c == "[":
                m = re.match(r"\[:(\^?)([a-z]+):\]", key[i:])
                if m:
                    if m.group(2) not in _ASCII_CLASSES:
                        raise _NoRegex()
                    members = {ch for ch in _LATIN
                               if re.match("[" + _ASCII_CLASSES[m.group(2)] + "]", ch)}
                    out.append(cls(members, m.group(1) == "^"))
                    i += len(m.group(0))
                    continue
                out.append("\\[")
                i += 1
                continue
            if c in "&~|":
                out.append("\\" + c)
                i += 1
                continue
            if c == "-" and (re.match(r"\\[dDwWsShHvVpP]", key[i + 1:i + 3]) or
                             re.search(r"\\[dDwWsShHvVpP](?:\{[^}]*\}|\w)?$", key[:i]) and
                             out and out[-1] != "-"):
                out.append("\\-")         # next to a class escape '-' is itself
                i += 1
                continue
        elif c == "[":
            in_class = True
            out.append("[")
            i += 1
            if key[i:i + 1] == "^":
                out.append("^")
                i += 1
            class_start = i
            if key[i:i + 1] == "]":
                out.append("\\]")
                i += 1
            continue
        elif c == "{" and (not out or out[-1] == "|" or
                           out[-1].startswith("(") and not out[-1].endswith(")")):
            out.append("\\{")               # quantifies nothing: a literal brace
            i += 1
            continue
        elif c == "(":
            m = re.match(r"\(\?(?:P?<([A-Za-z_]\w*)>|'([A-Za-z_]\w*)')", key[i:], re.A)
            if m:
                groups.append(m.group(1) or m.group(2))
                out.append("(?P<%s>" % groups[-1])
                i += len(m.group(0))
                continue
            if key[i + 1:i + 2] != "?" and key[i + 1:i + 2] != "*":
                groups.append("_g%d" % (len(groups) + 1))
                out.append("(?P<%s>" % groups[-1])
                i += 1
                continue
            if i > 0 and re.match(r"\(\?\^?[a-z]*(?:-[a-z]*)?\)", key[i:]):
                raise _NoRegex()            # flags past the start: rest of the group
            out.append(c)
            i += 1
            continue
        if c != "\\":
            out.append(c)
            i += 1
            continue
        # an escape
        if i + 1 >= n:
            raise _NoRegex()
        e = key[i + 1]
        i += 2
        if e == "x":
            m = re.match(r"\{\s*([0-9A-Fa-f_]*)\s*\}", key[i:])
            if m:
                v = int(m.group(1).replace("_", "") or "0", 16)
                i += len(m.group(0))
            else:
                m = re.match(r"[0-9A-Fa-f]{0,2}", key[i:])
                assert m is not None        # {0,2} also matches nothing
                v = int(m.group(0) or "0", 16)
                i += len(m.group(0))
            out.append(char(chr(v)))
        elif e == "o":
            m = re.match(r"\{([0-7]+)\}", key[i:])
            if not m:
                raise _NoRegex()
            out.append(char(chr(int(m.group(1), 8))))
            i += len(m.group(0))
        elif e == "c":
            if i >= n:
                raise _NoRegex()
            out.append(char(chr(ord(key[i].upper()) ^ 64)))
            i += 1
        elif e == "e":
            out.append("\\x1b")
        elif e == "N" and key[i:i + 1] == "{":
            m = re.match(r"\{(?:U\+([0-9A-Fa-f]+)|([^}]*))\}", key[i:])
            if not m:
                raise _NoRegex()
            try:
                ch = chr(int(m.group(1), 16)) if m.group(1) else unicodedata.lookup(m.group(2))
            except KeyError:
                raise _NoRegex() from None
            out.append(char(ch))
            i += len(m.group(0))
        elif e in "pP":
            m = re.match(r"\{([^}]*)\}|(\w)", key[i:], re.A)
            if not m:
                raise _NoRegex()
            members = _unicode_prop(m.group(1) if m.group(1) is not None else m.group(2))
            if members is None:
                raise _NoRegex()
            out.append(cls(members, e == "P"))
            i += len(m.group(0))
        elif e in "hHvV":
            out.append(cls(_HSPACE if e in "hH" else _VSPACE, e in "HV"))
        elif in_class:
            if e in "dDwWsSbtnrfa\\":
                out.append("\\" + e)
            elif e.isdigit():
                m = re.match(r"[0-7]{1,3}", key[i - 1:])
                if not m:
                    raise _NoRegex()
                out.append(char(chr(int(m.group(0), 8) & 0xFF)))
                i += len(m.group(0)) - 1
            elif e.isalpha() and e.isascii():
                if e in "NRXC":
                    raise _NoRegex()
                out.append(e)
            else:
                out.append(char(e))
        elif e == "K":
            out.append("(?P<_K%d>)" % kcount)
            kcount += 1
        elif e == "G":
            if i != 2:
                raise _NoRegex()
            g_start = True
        elif e == "Z":
            out.append("(?=\\n?\\Z)")
        elif e == "z":
            out.append("\\Z")
        elif e == "N":
            out.append("[^\\n]")
        elif e == "R":
            out.append("(?:\\r\\n|[\\n\\x0b\\x0c\\r\\x85])")
        elif e == "X":
            out.append("(?:\\r\\n|(?s:.))")
        elif e in "bB":
            if key[i:i + 1] == "{":
                raise _NoRegex()
            out.append("\\" + e)
        elif e == "g":
            m = re.match(r"\{(-?\d+)\}|(-?\d+)|\{([A-Za-z_]\w*)\}", key[i:], re.A)
            if not m:
                raise _NoRegex()
            if m.group(3):
                out.append(ref("name", m.group(3)))
            else:
                v = int(m.group(1) or m.group(2))
                v = len(groups) + 1 + v if v < 0 else v
                out.append(ref("num", v))
            i += len(m.group(0))
        elif e == "k":
            m = re.match(r"<([A-Za-z_]\w*)>|'([A-Za-z_]\w*)'|\{([A-Za-z_]\w*)\}", key[i:], re.A)
            if not m:
                raise _NoRegex()
            out.append(ref("name", m.group(1) or m.group(2) or m.group(3)))
            i += len(m.group(0))
        elif e.isdigit():
            m = re.match(r"\d+", key[i - 1:])
            assert m is not None            # key[i - 1] is a digit
            d = m.group(0)
            total = len(re.findall(r"(?<!\\)\((?!\?[^<P'])", key))
            if d[0] != "0" and (len(d) == 1 or int(d) <= total):
                out.append(ref("num", int(d)))
                i += len(d) - 1
            else:
                o = re.match(r"[0-7]{1,3}", d)
                if not o:
                    raise _NoRegex()
                out.append(char(chr(int(o.group(0), 8) & 0xFF)))
                i += len(o.group(0)) - 1
        elif e in "dDwWsSAtnrfa":
            out.append("\\" + e)
        elif e == "C":
            raise _NoRegex()
        elif e.isalpha() and e.isascii():
            out.append(e)           # an unknown escape is the letter itself
        else:
            out.append(char(e))
    if in_class:
        raise _NoRegex()
    return out, g_start, groups, refs


def perl_sub(rx, anchored, value, line):
    """$line =~ s/$key/$value/g with the key from perl_regex. With \\K or \\G, Perl's rule that
    a zero-length match cannot follow one at the same place is applied to the part replaced."""
    ks = [g for g in rx.groupindex if g.startswith("_K")]
    if not ks and not anchored:
        return rx.sub(lambda m: value, line)
    out, pos, copied, zero_at = [], 0, 0, -1
    while pos <= len(line):
        m = rx.match(line, pos) if anchored else rx.search(line, pos)
        if not m:
            break
        k = max([m.start(g) for g in ks if m.start(g) >= 0] or [m.start()])
        if k == m.end() == zero_at:
            if anchored:
                break
            pos = m.start() + 1
            continue
        out.append(line[copied:k] + value)
        copied = m.end()
        zero_at = m.end() if k == m.end() else -1
        pos = m.end()
    return "".join(out) + line[copied:]


def dynamic_only(text):
    """A template whose colours and depth are all DYNAMIC (daemon.pm's codes are then the
    only colours in the file)."""
    for line in lines_of(text or ""):
        if re.match(r"(RGB|BG|BITS)=", line) and not re.match(r"(RGB|BG|BITS)=DYNAMIC", line):
            return False
        if re.match(r"(SOURCE_MAX|VAR|MACRO)=|BITS=DYNAMIC\|\|", line):
            return False                # BITS=DYNAMIC||N: an empty bits field takes N
        if line.startswith("TEXT=") and "$RGB" in line:
            return False                # replace_string writes daemon.pm's codes into it
    return True


# the directories a template name may lead to ($var_dir = /var/lib/PGenerator/)
VAR_DIR = "/var/lib/PGenerator"
TMP_DIR, RAMDISK_DIR = VAR_DIR + "/tmp", VAR_DIR + "/running/tmp"
DIRS = ("/", "/var", "/var/lib", VAR_DIR, VAR_DIR + "/running", TMP_DIR, RAMDISK_DIR,
        VAR_DIR + "/frames")


def resolve(base, name):
    """"$base/$name" as the file system resolves it -> (directory, file name), or None when a
    directory on the way does not exist or the path is a directory (open and rename fail).
    Only PGenerator's own directories are modelled; its other files are not here."""
    if "\x00" in name:
        return None
    path = base
    parts = name.split("/")
    for k, part in enumerate(parts):
        last = k == len(parts) - 1
        if part in ("", "."):
            if last:
                return None
            continue
        if part == "..":
            path = posixpath.dirname(path) if path != "/" else "/"
            if last:
                return None
            continue
        full = posixpath.join(path, part)
        if last:
            return None if full in DIRS or not path.startswith(VAR_DIR) else (path, part)
        if full not in DIRS:
            return None
        path = full
    return None


class Store:
    """$var_dir/tmp (templates), running/tmp (TEMPLATERAMDISK) and the directories around
    them, kept in memory: a template name is a path, as in PGenerator."""

    def __init__(self):
        self.files: dict[str, dict[str, str]] = {d: {} for d in DIRS}
        self.files[TMP_DIR].update(SHIPPED)
        self.dirs = {"tmp": self.files[TMP_DIR], "ramdisk": self.files[RAMDISK_DIR]}

    def read(self, base, name):
        where = resolve(base, name)
        return None if where is None else self.files[where[0]].get(where[1])

    def get(self, name, ramdisk=False):
        return self.read(RAMDISK_DIR if ramdisk else TMP_DIR, name)

    def listing(self, where):
        """get_file_list(get_destination(where)): the directory's files, sorted, one a line."""
        d = resolve(VAR_DIR, where + "/x")
        return "\n".join(sorted(self.files[d[0]])) if d else ""

    def upload(self, where, name, data):
        """END_UPLOAD (daemon.pm:1013-1024): /tmp/UPLOAD_FILE.* is unzipped into
        "$var_dir/$where" (a Zip, as file(1) reports one) or renamed to "$var_dir/$where/$name".
        running/ is a tmpfs, so the rename from /tmp fails there (EXDEV); IMAGES, VIDEO and
        the other directories are not modelled, so nothing is stored in them."""
        text = data.decode("latin-1")
        if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
            d = resolve(VAR_DIR, where + "/x")
            if d is None:
                return
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for info in z.infolist():
                        f = None if info.is_dir() else resolve(d[0], info.filename)
                        if f is not None:
                            self.files[f[0]][f[1]] = z.read(info).decode("latin-1")
            except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError, ValueError):
                pass                        # as PGenerator: a zip that fails to unpack extracts nothing
            return
        f = resolve(VAR_DIR, where + "/" + name)
        if f is not None and not f[0].startswith(VAR_DIR + "/running"):
            self.files[f[0]][f[1]] = text

    def delete(self, where, name):
        """unlink("$var_dir/$where/$name")."""
        d = resolve(VAR_DIR, where + "/x")
        f = resolve(d[0], name) if d else None
        if f is not None:
            self.files[f[0]].pop(f[1], None)

    def clean_pattern_files(self):
        """clean_pattern_files (pattern.pm:740-746): /.jpg$/ and /.png$/ in tmp and running,
        /.save$/ in running, everything in frames."""
        for d, pats in ((TMP_DIR, (r".jpg$", r".png$")),
                        (VAR_DIR + "/running", (r".jpg$", r".png$", r".save$")),
                        (VAR_DIR + "/frames", (r"",))):
            files = self.files[d]
            for name in list(files):
                if any(re.search(p, name) for p in pats):
                    del files[name]

    def get_conf(self, name, key):
        """get_conf_pattern: the first KEY= value, else the whole file less its last newline."""
        text = self.get(name)
        if text is None:
            return ""
        acc = ""
        match = _conf_match(key)
        for line in lines_of(text):
            acc += line
            v = match(line)
            if v is not False:
                v = v or ""                 # an unset $1 is undef: ""
                return v[:-1] if v.endswith("\n") else v
        return acc[:-1] if acc.endswith("\n") else acc

    def set_conf(self, name, ctype, val):
        """set_conf_pattern: written to "$name.tmp", then renamed to $name. TESTCMD copies the
        template over the pattern file without telling the renderer (never shown; the classic
        CMD: handler takes such a key first)."""
        base = RAMDISK_DIR if ctype == "TEMPLATERAMDISK" else TMP_DIR
        val = re.sub("\r\n?", "\n", val)
        if ctype == "TESTCMD":
            return
        keep = ""
        template = ctype.startswith("TEMPLATE")
        if not template:
            match = _conf_match(ctype)
            for line in lines_of(self.read(base, name) or ""):
                if match(line) is not False or line.startswith("END=1"):
                    continue
                keep += line
        if name == "HCFR":
            # deviates from conf.pm:161: with fewer than two DRAW= blocks PGenerator splices
            # the captures of an earlier match; the value is kept as sent
            m = re.search(r"(.*)(DRAW=.*)(DRAW=.*)", val, re.S)
            if m:
                val = m.group(1) + m.group(3) + "\n" + m.group(2)
            val = val.replace("BG=-1,-1,-1", "BG=DYNAMIC", 1)
            val = re.sub(r"(BG=DYNAMIC.*)BG=DYNAMIC", lambda x: x.group(1) + "BG=-1,-1,-1",
                         val, count=1, flags=re.S)
            if val.endswith("\n"):
                val = val[:-1]
        tmp, dest = resolve(base, name + ".tmp"), resolve(base, name)
        if tmp is None:
            return                  # open fails, and so does the rename
        text = ("" if template else ctype + "=") + val + "\n" + keep + \
            ("" if template else "END=1\n")
        if dest is None:
            self.files[tmp[0]][tmp[1]] = text       # the rename fails: $name.tmp stays
        else:
            self.files[tmp[0]].pop(tmp[1], None)
            self.files[dest[0]][dest[1]] = text


class Engine:
    """get_pattern for one daemon: ctx supplies the screen and the defaults."""

    def __init__(self, store, stat):
        self.store = store
        self.stat = stat            # stat("patterns") / stat("errors")
        self.lut_calls = 0          # lut(): opens and closes the LUT file handle
        self.identify_calls = 0     # DIM=NATIVE on an IMAGE: open(IDENTIFY, "identify ..|")
        self.pattern_writes = 0     # a finished get_pattern: open(PATTERN) ... close(PATTERN)

    def _handles(self):
        return {"LUT": self.lut_calls, "IDENTIFY": self.identify_calls,
                "PATTERN": self.pattern_writes}

    def error(self, text="ERR"):
        self.stat("errors")
        return text

    def get_pattern(self, ptype, pattern, rgb, width, height, bits_default, source_range="",
                    source_max=0, count=True, exact=None):
        """-> (reply, pattern file text or None). count: stats("patterns"). exact: payload
        fields (0 rgb, 1 bg) PGenerator scaled to 12-bit, as their own-depth codes
        (prepare_payload); self.marks then says which RGB=/BG= lines of the file (by their
        order among those lines) carry them, for the renderer to draw losslessly."""
        source_max = source_max if source_max in (255, 1023, 4095) else 0
        self.marks: dict[str, dict] = {"RGB": {}, "BG": {}}
        err, body, bits = self._process(ptype, pattern, rgb, width, height, bits_default,
                                        source_range, source_max, 0, exact or {}, self.marks)
        if err is not None:
            return err, None
        if source_max > 0 and not re.search(r"^SOURCE_MAX=", body, re.M):
            body = re.sub(r"^END=(.*)$",
                          lambda m: "SOURCE_MAX=%d\nEND=%s" % (source_max, m.group(1)),
                          body, flags=re.M)
        if source_range != "":
            body = re.sub(r"^END=(.*)$", lambda m: "SOURCE_RANGE=%s\nEND=%s" % (source_range,
                                                                               m.group(1)),
                          body, flags=re.M)
        if not re.search(r"\n^FRAME=", body, re.M):
            body += "FRAME=%s\n" % DEFAULTS["frame"]
        head = ""
        if not re.search(r"^PATTERN_NAME=", body, re.M):
            head += "PATTERN_NAME=%s\n" % pattern
        if not re.search(r"\n^BITS=", body, re.M):
            head += "BITS=%s\n" % bits
        if count:
            self.stat("patterns")
        return "OK", head + body

    def _process(self, ptype, pattern, rgb, w_s, h_s, bits_default, source_range, source_max,
                 depth, exact, marks):
        source_max = source_max if source_max in (255, 1023, 4095) else 0
        bg = dim = draw = pos = res = frame = other = bits = ""
        var = {}
        scaling_disabled = False
        if ";" in rgb:
            el = split_perl(rgb, ";") + [""] * 9
            rgb, bg, draw, dim, pos, res, frame, other = el[:8]
            if el[8] != "":
                bits = el[8]
            scaling_disabled = True
        text = self.store.get(pattern, ptype == "TESTTEMPLATERAMDISK")
        if text is None:
            return self.error(), None, None
        rows = lines_of(text)
        first = rows[0] if rows else ""
        if first.startswith("PERMANENT=") and len(rows) > 1:
            first = rows[1]
        if first.rstrip("\n") == "EVALPATTERN=":
            # deviates from pattern.pm:444-455 (eval of the template as Perl): refused
            return self.error(), None, None
        out = ""
        handles = self._handles()
        for line in rows:
            if pattern in handles and handles[pattern] != self._handles()[pattern]:
                # pattern.pm opens the template on the symbolic handle named after it: lut()'s
                # open(LUT)/close(LUT) closes a template called LUT after its first RGB= line,
                # identify's open(IDENTIFY) replaces one called IDENTIFY, and a MACRO's
                # finished get_pattern (open(PATTERN) ... close(PATTERN)) closes one called
                # PATTERN
                break
            if line.startswith("# SCALING=DISABLED"):
                scaling_disabled = True
            if re.match(r"(#|\n|\r)", line):
                continue
            m = re.match(r"VAR=(.*)=(.*)", line)
            if m:
                var[m.group(1)] = self.replace_string(m.group(2), rgb)
                continue
            for k, v in var.items():
                rx = perl_regex(k)
                line = line.replace(k, v) if rx is None else perl_sub(rx[0], rx[1], v, line)
            if line.startswith("FRAME=DYNAMIC"):
                m = re.match(r"FRAME=DYNAMIC\|\|(.*)", line)
                if frame == "" and m:
                    frame = m.group(1)
                frame = frame or DEFAULTS["frame"]
                out += "FRAME=%s\n" % frame
                continue
            if line.startswith("IMAGE=DYNAMIC"):
                m = re.match(r"IMAGE=DYNAMIC\|\|(.*)", line)
                if other == "" and m:
                    other = m.group(1)
                line = "IMAGE=%s\n" % other
            m = re.match(r"IMAGE=(.*)", line)
            if m:
                out += "IMAGE=%s\n" % m.group(1)
                continue
            if line.startswith("TEXT=DYNAMIC"):
                m = re.match(r"TEXT=DYNAMIC\|\|(.*)", line)
                if other == "" and m:
                    other = m.group(1)
                other = other or DEFAULTS["text"]
                line = "TEXT=%s\n" % other
            m = re.match(r"TEXT=(.*)", line)
            if m:
                other = self.replace_string(m.group(1), rgb)
                out += "TEXT=%s\n" % other
                continue
            if line.startswith("DRAW=DYNAMIC"):
                m = re.match(r"DRAW=DYNAMIC\|\|(.*)", line)
                if draw == "" and m:
                    draw = m.group(1)
                draw = draw or DEFAULTS["draw"]
                line = "DRAW=%s\n" % draw
            m = re.match(r"DRAW=(.*)", line)
            if m:
                draw = m.group(1)
                if draw not in DRAWS:
                    return self.error(), None, None
                out += line
                continue
            if line.startswith("DIM=DYNAMIC"):
                m = re.match(r"DIM=DYNAMIC\|\|(.*)", line)
                if dim == "" and m:
                    dim = m.group(1)
                dim = dim or DEFAULTS["dim"]
                line = "DIM=%s\n" % dim
            if line.startswith("DIM=NATIVE") and draw == "IMAGE":
                dim = ""                # identify: no image files here
                self.identify_calls += 1
            m = re.match(r"DIM=(.*)%", line)
            if m:
                p = perl_num(m.group(1)) / 100.0
                if p < 0:               # deviates from pattern.pm:539: Perl dies on sqrt(<0)
                    return self.error(), None, None
                s = math.sqrt(p) if p == p else p
                dim = "%s,%s" % (round_val(s * w_s), round_val(s * h_s))
                line = "DIM=%s\n" % dim
            m = re.match(r"DIM=(.*)", line)
            if m:
                dim = m.group(1)
                if not scaling_disabled:
                    d = split_perl(dim) + ["", ""]
                    dim = "%s,%s" % (round_val(perl_num(d[0]) * 1.0),
                                     round_val(perl_num(d[1]) * 1.0))
                line = "DIM=%s\n" % dim
                nums = split_perl(dim)
                if any(re.search(r"[^0-9]", v) for v in nums):
                    return self.error(), None, None
                nums += ["", ""]
                if perl_num(nums[0]) > w_s or perl_num(nums[1]) > h_s:
                    return self.error(), None, None
                out += line
                continue
            m = re.match(r"MACRO=(.*)", line)
            if m:
                # deviates from pattern.pm:560: PGenerator draws the other template on its own
                # and then overwrites it with this one; its shapes are included here
                if depth >= MACRO_DEPTH:
                    self.error()
                    continue
                sub_marks: dict[str, dict] = {"RGB": {}, "BG": {}}
                err, sub, _ = self._process(ptype, m.group(1), rgb, w_s, h_s, bits_default,
                                            source_range, source_max, depth + 1,
                                            {k: v for k, v in exact.items() if k == 0},
                                            sub_marks)
                if err is None:
                    self.pattern_writes += 1
                    self.stat("patterns")
                    for k, got in sub_marks.items():
                        base = _count(k, out)
                        marks[k].update((base + i, v) for i, v in got.items())
                    out += sub
                continue
            if re.match(r"EVAL=(.*)", line):
                return self.error("eval denied"), None, None
            if line.startswith("POSITION=DYNAMIC"):
                m = re.match(r"POSITION=DYNAMIC\|\|(.*)", line)
                if pos == "" and m:
                    pos = m.group(1)
                pos = pos or DEFAULTS["pos"]
                line = "POSITION=%s\n" % pos
            m = re.match(r"POSITION=(.*)", line)
            if m:
                pos = get_position(dim, draw, m.group(1), scaling_disabled, w_s, h_s)
                if any(re.search(r"[^0-9-]", v) for v in split_perl(m.group(1))) or \
                        any(re.search(r"[^0-9-]", v) for v in split_perl(pos)):
                    return self.error(), None, None
                out += "POSITION=%s\n" % pos
                continue
            if line.startswith("BG=DYNAMIC"):
                m = re.match(r"BG=DYNAMIC\|\|(.*)", line)
                if bg == "" and m:
                    bg = m.group(1)
                bg = bg or DEFAULTS["bg"]
                if 1 in exact:          # the payload's own background
                    marks["BG"][_count("BG", out)] = exact[1]
                out += "BG=%s\n" % bg
                continue
            if line.startswith("RESOLUTION=DYNAMIC"):
                m = re.match(r"RESOLUTION=DYNAMIC\|\|(.*)", line)
                if res == "" and m:
                    res = m.group(1)
                res = res or DEFAULTS["res"]
                out += "RESOLUTION=%s\n" % res
                continue
            if line.startswith("BITS=DYNAMIC"):
                m = re.match(r"BITS=DYNAMIC\|\|(.*)", line)
                if bits == "" and m:
                    bits = m.group(1)
                bits = bits or str(bits_default)
                line = "BITS=%s\n" % bits
            if line.startswith("RGB=DYNAMIC"):
                m = re.match(r"RGB=DYNAMIC\|\|(.*)", line)
                if rgb == "" and m:
                    rgb = m.group(1)
                rgb = rgb or DEFAULTS["rgb"]
                line = "RGB=%s\n" % rgb
                if 0 in exact:          # the payload's own colour
                    marks["RGB"][_count("RGB", out)] = exact[0]
            m = re.match(r"RGB=(.*)", line)
            if m:
                rgb = rgb or DEFAULTS["rgb"]
                if any(re.search(r"[^0-9]", v) for v in split_perl(m.group(1))):
                    return self.error(), None, None
                lut = ",".join((m.group(1).split(",") + ["", "", ""])[:3])   # lut.txt: identity
                self.lut_calls += 1
                if any(re.search(r"[^0-9]", v) for v in split_perl(lut)):
                    return self.error(), None, None
                out += "RGB=%s\n" % lut
                continue
            out += line
        return None, out, bits or str(bits_default)

    @staticmethod
    def replace_string(text, rgb):
        t = time.localtime()
        date = "%s %s %2d %02d:%02d:%02d %d" % (
            ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[t.tm_wday],
            ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
             "Dec")[t.tm_mon - 1], t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, t.tm_year)
        text = text.replace("$RGB", rgb).replace("$DATE", date)
        if "$ETH_INTERFACE" in text:
            text = text.replace("$ETH_INTERFACE", sysinfo.ip_address("eth0"))
        return text


def get_position(dim, draw, pos, scaling_disabled, w_s, h_s):
    """pattern.pm get_position."""
    d = split_perl(dim) + ["", ""]
    w, h = perl_num(d[0]), perl_num(d[1])
    p = split_perl(pos) + ["", "", "", ""]
    x, y, dx, dy = (perl_num(v) for v in p[:4])
    dx = 0 if w == w_s else dx
    dy = 0 if h == h_s else dy
    if draw == "RECTANGLE":
        x = (w_s - w) / 2.0 if x == -1 else x
        y = (h_s - h) / 2.0 if y == -1 else y
    if draw in ("CIRCLE", "TRIANGLE"):
        x = w_s / 2.0 if x == -1 and w != w_s else x
        y = h_s / 2.0 if y == -1 and h != h_s else y
    xs, ys = perl_int_str(x + dx), perl_int_str(y + dy)
    if not scaling_disabled and perl_num(p[0]) != -1:
        xs = round_val(perl_num(xs) * 1.0)
    if not scaling_disabled and perl_num(p[1]) != -1:
        ys = round_val(perl_num(ys) * 1.0)
    return "%s,%s" % (xs, ys)


def source_bits(declared, draw):
    """legacy_external_source_bits."""
    m = re.search(r"(8|10|12)bit$", draw or "", re.I | re.A)
    if m:
        return int(m.group(1))
    if declared in ("8", "10", "12"):
        return int(declared)
    return 8


def dv_triplet(triplet, bits):
    """legacy_external_scale_dv_triplet (daemon.pm:687-698) -> (the triplet as PGenerator
    writes it, its codes at their own depth as shapes.Codes) or (triplet, None) when it is
    passed through unscaled (not exactly three digit fields): read as 12-bit codes."""
    vals = triplet.split(",")
    if triplet == "" or len(vals) != 3 or not all(re.match(r"\d+\n?\Z", v, re.A) for v in vals):
        return triplet, None
    cmax = (1 << bits) - 1
    nums = [dec_int(v.rstrip("\n")) for v in vals]
    scaled = [min(v, 4095) if bits == 12 else 4095 if v >= cmax else v << (12 - bits)
              for v in nums]
    return ",".join(map(str, scaled)), Codes([min(v, cmax) for v in nums], cmax)


def prepare_payload(payload, dv, hcfr):
    """legacy_external_prepare_dv_template_payload (+ the HCFR bits rule) -> (payload as
    get_pattern gets it, {field: own-depth codes} for the triplets PGenerator scaled to
    12-bit, or None outside Dolby Vision). Those are drawn at their own depth (deviation:
    PGenerator's v<<(12-b), daemon.pm:670-690); everything else in the file is read as
    12-bit codes under SOURCE_MAX=4095, as in PGenerator."""
    exact = None
    if (dv or hcfr) and payload == "0":
        payload = ""                # split(/;/, $payload || "", -1): "0" is false
    if dv:
        f = payload.split(";") + [""] * 9
        depth = source_bits(f[8], f[2])
        exact = {}
        for i in (0, 1):
            f[i], codes = dv_triplet(f[i], depth)
            if codes is not None:
                exact[i] = codes
        f[2] = re.sub(r"(?:8|10|12)bit$", "", f[2], flags=re.I | re.A)
        f[8] = "8"
        n = max(9, len(payload.split(";")))
        payload = ";".join(f[:n])
    if hcfr:
        f = payload.split(";") + [""] * 9
        f[8] = "8"
        payload = ";".join(f[:9])
    return payload, exact
