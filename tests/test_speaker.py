#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Prueba los bytes de wii_speaker_test.py sin Wiimote. No demuestra que suene.

    python3 tests/test_speaker.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import wii_speaker_test as ws  # noqa: E402
import wii_gamepad as wg  # noqa: E402


class Falso(ws.Wiimote):
    """Contesta por una tubería como el Wiimote: un ACK por registro y el estado."""

    def __init__(self, error=0, activa=True, mudo=False):
        self.lectura, self.salida = os.pipe()
        super().__init__("/dev/falso", "AA", "RVL", fd=self.lectura)
        self.enviados, self.error, self.activa, self.mudo = [], error, activa, mudo

    def escribir(self, datos):
        self.enviados.append(bytes(datos))
        if self.mudo:
            return
        if datos[0] == ws.WMEM or datos[0] in (ws.ENABLE, ws.MUTE) and datos[1] & ws.PEDIR_ACK:
            os.write(self.salida, bytes((ws.ACK, 0, 0, datos[0], self.error)))
        elif datos[0] == ws.STATUS:
            os.write(self.salida, bytes((ws.ESTADO, 0, 0, 0x04 if self.activa else 0, 0, 0, 200)))


def decodificar(datos):
    predictor, paso, salida = 0, 127, []
    for byte in datos:
        for nibble in (byte >> 4, byte & 0xF):
            predictor = max(-32768, min(32767, predictor + int(paso * ws.DIFF[nibble] / 8)))
            paso = max(127, min(24576, (paso * ws.ESCALA[nibble]) >> 8))
            salida.append(predictor / 32768)
    return salida


class Bytes(unittest.TestCase):
    def test_escritura_de_registro(self):
        r = ws.reporte_wmem(0xA20009, b"\x01")
        self.assertEqual(r[:7], bytes((0x16, 0x04, 0xA2, 0x00, 0x09, 0x01, 0x01)))
        self.assertEqual(len(r), 22)

    def test_nunca_escribe_en_eeprom(self):
        self.assertTrue(ws.reporte_wmem(0, b"\0")[1] & 0x04)

    def test_datos_llevan_el_largo_desplazado(self):
        r = ws.reporte_datos(bytes(range(20)))
        self.assertEqual(r[:3], bytes((0x18, 20 << 3, 0)))
        self.assertEqual(len(r), 22)
        self.assertEqual(ws.reporte_datos(b"\x01")[1], 1 << 3)

    def test_configuracion_de_wiibrew(self):
        self.assertEqual(ws.configuracion("pcm", 0x60).hex(), "00407017600000")
        self.assertEqual(ws.configuracion("adpcm", 0x40).hex(), "0000d007400000")

    def test_secuencia_de_encendido(self):
        w = Falso()
        w.encender("pcm", 0x60)
        cabeceras = [(r[0], r[1]) if r[0] != ws.WMEM else (r[0], int.from_bytes(r[2:5], "big")) for r in w.enviados]
        self.assertEqual(cabeceras, [(0x14, 6), (0x19, 6), (0x16, 0xA20009), (0x16, 0xA20001),
                                     (0x16, 0xA20001), (0x16, 0xA20008), (0x19, 2)])
        self.assertEqual(w.ack_fallidos, [])

    def test_apagar_silencia_y_desactiva(self):
        w = Falso()
        w.apagar()
        self.assertEqual(w.enviados, [bytes((0x19, 4)), bytes((0x14, 0))])

    def test_ritmos(self):
        for formato, (_, _, _, hz, muestras, periodo) in ws.FORMATOS.items():
            self.assertAlmostEqual(periodo, muestras / hz)
            n = len(ws.paquetes(formato))
            self.assertAlmostEqual(n * periodo, len(ws.NOTAS) * ws.SEGUNDOS_NOTA, delta=periodo * 2)


