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

    def __init__(self, n=1):
        # Sin un dispositivo HID padre, el puente agrupa los nodos de cada Wiimote falso por su phys.
        self.id = f"fake-wiimote-{n}"
        keys = [code for code, _ in wg.SOURCES.values() if code not in (e.BTN_C, e.BTN_Z)]
        self.wiimote = UInput({e.EV_KEY: keys, e.EV_FF: [e.FF_RUMBLE]}, name=wg.WIIMOTE, phys=self.id,
                              max_effects=4)
        self.nunchuk = self.make_nunchuk()
        gyro = AbsInfo(0, -16000, 16000, 4, 8, 0)
        self.motionplus = UInput({e.EV_ABS: [(e.ABS_RX, gyro), (e.ABS_RY, gyro), (e.ABS_RZ, gyro)]},
                                 name=wg.MOTIONPLUS, phys=self.id)
        self.rumble = []  # valores EV_FF que llegan al Wiimote (1 = vibrar, 0 = parar)
        self.running = True
        threading.Thread(target=self._serve_ff, daemon=True).start()

    def make_nunchuk(self, x=0):
        """x: dónde está el stick al enchufarlo."""
        return UInput({e.EV_KEY: [e.BTN_C, e.BTN_Z],
                       e.EV_ABS: [(e.ABS_HAT0X, AbsInfo(x, -120, 120, 2, 4, 0)),
                                  (e.ABS_HAT0Y, AbsInfo(0, -120, 120, 2, 4, 0))]}, name=wg.NUNCHUK, phys=self.id)

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


def quiet_config(tmp, **extra):
    cfg_path = tmp / "config.json"
    cfg = wg.default_config()
    cfg["sounds"] = {k: "" for k in cfg["sounds"]}  # en silencio
    cfg["notifications"] = {k: False for k in cfg["notifications"]}
    cfg["mouse"]["auto_keyboard"] = False  # que no aparezca el teclado en el escritorio
    cfg["idle_timeout_min"] = 0
    cfg_path.write_text(json.dumps(cfg | extra))
    return cfg_path


class BridgeProcess:
    """El puente real en otro proceso, con su salida a mano."""

    def __init__(self, tmp, cfg_path):
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "wii_gamepad.py"), "--config", str(cfg_path),
                                      "--once"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     env=os.environ | {"PYTHONUNBUFFERED": "1", "XDG_CONFIG_HOME": str(tmp)})
        self.out = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.out.append(line)

    def count(self, text):
        return sum(text in l for l in self.out)

    def wait_output(self, text, timeout=10, times=1):
        end = time.monotonic() + timeout
        while time.monotonic() < end and self.count(text) < times:
            time.sleep(0.05)
        return self.count(text) >= times

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)


def test_bridge(tmp):
    cfg_path = quiet_config(tmp)
    fake = FakeWiimote()
    b = BridgeProcess(tmp, cfg_path)
    bridge, out, wait_output = b.proc, b.out, b.wait_output
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
        time.sleep(1 + wg.UNPLUG_GRACE + 0.5)
        check("quitar el Nunchuk no desconecta el Wiimote",
              any("Nunchuk desconectado" in l for l in out) and bridge.poll() is None)
        # Se vuelve a enchufar con el stick movido (al reconectarlo para seguir jugando).
        fake.nunchuk = fake.make_nunchuk(x=100)
        time.sleep(0.5)
        try:  # si el puente ya lo tomó, nadie más puede: los juegos no lo ven como otro jugador
            node = wait_device(wg.NUNCHUK)
            node.grab()
            node.ungrab()
            grabbed = False
        except OSError:
            grabbed = True
        check("al reconectar el Nunchuk, el puente lo toma enseguida", grabbed)
        reattached = sum("Nunchuk conectado" in l for l in out) == 2
        drain(pad)
        fake.stick(110, 0)
        check("y su stick llega a fondo aunque estuviera movido al enchufarlo",
              wait_event(pad, axis(e.ABS_X, lambda v: v > 25000)) is not None)
        # Apagar el Wiimote con el Nunchuk puesto: se van los dos, pero solo avisa el Wiimote.
        fake.nunchuk.close()
        fake.motionplus.close()
        fake.wiimote.close()
        try:
            bridge.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        check("apagar el Wiimote -> el puente lo detecta y sale (--once)",
              bridge.returncode == 0 and any("Wiimote desconectado" in l for l in out), "".join(out[-5:]))
        check("al apagarlo con el Nunchuk puesto no avisa también del Nunchuk",
              reattached and sum("Nunchuk desconectado" in l for l in out) == 1, "".join(out[-5:]))
    finally:
        fake.close()
        for d in (pad, mouse):
            if d:
                try:
                    d.close()
                except OSError:
                    pass
        b.stop()


