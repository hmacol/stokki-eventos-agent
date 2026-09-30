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
from datetime import datetime, timedelta
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


class TestReservarPedido(BaseWMS):
    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, dun, qtd_por_caixa, "
            "unidade, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO 1', 'MARIA DOLORES', "
            "'111111111111', '222222222222', 6, 'UN', '2026-09-21 10:00:00')")
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=10,
                                lote="L-A", validade="2026-10-15", destino="C9-E1-N1")
        self.itens = [
            {"linha": 1, "sku": "SKU1", "ean_linha": "111111111111",
             "descricao": "PRODUTO 1", "qtd_embalagem": 4},
        ]
        self.pedido = {"id_stokki": 39751, "codigo_ps": "PS-39751",
                       "embarcador": "MARIA DOLORES", "situacao": "Waiting for Carrier"}

    def test_registrar_pedido_grava_itens_resolvidos(self):
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        item = self.conn.execute("SELECT * FROM wms_pedido_itens WHERE pedido_id = ?", (pid,)).fetchone()
        self.assertEqual(item["produto_id"], 1)
        self.assertEqual(item["qtd_un"], 4)

    def test_registrar_duas_vezes_nao_duplica(self):
        pid1 = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        pid2 = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        self.assertEqual(pid1, pid2)
        n = self.conn.execute("SELECT COUNT(*) n FROM wms_pedido_itens").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_reserva_completa_marca_pedido_como_reservado(self):
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        r = wms_pedidos.reservar_pedido(self.conn, pid)
        self.assertEqual(r["estado"], "RESERVADO")
        self.assertEqual(r["reservas"], 1)
        reserva = self.conn.execute("SELECT * FROM wms_reservas").fetchone()
        self.assertEqual(reserva["lote"], "L-A")
        self.assertEqual(reserva["quantidade_un"], 4)
        self.assertEqual(reserva["estado"], "ATIVA")

    def test_reservar_duas_vezes_nao_duplica_reserva(self):
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        wms_pedidos.reservar_pedido(self.conn, pid)
        wms_pedidos.reservar_pedido(self.conn, pid)
        n = self.conn.execute("SELECT COUNT(*) n FROM wms_reservas WHERE estado='ATIVA'").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_sem_saldo_suficiente_fica_parcial_e_nunca_falha(self):
        itens = [dict(self.itens[0], qtd_embalagem=99)]
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, itens)
        r = wms_pedidos.reservar_pedido(self.conn, pid)
        self.assertEqual(r["estado"], "PARCIAL")
        self.assertTrue(any("falt" in p.lower() for p in r["pendencias"]))
        self.assertEqual(self.conn.execute(
            "SELECT SUM(quantidade_un) s FROM wms_reservas").fetchone()["s"], 10)

    def test_item_nao_resolvido_nao_vira_reserva(self):
        itens = [{"linha": 1, "sku": "FANTASMA", "ean_linha": "000",
                  "descricao": "NAO EXISTE", "qtd_embalagem": 1}]
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, itens)
        r = wms_pedidos.reservar_pedido(self.conn, pid)
        self.assertEqual(r["reservas"], 0)
        self.assertEqual(r["estado"], "PARCIAL")
        self.assertTrue(r["pendencias"])

    def test_cancelar_reservas_libera_o_disponivel(self):
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        wms_pedidos.reservar_pedido(self.conn, pid)
        wms_pedidos.cancelar_reservas(self.conn, pid, "pedido cancelado na Stokki")
        linha = wms_pedidos.disponivel_por_lote(self.conn, 1)[0]
        self.assertEqual(linha["disponivel"], 10)
        estado = self.conn.execute("SELECT estado_reserva FROM wms_pedidos WHERE id = ?",
                                   (pid,)).fetchone()["estado_reserva"]
        self.assertEqual(estado, "CANCELADO")

    # -- Correcao 21/09, rodada 1: reconciliacao (pedido editado na Stokki
    # entre rodadas de 15 min, antes da expedicao). --

    def test_quantidade_sobe_cancela_a_reserva_velha_e_realoca_pro_valor_novo(self):
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)  # 4 UN
        wms_pedidos.reservar_pedido(self.conn, pid)

        itens_editados = [dict(self.itens[0], qtd_embalagem=6)]  # Stokki: 4 -> 6
        wms_pedidos.registrar_pedido(self.conn, self.pedido, itens_editados)
        r = wms_pedidos.reservar_pedido(self.conn, pid)

        self.assertEqual(r["estado"], "RESERVADO")
        ativas = self.conn.execute(
            "SELECT quantidade_un FROM wms_reservas WHERE estado = 'ATIVA'").fetchall()
        self.assertEqual([a["quantidade_un"] for a in ativas], [6])
        canceladas = self.conn.execute(
            "SELECT COUNT(*) n FROM wms_reservas WHERE estado = 'CANCELADA'").fetchone()["n"]
        self.assertEqual(canceladas, 1)

    def test_quantidade_desce_cancela_a_reserva_velha_e_o_disponivel_volta_a_subir(self):
        itens_iniciais = [dict(self.itens[0], qtd_embalagem=8)]
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, itens_iniciais)  # 8 UN
        wms_pedidos.reservar_pedido(self.conn, pid)
        disponivel_com_8 = wms_pedidos.disponivel_por_lote(self.conn, 1)[0]["disponivel"]
        self.assertEqual(disponivel_com_8, 2)  # 10 em estoque, 8 reservadas

        itens_editados = [dict(self.itens[0], qtd_embalagem=2)]  # Stokki: 8 -> 2
        wms_pedidos.registrar_pedido(self.conn, self.pedido, itens_editados)
        r = wms_pedidos.reservar_pedido(self.conn, pid)

        self.assertEqual(r["estado"], "RESERVADO")
        disponivel_com_2 = wms_pedidos.disponivel_por_lote(self.conn, 1)[0]["disponivel"]
        # a prova do achado: o disponivel sobe de volta (nao fica presa
        # escondendo 6 UN que existem de verdade no galpao)
        self.assertEqual(disponivel_com_2, 8)
        ativas = self.conn.execute(
            "SELECT SUM(quantidade_un) s FROM wms_reservas WHERE estado = 'ATIVA'").fetchone()["s"]
        self.assertEqual(ativas, 2)

    def test_linha_que_some_do_pedido_cancela_a_reserva_mas_mantem_a_linha(self):
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        wms_pedidos.reservar_pedido(self.conn, pid)

        wms_pedidos.registrar_pedido(self.conn, self.pedido, [])  # linha 1 saiu do pedido

        reserva = self.conn.execute("SELECT estado FROM wms_reservas").fetchone()
        self.assertEqual(reserva["estado"], "CANCELADA")
        n_itens = self.conn.execute(
            "SELECT COUNT(*) n FROM wms_pedido_itens WHERE pedido_id = ?", (pid,)).fetchone()["n"]
        self.assertEqual(n_itens, 1)  # a linha continua no historico, so a reserva foi liberada
        disponivel = wms_pedidos.disponivel_por_lote(self.conn, 1)[0]["disponivel"]
        self.assertEqual(disponivel, 10)

    def test_reserva_manual_e_preservada_quando_o_pedido_muda_e_vira_pendencia(self):
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        item_id = self.conn.execute(
            "SELECT id FROM wms_pedido_itens WHERE pedido_id = ?", (pid,)).fetchone()["id"]
        agora = wms.agora()
        self.conn.execute("""
            INSERT INTO wms_reservas (pedido_id, item_id, produto_id, posicao, lote, validade,
                                      quantidade_un, estado, origem, criado_em, atualizado_em)
            VALUES (?,?,?,?,?,?,?,'ATIVA','MANUAL',?,?)""",
            (pid, item_id, 1, "C9-E1-N1", "L-A", "2026-10-15", 4, agora, agora))
        self.conn.commit()

        itens_editados = [dict(self.itens[0], qtd_embalagem=6)]  # Stokki: 4 -> 6
        wms_pedidos.registrar_pedido(self.conn, self.pedido, itens_editados)
        r = wms_pedidos.reservar_pedido(self.conn, pid)

        self.assertEqual(r["reservas"], 0)  # nao criou nem tocou em nada
        reserva = self.conn.execute("SELECT * FROM wms_reservas").fetchone()
        self.assertEqual(reserva["estado"], "ATIVA")
        self.assertEqual(reserva["origem"], "MANUAL")
        self.assertEqual(reserva["quantidade_un"], 4)
        self.assertTrue(any("manual" in p.lower() for p in r["pendencias"]))
        self.assertEqual(r["estado"], "PARCIAL")


