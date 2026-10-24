#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Wiimote + Nunchuk -> mando virtual Xbox 360 (uinput).

Usa el driver del kernel (hid_wiimote) y expone un único mando XInput que
reconocen Steam/Proton, Rocket League, Roblox (Sober) y casi cualquier juego.

El mapeo se lee de ~/.config/wii-gamepad/config.json (perfiles por juego) y se
recarga solo al guardarlo. Para editarlo con ventana: wii-gamepad-config
"""
import argparse
import copy
import ctypes
import fcntl
import json
import math
import os
import select
import shutil
import subprocess
import time
from pathlib import Path

import evdev
from evdev import UInput, AbsInfo, ecodes as e, ff

import wii_keyboard

WIIMOTE = "Nintendo Wii Remote"
NUNCHUK = "Nintendo Wii Remote Nunchuk"
ACCEL = "Nintendo Wii Remote Accelerometer"
MOTIONPLUS = "Nintendo Wii Remote Motion Plus"
PAD_NAME = "Microsoft X-Box 360 pad"
MOUSE_NAME = "Wii Gamepad Mouse"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "wii-gamepad" / "config.json"

# Botones de origen: id -> (código evdev, etiqueta)
SOURCES = {
    "A": (e.BTN_A, "A"), "B": (e.BTN_B, "B (gatillo)"),
    "1": (e.BTN_1, "1"), "2": (e.BTN_2, "2"),
    "+": (e.KEY_NEXT, "+"), "-": (e.KEY_PREVIOUS, "−"), "Home": (e.BTN_MODE, "Home"),
    "Up": (e.KEY_UP, "Cruceta ↑"), "Down": (e.KEY_DOWN, "Cruceta ↓"),
    "Left": (e.KEY_LEFT, "Cruceta ←"), "Right": (e.KEY_RIGHT, "Cruceta →"),
    "C": (e.BTN_C, "C"), "Z": (e.BTN_Z, "Z"),
}
SOURCE_OF = {code: s for s, (code, _) in SOURCES.items()}
WIIMOTE_SOURCES = ["A", "B", "1", "2", "+", "-", "Home", "Up", "Down", "Left", "Right"]
NUNCHUK_SOURCES = ["C", "Z"]

# Destinos en el mando Xbox: id -> etiqueta
TARGETS = {
    "none": "— Nada —",
    "A": "A", "B": "B", "X": "X", "Y": "Y",
    "LB": "LB", "RB": "RB", "LT": "LT (gatillo izq.)", "RT": "RT (gatillo der.)",
    "Back": "Back / Select", "Start": "Start", "Guide": "Guide (Xbox)",
    "L3": "L3 (clic stick izq.)", "R3": "R3 (clic stick der.)",
    "DUp": "Cruceta ↑", "DDown": "Cruceta ↓", "DLeft": "Cruceta ←", "DRight": "Cruceta →",
    "LSUp": "Stick izq. ↑", "LSDown": "Stick izq. ↓", "LSLeft": "Stick izq. ←", "LSRight": "Stick izq. →",
    "RSUp": "Stick der. ↑", "RSDown": "Stick der. ↓", "RSLeft": "Stick der. ←", "RSRight": "Stick der. →",
}
STICK_TARGETS = {"none": "— Nada —", "left": "Stick izquierdo", "right": "Stick derecho", "dpad": "Cruceta"}
TILT_TARGETS = {"none": "— Nada —", "left": "Stick izquierdo", "right": "Stick derecho"}
GYRO_TARGETS = TILT_TARGETS
GYRO_MODES = {"speed": "Velocidad (girar mueve la cámara)", "aim": "Apuntar (estilo Wii, se recentra)"}
# Combinación que recentra el giroscopio: id -> (fuentes, etiqueta)
RECENTER_COMBOS = {"none": ((), "Desactivada"), "B+Down": (("B", "Down"), "B + Cruceta ↓"),
                   "Z+Down": (("Z", "Down"), "Z + Cruceta ↓"), "C+Down": (("C", "Down"), "C + Cruceta ↓")}
# Combinación que alterna entre mando de juego y mouse: id -> (fuentes, etiqueta)
MOUSE_COMBOS = {"none": ((), "Desactivada"), "+-": (("+", "-"), "+ y − a la vez"), "1+2": (("1", "2"), "1 y 2 a la vez")}
MOUSE_MOVES = {"stick": "Stick del Nunchuk", "gyro": "Giroscopio (apuntar)", "dpad": "Cruceta"}
# Acciones del modo mouse: id -> (teclas que se aprietan juntas, etiqueta)
MOUSE_ACTIONS = {
    "none": ((), "— Nada —"),
    "left": ((e.BTN_LEFT,), "Clic izquierdo"), "right": ((e.BTN_RIGHT,), "Clic derecho"),
    "middle": ((e.BTN_MIDDLE,), "Clic central"),
    "wheel_up": ((), "Rueda ↑ (scroll)"), "wheel_down": ((), "Rueda ↓ (scroll)"),
    "backspace": ((e.KEY_BACKSPACE,), "Borrar (Retroceso)"), "enter": ((e.KEY_ENTER,), "Enter"),
    "esc": ((e.KEY_ESC,), "Esc"), "tab": ((e.KEY_TAB,), "Tab"), "space": ((e.KEY_SPACE,), "Espacio"),
    "super": ((e.KEY_LEFTMETA,), "Super (menú)"),
    "nav_back": ((e.KEY_LEFTALT, e.KEY_LEFT), "Atrás (Alt+←)"),
    "nav_fwd": ((e.KEY_LEFTALT, e.KEY_RIGHT), "Adelante (Alt+→)"),
    "alt_tab": ((e.KEY_LEFTALT, e.KEY_TAB), "Cambiar ventana (Alt+Tab)"),
    "close": ((e.KEY_LEFTALT, e.KEY_F4), "Cerrar ventana (Alt+F4)"),
    "up": ((e.KEY_UP,), "Flecha ↑"), "down": ((e.KEY_DOWN,), "Flecha ↓"),
    "left_key": ((e.KEY_LEFT,), "Flecha ←"), "right_key": ((e.KEY_RIGHT,), "Flecha →"),
    "vol_up": ((e.KEY_VOLUMEUP,), "Subir volumen"), "vol_down": ((e.KEY_VOLUMEDOWN,), "Bajar volumen"),
    "mute": ((e.KEY_MUTE,), "Silenciar"), "play": ((e.KEY_PLAYPAUSE,), "Reproducir / pausa"),
    "keyboard": ((), "Teclado en pantalla"),
}
WHEEL_ACTIONS = {"wheel_up": 1, "wheel_down": -1}
# + y − quedan en "Nada": son la combinación para salir y, si no, cada cambio de modo tocaría el volumen.
MOUSE_DEFAULTS = {"move": "stick", "speed": 1.0, "invert_gyro_x": False, "invert_gyro_y": False,
                  "auto_keyboard": True,  # mostrar el teclado en pantalla al entrar a un campo de texto
                  "buttons": {"A": "left", "B": "backspace", "Z": "right", "C": "enter",
                              "Up": "wheel_up", "Down": "wheel_down", "Left": "nav_back", "Right": "nav_fwd",
                              "1": "esc", "2": "keyboard", "Home": "super", "+": "none", "-": "none"}}
# Motion Plus: eje evdev -> (eje del stick, invertido). Medido con un RVL-CNT-01-TR:
# RY = giro izq./der. (derecha da negativo), RZ = punta arriba/abajo (arriba da
# positivo; en Xbox arriba es negativo). RX (alabeo) no se usa.
GYRO_AXES = {e.ABS_RY: (0, True), e.ABS_RZ: (1, True)}

KEY_TARGETS = {"A": e.BTN_A, "B": e.BTN_B, "X": e.BTN_X, "Y": e.BTN_Y, "LB": e.BTN_TL, "RB": e.BTN_TR,
               "Back": e.BTN_SELECT, "Start": e.BTN_START, "Guide": e.BTN_MODE,
               "L3": e.BTN_THUMBL, "R3": e.BTN_THUMBR}
TRIGGER_TARGETS = {"LT": e.ABS_Z, "RT": e.ABS_RZ}
# eje -> (destino negativo, destino positivo); en Xbox Y negativo = arriba
DIGITAL_AXES = {e.ABS_HAT0X: ("DLeft", "DRight"), e.ABS_HAT0Y: ("DUp", "DDown"),
                e.ABS_X: ("LSLeft", "LSRight"), e.ABS_Y: ("LSUp", "LSDown"),
                e.ABS_RX: ("RSLeft", "RSRight"), e.ABS_RY: ("RSUp", "RSDown")}
AXIS_OF = {t: (code, sign) for code, pair in DIGITAL_AXES.items() for sign, t in zip((-1, 1), pair)}
STICK_AXES = {"left": (e.ABS_X, e.ABS_Y), "right": (e.ABS_RX, e.ABS_RY)}

XBOX_KEYS = list(KEY_TARGETS.values())
STICK = AbsInfo(0, -32768, 32767, 16, 128, 0)
TRIG = AbsInfo(0, 0, 255, 0, 0, 0)
HAT = AbsInfo(0, -1, 1, 0, 0, 0)
XBOX_ABS = [(e.ABS_X, STICK), (e.ABS_Y, STICK), (e.ABS_RX, STICK), (e.ABS_RY, STICK),
            (e.ABS_Z, TRIG), (e.ABS_RZ, TRIG), (e.ABS_HAT0X, HAT), (e.ABS_HAT0Y, HAT)]

_BASE = {"A": "A", "B": "B", "1": "L3", "2": "R3", "+": "Start", "-": "Back", "Home": "Guide",
         "Up": "DUp", "Down": "DDown", "Left": "DLeft", "Right": "DRight", "C": "LT", "Z": "RT"}
# Plantilla de los perfiles nuevos. Cada dedo hace lo que haría en un mando Xbox:
# índices (Z/C izq., B der.) = gatillos, pulgar derecho = A + cruceta como X/Y/B/RB,
# 1 y 2 (lejos de la mano) = Back y Start para pausar, − y + = clic de los sticks.
RECOMMENDED = "Recomendado (Nunchuk)"
STEAM_INPUT = "Steam Input (1:1)"
RECOMMENDED_PROFILE = {
    "buttons": {"A": "A", "B": "RT", "Z": "LT", "C": "LB",
                "Up": "Y", "Left": "X", "Right": "B", "Down": "RB",
                "-": "L3", "+": "R3", "Home": "Guide", "1": "Back", "2": "Start"},
    "nunchuk_stick": "left", "tilt": "none", "gyro": "right",
}
DEFAULT_PROFILES = {
    RECOMMENDED: RECOMMENDED_PROFILE,
    "Rocket League": {
        "buttons": _BASE | {"Up": "Y", "Down": "X", "Left": "LB", "Right": "RB"},
        "nunchuk_stick": "left", "tilt": "none",
    },
    "Geometry Dash": {
        "buttons": _BASE | {"B": "A", "Z": "A", "C": "B", "1": "B", "2": "A"},
        "nunchuk_stick": "left", "tilt": "none",
    },
    "Roblox": {
        "buttons": _BASE | {"B": "RT", "Z": "LT", "C": "B", "1": "X", "2": "Y"},
        "nunchuk_stick": "left", "tilt": "none", "gyro": "right",
    },
    "Wiimote de lado (sin Nunchuk)": {
        "buttons": _BASE | {"Up": "LSLeft", "Down": "LSRight", "Left": "LSDown", "Right": "LSUp",
                            "1": "B", "2": "A", "A": "Y", "B": "X", "C": "none", "Z": "none"},
        "nunchuk_stick": "none", "tilt": "none",
    },
    # Para remapear en Steam Input: cada botón del Wiimote es un botón distinto del mando
    # virtual y este perfil no se toca, así los layouts de Steam siempre reciben lo mismo.
    STEAM_INPUT: {
        "buttons": {"A": "A", "B": "RT", "Z": "LT", "C": "LB", "1": "B", "2": "X",
                    "-": "Back", "+": "Start", "Home": "Guide",
                    "Up": "DUp", "Down": "DDown", "Left": "DLeft", "Right": "DRight"},
        "nunchuk_stick": "left", "tilt": "none", "gyro": "right",
    },
}
PROFILE_OPTIONS = {"deadzone": 0.10, "tilt_sensitivity": 1.0, "invert_tilt_y": False,
                   "gyro_sensitivity": 1.0, "invert_gyro_y": False, "gyro_mode": "speed"}
# Sonidos de aviso en la PC: evento -> etiqueta.
SOUND_EVENTS = {"connect": "Wiimote conectado", "disconnect": "Wiimote apagado o desconectado",
                "recenter": "Giroscopio recentrado", "nunchuk": "Nunchuk conectado / desconectado",
                "mouse": "Modo mouse activado / desactivado"}
SOUND_DIRS = [CONFIG.parent / "sonidos", Path(__file__).resolve().parent / "sonidos"]  # los del usuario primero
SOUND_EXTS = {".wav", ".mp3", ".ogg", ".oga", ".flac", ".opus"}
GLOBAL_OPTIONS = {"idle_timeout_min": 10, "recenter_combo": "B+Down", "rumble": True,
                  "mouse_combo": "+-", "mouse": MOUSE_DEFAULTS,
                  "sounds": {"connect": "maximize_006.ogg", "disconnect": "minimize_006.ogg",
                             "recenter": "confirmation_001.ogg", "nunchuk": "toggle_001.ogg",
                             "mouse": "switch_003.ogg"}}
IDLE_TIMEOUT_MIN = GLOBAL_OPTIONS["idle_timeout_min"]  # minutos sin uso antes de apagar (0 = nunca)
MOTION_THRESHOLD = 25  # cambio del acelerómetro (~0.25 g) que cuenta como "en uso"
ACTIVITY = 0.15  # un stick analógico cuenta como "en uso" (para el apagado) pasado este valor
ONCE_WAIT = 15  # con --once, segundos que espera al Wiimote antes de rendirse


def new_profile():
    return copy.deepcopy(RECOMMENDED_PROFILE) | PROFILE_OPTIONS


def default_config():
    profiles = {n: p | PROFILE_OPTIONS for n, p in copy.deepcopy(DEFAULT_PROFILES).items()}
    return ({"active": RECOMMENDED} | copy.deepcopy(GLOBAL_OPTIONS)
            | {"profiles": profiles, "default_profiles_seen": list(DEFAULT_PROFILES)})


def load_config(path=CONFIG):
    try:
        cfg = json.loads(path.read_text())
        if not cfg.get("profiles"):
            raise ValueError
    except FileNotFoundError:
        cfg = default_config()
        save_config(cfg, path)
    except (ValueError, json.JSONDecodeError):
        print(f"Config inválida en {path}; usando valores por defecto")
        cfg = default_config()
    if cfg.get("active") not in cfg["profiles"]:
        cfg["active"] = next(iter(cfg["profiles"]))
    for k, v in GLOBAL_OPTIONS.items():
        cfg.setdefault(k, copy.deepcopy(v))
    for k, v in GLOBAL_OPTIONS["sounds"].items():
        # Si el archivo ya no existe (p. ej. un sonido de versiones anteriores), vuelve el de por defecto.
        if cfg["sounds"].setdefault(k, v) and not sound_path(cfg["sounds"][k]):
            cfg["sounds"][k] = v
    for k, v in MOUSE_DEFAULTS.items():
        cfg["mouse"].setdefault(k, copy.deepcopy(v))
    for k, v in MOUSE_DEFAULTS["buttons"].items():
        cfg["mouse"]["buttons"].setdefault(k, v)
    # Perfiles por defecto de versiones nuevas: se añaden una sola vez (si los borras, no vuelven).
    # Las configs anteriores al perfil de Steam ya conocían los demás.
    seen = cfg.setdefault("default_profiles_seen", [n for n in DEFAULT_PROFILES if n != STEAM_INPUT])
    added = [n for n in DEFAULT_PROFILES if n not in seen]
    for name in added:
        cfg["profiles"].setdefault(name, copy.deepcopy(DEFAULT_PROFILES[name]) | PROFILE_OPTIONS)
        seen.append(name)
    for p in cfg["profiles"].values():
        for k, v in PROFILE_OPTIONS.items():
            p.setdefault(k, v)
        p.setdefault("buttons", {})
        p.setdefault("nunchuk_stick", "left")
        p.setdefault("tilt", "none")
        p.setdefault("gyro", "none")
    if added:
        save_config(cfg, path)
    return cfg


def save_config(cfg, path=CONFIG):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    tmp.replace(path)


class Axis:
    """Normaliza un eje del Nunchuk/acelerómetro a -1..1 con zona muerta
    y autocalibración del rango."""

    def __init__(self, center, span, deadzone, invert=False):
        self.center, self.span, self.deadzone, self.invert = center, span, deadzone, invert

    def convert(self, raw):
        v = raw - self.center
        self.span = max(self.span, abs(v))
        n = v / self.span
        if abs(n) < self.deadzone:
            return 0.0
        n = (abs(n) - self.deadzone) / (1 - self.deadzone) * (1 if n > 0 else -1)
        return max(-1.0, min(1.0, -n if self.invert else n))


class Gyro:
    """Un eje del Motion Plus -> -1..1. Cada giroscopio tiene su propio valor
    en reposo y deriva un poco, así que se recalibra solo cada vez que el
    Wiimote se queda quieto un momento.

    Modo "speed": la velocidad de giro mueve el stick (dejas de girar, la
    cámara se para). Modo "aim": se suma el giro para saber hacia dónde apunta
    el Wiimote respecto al centro, y apuntar a un lado empuja el stick hacia
    ese lado; recenter() toma la dirección actual como el nuevo frente."""

    WINDOW = 40      # muestras (~1 s a 40 reportes/s)
    SMOOTH = 5       # mediana de las últimas lecturas: el eje de giro tiene ráfagas de ruido de hasta ±2500 en reposo
    STILL = 250      # variación máxima en la ventana para considerarlo quieto (en la mano ≈ ±200)
    MAX_DRIFT = 400  # una recalibración no mueve el reposo más que esto (evita "aprender" un giro lento)
    DEADZONE = 1200  # con la mediana, el ruido en reposo queda por debajo (medido); ≈ 6.5 °/s
    FULL = 8000      # giro que lleva el stick al tope con sensibilidad 1 (un giro rápido llega a ~70000)
    UNITS_PER_DPS = 185  # medido contra el acelerómetro: 174 girando lento, 197 rápido
    AIM_NOISE = 800  # en modo apuntar, por debajo de esto no se suma (si no, deriva)
    AIM_DEADZONE = 3  # grados
    AIM_RANGE = 20   # grados hacia un lado para llevar el stick al tope con sensibilidad 1

    def __init__(self, sensitivity, invert=False, aim=False, bias=None):
        self.sensitivity = max(0.1, sensitivity)
        self.invert, self.aim, self.bias = invert, aim, bias
        self.window, self.recent = [], []
        self.angle = 0.0  # grados respecto al centro (modo apuntar)
        self.last_t = None

    def calibrate(self, strict=True):
        w = self.window
        if len(w) < self.WINDOW // 2 or max(w) - min(w) >= (self.STILL if strict else self.STILL * 4):
            return False
        mean = sum(w) / len(w)
        if strict and self.bias is not None and abs(mean - self.bias) >= self.MAX_DRIFT:
            return False
        self.bias = mean
        return True

    def recenter(self):
        """La dirección actual pasa a ser el frente; si el Wiimote está más o
        menos quieto, de paso recalibra el reposo."""
        self.angle = 0.0
        self.calibrate(strict=False)

    def _sample(self, raw, t):
        """Suaviza y recalibra; devuelve (giro respecto al reposo, o None si aún
        no está calibrado, y segundos desde la lectura anterior)."""
        self.recent = (self.recent + [raw])[-self.SMOOTH:]
        raw = sorted(self.recent)[len(self.recent) // 2]
        self.window = (self.window + [raw])[-self.WINDOW:]
        if len(self.window) == self.WINDOW:
            self.calibrate()
        dt = t - self.last_t if self.last_t is not None else 0.0
        self.last_t = t
        return (None if self.bias is None else raw - self.bias), dt

    def rate(self, raw, t):
        """Para el modo mouse: (grados/s, 0 dentro de la zona muerta; segundos desde la lectura anterior)."""
        v, dt = self._sample(raw, t)
        if v is None or abs(v) < self.DEADZONE or not 0 < dt < 0.2:
            return 0.0, 0.0
        return (abs(v) - self.DEADZONE) / self.UNITS_PER_DPS * (1 if v > 0 else -1), dt

    def convert(self, raw, t):
        v, dt = self._sample(raw, t)
        if v is None:  # aún sin calibrar: no mover la cámara
            return 0.0
        if self.aim:
            if abs(v) > self.AIM_NOISE and 0 < dt < 0.2:
                self.angle = max(-180.0, min(180.0, self.angle + v / self.UNITS_PER_DPS * dt))
            dead, full = self.AIM_DEADZONE, self.AIM_RANGE / self.sensitivity
            if abs(self.angle) < dead:
                return 0.0
            n = (abs(self.angle) - dead) / full * (1 if self.angle > 0 else -1)
        else:
            if abs(v) < self.DEADZONE:
                return 0.0
            n = (abs(v) - self.DEADZONE) / (self.FULL / self.sensitivity) * (1 if v > 0 else -1)
        return max(-1.0, min(1.0, -n if self.invert else n))


# ioctl de uinput para atender vibraciones. Se llaman a mano porque
# UInput.begin_upload/begin_erase de python-evdev no rellenan request_id.
def _iowr(nr, size, write_only=False):
    return ((1 if write_only else 3) << 30) | (size << 16) | (ord("U") << 8) | nr


UI_BEGIN_FF_UPLOAD = _iowr(200, ctypes.sizeof(ff.UInputUpload))
UI_END_FF_UPLOAD = _iowr(201, ctypes.sizeof(ff.UInputUpload), write_only=True)
UI_BEGIN_FF_ERASE = _iowr(202, ctypes.sizeof(ff.UInputErase))
UI_END_FF_ERASE = _iowr(203, ctypes.sizeof(ff.UInputErase), write_only=True)


class Rumble:
    """Motor del Wiimote. Solo se enciende o se apaga, así que las
    vibraciones suaves se imitan encendiéndolo a ratos."""

    PERIOD = 0.12  # s
    FULL_LEVEL = 0.75  # desde aquí, encendido continuo
    MIN_LEVEL = 0.1    # por debajo, apagado

    def __init__(self, dev):
        self.dev, self.on = dev, False
        fx = ff.Effect(e.FF_RUMBLE, -1, 0, ff.Trigger(0, 0), ff.Replay(0, 0),
                       ff.EffectType(ff_rumble_effect=ff.Rumble(strong_magnitude=0xFFFF, weak_magnitude=0)))
        self.id = dev.upload_effect(fx)
        self.pulse_until = 0.0

    def pulse(self, seconds):
        self.pulse_until = time.monotonic() + seconds

    def update(self, level, now):
        """Aplica el nivel (0..1) y devuelve en cuántos segundos hay que volver a llamar."""
        if now < self.pulse_until:
            level = 1.0
        wait = 1.0
        if level < self.MIN_LEVEL:
            want = False
        elif level >= self.FULL_LEVEL:
            want = True
        else:
            phase, on_time = now % self.PERIOD, level * self.PERIOD
            want = phase < on_time
            wait = (on_time - phase) if want else (self.PERIOD - phase)
        if now < self.pulse_until:
            wait = min(wait, self.pulse_until - now)
        if want != self.on:
            self.dev.write(e.EV_FF, self.id, int(want))
            self.on = want
        return max(0.005, wait)


class Pad:
    """Mando virtual. Varias fuentes pueden apuntar al mismo destino: un
    botón sigue presionado mientras alguna de sus fuentes lo esté."""

    def __init__(self):
        self.ui = UInput({e.EV_KEY: XBOX_KEYS, e.EV_ABS: XBOX_ABS, e.EV_FF: [e.FF_RUMBLE]}, name=PAD_NAME,
                         vendor=0x045E, product=0x028E, version=0x110, bustype=e.BUS_USB, max_effects=16)
        self.name = PAD_NAME
        self.held = {}    # destino -> fuentes que lo mantienen
        self.analog = {}  # eje -> valor analógico (-1..1)
        self.dirty = False
        self.effects = {}  # vibraciones subidas por los juegos: id -> (nivel 0..1, duración s; 0 = sin fin)
        self.playing = {}  # id -> (nivel, hasta cuándo)

    def handle_ff(self):
        """Atiende al juego: subir, borrar y reproducir vibraciones."""
        for ev in self.ui.read():
            if ev.type == e.EV_UINPUT and ev.code == e.UI_FF_UPLOAD:
                up = ff.UInputUpload(request_id=ev.value)
                fcntl.ioctl(self.ui.fd, UI_BEGIN_FF_UPLOAD, up)
                fx = up.effect
                r = fx.u.ff_rumble_effect
                self.effects[fx.id] = (max(r.strong_magnitude, r.weak_magnitude) / 0xFFFF, fx.ff_replay.length / 1000)
                up.retval = 0
                fcntl.ioctl(self.ui.fd, UI_END_FF_UPLOAD, up)
            elif ev.type == e.EV_UINPUT and ev.code == e.UI_FF_ERASE:
                er = ff.UInputErase(request_id=ev.value)
                fcntl.ioctl(self.ui.fd, UI_BEGIN_FF_ERASE, er)
                self.effects.pop(er.effect_id, None)
                self.playing.pop(er.effect_id, None)
                er.retval = 0
                fcntl.ioctl(self.ui.fd, UI_END_FF_ERASE, er)
            elif ev.type == e.EV_FF and ev.code in self.effects:
                level, length = self.effects[ev.code]
                if ev.value:
                    self.playing[ev.code] = (level, time.monotonic() + length if length else math.inf)
                else:
                    self.playing.pop(ev.code, None)

    def rumble_level(self, now):
        """Nivel de vibración pedido ahora y en cuántos segundos termina el próximo efecto."""
        self.playing = {i: p for i, p in self.playing.items() if p[1] > now}
        level = max((lv for lv, _ in self.playing.values()), default=0.0)
        return level, min((until - now for _, until in self.playing.values()), default=math.inf)

    def _write(self, etype, code, value):
        self.ui.write(etype, code, value)
        self.dirty = True

    def _axis(self, code):
        neg, pos = DIGITAL_AXES[code]
        digital = bool(self.held.get(pos)) - bool(self.held.get(neg))
        value = digital or self.analog.get(code, 0.0)
        self._write(e.EV_ABS, code, round(value) if code in (e.ABS_HAT0X, e.ABS_HAT0Y) else int(value * 32767))

    def press(self, target, source, on):
        if target == "none":
            return
        srcs = self.held.setdefault(target, set())
        srcs.add(source) if on else srcs.discard(source)
        if target in KEY_TARGETS:
            self._write(e.EV_KEY, KEY_TARGETS[target], int(bool(srcs)))
        elif target in TRIGGER_TARGETS:
            self._write(e.EV_ABS, TRIGGER_TARGETS[target], 255 if srcs else 0)
        elif target in AXIS_OF:
            self._axis(AXIS_OF[target][0])

    def move(self, code, value):
        self.analog[code] = value
        self._axis(code)

    def reset(self):
        for target, srcs in list(self.held.items()):
            for s in list(srcs):
                self.press(target, s, False)
        for code in list(self.analog):
            self.move(code, 0.0)
        self.syn()

    def syn(self):
        if self.dirty:
            self.ui.syn()
            self.dirty = False

    def close(self):
        self.ui.close()


class Mouse:
    """Mouse y teclado virtuales del modo mouse. Como en Pad, una acción sigue
    apretada mientras alguna de sus fuentes lo esté."""

    MAX_SPEED = 1400          # px/s con el stick al tope y velocidad 1
    DPAD_SPEED = (250, 1000)  # px/s de la cruceta al empezar y tras mantenerla 1 s
    PX_PER_DEGREE = 30        # giroscopio: girar el Wiimote 1° mueve el puntero esto con velocidad 1
    WHEEL_REPEAT = (0.4, 0.08)  # s hasta que la rueda se repite al mantener el botón, y entre repeticiones

    def __init__(self):
        # Todas las teclas: el teclado en pantalla usa las que pida la distribución del usuario.
        keys = [*range(e.KEY_ESC, e.KEY_MICMUTE + 1), e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE]
        self.ui = UInput({e.EV_KEY: keys, e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL]}, name=MOUSE_NAME)
        self.held = {}        # acción -> fuentes que la mantienen
        self.wheel_next = {}  # acción de rueda mantenida -> cuándo repetirla
        self.velocity = (0.0, 0.0)  # px/s del stick o la cruceta
        self.frac = [0.0, 0.0]      # fracciones de píxel pendientes
        self.last_t = None

    def press(self, action, source, on):
        if action not in MOUSE_ACTIONS or action == "none":
            return
        srcs = self.held.setdefault(action, set())
        was = bool(srcs)
        srcs.add(source) if on else srcs.discard(source)
        if bool(srcs) == was:
            return
        if action in WHEEL_ACTIONS:
            if srcs:
                self.scroll(action)
                self.wheel_next[action] = time.monotonic() + self.WHEEL_REPEAT[0]
            else:
                self.wheel_next.pop(action, None)
            return
        codes = MOUSE_ACTIONS[action][0]
        for code in codes if srcs else reversed(codes):  # Alt antes que la tecla, y se suelta después
            self.ui.write(e.EV_KEY, code, int(bool(srcs)))
            self.ui.syn()

    def tap(self, strokes):
        """Escribe [(tecla, modificadores), ...] tecla a tecla, como alguien tecleando."""
        for code, mods in strokes:
            for m in mods:
                self.ui.write(e.EV_KEY, m, 1)
            self.ui.syn()
            self.ui.write(e.EV_KEY, code, 1)
            self.ui.syn()
            self.ui.write(e.EV_KEY, code, 0)
            for m in reversed(mods):
                self.ui.write(e.EV_KEY, m, 0)
            self.ui.syn()

    def scroll(self, action):
        self.ui.write(e.EV_REL, e.REL_WHEEL, WHEEL_ACTIONS[action])
        self.ui.syn()

    def nudge(self, dx, dy):
        """Mueve el puntero; guarda las fracciones de píxel para que los movimientos lentos no se pierdan."""
        moved = False
        for i, (code, d) in enumerate(((e.REL_X, dx), (e.REL_Y, dy))):
            self.frac[i] += d
            whole = int(self.frac[i])
            if whole:
                self.frac[i] -= whole
                self.ui.write(e.EV_REL, code, whole)
                moved = True
        if moved:
            self.ui.syn()

    def tick(self, now):
        """Mueve el puntero según la velocidad y repite la rueda; devuelve en cuántos segundos volver a llamar."""
        dt = min(now - self.last_t, 0.05) if self.last_t is not None else 0.0
        self.last_t = now
        wait = math.inf
        if any(self.velocity):
            self.nudge(self.velocity[0] * dt, self.velocity[1] * dt)
            wait = 1 / 120
        for action, t in self.wheel_next.items():
            if now >= t:
                self.scroll(action)
                t = self.wheel_next[action] = now + self.WHEEL_REPEAT[1]
            wait = min(wait, t - now)
        return max(0.005, wait)

    def busy(self):
        return any(self.held.values()) or any(self.velocity)

    def reset(self):
        for action, srcs in list(self.held.items()):
            for s in list(srcs):
                self.press(action, s, False)
        self.velocity, self.frac = (0.0, 0.0), [0.0, 0.0]

    def close(self):
        self.ui.close()


warned = set()


def sysfs(dev_path, attr):
    """Atributo de /sys/class/input/eventN/device/..., o None."""
    try:
        return (Path("/sys/class/input") / Path(dev_path).name / "device" / attr).read_text()
    except OSError:
        return None


def find(name):
    for path in evdev.list_devices():
        # Mirar el nombre en sysfs evita abrir cada dispositivo (abrir el
        # acelerómetro lo enciende y gasta batería del Wiimote).
        sys_name = sysfs(path, "name")
        if sys_name is not None and sys_name.strip() != name:
            continue
        try:
            d = evdev.InputDevice(path)
        except PermissionError:
            if path not in warned:
                warned.add(path)
                print(f"Sin permiso para leer {path}; ejecuta install.sh o reinicia sesión")
            continue
        except OSError:
            continue
        if d.name == name:
            return d
        d.close()
    return None


def sound_files():
    """Nombres de los sonidos disponibles (carpeta del usuario + los incluidos)."""
    names = {f.name for d in SOUND_DIRS if d.is_dir() for f in d.iterdir() if f.suffix.lower() in SOUND_EXTS}
    return sorted(names, key=str.lower)


def sound_path(name):
    return next((d / name for d in SOUND_DIRS if name and (d / name).is_file()), None)


def play_on_pc(name, wait=False):
    path = sound_path(name)
    player = shutil.which("pw-play") or shutil.which("paplay")
    if not path or not player:
        return
    try:
        proc = subprocess.Popen([player, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if wait:  # al salir, systemd mataría el reproductor a medio sonido
            proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def power_off(dev):
    """Corta el Bluetooth del Wiimote, que al perder la conexión se apaga."""
    uevent = sysfs(dev.path, "device/uevent") or ""  # dispositivo HID padre
    mac = next((l.split("=", 1)[1] for l in uevent.splitlines() if l.startswith("HID_UNIQ=")), "")
    if not mac:
        return False
    try:
        return subprocess.run(["bluetoothctl", "disconnect", mac], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Bridge:
    def __init__(self, pad, mouse, config_path):
        self.pad, self.mouse, self.config_path = pad, mouse, config_path
        self.mtime = None
        self.rumble = None
        self.down, self.suppressed = set(), set()  # botones físicos apretados / tragados por la combinación
        self.extras = {}  # accesorios abiertos: nombre -> dispositivo
        self.mouse_mode = False
        self.mouse_stick = [0.0, 0.0]  # stick del Nunchuk en modo mouse
        self.dpad_since = None  # desde cuándo la cruceta mueve el puntero (para acelerarlo)
        self.rescan = False  # buscar accesorios ya (p. ej. abrir el Motion Plus al entrar al modo mouse)
        self.kb = wii_keyboard.Keyboard(mouse.tap)
        self.reload()

    def reload(self):
        cfg = load_config(self.config_path)
        self.mtime = self.config_path.stat().st_mtime
        self.pad.reset()
        self.mouse.reset()
        self.idle_timeout = cfg["idle_timeout_min"] * 60
        self.combo = RECENTER_COMBOS.get(cfg["recenter_combo"], RECENTER_COMBOS["none"])[0]
        self.mouse_combo = MOUSE_COMBOS.get(cfg["mouse_combo"], MOUSE_COMBOS["none"])[0]
        self.mouse_cfg = cfg["mouse"]
        if self.mouse_mode and not self.mouse_combo:  # sin combinación no habría forma de salir
            self.mouse_mode = False
            self.kb.hide()
        if self.mouse_mode and self.mouse_cfg["auto_keyboard"]:
            self.kb.start_watch()
        else:
            self.kb.stop_watch()
        self.rumble_on = cfg["rumble"]
        self.sounds = cfg["sounds"]
        self.profile = cfg["profiles"][cfg["active"]]
        self.buttons = {SOURCES[s][0]: (s, t) for s, t in self.profile["buttons"].items() if s in SOURCES}
        self.make_axes()
        print(f"Perfil activo: {cfg['active']}")

    def make_axes(self):
        p = self.profile
        cx, cy = getattr(self, "nunchuk_center", (0, 0))
        self.nunchuk_axes = (Axis(cx, 95, p["deadzone"]), Axis(cy, 95, p["deadzone"], invert=True))
        span = 100 / max(0.1, p["tilt_sensitivity"])
        self.tilt_axes = (Axis(0, span, 0.15, invert=True), Axis(0, span, 0.15, invert=p["invert_tilt_y"]))
        old = getattr(self, "gyro_axes", {})  # conservar la calibración al recargar
        self.gyro_axes = {code: Gyro(p["gyro_sensitivity"], inv ^ (i == 1 and p["invert_gyro_y"]),
                                     aim=p["gyro_mode"] == "aim", bias=old[code].bias if code in old else None)
                          for code, (i, inv) in GYRO_AXES.items()}

    def config_changed(self):
        try:
            return self.config_path.stat().st_mtime != self.mtime
        except FileNotFoundError:
            return False

    def handle_button(self, code, value):
        src = SOURCE_OF.get(code)
        if src is None:
            return
        self.down.add(src) if value else self.down.discard(src)
        if src in self.suppressed:  # forma parte de la combinación: no llega al juego hasta soltarlo
            if not value:
                self.suppressed.discard(src)
            return
        if value == 1:
            for combo, action in ((self.mouse_combo, self.toggle_mouse), (self.combo, self.recenter)):
                if combo and self.down.issuperset(combo):
                    for s in combo:  # soltar lo que ya se había apretado
                        self.pad.press(self.profile["buttons"].get(s, "none"), s, False)
                        self.mouse.press(self.mouse_cfg["buttons"].get(s, "none"), s, False)
                    self.suppressed.update(combo)
                    action()
                    return
        if self.mouse_mode:
            action = self.mouse_cfg["buttons"].get(src, "none")
            if self.kb.visible and self.kb.button(src, bool(value), time.monotonic()):
                return
            if action == "keyboard":
                if value == 1:
                    self.kb.hide() if self.kb.visible else self.show_keyboard()
            elif not (src in ("Up", "Down", "Left", "Right") and self.dpad_moves()):
                self.mouse.press(action, src, bool(value))
        elif code in self.buttons:
            src, target = self.buttons[code]
            self.pad.press(target, src, bool(value))

    def toggle_mouse(self):
        self.mouse_mode = not self.mouse_mode
        self.pad.reset()
        self.mouse.reset()
        self.mouse_stick = [0.0, 0.0]
        self.rescan = True
        self.kb.hide()
        if self.mouse_mode and self.mouse_cfg["auto_keyboard"]:
            self.kb.start_watch()
        else:
            self.kb.stop_watch()
        if self.rumble and self.rumble_on:
            self.rumble.pulse(0.15)
        self.notify("mouse")
        print("Modo mouse" if self.mouse_mode else "Modo mando de juego")

    def show_keyboard(self, auto=False):
        self.mouse.reset()  # p. ej. el clic con A que entró al campo de texto sigue apretado
        self.kb.show(auto)

    def focus_changed(self):
        """Aviso del vigilante de foco: mostrar el teclado al entrar a un campo de texto, quitarlo al salir."""
        text = self.kb.read_focus()
        if not self.mouse_mode or text is None:
            return
        if text and not self.kb.visible:
            self.show_keyboard(auto=True)
        elif not text and self.kb.auto:
            self.kb.hide()

    def dpad_moves(self):
        """En modo mouse, la cruceta mueve el puntero si se eligió así o si falta el
        Nunchuk / Motion Plus elegido para moverlo."""
        move = self.mouse_cfg["move"]
        return move == "dpad" or {"stick": NUNCHUK, "gyro": MOTIONPLUS}.get(move) not in self.extras

    def mouse_velocity(self, now):
        """Velocidad del puntero (px/s) según el stick o la cruceta; el giroscopio lo mueve aparte."""
        speed = self.mouse_cfg["speed"]
        if self.kb.visible:  # la cruceta y el stick eligen teclas
            return 0.0, 0.0
        if self.dpad_moves():
            dx = ("Right" in self.down) - ("Left" in self.down)
            dy = ("Down" in self.down) - ("Up" in self.down)
            if not (dx or dy):
                self.dpad_since = None
                return 0.0, 0.0
            self.dpad_since = self.dpad_since or now
            slow, fast = Mouse.DPAD_SPEED
            v = (slow + (fast - slow) * min(1.0, now - self.dpad_since)) * speed
            if dx and dy:
                v /= math.sqrt(2)
            return dx * v, dy * v
        if self.mouse_cfg["move"] == "stick":
            # al cuadrado: con el stick poco inclinado va lento y preciso
            return tuple(math.copysign(v * v, v) * Mouse.MAX_SPEED * speed for v in self.mouse_stick)
        return 0.0, 0.0

    def gyro_pointer(self, code, raw, t):
        i, inv = GYRO_AXES[code]
        dps, dt = self.gyro_axes[code].rate(raw, t)
        if self.kb.visible:
            return dps
        if self.mouse_cfg[("invert_gyro_x", "invert_gyro_y")[i]]:
            inv = not inv
        d = dps * dt * Mouse.PX_PER_DEGREE * self.mouse_cfg["speed"] * (-1 if inv else 1)
        self.mouse.nudge(d, 0) if i == 0 else self.mouse.nudge(0, d)
        return dps

    def recenter(self):
        for g in self.gyro_axes.values():
            g.recenter()
        if self.rumble and self.rumble_on:
            self.rumble.pulse(0.15)  # aviso de que se recentró
        self.notify("recenter")
        print("Giroscopio recentrado")

    def notify(self, event, wait=False):
        """Sonido de aviso en la PC."""
        play_on_pc(self.sounds.get(event), wait)

    def handle_stick(self, mode, axis_id, value):
        """axis_id: 0 = x, 1 = y; value en -1..1 con y positivo = abajo."""
        if mode in STICK_AXES:
            self.pad.move(STICK_AXES[mode][axis_id], value)
        elif mode == "dpad":
            neg, pos = DIGITAL_AXES[(e.ABS_HAT0X, e.ABS_HAT0Y)[axis_id]]
            self.pad.press(neg, "stick", value < -0.5)
            self.pad.press(pos, "stick", value > 0.5)

    def run(self):
        wiimote = find(WIIMOTE)
        if not wiimote:
            return False
        devices = {wiimote.fd: wiimote}
        extras = self.extras
        extras.clear()

        def wanted():
            # El acelerómetro y el Motion Plus solo se abren si el perfil los usa:
            # abiertos, el Wiimote envía datos sin parar y gasta más batería.
            p = self.profile
            gyro = self.mouse_cfg["move"] == "gyro" if self.mouse_mode else p["gyro"] != "none"
            return {NUNCHUK} | ({ACCEL} if p["tilt"] != "none" else set()) | ({MOTIONPLUS} if gyro else set())

        attach_after = 0.0  # tras quitar un accesorio, esperar a que su nodo desaparezca

        def attach_extras():
            if time.monotonic() < attach_after:
                return
            for name in wanted() - extras.keys():
                if d := find(name):
                    try:
                        d.grab()
                    except OSError:  # se está quitando justo ahora
                        d.close()
                        continue
                    devices[d.fd] = d
                    extras[name] = d
                    if name == NUNCHUK:
                        self.nunchuk_center = (d.absinfo(e.ABS_HAT0X).value, d.absinfo(e.ABS_HAT0Y).value)
                        self.make_axes()
                        print("Nunchuk conectado")
                        if not first_attach:  # al encender ya suena el aviso del Wiimote
                            self.notify("nunchuk")

        def detach(name, gone):
            nonlocal attach_after
            d = extras.pop(name)
            devices.pop(d.fd, None)
            d.close()
            self.pad.reset()
            if name == NUNCHUK:
                self.mouse_stick = [0.0, 0.0]
            if gone:
                attach_after = time.monotonic() + 2
                if name == NUNCHUK:
                    print("Nunchuk desconectado")
                    self.notify("nunchuk")

        def detach_extras():
            present = set(evdev.list_devices())
            for name, d in list(extras.items()):
                if d.path not in present or name not in wanted():
                    detach(name, d.path not in present)

        wiimote.grab()
        self.down.clear()
        self.suppressed.clear()
        self.mouse_mode = False  # al encender, siempre en modo mando de juego
        self.kb.hide()
        self.kb.stop_watch()
        try:
            self.rumble = Rumble(wiimote)
        except OSError as err:
            print(f"El Wiimote no acepta vibración ({err})")
        first_attach = True
        attach_extras()
        first_attach = False
        print(f"Wiimote conectado -> mando virtual '{self.pad.name}'")
        if self.rumble and self.rumble_on:
            self.rumble.pulse(0.3)
        self.notify("connect")
        last_scan = last_input = time.monotonic()
        motion = {}  # eje del acelerómetro -> último valor que contó como movimiento
        timeout = 1.0
        try:
            while True:
                watch_fd = self.kb.watch_fd()
                r, _, _ = select.select([*devices, self.pad.ui.fd, *([watch_fd] if watch_fd else [])], [], [], timeout)
                p = self.profile
                for fd in r:
                    if fd == self.pad.ui.fd:
                        self.pad.handle_ff()
                        continue
                    if fd == watch_fd:
                        self.focus_changed()
                        continue
                    dev = devices.get(fd)
                    if dev is None:  # se quitó en esta misma vuelta
                        continue
                    try:
                        events = list(dev.read())
                    except BlockingIOError:
                        continue
                    except OSError:
                        if dev is wiimote:
                            raise
                        # Un accesorio que se quita (p. ej. el Nunchuk) no es el Wiimote desconectándose.
                        detach(next(n for n, d in extras.items() if d is dev), True)
                        continue
                    for ev in events:
                        if ev.type == e.EV_KEY:
                            last_input = time.monotonic()
                            self.handle_button(ev.code, ev.value)
                        elif ev.type == e.EV_ABS and dev.name == NUNCHUK and ev.code in (e.ABS_HAT0X, e.ABS_HAT0Y):
                            i = ev.code == e.ABS_HAT0Y
                            value = self.nunchuk_axes[i].convert(ev.value)
                            if abs(value) > ACTIVITY:
                                last_input = time.monotonic()
                            if self.mouse_mode:
                                self.mouse_stick[i] = value
                            else:
                                self.handle_stick(p["nunchuk_stick"], i, value)
                        elif ev.type == e.EV_ABS and dev.name == ACCEL:
                            if abs(ev.value - motion.setdefault(ev.code, ev.value)) > MOTION_THRESHOLD:
                                motion[ev.code] = ev.value
                                last_input = time.monotonic()
                            # Si el giroscopio usa el mismo stick, manda él.
                            if ev.code in (e.ABS_RX, e.ABS_RY) and p["tilt"] != p["gyro"] and not self.mouse_mode:
                                i = ev.code == e.ABS_RY
                                self.handle_stick(p["tilt"], i, self.tilt_axes[i].convert(ev.value))
                        elif ev.type == e.EV_ABS and dev.name == MOTIONPLUS and ev.code in GYRO_AXES:
                            if self.mouse_mode:
                                if self.mouse_cfg["move"] == "gyro" and self.gyro_pointer(ev.code, ev.value, ev.timestamp()):
                                    last_input = time.monotonic()
                                continue
                            value = self.gyro_axes[ev.code].convert(ev.value, ev.timestamp())
                            if abs(value) > ACTIVITY:
                                last_input = time.monotonic()
                            self.handle_stick(p["gyro"], GYRO_AXES[ev.code][0], value)
                        elif ev.type == e.EV_SYN:
                            self.pad.syn()
                now = time.monotonic()
                timeout = 1.0
                if self.rumble:
                    level, ends = self.pad.rumble_level(now)
                    timeout = min(timeout, ends, self.rumble.update(level if self.rumble_on else 0.0, now))
                if self.mouse_mode:
                    self.mouse.velocity = self.mouse_velocity(now)
                    timeout = min(timeout, self.mouse.tick(now))
                if self.kb.visible:
                    self.kb.stick(*self.mouse_stick, now)
                    timeout = min(timeout, self.kb.tick(now))
                if any(self.pad.held.values()) or self.mouse.busy():  # botón mantenido (p. ej. acelerar)
                    last_input = now
                if self.idle_timeout and now - last_input > self.idle_timeout:
                    print(f"{self.idle_timeout // 60} min sin usar el Wiimote: apagándolo para ahorrar batería "
                          "(presiona 1+2 para volver a conectarlo)")
                    self.pad.reset()
                    self.mouse.reset()
                    if not power_off(wiimote):
                        print("No se pudo desconectar el Wiimote (¿falta bluetoothctl?)")
                    last_input = now
                if now - last_scan > 1 or self.rescan:
                    last_scan, self.rescan = now, False
                    if self.config_changed():
                        self.reload()
                    detach_extras()
                    attach_extras()
        except OSError:
            print("Wiimote desconectado")
            self.pad.reset()
            self.mouse.reset()
            self.kb.hide()
            self.kb.stop_watch()
            self.notify("disconnect", wait=True)
            return True
        finally:
            self.rumble = None
            extras.clear()
            for d in devices.values():
                try:
                    d.ungrab()
                except OSError:
                    pass
                d.close()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, default=CONFIG, help=f"archivo de configuración (def. {CONFIG})")
    p.add_argument("--profile", help="activa este perfil al arrancar")
    p.add_argument("--idle", type=int, metavar="MIN",
                   help=f"apaga el Wiimote tras MIN minutos sin uso; 0 = nunca (def. {IDLE_TIMEOUT_MIN})")
    p.add_argument("--once", action="store_true",
                   help="sale cuando el Wiimote se desconecta (lo usa el servicio que arranca al conectarlo)")
    args = p.parse_args()

    if args.profile or args.idle is not None:
        cfg = load_config(args.config)
        if args.profile:
            if args.profile not in cfg["profiles"]:
                raise SystemExit(f"Perfil desconocido. Disponibles: {', '.join(cfg['profiles'])}")
            cfg["active"] = args.profile
        if args.idle is not None:
            cfg["idle_timeout_min"] = max(0, args.idle)
        save_config(cfg, args.config)

    try:
        pad = Pad()
        mouse = Mouse()
    except (OSError, evdev.UInputError) as err:
        raise SystemExit(f"No se pudo crear el mando virtual ({err}).\n"
                         "Ejecuta install.sh y reinicia sesión (necesita acceso a /dev/uinput).")
    bridge = Bridge(pad, mouse, args.config)
    print("Esperando el Wiimote (presiona 1+2 o el botón sync)... Ctrl+C para salir")
    deadline = time.monotonic() + ONCE_WAIT
    try:
        while True:
            if bridge.run():
                if args.once:
                    break
            elif args.once and time.monotonic() > deadline:
                print("No apareció el Wiimote; saliendo")
                break
            else:
                time.sleep(1)
                if bridge.config_changed():
                    bridge.reload()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.kb.close()
        pad.close()
        mouse.close()


if __name__ == "__main__":
    main()
