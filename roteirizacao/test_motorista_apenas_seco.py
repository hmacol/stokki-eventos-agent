# -*- coding: utf-8 -*-
"""
test_motorista_apenas_seco.py

Motorista com APENAS_CARGA_SECA (Hugo, 23/09 -- caso do Fagner) so pode
pegar rota em que TODOS os pedidos sao de embarcador CADASTRADO como
Seco. Embarcador sem cadastro (que a roteirizacao trata como Seco por
padrao) nao conta.

Rodar (da raiz):
    py -3.11 -m unittest roteirizacao.test_motorista_apenas_seco -v
"""
import sys
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

from regras.preferencias_motoristas import MotoristaPreferencias, _construir_motorista  # noqa: E402
from regras.tipo_carga_embarcador import carga_seca_confirmada, marcar_tipo_carga  # noqa: E402
import alocacao_motoristas  # noqa: E402

DATA = date(2026, 9, 23)
MAPA = {10: "Seco", 20: "Refrigerado", 30: "Congelado"}  # sender 99 = sem cadastro


def _pedido(sender_id):
    s = {"address": "Rua X, 100", "dimension_3": 10, "sender_id": sender_id}
    marcar_tipo_carga(s, MAPA)
    return s


def _motorista(agent_id, apenas_seco=False):
    return MotoristaPreferencias(
        agent_id=agent_id, vehicle_id=None, nome=f"M{agent_id}", aceita_viagens=True,
        dias_disponiveis=[0, 1, 2, 3, 4, 5, 6], max_rotas_dia=1, ativo=True,
        zonas_preferidas=["ZONA NORTE"], tipo_veiculo="FIORINO", apenas_carga_seca=apenas_seco,
    )


class TestSecoConfirmado(unittest.TestCase):
    def test_so_cadastrado_como_seco_conta(self):
        self.assertTrue(carga_seca_confirmada(_pedido(10)))
        self.assertFalse(carga_seca_confirmada(_pedido(20)))
        self.assertFalse(carga_seca_confirmada(_pedido(30)))

    def test_sem_cadastro_nao_conta_mesmo_caindo_no_padrao_seco(self):
        s = _pedido(99)
        self.assertEqual(s["_tipo_carga"], "Seco")   # roteirizacao continua tratando como seco
        self.assertFalse(carga_seca_confirmada(s))  # mas nao libera o motorista so-seco

    def test_servico_nunca_marcado_nao_conta(self):
        self.assertFalse(carga_seca_confirmada({"_tipo_carga": "Seco"}))
        self.assertFalse(carga_seca_confirmada({}))


class TestCadastro(unittest.TestCase):
    def _m(self, valor):
        return _construir_motorista({"AGENT_ID_VUUPT": 1, "NOME_MOTORISTA": "X", "ATIVO": "SIM",
                                     "APENAS_CARGA_SECA": valor})

    def test_coluna(self):
        self.assertTrue(self._m("SIM").apenas_carga_seca)
        self.assertFalse(self._m("NAO").apenas_carga_seca)
        self.assertFalse(self._m(None).apenas_carga_seca)
        self.assertFalse(self._m(float("nan")).apenas_carga_seca)


class TestElegibilidade(unittest.TestCase):
    def setUp(self):
        for nome, valor in (
            ("classificar_rota_viagem", lambda *a, **k: False),
            ("classificar_rota_zona", lambda *a, **k: None),
            ("sublote_em_area_rodizio", lambda *a, **k: False),
        ):
            patcher = mock.patch.object(alocacao_motoristas, nome, valor)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fagner = _motorista(1, apenas_seco=True)
        self.outro = _motorista(2)

    def elegiveis(self, sublote, motoristas=None):
        return {m.agent_id for m in alocacao_motoristas.listar_motoristas_elegiveis(
            sublote, DATA, motoristas or [self.fagner, self.outro], {})}

    def test_rota_toda_seca_confirmada_aceita(self):
        self.assertEqual(self.elegiveis([_pedido(10), _pedido(10)]), {1, 2})

    def test_um_pedido_frio_tira_o_motorista_so_seco(self):
        self.assertEqual(self.elegiveis([_pedido(10), _pedido(20)]), {2})

    def test_um_pedido_sem_cadastro_tira_o_motorista_so_seco(self):
        self.assertEqual(self.elegiveis([_pedido(10), _pedido(99)]), {2})

    def test_rota_sem_marcacao_tira_o_motorista_so_seco(self):
        self.assertEqual(self.elegiveis([{"address": "Rua X, 100", "dimension_3": 10}]), {2})

    def test_escolha_automatica_respeita(self):
        # Fagner tem o menor agent_id e historico zerado: sem a trava, seria ele.
        escolhido = alocacao_motoristas.selecionar_motorista_equitativo(
            [_pedido(20)], DATA, [self.fagner, self.outro], {})
        self.assertEqual(escolhido.agent_id, 2)

    def test_so_ele_disponivel_e_rota_fria_fica_sem_motorista(self):
        self.assertIsNone(alocacao_motoristas.selecionar_motorista_equitativo(
            [_pedido(20)], DATA, [self.fagner], {}))

    def test_motorista_comum_nao_muda(self):
        self.assertEqual(self.elegiveis([_pedido(30)], [self.outro, replace(self.outro, agent_id=3)]), {2, 3})


if __name__ == "__main__":
    unittest.main()
