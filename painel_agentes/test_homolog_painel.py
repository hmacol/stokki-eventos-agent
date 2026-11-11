# -*- coding: utf-8 -*-
"""Producao (sem FRESH_AMBIENTE) nao muda: sem rotas e sem faixa.

    py -3.11 -m unittest test_homolog_painel   (de dentro de painel_agentes/)
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.pop("FRESH_AMBIENTE", None)
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)


class TestProducaoIntacta(unittest.TestCase):
    def test_producao_sem_variavel_nao_tem_rotas_nem_faixa(self):
        c = painel_agentes.app.test_client()
        self.assertEqual(c.get("/homologacao/saude").status_code, 404)
        self.assertNotIn("faixa-homologacao", c.get("/login").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
