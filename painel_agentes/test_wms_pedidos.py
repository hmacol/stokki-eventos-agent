# -*- coding: utf-8 -*-
"""
test_wms_pedidos.py

Testes do estoque com reserva por pedido (WMS fase 2, Hugo 21/09/2026).
Nenhum teste toca na Stokki nem no dados.db real: o banco vai pra uma
pasta temporaria.
Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_wms_pedidos -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import wms  # noqa: E402
import wms_pedidos  # noqa: E402


class BaseWMS(unittest.TestCase):
    """Banco temporario com uma area, posicoes e produtos de teste."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        self.conn = wms_pedidos.conectar(self.db)
        wms.criar_area(self.conn, "C9", "CONTAINER", "Container 9")
        wms.gerar_posicoes(self.conn, "C9", estantes=2, niveis=2)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()


class TestTabelas(BaseWMS):
    def test_conectar_cria_as_tres_tabelas(self):
        nomes = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'wms_%'")}
        self.assertIn("wms_pedidos", nomes)
        self.assertIn("wms_pedido_itens", nomes)
        self.assertIn("wms_reservas", nomes)

    def test_conectar_tambem_cria_as_tabelas_da_fase_1(self):
        nomes = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'wms_%'")}
        self.assertIn("wms_saldos", nomes)
        self.assertIn("wms_movimentos", nomes)


class TestResolverItem(BaseWMS):
    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, dun, qtd_por_caixa, "
            "unidade, atualizado_em) VALUES (1, 900, '72400017', 'COXINHA FESTA ZC 5KG PCT', "
            "'MARIA DOLORES', '724000170000', '430000100000', 6, 'UN', '2026-09-21 10:00:00')")
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, dun, qtd_por_caixa, "
            "unidade, atualizado_em) VALUES (2, 901, '72400099', 'QUIBE AVULSO', 'MARIA DOLORES', "
            "'111111111111', NULL, 1, 'UN', '2026-09-21 10:00:00')")
        self.conn.commit()

    def test_ean_da_linha_igual_ao_dun_multiplica_pela_caixa(self):
        r = wms_pedidos.resolver_item(self.conn, {
            "sku": "72400017", "ean_linha": "430000100000", "qtd_embalagem": 2})
        self.assertEqual(r["produto_id"], 1)
        self.assertEqual(r["qtd_un"], 12)
        self.assertEqual(r["motivo_pendencia"], "")

    def test_ean_da_linha_igual_ao_ean_unitario_e_um_pra_um(self):
        r = wms_pedidos.resolver_item(self.conn, {
            "sku": "72400017", "ean_linha": "724000170000", "qtd_embalagem": 3})
        self.assertEqual(r["produto_id"], 1)
        self.assertEqual(r["qtd_un"], 3)

    def test_sku_unico_com_caixa_de_um_resolve_sem_ean(self):
        r = wms_pedidos.resolver_item(self.conn, {
            "sku": "72400099", "ean_linha": "999999999999", "qtd_embalagem": 4})
        self.assertEqual(r["produto_id"], 2)
        self.assertEqual(r["qtd_un"], 4)

    def test_produto_desconhecido_vira_pendencia_sem_quantidade(self):
        r = wms_pedidos.resolver_item(self.conn, {
            "sku": "NAO-EXISTE", "ean_linha": "123", "qtd_embalagem": 1})
        self.assertIsNone(r["produto_id"])
        self.assertIsNone(r["qtd_un"])
        self.assertIn("nao encontrado", r["motivo_pendencia"].lower())

    def test_ean_que_nao_bate_com_caixa_maior_que_um_vira_pendencia(self):
        # SKU conhecido, mas o EAN da linha nao e nem o unitario nem o DUN:
        # nao da pra saber se sao 2 unidades ou 2 caixas de 6. Nao inventa.
        r = wms_pedidos.resolver_item(self.conn, {
            "sku": "72400017", "ean_linha": "555555555555", "qtd_embalagem": 2})
        self.assertIsNone(r["qtd_un"])
        self.assertIn("unidade", r["motivo_pendencia"].lower())

    def test_ean_em_dois_produtos_ativos_vira_pendencia(self):
        # mesmo EAN unitario cadastrado em dois produtos ativos: nao da pra
        # saber qual dos dois a linha do pedido quer dizer.
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, dun, qtd_por_caixa, "
            "unidade, atualizado_em) VALUES (3, 902, '72400200', 'PRODUTO DUPLICADO', 'MARIA DOLORES', "
            "'724000170000', NULL, 1, 'UN', '2026-09-21 10:00:00')")
        self.conn.commit()
        r = wms_pedidos.resolver_item(self.conn, {
            "sku": "72400200", "ean_linha": "724000170000", "qtd_embalagem": 2})
        self.assertIsNone(r["produto_id"])
        self.assertIsNone(r["qtd_un"])
        self.assertIn("2 produtos", r["motivo_pendencia"])

    def test_sku_em_dois_produtos_ativos_vira_pendencia(self):
        # mesmo SKU cadastrado em dois produtos ativos: ambiguo, cai fora da
        # regra 3 e vira pendencia.
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, dun, qtd_por_caixa, "
            "unidade, atualizado_em) VALUES (3, 902, '72400099', 'QUIBE DUPLICADO', 'MARIA DOLORES', "
            "'222222222222', NULL, 1, 'UN', '2026-09-21 10:00:00')")
        self.conn.commit()
        r = wms_pedidos.resolver_item(self.conn, {
            "sku": "72400099", "ean_linha": "999999999999", "qtd_embalagem": 4})
        self.assertIsNone(r["produto_id"])
        self.assertIsNone(r["qtd_un"])
        self.assertIn("2 produtos", r["motivo_pendencia"])

    def test_produto_inativo_nunca_resolve(self):
        # EAN, DUN e SKU todos batem, mas o produto esta ativo = 0: tem que
        # cair em "nao encontrado", nunca resolver pra um produto desligado.
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, dun, qtd_por_caixa, "
            "unidade, ativo, atualizado_em) VALUES (3, 902, 'SKU-INATIVO', 'PRODUTO INATIVO', "
            "'MARIA DOLORES', '333333333333', '440000100000', 6, 'UN', 0, '2026-09-21 10:00:00')")
        self.conn.commit()
        r = wms_pedidos.resolver_item(self.conn, {
            "sku": "SKU-INATIVO", "ean_linha": "440000100000", "qtd_embalagem": 2})
        self.assertIsNone(r["produto_id"])
        self.assertIsNone(r["qtd_un"])
        self.assertIn("nao encontrado", r["motivo_pendencia"].lower())


