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

    def test_erro_vuupt_vira_falha(self):
        self.vuupt.cancelar_servico_oficial.side_effect = cs.VuuptAPIError("409 ja done")
        with mock.patch.object(cs, "rascunho_do_servico", return_value=(None, None)):
            r = cs.cancelar_servico_completo("tok", 111, vuupt=self.vuupt)
        self.assertFalse(r["ok"])
        self.assertIn("409", r["erro"])
        cs.ressincronizar_ids.assert_not_called()


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


if __name__ == "__main__":
    unittest.main()
