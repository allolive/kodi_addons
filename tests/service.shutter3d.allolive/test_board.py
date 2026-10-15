"""The USB board's host side: recognising it, and what it is told."""
from shutter3d import board, core, status


def test_recognises_the_board_by_its_status_line():
    assert board.status_line(b"promicro-vesa-stereo 0.1.0 mode 2 delay_us 0 invert 0\r\n")
    assert board.status_line(b"mode 2 delay_us 0 invert 0\r\n")          # older firmware
    assert board.status_line(b"E 0 1 0001f2c3\r\n") is None                  # the light rig
    assert board.status_line(b"") is None


def test_sync_line():
    assert core.sync_line(5, 4567890123, "L") == "S 5 4567890 L\n"
    assert core.sync_line(5, 2**45 * 1000, "-") == "S 5 0 -\n"
    assert core.sync_line(2**32 + 7, (2**32 + 9) * 1000, "R") == "S 7 9 R\n"


class Sent(board.Board):
    def __init__(self):
        super().__init__("/dev/ttyACM0", lambda m: None)
        self.fd = -1
        self.lines = []

    def send(self, text):
        self.lines += text.splitlines()
        return True


def linked(*args):
    link = board.BoardLink(lambda m: None, *args)
    link.set_delay(1500)                 # Kodi's 3D glasses delay, as the service follows it
    return link


def test_the_test_and_back():
    link = linked()
    b = Sent()
    link.test(120.0)
    link._apply_test(b)
    assert b.lines == ["P 1", "F 120000", "D 1500", "I 0", "M 1"]
    link.test(0.0)
    b.lines = []
    link._apply_test(b)
    assert b.lines == ["D 1500", "I 0", "M 2"]     # the emitter stays on a while


def test_the_delay_reaches_the_board_at_once():
    link = linked()
    link.board = b = Sent()
    link.set_delay(2050)
    assert b.lines == ["D 2050", "I 0"]
    assert link.state()["delay_us"] == 2050
    link.test(96.0)
    b.lines = []
    link._apply_test(b)                  # the test keeps it
    assert b.lines == ["P 1", "F 96000", "D 2050", "I 0", "M 1"]


def test_waits_for_a_port_to_come(monkeypatch):
    seen = iter([["/dev/ttyACM0"], ["/dev/ttyACM0"], ["/dev/ttyACM0", "/dev/ttyACM1"]])
    monkeypatch.setattr(board, "ports", lambda: next(seen))
    link = board.BoardLink(lambda m: None)
    waits = []
    monkeypatch.setattr(link.changed, "wait", lambda t: waits.append(t) or False)
    link._wait_for_ports()
    assert waits == [board.PORTS_S, board.PORTS_S]   # back as soon as the port list changed


def test_3d_start_ends_the_test_and_calls_back():
    calls = []
    link = linked(lambda: calls.append(1))
    b = Sent()
    link.test_hz = 120.0
    link._enter_3d(b)
    assert b.lines == ["P 1", "D 1500", "I 0", "M 2", "O 0 0"] and calls == [1]
    assert link.in_3d and link.test_hz == 0.0


def test_a_failing_callback_does_not_stop_the_glasses():
    def boom():
        raise RuntimeError("no player")
    logged = []
    link = board.BoardLink(logged.append, boom)
    link._enter_3d(Sent())
    assert link.in_3d and any("no player" in m for m in logged)


def test_state_and_summary():
    link = linked()
    state = link.state()
    assert state["board"] == ""
    assert status.summary(state)[1] == "No emitter found"
    link.board = Sent()
    state = link.state()
    assert state["board"] == "/dev/ttyACM0"
    assert status.summary(state)[1] == "Standby"
    assert "1500 µs" in status.summary(state)[2]
    link.in_3d = True
    assert status.summary(link.state())[1] == "On: 3D film"
    link.in_3d, link.test_hz = False, 96.0
    assert status.summary(link.state())[1] == "On: glasses test at 96 Hz"


