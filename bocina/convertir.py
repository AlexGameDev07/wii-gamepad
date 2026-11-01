#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Convierte ../sonidos/*.ogg en PCM de 8 bits a 2000 Hz para la bocina del Wiimote.

    python3 bocina/convertir.py      (necesita ffmpeg; la app usa los .s8 ya convertidos)
"""
import subprocess
from pathlib import Path

AQUI = Path(__file__).resolve().parent
PICO = 0.7   # como en los tonos: a fondo se distorsiona más
MAXIMO = 1.5  # segundos


def convertir(ogg):
    filtro = "aresample=2000,highpass=f=200"
    salida = subprocess.run(["ffmpeg", "-v", "error", "-i", str(ogg), "-af", filtro, "-ac", "1", "-t", str(MAXIMO),
                             "-f", "s8", "-"], check=True, capture_output=True).stdout
    muestras = [b - 256 if b > 127 else b for b in salida]
    pico = max(map(abs, muestras), default=0) or 1
    return bytes(round(m * PICO * 127 / pico) & 0xFF for m in muestras)


def main():
    for ogg in sorted((AQUI.parent / "sonidos").glob("*.ogg")):
        datos = convertir(ogg)
        (AQUI / f"{ogg.stem}.s8").write_bytes(datos)
        print(f"{ogg.name}: {len(datos) / 2000:.2f} s")


if __name__ == "__main__":
    main()
