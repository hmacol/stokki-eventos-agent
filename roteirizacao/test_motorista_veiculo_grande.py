# -*- coding: utf-8 -*-
"""
test_motorista_veiculo_grande.py

Motorista que nao e Fiorino (Hugo, 25/09) so pega rota classificada com
o porte EXATO do carro dele -- VAN_HR so rota VAN_HR, VUC so rota VUC,
etc. Nunca rota comum (ultima milha, Fiorino), nunca rota de outro
porte, nem menor. Fiorino continua so na rota comum.

Rodar (da raiz):
    py -3.11 -m unittest roteirizacao.test_motorista_veiculo_grande -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

from regras.preferencias_motoristas import MotoristaPreferencias  # noqa: E402
import alocacao_motoristas  # noqa: E402

DATA = date(2026, 9, 23)
ROTA_COMUM = [{"address": "Rua X, 100", "dimension_3": 10}]
ROTA_VAN_HR = [{"address": "Rua X, 100", "dimension_3": 200}]   # 101-400 cx = VAN_HR
ROTA_VUC = [{"address": "Rua X, 100", "dimension_3": 500}]      # 401-600 cx = VUC


def _motorista(agent_id, tipo):
    return MotoristaPreferencias(
        agent_id=agent_id, vehicle_id=None, nome=f"M{agent_id}", aceita_viagens=True,
        dias_disponiveis=[0, 1, 2, 3, 4, 5, 6], max_rotas_dia=1, ativo=True,
        zonas_preferidas=["ZONA NORTE"], tipo_veiculo=tipo,
    )


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
        self.van = _motorista(1, "VAN_HR")
        self.fiorino = _motorista(2, "FIORINO")
        self.vuc = _motorista(3, "VUC")
        self.truck = _motorista(4, "TRUCK")

    def elegiveis(self, sublote, motoristas=None):
        return {m.agent_id for m in alocacao_motoristas.listar_motoristas_elegiveis(
            sublote, DATA, motoristas or [self.van, self.fiorino, self.vuc, self.truck], {})}

    def test_rota_comum_so_fiorino(self):
        self.assertEqual(self.elegiveis(ROTA_COMUM), {2})

    def test_rota_van_hr_so_van_hr(self):
        self.assertEqual(self.elegiveis(ROTA_VAN_HR), {1})

    def test_rota_vuc_so_vuc(self):
        self.assertEqual(self.elegiveis(ROTA_VUC), {3})

    def test_escolha_automatica_respeita(self):
        # VAN_HR tem o menor agent_id e historico zerado: sem a trava, seria ele.
        escolhido = alocacao_motoristas.selecionar_motorista_equitativo(
            ROTA_COMUM, DATA, [self.van, self.fiorino], {})
        self.assertEqual(escolhido.agent_id, 2)

    def test_so_veiculo_grande_disponivel_e_rota_comum_fica_sem_motorista(self):
        self.assertIsNone(alocacao_motoristas.selecionar_motorista_equitativo(
            ROTA_COMUM, DATA, [self.van, self.vuc, self.truck], {}))

    def test_rota_vuc_sem_vuc_fica_sem_motorista_mesmo_com_truck(self):
        self.assertIsNone(alocacao_motoristas.selecionar_motorista_equitativo(
            ROTA_VUC, DATA, [self.fiorino, self.truck], {}))


if __name__ == "__main__":
    unittest.main()
