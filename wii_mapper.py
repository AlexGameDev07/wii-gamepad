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
        self.chip = True  # lleva etiqueta con lo que hace (no los botones sin uso en el modo mouse)

    def title(self):
        return self.label if self.kind == "stick" else wg.source_label(self.src)

    def text(self, value):
        """Lo que se lee en su etiqueta."""
        return chip_text(value, self.kind)

    def describe(self, value):
        """Lo que hace, completo, para el tooltip."""
        return self.options().get(value, value)

    def face(self, value):
        """El botón A/B/X/Y del mando Xbox con su color, si es eso."""
        return value if self.kind == "button" and value in FACE else None

    def picker(self, current, pick):
        return TargetGrid(self, current, pick)

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
    draw_nunchuk_body(cr, paint)
    draw_wiimote_body(cr, paint)
    draw_spots(cr, paint, spots)


def draw_wiimote_alone(cr, paint, spots):
    """El Wiimote sin Nunchuk: el mismo dibujo corrido a la izquierda (sus botones ya vienen corridos)."""
    cr.save()
    cr.translate(-WIIMOTE_ALONE_DX, 0)
    draw_wiimote_body(cr, paint)
    cr.restore()
    draw_spots(cr, paint, spots)


def draw_nunchuk_body(cr, paint):
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


def draw_wiimote_body(cr, paint):
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
WIIMOTE_ALONE_DX = 180  # el Wiimote solo empieza donde termina el Nunchuk


# --- modo mouse --------------------------------------------------------------------
# Las acciones del modo mouse, por tema, para elegirlas en el menú de cada botón.
ACTION_SECTIONS = (
    ("Mouse", ("left", "right", "middle", "wheel_up", "wheel_down")),
    ("Teclas", ("enter", "esc", "backspace", "space", "tab", "up", "down", "left_key", "right_key", "super",
                "keyboard")),
    ("Ventanas", ("alt_tab", "alt_tab_prev", "close", "fullscreen", "screenshot")),
    ("Navegador", ("nav_back", "nav_fwd", "reload", "tab_next", "tab_prev", "tab_new", "tab_close", "tab_reopen",
                   "browser_close")),
    ("Edición", ("copy", "paste", "cut", "undo", "select_all")),
    ("Multimedia", ("vol_up", "vol_down", "mute", "play")),
)
POINTER = "pointer"  # lo que muestra la cruceta cuando mueve el puntero
DPAD = ("Up", "Down", "Left", "Right")


def action_short(action):
    """Nombre corto de una acción, sin las teclas entre paréntesis: «Cambiar ventana»."""
    if action == "none":
        return "Nada"
    return wg.MOUSE_ACTIONS.get(action, ((), action))[1].split(" (")[0]


def dpad_moves(m, pad):
    """Si en modo mouse la cruceta mueve el puntero en ese mando (como Player.dpad_moves)."""
    return m["move"] == "dpad" or (m["move"] == "stick" and pad == "wiimote")


class MouseSpot(Spot):
    """Un botón del dibujo en el modo mouse: lee y cambia la acción del botón del Wiimote/Nunchuk
    al que equivale (en el Classic, b hace de A, ZL de C...). El stick elige con qué se mueve el puntero."""

    def __init__(self, base, pad, m, dx=0, side=None):
        super().__init__(base.src, base.x + dx, base.y, base.shape, side or base.side, base.kind, base.key,
                         base.label, base.back)
        self.pad, self.m = pad, m
        self.alias = wg.CLASSIC_MOUSE_ALIAS.get(base.src) if pad == "classic" else base.src
        # Sin uso en el modo mouse: x, y y el stick derecho del Classic.
        self.chip = self.alias is not None if self.kind == "button" else base.src != "classic_rstick"

    def value(self, m):
        return m["move"] if self.kind == "stick" else m["buttons"].get(self.alias, "none")

    def moves_pointer(self):
        return self.kind == "button" and self.alias in DPAD and dpad_moves(self.m, self.pad)

    def get(self, m):
        return POINTER if self.moves_pointer() else self.value(m)

    def set(self, m, value):
        if self.kind == "stick":
            m["move"] = value
        else:
            m["buttons"][self.alias] = value

    def options(self):
        if self.kind == "stick":
            return wg.MOUSE_MOVES
        return {k: label for k, (_, label) in wg.MOUSE_ACTIONS.items()}

    def text(self, value):
        if self.kind == "stick":
            return "Mueve el puntero" if value == "stick" else "Nada"
        return "Mueve el puntero" if value == POINTER else action_short(value)

    def describe(self, value):
        if self.kind == "stick":
            return "Mueve el puntero" if value == "stick" else f"Nada: el puntero va con {wg.MOUSE_MOVES[value]}"
        if value == POINTER:
            return f"Mueve el puntero. Si no, {self.options().get(self.value(self.m))}"
        return self.options().get(value, value)

    def face(self, value):
        return None

    def picker(self, current, pick):
        return MousePicker(self, pick)


