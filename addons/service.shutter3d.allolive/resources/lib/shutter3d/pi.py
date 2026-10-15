"""The Raspberry Pi Zero W emitter's boot: the add-on holds its firmware (the
latest pi-bt3dsp-firmware release, bundled when the add-on is built) and boots
any Pi waiting on USB with it. No Kodi imports."""

import os
import threading

from . import rpiboot

WATCH_S = 2.0                    # a Pi plugged in, or back in its boot ROM, is booted this soon


def bundled_build(boot_dir):
    """The build of the bundled image (its build.txt), or None without one."""
    try:
        with open(os.path.join(boot_dir, "build.txt")) as f:
            return f.read().strip() or None
    except OSError:
        return None


class Booter(threading.Thread):
    """Runs for the life of the service: boots every Pi that appears in its
    boot ROM (plugged in, the box just started, or sent back by the link to
    take a newer image)."""

    def __init__(self, boot_dir, log):
        super().__init__(name="shutter3d-pi-boot", daemon=True)
        self.boot_dir = boot_dir
        self.log = log
        self.stopping = threading.Event()
        self.warned = False

    def stop(self):
        self.stopping.set()
        self.join(timeout=2.0)

    def run(self):
        while not self.stopping.wait(WATCH_S):
            if not rpiboot.find():
                continue
            if not os.path.isfile(os.path.join(self.boot_dir, "bootcode.bin")):
                if not self.warned:
                    self.log("a Pi waits on USB, but this add-on was built without its firmware")
                    self.warned = True
                continue
            self.log("booting the Pi with build %s" % (bundled_build(self.boot_dir) or "unknown"))
            if not rpiboot.boot(self.boot_dir, self.log):
                self.log("the Pi did not take the firmware; trying again")