def test_only_usb_cdc_ports_are_asked(monkeypatch):
    monkeypatch.setattr(board.glob, "glob", lambda pattern: {
        "/dev/ttyACM*": ["/dev/ttyACM1", "/dev/ttyACM0"]}.get(pattern, ["/dev/ttyUSB0"]))
    asked = []
    monkeypatch.setattr(board, "probe", lambda port: asked.append(port) or (
        "promicro-vesa-stereo 0.2.0" if port == "/dev/ttyACM1" else None))
    assert board.find() == ("/dev/ttyACM1", "promicro-vesa-stereo 0.2.0")
    assert asked == ["/dev/ttyACM0", "/dev/ttyACM1"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def powered(monkeypatch):
    link = linked()
    link.clock = clock = Clock()
    b = Sent()
    monkeypatch.setattr(board, "find", lambda: ("/dev/ttyACM0", "promicro-vesa-stereo 0.2.0"))
    monkeypatch.setattr(board.Board, "open", lambda self: None)
    link._connect()
    link.board = b
    return link, b, clock


def test_the_emitter_is_off_while_nothing_plays(monkeypatch):
    link, b, clock = powered(monkeypatch)
    link._follow_power(b)                # just connected, no film, no test
    assert b.lines == ["P 0"]
    link._follow_power(b)
    assert b.lines == ["P 0"]            # said once


def test_on_for_3d_and_off_a_minute_after(monkeypatch):
    link, b, clock = powered(monkeypatch)
    link._enter_3d(b)
    assert b.lines[0] == "P 1"
    b.lines = []
    link.in_3d = False                   # the film ended: what the loop does
    link._power_off_later()
    clock.t += board.POWER_OFF_S - 1
    link._follow_power(b)
    assert b.lines == []                 # the next episode may be about to start
    clock.t += 1
    link._follow_power(b)
    assert b.lines == ["P 0"]


def test_a_film_within_the_minute_keeps_it_on(monkeypatch):
    link, b, clock = powered(monkeypatch)
    link._enter_3d(b)
    link.in_3d = False
    link._power_off_later()
    clock.t += 30
    b.lines = []
    link._enter_3d(b)                    # already on: nothing to say
    assert "P 1" not in b.lines and "P 0" not in b.lines
    clock.t += board.POWER_OFF_S
    link._follow_power(b)                # in 3D: never off
    assert "P 0" not in b.lines


def test_the_test_powers_it_too(monkeypatch):
    link, b, clock = powered(monkeypatch)
    link.test(120.0)
    link._apply_test(b)
    assert b.lines[0] == "P 1"
    clock.t += board.POWER_OFF_S * 2
    link._follow_power(b)
    assert "P 0" not in b.lines          # still testing
    link.test(0.0)
    link._apply_test(b)
    clock.t += board.POWER_OFF_S
    link._follow_power(b)
    assert b.lines[-1] == "P 0"


def test_stopping_gives_the_board_its_default(monkeypatch):
    link, b, clock = powered(monkeypatch)
    link._follow_power(b)
    b.close = lambda: None
    link._drop()
    assert b.lines[-1] == "P 1"


def test_the_board_s_lines_are_read_whole(monkeypatch):
    b = board.Board("/dev/ttyACM0", lambda m: None)
    b.fd = 5
    chunks = [b"pi-bt3dsp 0.1.1 build x mode 2\r\nG 00:11", b"", b":22 1 0 80\r\n"]

    def read(fd, n):
        if not chunks:
            raise BlockingIOError
        return chunks.pop(0)
    monkeypatch.setattr(board.os, "read", read)
    assert b.read_lines() == ["pi-bt3dsp 0.1.1 build x mode 2"]    # the half line waits
    assert b.read_lines() == ["G 00:11:22 1 0 80"]
    assert b.read_lines() == []                                     # nothing new: no wait


def test_in_3d_the_pi_is_asked_for_its_status_and_it_is_logged():
    logs = []
    link = board.BoardLink(logs.append)
    link.clock = clock = Clock()
    b = Sent()
    b.kind = "pi"
    b.read_lines = lambda: ["pi-bt3dsp 0.1.1 build x fsi_jump_max_us 12"]
    link._follow_status(b)
    assert b.lines == ["?"] and logs == []
    clock.t += board.STATUS_READ_S
    link._follow_status(b)
    assert logs == ["Pi says: pi-bt3dsp 0.1.1 build x fsi_jump_max_us 12"]
    clock.t += board.STATUS_EVERY_S / 2
    link._follow_status(b)
    assert b.lines == ["?"]                                         # not yet again
    clock.t += board.STATUS_EVERY_S
    link._follow_status(b)
    assert b.lines == ["?", "?"]


def test_a_vesa_board_is_not_asked():
    link = board.BoardLink(lambda m: None)
    link.clock = Clock()
    b = Sent()
    link._follow_status(b)
    assert b.lines == []
