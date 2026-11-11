# -*- coding: utf-8 -*-
"""
test_expedicao_excluir.py -- "Excluir da Rota" da Torre/Expedicao usa a mesma regra do card da rota
enviada no Planejamento (Hugo, 10/11): status ao vivo, DELETE (nunca PUT com lista), pedido
comecado/entregue recusa, rota iniciada vira Retornando. Expedicao (permitir_rota_em_andamento=False)
mantem a trava de rota nao iniciada.

    py -3.11 -m unittest test_expedicao_excluir -v   (de dentro de painel_agentes/)
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import expedicao  # noqa: E402
import pedidos_retornando  # noqa: E402
import rascunhos_rota  # noqa: E402
from test_rota_enviada_edicao import _Base, _parada, ROTA_ID  # noqa: E402

MOTIVO = "Não coube"


def _sv(sid, status):
    return {"id": sid, "code": f"#PS-{sid}", "status": status}


class TorreExcluirTest(_Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(expedicao, "_carregar_config", return_value={})
        p.start()
        self.addCleanup(p.stop)

    def _excluir(self, sid, permitir=True):
        return expedicao.excluir_pedido_da_rota(date.today(), ROTA_ID, sid, MOTIVO, "obs", "hugo",
                                                permitir_rota_em_andamento=permitir)

    def _retornando(self):
        conn = pedidos_retornando.conectar(self.db)
        try:
            return [tuple(r) for r in conn.execute("SELECT service_id FROM pedidos_retornando").fetchall()]
        finally:
            conn.close()

    def test_torre_recusa_pedido_a_caminho(self):
        self._rota_viva("started", [_sv(1, "on_route"), _sv(2, "assigned")])
        r = self._excluir(1)
        self.assertFalse(r["ok"])
        self.assertEqual(self.vuupt.escritas(), [])

    def test_torre_recusa_entregue(self):
        self._rota_viva("started", [_sv(1, "done"), _sv(2, "assigned")])
        r = self._excluir(1)
        self.assertFalse(r["ok"])
        self.assertEqual(self.vuupt.escritas(), [])

    def test_torre_tira_pendente_com_delete_e_atualiza_rascunho(self):
        rid = self._inserir_rascunho([_parada(1, "#PS-1"), _parada(2, "#PS-2")])
        self._rota_viva("assigned", [_sv(1, "assigned"), _sv(2, "assigned")])
        r = self._excluir(1)
        self.assertTrue(r["ok"])
        self.assertFalse(r["rota_cancelada"])
        self.assertEqual(self.vuupt.escritas(), [("DELETE", 1)])
        self.assertEqual([p["service_id"] for p in self._rascunho(rid)["paradas"]], [2])
        self.assertEqual([x["service_id"] for x in expedicao._exclusoes_por_rota(date.today(), ROTA_ID)], [1])
        self.marcar_aberto.assert_called_once_with([1])
        self.assertEqual(self._retornando(), [])
        self.agendar.assert_called_once()

    def test_torre_em_rota_iniciada_marca_retornando(self):
        rid = self._inserir_rascunho([_parada(1, "#PS-1"), _parada(2, "#PS-2")])
        self._rota_viva("started", [_sv(1, "assigned"), _sv(2, "on_route")])
        r = self._excluir(1)
        self.assertTrue(r["ok"])
        self.assertEqual(self.vuupt.escritas(), [("DELETE", 1)])
        self.assertEqual(self._retornando(), [(1,)])
        self.assertEqual([p["service_id"] for p in self._rascunho(rid)["paradas"]], [2])
        self.marcar_aberto.assert_not_called()

    def test_torre_sem_rascunho_tira_nome_do_motorista_do_nucleo(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE nucleo_rotas (id INTEGER PRIMARY KEY, vuupt_route_id INTEGER, motorista_nome TEXT)")
        conn.execute("INSERT INTO nucleo_rotas (vuupt_route_id, motorista_nome) VALUES (?, 'Joana')", (ROTA_ID,))
        conn.commit()
        conn.close()
        self._rota_viva("started", [_sv(1, "assigned"), _sv(2, "on_route")])
        self.assertTrue(self._excluir(1)["ok"])
        conn = pedidos_retornando.conectar(self.db)
        try:
            nome = conn.execute("SELECT motorista_nome FROM pedidos_retornando WHERE service_id = 1").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(nome, "Joana")

    def test_expedicao_mantem_trava_de_rota_nao_iniciada(self):
        self._rota_viva("started", [_sv(1, "assigned"), _sv(2, "assigned")])
        r = self._excluir(1, permitir=False)
        self.assertFalse(r["ok"])
        self.assertIn("não pode ser alterada por aqui", r["erro"])
        self.assertEqual(self.vuupt.escritas(), [])

    def test_expedicao_rota_nao_iniciada_usa_delete(self):
        self._rota_viva("assigned", [_sv(1, "assigned"), _sv(2, "assigned")])
        r = self._excluir(1, permitir=False)
        self.assertTrue(r["ok"])
        self.assertEqual(self.vuupt.escritas(), [("DELETE", 1)])

    def test_auditoria_leva_nome_da_rota_viva_sem_rascunho(self):
        self.vuupt.rota["name"] = "Planejamento - 12/11 - #3"
        self._rota_viva("assigned", [_sv(1, "assigned"), _sv(2, "assigned")])
        self.assertTrue(self._excluir(1)["ok"])
        ex = expedicao._exclusoes_por_rota(date.today(), ROTA_ID)
        self.assertEqual(len(ex), 1)
        conn = expedicao._conectar_db()
        try:
            nome = conn.execute("SELECT rota_nome FROM expedicao_exclusoes").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(nome, "Planejamento - 12/11 - #3")

    def test_torre_ultimo_pedido_cancela_rota_e_reverte_rascunho(self):
        rid = self._inserir_rascunho([_parada(1, "#PS-1")])
        self._rota_viva("assigned", [_sv(1, "assigned")])
        r = self._excluir(1)
        self.assertTrue(r["ok"])
        self.assertTrue(r["rota_cancelada"])
        self.assertEqual(self.vuupt.escritas(), [("CANCELAR", "unassign")])
        rasc = self._rascunho(rid)
        self.assertEqual(rasc["status"], "RASCUNHO")
        self.assertIsNone(rasc["vuupt_route_id"])
        self.assertEqual(rasc["paradas"], [])

    def test_retornando_falhando_ainda_devolve_ok(self):
        rid = self._inserir_rascunho([_parada(1, "#PS-1"), _parada(2, "#PS-2")])
        self._rota_viva("started", [_sv(1, "assigned"), _sv(2, "on_route")])
        with mock.patch.object(pedidos_retornando, "marcar", side_effect=RuntimeError("banco travado")) as m:
            with self.assertLogs("rascunhos_rota", level="ERROR"):
                r = self._excluir(1)
        self.assertEqual(m.call_count, 2)
        self.assertTrue(r["ok"])
        self.assertFalse(r["retornando"])
        self.assertIn("#PS-1", r["aviso"])
        self.assertIn("Vinicius", r["aviso"])
        self.assertIn("NÃO ficou como Retornando", r["aviso"])
        self.assertEqual([p["service_id"] for p in self._rascunho(rid)["paradas"]], [2])
        self.marcar_aberto.assert_not_called()

    def test_retornando_falha_uma_vez_e_repete_sem_aviso(self):
        self._inserir_rascunho([_parada(1, "#PS-1"), _parada(2, "#PS-2")])
        self._rota_viva("started", [_sv(1, "assigned"), _sv(2, "on_route")])
        original = pedidos_retornando.marcar
        chamadas = []

        def falha_uma_vez(*a, **k):
            chamadas.append(1)
            if len(chamadas) == 1:
                raise RuntimeError("banco travado")
            return original(*a, **k)

        with mock.patch.object(pedidos_retornando, "marcar", side_effect=falha_uma_vez):
            r = self._excluir(1)
        self.assertTrue(r["ok"])
        self.assertTrue(r["retornando"])
        self.assertNotIn("aviso", r)
        self.assertEqual(self._retornando(), [(1,)])

    def test_planejamento_tambem_devolve_aviso(self):
        rid = self._inserir_rascunho([_parada(1, "#PS-1"), _parada(2, "#PS-2")])
        self._rota_viva("started", [_sv(1, "assigned"), _sv(2, "on_route")])
        with mock.patch.object(pedidos_retornando, "marcar", side_effect=RuntimeError("banco travado")):
            with self.assertLogs("rascunhos_rota", level="ERROR"):
                r = rascunhos_rota.remover_de_rota_enviada(rid, 1, MOTIVO, "", "tk", "hugo")
        self.assertTrue(r["ok"])
        self.assertIn("#PS-1", r["aviso"])

    def test_fora_da_rota_devolve_erro_mas_tira_parada_local(self):
        rid = self._inserir_rascunho([_parada(1, "#PS-1"), _parada(2, "#PS-2")])
        self._rota_viva("assigned", [_sv(2, "assigned")])
        r = self._excluir(1)
        self.assertFalse(r["ok"])
        self.assertEqual([p["service_id"] for p in self._rascunho(rid)["paradas"]], [2])
        self.assertEqual(self.vuupt.escritas(), [])


if __name__ == "__main__":
    unittest.main()
