"""The service's delay per frame rate, with Kodi's modules stubbed."""
import importlib
import sys
import types


class Addon:
    def __init__(self, *args):
        pass

    def getAddonInfo(self, key):
        return "service.shutter3d.allolive"

    def getSettingNumber(self, key):
        return 0.0

    def getSettingInt(self, key):
        return 0

    def getSettingBool(self, key):
        return True


class Kodi:
    """What the service sees of Kodi: the delay setting and the film rate."""

    def __init__(self, tmp_path):
        self.delay = 0
        self.fps = "0"
        self.fail_sets = False
        self.path = str(tmp_path / "delays.json")

    def rpc(self, method, **params):
        if method == "Settings.GetSettingValue":
            return {"value": self.delay}
        if method == "Settings.SetSettingValue":
            if self.fail_sets:
                return None
            self.delay = params["value"]
            return True
        raise AssertionError(method)


def service(monkeypatch, tmp_path):
    stubs = {
        "xbmc": types.SimpleNamespace(LOGINFO=1, LOGWARNING=2, log=lambda *a: None,
                                      Monitor=object, getInfoLabel=None),
        "xbmcaddon": types.SimpleNamespace(Addon=Addon),
        "xbmcgui": types.SimpleNamespace(Window=None),
        "xbmcvfs": types.SimpleNamespace(translatePath=None),
    }
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
    import shutter3d
    for name in ("ids", "kodi"):                 # imported afresh, against these stubs
        monkeypatch.delitem(sys.modules, "shutter3d." + name, raising=False)
        monkeypatch.delattr(shutter3d, name, raising=False)
    kodi = importlib.import_module("shutter3d.kodi")
    k = Kodi(tmp_path)
    monkeypatch.setattr(kodi, "rpc", k.rpc)
    monkeypatch.setattr(kodi.xbmc, "getInfoLabel", lambda label: k.fps)
    monkeypatch.setattr(kodi.xbmcvfs, "translatePath", lambda path: k.path)
    s = kodi.Service.__new__(kodi.Service)
    s.settings = kodi.Settings()
    s.link = types.SimpleNamespace(in_3d=False, set_delay=lambda value: None)
    s.delay, s.delay_misses = None, 0
    s.item_rate = s.rate_seen = s.rate = None
    s.armed, s.hold_cancel = False, None
    s.delays = kodi.load_delays()
    return kodi, s, k


def tick(s, times=1):
    for _ in range(times):
        s._follow_rate()
        s._follow_delay()


def film(s, k, fps):
    """A new 3D film at <fps>, its rate settled."""
    s._player_event("Player.OnPlay")
    k.fps, s.link.in_3d = fps, True
    tick(s, 2)


def test_film_rate(monkeypatch, tmp_path):
    film_rate = service(monkeypatch, tmp_path)[0].film_rate
    assert film_rate("23.976") == "23.976"
    assert film_rate("23.97") == "23.976"        # measured
    assert film_rate("24") == "24"
    assert film_rate("29.97") == "29.97"
    assert film_rate("47.952") == "47.952"
    assert film_rate("0") is None and film_rate("") is None


def test_each_rate_keeps_its_delay(monkeypatch, tmp_path):
    kodi, s, k = service(monkeypatch, tmp_path)
    k.delay = 1300
    film(s, k, "23.976")
    assert s.delays == {"23.976": 1300}           # first time at a rate: the delay in use
    k.delay = 1500                                 # moved on the slider
    tick(s)
    assert s.delays == {"23.976": 1500}
    s._player_event("Player.OnStop")
    s.link.in_3d = False
    tick(s)
    k.delay = 0                                    # set in System settings, out of 3D
    tick(s)
    assert s.delays == {"23.976": 1500}
    film(s, k, "25")
    k.delay = 900
    tick(s)
    assert s.delays == {"23.976": 1500, "25": 900}
    s._player_event("Player.OnStop")
    s.link.in_3d = False
    tick(s)
    film(s, k, "23.976")                           # back to a 23.976 film
    assert k.delay == 1500 and s.delay == 1500
    assert kodi.load_delays() == {"23.976": 1500, "25": 900}


def test_the_rate_settles_once_per_film(monkeypatch, tmp_path):
    kodi, s, k = service(monkeypatch, tmp_path)
    s.delays = {"23.976": 1500, "25": 900}
    k.delay, k.fps, s.link.in_3d = 0, "0", True    # Kodi has no rate yet
    s._player_event("Player.OnPlay")
    tick(s, 3)
    assert s.rate is None and k.delay == 0 and s.delays == {"23.976": 1500, "25": 900}
    k.fps = "25"
    tick(s)                                        # once is not settled
    assert k.delay == 0
    tick(s)
    assert k.delay == 900
    k.fps = "24.5"                                 # a measured rate later in the film
    k.delay = 1000
    tick(s, 3)
    assert s.delays == {"23.976": 1500, "25": 1000}


def test_a_refused_delay_is_dropped(monkeypatch, tmp_path):
    kodi, s, k = service(monkeypatch, tmp_path)
    s.delays = {"25": 9000}
    k.delay, k.fail_sets = 1300, True
    film(s, k, "25")
    assert s.delays == {"25": 1300} and k.delay == 1300
    k.delay = 1400
    tick(s)
    assert s.delays == {"25": 1400}



def test_the_delay_message():
    from shutter3d import core
    assert core.delay_message("23.976", -400) == "delay_23p976_-400"
    assert core.parse_delay_message("delay_23p976_-400") == ("23.976", -400)
    assert core.parse_delay_message("delay_25_900") == ("25", 900)
    assert core.parse_delay_message("test_96000") is None
    assert core.parse_delay_message("delay_25_x") is None
    assert core.clamp_delay(1234) == 1200 and core.clamp_delay(-99999) == -10500


def test_a_delay_set_in_the_settings(monkeypatch, tmp_path):
    kodi, s, k = service(monkeypatch, tmp_path)
    applied = []
    s.link.set_delay = applied.append
    s._publish = lambda: None                      # the screen's state: not under test
    k.delay = 1300
    s.onNotification("service.shutter3d.allolive", "Other.delay_25_900", "")
    assert s.delays == {"25": 900} and kodi.load_delays() == {"25": 900}
    assert k.delay == 1300 and applied == []       # no 3D at 25 fps: kept for later
    film(s, k, "23.976")
    s.onNotification("service.shutter3d.allolive", "Other.delay_23p976_-460", "")
    assert s.delays["23.976"] == -500              # Kodi's 100 us steps
    assert k.delay == -500                         # playing at that rate: into Kodi at once
    s._follow_delay()
    assert applied[-1] == -500                     # and to the emitter on the next tick


def test_the_tuning_panel_has_the_lens_settings_reread(monkeypatch, tmp_path):
    kodi, s, k = service(monkeypatch, tmp_path)
    reread = []
    s.onSettingsChanged = lambda: reread.append(1)
    s._publish = lambda: None
    s.onNotification("service.shutter3d.allolive", "Other.lens", "")
    assert reread == [1]
