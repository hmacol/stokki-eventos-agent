# -*- coding: utf-8 -*-
"""
Em espera a pedido do cliente (27/10/2026) no Planejamento: o pool MARCA o
pedido pausado (nao esconde) e "roteirizar selecionados" pula quem esta pausado.

    py -3.11 -m unittest test_pausados_planejamento   (de dentro de painel_agentes/)
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import planejamento_rotas as pr  # noqa: E402


def _servico(sid, code=None):
    return {"id": sid, "code": code or f"#PS-{sid}"}


class TestMarcarPool(unittest.TestCase):

    def test_pool_marca_pausado(self):
        pool = [{"service_id": 1, "codigo": "#PS-1"}, {"service_id": 2, "codigo": "#PS-2"},
                {"service_id": 3, "codigo": "#PS-3-R1"}]
        brutos = [_servico(1), _servico(2), _servico(3, "#PS-3-R1")]
        with mock.patch("pedidos_pausados_cliente.codigos_pausados", return_value={"PS-1", "PS-3"}):
            pr.marcar_pausados_cliente(pool, brutos)
        self.assertEqual([p["pausado_cliente"] for p in pool], [True, False, True])
        self.assertEqual(len(pool), 3)  # marca, nao esconde

    def test_sem_tabela_nada_e_marcado(self):
        pool = [{"service_id": 1, "codigo": "#PS-1"}]
        with mock.patch("pedidos_pausados_cliente.codigos_pausados", return_value=set()):
            pr.marcar_pausados_cliente(pool, [_servico(1)])
        self.assertFalse(pool[0]["pausado_cliente"])


class TestRoteirizarSelecionados(unittest.TestCase):

    def _rodar(self, service_ids, pausados):
        brutos = [_servico(1), _servico(2), _servico(3)]
        rascunhos = [{"sublote": [{"id": 2}], "nome": "x"}]
        self.roteirizar = mock.Mock(return_value=rascunhos)
        import criar_rotas_diarias
        patches = [
            mock.patch.object(pr, "_carregar_config", return_value={}),
            mock.patch.object(pr.rascunhos_rota, "listar_rascunhos_do_dia", return_value=[]),
            mock.patch.object(pr, "VuuptClient"),
            mock.patch.object(pr, "listar_pool_not_assigned", return_value=brutos),
            mock.patch.object(pr.rascunhos_rota, "referencia_para_rascunho_manual", return_value={"lote_id": 1}),
            mock.patch.object(pr.rascunhos_rota, "criar_lote_rascunhos"),
            mock.patch.object(criar_rotas_diarias, "roteirizar_para_rascunhos", self.roteirizar),
            mock.patch("pedidos_pausados_cliente.codigos_pausados", return_value=set(pausados)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return pr.roteirizar_selecionados(date(2026, 10, 29), service_ids)

    def test_roteirizar_selecionados_pula_pausado(self):
        r = self._rodar([1, 2], {"PS-1"})
        self.assertEqual([s["id"] for s in self.roteirizar.call_args[0][0]], [2])
        self.assertEqual(r["pedidos_pausados"], 1)
        self.assertEqual(r["pedidos_indisponiveis"], 0)

    def test_sem_pausado_conta_zero(self):
        r = self._rodar([1, 2], set())
        self.assertEqual(r["pedidos_pausados"], 0)

    def test_tudo_pausado_diz_o_motivo(self):
        with self.assertRaises(ValueError) as ctx:
            self._rodar([1], {"PS-1"})
        self.assertIn("em espera a pedido do cliente", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
