"""Makes resources/icon.png: a happy face in modern shutter glasses, winking
through the shut lens, over a 3D logo standing out in 3D. 512x512 PNG.
Needs pycairo and the Ubuntu font. Usage: python3 icon.py icon.png"""
import math
import sys

import cairo

S = 512
out = sys.argv[1]
surf = cairo.ImageSurface(cairo.FORMAT_RGB24, S, S)
c = cairo.Context(surf)

# a cinema-night sky: purple to pink
g = cairo.LinearGradient(0, 0, S, S)
g.add_color_stop_rgb(0, 0.36, 0.16, 0.72)
g.add_color_stop_rgb(1, 0.95, 0.30, 0.55)
c.set_source(g)
c.paint()
# rays from behind the face
c.save()
c.translate(S / 2, 206)
for _ in range(16):
    c.rotate(math.pi / 8)
    c.move_to(0, 0)
    c.line_to(-40, -400)
    c.line_to(40, -400)
    c.close_path()
c.set_source_rgba(1, 1, 1, 0.07)
c.fill()
c.restore()


def star(x, y, r, colour):
    c.save()
    c.translate(x, y)
    c.move_to(0, -r)
    for i in range(1, 8):
        a = i * math.pi / 4
        rr = r if i % 2 == 0 else r * 0.32
        c.line_to(rr * math.sin(a), -rr * math.cos(a))
    c.close_path()
    c.set_source_rgb(*colour)
    c.fill()
    c.restore()


fx, fy, fr = S / 2, 206, 166

# shadow and face
c.arc(fx + 8, fy + 14, fr, 0, 2 * math.pi)
c.set_source_rgba(0.2, 0.0, 0.25, 0.35)
c.fill()
face = cairo.RadialGradient(fx - 60, fy - 80, 20, fx, fy, fr)
face.add_color_stop_rgb(0, 1.0, 0.92, 0.45)
face.add_color_stop_rgb(1, 1.0, 0.72, 0.12)
c.arc(fx, fy, fr, 0, 2 * math.pi)
c.set_source(face)
c.fill_preserve()
c.set_source_rgb(0.55, 0.30, 0.05)
c.set_line_width(6)
c.stroke()

# cheeks
for cx in (fx - 112, fx + 112):
    c.save()
    c.translate(cx, fy + 52)
    c.scale(1.4, 0.8)
    c.arc(0, 0, 20, 0, 2 * math.pi)
    c.restore()
    c.set_source_rgba(1.0, 0.40, 0.40, 0.45)
    c.fill()

# big smile
c.move_to(fx - 92, fy + 50)
c.curve_to(fx - 70, fy + 150, fx + 70, fy + 150, fx + 92, fy + 50)
c.close_path()
c.set_source_rgb(0.45, 0.10, 0.12)
c.fill_preserve()
c.save()
c.clip()
c.arc(fx, fy + 140, 46, 0, 2 * math.pi)
c.set_source_rgb(1.0, 0.45, 0.50)
c.fill()
c.rectangle(fx - 92, fy + 44, 184, 22)
c.set_source_rgb(1, 1, 1)
c.fill()
c.restore()
c.move_to(fx - 92, fy + 50)
c.curve_to(fx - 70, fy + 150, fx + 70, fy + 150, fx + 92, fy + 50)
c.close_path()
c.set_source_rgb(0.55, 0.30, 0.05)
c.set_line_width(6)
c.set_line_join(cairo.LINE_JOIN_ROUND)
c.stroke()


ly = fy - 40
gw, gh = 300, 96                     # the visor, both lenses in one piece


def visor_path():
    """A wraparound visor: a flat top, the lenses sweeping down and out, a
    notch over the nose."""
    left, right, top, bot = fx - gw / 2, fx + gw / 2, ly - gh / 2, ly + gh / 2
    c.new_sub_path()
    c.move_to(left + 18, top)
    c.line_to(right - 18, top)
    c.curve_to(right + 6, top, right + 8, top + 20, right, top + 46)
    c.curve_to(right - 12, bot - 6, right - 40, bot, right - 74, bot)
    c.curve_to(fx + 44, bot, fx + 24, bot - 4, fx + 16, bot - 26)
    c.curve_to(fx + 8, bot - 40, fx - 8, bot - 40, fx - 16, bot - 26)
    c.curve_to(fx - 24, bot - 4, fx - 44, bot, left + 74, bot)
    c.curve_to(left + 40, bot, left + 12, bot - 6, left, top + 46)
    c.curve_to(left - 8, top + 20, left - 6, top, left + 18, top)
    c.close_path()


