#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
"""Prueba las novedades y avisos de wii_news.py y que los datos estén bien escritos.

    python3 tests/test_news.py
"""
import copy
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import wii_gamepad as wg  # noqa: E402
import wii_news as wn  # noqa: E402


def config():
    return copy.deepcopy(wg.default_config() | {"news": {"seen": "", "read": []}})


class Novedades(unittest.TestCase):
    def test_la_version_actual_tiene_novedades(self):
        self.assertEqual(wn.RELEASES[0]["version"], wn.VERSION)
        self.assertTrue(wn.RELEASES[0]["pages"])

    def test_se_muestran_una_vez(self):
        cfg = config()
        self.assertIs(wn.pending_release(cfg), wn.RELEASES[0])
        wn.mark_seen(cfg)
        self.assertIsNone(wn.pending_release(cfg))

    def test_una_version_nueva_se_vuelve_a_mostrar(self):
        cfg = config()
        cfg["news"]["seen"] = "0.1"
        self.assertIsNotNone(wn.pending_release(cfg))

    def test_avisos_leidos(self):
        cfg = config()
        notice = wn.NOTICES[0]
        self.assertIn(notice, wn.unread_notices(cfg))
        wn.mark_read(cfg, notice["id"])
        wn.mark_read(cfg, notice["id"])
        self.assertNotIn(notice, wn.unread_notices(cfg))
        self.assertEqual(cfg["news"]["read"].count(notice["id"]), 1)

    def test_ids_sin_repetir(self):
        ids = [n["id"] for n in wn.NOTICES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_los_destinos_existen_en_la_ventana(self):
        fuente = (ROOT / "wii_gamepad_adw.py").read_text()
        paginas = set(re.findall(r'name="(\w+)"\)', fuente))
        for notice in wn.NOTICES:
            kind, _, where = notice["target"].partition(":")
            self.assertEqual(kind, "preferences")
            self.assertIn(where, paginas)

    def test_config_vieja_recibe_las_opciones_nuevas(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = wg.default_config()
            for key in ("speaker", "speaker_volume", "news"):
                del cfg[key]
            path.write_text(json.dumps(cfg))
            loaded = wg.load_config(path)
        self.assertFalse(loaded["speaker"])
        self.assertEqual(loaded["news"], {"seen": "", "read": []})


if __name__ == "__main__":
    unittest.main(buffer=True)
