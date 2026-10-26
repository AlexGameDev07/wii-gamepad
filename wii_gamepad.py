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
import threading
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
# Qué Wiimotes hay y qué mando virtual maneja cada uno; lo escribe el puente y lo lee la ventana.
STATE = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "wii-gamepad" / "state.json"
MAX_PLAYERS = 4  # los LEDs del Wiimote

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
# Quién puede pasar al modo mouse cuando hay varios Wiimotes
MOUSE_PLAYERS = {"any": "Cualquiera (uno a la vez)", "p1": "Solo el jugador 1"}
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
# Avisos en la PC (sonido y notificación de escritorio): evento -> etiqueta.
SOUND_EVENTS = {"connect": "Wiimote conectado", "disconnect": "Wiimote apagado o desconectado",
                "battery": "Batería baja del Wiimote",
                "recenter": "Giroscopio recentrado", "nunchuk": "Nunchuk conectado / desconectado",
                "mouse": "Modo mouse activado / desactivado"}
SOUND_DIRS = [CONFIG.parent / "sonidos", Path(__file__).resolve().parent / "sonidos"]  # los del usuario primero
SOUND_EXTS = {".wav", ".mp3", ".ogg", ".oga", ".flac", ".opus"}
GLOBAL_OPTIONS = {"idle_timeout_min": 10, "recenter_combo": "B+Down", "rumble": True,
                  "mouse_combo": "+-", "mouse": MOUSE_DEFAULTS,
                  "sounds": {"connect": "maximize_006.ogg", "disconnect": "minimize_006.ogg",
                             "recenter": "confirmation_001.ogg", "nunchuk": "toggle_001.ogg",
                             "mouse": "switch_003.ogg", "battery": "bong_001.ogg"},
                  "notifications": {"connect": True, "disconnect": True, "battery": True,
                                    "recenter": False, "nunchuk": False, "mouse": False},
                  "mouse_players": "any",
                  # Por Wiimote (su MAC): {"name": ..., "profile": ...}; sin perfil usa "active".
                  "devices": {},
                  "ui": {"animations": True, "theme": "system"}}
IDLE_OPTIONS = {0: "Nunca", 5: "Tras 5 min", 10: "Tras 10 min", 15: "Tras 15 min", 20: "Tras 20 min",
                30: "Tras 30 min"}
IDLE_TIMEOUT_MIN = GLOBAL_OPTIONS["idle_timeout_min"]  # minutos sin uso antes de apagar (0 = nunca)
MOTION_THRESHOLD = 25  # cambio del acelerómetro (~0.25 g) que cuenta como "en uso"
ACTIVITY = 0.15  # un stick analógico cuenta como "en uso" (para el apagado) pasado este valor
ONCE_WAIT = 15  # con --once, segundos que espera al Wiimote antes de rendirse
NUNCHUK_SPAN = 128  # el driver da el stick del Nunchuk en -128..127
NUNCHUK_REST = 25  # más lejos del 0 que esto, el stick no estaba en reposo al conectarlo
INPUT_CHECK = 0.1  # cada cuánto mirar si apareció un nodo en /dev/input (p. ej. el Nunchuk)
UNPLUG_GRACE = 1.5  # al apagarse el Wiimote se va también el Nunchuk: su aviso espera por si es eso
BATTERY_LOW = (20, 10)  # avisa al bajar de cada nivel (%), una vez por conexión
BATTERY_CHECK = 120  # segundos entre lecturas de la batería (cada una le pide su estado al Wiimote)


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
    for k, v in GLOBAL_OPTIONS["notifications"].items():
        cfg["notifications"].setdefault(k, v)
    for k, v in GLOBAL_OPTIONS["ui"].items():
        cfg["ui"].setdefault(k, v)
    if cfg["mouse_players"] not in MOUSE_PLAYERS:
        cfg["mouse_players"] = "any"
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


def rename_profile(cfg, old, new):
    cfg["profiles"] = {(new if k == old else k): v for k, v in cfg["profiles"].items()}
    if cfg["active"] == old:
        cfg["active"] = new
    for d in cfg["devices"].values():
        if d.get("profile") == old:
            d["profile"] = new


def delete_profile(cfg, name):
    """Borra el perfil; los Wiimotes que lo usaban pasan al perfil por defecto."""
    del cfg["profiles"][name]
    if cfg["active"] == name:
        cfg["active"] = next(iter(cfg["profiles"]))
    for d in cfg["devices"].values():
        if d.get("profile") == name:
            del d["profile"]


