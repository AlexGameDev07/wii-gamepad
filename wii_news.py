#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Novedades de cada versión y avisos dentro de la app, para las dos ventanas.

Para una versión nueva: sube VERSION y agrega su entrada arriba en RELEASES. Para un aviso,
agrega una entrada a NOTICES con un id que no se haya usado. target dice a dónde lleva «action»:
"preferences:<página>"; las páginas son general, alerts, mouse y experimental."""

VERSION = "1.4"

# Cada página: (icono simbólico, título, texto)
RELEASES = [
    {"version": "1.4", "title": "Novedades de Wii Gamepad 1.4", "pages": [
        ("input-gaming-symbolic", "Classic Controller y Classic Pro",
         "Enchúfalo al Wiimote y funciona solo: los dos sticks, todos sus botones y también el modo mouse."),
        ("applications-games-symbolic", "Editor de botones nuevo",
         "El mando aparece dibujado con lo que hace cada botón. Haz clic en uno para cambiarlo."),
        ("emblem-ok-symbolic", "Configurar con tu mando",
         "Se ilumina un botón del mando Xbox y aprietas en tu mando el que quieres para él, como en Steam."),
    ]},
    {"version": "1.3", "title": "Novedades de Wii Gamepad 1.3", "pages": [
        ("audio-speakers-symbolic", "La bocina del Wiimote",
         "Ahora los avisos también pueden sonar en el propio Wiimote. Es experimental: se activa en Preferencias."),
        ("input-gaming-symbolic", "Combinaciones en el modo mouse",
         "Alt+Tab, pestañas, cerrar ventanas, copiar y pegar con 2 a 4 botones. Puedes crear las tuyas."),
        ("preferences-system-notifications-symbolic", "Avisos dentro de la app",
         "La campana te cuenta lo nuevo y te lleva directo a donde se activa."),
    ]},
]

NOTICES = [
    {"id": "bocina-experimental", "title": "Bocina del Wiimote",
     "body": "Función experimental: el sonido es áspero y gasta más pilas. Actívala si quieres probarla.",
     "action": "Ir", "target": "preferences:experimental"},
]


def pending_release(cfg):
    """La entrada que falta por mostrar de esta versión, o None."""
    if cfg["news"]["seen"] == VERSION:
        return None
    return next((r for r in RELEASES if r["version"] == VERSION), None)


def mark_seen(cfg):
    cfg["news"]["seen"] = VERSION


def unread_notices(cfg):
    return [n for n in NOTICES if n["id"] not in cfg["news"]["read"]]


def mark_read(cfg, notice_id):
    if notice_id not in cfg["news"]["read"]:
        cfg["news"]["read"].append(notice_id)
