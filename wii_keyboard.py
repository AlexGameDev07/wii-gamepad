#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Teclado en pantalla de wii-gamepad: escribir con el Wiimote en modo mouse.

El puente (wii_gamepad.py) lleva el estado (qué tecla está seleccionada) y
escribe con su teclado virtual (uinput), así la ventana nunca le quita el foco
al campo de texto. Además lanza este archivo como dos procesos auxiliares:
  --window  dibuja el teclado; lee el estado por stdin, una línea JSON por cambio
  --watch   avisa por stdout si el foco está en un campo de texto (AT-SPI): "1" o "0"
"""
import ctypes
import ctypes.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from evdev import ecodes as e

BG, KEY_BG, FG, MUTED, ACCENT, ON = "#1e1f26", "#3a3d4a", "#e8e8ee", "#6c6f80", "#3fa7ff", "#2e6da4"

# Teclas: un carácter escribe ese carácter; si no, (etiqueta, acción, ancho).
# Todas las filas miden lo mismo (11.5) para que subir y bajar caiga en la tecla de encima.
PAGES = [
    [[*"1234567890", ("borrar", "backspace", 1.5)],
     [*"qwertyuiop", ("´", "dead:dead_acute", 1.5)],
     [*"asdfghjklñ", ("↵", "enter", 1.5)],
     [("Mayús", "shift", 1.5), *"zxcvbnm,.-"],
     [("?123", "page", 1.5), "@", ("espacio", "space", 5), ("←", "left"), ("→", "right"), ("cerrar", "hide", 2)]],
    [[*'!"#$%&/()=', ("borrar", "backspace", 1.5)],
     [*"?¿¡'+*[]{}", ("¨", "dead:dead_diaeresis", 1.5)],
     [*"@_:;<>|\\~^", ("↵", "enter", 1.5)],
     [("´", "dead:dead_acute", 1.5), *"°¬`€£§«»©·"],
     [("abc", "page", 1.5), "@", ("espacio", "space", 5), ("←", "left"), ("→", "right"), ("cerrar", "hide", 2)]],
]
SPECIAL_KEYS = {"backspace": e.KEY_BACKSPACE, "enter": e.KEY_ENTER, "space": e.KEY_SPACE,
                "left": e.KEY_LEFT, "right": e.KEY_RIGHT}
DIRS = {"Up": (0, -1), "Down": (0, 1), "Left": (-1, 0), "Right": (1, 0)}
# Con el teclado visible: botón del Wiimote -> acción (None = la tecla seleccionada)
BUTTONS = {"A": None, "B": "backspace", "C": "hide", "Z": "shift", "1": "space", "2": "enter"}
HINT = "A escribir · B borrar · C cerrar · Z mayúsculas · 1 espacio · 2 Enter · cruceta o stick: elegir"


def page_rows(page):
    return [[(*k, 1.0)[:3] if isinstance(k, tuple) else (k, k, 1.0) for k in row] for row in PAGES[page]]


def centers(row):
    x, out = 0.0, []
    for _, _, w in row:
        out.append(x + w / 2)
        x += w
    return out


def current_layout():
    """(distribución, variante) xkb del usuario: la de GNOME, la de KDE o la del sistema; si no, us."""
    def run(*cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    if m := re.search(r"\('xkb', '([^']+)'\)", run("gsettings", "get", "org.gnome.desktop.input-sources", "sources")):
        layout, _, variant = m.group(1).partition("+")
        return layout, variant
    kxkbrc = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kxkbrc"
    try:
        text = kxkbrc.read_text()
        if m := re.search(r"^LayoutList=([^,\n]+)", text, re.M):
            v = re.search(r"^VariantList=([^,\n]*)", text, re.M)
            return m.group(1), v.group(1) if v else ""
    except OSError:
        pass
    out = run("localectl", "status")
    if m := re.search(r"X11 Layout:\s*([^,\s]+)", out):
        v = re.search(r"X11 Variant:\s*([^,\s]+)", out)
        return m.group(1), v.group(1) if v else ""
    return "us", ""


class Layout:
    """Qué tecla (y modificadores) produce cada carácter con la distribución del
    usuario: el teclado virtual manda teclas físicas y es el escritorio el que
    las convierte en letras según su distribución."""

    MODS = ((), (e.KEY_LEFTSHIFT,), (e.KEY_RIGHTALT,), (e.KEY_LEFTSHIFT, e.KEY_RIGHTALT))  # nivel xkb -> modificadores
    TYPING_KEYS = [*range(e.KEY_1, e.KEY_SLASH + 1), e.KEY_102ND, e.KEY_SPACE]
    DEAD = {"´": "dead_acute", "¨": "dead_diaeresis", "~": "dead_tilde", "^": "dead_circumflex", "`": "dead_grave"}

    def __init__(self, layout=None, variant=""):
        if layout is None:
            layout, variant = current_layout()
        self.name = layout + (f"({variant})" if variant else "")
        self.chars, self.syms = {}, {}  # carácter / nombre de keysym (teclas muertas) -> (tecla, modificadores)
        try:
            self._load(layout, variant)
        except OSError as err:
            print(f"Teclado: no se pudo leer la distribución {self.name} ({err}); se usa us")
            if layout != "us":
                self._load("us", "")

    def _load(self, layout, variant):
        lib = ctypes.CDLL(ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0")

        class Names(ctypes.Structure):
            _fields_ = [(f, ctypes.c_char_p) for f in ("rules", "model", "layout", "variant", "options")]

        vp, u32 = ctypes.c_void_p, ctypes.c_uint32
        lib.xkb_context_new.restype, lib.xkb_context_new.argtypes = vp, [ctypes.c_int]
        lib.xkb_keymap_new_from_names.restype = vp
        lib.xkb_keymap_new_from_names.argtypes = [vp, ctypes.POINTER(Names), ctypes.c_int]
        lib.xkb_keymap_num_levels_for_key.restype = u32
        lib.xkb_keymap_num_levels_for_key.argtypes = [vp, u32, u32]
        lib.xkb_keymap_key_get_syms_by_level.restype = ctypes.c_int
        lib.xkb_keymap_key_get_syms_by_level.argtypes = [vp, u32, u32, u32, ctypes.POINTER(ctypes.POINTER(u32))]
        lib.xkb_keysym_to_utf32.restype, lib.xkb_keysym_to_utf32.argtypes = u32, [u32]
        lib.xkb_keysym_get_name.argtypes = [u32, ctypes.c_char_p, ctypes.c_size_t]
        lib.xkb_keymap_unref.argtypes = lib.xkb_context_unref.argtypes = [vp]

        ctx = lib.xkb_context_new(0)
        names = Names(b"evdev", b"pc105", layout.encode(), variant.encode() or None, None)
        keymap = lib.xkb_keymap_new_from_names(ctx, ctypes.byref(names), 0)
        if not keymap:
            lib.xkb_context_unref(ctx)
            raise OSError("distribución desconocida")
        buf = ctypes.create_string_buffer(64)
        for level in range(4):  # primero los niveles bajos: mejor sin AltGr si el carácter está en dos teclas
            for code in self.TYPING_KEYS:
                xkb_code = code + 8  # los códigos xkb son los de evdev + 8
                if level >= lib.xkb_keymap_num_levels_for_key(keymap, xkb_code, 0):
                    continue
                syms = ctypes.POINTER(u32)()
                for i in range(lib.xkb_keymap_key_get_syms_by_level(keymap, xkb_code, 0, level, ctypes.byref(syms))):
                    stroke = (code, self.MODS[level])
                    cp = lib.xkb_keysym_to_utf32(syms[i])
                    if cp and chr(cp).isprintable():
                        self.chars.setdefault(chr(cp), stroke)
                    else:
                        lib.xkb_keysym_get_name(syms[i], buf, len(buf))
                        self.syms.setdefault(buf.value.decode(), stroke)
        lib.xkb_keymap_unref(keymap)
        lib.xkb_context_unref(ctx)

    def strokes(self, ch):
        """Pulsaciones [(tecla, modificadores)] que escriben ch, o None si esta distribución no puede."""
        if ch in self.chars:
            return [self.chars[ch]]
        dead = self.DEAD.get(ch)  # p. ej. ~ en latam es una tecla muerta: se escribe con ella + espacio
        if dead in self.syms and " " in self.chars:
            return [self.syms[dead], self.chars[" "]]
        return None

    def action_strokes(self, action, shift=False):
        if action.startswith("dead:"):
            name = action[5:]
            return [self.syms[name]] if name in self.syms else None
        if action in SPECIAL_KEYS:
            return [(SPECIAL_KEYS[action], ())]
        if len(action) == 1:
            return self.strokes(action.upper() if shift else action)
        return []  # acciones del propio teclado (página, mayúsculas, cerrar)


class Keyboard:
    """Estado del teclado en pantalla, que maneja el puente, y sus procesos auxiliares."""

    REPEAT = (0.4, 0.09)  # s hasta que una dirección o B mantenidos se repiten, y entre repeticiones
    STICK = 0.6  # inclinación del stick que cuenta como una dirección

    def __init__(self, tap):
        self.tap = tap  # tap([(tecla, modificadores), ...]) escribe con el teclado virtual
        self.layout = None  # se lee al mostrarlo la primera vez
        self.visible = self.auto = self.shift = False
        self.page, self.row, self.col = 0, 1, 0
        self.held = {}  # "Up"/"B"/"stick"... -> cuándo repetir
        self.stick_dir = None
        self.window = self.watcher = None

    # --- procesos auxiliares -----------------------------------------------------------
    @staticmethod
    def _spawn(flag):
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), flag], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE if flag == "--watch" else subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)

    @staticmethod
    def _stop(proc):
        if proc and proc.poll() is None:
            proc.stdin.close()  # al ver stdin cerrado salen solos
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()

    def start_watch(self):
        if self.watcher is None or self.watcher.poll() is not None:
            try:
                self.watcher = self._spawn("--watch")
            except OSError:
                self.watcher = None

    def stop_watch(self):
        self._stop(self.watcher)
        self.watcher = None

    def watch_fd(self):
        return self.watcher.stdout.fileno() if self.watcher else None

    def read_focus(self):
        """Lee al vigilante de foco: True / False si el foco entró o salió de un campo de texto, None si nada."""
        data = os.read(self.watcher.stdout.fileno(), 1024)
        if not data:  # terminó (p. ej. falta AT-SPI): queda solo el botón del teclado
            print("Teclado: no se puede detectar el foco (¿falta python3-gobject / AT-SPI?)")
            self.stop_watch()
            return None
        return data.strip().endswith(b"1")

    def close(self):
        self.stop_watch()
        self._stop(self.window)
        self.window = None

    def _send(self):
        if self.window is None or self.window.poll() is not None:
            if not self.visible:
                return
            try:
                self.window = self._spawn("--window")
            except OSError:
                return
        state = {"show": self.visible, "page": self.page, "row": self.row, "col": self.col, "shift": self.shift}
        try:
            self.window.stdin.write(json.dumps(state).encode() + b"\n")
            self.window.stdin.flush()
        except OSError:
            self.window = None

    # --- estado ------------------------------------------------------------------------
    def show(self, auto=False):
        if self.layout is None:
            self.layout = Layout()
            print(f"Teclado en pantalla con la distribución {self.layout.name}")
        self.visible, self.auto = True, auto
        self._send()

    def hide(self):
        if self.visible:
            self.visible = self.auto = self.shift = False
            self.held.clear()
            self.stick_dir = None
            self._send()

    def move(self, dx, dy):
        rows = page_rows(self.page)
        if dy:
            x = centers(rows[self.row])[self.col]
            self.row = (self.row + dy) % len(rows)
            cs = centers(rows[self.row])
            self.col = min(range(len(cs)), key=lambda i: abs(cs[i] - x))
        else:
            self.col = (self.col + dx) % len(rows[self.row])
        self._send()

    def activate(self, action):
        if action == "hide":
            self.hide()
        elif action == "shift":
            self.shift = not self.shift
            self._send()
        elif action == "page":
            self.page = 1 - self.page
            self.shift = False
            self._send()
        elif strokes := self.layout.action_strokes(action, self.shift):
            self.tap(strokes)
            if self.shift and len(action) == 1:  # mayúscula solo para una letra
                self.shift = False
                self._send()

    def button(self, src, on, now):
        """Botón del Wiimote con el teclado visible; False si no es del teclado."""
        if src in DIRS:
            if on:
                self.move(*DIRS[src])
                self.held[src] = now + self.REPEAT[0]
            else:
                self.held.pop(src, None)
            return True
        if src not in BUTTONS:
            return False
        if on:
            self.activate(BUTTONS[src] or page_rows(self.page)[self.row][self.col][1])
            if src == "B":
                self.held[src] = now + self.REPEAT[0]
        else:
            self.held.pop(src, None)
        return True

    def stick(self, x, y, now):
        d = None
        if max(abs(x), abs(y)) > self.STICK:
            d = ("Right" if x > 0 else "Left") if abs(x) > abs(y) else ("Down" if y > 0 else "Up")
        if d != self.stick_dir:
            self.stick_dir = d
            self.held.pop("stick", None)
            if d:
                self.move(*DIRS[d])
                self.held["stick"] = now + self.REPEAT[0]

    def tick(self, now):
        """Repite lo mantenido; devuelve en cuántos segundos volver a llamar."""
        wait = 1.0
        for src, t in list(self.held.items()):
            if now >= t:
                if src == "B":
                    self.activate("backspace")
                else:
                    self.move(*DIRS[self.stick_dir if src == "stick" else src])
                t = self.held[src] = now + self.REPEAT[1]
            wait = min(wait, t - now)
        return max(0.005, wait)


def primary_monitor():
    """(x, y, ancho, alto) del monitor principal, o None: con varios monitores Tk los ve como una sola pantalla."""
    try:
        out = subprocess.run(["xrandr", "--listmonitors"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"\+\*\S+ (\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)", out)
    return (int(m[3]), int(m[4]), int(m[1]), int(m[2])) if m else None


def run_window():
    import tkinter as tk

    unit, gap, pad = 54, 6, 10
    root = tk.Tk(className="wii-gamepad-keyboard")
    root.overrideredirect(True)  # sin bordes y sin recibir el foco: las teclas van al campo de texto
    root.attributes("-topmost", True)
    root.withdraw()
    canvas = tk.Canvas(root, bg=BG, highlightthickness=1, highlightbackground=ACCENT)
    canvas.pack(fill="both", expand=True)
    layout = Layout()
    monitor = primary_monitor()
    buf = b""

    def draw(state):
        if not state["show"]:
            root.withdraw()
            return
        canvas.delete("all")
        rows = page_rows(state["page"])
        width = int(max(sum(w for *_, w in row) for row in rows) * (unit + gap) - gap + 2 * pad)
        y = pad
        for r, row in enumerate(rows):
            x = pad
            for c, (label, action, w) in enumerate(row):
                kw = w * (unit + gap) - gap
                selected = (r, c) == (state["row"], state["col"])
                on = action == "shift" and state["shift"]
                usable = layout.action_strokes(action, state["shift"]) is not None
                canvas.create_rectangle(x, y, x + kw, y + unit, outline="", width=0,
                                        fill=ACCENT if selected else ON if on else KEY_BG)
                if len(action) == 1 and state["shift"]:
                    label = label.upper()
                canvas.create_text(x + kw / 2, y + unit / 2, text=label, font=("Sans", 15 if len(label) == 1 else 11),
                                   fill="#0b1220" if selected else FG if usable else MUTED)
                x += kw + gap
            y += unit + gap
        canvas.create_text(width / 2, y + 8, text=HINT, fill=MUTED, font=("Sans", 9))
        height = y + 22
        mx, my, mw, mh = monitor or (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        root.geometry(f"{width}x{height}+{mx + (mw - width) // 2}+{my + mh - height - 60}")
        root.deiconify()
        root.lift()

    def on_input(*_):
        nonlocal buf
        data = os.read(sys.stdin.fileno(), 65536)
        if not data:
            root.destroy()
            return
        buf += data
        *lines, buf = buf.split(b"\n")
        if lines:
            draw(json.loads(lines[-1]))

    root.tk.createfilehandler(sys.stdin, tk.READABLE, on_input)
    root.mainloop()


def run_watch():
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, GLib

    last = None

    def on_focus(event):
        nonlocal last
        if not event.detail1:  # perder el foco no dice nada: lo dice el siguiente que lo gana
            return
        try:
            states = event.source.get_state_set()
            text = states.contains(Atspi.StateType.EDITABLE) and not states.contains(Atspi.StateType.READ_ONLY)
        except GLib.Error:
            text = False
        if text != last:
            last = text
            print(int(text), flush=True)

    listener = Atspi.EventListener.new(on_focus)
    listener.register("object:state-changed:focused")
    GLib.io_add_watch(GLib.IOChannel.unix_new(sys.stdin.fileno()), GLib.PRIORITY_DEFAULT,
                      GLib.IOCondition.IN | GLib.IOCondition.HUP, lambda *_: Atspi.event_quit() or False)
    Atspi.event_main()


if __name__ == "__main__":
    if sys.argv[1:] == ["--window"]:
        run_window()
    elif sys.argv[1:] == ["--watch"]:
        run_watch()
    else:
        sys.exit(__doc__)
