# Active 3D for 120 Hz VRR displays (allolive)

The glasses half of frame-sequential 3D, locked to the box's vsync while Kodi
shows the eyes alternately on a 2D display with game VRR:

- **Bluetooth 3D glasses** through a **Raspberry Pi Zero W** (v1.1, not the
  Zero 2 W) plugged into the box with one USB cable, no SD card. The add-on
  starts the Pi itself with the firmware it carries (the latest
  [pi-bt3dsp-firmware](https://github.com/allolive/pi-bt3dsp-firmware)
  release, bundled when the add-on is built), and the Pi broadcasts the
  shutter timing to the glasses (Hi-Shock RF Pro tested).
- Or a **VESA stereo emitter** (Hi-Shock, Monster, Volfoni and alike) through
  a USB board running the
  [promicro-vesa-stereo firmware](https://github.com/allolive/promicro-vesa-stereo-firmware).

## Needs

- The [CoreELEC yacer-patched build](https://github.com/allolive/CoreELEC/tree/yacer)
  with `18-coreelec-frame_sequential_3d`. Kodi does the
  display side itself: on a TV with game VRR and no HDMI 3D it adds the 3D mode
  **Active 3D for 120 Hz VRR displays** for Blu-ray MVC, side-by-side and top-and-bottom films. It keeps the
  output at 1080p 120 Hz with game mode, slows it with VRR to the film rate,
  and alternates the video's eyes per refresh, with the menus drawn once over
  both. `/sys/class/video/fa_sync` reports each eye, with the vsync's time
  stamped in the video interrupt.
- For Bluetooth glasses: a Raspberry Pi Zero W, v1.1, and a micro-USB data
  cable from the box to the Pi's inner port (labelled USB, not PWR). Nothing
  to install on the Pi.
- Or for a VESA emitter: a Pro Micro 5V (or Leonardo, Micro) with its
  firmware, and an emitter with a VESA stereo input. The firmware's
  [guide](https://github.com/allolive/promicro-vesa-stereo-firmware/blob/main/docs/GUIDE.md)
  covers the parts, the wiring, flashing, and setting it up with this add-on.
- Active 3D for 120 Hz VRR displays as the 3D mode: pick it when Kodi asks at the start of a 3D film, or
  make it the preferred mode (Settings → System → Display → Stereoscopic 3D).
  The yacer build also carries the 3D glasses delay this add-on applies.

## How it works

A Raspberry Pi with no SD card waits on USB in its boot ROM. The service
watches for one and boots it with the firmware the add-on carries, in Python
(the rpiboot protocol over Linux's `/dev/bus/usb`, nothing compiled), within
a few seconds of the Pi being plugged in or the box starting. A Pi still
running an older build, after an add-on update, is told to restart and gets
the new one.

The service then finds the emitter on the box's USB serial ports (the Pi, or
a VESA board) and forwards every vsync to it: its number, the time it was
stamped and the eye. The emitter maps the box clock onto its own, so nothing
on the box's side (USB, Kodi's own load) reaches the glasses' timing. The Pi
turns it into the Bluetooth 3D broadcast, about nine times a second, and the
glasses keep their own timer from it; the board switches its VESA sync pin
in hardware.

Pairing:

- **Bluetooth glasses:** while a 3D film plays, hold the glasses' button close
  to the Pi until the lenses shutter (Hi-Shock RF Pro: up to 20 s). Paired
  glasses come back on by themselves at the next 3D film, and go to sleep
  about ten seconds after it ends.
- **VESA emitter:** pair the glasses with the emitter as its manual says.

## Closing the lenses while the screen redraws (Bluetooth glasses)

The box sends each picture at 120 Hz speed, 1080 of the 1125 lines of a
1080p 120 Hz frame: about 8.0 ms. VRR then lengthens the gap after it to the
film rate (10.4 ms a frame at 95.9 Hz). While the new picture is being drawn
the screen shows parts of both eyes; only in the gap does it show one.

*Settings → Close the lenses while the screen redraws*:

- **On** (the default): both lenses stay closed for the redraw time at the
  start of each eye's frame, then the eye's lens opens until the next frame.
  No crosstalk, but darker than the whole frame (about 2.4 ms open in
  10.4 ms at 95.9 Hz). **Redraw time:**
  - **Automatic:** the time the box takes to send a picture, worked out from
    the HDMI mode in use (8.0 ms at 1080p 120 Hz).
  - **Manual:** in microseconds. More if a faint copy of the other eye
    remains, less for a brighter picture.
- **Off:** each lens open its whole frame. Brightest, with some crosstalk.

To move the window earlier or later, use the 3D glasses delay (below): it
moves every eye switch, the window with it.

## The screen

*Programs → Active 3D for 120 Hz VRR displays* stays open and updates every second:

- **USB board:** not found, standby, on for a 3D film, or on for the glasses
  test, with the board's port and the delay in use. The board is the USB
  serial port (ttyACM) that answers as the firmware; other serial devices are
  never written to. A board unplugged or plugged in is noticed within a few
  seconds.
- **Test glasses / Stop glasses test:** alternates the glasses without a
  film, at the rate a film of 23.976, 24, 25, 29.97 or 30 fps plays at
  (asked each time). A 3D film takes over from it.
- **Tune with test pattern:** plays the add-on's 3D pattern in Active 3D at the
  film rate you pick, with the lens settings and the delay over it (below).

## Tuning the delay

*Settings → 3D glasses delay…* lists the film rates with their delays: pick
one and type the new value in microseconds (negative moves the switch
earlier). A 3D film playing at that rate takes it at once. It works the same
with the Pi and with a VESA board.

The eye switch delay is Kodi's own **3D glasses delay**. While 3D plays, the
allolive skin's video stream dialog has a *3D delay* button showing it, which
opens the same slider as the audio offset, and a *Swap eyes* button. It is also
in the player's video settings, next to the stereoscopic mode and *Invert
stereoscopic 3D mode*, and under System → Display → Stereoscopic 3D. It is a setting for the TV and the glasses, not for a film.
Each step reaches the board within a quarter of a second, and Kodi saves it
when the dialog closes.

It goes from -5300 to 5300 µs in steps of 100 µs, just over half a frame each
way at 95.9 Hz (a frame lasts 10427 µs). A whole frame later is the same as
swapping the eyes, so if it still improves at one end of the range, turn on
*Invert stereoscopic 3D mode* and carry on from the other end.

**Tune with test pattern** asks for a film rate (23.976, 24, 25, 29.97 or
30 fps) and plays that pattern, side by side, which the box runs at four times
the rate (95.9, 96, 100, 119.9 or 120 Hz) like a film. A white bar sweeps right in the left eye and
left in the right eye, and each eye carries its name and the rate. Close one
eye: a faint copy of the other eye's bar or name is crosstalk, and where it
shows (top or bottom of the screen) says which way to move the delay. Over
the pattern, mid-screen so the top and the bottom stay in sight, are the
settings' *Lens opening* group and the 3D glasses delay for the pattern's
rate, as in Settings and with the same help: Left/Right change the focused
one, Select toggles, and each change reaches the emitter at once. Back or Stop
ends the pattern. `resources/media/crosstalk.sh` remakes the patterns with
ffmpeg.

The box sends each picture at 120 Hz speed, in about 8 ms, and the rest of the
frame is a gap in which the whole screen shows one eye: about 2.4 ms at
95.9 Hz, 2 ms at 100 Hz, almost none at 119.9 Hz. Glasses open for the whole
frame always see some of the other eye, and the delay only moves it: set it
where the copy is as faint at the top as at the bottom. Glasses or an emitter
that can shorten the opening can remove it (the Monster Vision BC100 emitter
does, with its joystick): shorten the opening to fit the gap, then move the
delay until the copy is gone. A shorter gap at a higher rate may need a shorter
opening.

The delay is remembered for each film rate: the screen's drawing time is a
different share of the frame at each rate, so the best delay can differ. A 3D
film starts with its rate's delay, and moving the slider during it changes only
that rate's. A rate not tuned yet starts from the delay in use. The screen
lists the remembered delays.

The light rig in the firmware repository (`tools/lightrig`) measures the
display's drawing time and the lenses against the picture, for setting the
delay by numbers rather than by eye.

## Pause at the start of 3D

Shutter glasses take a few seconds to find the emitter. *Pause at the start of
3D* (0 to 10 s, in 0.1 s steps; 0 = off) pauses each film that long once the
box starts showing it in 3D. The box keeps alternating the eyes on the paused
picture, so the glasses sync without missing any of the film. A film you have
paused is left alone, and pressing play during the pause ends it.

## Swapping the eyes

When the depth looks inside out, use Kodi's own *Invert stereoscopic 3D mode*
in the player's video settings. Kodi keeps it per film, and the box tells the
glasses about it, so the add-on has no swap of its own.

## License

Free for personal and other non-commercial use, not for sale: PolyForm
Noncommercial 1.0.0, see `LICENSE.txt`. For commercial use, ask allolive on
GitHub.
