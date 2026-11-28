# -*- coding: utf-8 -*-
"""py -3.11 -m unittest roteirizacao.test_cancelar_servico"""
import unittest
from unittest import mock

from roteirizacao import cancelar_servico as cs


class CancelarServicoCompleto(unittest.TestCase):
    def setUp(self):
        self.vuupt = mock.Mock()
        self.vuupt.buscar_servico_por_id.return_value = {"id": 111, "status": "not_assigned"}
        mock.patch.object(cs, "ressincronizar_ids").start()
        mock.patch.object(cs.rascunhos_rota, "preparar_cancelamento_de_parada", return_value={"ok": True}).start()
        self.remover = mock.patch.object(cs.rascunhos_rota, "remover_parada").start()
        self.preparar = cs.rascunhos_rota.preparar_cancelamento_de_parada
        self.addCleanup(mock.patch.stopall)

    def test_no_pool_cancela_direto(self):
        with mock.patch.object(cs, "rascunho_do_servico", return_value=(None, None)):
            r = cs.cancelar_servico_completo("tok", 111, vuupt=self.vuupt)
        self.assertEqual(r, {"ok": True, "ja_estava": False, "erro": ""})
        self.vuupt.cancelar_servico_oficial.assert_called_once_with(111)
        self.vuupt.cancelar_servico.assert_not_called()
        cs.ressincronizar_ids.assert_called_once_with(self.vuupt, [111])

    def test_em_rascunho_tira_a_parada(self):
        with mock.patch.object(cs, "rascunho_do_servico", return_value=(7, "RASCUNHO")):
            r = cs.cancelar_servico_completo("tok", 111, vuupt=self.vuupt)
        self.assertTrue(r["ok"])
        self.remover.assert_called_once_with(7, 111)
        self.preparar.assert_not_called()

    def test_em_rota_enviada_prepara_antes(self):
        with mock.patch.object(cs, "rascunho_do_servico", return_value=(7, "ENVIADO")):
            r = cs.cancelar_servico_completo("tok", 111, vuupt=self.vuupt)
        self.assertTrue(r["ok"])
        self.preparar.assert_called_once_with(7, 111, "tok")
        self.remover.assert_not_called()

    def test_cancelamento_falho_apos_sair_de_rota_iniciada_marca_retornando(self):
        self.preparar.return_value = {"ok": True, "rota_iniciada": True, "vuupt_route_id": 9, "codigo": "#PS-1"}
        self.vuupt.cancelar_servico_oficial.side_effect = cs.VuuptAPIError("500")
        with mock.patch.object(cs, "rascunho_do_servico", return_value=(7, "ENVIADO")), \
                mock.patch.object(cs.rascunhos_rota, "_marcar_retornando_com_retry", return_value=True) as marcar:
            r = cs.cancelar_servico_completo("tok", 111, vuupt=self.vuupt)
        self.assertFalse(r["ok"])
        self.assertIn("Retornando", r["erro"])
        self.assertEqual(marcar.call_args.kwargs["vuupt_route_id"], 9)

    def test_preparo_que_falha_nao_cancela(self):
        self.preparar.return_value = {"ok": False, "erro": "rota ja iniciou"}
        with mock.patch.object(cs, "rascunho_do_servico", return_value=(7, "ENVIADO")):
            r = cs.cancelar_servico_completo("tok", 111, vuupt=self.vuupt)
        self.assertEqual((r["ok"], r["erro"]), (False, "rota ja iniciou"))
        self.vuupt.cancelar_servico_oficial.assert_not_called()

    def test_ja_cancelado_conta_como_ok(self):
        self.vuupt.buscar_servico_por_id.return_value = {"id": 111, "status": "canceled"}
        with mock.patch.object(cs, "rascunho_do_servico", return_value=(None, None)):
            r = cs.cancelar_servico_completo("tok", 111, vuupt=self.vuupt)
        self.assertEqual((r["ok"], r["ja_estava"]), (True, True))
        self.vuupt.cancelar_servico_oficial.assert_not_called()

    def test_servico_em_andamento_ou_concluido_na_vuupt_nao_cancela(self):
        for status in ("on_route", "arrived", "done"):
            self.vuupt.buscar_servico_por_id.return_value = {"id": 111, "status": status}
            with mock.patch.object(cs, "rascunho_do_servico", return_value=(None, None)):
                r = cs.cancelar_servico_completo("tok", 111, vuupt=self.vuupt)
            self.assertFalse(r["ok"], status)
            self.assertIn(status, r["erro"])
        self.vuupt.cancelar_servico_oficial.assert_not_called()

    def test_erro_vuupt_vira_falha(self):
        self.vuupt.cancelar_servico_oficial.side_effect = cs.VuuptAPIError("409 ja done")
        with mock.patch.object(cs, "rascunho_do_servico", return_value=(None, None)):
            r = cs.cancelar_servico_completo("tok", 111, vuupt=self.vuupt)
        self.assertFalse(r["ok"])
        self.assertIn("409", r["erro"])
        cs.ressincronizar_ids.assert_not_called()


