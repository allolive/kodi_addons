"""service.pattern.generator.allolive: the regression suite, run with pytest."""
import re
import subprocess

import pytest

from pgtest import ADDON, DOVI_TOOL, HERE, needs_dovi_tool, needs_ffmpeg

# t_*.py scripts that are complete checks on their own (exit status = result)
SCRIPTS = {
    "cabac-roundtrip": "t_cabac.py",
    "kodi-backend": "t_kodi.py",
    "g1-parity": "t_g1.py",
    "classic-85-ls-dc": "t_classic.py",
    "static-metadata": "t_meta.py",
    "overlay": "t_overlay.py",
    "network": "t_net.py",
    "apl-vs-pgenerator": "t_apl.py",
    "live-stream": "t_live.py",
    "gen-live-routing": "t_gen_live.py",
    "hdmi-state": "t_hdmi.py",
    "service-status": "t_status.py",
    "classic-check1": "t_classic2.py",
    "parity-round1-fixes": "t_parity2.py",
    "check2": "t_check2.py",
}


@pytest.mark.parametrize("name", SCRIPTS.values(), ids=SCRIPTS.keys())
def test_script(script, name):
    script(name)


@needs_ffmpeg
@pytest.mark.parametrize("args", [("1920", "1080"), ("1280", "720", "noise"),
                                  ("3840", "2160", "noise"), ("640", "360", "noise")],
                         ids=lambda a: "x".join(a[:2]) + ("-noise" if "noise" in a else ""))
def test_hevc_bitexact(script, args):
    """ffmpeg decodes the encoder's frames to exactly the source pixels."""
    out = script("t_hevc.py", *args).stdout
    assert re.search(r"ffmpeg err: $", out, re.M), out
    assert not re.search(r"mismatched rows [1-9]", out), out
    assert "frame 4 mismatched rows 0" in out, out


def test_dovi_rpu_byte_exact():
    """The Dolby Vision RPUs match dovi_tool's for the same metadata, byte for byte."""
    from patterngen import bitstream, dovi
    ref1 = (HERE / "dvref" / "ref.bin").read_bytes()[4:]
    ref2 = (HERE / "dvref" / "ref2.bin").read_bytes()[4:]
    assert bitstream.escape(dovi.rpu((0, 2081, 1000), 62, 3079, (1000, 1, 1000, 400), 1,
                                     l5=True, profile=8)) == ref1
    assert bitstream.escape(dovi.rpu((12, 4095, 1234), 7, 3696, None, None,
                                     l5=True, profile=8)) == ref2


def test_render_every_mode(rendered):
    _, out = rendered
    assert out.count("rendered") == 4, out


@needs_ffmpeg
def test_clips_are_main10_hevc_ffmpeg_decodes(rendered):
    cwd, _ = rendered
    clips = sorted((cwd / "cache").glob("*.mkv"))
    assert len(clips) == 4
    for clip in clips:
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                "stream=codec_name,profile,color_transfer", "-of", "csv=p=0",
                                str(clip)], capture_output=True, text=True, check=True)
        assert "hevc,Main 10" in probe.stdout, (clip.name, probe.stdout)
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(clip), "-frames:v", "3", "-f", "null",
                        "-"], capture_output=True, check=True)


@needs_ffmpeg
@needs_dovi_tool
def test_dv_rpu_extracts_with_dovi_tool(rendered):
    cwd, _ = rendered
    dv = [c for c in sorted((cwd / "cache").glob("*.mkv")) if b"dvcC" in c.read_bytes()]
    assert dv, "no Dolby Vision clip"
    hevc, rpu = cwd / "dv.hevc", cwd / "dv.rpu"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(dv[0]), "-c", "copy", "-bsf:v",
                    "hevc_mp4toannexb", "-frames:v", "20", "-f", "hevc", str(hevc)], check=True)
    subprocess.run([str(DOVI_TOOL), "extract-rpu", str(hevc), "-o", str(rpu)],
                   capture_output=True, check=True)
    info = subprocess.run([str(DOVI_TOOL), "info", "-i", str(rpu), "-s"],
                          capture_output=True, text=True, check=True)
    assert "Frames: 20" in info.stdout, info.stdout


def test_protocol_transcript(script):
    """A Calman session played through UPGCI: no error, one play of 13 patterns, and
    GET_SETTINGS answering the mode set."""
    out = script("t_proto.py").stdout
    assert not re.search(r"^ERR", out, re.M), out
    assert out.count("played:") == 1 and "played: 13" in out, out
    assert re.search(r"GET_SETTINGS.*Resolution=3840x2160", out), out


def test_addon_python_compiles():
    for path in ADDON.rglob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
