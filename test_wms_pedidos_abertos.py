"""
test_wms_pedidos_abertos.py

Pedido ABERTO na Stokki entrando no WMS (17/11): leitor da tabela #example1,
quantidade na unidade do produto, data de entrega pela data de expedicao + dia
da regiao. Sessao Stokki sempre duble (a de test_sincronizar_pedidos_wms).

Rodar (da raiz):
    py -3.11 -m unittest test_wms_pedidos_abertos -v
"""
import sys
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "painel_agentes"))

import wms_pedidos  # noqa: E402
import sincronizar_pedidos_wms as mod  # noqa: E402
from stokki import pedidos as stokki_pedidos  # noqa: E402
from test_sincronizar_pedidos_wms import (  # noqa: E402
    BaseRotina, SessaoFalsa, _HTML_ITENS, _linha, _linhas_vazias)

PILOTO_ID, PILOTO_NOME = "22", "MOMBAK"

# Estrutura real (notas da F0, PS-42906): NR. | ID | SKU | Nome | Quantidade | unidade | ...
_HTML_ABERTO = """
<div class="invoice-col">Destino:<address><strong>MERCADO BOM</strong><br>Rua das Flores, 10<br>Santos - SP<br>CEP: 11000-000</address></div>
<table id="example1" class="table">
  <thead><tr><th>NR.</th><th>ID</th><th>SKU</th><th>Nome</th><th>Quantidade</th><th>unidade</th>
  <th>Valor Unitário</th><th>Quantidade confirmada</th><th>Peso informado</th><th>Obs</th></tr></thead>
  <tbody>
    <tr><td>1</td><td>#ITM-1</td><td>SKU1</td><td>PRODUTO 1</td>
        <td><h6><span class="badge badge-secondary">4</span></h6></td><td>UN</td><td>R$ 10,0000</td>
        <td><h6><span class="badge badge-danger"></span></h6></td><td>-</td><td></td></tr>
    <tr><td>2</td><td>#ITM-2</td><td>SKU2</td><td>PRODUTO 2</td>
        <td><h6><span class="badge badge-secondary">1.200</span></h6></td><td>CX</td><td>R$ 10,0000</td>
        <td><h6><span class="badge badge-danger"></span></h6></td><td>-</td><td></td></tr>
  </tbody>
</table>
"""


class TestLeitorDaTabelaDoPedido(unittest.TestCase):
    def test_le_sku_nome_quantidade_e_o_numero_da_linha(self):
        itens = stokki_pedidos.extrair_itens_tabela_pedido(_HTML_ABERTO)
        self.assertEqual([(i["linha"], i["sku"], i["descricao"], i["qtd_embalagem"], i["ean_linha"]) for i in itens],
                         [(1, "SKU1", "PRODUTO 1", 4.0, ""), (2, "SKU2", "PRODUTO 2", 1200.0, "")])
        self.assertTrue(all(i["unidade_do_produto"] for i in itens))

    def test_pagina_sem_a_tabela_ou_com_outra_tabela_devolve_vazio(self):
        self.assertEqual(stokki_pedidos.extrair_itens_tabela_pedido("<p>nada</p>"), [])
        self.assertEqual(stokki_pedidos.extrair_itens_tabela_pedido(_HTML_ITENS), [])

    def test_linha_com_quantidade_ilegivel_e_pulada_sem_renumerar(self):
        html = _HTML_ABERTO.replace('<span class="badge badge-secondary">4</span>', "")
        self.assertEqual([i["linha"] for i in stokki_pedidos.extrair_itens_tabela_pedido(html)], [2])

    def test_pagina_real_em_separacao(self):
        html = (_RAIZ / "galpao3d" / "fixtures" / "show_separando.html").read_text(encoding="utf-8")
        itens = stokki_pedidos.extrair_itens_tabela_pedido(html)
        self.assertEqual((itens[0]["linha"], itens[0]["sku"], itens[0]["qtd_embalagem"]), (1, "8001", 2.0))
        self.assertGreaterEqual(len(itens), 3)


