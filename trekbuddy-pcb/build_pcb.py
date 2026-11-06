#!/usr/bin/env python3
"""Generate the rough TrekBuddy main PCB (KiCad 7 format, opens in KiCad 7/8/9).

Outputs, next to this script:
  trekbuddy_main.kicad_pcb   board outline, holes, placeholder footprints, antenna keepout
  trekbuddy_layout.svg       labelled top view
  outline_check.png          outline drawn over the enclosure sketch (fit check)

Rough placement only: no pads, nets or traces. Part boxes are body sizes, not land patterns.
"""
import math
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ---- scale from the sketch: the 37.503 mm dimension spans px x=147..611 ----
PX_ORIGIN = (147, 142)              # top-left dimension point = board (0, 0)
MM_PER_PX = 37.5034 / (611 - 147)
KX, KY = 100.0, 100.0               # where board (0, 0) sits in KiCad page coords


def px2mm(p):
    return (round((p[0] - PX_ORIGIN[0]) * MM_PER_PX, 2),
            round((p[1] - PX_ORIGIN[1]) * MM_PER_PX, 2))


# Outline traced from the blue-tinted board shape in enclosure_sketch.jpg (pixels).
OUTLINE_PX = [
    (145, 145), (611, 155),                                   # top edge
    (611, 205), (618, 214), (627, 222), (641, 228), (657, 230),  # notch round top-right boss
    (689, 662),                                               # right edge
    (839, 762),                                               # right lobe tip
    (700, 848),                                               # lobe lower edge
    (686, 840), (678, 830), (663, 820), (642, 815), (621, 820),
    (607, 830), (599, 840), (594, 852), (593, 880), (597, 891), (604, 903),  # bottom-right boss notch
    (340, 897),                                               # bottom edge
    (345, 890), (348, 880), (349, 865), (346, 850), (341, 840),
    (333, 830), (319, 820), (302, 813), (288, 810),           # bottom-left boss notch
]
OUTLINE = [px2mm(p) for p in OUTLINE_PX]

# Two small round holes drawn inside the board on the sketch (measured ~2.0 mm).
HOLES = [("H1", px2mm((566, 175))), ("H2", px2mm((380.5, 865)))]
HOLE_D = 2.2

# ---- parts: ref, label, value/package note, x0, y0, w, h (board mm, top-left corner) ----
PARTS = [
    ("U1", "ESP32-S3", "ESP32-S3-WROOM-2 (18 x 25.5)", 7.0, 1.3, 18.0, 25.5),
    ("U2", "GPS", "u-blox NEO-F10N (12.2 x 16.0)", 26.0, 8.5, 12.2, 16.0),
    ("J3", "GNSS ANT", "U.FL to GNSS antenna", 29.0, 4.2, 3.0, 3.0),
    ("U3", "eMMC", "eMMC BGA-153 (11.5 x 13)", 10.0, 28.0, 11.5, 13.0),
    ("BZ1", "BUZZER", "Passive piezo, SMD 9 x 9", 26.0, 26.5, 9.0, 9.0),
    ("U6", "3V3", "3.3 V buck, ~3 x 3", 18.0, 44.0, 3.0, 3.0),
    ("L2", "L", "3V3 buck inductor", 18.0, 48.0, 3.0, 2.5),
    ("SW1", "PWR", "Power button, side-actuated", 38.6, 26.0, 3.5, 6.0),
    ("SW2", "MIC", "Mic button, side-actuated", 38.6, 33.0, 3.5, 6.0),
    ("J1", "DISPLAY FPC", "Display FPC connector", 22.0, 37.0, 15.0, 4.0),
    ("U7", "IMU", "Bosch BMI270 LGA-14 (2.5 x 3.0)", 13.0, 44.0, 2.5, 3.0),
    ("MK1", "MIC", "I2S MEMS mic (~3.5 x 2.65)", 14.0, 49.0, 3.5, 2.65),
    ("U4", "BQ25895", "TI BQ25895 WQFN-24 (4 x 4)", 27.0, 45.0, 4.0, 4.0),
    ("L1", "L", "BQ25895 inductor, 4 x 4", 33.0, 45.0, 4.0, 4.0),
    ("U5", "FG", "MAX17048 (TDFN-8 2 x 2, package TBC)", 39.0, 46.0, 2.0, 2.0),
    ("J2", "BATT", "LiPo connector, JST-PH 2-pin", 44.0, 47.5, 6.0, 4.5),
    ("J4", "USB-C", "USB-C receptacle (~9 x 7.5)", 21.0, 54.5, 9.0, 7.5),
]

