#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Graba las capturas y el GIF del README sin Wiimote ni escritorio.

    python3 docs/escena.py        (necesita podman y acceso a /dev/uinput)

En la PC crea un Wiimote falso (el de tests/test_bridge.py) y arranca el puente
real; en un contenedor abre la ventana en una pantalla virtual (Xvfb), lee el
mando virtual de la PC y graba mientras el Wiimote falso pulsa botones.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
IMAGE = "wii-gamepad-capturas"
CONTAINERFILE = """FROM registry.fedoraproject.org/fedora:43
RUN dnf install -y python3-tkinter python3-evdev xorg-x11-server-Xvfb ffmpeg-free ImageMagick xdotool \\
        libxkbcommon xkeyboard-config dejavu-sans-fonts google-noto-sans-symbols2-fonts && dnf clean all
"""
DURATION = 15  # s de GIF
# Guion del Wiimote falso: (segundo, acción, argumentos)
SCRIPT = [
    (0.6, "press", ("A", 0.45)), (1.3, "press", ("B", 0.5)), (2.0, "press", ("Z", 0.45)), (2.7, "press", ("C", 0.4)),
    (3.4, "press", ("Up", 0.3)), (3.8, "press", ("Left", 0.3)), (4.2, "press", ("Right", 0.3)),
    (4.6, "press", ("Down", 0.3)), (5.2, "circle", ()), (7.6, "gyro", ()),
    (10.0, "press", ("Home", 0.4)), (10.6, "press", ("1", 0.35)), (11.1, "press", ("2", 0.35)),
    # a los 12 s la ventana cambia al perfil Geometry Dash: B pasa a ser A
    (13.0, "press", ("B", 0.5)), (13.8, "press", ("B", 0.5)),
]


# --- dentro del contenedor --------------------------------------------------------
def inside():
    sys.path.insert(0, "/src")
    os.environ["DISPLAY"] = ":99"
    xvfb = subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1600x1200x24", "-nolisten", "tcp"])
    time.sleep(1)
    import wii_gamepad_gui as gui

    out = Path("/out")
    app = gui.App()

    def region(w):
        w.update_idletasks()
        return w.winfo_rootx(), w.winfo_rooty(), w.winfo_width(), w.winfo_height()

    def shot(w, name):
        x, y, wd, ht = region(w)
        subprocess.run(["import", "-window", "root", "-crop", f"{wd}x{ht}+{x}+{y}", str(out / f"{name}.png")])

    def record():
        # El aviso de abajo ensancha la ventana al cambiar de perfil: se pone ya y se fija el tamaño.
        app.status.configure(text="Guardado y aplicado: Recomendado (Nunchuk)")
        app.update()
        x, y, w, h = region(app)
        app.geometry(f"{w}x{h}")
        proc = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "x11grab", "-draw_mouse", "0", "-framerate", "20",
                                 "-video_size", f"{w}x{h}", "-i", f":99+{x},{y}", "-t", str(DURATION),
                                 "-c:v", "ffv1", str(out / "demo.mkv")])
        (out / "ready").touch()  # la PC empieza el guion
        app.after(12000, lambda: (app.profile_var.set("Geometry Dash"), app.select_profile()))
        app.after(DURATION * 1000 + 800, lambda: (proc.wait(), stills()))

    def stills():
        app.cfg["sounds"] = dict(gui.wg.GLOBAL_OPTIONS["sounds"])  # se grabó en silencio; aquí, los de siempre
        app.profile_var.set("Recomendado (Nunchuk)")
        app.select_profile()
        app.update()
        shot(app, "ventana")
        app.open_mouse()
        app.after(600, lambda: (shot(app.mouse_win, "modo-mouse"), app.mouse_win.destroy(), app.open_sounds(),
                                app.after(600, lambda: (shot(app.sounds_win, "sonidos"), keyboard()))))

    def keyboard():
        kb = subprocess.Popen([sys.executable, "/src/wii_keyboard.py", "--window"], stdin=subprocess.PIPE)
        kb.stdin.write(json.dumps({"show": True, "page": 0, "row": 2, "col": 4, "shift": False}).encode() + b"\n")
        kb.stdin.flush()
        time.sleep(1.5)
        wid = subprocess.run(["xdotool", "search", "--class", "wii-gamepad-keyboard"],
                             capture_output=True, text=True).stdout.split()[-1]
        subprocess.run(["import", "-window", wid, str(out / "teclado.png")])
        kb.stdin.close()
        kb.wait()
        app.destroy()

    app.after(2500, record)  # tiempo para que encuentre el mando virtual
    app.mainloop()
    xvfb.terminate()


