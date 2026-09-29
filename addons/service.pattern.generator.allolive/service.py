import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "lib"))

from patterngen.kodi import Service  # noqa: E402

Service().run()
