"""pytest wiring for service.shutter3d.allolive: its library on the path."""
import sys
from pathlib import Path

ADDON = Path(__file__).resolve().parents[2] / "addons" / "service.shutter3d.allolive"
sys.path.insert(0, str(ADDON / "resources" / "lib"))
