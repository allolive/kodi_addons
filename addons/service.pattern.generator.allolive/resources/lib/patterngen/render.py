"""Scene + signal -> playable Matroska file (cached by content)."""

import hashlib
import marshal
import os
import time
from typing import Any

from . import dovi, hevc, mkv
from .hevc import (MATRIX_BT709, MATRIX_BT2020_NCL, PRIMARIES_BT709, PRIMARIES_BT2020,
                   TRANSFER_BT709, TRANSFER_HLG, TRANSFER_PQ)
from .patterns import DV_DEFAULT_MAP, DV_MIN, DV_PEAK, pq12

VERSION = 14  # bump when the bitstream layout or metadata changes, invalidates the cache

_DV_SRC_MIN, _DV_SRC_MAX = pq12(DV_MIN), pq12(DV_PEAK)
_DV_L1 = (_DV_SRC_MIN, _DV_SRC_MAX, (_DV_SRC_MIN + _DV_SRC_MAX) // 2)


def _colour_tuple(sig):
    if sig.colorimetry == "bt2020":
        prim, matrix = PRIMARIES_BT2020, MATRIX_BT2020_NCL
    else:
        prim, matrix = PRIMARIES_BT709, MATRIX_BT709
    transfer = {"sdr": TRANSFER_BT709, "hlg": TRANSFER_HLG}.get(sig.mode, TRANSFER_PQ)
    return prim, transfer, matrix


def _key(scene, sig, *extra):
    return hashlib.sha1(repr((VERSION, scene.key(), sig.key(), *extra)).encode()).hexdigest()[:16]


def cache_path(cache_dir, scene, sig, duration):
    return os.path.join(cache_dir, "pattern-%s-%s.mkv" % (scene.name, _key(scene, sig, duration)))


class Encoded:
    """One pattern encoded for one signal: what a clip file or the live stream needs.

    The access units are length-prefixed NAL units. A key frame is key_head + key_tail
    and repeat frame i is P slice i + tail; the tail (the Dolby Vision RPU) is
    kept apart so filler data can go before it. stream_key is everything a player
    fixes when it opens the stream: patterns sharing it can follow each other in one.
    """

    FIELDS = ("name", "width", "height", "fps_num", "fps_den", "codec_private", "colour",
              "dovi_record", "key_head", "key_tail", "p_data", "tail", "residual_cus",
              "dv_format")

    name: str
    width: int
    height: int
    fps_num: int
    fps_den: int
    codec_private: bytes
    colour: tuple
    dovi_record: Any
    key_head: bytes
    key_tail: bytes
    p_data: bytes
    tail: bytes
    residual_cus: int
    dv_format: Any

    def __init__(self, **kw):
        for f in self.FIELDS:
            setattr(self, f, kw[f])

    def as_tuple(self):
        return tuple(getattr(self, f) for f in self.FIELDS)

    @classmethod
    def from_tuple(cls, t):
        return cls(**dict(zip(cls.FIELDS, t, strict=True)))

    @property
    def stream_key(self):
        if self.dv_format is not None:
            # Dolby Vision: the mapping (L255) travels in every frame's RPU, and a mapping
            # change only changes the picture's values, so patterns that differ in it
            # follow each other in one stream (the signal is not restarted)
            return ("dv", self.width, self.height, self.fps_num, self.fps_den, self.dv_format)
        return (self.width, self.height, self.fps_num, self.fps_den, self.codec_private,
                self.colour, self.dovi_record, self.key_tail, self.tail)

    def key_frame(self, filler=b""):
        return self.key_head + filler + self.key_tail

    def repeat_frame(self, i, filler=b""):
        n = hevc.p_slice_nal(i, self.p_data)
        return len(n).to_bytes(4, "big") + n + filler + self.tail

    def container_colour(self):
        matrix, transfer, primaries, max_cll, max_fall, mastering = self.colour
        return mkv.Colour(matrix, transfer, primaries, max_cll=max_cll, max_fall=max_fall,
                          mastering=mastering, full_range=self.dv_format is not None)


def encode(scene, sig):
    """Encode scene for sig: an Encoded."""
    dv = sig.mode == "dv"
    colour = _colour_tuple(sig)
    run_mode = sig.dv_map_mode if sig.dv_map_mode is not None else DV_DEFAULT_MAP
    if dv:
        # Dolby Vision is profile 5, a full-range IPT picture computed here. With profile
        # 8.1 the Amlogic core converted the Y'CbCr to IPT itself, expanding the limited
        # range, and in Relative mapping the LG read the result as limited range again:
        # 5 % showed black, 80 % 18 % too bright, 2026-09-27. Colour tags unspecified.
        colour = (2, 2, 2)

        def code(c):
            return dovi.ipt10(c.r, c.g, c.b, literal=run_mode == 2)
    else:
        def code(c):
            return c.ycc(sig.colorimetry)
    pic = hevc.Picture(scene.width, scene.height, code(scene.background))
    for x, y, w, h, c in scene.rects:
        pic.fill_rect(x, y, w, h, code(c))
    params = hevc.make_params(scene.width, scene.height, sig.fps_num, sig.fps_den, colour,
                              full_range=dv)
    idr, residual_cus = hevc.encode_idr(params, pic)
    p_data = hevc.p_slice_data(params)
    vps, sps, pps = hevc.vps(params), hevc.sps(params), hevc.pps()

    mastering = None
    sei = None
    cll, fall = (max(0, min(65535, int(v))) for v in (sig.max_cll, sig.max_fall))
    max_l = max(0.0, min(429496.0, float(sig.max_luma)))
    min_l = max(0.0, min(6.5535, float(sig.min_luma)))
    if sig.mode == "hdr10":
        r, g, b, wp = sig.mastering_primaries()
        mastering = (r, g, b, wp, max_l, min_l)
        sei = hevc.sei_hdr10(mastering, cll, fall)
    elif sig.mode == "hlg":     # updateHDR_Infoframe EOTF 3: no primaries, conf luminance
        z = (0.0, 0.0)
        mastering = (z, z, z, z, max_l, min_l)
        sei = hevc.sei_hdr10(mastering, cll, fall)

    rpu_first = rpu_next = None
    dovi_record = None
    fps = sig.fps_num / float(sig.fps_den)
    if sig.mode == "dv":
        # PGenerator's metadata: source range and L1 from its fixed 0.005-4000 nits and
        # L255 (Calman's mapping, else its default Relative), nothing else: an LG G5
        # blacks out on L255 next to L5/L6, 2026-09-26
        rpu_first, rpu_next = (dovi.rpu_nal(dovi.rpu(_DV_L1, _DV_SRC_MIN, _DV_SRC_MAX, None,
                                                     run_mode, first))
                               for first in (True, False))
        dovi_record = dovi.config_record(dovi.dv_level(scene.width, scene.height, fps))

    key_nals = [vps, sps, pps] + [n for n in (sei,) if n] + [idr]
    return Encoded(
        name=scene.name, width=scene.width, height=scene.height,
        fps_num=sig.fps_num, fps_den=sig.fps_den,
        codec_private=mkv.hvcc(vps, sps, pps, params.level_idc, sei),
        # what a DV stream fixes when it opens: the parameter sets, the colour container
        # and the DV configuration, not the per-frame metadata
        dv_format=(mkv.hvcc(vps, sps, pps, params.level_idc, None), colour, dovi_record)
        if dovi_record is not None else None,
        colour=(colour[2], colour[1], colour[0], cll if mastering else None,
                fall if mastering else None, mastering),
        dovi_record=dovi_record,
        key_head=mkv.length_prefixed(key_nals),
        key_tail=mkv.length_prefixed([rpu_first]) if rpu_first else b"",
        p_data=p_data,
        tail=mkv.length_prefixed([rpu_next]) if rpu_next else b"",
        residual_cus=residual_cus)


def encoded(scene, sig, cache_dir, log=None):
    """encode(), cached on disk by content like the clips, so a pattern shown again (a
    greyscale sweep repeats its steps) is not encoded again."""
    path = os.path.join(cache_dir, "enc-%s-%s.bin" % (scene.name, _key(scene, sig)))
    try:
        os.utime(path, None)            # first: the pruner keeps a file used since it listed
        with open(path, "rb") as f:
            data = f.read()
        # a damaged file that still unpacks would put a wrong picture on screen silently
        if hashlib.sha1(data[20:]).digest() != data[:20]:
            raise ValueError("checksum")
        return Encoded.from_tuple(marshal.loads(data[20:]))
    except (OSError, EOFError, ValueError, TypeError, KeyError) as exc:
        if not isinstance(exc, FileNotFoundError) and log:
            log("cached %s unusable (%r): encoding it again" % (os.path.basename(path), exc))
    t0 = time.time()
    enc = encode(scene, sig)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        data = marshal.dumps(enc.as_tuple())
        mkv.write_file(path, hashlib.sha1(data).digest(), data)
    except OSError as exc:
        if log:
            log("could not cache %s: %s" % (os.path.basename(path), exc), error=True)
    if log:
        log("encoded %s (%s, %d residual CUs) in %.2fs"
            % (os.path.basename(path), sig.describe(), enc.residual_cus, time.time() - t0))
    return enc


def render(scene, sig, cache_dir, duration=300, log=None):
    """Return the path of a Matroska file showing `scene` for `duration` seconds."""
    path = cache_path(cache_dir, scene, sig, duration)
    try:
        os.utime(path, None)      # hit: mark as recently used
        return path
    except OSError:
        pass
    t0 = time.time()
    os.makedirs(cache_dir, exist_ok=True)
    enc = encoded(scene, sig, cache_dir, log)
    fps = sig.fps_num / float(sig.fps_den)
    frames = max(1, int(round(duration * fps)))
    mkv.write(path, enc.width, enc.height, sig.fps_num, sig.fps_den, enc.codec_private,
              enc.key_frame(), enc.repeat_frame, frames, enc.container_colour(),
              enc.dovi_record, title=scene.name)
    if log:
        log("rendered %s (%s, %d residual CUs, %d frames) in %.2fs"
            % (os.path.basename(path), sig.describe(), enc.residual_cus, frames, time.time() - t0))
    return path


def prune(cache_dir, keep, part_age=3600):
    """Keep the `keep` most recently used clips and as many encoded patterns (the live
    stream's), and drop stale .part files.

    The manual-pattern script may be encoding into the same directory, so a
    young .part file is left alone.
    """
    now = time.time()
    kept: dict[str, list[tuple[float, str]]] = {".mkv": [], ".bin": []}
    stale = []
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return
    for f in names:
        if not f.startswith(("pattern-", "enc-")):
            continue
        p = os.path.join(cache_dir, f)
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        ext = os.path.splitext(f)[1]
        if ext in kept:
            kept[ext].append((mtime, p))
        elif f.endswith(".part") and now - mtime > part_age:
            stale.append(p)
    drop: list[tuple[float | None, str]] = [(None, p) for p in stale]
    for files in kept.values():
        files.sort(reverse=True)
        drop += files[max(1, keep):]
    for listed, p in drop:
        try:
            # it runs beside the generator: a file used again since it was listed stays
            if listed is None or os.path.getmtime(p) == listed:
                os.remove(p)
        except OSError:
            pass