class CancelarSemServico(unittest.TestCase):
    """Pool direto da Stokki (Task 6): id sintetico negativo cancela LOCAL, nunca chama a Vuupt."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from nucleo import banco, pedidos
        from nucleo.test_materializar import _payload
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        mock.patch.object(banco, "DB_PATH", Path(tmp.name) / "t.db").start()
        self.addCleanup(mock.patch.stopall)
        self.vuupt_cls = mock.patch.object(cs, "VuuptClient").start()
        mock.patch.object(cs, "ressincronizar_ids").start()
        self.tirar = mock.patch("roteirizacao.conferir_pool.tirar_dos_rascunhos", return_value=[7]).start()
        conn = banco.conectar()
        try:
            pedidos.registrar_importacao(_payload("PS-1"), None, "nucleo_sem_servico", conn=conn)
            self.sid = pedidos.id_sintetico(pedidos.buscar_pedido("PS-1", conn=conn))
        finally:
            conn.close()
        self.banco, self.pedidos = banco, pedidos

    def test_id_negativo_cancela_no_nucleo_sem_vuupt(self):
        vuupt = mock.Mock()
        r = cs.cancelar_servico_completo("tok", self.sid, vuupt=vuupt)
        self.assertEqual(r, {"ok": True, "ja_estava": False, "erro": ""})
        self.assertEqual(self.pedidos.buscar_pedido("PS-1")["status"], self.banco.PEDIDO_CANCELADO)
        self.tirar.assert_called_once_with(self.sid)
        self.assertEqual(vuupt.mock_calls, [])
        self.vuupt_cls.assert_not_called()
        cs.ressincronizar_ids.assert_not_called()

    def test_id_negativo_desconhecido_recusa_com_motivo(self):
        r = cs.cancelar_servico_completo("tok", -999)
        self.assertFalse(r["ok"])
        self.assertIn("nao encontrado no nucleo", r["erro"])
        self.tirar.assert_not_called()
        self.vuupt_cls.assert_not_called()

    def test_falha_ao_tirar_do_rascunho_diz_o_que_falta(self):
        self.tirar.side_effect = RuntimeError("database is locked")
        r = cs.cancelar_servico_completo("tok", self.sid)
        self.assertFalse(r["ok"])
        self.assertIn("remova a parada a mao", r["erro"])
        self.assertEqual(self.pedidos.buscar_pedido("PS-1")["status"], self.banco.PEDIDO_CANCELADO)


class RascunhoDoServico(unittest.TestCase):
    def test_acha_o_rascunho_vivo_mais_recente(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE rascunhos_rota (id INTEGER PRIMARY KEY, status TEXT);
            CREATE TABLE rascunhos_parada (id INTEGER PRIMARY KEY, rascunho_id INTEGER, service_id INTEGER);
            INSERT INTO rascunhos_rota VALUES (1, 'DESCARTADO'), (2, 'RASCUNHO');
            INSERT INTO rascunhos_parada VALUES (1, 1, 111), (2, 2, 111);
        """)
        with mock.patch.object(cs, "_conectar_rascunhos", side_effect=lambda: conn):
            self.assertEqual(cs.rascunho_do_servico(111), (2, "RASCUNHO"))
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("CREATE TABLE rascunhos_rota (id INTEGER PRIMARY KEY, status TEXT);"
                           "CREATE TABLE rascunhos_parada (id INTEGER PRIMARY KEY, rascunho_id INTEGER, service_id INTEGER);")
        with mock.patch.object(cs, "_conectar_rascunhos", side_effect=lambda: conn):
            self.assertEqual(cs.rascunho_do_servico(999), (None, None))


class OrfaSemServicoTest(unittest.TestCase):
    """Onda 4 (M1): cancelar local pelo id sintetico recusa a linha orfa com o motivo (operacao)."""

    def test_orfa_recusa_com_motivo(self):
        import tempfile
        from pathlib import Path as _P
        from nucleo import banco, pedidos
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(banco, "DB_PATH", _P(tmp.name) / "t.db"):
            conn = banco.conectar()
            try:
                pedidos.registrar_importacao({"code": "#PS-5", "title": "x"}, None, "pulado_sem_alteracao", conn=conn)
                sid = pedidos.id_sintetico(pedidos.buscar_pedido("PS-5", conn=conn))
            finally:
                conn.close()
            with mock.patch("roteirizacao.conferir_pool.tirar_dos_rascunhos", side_effect=AssertionError("rascunho")):
                r = cs.cancelar_sem_servico_local(sid, "teste", banco.ORIGEM_PAINEL)
            self.assertFalse(r["ok"])
            self.assertIn("operacao", r["erro"])
            self.assertEqual(pedidos.buscar_pedido("PS-5")["status"], banco.PEDIDO_ABERTO)


if __name__ == "__main__":
    unittest.main()
