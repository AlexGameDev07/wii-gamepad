#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Ventana para configurar wii-gamepad con GTK4 + libadwaita: los Wiimotes conectados
(cada uno con su perfil y su mando virtual en vivo), los perfiles por juego y las
preferencias. Se adapta al ancho de la ventana y sigue el tema del sistema."""
import copy
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

import evdev  # noqa: E402
from evdev import ecodes as e, ff  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wii_gamepad as wg  # noqa: E402
import wii_mapper as wm  # noqa: E402
import wii_news as wn  # noqa: E402

APP_ID = "io.github.alexgamedev07.WiiGamepad"
VERSION = wn.VERSION
SERVICE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / "wii-gamepad.service"
THEMES = {"system": "Como el sistema", "light": "Claro", "dark": "Oscuro"}
# Colores de los botones del mando Xbox (paleta de GNOME)
XBOX_COLORS = {"A": "#2ec27e", "B": "#e01b24", "X": "#3584e4", "Y": "#f6d32d"}
CSS = """
.player-badge {
  background-color: @accent_bg_color; color: @accent_fg_color;
  border-radius: 9999px; min-width: 30px; min-height: 30px; font-weight: 800;
  transition: background-color 250ms ease, color 250ms ease;
}
.player-badge.offline { background-color: alpha(currentColor, 0.12); color: alpha(currentColor, 0.55); }
.player-badge.big { min-width: 72px; min-height: 72px; font-size: 32px; }
.hero-title { font-size: 22px; font-weight: 800; }
.battery-low { color: @error_color; }
.bridge-dot { color: @success_color; transition: color 250ms ease; }
.bridge-dot.off { color: alpha(currentColor, 0.35); }
.bridge-dot.waiting { color: @warning_color; animation: pulse 1.4s ease-in-out infinite alternate; }
.pad-card { padding: 12px; }
.notice-dot { background-color: @accent_bg_color; border-radius: 999px; min-width: 8px; min-height: 8px; }
.news-icon { color: @accent_color; }
@keyframes pulse { from { opacity: 1; } to { opacity: 0.35; } }
"""


def rgba(spec, alpha=None):
    c = Gdk.RGBA()
    c.parse(spec)
    if alpha is not None:
        c.alpha = alpha
    return c


def mix(a, b, t):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = (x + (y - x) * t for x, y in
                                       ((a.red, b.red), (a.green, b.green), (a.blue, b.blue), (a.alpha, b.alpha)))
    return c


def fade(color, alpha):
    c = color.copy()
    c.alpha = color.alpha * alpha
    return c


def accent_color():
    try:  # libadwaita >= 1.6
        return Adw.StyleManager.get_default().get_accent_color_rgba()
    except AttributeError:
        return rgba("#3584e4")


def battery_icon(level):
    if level is None:
        return "battery-missing-symbolic"
    return f"battery-level-{min(100, round(level / 10) * 10)}-symbolic"


def combo_row(title, options, current, on_change, subtitle=None):
    """Adw.ComboRow sobre un dict id -> etiqueta; on_change recibe el id elegido."""
    keys = list(options)
    row = Adw.ComboRow(title=title, model=Gtk.StringList.new([str(v) for v in options.values()]))
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_selected(keys.index(current) if current in keys else 0)
    row.connect("notify::selected", lambda r, _: on_change(keys[r.get_selected()]))
    return row


def switch_row(title, active, on_change, subtitle=None):
    row = Adw.SwitchRow(title=title, active=active)
    if subtitle:
        row.set_subtitle(subtitle)
    row.connect("notify::active", lambda r, _: on_change(r.get_active()))
    return row


def scale_row(title, lo, hi, value, on_change, fmt="×{:.1f}", subtitle=None):
    row = Adw.ActionRow(title=title)
    if subtitle:
        row.set_subtitle(subtitle)
    scale = Gtk.Scale(adjustment=Gtk.Adjustment(lower=lo, upper=hi, value=value, step_increment=0.05),
                      hexpand=True, draw_value=True, value_pos=Gtk.PositionType.LEFT, width_request=200,
                      valign=Gtk.Align.CENTER)
    scale.set_format_value_func(lambda _s, v: fmt.format(v))
    scale.connect("value-changed", lambda s: on_change(round(s.get_value(), 3)))
    row.add_suffix(scale)
    return row


def button_row(title, label, callback, subtitle=None, style=None, icon=None):
    row = Adw.ActionRow(title=title)
    if subtitle:
        row.set_subtitle(subtitle)
    button = Gtk.Button(valign=Gtk.Align.CENTER)
    if icon:
        button.set_child(Adw.ButtonContent(icon_name=icon, label=label))
    else:
        button.set_label(label)
    if style:
        button.add_css_class(style)
    button.connect("clicked", lambda _b: callback())
    row.add_suffix(button)
    row.set_activatable_widget(button)
    return row


def group(title=None, description=None, *rows):
    g = Adw.PreferencesGroup()
    if title:
        g.set_title(title)
    if description:
        g.set_description(description)
    for r in rows:
        g.add(r)
    return g


def test_rumble(phys):
    """Pide una vibración al mando virtual, igual que haría un juego. Devuelve el error o None."""
    path = wg.find_phys(phys)
    if path is None:
        return "El mando virtual no está disponible"
    dev = evdev.InputDevice(path)
    try:
        fx = ff.Effect(e.FF_RUMBLE, -1, 0, ff.Trigger(0, 0), ff.Replay(600, 0),
                       ff.EffectType(ff_rumble_effect=ff.Rumble(strong_magnitude=0xFFFF, weak_magnitude=0xFFFF)))
        dev.write(e.EV_FF, dev.upload_effect(fx), 1)
    except OSError as err:
        dev.close()
        return f"No se pudo pedir la vibración ({err})"
    # el efecto pertenece a este descriptor: cerrarlo antes de tiempo lo cortaría
    GLib.timeout_add(800, lambda: dev.close() and False)
    return None


def test_speaker(player, cfg):
    path = (player or {}).get("hidraw")
    if not path:
        return "El Wiimote no está conectado"
    if not wg.wii_speaker.sonar(path, "prueba", cfg["speaker_volume"], wg.wii_speaker.sonido_de(cfg, "connect")):
        return "Sin permiso sobre la bocina: vuelve a ejecutar install.sh"
    return None


class Store:
    """La configuración en memoria. Guarda al momento o, para los deslizadores y textos,
    con un poco de retraso para no escribir en cada píxel."""

    def __init__(self):
        new_install = not wg.CONFIG.exists()
        self.cfg = wg.load_config()
        if new_install:  # a quien acaba de instalar no hay nada que contarle como novedad
            wn.mark_seen(self.cfg)
            wg.save_config(self.cfg)
        self._pending = None

    def save(self, delay=False):
        if self._pending:
            GLib.source_remove(self._pending)
            self._pending = None
        if delay:
            self._pending = GLib.timeout_add(300, self.flush)
        else:
            self.flush()

    def flush(self):
        self._pending = None
        wg.save_config(self.cfg)
        return False


class PadView(Gtk.Widget):
    """El mando Xbox virtual de un jugador, dibujado y animado según los eventos que recibe."""

    W, H = 360, 236
    CIRCLES = {"Y": (290, 76, 15, "Y"), "X": (260, 104, 15, "X"), "B": (320, 104, 15, "B"), "A": (290, 132, 15, "A"),
               "Back": (148, 94, 10, "◀"), "Guide": (180, 72, 14, "⊛"), "Start": (212, 94, 10, "▶")}
    RECTS = {"LB": (40, 36, 64, 18, "LB"), "RB": (256, 36, 64, 18, "RB"),
             "DUp": (99, 142, 24, 24, "↑"), "DDown": (99, 190, 24, 24, "↓"),
             "DLeft": (75, 166, 24, 24, "←"), "DRight": (123, 166, 24, 24, "→")}
    TRIGGERS = {"LT": (40, 10, 64, 20), "RT": (256, 10, 64, 20)}
    STICKS = {"l": (70, 100), "r": (232, 182)}
    STICK_KEYS = {e.ABS_X: "lx", e.ABS_Y: "ly", e.ABS_RX: "rx", e.ABS_RY: "ry"}

    def __init__(self):
        super().__init__(hexpand=True)
        self.target, self.value = {}, {}
        self.dev = self.watch = self.tick = None
        self.last_t = None
        self.demo = False  # muestra (light) en vez de un mando en vivo: no se atenúa
        self.keymap = {v: k for k, v in wg.KEY_TARGETS.items()}
        self.connect("unrealize", lambda _w: self.detach())

    # --- datos -----------------------------------------------------------------
    def attach(self, phys):
        if self.dev is not None and self.phys == phys:
            return
        self.detach()
        self.phys = phys
        path = wg.find_phys(phys) if phys else None
        if path:
            try:
                self.dev = evdev.InputDevice(path)
            except OSError:
                self.dev = None
        if self.dev is not None:
            for code in self.dev.active_keys():
                self.on_event(e.EV_KEY, code, 1)
            for code in (*self.STICK_KEYS, e.ABS_Z, e.ABS_RZ, e.ABS_HAT0X, e.ABS_HAT0Y):
                self.on_event(e.EV_ABS, code, self.dev.absinfo(code).value)
            self.watch = GLib.io_add_watch(self.dev.fd, GLib.PRIORITY_DEFAULT,
                                           GLib.IOCondition.IN | GLib.IOCondition.ERR | GLib.IOCondition.HUP,
                                           self.on_io)
        self.queue_draw()

    def detach(self):
        if self.watch:
            GLib.source_remove(self.watch)
            self.watch = None
        if self.dev is not None:
            self.dev.close()
            self.dev = None
        self.target = {k: 0.0 for k in self.target}
        self.animate()

    def on_io(self, _fd, cond):
        try:
            if cond & (GLib.IOCondition.ERR | GLib.IOCondition.HUP):
                raise OSError
            for ev in self.dev.read():
                self.on_event(ev.type, ev.code, ev.value)
        except BlockingIOError:
            pass
        except OSError:  # el puente se cerró
            self.watch = None
            self.detach()
            return False
        return True

    def on_event(self, etype, code, value):
        t = self.target
        if etype == e.EV_KEY and code in self.keymap:
            t[self.keymap[code]] = float(bool(value))
        elif etype == e.EV_ABS:
            if code == e.ABS_Z:
                t["LT"] = value / 255
            elif code == e.ABS_RZ:
                t["RT"] = value / 255
            elif code == e.ABS_HAT0X:
                t["DLeft"], t["DRight"] = float(value < 0), float(value > 0)
            elif code == e.ABS_HAT0Y:
                t["DUp"], t["DDown"] = float(value < 0), float(value > 0)
            elif code in self.STICK_KEYS:
                t[self.STICK_KEYS[code]] = value / 32767
        else:
            return
        self.animate()

    def light(self, values):
        """Modo muestra: ilumina estos controles ({clave: 0..1}, p. ej. {"A": 1, "lx": 0.6})."""
        self.demo = True
        self.target = {k: 0.0 for k in self.target} | dict(values)
        self.animate()

    # --- animación ---------------------------------------------------------------
    def animate(self):
        if self.tick is None and self.get_mapped():
            self.last_t = None
            self.tick = self.add_tick_callback(self.on_tick)
        elif not self.get_mapped():
            self.value = dict(self.target)

    def on_tick(self, _widget, clock):
        now = clock.get_frame_time() / 1e6
        dt = 0.0 if self.last_t is None else now - self.last_t
        self.last_t = now
        animate = Gtk.Settings.get_default().props.gtk_enable_animations
        k = 1.0 if not animate else 1 - math.exp(-dt / 0.045)
        moving = False
        for key, goal in self.target.items():
            cur = self.value.get(key, 0.0)
            new = cur + (goal - cur) * k
            if abs(new - goal) < 0.003:
                new = goal
            else:
                moving = True
            self.value[key] = new
        self.queue_draw()
        if moving:
            return GLib.SOURCE_CONTINUE
        self.tick = None
        return GLib.SOURCE_REMOVE

    # --- dibujo ------------------------------------------------------------------
    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 240, self.W, -1, -1
        width = for_size if for_size > 0 else self.W
        h = round(min(width, self.W * 1.6) * self.H / self.W)
        return h, h, -1, -1

    def text(self, snapshot, label, x, y, color, size=9):
        layout = self.create_pango_layout(label)
        font = Pango.FontDescription.from_string(f"Sans Bold {size}")
        layout.set_font_description(font)
        w, h = layout.get_pixel_size()
        snapshot.save()
        snapshot.translate(Graphene.Point().init(x - w / 2, y - h / 2))
        snapshot.append_layout(layout, color)
        snapshot.restore()

    def shape(self, snapshot, x, y, w, h, radius, color):
        rect = Graphene.Rect().init(x, y, w, h)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(rect, radius)
        snapshot.push_rounded_clip(rounded)
        snapshot.append_color(color, rect)
        snapshot.pop()

    def ring(self, snapshot, cx, cy, r, width, color):
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(Graphene.Rect().init(cx - r, cy - r, 2 * r, 2 * r), r)
        snapshot.append_border(rounded, [width] * 4, [color] * 4)

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        scale = min(w / self.W, h / self.H)
        fg = self.get_color()
        off, muted = fade(fg, 0.12), fade(fg, 0.45)
        accent = accent_color()
        v = self.value
        snapshot.save()
        snapshot.translate(Graphene.Point().init((w - self.W * scale) / 2, (h - self.H * scale) / 2))
        snapshot.scale(scale, scale)
        dim = self.dev is None and not self.demo
        if dim:
            snapshot.push_opacity(0.45)

        for key, (x, y, tw, th) in self.TRIGGERS.items():
            self.shape(snapshot, x, y, tw, th, 6, off)
            if v.get(key, 0) > 0.01:
                self.shape(snapshot, x, y, tw * v[key], th, 6, accent)
            self.text(snapshot, key, x + tw / 2, y + th / 2, fg, 8)
        for key, (x, y, rw, rh, label) in self.RECTS.items():
            t = v.get(key, 0.0)
            grow = 2 * t
            self.shape(snapshot, x - grow, y - grow, rw + 2 * grow, rh + 2 * grow, 6, mix(off, accent, t))
            self.text(snapshot, label, x + rw / 2, y + rh / 2, fg, 8)
        for key, (x, y, r, label) in self.CIRCLES.items():
            t = v.get(key, 0.0)
            color = rgba(XBOX_COLORS[key]) if key in XBOX_COLORS else accent
            if t > 0.01:  # halo
                halo = r + 7 * t
                self.shape(snapshot, x - halo, y - halo, 2 * halo, 2 * halo, halo, fade(color, 0.3 * t))
            rr = r * (1 + 0.12 * t)
            self.shape(snapshot, x - rr, y - rr, 2 * rr, 2 * rr, rr, mix(off, color, t))
            text = rgba("#1e1e1e") if key == "Y" and t > 0.5 else fg
            self.text(snapshot, label, x, y, text, 9 if r > 12 else 8)
        for side, (cx, cy) in self.STICKS.items():
            click = v.get("L3" if side == "l" else "R3", 0.0)
            self.ring(snapshot, cx, cy, 31, 2 + click, mix(muted, accent, click))
            dx, dy = v.get(side + "x", 0.0) * 22, v.get(side + "y", 0.0) * 22
            moved = min(1.0, math.hypot(dx, dy) / 22)
            self.shape(snapshot, cx + dx - 10, cy + dy - 10, 20, 20, 10, mix(muted, accent, moved))

        if dim:
            snapshot.pop()
        snapshot.restore()


class SidebarRow(Gtk.ListBoxRow):
    def __init__(self, section, key, title, subtitle="", badge=None, offline=False, icon=None, battery=None):
        super().__init__()
        self.section, self.key = section, key
        box = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=6, margin_end=6)
        if badge is not None:
            b = Gtk.Label(label=badge, css_classes=["player-badge"] + (["offline"] if offline else []),
                          valign=Gtk.Align.CENTER)
            box.append(b)
        elif icon:
            box.append(Gtk.Image(icon_name=icon))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        text.append(Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END))
        if subtitle:
            text.append(Gtk.Label(label=subtitle, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                  css_classes=["caption", "dim-label"]))
        box.append(text)
        if battery is not None:
            img = Gtk.Image(icon_name=battery_icon(battery), tooltip_text=f"Batería: {battery} %")
            if battery <= 20:
                img.add_css_class("battery-low")
            box.append(img)
        self.set_child(box)


class ProfilePage(Adw.NavigationPage):
    """Editor de un perfil: qué hace cada botón (sobre el dibujo del mando, o en lista si la
    ventana es estrecha) y cómo se usan los sticks, la inclinación y el giroscopio."""

    def __init__(self, win, name):
        super().__init__(title=name, tag=f"profile:{name}")
        self.win, self.name = win, name
        cfg = win.store.cfg
        p = cfg["profiles"][name]

        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("Duplicar…", "profile.duplicate")
        menu.append("Renombrar…", "profile.rename")
        menu.append("Restaurar valores", "profile.reset")
        section = Gio.Menu()
        section.append("Borrar…", "profile.delete")
        menu.append_section(None, section)
        header.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="Perfil"))
        actions = Gio.SimpleActionGroup()
        for action, cb in (("duplicate", win.duplicate_profile), ("rename", win.rename_profile),
                           ("reset", win.reset_profile), ("delete", win.delete_profile)):
            a = Gio.SimpleAction.new(action, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb(name))
            actions.add_action(a)
        a = actions.lookup_action("delete")
        a.set_enabled(len(cfg["profiles"]) > 1)
        self.insert_action_group("profile", actions)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_top=24, margin_bottom=24,
                          margin_start=12, margin_end=12)

        def narrow(widget):
            content.append(Adw.Clamp(maximum_size=640, child=widget))

        users = [pl["name"] for pl in win.players() if pl.get("profile") == name]
        default = switch_row("Perfil por defecto", cfg["active"] == name, self.set_default,
                             "Lo usan los Wiimotes que no tienen un perfil elegido")
        default.set_sensitive(cfg["active"] != name)
        narrow(group(None, "Lo está usando: " + ", ".join(users) if users else None, default))

        # Con un Classic enchufado se abre directamente su dibujo.
        layout = "classic" if any(pl.get("classic") for pl in win.players()) else win.mapper_layout
        self.buttons = wm.ButtonEditor(p, win.store.save, layout, pad_factory=PadView,
                                       start_bridge=lambda: None if win.state else win.toggle_bridge())
        self.buttons.on_layout = lambda key: setattr(win, "mapper_layout", key)
        content.append(Adw.Clamp(maximum_size=1000, child=self.buttons))

        narrow(group("Sticks", "Del Nunchuk y del Classic Controller",
                     scale_row("Zona muerta", 0, 0.4, p["deadzone"], self.setter("deadzone", delay=True), "{:.0%}",
                               "Cuánto hay que mover el stick para que cuente")))

        tilt = group("Inclinación del Wiimote")
        tilt.add(combo_row("Mover", wg.TILT_TARGETS, p["tilt"], self.setter("tilt")))
        tilt.add(scale_row("Sensibilidad", 0.3, 3.0, p["tilt_sensitivity"], self.setter("tilt_sensitivity", True)))
        tilt.add(switch_row("Invertir eje vertical", p["invert_tilt_y"], self.setter("invert_tilt_y")))
        narrow(tilt)

        gyro = group("Giroscopio (Motion Plus)",
                     "Se calibra solo al dejar el Wiimote quieto 1 s. En modo Apuntar, apunta al frente y "
                     "usa la combinación de recentrar.")
        gyro.add(combo_row("Mover", wg.GYRO_TARGETS, p["gyro"], self.setter("gyro")))
        gyro.add(combo_row("Modo", wg.GYRO_MODES, p["gyro_mode"], self.setter("gyro_mode")))
        gyro.add(scale_row("Sensibilidad", 0.3, 3.0, p["gyro_sensitivity"], self.setter("gyro_sensitivity", True)))
        gyro.add(switch_row("Invertir eje vertical", p["invert_gyro_y"], self.setter("invert_gyro_y")))
        narrow(gyro)

        bin_ = Adw.BreakpointBin(width_request=300, height_request=300,
                                 child=Gtk.ScrolledWindow(child=content, hscrollbar_policy=Gtk.PolicyType.NEVER))
        # Por debajo de este ancho el dibujo con sus etiquetas no cabe: lista de botones.
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 620sp"))
        bp.connect("apply", lambda _bp: self.buttons.set_narrow(True))
        bp.connect("unapply", lambda _bp: self.buttons.set_narrow(False))
        bin_.add_breakpoint(bp)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(bin_)
        self.set_child(view)

    def setter(self, key, delay=False):
        def changed(value):
            self.win.store.cfg["profiles"][self.name][key] = value
            self.win.store.save(delay)
        return changed

    def set_default(self, on):
        if on:
            self.win.store.cfg["active"] = self.name
            self.win.store.save()
            self.win.refresh(rebuild_page=True)

    def refresh(self, _state):
        pass


class PlayerPage(Adw.NavigationPage):
    """Un Wiimote: su perfil, su batería y su mando virtual en vivo."""

    def __init__(self, win, wiimote_id):
        super().__init__(title="Wiimote", tag=f"player:{wiimote_id}")
        self.win, self.id = win, wiimote_id
        cfg = win.store.cfg
        dev = cfg["devices"].get(wiimote_id, {})

        header = Adw.HeaderBar()
        view = Adw.ToolbarView()
        view.add_top_bar(header)

        # Cabecera: número de jugador, nombre y batería
        self.badge = Gtk.Label(css_classes=["player-badge", "big"], valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0, css_classes=["hero-title"], ellipsize=Pango.EllipsizeMode.END)
        self.subtitle = Gtk.Label(xalign=0, css_classes=["dim-label"], ellipsize=Pango.EllipsizeMode.END)
        self.battery = Gtk.LevelBar(min_value=0, max_value=100, valign=Gtk.Align.CENTER, width_request=120)
        self.battery_label = Gtk.Label(css_classes=["numeric"])
        self.battery_icon = Gtk.Image()
        batt = Gtk.Box(spacing=8, margin_top=6)
        batt.append(self.battery_icon)
        batt.append(self.battery)
        batt.append(self.battery_label)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True, spacing=2)
        names.append(self.title)
        names.append(self.subtitle)
        names.append(batt)
        hero = Gtk.Box(spacing=16, margin_bottom=6)
        hero.append(self.badge)
        hero.append(names)

        self.pad = PadView()
        self.pad_caption = Gtk.Label(css_classes=["caption", "dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["card", "pad-card"],
                       valign=Gtk.Align.START, hexpand=True)
        card.append(self.pad)
        card.append(self.pad_caption)

        # Ajustes del Wiimote
        name_row = Adw.EntryRow(title="Nombre", text=dev.get("name", ""), show_apply_button=True)
        name_row.connect("apply", self.set_name)
        profiles = {"": "Por defecto"} | {n: n for n in cfg["profiles"]}
        profile_row = combo_row("Perfil", profiles, dev.get("profile", ""), self.set_profile,
                                f"Por defecto: {cfg['active']}")
        edit_row = Adw.ActionRow(title="Editar el perfil", activatable=True)
        edit_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        edit_row.connect("activated", lambda _r: win.select(("profile", self.profile_name())))
        self.actions = group("Acciones", None,
                             button_row("Probar vibración", "Vibrar", self.rumble),
                             button_row("Probar bocina", "Sonar", self.speaker, "Experimental"),
                             button_row("Apagar el Wiimote", "Apagar", self.power_off, style="destructive-action",
                                        subtitle="Para volver a usarlo, presiona 1+2"))
        forget = group(None, None, button_row("Olvidar este Wiimote", "Olvidar", self.forget,
                                              "Borra su nombre y su perfil", "destructive-action"))
        self.forget_group = forget
        settings = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, hexpand=True)
        for g in (group("Wiimote", None, name_row), group("Perfil", None, profile_row, edit_row), self.actions, forget):
            settings.append(g)

        columns = Gtk.Box(spacing=24)
        columns.append(card)
        columns.append(settings)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=24, margin_bottom=24,
                          margin_start=18, margin_end=18)
        content.append(hero)
        content.append(columns)
        clamp = Adw.Clamp(maximum_size=1100, child=content)
        bin_ = Adw.BreakpointBin(width_request=300, height_request=300,
                                 child=Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER))
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        bp.add_setter(columns, "orientation", Gtk.Orientation.VERTICAL)
        bin_.add_breakpoint(bp)
        view.set_content(bin_)
        self.set_child(view)
        self.refresh(win.state)

    def player(self):
        return next((p for p in self.win.players() if p["id"] == self.id), None)

    def profile_name(self):
        cfg = self.win.store.cfg
        name = cfg["devices"].get(self.id, {}).get("profile")
        return name if name in cfg["profiles"] else cfg["active"]

    def refresh(self, _state):
        p = self.player()
        online = p is not None
        name = p["name"] if online else self.win.store.cfg["devices"].get(self.id, {}).get("name") or self.id
        self.set_title(name)
        self.title.set_label(name)
        self.badge.set_label(str(p["number"]) if online else "–")
        (self.badge.remove_css_class if online else self.badge.add_css_class)("offline")
        mac = self.id if ":" in self.id else ""
        player = f"Jugador {p['number']}" if online else "Desconectado"
        accessory = ("Classic Controller" if p.get("classic") else "Nunchuk" if p.get("nunchuk") else "") if online else ""
        parts = [x for x in (player if player != name else "", accessory, mac) if x]
        self.subtitle.set_label(" · ".join(parts) or f"Mando Xbox virtual {p['number']}")
        level = p.get("battery") if online else None
        self.battery.get_parent().set_visible(level is not None)
        self.battery_icon.set_from_icon_name(battery_icon(level))
        self.battery.set_value(level or 0)
        self.battery_label.set_label(f"{level} %" if level is not None else "")
        (self.battery_label.add_css_class if level is not None and level <= 20 else
         self.battery_label.remove_css_class)("battery-low")
        self.actions.set_sensitive(online)
        self.forget_group.set_visible(not online and self.id in self.win.store.cfg["devices"])
        self.pad.attach(p["pad"] if online else None)
        self.pad_caption.set_label("Aprieta botones del Wiimote para probarlos" if online else
                                   "Enciende este Wiimote (1+2) para verlo en vivo")

    def set_name(self, row):
        dev = self.win.store.cfg["devices"].setdefault(self.id, {})
        name = row.get_text().strip()
        dev["name"] = name
        if not name:
            del dev["name"]
        self.win.store.save()
        self.win.toast("Nombre guardado")
        self.win.refresh()

    def set_profile(self, name):
        dev = self.win.store.cfg["devices"].setdefault(self.id, {})
        if name:
            dev["profile"] = name
        else:
            dev.pop("profile", None)
        self.win.store.save()
        self.win.refresh()

    def rumble(self):
        p = self.player()
        err = test_rumble(p["pad"]) if p else "El Wiimote no está conectado"
        if err is None and not self.win.store.cfg["rumble"]:
            err = "La vibración está desactivada en Preferencias"
        self.win.toast(err or "Vibrando…")

    def speaker(self):
        self.win.toast(test_speaker(self.player(), self.win.store.cfg) or "Sonando…")

    def power_off(self):
        if not wg.disconnect(self.id):
            self.win.toast("No se pudo apagar (¿falta bluetoothctl?)")

    def forget(self):
        self.win.store.cfg["devices"].pop(self.id, None)
        self.win.store.save()
        self.win.select(None)
        self.win.refresh()


class MousePage(Adw.NavigationPage):
    """El modo mouse: cómo se entra, qué hace cada botón sobre el dibujo del mando y las
    combinaciones. Los nombres de los botones son los del mando elegido (el Classic tiene su
    propio b, que hace de A), que se elige solo según lo que está enchufado."""

    def __init__(self, win):
        super().__init__(title="Modo mouse", tag="mouse")
        self.win = win
        self.pad = None
        self.editor = None
        self.wide_editor = True
        header = Adw.HeaderBar()
        self.switcher = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        self.pad_buttons = {}
        first = None
        for key, label in wg.MOUSE_PADS.items():
            b = Gtk.ToggleButton(label=label, group=first)
            first = first or b
            b.connect("toggled", lambda b, key=key: b.get_active() and self.pick_pad(key))
            self.switcher.append(b)
            self.pad_buttons[key] = b
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_top=24, margin_bottom=24,
                               margin_start=12, margin_end=12)
        bin_ = Adw.BreakpointBin(width_request=300, height_request=300,
                                 child=Gtk.ScrolledWindow(child=self.content, hscrollbar_policy=Gtk.PolicyType.NEVER))
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 620sp"))
        bp.connect("apply", lambda _bp: self.set_narrow(True))
        bp.connect("unapply", lambda _bp: self.set_narrow(False))
        bin_.add_breakpoint(bp)
        self.status = Adw.Banner()
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.status)
        view.set_content(bin_)
        self.set_child(view)
        self.refresh(win.state)

    # --- qué mando ------------------------------------------------------------------
    def detected_pad(self):
        """El mando del que está en modo mouse, si no el del primero conectado."""
        players = self.win.players()
        player = next((p for p in players if p.get("mouse")), players[0] if players else None)
        return wg.mouse_pad_of(player) if player else "nunchuk"

    def pick_pad(self, pad):
        if pad == self.pad:
            return
        if self.pad is not None:  # lo eligió a mano: se respeta hasta que cambie lo enchufado
            self.win.mouse_pad = (pad, self.detected_pad())
        self.build(pad)

    def refresh(self, _state):
        players = self.win.players()
        owner = next((p for p in players if p.get("mouse")), None)
        combo = wg.mouse_toggle_label(self.win.store.cfg["mouse_combo"], self.pad or self.detected_pad())
        if owner:
            self.status.set_title(f"{owner['name']} está en modo mouse ahora · para salir: {combo}")
        self.status.set_revealed(owner is not None)
        detected = self.detected_pad()
        chosen = self.win.mouse_pad
        pad = chosen[0] if chosen and chosen[1] == detected else detected
        if chosen and chosen[1] != detected:
            self.win.mouse_pad = None
        if pad != self.pad:
            self.build(pad)

    # --- contenido -------------------------------------------------------------------
    def set_narrow(self, narrow):
        self.wide_editor = not narrow
        self.switcher.set_orientation(Gtk.Orientation.VERTICAL if narrow else Gtk.Orientation.HORIZONTAL)
        if self.editor:
            self.editor.set_narrow(narrow)

    def build(self, pad):
        self.pad = pad
        self.pad_buttons[pad].set_active(True)
        cfg = self.win.store.cfg
        m = cfg["mouse"]
        save = self.win.store.save
        if parent := self.switcher.get_parent():  # se vuelve a poner arriba
            parent.remove(self.switcher)
        while child := self.content.get_first_child():
            self.content.remove(child)
        name = lambda src: wg.mouse_button_label(src, pad)  # noqa: E731

        def narrow(widget):
            self.content.append(Adw.Clamp(maximum_size=640, child=widget))

        def set_m(key, delay=False, rebuild=False):
            def changed(value):
                m[key] = value
                save(delay)
                if rebuild:
                    self.editor.sync()
            return changed

        # Arriba: el mando y cómo se entra y sale
        title = Gtk.Label(label="Tu mando", xalign=0, css_classes=["heading"])
        sub = Gtk.Label(label="Los botones se nombran como en él. Se elige solo según lo que tengas enchufado.",
                        xalign=0, wrap=True, css_classes=["caption", "dim-label"])
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        top.append(title)
        top.append(sub)
        self.switcher.set_halign(Gtk.Align.START)
        top.append(self.switcher)
        narrow(top)

        toggles = {k: wg.mouse_toggle_label(k, pad) for k in wg.MOUSE_COMBOS}

        def set_combo(value):
            cfg["mouse_combo"] = value
            save()
            self.refresh(self.win.state)

        narrow(group(
            "Entrar y salir", "El mando pasa de mando de juego a mouse y viceversa (vibra y suena al cambiar).",
            combo_row("Combinación", toggles, cfg["mouse_combo"], set_combo)))

        # El dibujo con lo que hace cada botón
        heading = Gtk.Label(label="Botones", xalign=0, css_classes=["heading"])
        self.editor = wm.MouseEditor(m, pad, save)
        self.editor.set_narrow(not self.wide_editor)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(heading)
        box.append(self.editor)
        self.content.append(Adw.Clamp(maximum_size=1000, child=box))

        self.combos = self.combos_group(pad)
        narrow(self.combos)

        stick = "el stick izquierdo del Classic" if pad == "classic" else "el stick del Nunchuk"
        pointer = group("Puntero", f"«Stick» es {stick}. Sin él, o sin Motion Plus con «Giroscopio», la cruceta "
                        "mueve el puntero." if pad != "wiimote" else
                        "Sin Nunchuk ni Classic, con «Stick» la cruceta mueve el puntero. El giroscopio necesita "
                        "el Motion Plus.")
        pointer.add(combo_row("Mover el puntero con", wg.MOUSE_MOVES, m["move"], set_m("move", rebuild=True)))
        pointer.add(scale_row("Velocidad", 0.3, 3.0, m["speed"], set_m("speed", True)))
        if pad != "classic":
            pointer.add(switch_row("Invertir eje horizontal del giroscopio", m["invert_gyro_x"],
                                   set_m("invert_gyro_x")))
            pointer.add(switch_row("Invertir eje vertical del giroscopio", m["invert_gyro_y"],
                                   set_m("invert_gyro_y")))
        narrow(pointer)

        keys = [(name("A"), "la escribe"), (name("B"), "borra")]
        if pad != "wiimote":
            keys += [(name("C"), "lo cierra"), (name("Z"), "mayúsculas")]
        keys += [(name("1"), "espacio"), (name("2"), "Enter")]
        keyboard_owner = next((name(s) for s, a in m["buttons"].items() if a == "keyboard"
                               and wg.mouse_pad_has(s, pad)), None)
        how = f"También se abre con {keyboard_owner}. " if keyboard_owner else ""
        choose = "cruceta o stick" if pad != "wiimote" else "la cruceta"
        narrow(group(
            "Teclado en pantalla", how + f"Con el teclado visible: {choose} eligen la tecla, "
            + ", ".join(f"{b} {what}" for b, what in keys[:-1]) + f" y {keys[-1][0]} {keys[-1][1]}.",
            switch_row("Mostrarlo al entrar a un campo de texto", m["auto_keyboard"], set_m("auto_keyboard"))))

        def restore():
            cfg["mouse_combo"] = wg.GLOBAL_OPTIONS["mouse_combo"]
            m.clear()
            m.update(copy.deepcopy(wg.MOUSE_DEFAULTS))
            save()
            self.win.toast("Modo mouse restaurado")
            self.build(pad)

        narrow(group(None, "Quién puede usar el modo mouse con varios Wiimotes se elige en Preferencias.",
                     button_row("Restaurar el modo mouse", "Restaurar", restore, style="destructive-action")))

    # --- combinaciones ----------------------------------------------------------------
    def combos_group(self, pad):
        m = self.win.store.cfg["mouse"]
        mods = f"{wg.mouse_button_label('C', pad)} o {wg.mouse_button_label('Z', pad)}"
        if pad == "wiimote":
            desc = ("De 2 a 4 botones. Dos botones del Wiimote se aprietan casi a la vez. Las que usan C o Z "
                    "necesitan el Nunchuk (o ZL/ZR del Classic).")
        else:
            desc = (f"De 2 a 4 botones. Si {mods} están en una combinación, su función normal actúa al soltarlos. "
                    f"Mantén {mods} y repite la cruceta para seguir cambiando de ventana o pestaña.")
        box = group("Combinaciones", desc)
        rows = []

        def remove(item):
            m["combos"].remove(item)
            self.win.store.save()
            refresh()

        def refresh():
            while rows:
                box.remove(rows.pop())
            for item in m["combos"]:
                action = wg.MOUSE_ACTIONS.get(item["action"], ((), item["action"]))[1]
                usable = all(wg.mouse_pad_has(b, pad) for b in item["buttons"])
                row = Adw.ActionRow(title=wg.combo_label(item["buttons"], pad),
                                    subtitle=action if usable else f"{action} · necesita el Nunchuk")
                if not usable:
                    row.add_css_class("dim-label")
                delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                    tooltip_text="Quitar esta combinación", css_classes=["flat"])
                delete.connect("clicked", lambda _b, item=item: remove(item))
                row.add_suffix(delete)
                rows.append(row)
                box.add(row)

        add = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["flat"],
                         child=Adw.ButtonContent(icon_name="list-add-symbolic", label="Añadir"))
        add.connect("clicked", lambda _b: self.add_combo(pad, refresh))
        box.set_header_suffix(add)
        refresh()
        return box

    def add_combo(self, pad, refresh):
        cfg = self.win.store.cfg
        actions = {k: label for k, (_, label) in wg.MOUSE_ACTIONS.items() if k != "none"}
        sources = [s for s in wg.NUNCHUK_SOURCES + wg.WIIMOTE_SOURCES if wg.mouse_pad_has(s, pad)]
        checks = {s: Gtk.ToggleButton(label=wg.mouse_button_label(s, pad)) for s in sources}
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4, homogeneous=True,
                           column_spacing=6, row_spacing=6)
        for check in checks.values():
            flow.append(check)
        action = Gtk.DropDown.new_from_strings(list(actions.values()))
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.append(flow)
        content.append(action)
        dialog = Adw.AlertDialog(heading="Añadir combinación",
                                 body=f"Elige los botones de tu {wg.MOUSE_PADS[pad]} y qué hacen.",
                                 extra_child=content)
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("add", "Añadir")
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)

        def done(_d, response):
            if response != "add":
                return
            buttons = [s for s, check in checks.items() if check.get_active()]
            if problem := wg.combo_problem(cfg, buttons):
                self.win.toast(problem)
                return
            cfg["mouse"]["combos"].append({"buttons": buttons, "action": list(actions)[action.get_selected()]})
            self.win.store.save()
            refresh()

        dialog.connect("response", done)
        dialog.present(self)


class Preferences(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title="Preferencias", search_enabled=True, content_width=720)
        self.win = win
        cfg = win.store.cfg
        save = win.store.save

        def set_global(key, delay=False):
            def changed(value):
                cfg[key] = value
                save(delay)
            return changed

        general = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic", name="general")
        idle = dict(wg.IDLE_OPTIONS)
        idle.setdefault(cfg["idle_timeout_min"], f"Tras {cfg['idle_timeout_min']} min")  # puesto con --idle
        general.add(group(
            "Wiimote", None,
            combo_row("Apagar sin uso", idle, cfg["idle_timeout_min"], set_global("idle_timeout_min"),
                      "Ahorra pilas; para volver a usarlo, presiona 1+2"),
            combo_row("Recentrar giroscopio", {k: label for k, (_, label) in wg.RECENTER_COMBOS.items()},
                      cfg["recenter_combo"], set_global("recenter_combo")),
            switch_row("Vibrar cuando el juego lo pida", cfg["rumble"], set_global("rumble"))))
        general.add(group(
            "Varios Wiimotes", "Cada Wiimote es un jugador con su propio mando virtual; su LED muestra el número.",
            combo_row("Pueden usar el modo mouse", wg.MOUSE_PLAYERS, cfg["mouse_players"],
                      set_global("mouse_players"))))

        def set_ui(key):
            def changed(value):
                cfg["ui"][key] = value
                save()
                win.get_application().apply_ui()
            return changed

        general.add(group(
            "Apariencia", None,
            combo_row("Tema", THEMES, cfg["ui"]["theme"], set_ui("theme")),
            switch_row("Animaciones", cfg["ui"]["animations"], set_ui("animations"),
                       "Desactívalas si prefieres menos movimiento")))
        self.add(general)
        self.add(self.alerts_page())
        self.add(self.experimental_page())

    def experimental_page(self):
        cfg = self.win.store.cfg
        save = self.win.store.save

        def set_on(value):
            cfg["speaker"] = value
            save()

        def set_sounds(value):
            cfg["speaker_sounds"] = value
            save()

        def set_volume(value):
            cfg["speaker_volume"] = round(value)
            save(True)

        def test():
            players = self.win.players()
            self.win.toast(test_speaker(players[0] if players else None, cfg) or "Sonando…")

        page = Adw.PreferencesPage(title="Experimental", icon_name="applications-science-symbolic",
                                   name="experimental")
        page.add(group(
            "Bocina del Wiimote", "Los avisos suenan en el Wiimote además de en la PC. Se oye áspero, gasta más "
            "pilas y puede cortar la vibración mientras suena.",
            switch_row("Usar la bocina", cfg["speaker"], set_on),
            combo_row("Sonidos", wg.SPEAKER_SOUNDS, cfg["speaker_sounds"], set_sounds),
            scale_row("Volumen", 10, 100, cfg["speaker_volume"], set_volume, "{:.0f} %"),
            button_row("Probar", "Sonar", test, "En el primer Wiimote conectado")))
        return page

    def alerts_page(self):
        cfg = self.win.store.cfg
        sounds, notes = cfg["sounds"], cfg["notifications"]
        none = "— Ninguno —"
        files = {"": none} | {f: f for f in wg.sound_files()}
        page = Adw.PreferencesPage(title="Avisos", icon_name="preferences-system-notifications-symbolic",
                                   name="alerts")
        events = group("Sonido y notificación", "Cada aviso puede sonar en la PC y mostrar una notificación.")

        def summary(event):
            return " · ".join(x for x in (sounds.get(event) or "Sin sonido",
                                          "Con notificación" if notes.get(event) else "") if x)

        for event, label in wg.SOUND_EVENTS.items():
            row = Adw.ExpanderRow(title=label, subtitle=summary(event))

            def set_sound(name, event=event, row=row):
                sounds[event] = name
                self.win.store.save()
                wg.play_on_pc(name)
                row.set_subtitle(summary(event))

            def set_note(on, event=event, row=row, label=label):
                notes[event] = on
                self.win.store.save()
                row.set_subtitle(summary(event))
                if on:  # muestra cómo se verá
                    wg.notify_desktop(label, "Así se verá este aviso.")

            sound = combo_row("Sonido", files, sounds.get(event, ""), set_sound)
            play = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Escuchar", css_classes=["flat"])
            play.connect("clicked", lambda _b, event=event: wg.play_on_pc(sounds.get(event)))
            sound.add_suffix(play)
            row.add_row(sound)
            row.add_row(switch_row("Notificación de escritorio", notes.get(event, False), set_note))
            events.add(row)
        page.add(events)
        user_dir = wg.SOUND_DIRS[0]
        hint = f"Copia tus sonidos a {user_dir}"
        if not wg.shutil.which("notify-send"):
            hint += ". Para las notificaciones falta notify-send (paquete libnotify)."

        def open_dir():
            user_dir.mkdir(parents=True, exist_ok=True)
            Gio.AppInfo.launch_default_for_uri(user_dir.as_uri(), None)

        page.add(group("Tus sonidos", hint, button_row("Carpeta de sonidos", "Abrir", open_dir,
                                                       icon="folder-open-symbolic")))
        return page


class ReleaseDialog(Adw.Dialog):
    """Lo nuevo de la versión, página por página."""

    def __init__(self, release, on_done):
        super().__init__(title=release["title"], content_width=420, content_height=420)
        self.connect("closed", lambda _d: on_done())
        self.carousel = Adw.Carousel(hexpand=True, vexpand=True, interactive=True)
        for icon, title, body in release["pages"]:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, valign=Gtk.Align.CENTER, hexpand=True,
                           margin_start=32, margin_end=32)
            page.append(Gtk.Image(icon_name=icon, pixel_size=72, css_classes=["news-icon"]))
            page.append(Gtk.Label(label=title, wrap=True, justify=Gtk.Justification.CENTER, css_classes=["title-2"]))
            page.append(Gtk.Label(label=body, wrap=True, justify=Gtk.Justification.CENTER))
            self.carousel.append(page)
        self.next = Gtk.Button(label="Siguiente", halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        self.next.connect("clicked", lambda _b: self.advance())
        self.carousel.connect("notify::position", lambda *_: self.relabel())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_bottom=20)
        box.append(self.carousel)
        box.append(Adw.CarouselIndicatorDots(carousel=self.carousel))
        box.append(self.next)
        view = Adw.ToolbarView(content=box)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)
        self.relabel()

    def last(self):
        return round(self.carousel.get_position()) >= self.carousel.get_n_pages() - 1

    def relabel(self):
        self.next.set_label("Listo" if self.last() else "Siguiente")

    def advance(self):
        if self.last():
            self.close()
        else:
            self.carousel.scroll_to(self.carousel.get_nth_page(round(self.carousel.get_position()) + 1), True)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Wii Gamepad", default_width=1060, default_height=760)
        self.set_size_request(360, 500)
        self.store = Store()
        self.state = wg.read_state()
        self.state_text = None
        self.bridge = None  # el puente que arrancó esta ventana, si alguno
        self.selected = None
        self.page = None
        self.mapper_layout = "wiimote"  # el último mando elegido en el editor de botones
        self.mouse_pad = None  # (mando elegido a mano en «Modo mouse», el que había detectado entonces)

        self.list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.list.set_header_func(self.row_header)
        self.list.connect("row-activated", lambda _l, row: self.select(row.key))
        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("Preferencias", "app.preferences")
        menu.append("Novedades", "app.news")
        menu.append("Acerca de Wii Gamepad", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                       tooltip_text="Menú principal"))
        new = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Nuevo perfil")
        new.connect("clicked", lambda _b: self.new_profile())
        header.pack_start(new)
        self.notice_dot = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.START, css_classes=["notice-dot"])
        bell = Gtk.Overlay(child=Gtk.Image(icon_name="preferences-system-notifications-symbolic"))
        bell.add_overlay(self.notice_dot)
        self.notices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=8, margin_bottom=8,
                               margin_start=8, margin_end=8, width_request=280)
        self.notice_button = Gtk.MenuButton(child=bell, popover=Gtk.Popover(child=self.notices),
                                            tooltip_text="Avisos")
        header.pack_end(self.notice_button)
        self.refresh_notices()

        self.bridge_dot = Gtk.Label(label="●", css_classes=["bridge-dot"])
        self.bridge_label = Gtk.Label(xalign=0, hexpand=True, wrap=True, css_classes=["caption"])
        self.bridge_button = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["pill", "small"])
        self.bridge_button.connect("clicked", lambda _b: self.toggle_bridge())
        status = Gtk.Box(spacing=8, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        for w in (self.bridge_dot, self.bridge_label, self.bridge_button):
            status.append(w)

        sidebar = Adw.ToolbarView()
        sidebar.add_top_bar(header)
        sidebar.set_content(Gtk.ScrolledWindow(child=self.list, vexpand=True,
                                               hscrollbar_policy=Gtk.PolicyType.NEVER))
        sidebar.add_bottom_bar(status)

        self.split = Adw.NavigationSplitView(min_sidebar_width=250, max_sidebar_width=320,
                                             sidebar=Adw.NavigationPage(title="Wii Gamepad", child=sidebar))
        self.toasts = Adw.ToastOverlay(child=self.split)
        self.set_content(self.toasts)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 700sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        self.refresh(force=True)
        players = self.players()
        self.select(("player", players[0]["id"]) if players else ("profile", self.store.cfg["active"]), show=False)
        GLib.timeout_add(1000, self.poll)
        self.connect("close-request", self.on_close)
        GLib.idle_add(self.show_news)

    # --- datos -----------------------------------------------------------------
    def players(self):
        return (self.state or {}).get("players", [])

    def poll(self):
        state = wg.read_state()
        text = json.dumps(state, sort_keys=True)
        if text != self.state_text:
            self.state = state
            self.refresh()
        else:
            self.update_bridge()
        return True

    def refresh(self, force=False, rebuild_page=False):
        """Vuelve a llenar la barra lateral y actualiza la página abierta."""
        self.state_text = json.dumps(self.state, sort_keys=True)
        cfg = self.store.cfg
        while row := self.list.get_row_at_index(0):
            self.list.remove(row)
        online = {p["id"] for p in self.players()}
        for p in self.players():
            profile = cfg["devices"].get(p["id"], {}).get("profile")
            self.list.append(SidebarRow("Mandos", ("player", p["id"]), p["name"],
                                        profile if profile in cfg["profiles"] else f"{cfg['active']} (por defecto)",
                                        badge=str(p["number"]), battery=p.get("battery")))
        for wid, dev in cfg["devices"].items():
            if wid not in online:
                self.list.append(SidebarRow("Mandos", ("player", wid), dev.get("name") or wid, "Desconectado",
                                            badge="–", offline=True))
        if not self.list.get_row_at_index(0):
            hint = SidebarRow("Mandos", None, "Enciende un Wiimote", "Presiona 1+2 (o el botón rojo la primera vez)",
                              icon="input-gaming-symbolic")
            hint.set_activatable(False)
            hint.set_selectable(False)
            self.list.append(hint)
        owner = next((p for p in self.players() if p.get("mouse")), None)
        self.list.append(SidebarRow("Escritorio", ("mouse", ""), "Modo mouse",
                                    f"Activo: {owner['name']}" if owner else
                                    wg.mouse_toggle_label(cfg["mouse_combo"], wg.mouse_pad_of(self.players()[0])
                                                          if self.players() else "nunchuk"),
                                    icon="input-mouse-symbolic"))
        for name in cfg["profiles"]:
            self.list.append(SidebarRow("Perfiles", ("profile", name), name,
                                        "Por defecto" if name == cfg["active"] else "",
                                        icon="starred-symbolic" if name == cfg["active"]
                                        else "applications-games-symbolic"))
        self.highlight()
        self.update_bridge()
        if rebuild_page and self.selected:
            self.select(self.selected, show=False)
        elif self.page is not None:
            self.page.refresh(self.state)

    def row_header(self, row, before):
        if before is None or before.section != row.section:
            row.set_header(Gtk.Label(label=row.section, xalign=0, css_classes=["heading", "dim-label"],
                                     margin_top=12 if before else 6, margin_bottom=4, margin_start=12))
        else:
            row.set_header(None)

    def highlight(self):
        i = 0
        while row := self.list.get_row_at_index(i):
            if row.key == self.selected:
                self.list.select_row(row)
                return
            i += 1
        self.list.unselect_all()

    def select(self, key, show=True):
        """Abre la página de un Wiimote o de un perfil."""
        cfg = self.store.cfg
        if key and key[0] == "profile" and key[1] not in cfg["profiles"]:
            key = ("profile", cfg["active"])
        if key is None:
            key = ("profile", cfg["active"])
        self.selected = key
        if self.page is not None and isinstance(self.page, PlayerPage):
            self.page.pad.detach()
        self.page = (ProfilePage(self, key[1]) if key[0] == "profile" else MousePage(self) if key[0] == "mouse"
                     else PlayerPage(self, key[1]))
        self.split.set_content(self.page)
        GLib.idle_add(lambda: self.set_focus(None))  # que no empiece escribiendo en «Nombre»
        if show:
            self.split.set_show_content(True)
        self.highlight()

    # --- novedades y avisos ------------------------------------------------------
    def show_news(self, again=False):
        release = wn.RELEASES[0] if again else wn.pending_release(self.store.cfg)
        if release:
            def done():
                wn.mark_seen(self.store.cfg)
                self.store.save()
            ReleaseDialog(release, done).present(self)

    def refresh_notices(self):
        while child := self.notices.get_first_child():
            self.notices.remove(child)
        unread = wn.unread_notices(self.store.cfg)
        self.notice_dot.set_visible(bool(unread))
        if not unread:
            self.notices.append(Gtk.Label(label="No hay avisos nuevos", css_classes=["dim-label"]))
        for notice in unread:
            self.notices.append(self.notice_row(notice))

    def notice_row(self, notice):
        go = Gtk.Button(label=notice["action"], css_classes=["suggested-action"])
        go.connect("clicked", lambda _b: self.open_notice(notice))
        dismiss = Gtk.Button(label="Descartar")
        dismiss.connect("clicked", lambda _b: self.read_notice(notice))
        buttons = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        buttons.append(dismiss)
        buttons.append(go)
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        row.append(Gtk.Label(label=notice["title"], xalign=0, css_classes=["heading"]))
        row.append(Gtk.Label(label=notice["body"], xalign=0, wrap=True, max_width_chars=36))
        row.append(buttons)
        return row

    def read_notice(self, notice):
        wn.mark_read(self.store.cfg, notice["id"])
        self.store.save()
        self.refresh_notices()

    def open_notice(self, notice):
        self.notice_button.popdown()
        self.read_notice(notice)
        kind, _, where = notice["target"].partition(":")
        if kind == "mouse" or (kind, where) == ("preferences", "mouse"):
            self.select(("mouse", ""))
        elif kind == "preferences":
            self.get_application().show_preferences(page=where)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=2))

    # --- perfiles ----------------------------------------------------------------
    def ask_name(self, heading, initial, verb, done):
        entry = Gtk.Entry(text=initial, activates_default=True)
        dialog = Adw.AlertDialog(heading=heading, body="Nombre del perfil:", extra_child=entry)
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("ok", verb)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")

        def validate(*_):
            name = entry.get_text().strip()
            dialog.set_response_enabled("ok", bool(name) and name not in self.store.cfg["profiles"])

        entry.connect("changed", validate)
        validate()
        dialog.connect("response", lambda _d, r: r == "ok" and done(entry.get_text().strip()))
        dialog.present(self)
        entry.grab_focus()

    def new_profile(self):
        def done(name):
            self.store.cfg["profiles"][name] = wg.new_profile()
            self.store.save()
            self.refresh()
            self.select(("profile", name))
            self.toast(f"Creado «{name}» con el layout recomendado")
        self.ask_name("Nuevo perfil", "Mi perfil", "Crear", done)

    def duplicate_profile(self, old):
        def done(name):
            self.store.cfg["profiles"][name] = copy.deepcopy(self.store.cfg["profiles"][old])
            self.store.save()
            self.refresh()
            self.select(("profile", name))
        self.ask_name("Duplicar perfil", f"{old} (copia)", "Duplicar", done)

    def rename_profile(self, old):
        def done(name):
            wg.rename_profile(self.store.cfg, old, name)
            self.store.save()
            self.refresh()
            self.select(("profile", name))
        self.ask_name("Renombrar perfil", old, "Renombrar", done)

    def reset_profile(self, name):
        self.store.cfg["profiles"][name] = wg.default_config()["profiles"].get(name) or wg.new_profile()
        self.store.save()
        self.select(("profile", name), show=False)
        self.toast(f"«{name}» restaurado")

    def delete_profile(self, name):
        dialog = Adw.AlertDialog(heading=f"¿Borrar «{name}»?",
                                 body="Los Wiimotes que lo usaban pasarán al perfil por defecto.")
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("delete", "Borrar")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def response(_d, r):
            if r == "delete":
                wg.delete_profile(self.store.cfg, name)
                self.store.save()
                self.refresh()
                self.select(("profile", self.store.cfg["active"]))
                self.toast(f"«{name}» borrado")
        dialog.connect("response", response)
        dialog.present(self)

    # --- puente -----------------------------------------------------------------
    def update_bridge(self):
        running = self.state is not None
        auto = SERVICE.exists()  # instalado con install.sh --service (o el paquete)
        mine = self.bridge is not None and self.bridge.poll() is None
        for c in ("off", "waiting"):
            self.bridge_dot.remove_css_class(c)
        if running:
            n = len(self.players())
            self.bridge_label.set_label("Puente activo · " + (f"{n} Wiimote" + ("s" if n != 1 else "") if n else
                                                              "esperando al Wiimote"))
            if not n:
                self.bridge_dot.add_css_class("waiting")
        elif auto:
            self.bridge_label.set_label("Arranca solo al encender un Wiimote")
            self.bridge_dot.add_css_class("waiting")
        else:
            self.bridge_label.set_label("Puente detenido")
            self.bridge_dot.add_css_class("off")
        self.bridge_button.set_label("Detener" if mine else "Iniciar")
        self.bridge_button.set_visible(mine or not (running or auto))

    def toggle_bridge(self):
        if self.bridge and self.bridge.poll() is None:
            self.bridge.terminate()
            self.bridge = None
        else:
            script = Path(__file__).resolve().parent / "wii_gamepad.py"
            self.bridge = subprocess.Popen([sys.executable, str(script)], start_new_session=True,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        GLib.timeout_add(800, lambda: self.poll() and False)

    def on_close(self, _win):
        self.store.flush() if self.store._pending else None
        if not (self.bridge and self.bridge.poll() is None):
            return False
        dialog = Adw.AlertDialog(heading="¿Detener también el puente?",
                                 body="Si sigue en marcha, los Wiimotes seguirán funcionando como mandos.")
        dialog.add_response("keep", "Dejarlo en marcha")
        dialog.add_response("stop", "Detenerlo")
        dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("keep")

        def response(_d, r):
            if r == "stop":
                self.bridge.terminate()
            self.bridge = None
            self.destroy()
        dialog.connect("response", response)
        dialog.present(self)
        return True


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.system_animations = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name("Wii Gamepad")
        Gtk.Window.set_default_icon_name("wii-gamepad")
        css = Gtk.CssProvider()
        css.load_from_string(CSS + wm.CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, cb, accels in (("preferences", self.show_preferences, ["<Control>comma"]),
                                 ("news", lambda *_: self.get_active_window().show_news(again=True), []),
                                 ("about", self.show_about, []),
                                 ("quit", lambda *_: self.get_active_window().close(), ["<Control>q"])):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
            self.set_accels_for_action(f"app.{name}", accels)

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        self.apply_ui()
        win.present()

    def apply_ui(self):
        win = self.get_active_window()
        ui = (win.store.cfg if win else wg.load_config())["ui"]
        Adw.StyleManager.get_default().set_color_scheme(
            {"light": Adw.ColorScheme.FORCE_LIGHT, "dark": Adw.ColorScheme.FORCE_DARK}.get(ui["theme"],
                                                                                          Adw.ColorScheme.DEFAULT))
        settings = Gtk.Settings.get_default()
        if self.system_animations is None:
            self.system_animations = settings.props.gtk_enable_animations
        settings.props.gtk_enable_animations = self.system_animations and ui["animations"]

    def show_preferences(self, *_, page=None):
        dialog = Preferences(self.get_active_window())
        if page:
            dialog.set_visible_page_name(page)
        dialog.present(self.get_active_window())

    def show_about(self, *_):
        about = Adw.AboutDialog(application_name="Wii Gamepad", application_icon="wii-gamepad",
                                developer_name="AlexDev07", version=VERSION,
                                comments="Wiimote y Nunchuk como mando de Xbox 360 en Linux",
                                website="https://github.com/AlexGameDev07/wii-gamepad",
                                issue_url="https://github.com/AlexGameDev07/wii-gamepad/issues",
                                license_type=Gtk.License.GPL_3_0, copyright="© 2026 AlexDev07")
        about.add_credit_section("Sonidos", ["Kenney https://kenney.nl"])
        about.present(self.get_active_window())


def main():
    sys.exit(App().run(sys.argv))


if __name__ == "__main__":
    main()