def open_pad(number):
    path = wg.find_phys(f"wii-gamepad/pad{number}")
    if not path:
        return None
    pad = evdev.InputDevice(path)
    pad.grab()
    return pad


def test_players(tmp):
    print("Varios Wiimotes")
    cfg_path = quiet_config(tmp, devices={"fake-wiimote-2": {"profile": "Geometry Dash"}})
    fakes = [FakeWiimote(1), FakeWiimote(2)]
    b = BridgeProcess(tmp, cfg_path)
    pads = []
    try:
        check("detecta los dos Wiimotes", b.wait_output("Wiimote conectado", times=2), "".join(b.out))
        time.sleep(0.5)
        state = json.loads(wg.STATE.read_text())
        numbers = {p["id"]: p["number"] for p in state["players"]}
        check("cada uno es un jugador distinto", numbers == {"fake-wiimote-1": 1, "fake-wiimote-2": 2}, str(numbers))
        pads = [open_pad(1), open_pad(2)]
        check("un mando virtual por jugador", all(pads))
        if not all(pads):
            return
        for pad in pads:
            drain(pad)
        fakes[0].press("A")
        check("A del jugador 1 llega solo a su mando",
              wait_event(pads[0], key(e.BTN_A)) is not None and wait_event(pads[1], key(e.BTN_A), 0.3) is None)
        fakes[1].press("B")
        check("cada Wiimote usa su perfil (jugador 2 en Geometry Dash: B -> A)",
              wait_event(pads[1], key(e.BTN_A)) is not None and wait_event(pads[0], key(e.BTN_A), 0.3) is None)

        fakes[0].combo("+", "-")
        fakes[1].combo("+", "-")
        check("con «cualquiera», el modo mouse pasa al último que lo pide",
              b.wait_output("[Jugador 2] Modo mouse", 2) and b.wait_output("[Jugador 1] Modo mando de juego", 2))
        fakes[1].combo("+", "-")
        b.wait_output("[Jugador 2] Modo mando de juego", 2)
        cfg = json.loads(cfg_path.read_text())
        cfg["mouse_players"] = "p1"
        cfg_path.write_text(json.dumps(cfg))
        time.sleep(1.5)
        fakes[1].combo("+", "-")
        check("con «solo el jugador 1», el jugador 2 no puede", b.wait_output("Solo el jugador 1", 2)
              and b.count("[Jugador 2] Modo mouse") == 1)

        fakes[0].close()
        check("apagar el jugador 1 no afecta al 2",
              b.wait_output("[Jugador 1] Wiimote desconectado", 5) and b.proc.poll() is None)
        drain(pads[1])
        fakes[1].press("B")
        check("el jugador 2 sigue funcionando", wait_event(pads[1], key(e.BTN_A)) is not None)
        drain(pads[0])
        fakes[0] = FakeWiimote(1)
        check("al volver, el Wiimote recupera su número", b.wait_output("[Jugador 1] Wiimote conectado", 5, times=2))
        time.sleep(0.3)
        fakes[0].press("A")
        check("y su mismo mando virtual (el juego no lo pierde)", wait_event(pads[0], key(e.BTN_A)) is not None)
        for f in fakes:
            f.close()
        try:
            b.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        check("sin Wiimotes, el puente sale (--once)", b.proc.returncode == 0, "".join(b.out[-5:]))
    finally:
        for f in fakes:
            f.close()
        for pad in pads:
            if pad:
                try:
                    pad.close()
                except OSError:
                    pass
        b.stop()


def main():
    if not os.access("/dev/uinput", os.W_OK):
        raise SystemExit("Sin acceso a /dev/uinput: ejecuta install.sh primero.")
    blocked = block_service()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            test_config(Path(tmp))
            test_bridge(Path(tmp))
            test_players(Path(tmp))
    finally:
        if blocked:
            unblock_service()
    print(f"\n{sum(results)}/{len(results)} comprobaciones correctas")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