def read_state():
    """Lo que publica el puente en marcha (sus jugadores), o None si no está en marcha."""
    try:
        state = json.loads(STATE.read_text())
        os.kill(state["pid"], 0)
        return state
    except (OSError, ValueError, KeyError, TypeError):
        return None


class Axis:
    """Normaliza un eje del Nunchuk/acelerómetro a -1..1 con zona muerta
    y autocalibración del rango."""

    def __init__(self, center, span, deadzone, invert=False, limit=math.inf):
        self.center, self.span, self.deadzone, self.invert = center, span, deadzone, invert
        self.limit = limit  # el rango aprendido nunca pasa de esto (un pico raro lo dejaría lento)

    def convert(self, raw):
        v = raw - self.center
        self.span = min(self.limit, max(self.span, abs(v)))
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

    def __init__(self, number=1):
        self.phys = f"wii-gamepad/pad{number}"  # así la ventana sabe qué mando es de qué jugador
        self.ui = UInput({e.EV_KEY: XBOX_KEYS, e.EV_ABS: XBOX_ABS, e.EV_FF: [e.FF_RUMBLE]}, name=PAD_NAME,
                         vendor=0x045E, product=0x028E, version=0x110, bustype=e.BUS_USB, phys=self.phys,
                         max_effects=16)
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


def group_of(dev_path):
    """Qué Wiimote es: el dispositivo HID del que cuelgan todos sus nodos (botones, Nunchuk,
    acelerómetro...). Los dispositivos uinput (las pruebas) no tienen padre: van por su phys."""
    try:
        return str((Path("/sys/class/input") / Path(dev_path).name / "device" / "device").resolve(strict=True))
    except OSError:
        return "phys:" + (sysfs(dev_path, "phys") or "").strip()


def device_id(dev_path):
    """Identificador estable de un Wiimote: su MAC (o el phys de uno falso)."""
    uevent = sysfs(dev_path, "device/uevent") or ""  # dispositivo HID padre
    mac = next((l.split("=", 1)[1] for l in uevent.splitlines() if l.startswith("HID_UNIQ=")), "")
    return mac.upper() or (sysfs(dev_path, "phys") or "").strip()


def wiimotes():
    """Wiimotes conectados, en el orden en que aparecieron: grupo -> nodo de sus botones.
    Solo mira sysfs, no abre nada."""
    paths = [p for p in evdev.list_devices() if (sysfs(p, "name") or "").strip() == WIIMOTE]
    return {group_of(p): p for p in sorted(paths, key=lambda p: int(p.rsplit("event", 1)[-1]))}


def find_phys(phys):
    """Ruta del dispositivo con ese phys (los mandos virtuales se distinguen así), o None."""
    return next((p for p in evdev.list_devices() if (sysfs(p, "phys") or "").strip() == phys), None)


def set_leds(group, number):
    """Enciende en el Wiimote el LED de su número de jugador. Escribir en los LEDs necesita la
    regla udev de install.sh; sin ella el Wiimote se queda con el LED que pone el driver."""
    if not group.startswith("/"):
        return
    for led in Path(group).glob("leds/*:blue:p[0-3]"):
        try:
            (led / "brightness").write_text("1" if int(led.name[-1]) == number - 1 else "0")
        except (OSError, ValueError):
            pass


def find(name, group=None, skip=()):
    for path in evdev.list_devices():
        if path in skip:
            continue
        # Mirar el nombre en sysfs evita abrir cada dispositivo (abrir el
        # acelerómetro lo enciende y gasta batería del Wiimote).
        sys_name = sysfs(path, "name")
        if sys_name is not None and sys_name.strip() != name:
            continue
        if group is not None and group_of(path) != group:
            continue
        try:
            d = evdev.InputDevice(path)
        except PermissionError:
            # Un nodo recién creado es de root hasta que udev le aplica la regla: no avisar aún.
            if path not in warned and time.time() - os.stat(path).st_ctime > 2:
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


