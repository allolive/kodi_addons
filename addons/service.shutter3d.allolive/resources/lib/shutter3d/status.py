"""What the screen says about the emitter. No Kodi imports."""

GREEN, GREY = "FF3FD37A", "grey"

GLASSES = ("[B]Bluetooth glasses (the Pi):[/B] to pair a new pair, play a 3D film or "
           "[B]Test glasses[/B] and hold the glasses' button close to the Pi until the lenses "
           "shutter. Paired glasses come back on by themselves at the next 3D film and go to "
           "sleep shortly after it ends.\n"
           "[B]VESA emitter (the board):[/B] pair the glasses with the emitter as its manual says.\n\n"
           "[B]Tune with test pattern[/B] plays a 3D pattern at the film rate you pick: a white bar "
           "goes back and forth across the screen, the two eyes' bars going opposite ways. Close "
           "one eye: a faint copy of the other eye's bar or label is crosstalk. Over the pattern are "
           "the lens settings and the [B]3D glasses delay[/B] for its rate, as in Settings, "
           "applied as you change them (Left/Right): set the delay where the copy is as faint at "
           "the top of the screen as at the bottom.\n\n"
           "The delay is kept for each film rate. During a film it is also in the video stream "
           "dialog, next to [B]Swap eyes[/B] for depth that looks inside out.")


def summary(state):
    """(colour, title, detail) for the board link's state() dict."""
    path = state.get("board")
    if not path:
        return GREY, "No emitter found", ("Plug the Raspberry Pi Zero W into the box by its inner "
                                          "USB port: the add-on starts it within seconds. A VESA "
                                          "board is found within 30 s.")
    if state.get("kind") == "pi":
        where = "Pi %s, firmware %s, 3D glasses delay %d µs." % (
            path, state.get("build") or "unknown", state.get("delay_us", 0))
    else:
        where = "Board %s, 3D glasses delay %d µs." % (path, state.get("delay_us", 0))
    delays = state.get("delays")
    if delays:
        where += " By frame rate: %s." % ", ".join(
            "%s fps %d µs" % (rate, delays[rate]) for rate in sorted(delays, key=float))
    if state.get("in_3d"):
        return GREEN, "On: 3D film", "The emitter follows the box. " + where
    if state.get("test_hz"):
        return GREEN, "On: glasses test at %g Hz" % state["test_hz"], (
            "The emitter alternates the glasses without a film. " + where)
    return GREY, "Standby", "Drives the emitter when a 3D film plays. " + where
