# service.pattern.generator.allolive

> **Work in progress:** the patterns may be inaccurate. If you use them, validate your
> results against another source (a meter reading of a known-good generator, or the
> display's own test patterns).

A lot of this add-on is ported from **[PGenerator+](https://github.com/BigShoots/PGenerator-Plus)**
(BigShoots), itself built on Riccardo Biasiotto's original PGenerator: the protocols
(UPGCI, the G1 RPC, the classic port 85, LightSpace, Resolve), their replies and timing,
the pattern templates and the Dolby Vision metadata all follow its Perl modules, and
the tests run those modules side by side with the add-on to check that they agree.
The Dolby Vision RPU writer follows the layout of quietvoid's
[dovi_tool](https://github.com/quietvoid/dovi_tool) (MIT), and the tests check it byte for
byte against dovi_tool's output. Like PGenerator, the add-on is GPL-3.0-or-later
(`LICENSE.txt`).

Kodi service that answers Calman as PGenerator 2.12.1 does (UPGCI on TCP 2100,
the "Portrait Displays G1" RPC source on TCP 2101, UDP discovery), serves
PGenerator's classic protocol to HCFR and DeviceControl (TCP 85, DeviceControl
discovery on UDP 1977), runs PGenerator's LightSpace / ColourSpace client (UDP
20123 trigger, outbound TCP), and shows each pattern as 10-bit HEVC video (SDR,
HDR10, HLG, Dolby Vision profile 5): the calibration software's patterns follow
each other inside one continuous stream (see below), manual patterns are clips.

Protocol behaviour (framing, replies, command set and parsing, state machine,
replay timing, identity strings, discovery) is PGenerator's, byte for byte.
Pattern colour values follow PGenerator's range choice but are converted
losslessly, as below. The HDMI colour format, bit depth and range are the box's
own (what the player uses for films): Calman's choices are reported back to it but
change neither the link nor how its codes are read, except that its range still
says whether those codes are 0-255 or 16-235.

## Platforms

Written and tested only on CoreELEC on Amlogic (Ugoos AM9 Pro, S905X5). Most of it is
plain Kodi and should run anywhere Kodi does, but that is untested:

| | Any Kodi | Amlogic only |
|---|---|---|
| Protocols (UPGCI, G1 RPC, port 85, LightSpace, Resolve), discovery | yes | |
| Patterns: HEVC Main10 clips, played fullscreen | yes | |
| SDR / HDR10 / HLG / Dolby Vision profile 5 | if the box can output them | |
| **Continuous stream** (signal stays on between patterns, ~0.5 s per pattern) | falls back to one clip per pattern | paced by the Amlogic sync clock (`/sys/class/tsync/pts_video`) |
| **Waiting for the HDR signal** before acknowledging a pattern (and the transmitter's HDR mute) | skipped: acknowledged on Kodi's playback start + settle frames | reads `hdmi_hdr_status` / `hdr_mute_frame` |
| **Waiting out an output-mode change** (Kodi's "delay after change of refresh rate") | skipped | reads `/sys/class/display/mode` |
| **HDMI output readout** (main screen's HDMI OUTPUT card, status line) | shows "not available" | Amlogic transmitter sysfs, below |

The encoder never uses PCM coding units because Amlogic's HEVC firmware rejects them;
that choice decodes everywhere.

## Using it

The generator is off whenever Kodi starts. The service is loaded, but it has no
TCP 2100/2101/85 listener, does not answer UDP discovery (Calman or DeviceControl),
does not listen for LightSpace and does not connect to a Resolve target. Open
Programs -> Pattern Generator opens the add-on's window: the status on the left,
buttons on the right.

- **Start generator** / **Stop generator** switches the network generator, where
  the calibration software picks the pattern and the signal.
- **Show test pattern**: manual patterns, no calibration software (see below).
- **Settings** opens the settings; closing them returns to the window. **Close**.

When the calibration software shows a pattern while the window is open, the window
steps aside for the video and comes back once nothing has played for 1.5 s.

The status reads the network generator's state (stopped, Listening on
<ip>:2100/2101/85, `<software> connected from <pc>` where the software is what the
client calls itself: Calman, HCFR, LightSpace, a `CLIENTNAME=` name, or Client
before it has said anything; Waiting for LightSpace on UDP 20123 when only the UDP
listeners are on, Answering DeviceControl discovery on UDP 1977 when that is all
that is on, Not listening (port in use?) when no TCP port could be opened (UDP 20123
for LightSpace); or Resolve connected to <pc>:<port>), and the live HDMI link (see
HDMI output below). It refreshes
every second.

The settings keep the two ways of showing patterns apart:

**Network generator**:

- **Signal (follows the calibration software)**: the start-up signal until the
  client selects one. Every request is then sent as the signal it asks for; the
  Dolby Vision mapping (Perceptual, Absolute or Relative) is always Calman's choice.
- **Notifications** on signal, mapping and connection changes.

**Network connections**: Calman (UPGCI + G1) or a Resolve target, and the other
listeners (HCFR port 85, LightSpace, DeviceControl; see below).

**Manual patterns** (no calibration software):

- **Show test pattern** (the window's button): pick a pattern and it plays with its name at the top
  left. Left/Right step to the previous/next pattern (both are encoded in advance,
  so a step only switches clips), OK opens a menu to pick the pattern, the signal
  or the max luminance, and Back leaves the patterns and returns to the window.
- **Test pattern signal** (SDR, HDR10, HLG, Dolby Vision) and **Max luminance**
  (400 to 10000 nits, default 4000, HDR10 and HLG): what the manual patterns start
  with; the last ones picked in the OK menu are saved here when you leave them.

**Output (both)**: pattern playback (continuous stream or clips, see below),
resolution and frame rate, clip length (clips only) and the rest, and diagnostics: the
debug overlay and debug logging.

HDMI output is read from the Amlogic transmitter's sysfs nodes
(`/sys/class/amhdmitx/amhdmitx0/` `config`, `disp_mode`, `cs`, `cd`,
`hdmi_hdr_status`, `hdmitx_pkt_dump`, `dc_cap`, `frac_rate_policy`, and
`/sys/class/display/mode`). It shows what is on the wire, not what was requested.
The summary is resolution and scan with the exact refresh rate (23.976 when the
transmitter runs at 1000/1001), the colour format from the AVI infoframe (RGB,
YCC444, YCC422, YCC420), the bit depth, the AVI colorimetry (`nocolorimetry` when
the AVI packet names none, so the TV assumes BT.601/709 from the resolution), the
EOTF (SDR, PQ, HLG, HDR10+, DV, DV-LL, from `hdmi_hdr_status`), and the two AVI
quantization fields as `Q/YQ` (`def`, `lim`, `full`). Q applies to RGB and YQ to
Y'CbCr. The details add the range the TV actually applies, the HDR (DRM)
infoframe values, the TMDS clock and the TV's deep-colour list (`dc_cap`). The
ranges come only from `hdmitx_pkt_dump`: on hdmitx21 (S905X5) the `Colour range`
line in `config` is guessed from the format, not read from the packet, so it is
listed apart and never used. Each node is optional. On a box without one (not
Amlogic) the entry reads "not available" and the status line and overlay leave
it out. Both g12b (hdmitx20) and S905X5 (hdmitx21) formats are parsed.

The debug overlay writes, in light grey on a black box at the bottom right of every
pattern: the pattern request (REQ), the signal with the Dolby Vision mapping,
resolution, frame rate, colorimetry, the HDR10 values (MAXL, MINL, MaxCLL,
MaxFALL) when the clip carries them (HDR10, HLG) and, for Dolby Vision, the static
metadata actually sent, the patch and background Y'CbCr
codes, and the window size and position. Only what the request decides is shown, so
the same request draws the same clip and the cache reuses it (the live HDMI link is
on the main screen). It is part of the picture, so it
changes full-field and APL measurements a little: use it for debugging only.

A manual test pattern is sent in the signal picked in its list. The start-up signal
setting only sets the state before the client selects one (an old "HDR10+" start-up
choice, which no longer exists, falls back to SDR). Apart from the manual patterns'
max luminance, the add-on has no luminance settings:
HDR10 static metadata (and HLG's, which like PGenerator's has no primaries) starts
at PGenerator's stock values (1000 nits peak, 0.005 min, MaxCLL 1000, MaxFALL 400)
until Calman sends its own, and Dolby Vision uses the static metadata below.

Start and Stop last until Kodi restarts. Stop also ends the pattern stream (or clip). When
you change a setting while the generator runs, the listeners restart.

Notifications (about 2 s, can be turned off in the settings) report:

- a change of signal ("Signal: HDR10");
- a change of Dolby Vision mapping;
- the generator starting or stopping;
- a calibration client (named as in the status line) or Resolve connecting or
  disconnecting.

Calman's configuration commands redraw the last pattern, but those redraws show
no notification. A burst of configuration commands therefore produces one
notification, at the next pattern. A notification draws over the pattern, so the
pattern is only acknowledged once Kodi has closed the notification window (about
3 s, longer when a long text scrolls). This also applies to Kodi's own
notifications and when the add-on's are turned off.

## Continuous stream

On an Amlogic box the network generator's patterns are not separate clips. Kodi plays
one endless stream that the add-on serves on 127.0.0.1 (flagged as a live source, so
Kodi starts on a short buffer), and a new pattern is spliced into it at its next frame.
The video decoder never closes, so the signal is never torn down between patterns:
with clips every pattern turned Dolby Vision off and on again and the TV took an
unpredictable time to lock back, during which Calman's settle delay was already
running. Measured on the AM9 (2026-09-27): Dolby Vision held through 10 minutes and
209 pattern changes; a pattern is on screen 0.4-0.5 s after it is asked for once it
has been encoded (a new 4K pattern costs about 0.6 s more to encode, the debug
overlay about 0.8 s on top; encoded patterns are cached, so a repeat is not encoded
again).

- The pattern is acknowledged when the box displays it (the Amlogic sync clock, to
  half a frame), plus the settle frames, not when it was sent.
- Frames are made only as the display consumes them, 0.4 s ahead: under ~0.25 s the
  player runs dry and slows down.
- Until the first picture is up, frames carry 256 KB of filler: the Amlogic codec
  shows nothing before its compressed buffer is 5% full, and a frame the size of the
  whole buffer never enters it at 1080p.
- A change of signal, resolution, frame rate or static metadata (MaxL for HDR10
  and HLG, which the box sends the TV once, at stream start) starts a new
  stream (2-4 s, the HDMI mode changes anyway); the old one keeps running until Kodi
  closes it, since ending it first made Kodi stop the new one. Dolby Vision is the
  exception: its mapping (L255) travels in every frame's metadata and its L1 range is
  fixed, so Perceptual/Absolute/Relative and MaxL/MinL changes stay in the running stream
  (about 0.6 s; the metadata the TV receives was read back on the AM9 after each
  change, 2026-09-27).
- Stopping playback by hand ends the stream; the next pattern starts a new one.
- "Pattern playback" in the settings chooses it ("Continuous stream (signal stays
  on)", the default) or "One clip per pattern", the previous way; a box without the
  Amlogic sync clock always uses clips. Every signal stays on in the stream (SDR,
  HDR10, HLG, Dolby Vision), not only Dolby Vision. "Clip length" is shown only for
  clips, and also sets the manual test patterns' clips, which are clips in both
  modes; the extra (settle) frames and the cache size apply to both.

## HCFR, DeviceControl and LightSpace

Settings -> Connection -> HCFR, DeviceControl and LightSpace (each on by default,
and each only while the generator is started):

- **Classic protocol on TCP 85** (port setting). PGenerator serves every protocol on
  every port, so 85, 2100 and 2101 accept the same commands; a command is a chunk
  ending with STX CR (reply text + STX CR) or ETX (a Calman key, ACK). HCFR and
  DeviceControl use:
  - `SETCONF:<template>:TEMPLATE:<text>` stores a template (CR LF becomes LF);
    `SETCONF:<template>:<KEY>:<value>` puts `KEY=value` first, drops the old `KEY=`
    and `END=1` lines and ends with `END=1`; `TEMPLATERAMDISK` stores it in the
    ramdisk directory that `TESTTEMPLATERAMDISK` reads. For HCFR's own template
    (`SETCONF:HCFR:...`) PGenerator's patch applies: the last `DRAW=` block moves in
    front of the one before it, the first `BG=-1,-1,-1` becomes `BG=DYNAMIC` and a
    later `BG=DYNAMIC` becomes `BG=-1,-1,-1`. Templates live in memory until the
    generator stops; `PatternStart` and `PatternDynamic` are built in. A template name
    is a path, as PGenerator's file names are: it is written as `<name>.tmp` and renamed
    (`SETCONF:A` removes a template `A.tmp`), `../running/tmp/Z` is the ramdisk's `Z`,
    and a name that is a directory (`''`, `.`, `..`) or crosses a missing one (`a/b`)
    stores nothing (for `''`, `.` and `..` the `.tmp` file stays). Every pattern drawn
    (Calman's, `TESTTEMPLATE` after its reply, `TESTPATTERN`, `RGB=`) removes the
    templates whose name ends in any character then `jpg` or `png` (clean_pattern_files,
    pattern.pm:740-746, as in PGenerator). `GETFILELIST:tmp` / `GETFILELIST:running/tmp`
    list the templates (sorted) and `DELETE:tmp:<name>` removes one. As in
    PGenerator (clean_files, command.pm:718-745) every renderer restart
    (`RESTARTPGENERATOR:`, `SET_MODE`, a new `rgb_quant_range`, a Calman mode change,
    `VIDEO=`) deletes the stored templates except `PatternDynamic`, `PatternStart`,
    `MeterProfile`, `MeterPosition`, `ScreenSaver`, `CalmanCustomPattern1`-`4` and those
    whose first line is `PERMANENT=yes`; ramdisk templates stay. A stored
    `PatternStart` is what a restart shows, a stored `PatternDynamic` is what Calman's
    `RGB_B`/`RGB_<other>` draw, and a stored `CalmanCustomPattern1`-`4` is drawn instead
    of the exact keys `STX RGB_B:0020,0020,0020,0000`, `STX RGB_B:1000,1000,1000,1020`,
    `STX RGB_S:0940,0940,0940,018` and `STX RGB_S:0064,0064,0064,018` (variables.pm:303-306;
    a replay rebuilds the key with STX).
  - `GETCONF:<template>:<KEY>` answers the first `KEY=` value, else the whole
    template less its last newline, `OK:` when the template is unknown. As in conf.pm,
    `KEY` (here and in `SETCONF:<t>:<KEY>:<v>`, which drops the lines it matches) is a
    Perl regex in `/^KEY=(.*)/`: `DR.W` finds `DRAW=`, `RGB|DIM` also matches `DIM=`
    anywhere in a line, and a group in the key is the `$1` answered.
  - `TESTTEMPLATE:<template>:<payload>` draws the template (pattern.pm get_pattern):
    the payload is `r,g,b` or `r,g,b;bg;DRAW;w,h;x,y;resolution;frame;text;bits`,
    `X=DYNAMIC||default` takes the payload field, else the default, else
    PGenerator's (RECTANGLE, 640,360, centred, BG 0,0,0, resolution 100, RGB
    16,16,16, "No Text", frame 1, BITS = `$bits_default`: max_bpc as it was when the
    generator started or at Calman's last change of a conf key, 8 in Dolby Vision; the
    classic `CMD:` setters and restarts leave it as it is, conf.pm:88-98). `VAR=name=value`
    substitutes later lines (`name` is a Perl regex, read as Perl reads it: `\h`, `\K`,
    `\p{L}`, `[[:digit:]]`, `\Z`, an unknown escape is its letter), and a template named
    `LUT` ends after its first `RGB=` line (lut() closes the file handle of that name),
    one named `PATTERN` after its first `MACRO=` line that draws (the nested get_pattern
    closes PATTERN) and one named `IDENTIFY` after a `DIM=NATIVE` line in an IMAGE draw
    (open(IDENTIFY) replaces it) (`$RGB`, `$DATE`, `$ETH_INTERFACE` in values and
    `TEXT=`), `DIM=N%` is a centred window of N% of the area, each `END=1` is one
    draw, `BG=-1,-1,-1` draws without clearing (here and in `RGB=`: the last
    background colour stays), a line without a value keeps the
    previous draw's (the renderer keeps DRAW, RGB, BG, POSITION, BITS between draws
    and patterns). The reply is `OK`, `ERR` (unknown template, a DRAW other than
    RECTANGLE/CIRCLE/TRIANGLE/TEXT/IMAGE, a DIM that is not digits or larger than
    the screen, codes that are not digits, a bad position) or `eval denied`
    (`EVAL=`). For HCFR (`TESTTEMPLATE:HCFR:`) the payload's bits field is 8 and
    16/235-anchored codes are a Limited source, as in PGenerator.
  - Shapes, for templates, `TESTPATTERN` and `RGB=`: RECTANGLE (`x,y,w,h`; -1
    centres), CIRCLE (a regular polygon of `resolution` sides around `x,y`, radius
    `w`), TRIANGLE (apex `x,y-w`, base `x-w..x+w` at `y+w`), TEXT (baseline at `y`;
    -1 centres it on the screen), IMAGE (the background only: there are no image
    files). Shapes are rasterised on even pixels (2x2 blocks sampled at their
    centre) for 4:2:0.
  - `CMD:` getters: `GET_STATUS`, `GET_RESOLUTION`, `GET_REFRESH`, `GET_OUTPUT_RANGE`,
    `GET_HDMI_INFO` (`MODETEST (<idx>) RGB full 16:9, 1920x1080 @ 23.98Hz,
    progressive`), `GET_MODE` and `GET_MODES_AVAILABLE` (base64; rows like
    `36[1920x1080 23.98Hz 74.18MHz phsyncpvsync]` from the assumed TV mode list, see
    CONF_FORMAT below), `GET_PGENERATOR_CONF_<KEY>`/`_ALL`, `GET_PGENERATOR_VERSION`,
    `GET_HOSTNAME`, `GET_DEVICE_MODEL`, `GET_CPU`, `GET_TEMPERATURE`, `GET_UP_FROM`,
    `GET_LA`, `GET_FREE_MEM`, `GET_EDID_INFO` (base64 "No info available" for both
    connectors, as PGenerator without edidparser), `GET_IP-<if>`/`GET_MAC-<if>` (None
    for an interface without an Ethernet address, such as `lo`),
    `ETH`/`ETHMAC`/`WIFI`/`WIFIMAC` (the MACs end in `BRD`, as PGenerator's greedy
    get_mac regex captures them), `BTMAC` (`hcitool dev`, else None), `GET_DISCOVERABLE`,
    `GET_SCALING_GOVERNOR`/`_AVAILABLE`/`_CUR_FREQ`, `GET_CPU_INFO`/`_HARDWARE`/`_REVISION`/
    `_SERIAL` (/proc/cpuinfo's Hardware, Revision and Serial: all three, then one each),
    `FREE_DISK`/`GET_FREE_DISK`
    (`df -kh`), `GET_DMESG` (answered empty: the box's kernel log is not handed out on
    the network), `MULTIPLE:a:b`. `GET_DEVICE_MODEL` keeps the device
    tree's trailing NUL. `GET_MODE`, `GET_HDMI_INFO`, `GET_RESOLUTION`, `GET_REFRESH` and
    Calman's GET_SETTINGS report the mode of the conf `mode_idx`, interlaced ones
    included (get_hdmi_info, command.pm:1335-1415, which the device_info thread reruns
    every 5 s), so a stored `MODE_IDX` shows there before the restart that plays it.
    Anything else answers `OK:`. As in PGenerator, a
    `GET_PGENERATOR_CONF_<KEY>` is answered from the device_info thread's cache (info.pm,
    rewritten every 5 s): the raw conf value (`min_luma` in nits) as of the last cycle,
    except between a `SET_` of that key and the next cycle (wire units then);
    `STATSRESET` and `TESTPATTERN` answer 1 when that cycle's cached GET_STATS was still
    there (no pattern, connection or error since), else 0.
  - `CMD:` setters: `SET_PGENERATOR_CONF_<KEY>:<v>` for PGenerator's list (IS_SDR,
    IS_HDR, IS_LL_DOVI, IS_STD_DOVI, EOTF, PRIMARIES, MAX_LUMA, MIN_LUMA in 0.0001-nit
    units, MAX_CLL, MAX_FALL, COLOR_FORMAT, COLORIMETRY, RGB_QUANT_RANGE, MAX_BPC,
    DV_*, MODE_IDX, SIGNAL_MODE, CALMAN_MODE_IDX) write the conf as sent, with DV_METADATA
    and DV_MAP_MODE kept coupled and the DV transport normalised when DV is turned
    on. As in PGenerator the signal follows at the next renderer restart:
    `RESTARTPGENERATOR:` (which normalises the signal mode, shows PatternStart and
    latches the mode, format, range and DV mapping), `SET_MODE:<idx>` /
    `SET_CEA_DMT:<idx>` (a mode index of `GET_MODES_AVAILABLE`, stored as sent and read
    with atoi at the restart, which comes at once; empty picks 2160p30 as
    auto_select_4k_mode does; `mode_idx` reports the stored value; an interlaced mode is
    played as progressive frames of its size and rate). `CALMAN_MODE_IDX` is the mode
    Calman's INIT restores (see CONF_FORMAT below). The Dolby Vision flags
    count as on when they are numerically 1 (`01`, `1.0`), and `COLOR_FORMAT` and
    `RGB_QUANT_RANGE` are read with atoi (also by `GET_OUTPUT_RANGE`/`GET_HDMI_INFO`), as
    PGenerator does. The renderer's HDR switch is atoi(`IS_HDR`) as a truth value (`2`
    and `-1` are HDR; `SIGNAL_MODE` is still written from `IS_HDR` == 1), and
    `COLORIMETRY` goes on the wire as drm_override.so reads it: its leading digits only
    (`+9` and ` 9` are 0, `1e1` is 1).
    `SET_REFRESH:<rate>` answers `OK:ERR:Error with tvservice`, as PGenerator on KMS.
    The luminance keys take effect on the pattern shown, re-encoded in HDR10 and DV.
    `SET_DISCOVERABLE:0/1` switches the discovery answers and, like PGenerator's
    `DISCOVERABLE.disabled` file, lasts across restarts (a file in the add-on's data
    folder). REBOOT, HALT and the
    system settings (host name, Wi-Fi, boot config, GPU memory) are ignored.
  - The client's name is kept as PGenerator's status keeps it: Calman, HCFR,
    DeviceControl, the name given by `CLIENTNAME=`/`GETSTATUS:CLIENTNAME=`, or
    LightSpace (`upgci.Server.client()`). The end of a LightSpace session clears it,
    whichever client set it meanwhile (discovery.pm:76-79).
- **DeviceControl discovery**: a datagram to UDP 1977 containing `Who is a
  PGenerator` gets `I am a PGenerator <name>` back to the sender (the name as for
  Calman's discovery, cut to 24 bytes).
- **LightSpace / ColourSpace**: LightSpace sends a UDP datagram to port 20123
  containing `LS:` and ending with `:<port>`; the generator connects out to the
  sender on that TCP port (the listener closes meanwhile: one session at a time; the
  port is read as IO::Socket::INET reads it: `name(NNN)`, a service name, a number
  above 65535 truncated). Colours and geometry are read as XML::Simple's XMLin gives
  them to client.pm: an element with attributes or a repeated name becomes
  `HASH(0x..)`/`ARRAY(0x..)`, which the template refuses (a coordinate reads 0);
  element names are taken as written (no namespace expansion: `xmlns` is an attribute,
  `a:shapes` is not `shapes`).
  Each received chunk holding `<calibration>...</calibration>` is a pattern: one
  `<rectangle>` is the patch on black, two are the background then the patch;
  colours come from `<color>` (8-bit) or `<colex bits="..">`, geometry `x y cx cy`
  from fractions of the screen (a decimal comma is accepted). The patch is drawn
  through PatternDynamic at full range, a repeat of the same message is skipped, and
  nothing is sent back except `Command:IsAlive` after 5 s of silence; another 5 s
  of silence ends the session. When it ends, the range is released, a black full
  field is drawn and the UDP listener opens again. This is not the add-on's Resolve
  client (Settings -> Resolve), which PGenerator configures by hand and frames with
  a 4-byte length.

## Deviations from PGenerator

Line numbers are PGenerator 2.12.1's `daemon.pm` unless another file is named.

- **Codes read at their own depth** (daemon.pm:76-93). PGenerator shifts every code
  to `max_bpc` (8-bit by default) before drawing. That drops two bits of a 10-bit
  `RGB_*` code and bends full-range values (8-bit 128 becomes 512/1023). The add-on
  turns each code directly into E' from its own depth: 10-bit for `RGB_*`/`YCC_*`,
  10 or 8-bit for `CommandRGB` (by tenBit), 8-bit for chart bytes. Limited range
  is (c - 16·2^(b-8)) / (219·2^(b-8)) and full range is c / (2^b - 1). `max_bpc`
  is only reported (GET_SETTINGS).
- **Dolby Vision**: none since 0.4.13. PGenerator scales codes to 12-bit (`v<<2`,
  `v<<4` for 8-bit) and its tunnel shows them as limited range, E' = (c12 - 256)/3504:
  64 is black and 940 peak white in 10-bit terms, which is what Calman sends. The
  add-on draws the same E' (until 0.4.12 it read them as full range, lifting black to
  ~6% PQ).
- **No limited-range clamp** (ofApp.cpp:648-651, selected by daemon.pm:369-372).
  Below-black and above-white codes, including the BRIGHTNESS/CONTRAST chart bars,
  reach the encoder. The encoder clips only to the reserved-code limits 4..1019.
  PGenerator's internal black (reset background, CommandRGB surround) stays black.
- **Stored background keeps its depth** (daemon.pm:479-481). `calman_bg` is stored
  as codes plus their depth, so a later BITD or DV switch cannot reinterpret it.
- **APL surround**: none since 0.4.14 - PGenerator's calman_apl_bg_value arithmetic
  exactly (codes scaled to the target depth, levels of the conf's wire range, full in
  Dolby Vision, rounded to a whole code at that depth), checked against its own Perl
  over 420 cases by the test suite. Only YCC_* patches, which PGenerator does not draw,
  keep the add-on's own surround.
- **Unknown SPECIALTY grey** (daemon.pm:551). Drawn as 8-bit 128 at its own depth:
  128/255 full range, or (128-16)/219 limited.
- **HDR10 chart levels** (webui.pm:11817, 11838). The PQ peak scaling is kept, but
  the peak and levels are not rounded to 8-bit. Each level is rounded to a whole
  10-bit code in its range.
- **YCC_S / YCC_A / YCC_B are drawn** (daemon.pm:2393 ACKs them without drawing,
  though STATUS at daemon.pm:1095 advertises them). They are handled like
  `RGB_S/RGB_A/RGB_B` (daemon.pm:2171-2176), with the same fields, window, sticky
  background, replay and ACK timing; like PGenerator they still pass the
  source-range line at daemon.pm:2182. The codes are 10-bit Y'CbCr, read in the
  pattern range. Limited codes are encoded unchanged; full-range codes are
  rounded once to limited 10-bit. The `YCC_B` background is (Y, 512, 512). The
  payload layout is inferred from `RGB_*`, because PGenerator has no reference.
- **Window geometry** (daemon.pm:500-501). The size and position are rounded to
  even pixels for 4:2:0 HEVC, instead of `int(sqrt(p)·max)`.
- **MAXL/MINL/MAXCLL/MAXFALL** (daemon.pm:2058-2089 only saves them). In HDR10 and
  DV the picture on screen is re-encoded before the ACK, because the clip carries the
  metadata; nothing is redrawn and the source range is left alone.
- **Static Dolby Vision metadata**, the same on every frame of every
  pattern: always PGenerator's fixed Dolby Vision range, 4000 and 0.005 nits (its
  dv_maxpq/dv_minpq 3696/62), whatever MaxL/MinL the client sends - PGenerator never
  takes them from Calman, and Calman's DV workflow (Relative mapping measures against
  L1) is built around that. The HDR10 values still follow Calman.
  - Dolby Vision: PGenerator's DM metadata. Source range and L1 = PQ12(min),
    PQ12(peak) and their mean rounded down, and L255 = Calman's mapping
    (Perceptual/Absolute/Relative), else PGenerator's default Relative. Nothing
    else: an LG G5 shows a black screen, menus
    included, for L255 next to L5 and L6, while L1 + L255 and L1 + L5 + L6 both
    display (tested 2026-09-26).
