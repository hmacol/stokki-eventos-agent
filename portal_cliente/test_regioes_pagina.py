# -*- coding: utf-8 -*-
"""
Rota pública /regioes do portal (sem sessão). Precisa do config.yaml local
(portal_cliente.secret_key), como os outros testes que importam app.
    cd portal_cliente && py -3.11 -m unittest test_regioes_pagina -v
"""
import os
import sys
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
for _p in (_AQUI.parent, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
os.environ.setdefault("PORTAL_CLIENTE_DEV", "1")

import app as portal  # noqa: E402


class TestPaginaRegioes(unittest.TestCase):
    def setUp(self):
        self.tc = portal.app.test_client()

    def test_abre_sem_login_com_regioes_e_calendario(self):
        r = self.tc.get("/regioes")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        for trecho in ("Dias de atendimento por região", "ABCD", "Sorocaba", "TAFF", "Grande São Paulo", "/regioes/logo.png"):
            self.assertIn(trecho, html)
        self.assertIn("max-age=900", r.headers.get("Cache-Control", ""))

    def test_mes_na_url_e_lixo_cai_no_mes_atual(self):
        self.assertEqual(self.tc.get("/regioes?mes=2026-13").status_code, 200)
        self.assertEqual(self.tc.get("/regioes?mes=abc").status_code, 200)

    def test_logo(self):
        r = self.tc.get("/regioes/logo.png")
        self.assertEqual((r.status_code, r.mimetype), (200, "image/png"))

    def test_prefixo_do_caddy_nos_links(self):
        r = self.tc.get("/regioes", headers={"X-Forwarded-Prefix": "/cliente"})
        self.assertIn('src="/cliente/regioes/logo.png"', r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
