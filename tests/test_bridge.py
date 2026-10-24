#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Prueba de punta a punta sin Wiimote: crea un Wiimote, un Nunchuk y un Motion Plus
falsos con uinput (mismos nombres y botones que el driver hid-wiimote), arranca el
puente real y comprueba lo que sale por el mando Xbox virtual y el mouse virtual.

    python3 tests/test_bridge.py

Necesita acceso a /dev/uinput (lo da install.sh). Mientras corre, el servicio
wii-gamepad no arranca solo, para que no se quede con el Wiimote falso.
"""
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
import fcntl
from pathlib import Path

import evdev
from evdev import UInput, AbsInfo, ecodes as e, ff

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import wii_gamepad as wg  # noqa: E402

RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
DROPIN = RUNTIME / "systemd" / "user.control" / "wii-gamepad.service.d" / "zz-test.conf"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {'ok ' if ok else 'MAL'}  {name}{'' if ok or not detail else f'  ({detail})'}")


class FakeWiimote:
    """Dispositivos falsos. El Wiimote atiende las vibraciones como el driver real."""

    def __init__(self):
        keys = [code for code, _ in wg.SOURCES.values() if code not in (e.BTN_C, e.BTN_Z)]
        self.wiimote = UInput({e.EV_KEY: keys, e.EV_FF: [e.FF_RUMBLE]}, name=wg.WIIMOTE, max_effects=4)
        self.nunchuk = UInput({e.EV_KEY: [e.BTN_C, e.BTN_Z],
                               e.EV_ABS: [(e.ABS_HAT0X, AbsInfo(0, -120, 120, 2, 4, 0)),
                                          (e.ABS_HAT0Y, AbsInfo(0, -120, 120, 2, 4, 0))]}, name=wg.NUNCHUK)
        gyro = AbsInfo(0, -16000, 16000, 4, 8, 0)
        self.motionplus = UInput({e.EV_ABS: [(e.ABS_RX, gyro), (e.ABS_RY, gyro), (e.ABS_RZ, gyro)]},
                                 name=wg.MOTIONPLUS)
        self.rumble = []  # valores EV_FF que llegan al Wiimote (1 = vibrar, 0 = parar)
        self.running = True
        threading.Thread(target=self._serve_ff, daemon=True).start()

    def _serve_ff(self):
        ui = self.wiimote
        while self.running:
            try:
                if not select.select([ui.fd], [], [], 0.05)[0]:
                    continue
                events = list(ui.read())
            except (OSError, ValueError):  # el Wiimote falso se cerró
                return
            for ev in events:
                if ev.type == e.EV_UINPUT and ev.code == e.UI_FF_UPLOAD:
                    up = ff.UInputUpload(request_id=ev.value)
                    fcntl.ioctl(ui.fd, wg.UI_BEGIN_FF_UPLOAD, up)
                    up.retval = 0
                    fcntl.ioctl(ui.fd, wg.UI_END_FF_UPLOAD, up)
                elif ev.type == e.EV_UINPUT and ev.code == e.UI_FF_ERASE:
                    er = ff.UInputErase(request_id=ev.value)
                    fcntl.ioctl(ui.fd, wg.UI_BEGIN_FF_ERASE, er)
                    er.retval = 0
                    fcntl.ioctl(ui.fd, wg.UI_END_FF_ERASE, er)
                elif ev.type == e.EV_FF:
                    self.rumble.append(ev.value)

    def press(self, src, hold=0.05):
        code = wg.SOURCES[src][0]
        dev = self.nunchuk if src in ("C", "Z") else self.wiimote
        dev.write(e.EV_KEY, code, 1)
        dev.syn()
        time.sleep(hold)
        dev.write(e.EV_KEY, code, 0)
        dev.syn()

    def combo(self, *srcs):
        """Aprieta varios botones del Wiimote a la vez."""
        for value in (1, 0):
            for src in srcs:
                self.wiimote.write(e.EV_KEY, wg.SOURCES[src][0], value)
            self.wiimote.syn()
            time.sleep(0.1)

    def stick(self, x, y):
        self.nunchuk.write(e.EV_ABS, e.ABS_HAT0X, x)
        self.nunchuk.write(e.EV_ABS, e.ABS_HAT0Y, y)
        self.nunchuk.syn()

    def gyro(self, ry, seconds):
        """Motion Plus a 40 lecturas/s con el giro ry (y un poco de ruido para que cada lectura cuente)."""
        for i in range(int(seconds * 40)):
            self.motionplus.write(e.EV_ABS, e.ABS_RY, ry + (i % 2) * 10)
            self.motionplus.write(e.EV_ABS, e.ABS_RZ, 50 + (i % 2) * 10)
            self.motionplus.syn()
            time.sleep(0.025)

    def close(self):
        self.running = False
        for d in (self.motionplus, self.nunchuk, self.wiimote):
            try:
                d.close()
            except OSError:
                pass


def wait_device(name, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if d := wg.find(name):
            return d
        time.sleep(0.1)
    return None


def drain(dev):
    while select.select([dev.fd], [], [], 0.05)[0]:
        list(dev.read())


def wait_event(dev, pred, timeout=2.0):
    """Primer evento que cumple pred (o None)."""
    end = time.monotonic() + timeout
    while (left := end - time.monotonic()) > 0:
        if select.select([dev.fd], [], [], left)[0]:
            for ev in dev.read():
                if pred(ev):
                    return ev
    return None


def key(code, value=1):
    return lambda ev: ev.type == e.EV_KEY and ev.code == code and ev.value == value


def axis(code, cond):
    return lambda ev: ev.type == e.EV_ABS and ev.code == code and cond(ev.value)


def block_service():
    """Que el servicio de usuario no arranque con el Wiimote falso (solo mientras dura la prueba)."""
    unit = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user/wii-gamepad.service"
    if not unit.exists():
        return False
    active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "wii-gamepad.service"]).returncode == 0
    if active:
        raise SystemExit("El puente está en marcha con un Wiimote real; apágalo antes de la prueba.")
    DROPIN.parent.mkdir(parents=True, exist_ok=True)
    DROPIN.write_text("[Unit]\nConditionPathExists=/wii-gamepad-en-prueba\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    return True


def unblock_service():
    DROPIN.unlink(missing_ok=True)
    try:
        DROPIN.parent.rmdir()
    except OSError:
        pass
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)


def test_config(tmp):
    print("Configuración")
    path = tmp / "vieja.json"
    cfg = wg.default_config()
    cfg["sounds"] |= {"connect": "mixkit-que-ya-no-existe.wav", "nunchuk": ""}
    path.write_text(json.dumps(cfg))
    loaded = wg.load_config(path)
    check("un sonido que ya no existe vuelve al de por defecto",
          loaded["sounds"]["connect"] == wg.GLOBAL_OPTIONS["sounds"]["connect"])
    check("«Ninguno» se respeta", loaded["sounds"]["nunchuk"] == "")
    check("los sonidos por defecto están incluidos",
          all(wg.sound_path(n) for n in wg.GLOBAL_OPTIONS["sounds"].values()))


def test_mouse(fake, mouse):
    fake.stick(110, 0)
    check("el stick mueve el puntero", wait_event(mouse, lambda ev: ev.type == e.EV_REL and ev.code == e.REL_X
                                                  and ev.value > 0) is not None)
    fake.stick(0, 0)
    fake.press("A")
    check("A -> clic izquierdo", wait_event(mouse, key(e.BTN_LEFT)) is not None)
    fake.press("Down")
    check("cruceta ↓ -> rueda", wait_event(mouse, lambda ev: ev.type == e.EV_REL and ev.code == e.REL_WHEEL)
          is not None)


def test_bridge(tmp):
    cfg_path = tmp / "config.json"
    cfg = wg.default_config()
    cfg["sounds"] = {k: "" for k in cfg["sounds"]}  # en silencio
    cfg["mouse"]["auto_keyboard"] = False  # que no aparezca el teclado en el escritorio
    cfg["idle_timeout_min"] = 0
    cfg_path.write_text(json.dumps(cfg))

    fake = FakeWiimote()
    bridge = subprocess.Popen([sys.executable, str(ROOT / "wii_gamepad.py"), "--config", str(cfg_path), "--once"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              env=os.environ | {"PYTHONUNBUFFERED": "1", "XDG_CONFIG_HOME": str(tmp)})
    out = []

    def read_output():
        for line in bridge.stdout:
            out.append(line)

    def wait_output(text, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not any(text in l for l in out):
            time.sleep(0.05)
        return any(text in l for l in out)

    threading.Thread(target=read_output, daemon=True).start()
    pad = mouse = None
    try:
        print("Mando Xbox virtual")
        pad = wait_device(wg.PAD_NAME)
        check("el puente crea el mando 'Microsoft X-Box 360 pad'", pad is not None, "".join(out))
        if pad is None:
            return
        pad.grab()  # que los eventos de la prueba no lleguen a Steam ni a otros programas
        check("identificadores de un mando Xbox 360 (045e:028e)",
              (pad.info.vendor, pad.info.product) == (0x045E, 0x028E))
        check("detecta el Wiimote", wait_output("Wiimote conectado"), "".join(out))
        check("detecta el Nunchuk", wait_output("Nunchuk conectado"))
        time.sleep(0.3)
        drain(pad)

        for src, code, label in (("A", e.BTN_A, "A"), ("Up", e.BTN_Y, "Y"), ("Left", e.BTN_X, "X"),
                                 ("1", e.BTN_SELECT, "Back"), ("2", e.BTN_START, "Start"),
                                 ("Home", e.BTN_MODE, "Guide"), ("C", e.BTN_TL, "LB")):
            fake.press(src)
            check(f"{src} -> {label}", wait_event(pad, key(code)) is not None)
        fake.press("B")
        check("B -> gatillo RT a fondo", wait_event(pad, axis(e.ABS_RZ, lambda v: v == 255)) is not None)
        fake.press("Z")
        check("Z -> gatillo LT a fondo", wait_event(pad, axis(e.ABS_Z, lambda v: v == 255)) is not None)

        fake.stick(110, 0)
        check("stick del Nunchuk a la derecha -> stick izq. a la derecha",
              wait_event(pad, axis(e.ABS_X, lambda v: v > 25000)) is not None)
        fake.stick(0, 110)
        check("stick del Nunchuk arriba -> stick izq. arriba (Y negativa en Xbox)",
              wait_event(pad, axis(e.ABS_Y, lambda v: v < -25000)) is not None)
        fake.stick(0, 0)
        check("stick del Nunchuk al centro -> stick en reposo",
              wait_event(pad, axis(e.ABS_Y, lambda v: v == 0)) is not None)

        print("Giroscopio (Motion Plus)")
        fake.gyro(100, 1.5)  # quieto: se calibra solo
        drain(pad)
        threading.Thread(target=fake.gyro, args=(-20000, 0.5), daemon=True).start()
        check("girar el Wiimote a la derecha mueve el stick derecho",
              wait_event(pad, axis(e.ABS_RX, lambda v: v > 16000), 1.5) is not None)
        time.sleep(0.6)
        fake.gyro(100, 0.3)
        check("al dejar de girar, el stick vuelve al centro",
              wait_event(pad, axis(e.ABS_RX, lambda v: v == 0), 1.5) is not None)

        print("Vibración")
        fx = ff.Effect(e.FF_RUMBLE, -1, 0, ff.Trigger(0, 0), ff.Replay(300, 0),
                       ff.EffectType(ff_rumble_effect=ff.Rumble(strong_magnitude=0xFFFF, weak_magnitude=0)))
        fake.rumble.clear()
        effect = pad.upload_effect(fx)  # lo que hace un juego
        pad.write(e.EV_FF, effect, 1)
        time.sleep(0.6)
        check("el juego pide vibrar -> el Wiimote vibra y luego para", fake.rumble[:1] == [1] and fake.rumble[-1:] == [0],
              f"recibido {fake.rumble}")
        pad.erase_effect(effect)

        print("Cambios en vivo")
        cfg = json.loads(cfg_path.read_text())
        cfg["active"] = "Geometry Dash"
        cfg_path.write_text(json.dumps(cfg))
        time.sleep(1.5)
        drain(pad)
        fake.press("B")
        check("cambiar de perfil sin reiniciar (Geometry Dash: B -> A)", wait_event(pad, key(e.BTN_A)) is not None)

        print("Modo mouse")
        # El mouse virtual solo lo puede leer root (o el grupo input); sin eso no se prueba
        # mover ni clicar, porque movería y clicaría el escritorio de verdad.
        mouse = wg.find(wg.MOUSE_NAME)
        if mouse:
            mouse.grab()
            drain(mouse)
        fake.combo("+", "-")
        check("+ y − a la vez -> modo mouse", wait_output("Modo mouse", 2))
        drain(pad)
        fake.press("-")  # en modo mouse no hace nada, así la prueba no toca el escritorio
        check("en modo mouse los botones ya no llegan al mando", wait_event(pad, key(e.BTN_THUMBL), 0.5) is None)
        if mouse is None:
            print("  --   puntero y clics sin probar (el mouse virtual solo lo lee root o el grupo input)")
        else:
            test_mouse(fake, mouse)
        fake.combo("+", "-")
        check("otra vez + y − -> vuelve a ser mando", wait_output("Modo mando de juego", 2))
        fake.press("A")
        check("A vuelve a llegar al mando", wait_event(pad, key(e.BTN_A)) is not None)

        print("Desconexiones")

        fake.nunchuk.close()
        time.sleep(1.5)
        check("quitar el Nunchuk no desconecta el Wiimote",
              any("Nunchuk desconectado" in l for l in out) and bridge.poll() is None)
        fake.motionplus.close()
        fake.wiimote.close()
        try:
            bridge.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        check("apagar el Wiimote -> el puente lo detecta y sale (--once)",
              bridge.returncode == 0 and any("Wiimote desconectado" in l for l in out), "".join(out[-5:]))
    finally:
        fake.close()
        for d in (pad, mouse):
            if d:
                try:
                    d.close()
                except OSError:
                    pass
        if bridge.poll() is None:
            bridge.terminate()
            bridge.wait(timeout=5)


def main():
    if not os.access("/dev/uinput", os.W_OK):
        raise SystemExit("Sin acceso a /dev/uinput: ejecuta install.sh primero.")
    blocked = block_service()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            test_config(Path(tmp))
            test_bridge(Path(tmp))
    finally:
        if blocked:
            unblock_service()
    print(f"\n{sum(results)}/{len(results)} comprobaciones correctas")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