def notify_desktop(summary, body="", urgency="normal", transient=True, wait=False):
    """Notificación de escritorio con notify-send (libnotify); sin él no hace nada."""
    sender = shutil.which("notify-send")
    if not sender:
        return
    cmd = [sender, "-a", "Wii Gamepad", "-i", "wii-gamepad", "-u", urgency]
    if transient:  # los avisos de rutina no se quedan en el historial
        cmd += ["-h", "boolean:transient:true"]
    try:
        proc = subprocess.Popen([*cmd, summary, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if wait:
            proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def battery_path(dev_path=None):
    """Archivo de sysfs con la carga del Wiimote de ese dispositivo (o del primero que haya), o None."""
    base = (Path("/sys/class/input") / Path(dev_path).name / "device" / "device" / "power_supply"
            if dev_path else Path("/sys/class/power_supply"))
    return next(base.glob("wiimote_battery_*/capacity"), None)


def battery_level(path):
    """Carga en %, o None. El driver da byte * 100 / 255, pero con pilas nuevas el Wiimote
    no pasa de 0xC8 (~78 %): se reescala para que unas pilas nuevas marquen 100 %."""
    try:
        raw = int(Path(path).read_text())
    except (OSError, TypeError, ValueError):
        return None
    return min(100, round(raw * 255 / 0xC8))


def power_off(dev):
    """Corta el Bluetooth del Wiimote, que al perder la conexión se apaga."""
    return disconnect(device_id(dev.path))


def disconnect(mac):
    if ":" not in mac:  # un Wiimote falso de las pruebas
        return False
    try:
        return subprocess.run(["bluetoothctl", "disconnect", mac], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False




class Player:
    """Un Wiimote con sus accesorios y el mando virtual que maneja."""

    def __init__(self, bridge, group, wiimote, number, pad):
        self.bridge, self.group, self.wiimote, self.number, self.pad = bridge, group, wiimote, number, pad
        self.id = device_id(wiimote.path)
        self.devices = {wiimote.fd: wiimote}
        self.extras = {}  # accesorios abiertos: nombre -> dispositivo
        self.rumble = None
        self.down, self.suppressed = set(), set()  # botones físicos apretados / tragados por la combinación
        self.mouse_stick = [0.0, 0.0]  # stick del Nunchuk en modo mouse
        self.dpad_since = None  # desde cuándo la cruceta mueve el puntero (para acelerarlo)
        self.stale = set()  # nodos de accesorios quitados que aún no desaparecen
        self.nunchuk_gone = None  # cuándo se quitó el Nunchuk, si su aviso está pendiente
        self.motion = {}  # eje del acelerómetro -> último valor que contó como movimiento
        self.battery = self.battery_level = None
        self.battery_warned = set()  # niveles de BATTERY_LOW ya avisados
        self.battery_reads = []  # la lectura se hace en otro hilo: puede tardar y frenaría los botones
        self.idle_off = False
        self.last_input = self.last_battery = time.monotonic()

    @property
    def label(self):
        return self.bridge.devices.get(self.id, {}).get("name") or f"Jugador {self.number}"

    @property
    def mouse_mode(self):
        return self.bridge.mouse_owner is self

    def start(self):
        self.wiimote.grab()
        try:
            self.rumble = Rumble(self.wiimote)
        except OSError as err:
            print(f"[{self.label}] El Wiimote no acepta vibración ({err})")
        self.load_profile()
        self.attach_extras(first=True)
        set_leds(self.group, self.number)
        print(f"[{self.label}] Wiimote conectado -> mando virtual {self.number}")
        if self.rumble and self.bridge.rumble_on:
            self.rumble.pulse(0.3)
        self.battery = battery_path(self.wiimote.path)
        self.battery_level = battery_level(self.battery)
        battery = f" · Batería: {self.battery_level} %" if self.battery_level is not None else ""
        self.bridge.notify("connect", f"{self.label} conectado", f"Mando virtual {self.number}{battery}")
        self.battery_reads.append(self.battery_level)

    def load_profile(self):
        cfg = self.bridge.cfg
        name = self.bridge.devices.get(self.id, {}).get("profile")
        self.profile_name = name if name in cfg["profiles"] else cfg["active"]
        self.profile = cfg["profiles"][self.profile_name]
        self.buttons = {SOURCES[s][0]: (s, t) for s, t in self.profile["buttons"].items() if s in SOURCES}
        self.pad.reset()
        self.make_axes()
        print(f"[{self.label}] Perfil: {self.profile_name}")

    def make_axes(self):
        p = self.profile
        cx, cy = getattr(self, "nunchuk_center", (0, 0))
        self.nunchuk_axes = (Axis(cx, 95, p["deadzone"], limit=NUNCHUK_SPAN),
                             Axis(cy, 95, p["deadzone"], invert=True, limit=NUNCHUK_SPAN))
        span = 100 / max(0.1, p["tilt_sensitivity"])
        self.tilt_axes = (Axis(0, span, 0.15, invert=True), Axis(0, span, 0.15, invert=p["invert_tilt_y"]))
        old = getattr(self, "gyro_axes", {})  # conservar la calibración al recargar
        self.gyro_axes = {code: Gyro(p["gyro_sensitivity"], inv ^ (i == 1 and p["invert_gyro_y"]),
                                     aim=p["gyro_mode"] == "aim", bias=old[code].bias if code in old else None)
                          for code, (i, inv) in GYRO_AXES.items()}

    # --- accesorios -------------------------------------------------------------
    def wanted(self):
        # El acelerómetro y el Motion Plus solo se abren si el perfil los usa:
        # abiertos, el Wiimote envía datos sin parar y gasta más batería.
        p = self.profile
        gyro = self.bridge.mouse_cfg["move"] == "gyro" if self.mouse_mode else p["gyro"] != "none"
        return {NUNCHUK} | ({ACCEL} if p["tilt"] != "none" else set()) | ({MOTIONPLUS} if gyro else set())

    def attach_extras(self, first=False):
        for name in self.wanted() - self.extras.keys():
            if d := find(name, self.group, skip=self.stale):
                try:
                    d.grab()
                except OSError:  # se está quitando justo ahora
                    d.close()
                    continue
                self.devices[d.fd] = d
                self.extras[name] = d
                if name == NUNCHUK:
                    # El driver ya centra el stick en 0. Si al conectarlo se estaba moviendo (o
                    # llegó un dato basura al enchufarlo), ese no es el reposo: mejor 0.
                    center = (d.absinfo(e.ABS_HAT0X).value, d.absinfo(e.ABS_HAT0Y).value)
                    self.nunchuk_center = tuple(c if abs(c) <= NUNCHUK_REST else 0 for c in center)
                    self.make_axes()
                    print(f"[{self.label}] Nunchuk conectado")
                    if not first:  # al encender ya suena el aviso del Wiimote
                        self.bridge.notify("nunchuk", f"{self.label}: Nunchuk conectado")

    def detach(self, name, gone):
        d = self.extras.pop(name)
        self.devices.pop(d.fd, None)
        d.close()
        self.pad.reset()
        if name == NUNCHUK:
            self.mouse_stick = [0.0, 0.0]
        if gone:
            self.stale.add(d.path)
            if name == NUNCHUK:
                self.nunchuk_gone = time.monotonic()

    def scan(self):
        present = set(evdev.list_devices())
        self.stale &= present
        for name, d in list(self.extras.items()):
            if d.path not in present or name not in self.wanted():
                self.detach(name, d.path not in present)
        self.attach_extras()

    # --- eventos ----------------------------------------------------------------
    def fds(self):
        return list(self.devices)

    def handle(self, fd):
        """Lee un dispositivo del Wiimote. Si el que falla es el Wiimote, deja pasar el OSError."""
        dev = self.devices.get(fd)
        if dev is None:  # se quitó en esta misma vuelta
            return
        try:
            events = list(dev.read())
        except BlockingIOError:
            return
        except OSError:
            if dev is self.wiimote:
                raise
            # Un accesorio que se quita (p. ej. el Nunchuk) no es el Wiimote desconectándose.
            self.detach(next(n for n, d in self.extras.items() if d is dev), True)
            return
        p = self.profile
        for ev in events:
            if ev.type == e.EV_KEY:
                self.last_input = time.monotonic()
                self.handle_button(ev.code, ev.value)
            elif ev.type == e.EV_ABS and dev.name == NUNCHUK and ev.code in (e.ABS_HAT0X, e.ABS_HAT0Y):
                i = ev.code == e.ABS_HAT0Y
                value = self.nunchuk_axes[i].convert(ev.value)
                if abs(value) > ACTIVITY:
                    self.last_input = time.monotonic()
                if self.mouse_mode:
                    self.mouse_stick[i] = value
                else:
                    self.handle_stick(p["nunchuk_stick"], i, value)
            elif ev.type == e.EV_ABS and dev.name == ACCEL:
                if abs(ev.value - self.motion.setdefault(ev.code, ev.value)) > MOTION_THRESHOLD:
                    self.motion[ev.code] = ev.value
                    self.last_input = time.monotonic()
                # Si el giroscopio usa el mismo stick, manda él.
                if ev.code in (e.ABS_RX, e.ABS_RY) and p["tilt"] != p["gyro"] and not self.mouse_mode:
                    i = ev.code == e.ABS_RY
                    self.handle_stick(p["tilt"], i, self.tilt_axes[i].convert(ev.value))
            elif ev.type == e.EV_ABS and dev.name == MOTIONPLUS and ev.code in GYRO_AXES:
                if self.mouse_mode:
                    if self.bridge.mouse_cfg["move"] == "gyro" and self.gyro_pointer(ev.code, ev.value,
                                                                                      ev.timestamp()):
                        self.last_input = time.monotonic()
                    continue
                value = self.gyro_axes[ev.code].convert(ev.value, ev.timestamp())
                if abs(value) > ACTIVITY:
                    self.last_input = time.monotonic()
                self.handle_stick(p["gyro"], GYRO_AXES[ev.code][0], value)
            elif ev.type == e.EV_SYN:
                self.pad.syn()

    def handle_button(self, code, value):
        src = SOURCE_OF.get(code)
        if src is None:
            return
        b = self.bridge
        self.down.add(src) if value else self.down.discard(src)
        if src in self.suppressed:  # forma parte de la combinación: no llega al juego hasta soltarlo
            if not value:
                self.suppressed.discard(src)
            return
        if value == 1:
            for combo, action in ((b.mouse_combo, lambda: b.toggle_mouse(self)), (b.combo, self.recenter)):
                if combo and self.down.issuperset(combo):
                    for s in combo:  # soltar lo que ya se había apretado
                        self.pad.press(self.profile["buttons"].get(s, "none"), s, False)
                        if self.mouse_mode:
                            b.mouse.press(b.mouse_cfg["buttons"].get(s, "none"), s, False)
                    self.suppressed.update(combo)
                    action()
                    return
        if self.mouse_mode:
            action = b.mouse_cfg["buttons"].get(src, "none")
            if b.kb.visible and b.kb.button(src, bool(value), time.monotonic()):
                return
            if action == "keyboard":
                if value == 1:
                    b.kb.hide() if b.kb.visible else b.show_keyboard()
            elif not (src in ("Up", "Down", "Left", "Right") and self.dpad_moves()):
                b.mouse.press(action, src, bool(value))
        elif code in self.buttons:
            src, target = self.buttons[code]
            self.pad.press(target, src, bool(value))

    def mode_changed(self):
        """Entró o salió del modo mouse."""
        self.pad.reset()
        self.mouse_stick = [0.0, 0.0]
        self.bridge.rescan = True  # p. ej. abrir el Motion Plus si el puntero va con el giroscopio
        if self.rumble and self.bridge.rumble_on:
            self.rumble.pulse(0.15)
        mode = "Modo mouse" if self.mouse_mode else "Modo mando de juego"
        self.bridge.notify("mouse", f"{self.label}: {mode.lower()}")
        print(f"[{self.label}] {mode}")

    def dpad_moves(self):
        """En modo mouse, la cruceta mueve el puntero si se eligió así o si falta el
        Nunchuk / Motion Plus elegido para moverlo."""
        move = self.bridge.mouse_cfg["move"]
        return move == "dpad" or {"stick": NUNCHUK, "gyro": MOTIONPLUS}.get(move) not in self.extras

    def mouse_velocity(self, now):
        """Velocidad del puntero (px/s) según el stick o la cruceta; el giroscopio lo mueve aparte."""
        speed = self.bridge.mouse_cfg["speed"]
        if self.bridge.kb.visible:  # la cruceta y el stick eligen teclas
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
        if self.bridge.mouse_cfg["move"] == "stick":
            # al cuadrado: con el stick poco inclinado va lento y preciso
            return tuple(math.copysign(v * v, v) * Mouse.MAX_SPEED * speed for v in self.mouse_stick)
        return 0.0, 0.0

    def gyro_pointer(self, code, raw, t):
        cfg = self.bridge.mouse_cfg
        i, inv = GYRO_AXES[code]
        dps, dt = self.gyro_axes[code].rate(raw, t)
        if self.bridge.kb.visible:
            return dps
        if cfg[("invert_gyro_x", "invert_gyro_y")[i]]:
            inv = not inv
        d = dps * dt * Mouse.PX_PER_DEGREE * cfg["speed"] * (-1 if inv else 1)
        self.bridge.mouse.nudge(d, 0) if i == 0 else self.bridge.mouse.nudge(0, d)
        return dps

    def recenter(self):
        for g in self.gyro_axes.values():
            g.recenter()
        if self.rumble and self.bridge.rumble_on:
            self.rumble.pulse(0.15)  # aviso de que se recentró
        self.bridge.notify("recenter", f"{self.label}: giroscopio recentrado")
        print(f"[{self.label}] Giroscopio recentrado")

    def handle_stick(self, mode, axis_id, value):
        """axis_id: 0 = x, 1 = y; value en -1..1 con y positivo = abajo."""
        if mode in STICK_AXES:
            self.pad.move(STICK_AXES[mode][axis_id], value)
        elif mode == "dpad":
            neg, pos = DIGITAL_AXES[(e.ABS_HAT0X, e.ABS_HAT0Y)[axis_id]]
            self.pad.press(neg, "stick", value < -0.5)
            self.pad.press(pos, "stick", value > 0.5)

    def tick(self, now):
        """Trabajo por tiempo (vibración, apagado, batería...); devuelve en cuántos segundos volver."""
        b = self.bridge
        timeout = math.inf
        if self.nunchuk_gone is not None:
            if now - self.nunchuk_gone >= UNPLUG_GRACE:
                self.nunchuk_gone = None
                print(f"[{self.label}] Nunchuk desconectado")
                b.notify("nunchuk", f"{self.label}: Nunchuk desconectado")
            else:
                timeout = self.nunchuk_gone + UNPLUG_GRACE - now
        if self.rumble:
            level, ends = self.pad.rumble_level(now)
            timeout = min(timeout, ends, self.rumble.update(level if b.rumble_on else 0.0, now))
        if any(self.pad.held.values()) or (self.mouse_mode and b.mouse.busy()):  # botón mantenido
            self.last_input = now
        if b.idle_timeout and now - self.last_input > b.idle_timeout:
            print(f"[{self.label}] {b.idle_timeout // 60} min sin usar el Wiimote: apagándolo para ahorrar "
                  "batería (presiona 1+2 para volver a conectarlo)")
            self.pad.reset()
            if self.mouse_mode:
                b.mouse.reset()
            if power_off(self.wiimote):
                self.idle_off = True
            else:
                print("No se pudo desconectar el Wiimote (¿falta bluetoothctl?)")
            self.last_input = now
        if self.battery and now - self.last_battery > BATTERY_CHECK:
            self.last_battery = now
            threading.Thread(target=lambda: self.battery_reads.append(battery_level(self.battery)),
                             daemon=True).start()
        while self.battery_reads:
            level = self.battery_reads.pop(0)
            if level is not None and level != self.battery_level:
                self.battery_level = level
                b.state_dirty = True
            low = [t for t in BATTERY_LOW if level is not None and level <= t and t not in self.battery_warned]
            if low:
                self.battery_warned.update(low)
                print(f"[{self.label}] Batería baja del Wiimote: {level} %")
                b.notify("battery", f"{self.label}: batería baja", f"Queda {level} %. Cambia las pilas pronto.",
                         urgency="critical" if min(low) == min(BATTERY_LOW) else "normal")
        return timeout

    def close(self, gone, wait=False):
        self.pad.reset()
        if self.rumble and self.rumble.on and not gone:
            try:
                self.wiimote.write(e.EV_FF, self.rumble.id, 0)
            except OSError:
                pass
        self.rumble = None
        for d in self.devices.values():
            try:
                d.ungrab()
            except OSError:
                pass
            d.close()
        self.devices.clear()
        self.extras.clear()
        if gone:
            print(f"[{self.label}] Wiimote desconectado")
            idle = f"Se apagó tras {self.bridge.idle_timeout // 60} min sin uso. " if self.idle_off else ""
            self.bridge.notify("disconnect", f"{self.label} desconectado",
                               idle + "Presiona 1+2 para volver a conectarlo.", wait=wait)


class Bridge:
    """Reparte los Wiimotes conectados entre los mandos virtuales (uno por jugador) y
    comparte entre ellos el mouse virtual y el teclado en pantalla."""

    def __init__(self, pad, mouse, config_path):
        self.mouse, self.config_path = mouse, config_path
        # Los mandos virtuales no se cierran al irse su Wiimote: así el juego no pierde el
        # mando y, al volver, el Wiimote recupera su mismo número.
        self.pads = {1: pad}
        self.players = {}  # grupo -> Player
        self.last_number = {}  # id del Wiimote -> último número de jugador que tuvo
        self.mouse_owner = None  # el jugador que maneja el puntero, si alguno está en modo mouse
        self.mtime = None
        self.rescan = False  # buscar accesorios ya (p. ej. abrir el Motion Plus al entrar al modo mouse)
        self.state_dirty = True
        self.full_warned = False
        self.kb = wii_keyboard.Keyboard(mouse.tap)
        self.reload()

    def reload(self):
        cfg = self.cfg = load_config(self.config_path)
        self.mtime = self.config_path.stat().st_mtime
        self.mouse.reset()
        self.idle_timeout = cfg["idle_timeout_min"] * 60
        self.combo = RECENTER_COMBOS.get(cfg["recenter_combo"], RECENTER_COMBOS["none"])[0]
        self.mouse_combo = MOUSE_COMBOS.get(cfg["mouse_combo"], MOUSE_COMBOS["none"])[0]
        self.mouse_players = cfg["mouse_players"]
        self.mouse_cfg = cfg["mouse"]
        self.rumble_on = cfg["rumble"]
        self.sounds = cfg["sounds"]
        self.notifications = cfg["notifications"]
        self.devices = cfg["devices"]
        owner = self.mouse_owner
        # Sin combinación no habría forma de salir; y con "solo el jugador 1", nadie más se queda el puntero.
        if owner and (not self.mouse_combo or not self.may_use_mouse(owner)):
            self.toggle_mouse(owner)
        self.watch_focus()
        for p in self.players.values():
            p.load_profile()
        if not self.players:
            print(f"Perfil por defecto: {cfg['active']}")
        self.state_dirty = True

    def config_changed(self):
        try:
            return self.config_path.stat().st_mtime != self.mtime
        except FileNotFoundError:
            return False

    def notify(self, event, summary=None, body="", urgency="normal", wait=False):
        """Aviso en la PC: notificación de escritorio (si está activada para ese evento) y sonido."""
        if self.notifications.get(event):
            notify_desktop(summary or SOUND_EVENTS[event], body, urgency, event != "battery", wait)
        play_on_pc(self.sounds.get(event), wait)

    # --- jugadores --------------------------------------------------------------
    def pad_for(self, number):
        if number not in self.pads:
            try:
                self.pads[number] = Pad(number)
            except (OSError, evdev.UInputError) as err:
                print(f"No se pudo crear el mando virtual {number} ({err})")
                return None
        return self.pads[number]

    def free_number(self, wiimote_id):
        used = {p.number for p in self.players.values()}
        last = self.last_number.get(wiimote_id)
        if last and last not in used:
            return last
        return next((n for n in range(1, MAX_PLAYERS + 1) if n not in used), None)

    def scan(self):
        """Altas y bajas de Wiimotes y de sus accesorios (una vez por segundo)."""
        present = wiimotes()
        for group, p in list(self.players.items()):
            if group not in present:
                self.remove(p, gone=True)
        for p in self.players.values():
            p.scan()
        for group, path in present.items():
            if group in self.players:
                continue
            number = self.free_number(device_id(path))
            if number is None:
                if not self.full_warned:
                    self.full_warned = True
                    print(f"Ya hay {MAX_PLAYERS} Wiimotes conectados; los demás se ignoran")
                continue
            pad = self.pad_for(number)
            wiimote = find(WIIMOTE, group) if pad else None
            if wiimote is None:
                continue
            p = Player(self, group, wiimote, number, pad)
            try:
                p.start()
            except OSError as err:  # se fue mientras arrancaba
                print(f"No se pudo usar el Wiimote ({err})")
                p.close(gone=False)
                continue
            self.players[group] = p
            self.last_number[p.id] = number
            self.state_dirty = True

    def remove(self, player, gone):
        del self.players[player.group]
        if self.mouse_owner is player:
            self.mouse_owner = None
            self.mouse.reset()
            self.kb.hide()
            self.watch_focus()
        player.close(gone, wait=not self.players)  # si es el último, con --once el puente sale ya
        self.full_warned = False
        self.state_dirty = True

    # --- modo mouse -------------------------------------------------------------
    def may_use_mouse(self, player):
        return self.mouse_players != "p1" or player.number == 1

    def toggle_mouse(self, player):
        """El jugador entra o sale del modo mouse. Si otro lo tenía, vuelve a ser mando."""
        if not self.may_use_mouse(player):
            print(f"[{player.label}] Solo el jugador 1 puede usar el modo mouse")
            return
        old = self.mouse_owner
        self.mouse_owner = None if old is player else player
        self.mouse.reset()
        self.kb.hide()
        self.watch_focus()
        if old and old is not player:
            old.mode_changed()
        player.mode_changed()
        self.state_dirty = True

    def watch_focus(self):
        if self.mouse_owner and self.mouse_cfg["auto_keyboard"]:
            self.kb.start_watch()
        else:
            self.kb.stop_watch()

    def show_keyboard(self, auto=False):
        self.mouse.reset()  # p. ej. el clic con A que entró al campo de texto sigue apretado
        self.kb.show(auto)

    def focus_changed(self):
        """Aviso del vigilante de foco: mostrar el teclado al entrar a un campo de texto, quitarlo al salir."""
        text = self.kb.read_focus()
        if not self.mouse_owner or text is None:
            return
        if text and not self.kb.visible:
            self.show_keyboard(auto=True)
        elif not text and self.kb.auto:
            self.kb.hide()

    # --- bucle ------------------------------------------------------------------
    def write_state(self):
        players = [{"id": p.id, "number": p.number, "name": p.label, "profile": p.profile_name,
                    "pad": p.pad.phys, "mouse": p.mouse_mode, "battery": p.battery_level}
                   for p in sorted(self.players.values(), key=lambda p: p.number)]
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"pid": os.getpid(), "players": players}, ensure_ascii=False))
            tmp.replace(STATE)
        except OSError:
            pass
        self.state_dirty = False

    def run(self, once=False):
        """Atiende a los Wiimotes. Con once, vuelve cuando ya no queda ninguno."""
        deadline = time.monotonic() + ONCE_WAIT
        seen = False
        last_scan = -math.inf
        timeout = 0.0
        inputs, fast_until = None, 0.0
        while True:
            now = time.monotonic()
            # Un accesorio enchufado crea un nodo nuevo que los juegos también ven: hay que
            # tomarlo (grab) enseguida o el juego lo cuenta como otro jugador.
            try:
                changed, inputs = inputs != (m := os.stat("/dev/input").st_mtime_ns), m
            except OSError:
                changed = False
            if changed:  # el nodo es de root hasta que udev le da permisos: reintentar seguido
                fast_until = now + 2
            every = INPUT_CHECK if now < fast_until else 1
            if now - last_scan >= every or self.rescan or changed:
                last_scan, self.rescan = now, False
                if self.config_changed():
                    self.reload()
                self.scan()
                seen = seen or bool(self.players)
                if once and not self.players and (seen or now > deadline):
                    if not seen:
                        print("No apareció el Wiimote; saliendo")
                    return
            if self.state_dirty:
                self.write_state()
            watch_fd = self.kb.watch_fd()
            fds = {fd: p for p in self.players.values() for fd in p.fds()}
            pads = {pad.ui.fd: pad for pad in self.pads.values()}
            r, _, _ = select.select([*fds, *pads, *([watch_fd] if watch_fd else [])], [], [], timeout)
            for fd in r:
                if fd in pads:  # los juegos suben y lanzan vibraciones aunque el mando no tenga Wiimote
                    pads[fd].handle_ff()
                elif fd == watch_fd:
                    self.focus_changed()
                elif self.players.get(fds[fd].group) is fds[fd]:
                    try:
                        fds[fd].handle(fd)
                    except OSError:
                        self.remove(fds[fd], gone=True)
            now = time.monotonic()
            timeout = last_scan + every - now
            if self.players:
                timeout = min(timeout, INPUT_CHECK)
            for p in list(self.players.values()):
                timeout = min(timeout, p.tick(now))
            if owner := self.mouse_owner:
                self.mouse.velocity = owner.mouse_velocity(now)
                timeout = min(timeout, self.mouse.tick(now))
                if self.kb.visible:
                    self.kb.stick(*owner.mouse_stick, now)
                    timeout = min(timeout, self.kb.tick(now))
            timeout = max(0.0, timeout)

    def close(self):
        for p in list(self.players.values()):
            p.close(gone=False)
        self.players.clear()
        self.kb.close()
        for pad in self.pads.values():
            pad.close()
        self.mouse.close()
        STATE.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, default=CONFIG, help=f"archivo de configuración (def. {CONFIG})")
    p.add_argument("--profile", help="perfil por defecto de los Wiimotes que no tienen uno elegido")
    p.add_argument("--idle", type=int, metavar="MIN",
                   help=f"apaga el Wiimote tras MIN minutos sin uso; 0 = nunca (def. {IDLE_TIMEOUT_MIN})")
    p.add_argument("--once", action="store_true",
                   help="sale cuando no queda ningún Wiimote (lo usa el servicio que arranca al conectarlo)")
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
    try:
        bridge.run(args.once)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