# Parts allowed to cross the board edge on purpose (connector lip to the enclosure cutout).
EDGE_OK = {"J4"}

# Approximate ESP32 antenna keepout: top of U1. Check the WROOM-2 datasheet for the exact size.
ANT_KEEPOUT = [(7.0, 0.6), (25.0, 0.95), (25.0, 7.3), (7.0, 7.3)]


def uid():
    return str(uuid.uuid4())


def k(p):
    return f"(xy {KX + p[0]:.3f} {KY + p[1]:.3f})"


def footprint(ref, label, value, x0, y0, w, h):
    cx, cy = x0 + w / 2, y0 + h / 2
    hw, hh = w / 2, h / 2
    font = min(1.0, max(0.8, min(w, h) * 0.3))
    return f"""  (footprint "TrekBuddy:{ref}_placeholder" (layer "F.Cu")
    (tstamp {uid()})
    (at {KX + cx:.3f} {KY + cy:.3f})
    (descr "{value}")
    (fp_text reference "{ref}" (at 0 {-hh * 0.35:.3f}) (layer "F.SilkS")
      (effects (font (size {font:.2f} {font:.2f}) (thickness 0.12)))
      (tstamp {uid()}))
    (fp_text value "{value}" (at 0 {hh + 0.6:.3f}) (layer "F.Fab")
      (effects (font (size 0.5 0.5) (thickness 0.08)))
      (tstamp {uid()}))
    (fp_text user "{label}" (at 0 {hh * 0.35:.3f}) (layer "F.Fab")
      (effects (font (size {font * 0.8:.2f} {font * 0.8:.2f}) (thickness 0.1)))
      (tstamp {uid()}))
    (fp_rect (start {-hw:.3f} {-hh:.3f}) (end {hw:.3f} {hh:.3f})
      (stroke (width 0.1) (type solid)) (fill none) (layer "F.Fab") (tstamp {uid()}))
    (fp_rect (start {-hw - 0.25:.3f} {-hh - 0.25:.3f}) (end {hw + 0.25:.3f} {hh + 0.25:.3f})
      (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd") (tstamp {uid()}))
  )
"""


def hole(ref, c):
    return f"""  (footprint "MountingHole:MountingHole_2.2mm_M2" (layer "F.Cu")
    (tstamp {uid()})
    (at {KX + c[0]:.3f} {KY + c[1]:.3f})
    (fp_text reference "{ref}" (at 0 -2) (layer "F.SilkS") hide
      (effects (font (size 0.8 0.8) (thickness 0.12))) (tstamp {uid()}))
    (fp_text value "NPTH {HOLE_D} mm" (at 0 2) (layer "F.Fab")
      (effects (font (size 0.5 0.5) (thickness 0.08))) (tstamp {uid()}))
    (fp_circle (center 0 0) (end {HOLE_D / 2 + 0.25:.3f} 0)
      (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd") (tstamp {uid()}))
    (pad "" np_thru_hole circle (at 0 0) (size {HOLE_D} {HOLE_D}) (drill {HOLE_D})
      (layers "*.Cu" "*.Mask") (tstamp {uid()}))
  )
"""


LAYERS = """  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (40 "Dwgs.User" user "User.Drawings")
    (41 "Cmts.User" user "User.Comments")
    (42 "Eco1.User" user "User.Eco1")
    (43 "Eco2.User" user "User.Eco2")
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
"""


