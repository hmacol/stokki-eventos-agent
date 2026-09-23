# -*- coding: utf-8 -*-
"""
test_recebimentos.py

Parser dos recebimentos da Stokki (incoming), sobre fixtures SINTETICAS
-- Hugo proibiu conectar na Stokki desta maquina (derruba a sessao de
producao).

Decisao do Hugo (23/09/2026): sem pre-preenchimento vindo da Stokki -- o
parser sempre le a tabela de itens (SKU/Nome/Quantidade total recebida)
e ignora a tabela de lote, esteja ela presente na pagina ou nao.

  - recebimento_itens.html: reproduz a estrutura real que o Hugo levantou
    ao vivo em 22/09/2026 -- as duas tabelas no detalhe (lote e itens),
    cabecalhos exatos; prova que a tabela de lote e ignorada mesmo
    presente.
  - recebimento_sem_tabela_lote.html: reproduz o caso do PE-2440, achado
    na revisao (22/09/2026) -- recebimento "Recebido" SEM a tabela de
    lote na pagina, so com a tabela de itens.

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
_FIXTURE_SEM_LOTE = Path(__file__).parent / "fixtures" / "recebimento_sem_tabela_lote.html"

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


# Tabela de itens com a primeira linha de quantidade ilegivel (I3).
_HTML_ITENS_LINHA_ILEGIVEL = """
<table class="table">
  <thead>
    <tr>
      <th>NR.</th><th>ID</th><th>SKU</th><th>Nome</th><th>Localização</th>
      <th>Quantidade</th><th>unidade</th><th>Valor Unitário</th>
      <th>Quantidade total recebida</th><th>Obs</th>
    </tr>
  </thead>
  <tbody>
    <tr><td>1</td><td>1</td><td>SKU-A</td><td>PRODUTO A</td><td>C1</td><td>—</td><td>UN</td><td>0,00</td><td>—</td><td></td></tr>
    <tr><td>2</td><td>2</td><td>SKU-B</td><td>PRODUTO B</td><td>C1</td><td>3</td><td>UN</td><td>0,00</td><td>3</td><td></td></tr>
    <tr><td>3</td><td>3</td><td>SKU-C</td><td>PRODUTO C</td><td>C1</td><td>5</td><td>UN</td><td>0,00</td><td>5</td><td></td></tr>
  </tbody>
</table>
"""


class TestExtrairItensDoRecebimento(unittest.TestCase):
    def setUp(self):
        self.html = _FIXTURE.read_text(encoding="utf-8")
        self.itens = extrair_itens_do_recebimento(self.html)

    def test_le_itens_do_recebimento(self):
        self.assertTrue(self.itens)
        self.assertIn("sku", self.itens[0])
        self.assertIn("qtd_embalagem", self.itens[0])

    def test_le_as_duas_linhas_da_tabela_de_itens_e_ignora_a_de_lote(self):
        # a fixture tem a tabela de lote (Produto/Lote/.../Quantidade)
        # tambem -- se o parser a lesse por engano, contaria valores
        # diferentes (ela nao tem "Quantidade total recebida").
        self.assertEqual(len(self.itens), 2)

    def test_le_sku_e_descricao(self):
        self.assertEqual(self.itens[0]["sku"], "CXPTD1")
        self.assertEqual(self.itens[0]["descricao"], "CX PALITINHO DE TAPIOCA TRAD")

    def test_nao_tem_coluna_ean_nesta_tabela(self):
        self.assertEqual(self.itens[0]["ean_linha"], "")

    def test_qtd_embalagem_vem_da_quantidade_total_recebida_nao_da_nominal(self):
        # a fixture tem "Quantidade" (nominal, 12/6) e "Quantidade total
        # recebida" (o que chegou de verdade, 10/5) com valores diferentes
        # de proposito -- o parser tem que usar a segunda.
        self.assertEqual(self.itens[0]["qtd_embalagem"], 10.0)
        self.assertEqual(self.itens[1]["qtd_embalagem"], 5.0)

    def test_nao_tem_lote_nem_validade_no_item(self):
        # decisao do Hugo (23/09/2026): sem pre-preenchimento vindo da
        # Stokki -- o item extraido nao carrega lote/validade nenhum.
        self.assertNotIn("lote", self.itens[0])
        self.assertNotIn("validade", self.itens[0])

    def test_linha_e_1_based_e_estavel(self):
        self.assertEqual([i["linha"] for i in self.itens], [1, 2])

    def test_linha_ilegivel_no_meio_nao_renumera_as_de_baixo(self):
        # I3 da revisao final: linha pulada por quantidade ilegivel nao
        # pode renumerar as de baixo -- `linha` e a chave de reconciliacao
        # (UNIQUE(recebimento_id, linha)).
        itens = extrair_itens_do_recebimento(_HTML_ITENS_LINHA_ILEGIVEL)
        self.assertEqual([i["sku"] for i in itens], ["SKU-B", "SKU-C"])
        self.assertEqual([i["linha"] for i in itens], [2, 3])

    def test_recebimento_sem_itens_devolve_lista_vazia(self):
        self.assertEqual(extrair_itens_do_recebimento("<html></html>"), [])

    def test_recebimento_em_transito_sem_tabela_devolve_lista_vazia(self):
        # caso real visto pelo Hugo: recebimento "Em transito" ainda sem a
        # tabela de itens -- nao pode quebrar, so devolver vazio.
        html_em_transito = "<html><body><p>Em transito</p></body></html>"
        self.assertEqual(extrair_itens_do_recebimento(html_em_transito), [])


class TestSemTabelaDeLoteNaPagina(unittest.TestCase):
    """
    Achado da revisao (Hugo, 22/09/2026, comparando PE-2440 x PE-2478):
    um recebimento "Recebido" pode nao ter a tabela de lote e ainda assim
    ter mercadoria real, enderecavel, na tabela de itens. Antes isso
    virava erro e o recebimento inteiro sumia do sistema -- agora e
    registrado normalmente, porque a tabela de itens e a unica que o
    parser sempre le (com ou sem a tabela de lote presente na pagina).
    """

    def setUp(self):
        self.html = _FIXTURE_SEM_LOTE.read_text(encoding="utf-8")
        self.itens = extrair_itens_do_recebimento(self.html)

    def test_acha_a_mercadoria_mesmo_sem_a_tabela_de_lote(self):
        self.assertEqual(len(self.itens), 1)

    def test_le_sku_nome_e_quantidade_total_recebida(self):
        item = self.itens[0]
        self.assertEqual(item["sku"], "AMRF")
        self.assertEqual(item["descricao"], "CAIXA DE AMOSTRAS")
        self.assertEqual(item["qtd_embalagem"], 1.0)

    def test_nao_tem_coluna_ean(self):
        self.assertEqual(self.itens[0]["ean_linha"], "")

    def test_recebimento_sem_a_tabela_de_itens_devolve_lista_vazia(self):
        # sem a tabela de itens -- so entao vira erro (decisao de quem
        # chama, ver sincronizar_recebimentos_wms.py).
        html_sem_tabelas = "<html><body><table><tr><td>nada a ver</td></tr></table></body></html>"
        self.assertEqual(extrair_itens_do_recebimento(html_sem_tabelas), [])


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
