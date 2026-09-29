"""Minimal HEVC Main10 encoder for synthetic test patterns.

Every picture is split into 32x32 coding units (CTB = min CB = 32). The key
frame reproduces the source picture bit-exactly (see intra.py): each CU uses
the DC, horizontal or vertical intra prediction that reproduces it, and only
CUs no prediction matches carry a lossless (transquant bypass) residual.
Deblocking and SAO are off.
All following pictures are P pictures made only of skipped CUs (zero motion,
single merge candidate), i.e. exact copies of the key frame.
"""

from array import array
from functools import lru_cache

from . import intra
from .bitstream import BitWriter, escape, hevc_nal
from .cabac import CabacEncoder, Context

CTB_LOG2 = 5
CTB = 1 << CTB_LOG2
BIT_DEPTH = 10

NAL_TRAIL_R = 1
NAL_IDR_W_RADL = 19
NAL_VPS = 32
NAL_SPS = 33
NAL_PPS = 34
NAL_SEI_PREFIX = 39
NAL_UNSPEC62 = 62

SLICE_QP = 26

# colour_primaries / transfer_characteristics / matrix_coeffs (H.273)
PRIMARIES_BT709, PRIMARIES_BT2020 = 1, 9
TRANSFER_BT709, TRANSFER_PQ, TRANSFER_HLG = 1, 16, 18
MATRIX_BT709, MATRIX_BT2020_NCL = 1, 9


def padded(size):
    return (size + CTB - 1) // CTB * CTB


