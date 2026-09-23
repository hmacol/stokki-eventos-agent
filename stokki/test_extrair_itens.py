# -*- coding: utf-8 -*-
"""
test_extrair_itens.py

Parser da aba "Itens do pedido" da Stokki, sobre HTML real do PS-39751.
Rodar (da raiz):
    py -3.11 -m unittest stokki.test_extrair_itens -v
"""
import sys
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from stokki.pedidos import extrair_itens_do_pedido  # noqa: E402

_FIXTURE = Path(__file__).parent / "fixtures" / "pedido_39751_itens.html"

# HTML minimo so pra testar a quantidade com virgula decimal -- a fixture
# real (capturada do PS-39751) nao tem nenhuma linha assim.
_HTML_QTD_COM_VIRGULA = """
<table class="table">
  <thead>
    <tr><th>Produto</th><th>EAN</th><th>Quantidade</th></tr>
  </thead>
  <tbody>
    <tr>
      <td>72400044 - PASTELZINHO DE PRESUNTO/QUEIJO FESTA ZC 5KG PCT</td>
      <td>724000444</td>
      <td>2,00</td>
    </tr>
  </tbody>
</table>
"""


# Primeira linha com a celula de quantidade ilegivel -- o parser pula a
# linha de proposito, mas nao pode renumerar as de baixo (I3).
_HTML_LINHA_ILEGIVEL = """
<table class="table">
  <thead>
    <tr><th>Produto</th><th>EAN</th><th>Quantidade</th></tr>
  </thead>
  <tbody>
    <tr><td>SKU-A - PRODUTO A</td><td>111111111111</td><td>-</td></tr>
    <tr><td>SKU-B - PRODUTO B</td><td>222222222222</td><td>3</td></tr>
    <tr><td>SKU-C - PRODUTO C</td><td>333333333333</td><td>5</td></tr>
  </tbody>
</table>
"""


class TestExtrairItens(unittest.TestCase):
    def setUp(self):
        self.html = _FIXTURE.read_text(encoding="utf-8")
        self.itens = extrair_itens_do_pedido(self.html)

    def test_le_todas_as_linhas(self):
        # Fixture real do PS-39751 tem 9 linhas de produto.
        self.assertEqual(len(self.itens), 9)

    def test_separa_sku_da_descricao(self):
        self.assertEqual(self.itens[0]["sku"], "72400005")
        self.assertEqual(self.itens[0]["descricao"], "BOLINHA DE QUEIJO FESTA ZC 5KG PCT")
        self.assertEqual(self.itens[0]["ean_linha"], "430000200000")

    def test_mesmo_sku_em_duas_linhas_nao_e_agrupado(self):
        skus = [i["sku"] for i in self.itens]
        self.assertEqual(skus.count("72400017"), 2)
        eans = [i["ean_linha"] for i in self.itens if i["sku"] == "72400017"]
        self.assertEqual(eans, ["724000170000", "430000100000"])

    def test_linha_e_1_based_e_estavel(self):
        self.assertEqual([i["linha"] for i in self.itens], list(range(1, 10)))

    def test_quantidade_com_virgula_vira_float(self):
        # Nao usa a fixture real (nenhuma quantidade dela tem virgula) --
        # HTML minimo escrito so pra este caso.
        itens = extrair_itens_do_pedido(_HTML_QTD_COM_VIRGULA)
        self.assertEqual(len(itens), 1)
        self.assertEqual(itens[0]["qtd_embalagem"], 2.0)

    def test_html_sem_aba_de_itens_devolve_lista_vazia(self):
        self.assertEqual(extrair_itens_do_pedido("<html><body>nada</body></html>"), [])

    def test_linha_ilegivel_no_meio_nao_renumera_as_de_baixo(self):
        # I3 da revisao final: `linha` e a chave de reconciliacao
        # (UNIQUE(pedido_id, linha)) e entra no uuid deterministico da
        # baixa. Se a quantidade da linha A for ilegivel (o parser pula a
        # linha, de proposito), a linha B NAO pode virar a linha 1 -- a
        # reserva ativa continuaria no lote de A e a tela mandaria o
        # operador buscar o produto errado no lote errado.
        itens = extrair_itens_do_pedido(_HTML_LINHA_ILEGIVEL)
        self.assertEqual([i["sku"] for i in itens], ["SKU-B", "SKU-C"])
        self.assertEqual([i["linha"] for i in itens], [2, 3])


if __name__ == "__main__":
    unittest.main()