class MousePicker(Gtk.Box):
    """El menú de un botón en el modo mouse: las acciones por tema (o, para el stick, con qué se
    mueve el puntero); on_pick(id)."""

    def __init__(self, spot, on_pick):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=6, margin_bottom=6,
                         margin_start=6, margin_end=6)
        self.on_pick = on_pick
        current = spot.value(spot.m)
        if spot.kind == "stick":
            self.append(Gtk.Label(label="Con qué se mueve el puntero:", xalign=0, css_classes=["dim-label"]))
            for value, label in wg.MOUSE_MOVES.items():
                self.append(self.button(value, label, current, label))
            return
        if spot.moves_pointer():
            how = ("elegiste moverlo con la cruceta" if spot.m["move"] == "dpad" else
                   "sin Nunchuk ni Classic la cruceta lo mueve")
            self.append(Gtk.Label(label=f"Ahora la cruceta mueve el puntero ({how}). Lo que elijas se usa cuando "
                                  "no lo mueve.", xalign=0, wrap=True, max_width_chars=40,
                                  css_classes=["caption", "dim-label"]))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        listed = set()
        for title, actions in ACTION_SECTIONS:
            box.append(self.section(title, actions, current))
            listed.update(actions)
        rest = [a for a in wg.MOUSE_ACTIONS if a not in listed and a != "none"]
        if rest:
            box.append(self.section("Otras", rest, current))
        scroll = Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                    max_content_height=380)
        self.append(scroll)
        self.append(self.button("none", "Nada (desactivar)", current, "No hace nada"))

    def section(self, title, actions, current):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(Gtk.Label(label=title, xalign=0, css_classes=["caption-heading", "dim-label"]))
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=6,
                           row_spacing=6, max_children_per_line=2, min_children_per_line=2)
        for action in actions:
            child = Gtk.FlowBoxChild(focusable=False)
            child.set_child(self.button(action, action_short(action), current, wg.MOUSE_ACTIONS[action][1]))
            flow.append(child)
        box.append(flow)
        return box

    def button(self, value, label, current, tooltip):
        b = Gtk.Button(label=label, tooltip_text=tooltip, css_classes=["target-btn"], hexpand=True)
        b.get_child().set_ellipsize(Pango.EllipsizeMode.END)
        if value == current:
            b.add_css_class("current")
            b.add_css_class("suggested-action")
        b.connect("clicked", lambda _b: self.on_pick(value))
        return b


def mouse_layout(pad, m):
    """El dibujo del mando para el modo mouse: el Wiimote solo, con Nunchuk o el Classic."""
    if pad == "classic":
        return Layout("mouse-classic", "Classic", CLASSIC_LAYOUT.size,
                      [MouseSpot(s, pad, m) for s in CLASSIC_LAYOUT.spots], draw_classic)
    if pad == "nunchuk":
        return Layout("mouse-nunchuk", "Wiimote + Nunchuk", WIIMOTE_LAYOUT.size,
                      [MouseSpot(s, pad, m) for s in WIIMOTE_LAYOUT.spots], draw_wiimote)
    # Sin Nunchuk las etiquetas se reparten a los dos lados del Wiimote.
    left = {"Left", "-", "1", "B"}
    spots = [MouseSpot(s, pad, m, -WIIMOTE_ALONE_DX, "l" if s.src in left else "r")
             for s in WIIMOTE_LAYOUT.spots if s.src in wg.WIIMOTE_SOURCES]
    return Layout("mouse-wiimote", "Wiimote solo", (104, WIIMOTE_LAYOUT.size[1]), spots, draw_wiimote_alone)


