# -*- coding: utf-8 -*-
"""
Testes das caixas com serial e do codigo interno (dados).
Rodar (da raiz): py -3.11 -m unittest painel_agentes.test_wms_caixas -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

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
        cx.ativar_caixa(self.conn, s, "C9-E1-N1")  # sem a barra: simula contabilizar perdido
        cx.ativar_caixa(self.conn, s, "C9-E1-N1", recebimento_id=rid, item_id=itens[0]["id"])
        self.assertEqual(self.enderecada(itens[0]["id"]), 6)
        self.assertEqual(self.saldo(), 6)

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
                                observacao="Cancelamento")
        r = cx.cancelar_caixa(self.conn, s, "queda")
        self.assertTrue(r["duplicado"])
        self.assertEqual(r["caixa"]["situacao"], "CANCELADA")
        self.assertEqual(self.saldo(), 0)


if __name__ == "__main__":
    unittest.main()