# --- en la PC ---------------------------------------------------------------------
def run_script(fake):
    def circle():
        import math
        for i in range(48):
            a = i / 48 * 2 * math.pi
            fake.stick(int(100 * math.cos(a)), int(100 * math.sin(a)))
            time.sleep(0.04)
        fake.stick(0, 0)

    def gyro():
        fake.gyro(-14000, 0.7)
        fake.gyro(100, 0.3)
        fake.gyro(14000, 0.7)
        fake.gyro(100, 0.3)

    start = time.monotonic()
    for t, action, args in SCRIPT:
        time.sleep(max(0.0, start + t - time.monotonic()))
        if action == "press":
            threading.Thread(target=fake.press, args=args, daemon=True).start()
        else:
            threading.Thread(target={"circle": circle, "gyro": gyro}[action], daemon=True).start()


def outside():
    sys.path.insert(0, str(ROOT / "tests"))
    import test_bridge as tb
    import wii_gamepad as wg

    engine = shutil.which("podman") or sys.exit("Necesitas podman")
    print("Preparando el contenedor...")
    subprocess.run([engine, "build", "-q", "-t", IMAGE, "-f", "-", str(DOCS)], input=CONTAINERFILE.encode(),
                   check=True, stdout=subprocess.DEVNULL)
    blocked = tb.block_service()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg_dir, out = tmp / "config" / "wii-gamepad", tmp / "out"
            cfg_dir.mkdir(parents=True)
            out.mkdir()
            (tmp / "config" / "kxkbrc").write_text("[Layout]\nLayoutList=latam\n")  # teclado en español
            cfg = wg.default_config()
            cfg["sounds"] = {k: "" for k in cfg["sounds"]}
            cfg["idle_timeout_min"] = 0
            (cfg_dir / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

            # Motion Plus quieto desde el principio, para que el giroscopio ya esté calibrado al grabar
            fake = tb.FakeWiimote()
            env = os.environ | {"XDG_CONFIG_HOME": str(tmp / "config"), "PYTHONUNBUFFERED": "1"}
            bridge = subprocess.Popen([sys.executable, str(ROOT / "wii_gamepad.py"), "--once"], env=env,
                                      stdout=subprocess.DEVNULL)
            calm = threading.Event()
            threading.Thread(target=lambda: [fake.gyro(100, 0.25) for _ in iter(calm.is_set, True)],
                             daemon=True).start()
            container = subprocess.Popen([
                engine, "run", "--rm", "--security-opt", "label=disable",
                "-v", "/dev/input:/dev/input", "-v", f"{ROOT}:/src:ro", "-v", f"{out}:/out",
                "-v", f"{tmp / 'config'}:/home/usuario/.config", "-e", "XDG_CONFIG_HOME=/home/usuario/.config",
                IMAGE, "python3", "/src/docs/escena.py", "--dentro"])
            print("Grabando...")
            while not (out / "ready").exists():
                if container.poll() is not None:
                    sys.exit("El contenedor terminó antes de grabar")
                time.sleep(0.05)
            calm.set()
            time.sleep(0.3)
            run_script(fake)
            container.wait()
            fake.close()
            bridge.wait(timeout=10)

            print("Creando el GIF...")
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(out / "demo.mkv"), "-vf",
                            "fps=12,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];"
                            "[b][p]paletteuse=dither=none:diff_mode=rectangle",
                            str(DOCS / "demo.gif")], check=True)
            for name in ("ventana", "modo-mouse", "sonidos", "teclado"):
                shutil.copy(out / f"{name}.png", DOCS / f"{name}.png")
    finally:
        if blocked:
            tb.unblock_service()
    print("Listo: docs/demo.gif, docs/ventana.png, docs/modo-mouse.png, docs/sonidos.png, docs/teclado.png")


if __name__ == "__main__":
    inside() if sys.argv[1:] == ["--dentro"] else outside()
