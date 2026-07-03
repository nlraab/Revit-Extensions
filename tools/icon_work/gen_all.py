# -*- coding: utf-8 -*-
"""Generate solid-background toolbar icons for every BIM Tools pushbutton.

Family rule (from the Clash Detection icons): a rounded-square solid fill with
~7px inset and a small corner radius, glyph centered on top. Unlike the Clash
family these each get their own vibrant background color, and glyphs may be
multi-colored. Rendered at 4x then downsampled (LANCZOS) for clean edges.

Run:  py -3 tools/icon_work/gen_all.py
Outputs PNGs to tools/icon_work/out/ plus a contact sheet contact.png.
"""
import os
import math
from PIL import Image, ImageDraw, ImageFont

S = 4
SZ = 96 * S
WHITE = (255, 255, 255, 255)


def P(v):
    return int(round(v * S))


INSET = P(7)
RAD = P(5)

# ---- palette (tool -> background color) -------------------------------------
PALETTE = {
    "AlignViews":             (76, 81, 191, 255),    # indigo  #4C51BF
    "Chatbot":                (107, 70, 193, 255),   # violet  #6B46C1
    "Parameters Management":  (43, 108, 176, 255),   # blue    #2B6CB0
    "Revisions Manager":      (197, 48, 48, 255),    # red     #C53030
    "Sheet Manager":          (44, 156, 146, 255),   # teal    #2C9C92
    "SheetSetup":             (26, 54, 93, 255),     # navy    #1A365D
    "View Range Helper":      (45, 55, 72, 255),     # slate   #2D3748
    "View Templates Manager": (85, 60, 154, 255),    # plum    #553C9A
}

# retro / plane accent colors
YELLOW = (246, 224, 94, 255)
GREEN = (104, 211, 145, 255)
BLUE = (99, 179, 237, 255)
ORANGE = (237, 137, 54, 255)
PLANE_TOP = (56, 161, 105, 255)     # green
PLANE_CUT = (245, 101, 101, 255)    # red
PLANE_BOT = (66, 153, 225, 255)     # blue
PLANE_DEPTH = (159, 122, 234, 255)  # purple


