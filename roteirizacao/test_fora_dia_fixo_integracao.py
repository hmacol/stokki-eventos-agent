# -*- coding: utf-8 -*-
"""
A regra de data fora do dia fixo roda depois do dia fixo e antes de separar
os dedicados, no job das 18h e no incremento (dias fixos v2, Hugo 03/10).
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_fora_dia_fixo_integracao -v
"""
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import criar_rotas_diarias as crd
import incrementar_rotas as inc


class OrdemDasChamadas(unittest.TestCase):
    def _confere(self, fonte: str):
        dia_fixo = fonte.index("aplicar_regioes_dia_fixo(")
        fora = fonte.index("tratar_fora_dia_fixo(")
        dedicados = fonte.index("separar_dedicados(")
        self.assertLess(dia_fixo, fora)
        self.assertLess(fora, dedicados)
        self.assertIn("modo_teste=modo_teste", fonte[fora:dedicados])

    def test_criar_rotas_diarias(self):
        self._confere(inspect.getsource(crd.main))

    def test_incrementar_rotas(self):
        self._confere(inspect.getsource(inc.main))


if __name__ == "__main__":
    unittest.main()
