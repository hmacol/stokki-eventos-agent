# -*- coding: utf-8 -*-
"""
test_embarcadores.py

embarcador_prioritario decide pelo id #stkkc-NN da linha (achado 28/09:
o casamento por pedaço de nome descartava embarcador de nome parecido da
Fonte 2 do pipeline, e ele sumia da importação).

Rodar (da raiz):
    python -m unittest regras.test_embarcadores -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from regras.embarcadores import embarcador_prioritario

PRIORITARIOS = {
    "98": "COMERCIO DE CEREAIS QUATRO ESTRELAS LTDA",
    "18": "LATICINIOS DOURADO - INDUSTRIA E COMERCIO LTDA",
    "79": "JERSEY VALE AGROINDUSTRIAL LTDA",
}


class TestEmbarcadorPrioritario(unittest.TestCase):
    def test_id_prioritario(self):
        linha = {"client": "<a href='#'>LATICINIOS DOURADO - IND ...</a> #stkkc-18"}
        self.assertTrue(embarcador_prioritario(linha, PRIORITARIOS))

    def test_nome_parecido_com_outro_id_nao_e_prioritario(self):
        linha = {"client": "<a>JERSEY VALE AGROINDUSTRIAL LTDA FILIAL</a> #stkkc-140"}
        self.assertFalse(embarcador_prioritario(linha, PRIORITARIOS))
        linha = {"client": "<a>LATICINIOS</a> #stkkc-55"}
        self.assertFalse(embarcador_prioritario(linha, PRIORITARIOS))

    def test_sem_id_cai_no_nome(self):
        self.assertTrue(embarcador_prioritario("Jersey Vale Agroindustrial Ltda", PRIORITARIOS))
        self.assertFalse(embarcador_prioritario("OUTRO EMBARCADOR", PRIORITARIOS))

    def test_vazio(self):
        self.assertFalse(embarcador_prioritario({"client": ""}, PRIORITARIOS))


if __name__ == "__main__":
    unittest.main()
