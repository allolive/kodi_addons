"""HEVC CABAC arithmetic encoder (ITU-T H.265 9.3.4), after the HM reference."""

RANGE_LPS = (
    (128, 176, 208, 240), (128, 167, 197, 227), (128, 158, 187, 216), (123, 150, 178, 205),
    (116, 142, 169, 195), (111, 135, 160, 185), (105, 128, 152, 175), (100, 122, 144, 166),
    (95, 116, 137, 158), (90, 110, 130, 150), (85, 104, 123, 142), (81, 99, 117, 135),
    (77, 94, 111, 128), (73, 89, 105, 122), (69, 85, 100, 116), (66, 80, 95, 110),
    (62, 76, 90, 104), (59, 72, 86, 99), (56, 69, 81, 94), (53, 65, 77, 89),
    (51, 62, 73, 85), (48, 59, 69, 80), (46, 56, 66, 76), (43, 53, 63, 72),
    (41, 50, 59, 69), (39, 48, 56, 65), (37, 45, 54, 62), (35, 43, 51, 59),
    (33, 41, 48, 56), (32, 39, 46, 53), (30, 37, 43, 50), (29, 35, 41, 48),
    (27, 33, 39, 45), (26, 31, 37, 43), (24, 30, 35, 41), (23, 28, 33, 39),
    (22, 27, 32, 37), (21, 26, 30, 35), (20, 24, 29, 33), (19, 23, 27, 31),
    (18, 22, 26, 30), (17, 21, 25, 28), (16, 20, 23, 27), (15, 19, 22, 25),
    (14, 18, 21, 24), (14, 17, 20, 23), (13, 16, 19, 22), (12, 15, 18, 21),
    (12, 14, 17, 20), (11, 14, 16, 19), (11, 13, 15, 18), (10, 12, 15, 17),
    (10, 12, 14, 16), (9, 11, 13, 15), (9, 11, 12, 14), (8, 10, 12, 14),
    (8, 9, 11, 13), (7, 9, 11, 12), (7, 9, 10, 12), (7, 8, 10, 11),
    (6, 8, 9, 11), (6, 7, 9, 10), (6, 7, 8, 9), (2, 2, 2, 2),
)

TRANS_LPS = (
    0, 0, 1, 2, 2, 4, 4, 5, 6, 7, 8, 9, 9, 11, 11, 12, 13, 13, 15, 15, 16, 16, 18, 18,
    19, 19, 21, 21, 22, 22, 23, 24, 24, 25, 26, 26, 27, 27, 28, 29, 29, 30, 30, 30, 31, 32,
    32, 33, 33, 33, 34, 34, 35, 35, 35, 36, 36, 36, 37, 37, 37, 38, 38, 63,
)


class Context:
    __slots__ = ("state", "mps")

    def __init__(self, init_value, qp):
        slope = (init_value >> 4) * 5 - 45
        offset = ((init_value & 15) << 3) - 16
        pre = min(max(1, ((slope * min(max(qp, 0), 51)) >> 4) + offset), 126)
        self.mps = 1 if pre > 63 else 0
        self.state = pre - 64 if self.mps else 63 - pre


class CabacEncoder:
    def __init__(self, writer):
        self.w = writer
        self.start()

    def start(self):
        self.low = 0
        self.range = 510
        self.bits_left = 23
        self.num_buffered = 0
        self.buffered_byte = 0xFF

    def encode(self, ctx, bin_val):
        lps = RANGE_LPS[ctx.state][(self.range >> 6) & 3]
        self.range -= lps
        if bin_val != ctx.mps:
            num_bits = 9 - lps.bit_length()
            self.low = (self.low + self.range) << num_bits
            self.range = lps << num_bits
            if ctx.state == 0:
                ctx.mps = 1 - ctx.mps
            ctx.state = TRANS_LPS[ctx.state]
            self.bits_left -= num_bits
        else:
            if ctx.state < 62:
                ctx.state += 1
            if self.range >= 256:
                return
            self.low <<= 1
            self.range <<= 1
            self.bits_left -= 1
        if self.bits_left < 12:
            self._write_out()

    def encode_bypass(self, bin_val):
        self.low <<= 1
        if bin_val:
            self.low += self.range
        self.bits_left -= 1
        if self.bits_left < 12:
            self._write_out()

    def encode_bypass_bits(self, value, n):
        """n bypass bins, MSB first, at most 8 per renormalisation (HM encodeBinsEP)."""
        while n > 8:
            n -= 8
            self.low = (self.low << 8) + self.range * ((value >> n) & 0xFF)
            self.bits_left -= 8
            if self.bits_left < 12:
                self._write_out()
        self.low = (self.low << n) + self.range * (value & ((1 << n) - 1))
        self.bits_left -= n
        if self.bits_left < 12:
            self._write_out()

    def encode_terminate(self, bin_val):
        self.range -= 2
        if bin_val:
            self.low += self.range
            self.low <<= 7
            self.range = 2 << 7
            self.bits_left -= 7
        elif self.range >= 256:
            return
        else:
            self.low <<= 1
            self.range <<= 1
            self.bits_left -= 1
        if self.bits_left < 12:
            self._write_out()

    def _write_out(self):
        lead_byte = self.low >> (24 - self.bits_left)
        self.bits_left += 8
        self.low &= 0xFFFFFFFF >> self.bits_left
        if lead_byte == 0xFF:
            self.num_buffered += 1
        elif self.num_buffered > 0:
            carry = lead_byte >> 8
            byte = self.buffered_byte + carry
            self.buffered_byte = lead_byte & 0xFF
            self.w.u(byte, 8)
            byte = (0xFF + carry) & 0xFF
            while self.num_buffered > 1:
                self.w.u(byte, 8)
                self.num_buffered -= 1
        else:
            self.num_buffered = 1
            self.buffered_byte = lead_byte

    def finish(self):
        if (self.low >> (32 - self.bits_left)) & 1:
            self.w.u(self.buffered_byte + 1, 8)
            while self.num_buffered > 1:
                self.w.u(0x00, 8)
                self.num_buffered -= 1
            self.low -= 1 << (32 - self.bits_left)
        else:
            if self.num_buffered > 0:
                self.w.u(self.buffered_byte, 8)
            while self.num_buffered > 1:
                self.w.u(0xFF, 8)
                self.num_buffered -= 1
        self.w.u(self.low >> 8, 24 - self.bits_left)