# arms to the sides of the head
c.set_line_cap(cairo.LINE_CAP_ROUND)
c.set_line_width(12)
c.set_source_rgb(0.10, 0.10, 0.14)
c.move_to(fx - gw / 2 + 6, ly - gh / 2 + 16)
c.line_to(fx - fr + 2, ly - gh / 2 + 6)
c.move_to(fx + gw / 2 - 6, ly - gh / 2 + 16)
c.line_to(fx + fr - 2, ly - gh / 2 + 6)
c.stroke()

lx, rx = fx - 72, fx + 72
# left half: clear, cool tint, the open eye behind it
c.save()
visor_path()
c.clip()
c.rectangle(0, 0, fx, S)
c.clip()
c.set_source_rgba(0.70, 0.93, 1.0, 0.50)
c.paint()
c.save()
c.translate(lx + 2, ly + 4)
c.scale(1.0, 1.15)
c.arc(0, 0, 30, 0, 2 * math.pi)
c.restore()
c.set_source_rgb(1, 1, 1)
c.fill()
c.arc(lx + 10, ly + 8, 18, 0, 2 * math.pi)
c.set_source_rgb(0.12, 0.08, 0.20)
c.fill()
c.arc(lx + 17, ly + 1, 6, 0, 2 * math.pi)
c.set_source_rgb(1, 1, 1)
c.fill()
c.restore()
# right half: shut, glossy black, the wink glowing through
c.save()
visor_path()
c.clip()
c.rectangle(fx, 0, S - fx, S)
c.clip()
dark = cairo.LinearGradient(fx, ly - gh / 2, fx + gw / 2, ly + gh / 2)
dark.add_color_stop_rgb(0, 0.16, 0.14, 0.26)
dark.add_color_stop_rgb(1, 0.03, 0.02, 0.07)
c.set_source(dark)
c.paint()
c.move_to(rx - 30, ly + 8)
c.curve_to(rx - 14, ly - 12, rx + 14, ly - 12, rx + 30, ly + 8)
c.set_source_rgba(1.0, 0.85, 0.35, 0.9)
c.set_line_width(7)
c.stroke()
c.restore()
# a glossy streak across the whole visor
c.save()
visor_path()
c.clip()
c.set_source_rgba(1, 1, 1, 0.35)
c.move_to(fx - gw / 2, ly - 6)
c.line_to(fx - gw / 2 + 60, ly - gh / 2)
c.line_to(fx - gw / 2 + 84, ly - gh / 2)
c.line_to(fx - gw / 2, ly + 16)
c.close_path()
c.fill()
c.set_source_rgba(1, 1, 1, 0.16)
c.move_to(fx + 30, ly + gh / 2)
c.line_to(fx + 110, ly - gh / 2)
c.line_to(fx + 124, ly - gh / 2)
c.line_to(fx + 44, ly + gh / 2)
c.close_path()
c.fill()
c.restore()
# thin dark rim, a cyan line along the top, the emitter's little sensor
visor_path()
c.set_source_rgb(0.08, 0.08, 0.12)
c.set_line_width(8)
c.set_line_join(cairo.LINE_JOIN_ROUND)
c.stroke()
c.move_to(fx - gw / 2 + 20, ly - gh / 2 + 1)
c.line_to(fx + gw / 2 - 20, ly - gh / 2 + 1)
c.set_source_rgb(0.30, 0.88, 1.0)
c.set_line_width(4)
c.stroke()
c.arc(fx, ly - gh / 2 + 10, 5, 0, 2 * math.pi)
c.set_source_rgb(0.30, 0.88, 1.0)
c.fill()

# the 3D logo, extruded, standing out of the picture
c.select_font_face("Ubuntu", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
c.set_font_size(150)
ext = c.text_extents("3D")
tx = S / 2 - ext.width / 2 - ext.x_bearing
ty = 470
depth = 22
for d in range(depth, 0, -1):
    c.move_to(tx + d * 0.9, ty + d * 0.9)
    c.text_path("3D")
    k = d / depth
    c.set_source_rgb(0.55 - 0.25 * k, 0.10, 0.45 - 0.15 * k)
    c.fill()
c.move_to(tx, ty)
c.text_path("3D")
tg = cairo.LinearGradient(0, ty - ext.height, 0, ty)
tg.add_color_stop_rgb(0, 1.0, 1.0, 1.0)
tg.add_color_stop_rgb(1, 1.0, 0.86, 0.30)
c.set_source(tg)
c.fill_preserve()
c.set_source_rgb(0.40, 0.08, 0.35)
c.set_line_width(4)
c.stroke()

star(40, 52, 20, (1.0, 0.95, 0.55))
star(470, 48, 16, (1.0, 1.0, 1.0))
star(470, 250, 18, (1.0, 0.95, 0.55))
star(42, 270, 14, (1.0, 1.0, 1.0))
star(64, 420, 20, (1.0, 0.95, 0.55))
star(450, 420, 16, (1.0, 1.0, 1.0))

surf.write_to_png(out)