class TestArredondamentoFracionario(BaseWMS):
    """Achado 2 da revisao: sem ROUND(...,3) na comparacao de RESERVADO x
    PARCIAL, a SOMA de varias reservas fracionarias acumula residuo de
    ponto flutuante e classifica errado um pedido 100% reservado (o
    revisor simulou 500 mil combinacoes: 7,3% davam erro sem o ROUND)."""

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, unidade, qtd_por_caixa, "
            "atualizado_em) VALUES (1, 900, 'SKUFRAC', 'PRODUTO FRACIONARIO', 'MARIA DOLORES', 'UN', 1, "
            "'2026-09-21 10:00:00')")
        self.conn.commit()
        # mesmo exemplo do relatorio da revisao: soma pura em ponto flutuante
        # de 37.123+5.657+16.861+1.542+22.433+38.299+36.998 = 158.91299999999998,
        # mas 158.913 (a soma "de verdade") e exatamente a qtd_un do item.
        lotes = [
            ("L1", "2026-10-01", 37.123),
            ("L2", "2026-10-02", 5.657),
            ("L3", "2026-10-03", 16.861),
            ("L4", "2026-10-04", 1.542),
            ("L5", "2026-10-05", 22.433),
            ("L6", "2026-10-06", 38.299),
            ("L7", "2026-10-07", 36.998),
        ]
        for lote, validade, qtd in lotes:
            wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=qtd,
                                    lote=lote, validade=validade, destino="C9-E1-N1")

    def test_soma_fracionaria_com_residuo_de_ponto_flutuante_fica_reservado(self):
        itens = [{"linha": 1, "sku": "SKUFRAC", "ean_linha": "",
                  "descricao": "PRODUTO FRACIONARIO", "qtd_embalagem": 158.913}]
        pedido = {"id_stokki": 50001, "codigo_ps": "PS-50001",
                  "embarcador": "MARIA DOLORES", "situacao": "Waiting for Carrier"}
        pid = wms_pedidos.registrar_pedido(self.conn, pedido, itens)
        r = wms_pedidos.reservar_pedido(self.conn, pid)
        self.assertEqual(r["pendencias"], [])
        self.assertEqual(r["estado"], "RESERVADO")
        total = self.conn.execute(
            "SELECT SUM(quantidade_un) s FROM wms_reservas WHERE estado = 'ATIVA'").fetchone()["s"]
        self.assertAlmostEqual(total, 158.913, places=3)


