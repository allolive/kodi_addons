"""Lossless intra coding of a key frame with 32x32 coding units.

Every CU is predicted with DC, horizontal or vertical intra prediction (no
reference filtering or boundary filters apply at 32x32 luma / 16x16 chroma).
When one of them reproduces the CU exactly, no residual is sent. Otherwise
the CU is coded with cu_transquant_bypass and the spatial residual goes
through the regular residual_coding() syntax, which is lossless.

PCM would be simpler but the Amlogic HEVC firmware rejects PCM coding units.
Syntax and context selection mirror ffmpeg's hls_residual_coding().
"""

from array import array
from operator import eq, itemgetter, sub

from .cabac import Context

MODE_PLANAR, MODE_DC, MODE_HOR, MODE_VER = 0, 1, 10, 26
MODES = (MODE_DC, MODE_HOR, MODE_VER)

# initType 0 (I slice) initValues
_LAST = (110, 110, 124, 125, 140, 153, 125, 127, 140, 109, 111, 143, 127, 111, 79, 108, 123, 63)
_CSBF = (91, 171, 134, 141)
_SIG = (111, 111, 125, 110, 110, 94, 124, 108, 124, 107, 125, 141, 179, 153, 125, 107, 125,
        141, 179, 153, 125, 107, 125, 141, 179, 153, 125, 140, 139, 182, 182, 152, 136, 152,
        136, 153, 136, 139, 111, 136, 139, 111, 141, 111)
_GT1 = (140, 92, 137, 138, 140, 152, 138, 139, 153, 74, 149, 92, 139, 107, 122, 152,
        140, 179, 166, 182, 140, 227, 122, 197)
_GT2 = (138, 153, 136, 167, 152, 152)
# cu_transquant_bypass, part_mode, prev_intra_luma_pred, intra_chroma_pred_mode,
# cbf_luma and cbf_cb/cbf_cr at trafoDepth 0
_CU = (154, 184, 184, 63, 141, 94)

