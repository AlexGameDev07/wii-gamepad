#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Ventana para configurar wii-gamepad: qué botón de Xbox corresponde a cada
botón del Wiimote/Nunchuk, con perfiles por juego y prueba en vivo."""
import copy
import os
import select
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

import evdev
from evdev import ecodes as e, ff

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wii_gamepad as wg  # noqa: E402

BG, PANEL, FG, MUTED, ACCENT, OFF = "#1e1f26", "#2a2c36", "#e8e8ee", "#9a9cab", "#3fa7ff", "#3a3d4a"
XBOX_COLORS = {"A": "#4caf50", "B": "#f44336", "X": "#2196f3", "Y": "#ffc107"}
SERVICE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / "wii-gamepad.service"
IDLE_OPTIONS = {0: "Nunca", 5: "Tras 5 min", 10: "Tras 10 min", 15: "Tras 15 min", 20: "Tras 20 min",
                30: "Tras 30 min"}


class LiveView(tk.Canvas):
    """Dibuja el mando Xbox virtual y lo ilumina leyendo sus eventos."""

    def __init__(self, master):
        super().__init__(master, width=360, height=230, bg=PANEL, highlightthickness=0)
        self.items = {}
        self.dev = None
        self.state = {"lx": 0, "ly": 0, "rx": 0, "ry": 0}

        def btn(key, x, y, r=14, label=None):
            self.items[key] = self.create_oval(x - r, y - r, x + r, y + r, fill=OFF, outline="")
            self.create_text(x, y, text=label or key, fill=FG, font=("Sans", 9, "bold"))

        def rect(key, x, y, w, h, label):
            self.items[key] = self.create_rectangle(x, y, x + w, y + h, fill=OFF, outline="")
            self.create_text(x + w / 2, y + h / 2, text=label, fill=FG, font=("Sans", 8, "bold"))

        rect("LT", 40, 8, 60, 18, "LT")
        rect("RT", 260, 8, 60, 18, "RT")
        rect("LB", 40, 32, 60, 18, "LB")
        rect("RB", 260, 32, 60, 18, "RB")
        btn("Y", 290, 72, label="Y")
        btn("X", 262, 98, label="X")
        btn("B", 318, 98, label="B")
        btn("A", 290, 124, label="A")
        btn("Back", 150, 90, 10, "◀")
        btn("Guide", 180, 70, 13, "⊛")
        btn("Start", 210, 90, 10, "▶")
        rect("DUp", 102, 140, 22, 22, "↑")
        rect("DDown", 102, 184, 22, 22, "↓")
        rect("DLeft", 80, 162, 22, 22, "←")
        rect("DRight", 124, 162, 22, 22, "→")
        self.sticks = {}
        for side, (cx, cy) in {"l": (70, 95), "r": (230, 175)}.items():
            ring = self.create_oval(cx - 30, cy - 30, cx + 30, cy + 30, outline=MUTED, width=2)
            dot = self.create_oval(cx - 9, cy - 9, cx + 9, cy + 9, fill=MUTED, outline="")
            self.sticks[side] = (cx, cy, dot, ring)
        self.status = self.create_text(180, 222, text="", fill=MUTED, font=("Sans", 8))
        self.keymap = {v: k for k, v in wg.KEY_TARGETS.items()}
        self.thumbs = {e.BTN_THUMBL: "l", e.BTN_THUMBR: "r"}
        self.after(200, self.poll)

    def light(self, key, on):
        color = XBOX_COLORS.get(key, ACCENT) if on else OFF
        self.itemconfigure(self.items[key], fill=color)

    def draw_stick(self, side):
        cx, cy, dot, _ = self.sticks[side]
        x = cx + self.state[side + "x"] / 32767 * 22
        y = cy + self.state[side + "y"] / 32767 * 22
        self.coords(dot, x - 9, y - 9, x + 9, y + 9)

    def connect(self):
        self.dev = wg.find(wg.PAD_NAME)
        return self.dev is not None

    def poll(self):
        try:
            if self.dev is None and not self.connect():
                self.itemconfigure(self.status, text="Mando virtual no encontrado (inicia el puente)")
            else:
                self.itemconfigure(self.status, text="Prueba en vivo: aprieta botones del Wiimote")
                while select.select([self.dev], [], [], 0)[0]:
                    for ev in self.dev.read():
                        self.on_event(ev)
        except OSError:
            self.dev = None
        self.after(16, self.poll)

    def on_event(self, ev):
        if ev.type == e.EV_KEY:
            if ev.code in self.thumbs:
                _, _, _, ring = self.sticks[self.thumbs[ev.code]]
                self.itemconfigure(ring, outline=ACCENT if ev.value else MUTED)
            elif ev.code in self.keymap and self.keymap[ev.code] in self.items:
                self.light(self.keymap[ev.code], ev.value)
        elif ev.type == e.EV_ABS:
            if ev.code == e.ABS_Z:
                self.light("LT", ev.value > 30)
            elif ev.code == e.ABS_RZ:
                self.light("RT", ev.value > 30)
            elif ev.code == e.ABS_HAT0X:
                self.light("DLeft", ev.value < 0)
                self.light("DRight", ev.value > 0)
            elif ev.code == e.ABS_HAT0Y:
                self.light("DUp", ev.value < 0)
                self.light("DDown", ev.value > 0)
            else:
                key = {e.ABS_X: "lx", e.ABS_Y: "ly", e.ABS_RX: "rx", e.ABS_RY: "ry"}.get(ev.code)
                if key:
                    self.state[key] = ev.value
                    self.draw_stick(key[0])


class App(tk.Tk):
    def __init__(self):
        # className = StartupWMClass del .desktop: así el dock la asocia a su entrada del menú
        super().__init__(className="wii-gamepad")
        self.title("Wii Gamepad — Configuración")
        icon = Path(__file__).resolve().parent / "wii-gamepad.svg"
        try:
            self.iconphoto(True, tk.PhotoImage(file=str(icon)))
        except tk.TclError:  # Tk sin soporte SVG o icono no instalado
            pass
        self.configure(bg=BG)
        self.minsize(760, 540)
        self.cfg = wg.load_config()
        self.loading = False
        self.bridge = None
        self.style()
        self.build()
        self.load_profile()
        self.after(1000, self.refresh_bridge)

    def style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, font=("Sans", 10))
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("Panel.TLabel", background=PANEL, foreground=FG)
        s.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        s.configure("Title.TLabel", background=PANEL, foreground=ACCENT, font=("Sans", 11, "bold"))
        s.configure("Head.TLabel", background=BG, foreground=FG, font=("Sans", 15, "bold"))
        s.configure("Status.TLabel", background=BG, foreground=MUTED)
        s.configure("TButton", background=OFF, foreground=FG, borderwidth=0, padding=(10, 5))
        s.map("TButton", background=[("active", "#4a4e5e")])
        s.configure("Accent.TButton", background=ACCENT, foreground="#0b1220")
        s.map("Accent.TButton", background=[("active", "#6bbcff")])
        s.configure("TCombobox", arrowcolor=FG, background=OFF, foreground=FG, selectbackground=PANEL,
                    selectforeground=FG)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)])
        s.configure("Panel.TCheckbutton", background=PANEL, foreground=FG)
        s.map("Panel.TCheckbutton", background=[("active", PANEL)])
        s.configure("Horizontal.TScale", background=PANEL, troughcolor=OFF)
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

    def panel(self, parent, title):
        f = ttk.Frame(parent, style="Panel.TFrame", padding=12)
        ttk.Label(f, text=title, style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        f.columnconfigure(1, weight=1)
        return f

    def combo(self, parent, row, label, options, on_change):
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
        var = tk.StringVar()
        cb = ttk.Combobox(parent, textvariable=var, values=list(options.values()), state="readonly", width=20)
        cb.grid(row=row, column=1, sticky="ew", pady=2)
        cb.bind("<<ComboboxSelected>>", lambda _: on_change())
        return var

    def build(self):
        top = ttk.Frame(self, padding=(14, 12, 14, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Wii Gamepad", style="Head.TLabel").pack(side="left")
        ttk.Button(top, text="Borrar", command=self.delete_profile).pack(side="right", padx=2)
        ttk.Button(top, text="Renombrar", command=self.rename_profile).pack(side="right", padx=2)
        ttk.Button(top, text="Duplicar", command=self.duplicate_profile).pack(side="right", padx=2)
        ttk.Button(top, text="Nuevo", command=self.new_profile).pack(side="right", padx=2)
        self.profile_var = tk.StringVar()
        self.profile_cb = ttk.Combobox(top, textvariable=self.profile_var, state="readonly", width=28)
        self.profile_cb.pack(side="right", padx=8)
        self.profile_cb.bind("<<ComboboxSelected>>", lambda _: self.select_profile())
        ttk.Label(top, text="Perfil:", style="Status.TLabel").pack(side="right")

        body = ttk.Frame(self, padding=(14, 6))
        body.pack(fill="both", expand=True)
        body.columnconfigure((0, 1), weight=1, uniform="col")

        self.targets_by_label = {v: k for k, v in wg.TARGETS.items()}
        self.button_vars = {}
        wii = self.panel(body, "Wiimote")
        wii.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 6))
        for i, src in enumerate(wg.WIIMOTE_SOURCES, start=1):
            self.button_vars[src] = self.combo(wii, i, wg.SOURCES[src][1], wg.TARGETS, self.changed)
        row = len(wg.WIIMOTE_SOURCES) + 1
        ttk.Separator(wii).grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        self.idle_var = self.combo(wii, row + 1, "Apagar sin uso", IDLE_OPTIONS, self.global_changed)
        self.combo_labels = {k: label for k, (_, label) in wg.RECENTER_COMBOS.items()}
        self.recenter_var = self.combo(wii, row + 2, "Recentrar giroscopio", self.combo_labels, self.global_changed)
        self.rumble_var = tk.BooleanVar()
        ttk.Checkbutton(wii, text="Vibrar cuando el juego lo pida", variable=self.rumble_var,
                        style="Panel.TCheckbutton", command=self.global_changed).grid(
            row=row + 3, column=0, sticky="w", pady=(4, 0))
        ttk.Button(wii, text="Probar vibración", command=self.test_rumble).grid(
            row=row + 3, column=1, sticky="e", pady=(4, 0))
        ttk.Label(wii, text="Para todos los perfiles. Tras apagarse, reconecta con 1+2.", style="Muted.TLabel").grid(
            row=row + 4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        nun = self.panel(right, "Nunchuk")
        nun.pack(fill="x")
        for i, src in enumerate(wg.NUNCHUK_SOURCES, start=1):
            self.button_vars[src] = self.combo(nun, i, wg.SOURCES[src][1], wg.TARGETS, self.changed)
        self.stick_var = self.combo(nun, 3, "Stick", wg.STICK_TARGETS, self.changed)
        ttk.Label(nun, text="Zona muerta", style="Panel.TLabel").grid(row=4, column=0, sticky="w")
        self.deadzone = tk.DoubleVar()
        ttk.Scale(nun, from_=0, to=0.4, variable=self.deadzone, command=lambda _: self.changed(delay=True)).grid(
            row=4, column=1, sticky="ew", pady=4)

        tilt = self.panel(right, "Inclinación del Wiimote")
        tilt.pack(fill="x", pady=(12, 0))
        self.tilt_var = self.combo(tilt, 1, "Mover", wg.TILT_TARGETS, self.changed)
        ttk.Label(tilt, text="Sensibilidad", style="Panel.TLabel").grid(row=2, column=0, sticky="w")
        self.tilt_sens = tk.DoubleVar()
        ttk.Scale(tilt, from_=0.3, to=3.0, variable=self.tilt_sens, command=lambda _: self.changed(delay=True)).grid(
            row=2, column=1, sticky="ew", pady=4)
        self.invert_y = tk.BooleanVar()
        ttk.Checkbutton(tilt, text="Invertir eje vertical", variable=self.invert_y, style="Panel.TCheckbutton",
                        command=self.changed).grid(row=3, column=0, columnspan=2, sticky="w")

        gyro = self.panel(right, "Giroscopio (Motion Plus)")
        gyro.pack(fill="x", pady=(12, 0))
        self.gyro_var = self.combo(gyro, 1, "Mover", wg.GYRO_TARGETS, self.changed)
        self.gyro_mode_var = self.combo(gyro, 2, "Modo", wg.GYRO_MODES, self.changed)
        ttk.Label(gyro, text="Sensibilidad", style="Panel.TLabel").grid(row=3, column=0, sticky="w")
        self.gyro_sens = tk.DoubleVar()
        ttk.Scale(gyro, from_=0.3, to=3.0, variable=self.gyro_sens, command=lambda _: self.changed(delay=True)).grid(
            row=3, column=1, sticky="ew", pady=4)
        self.invert_gyro_y = tk.BooleanVar()
        ttk.Checkbutton(gyro, text="Invertir eje vertical", variable=self.invert_gyro_y, style="Panel.TCheckbutton",
                        command=self.changed).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Label(gyro, text="Se calibra solo al dejar el Wiimote quieto 1 s. En modo Apuntar, apunta al "
                  "frente y usa la combinación de recentrar.",
                  style="Muted.TLabel", wraplength=330).grid(row=5, column=0, columnspan=2, sticky="w")

        live = self.panel(body, "Mando Xbox virtual")
        live.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(12, 0))
        LiveView(live).grid(row=1, column=0, columnspan=2)

        bottom = ttk.Frame(self, padding=(14, 6, 14, 12))
        bottom.pack(fill="x")
        self.bridge_status = ttk.Label(bottom, text="", style="Status.TLabel")
        self.bridge_status.pack(side="left")
        self.status = ttk.Label(bottom, text="", style="Status.TLabel")
        self.status.pack(side="left", padx=(16, 0))
        self.bridge_btn = ttk.Button(bottom, text="Iniciar puente", command=self.toggle_bridge)
        self.bridge_btn.pack(side="right", padx=2)
        ttk.Button(bottom, text="Restaurar perfil", command=self.reset_profile).pack(side="right", padx=2)
        ttk.Button(bottom, text="Sonidos…", command=self.open_sounds).pack(side="right", padx=2)
        ttk.Button(bottom, text="Modo mouse…", command=self.open_mouse).pack(side="right", padx=2)

    # --- perfiles ---------------------------------------------------------------
    def profile(self):
        return self.cfg["profiles"][self.cfg["active"]]

    def load_profile(self):
        self.loading = True
        self.profile_cb.configure(values=list(self.cfg["profiles"]))
        self.profile_var.set(self.cfg["active"])
        p = self.profile()
        for src, var in self.button_vars.items():
            var.set(wg.TARGETS.get(p["buttons"].get(src, "none"), wg.TARGETS["none"]))
        self.stick_var.set(wg.STICK_TARGETS.get(p["nunchuk_stick"], wg.STICK_TARGETS["none"]))
        self.tilt_var.set(wg.TILT_TARGETS.get(p["tilt"], wg.TILT_TARGETS["none"]))
        self.deadzone.set(p["deadzone"])
        self.tilt_sens.set(p["tilt_sensitivity"])
        self.invert_y.set(p["invert_tilt_y"])
        self.gyro_var.set(wg.GYRO_TARGETS.get(p["gyro"], wg.GYRO_TARGETS["none"]))
        self.gyro_sens.set(p["gyro_sensitivity"])
        self.invert_gyro_y.set(p["invert_gyro_y"])
        self.gyro_mode_var.set(wg.GYRO_MODES.get(p["gyro_mode"], wg.GYRO_MODES["speed"]))
        idle = self.cfg["idle_timeout_min"]
        self.idle_var.set(IDLE_OPTIONS.get(idle, f"Tras {idle} min"))
        self.recenter_var.set(self.combo_labels.get(self.cfg["recenter_combo"], self.combo_labels["none"]))
        self.rumble_var.set(self.cfg["rumble"])
        self.loading = False

    def changed(self, delay=False):
        if self.loading:
            return
        p = self.profile()
        p["buttons"] = {s: self.targets_by_label[v.get()] for s, v in self.button_vars.items()}
        p["nunchuk_stick"] = {v: k for k, v in wg.STICK_TARGETS.items()}[self.stick_var.get()]
        p["tilt"] = {v: k for k, v in wg.TILT_TARGETS.items()}[self.tilt_var.get()]
        p["deadzone"] = round(self.deadzone.get(), 3)
        p["tilt_sensitivity"] = round(self.tilt_sens.get(), 2)
        p["invert_tilt_y"] = self.invert_y.get()
        p["gyro"] = {v: k for k, v in wg.GYRO_TARGETS.items()}[self.gyro_var.get()]
        p["gyro_sensitivity"] = round(self.gyro_sens.get(), 2)
        p["invert_gyro_y"] = self.invert_gyro_y.get()
        p["gyro_mode"] = {v: k for k, v in wg.GYRO_MODES.items()}[self.gyro_mode_var.get()]
        self.save_soon() if delay else self.save()

    def save_soon(self):
        """Los deslizadores guardan al soltar, no en cada píxel."""
        if getattr(self, "_pending", None):
            self.after_cancel(self._pending)
        self._pending = self.after(300, self.save)

    def global_changed(self):
        if self.loading:
            return
        idle = {v: k for k, v in IDLE_OPTIONS.items()}.get(self.idle_var.get())
        if idle is not None:  # un valor puesto con --idle que no está en la lista se conserva
            self.cfg["idle_timeout_min"] = idle
        self.cfg["recenter_combo"] = {v: k for k, v in self.combo_labels.items()}[self.recenter_var.get()]
        self.cfg["rumble"] = self.rumble_var.get()
        self.save()

    def open_sounds(self):
        if getattr(self, "sounds_win", None) and self.sounds_win.winfo_exists():
            self.sounds_win.lift()
            return
        win = self.sounds_win = tk.Toplevel(self)
        win.title("Sonidos")
        win.configure(bg=BG)
        f = self.panel(win, "Sonidos de aviso")
        f.pack(fill="both", expand=True, padx=12, pady=12)
        none = "— Ninguno —"
        files = wg.sound_files()
        sounds = self.cfg["sounds"]

        def row(r, event, label):
            ttk.Label(f, text=label, style="Panel.TLabel").grid(row=r, column=0, sticky="w", padx=(0, 10), pady=3)
            var = tk.StringVar(value=sounds.get(event) or none)
            cb = ttk.Combobox(f, textvariable=var, values=[none, *files], state="readonly", width=40)
            cb.grid(row=r, column=1, sticky="ew", pady=3)

            def changed(_):
                sounds[event] = "" if var.get() == none else var.get()
                self.save()
                wg.play_on_pc(sounds[event])

            cb.bind("<<ComboboxSelected>>", changed)
            ttk.Button(f, text="▶", width=3, command=lambda: wg.play_on_pc(sounds.get(event))).grid(
                row=r, column=2, padx=(6, 0))

        for r, (event, label) in enumerate(wg.SOUND_EVENTS.items(), start=1):
            row(r, event, label)
        r = len(wg.SOUND_EVENTS) + 1
        user_dir = wg.SOUND_DIRS[0]
        ttk.Label(f, text=f"Para añadir tus sonidos, cópialos a {user_dir}", style="Muted.TLabel",
                  wraplength=460).grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))

        def open_dir():
            user_dir.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["xdg-open", str(user_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        ttk.Button(f, text="Abrir carpeta", command=open_dir).grid(row=r + 1, column=0, sticky="w", pady=(6, 0))

    def open_mouse(self):
        if getattr(self, "mouse_win", None) and self.mouse_win.winfo_exists():
            self.mouse_win.lift()
            return
        win = self.mouse_win = tk.Toplevel(self)
        win.title("Modo mouse")
        win.configure(bg=BG)
        m = self.cfg["mouse"]
        combos = {k: label for k, (_, label) in wg.MOUSE_COMBOS.items()}
        actions = {k: label for k, (_, label) in wg.MOUSE_ACTIONS.items()}
        loading = False

        def key(options, var):
            return {v: k for k, v in options.items()}[var.get()]

        def changed(delay=False):
            if loading:
                return
            self.cfg["mouse_combo"] = key(combos, combo_var)
            m["move"] = key(wg.MOUSE_MOVES, move_var)
            m["speed"] = round(speed.get(), 2)
            m["invert_gyro_x"], m["invert_gyro_y"] = invert_x.get(), invert_y.get()
            m["auto_keyboard"] = auto_kb.get()
            m["buttons"] = {s: key(actions, v) for s, v in button_vars.items()}
            self.save_soon() if delay else self.save()

        left = self.panel(win, "Modo mouse")
        left.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=12)
        combo_var = self.combo(left, 1, "Activar / desactivar", combos, changed)
        move_var = self.combo(left, 2, "Mover el puntero con", wg.MOUSE_MOVES, changed)
        ttk.Label(left, text="Velocidad", style="Panel.TLabel").grid(row=3, column=0, sticky="w")
        speed = tk.DoubleVar()
        ttk.Scale(left, from_=0.3, to=3.0, variable=speed, command=lambda _: changed(delay=True)).grid(
            row=3, column=1, sticky="ew", pady=4)
        invert_x, invert_y = tk.BooleanVar(), tk.BooleanVar()
        ttk.Checkbutton(left, text="Invertir eje horizontal del giroscopio", variable=invert_x,
                        style="Panel.TCheckbutton", command=changed).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(left, text="Invertir eje vertical del giroscopio", variable=invert_y,
                        style="Panel.TCheckbutton", command=changed).grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Label(left, text="Presiona la combinación para pasar del mando de juego al mouse y viceversa "
                  "(vibra y suena al cambiar). Si falta el Nunchuk, o el Motion Plus con «Giroscopio», "
                  "la cruceta mueve el puntero. Con el giroscopio, deja el Wiimote quieto 1 s para "
                  "que se calibre.", style="Muted.TLabel", wraplength=330).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Separator(left).grid(row=7, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Label(left, text="Teclado en pantalla", style="Title.TLabel").grid(row=8, column=0, columnspan=2, sticky="w")
        auto_kb = tk.BooleanVar()
        ttk.Checkbutton(left, text="Mostrarlo al entrar a un campo de texto", variable=auto_kb,
                        style="Panel.TCheckbutton", command=changed).grid(row=9, column=0, columnspan=2, sticky="w")
        ttk.Label(left, text="También se abre con el botón que tenga «Teclado en pantalla». Con el teclado "
                  "visible: cruceta o stick eligen la tecla, A la escribe, B borra, C lo cierra, "
                  "Z mayúsculas, 1 espacio y 2 Enter.", style="Muted.TLabel", wraplength=330).grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(4, 0))

        right = self.panel(win, "Botones en modo mouse")
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)
        button_vars = {src: self.combo(right, i, wg.SOURCES[src][1], actions, changed)
                       for i, src in enumerate(wg.WIIMOTE_SOURCES + wg.NUNCHUK_SOURCES, start=1)}

        def load():
            nonlocal loading
            loading = True
            combo_var.set(combos.get(self.cfg["mouse_combo"], combos["none"]))
            move_var.set(wg.MOUSE_MOVES.get(m["move"], wg.MOUSE_MOVES["stick"]))
            speed.set(m["speed"])
            invert_x.set(m["invert_gyro_x"])
            invert_y.set(m["invert_gyro_y"])
            auto_kb.set(m["auto_keyboard"])
            for src, var in button_vars.items():
                var.set(actions.get(m["buttons"].get(src, "none"), actions["none"]))
            loading = False

        def restore():
            self.cfg["mouse_combo"] = wg.GLOBAL_OPTIONS["mouse_combo"]
            m.clear()
            m.update(copy.deepcopy(wg.MOUSE_DEFAULTS))
            load()
            self.save()

        ttk.Button(win, text="Restaurar modo mouse", command=restore).grid(
            row=1, column=1, sticky="e", padx=12, pady=(0, 12))
        load()

    def test_rumble(self):
        """Pide una vibración al mando virtual, igual que haría un juego."""
        dev = wg.find(wg.PAD_NAME)
        if dev is None:
            self.status.configure(text="El puente no está activo: enciende el Wiimote")
            return
        try:
            fx = ff.Effect(e.FF_RUMBLE, -1, 0, ff.Trigger(0, 0), ff.Replay(600, 0),
                           ff.EffectType(ff_rumble_effect=ff.Rumble(strong_magnitude=0xFFFF, weak_magnitude=0xFFFF)))
            effect_id = dev.upload_effect(fx)
            dev.write(e.EV_FF, effect_id, 1)
        except OSError as err:
            dev.close()
            self.status.configure(text=f"No se pudo pedir la vibración ({err})")
            return
        # el efecto pertenece a este descriptor: cerrarlo antes de tiempo lo cortaría
        self.after(800, dev.close)
        self.status.configure(text="Vibrando..." if self.cfg["rumble"] else
                              "Vibración desactivada: marca «Vibrar cuando el juego lo pida»")

    def save(self):
        self._pending = None
        wg.save_config(self.cfg)
        self.status.configure(text=f"Guardado y aplicado: {self.cfg['active']}")

    def select_profile(self):
        self.cfg["active"] = self.profile_var.get()
        self.load_profile()
        self.save()

    def ask_name(self, title, initial=""):
        name = simpledialog.askstring(title, "Nombre del perfil:", initialvalue=initial, parent=self)
        name = (name or "").strip()
        if name and name in self.cfg["profiles"]:
            messagebox.showerror("Perfil", "Ya existe un perfil con ese nombre.", parent=self)
            return None
        return name or None

    def new_profile(self):
        name = self.ask_name("Nuevo perfil", "Mi perfil")
        if name:
            self.cfg["profiles"][name] = wg.new_profile()
            self.cfg["active"] = name
            self.load_profile()
            self.save()
            self.status.configure(text=f"Creado «{name}» con el layout recomendado; cámbialo a tu gusto")

    def duplicate_profile(self):
        name = self.ask_name("Duplicar perfil", self.cfg["active"] + " (copia)")
        if name:
            self.cfg["profiles"][name] = copy.deepcopy(self.profile())
            self.cfg["active"] = name
            self.load_profile()
            self.save()

    def rename_profile(self):
        old = self.cfg["active"]
        name = self.ask_name("Renombrar perfil", old)
        if name:
            self.cfg["profiles"] = {(name if k == old else k): v for k, v in self.cfg["profiles"].items()}
            self.cfg["active"] = name
            self.load_profile()
            self.save()

    def delete_profile(self):
        if len(self.cfg["profiles"]) == 1:
            messagebox.showinfo("Perfil", "Debe quedar al menos un perfil.", parent=self)
            return
        name = self.cfg["active"]
        if messagebox.askyesno("Borrar perfil", f"¿Borrar el perfil «{name}»?", parent=self):
            del self.cfg["profiles"][name]
            self.cfg["active"] = next(iter(self.cfg["profiles"]))
            self.load_profile()
            self.save()

    def reset_profile(self):
        name = self.cfg["active"]
        self.cfg["profiles"][name] = wg.default_config()["profiles"].get(name) or wg.new_profile()
        self.load_profile()
        self.save()

    # --- puente -----------------------------------------------------------------
    def bridge_running(self):
        return wg.find(wg.PAD_NAME) is not None

    def refresh_bridge(self):
        self.update_bridge()
        self.after(1500, self.refresh_bridge)

    def update_bridge(self):
        running = self.bridge_running()
        auto = SERVICE.exists()  # instalado con install.sh --service
        self.bridge_btn.configure(text="Detener puente" if running and self.bridge else
                                  "Puente activo" if running else "Automático" if auto else "Iniciar puente",
                                  state="disabled" if (running or auto) and not self.bridge else "normal")
        self.bridge_status.configure(text="● Puente activo: los cambios se aplican al instante" if running else
                                     "○ Esperando al Wiimote: el puente arranca solo al encenderlo" if auto else
                                     "○ Puente detenido: inícialo para jugar")

    def toggle_bridge(self):
        if self.bridge and self.bridge.poll() is None:
            self.bridge.terminate()
            self.bridge = None
        else:
            script = Path(__file__).resolve().parent / "wii_gamepad.py"
            self.bridge = subprocess.Popen([sys.executable, str(script)], start_new_session=True,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.after(800, self.update_bridge)

    def destroy(self):
        if self.bridge and self.bridge.poll() is None and messagebox.askyesno(
                "Salir", "¿Detener también el puente? (si dices que no, seguirá funcionando)", parent=self):
            self.bridge.terminate()
        super().destroy()


if __name__ == "__main__":
    os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
    App().mainloop()