class TestResolverNaUnidadeDoProduto(BaseRotina):
    def _item(self, sku, **extra):
        return {"linha": 1, "sku": sku, "ean_linha": "", "qtd_embalagem": 7, "unidade_do_produto": True, **extra}

    def test_sku_unico_vale_a_quantidade_sem_converter_por_caixa(self):
        # o produto 1 tem qtd_por_caixa 6: na tabela do pedido a quantidade ja e a do produto
        r = wms_pedidos.resolver_item(self.conn, self._item("sku1"))
        self.assertEqual((r["produto_id"], r["qtd_un"], r["motivo_pendencia"]), (1, 7, ""))

    def test_sku_repetido_em_outro_embarcador_e_decidido_pelo_embarcador_do_pedido(self):
        self.conn.execute("UPDATE wms_produtos SET embarcador_id = 22 WHERE id = 1")
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, unidade, "
            "atualizado_em) VALUES (2, 901, 'SKU1', 'OUTRO', 'OUTRO', 99, 'UN', '2026-09-21 10:00:00')")
        r = wms_pedidos.resolver_item(self.conn, self._item("SKU1"))
        self.assertIsNone(r["produto_id"])
        self.assertIn("2 produtos ativos", r["motivo_pendencia"])
        r = wms_pedidos.resolver_item(self.conn, self._item("SKU1", embarcador_id=22))
        self.assertEqual((r["produto_id"], r["qtd_un"]), (1, 7))

    def test_sku_desconhecido_ou_vazio_vira_pendencia(self):
        for sku in ("NAOEXISTE", ""):
            r = wms_pedidos.resolver_item(self.conn, self._item(sku))
            self.assertIsNone(r["qtd_un"])
            self.assertIn("nao encontrado", r["motivo_pendencia"])


class TestDataDeEntrega(unittest.TestCase):
    SANTOS = {"endereco": "Rua das Flores, 10", "cidade": "Santos", "uf": "SP", "cep": "11000-000"}
    CAPITAL = {"endereco": "Av Paulista, 1000", "cidade": "São Paulo", "uf": "SP", "cep": "01310-100"}

    def test_dia_de_visita_da_regiao_fica(self):
        # Baixada Santista recebe as tercas; 24/11/2026 e terca
        self.assertEqual(mod.calcular_data_entrega("24/11/2026", self.SANTOS), ("2026-11-24", ""))

    def test_fora_do_dia_de_visita_vai_pro_proximo(self):
        # 18/11/2026 e quarta -> proxima terca
        self.assertEqual(mod.calcular_data_entrega("2026-11-18", self.SANTOS), ("2026-11-24", "Baixada Santista"))

    def test_cidade_sem_dia_fixo_vale_a_expedicao(self):
        self.assertEqual(mod.calcular_data_entrega("18/11/2026", self.CAPITAL), ("2026-11-18", ""))

    def test_sabado_rola_pra_dia_util(self):
        # 21/11/2026 e sabado
        self.assertEqual(mod.calcular_data_entrega("21/11/2026", self.CAPITAL), ("2026-11-23", ""))

    def test_sem_expedicao_ou_sem_destino(self):
        self.assertEqual(mod.calcular_data_entrega("", self.SANTOS), ("", ""))
        self.assertEqual(mod.calcular_data_entrega("lixo", None), ("", ""))
        self.assertEqual(mod.calcular_data_entrega("18/11/2026", None), ("2026-11-18", ""))
        self.assertEqual(mod.calcular_data_entrega("18/11/2026", {"cidade": None}), ("2026-11-18", ""))

    def test_expedition_date_com_html_em_volta(self):
        self.assertEqual(mod.calcular_data_entrega("<span>18/11/2026</span>", self.CAPITAL)[0], "2026-11-18")


