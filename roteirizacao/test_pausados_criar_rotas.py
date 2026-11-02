# -*- coding: utf-8 -*-
"""
Em espera a pedido do cliente (27/10/2026) no criador diario: o pedido pausado
sai do pool ANTES do dia fixo (senao o dia fixo grava data e avisa o remetente).
main() com Vuupt/banco/e-mail mockados; pool sem pedido elegivel termina cedo.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_pausados_criar_rotas -v
"""
import sys
import tempfile
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


class _RodarCriador:
    """main() com tudo que sai do processo mockado."""

    def _rodar(self, pool, pausados=frozenset(), gerar_rascunho=False, extras=()):
        self.aplicar_dia_fixo = mock.Mock(return_value=[])
        # marcar_datas_automaticas abre o registro de origens: banco temporario,
        # nunca o dados/dados.db (revisao final, 02/11)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "t.db"
        modulos_falsos = {
            "dedicados": mock.Mock(separar_dedicados=lambda s: (s, [])),
            "fora_dia_fixo": mock.Mock(),
        }
        patches = [
            mock.patch.dict(sys.modules, modulos_falsos),
            mock.patch("registro_dia_fixo.DB_PATH", self.db),
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
            *extras,
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        crd.main(modo_teste=True, gerar_rascunho=gerar_rascunho)


class TestPausadoNoCriadorDiario(_RodarCriador, unittest.TestCase):

    def test_pausado_fica_fora_do_pool_antes_do_dia_fixo(self):
        self._rodar([_servico(1), _servico(2)], pausados={"PS-1"})
        self.assertEqual([s["id"] for s in self.aplicar_dia_fixo.call_args[0][0]], [2])

    def test_reentrega_do_pausado_fica_fora(self):
        self._rodar([_servico(1, "#PS-1-R1"), _servico(2)], pausados={"PS-1"})
        self.assertEqual([s["id"] for s in self.aplicar_dia_fixo.call_args[0][0]], [2])

    def test_pool_segue_sem_tabela(self):
        self._rodar([_servico(1), _servico(2)])
        self.assertEqual([s["id"] for s in self.aplicar_dia_fixo.call_args[0][0]], [1, 2])
        self.assertTrue(self.db.exists())  # o registro de origens abriu o banco temporario


class TestDataAutomaticaNoRascunho(_RodarCriador, unittest.TestCase):
    """Revisao final 02/11: na rodada do rascunho (16h), pedido em rota da
    Vuupt nao anda, e se a lista de rotas falhar ninguem anda; a data
    automatica e marcada antes de procurar agendamento pendente."""

    def _rodar_rascunho(self, listar_rotas):
        self.andar = mock.Mock(return_value=[])
        extras = [mock.patch.object(crd, "listar_rotas", listar_rotas),
                  mock.patch("datas_automaticas.andar_datas_automaticas", self.andar),
                  mock.patch("rascunhos_rota.listar_rascunhos_do_dia", return_value=[])]
        # data futura: ninguem e elegivel pra 28/10 e o main termina cedo
        pool = [dict(_servico(1), scheduled_start="2026-11-10 03:00:00"),
                dict(_servico(2), scheduled_start="2026-11-10 03:00:00")]
        self._rodar(pool, gerar_rascunho=True, extras=extras)

    def test_pedido_em_rota_da_vuupt_fica_fora_do_andar(self):
        rotas = mock.Mock(return_value=[{"id": 9, "status": "not_started", "services": {"data": [{"id": 1}]}}])
        self._rodar_rascunho(rotas)
        self.andar.assert_called_once()
        self.assertIn(1, self.andar.call_args[0][3])
        self.assertNotIn(2, self.andar.call_args[0][3])
        self.assertEqual(rotas.call_args.kwargs["include"], ["services"])

    def test_falha_ao_listar_rotas_ninguem_anda(self):
        self._rodar_rascunho(mock.Mock(side_effect=RuntimeError("Vuupt 500")))
        self.andar.assert_not_called()
        self.aplicar_dia_fixo.assert_called_once()  # o resto da rodada segue

    def test_marca_data_automatica_antes_do_agendamento_pendente(self):
        def marcar(servicos, db_path=None):
            for s in servicos:
                s["_data_automatica"] = True
            return len(servicos)
        pendentes = mock.Mock(return_value=[])
        self._rodar([_servico(1), _servico(2)], extras=[
            mock.patch("datas_automaticas.marcar_datas_automaticas", side_effect=marcar),
            mock.patch.object(crd, "identificar_pendentes", pendentes)])
        vistos = pendentes.call_args[0][0]
        self.assertEqual([s["id"] for s in vistos], [1, 2])
        self.assertTrue(all(s.get("_data_automatica") for s in vistos))


if __name__ == "__main__":
    unittest.main()
