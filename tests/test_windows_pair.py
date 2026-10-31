#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Prueba el emparejamiento de Windows con una API de Bluetooth falsa, sin Wiimote.
No demuestra que Windows acepte el PIN: eso solo se ve con un Wiimote de verdad.

    python3 tests/test_windows_pair.py
"""
import contextlib
import ctypes
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import wii_windows_pair as wp  # noqa: E402

PC = (0x11, 0x22, 0x33, 0x44, 0x55, 0x66)  # en orden rgBytes: 66:55:44:33:22:11
MANDO = (0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6)


class Dispositivo:
    def __init__(self, nombre="Nintendo RVL-CNT-01", direccion=MANDO, **flags):
        self.nombre, self.direccion = nombre, direccion
        self.conectado = flags.get("conectado", False)
        self.recordado = flags.get("recordado", False)
        self.autenticado = flags.get("autenticado", False)
        self.clase = flags.get("clase", 0x002504)

    def rellenar(self, info):
        info.Address.rgBytes[:] = self.direccion
        info.ulClassofDevice = self.clase
        info.fConnected, info.fRemembered, info.fAuthenticated = self.conectado, self.recordado, self.autenticado
        for i, c in enumerate(self.nombre):
            info.szName[i] = ord(c)
        info.szName[len(self.nombre)] = 0


class ApiFalsa:
    """Se comporta como bthprops.cpl y exige el PIN del botón SYNC."""

    def __init__(self, *dispositivos, set_service=wp.ERROR_SUCCESS):
        self.dispositivos = list(dispositivos)
        self.set_service = set_service
        self.pines, self.habilitados, self.quitados, self.inquiries = [], [], [], []
        self.abiertos = 0
        self.pendientes = []

    def BluetoothFindFirstRadio(self, _params, radio):
        radio._obj.value = 1
        self.abiertos += 1
        return 100

    def BluetoothFindNextRadio(self, _busqueda, _radio):
        return 0

    def BluetoothFindRadioClose(self, _busqueda):
        self.abiertos -= 1
        return 1

    def BluetoothGetRadioInfo(self, _radio, info):
        info._obj.address.rgBytes[:] = PC
        return wp.ERROR_SUCCESS

    def BluetoothFindFirstDevice(self, params, dev):
        self.inquiries.append(params._obj.cTimeoutMultiplier if params._obj.fIssueInquiry else 0)
        self.pendientes = list(self.dispositivos)
        if not self.pendientes:
            return None
        self.abiertos += 1
        self.pendientes.pop(0).rellenar(dev._obj)
        return 200

    def BluetoothFindNextDevice(self, _busqueda, dev):
        if not self.pendientes:
            return 0
        self.pendientes.pop(0).rellenar(dev._obj)
        return 1

    def BluetoothFindDeviceClose(self, _busqueda):
        self.abiertos -= 1
        return 1

    def _buscar(self, direccion):
        return next(d for d in self.dispositivos if d.direccion == tuple(direccion.rgBytes))

    def BluetoothRemoveDevice(self, direccion):
        d = self._buscar(direccion._obj)
        self.quitados.append(d)
        self.dispositivos.remove(d)
        return wp.ERROR_SUCCESS

    def BluetoothAuthenticateDevice(self, _ventana, _radio, dev, pin, largo):
        self.pines.append(tuple(pin[:largo]))
        if tuple(pin[:largo]) != PC:
            return 1244
        self._buscar(dev._obj.Address).autenticado = True
        return wp.ERROR_SUCCESS

    def BluetoothEnumerateInstalledServices(self, _radio, _dev, servicios, _guids):
        servicios._obj.value = 1
        return wp.ERROR_SUCCESS

    def BluetoothSetServiceState(self, _radio, dev, servicio, estado):
        assert servicio._obj.Data1 == 0x1124 and estado == wp.BLUETOOTH_SERVICE_ENABLE
        self.habilitados.append(wp.mac(dev._obj.Address))
        if self.set_service == wp.ERROR_SUCCESS:
            self._buscar(dev._obj.Address).conectado = True
        return self.set_service


class Estructuras(unittest.TestCase):
    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "tamaños de 64 bits")
    def test_miden_lo_mismo_que_en_windows(self):
        self.assertEqual(ctypes.sizeof(wp.RadioInfo), 520)
        self.assertEqual(ctypes.sizeof(wp.DeviceInfo), 560)
        self.assertEqual(ctypes.sizeof(wp.DeviceSearchParams), 40)
        self.assertEqual(ctypes.sizeof(wp.FindRadioParams), 4)
        self.assertEqual(ctypes.sizeof(wp.Guid), 16)


class Pin(unittest.TestCase):
    def test_ejemplo_de_wiibrew(self):
        # 11:22:33:44:55:66 da el PIN 66 55 44 33 22 11
        direccion = wp.BluetoothAddress()
        direccion.rgBytes[:] = (0x66, 0x55, 0x44, 0x33, 0x22, 0x11)
        self.assertEqual(wp.mac(direccion), "11:22:33:44:55:66")
        self.assertEqual(tuple(wp.clave(direccion)), (0x66, 0x55, 0x44, 0x33, 0x22, 0x11))

    def test_nombres(self):
        for nombre in ("Nintendo RVL-CNT-01", "Nintendo RVL-CNT-01-TR", "Nintendo RVL-WBC-01"):
            self.assertTrue(wp.es_wiimote(nombre))
        self.assertFalse(wp.es_wiimote("Xbox Wireless Controller"))

    def test_sin_nombre_se_reconoce_por_la_clase(self):
        self.assertTrue(wp.es_wiimote("", 0x002504))
        self.assertTrue(wp.es_wiimote("", 0x000508))
        self.assertFalse(wp.es_wiimote("", 0x240404))  # audífonos
        self.assertFalse(wp.es_wiimote("Mando genérico", 0x000508))

    def test_nombre_fuera_del_plano_basico(self):
        info = wp.DeviceInfo()
        info.szName[:3] = (0xD83C, 0xDFA7, 0x41)  # surrogados de 🎧 y una A
        self.assertEqual(wp.nombre_de(info), "🎧A")


class Emparejar(unittest.TestCase):
    def test_sync_usa_la_direccion_del_pc(self):
        api = ApiFalsa(Dispositivo())
        self.assertEqual(wp.emparejar(api), {"A6:A5:A4:A3:A2:A1"})
        self.assertEqual(api.pines, [PC])
        self.assertEqual(len(api.habilitados), 1)
        self.assertEqual(wp.conectados(api), ["A6:A5:A4:A3:A2:A1"])
        self.assertEqual(api.abiertos, 0)

    def test_1_2_no_usa_pin(self):
        api = ApiFalsa(Dispositivo())
        self.assertEqual(wp.emparejar(api, con_pin=False), {"A6:A5:A4:A3:A2:A1"})
        self.assertEqual(api.pines, [])

    def test_ignora_lo_que_no_es_wiimote(self):
        api = ApiFalsa(Dispositivo("Audífonos", (1, 2, 3, 4, 5, 6)))
        self.assertEqual(wp.emparejar(api), set())
        self.assertEqual(api.pines + api.habilitados, [])

    def test_no_toca_el_ya_conectado(self):
        api = ApiFalsa(Dispositivo(conectado=True, recordado=True, autenticado=True))
        self.assertEqual(wp.emparejar(api), set())
        self.assertEqual(api.habilitados, [])

    def test_ya_autenticado_no_repite_pin(self):
        api = ApiFalsa(Dispositivo(recordado=True, autenticado=True))
        self.assertEqual(wp.emparejar(api), {"A6:A5:A4:A3:A2:A1"})
        self.assertEqual(api.pines, [])

    def test_borra_el_recordado_sin_autenticar(self):
        api = ApiFalsa(Dispositivo(recordado=True))
        self.assertEqual(wp.emparejar(api), set())
        self.assertEqual(len(api.quitados), 1)

    def test_sin_wiimote_sigue_buscando_todas_las_rondas(self):
        api = ApiFalsa(Dispositivo(conectado=True))
        wp.emparejar(api)
        self.assertEqual(api.inquiries, [0] + [wp.INQUIRY] * wp.RONDAS)

    def test_al_emparejar_para_tras_las_rondas_minimas(self):
        api = ApiFalsa(Dispositivo(recordado=True, autenticado=True))
        wp.emparejar(api)
        self.assertEqual(api.inquiries, [0] + [wp.INQUIRY] * wp.RONDAS_MINIMAS)

    def test_empareja_el_que_llega_sin_nombre(self):
        api = ApiFalsa(Dispositivo(""))
        self.assertEqual(wp.emparejar(api), {"A6:A5:A4:A3:A2:A1"})
        self.assertEqual(api.pines, [PC])

    def test_no_toca_audifonos_sin_nombre(self):
        api = ApiFalsa(Dispositivo("", (1, 2, 3, 4, 5, 6), clase=0x240404))
        self.assertEqual(wp.emparejar(api), set())
        self.assertEqual(api.pines + api.habilitados, [])

    def test_si_falla_el_hid_no_lo_cuenta(self):
        api = ApiFalsa(Dispositivo(), set_service=87)
        self.assertEqual(wp.emparejar(api), set())
        self.assertEqual(len(api.habilitados), wp.RONDAS)

    def test_diagnostico_muestra_todo_y_no_empareja(self):
        api = ApiFalsa(Dispositivo(), Dispositivo("Audífonos", (1, 2, 3, 4, 5, 6)))
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            wp.diagnostico(api)
        self.assertIn("'Nintendo RVL-CNT-01'", salida.getvalue())
        self.assertIn("'Audífonos'", salida.getvalue())
        self.assertIn("wiimote=False", salida.getvalue())
        self.assertEqual(api.inquiries, [0] + [wp.INQUIRY] * wp.RONDAS)
        self.assertEqual(api.pines + api.habilitados, [])

    def test_varios_wiimotes(self):
        otro = (0xB1, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6)
        api = ApiFalsa(Dispositivo(), Dispositivo("Nintendo RVL-CNT-01-TR", otro))
        self.assertEqual(wp.emparejar(api), {"A6:A5:A4:A3:A2:A1", "B6:B5:B4:B3:B2:B1"})


if __name__ == "__main__":
    unittest.main(buffer=True)