class Picture:
    """Y'CbCr 4:2:0 10-bit picture padded to a multiple of the CTB size."""

    def __init__(self, width, height, fill=(64, 512, 512)):
        if width % 2 or height % 2:
            raise ValueError("4:2:0 needs even dimensions")
        self.width = width
        self.height = height
        self.cw = padded(width)
        self.ch = padded(height)
        self.planes = [
            array("H", [fill[0]]) * (self.cw * self.ch),
            array("H", [fill[1]]) * (self.cw * self.ch // 4),
            array("H", [fill[2]]) * (self.cw * self.ch // 4),
        ]

    def fill_rect(self, x, y, w, h, ycc):
        """Fill a rectangle in luma coordinates; edges are rounded to even."""
        x0, y0 = max(0, x) & ~1, max(0, y) & ~1
        x1 = min(self.width, x + w + 1) & ~1
        y1 = min(self.height, y + h + 1) & ~1
        if x1 <= x0 or y1 <= y0:
            return
        # Extend into the padding so the cropped-away area is flat too.
        if x1 == self.width:
            x1 = self.cw
        if y1 == self.height:
            y1 = self.ch
        for plane, scale, value in zip(self.planes, (1, 2, 2), ycc, strict=True):
            stride = self.cw // scale
            px0, px1 = x0 // scale, x1 // scale
            run = array("H", [value]) * (px1 - px0)
            for py in range(y0 // scale, y1 // scale):
                o = py * stride
                plane[o + px0:o + px1] = run


class _Params:
    def __init__(self, width, height, fps_num, fps_den, colour, level_idc, full_range=False):
        self.width = width
        self.height = height
        self.cw = padded(width)
        self.ch = padded(height)
        self.fps_num = fps_num
        self.fps_den = fps_den
        self.primaries, self.transfer, self.matrix = colour
        self.level_idc = level_idc
        self.full_range = full_range


def _profile_tier_level(w, level_idc):
    w.u(0, 2)          # general_profile_space
    w.u(0, 1)          # general_tier_flag (Main)
    w.u(2, 5)          # general_profile_idc = Main 10
    w.u(0x20000000, 32)  # compatibility flag[2]
    w.u(1, 1)          # progressive_source
    w.u(0, 1)          # interlaced_source
    w.u(0, 1)          # non_packed_constraint
    w.u(1, 1)          # frame_only_constraint
    w.u(0, 32)         # 43 reserved + inbld bits
    w.u(0, 12)
    w.u(level_idc, 8)


def vps(p):
    w = BitWriter()
    w.u(0, 4)      # vps_video_parameter_set_id
    w.u(1, 1)      # base_layer_internal
    w.u(1, 1)      # base_layer_available
    w.u(0, 6)      # max_layers_minus1
    w.u(0, 3)      # max_sub_layers_minus1
    w.u(1, 1)      # temporal_id_nesting
    w.u(0xFFFF, 16)
    _profile_tier_level(w, p.level_idc)
    w.u(1, 1)      # sub_layer_ordering_info_present
    w.ue(1)        # max_dec_pic_buffering_minus1
    w.ue(0)        # max_num_reorder_pics
    w.ue(0)        # max_latency_increase_plus1
    w.u(0, 6)      # max_layer_id
    w.ue(0)        # num_layer_sets_minus1
    w.u(1, 1)      # timing_info_present
    w.u(p.fps_den, 32)
    w.u(p.fps_num, 32)
    w.u(0, 1)      # poc_proportional_to_timing
    w.ue(0)        # num_hrd_parameters
    w.u(0, 1)      # extension
    w.trailing_bits()
    return hevc_nal(NAL_VPS, w.getvalue())


def sps(p):
    w = BitWriter()
    w.u(0, 4)      # sps_video_parameter_set_id
    w.u(0, 3)      # max_sub_layers_minus1
    w.u(1, 1)      # temporal_id_nesting
    _profile_tier_level(w, p.level_idc)
    w.ue(0)        # sps_seq_parameter_set_id
    w.ue(1)        # chroma_format_idc 4:2:0
    w.ue(p.cw)
    w.ue(p.ch)
    crop_r, crop_b = (p.cw - p.width) // 2, (p.ch - p.height) // 2
    w.flag(crop_r or crop_b)
    if crop_r or crop_b:
        w.ue(0)
        w.ue(crop_r)
        w.ue(0)
        w.ue(crop_b)
    w.ue(BIT_DEPTH - 8)
    w.ue(BIT_DEPTH - 8)
    w.ue(12)       # log2_max_pic_order_cnt_lsb_minus4 -> 16 bit POC LSB
    w.u(1, 1)      # sub_layer_ordering_info_present
    w.ue(1)
    w.ue(0)
    w.ue(0)
    w.ue(CTB_LOG2 - 3)  # log2_min_luma_coding_block_size_minus3
    w.ue(0)        # log2_diff_max_min_luma_coding_block_size
    w.ue(0)        # log2_min_luma_transform_block_size_minus2
    w.ue(3)        # log2_diff_max_min_luma_transform_block_size (4..32)
    w.ue(0)        # max_transform_hierarchy_depth_inter
    w.ue(0)        # max_transform_hierarchy_depth_intra
    w.u(0, 1)      # scaling_list_enabled
    w.u(0, 1)      # amp_enabled
    w.u(0, 1)      # sample_adaptive_offset_enabled
    w.u(0, 1)      # pcm_enabled (Amlogic firmware cannot decode PCM)
    w.ue(1)        # num_short_term_ref_pic_sets
    w.ue(1)        # st_ref_pic_set(0): num_negative_pics
    w.ue(0)        # num_positive_pics
    w.ue(0)        # delta_poc_s0_minus1
    w.u(1, 1)      # used_by_curr_pic_s0
    w.u(0, 1)      # long_term_ref_pics_present
    w.u(0, 1)      # sps_temporal_mvp_enabled
    w.u(0, 1)      # strong_intra_smoothing_enabled
    w.u(1, 1)      # vui_parameters_present
    w.u(1, 1)      # aspect_ratio_info_present
    w.u(1, 8)      # 1:1
    w.u(0, 1)      # overscan_info_present
    w.u(1, 1)      # video_signal_type_present
    w.u(5, 3)      # video_format unspecified
    w.u(1 if p.full_range else 0, 1)  # video_full_range
    w.u(1, 1)      # colour_description_present
    w.u(p.primaries, 8)
    w.u(p.transfer, 8)
    w.u(p.matrix, 8)
    w.u(0, 1)      # chroma_loc_info_present
    w.u(0, 1)      # neutral_chroma_indication
    w.u(0, 1)      # field_seq
    w.u(0, 1)      # frame_field_info_present
    w.u(0, 1)      # default_display_window
    w.u(1, 1)      # vui_timing_info_present
    w.u(p.fps_den, 32)
    w.u(p.fps_num, 32)
    w.u(0, 1)      # poc_proportional_to_timing
    w.u(0, 1)      # hrd_parameters_present
    w.u(0, 1)      # bitstream_restriction
    w.u(0, 1)      # sps_extension_present
    w.trailing_bits()
    return hevc_nal(NAL_SPS, w.getvalue())


def pps():
    w = BitWriter()
    w.ue(0)        # pps_pic_parameter_set_id
    w.ue(0)        # pps_seq_parameter_set_id
    w.u(0, 1)      # dependent_slice_segments_enabled
    w.u(0, 1)      # output_flag_present
    w.u(0, 3)      # num_extra_slice_header_bits
    w.u(0, 1)      # sign_data_hiding_enabled
    w.u(0, 1)      # cabac_init_present
    w.ue(0)        # num_ref_idx_l0_default_active_minus1
    w.ue(0)        # num_ref_idx_l1_default_active_minus1
    w.se(SLICE_QP - 26)
    w.u(0, 1)      # constrained_intra_pred
    w.u(0, 1)      # transform_skip_enabled
    w.u(0, 1)      # cu_qp_delta_enabled
    w.se(0)        # pps_cb_qp_offset
    w.se(0)        # pps_cr_qp_offset
    w.u(0, 1)      # pps_slice_chroma_qp_offsets_present
    w.u(0, 1)      # weighted_pred
    w.u(0, 1)      # weighted_bipred
    w.u(1, 1)      # transquant_bypass_enabled
    w.u(0, 1)      # tiles_enabled
    w.u(0, 1)      # entropy_coding_sync_enabled
    w.u(0, 1)      # pps_loop_filter_across_slices_enabled
    w.u(1, 1)      # deblocking_filter_control_present
    w.u(0, 1)      # deblocking_filter_override_enabled
    w.u(1, 1)      # pps_deblocking_filter_disabled
    w.u(0, 1)      # pps_scaling_list_data_present
    w.u(0, 1)      # lists_modification_present
    w.ue(0)        # log2_parallel_merge_level_minus2
    w.u(0, 1)      # slice_segment_header_extension_present
    w.u(0, 1)      # pps_extension_present
    w.trailing_bits()
    return hevc_nal(NAL_PPS, w.getvalue())


def _sei_payload(w, payload_type, payload):
    for value in (payload_type, len(payload)):
        while value >= 255:
            w.u(255, 8)
            value -= 255
        w.u(value, 8)
    w.raw(payload)


def sei_hdr10(mastering, max_cll, max_fall):
    """Mastering display colour volume + content light level SEI.

    mastering = ((rx, ry), (gx, gy), (bx, by), (wx, wy), max_nits, min_nits)
    """
    w = BitWriter()
    if mastering:
        (r, g, b, wp, max_l, min_l) = mastering
        m = BitWriter()
        for x, y in (g, b, r):
            m.u(int(round(x * 50000)), 16)
            m.u(int(round(y * 50000)), 16)
        m.u(int(round(wp[0] * 50000)), 16)
        m.u(int(round(wp[1] * 50000)), 16)
        m.u(int(round(max_l * 10000)), 32)
        m.u(int(round(min_l * 10000)), 32)
        _sei_payload(w, 137, m.getvalue())
    c = BitWriter()
    c.u(min(max_cll, 65535), 16)
    c.u(min(max_fall, 65535), 16)
    _sei_payload(w, 144, c.getvalue())
    w.trailing_bits()
    return hevc_nal(NAL_SEI_PREFIX, w.getvalue())


_P_BYPASS_INIT = 154
_P_SKIP_INIT = (197, 185, 201)


def _slice_header(w, idr, slice_type, poc):
    w.u(1, 1)      # first_slice_segment_in_pic
    if idr:
        w.u(0, 1)  # no_output_of_prior_pics
    w.ue(0)        # slice_pic_parameter_set_id
    w.ue(slice_type)
    if not idr:
        w.u(poc & 0xFFFF, 16)
        w.u(1, 1)  # short_term_ref_pic_set_sps_flag (only one set, no idx)
    if slice_type == 1:
        w.u(0, 1)  # num_ref_idx_active_override
        w.ue(4)    # five_minus_max_num_merge_cand -> one merge candidate
    w.se(0)        # slice_qp_delta
    w.trailing_bits()  # byte_alignment()


def _uniform_ctb_rows(plane, stride, rows, n):
    """Per CTB row: True when all its sample rows are identical across the picture."""
    same = [plane[o:o + stride] == plane[o - stride:o]
            for o in range(stride, len(plane), stride)]
    return [all(same[r * n:(r + 1) * n - 1]) for r in range(rows)]


def encode_idr(p, pic):
    """IDR slice NAL; returns (nal, number of CUs that needed a residual)."""
    w = BitWriter()
    _slice_header(w, True, 2, 0)
    enc = CabacEncoder(w)
    coder = intra.IntraCoder(enc, SLICE_QP)
    cols, rows = pic.cw // CTB, pic.ch // CTB
    planes = [(plane, pic.cw // s, CTB // s, _uniform_ctb_rows(plane, pic.cw // s, rows, CTB // s))
              for plane, s in zip(pic.planes, (1, 2, 2), strict=True)]
    residual_cus = 0
    chosen: dict[tuple, tuple] = {}     # a pattern repeats the same CU over and over
    for cy in range(rows):
        left_mode = intra.MODE_DC
        for cx in range(cols):
            blocks = []
            for plane, stride, n, uniform in planes:
                x, y = cx * n, cy * n
                left, top = intra.refs(plane, stride, x, y, n, cx > 0, cy > 0)
                blocks.append((intra.block(plane, stride, x, y, n, uniform[cy]), n, left, top))
            key = tuple(a.tobytes() for src, _, left, top in blocks for a in (src, left, top))
            if key not in chosen:
                chosen[key] = intra.choose(blocks)
            mode, residuals = chosen[key]
            residual_cus += residuals != (None, None, None)
            coder.coding_unit(mode, left_mode, residuals)
            left_mode = mode
            enc.encode_terminate(cy == rows - 1 and cx == cols - 1)  # end_of_slice_segment_flag
    enc.finish()
    w.trailing_bits()
    return hevc_nal(NAL_IDR_W_RADL, w.getvalue()), residual_cus


def p_slice_data(p):
    """Slice data of an all-skip P picture (identical for every P picture)."""
    w = BitWriter()
    enc = CabacEncoder(w)
    ctxs = [Context(v, SLICE_QP) for v in _P_SKIP_INIT]
    bypass = Context(_P_BYPASS_INIT, SLICE_QP)
    cols, rows = p.cw // CTB, p.ch // CTB
    for cy in range(rows):
        for cx in range(cols):
            enc.encode(bypass, 0)                     # cu_transquant_bypass_flag
            enc.encode(ctxs[(cx > 0) + (cy > 0)], 1)  # cu_skip_flag
            enc.encode_terminate(1 if (cy == rows - 1 and cx == cols - 1) else 0)
    enc.finish()
    w.trailing_bits()
    return w.getvalue()


def _p_header():
    w = BitWriter()
    _slice_header(w, False, 1, 0)
    return int.from_bytes(hevc_nal(NAL_TRAIL_R, w.getvalue()), "big")


# NAL + slice header of a P picture: 6 bytes, POC LSB at bits 26..11. Only byte 3 can
# be zero, so no start code emulation can involve the header: escape just the data.
_P_HEADER = _p_header()


@lru_cache(maxsize=4)
def _escaped(data):
    return escape(data)


def p_slice_nal(poc, data):
    return (_P_HEADER | (poc & 0xFFFF) << 11).to_bytes(6, "big") + _escaped(data)


def level_for(width, height, fps):
    """general_level_idc (x30) for the picture size and rate."""
    luma = width * height
    rate = luma * fps
    side = max(width, height) ** 2
    for level, max_ps, max_sr in ((30, 36864, 552960), (60, 122880, 3686400),
                                  (63, 245760, 7372800), (90, 552960, 16588800),
                                  (93, 983040, 33177600), (120, 2228224, 66846720),
                                  (123, 2228224, 133693440), (150, 8912896, 267386880),
                                  (153, 8912896, 534773760), (156, 8912896, 1069547520),
                                  (180, 35651584, 1069547520), (183, 35651584, 2139095040),
                                  (186, 35651584, 4278190080)):
        if luma <= max_ps and rate <= max_sr and side <= 8 * max_ps:
            return level
    return 186


def make_params(width, height, fps_num, fps_den, colour, full_range=False):
    fps = fps_num / fps_den
    return _Params(width, height, fps_num, fps_den, colour,
                   level_for(padded(width), padded(height), fps), full_range)