class Audio(unittest.TestCase):
    def test_pcm_con_signo(self):
        self.assertEqual(ws.pcm([0, 1, -1]), bytes((0, 127, 0x81)))

    def test_adpcm_ida_y_vuelta(self):
        original = ws.tono((440,), 3000, 0.5)
        original = original[:len(original) // 2 * 2]
        decodificado = decodificar(ws.adpcm(original))
        suma = sum(a * b for a, b in zip(original, decodificado))
        energia = (sum(a * a for a in original) * sum(b * b for b in decodificado)) ** 0.5
        self.assertGreater(suma / energia, 0.95)


class Mandos(unittest.TestCase):
    def test_buscar_solo_wiimotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            for nombre, hid, uniq in (("hidraw0", "0003:00000458:00006022", "x"),
                                      ("hidraw3", "0005:0000057E:00000306", "66:77:88:99:aa:bb"),
                                      ("hidraw4", "0005:0000057E:00000330", "00:11:22:33:44:55")):
                (raiz / nombre / "device").mkdir(parents=True)
                (raiz / nombre / "device/uevent").write_text(f"HID_ID={hid}\nHID_UNIQ={uniq}\n")
            self.assertEqual(ws.buscar(raiz), [("/dev/hidraw3", "66:77:88:99:AA:BB", "RVL-CNT-01"),
                                               ("/dev/hidraw4", "00:11:22:33:44:55", "RVL-CNT-01-TR")])

    def test_estado_lee_bocina_y_bateria(self):
        self.assertEqual(Falso().estado(), (True, 200))
        self.assertEqual(Falso(activa=False).estado(), (False, 200))

    def test_error_del_mando_se_reporta(self):
        w = Falso(error=4)
        w.encender("pcm", 1)
        self.assertEqual(len(w.ack_fallidos), 7)

    def test_mando_que_no_contesta(self):
        w = Falso(mudo=True)
        w.esperar = lambda *_a, **_k: None
        w.encender("pcm", 1)
        self.assertEqual([e for _, e in w.ack_fallidos], [None] * 7)
        self.assertIsNone(w.estado())

    def test_reproducir_dos_a_la_vez(self):
        a, b = Falso(), Falso()
        tiempo = [0.0]
        tarde = ws.reproducir([(a, [b"1", b"2"]), (b, [b"3"])], 0.01, lambda: tiempo[0],
                              lambda s: tiempo.__setitem__(0, tiempo[0] + s))
        self.assertEqual((a.enviados, b.enviados, tarde), ([b"1", b"2"], [b"3"], 0))

    def test_cuenta_bloques_tarde(self):
        w = Falso()
        tiempo = iter((0.0, 0.0, 0.05, 0.1))
        self.assertEqual(ws.reproducir([(w, [b"1", b"2", b"3"])], 0.01, lambda: next(tiempo), lambda s: None), 2)


class Avisos(unittest.TestCase):
    class Jugador:
        speak = wg.Player.speak

        def __init__(self, grupo):
            self.group = grupo
            self.bridge = type("B", (), {"speaker_volume": 100})()

    def test_ruta_del_wiimote(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "hidraw/hidraw7").mkdir(parents=True)
            self.assertEqual(ws.ruta_de(tmp), "/dev/hidraw7")
        self.assertIsNone(ws.ruta_de("phys:falso"))

    def test_las_melodias_son_de_bloques_completos(self):
        for evento in ws.MELODIAS:
            paquetes = ws.paquetes_melodia(evento)
            self.assertTrue(paquetes)
            self.assertTrue(all(p[1] == ws.BLOQUE << 3 for p in paquetes), evento)

    def test_cada_aviso_con_jugador_tiene_melodia(self):
        for evento in wg.SOUND_EVENTS:
            if evento != "disconnect":  # ya no hay Wiimote donde sonar
                self.assertIn(evento, ws.MELODIAS)

    def test_cada_sonido_incluido_tiene_su_conversion(self):
        for sonido in (ROOT / "sonidos").glob("*.ogg"):
            paquetes = ws.paquetes_sonido(sonido.name)
            self.assertTrue(paquetes, sonido.name)
            self.assertLessEqual(len(paquetes), 1.5 / ws.FORMATOS["pcm"][5] + 1)
            self.assertTrue(all(p[1] == ws.BLOQUE << 3 for p in paquetes))

    def test_elige_melodia_o_sonido_de_la_pc(self):
        cfg = {"sounds": {"connect": "a.ogg"}, "speaker_sounds": "melodies"}
        self.assertIsNone(ws.sonido_de(cfg, "connect"))
        cfg["speaker_sounds"] = "pc"
        self.assertEqual(ws.sonido_de(cfg, "connect"), "a.ogg")

    def test_un_sonido_sin_conversion_usa_la_melodia(self):
        self.assertIsNone(ws.paquetes_sonido("mio.mp3"))
        w = Falso()
        ws.tocar(w, "recenter", 100, "mio.mp3")
        self.assertEqual(sum(r[0] == ws.DATA for r in w.enviados), len(ws.paquetes_melodia("recenter")))

    def test_volumen(self):
        self.assertEqual((ws.volumen_pcm(100), ws.volumen_pcm(50), ws.volumen_pcm(0)), (255, 128, 0))

    def test_tocar_enciende_suena_y_apaga(self):
        w = Falso()
        ws.tocar(w, "recenter", 100)
        self.assertEqual(w.enviados[-2:], [bytes((0x19, 4)), bytes((0x14, 0))])
        self.assertIn(0x18, [r[0] for r in w.enviados])
        self.assertIn(255, next(r for r in w.enviados if r[0] == ws.WMEM and r[5] == 7)[6:])

    def test_sin_permiso_avisa_para_sonar_en_la_pc(self):
        self.assertFalse(ws.sonar("/dev/no-existe", "prueba", 100))

    def test_sin_bocina_no_falla(self):
        self.assertFalse(self.Jugador("phys:falso").speak("connect"))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(self.Jugador(tmp).speak("connect"))

    def test_los_avisos_suenan_en_la_pc_y_en_la_bocina(self):
        puente = wg.Bridge.__new__(wg.Bridge)
        puente.notifications, puente.sounds = {}, {"connect": "a.ogg"}
        pc, bocina = [], []
        original, wg.play_on_pc = wg.play_on_pc, lambda *a: pc.append(a)
        self.addCleanup(setattr, wg, "play_on_pc", original)
        jugador = type("J", (), {"speak": lambda _s, e: bocina.append(e)})()
        puente.speaker_volume = None
        puente.notify("connect", player=jugador)
        self.assertEqual((len(pc), len(bocina)), (1, 0))
        puente.speaker_volume = 100
        puente.notify("connect", player=jugador)
        self.assertEqual((len(pc), bocina), (2, ["connect"]))
        puente.notify("connect")  # sin Wiimote, como al desconectarse
        self.assertEqual((len(pc), len(bocina)), (3, 1))
        puente.sounds = {"connect": ""}  # «Ninguno» calla los dos
        puente.notify("connect", player=jugador)
        self.assertEqual(len(bocina), 1)


if __name__ == "__main__":
    unittest.main()
