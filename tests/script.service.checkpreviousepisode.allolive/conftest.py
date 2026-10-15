"""The add-on is upstream's with our patches, so assemble it the way build.sh does and test that."""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADDON = "script.service.checkpreviousepisode.allolive"
_tree = tempfile.TemporaryDirectory()


def pytest_configure(config):
    tree = Path(_tree.name) / ADDON
    subprocess.run(["bash", "-c", '. scripts/lib.sh && fork_source "$1" "$2" "$3"', "-",
                    ADDON, "ce22", str(tree)], cwd=ROOT, check=True)
    sys.path.insert(0, str(tree / "resources" / "lib"))