class TestBaixaNaExpedicao(BaseWMS):
    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, dun, qtd_por_caixa, "
            "unidade, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO 1', 'MARIA DOLORES', "
            "'111111111111', '222222222222', 6, 'UN', '2026-09-21 10:00:00')")
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=10,
                                lote="L-A", validade="2026-10-15", destino="C9-E1-N1")
        self.pid = wms_pedidos.registrar_pedido(
            self.conn,
            {"id_stokki": 39751, "codigo_ps": "PS-39751", "embarcador": "MARIA DOLORES",
             "situacao": "Waiting for Carrier"},
            [{"linha": 1, "sku": "SKU1", "ean_linha": "111111111111",
              "descricao": "PRODUTO 1", "qtd_embalagem": 4}])
        wms_pedidos.reservar_pedido(self.conn, self.pid)

    def test_baixa_gera_saida_e_derruba_o_saldo(self):
        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-39751")
        self.assertEqual(r["baixas"], 1)
        self.assertEqual(wms._saldo_atual(self.conn, "C9-E1-N1", 1, "L-A", "2026-10-15"), 6)
        mov = self.conn.execute("SELECT * FROM wms_movimentos WHERE tipo = 'SAIDA'").fetchone()
        self.assertEqual(mov["quantidade"], 4)
        self.assertEqual(mov["posicao_origem"], "C9-E1-N1")

    def test_baixar_duas_vezes_nao_baixa_em_dobro(self):
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-39751")
        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-39751")
        self.assertTrue(r["ja_baixado"])
        self.assertEqual(wms._saldo_atual(self.conn, "C9-E1-N1", 1, "L-A", "2026-10-15"), 6)
        n = self.conn.execute("SELECT COUNT(*) n FROM wms_movimentos WHERE tipo='SAIDA'").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_baixa_marca_pedido_e_reservas(self):
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-39751")
        estado = self.conn.execute("SELECT estado_reserva FROM wms_pedidos WHERE id = ?",
                                   (self.pid,)).fetchone()["estado_reserva"]
        self.assertEqual(estado, "BAIXADO")
        reserva = self.conn.execute("SELECT * FROM wms_reservas").fetchone()
        self.assertEqual(reserva["estado"], "CONSUMIDA")
        self.assertTrue(reserva["movimento_uuid"])

    def test_codigo_com_sufixo_de_reentrega_encontra_o_pedido(self):
        # a VUUPT devolve '#PS-39751-R2' em reentrega; tem que cair no mesmo pedido
        r = wms_pedidos.baixar_por_expedicao(self.conn, "#PS-39751-R2")
        self.assertEqual(r["pedido_id"], self.pid)

    def test_pedido_desconhecido_nao_explode(self):
        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-00000")
        self.assertIsNone(r["pedido_id"])
        self.assertEqual(r["baixas"], 0)

    # -- Correcao 1 da revisao (Achado 1, critical): a segunda reserva em
    # diante nunca baixava -- o UPDATE de wms_reservas dentro do loop abria
    # transacao implicita e o BEGIN IMMEDIATE da proxima reserva estourava,
    # o except engolia e o rollback desfazia ate a reserva anterior. Os
    # testes acima (pedido de 1 linha, 1 reserva) passavam sem cobrir isso.

    def test_pedido_com_dois_itens_baixa_as_duas_linhas_sem_erro(self):
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, dun, qtd_por_caixa, "
            "unidade, atualizado_em) VALUES (2, 901, 'SKU2', 'PRODUTO 2', 'MARIA DOLORES', "
            "'333333333333', '444444444444', 1, 'UN', '2026-09-21 10:00:00')")
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=2, quantidade=10,
                                lote="L-X", validade="2026-11-01", destino="C9-E2-N1")
        pid = wms_pedidos.registrar_pedido(
            self.conn,
            {"id_stokki": 39752, "codigo_ps": "PS-39752", "embarcador": "MARIA DOLORES",
             "situacao": "Waiting for Carrier"},
            [{"linha": 1, "sku": "SKU1", "ean_linha": "111111111111",
              "descricao": "PRODUTO 1", "qtd_embalagem": 2},
             {"linha": 2, "sku": "SKU2", "ean_linha": "333333333333",
              "descricao": "PRODUTO 2", "qtd_embalagem": 3}])
        wms_pedidos.reservar_pedido(self.conn, pid)

        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-39752")

        self.assertEqual(r["baixas"], 2)
        self.assertEqual(r["erros"], [])
        n_saida = self.conn.execute(
            "SELECT COUNT(*) n FROM wms_movimentos WHERE tipo = 'SAIDA'").fetchone()["n"]
        self.assertEqual(n_saida, 2)
        consumidas = self.conn.execute(
            "SELECT COUNT(*) n FROM wms_reservas WHERE pedido_id = ? AND estado = 'CONSUMIDA'",
            (pid,)).fetchone()["n"]
        self.assertEqual(consumidas, 2)

    def test_item_dividido_em_dois_lotes_pelo_fefo_baixa_as_duas_reservas(self):
        # o setUp deixa uma transacao pendente (registrar_pedido/reservar_pedido
        # sem commit); fechar aqui pra poder chamar wms.registrar_movimento
        # direto (BEGIN IMMEDIATE nao aceita rodar dentro de outra transacao)
        self.conn.commit()
        # segundo lote do mesmo produto, vencendo depois do L-A -- o FEFO
        # esgota o L-A (sobrando 6 depois da reserva do setUp) e completa no L-B
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=5,
                                lote="L-B", validade="2026-11-01", destino="C9-E1-N2")
        pid = wms_pedidos.registrar_pedido(
            self.conn,
            {"id_stokki": 39753, "codigo_ps": "PS-39753", "embarcador": "MARIA DOLORES",
             "situacao": "Waiting for Carrier"},
            [{"linha": 1, "sku": "SKU1", "ean_linha": "111111111111",
              "descricao": "PRODUTO 1", "qtd_embalagem": 8}])
        r_reserva = wms_pedidos.reservar_pedido(self.conn, pid)
        self.assertEqual(r_reserva["estado"], "RESERVADO")
        lotes_reservados = self.conn.execute(
            "SELECT lote FROM wms_reservas WHERE pedido_id = ? AND estado = 'ATIVA' ORDER BY lote",
            (pid,)).fetchall()
        self.assertEqual([l["lote"] for l in lotes_reservados], ["L-A", "L-B"])  # confirma que dividiu

        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-39753")

        self.assertEqual(r["baixas"], 2)
        self.assertEqual(r["erros"], [])
        # um uuid por RESERVA (nao um ordinal da chamada -- ver C2 em
        # TestRetentativaDeLinhaPartida): dois movimentos distintos.
        ids_reservas = [r2["id"] for r2 in self.conn.execute(
            "SELECT id FROM wms_reservas WHERE pedido_id = ? ORDER BY id", (pid,))]
        uuids = sorted(m["uuid"] for m in self.conn.execute(
            "SELECT mv.uuid FROM wms_movimentos mv "
            "JOIN wms_reservas rv ON rv.movimento_uuid = mv.uuid "
            "WHERE rv.pedido_id = ?", (pid,)).fetchall())
        self.assertEqual(uuids, sorted(f"ps-39753-reserva-{i}" for i in ids_reservas))
        consumidas = self.conn.execute(
            "SELECT COUNT(*) n FROM wms_reservas WHERE pedido_id = ? AND estado = 'CONSUMIDA'",
            (pid,)).fetchone()["n"]
        self.assertEqual(consumidas, 2)

    def test_baixar_duas_vezes_no_pedido_de_dois_itens_nao_baixa_em_dobro(self):
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, dun, qtd_por_caixa, "
            "unidade, atualizado_em) VALUES (2, 901, 'SKU2', 'PRODUTO 2', 'MARIA DOLORES', "
            "'333333333333', '444444444444', 1, 'UN', '2026-09-21 10:00:00')")
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=2, quantidade=10,
                                lote="L-X", validade="2026-11-01", destino="C9-E2-N1")
        pid = wms_pedidos.registrar_pedido(
            self.conn,
            {"id_stokki": 39754, "codigo_ps": "PS-39754", "embarcador": "MARIA DOLORES",
             "situacao": "Waiting for Carrier"},
            [{"linha": 1, "sku": "SKU1", "ean_linha": "111111111111",
              "descricao": "PRODUTO 1", "qtd_embalagem": 2},
             {"linha": 2, "sku": "SKU2", "ean_linha": "333333333333",
              "descricao": "PRODUTO 2", "qtd_embalagem": 3}])
        wms_pedidos.reservar_pedido(self.conn, pid)

        r1 = wms_pedidos.baixar_por_expedicao(self.conn, "PS-39754")
        r2 = wms_pedidos.baixar_por_expedicao(self.conn, "PS-39754")

        self.assertEqual(r1["baixas"], 2)
        self.assertEqual(r1["erros"], [])
        self.assertEqual(r2["baixas"], 0)
        self.assertTrue(r2["ja_baixado"])
        n_saida = self.conn.execute(
            "SELECT COUNT(*) n FROM wms_movimentos WHERE tipo = 'SAIDA'").fetchone()["n"]
        self.assertEqual(n_saida, 2)  # nao dobrou na segunda chamada
        self.assertEqual(wms._saldo_atual(self.conn, "C9-E1-N1", 1, "L-A", "2026-10-15"), 8)  # 10 - 2
        self.assertEqual(wms._saldo_atual(self.conn, "C9-E2-N1", 2, "L-X", "2026-11-01"), 7)  # 10 - 3

    # -- Correcao 2 da revisao (Important): o pedido nao pode virar BAIXADO
    # quando a baixa falhou parcialmente -- senao mascara a reserva orfa.

    def test_falha_genuina_numa_reserva_deixa_o_pedido_parcial(self):
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, dun, qtd_por_caixa, "
            "unidade, atualizado_em) VALUES (2, 901, 'SKU2', 'PRODUTO 2', 'MARIA DOLORES', "
            "'333333333333', '444444444444', 1, 'UN', '2026-09-21 10:00:00')")
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=2, quantidade=10,
                                lote="L-X", validade="2026-11-01", destino="C9-E2-N1")
        pid = wms_pedidos.registrar_pedido(
            self.conn,
            {"id_stokki": 39755, "codigo_ps": "PS-39755", "embarcador": "MARIA DOLORES",
             "situacao": "Waiting for Carrier"},
            [{"linha": 1, "sku": "SKU1", "ean_linha": "111111111111",
              "descricao": "PRODUTO 1", "qtd_embalagem": 2},
             {"linha": 2, "sku": "SKU2", "ean_linha": "333333333333",
              "descricao": "PRODUTO 2", "qtd_embalagem": 3}])
        wms_pedidos.reservar_pedido(self.conn, pid)
        # falha genuina: a posicao de origem da linha 2 foi desativada entre
        # a reserva e a expedicao (ex.: area em manutencao/bloqueio). Direto
        # na tabela porque wms.desativar_posicao recusa desativar posicao
        # com saldo -- exatamente o caso real que estamos simulando.
        self.conn.execute("UPDATE wms_posicoes SET ativo = 0 WHERE codigo = ?", ("C9-E2-N1",))
        self.conn.commit()

        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-39755")

        self.assertEqual(r["baixas"], 1)
        self.assertTrue(r["erros"])
        self.assertIn("linha 2", r["erros"][0])
        estado_pedido = self.conn.execute(
            "SELECT estado_reserva FROM wms_pedidos WHERE id = ?", (pid,)).fetchone()["estado_reserva"]
        self.assertEqual(estado_pedido, "PARCIAL")
        reservas = {r2["posicao"]: r2["estado"] for r2 in self.conn.execute(
            "SELECT posicao, estado FROM wms_reservas WHERE pedido_id = ?", (pid,)).fetchall()}
        self.assertEqual(reservas["C9-E1-N1"], "CONSUMIDA")
        self.assertEqual(reservas["C9-E2-N1"], "ATIVA")


