# -*- coding: utf-8 -*-
"""
Em espera a pedido do cliente (27/10/2026) no criador diario: o pedido pausado
sai do pool ANTES do dia fixo (senao o dia fixo grava data e avisa o remetente).
main() com Vuupt/banco/e-mail mockados; pool sem pedido elegivel termina cedo.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_pausados_criar_rotas -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import criar_rotas_diarias as crd


def _servico(sid, code=None):
    return {"id": sid, "code": code or f"#PS-{sid}", "address": f"Rua {sid}, Sao Paulo - SP",
            "latitude": -23.5, "longitude": -46.6, "dimension_3": 1, "sender_id": 1}


class TestPausadoNoCriadorDiario(unittest.TestCase):

    def _rodar(self, pool, pausados=frozenset()):
        self.aplicar_dia_fixo = mock.Mock(return_value=[])
        modulos_falsos = {
            "dedicados": mock.Mock(separar_dedicados=lambda s: (s, [])),
            "fora_dia_fixo": mock.Mock(),
        }
        patches = [
            mock.patch.dict(sys.modules, modulos_falsos),
            mock.patch.object(crd, "_carregar_config", return_value={}),
            mock.patch.object(crd.CatalogoMotoristas, "carregar", return_value=mock.Mock(motoristas=[])),
            mock.patch.object(crd, "VuuptClient"),
            mock.patch.object(crd, "_data_alvo_rotas", return_value=date(2026, 10, 28)),
            mock.patch.object(crd, "carregar_ajustes_dia", return_value={}),
            mock.patch.object(crd, "listar_pool_not_assigned", return_value=pool),
            mock.patch.object(crd, "aplicar_regioes_dia_fixo", self.aplicar_dia_fixo),
            mock.patch.object(crd.pedidos_segurados, "separar_segurados", lambda s, d: (s, [])),
            mock.patch.object(crd, "identificar_area_nao_atendida", return_value=[]),
            mock.patch.object(crd, "carregar_clientes_agendamento", return_value=set()),
            mock.patch.object(crd, "identificar_pendentes", return_value=[]),
            mock.patch.object(crd, "notificar_execucao"),
            mock.patch("pedidos_pausados_cliente.codigos_pausados", return_value=set(pausados)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        crd.main(modo_teste=True)

    def test_pausado_fica_fora_do_pool_antes_do_dia_fixo(self):
        self._rodar([_servico(1), _servico(2)], pausados={"PS-1"})
        self.assertEqual([s["id"] for s in self.aplicar_dia_fixo.call_args[0][0]], [2])

    def test_reentrega_do_pausado_fica_fora(self):
        self._rodar([_servico(1, "#PS-1-R1"), _servico(2)], pausados={"PS-1"})
        self.assertEqual([s["id"] for s in self.aplicar_dia_fixo.call_args[0][0]], [2])

    def test_pool_segue_sem_tabela(self):
        self._rodar([_servico(1), _servico(2)])
        self.assertEqual([s["id"] for s in self.aplicar_dia_fixo.call_args[0][0]], [1, 2])


if __name__ == "__main__":
    unittest.main()