- **No output mapping.** Every request is sent as the signal it asks for: a Dolby
  Vision request is Dolby Vision, an HDR10 request HDR10.
- **Classic protocol and LightSpace** (pattern.pm, conf.pm, command.pm, client.pm,
  ofApp.cpp):
  - Template, `TESTPATTERN`, `RGB=` and LightSpace codes are read at their own depth
    (BITS, the draw's `10bit` suffix, colex bits; in Dolby Vision the payload's) and
    never shifted to 12-bit (daemon.pm:670-690). In Dolby Vision this covers only the
    `RGB=`/`TESTPATTERN` colour, `RGB=` background and template payload colour and
    background that PGenerator shifts (each on its own: exactly three digit fields); the
    rest of the file (a template's own RGB=/BG=, the defaults, a triplet passed through
    unshifted such as `+255,0,0` or `1,2,3,0`) is read as 12-bit codes under
    SOURCE_MAX=4095, as in PGenerator, and `$RGB` in TEXT shows PGenerator's shifted codes.
    BITS=11 to 16 outside Dolby Vision
    are read at that depth, in templates as in `RGB=RECTANGLE16bit` (the renderer
    draws anything but 10 as 8-bit). A suffix below 9 bits (`RECTANGLE6bit`) takes
    8-bit codes, as PGenerator accepts and draws them. LightSpace's `<color>` is read
    as 8-bit, not at `max_bpc`. With a colex patch, LightSpace's background is drawn at
    the patch's depth from its own colex at that depth, else from its 8-bit `<color>`
    scaled to the nearest code (0 and 255 exact); PGenerator reads the 8-bit
    `<color>` values as codes of that depth (128 on a 10-bit patch is 12.5%, not 50%).
  - A pattern file with several frames (`FRAME=` lines) shows its first frame; the
    renderer animates them.
  - `MACRO=<template>` includes that template's shapes (PGenerator draws it and
    immediately overwrites it with the calling template), at most 8 deep.
  - Refused rather than crashing or running code: `EVALPATTERN=` templates (Perl
    code, ERR), `DIM=<negative>%` (ERR; Perl dies), a line the renderer cannot read
    (a non-integer or missing field; lexical_cast throws and the renderer dies) keeps
    the previous value, an unknown DRAW left to the renderer (`RGB=`, `TESTPATTERN` and
    templates) shows black (it exits, and the next pattern starts a new renderer with
    PGenerator's values reset), LightSpace XML that does not parse (bad XML, invalid
    UTF-8) or whose XMLin result makes client.pm die (a hash lookup on an array) is
    skipped (the session thread dies), and a LightSpace colour the renderer cannot read
    (`RGB=,1,1`) is not drawn, after the range is taken as PGenerator takes it. A template
    key (VAR=, GETCONF, SETCONF) Perl rejects (`\1` without a group, `\p{Foo}`) is taken literally, and so are the
    few it accepts that are not translated (`\G` past the start, `\b{wb}`, `(?i)` past
    the start, `(?|..)`).
  - Template names reach only PGenerator's own directories (`/var/lib/PGenerator`, its
    `tmp`, `running`, `running/tmp` and `frames`); a name leading elsewhere stores and
    finds nothing (PGenerator reads and writes any file there as root). The add-on's
    templates are the only files in them.
  - A NaN pattern code (`RGB_S:nan,...`, `CommandRGB:nan,...`) is drawn as 0, as
    PGenerator draws it when it shifts the code to another depth (at the same depth its
    renderer gets NaN and dies). A NaN window in `RGB_S`/`RGB_A` keeps the window size, as
    PGenerator does, and draws at it (PGenerator's draw is malformed); `10_SIZE:nan` and
    `11_APL:nan` are ignored (PGenerator stores NaN and dies drawing). `SET_MODE` past the
    mode list keeps the mode (PGenerator's renderer indexes out of bounds).
  - `SETCONF:HCFR` with fewer than two DRAW= blocks keeps the text; PGenerator splices
    captures left over from an earlier match. TEXT keeps a `=` in the text (the
    renderer splits the line on every `=`). A TEXT size of 0 or less is drawn as size 1
    (FreeType's 1 pt floor).
  - `GET_REFRESH` answers the refresh rate (PGenerator on KMS answers word 4 of its
    status line, the mode index) and `GET_HDMI_INFO` says `@ 23.98Hz` (PGenerator's
    says `HzHz`).
  - The LightSpace duplicate check is per session (PGenerator's lasts across
    sessions, so a first pattern equal to the session's last one stayed black). The Dolby Vision
    transport is always "standard" (profile 5): LL-DV cannot be sent.
- **Opt-ins, off by default:** blanking on disconnect (PGenerator keeps the
  pattern, daemon.pm:2768), and a `device_name` discovery-name override
  (discovery.pm:89-112).

Known gaps and assumptions:

- HDMI output on g12b (hdmitx20): `disp_mode` there gives only the VIC, so the
  refresh rate comes from the mode name, and 1000/1001 is inferred from
  `frac_rate_policy` (the policy the next mode set applies) for rates divisible by
  6. hdmitx21 reads the live timing. Untested on a g12b box.
- `RGB_B` and `RGB_<other>` draw PGenerator's PatternDynamic template as a centred
  640x360 pixel rectangle (daemon.pm:483, 537; variables.pm:270-271). The template
  file is not in the snapshot, so this is inferred. A PatternDynamic the client stored
  (SETCONF) is drawn as that template. When its colours and depth are all DYNAMIC the
  codes keep their own depth (BITS=10, or SOURCE_MAX=1023 in Dolby Vision) and the APL
  surround is a whole 10-bit code; when it has its own RGB=/BG=/BITS=/SOURCE_MAX=/VAR=/
  MACRO= lines, a `BITS=DYNAMIC||N` (N applies, since PGenerator's payload has no bits
  field) or a TEXT line with `$RGB`, it gets PGenerator's payload, the codes scaled to the pattern depth
  (`max_bpc`, 12-bit with SOURCE_MAX=4095 in Dolby Vision), so its own values read as
  in PGenerator. For `RGB_<other>`, PGenerator
  solves the APL surround for `calman_win_size` rather than for that box; this is
  kept.
- IMAGE and PUSH are advertised (daemon.pm:1095) but ACKed without drawing, as in
  PGenerator (daemon.pm:2393). No payload format is known.
- CONF_HDR picks the mastering primaries from PGenerator's table by red x, as
  PGenerator does (daemon.pm:1855-1868).
- Y'CbCr formats read codes in the wire range, as PGenerator's YCbCr shader does
  (ofApp.cpp:626-627). This is parity, not a deviation. 4:2:0 is assumed to behave
  like 4:4:4.
- Precision floor: every E' is encoded once as 10-bit limited Y'CbCr 4:2:0.
  Limited-range greys are exact. Full-range and non-grey values get one rounding.
- PGenerator's renderer reads the mode, colorimetry, mastering primaries, format,
  range and DV mapping only when it starts (main.cpp:115-154). The add-on keeps that
  latch: a saved setting shows on the next restart (an apply that changes a mode key,
  a new `rgb_quant_range`, a new HDMI mode). As in PGenerator, a restart shows
  PatternStart (as shipped a black full field; command.pm:525, 642), until the pattern is
  replayed or a new one is drawn. In Dolby Vision the restart also rewrites the DV transport keys
  (command.pm:135-167): `max_bpc` becomes 8 (10 if it was 10), even after an explicit
  BITD:12, and `rgb_quant_range` goes back to Full, until the next apply writes Calman's
  values again. GET_SETTINGS reports these values. This is parity.
- The renderer does not reload a chart image it is already showing (ofApp.cpp:475,
  491). BRIGHTNESS, CONTRAST and ALIGNMENT/OVERSCAN each use one image file. When the
  same chart is drawn again with no other pattern and no restart in between, the old
  levels stay, even after a MAXL change. This is parity.
- INIT restores the conf `calman_mode_idx` (daemon.pm:308-321): CONF_FORMAT writes the
  index of the mode it picks, `CMD:SET_PGENERATOR_CONF_CALMAN_MODE_IDX` sets it, and
  when it is empty, 0 or not digits INIT writes 1080p24's index. It is kept in the
  add-on's data folder, as PGenerator.conf keeps it.
- CONF_FORMAT picks a mode as PGenerator does (daemon.pm:2232-2262: same height and
  scan, `1080i60` an interlaced mode, `int(rate)` equal or within 0.25 Hz, exact width preferred, first listed wins;
  no match changes nothing). A format without a rate above 1080 lines takes the rate of
  the conf `mode_idx`'s mode (`$preferred_mode`, the one GET_MODE reports), even one
  stored by `CMD:` and not yet played. The add-on cannot read the TV's mode list, so it assumes
  a 4K TV's usual CTA-861 set in kernel order (2160p60 preferred; 4096 and 3840x2160,
  1080p and 1080i, 1440p60, 720p, 576p/i, 480p/i, 640x480). As on PGenerator with such
  a TV, 23.976 and 23.98 select 24.00, 59.94 selects 60.00 and 29.97 selects 30.00.
- NaN luminance values (`MAXL:nan`, `MAXCLL:nan`, the same inside `CONF_HDR`) are
  ignored and logged; PGenerator stores them and its daemon later dies drawing an HDR10
  chart (webui.pm:11903). `inf` is stored as PGenerator stores it (its charts clamp it
  to a 10000-nit peak, `-inf` to 1000). An `UPLOAD_FILE` size such as `0.0`
  gets ERR; PGenerator dies dividing by it. Crashes are never emulated.
- Classic protocol (daemon.pm:2396-2692; see HCFR, DeviceControl and LightSpace):
  the replies and their order are PGenerator's. `FUNCTIONS=` finds no function files,
  as in PGenerator. `VIDEO=`, file upload and delete reply as PGenerator does but
  change nothing outside the template directories; the last chunk of a `PLUGINS`
  upload gets `ERR:100%` (PGenerator's set_plugin refuses every archive but one), and
  `DELETE:PLUGINS:<name>` an empty reply (set_plugin dies on the missing archive). An
  upload into a template directory (`UPLOAD_FILE:tmp:<name>:...`) stores the file under
  that name at END_UPLOAD, or unzips a Zip there (the chunks go to one upload buffer
  shared by all connections, as PGenerator's /tmp file); `running/` is a tmpfs, so the
  rename into it fails and nothing is stored. An HTTP GET gets an empty frame listing (404
  when an `HTMLIMAGELIST.disabled` file lies in a directory of the path) or an empty image, then the
  connection is closed. `SETCONF:<t>:TESTCMD:` is taken by the `CMD:` handler first,
  as in PGenerator. TEXT is drawn in the add-on's 5x7 font scaled to the size (cell
  about size/7.5 pixels), not PGenerator's TrueType font; PGenerator's HCFR
  template is not known (HCFR uploads it), so its exact picture is untested.
  Templates are not kept across a generator stop (PGenerator keeps its own and the
  PERMANENT=yes ones on disk).
  The mode list, pixel clocks and mode indices are the assumed TV list's.
