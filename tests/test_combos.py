#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Prueba las combinaciones del modo mouse sin Wiimote ni uinput.

    python3 tests/test_combos.py
"""
import copy
import math
import sys
import unittest
from pathlib import Path

from evdev import ecodes as e

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import wii_gamepad as wg  # noqa: E402


def combos(*items):
    return [(frozenset(b.split("+")), a) for b, a in items]


class Registro:
    def __init__(self, *items):
        self.log = []
        self.chords = wg.Chords(combos(*items), lambda s, on: self.log.append((s, on)),
                                lambda a, on: self.log.append((a, on)), lambda: self.log.append("libre"))

    def toque(self, src, now=0):
        self.chords.press(src, now)
        self.chords.release(src)


class Chords(unittest.TestCase):
    def test_boton_fuera_de_combinaciones_actua_al_instante(self):
        r = Registro(("C+Right", "alt_tab"))
        r.chords.press("A", 0)
        self.assertEqual(r.log, [("A", True)])
        r.chords.release("A")
        self.assertEqual(r.log[-1], ("A", False))

    def test_c_solo_actua_al_soltarlo(self):
        r = Registro(("C+Right", "alt_tab"))
        r.chords.press("C", 0)
        r.chords.tick(100)
        self.assertEqual(r.log, [])
        r.chords.release("C")
        self.assertEqual(r.log, [("C", True), ("C", False)])

    def test_la_combinacion_no_dispara_la_funcion_normal(self):
        r = Registro(("C+Right", "alt_tab"))
        r.chords.press("C", 0)
        r.toque("Right")
        r.chords.release("C")
        self.assertEqual(r.log, [("alt_tab", True), ("alt_tab", False), "libre"])

    def test_mantener_c_y_repetir(self):
        r = Registro(("C+Right", "alt_tab"))
        r.chords.press("C", 0)
        r.toque("Right")
        r.toque("Right")
        self.assertEqual(r.log.count(("alt_tab", True)), 2)
        self.assertNotIn("libre", r.log)

    def test_c_con_un_boton_sin_combinacion_no_manda_enter(self):
        r = Registro(("C+Right", "alt_tab"))
        r.chords.press("C", 0)
        r.toque("A")
        r.chords.release("C")
        self.assertEqual(r.log, [("A", True), ("A", False), "libre"])

    def test_con_una_combinacion_mas_larga_espera_a_soltar(self):
        r = Registro(("C+Z", "play"), ("C+Z+B", "browser_close"))
        r.chords.press("C", 0)
        r.chords.press("Z", 0)
        self.assertEqual(r.log, [])
        r.chords.release("Z")
        r.chords.release("C")
        self.assertEqual(r.log, [("play", True), ("play", False), "libre"])

    def test_gana_la_combinacion_mas_larga(self):
        r = Registro(("C+Z", "play"), ("C+Z+B", "browser_close"))
        for s in ("C", "Z", "B"):
            r.chords.press(s, 0)
        for s in ("B", "Z", "C"):
            r.chords.release(s)
        self.assertEqual(r.log[:2], [("browser_close", True), ("browser_close", False)])
        self.assertNotIn(("play", True), r.log)

    def test_cuatro_botones(self):
        r = Registro(("C+Z+B+A", "close"))
        for s in ("C", "Z", "B", "A"):
            r.chords.press(s, 0)
        self.assertEqual(r.log, [("close", True)])

    def test_wiimote_casi_a_la_vez(self):
        r = Registro(("A+B", "reload"))
        r.chords.press("A", 0)
        r.chords.press("B", 0.05)
        self.assertEqual(r.log, [("reload", True)])

    def test_wiimote_mantenido_actua_solo_y_permite_arrastrar(self):
        r = Registro(("A+B", "reload"))
        r.chords.press("A", 0)
        self.assertEqual(r.chords.tick(0.05), wg.Chords.WINDOW - 0.05)
        self.assertEqual(r.chords.tick(wg.Chords.WINDOW), math.inf)
        self.assertEqual(r.log, [("A", True)])
        r.chords.release("A")
        self.assertEqual(r.log[-1], ("A", False))

    def test_toque_rapido_del_wiimote_no_se_pierde(self):
        r = Registro(("A+B", "reload"))
        r.toque("A")
        self.assertEqual(r.log, [("A", True), ("A", False)])

    def test_a_ya_apretado_se_suelta_al_formar_combinacion(self):
        r = Registro(("C+A", "paste"))
        r.chords.press("A", 0)
        r.chords.press("C", 0.01)
        self.assertEqual(r.log, [("A", True), ("A", False), ("paste", True)])

    def test_un_boton_del_wiimote_no_espera_a_c_si_c_no_esta_apretado(self):
        r = Registro(("C+A", "paste"))
        r.chords.press("A", 0)
        self.assertEqual(r.log, [("A", True)])
        self.assertEqual(r.chords.waiting, {})

    def test_olvidar_botones_de_una_combinacion_del_sistema(self):
        r = Registro(("C+B", "close"))
        r.chords.press("C", 0)
        r.chords.forget({"C"})
        r.chords.release("C")
        self.assertEqual(r.log, [])


class Teclas(unittest.TestCase):
    class Salida:
        def __init__(self):
            self.log = []

        def write(self, _tipo, code, value):
            self.log.append((code, value))

        def syn(self):
            pass

    def setUp(self):
        self.mouse = wg.Mouse.__new__(wg.Mouse)
        self.mouse.ui = self.Salida()
        self.mouse.held, self.mouse.wheel_next, self.mouse.switch_mods = {}, {}, []
        self.mouse.velocity, self.mouse.frac = (0.0, 0.0), [0.0, 0.0]

    def test_alt_sigue_apretado_entre_tabs(self):
        self.mouse.switch("alt_tab")
        self.mouse.switch("alt_tab")
        self.assertEqual(self.mouse.ui.log, [(e.KEY_LEFTALT, 1), (e.KEY_TAB, 1), (e.KEY_TAB, 0),
                                             (e.KEY_TAB, 1), (e.KEY_TAB, 0)])
        self.assertTrue(self.mouse.busy())
        self.mouse.release_switch()
        self.assertEqual(self.mouse.ui.log[-1], (e.KEY_LEFTALT, 0))
        self.assertFalse(self.mouse.busy())

    def test_volver_atras_solo_cambia_shift(self):
        self.mouse.switch("alt_tab")
        self.mouse.switch("alt_tab_prev")
        self.mouse.switch("alt_tab")
        log = self.mouse.ui.log
        self.assertEqual(log.count((e.KEY_LEFTALT, 1)), 1)
        self.assertEqual(log.count((e.KEY_LEFTSHIFT, 1)), 1)
        self.assertEqual(log.count((e.KEY_LEFTSHIFT, 0)), 1)

    def test_reset_no_deja_alt_pegado(self):
        self.mouse.switch("tab_next")
        self.mouse.reset()
        self.assertEqual(self.mouse.ui.log[-1], (e.KEY_LEFTCTRL, 0))


class Puente(unittest.TestCase):
    """De handle_button a las teclas, con un Mouse real sobre una salida falsa."""

    def setUp(self):
        mouse = wg.Mouse.__new__(wg.Mouse)
        mouse.ui = Teclas.Salida()
        mouse.held, mouse.wheel_next, mouse.switch_mods = {}, {}, []
        mouse.velocity, mouse.frac = (0.0, 0.0), [0.0, 0.0]
        cfg = wg.default_config()["mouse"]
        self.kb = type("Kb", (), {"visible": False, "button": lambda *_: True})()
        self.bridge = type("B", (), dict(mouse=mouse, mouse_cfg=cfg, kb=self.kb, combo=(), mouse_combo=("+", "-"),
                                         combos=wg.parse_combos(cfg["combos"])))()
        self.bridge.toggle_mouse = lambda _p: None
        self.player = wg.Player.__new__(wg.Player)
        self.player.bridge, self.player.extras = self.bridge, {wg.NUNCHUK: None}
        self.player.down, self.player.suppressed = set(), set()
        self.player.pad = type("Pad", (), {"press": lambda *_: None})()
        self.player.profile = {"buttons": {}}
        self.player.chords = wg.Chords(self.bridge.combos, self.player.mouse_button, self.player.mouse_combo_action,
                                       mouse.release_switch)
        self.bridge.mouse_owner = self.player
        self.log = mouse.ui.log

    def boton(self, src, value):
        self.player.handle_button(wg.SOURCES[src][0], value)

    def test_c_y_cruceta_cambia_de_ventana(self):
        self.boton("C", 1)
        self.boton("Right", 1), self.boton("Right", 0)
        self.boton("Right", 1), self.boton("Right", 0)
        self.boton("C", 0)
        self.assertEqual(self.log, [(e.KEY_LEFTALT, 1), (e.KEY_TAB, 1), (e.KEY_TAB, 0), (e.KEY_TAB, 1),
                                    (e.KEY_TAB, 0), (e.KEY_LEFTALT, 0)])

    def test_c_solo_sigue_siendo_enter(self):
        self.boton("C", 1), self.boton("C", 0)
        self.assertEqual(self.log, [(e.KEY_ENTER, 1), (e.KEY_ENTER, 0)])

    def test_z_y_b_cierra_la_pestana(self):
        self.boton("Z", 1), self.boton("B", 1)
        self.assertEqual(self.log, [])  # todavía puede ser C + Z + B
        self.boton("B", 0), self.boton("Z", 0)
        self.assertEqual(self.log, [(e.KEY_LEFTCTRL, 1), (e.KEY_W, 1), (e.KEY_W, 0), (e.KEY_LEFTCTRL, 0)])

    def test_quitar_el_nunchuk_no_deja_alt_pegado(self):
        nunchuk = type("D", (), {"fd": 7, "path": "/dev/falso", "close": lambda _s: None})()
        self.player.extras = {wg.NUNCHUK: nunchuk}
        self.player.devices, self.player.stale = {7: nunchuk}, set()
        self.player.pad.reset = lambda: None
        self.boton("C", 1), self.boton("Right", 1), self.boton("Right", 0)
        self.player.detach(wg.NUNCHUK, True)
        self.assertEqual(self.log[-1], (e.KEY_LEFTALT, 0))
        self.assertEqual(self.player.down, set())
        self.assertFalse(self.bridge.mouse.busy())

    def test_a_es_clic_al_instante_aunque_este_en_una_combinacion(self):
        self.boton("A", 1)
        self.assertEqual(self.log, [(e.BTN_LEFT, 1)])

    def test_el_teclado_en_pantalla_no_pasa_por_las_combinaciones(self):
        self.kb.visible = True
        self.boton("C", 1)
        self.assertEqual(self.player.chords.waiting, {})


class Configuracion(unittest.TestCase):
    def test_las_combinaciones_por_defecto_son_validas_y_no_chocan(self):
        cfg = wg.default_config()
        parsed = wg.parse_combos(cfg["mouse"]["combos"])
        self.assertEqual(len(parsed), len(cfg["mouse"]["combos"]))
        vacio = copy.deepcopy(cfg)
        vacio["mouse"]["combos"] = []
        for buttons, _ in parsed:
            self.assertIsNone(wg.combo_problem(vacio, buttons))

    def test_descarta_lo_invalido(self):
        items = [{"buttons": ["C"], "action": "close"}, {"buttons": ["C", "X"], "action": "close"},
                 {"buttons": ["C", "A"], "action": "nada"}, {"buttons": ["C", "A"], "action": "none"},
                 {"buttons": list("ABCZ") + ["1"], "action": "close"}, "basura", {"buttons": 3, "action": "close"}]
        self.assertEqual(wg.parse_combos(items), [])
        self.assertEqual(wg.parse_combos("basura"), [])

    def test_la_primera_repetida_gana(self):
        items = [{"buttons": ["C", "A"], "action": "copy"}, {"buttons": ["A", "C"], "action": "paste"}]
        self.assertEqual(wg.parse_combos(items), [(frozenset("CA"), "copy")])

    def test_problemas_al_anadir(self):
        cfg = wg.default_config()
        self.assertIn("2 a 4", wg.combo_problem(cfg, ["C"]))
        self.assertIn("2 a 4", wg.combo_problem(cfg, ["C", "Z", "A", "B", "1"]))
        self.assertIn("ya existe", wg.combo_problem(cfg, ["Right", "C"]))
        self.assertIn("Choca", wg.combo_problem(cfg, ["+", "-"]))
        self.assertIn("Choca", wg.combo_problem(cfg, ["C", "B", "Down"]))
        self.assertIsNone(wg.combo_problem(cfg, ["Z", "1"]))

    def test_una_config_vieja_recibe_las_combinaciones(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = wg.default_config()
            del cfg["mouse"]["combos"]
            path.write_text(json.dumps(cfg))
            self.assertEqual(wg.load_config(path)["mouse"]["combos"], wg.MOUSE_DEFAULTS["combos"])

    def test_etiqueta(self):
        self.assertEqual(wg.combo_label(["Right", "C"]), "Cruceta → + C")


if __name__ == "__main__":
    unittest.main(buffer=True)