def build_pcb():
    out = ['(kicad_pcb (version 20221018) (generator pcbnew)\n',
           '  (general (thickness 1.6))\n',
           '  (paper "A4")\n',
           '  (title_block (title "TrekBuddy main board - rough placement") (rev "0.1"))\n',
           LAYERS,
           '  (setup (pad_to_mask_clearance 0))\n',
           '  (net 0 "")\n']
    pts = " ".join(k(p) for p in OUTLINE)
    out.append(f'  (gr_poly (pts {pts})\n    (stroke (width 0.1) (type solid)) (fill none) '
               f'(layer "Edge.Cuts") (tstamp {uid()}))\n')
    out.append(f'  (gr_text "TrekBuddy main v0.1 - rough placement, 4L 1.6 mm" '
               f'(at {KX + 20:.3f} {KY - 3:.3f}) (layer "Cmts.User") (tstamp {uid()})\n'
               f'    (effects (font (size 1 1) (thickness 0.15))))\n')
    for ref, c in HOLES:
        out.append(hole(ref, c))
    for p in PARTS:
        out.append(footprint(*p))
    kp = " ".join(k(p) for p in ANT_KEEPOUT)
    out.append(f"""  (zone (net 0) (net_name "") (layers "F.Cu" "In1.Cu" "In2.Cu" "B.Cu") (tstamp {uid()})
    (name "ESP32_ANT_KEEPOUT") (hatch edge 0.5)
    (connect_pads (clearance 0))
    (min_thickness 0.25) (filled_areas_thickness no)
    (keepout (tracks not_allowed) (vias not_allowed) (pads not_allowed) (copperpour not_allowed) (footprints allowed))
    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts {kp}))
  )
""")
    out.append(")\n")
    (HERE / "trekbuddy_main.kicad_pcb").write_text("".join(out))