class TestRetentativaDeLinhaPartida(BaseWMS):
    """
    C2 da revisao (23/09): o uuid da baixa era ps-<id>-item-<linha>-<n>,
    com n contado so entre as reservas ATIVAS DAQUELA chamada. Linha
    partida pelo FEFO em dois lotes, primeira baixa OK e segunda
    estourando: na rodada seguinte a reserva sobrevivente virava n=1,
    colidia com o uuid ja gravado, voltava duplicado=True e era marcada
    CONSUMIDA apontando pro movimento DA OUTRA. Resultado: 5 UN saindo do
    galpao sem SAIDA nenhuma, pedido BAIXADO, e MUDO -- sem pendencia, sem
    reserva antiga, sem saldo negativo.

    E a varredura nova executa essa retentativa a cada 15 min.
    """

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, unidade, "
            "qtd_por_caixa, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO A', 'MARIA DOLORES', "
            "'111111111111', 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()
        # dois lotes de 5 UN: o FEFO parte a linha de 10 UN em dois
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=5, lote="L-A",
                                validade="2026-10-15", destino="C9-E1-N1")
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=5, lote="L-B",
                                validade="2026-11-15", destino="C9-E1-N2")
        self.pid = wms_pedidos.registrar_pedido(
            self.conn, {"id_stokki": 40400, "codigo_ps": "PS-40400",
                        "embarcador": "MARIA DOLORES", "situacao": "Waiting for Carrier"},
            [{"linha": 1, "sku": "SKU1", "ean_linha": "111111111111",
              "descricao": "PRODUTO A", "qtd_embalagem": 10}])
        wms_pedidos.reservar_pedido(self.conn, self.pid)
        self.conn.commit()
        lotes = [r["lote"] for r in self.conn.execute(
            "SELECT lote FROM wms_reservas WHERE estado = 'ATIVA' ORDER BY id")]
        self.assertEqual(lotes, ["L-A", "L-B"])  # confirma que partiu

    def _saidas(self):
        return self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(quantidade), 0) q FROM wms_movimentos "
            "WHERE tipo = 'SAIDA'").fetchone()

    def test_retentativa_depois_de_falha_parcial_gera_a_segunda_saida(self):
        # 1a baixa: o L-B falha (posicao desativada entre reserva e expedicao)
        self.conn.execute("UPDATE wms_posicoes SET ativo = 0 WHERE codigo = 'C9-E1-N2'")
        self.conn.commit()
        primeira = wms_pedidos.baixar_por_expedicao(self.conn, "PS-40400")
        self.assertEqual(primeira["baixas"], 1)
        self.assertTrue(primeira["erros"])
        self.assertEqual(self.conn.execute(
            "SELECT estado_reserva FROM wms_pedidos WHERE id = ?", (self.pid,)
        ).fetchone()["estado_reserva"], "PARCIAL")

        # problema resolvido; a rodada seguinte retenta
        self.conn.execute("UPDATE wms_posicoes SET ativo = 1 WHERE codigo = 'C9-E1-N2'")
        self.conn.commit()
        segunda = wms_pedidos.baixar_por_expedicao(self.conn, "PS-40400")

        # antes da correcao: baixas=0, sem erro, pedido BAIXADO e UMA saida so
        self.assertEqual(segunda["baixas"], 1)
        self.assertEqual(segunda["erros"], [])
        saidas = self._saidas()
        self.assertEqual(saidas["n"], 2)
        self.assertEqual(saidas["q"], 10)   # as 10 UN que foram no caminhao
        self.assertEqual(self.conn.execute(
            "SELECT estado_reserva FROM wms_pedidos WHERE id = ?", (self.pid,)
        ).fetchone()["estado_reserva"], "BAIXADO")

    def test_cada_reserva_aponta_pro_proprio_movimento(self):
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-40400")

        pares = self.conn.execute(
            "SELECT id, lote, movimento_uuid, estado FROM wms_reservas ORDER BY id").fetchall()
        self.assertEqual(len({p["movimento_uuid"] for p in pares}), 2)  # uuids distintos
        for p in pares:
            self.assertEqual(p["estado"], "CONSUMIDA")
            self.assertEqual(p["movimento_uuid"], f"ps-40400-reserva-{p['id']}")

    def test_prateleira_nao_fica_com_mercadoria_que_ja_foi_embora(self):
        self.conn.execute("UPDATE wms_posicoes SET ativo = 0 WHERE codigo = 'C9-E1-N2'")
        self.conn.commit()
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-40400")
        self.conn.execute("UPDATE wms_posicoes SET ativo = 1 WHERE codigo = 'C9-E1-N2'")
        self.conn.commit()
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-40400")

        self.assertEqual(wms._saldo_atual(self.conn, "C9-E1-N1", 1, "L-A", "2026-10-15"), 0)
        # antes da correcao ficavam 5 UN fantasmas no L-B
        self.assertEqual(wms._saldo_atual(self.conn, "C9-E1-N2", 1, "L-B", "2026-11-15"), 0)
        # e a invariante central: saldo == soma dos movimentos
        soma = self.conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN tipo = 'ENTRADA' THEN quantidade ELSE -quantidade END), 0) s "
            "FROM wms_movimentos").fetchone()["s"]
        saldo = self.conn.execute(
            "SELECT COALESCE(SUM(quantidade), 0) s FROM wms_saldos").fetchone()["s"]
        self.assertEqual(saldo, soma)

    def test_baixar_de_novo_depois_de_tudo_certo_nao_baixa_em_dobro(self):
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-40400")
        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-40400")

        self.assertEqual(r["baixas"], 0)
        self.assertTrue(r["ja_baixado"])
        self.assertEqual(self._saidas()["n"], 2)