class TestRodadaComPedidoAberto(BaseRotina):
    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE wms_produtos SET embarcador = 'MOMBAK', embarcador_id = 22 WHERE id = 1")
        self.conn.commit()

    def _rodar(self, status, html, id_pedido=50001):
        linhas = _linhas_vazias()
        linha = _linha(id_pedido, PILOTO_ID, nome_truncado="MOMBAK", status=status)
        linha["expedition_date"] = "18/11/2026"
        linhas[status] = [linha]
        return mod.rodar(self.conn, SessaoFalsa(linhas, {str(id_pedido): html}), PILOTO_NOME, PILOTO_ID,
                         limite=50, modo_teste=False)

    def _pedido(self, id_pedido=50001):
        return dict(self.conn.execute("SELECT * FROM wms_pedidos WHERE id_stokki = ?", (id_pedido,)).fetchone())

    def test_open_esta_entre_os_status_lidos(self):
        self.assertEqual(mod.STATUS_INTERESSANTES[0], "Open")

    def test_pedido_aberto_e_registrado_reservado_e_ganha_data_e_destino(self):
        res = self._rodar("Open", _HTML_ABERTO)
        self.assertEqual((res["do_piloto"], res["erros"]), (1, 0))
        p = self._pedido()
        self.assertEqual((p["situacao"], p["fonte_itens"], p["data_expedicao"]), ("Open", "PEDIDO", "2026-11-18"))
        self.assertEqual((p["data_entrega_calc"], p["regiao_dia_fixo"]), ("2026-11-24", "Baixada Santista"))
        self.assertEqual((p["destinatario"], p["cidade"], p["uf"]), ("MERCADO BOM", "Santos", "SP"))
        itens = self.conn.execute("SELECT linha, sku, qtd_un, produto_id, motivo_pendencia FROM wms_pedido_itens "
                                  "WHERE pedido_id = ? ORDER BY linha", (p["id"],)).fetchall()
        self.assertEqual([(i["linha"], i["sku"], i["qtd_un"], i["produto_id"]) for i in itens],
                         [(1, "SKU1", 4, 1), (2, "SKU2", None, None)])
        reservas = self.conn.execute("SELECT quantidade_un FROM wms_reservas WHERE estado = 'ATIVA'").fetchall()
        self.assertEqual([r["quantidade_un"] for r in reservas], [4])

    def test_reler_o_mesmo_pedido_em_separacao_nao_duplica_nem_troca_de_tabela(self):
        self._rodar("Open", _HTML_ABERTO)
        # na separacao a pagina passa a ter TAMBEM a tabela Produto|EAN|Quantidade
        self._rodar("Separating", _HTML_ABERTO + _HTML_ITENS)
        p = self._pedido()
        self.assertEqual((p["situacao"], p["fonte_itens"]), ("Separating", "PEDIDO"))
        n_itens = self.conn.execute("SELECT count(*) FROM wms_pedido_itens WHERE pedido_id = ? "
                                    "AND removido_em = ''", (p["id"],)).fetchone()[0]
        n_reservas = self.conn.execute("SELECT count(*), sum(quantidade_un) FROM wms_reservas "
                                       "WHERE estado = 'ATIVA'").fetchone()
        self.assertEqual((n_itens, tuple(n_reservas)), (2, (1, 4)))

    def test_pedido_ja_gravado_pela_tabela_antiga_continua_nela(self):
        self._rodar("Waiting for Carrier", _HTML_ITENS, id_pedido=50002)
        self.assertEqual(self._pedido(50002)["fonte_itens"], "")
        self._rodar("Waiting for Carrier", _HTML_ABERTO + _HTML_ITENS, id_pedido=50002)
        p = self._pedido(50002)
        self.assertEqual(p["fonte_itens"], "")
        linhas = self.conn.execute("SELECT linha, ean_linha FROM wms_pedido_itens WHERE pedido_id = ? "
                                   "AND removido_em = ''", (p["id"],)).fetchall()
        self.assertEqual([(l["linha"], l["ean_linha"]) for l in linhas], [(1, "111111111111")])

    def test_pedido_novo_sem_a_tabela_do_pedido_cai_na_antiga(self):
        self._rodar("Waiting for Carrier", _HTML_ITENS, id_pedido=50003)
        self.assertEqual(self._pedido(50003)["fonte_itens"], "")


if __name__ == "__main__":
    unittest.main()
