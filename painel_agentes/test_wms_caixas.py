# -*- coding: utf-8 -*-
"""
Testes das caixas com serial e do codigo interno (dados).
Rodar (da raiz): py -3.11 -m unittest painel_agentes.test_wms_caixas -v
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import wms  # noqa: E402
import wms_caixas as cx  # noqa: E402
import wms_fila_impressao as fila  # noqa: E402
import wms_pedidos  # noqa: E402


class BaseCaixas(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        self.conn = cx.conectar(self.db)
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, dun, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (1, 900, '72400017', "
            "'COXINHA FESTA', 'MARIA DOLORES', 48, '724000170000', NULL, 6, 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (2, 901, NULL, 'SEM CODIGO', "
            "'MARIA DOLORES', 48, NULL, 1, 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def n(self, tabela):
        return self.conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]


class TestCaixas(BaseCaixas):
    def test_criar_tres_caixas_gera_seriais_em_sequencia_e_um_trabalho_na_fila(self):
        r = cx.criar_caixas(self.conn, produto_id=1, lote="l1", validade="15/12/2026", qtd_un=None, quantidade=3)
        self.assertEqual(r["seriais"], ["C0000001", "C0000002", "C0000003"])
        c = cx.obter_caixa(self.conn, "C0000002")
        self.assertEqual((c["situacao"], c["lote"], c["validade"], c["qtd_un"]), ("IMPRESSA", "L1", "2026-12-15", 6))
        self.assertEqual(c["produto"]["id"], 1)
        job = fila.obter(self.conn, r["impressao_id"])
        self.assertEqual(job["itens"], r["seriais"])
        self.assertEqual((job["tipo"], job["tamanho"], job["titulo"]), ("CAIXA", "10x5", "COXINHA FESTA · 3 caixas"))
        self.assertEqual(self.n("wms_fila_impressao"), 1)
        ev = self.conn.execute("SELECT de, para FROM wms_caixa_eventos").fetchall()
        self.assertEqual(len(ev), 3)
        self.assertEqual((ev[0][0], ev[0][1]), (None, "IMPRESSA"))

    def test_serial_continua_de_onde_parou(self):
        cx.criar_caixas(self.conn, produto_id=1, lote="L1", validade="2026-12-15", qtd_un=None, quantidade=2)
        r = cx.criar_caixas(self.conn, produto_id=1, lote="L2", validade="2026-12-20", qtd_un=None, quantidade=1)
        self.assertEqual(r["seriais"], ["C0000003"])

    def test_qtd_un_informada_prevalece(self):
        r = cx.criar_caixas(self.conn, produto_id=1, lote="L1", validade="2026-12-15", qtd_un=4, quantidade=1)
        self.assertEqual(cx.obter_caixa(self.conn, r["seriais"][0])["qtd_un"], 4)

    def test_quantidade_fora_de_1_a_200_recusa(self):
        for q in (0, 201):
            with self.assertRaises(wms.ErroWMS):
                cx.criar_caixas(self.conn, produto_id=1, lote="L1", validade="2026-12-15", qtd_un=None, quantidade=q)
        self.assertEqual(self.n("wms_caixas"), 0)
        r = cx.criar_caixas(self.conn, produto_id=1, lote="L1", validade="2026-12-15", qtd_un=None, quantidade=200)
        self.assertEqual(len(r["seriais"]), 200)

    def test_validade_obrigatoria_quando_produto_controla(self):
        with self.assertRaises(wms.ErroWMS):
            cx.criar_caixas(self.conn, produto_id=1, lote="L1", validade=None, qtd_un=None, quantidade=1)
        self.assertEqual(self.n("wms_caixas"), 0)

    def test_criar_caixas_nao_cria_nada_se_falhar(self):
        with self.assertRaises(wms.ErroWMS):
            cx.criar_caixas(self.conn, produto_id=999, lote="L1", validade="2026-12-15", qtd_un=None, quantidade=2)
        self.assertEqual(self.n("wms_caixas"), 0)
        self.assertEqual(self.n("wms_fila_impressao"), 0)
        self.assertEqual(self.n("wms_caixa_eventos"), 0)
        self.assertEqual(self.n("wms_contadores"), 0)
        self.assertFalse(self.conn.in_transaction)

    def test_lote_vazio_e_aceito(self):
        r = cx.criar_caixas(self.conn, produto_id=1, lote="", validade="2026-12-15", qtd_un=None, quantidade=1)
        self.assertEqual(cx.obter_caixa(self.conn, r["seriais"][0])["lote"], "")

    def test_obter_caixa_inexistente(self):
        self.assertIsNone(cx.obter_caixa(self.conn, "C9999999"))

    def test_produto_usa_caixas(self):
        self.assertTrue(cx.produto_usa_caixas({"embarcador_id": 48}, 48))
        self.assertFalse(cx.produto_usa_caixas({"embarcador_id": 7}, 48))


class TestCodigoInterno(BaseCaixas):
    def test_codigo_interno_e_unico_e_estavel(self):
        a = cx.garantir_codigo_interno(self.conn, 2)
        b = cx.garantir_codigo_interno(self.conn, 2)
        self.assertEqual((a, b), ("FLP000001", "FLP000001"))
        self.assertEqual(cx.garantir_codigo_interno(self.conn, 1), "FLP000002")

    def test_produto_inexistente(self):
        with self.assertRaises(wms.ErroWMS):
            cx.garantir_codigo_interno(self.conn, 999)


class TestGarantirTabelas(unittest.TestCase):
    def test_garantir_tabelas_em_conexao_do_wms_pedidos_e_idempotente(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = wms_pedidos.conectar(Path(tmp) / "dados.db")
            try:
                for _ in range(2):
                    cx.garantir_tabelas(conn)
                conn.commit()
                nomes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for t in ("wms_caixas", "wms_caixa_eventos", "wms_contadores", "wms_fila_impressao"):
                    self.assertIn(t, nomes)
            finally:
                conn.close()


class TestAtivarMoverCancelar(BaseCaixas):
    def setUp(self):
        super().setUp()
        wms.criar_area(self.conn, "C9", "CONTAINER", "Container 9")
        wms.gerar_posicoes(self.conn, "C9", estantes=2, niveis=2)
        self.conn.commit()

    def novas(self, q=1, produto=1):
        return cx.criar_caixas(self.conn, produto_id=produto, lote="L1", validade="2026-12-15",
                               qtd_un=None, quantidade=q)["seriais"]

    def saldo(self, pos="C9-E1-N1", produto=1):
        return wms._saldo_atual(self.conn, pos, produto, "L1", "2026-12-15")

    def novo_recebimento(self):
        rid = wms_pedidos.registrar_recebimento(
            self.conn, {"id_stokki": 2478, "codigo": "#PE-2478", "embarcador": "MARIA DOLORES",
                        "situacao": "Recebido", "chegada": "22/09/2026"},
            [{"linha": 1, "sku": "72400017", "ean_linha": "", "descricao": "COXINHA FESTA", "qtd_embalagem": 12},
             {"linha": 2, "sku": "", "ean_linha": "", "descricao": "SEM CODIGO", "qtd_embalagem": 3}])
        self.conn.commit()
        itens = self.conn.execute("SELECT id, produto_id FROM wms_recebimento_itens WHERE recebimento_id = ? "
                                  "ORDER BY linha", (rid,)).fetchall()
        return rid, itens

    def enderecada(self, item_id):
        return self.conn.execute("SELECT qtd_enderecada FROM wms_recebimento_itens WHERE id = ?",
                                 (item_id,)).fetchone()[0]

    def test_ativar_no_inventario_soma_no_saldo_e_vira_em_estoque(self):
        s = self.novas(2)
        cx.ativar_caixa(self.conn, s[0], "C9-E1-N1")
        cx.ativar_caixa(self.conn, s[1], "C9-E1-N1")
        self.assertEqual(self.saldo(), 12)  # AJUSTE soma; INVENTARIO daria 6
        c = cx.obter_caixa(self.conn, s[0])
        self.assertEqual((c["situacao"], c["posicao"]), ("EM_ESTOQUE", "C9-E1-N1"))
        mov = self.conn.execute("SELECT tipo, observacao FROM wms_movimentos WHERE uuid = ?",
                                (f"cx-{s[0]}-ativar",)).fetchone()
        self.assertEqual(mov["tipo"], "AJUSTE")
        self.assertEqual(mov["observacao"], f"Inventario de abertura - caixa {s[0]}")
        ev = self.conn.execute("SELECT de, para FROM wms_caixa_eventos WHERE serial = ? ORDER BY id",
                               (s[0],)).fetchall()
        self.assertEqual([(e[0], e[1]) for e in ev], [(None, "IMPRESSA"), ("IMPRESSA", "EM_ESTOQUE")])
        self.assertFalse(self.conn.in_transaction)

    def test_ativar_duas_vezes_soma_uma(self):
        s = self.novas()[0]
        r1 = cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        r = cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        self.assertFalse(r1["duplicado"])
        self.assertTrue(r["duplicado"])
        self.assertEqual(self.saldo(), 6)
        self.assertEqual(self.n("wms_caixa_eventos"), 2)

    def test_ativar_em_outra_posicao_recusa(self):
        s = self.novas()[0]
        cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        with self.assertRaisesRegex(wms.ErroWMS, "Use Mover"):
            cx.ativar_caixa(self.conn, s, "C9-E1-N2")
        self.assertEqual(self.saldo("C9-E1-N2"), 0)

    def test_ativar_cancelada_ou_inexistente_recusa(self):
        s = self.novas()[0]
        cx.cancelar_caixa(self.conn, s, "errada")
        with self.assertRaises(wms.ErroWMS):
            cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        with self.assertRaises(wms.ErroWMS):
            cx.ativar_caixa(self.conn, "C9999999", "C9-E1-N1")
        self.assertEqual(self.saldo(), 0)

    def test_ativar_pelo_recebimento_anda_a_barra_uma_vez(self):
        rid, itens = self.novo_recebimento()
        s = self.novas()[0]
        r = cx.ativar_caixa(self.conn, s, "C9-E1-N1", recebimento_id=rid, item_id=itens[0]["id"])
        self.assertFalse(r["duplicado"])
        r = cx.ativar_caixa(self.conn, s, "C9-E1-N1", recebimento_id=rid, item_id=itens[0]["id"])
        self.assertTrue(r["duplicado"])
        self.assertEqual(self.enderecada(itens[0]["id"]), 6)
        self.assertEqual(self.saldo(), 6)
        tipo = self.conn.execute("SELECT tipo FROM wms_movimentos WHERE uuid = ?", (f"cx-{s}-ativar",)).fetchone()[0]
        self.assertEqual(tipo, "ENTRADA")

    def test_ativar_recebimento_de_outro_produto_recusa(self):
        rid, itens = self.novo_recebimento()
        s = self.novas(produto=1)[0]
        with self.assertRaises(wms.ErroWMS):
            cx.ativar_caixa(self.conn, s, "C9-E1-N1", recebimento_id=rid, item_id=itens[1]["id"])
        self.assertEqual(self.saldo(), 0)
        self.assertEqual(cx.obter_caixa(self.conn, s)["situacao"], "IMPRESSA")
        self.assertEqual(self.n("wms_movimentos"), 0)

    def test_ativar_item_de_outro_recebimento_recusa(self):
        rid, itens = self.novo_recebimento()
        s = self.novas()[0]
        with self.assertRaises(wms.ErroWMS):
            cx.ativar_caixa(self.conn, s, "C9-E1-N1", recebimento_id=rid + 99, item_id=itens[0]["id"])
        self.assertEqual(self.n("wms_movimentos"), 0)

    def _anunciar(self, item_id, qtd_un):
        self.conn.execute("UPDATE wms_recebimento_itens SET qtd_un = ? WHERE id = ?", (qtd_un, item_id))
        self.conn.commit()

    def test_caixa_alem_do_anunciado_pede_confirmacao(self):
        rid, itens = self.novo_recebimento()
        self._anunciar(itens[0]["id"], 6)       # anunciado = 1 caixa de 6
        s = self.novas(2)
        cx.ativar_caixa(self.conn, s[0], "C9-E1-N1", recebimento_id=rid, item_id=itens[0]["id"])
        with self.assertRaises(cx.SobraRecebimento) as ctx:
            cx.ativar_caixa(self.conn, s[1], "C9-E1-N1", recebimento_id=rid, item_id=itens[0]["id"])
        self.assertEqual((ctx.exception.dados["anunciado"], ctx.exception.dados["recebido"],
                          ctx.exception.dados["depois"], ctx.exception.dados["serial"]), (6, 6, 12, s[1]))
        # reconferir: nada mudou
        self.assertEqual(cx.obter_caixa(self.conn, s[1])["situacao"], "IMPRESSA")
        self.assertEqual((self.enderecada(itens[0]["id"]), self.saldo()), (6, 6))
        # confirmar: entra como sobra
        cx.ativar_caixa(self.conn, s[1], "C9-E1-N1", recebimento_id=rid, item_id=itens[0]["id"],
                        confirmar_sobra=True)
        self.assertEqual((self.enderecada(itens[0]["id"]), self.saldo()), (12, 12))

    def test_repeticao_de_caixa_ja_contada_nao_pede_confirmacao(self):
        rid, itens = self.novo_recebimento()
        self._anunciar(itens[0]["id"], 6)
        s = self.novas()[0]
        cx.ativar_caixa(self.conn, s, "C9-E1-N1", recebimento_id=rid, item_id=itens[0]["id"])
        r = cx.ativar_caixa(self.conn, s, "C9-E1-N1", recebimento_id=rid, item_id=itens[0]["id"])
        self.assertTrue(r["duplicado"])
        self.assertEqual(self.enderecada(itens[0]["id"]), 6)

    def test_recebimento_encerrado_nao_aceita_caixa_nova(self):
        rid, itens = self.novo_recebimento()
        self.conn.execute("UPDATE wms_recebimentos SET estado = 'ENDERECADO' WHERE id = ?", (rid,))
        self.conn.commit()
        s = self.novas()[0]
        with self.assertRaisesRegex(wms.ErroWMS, "ja encerrado"):
            cx.ativar_caixa(self.conn, s, "C9-E1-N1", recebimento_id=rid, item_id=itens[0]["id"],
                            confirmar_sobra=True)
        self.assertEqual(cx.obter_caixa(self.conn, s)["situacao"], "IMPRESSA")
        self.assertEqual(self.saldo(), 0)

    def test_queda_entre_movimento_e_caixa_completa_sem_somar(self):
        s = self.novas()[0]
        wms.registrar_movimento(self.conn, tipo="AJUSTE", produto_id=1, quantidade=6, lote="L1",
                                validade="2026-12-15", destino="C9-E1-N1", uuid=f"cx-{s}-ativar",
                                observacao=f"Inventario de abertura - caixa {s}")
        self.assertEqual(cx.obter_caixa(self.conn, s)["situacao"], "IMPRESSA")
        r = cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        self.assertTrue(r["duplicado"])
        self.assertEqual(cx.obter_caixa(self.conn, s)["situacao"], "EM_ESTOQUE")
        self.assertEqual(self.saldo(), 6)

    def test_queda_no_recebimento_completa_barra_na_repeticao(self):
        rid, itens = self.novo_recebimento()
        s = self.novas()[0]
        # queda real: a ENTRADA e a caixa foram gravadas, mas a barra do recebimento falhou
        with mock.patch.object(wms_pedidos, "contabilizar_enderecamento", side_effect=RuntimeError("queda")):
            with self.assertRaises(RuntimeError):
                cx.ativar_caixa(self.conn, s, "C9-E1-N1", recebimento_id=rid, item_id=itens[0]["id"])
        self.assertEqual(self.enderecada(itens[0]["id"]), 0)
        self.assertEqual(cx.obter_caixa(self.conn, s)["situacao"], "EM_ESTOQUE")
        r = cx.ativar_caixa(self.conn, s, "C9-E1-N1", recebimento_id=rid, item_id=itens[0]["id"])
        self.assertTrue(r["duplicado"])
        self.assertEqual(self.enderecada(itens[0]["id"]), 6)
        self.assertEqual(self.saldo(), 6)

    def test_caixa_ativada_no_inventario_nao_entra_no_recebimento(self):
        rid, itens = self.novo_recebimento()
        s = self.novas()[0]
        cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        with self.assertRaisesRegex(wms.ErroWMS, "ativada no inventario"):
            cx.ativar_caixa(self.conn, s, "C9-E1-N1", recebimento_id=rid, item_id=itens[0]["id"])
        self.assertEqual(self.enderecada(itens[0]["id"]), 0)
        self.assertEqual(self.saldo(), 6)

    def test_queda_ajuste_gravado_e_depois_recebimento_recusa_sem_mexer(self):
        rid, itens = self.novo_recebimento()
        s = self.novas()[0]
        wms.registrar_movimento(self.conn, tipo="AJUSTE", produto_id=1, quantidade=6, lote="L1",
                                validade="2026-12-15", destino="C9-E1-N1", uuid=f"cx-{s}-ativar",
                                observacao=f"Inventario de abertura - caixa {s}")
        with self.assertRaises(wms.ErroWMS):
            cx.ativar_caixa(self.conn, s, "C9-E1-N1", recebimento_id=rid, item_id=itens[0]["id"])
        self.assertEqual(self.enderecada(itens[0]["id"]), 0)
        self.assertEqual(cx.obter_caixa(self.conn, s)["situacao"], "IMPRESSA")

    def test_reenvio_de_mover_antigo_nao_muda_caixa(self):
        a = self.novas()[0]
        cx.ativar_caixa(self.conn, a, "C9-E1-N1")
        cx.mover_caixa(self.conn, a, "C9-E1-N2", uuid="dev-1")
        cx.mover_caixa(self.conn, a, "C9-E1-N1", uuid="dev-2")
        r = cx.mover_caixa(self.conn, a, "C9-E1-N2", uuid="dev-1")  # reenvio atrasado
        self.assertTrue(r["duplicado"])
        self.assertEqual(cx.obter_caixa(self.conn, a)["posicao"], "C9-E1-N1")
        self.assertEqual((self.saldo("C9-E1-N1"), self.saldo("C9-E1-N2")), (6, 0))

    def test_mover_com_uuid_de_outra_caixa_recusa(self):
        a, b = self.novas(2)
        cx.ativar_caixa(self.conn, a, "C9-E1-N1")
        cx.ativar_caixa(self.conn, b, "C9-E1-N1")
        cx.mover_caixa(self.conn, a, "C9-E1-N2", uuid="dev-x")
        with self.assertRaises(wms.ErroWMS):
            cx.mover_caixa(self.conn, b, "C9-E1-N2", uuid="dev-x")
        self.assertEqual(cx.obter_caixa(self.conn, b)["posicao"], "C9-E1-N1")
        self.assertEqual((self.saldo("C9-E1-N1"), self.saldo("C9-E1-N2")), (6, 6))

    def test_queda_do_mover_completa_a_caixa(self):
        a = self.novas()[0]
        cx.ativar_caixa(self.conn, a, "C9-E1-N1")
        wms.registrar_movimento(self.conn, tipo="TRANSFERENCIA", produto_id=1, quantidade=6, lote="L1",
                                validade="2026-12-15", origem="C9-E1-N1", destino="C9-E1-N2",
                                uuid="dev-q", observacao=f"Caixa {a}")
        r = cx.mover_caixa(self.conn, a, "C9-E1-N2", uuid="dev-q")
        self.assertTrue(r["duplicado"])
        self.assertEqual(r["caixa"]["posicao"], "C9-E1-N2")
        self.assertEqual((self.saldo("C9-E1-N1"), self.saldo("C9-E1-N2")), (0, 6))

    def test_cancelar_impressa_com_ativar_gravado_tira_o_saldo_orfao(self):
        s = self.novas()[0]
        wms.registrar_movimento(self.conn, tipo="AJUSTE", produto_id=1, quantidade=6, lote="L1",
                                validade="2026-12-15", destino="C9-E1-N1", uuid=f"cx-{s}-ativar",
                                observacao=f"Inventario de abertura - caixa {s}")
        self.assertEqual(self.saldo(), 6)
        r = cx.cancelar_caixa(self.conn, s, "etiqueta errada")
        self.assertEqual(r["caixa"]["situacao"], "CANCELADA")
        self.assertEqual(self.saldo(), 0)
        self.assertEqual(self.n("wms_movimentos"), 2)

    def test_mover_caixa_leva_o_saldo_junto(self):
        s = self.novas()[0]
        cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        r = cx.mover_caixa(self.conn, s, "C9-E1-N2", uuid="dev-1")
        self.assertEqual(r["caixa"]["posicao"], "C9-E1-N2")
        self.assertEqual((self.saldo("C9-E1-N1"), self.saldo("C9-E1-N2")), (0, 6))
        r = cx.mover_caixa(self.conn, s, "C9-E1-N2", uuid="dev-1")  # resposta perdida
        self.assertTrue(r["duplicado"])
        self.assertEqual((self.saldo("C9-E1-N1"), self.saldo("C9-E1-N2")), (0, 6))
        self.assertEqual(self.n("wms_caixa_eventos"), 3)

    def test_mover_para_mesma_posicao_recusa(self):
        s = self.novas()[0]
        cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        with self.assertRaises(wms.ErroWMS):
            cx.mover_caixa(self.conn, s, "C9-E1-N1", uuid="dev-2")
        self.assertEqual(self.saldo(), 6)

    def test_mover_exige_uuid_e_caixa_em_estoque(self):
        s = self.novas()[0]
        with self.assertRaises(wms.ErroWMS):
            cx.mover_caixa(self.conn, s, "C9-E1-N2", uuid="dev-3")  # IMPRESSA
        cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        with self.assertRaises(wms.ErroWMS):
            cx.mover_caixa(self.conn, s, "C9-E1-N2", uuid="")

    def test_cancelar_impressa_sem_movimento_e_em_estoque_com_ajuste(self):
        a, b = self.novas(2)
        cx.cancelar_caixa(self.conn, a, "etiqueta errada")
        self.assertEqual(cx.obter_caixa(self.conn, a)["situacao"], "CANCELADA")
        self.assertEqual(self.n("wms_movimentos"), 0)
        cx.ativar_caixa(self.conn, b, "C9-E1-N1")
        self.assertEqual(self.saldo(), 6)
        r = cx.cancelar_caixa(self.conn, b, "caixa avariada")
        self.assertEqual(r["caixa"]["situacao"], "CANCELADA")
        self.assertEqual(self.saldo(), 0)
        mov = self.conn.execute("SELECT tipo, observacao FROM wms_movimentos WHERE uuid = ?",
                                (f"cx-{b}-cancelar",)).fetchone()
        self.assertEqual(mov["tipo"], "AJUSTE")
        self.assertIn("caixa avariada", mov["observacao"])
        self.assertIn(b, mov["observacao"])

    def test_cancelar_sem_motivo_recusa(self):
        s = self.novas()[0]
        cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        for m in ("", "   ", None):
            with self.assertRaises(wms.ErroWMS):
                cx.cancelar_caixa(self.conn, s, m)
        self.assertEqual(self.saldo(), 6)
        self.assertEqual(cx.obter_caixa(self.conn, s)["situacao"], "EM_ESTOQUE")

    def test_cancelar_expedida_ou_cancelada_recusa(self):
        s = self.novas()[0]
        cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        self.conn.execute("UPDATE wms_caixas SET situacao = 'EXPEDIDA' WHERE serial = ?", (s,))
        self.conn.commit()
        with self.assertRaises(wms.ErroWMS):
            cx.cancelar_caixa(self.conn, s, "x")
        self.assertEqual(self.saldo(), 6)
        t = self.novas()[0]
        cx.cancelar_caixa(self.conn, t, "x")
        with self.assertRaises(wms.ErroWMS):
            cx.cancelar_caixa(self.conn, t, "x")

    def test_cancelar_repetido_depois_de_queda_completa_sem_tirar_duas_vezes(self):
        s = self.novas()[0]
        cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        wms.registrar_movimento(self.conn, tipo="AJUSTE", produto_id=1, quantidade=6, lote="L1",
                                validade="2026-12-15", origem="C9-E1-N1", uuid=f"cx-{s}-cancelar",
                                observacao=f"Cancelamento da caixa {s}: queda")
        r = cx.cancelar_caixa(self.conn, s, "queda")
        self.assertTrue(r["duplicado"])
        self.assertEqual(r["caixa"]["situacao"], "CANCELADA")
        self.assertEqual(self.saldo(), 0)


class TestBaixaConsomeCaixas(BaseCaixas):
    POS = "C9-E1-N1"

    def setUp(self):
        super().setUp()
        wms.criar_area(self.conn, "C9", "CONTAINER", "Container 9")
        wms.gerar_posicoes(self.conn, "C9", estantes=2, niveis=2)
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (3, 902, 'OUTRO1', 'OUTRO EMB', "
            "'OUTRO', 77, '333333333333', 1, 'UN', 0, '2026-09-21 10:00:00')")
        self.conn.commit()

    def ativar(self, n=1, produto=1):
        seriais = cx.criar_caixas(self.conn, produto_id=produto, lote="L1", validade="2026-12-15",
                                  qtd_un=None, quantidade=n)["seriais"]
        for s in seriais:
            cx.ativar_caixa(self.conn, s, self.POS)
        return seriais

    def pedido(self, id_stokki, qtd, sku="72400017", ean="724000170000", emb="MARIA DOLORES"):
        pid = wms_pedidos.registrar_pedido(
            self.conn, {"id_stokki": id_stokki, "codigo_ps": f"PS-{id_stokki}", "embarcador": emb,
                        "situacao": "Waiting for Carrier"},
            [{"linha": 1, "sku": sku, "ean_linha": ean, "descricao": "X", "qtd_embalagem": qtd}])
        wms_pedidos.reservar_pedido(self.conn, pid)
        self.conn.commit()
        return pid

    def sit(self, serial):
        c = cx.obter_caixa(self.conn, serial)
        return c["situacao"], c["qtd_un"]

    def test_baixa_inteira_marca_caixa_expedida(self):
        s = self.ativar(1)[0]
        self.pedido(1001, 6)
        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-1001")
        self.assertEqual((r["baixas"], r["erros"]), (1, []))
        self.assertEqual(self.sit(s), ("EXPEDIDA", 6))
        self.assertEqual(cx.divergencias(self.conn, 48), [])
        ev = self.conn.execute("SELECT para, movimento_uuid FROM wms_caixa_eventos WHERE serial = ? "
                               "ORDER BY id DESC LIMIT 1", (s,)).fetchone()
        self.assertEqual(ev["para"], "EXPEDIDA")
        self.assertTrue(ev["movimento_uuid"].startswith("ps-1001-reserva-"))

    def test_baixa_parcial_deixa_caixa_aberta_e_checagem_zerada(self):
        s = self.ativar(1)[0]
        self.pedido(1002, 4)
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-1002")
        self.assertEqual(self.sit(s), ("ABERTA", 2))
        self.assertEqual(wms._saldo_atual(self.conn, self.POS, 1, "L1", "2026-12-15"), 2)
        self.assertEqual(cx.divergencias(self.conn, 48), [])
        # cancelar a aberta tira so o que sobrou
        cx.cancelar_caixa(self.conn, s, "sobra")
        self.assertEqual(wms._saldo_atual(self.conn, self.POS, 1, "L1", "2026-12-15"), 0)

    def test_aberta_e_consumida_primeiro_depois_serial_crescente(self):
        s = self.ativar(3)
        self.pedido(1003, 3)
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-1003")          # abre s[0] (resta 3)
        self.pedido(1004, 5)
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-1004")          # s[0] inteira (3) + s[1] parcial (2)
        self.assertEqual(self.sit(s[0]), ("EXPEDIDA", 3))
        self.assertEqual(self.sit(s[1]), ("ABERTA", 4))
        self.assertEqual(self.sit(s[2]), ("EM_ESTOQUE", 6))
        self.assertEqual(cx.divergencias(self.conn, 48), [])

    def test_baixa_rodada_duas_vezes_nao_consome_duas_vezes(self):
        s = self.ativar(2)
        self.pedido(1005, 4)
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-1005")
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-1005")
        self.assertEqual(self.sit(s[0]), ("ABERTA", 2))
        self.assertEqual(self.sit(s[1]), ("EM_ESTOQUE", 6))
        movs = self.conn.execute("SELECT movimento_uuid FROM wms_caixa_eventos "
                                 "WHERE movimento_uuid LIKE 'ps-1005%'").fetchall()
        self.assertEqual(len(movs), 1)

    def test_consumir_caixas_e_idempotente_mesmo_com_saida_duplicada(self):
        s = self.ativar(1)[0]
        a = cx.consumir_caixas(self.conn, posicao=self.POS, produto_id=1, lote="L1", validade="2026-12-15",
                               qtd_un=2, movimento_uuid="mov-x")
        self.conn.commit()
        b = cx.consumir_caixas(self.conn, posicao=self.POS, produto_id=1, lote="L1", validade="2026-12-15",
                               qtd_un=2, movimento_uuid="mov-x")
        self.assertEqual((a["aberta"], a["ja_consumido"]), (s, False))
        self.assertTrue(b["ja_consumido"])
        self.assertEqual(self.sit(s), ("ABERTA", 4))

    def test_validade_vazia_e_none_casam_as_mesmas_caixas(self):
        self.conn.execute(
            "INSERT INTO wms_caixas (serial, produto_id, lote, validade, qtd_un, posicao, situacao, criado_em, "
            "atualizado_em) VALUES ('C9999999', 3, '', NULL, 5, ?, 'EM_ESTOQUE', 'x', 'x')", (self.POS,))
        r = cx.consumir_caixas(self.conn, posicao=self.POS, produto_id=3, lote="", validade="",
                               qtd_un=5, movimento_uuid="mov-y")
        self.assertEqual(r["expedidas"], ["C9999999"])

    def test_falta_de_caixa_vira_faltou_un_sem_erro(self):
        s = self.ativar(1)[0]
        r = cx.consumir_caixas(self.conn, posicao=self.POS, produto_id=1, lote="L1", validade="2026-12-15",
                               qtd_un=10, movimento_uuid="mov-z")
        self.assertEqual((r["expedidas"], r["faltou_un"]), ([s], 4))

    def test_produto_sem_caixa_nao_quebra_a_baixa(self):
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=3, quantidade=10, lote="L9",
                                destino=self.POS)
        self.pedido(1006, 4, sku="OUTRO1", ean="333333333333", emb="OUTRO")
        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-1006")
        self.assertEqual((r["baixas"], r["erros"]), (1, []))
        self.assertEqual(wms._saldo_atual(self.conn, self.POS, 3, "L9", ""), 6)
        self.assertEqual(self.conn.execute("SELECT estado_reserva FROM wms_pedidos WHERE id_stokki = 1006")
                         .fetchone()[0], "BAIXADO")
        self.assertEqual(self.n("wms_caixa_eventos"), 0)

    def test_falha_no_consumo_nao_quebra_a_baixa_nem_deixa_transacao_aberta(self):
        s = self.ativar(1)[0]
        self.pedido(1007, 6)
        with mock.patch.object(cx, "consumir_caixas", side_effect=RuntimeError("quebrou")):
            r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-1007")
        self.assertEqual((r["baixas"], r["erros"]), (1, []))
        self.assertEqual(self.conn.execute("SELECT estado_reserva FROM wms_pedidos WHERE id_stokki = 1007")
                         .fetchone()[0], "BAIXADO")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wms_movimentos WHERE tipo='SAIDA'")
                         .fetchone()[0], 1)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.sit(s), ("EM_ESTOQUE", 6))
        self.assertEqual(len(cx.divergencias(self.conn, 48)), 1)   # a checagem mostra

    def test_saida_ja_gravada_com_reserva_ativa_consome_caixa_uma_vez(self):
        s = self.ativar(1)[0]
        pid = self.pedido(1008, 4)
        rid = self.conn.execute("SELECT id FROM wms_reservas WHERE pedido_id = ?", (pid,)).fetchone()[0]
        wms.registrar_movimento(self.conn, tipo="SAIDA", produto_id=1, quantidade=4, lote="L1",
                                validade="2026-12-15", origem=self.POS, uuid=f"ps-1008-reserva-{rid}")
        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-1008")   # SAIDA duplicada
        self.assertEqual((r["baixas"], r["erros"]), (0, []))
        self.assertEqual(self.sit(s), ("ABERTA", 2))
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-1008")
        self.assertEqual(self.sit(s), ("ABERTA", 2))
        self.assertEqual(cx.divergencias(self.conn, 48), [])
        n = self.conn.execute("SELECT COUNT(*) FROM wms_caixa_eventos WHERE movimento_uuid = ?",
                              (f"ps-1008-reserva-{rid}",)).fetchone()[0]
        self.assertEqual(n, 1)

    def _consumir_com_intervalo(self, intervalo):
        """Roda consumir_caixas (2 UN) e, entre o SELECT e o UPDATE, `intervalo(outra)`."""
        outra = wms_pedidos.conectar(self.db)
        real, feito = wms.agora, {"x": False}

        def agora_intercalado():
            if not feito["x"]:
                feito["x"] = True
                intervalo(outra)
            return real()
        try:
            with mock.patch.object(wms, "agora", agora_intercalado):
                r = cx.consumir_caixas(self.conn, posicao=self.POS, produto_id=1, lote="L1",
                                       validade="2026-12-15", qtd_un=2, movimento_uuid="mov-A")
                self.conn.commit()
            return r
        finally:
            outra.close()

    def test_corrida_com_outra_baixa_nao_sobrescreve_a_caixa(self):
        s = self.ativar(1)[0]

        def outra_baixa(outra):
            cx.consumir_caixas(outra, posicao=self.POS, produto_id=1, lote="L1", validade="2026-12-15",
                               qtd_un=3, movimento_uuid="mov-B")
            outra.commit()
        r = self._consumir_com_intervalo(outra_baixa)
        self.assertEqual(self.sit(s), ("ABERTA", 3))      # so a de B; a de A foi pulada
        self.assertEqual((r["aberta"], r["faltou_un"]), (None, 2))
        n = self.conn.execute("SELECT COUNT(*) FROM wms_caixa_eventos WHERE movimento_uuid = 'mov-A'").fetchone()[0]
        self.assertEqual(n, 0)

    def test_caixa_cancelada_no_meio_do_consumo_nao_ressuscita(self):
        s = self.ativar(1)[0]
        self._consumir_com_intervalo(lambda outra: cx.cancelar_caixa(outra, s, "queda"))
        self.assertEqual(self.sit(s)[0], "CANCELADA")
        self.assertEqual(self.saldo_pos(), 0)

    def saldo_pos(self):
        return wms._saldo_atual(self.conn, self.POS, 1, "L1", "2026-12-15")

    def test_baixa_abre_transacao_imediata_antes_de_consumir(self):
        self.ativar(1)
        self.pedido(1009, 4)
        vistos = []
        real = cx.consumir_caixas

        def espia(conn, **kw):
            vistos.append(conn.in_transaction)
            return real(conn, **kw)
        with mock.patch.object(cx, "consumir_caixas", side_effect=espia):
            wms_pedidos.baixar_por_expedicao(self.conn, "PS-1009")
        self.assertEqual(vistos, [True])

    def test_zerar_saldo_recusa_com_caixas_ativas_mas_plano_le(self):
        s = self.ativar(1)[0]
        self.assertEqual(len(cx.plano_zerar_saldo(self.conn, 48)), 1)
        with self.assertRaises(wms.ErroWMS):
            cx.zerar_saldo(self.conn, 48, "limpeza")
        self.assertEqual(self.saldo_pos(), 6)
        cx.cancelar_caixa(self.conn, s, "teste")
        self.assertEqual(cx.zerar_saldo(self.conn, 48, "limpeza"), 0)

    def test_divergencia_aparece_dos_dois_lados(self):
        self.ativar(1)
        # saldo sem caixa
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=10, lote="L2",
                                validade="2026-11-01", destino=self.POS)
        # caixa sem saldo
        self.conn.execute(
            "INSERT INTO wms_caixas (serial, produto_id, lote, validade, qtd_un, posicao, situacao, criado_em, "
            "atualizado_em) VALUES ('C9999998', 1, 'L3', '2026-10-01', 6, 'C9-E1-N2', 'EM_ESTOQUE', 'x', 'x')")
        self.conn.commit()
        # outro embarcador nao entra
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=3, quantidade=9, lote="L9",
                                destino=self.POS)
        self.conn.commit()
        d = {(x["lote"]): x for x in cx.divergencias(self.conn, 48)}
        self.assertEqual(set(d), {"L2", "L3"})
        self.assertEqual((d["L2"]["saldo"], d["L2"]["caixas"], d["L2"]["sku"]), (10, 0, "72400017"))
        self.assertEqual((d["L3"]["saldo"], d["L3"]["caixas"]), (0, 6))

    def test_zerar_saldo_positivo_e_negativo(self):
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=10, lote="L2",
                                validade="2026-11-01", destino=self.POS)
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=1, lote="L5", validade="2026-11-02",
                                destino="C9-E1-N2")
        wms.registrar_movimento(self.conn, tipo="SAIDA", produto_id=1, quantidade=3, lote="L5", validade="2026-11-02",
                                origem="C9-E1-N2", permitir_negativo=True)   # L5: 1 - 3 = -2
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=3, quantidade=9, lote="L9",
                                destino=self.POS)
        self.conn.commit()
        plano = cx.plano_zerar_saldo(self.conn, 48)
        self.assertEqual(sorted(p["quantidade"] for p in plano), [-2, 10])
        self.assertEqual(self.n("wms_movimentos"), 4)   # plano nao escreveu
        with self.assertRaises(wms.ErroWMS):
            cx.zerar_saldo(self.conn, 48, "  ")
        self.assertEqual(cx.zerar_saldo(self.conn, 48, "limpeza do saldo antigo"), 2)
        self.assertEqual(cx.plano_zerar_saldo(self.conn, 48), [])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM wms_saldos s JOIN wms_produtos p ON p.id = s.produto_id "
            "WHERE p.embarcador_id = 48").fetchone()[0], 0)
        self.assertEqual(wms._saldo_atual(self.conn, self.POS, 3, "L9", ""), 9)   # outro embarcador intacto
        m = self.conn.execute("SELECT uuid, observacao FROM wms_movimentos WHERE uuid LIKE 'zerar-%' "
                              "ORDER BY id").fetchall()
        self.assertEqual(len(m), 2)
        self.assertRegex(m[0]["uuid"], r"^zerar-48-C9-E1-N\d-1-L\d-[\d-]*\d{8}$")
        self.assertIn("limpeza do saldo antigo", m[0]["observacao"])
        self.assertEqual(cx.zerar_saldo(self.conn, 48, "de novo"), 0)



class TestEndurecimento(BaseCaixas):
    def setUp(self):
        super().setUp()
        wms.criar_area(self.conn, "C9", "CONTAINER", "Container 9")
        wms.gerar_posicoes(self.conn, "C9", estantes=2, niveis=2)
        self.conn.commit()

    def novas(self, q=1, produto=1):
        return cx.criar_caixas(self.conn, produto_id=produto, lote="L1", validade="2026-12-15",
                               qtd_un=None, quantidade=q)["seriais"]

    def alheio(self, uuid, tipo="AJUSTE", produto=2, qtd=6, obs="x", lote="L1"):
        wms.registrar_movimento(self.conn, tipo=tipo, produto_id=produto, quantidade=qtd, lote=lote,
                                validade="2026-12-15", destino="C9-E1-N1", uuid=uuid, observacao=obs)

    def test_ativar_recusa_movimento_alheio_com_uuid_da_caixa(self):
        for produto, qtd, obs, lote in ((2, 6, None, "L1"), (1, 7, None, "L1"), (1, 6, "Caixa C0000099", "L1"),
                                        (1, 6, None, "OUTRO")):
            with self.subTest(produto=produto, qtd=qtd, obs=obs, lote=lote):
                s = self.novas(produto=1)[0]
                self.alheio(f"cx-{s}-ativar", produto=produto, qtd=qtd, obs=obs or f"Caixa {s}", lote=lote)
                with self.assertRaises(wms.ErroWMS) as ctx:
                    cx.ativar_caixa(self.conn, s, "C9-E1-N1")
                self.assertIn("nao pertence", str(ctx.exception))
                self.assertEqual(cx.obter_caixa(self.conn, s)["situacao"], "IMPRESSA")

    def test_ativar_recusa_tipo_errado(self):
        s = self.novas()[0]
        self.alheio(f"cx-{s}-ativar", tipo="ENTRADA", produto=1, obs=f"Caixa {s}")
        self.conn.execute("UPDATE wms_movimentos SET tipo = 'SAIDA' WHERE uuid = ?", (f"cx-{s}-ativar",))
        self.conn.commit()
        with self.assertRaises(wms.ErroWMS):
            cx.ativar_caixa(self.conn, s, "C9-E1-N1")

    def test_cancelar_recusa_movimento_alheio_com_uuid_da_caixa(self):
        s = self.novas()[0]
        cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        self.alheio(f"cx-{s}-cancelar", produto=2)
        with self.assertRaises(wms.ErroWMS):
            cx.cancelar_caixa(self.conn, s, "erro")
        self.assertEqual(cx.obter_caixa(self.conn, s)["situacao"], "EM_ESTOQUE")

    def test_cancelar_impressa_recusa_ativar_alheio(self):
        s = self.novas()[0]
        self.alheio(f"cx-{s}-ativar", produto=2)
        with self.assertRaises(wms.ErroWMS):
            cx.cancelar_caixa(self.conn, s, "erro")
        self.assertEqual(cx.obter_caixa(self.conn, s)["situacao"], "IMPRESSA")

    def test_qtd_un_nao_finita_recusa(self):
        for q in (float("nan"), float("inf"), 0, -1, 100001, "abc"):
            with self.subTest(q=q):
                with self.assertRaises(wms.ErroWMS):
                    cx.criar_caixas(self.conn, produto_id=1, lote="L1", validade="2026-12-15",
                                    qtd_un=q, quantidade=1)
        self.assertEqual(self.n("wms_caixas"), 0)
        cx.criar_caixas(self.conn, produto_id=1, lote="L1", validade="2026-12-15", qtd_un=100000, quantidade=1)

    def test_ativar_de_novo_apos_consumo_parcial_e_duplicado(self):
        s = self.novas()[0]
        cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        cx.consumir_caixas(self.conn, posicao="C9-E1-N1", produto_id=1, lote="L1", validade="2026-12-15",
                           qtd_un=2, movimento_uuid="mov-x")
        self.conn.commit()
        r = cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        self.assertTrue(r["duplicado"])
        self.assertEqual(r["caixa"]["situacao"], "ABERTA")

    def test_ativar_cancelada_diz_que_esta_cancelada(self):
        s = self.novas()[0]
        cx.cancelar_caixa(self.conn, s, "erro")
        with self.assertRaises(wms.ErroWMS) as ctx:
            cx.ativar_caixa(self.conn, s, "C9-E1-N1")
        self.assertIn("CANCELADA", str(ctx.exception))

    def test_registrar_movimento_recusa_quantidade_nao_finita(self):
        for q in ("nan", "inf", float("inf")):
            with self.subTest(q=q), self.assertRaises(wms.ErroWMS):
                wms.registrar_movimento(self.conn, tipo="AJUSTE", produto_id=1, quantidade=q, lote="L1",
                                        validade="2026-12-15", destino="C9-E1-N1", uuid=f"n-{q}")


class TestRevisaoFinal(BaseCaixas):
    """Correcoes da revisao final: mover reservada, reparo (acertar), zerar com reserva, M1."""
    POS = TestBaixaConsomeCaixas.POS

    def setUp(self):
        super().setUp()
        wms.criar_area(self.conn, "C9", "CONTAINER", "Container 9")
        wms.gerar_posicoes(self.conn, "C9", estantes=2, niveis=2)
        self.conn.commit()

    ativar = TestBaixaConsomeCaixas.ativar
    pedido = TestBaixaConsomeCaixas.pedido
    sit = TestBaixaConsomeCaixas.sit
    saldo_pos = TestBaixaConsomeCaixas.saldo_pos

    def eventos(self, serial):
        return self.conn.execute("SELECT para, observacao, movimento_uuid FROM wms_caixa_eventos "
                                 "WHERE serial = ? ORDER BY id", (serial,)).fetchall()

    # I2a -------------------------------------------------------------------
    def test_mover_caixa_reservada_recusa_e_nao_sobra_divergencia(self):
        # cenario do probe: 2 caixas de 6, reserva de 6, mover as duas depois da reserva
        s = self.ativar(2)
        self.pedido(1010, 6)
        cx.mover_caixa(self.conn, s[0], "C9-E2-N1", uuid="x" + "a" * 16)     # sobra 6 >= 6 reservado
        with self.assertRaises(wms.ErroWMS) as ctx:
            cx.mover_caixa(self.conn, s[1], "C9-E2-N1", uuid="x" + "b" * 16)
        self.assertEqual(str(ctx.exception),
                         "Caixa reservada para PS-1010: separe ou troque a reserva antes de mover.")
        self.assertEqual(cx.obter_caixa(self.conn, s[1])["posicao"], self.POS)
        self.assertFalse(self.conn.in_transaction)
        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-1010")
        self.assertEqual((r["baixas"], r["erros"]), (1, []))
        self.assertEqual(self.sit(s[1]), ("EXPEDIDA", 6))
        self.assertEqual(self.sit(s[0]), ("EM_ESTOQUE", 6))
        self.assertEqual(cx.divergencias(self.conn, 48), [])

    def test_mover_ignora_reserva_de_outra_chave_e_consumida(self):
        s = self.ativar(1)[0]
        pid = self.pedido(1011, 6)
        self.conn.execute("UPDATE wms_reservas SET lote = 'OUTRO' WHERE pedido_id = ?", (pid,))
        self.conn.commit()
        cx.mover_caixa(self.conn, s, "C9-E2-N1", uuid="x" + "c" * 16)
        self.assertEqual(cx.obter_caixa(self.conn, s)["posicao"], "C9-E2-N1")
        s2 = self.ativar(1)[0]
        pid2 = self.pedido(1012, 6)
        self.conn.execute("UPDATE wms_reservas SET estado = 'CONSUMIDA' WHERE pedido_id = ?", (pid2,))
        self.conn.commit()
        cx.mover_caixa(self.conn, s2, "C9-E2-N1", uuid="x" + "d" * 16)

    # I2b -------------------------------------------------------------------
    def test_acertar_caixa_marca_sem_movimento_e_grava_evento(self):
        a, b = self.ativar(2)
        movs = self.n("wms_movimentos")
        r = cx.acertar_caixa(self.conn, a, "EXPEDIDA", "saiu no PS-1 sem baixa", operador={"id": 1, "nome": "Hugo"})
        self.assertEqual(r["caixa"]["situacao"], "EXPEDIDA")
        self.assertEqual(self.n("wms_movimentos"), movs)
        self.assertEqual(self.saldo_pos(), 12)
        ev = self.eventos(a)[-1]
        self.assertEqual((ev["para"], ev["movimento_uuid"]), ("EXPEDIDA", None))
        self.assertIn("saiu no PS-1 sem baixa", ev["observacao"])
        cx.acertar_caixa(self.conn, b.lower(), "CANCELADA", "sumiu")
        self.assertEqual(self.sit(b)[0], "CANCELADA")
        self.assertFalse(self.conn.in_transaction)

    def test_acertar_caixa_recusa_situacao_motivo_e_caixa_fora_de_estoque(self):
        s = self.ativar(1)[0]
        impressa = cx.criar_caixas(self.conn, produto_id=1, lote="L1", validade="2026-12-15",
                                   qtd_un=None, quantidade=1)["seriais"][0]
        for serial, sit, motivo in ((s, "EM_ESTOQUE", "x"), (s, "ABERTA", "x"), (s, "EXPEDIDA", "  "),
                                    (impressa, "CANCELADA", "x"), ("C7777777", "EXPEDIDA", "x")):
            with self.subTest(serial=serial, sit=sit), self.assertRaises(wms.ErroWMS):
                cx.acertar_caixa(self.conn, serial, sit, motivo)
        cx.acertar_caixa(self.conn, s, "EXPEDIDA", "ok")
        with self.assertRaises(wms.ErroWMS):
            cx.acertar_caixa(self.conn, s, "CANCELADA", "de novo")

    def test_acertar_saldo_pra_menos_e_repetir_nao_faz_nada(self):
        self.ativar(1)
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=4, lote="L1",
                                validade="2026-12-15", destino=self.POS)
        self.assertEqual(self.saldo_pos(), 10)
        r = cx.acertar_saldo(self.conn, self.POS.lower(), 1, "l1", "2026-12-15", "sobra de 4 sem caixa")
        self.assertEqual((r["saldo"], r["caixas"], r["ajuste"]), (10, 6, -4))
        self.assertEqual(self.saldo_pos(), 6)
        self.assertEqual(cx.divergencias(self.conn, 48), [])
        m = self.conn.execute("SELECT uuid, tipo, posicao_origem, observacao FROM wms_movimentos "
                              "WHERE uuid LIKE 'acertar-%'").fetchall()
        self.assertEqual(len(m), 1)
        self.assertEqual((m[0]["tipo"], m[0]["posicao_origem"]), ("AJUSTE", self.POS))
        self.assertRegex(m[0]["uuid"], r"^acertar-C9-E1-N1-1-L1-2026-12-15-\d{8}$")
        self.assertIn("sobra de 4 sem caixa", m[0]["observacao"])
        r = cx.acertar_saldo(self.conn, self.POS, 1, "L1", "2026-12-15", "de novo")
        self.assertEqual(r["ajuste"], 0)
        self.assertEqual(self.n("wms_movimentos"), 3)

    def test_acertar_saldo_pra_mais_e_motivo_obrigatorio(self):
        self.conn.execute(
            "INSERT INTO wms_caixas (serial, produto_id, lote, validade, qtd_un, posicao, situacao, criado_em, "
            "atualizado_em) VALUES ('C9999997', 1, 'L3', '2026-10-01', 6, 'C9-E1-N2', 'EM_ESTOQUE', 'x', 'x')")
        self.conn.commit()
        with self.assertRaises(wms.ErroWMS):
            cx.acertar_saldo(self.conn, "C9-E1-N2", 1, "L3", "2026-10-01", " ")
        r = cx.acertar_saldo(self.conn, "C9-E1-N2", 1, "L3", "2026-10-01", "caixa sem saldo")
        self.assertEqual(r["ajuste"], 6)
        self.assertEqual(wms._saldo_atual(self.conn, "C9-E1-N2", 1, "L3", "2026-10-01"), 6)
        self.assertEqual(cx.divergencias(self.conn, 48), [])

    # I3 --------------------------------------------------------------------
    def test_zerar_recusa_com_reservas_ativas_do_embarcador(self):
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=10, lote="L2",
                                validade="2026-11-01", destino=self.POS)
        pid = self.pedido(1013, 4)
        self.assertEqual(cx.contar_reservas_ativas(self.conn, 48), 1)
        self.assertEqual(cx.contar_reservas_ativas(self.conn, 77), 0)
        with self.assertRaises(wms.ErroWMS) as ctx:
            cx.zerar_saldo(self.conn, 48, "limpeza")
        self.assertEqual(str(ctx.exception),
                         "Ha 1 reservas ativas do embarcador: cancele as reservas antes de zerar.")
        self.assertEqual(wms._saldo_atual(self.conn, self.POS, 1, "L2", "2026-11-01"), 10)
        wms_pedidos.cancelar_reservas(self.conn, pid, "teste")
        self.conn.commit()
        self.assertEqual(cx.contar_reservas_ativas(self.conn, 48), 0)
        self.assertEqual(cx.zerar_saldo(self.conn, 48, "limpeza"), 1)

    # M1 --------------------------------------------------------------------
    def test_falha_ao_garantir_tabelas_nao_derruba_a_baixa(self):
        s = self.ativar(1)[0]
        self.pedido(1014, 6)
        with mock.patch.object(cx, "garantir_tabelas", side_effect=sqlite3.OperationalError("database is locked")), \
                self.assertLogs("wms_pedidos", level="WARNING"):
            r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-1014")
        self.assertEqual((r["baixas"], r["erros"]), (1, []))
        self.assertEqual(self.conn.execute("SELECT estado_reserva FROM wms_pedidos WHERE id_stokki = 1014")
                         .fetchone()[0], "BAIXADO")
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.sit(s)[0], "EM_ESTOQUE")   # sem consumo; a checagem mostra


class TestMigracaoCodigoInterno(unittest.TestCase):
    """M2: dois processos abrindo o banco ao mesmo tempo -- o ALTER do segundo da duplicate column."""

    def test_corrida_na_migracao_segue(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "d.db"
            wms.conectar(db).close()            # coluna ja criada pelo "outro processo"
            real = sqlite3.connect

            class Atrasada(sqlite3.Connection):
                def execute(self, sql, *a):
                    cur = super().execute(sql, *a)
                    if sql.startswith("PRAGMA table_info(wms_produtos)"):
                        return [r for r in cur if r["name"] != "codigo_interno"]
                    return cur

            with mock.patch.object(wms.sqlite3, "connect",
                                   side_effect=lambda *a, **kw: real(*a, factory=Atrasada, **kw)):
                conn = wms.conectar(db)
            conn.close()

    def test_outro_erro_operacional_continua_subindo(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "d.db"
            real = sqlite3.connect

            class Quebrada(sqlite3.Connection):
                def execute(self, sql, *a):
                    if sql.startswith("ALTER TABLE wms_produtos ADD COLUMN codigo_interno"):
                        raise sqlite3.OperationalError("disk I/O error")
                    return super().execute(sql, *a)

            with mock.patch.object(wms.sqlite3, "connect",
                                   side_effect=lambda *a, **kw: real(*a, factory=Quebrada, **kw)), \
                    self.assertRaises(sqlite3.OperationalError):
                wms.conectar(db)


if __name__ == "__main__":
    unittest.main()
