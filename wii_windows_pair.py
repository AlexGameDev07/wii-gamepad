#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Empareja Wiimotes en Windows sin que el usuario escriba ningún PIN.

El PIN del Wiimote son 6 bytes binarios: una dirección Bluetooth al revés. Windows no
deja teclearlos, pero su API sí los acepta. Sigue los mismos pasos que Dolphin
(Source/Core/Core/HW/WiimoteReal/IOWin.cpp, GPL-2.0-or-later):

    python wii_windows_pair.py           # botón rojo SYNC: queda emparejado para siempre
    python wii_windows_pair.py --rapido  # 1+2: sin PIN, pero hay que repetirlo cada vez
    python wii_windows_pair.py --olvidar # quita los Wiimotes guardados en Windows
"""
import argparse
import ctypes
import sys
import time

BLUETOOTH_SERVICE_ENABLE = 1
ERROR_SUCCESS = 0
ERROR_NO_MORE_ITEMS = 259
ERROR_MORE_DATA = 234
INQUIRY = 3  # en unidades de 1,28 s
RONDAS = 8  # unos 30 s: da tiempo a abrir la tapa y cubre el parpadeo de SYNC entero
RONDAS_MINIMAS = 3  # Windows rara vez lo consigue a la primera
NOMBRES = ("Nintendo RVL-CNT", "Nintendo RVL-WBC")  # Wiimote y Balance Board
CLASES = (0x002504, 0x000508)  # RVL-CNT-01 y RVL-CNT-01-TR, por si Windows no resuelve el nombre


# Tipos de tamaño fijo y no los de wintypes: DWORD mide 8 bytes en Linux y las pruebas
# comprueban allí que estas estructuras miden lo mismo que en Windows.
class BluetoothAddress(ctypes.Union):
    _fields_ = [("ull", ctypes.c_uint64), ("rgBytes", ctypes.c_uint8 * 6)]


class SystemTime(ctypes.Structure):
    _fields_ = [("campos", ctypes.c_uint16 * 8)]


class FindRadioParams(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32)]


class RadioInfo(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("address", BluetoothAddress),
        ("szName", ctypes.c_uint16 * 248),
        ("ulClassofDevice", ctypes.c_uint32),
        ("lmpSubversion", ctypes.c_uint16),
        ("manufacturer", ctypes.c_uint16),
    ]


class DeviceSearchParams(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("fReturnAuthenticated", ctypes.c_int32),
        ("fReturnRemembered", ctypes.c_int32),
        ("fReturnUnknown", ctypes.c_int32),
        ("fReturnConnected", ctypes.c_int32),
        ("fIssueInquiry", ctypes.c_int32),
        ("cTimeoutMultiplier", ctypes.c_uint8),
        ("hRadio", ctypes.c_void_p),
    ]


class DeviceInfo(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("Address", BluetoothAddress),
        ("ulClassofDevice", ctypes.c_uint32),
        ("fConnected", ctypes.c_int32),
        ("fRemembered", ctypes.c_int32),
        ("fAuthenticated", ctypes.c_int32),
        ("stLastSeen", SystemTime),
        ("stLastUsed", SystemTime),
        ("szName", ctypes.c_uint16 * 248),
    ]


class Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_uint8 * 8),
    ]


HID_SERVICE = Guid(0x00001124, 0x0000, 0x1000, (ctypes.c_uint8 * 8)(0x80, 0x00, 0x00, 0x80, 0x5F, 0x9B, 0x34, 0xFB))


def cargar_api():
    """bthprops.cpl con los tipos de cada función. Solo existe en Windows."""
    api = ctypes.WinDLL("bthprops.cpl", use_last_error=True)
    h = ctypes.c_void_p
    p = ctypes.c_void_p  # todo lo demás son punteros a las estructuras de arriba
    for nombre, res, args in (
        ("BluetoothFindFirstRadio", h, [p, p]),
        ("BluetoothFindNextRadio", ctypes.c_int32, [h, p]),
        ("BluetoothFindRadioClose", ctypes.c_int32, [h]),
        ("BluetoothGetRadioInfo", ctypes.c_uint32, [h, p]),
        ("BluetoothFindFirstDevice", h, [p, p]),
        ("BluetoothFindNextDevice", ctypes.c_int32, [h, p]),
        ("BluetoothFindDeviceClose", ctypes.c_int32, [h]),
        ("BluetoothRemoveDevice", ctypes.c_uint32, [p]),
        ("BluetoothAuthenticateDevice", ctypes.c_uint32, [h, h, p, p, ctypes.c_uint32]),
        ("BluetoothEnumerateInstalledServices", ctypes.c_uint32, [h, p, p, p]),
        ("BluetoothSetServiceState", ctypes.c_uint32, [h, p, p, ctypes.c_uint32]),
    ):
        fn = getattr(api, nombre)
        fn.restype, fn.argtypes = res, args
    return api


def ultimo_error():
    return getattr(ctypes, "get_last_error", lambda: 0)()


def cerrar_handle(handle):
    if sys.platform == "win32":
        ctypes.windll.kernel32.CloseHandle(handle)


def mac(direccion):
    """rgBytes guarda la dirección al revés de como se escribe."""
    return ":".join(f"{b:02X}" for b in reversed(direccion.rgBytes))


def nombre_de(info):
    return bytes(info.szName).decode("utf-16-le", "replace").split("\0")[0]


def es_wiimote(nombre, clase=0):
    return nombre.startswith(NOMBRES) or (not nombre and clase in CLASES)


def clave(direccion):
    """El PIN: los 6 bytes de la dirección, cada uno en su propio WCHAR.

    rgBytes ya va al revés, que es justo el orden que espera el Wiimote."""
    return (ctypes.c_uint16 * 6)(*direccion.rgBytes)


def radios(api):
    """Cada adaptador Bluetooth como (handle, RadioInfo)."""
    params = FindRadioParams(ctypes.sizeof(FindRadioParams))
    radio = ctypes.c_void_p()
    busqueda = api.BluetoothFindFirstRadio(ctypes.byref(params), ctypes.byref(radio))
    if not busqueda:
        return
    try:
        while True:
            info = RadioInfo(dwSize=ctypes.sizeof(RadioInfo))
            try:
                if api.BluetoothGetRadioInfo(radio, ctypes.byref(info)) == ERROR_SUCCESS:
                    yield radio, info
            finally:
                cerrar_handle(radio)
            if not api.BluetoothFindNextRadio(busqueda, ctypes.byref(radio)):
                break
    finally:
        api.BluetoothFindRadioClose(busqueda)


def dispositivos(api, inquiry):
    """Cada dispositivo como (radio, RadioInfo, DeviceInfo). inquiry=0 da solo los conocidos."""
    for radio, radio_info in radios(api):
        params = DeviceSearchParams(
            dwSize=ctypes.sizeof(DeviceSearchParams),
            fReturnAuthenticated=True, fReturnRemembered=True, fReturnUnknown=True, fReturnConnected=True,
            fIssueInquiry=inquiry > 0, cTimeoutMultiplier=inquiry, hRadio=radio,
        )
        dev = DeviceInfo(dwSize=ctypes.sizeof(DeviceInfo))
        busqueda = api.BluetoothFindFirstDevice(ctypes.byref(params), ctypes.byref(dev))
        if not busqueda:
            error = ultimo_error()
            if error != ERROR_NO_MORE_ITEMS:
                print(f"No se pudo buscar dispositivos (error {error})", file=sys.stderr)
            continue
        try:
            while True:
                yield radio, radio_info, dev
                if not api.BluetoothFindNextDevice(busqueda, ctypes.byref(dev)):
                    break
        finally:
            api.BluetoothFindDeviceClose(busqueda)


def wiimotes(api, inquiry):
    return (d for d in dispositivos(api, inquiry) if es_wiimote(nombre_de(d[2]), d[2].ulClassofDevice))


def diagnostico(api, rondas=RONDAS):
    """Muestra todo lo que devuelve Windows, sin emparejar nada."""
    for radio, info in radios(api):
        print(f"Adaptador: {nombre_de(info)!r} {mac(info.address)} "
              f"fabricante={info.manufacturer} lmp=0x{info.lmpSubversion:04X}")
    for ronda in range(rondas + 1):
        inquiry = INQUIRY if ronda else 0
        print(f"--- {'búsqueda ' + str(ronda) if ronda else 'ya conocidos'}")
        inicio = time.monotonic()
        total = 0
        for _, _, dev in dispositivos(api, inquiry):
            total += 1
            print(f"{mac(dev.Address)} {nombre_de(dev)!r} clase=0x{dev.ulClassofDevice:06X} "
                  f"conectado={bool(dev.fConnected)} recordado={bool(dev.fRemembered)} "
                  f"autenticado={bool(dev.fAuthenticated)} "
                  f"wiimote={es_wiimote(nombre_de(dev), dev.ulClassofDevice)}")
        print(f"{total} dispositivos en {time.monotonic() - inicio:.1f} s")


def quitar(api, condicion):
    """Borra de Windows los Wiimotes que cumplan condicion(dev). Devuelve cuántos."""
    quitados = 0
    for _, _, dev in wiimotes(api, 0):
        if condicion(dev):
            quitados += api.BluetoothRemoveDevice(ctypes.byref(dev.Address)) == ERROR_SUCCESS
    return quitados


def quitar_inservibles(api):
    """Un Wiimote recordado pero sin autenticar no se puede reconectar: hay que borrarlo."""
    return quitar(api, lambda d: d.fRemembered and not d.fConnected and not d.fAuthenticated)


def autenticar(api, radio, radio_info, dev):
    """Con el botón SYNC el Wiimote espera la dirección del PC como PIN."""
    pin = clave(radio_info.address)
    res = api.BluetoothAuthenticateDevice(None, radio, ctypes.byref(dev), pin, len(pin))
    if res != ERROR_SUCCESS:
        print(f"{mac(dev.Address)}: no aceptó el PIN (error {res})", file=sys.stderr)
        return False
    # Sin esto el Wiimote no recuerda el emparejamiento.
    servicios = ctypes.c_uint32()
    res = api.BluetoothEnumerateInstalledServices(radio, ctypes.byref(dev), ctypes.byref(servicios), None)
    return res in (ERROR_SUCCESS, ERROR_MORE_DATA)


def emparejar(api, con_pin=True, rondas=RONDAS):
    """Busca Wiimotes y activa su servicio HID. Devuelve las MAC emparejadas.

    Con 1+2 no hay PIN: basta con activar el servicio. Usar el método equivocado hace
    que Windows lo desconecte al instante."""
    quitar_inservibles(api)
    emparejados = set()
    for ronda in range(rondas):
        print(f"Buscando... ({ronda + 1}/{rondas})", flush=True)
        for radio, radio_info, dev in wiimotes(api, INQUIRY):
            if dev.fConnected:
                continue
            if con_pin and not dev.fAuthenticated:
                autenticar(api, radio, radio_info, dev)
            res = api.BluetoothSetServiceState(
                radio, ctypes.byref(dev), ctypes.byref(HID_SERVICE), BLUETOOTH_SERVICE_ENABLE)
            if res == ERROR_SUCCESS:
                emparejados.add(mac(dev.Address))
            else:
                print(f"{mac(dev.Address)}: no se pudo activar el HID (error {res})", file=sys.stderr)
        if emparejados and ronda + 1 >= RONDAS_MINIMAS:
            break
    return emparejados


def conectados(api):
    return sorted(mac(dev.Address) for _, _, dev in wiimotes(api, 0) if dev.fConnected)


def main():
    parser = argparse.ArgumentParser(description="Empareja Wiimotes en Windows sin escribir ningún PIN")
    parser.add_argument("--rapido", action="store_true", help="usar 1+2 en vez del botón rojo SYNC")
    parser.add_argument("--olvidar", action="store_true", help="quitar de Windows los Wiimotes guardados")
    parser.add_argument("--diagnostico", action="store_true", help="mostrar lo que ve Windows, sin emparejar")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.exit(1, "Esto solo funciona en Windows.\n")

    api = cargar_api()
    if not next(radios(api), None):
        parser.exit(1, "No hay ningún adaptador Bluetooth encendido.\n")
    if args.olvidar:
        print(f"Wiimotes olvidados: {quitar(api, lambda d: d.fRemembered)}")
        return
    if args.diagnostico:
        print("Pulsa el botón rojo SYNC ahora...")
        diagnostico(api)
        return

    print("Pulsa 1+2 en el Wiimote..." if args.rapido
          else "Pulsa el botón rojo SYNC, bajo la tapa de las pilas...")
    emparejados = emparejar(api, con_pin=not args.rapido)
    if not emparejados:
        if ya := conectados(api):
            parser.exit(1, f"Windows ya lo da por conectado: {', '.join(ya)}. Prueba --olvidar y reinténtalo.\n")
        parser.exit(1, "No apareció ningún Wiimote. Mantén sus LEDs parpadeando y reinténtalo.\n")

    time.sleep(2)  # Windows tarda un poco en instalar el dispositivo HID
    activos = conectados(api)
    for direccion in sorted(emparejados):
        print(f"{direccion}: {'conectado' if direccion in activos else 'emparejado, aún sin conectar'}")
    if not args.rapido:
        print("La próxima vez basta con pulsar cualquier botón.")


if __name__ == "__main__":
    main()
