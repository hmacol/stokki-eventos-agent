# -*- coding: utf-8 -*-
"""
Deslogar por inatividade (24/10; 6 h desde 27/10): 6 h sem uso de verdade -> login.

    py -3.11 -m unittest test_sessao_inativa   (de dentro de painel_agentes/)
"""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)
import acesso_telas  # noqa: E402

ORIGEM = {"Origin": "http://localhost"}
NAVEGAR = {"Sec-Fetch-Mode": "navigate"}
FETCH = {"Sec-Fetch-Mode": "cors"}


class TestSessaoInativa(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(acesso_telas, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        painel_agentes.app.config["TESTING"] = True

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _cliente(self, nivel="total", minutos_parado=None):
        cliente = painel_agentes.app.test_client()
        with cliente.session_transaction() as s:
            s["nivel_acesso"] = nivel
            s["usuario"] = "fulano"
            if minutos_parado is not None:
                s["ultimo_uso"] = datetime.now().timestamp() - minutos_parado * 60
        return cliente

    def _ultimo_uso(self, cliente):
        with cliente.session_transaction() as s:
            return s.get("ultimo_uso")

    def test_6h01_parado_volta_pro_login_e_api_da_401(self):
        r = self._cliente(minutos_parado=361).get("/acessos", headers=NAVEGAR)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/login"))
        r = self._cliente(minutos_parado=361).get("/api/sessao/estado")
        self.assertEqual(r.status_code, 401)

    def test_5h59_parado_ainda_entra(self):
        self.assertEqual(self._cliente(minutos_parado=359).get("/acessos", headers=NAVEGAR).status_code, 200)

    def test_consulta_automatica_nao_renova(self):
        cliente = self._cliente(minutos_parado=350)
        antes = self._ultimo_uso(cliente)
        r = cliente.get("/api/sessao/estado")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(9 * 60 <= r.get_json()["restante"] <= 10 * 60)
        cliente.get("/acessos", headers=FETCH)                   # fetch() de caminho fora de /api/
        self.assertEqual(self._ultimo_uso(cliente), antes)

    def test_abrir_pagina_e_mexer_na_tela_renovam(self):
        cliente = self._cliente(minutos_parado=20)
        antes = self._ultimo_uso(cliente)
        cliente.get("/acessos", headers=NAVEGAR)
        self.assertGreater(self._ultimo_uso(cliente), antes + 60)
        cliente = self._cliente(minutos_parado=20)
        r = cliente.post("/api/sessao/atividade", headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        self.assertGreater(r.get_json()["restante"], 359 * 60)

    def test_galpao_e_expedicao_nao_caem(self):
        for nivel, url in (("galpao", "/wms"), ("expedicao", "/expedicao")):
            cliente = self._cliente(nivel=nivel, minutos_parado=600)
            self.assertNotEqual(cliente.get("/api/sessao/estado").status_code, 401, nivel)
            self.assertIsNone(cliente.get("/api/sessao/estado").get_json()["restante"], nivel)

    def test_sessao_de_antes_do_deploy_nao_cai_de_cara(self):
        cliente = self._cliente()                                # sem ultimo_uso
        self.assertEqual(cliente.get("/api/sessao/estado").status_code, 200)
        self.assertIsNotNone(self._ultimo_uso(cliente))

    def test_paginas_trazem_o_script(self):
        html = self._cliente(minutos_parado=1).get("/acessos", headers=NAVEGAR).get_data(as_text=True)
        self.assertIn("/api/sessao/atividade", html)


if __name__ == "__main__":
    unittest.main()
