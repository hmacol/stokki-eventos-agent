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

    def test_fora_da_rota_devolve_erro_mas_tira_parada_local(self):
        rid = self._inserir_rascunho([_parada(1, "#PS-1"), _parada(2, "#PS-2")])
        self._rota_viva("assigned", [_sv(2, "assigned")])
        r = self._excluir(1)
        self.assertFalse(r["ok"])
        self.assertEqual([p["service_id"] for p in self._rascunho(rid)["paradas"]], [2])
        self.assertEqual(self.vuupt.escritas(), [])


if __name__ == "__main__":
    unittest.main()
