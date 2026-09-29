"""Helpers and fixtures for the service.pattern.generator.allolive tests (conftest.py loads them).

The t_*.py scripts are the checks themselves (each exits non-zero on a failure); the
tests in test_pattern_generator.py run them, one per test, each in its own directory
with its own ports, and hold the checks that used to be shell one-liners.
"""
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ADDON = Path(os.environ.get("PGADDON") or HERE / "../../addons/service.pattern.generator.allolive").resolve()
LIB = ADDON / "resources" / "lib"
PGREF = HERE / "pgenerator-ref" / "usr" / "share" / "PGenerator"
DOVI_TOOL = HERE / "dtbin" / "dovi_tool"

sys.path.insert(0, str(LIB))

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="needs ffmpeg and ffprobe")
needs_dovi_tool = pytest.mark.skipif(not DOVI_TOOL.exists(), reason="run setup.sh for dovi_tool")


def _env():
    # scripts add their own offsets (up to +3000, then up to 400 ports each) to PGPORT: kept
    # below 32768, where Linux starts handing out ports to outgoing connections
    return dict(os.environ, PGADDON=str(ADDON), PGREF=str(PGREF),
                PGPORT=str(random.randrange(20000, 29000)))


def run(script, *args, cwd, timeout=900):
    """Run tests/<script> with cwd as its working directory; the finished process."""
    return subprocess.run([sys.executable, str(HERE / script), *args], cwd=cwd, env=_env(),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout)


def output(proc):
    return (proc.stdout + proc.stderr)[-4000:]


@pytest.fixture
def script(tmp_path):
    """script(name, *args): run it in this test's own directory; fails the test on a
    non-zero exit, with the script's output; returns the finished process."""
    def call(name, *args):
        proc = run(name, *args, cwd=tmp_path)
        assert proc.returncode == 0, "%s failed:\n%s" % (name, output(proc))
        return proc
    return call


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """Every signal mode rendered once: the directory holding cache/*.mkv."""
    cwd = tmp_path_factory.mktemp("render")
    proc = run("t_render.py", "sdr", "hdr10", "hlg", "dv", cwd=cwd)
    assert proc.returncode == 0, output(proc)
    return cwd, proc.stdout
