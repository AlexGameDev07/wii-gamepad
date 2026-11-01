#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Prueba a fondo la bocina de cada Wiimote conectado, con varios formatos:

    sudo python3 wii_speaker_test.py --listar
    sudo python3 wii_speaker_test.py                 # uno por uno, y pregunta qué se oyó
    sudo python3 wii_speaker_test.py --ambos         # los dos a la vez
    sudo python3 wii_speaker_test.py --mando 00:11:22:33:44:55 --formato pcm --volumen 255
"""
import argparse
import sys

from wii_speaker import *  # noqa: F403


def preguntar(texto):
    try:
        return input(f"  {texto} [s/n/r=repetir] ").strip().lower()[:1] or "n"
    except EOFError:
        return "n"


def probar(w, formato, volumen, interactivo=True):
    """Devuelve una línea para el resumen."""
    w.encender(formato, volumen)
    estado = w.estado()
    bocina = "sin respuesta" if estado is None else ("activa" if estado[0] else "inactiva")
    print(f"  {formato} a volumen {volumen}: bocina {bocina}; registros sin confirmar: {w.ack_fallidos or 'ninguno'}")
    while True:
        tarde = reproducir([(w, paquetes(formato))], FORMATOS[formato][5])
        respuesta = preguntar("¿Oíste tres notas subiendo?") if interactivo else "-"
        if respuesta != "r":
            break
    w.apagar()
    return f"{w.mac} {w.modelo} {formato} vol={volumen}: oído={respuesta} bocina={bocina} " \
           f"sin_confirmar={len(w.ack_fallidos)} bloques_tarde={tarde}"


def main():
    parser = argparse.ArgumentParser(description="Prueba la bocina de los Wiimotes conectados")
    parser.add_argument("--listar", action="store_true", help="mostrar los Wiimotes y salir")
    parser.add_argument("--mando", help="MAC del Wiimote; sin esto, todos")
    parser.add_argument("--formato", choices=FORMATOS, help="sin esto, todos los formatos")
    parser.add_argument("--volumen", type=int, help="0-255 en pcm, 0-64 en adpcm")
    parser.add_argument("--ambos", action="store_true", help="tonos distintos en dos Wiimotes a la vez")
    args = parser.parse_args()

    hallados = buscar()
    if args.mando:
        hallados = [h for h in hallados if h[1] == args.mando.upper()]
    if not hallados:
        parser.exit(1, "No hay ningún Wiimote conectado con ese criterio.\n")
    if args.listar:
        for ruta, mac, modelo in hallados:
            print(f"{ruta}  {mac}  {modelo}")
        return
    try:
        wiimotes = [Wiimote(*h) for h in hallados]
    except PermissionError:
        parser.exit(1, "Sin permiso sobre hidraw: corre esto con sudo.\n")

    resumen = []
    formatos = [args.formato] if args.formato else list(FORMATOS)
    try:
        for w in wiimotes:
            estado = w.estado()
            bateria = "?" if estado is None else f"{estado[1] * 100 // 200} %"
            print(f"\n{w.mac} {w.modelo} ({w.ruta}), batería {bateria}")
        if args.ambos:
            if len(wiimotes) < 2:
                parser.exit(1, "--ambos necesita dos Wiimotes conectados.\n")
            formato = args.formato or "pcm"
            volumen = args.volumen if args.volumen is not None else FORMATOS[formato][2]
            pares = [(w, paquetes(formato, (f,) * 4)) for w, f in zip(wiimotes, (262, 523))]
            for w, _ in pares:
                w.encender(formato, volumen)
            print(f"\nEl primero suena grave y el segundo agudo ({formato}).")
            tarde = reproducir(pares, FORMATOS[formato][5])
            resumen.append(f"ambos {formato}: oído={preguntar('¿Oíste los dos a la vez?')} bloques_tarde={tarde}")
        else:
            for w in wiimotes:
                print(f"\n=== Acerca el oído a {w.mac} ({w.modelo}) ===")
                for formato in formatos:
                    volumen = args.volumen if args.volumen is not None else FORMATOS[formato][2]
                    resumen.append(probar(w, formato, volumen))
    finally:
        for w in wiimotes:
            try:
                w.apagar()
            except OSError:
                print(f"{w.mac}: se desconectó", file=sys.stderr)
    print("\nResumen (pégalo tal cual):")
    print("\n".join(resumen))


if __name__ == "__main__":
    main()
