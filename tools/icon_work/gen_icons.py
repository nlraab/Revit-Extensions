# -*- coding: utf-8 -*-
"""Generate the 3D Viewer and Parameters Management toolbar icons (96x96 PNG).

Rendered at 4x then downsampled for clean anti-aliasing. Matches the Clash
Detection family layout: rounded-square solid fill + white glyph, transparent
corners, ~7px inset, small corner radius.
"""
import os
from PIL import Image, ImageDraw

S = 4            # supersample factor
SZ = 96 * S
INSET = 7 * S    # fill margin (matches Clash icons: square spans 7..89)
RAD = 5 * S      # corner radius


def new_canvas():
    img = Image.new("RGBA", (SZ, SZ), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def rounded_bg(draw, color):
    draw.rounded_rectangle([INSET, INSET, SZ - 1 - INSET, SZ - 1 - INSET],
                           radius=RAD, fill=color)


def finish(img, out_path):
    img = img.resize((96, 96), Image.LANCZOS)
    img.save(out_path)
    print("wrote", out_path)


# ---------------------------------------------------------------- 3D Viewer
def make_3d_viewer(out_path, bg):
    img, d = new_canvas()
    rounded_bg(d, bg)

    cx, cy = SZ / 2.0, SZ / 2.0 + 1 * S
    s = 27 * S            # cube half-size
    hs = s / 2.0
    T  = (cx, cy - s)
    R  = (cx + s, cy - hs)
    BR = (cx + s, cy + hs)
    B  = (cx, cy + s)
    BL = (cx - s, cy + hs)
    L  = (cx - s, cy - hs)
    C  = (cx, cy)

    white = (255, 255, 255, 255)
    # solid white cube silhouette (hexagon)
    d.polygon([T, R, BR, B, BL, L], fill=white)

    # interior cube edges drawn back in the bg color to read as a 3D cube
    lw = max(1, int(2.4 * S))
    for a, b in [(C, T), (C, L), (C, R)]:
        d.line([a, b], fill=bg, width=lw)

    # two layered "floor" slices on each front face -> echoes the model viewer
    def lerp(p, q, t):
        return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)
    slw = max(1, int(1.8 * S))
    for t in (0.33, 0.66):
        # left face: between edges C->L (top) and B->BL (bottom)
        d.line([lerp(C, B, t), lerp(L, BL, t)], fill=bg, width=slw)
        # right face: between edges C->R and B->BR
        d.line([lerp(C, B, t), lerp(R, BR, t)], fill=bg, width=slw)

    finish(img, out_path)


# -------------------------------------------------- Parameters Management
def make_parameters(out_path, bg):
    img, d = new_canvas()
    rounded_bg(d, bg)

    white = (255, 255, 255, 255)
    # a parameter "tag": rectangle with a pointed left end + grommet hole,
    # tilted slightly for a label feel. Two short value lines inside.
    # Build in tag-local coords then it's just axis-aligned (clean at small px).
    left = 24 * S
    right = 74 * S
    top = 32 * S
    bot = 64 * S
    tipx = 14 * S
    midy = (top + bot) / 2.0
    rrad = 4 * S
    # tag body: pointed-left pentagon with rounded right corners
    d.polygon([
        (tipx, midy),
        (left, top),
        (right - rrad, top),
        (right, top + rrad),
        (right, bot - rrad),
        (right - rrad, bot),
        (left, bot),
    ], fill=white)
    # round the right corners visually
    d.rounded_rectangle([left, top, right, bot], radius=rrad, fill=white)
    d.polygon([(tipx, midy), (left + 2 * S, top), (left + 2 * S, bot)], fill=white)

    # grommet hole near the tip
    hr = 4 * S
    hx, hy = left + 9 * S, midy
    d.ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill=bg)

    # two value lines (parameter rows) in bg color
    lw = max(1, int(3.0 * S))
    lx0 = left + 20 * S
    lx1 = right - 7 * S
    d.line([(lx0, midy - 7 * S), (lx1, midy - 7 * S)], fill=bg, width=lw)
    d.line([(lx0, midy + 7 * S), (lx1 - 10 * S, midy + 7 * S)], fill=bg, width=lw)

    finish(img, out_path)


if __name__ == "__main__":
    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    # candidate colors
    make_3d_viewer(os.path.join(outdir, "3dviewer_A.png"), (155, 44, 110, 255))  # deep magenta
    make_3d_viewer(os.path.join(outdir, "3dviewer_B.png"), (151, 38, 109, 255))  # pink.700
    make_3d_viewer(os.path.join(outdir, "3dviewer_C.png"), (176, 42, 96, 255))   # redder purple
    make_parameters(os.path.join(outdir, "params_blue.png"), (43, 108, 176, 255))
    make_parameters(os.path.join(outdir, "params_slate.png"), (74, 85, 104, 255))