class TestSequenciaDaRotina(BaseWMS):
    """
    Achado CRITICO da revisao final (22/09): os testes antigos exercitavam
    registrar_pedido e reservar_pedido isoladamente. A rotina de lote chama
    os DOIS em sequencia (sincronizar_pedidos_wms.rodar) -- e era so nessa
    sequencia que o bug aparecia. Todo teste desta classe roda a sequencia
    real.
    """

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, unidade, "
            "qtd_por_caixa, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO A', 'MARIA DOLORES', "
            "'111111111111', 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, unidade, "
            "qtd_por_caixa, atualizado_em) VALUES (2, 901, 'SKU2', 'PRODUTO B', 'MARIA DOLORES', "
            "'222222222222', 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=10,
                                lote="L-A", validade="2026-10-15", destino="C9-E1-N1")
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=2, quantidade=10,
                                lote="L-B", validade="2026-11-15", destino="C9-E1-N2")
        self.pedido = {"id_stokki": 39760, "codigo_ps": "PS-39760",
                       "embarcador": "MARIA DOLORES", "situacao": "Waiting for Carrier"}
        self.duas_linhas = [
            {"linha": 1, "sku": "SKU1", "ean_linha": "111111111111", "descricao": "PRODUTO A",
             "qtd_embalagem": 2},
            {"linha": 2, "sku": "SKU2", "ean_linha": "222222222222", "descricao": "PRODUTO B",
             "qtd_embalagem": 3},
        ]

    def _rodada(self, itens):
        """Exatamente o que sincronizar_pedidos_wms.rodar faz por pedido."""
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, itens)
        r = wms_pedidos.reservar_pedido(self.conn, pid)
        self.conn.commit()
        return pid, r

    def _ativas(self):
        return [(r["item_id"], r["quantidade_un"]) for r in self.conn.execute(
            "SELECT item_id, quantidade_un FROM wms_reservas WHERE estado = 'ATIVA' ORDER BY item_id")]

    def test_linha_que_sai_do_pedido_nao_ressuscita_na_reserva_da_mesma_rodada(self):
        # rodada 1: as duas linhas reservam
        self._rodada(self.duas_linhas)
        self.assertEqual(len(self._ativas()), 2)

        # rodada 2: a linha 2 saiu do pedido na Stokki. registrar_pedido
        # cancela a reserva dela -- e o reservar_pedido logo em seguida NAO
        # pode criar outra (era o critico: mercadoria saia do estoque sem
        # estar no pedido, sem erro nenhum).
        pid, _ = self._rodada(self.duas_linhas[:1])

        self.assertEqual(len(self._ativas()), 1)
        item_removido = self.conn.execute(
            "SELECT * FROM wms_pedido_itens WHERE pedido_id = ? AND linha = 2", (pid,)).fetchone()
        self.assertTrue(item_removido["removido_em"])  # a linha fica no historico
        # e a baixa gera UMA saida, nao duas
        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-39760")
        self.assertEqual(r["baixas"], 1)
        n_saida = self.conn.execute(
            "SELECT COUNT(*) n FROM wms_movimentos WHERE tipo = 'SAIDA'").fetchone()["n"]
        self.assertEqual(n_saida, 1)

    def test_linha_que_volta_pro_pedido_volta_a_reservar(self):
        self._rodada(self.duas_linhas)
        self._rodada(self.duas_linhas[:1])          # linha 2 saiu
        pid, r = self._rodada(self.duas_linhas)     # e voltou

        self.assertEqual(len(self._ativas()), 2)
        self.assertEqual(r["estado"], "RESERVADO")
        item = self.conn.execute(
            "SELECT * FROM wms_pedido_itens WHERE pedido_id = ? AND linha = 2", (pid,)).fetchone()
        self.assertEqual(item["removido_em"], "")

    def test_rodada_depois_da_baixa_nao_re_reserva_o_pedido(self):
        # Depois da baixa as reservas estao CONSUMIDAS e o pedido BAIXADO.
        # A rodada seguinte (o pedido ainda aparece em 'Pack' por uns
        # minutos) criava reserva ATIVA nova e devolvia o pedido pra
        # RESERVADO -- travando estoque que ja saiu do galpao.
        self._rodada(self.duas_linhas)
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-39760")

        pid, r = self._rodada(self.duas_linhas)

        self.assertEqual(r["estado"], "BAIXADO")
        self.assertEqual(r["reservas"], 0)
        self.assertEqual(self._ativas(), [])
        estado = self.conn.execute("SELECT estado_reserva FROM wms_pedidos WHERE id = ?",
                                   (pid,)).fetchone()["estado_reserva"]
        self.assertEqual(estado, "BAIXADO")

    def test_rodada_depois_do_cancelamento_nao_re_reserva_o_pedido(self):
        pid, _ = self._rodada(self.duas_linhas)
        wms_pedidos.cancelar_reservas(self.conn, pid, "pedido cancelado na Stokki")
        self.conn.commit()

        _, r = self._rodada(self.duas_linhas)

        self.assertEqual(r["estado"], "CANCELADO")
        self.assertEqual(self._ativas(), [])

    def test_cancelar_reservas_grava_o_motivo(self):
        pid, _ = self._rodada(self.duas_linhas)
        wms_pedidos.cancelar_reservas(self.conn, pid, "pedido nao existe mais na Stokki")
        motivo = self.conn.execute("SELECT motivo_cancelamento FROM wms_pedidos WHERE id = ?",
                                   (pid,)).fetchone()["motivo_cancelamento"]
        self.assertEqual(motivo, "pedido nao existe mais na Stokki")


