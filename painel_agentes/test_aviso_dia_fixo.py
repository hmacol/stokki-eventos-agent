# -*- coding: utf-8 -*-
"""
test_aviso_dia_fixo.py

Testes de planejamento_rotas._aviso_dia_fixo -- aviso do botão
"Roteirizar" quando um pedido selecionado é de região com dia fixo e a
data do planejamento não é um desses dias (Hugo, 23/09, caso PS-39752:
Americana, só quartas, roteirizado à mão pra uma quinta).

Rodar com (a partir da raiz do repo):
    python -m unittest painel_agentes.test_aviso_dia_fixo -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import planejamento_rotas

AMERICANA = {"address": "Rua Benedito Soares de Barros 74, Centro, Americana - SP, 13465-510, Brasil"}
TAFF = {"address": "Av. Prefeito João Vila Lobos Quero, 1505, Jardim Belval, Barueri - SP, 06422-122, Brasil"}
SAO_PAULO = {"address": "Rua Augusta 100, Consolação, São Paulo - SP, 01304-000, Brasil"}

QUARTA = date(2026, 9, 23)
QUINTA = date(2026, 9, 24)


class AvisoDiaFixoTestCase(unittest.TestCase):

    def test_regiao_fora_do_dia_avisa(self):
        self.assertEqual(planejamento_rotas._aviso_dia_fixo(AMERICANA, QUINTA), "Americana: só Quartas")

    def test_regiao_no_dia_certo_nao_avisa(self):
        self.assertIsNone(planejamento_rotas._aviso_dia_fixo(AMERICANA, QUARTA))

    def test_regra_por_endereco_usa_o_nome_do_galpao(self):
        self.assertEqual(planejamento_rotas._aviso_dia_fixo(TAFF, QUARTA), "TAFF: só Terças e Quintas")

    def test_cidade_sem_dia_fixo_nao_avisa(self):
        self.assertIsNone(planejamento_rotas._aviso_dia_fixo(SAO_PAULO, QUINTA))

    def test_sem_endereco_nao_avisa(self):
        self.assertIsNone(planejamento_rotas._aviso_dia_fixo({}, QUINTA))


if __name__ == "__main__":
    unittest.main()