# ---- geometry checks ----
def inside(pt, poly):
    x, y = pt
    c = False
    for i in range(len(poly)):
        (x1, y1), (x2, y2) = poly[i], poly[i - 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def seg_dist(p, a, b):
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def rect_edge_clearance(x0, y0, w, h, poly, step=0.25):
    """Smallest distance from the rectangle perimeter to the outline; negative if outside."""
    worst = 1e9
    pts = []
    for i in range(int(w / step) + 1):
        pts += [(x0 + i * step, y0), (x0 + i * step, y0 + h)]
    for j in range(int(h / step) + 1):
        pts += [(x0, y0 + j * step), (x0 + w, y0 + j * step)]
    for p in pts:
        d = min(seg_dist(p, poly[i], poly[i - 1]) for i in range(len(poly)))
        worst = min(worst, d if inside(p, poly) else -d)
    return worst


def check():
    problems = []
    for ref, _, _, x0, y0, w, h in PARTS:
        c = rect_edge_clearance(x0, y0, w, h, OUTLINE)
        flag = "" if c >= 0.3 or ref in EDGE_OK else "  <-- too close / outside"
        print(f"{ref:4s} edge clearance {c:6.2f} mm{flag}")
        if flag:
            problems.append(ref)
        for hr, (hx, hy) in HOLES:
            nx, ny = min(max(hx, x0), x0 + w), min(max(hy, y0), y0 + h)
            if math.hypot(hx - nx, hy - ny) < HOLE_D / 2 + 0.3:
                problems.append(f"{ref}/{hr}")
    for i, a in enumerate(PARTS):
        for b in PARTS[i + 1:]:
            if (a[3] < b[3] + b[5] + 0.3 and b[3] < a[3] + a[5] + 0.3 and
                    a[4] < b[4] + b[6] + 0.3 and b[4] < a[4] + a[6] + 0.3):
                problems.append(f"{a[0]}/{b[0]}")
    xs = [p[0] for p in OUTLINE]; ys = [p[1] for p in OUTLINE]
    print(f"Board extents: {max(xs) - min(xs):.1f} x {max(ys) - min(ys):.1f} mm")
    print("Problems:", problems or "none")
    return problems


# ---- previews ----
COLORS = {"U": "#3d6b8f", "J": "#8a6d3b", "L": "#6b6b6b", "S": "#5b7f4a", "B": "#7a4a7f", "M": "#7a4a7f"}


def build_svg():
    s = 12  # px per mm
    pad = 8
    xs = [p[0] for p in OUTLINE]; ys = [p[1] for p in OUTLINE]
    minx, miny = min(xs) - 2, min(ys) - 2
    W = (max(xs) - minx + 2) * s + 2 * pad
    H = (max(ys) - miny + 2) * s + 2 * pad + 30

    def T(x, y):
        return (x - minx) * s + pad, (y - miny) * s + pad

    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" '
         f'viewBox="0 0 {W:.0f} {H:.0f}" font-family="Helvetica, Arial, sans-serif">',
         f'<rect width="100%" height="100%" fill="#f4f1ea"/>']
    o.append('<polygon points="' + " ".join("%.1f,%.1f" % T(*p) for p in OUTLINE) +
             '" fill="#2f5d3a" stroke="#1d3a24" stroke-width="2"/>')
    for ref, label, _, x0, y0, w, h in PARTS:
        X, Y = T(x0, y0)
        col = COLORS.get(ref[0], "#555")
        o.append(f'<rect x="{X:.1f}" y="{Y:.1f}" width="{w * s:.1f}" height="{h * s:.1f}" '
                 f'fill="{col}" stroke="#f4f1ea" stroke-width="1"/>')
        cx, cy = X + w * s / 2, Y + h * s / 2
        fs = max(7, min(13, min(w, h) * s * 0.28))
        if min(w, h) < 3.6:
            o.append(f'<text x="{cx:.1f}" y="{cy + fs * 0.35:.1f}" fill="#fff" font-size="{fs:.0f}" '
                     f'text-anchor="middle">{ref}</text>')
        else:
            o.append(f'<text x="{cx:.1f}" y="{cy - fs * 0.2:.1f}" fill="#fff" font-size="{fs:.0f}" '
                     f'font-weight="bold" text-anchor="middle">{ref}</text>')
            o.append(f'<text x="{cx:.1f}" y="{cy + fs:.1f}" fill="#fff" font-size="{fs * 0.8:.0f}" '
                     f'text-anchor="middle">{label}</text>')
    o.append('<polygon points="' + " ".join("%.1f,%.1f" % T(*p) for p in ANT_KEEPOUT) +
             '" fill="#e8c547" fill-opacity="0.35" stroke="#e8c547" stroke-width="1.5" stroke-dasharray="5,3"/>')
    kx, ky = T(16, 4.6)
    o.append(f'<text x="{kx:.0f}" y="{ky:.0f}" fill="#fff" font-size="11" text-anchor="middle">'
             'antenna keepout (approx.)</text>')
    for ref, (hx, hy) in HOLES:
        X, Y = T(hx, hy)
        o.append(f'<circle cx="{X:.1f}" cy="{Y:.1f}" r="{HOLE_D / 2 * s:.1f}" fill="#f4f1ea" '
                 f'stroke="#1d3a24"/>')
    lx, ly = pad, H - 12
    o.append(f'<text x="{lx}" y="{ly}" fill="#333" font-size="11">TrekBuddy main board v0.1, '
             f'top view, rough placement. Scale 12 px = 1 mm. Yellow: ESP32 antenna keepout (approx.)</text>')
    o.append('</svg>')
    (HERE / "trekbuddy_layout.svg").write_text("\n".join(o))


def build_overlay():
    from PIL import Image, ImageDraw
    src = HERE / "enclosure_sketch.jpg"
    if not src.exists():
        return
    im = Image.open(src).convert("RGB")
    d = ImageDraw.Draw(im)

    def P(x, y):
        return (PX_ORIGIN[0] + x / MM_PER_PX, PX_ORIGIN[1] + y / MM_PER_PX)

    d.line([P(*p) for p in OUTLINE + [OUTLINE[0]]], fill=(255, 60, 60), width=2)
    for ref, label, _, x0, y0, w, h in PARTS:
        d.rectangle([P(x0, y0), P(x0 + w, y0 + h)], outline=(255, 210, 0), width=2)
        d.text(P(x0 + 0.3, y0 + 0.2), ref, fill=(255, 255, 255))
    for _, (hx, hy) in HOLES:
        r = HOLE_D / 2 / MM_PER_PX
        cx, cy = P(hx, hy)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(0, 220, 255), width=2)
    im.save(HERE / "outline_check.png")


if __name__ == "__main__":
    check()
    build_pcb()
    build_svg()
    build_overlay()
    print("wrote trekbuddy_main.kicad_pcb, trekbuddy_layout.svg, outline_check.png")
