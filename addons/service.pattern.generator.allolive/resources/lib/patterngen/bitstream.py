"""Bit writer, Exp-Golomb coding and NAL unit packaging."""

import re


class BitWriter:
    def __init__(self):
        self._out = bytearray()
        self._acc = 0
        self._n = 0

    def u(self, value, bits):
        if bits == 0:
            return
        if value < 0 or value >> bits:
            raise ValueError("value %d does not fit in %d bits" % (value, bits))
        self._acc = (self._acc << bits) | value
        self._n += bits
        while self._n >= 8:
            self._n -= 8
            self._out.append((self._acc >> self._n) & 0xFF)
        self._acc &= (1 << self._n) - 1

    def flag(self, value):
        self.u(1 if value else 0, 1)

    def ue(self, value):
        value += 1
        length = value.bit_length()
        self.u(0, length - 1)
        self.u(value, length)

    def se(self, value):
        self.ue(2 * value - 1 if value > 0 else -2 * value)

    def s(self, value, bits):
        self.u(value & ((1 << bits) - 1), bits)

    def raw(self, data):
        if self._n:
            for b in data:
                self.u(b, 8)
        else:
            self._out += data

    def align_zero(self):
        if self._n:
            self.u(0, 8 - self._n)

    def trailing_bits(self):
        self.u(1, 1)
        self.align_zero()

    def getvalue(self):
        if self._n:
            raise ValueError("bit writer not byte aligned")
        return bytes(self._out)


_EPB = re.compile(b"\x00\x00(?=[\x00-\x03])")


def escape(rbsp):
    """Insert emulation_prevention_three_byte where the RBSP needs it."""
    out = _EPB.sub(b"\x00\x00\x03", rbsp)
    if out.endswith(b"\x00"):
        out += b"\x03"
    return out


def hevc_nal(nal_type, rbsp):
    header = bytes(((nal_type << 1) & 0x7E, 1))
    return header + escape(rbsp)