class TestFEFO(BaseWMS):
    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, unidade, qtd_por_caixa, "
            "atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO 1', 'MARIA DOLORES', 'UN', 1, "
            "'2026-09-21 10:00:00')")
        self.conn.commit()
        # tres lotes do mesmo produto, validades fora de ordem de proposito
        for posicao, lote, validade, qtd in [
            ("C9-E1-N1", "L-B", "2026-12-31", 10),
            ("C9-E1-N2", "L-A", "2026-10-15", 6),
            ("C9-E2-N1", "L-C", "2027-05-01", 20),
        ]:
            wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=qtd,
                                    lote=lote, validade=validade, destino=posicao)

    def test_disponivel_vem_em_ordem_de_validade(self):
        linhas = wms_pedidos.disponivel_por_lote(self.conn, 1)
        self.assertEqual([l["lote"] for l in linhas], ["L-A", "L-B", "L-C"])
        self.assertEqual(linhas[0]["disponivel"], 6)

    def test_fefo_consome_o_que_vence_primeiro(self):
        alocacoes, faltou = wms_pedidos.alocar_fefo(self.conn, 1, 4)
        self.assertEqual(faltou, 0)
        self.assertEqual(len(alocacoes), 1)
        self.assertEqual(alocacoes[0]["lote"], "L-A")
        self.assertEqual(alocacoes[0]["quantidade_un"], 4)

    def test_fefo_quebra_em_varios_lotes_quando_precisa(self):
        alocacoes, faltou = wms_pedidos.alocar_fefo(self.conn, 1, 14)
        self.assertEqual(faltou, 0)
        self.assertEqual([(a["lote"], a["quantidade_un"]) for a in alocacoes],
                         [("L-A", 6), ("L-B", 8)])

    def test_fefo_sem_saldo_suficiente_aloca_o_que_da_e_informa_a_falta(self):
        alocacoes, faltou = wms_pedidos.alocar_fefo(self.conn, 1, 50)
        self.assertEqual(faltou, 14)  # 36 em estoque
        self.assertEqual(sum(a["quantidade_un"] for a in alocacoes), 36)

    def _reserva_crua(self, estado):
        """Insere uma reserva direto na tabela. Precisa de pedido e item de
        verdade: wms.conectar liga PRAGMA foreign_keys = ON."""
        agora = wms.agora()
        self.conn.execute(
            "INSERT INTO wms_pedidos (id, id_stokki, codigo_ps, embarcador, situacao, estado_reserva, "
            "lido_em, atualizado_em) VALUES (1, 39751, 'PS-39751', 'MARIA DOLORES', 'Separating', "
            "'PENDENTE', ?, ?)", (agora, agora))
        self.conn.execute(
            "INSERT INTO wms_pedido_itens (id, pedido_id, linha, sku, descricao, qtd_embalagem, qtd_un, "
            "produto_id) VALUES (1, 1, 1, 'SKU1', 'PRODUTO 1', 5, 5, 1)")
        self.conn.execute(
            "INSERT INTO wms_reservas (pedido_id, item_id, produto_id, posicao, lote, validade, "
            "quantidade_un, estado, origem, criado_em, atualizado_em) "
            f"VALUES (1, 1, 1, 'C9-E1-N2', 'L-A', '2026-10-15', 5, '{estado}', 'FEFO', ?, ?)",
            (agora, agora))
        self.conn.commit()

    def test_reserva_ativa_derruba_o_disponivel_mas_nao_o_saldo(self):
        self._reserva_crua("ATIVA")
        linhas = wms_pedidos.disponivel_por_lote(self.conn, 1)
        lote_a = [l for l in linhas if l["lote"] == "L-A"][0]
        self.assertEqual(lote_a["saldo"], 6)
        self.assertEqual(lote_a["reservado"], 5)
        self.assertEqual(lote_a["disponivel"], 1)
        # o saldo fisico da fase 1 nao mudou
        self.assertEqual(wms._saldo_atual(self.conn, "C9-E1-N2", 1, "L-A", "2026-10-15"), 6)

    def test_reserva_cancelada_nao_conta(self):
        self._reserva_crua("CANCELADA")
        lote_a = [l for l in wms_pedidos.disponivel_por_lote(self.conn, 1) if l["lote"] == "L-A"][0]
        self.assertEqual(lote_a["disponivel"], 6)


if __name__ == "__main__":
    unittest.main()
