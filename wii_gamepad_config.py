#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Abre la ventana de configuración de wii-gamepad: la de GTK4 + libadwaita si el
sistema la tiene (libadwaita 1.5 o más reciente) y, si no, la clásica de Tkinter.
Con WII_GAMEPAD_TK=1 abre siempre la clásica."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def adwaita_ok():
    if os.environ.get("WII_GAMEPAD_TK"):
        return False
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw
    except (ImportError, ValueError):
        return False
    return (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 5)


def main():
    if adwaita_ok():
        import wii_gamepad_adw
        wii_gamepad_adw.main()
    else:
        os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
        import wii_gamepad_gui
        wii_gamepad_gui.App().mainloop()


if __name__ == "__main__":
    main()
