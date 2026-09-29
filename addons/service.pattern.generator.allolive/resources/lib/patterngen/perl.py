"""Perl and C semantics that PGenerator's parsing relies on, for the protocol ports.

PGenerator's regexes run on recv() byte strings: \\s \\w \\b \\d and /i are ASCII only.
"""

import math
import re

_NUM = re.compile(r"[ \t\n\r\f\v]*([+-]?)(?:(1\.#inf|inf)|(1\.#(?:ind|qnan|snan)|q?nan|snan)"
                  r"|((?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))", re.I | re.A)
_IV = re.compile(r"[ \t\n\r\f\v]*([+-]?\d+)(?![.\d]|[eE][+-]?\d)", re.A)
_UC = {c: c - 32 for c in range(97, 123)}


def dec_int(digits):
    """int() of an optionally signed decimal string of any length: Perl and C read any number
    of leading zeros, Python refuses more than 4300 digits (a longer value is 10**4000)."""
    sign = -1 if digits[:1] == "-" else 1
    d = digits.lstrip("+-").lstrip("0") or "0"
    return sign * (int(d) if len(d) <= 4300 else 10 ** 4000)


def perl_num(s):
    """Perl's string -> number: the leading number (Inf/NaN forms, overflow to Inf), else 0."""
    if isinstance(s, (int, float)):
        return s
    m = _NUM.match(s or "")
    if not m:
        return 0.0
    if m.group(3):
        return float("nan")
    v = float("inf") if m.group(2) else float(m.group(4))
    return -v if m.group(1) == "-" else v


def perl_int(s):
    """int($s): a decimal integer string within IV/UV is exact (no trip through a double)."""
    if isinstance(s, str):
        m = _IV.match(s)
        if m and -2 ** 63 <= dec_int(m.group(1)) < 2 ** 64:
            return dec_int(m.group(1))
    v = perl_num(s)
    return int(v) if math.isfinite(v) else v       # Perl's int(Inf) stays Inf


def perl_str(v):
    if isinstance(v, float):
        if not math.isfinite(v):
            return "NaN" if v != v else ("Inf" if v > 0 else "-Inf")
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else "%.15g" % v
    if isinstance(v, int) and (v >= 1 << 64 or v < -(1 << 63)):
        return "%.15g" % v          # outside IV/UV Perl's int() stays an NV
    return str(v)


def perl_int_str(v):
    """int($v) as Perl prints it: an IV/UV as digits, otherwise the NV ("1e+20", "Inf").
    A decimal string within IV/UV is exact; a number stays an NV unless it is strictly
    inside the IV/UV range once a double (pp_int), so -2**63 - 1 prints as -9.22e+18."""
    if isinstance(v, str):
        m = _IV.match(v)
        if m and -2 ** 63 <= dec_int(m.group(1)) < 2 ** 64:
            return str(dec_int(m.group(1)))
        v = perl_num(v)
    if not math.isfinite(v):
        return perl_str(float(v))
    if 0 <= v < 2.0 ** 64 or -2.0 ** 63 < v < 0:
        return str(math.trunc(v))
    return perl_str(float(math.floor(v) if v >= 0 else math.ceil(v)))


def perl_uc(s):
    """uc() on a byte string: ASCII letters only."""
    return s.translate(_UC)


def split_perl(text, sep=","):
    """split($sep, $text) as Perl does: trailing empty fields dropped."""
    el = text.split(sep)
    while el and el[-1] == "":
        el.pop()
    return el


def c_atoi(s):
    """C atoi() (glibc: (int)strtol): leading whitespace, a sign, then digits only."""
    m = re.match(r"[ \t\n\v\f\r]*([+-]?\d+)", s or "", re.A)
    if not m:
        return 0
    v = max(-2 ** 63, min(2 ** 63 - 1, dec_int(m.group(1)))) & 0xFFFFFFFF
    return v - 2 ** 32 if v >= 2 ** 31 else v


def lead_digits(s):
    """drm_override.c read_config: the leading ASCII digits only (no space, sign or exponent)."""
    m = re.match(r"\d+", s or "", re.A)
    return dec_int(m.group(0)) if m else 0