class MouseEditor(Gtk.Box):
    """Los botones del modo mouse sobre el dibujo del mando elegido (o en lista si la ventana es
    estrecha). Cambiar algo lo guarda con save()."""

    def __init__(self, m, pad, save):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["card"])
        self.m, self.save = m, save
        self.narrow = False
        self.stack = Gtk.Stack(hhomogeneous=False, vhomogeneous=False,
                               transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.append(self.stack)
        self.hint = Gtk.Label(css_classes=["caption", "dim-label"], wrap=True, justify=Gtk.Justification.CENTER,
                              margin_bottom=10, margin_start=12, margin_end=12)
        self.append(self.hint)
        self.set_pad(pad)

    def set_pad(self, pad):
        self.pad = pad
        self.layout = mouse_layout(pad, self.m)
        for name in ("map", "list"):
            if old := self.stack.get_child_by_name(name):
                self.stack.remove(old)
        self.map = ControllerMap(self.layout, self.m, self.changed)
        self.map.set_margin_top(14)
        self.map.set_margin_start(12)
        self.map.set_margin_end(12)
        self.stack.add_named(self.map, "map")
        self.rows = {}
        listbox = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE, margin_top=12,
                              margin_bottom=12, margin_start=12, margin_end=12)
        for spot in self.layout.spots:
            if not spot.chip:
                continue
            options = spot.options()
            keys = list(options)
            row = Adw.ComboRow(title=spot.title(), model=Gtk.StringList.new([str(v) for v in options.values()]))
            row.set_selected(keys.index(spot.value(self.m)) if spot.value(self.m) in keys else 0)
            row.connect("notify::selected", lambda r, _p, spot=spot, keys=keys: self.list_changed(spot, keys, r))
            self.rows[spot.src] = (row, keys)
            listbox.append(row)
        self.stack.add_named(listbox, "list")
        self.sync()
        self.set_narrow(self.narrow)

    def set_narrow(self, narrow):
        self.narrow = narrow
        self.stack.set_visible_child_name("list" if narrow else "map")
        self.hint.set_visible(not narrow)
        extra = {"wiimote": " El B del Wiimote está por detrás (punteado).",
                 "nunchuk": " El B del Wiimote está por detrás (punteado).",
                 "classic": " x, y y el stick derecho no se usan en el modo mouse."}[self.pad]
        self.hint.set_label("Haz clic en un botón del dibujo o en su etiqueta para cambiar lo que hace." + extra)

    def list_changed(self, spot, keys, row):
        value = keys[row.get_selected()]
        if value != spot.value(self.m):
            spot.set(self.m, value)
            self.sync()
            self.save()

    def changed(self, _spot):
        self.sync()
        self.save()

    def sync(self):
        """Etiquetas y lista al día: cambiar con qué se mueve el puntero cambia también la cruceta."""
        self.map.refresh()
        for spot in self.layout.spots:
            if spot.src not in self.rows:
                continue
            row, keys = self.rows[spot.src]
            value = spot.value(self.m)
            if value in keys and row.get_selected() != keys.index(value):
                row.set_selected(keys.index(value))
            row.set_subtitle("Ahora mueve el puntero" if spot.moves_pointer() else "")


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
                if spot.chip:
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
        src_label = Gtk.Label(label=spot.title(), css_classes=["chip-src"])
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
        chip.target_label.set_label(spot.text(value))
        for c in ("unset", *("xb", "xb-A", "xb-B", "xb-X", "xb-Y")):
            chip.target_label.remove_css_class(c)
        if value == "none":
            chip.target_label.add_css_class("unset")
        elif face := spot.face(value):
            chip.target_label.add_css_class("xb")
            chip.target_label.add_css_class("xb-" + face)
        chip.set_tooltip_text(f"{spot.title()}: {spot.describe(value)}")

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
        box.append(Gtk.Label(label=f"Qué hace «{spot.title()}»",
                             xalign=0, css_classes=["heading"], margin_start=6, margin_top=4))
        box.append(spot.picker(spot.get(self.profile), pick))
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

    def __init__(self, profile, save, layout_key="wiimote", pad_factory=None, start_bridge=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.profile, self.save = profile, save
        self.layout = LAYOUTS[layout_key]
        self.narrow = False
        self.on_layout = None  # se llama con el mando elegido
        self.pad_factory, self.start_bridge = pad_factory, start_bridge

        title = Gtk.Label(label="Botones", xalign=0, css_classes=["heading"], hexpand=True)
        self.switcher = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        first = None
        for key, layout in LAYOUTS.items():
            b = Gtk.ToggleButton(label=layout.title, active=key == layout_key, group=first)
            first = first or b
            b.connect("toggled", lambda b, key=key: b.get_active() and self.set_layout(key))
            self.switcher.append(b)
        setup = Gtk.Button(child=Adw.ButtonContent(icon_name="input-gaming-symbolic", label="Configurar con tu mando"),
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
            self.hint.set_label("Haz clic en un botón del dibujo o en su etiqueta para cambiar lo que hace, o usa "
                                "«Configurar con tu mando» y aprieta los botones en tu mando." + extra)

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
            title = spot.title()
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
        def changed(src):
            if src and not src.startswith("stick:"):  # se ve en el dibujo del mando que se usó
                self.set_layout("classic" if src in wg.CLASSIC_BUTTONS else "wiimote")
            elif src:
                self.set_layout("classic" if src.startswith("stick:classic") else "wiimote")
            self.sync()
            self.save()
        dialog = LearnDialog(self.profile, self.pad_factory(), changed, self.start_bridge)
        dialog.present(self.get_root())
        return dialog

    def sync(self):
        """Refleja en el dibujo y en la lista cambios hechos desde fuera (el asistente)."""
        self.map.refresh()
        for spot in self.layout.spots:
            row, keys = self.rows[spot.src]
            value = spot.get(self.profile)
            if value in keys and row.get_selected() != keys.index(value):
                row.set_selected(keys.index(value))


def iter_children(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


# Pasos del asistente: (destino Xbox, nombre, qué se ilumina en el dibujo del mando Xbox)
LEARN_STEPS = [
    ("A", "A", {"A": 1}), ("B", "B", {"B": 1}), ("X", "X", {"X": 1}), ("Y", "Y", {"Y": 1}),
    ("LB", "LB (bumper izquierdo)", {"LB": 1}), ("RB", "RB (bumper derecho)", {"RB": 1}),
    ("LT", "LT (gatillo izquierdo)", {"LT": 1}), ("RT", "RT (gatillo derecho)", {"RT": 1}),
    ("DUp", "Cruceta ↑", {"DUp": 1}), ("DDown", "Cruceta ↓", {"DDown": 1}),
    ("DLeft", "Cruceta ←", {"DLeft": 1}), ("DRight", "Cruceta →", {"DRight": 1}),
    ("Back", "Back / Select", {"Back": 1}), ("Start", "Start", {"Start": 1}), ("Guide", "Guide (Xbox)", {"Guide": 1}),
    ("L3", "L3 (clic del stick izquierdo)", {"L3": 1}), ("R3", "R3 (clic del stick derecho)", {"R3": 1}),
    ("left", "Stick izquierdo", {"L3": 0.6, "lx": 0.7, "ly": -0.4}),
    ("right", "Stick derecho", {"R3": 0.6, "rx": 0.7, "ry": -0.4}),
]
STICK_STEPS = {"left", "right"}
# Botones y sticks de cada mando físico: asignar uno quita ese destino a los demás del mismo mando.
FAMILIES = (
    (set(wg.SOURCES), {"nunchuk_stick"}),
    (set(wg.CLASSIC_BUTTONS), {"classic_lstick", "classic_rstick"}),
)


def physical_name(src):
    """«b» del Classic, «C» del Nunchuk, «2» del Wiimote, «el stick del Nunchuk»..."""
    if src.startswith("stick:"):
        return {"nunchuk_stick": "el stick del Nunchuk", "classic_lstick": "el stick izquierdo del Classic",
                "classic_rstick": "el stick derecho del Classic"}.get(src[6:], src[6:])
    where = ("del Classic" if src in wg.CLASSIC_BUTTONS else
             "del Nunchuk" if src in wg.NUNCHUK_SOURCES else "del Wiimote")
    return f"«{wg.source_label(src)}» {where}"


class LearnDialog(Adw.Dialog):
    """Asistente estilo Steam: ilumina un botón del mando Xbox y espera a que aprietes en tu
    mando (Wiimote, de lado o no, Nunchuk o Classic) el que quieres para él. Lo detecta el
    puente, que mientras tanto no manda nada al juego."""

    POLL = 40     # ms entre lecturas de lo que anota el puente
    SETTLE = 450  # ms que se ve la confirmación antes de pasar al siguiente
    PULSE = 550   # ms del parpadeo del botón iluminado

    def __init__(self, profile, pad, on_change, start_bridge=None):
        super().__init__(title="Configurar con tu mando", content_width=520)
        self.profile, self.pad, self.on_change, self.start_bridge = profile, pad, on_change, start_bridge
        self.snapshot = {k: (dict(v) if isinstance(v, dict) else v) for k, v in profile.items()}
        self.index = 0
        self.offset = 0
        self.learning = False
        self.busy = False  # mostrando la confirmación de un paso
        self.done = {}     # paso -> lo que se le asignó
        self.pulse_on = True

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vhomogeneous=False)
        self.stack.add_named(self.build_steps(), "steps")
        self.stack.add_named(self.build_waiting(), "waiting")
        self.stack.add_named(self.build_summary(), "summary")
        header = Adw.HeaderBar()
        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        self.set_child(view)

        self.connect("closed", lambda _d: self.stop())
        self.timers = [GLib.timeout_add(self.POLL, self.poll), GLib.timeout_add(self.PULSE, self.blink)]
        self.check_bridge()

    # --- páginas ---------------------------------------------------------------
    def build_steps(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=18, margin_end=18,
                      margin_bottom=18, margin_top=6)
        self.progress = Gtk.ProgressBar()
        card = Gtk.Box(css_classes=["card"])
        self.pad.set_margin_top(12)
        self.pad.set_margin_bottom(12)
        self.pad.set_margin_start(12)
        self.pad.set_margin_end(12)
        card.append(self.pad)
        self.step = Gtk.Label(css_classes=["caption", "dim-label"], xalign=0)
        self.name = Gtk.Label(css_classes=["setup-title"], xalign=0, wrap=True)
        self.hint = Gtk.Label(css_classes=["dim-label"], xalign=0, wrap=True)
        self.result = Gtk.Label(xalign=0, wrap=True, css_classes=["heading"])
        back = Gtk.Button(label="Anterior")
        back.connect("clicked", lambda _b: self.go(self.index - 1))
        skip = Gtk.Button(label="Saltar")
        skip.connect("clicked", lambda _b: self.go(self.index + 1))
        self.back = back
        actions = Gtk.Box(spacing=8, homogeneous=True, margin_top=6)
        actions.append(back)
        actions.append(skip)
        for w in (self.progress, card, self.step, self.name, self.hint, self.result, actions):
            box.append(w)
        return box

    def build_waiting(self):
        self.wait_page = Adw.StatusPage(icon_name="input-gaming-symbolic")
        self.wait_button = Gtk.Button(label="Iniciar el puente", css_classes=["pill", "suggested-action"],
                                      halign=Gtk.Align.CENTER)
        self.wait_button.connect("clicked", lambda _b: self.start_bridge and self.start_bridge())
        self.wait_page.set_child(self.wait_button)
        return self.wait_page

    def build_summary(self):
        page = Adw.StatusPage(icon_name="emblem-ok-symbolic", title="¡Listo!")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.summary = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        box.append(self.summary)
        buttons = Gtk.Box(spacing=8, homogeneous=True, halign=Gtk.Align.CENTER)
        undo = Gtk.Button(label="Deshacer todo", css_classes=["pill"])
        undo.connect("clicked", lambda _b: self.undo())
        ok = Gtk.Button(label="Terminar", css_classes=["pill", "suggested-action"])
        ok.connect("clicked", lambda _b: self.close())
        buttons.append(undo)
        buttons.append(ok)
        box.append(buttons)
        page.set_child(box)
        return page

    # --- puente -----------------------------------------------------------------
    def check_bridge(self):
        """Hace falta el puente con al menos un mando: es quien lee los botones."""
        if self.stack.get_visible_child_name() == "summary":
            return  # ya terminó: no volver a detectar
        state = wg.read_state()
        if state and state.get("players"):
            if not self.learning:
                wg.start_learning()
                self.learning, self.offset = True, 0
                if self.stack.get_visible_child_name() != "summary":
                    self.stack.set_visible_child_name("steps")
                    self.go(self.index)
            return
        if self.learning:
            wg.stop_learning()
            self.learning = False
        running = state is not None
        self.wait_page.set_title("Enciende tu Wiimote" if running else "El puente está detenido")
        self.wait_page.set_description("Presiona 1+2. Con el Nunchuk o el Classic enchufados si los vas a usar."
                                       if running else "Es quien lee tu mando: inícialo para seguir.")
        self.wait_button.set_visible(not running and self.start_bridge is not None)
        if self.stack.get_visible_child_name() != "summary":
            self.stack.set_visible_child_name("waiting")

    def poll(self):
        self.ticks = getattr(self, "ticks", 0) + 1
        if self.ticks % 25 == 0 or not self.learning:  # ~1 s
            self.check_bridge()
        if not self.learning:
            return GLib.SOURCE_CONTINUE
        events, self.offset = wg.read_learned(self.offset)
        for ev in events:
            if not self.busy and self.stack.get_visible_child_name() == "steps":
                self.pressed(ev.get("src", ""))
        return GLib.SOURCE_CONTINUE

    def stop(self):
        for t in self.timers:
            GLib.source_remove(t)
        self.timers = []
        if self.learning:
            wg.stop_learning()
            self.learning = False

    # --- pasos ------------------------------------------------------------------
    def go(self, index):
        if index < 0:
            return
        if index >= len(LEARN_STEPS):
            self.finish()
            return
        self.index, self.busy = index, False
        target, label, lit = LEARN_STEPS[index]
        n = len(LEARN_STEPS)
        self.progress.set_fraction(index / n)
        self.step.set_label(f"Paso {index + 1} de {n}")
        if target in STICK_STEPS:
            self.name.set_label(f"Mueve el stick que quieres usar como {label.lower()}")
            self.hint.set_label("El del Nunchuk o uno de los del Classic. Si no quieres ninguno, salta este paso.")
        else:
            self.name.set_label(f"Aprieta el botón que quieres para {label}")
            self.hint.set_label("En tu Wiimote (como lo tengas, de lado o no), el Nunchuk o el Classic.")
        self.result.set_label(f"Ahora: {self.current(target)}")
        self.result.remove_css_class("success")
        self.back.set_sensitive(index > 0)
        self.pulse_on = True
        self.pad.light(lit)

    def current(self, target):
        """Qué botones físicos hacen hoy ese destino."""
        if target in STICK_STEPS:
            keys = [k for _, sticks in FAMILIES for k in sorted(sticks) if self.profile.get(k) == target]
            names = [physical_name("stick:" + k) for k in keys]
        else:
            names = [physical_name(s) for s, t in self.profile["buttons"].items() if t == target]
        return ", ".join(names) if names else "nada"

    def pressed(self, src):
        target, label, _ = LEARN_STEPS[self.index]
        stick = src.startswith("stick:")
        if (target in STICK_STEPS) != stick:
            self.result.set_label("Eso es un stick: aprieta un botón." if stick else
                                  "Eso es un botón: mueve un stick (o salta este paso).")
            return
        for buttons, sticks in FAMILIES:  # ese destino deja de estar en los demás controles del mismo mando
            if stick and src[6:] in sticks:
                for k in sticks:
                    if self.profile.get(k) == target:
                        self.profile[k] = "none"
                self.profile[src[6:]] = target
            elif not stick and src in buttons:
                for k in buttons:
                    if self.profile["buttons"].get(k) == target:
                        self.profile["buttons"][k] = "none"
                self.profile["buttons"][src] = target
        self.done[self.index] = (label, physical_name(src))
        self.on_change(src)
        self.busy = True
        self.result.set_label(f"✓ {physical_name(src)} → {label}")
        self.result.add_css_class("success")
        GLib.timeout_add(self.SETTLE, lambda: self.go(self.index + 1) and False)

    def blink(self):
        if self.stack.get_visible_child_name() == "steps" and not self.busy:
            self.pulse_on = not self.pulse_on
            lit = LEARN_STEPS[self.index][2]
            self.pad.light(lit if self.pulse_on else {k: v * 0.35 for k, v in lit.items()})
        return GLib.SOURCE_CONTINUE

    def finish(self):
        if self.learning:
            wg.stop_learning()
            self.learning = False
        while row := self.summary.get_row_at_index(0):
            self.summary.remove(row)
        for i in sorted(self.done):
            label, name = self.done[i]
            row = Adw.ActionRow(title=label, subtitle=name)
            self.summary.append(row)
        if not self.done:
            self.summary.append(Adw.ActionRow(title="No cambiaste nada"))
        self.stack.set_visible_child_name("summary")

    def undo(self):
        self.profile.clear()
        self.profile.update({k: (dict(v) if isinstance(v, dict) else v) for k, v in self.snapshot.items()})
        self.done.clear()
        self.on_change(None)
        self.close()