class TestFEFOEntradaMaisAntiga(BaseWMS):
    """spec 7.3 item 4: validade crescente, depois ENTRADA mais antiga."""

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, unidade, qtd_por_caixa, "
            "atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO 1', 'MARIA DOLORES', 'UN', 1, "
            "'2026-09-21 10:00:00')")
        self.conn.commit()
        # mesma validade nos dois lotes; o que ENTROU antes esta na posicao
        # que ordena DEPOIS, entao ordenar por posicao daria o lote errado.
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=5, lote="L-VELHO",
                                validade="2026-12-31", destino="C9-E2-N1", criado_em="2026-08-01 08:00:00")
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=5, lote="L-NOVO",
                                validade="2026-12-31", destino="C9-E1-N1", criado_em="2026-09-20 08:00:00")

    def test_empate_de_validade_sai_primeiro_o_que_entrou_primeiro(self):
        alocacoes, faltou = wms_pedidos.alocar_fefo(self.conn, 1, 3)
        self.assertEqual(faltou, 0)
        self.assertEqual(alocacoes[0]["lote"], "L-VELHO")


class TestFaltaDeSaldoViraPendencia(BaseWMS):
    """
    I7 da revisao: falta de saldo so aparecia no retorno da funcao (ia pro
    log). Na primeira semana, com o galpao ainda sem enderecar nada, TODO
    pedido fica PARCIAL e a tela dizia "Nenhuma pendencia".
    """

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, unidade, "
            "qtd_por_caixa, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO 1', 'MARIA DOLORES', "
            "'111111111111', 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()
        self.pedido = {"id_stokki": 39770, "codigo_ps": "PS-39770",
                       "embarcador": "MARIA DOLORES", "situacao": "Waiting for Carrier"}
        self.itens = [{"linha": 1, "sku": "SKU1", "ean_linha": "111111111111",
                       "descricao": "PRODUTO 1", "qtd_embalagem": 6}]

    def test_estoque_zerado_vira_pendencia_de_saldo_na_tela(self):
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        wms_pedidos.reservar_pedido(self.conn, pid)

        p = wms_pedidos.pendencias(self.conn)

        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]["tipo_pendencia"], "SALDO")
        self.assertEqual(p[0]["falta_un"], 6)
        self.assertIn("falt", p[0]["motivo"].lower())
        self.assertEqual(p[0]["motivo_pendencia"], "")  # nao e falha de catalogo

    def test_pendencia_de_saldo_some_quando_o_galpao_endereca(self):
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        wms_pedidos.reservar_pedido(self.conn, pid)
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=6,
                                lote="L-A", validade="2026-12-31", destino="C9-E1-N1")

        wms_pedidos.reservar_pedido(self.conn, pid)

        self.assertEqual(wms_pedidos.pendencias(self.conn), [])

    def test_falha_de_catalogo_continua_distinguivel_da_falta_de_saldo(self):
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, [
            {"linha": 1, "sku": "FANTASMA", "ean_linha": "000", "descricao": "NAO EXISTE",
             "qtd_embalagem": 1}])
        wms_pedidos.reservar_pedido(self.conn, pid)

        p = wms_pedidos.pendencias(self.conn)

        self.assertEqual(p[0]["tipo_pendencia"], "CATALOGO")
        self.assertEqual(p[0]["falta_un"], 0)


class TestSaldoNegativoNaBaixa(BaseWMS):
    """
    I12: permitir_negativo=True e decisao consciente (a mercadoria ja saiu),
    mas o negativo precisa ficar visivel -- antes: erros: [], BAIXADO, e
    ninguem ficava sabendo.
    """

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, unidade, "
            "qtd_por_caixa, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO 1', 'MARIA DOLORES', "
            "'111111111111', 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=10,
                                lote="L-A", validade="2026-10-15", destino="C9-E1-N1")
        self.pid = wms_pedidos.registrar_pedido(
            self.conn, {"id_stokki": 39780, "codigo_ps": "PS-39780", "embarcador": "MARIA DOLORES",
                        "situacao": "Waiting for Carrier"},
            [{"linha": 1, "sku": "SKU1", "ean_linha": "111111111111",
              "descricao": "PRODUTO 1", "qtd_embalagem": 8}])
        wms_pedidos.reservar_pedido(self.conn, self.pid)
        self.conn.commit()
        # o saldo some entre a reserva e a expedicao (contagem/ajuste do
        # galpao) -- a baixa vai deixar a posicao negativa.
        wms.registrar_movimento(self.conn, tipo="AJUSTE", produto_id=1, quantidade=9, lote="L-A",
                                validade="2026-10-15", origem="C9-E1-N1", observacao="quebra no galpao")

    def test_baixa_negativa_volta_no_retorno_e_fica_gravada(self):
        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-39780")

        self.assertEqual(r["baixas"], 1)
        self.assertEqual(r["erros"], [])           # segue sendo permitido
        self.assertTrue(r["negativos"])            # mas nao passa calado
        self.assertIn("C9-E1-N1", r["negativos"][0])
        gravado = self.conn.execute("SELECT saldo_negativo FROM wms_pedidos WHERE id = ?",
                                    (self.pid,)).fetchone()["saldo_negativo"]
        self.assertIn("C9-E1-N1", gravado)
        self.assertEqual(wms._saldo_atual(self.conn, "C9-E1-N1", 1, "L-A", "2026-10-15"), -7)


