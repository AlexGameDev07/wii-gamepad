#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Prueba la bocina de cada Wiimote conectado. Herramienta experimental: no toca la app.

hid-wiimote no maneja la bocina, así que se le habla al Wiimote por /dev/hidraw con los
reportes 0x14, 0x16, 0x18 y 0x19 (wiibrew.org/wiki/Wiimote). hidraw es de root:

    sudo python3 wii_speaker_test.py --listar
    sudo python3 wii_speaker_test.py                 # uno por uno, y pregunta qué se oyó
    sudo python3 wii_speaker_test.py --ambos         # los dos a la vez
    sudo python3 wii_speaker_test.py --mando 00:11:22:33:44:55 --formato pcm --volumen 255
"""
import argparse
import itertools
import math
import os
import select
import sys
import time
from pathlib import Path

NINTENDO = 0x057E
PRODUCTOS = {0x0306: "RVL-CNT-01", 0x0330: "RVL-CNT-01-TR"}
ENABLE, DATA, MUTE, WMEM, STATUS = 0x14, 0x18, 0x19, 0x16, 0x15
ON, OFF = 0x04, 0x00
PEDIR_ACK = 0x02
ACK, ESTADO = 0x22, 0x20
CONTROL = 0x04  # registros y no EEPROM: sin este bit se sobrescribe la memoria del Wiimote
BLOQUE = 20
# Graves: en pcm (2000 Hz) una nota cerca de 1000 Hz se oye sucia, sin filtro que quite su imagen.
NOTAS = (262, 330, 392)
SEGUNDOS_NOTA = 0.5

DIFF = (1, 3, 5, 7, 9, 11, 13, 15, -1, -3, -5, -7, -9, -11, -13, -15)
ESCALA = (230, 230, 230, 230, 307, 409, 512, 614) * 2

# nombre -> (formato, valor de frecuencia, volumen por defecto, Hz, muestras/bloque, s/bloque)
FORMATOS = {
    "pcm": (0x40, 0x1770, 0x60, 2000, BLOQUE, 0.010),
    "adpcm": (0x00, 0x07D0, 0x40, 3000, BLOQUE * 2, BLOQUE * 2 / 3000),
    # Dolphin duda de si ADPCM va al doble: con el mismo ajuste, el doble de datos por segundo.
    "adpcm-rapido": (0x00, 0x07D0, 0x40, 6000, BLOQUE * 2, BLOQUE * 2 / 6000),
}


def reporte_wmem(offset, datos):
    assert len(datos) <= 16
    return bytes((WMEM, CONTROL, *offset.to_bytes(3, "big"), len(datos), *datos)).ljust(22, b"\0")


def reporte_datos(bloque):
    assert 0 < len(bloque) <= BLOQUE
    return bytes((DATA, len(bloque) << 3, *bloque)).ljust(22, b"\0")


def configuracion(formato, volumen):
    fmt, valor = FORMATOS[formato][:2]
    return bytes((0x00, fmt, valor & 0xFF, valor >> 8, volumen, 0x00, 0x00))


def tono(frecuencias, hz, segundos, amplitud=0.6):
    """Muestras de -1 a 1, con un silencio corto entre notas para distinguirlas."""
    salida = []
    for f in frecuencias:
        n = int(hz * segundos)
        corte = n - int(hz * 0.05)
        salida += [amplitud * math.sin(2 * math.pi * f * i / hz) if i < corte else 0.0 for i in range(n)]
    return salida


def pcm(muestras):
    return bytes(int(s * 127) & 0xFF for s in muestras)


def adpcm(muestras):
    """Yamaha ADPCM de 4 bits; el Wiimote lee primero el nibble alto (Dolphin, Speaker.cpp)."""
    predictor, paso, nibbles = 0, 127, []
    for s in muestras:
        delta = int(s * 32767) - predictor
        nibble = min(7, abs(delta) * 4 // paso) + (8 if delta < 0 else 0)
        predictor = max(-32768, min(32767, predictor + int(paso * DIFF[nibble] / 8)))
        paso = max(127, min(24576, (paso * ESCALA[nibble]) >> 8))
        nibbles.append(nibble)
    if len(nibbles) % 2:
        nibbles.append(0)
    return bytes(a << 4 | b for a, b in zip(nibbles[::2], nibbles[1::2]))


def paquetes(formato, frecuencias=NOTAS):
    hz, muestras_bloque = FORMATOS[formato][3:5]
    muestras = tono(frecuencias, hz, SEGUNDOS_NOTA)
    muestras = muestras[:len(muestras) // muestras_bloque * muestras_bloque]
    datos = pcm(muestras) if formato == "pcm" else adpcm(muestras)
    return [reporte_datos(datos[i:i + BLOQUE]) for i in range(0, len(datos), BLOQUE)]


def mac_de(uniq):
    return uniq.upper()


def buscar(raiz=Path("/sys/class/hidraw")):
    """Los Wiimotes conectados como [(ruta, MAC, modelo), ...]."""
    hallados = []
    for nodo in sorted(raiz.glob("hidraw*")):
        try:
            campos = dict(l.split("=", 1) for l in (nodo / "device/uevent").read_text().splitlines() if "=" in l)
            _, vendor, product = (int(x, 16) for x in campos["HID_ID"].split(":"))
        except (OSError, KeyError, ValueError):
            continue
        if vendor == NINTENDO and product in PRODUCTOS:
            hallados.append((f"/dev/{nodo.name}", mac_de(campos.get("HID_UNIQ", "?")), PRODUCTOS[product]))
    return hallados


class Wiimote:
    def __init__(self, ruta, mac, modelo, fd=None):
        self.ruta, self.mac, self.modelo = ruta, mac, modelo
        self.fd = os.open(ruta, os.O_RDWR) if fd is None else fd
        self.ack_fallidos = []

    def escribir(self, datos):
        os.write(self.fd, datos)

    def esperar(self, condicion, limite=0.5):
        """El primer reporte que cumpla la condición, o None. Los de botones se ignoran."""
        fin = time.monotonic() + limite
        while (falta := fin - time.monotonic()) > 0:
            if select.select([self.fd], [], [], falta)[0]:
                reporte = os.read(self.fd, 64)
                if condicion(reporte):
                    return reporte
        return None

    def registro(self, offset, datos):
        self.escribir(reporte_wmem(offset, datos))
        ack = self.esperar(lambda r: len(r) >= 5 and r[0] == ACK and r[3] == WMEM)
        if ack is None or ack[4]:
            self.ack_fallidos.append((hex(offset), None if ack is None else ack[4]))

    def comando(self, reporte, valor):
        """0x14 y 0x19 con ACK: sin esperarlo, la siguiente orden puede llegar antes de tiempo."""
        self.escribir(bytes((reporte, valor | PEDIR_ACK)))
        ack = self.esperar(lambda r: len(r) >= 5 and r[0] == ACK and r[3] == reporte)
        if ack is None or ack[4]:
            self.ack_fallidos.append((hex(reporte), None if ack is None else ack[4]))

    def vaciar(self):
        """Descarta lo que hidraw guardó mientras nadie leía."""
        while select.select([self.fd], [], [], 0)[0]:
            os.read(self.fd, 64)

    def estado(self):
        """(bocina activa, batería de 0 a 255) o None si no contesta."""
        self.vaciar()
        self.escribir(bytes((STATUS, 0)))
        r = self.esperar(lambda r: len(r) >= 7 and r[0] == ESTADO)
        return None if r is None else (bool(r[3] & 0x04), r[6])

    def encender(self, formato, volumen):
        self.ack_fallidos = []
        self.vaciar()
        self.comando(ENABLE, ON)
        self.comando(MUTE, ON)
        self.registro(0xA20009, b"\x01")
        self.registro(0xA20001, b"\x08")
        self.registro(0xA20001, configuracion(formato, volumen))
        self.registro(0xA20008, b"\x01")
        self.comando(MUTE, OFF)

    def apagar(self):
        self.escribir(bytes((MUTE, ON)))
        self.escribir(bytes((ENABLE, OFF)))


def reproducir(pares, periodo, reloj=time.monotonic, dormir=time.sleep):
    """Manda [(wiimote, paquetes), ...] a ritmo fijo. Devuelve cuántos bloques salieron tarde."""
    inicio, tarde = reloj(), 0
    for i, grupo in enumerate(itertools.zip_longest(*(p for _, p in pares))):
        turno = inicio + i * periodo
        if (falta := turno - reloj()) > 0:
            dormir(falta)
        elif -falta > periodo:
            tarde += 1
        for (wiimote, _), paquete in zip(pares, grupo):
            if paquete:
                wiimote.escribir(paquete)
    return tarde


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