def canvas():
    img = Image.new("RGBA", (SZ, SZ), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def bg(d, color):
    d.rounded_rectangle([INSET, INSET, SZ - 1 - INSET, SZ - 1 - INSET],
                        radius=RAD, fill=color)


def finish(img, path):
    img.resize((96, 96), Image.LANCZOS).save(path)


def rrect(d, x0, y0, x1, y1, r, **kw):
    d.rounded_rectangle([P(x0), P(y0), P(x1), P(y1)], radius=P(r), **kw)


def line(d, x0, y0, x1, y1, w, fill):
    d.line([P(x0), P(y0), P(x1), P(y1)], fill=fill, width=max(1, P(w)))


def gear(d, cx, cy, r_out, r_in, teeth, fill, hole_r=None, hole_fill=None):
    pts = []
    for i in range(teeth * 2):
        ang = math.pi * i / teeth
        r = r_out if i % 2 == 0 else r_in
        pts.append((P(cx + r * math.cos(ang)), P(cy + r * math.sin(ang))))
    d.polygon(pts, fill=fill)
    d.ellipse([P(cx - r_in * .75), P(cy - r_in * .75),
               P(cx + r_in * .75), P(cy + r_in * .75)], fill=fill)
    if hole_r:
        d.ellipse([P(cx - hole_r), P(cy - hole_r),
                   P(cx + hole_r), P(cy + hole_r)], fill=hole_fill)


# ---------------------------------------------------------------- glyphs

def g_align(d, c):
    # two white viewports + center double-arrow (sync/align)
    rrect(d, 18, 34, 41, 64, 3, fill=WHITE)
    rrect(d, 55, 34, 78, 64, 3, fill=WHITE)
    # title bars + content lines in bg color
    for x0, x1 in [(18, 41), (55, 78)]:
        line(d, x0 + 4, 41, x1 - 4, 41, 2.4, c)
        line(d, x0 + 4, 50, x1 - 6, 50, 2, c)
        line(d, x0 + 4, 56, x1 - 9, 56, 2, c)
    # center sync double-arrow
    line(d, 41, 49, 55, 49, 2.6, WHITE)
    d.polygon([(P(41), P(49)), (P(46), P(45.5)), (P(46), P(52.5))], fill=WHITE)
    d.polygon([(P(55), P(49)), (P(50), P(45.5)), (P(50), P(52.5))], fill=WHITE)


def g_chatbot(d, c):
    # white speech bubble + 4-point sparkle
    rrect(d, 20, 24, 76, 60, 9, fill=WHITE)
    d.polygon([(P(34), P(58)), (P(34), P(72)), (P(48), P(58))], fill=WHITE)

    def spark(cx, cy, r):
        k = r * 0.34
        d.polygon([(P(cx), P(cy - r)), (P(cx + k), P(cy - k)),
                   (P(cx + r), P(cy)), (P(cx + k), P(cy + k)),
                   (P(cx), P(cy + r)), (P(cx - k), P(cy + k)),
                   (P(cx - r), P(cy)), (P(cx - k), P(cy - k))], fill=c)
    spark(48, 42, 12)
    spark(64, 34, 6)
    spark(34, 48, 5)


def g_params(d, c):
    # parameter tag (matches existing Parameters Management glyph)
    left, right, top, bot = 24, 74, 32, 64
    midy = (top + bot) / 2.0
    d.rounded_rectangle([P(left), P(top), P(right), P(bot)], radius=P(4), fill=WHITE)
    d.polygon([(P(14), P(midy)), (P(left + 2), P(top)), (P(left + 2), P(bot))], fill=WHITE)
    d.ellipse([P(left + 5), P(midy - 4), P(left + 13), P(midy + 4)], fill=c)
    line(d, left + 20, midy - 7, right - 7, midy - 7, 3, c)
    line(d, left + 20, midy + 7, right - 17, midy + 7, 3, c)


def g_revisions(d, c):
    # white revision cloud + delta triangle marker
    bumps = [(34, 40, 11), (46, 36, 12), (60, 40, 11),
             (64, 50, 11), (54, 58, 12), (40, 58, 11), (30, 50, 11)]
    for cx, cy, r in bumps:
        d.ellipse([P(cx - r), P(cy - r), P(cx + r), P(cy + r)], fill=WHITE)
    d.rectangle([P(32), P(44), P(64), P(56)], fill=WHITE)
    # delta triangle
    d.polygon([(P(48), P(42)), (P(40), P(56)), (P(56), P(56))], fill=c)
    d.polygon([(P(48), P(47)), (P(44), P(54.5)), (P(52), P(54.5))], fill=WHITE)


def g_sheetmgr(d, c):
    # clean white stack of sheets + title-block corner (NEW glyph)
    rrect(d, 26, 24, 64, 66, 3, fill=(255, 255, 255, 150))
    rrect(d, 31, 29, 69, 71, 3, fill=(255, 255, 255, 200))
    rrect(d, 36, 34, 76, 78, 3, fill=WHITE)
    # front sheet content + title block
    for i, yy in enumerate((42, 48, 54)):
        line(d, 41, yy, 60, yy, 2, c)
    d.rectangle([P(58), P(60), P(72), P(74)], outline=c, width=max(1, P(1.4)))
    line(d, 58, 67, 72, 67, 1.4, c)
    line(d, 65, 60, 65, 74, 1.4, c)


def g_sheetsetup(d, c):
    # retro: offset colored sheet layers + front sheet w/ lines + orange gear
    rrect(d, 24, 22, 60, 62, 2, fill=YELLOW)
    rrect(d, 30, 28, 66, 68, 2, fill=GREEN)
    rrect(d, 36, 34, 74, 76, 2, fill=WHITE)
    d.rounded_rectangle([P(36), P(34), P(74), P(76)], radius=P(2),
                        outline=(43, 108, 176, 255), width=max(1, P(1.6)))
    NAVY = (26, 54, 93, 255)
    for yy in (42, 48, 54, 60):
        line(d, 41, yy, 69, yy, 1.8, (160, 174, 192, 255))
    line(d, 41, 42, 69, 42, 2.2, (43, 108, 176, 255))
    gear(d, 70, 70, 11, 6.5, 8, ORANGE, hole_r=3.4, hole_fill=WHITE)


def g_viewrange(d, c):
    # two white posts + 4 colored range planes (Top/Cut/Bottom/Depth)
    rrect(d, 24, 24, 28.5, 74, 1, fill=WHITE)
    rrect(d, 67.5, 24, 72, 74, 1, fill=WHITE)
    planes = [(34, PLANE_TOP), (45, PLANE_CUT), (56, PLANE_BOT), (66, PLANE_DEPTH)]
    for yy, col in planes:
        # dashed colored line
        x = 30
        while x < 66:
            line(d, x, yy, min(x + 4, 66), yy, 2.4, col)
            x += 7
        d.ellipse([P(46), P(yy - 2.6), P(51.2), P(yy + 2.6)], fill=col)


def g_viewtemplates(d, c):
    # retro: document w/ template rows (colored) + gear overlap
    rrect(d, 22, 22, 62, 74, 3, fill=(255, 255, 255, 160))
    rrect(d, 30, 26, 70, 78, 3, fill=WHITE)
    rows = [(34, (43, 108, 176, 255)), (41, (104, 211, 145, 255)),
            (48, (237, 137, 54, 255)), (55, (160, 174, 192, 255))]
    for yy, col in rows:
        d.ellipse([P(35), P(yy - 2), P(39), P(yy + 2)], fill=col)
        line(d, 43, yy, 64, yy, 2.2, col)
    gear(d, 64, 66, 12, 7, 8, (49, 130, 206, 255), hole_r=3.6, hole_fill=WHITE)


GLYPHS = {
    "AlignViews": g_align,
    "Chatbot": g_chatbot,
    "Parameters Management": g_params,
    "Revisions Manager": g_revisions,
    "Sheet Manager": g_sheetmgr,
    "SheetSetup": g_sheetsetup,
    "View Range Helper": g_viewrange,
    "View Templates Manager": g_viewtemplates,
}

ORDER = ["AlignViews", "Chatbot", "Parameters Management", "Revisions Manager",
         "Sheet Manager", "SheetSetup", "View Range Helper", "View Templates Manager"]


def render(name):
    img, d = canvas()
    bg(d, PALETTE[name])
    GLYPHS[name](d, PALETTE[name])
    return img.resize((96, 96), Image.LANCZOS)


def contact(outdir):
    cols, cell, pad, labh = 4, 120, 16, 18
    rows = (len(ORDER) + cols - 1) // cols
    W = cols * cell + pad * (cols + 1)
    H = rows * (cell + labh) + pad * (rows + 1)
    sheet = Image.new("RGBA", (W, H), (232, 232, 232, 255))
    dr = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("segoeui.ttf", 13)
    except Exception:
        font = ImageFont.load_default()
    for i, name in enumerate(ORDER):
        r, cc = divmod(i, cols)
        x = pad + cc * (cell + pad)
        y = pad + r * (cell + labh + pad)
        ic = render(name).resize((cell, cell), Image.LANCZOS)
        sheet.alpha_composite(ic, (x, y))
        dr.text((x + 2, y + cell + 2), name, fill=(45, 55, 72, 255), font=font)
    sheet.convert("RGB").save(os.path.join(outdir, "contact.png"))


if __name__ == "__main__":
    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    for name in ORDER:
        render(name).save(os.path.join(outdir, name + ".png"))
    contact(outdir)
    print("wrote", len(ORDER), "icons + contact.png to", outdir)