class TestEnderecamentoDoRecebimento(BaseWMS):
    """I6 (idempotencia) e I13 (incremento no SQL) da contabilizacao."""

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, unidade, "
            "qtd_por_caixa, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO 1', 'MARIA DOLORES', "
            "'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()
        self.rid = wms_pedidos.registrar_recebimento(
            self.conn, {"id_stokki": 2478, "codigo": "#PE-2478", "embarcador": "MARIA DOLORES",
                        "situacao": "Recebido", "chegada": "22/09/2026"},
            [{"linha": 1, "sku": "SKU1", "ean_linha": "", "descricao": "PRODUTO 1",
              "qtd_embalagem": 10}])
        self.conn.commit()
        self.item_id = self.conn.execute(
            "SELECT id FROM wms_recebimento_itens WHERE recebimento_id = ?", (self.rid,)).fetchone()["id"]

    def test_mesma_chamada_repetida_nao_conta_duas_vezes(self):
        r1 = wms_pedidos.contabilizar_enderecamento(self.conn, self.rid, self.item_id, 10, "mov-1")
        r2 = wms_pedidos.contabilizar_enderecamento(self.conn, self.rid, self.item_id, 10, "mov-1")

        self.assertFalse(r1["duplicado"])
        self.assertTrue(r2["duplicado"])
        self.assertEqual(r2["item"]["qtd_enderecada"], 10)

    def test_dois_enderecamentos_diferentes_somam_e_fecham_o_recebimento(self):
        wms_pedidos.contabilizar_enderecamento(self.conn, self.rid, self.item_id, 4, "mov-1")
        r = wms_pedidos.contabilizar_enderecamento(self.conn, self.rid, self.item_id, 6, "mov-2")

        self.assertEqual(r["item"]["qtd_enderecada"], 10)
        self.assertEqual(r["recebimento"]["estado"], "ENDERECADO")


class TestPendencias(BaseWMS):
    def test_pendencias_lista_item_nao_resolvido_com_o_pedido(self):
        pid = wms_pedidos.registrar_pedido(
            self.conn,
            {"id_stokki": 40001, "codigo_ps": "PS-40001", "embarcador": "MARIA DOLORES",
             "situacao": "Waiting for Carrier"},
            [{"linha": 1, "sku": "FANTASMA", "ean_linha": "000",
              "descricao": "NAO EXISTE", "qtd_embalagem": 1}])
        wms_pedidos.reservar_pedido(self.conn, pid)
        p = wms_pedidos.pendencias(self.conn)
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]["codigo_ps"], "PS-40001")
        self.assertIn("nao encontrado", p[0]["motivo_pendencia"].lower())

    def test_pedido_cancelado_nao_aparece_na_pendencia(self):
        pid = wms_pedidos.registrar_pedido(
            self.conn,
            {"id_stokki": 40002, "codigo_ps": "PS-40002", "embarcador": "MARIA DOLORES",
             "situacao": "Waiting for Carrier"},
            [{"linha": 1, "sku": "FANTASMA", "ean_linha": "000",
              "descricao": "NAO EXISTE", "qtd_embalagem": 1}])
        wms_pedidos.reservar_pedido(self.conn, pid)
        wms_pedidos.cancelar_reservas(self.conn, pid, "pedido cancelado na Stokki")
        self.assertEqual(wms_pedidos.pendencias(self.conn), [])

    def test_limite_corta_a_lista(self):
        for i in range(3):
            pid = wms_pedidos.registrar_pedido(
                self.conn,
                {"id_stokki": 41000 + i, "codigo_ps": f"PS-{41000 + i}", "embarcador": "MARIA DOLORES",
                 "situacao": "Waiting for Carrier"},
                [{"linha": 1, "sku": "FANTASMA", "ean_linha": "000",
                  "descricao": "NAO EXISTE", "qtd_embalagem": 1}])
            wms_pedidos.reservar_pedido(self.conn, pid)
        self.assertEqual(len(wms_pedidos.pendencias(self.conn, limite=2)), 2)


class TestTrocarLote(BaseWMS):
    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, unidade, "
            "qtd_por_caixa, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO 1', 'MARIA DOLORES', "
            "'111111111111', 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=5,
                                lote="L-A", validade="2026-10-15", destino="C9-E1-N1")
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=5,
                                lote="L-B", validade="2026-12-31", destino="C9-E1-N2")
        self.pid = wms_pedidos.registrar_pedido(
            self.conn,
            {"id_stokki": 39751, "codigo_ps": "PS-39751", "embarcador": "MARIA DOLORES",
             "situacao": "Separating"},
            [{"linha": 1, "sku": "SKU1", "ean_linha": "111111111111",
              "descricao": "PRODUTO 1", "qtd_embalagem": 3}])
        wms_pedidos.reservar_pedido(self.conn, self.pid)

    def test_operador_pegou_outro_lote(self):
        reserva = self.conn.execute("SELECT * FROM wms_reservas WHERE estado='ATIVA'").fetchone()
        self.assertEqual(reserva["lote"], "L-A")  # FEFO sugeriu o que vence antes
        nova = wms_pedidos.trocar_lote_reserva(self.conn, reserva["id"], "C9-E1-N2", "L-B", "2026-12-31")
        self.assertEqual(nova["lote"], "L-B")
        self.assertEqual(nova["origem"], "MANUAL")
        self.assertEqual(nova["quantidade_un"], 3)
        ativas = self.conn.execute("SELECT COUNT(*) n FROM wms_reservas WHERE estado='ATIVA'").fetchone()["n"]
        self.assertEqual(ativas, 1)

    def test_bipar_o_proprio_lote_sugerido_e_confirmacao_nao_erro(self):
        # I4 da revisao: disponivel_por_lote ja desconta a propria reserva,
        # entao confirmar o lote que o FEFO sugeriu -- a acao mais natural
        # do operador -- dava "tem 0 UN disponivel".
        reserva = self.conn.execute("SELECT * FROM wms_reservas WHERE estado='ATIVA'").fetchone()
        self.assertEqual(reserva["lote"], "L-A")

        r = wms_pedidos.trocar_lote_reserva(self.conn, reserva["id"], "C9-E1-N1", "L-A", "2026-10-15")

        self.assertTrue(r["confirmada"])
        self.assertEqual(r["id"], reserva["id"])       # a mesma reserva, sem troca
        self.assertEqual(r["estado"], "ATIVA")
        n = self.conn.execute("SELECT COUNT(*) n FROM wms_reservas").fetchone()["n"]
        self.assertEqual(n, 1)                          # nao criou reserva nova

    def test_troca_de_verdade_continua_marcando_manual(self):
        reserva = self.conn.execute("SELECT * FROM wms_reservas WHERE estado='ATIVA'").fetchone()
        nova = wms_pedidos.trocar_lote_reserva(self.conn, reserva["id"], "C9-E1-N2", "L-B", "2026-12-31")
        self.assertFalse(nova["confirmada"])
        self.assertEqual(nova["origem"], "MANUAL")

    def test_trocar_pra_lote_sem_saldo_avisa_e_nao_troca(self):
        reserva = self.conn.execute("SELECT * FROM wms_reservas WHERE estado='ATIVA'").fetchone()
        with self.assertRaises(wms.ErroWMS):
            wms_pedidos.trocar_lote_reserva(self.conn, reserva["id"], "C9-E1-N2", "L-FANTASMA", "2027-01-01")


