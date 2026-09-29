"""Tiny Matroska muxer for a single HEVC track made of pre-built frames."""

import os
import struct
import threading


def _vint_size(n):
    for length in range(1, 9):
        if n < (1 << (7 * length)) - 1:
            return (n | (1 << (7 * length))).to_bytes(length, "big")
    raise ValueError("element too large")


def el(eid, payload):
    head = eid.to_bytes((eid.bit_length() + 7) // 8, "big")
    return head + _vint_size(len(payload)) + payload


def uint(eid, value):
    return el(eid, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def flt(eid, value):
    return el(eid, struct.pack(">d", value))


def string(eid, value):
    return el(eid, value.encode("utf-8"))


def hvcc(vps, sps, pps, level_idc, prefix_sei=None):
    """HEVCDecoderConfigurationRecord for the Main10 streams hevc.py writes."""
    out = bytearray()
    out += bytes([1, 0x02])                      # version; profile_space/tier/idc
    out += (0x20000000).to_bytes(4, "big")       # compatibility flags
    out += bytes([0x90, 0, 0, 0, 0, 0])          # progressive + frame_only
    out += bytes([level_idc])
    out += bytes([0xF0, 0x00])                   # min_spatial_segmentation_idc
    out += bytes([0xFC])                         # parallelismType
    out += bytes([0xFD])                         # chroma_format_idc = 1
    out += bytes([0xFA, 0xFA])                   # bit depth luma/chroma minus 8 = 2
    out += bytes([0, 0])                         # avgFrameRate
    out += bytes([0x0F])                         # numTemporalLayers=1, nested, lengthSize=4
    arrays = [(32, vps), (33, sps), (34, pps)]
    if prefix_sei:
        arrays.append((39, prefix_sei))
    out += bytes([len(arrays)])
    for nal_type, nal in arrays:
        out += bytes([0x80 | nal_type]) + (1).to_bytes(2, "big")
        out += len(nal).to_bytes(2, "big") + nal
    return bytes(out)


def length_prefixed(nals):
    return b"".join(len(n).to_bytes(4, "big") + n for n in nals)


class Colour:
    def __init__(self, matrix, transfer, primaries, max_cll=None, max_fall=None,
                 mastering=None, full_range=False):
        self.matrix = matrix
        self.transfer = transfer
        self.primaries = primaries
        self.max_cll = max_cll
        self.max_fall = max_fall
        self.mastering = mastering   # ((rx,ry),(gx,gy),(bx,by),(wx,wy),max,min)
        self.full_range = full_range

    def element(self):
        body = uint(0x55B1, self.matrix) + uint(0x55B9, 2 if self.full_range else 1) + \
            uint(0x55BA, self.transfer) + uint(0x55BB, self.primaries)
        if self.max_cll is not None:
            body += uint(0x55BC, self.max_cll)
        if self.max_fall is not None:
            body += uint(0x55BD, self.max_fall)
        if self.mastering:
            (r, g, b, wp, max_l, min_l) = self.mastering
            m = b"".join(flt(eid, v) for eid, v in (
                (0x55D1, r[0]), (0x55D2, r[1]), (0x55D3, g[0]), (0x55D4, g[1]),
                (0x55D5, b[0]), (0x55D6, b[1]), (0x55D7, wp[0]), (0x55D8, wp[1]),
                (0x55D9, float(max_l)), (0x55DA, float(min_l))))
            body += el(0x55D0, m)
        return el(0x55B0, body)


def _pos(eid, value):
    """Fixed-width position so the SeekHead and Cues sizes never depend on it."""
    return el(eid, value.to_bytes(8, "big"))


EBML_HEADER = el(0x1A45DFA3, uint(0x4286, 1) + uint(0x42F7, 1) + uint(0x42F2, 4) +
                 uint(0x42F3, 8) + string(0x4282, "matroska") + uint(0x4287, 4) +
                 uint(0x4285, 2))


def _tracks(width, height, frame_ns, codec_private, colour, dovi_record):
    video = uint(0xB0, width) + uint(0xBA, height) + colour.element()
    track = uint(0xD7, 1) + uint(0x73C5, 1) + uint(0x83, 1) + uint(0x9C, 0) + \
        string(0x86, "V_MPEGH/ISO/HEVC") + el(0x63A2, codec_private) + \
        uint(0x23E383, int(round(frame_ns))) + string(0x22B59C, "und") + \
        el(0xE0, video)
    if dovi_record is not None:
        fourcc = b"dvvC" if dovi_record[2] >> 1 > 7 else b"dvcC"
        track += el(0x41E4, string(0x41A4, "Dolby Vision configuration") +
                    uint(0x41E7, int.from_bytes(fourcc, "big")) +
                    el(0x41ED, dovi_record))
    return el(0x1654AE6B, el(0xAE, track))


def live_header(width, height, fps_num, fps_den, codec_private, colour, dovi_record=None,
                title="Pattern"):
    """The start of an endless stream: a Segment of unknown size, no duration, no cues;
    live_frame() clusters follow."""
    frame_ns = 1000000000 * fps_den / fps_num
    info = el(0x1549A966, uint(0x2AD7B1, 1000000) + string(0x4D80, "service.pattern.generator.allolive") +
              string(0x5741, "service.pattern.generator.allolive") + string(0x7BA9, title))
    return (EBML_HEADER + b"\x18\x53\x80\x67\x01\xFF\xFF\xFF\xFF\xFF\xFF\xFF" + info +
            _tracks(width, height, frame_ns, codec_private, colour, dovi_record))


def live_frame(ms, payload, key):
    """One frame as its own cluster, timestamped in ms, so it can be sent as it is made."""
    block = b"\xA3" + _vint_size(len(payload) + 4) + b"\x81" + \
        struct.pack(">hB", 0, 0x80 if key else 0) + payload
    return el(0x1F43B675, uint(0xE7, ms) + block)


def write(path, width, height, fps_num, fps_den, codec_private, key_frame,
          repeat_frame, frame_count, colour, dovi_record=None, title="Pattern"):
    """Write key_frame followed by frame_count - 1 frames.

    repeat_frame(i) returns the length-prefixed payload of frame i (i >= 1).
    """
    frame_ns = 1000000000 * fps_den / fps_num

    def ms(i):                                   # TimestampScale is 1 ms
        return int(round(i * frame_ns / 1000000))

    header = EBML_HEADER
    info = el(0x1549A966, uint(0x2AD7B1, 1000000) + flt(0x4489, frame_count * frame_ns / 1000000) +
              string(0x4D80, "service.pattern.generator.allolive") +
              string(0x5741, "service.pattern.generator.allolive") + string(0x7BA9, title))
    tracks = _tracks(width, height, frame_ns, codec_private, colour, dovi_record)

    clusters = bytearray()
    per_cluster = max(1, int(round(2e9 / frame_ns)))   # ~2 s
    heads: dict[int, bytes] = {}        # SimpleBlock id + size + track by payload length
    pack = struct.Struct(">hB").pack
    for first in range(0, frame_count, per_cluster):
        start = ms(first)
        body = bytearray(uint(0xE7, start))
        for i in range(first, min(first + per_cluster, frame_count)):
            payload = repeat_frame(i) if i else key_frame
            head = heads.get(len(payload))
            if head is None:
                head = heads[len(payload)] = b"\xA3" + _vint_size(len(payload) + 4) + b"\x81"
            body += head
            body += pack(ms(i) - start, 0 if i else 0x80)
            body += payload
        clusters += el(0x1F43B675, body)

    # Segment layout: SeekHead, Info, Tracks, Clusters..., Cues
    seek_len = len(el(0x114D9B74, 3 * el(0x4DBB, el(0x53AB, b"1234") + _pos(0x53AC, 0))))
    pos_tracks = seek_len + len(info)
    pos_clusters = pos_tracks + len(tracks)
    pos_cues = pos_clusters + len(clusters)
    cues = el(0x1C53BB6B, el(0xBB, uint(0xB3, 0) +
                                el(0xB7, uint(0xF7, 1) + _pos(0xF1, pos_clusters))))
    seekhead = el(0x114D9B74, b"".join(
        el(0x4DBB, el(0x53AB, sid) + _pos(0x53AC, pos))
        for sid, pos in ((b"\x15\x49\xA9\x66", seek_len),
                         (b"\x16\x54\xAE\x6B", pos_tracks),
                         (b"\x1C\x53\xBB\x6B", pos_cues))))
    write_file(path, header, b"\x18\x53\x80\x67" + _vint_size(pos_cues + len(cues)), seekhead,
               info, tracks, clusters, cues)


def write_file(path, *chunks):
    """Write `path` whole or not at all: into <path>.<pid>.<thread>.part, then renamed
    (render.prune clears .part files left by a crash)."""
    tmp = "%s.%d.%d.part" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "wb") as f:
            for chunk in chunks:
                f.write(chunk)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