# sig_coeff_flag context by 4x4 diagonal scan position, rows: prevCsbf 0..3
SIG_MAP = (
    (2, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    (2, 1, 2, 0, 1, 2, 0, 0, 1, 2, 0, 0, 1, 0, 0, 0),
    (2, 2, 1, 2, 1, 0, 2, 1, 0, 0, 1, 0, 0, 0, 0, 0),
    (2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2),
)

GROUP_IDX = (0, 1, 2, 3, 4, 4, 5, 5, 6, 6, 6, 6, 7, 7, 7, 7,
             8, 8, 8, 8, 8, 8, 8, 8, 9, 9, 9, 9, 9, 9, 9, 9)
MIN_IN_GROUP = (0, 1, 2, 3, 4, 6, 8, 12, 16, 24)


def _diag_scan(size):
    out = []
    for d in range(2 * size - 1):
        out += [(d - y, y) for y in range(min(d, size - 1), max(-1, d - size), -1)]
    return out


DIAG4 = _diag_scan(4)


def _tb_scan(log2):
    """Per 4x4 sub-block in scan order: (xs, ys, getter of its 16 samples in scan order)."""
    size = 1 << log2
    return [(xs, ys, itemgetter(*[((ys << 2) + y) * size + (xs << 2) + x for x, y in DIAG4]))
            for xs, ys in _diag_scan(size >> 2)]


# Only 32x32 luma and 16x16 chroma transform blocks occur (diagonal scan).
_LOG2 = (5, 4, 4)
_SCAN = {log2: _tb_scan(log2) for log2 in (4, 5)}


class IntraCoder:
    def __init__(self, enc, qp):
        def contexts(values):
            return [Context(v, qp) for v in values]
        self.enc = enc
        self.last_x, self.last_y = contexts(_LAST), contexts(_LAST)
        self.csbf, self.gt1, self.gt2 = contexts(_CSBF), contexts(_GT1), contexts(_GT2)
        (self.bypass, self.part_mode, self.prev_intra, self.chroma_mode, self.cbf_luma,
         self.cbf_chroma) = contexts(_CU)
        # sig_coeff_flag contexts [chroma][sub-block > 0][prevCsbf] -> 16 by scan position
        sig = contexts(_SIG)
        self.sig = []
        for chroma in (0, 1):
            per_sub = []
            for later in (0, 1):
                off = 27 + 12 if chroma else 21 + 3 * later
                rows = [[sig[v + off] for v in row] for row in SIG_MAP]
                if not later:
                    for row in rows:
                        row[0] = sig[27 if chroma else 0]
                per_sub.append(rows)
            self.sig.append(per_sub)

    # -- residual_coding() --------------------------------------------------
    def _last_prefix(self, ctxs, value, log2, cidx):
        if cidx == 0:
            off, shift = 3 * (log2 - 2) + ((log2 - 1) >> 2), (log2 + 1) >> 2
        else:
            off, shift = 15, log2 - 2
        group = GROUP_IDX[value]
        for i in range(group):
            self.enc.encode(ctxs[off + (i >> shift)], 1)
        if group < (log2 << 1) - 1:
            self.enc.encode(ctxs[off + (group >> shift)], 0)
        return group

    def _last_suffix(self, value, group):
        if group > 3:
            self.enc.encode_bypass_bits(value - MIN_IN_GROUP[group], (group >> 1) - 1)

    def _remaining(self, value, rice):
        if value < (3 << rice):
            prefix = value >> rice
            self.enc.encode_bypass_bits(((2 << prefix) - 2) << rice | (value & ((1 << rice) - 1)),
                                        prefix + 1 + rice)
            return
        k = 0
        while value >= (((2 << k) + 2) << rice):
            k += 1
        self.enc.encode_bypass_bits((1 << (4 + k)) - 2, 4 + k)
        self.enc.encode_bypass_bits(value - (((1 << k) + 2) << rice), k + rice)

    def residual(self, res, cidx):
        """res: flat row-major residual of the TB, at least one non-zero."""
        enc = self.enc
        encode, bypass = enc.encode, enc.encode_bypass
        log2 = _LOG2[cidx]
        scan = _SCAN[log2]
        chroma = 1 if cidx else 0
        for last_sub in range(len(scan) - 1, -1, -1):
            vals = scan[last_sub][2](res)
            if any(vals):
                break
        last_pos = max(n for n in range(16) if vals[n])
        xs, ys, _ = scan[last_sub]
        last_x = (xs << 2) + DIAG4[last_pos][0]
        last_y = (ys << 2) + DIAG4[last_pos][1]
        gx = self._last_prefix(self.last_x, last_x, log2, cidx)
        gy = self._last_prefix(self.last_y, last_y, log2, cidx)
        self._last_suffix(last_x, gx)
        self._last_suffix(last_y, gy)

        nsb = 1 << (log2 - 2)
        stride = nsb + 1                    # zero border right and below
        csbf = [0] * (stride * stride)
        greater1_ctx = 1
        gt1_off, gt2_off = 16 * chroma, 4 * chroma
        for i in range(last_sub, -1, -1):
            xs, ys, get = scan[i]
            if i != last_sub:
                vals = get(res)
            c = ys * stride + xs
            right, below = csbf[c + 1], csbf[c + stride]
            implicit = 0 < i < last_sub
            if implicit:
                flag = 1 if any(vals) else 0
                encode(self.csbf[(1 if right or below else 0) + 2 * chroma], flag)
                if not flag:
                    continue
            csbf[c] = 1
            ctxs = self.sig[chroma][1 if i else 0][right + 2 * below]
            sig_list = []           # scan positions of non-zero coefficients, high to low
            if i == last_sub:
                sig_list.append(last_pos)
                start = last_pos - 1
            else:
                start = 15
            for n in range(start, -1, -1):
                if n == 0 and implicit and not sig_list:
                    sig_list.append(0)      # inferred
                    break
                v = vals[n]
                encode(ctxs[n], 1 if v else 0)
                if v:
                    sig_list.append(n)
            if not sig_list:
                continue
            ctx_set = 2 if (i and not chroma) else 0
            if i != last_sub and greater1_ctx == 0:
                ctx_set += 1
            greater1_ctx = 1
            absl = [abs(vals[n]) for n in sig_list]
            first_g1 = -1
            for m in range(min(8, len(absl))):
                g1 = absl[m] > 1
                encode(self.gt1[(ctx_set << 2) + greater1_ctx + gt1_off], g1)
                if g1:
                    greater1_ctx = 0
                    if first_g1 < 0:
                        first_g1 = m
                elif 0 < greater1_ctx < 3:
                    greater1_ctx += 1
            if first_g1 >= 0:
                encode(self.gt2[ctx_set + gt2_off], absl[first_g1] > 2)
            for n in sig_list:
                bypass(vals[n] < 0)
            rice = 0
            for m, a in enumerate(absl):
                base = (3 if m == first_g1 else 2) if m < 8 else 1
                if a >= base:
                    self._remaining(a - base, rice)
                    if a > (3 << rice):
                        rice = min(rice + 1, 4)

    # -- coding_unit() -----------------------------------------------------
    def coding_unit(self, mode, cand_a, residuals):
        """residuals: (luma, cb, cr) flat residuals or None each."""
        enc = self.enc
        enc.encode(self.bypass, 1)                  # cu_transquant_bypass_flag
        enc.encode(self.part_mode, 1)               # PART_2Nx2N
        # candB is always DC: the CU above lies in another CTB.
        if cand_a == MODE_DC:
            mpm = (MODE_PLANAR, MODE_DC, MODE_VER)
        else:
            mpm = (cand_a, MODE_DC, MODE_PLANAR)
        if mode in mpm:
            idx = mpm.index(mode)
            enc.encode(self.prev_intra, 1)
            if idx:
                enc.encode_bypass_bits(2 | (idx - 1), 2)
            else:
                enc.encode_bypass(0)
        else:
            enc.encode(self.prev_intra, 0)
            enc.encode_bypass_bits(mode - sum(1 for c in mpm if c < mode), 5)
        enc.encode(self.chroma_mode, 0)             # DM
        luma, cb, cr = residuals
        enc.encode(self.cbf_chroma, cb is not None)
        enc.encode(self.cbf_chroma, cr is not None)
        enc.encode(self.cbf_luma, luma is not None)
        for cidx, res in enumerate(residuals):
            if res is not None:
                self.residual(res, cidx)


def refs(plane, stride, x, y, n, has_left, has_top):
    """(left, top) reference arrays for DC/H/V after H.265 substitution."""
    if has_left:
        o = y * stride + x - 1
        left = plane[o:o + n * stride:stride]
    if has_top:
        o = (y - 1) * stride + x
        top = plane[o:o + n]
    if has_left and not has_top:
        top = array("H", [left[0]]) * n
    elif has_top and not has_left:
        left = array("H", [top[0]]) * n
    elif not has_left and not has_top:
        left = top = array("H", [512]) * n
    return left, top


def block(plane, stride, x, y, n, uniform=False):
    """Flat row-major copy of an n x n block; uniform: all its rows are equal."""
    o = y * stride + x
    if uniform:
        return plane[o:o + n] * n
    out = array("H")
    for row in range(o, o + n * stride, stride):
        out += plane[row:row + n]
    return out


def prediction(mode, n, left, top):
    """Flat DC/H/V intra prediction of an n x n block."""
    if mode == MODE_DC:
        return array("H", [(sum(left) + sum(top) + n) >> n.bit_length()]) * (n * n)
    if mode == MODE_VER:
        return top * n
    out = array("H")
    for v in left:
        out += array("H", [v]) * n
    return out


def choose(blocks):
    """Mode and (luma, cb, cr) residuals for a CU of (src, n, left, top) blocks.

    The first mode that predicts the CU exactly wins; otherwise the one leaving
    the fewest non-zero residual samples.
    """
    for mode in MODES:
        if all(prediction(mode, n, left, top) == src for src, n, left, top in blocks):
            return mode, (None, None, None)
    best = None
    for mode in MODES:
        preds = [prediction(mode, n, left, top) for _, n, left, top in blocks]
        cost = sum(len(b[0]) - sum(map(eq, b[0], p)) for b, p in zip(blocks, preds, strict=True))
        if best is None or cost < best[0]:
            best = cost, mode, preds
    assert best is not None       # MODES is not empty
    _, mode, preds = best
    return mode, tuple(None if b[0] == p else list(map(sub, b[0], p))
                       for b, p in zip(blocks, preds, strict=True))