class TestReservasAntigas(BaseWMS):
    """
    Reserva ATIVA parada ha dias e sintoma de baixa que nunca veio: a
    mercadoria sumiu do disponivel sem ter saido do galpao e faz pedido
    novo nascer PARCIAL por falta que nao existe. A tela da equipe mostra
    quem passou do corte -- e so quem passou.
    """

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, unidade, "
            "qtd_por_caixa, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO A', 'MARIA DOLORES', "
            "'111111111111', 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=10,
                                lote="L-A", validade="2026-10-15", destino="C9-E1-N1")
        self.item = [{"linha": 1, "sku": "SKU1", "ean_linha": "111111111111",
                      "descricao": "PRODUTO A", "qtd_embalagem": 2}]

    def _pedido_reservado(self, id_stokki, dias_atras):
        """Pedido com reserva ATIVA criada ha `dias_atras` dias."""
        pid = wms_pedidos.registrar_pedido(
            self.conn, {"id_stokki": id_stokki, "codigo_ps": f"PS-{id_stokki}",
                        "embarcador": "MARIA DOLORES", "situacao": "Waiting for Carrier"}, self.item)
        wms_pedidos.reservar_pedido(self.conn, pid)
        criado = (datetime.now() - timedelta(days=dias_atras)).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute("UPDATE wms_reservas SET criado_em = ? WHERE pedido_id = ?", (criado, pid))
        self.conn.commit()
        return pid

    def test_reserva_velha_aparece_e_reserva_nova_nao(self):
        self._pedido_reservado(50001, dias_atras=9)
        self._pedido_reservado(50002, dias_atras=1)

        antigas = wms_pedidos.reservas_antigas(self.conn, dias=4)

        self.assertEqual([a["codigo_ps"] for a in antigas], ["PS-50001"])
        self.assertEqual(antigas[0]["reservas"], 1)
        self.assertEqual(antigas[0]["total_un"], 2)
        self.assertAlmostEqual(antigas[0]["dias"], 9, delta=0.2)

    def test_lista_o_que_esta_preso(self):
        self._pedido_reservado(50003, dias_atras=9)

        preso = wms_pedidos.reservas_antigas(self.conn, dias=4)[0]["itens"]

        self.assertEqual(len(preso), 1)
        self.assertEqual(preso[0]["descricao"], "PRODUTO A")
        self.assertEqual(preso[0]["posicao"], "C9-E1-N1")
        self.assertEqual(preso[0]["lote"], "L-A")
        self.assertEqual(preso[0]["quantidade_un"], 2)

    def test_corte_em_dias_manda(self):
        self._pedido_reservado(50004, dias_atras=5)

        self.assertEqual(len(wms_pedidos.reservas_antigas(self.conn, dias=4)), 1)
        self.assertEqual(len(wms_pedidos.reservas_antigas(self.conn, dias=6)), 0)

    def test_reserva_ja_baixada_ou_liberada_sai_da_lista(self):
        # so reserva ATIVA trava o disponivel -- CONSUMIDA (baixou) e
        # CANCELADA (liberou) nao sao sintoma de nada.
        self._pedido_reservado(50005, dias_atras=9)
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-50005")
        outro = self._pedido_reservado(50006, dias_atras=9)
        wms_pedidos.cancelar_reservas(self.conn, outro, "liberado na mao")
        self.conn.commit()

        self.assertEqual(wms_pedidos.reservas_antigas(self.conn, dias=4), [])


class TestLiberarReservaNaMao(BaseWMS):
    """
    Frente 3: saida manual pro que escapou das varreduras automaticas.
    Libera as reservas ATIVAS do pedido, devolve o disponivel e grava o
    motivo -- seis meses depois, "por que este pedido esta CANCELADO"
    precisa ter resposta.
    """

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, unidade, "
            "qtd_por_caixa, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO A', 'MARIA DOLORES', "
            "'111111111111', 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=10,
                                lote="L-A", validade="2026-10-15", destino="C9-E1-N1")
        self.pid = wms_pedidos.registrar_pedido(
            self.conn, {"id_stokki": 51000, "codigo_ps": "PS-51000",
                        "embarcador": "MARIA DOLORES", "situacao": "Waiting for Carrier"},
            [{"linha": 1, "sku": "SKU1", "ean_linha": "111111111111",
              "descricao": "PRODUTO A", "qtd_embalagem": 3}])
        wms_pedidos.reservar_pedido(self.conn, self.pid)
        self.conn.commit()

    def test_liberar_devolve_o_disponivel_e_grava_o_motivo(self):
        self.assertEqual(wms_pedidos.disponivel_por_lote(self.conn, 1)[0]["disponivel"], 7)

        n = wms_pedidos.cancelar_reservas(
            self.conn, self.pid, "Liberado manualmente por hugo: saiu por redespacho, baixa nao veio")
        self.conn.commit()

        self.assertEqual(n, 1)
        self.assertEqual(wms_pedidos.disponivel_por_lote(self.conn, 1)[0]["disponivel"], 10)
        pedido = self.conn.execute("SELECT * FROM wms_pedidos WHERE id = ?", (self.pid,)).fetchone()
        self.assertEqual(pedido["estado_reserva"], "CANCELADO")
        self.assertIn("Liberado manualmente por hugo", pedido["motivo_cancelamento"])

    def test_liberar_nao_mexe_no_saldo_fisico(self):
        # invariante central: reserva nunca toca wms_saldos.
        saldo_antes = self.conn.execute(
            "SELECT quantidade FROM wms_saldos WHERE produto_id = 1").fetchone()["quantidade"]

        wms_pedidos.cancelar_reservas(self.conn, self.pid, "engano")
        self.conn.commit()

        saldo_depois = self.conn.execute(
            "SELECT quantidade FROM wms_saldos WHERE produto_id = 1").fetchone()["quantidade"]
        self.assertEqual(saldo_antes, saldo_depois)
        n_mov = self.conn.execute("SELECT COUNT(*) n FROM wms_movimentos").fetchone()["n"]
        self.assertEqual(n_mov, 1)  # so a ENTRADA do setUp

    def test_pedido_liberado_some_das_reservas_antigas(self):
        self.conn.execute("UPDATE wms_reservas SET criado_em = '2026-01-01 08:00:00'")
        self.conn.commit()
        self.assertEqual(len(wms_pedidos.reservas_antigas(self.conn, dias=4)), 1)

        wms_pedidos.cancelar_reservas(self.conn, self.pid, "liberado na mao")
        self.conn.commit()

        self.assertEqual(wms_pedidos.reservas_antigas(self.conn, dias=4), [])


if __name__ == "__main__":
    unittest.main()
