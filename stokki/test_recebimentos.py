# -*- coding: utf-8 -*-
"""
test_recebimentos.py

Parser dos recebimentos da Stokki (incoming), sobre uma fixture
SINTETICA -- Hugo proibiu conectar na Stokki desta maquina (derruba a
sessao de producao). O HTML de stokki/fixtures/recebimento_itens.html
reproduz a estrutura real que ele levantou ao vivo em 22/09/2026: duas
tabelas no detalhe, cabecalhos exatos, uma linha com lote e validade
preenchidos e uma com validade vazia.

Rodar (da raiz):
    py -3.11 -m unittest stokki.test_recebimentos -v
"""
import sys
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from stokki.recebimentos import (  # noqa: E402
    extrair_itens_do_recebimento,
    extrair_id_da_linha,
    extrair_codigo_da_linha,
    extrair_cabecalho_da_linha,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "recebimento_itens.html"

# HTML da armadilha descrita pelo Hugo: a coluna 'id' tem o link com o id
# que abre o detalhe (2478) e um numero solto (41099) que NAO serve --
# incoming/show/41099 devolve 500 na Stokki real.
_LINHA_ARMADILHA = {
    "id": ('<a href="https://freshlog.stokki.com.br/pt-br/administrator/inventory/incoming/show/2478">'
           '#PE-2478</a><br><span class="text-muted">41099</span>'),
    "arrival_date": "18/09/2026",
    "client": "MARIA DOLORES IND E COM ... <span class=\"text-muted\">#stkkc-48</span>",
    "state": "Recebido",
}


class TestExtrairItensDoRecebimento(unittest.TestCase):
    def setUp(self):
        self.html = _FIXTURE.read_text(encoding="utf-8")
        self.itens = extrair_itens_do_recebimento(self.html)

    def test_le_itens_do_recebimento(self):
        self.assertTrue(self.itens)
        self.assertIn("sku", self.itens[0])
        self.assertIn("qtd_embalagem", self.itens[0])

    def test_le_as_duas_linhas_da_tabela_certa_e_nao_a_de_localizacao(self):
        # a fixture tem uma tabela "NR/ID/SKU/.../Localizacao/.../Valor
        # Unitario" antes -- se o parser pegasse ela por engano, contaria
        # so 1 linha (a de localizacao) em vez de 2.
        self.assertEqual(len(self.itens), 2)

    def test_separa_sku_da_descricao(self):
        self.assertEqual(self.itens[0]["sku"], "CXPTD1")
        self.assertEqual(self.itens[0]["descricao"], "CX PALITINHO DE TAPIOCA TRAD")

    def test_nao_tem_coluna_ean_nesta_tabela(self):
        self.assertEqual(self.itens[0]["ean_linha"], "")

    def test_lote_e_validade_preenchidos_viram_sugestao(self):
        self.assertEqual(self.itens[0]["lote"], "1809")
        self.assertEqual(self.itens[0]["validade"], "18/02/2027")
        self.assertEqual(self.itens[0]["qtd_embalagem"], 10.0)

    def test_validade_vazia_fica_string_vazia_sem_quebrar(self):
        self.assertEqual(self.itens[1]["lote"], "1810")
        self.assertEqual(self.itens[1]["validade"], "")

    def test_linha_e_1_based_e_estavel(self):
        self.assertEqual([i["linha"] for i in self.itens], [1, 2])

    def test_recebimento_sem_itens_devolve_lista_vazia(self):
        self.assertEqual(extrair_itens_do_recebimento("<html></html>"), [])

    def test_recebimento_em_transito_sem_tabela_devolve_lista_vazia(self):
        # caso real visto pelo Hugo: recebimento "Em transito" ainda sem a
        # tabela de itens -- nao pode quebrar, so devolver vazio.
        html_em_transito = "<html><body><p>Em transito</p></body></html>"
        self.assertEqual(extrair_itens_do_recebimento(html_em_transito), [])


class TestLinhaDaListagem(unittest.TestCase):
    def test_extrai_o_id_do_href_e_nao_o_numero_solto(self):
        # a armadilha: 2478 (do href) e o id certo; 41099 (numero solto,
        # sem link) devolve 500 se usado.
        self.assertEqual(extrair_id_da_linha(_LINHA_ARMADILHA), 2478)

    def test_extrai_o_codigo_pe(self):
        self.assertEqual(extrair_codigo_da_linha(_LINHA_ARMADILHA), "#PE-2478")

    def test_linha_sem_id_devolve_none(self):
        self.assertIsNone(extrair_id_da_linha({"id": "nada aqui"}))

    def test_cabecalho_da_linha_junta_os_campos(self):
        dados = extrair_cabecalho_da_linha(_LINHA_ARMADILHA)
        self.assertEqual(dados["id_stokki"], 2478)
        self.assertEqual(dados["codigo"], "#PE-2478")
        self.assertIn("MARIA DOLORES", dados["embarcador"])
        self.assertEqual(dados["situacao"], "Recebido")
        self.assertEqual(dados["chegada"], "18/09/2026")


if __name__ == "__main__":
    unittest.main()
