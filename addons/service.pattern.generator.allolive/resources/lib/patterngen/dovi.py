"""Dolby Vision profile 5 and 8.1 RPU and configuration record writer.

The layout follows dovi_tool's writer (header, identity polynomial mapping,
VDR DM data with CM v2.9 extension blocks) and is verified byte-exact against
`dovi_tool generate` output.
"""

import math

from .bitstream import BitWriter, hevc_nal
from .hevc import NAL_UNSPEC62
from .patterns import pq_decode, pq_encode


def _crc32_mpeg2(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else (crc << 1)
            crc &= 0xFFFFFFFF
    return crc


# Profile 8.1 composer/DM constants (BT.2020 PQ YCbCr base layer).
_YCC_TO_RGB = (9574, 0, 13802, 9574, -1540, -5348, 9574, 17610, 0)
_YCC_TO_RGB_OFFSET = (16777216, 134217728, 134217728)
_RGB_TO_LMS = (7222, 8771, 390, 2654, 12430, 1300, 0, 422, 15962)

# Profile 5 (full-range IPT-PQ-c2 base layer), as dovi_tool writes it: IPT -> L'M'S'
# and the 2 % crosstalk removal (LMS as coded -> LMS)
_IPT_TO_LMS = (8192, 799, 1681, 8192, -933, 1091, 8192, 267, -5545)
_IPT_OFFSET = (0, 134217728, 134217728)
_LMS_DECROSSTALK = (17081, -349, -349, -349, 17081, -349, -349, -349, 17081)

LOG2_DENOM = 23


def _inv3(m):
    a, b, c, d, e, f, g, h, i = m
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return ((e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det,
            (f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det,
            (d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det)


def _mul(m, v):
    return (m[0] * v[0] + m[1] * v[1] + m[2] * v[2], m[3] * v[0] + m[4] * v[1] + m[5] * v[2],
            m[6] * v[0] + m[7] * v[1] + m[8] * v[2])


_RGB2LMS = tuple(c / 16384.0 for c in _RGB_TO_LMS)
_CROSSTALK = _inv3(tuple(c / 16384.0 for c in _LMS_DECROSSTALK))
_LMS2IPT = _inv3(tuple(c / 8192.0 for c in _IPT_TO_LMS))


def ipt10(r, g, b, literal=False):
    """10-bit full-range IPT-PQ-c2 code values of a PQ BT.2020 R'G'B' colour (the
    normalised limited-range reading of the client's codes, 0..1, may exceed), inverting
    the profile 5 DM data above - the conversion the Amlogic core applies to a profile
    8.1 picture before HDMI.

    literal: Relative mapping. An LG in that mode (its calibration mode) reads the
    tunnel as limited range, 64 black and 940 white, so the client's code must arrive as
    is: code 108 is I 108, not the PQ picture of 5 % (I 51, which it shows as black).
    """
    if literal:
        rgb = tuple((64 + 876 * v) / 1024.0 for v in (r, g, b))
    else:
        rgb = (r, g, b)
    lin = tuple(pq_decode(v) / 10000.0 for v in rgb)
    lms = _mul(_CROSSTALK, _mul(_RGB2LMS, lin))
    i, p, t = _mul(_LMS2IPT, tuple(pq_encode(max(0.0, v) * 10000.0) for v in lms))
    return tuple(max(0, min(1023, int(math.floor(v * 1024 + 0.5)))) for v in (i, p + 0.5, t + 0.5))


def _clip(v, bits):
    return max(0, min((1 << bits) - 1, int(v)))


def rpu(l1, source_min_pq, source_max_pq, l6=None, run_mode=None, scene_refresh=True,
        l5=False, profile=5):
    """RPU payload (starting with the 0x19 prefix, before emulation prevention).

    l1 = (min_pq, max_pq, avg_pq) 12-bit PQ codes.
    l6 = (max_mdl, min_mdl, max_cll, max_fall), min_mdl in 0.0001 nits.
    run_mode = level 255 dm_run_mode (0 perceptual, 1 absolute, 2 relative) or None.
    l5 = whether to write the (all-zero) level 5 active area.
    profile = 5 (full-range IPT, what the add-on sends) or 8 (8.1, BT.2020 limited-range
    Y'CbCr base layer: kept because the tests check it byte for byte against dovi_tool).
    """
    p5 = profile == 5
    w = BitWriter()
    w.u(0x19, 8)
    # rpu_data_header
    w.u(2, 6)            # rpu_type
    w.u(18, 11)          # rpu_format
    w.u(0 if p5 else 1, 4)  # vdr_rpu_profile
    w.u(0, 4)            # vdr_rpu_level
    w.u(1, 1)            # vdr_seq_info_present
    w.u(0, 1)            # chroma_resampling_explicit_filter
    w.u(0, 2)            # coefficient_data_type
    w.ue(LOG2_DENOM)
    w.u(1, 2)            # vdr_rpu_normalized_idc
    w.u(1 if p5 else 0, 1)  # bl_video_full_range
    w.ue(2)              # bl_bit_depth_minus8
    w.ue(2)              # el_bit_depth_minus8
    w.ue(4)              # vdr_bit_depth_minus8
    w.u(0, 1)            # spatial_resampling_filter
    w.u(0, 3)            # reserved_zero_3bits
    w.u(0, 1)            # el_spatial_resampling_filter
    w.u(1, 1)            # disable_residual
    w.u(1, 1)            # vdr_dm_metadata_present
    w.u(0, 1)            # use_prev_vdr_rpu
    # rpu_data_mapping: identity polynomial on the full 10-bit range
    w.ue(0)              # vdr_rpu_id
    w.ue(0)              # mapping_color_space
    w.ue(0)              # mapping_chroma_format_idc
    for _ in range(3):
        w.ue(0)          # num_pivots_minus2
        w.u(0, 10)
        w.u(1023, 10)
    w.ue(0)              # num_x_partitions_minus1
    w.ue(0)              # num_y_partitions_minus1
    for _ in range(3):
        w.ue(0)          # mapping_idc polynomial
        w.ue(0)          # poly_order_minus1
        w.u(0, 1)        # linear_interp_flag
        for coef_int in (0, 1):
            w.se(coef_int)
            w.u(0, LOG2_DENOM)
    # vdr_dm_data
    w.ue(0)              # affected_dm_metadata_id
    w.ue(0)              # current_dm_metadata_id
    w.ue(1 if scene_refresh else 0)
    for c in _IPT_TO_LMS if p5 else _YCC_TO_RGB:
        w.s(c, 16)
    for c in _IPT_OFFSET if p5 else _YCC_TO_RGB_OFFSET:
        w.u(c, 32)
    for c in _LMS_DECROSSTALK if p5 else _RGB_TO_LMS:
        w.s(c, 16)
    w.u(65535, 16)       # signal_eotf
    w.u(0, 16)
    w.u(0, 16)
    w.u(0, 32)
    w.u(12, 5)           # signal_bit_depth
    w.u(2 if p5 else 0, 2)  # signal_color_space (2: IPT)
    w.u(0, 2)            # signal_chroma_format
    w.u(1, 2)            # signal_full_range_flag
    w.u(_clip(source_min_pq, 12), 12)
    w.u(_clip(source_max_pq, 12), 12)
    w.u(42, 10)          # source_diagonal
    # CM v2.9 extension blocks, sorted by level: (level, [(value, bits), ...])
    blocks = [(1, [(_clip(v, 12), 12) for v in l1])]
    if l5:
        blocks.append((5, [(0, 13)] * 4))
    if l6:
        blocks.append((6, [(_clip(v, 16), 16) for v in l6]))
    if run_mode is not None:
        blocks.append((255, [(run_mode, 8)] + [(0, 8)] * 5))
    w.ue(len(blocks))
    w.align_zero()
    for level, fields in blocks:
        bits = sum(n for _, n in fields)
        w.ue((bits + 7) // 8)
        w.u(level, 8)
        for v, n in fields:
            w.u(v, n)
        w.u(0, -bits % 8)
    w.align_zero()
    body = w.getvalue()
    crc = _crc32_mpeg2(body[1:])
    return body + crc.to_bytes(4, "big") + b"\x80"


def rpu_nal(payload):
    """HEVC UNSPEC62 NAL unit carrying the RPU."""
    return hevc_nal(NAL_UNSPEC62, payload)


def dv_level(width, height, fps):
    """Dolby Vision level from pixel rate (dv levels 1-13)."""
    rate = width * height * fps
    for level, max_w, max_rate in ((1, 1280, 22118400), (2, 1280, 27648000),
                                   (3, 1920, 49766400), (4, 2560, 62208000),
                                   (5, 3840, 124416000), (6, 3840, 199065600),
                                   (7, 3840, 248832000), (8, 3840, 398131200),
                                   (9, 3840, 497664000), (10, 3840, 995328000),
                                   (11, 7680, 995328000), (12, 7680, 1990656000),
                                   (13, 7680, 3981312000)):
        if width <= max_w and rate <= max_rate:
            return level
    return 13


def config_record(level, profile=5, compat_id=0):
    """DOVIDecoderConfigurationRecord (24 bytes), dvvC/dvcC payload."""
    w = BitWriter()
    w.u(1, 8)            # dv_version_major
    w.u(0, 8)            # dv_version_minor
    w.u(profile, 7)
    w.u(level, 6)
    w.u(1, 1)            # rpu_present
    w.u(0, 1)            # el_present
    w.u(1, 1)            # bl_present
    w.u(compat_id, 4)
    w.u(0, 28)
    for _ in range(4):
        w.u(0, 32)
    return w.getvalue()
