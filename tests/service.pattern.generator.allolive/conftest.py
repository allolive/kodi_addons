"""pytest wiring for service.pattern.generator.allolive: the fixtures live in pgtest.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pgtest import rendered, script  # noqa: E402, F401  (fixtures)
