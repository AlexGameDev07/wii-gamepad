#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Editor visual de botones para la ventana GTK4: el mando dibujado de fondo, una etiqueta
por botón con lo que hace (como en Steam) y un asistente que va botón por botón,
iluminándolo en el dibujo, para elegir qué hace cada uno."""
import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk, Pango, PangoCairo  # noqa: E402

import wii_gamepad as wg  # noqa: E402

CSS = """
.map-chip > button { padding: 3px 9px; min-height: 26px; border-radius: 9px; }
.map-chip.lit > button { background-color: alpha(@accent_bg_color, 0.25); box-shadow: inset 0 0 0 1px @accent_color; }
.chip-src { font-size: 0.82em; opacity: 0.62; }
.chip-target { font-weight: 700; }
.chip-target.unset { font-weight: 400; opacity: 0.5; }
.xb { border-radius: 999px; min-width: 22px; min-height: 22px; padding: 0; font-weight: 800; color: #fff; }
.xb-A { background-color: #2ec27e; } .xb-B { background-color: #e01b24; }
.xb-X { background-color: #3584e4; } .xb-Y { background-color: #f6d32d; color: #1e1e1e; }
.target-btn { min-width: 52px; min-height: 36px; padding: 2px 6px; font-weight: 700; }
.target-btn.xbox-face { border-radius: 999px; min-width: 40px; color: #fff; }
.target-btn.xbox-face.A { background-color: #2ec27e; } .target-btn.xbox-face.B { background-color: #e01b24; }
.target-btn.xbox-face.X { background-color: #3584e4; }
.target-btn.xbox-face.Y { background-color: #f6d32d; color: #1e1e1e; }
.target-btn.current { box-shadow: 0 0 0 3px @accent_color; }
.target-btn.xbox-face:not(.current) { opacity: 0.8; }
.setup-title { font-size: 20px; font-weight: 800; }
"""

# Etiquetas cortas de los destinos, para las etiquetas del dibujo y la cuadrícula.
SHORT = {"none": "Nada", "LT": "LT", "RT": "RT", "Back": "Back", "Start": "Start", "Guide": "Guide",
         "L3": "L3", "R3": "R3", "DUp": "↑", "DDown": "↓", "DLeft": "←", "DRight": "→",
         "LSUp": "↑", "LSDown": "↓", "LSLeft": "←", "LSRight": "→",
         "RSUp": "↑", "RSDown": "↓", "RSLeft": "←", "RSRight": "→"}
TARGET_SECTIONS = (
    ("Botones", ("A", "B", "X", "Y", "LB", "RB", "LT", "RT", "Back", "Start", "Guide", "L3", "R3")),
    ("Cruceta", ("DUp", "DDown", "DLeft", "DRight")),
    ("Stick izquierdo", ("LSUp", "LSDown", "LSLeft", "LSRight")),
    ("Stick derecho", ("RSUp", "RSDown", "RSLeft", "RSRight")),
)
FACE = {"A", "B", "X", "Y"}


def chip_text(target, kind):
    if kind == "stick":
        return wg.STICK_TARGETS.get(target, "— Nada —")
    if target.startswith("D") and target in SHORT:
        return "Cruceta " + SHORT[target]
    if target.startswith("LS"):
        return "Stick izq. " + SHORT[target]
    if target.startswith("RS"):
        return "Stick der. " + SHORT[target]
    return SHORT.get(target, wg.TARGETS.get(target, target))


class Spot:
    """Un botón (o stick) del dibujo: dónde está y cómo se ilumina."""

    def __init__(self, src, x, y, shape, side, kind="button", key=None, label=None, back=False):
        self.src, self.x, self.y, self.shape, self.side = src, x, y, shape, side
        self.kind = kind  # "button": profile["buttons"][src]; "stick": profile[key]
        self.key = key
        self.label = label or wg.source_label(src)
        self.back = back  # está por detrás del mando (el B del Wiimote): se dibuja punteado

    def get(self, profile):
        if self.kind == "stick":
            return profile.get(self.key, "none")
        return profile["buttons"].get(self.src, "none")

    def set(self, profile, value):
        if self.kind == "stick":
            profile[self.key] = value
        else:
            profile["buttons"][self.src] = value

    def options(self):
        return wg.STICK_TARGETS if self.kind == "stick" else wg.TARGETS


def circle(r):
    return ("circle", r)


def rect(w, h, r=6):
    return ("rect", w, h, r)


class Layout:
    """Un mando: su tamaño base, sus botones y cómo se dibuja."""

    def __init__(self, key, title, size, spots, draw):
        self.key, self.title, self.size, self.spots, self.draw = key, title, size, spots, draw


# --- dibujo ----------------------------------------------------------------------
def rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def spot_path(cr, spot, grow=0.0):
    if spot.shape[0] == "circle":
        cr.new_sub_path()
        cr.arc(spot.x, spot.y, spot.shape[1] + grow, 0, 2 * math.pi)
    else:
        _, w, h, r = spot.shape
        rounded(cr, spot.x - w / 2 - grow, spot.y - h / 2 - grow, w + 2 * grow, h + 2 * grow, r + grow)


class Paint:
    """Colores del tema actual para el dibujo con cairo."""

    def __init__(self, widget):
        self.fg = widget.get_color()
        self.accent = accent_rgba()

    def set(self, cr, color, alpha=1.0):
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha * alpha)

    def body(self, cr):
        self.set(cr, self.fg, 0.07)
        cr.fill_preserve()
        self.set(cr, self.fg, 0.38)
        cr.set_line_width(1.6)
        cr.stroke()

    def part(self, cr, alpha=0.16):
        self.set(cr, self.fg, alpha)
        cr.fill_preserve()
        self.set(cr, self.fg, 0.3)
        cr.set_line_width(1)
        cr.stroke()


def accent_rgba():
    try:  # libadwaita >= 1.6
        return Adw.StyleManager.get_default().get_accent_color_rgba()
    except AttributeError:
        c = Gdk.RGBA()
        c.parse("#3584e4")
        return c


def text(cr, paint, label, x, y, size=9, alpha=0.85, color=None):
    layout = PangoCairo.create_layout(cr)
    layout.set_font_description(Pango.FontDescription.from_string(f"Sans Bold {size}"))
    layout.set_text(label, -1)
    w, h = layout.get_pixel_size()
    paint.set(cr, color or paint.fg, alpha)
    cr.move_to(x - w / 2, y - h / 2)
    PangoCairo.show_layout(cr, layout)


def draw_spots(cr, paint, spots, skip=()):
    """Los botones con su nombre; los sticks como un círculo con su palanca."""
    for s in spots:
        if s.src in skip:
            continue
        if s.kind == "stick":
            cr.new_path()  # que el arco no salga unido al último texto
            cr.arc(s.x, s.y, s.shape[1], 0, 2 * math.pi)
            paint.part(cr, 0.12)
            cr.new_path()
            cr.arc(s.x, s.y, s.shape[1] * 0.6, 0, 2 * math.pi)
            paint.part(cr, 0.28)
            continue
        spot_path(cr, s)
        if s.back:
            cr.set_dash([3, 3])
            paint.set(cr, paint.fg, 0.05)
            cr.fill_preserve()
            paint.set(cr, paint.fg, 0.45)
            cr.set_line_width(1.2)
            cr.stroke()
            cr.set_dash([])
        else:
            paint.part(cr)
        size = 8 if (s.shape[0] == "circle" and s.shape[1] < 12) or len(s.label) > 2 else 10
        text(cr, paint, s.label if len(s.label) <= 4 else s.label[-1], s.x, s.y, size)


def draw_wiimote(cr, paint, spots):
    # Cable del Nunchuk al Wiimote
    paint.set(cr, paint.fg, 0.3)
    cr.set_line_width(2)
    cr.move_to(85, 318)
    cr.curve_to(85, 410, 232, 400, 232, 430)
    cr.stroke()
    # Nunchuk
    cr.move_to(23, 140)
    cr.arc(85, 140, 62, math.pi, 2 * math.pi)
    cr.curve_to(147, 195, 122, 240, 120, 300)
    cr.curve_to(119, 324, 51, 324, 50, 300)
    cr.curve_to(48, 240, 23, 195, 23, 140)
    cr.close_path()
    paint.body(cr)
    # Wiimote
    rounded(cr, 190, 8, 84, 420, 26)
    paint.body(cr)
    cr.arc(232, 34, 7, 0, 2 * math.pi)  # encendido
    paint.part(cr, 0.1)
    rounded(cr, 222, 82, 20, 20, 3)  # centro de la cruceta
    paint.part(cr, 0.16)
    for row in range(3):  # bocina
        for col in range(5):
            cr.arc(212 + col * 10, 302 + row * 8, 1.4, 0, 2 * math.pi)
            paint.set(cr, paint.fg, 0.35)
            cr.fill()
    for i in range(4):  # LEDs
        rounded(cr, 207 + i * 14, 412, 8, 5, 2)
        paint.set(cr, paint.fg, 0.25)
        cr.fill()
    draw_spots(cr, paint, spots)


def draw_classic(cr, paint, spots):
    # Hombros: ZL/ZR detrás, L/R delante
    shoulders = [s for s in spots if s.src in ("cZL", "cZR", "cL", "cR")]
    draw_spots(cr, paint, shoulders)
    cr.move_to(110, 38)
    cr.line_to(330, 38)
    cr.curve_to(395, 38, 425, 62, 425, 112)
    cr.curve_to(425, 172, 420, 238, 382, 250)
    cr.curve_to(348, 260, 322, 232, 302, 192)
    cr.line_to(138, 192)
    cr.curve_to(118, 232, 92, 260, 58, 250)
    cr.curve_to(20, 238, 15, 172, 15, 112)
    cr.curve_to(15, 62, 45, 38, 110, 38)
    cr.close_path()
    paint.body(cr)
    rounded(cr, 79, 83, 18, 18, 3)  # centro de la cruceta
    paint.part(cr, 0.16)
    for i in range(4):  # LEDs
        rounded(cr, 199 + i * 12, 52, 6, 4, 2)
        paint.set(cr, paint.fg, 0.25)
        cr.fill()
    draw_spots(cr, paint, spots, skip={s.src for s in shoulders})


WIIMOTE_LAYOUT = Layout("wiimote", "Wiimote y Nunchuk", (300, 440), [
    Spot("C", 85, 28, rect(34, 18, 8), "l"),
    Spot("Z", 85, 54, rect(58, 24, 9), "l"),
    Spot("nunchuk_stick", 85, 140, circle(32), "l", "stick", "nunchuk_stick", "Stick"),
    Spot("Up", 232, 70, rect(20, 22, 4), "r", label="↑"),
    Spot("Left", 210, 92, rect(22, 20, 4), "r", label="←"),
    Spot("Right", 254, 92, rect(22, 20, 4), "r", label="→"),
    Spot("Down", 232, 114, rect(20, 22, 4), "r", label="↓"),
    Spot("A", 232, 160, circle(18), "r"),
    Spot("B", 232, 206, rect(48, 28, 12), "r", label="B", back=True),
    Spot("-", 204, 258, circle(9), "r"),
    Spot("Home", 232, 258, circle(11), "r", label="⌂"),
    Spot("+", 260, 258, circle(9), "r"),
    Spot("1", 232, 345, circle(15), "r"),
    Spot("2", 232, 385, circle(15), "r"),
], draw_wiimote)

CLASSIC_LAYOUT = Layout("classic", "Classic / Classic Pro", (440, 268), [
    Spot("cZL", 88, 12, rect(76, 16, 7), "l"),
    Spot("cL", 88, 31, rect(88, 15, 6), "l"),
    Spot("cUp", 88, 73, rect(18, 20, 4), "l", label="↑"),
    Spot("cLeft", 69, 92, rect(20, 18, 4), "l", label="←"),
    Spot("cRight", 107, 92, rect(20, 18, 4), "l", label="→"),
    Spot("cDown", 88, 111, rect(18, 20, 4), "l", label="↓"),
    Spot("classic_lstick", 165, 150, circle(28), "l", "stick", "classic_lstick", "Stick izq."),
    Spot("c-", 190, 84, circle(8), "l"),
    Spot("cZR", 352, 12, rect(76, 16, 7), "r"),
    Spot("cR", 352, 31, rect(88, 15, 6), "r"),
    Spot("cX", 352, 66, circle(13), "r"),
    Spot("cY", 326, 92, circle(13), "r"),
    Spot("cA", 378, 92, circle(13), "r"),
    Spot("cB", 352, 118, circle(13), "r"),
    Spot("classic_rstick", 275, 150, circle(28), "r", "stick", "classic_rstick", "Stick der."),
    Spot("cHome", 220, 104, circle(10), "r", label="⌂"),
    Spot("c+", 250, 84, circle(8), "r"),
], draw_classic)

LAYOUTS = {l.key: l for l in (WIIMOTE_LAYOUT, CLASSIC_LAYOUT)}


# --- widgets ---------------------------------------------------------------------
class TargetGrid(Gtk.Box):
    """Los destinos del mando Xbox como botones (A/B/X/Y con sus colores); on_pick(destino)."""

    def __init__(self, spot, current, on_pick):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=6, margin_bottom=6,
                         margin_start=6, margin_end=6)
        self.on_pick = on_pick
        if spot.kind == "stick":
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            for value, label in wg.STICK_TARGETS.items():
                box.append(self.button(value, label.strip("— "), current, wide=True))
            self.append(box)
            return
        for title, targets in TARGET_SECTIONS:
            self.append(Gtk.Label(label=title, xalign=0, css_classes=["caption-heading", "dim-label"]))
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=6,
                               row_spacing=6, max_children_per_line=7, min_children_per_line=4)
            for t in targets:
                child = Gtk.FlowBoxChild(focusable=False)
                child.set_child(self.button(t, SHORT.get(t, t), current))
                flow.append(child)
            self.append(flow)
        self.append(self.button("none", "Nada (desactivar)", current, wide=True))

    def button(self, value, label, current, wide=False):
        b = Gtk.Button(label=label, css_classes=["target-btn"], tooltip_text=wg.TARGETS.get(value, label))
        if value in FACE:
            b.add_css_class("xbox-face")
            b.add_css_class(value)
        if value == current:
            b.add_css_class("current")
            if value not in FACE:
                b.add_css_class("suggested-action")
        if wide:
            b.set_hexpand(True)
        b.connect("clicked", lambda _b: self.on_pick(value))
        return b


class ControllerMap(Gtk.Widget):
    """El mando dibujado. Con etiquetas (chips=True), cada botón tiene la suya a un lado con lo
    que hace, unida por una línea; al hacer clic en la etiqueta o en el botón del dibujo se
    elige otro destino. Sin etiquetas solo dibuja e ilumina (para el asistente)."""

    GAP = 30  # espacio entre las etiquetas y el dibujo, para las líneas
    MIN_SCALE = 0.62
    CHIP_SPACING = 6

    def __init__(self, layout, profile=None, on_change=None, chips=True, max_scale=1.25):
        super().__init__(hexpand=True)
        self.layout, self.profile, self.on_change = layout, profile, on_change
        self.max_scale = max_scale
        self.lit = None    # botón iluminado (el del asistente)
        self.hover = None  # etiqueta bajo el puntero o con su menú abierto
        self.chips = {}
        self.pulse = 0.0
        self._tick = None
        self._geom = None
        if chips:
            for spot in layout.spots:
                self.chips[spot.src] = self.make_chip(spot)
            click = Gtk.GestureClick()
            click.connect("released", self.on_click)
            self.add_controller(click)
        self.connect("unrealize", lambda _w: self.stop_pulse())

    def do_dispose(self):
        for chip in self.chips.values():
            chip.unparent()
        self.chips = {}
        Gtk.Widget.do_dispose(self)

    # --- etiquetas ---------------------------------------------------------------
    def make_chip(self, spot):
        src_label = Gtk.Label(label=spot.label if spot.kind == "stick" else wg.source_label(spot.src),
                              css_classes=["chip-src"])
        target = Gtk.Label(css_classes=["chip-target"])
        box = Gtk.Box(spacing=6)
        box.append(src_label)
        box.append(target)
        chip = Gtk.MenuButton(child=box, css_classes=["map-chip"], always_show_arrow=False)
        chip.target_label = target
        chip.spot = spot
        chip.set_create_popup_func(self.fill_popover)
        chip.connect("notify::active", lambda c, _p: self.chip_hover(c, c.get_active()))
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self.chip_hover(chip, True))
        motion.connect("leave", lambda *_: self.chip_hover(chip, False))
        chip.add_controller(motion)
        chip.set_parent(self)
        self.update_chip(chip)
        return chip

    def update_chip(self, chip):
        spot = chip.spot
        value = spot.get(self.profile)
        chip.target_label.set_label(chip_text(value, spot.kind))
        for c in ("unset", *("xb", "xb-A", "xb-B", "xb-X", "xb-Y")):
            chip.target_label.remove_css_class(c)
        if value == "none":
            chip.target_label.add_css_class("unset")
        elif spot.kind == "button" and value in FACE:
            chip.target_label.add_css_class("xb")
            chip.target_label.add_css_class("xb-" + value)
        chip.set_tooltip_text(f"{spot.label if spot.kind == 'stick' else wg.source_label(spot.src)}: "
                              f"{spot.options().get(value, value)}")

    def refresh(self):
        for chip in self.chips.values():
            self.update_chip(chip)
        self.queue_resize()

    def fill_popover(self, chip):
        spot = chip.spot
        pop = chip.get_popover() or Gtk.Popover()

        def pick(value):
            spot.set(self.profile, value)
            self.update_chip(chip)
            self.queue_resize()
            pop.popdown()
            if self.on_change:
                self.on_change(spot)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, width_request=300)
        box.append(Gtk.Label(label=f"Qué hace «{spot.label if spot.kind == 'stick' else wg.source_label(spot.src)}»",
                             xalign=0, css_classes=["heading"], margin_start=6, margin_top=4))
        box.append(TargetGrid(spot, spot.get(self.profile), pick))
        pop.set_child(box)
        chip.set_popover(pop)

    def chip_hover(self, chip, on):
        """El puntero entra o sale de una etiqueta, o se abre o cierra su menú."""
        src = chip.spot.src
        if on:
            self.hover = src
        elif self.hover == src and not chip.get_active():
            self.hover = None
        self.mark_chips()
        self.queue_draw()

    def mark_chips(self):
        for s, chip in self.chips.items():
            (chip.add_css_class if s in (self.hover, self.lit) else chip.remove_css_class)("lit")

    def set_lit(self, src):
        self.lit = src
        self.mark_chips()
        if src:
            self.start_pulse()
        else:
            self.stop_pulse()
        self.queue_draw()

    def on_click(self, gesture, _n, x, y):
        g = self.geometry(self.get_width())
        bx, by = (x - g["ox"]) / g["scale"], (y - g["oy"]) / g["scale"]
        best = None
        for spot in self.layout.spots:
            reach = (spot.shape[1] if spot.shape[0] == "circle" else max(spot.shape[1], spot.shape[2]) / 2) + 8
            d = math.hypot(bx - spot.x, by - spot.y)
            if d <= reach and (best is None or d < best[0]):
                best = (d, spot)
        if best and best[1].src in self.chips:
            self.chips[best[1].src].popup()

    # --- animación del botón iluminado -------------------------------------------
    def start_pulse(self):
        if self._tick is None and Gtk.Settings.get_default().props.gtk_enable_animations:
            self._tick = self.add_tick_callback(self.on_tick)

    def stop_pulse(self):
        if self._tick is not None:
            self.remove_tick_callback(self._tick)
            self._tick = None
        self.pulse = 0.0

    def on_tick(self, _w, clock):
        self.pulse = (math.sin(clock.get_frame_time() / 1e6 * 4.2) + 1) / 2
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    # --- tamaño y colocación -----------------------------------------------------
    def chip_sizes(self):
        sizes = {}
        for src, chip in self.chips.items():
            _, nat_w, _, _ = chip.measure(Gtk.Orientation.HORIZONTAL, -1)
            _, nat_h, _, _ = chip.measure(Gtk.Orientation.VERTICAL, nat_w)
            sizes[src] = (nat_w, nat_h)
        return sizes

    def geometry(self, width):
        """Escala del dibujo, su origen, dónde va cada etiqueta y el alto total para este ancho."""
        bw, bh = self.layout.size
        sizes = self.chip_sizes()
        side_w = {s: max((sizes[sp.src][0] for sp in self.layout.spots if sp.side == s and sp.src in sizes),
                         default=0) for s in "lr"}
        gap = self.GAP if self.chips else 0
        avail = width - side_w["l"] - side_w["r"] - 2 * gap
        scale = max(self.MIN_SCALE if self.chips else 0.1, min(self.max_scale, avail / bw))
        art_w, art_h = bw * scale, bh * scale
        total = side_w["l"] + gap + art_w + gap + side_w["r"]
        left = max(0.0, (width - total) / 2)
        ox = left + side_w["l"] + gap
        pad, sp_ = 6.0, self.CHIP_SPACING
        columns = {s: sorted((sp for sp in self.layout.spots if sp.side == s and sp.src in sizes),
                             key=lambda sp: (sp.y, sp.x if s == "l" else -sp.x)) for s in "lr"}
        stacks = [sum(sizes[sp.src][1] + sp_ for sp in col) - sp_ for col in columns.values() if col]
        content_h = max([art_h, *stacks])
        oy = pad + (content_h - art_h) / 2  # el dibujo, centrado respecto a las columnas de etiquetas
        rects = {}
        for s, col in columns.items():
            # Cada etiqueta a la altura de su botón; si se pisan, se empujan hacia abajo y,
            # si se salen por abajo, hacia arriba.
            ys, y_prev = [], pad
            for sp in col:
                h = sizes[sp.src][1]
                y = max(oy + sp.y * scale - h / 2, y_prev)
                ys.append(y)
                y_prev = y + h + sp_
            limit = pad + content_h
            for i in reversed(range(len(col))):
                h = sizes[col[i].src][1]
                ys[i] = min(ys[i], limit - h)
                limit = ys[i] - sp_
            for sp, y in zip(col, ys):
                w, h = sizes[sp.src]
                x = left + side_w["l"] - w if s == "l" else ox + art_w + gap
                rects[sp.src] = (x, y, w, h)
        return {"scale": scale, "ox": ox, "oy": oy, "rects": rects, "height": math.ceil(content_h + 2 * pad),
                "min_width": math.ceil(side_w["l"] + side_w["r"] + 2 * gap + bw * self.MIN_SCALE)}

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation, for_size):
        bw, bh = self.layout.size
        if orientation == Gtk.Orientation.HORIZONTAL:
            if not self.chips:
                return 160, round(bw * self.max_scale), -1, -1
            g = self.geometry(10 ** 4)
            return g["min_width"], g["min_width"] + round(bw * (self.max_scale - self.MIN_SCALE)), -1, -1
        width = for_size if for_size > 0 else round(bw * self.max_scale)
        h = self.geometry(width)["height"]
        return h, h, -1, -1

    def do_size_allocate(self, width, height, _baseline):
        self._geom = g = self.geometry(width)
        g["width"] = width
        for src, (x, y, w, h) in g["rects"].items():
            t = Gsk.Transform.new().translate(Graphene.Point().init(round(x), round(y)))
            self.chips[src].allocate(round(w), round(h), -1, t)

    # --- dibujo ------------------------------------------------------------------
    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        g = self._geom if self._geom and self._geom.get("width") == w else self.geometry(w)
        paint = Paint(self)
        cr = snapshot.append_cairo(Graphene.Rect().init(0, 0, w, h))
        s, ox, oy = g["scale"], g["ox"], g["oy"]
        cr.save()
        cr.translate(ox, oy)
        cr.scale(s, s)
        self.layout.draw(cr, paint, self.layout.spots)
        cr.restore()
        # Líneas de las etiquetas: del borde del botón a su etiqueta
        for spot in self.layout.spots:
            if spot.src not in g["rects"]:
                continue
            x, y, cw, ch = g["rects"][spot.src]
            on = spot.src in (self.hover, self.lit)
            ex = x + cw if spot.side == "l" else x
            ey = y + ch / 2
            elbow = ex + (10 if spot.side == "l" else -10)
            px, py = edge_point(spot, (elbow - ox) / s, (ey - oy) / s)
            px, py = ox + px * s, oy + py * s
            paint.set(cr, paint.accent if on else paint.fg, 0.9 if on else 0.28)
            cr.set_line_width(1.6 if on else 1)
            cr.move_to(ex, ey)
            cr.line_to(elbow, ey)
            cr.line_to(px, py)
            cr.stroke()
            cr.arc(px, py, 2.4, 0, 2 * math.pi)
            cr.fill()
        cr.save()
        cr.translate(ox, oy)
        cr.scale(s, s)
        for spot in self.layout.spots:  # iluminación
            on = spot.src in (self.hover, self.lit)
            if on:
                strength = 0.55 + 0.45 * self.pulse if spot.src == self.lit and self._tick else 1.0
                spot_path(cr, spot, 4 + 3 * strength)
                paint.set(cr, paint.accent, 0.22 * strength)
                cr.fill()
                spot_path(cr, spot)
                paint.set(cr, paint.accent, 0.55 + 0.35 * strength)
                cr.fill_preserve()
                paint.set(cr, paint.accent, 1)
                cr.set_line_width(2 / s)
                cr.stroke()
                label = spot.label if len(spot.label) <= 4 else spot.label[-1]
                if spot.kind != "stick":
                    text(cr, paint, label, spot.x, spot.y, 10, 1, rgba_white())
        cr.restore()
        for chip in self.chips.values():
            self.snapshot_child(chip, snapshot)


def edge_point(spot, tx, ty):
    """Punto del borde del botón en dirección a (tx, ty), en coordenadas del dibujo."""
    dx, dy = tx - spot.x, ty - spot.y
    d = math.hypot(dx, dy) or 1.0
    if spot.shape[0] == "circle":
        k = spot.shape[1] / d
    else:
        _, w, h, _r = spot.shape
        k = min((w / 2) / abs(dx) if dx else math.inf, (h / 2) / abs(dy) if dy else math.inf)
    k = min(k, 1.0)
    return spot.x + dx * k, spot.y + dy * k


def rgba_white():
    c = Gdk.RGBA()
    c.parse("#ffffff")
    return c


class ButtonEditor(Gtk.Box):
    """La sección «Botones» del perfil: elegir mando, el dibujo con etiquetas (o una lista si
    la ventana es estrecha) y el asistente paso a paso."""

    def __init__(self, profile, save, layout_key="wiimote"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.profile, self.save = profile, save
        self.layout = LAYOUTS[layout_key]
        self.narrow = False
        self.on_layout = None  # se llama con el mando elegido

        title = Gtk.Label(label="Botones", xalign=0, css_classes=["heading"], hexpand=True)
        self.switcher = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        first = None
        for key, layout in LAYOUTS.items():
            b = Gtk.ToggleButton(label=layout.title, active=key == layout_key, group=first)
            first = first or b
            b.connect("toggled", lambda b, key=key: b.get_active() and self.set_layout(key))
            self.switcher.append(b)
        setup = Gtk.Button(child=Adw.ButtonContent(icon_name="input-gaming-symbolic", label="Configurar paso a paso"),
                           css_classes=["suggested-action"], valign=Gtk.Align.CENTER)
        setup.connect("clicked", lambda _b: self.run_setup())
        top = Gtk.Box(spacing=12)
        top.append(title)
        self.append(top)
        tools = Gtk.Box(spacing=12)
        self.switcher.set_hexpand(True)
        self.switcher.set_halign(Gtk.Align.START)
        tools.append(self.switcher)
        tools.append(setup)
        self.tools = tools
        self.append(tools)

        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["card"], spacing=6)
        self.stack = Gtk.Stack(hhomogeneous=False, vhomogeneous=False, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.card.append(self.stack)
        self.hint = Gtk.Label(css_classes=["caption", "dim-label"], wrap=True, justify=Gtk.Justification.CENTER,
                              margin_bottom=10, margin_start=12, margin_end=12)
        self.card.append(self.hint)
        self.append(self.card)
        self.build()

    def set_layout(self, key):
        if self.layout.key != key:
            self.layout = LAYOUTS[key]
            self.build()
            if self.on_layout:
                self.on_layout(key)

    def set_narrow(self, narrow):
        """Con poco ancho, lista en vez de dibujo y los botones de arriba uno debajo del otro."""
        self.narrow = narrow
        self.tools.set_orientation(Gtk.Orientation.VERTICAL if narrow else Gtk.Orientation.HORIZONTAL)
        self.switcher.set_halign(Gtk.Align.FILL if narrow else Gtk.Align.START)
        for b in iter_children(self.switcher):
            b.set_hexpand(narrow)
        self.stack.set_visible_child_name("list" if narrow else "map")
        self.update_hint()

    def update_hint(self):
        if self.narrow:
            self.hint.set_visible(False)
        else:
            self.hint.set_visible(True)
            extra = (" El B del Wiimote está por detrás (punteado)." if self.layout.key == "wiimote" else
                     " Funciona con el Classic Controller y el Classic Controller Pro enchufados al Wiimote.")
            self.hint.set_label("Haz clic en un botón del dibujo o en su etiqueta para cambiar lo que hace." + extra)

    def build(self):
        for name in ("map", "list"):
            if old := self.stack.get_child_by_name(name):
                self.stack.remove(old)
        self.map = ControllerMap(self.layout, self.profile, self.changed)
        self.map.set_margin_top(14)
        self.map.set_margin_start(12)
        self.map.set_margin_end(12)
        self.stack.add_named(self.map, "map")
        self.rows = {}
        listbox = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE, margin_top=12,
                              margin_bottom=12, margin_start=12, margin_end=12)
        for spot in self.layout.spots:
            options = spot.options()
            keys = list(options)
            title = spot.label if spot.kind == "stick" else wg.source_label(spot.src)
            if spot.src == "B":
                title = "B (gatillo)"
            row = Adw.ComboRow(title=title, model=Gtk.StringList.new([str(v) for v in options.values()]))
            current = spot.get(self.profile)
            row.set_selected(keys.index(current) if current in keys else 0)
            row.connect("notify::selected", lambda r, _p, spot=spot, keys=keys: self.list_changed(spot, keys, r))
            self.rows[spot.src] = (row, keys)
            listbox.append(row)
        self.stack.add_named(listbox, "list")
        self.stack.set_visible_child_name("list" if self.narrow else "map")
        self.update_hint()

    def list_changed(self, spot, keys, row):
        value = keys[row.get_selected()]
        if value != spot.get(self.profile):
            spot.set(self.profile, value)
            self.map.refresh()
            self.save()

    def changed(self, spot):
        """Un cambio desde el dibujo o el asistente: guardarlo y reflejarlo en la lista."""
        row, keys = self.rows[spot.src]
        value = spot.get(self.profile)
        if value in keys and row.get_selected() != keys.index(value):
            row.set_selected(keys.index(value))
        self.save()

    def run_setup(self):
        def changed(spot):
            self.map.refresh()
            self.changed(spot)
        SetupDialog(self.layout, self.profile, changed).present(self.get_root())


def iter_children(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


class SetupDialog(Adw.Dialog):
    """Asistente: ilumina cada botón del mando, uno tras otro, y pregunta qué debe hacer."""

    def __init__(self, layout, profile, on_change):
        super().__init__(title=f"Configurar: {layout.title}", content_width=560)
        self.layout, self.profile, self.on_change = layout, profile, on_change
        self.spots = layout.spots
        self.index = 0

        self.art = ControllerMap(layout, profile, chips=False, max_scale=0.7 if layout.key == "wiimote" else 1.0)
        self.art.set_halign(Gtk.Align.CENTER)
        art_box = Gtk.Box(css_classes=["card"], halign=Gtk.Align.FILL)
        art_box.append(self.art)
        self.art.set_margin_top(10)
        self.art.set_margin_bottom(10)
        self.art_box = art_box

        self.step = Gtk.Label(css_classes=["caption", "dim-label"], xalign=0)
        self.name = Gtk.Label(css_classes=["setup-title"], xalign=0, wrap=True)
        self.now = Gtk.Label(css_classes=["dim-label"], xalign=0, wrap=True)
        self.grid_holder = Adw.Bin()
        self.progress = Gtk.ProgressBar(css_classes=["osd"])

        back = Gtk.Button(label="Anterior")
        back.connect("clicked", lambda _b: self.go(self.index - 1))
        skip = Gtk.Button(label="Dejar como está")
        skip.connect("clicked", lambda _b: self.advance())
        self.back = back
        actions = Gtk.Box(spacing=8, homogeneous=True)
        actions.append(back)
        actions.append(skip)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=18, margin_end=18,
                          margin_bottom=18, margin_top=6)
        for wdg in (self.progress, art_box, self.step, self.name, self.now, self.grid_holder, actions):
            content.append(wdg)
        header = Adw.HeaderBar()
        view = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=content, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                                         propagate_natural_height=True))
        view.add_top_bar(header)
        self.set_child(view)
        self.go(0)

    def go(self, index):
        if index < 0:
            return
        if index >= len(self.spots):
            self.close()
            return
        self.index = index
        spot = self.spots[index]
        self.art.set_lit(spot.src)
        n = len(self.spots)
        self.progress.set_fraction(index / n)
        self.step.set_label(f"Paso {index + 1} de {n}")
        label = spot.label if spot.kind == "stick" else wg.source_label(spot.src)
        where = "del Nunchuk" if spot.src in wg.NUNCHUK_SOURCES or spot.src == "nunchuk_stick" else (
            "del Classic" if self.layout.key == "classic" else "del Wiimote")
        self.name.set_label(f"¿Qué hace «{label}» {where}?")
        value = spot.get(self.profile)
        self.now.set_label(f"Ahora: {spot.options().get(value, value)}")
        self.back.set_sensitive(index > 0)
        self.grid_holder.set_child(TargetGrid(spot, value, self.pick))

    def pick(self, value):
        spot = self.spots[self.index]
        spot.set(self.profile, value)
        self.on_change(spot)
        self.advance()

    def advance(self):
        self.go(self.index + 1)
