# -*- coding: utf-8 -*-
"""
test_alocacao_justa.py

Ordem nova da alocacao (Hugo, 22/09). Antes era (carga do dia, agent_id)
-- e como 28 dos 30 motoristas tem MAX_ROTAS_DIA=1, a carga do dia era
quase sempre 0 pra todos e a escolha virava "menor agent_id". Quem tinha
id baixo rodava quase todo dia.

Agora: carga do dia, rotas longas em 7d (so quando a rota e longa),
rotas em 7d, rotas em 30d, agent_id.

A mudanca e de ORDENACAO, nunca de filtro -- nenhuma rota pode ficar sem
motorista por causa dela.

Rodar (da raiz):
    py -3.11 -m unittest roteirizacao.test_alocacao_justa -v
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

from regras.preferencias_motoristas import MotoristaPreferencias  # noqa: E402
import alocacao_motoristas  # noqa: E402

# Terca-feira: dia util (rodizio ativo), dentro do padrao semanal abaixo.
DATA = date(2026, 9, 22)
SUBLOTE = [{"address": "Rua X, 100", "dimension_3": 10}]


def _motorista(agent_id, zonas=("ZONA NORTE",), placa=None, tipo="FIORINO"):
    return MotoristaPreferencias(
        agent_id=agent_id, vehicle_id=agent_id, nome=f"Motorista {agent_id}",
        aceita_viagens=True, dias_disponiveis={0, 1, 2, 3, 4, 5, 6},
        max_rotas_dia=1, ativo=True, zonas_preferidas=set(zonas),
        telefone=None, email=None, placa=placa, tipo_veiculo=tipo, cpf=None,
    )


class BaseAlocacao(unittest.TestCase):
    """Neutraliza os classificadores que dependem de geocodificacao: o
    foco aqui e a ORDEM, nao a elegibilidade."""

    def setUp(self):
        for nome, valor in (
            ("classificar_rota_viagem", lambda *a, **k: False),
            ("classificar_rota_zona", lambda *a, **k: None),
            ("sublote_em_area_rodizio", lambda *a, **k: False),
        ):
            patcher = mock.patch.object(alocacao_motoristas, nome, valor)
            patcher.start()
            self.addCleanup(patcher.stop)

    def escolher(self, motoristas, **kwargs):
        return alocacao_motoristas.selecionar_motorista_equitativo(
            SUBLOTE, DATA, motoristas, kwargs.pop("carga", {}), None, **kwargs,
        )


class TestJusticaPorHistorico(BaseAlocacao):
    def test_menos_rotas_em_7d_ganha_do_menor_agent_id(self):
        motoristas = [_motorista(1), _motorista(99)]
        escolhido = self.escolher(motoristas, rotas_7d={1: 4, 99: 0})
        self.assertEqual(escolhido.agent_id, 99)

    def test_empate_em_7d_decide_por_30d(self):
        motoristas = [_motorista(1), _motorista(99)]
        escolhido = self.escolher(motoristas, rotas_7d={1: 2, 99: 2},
                                  rotas_30d={1: 10, 99: 3})
        self.assertEqual(escolhido.agent_id, 99)

    def test_empate_total_decide_por_agent_id(self):
        motoristas = [_motorista(99), _motorista(1)]
        escolhido = self.escolher(motoristas, rotas_7d={1: 2, 99: 2},
                                  rotas_30d={1: 5, 99: 5})
        self.assertEqual(escolhido.agent_id, 1)

    def test_carga_do_dia_vem_antes_do_historico(self):
        # m1 pode pegar 2 rotas/dia e ja pegou 1; m2 esta zerado no dia mas
        # rodou muito na semana. A carga do dia manda.
        m1 = replace(_motorista(1), max_rotas_dia=2)
        escolhido = self.escolher([m1, _motorista(2)],
                                  carga={1: 1}, rotas_7d={1: 0, 2: 9})
        self.assertEqual(escolhido.agent_id, 2, "quem esta zerado no dia vem primeiro")


class TestRodizioDeRotasLongas(BaseAlocacao):
    def test_rota_longa_prefere_quem_pegou_menos_longas(self):
        motoristas = [_motorista(1), _motorista(2)]
        escolhido = self.escolher(
            motoristas, rota_longa=True,
            longas_7d={1: 2, 2: 0},
            rotas_7d={1: 0, 2: 5},   # 2 rodou MAIS no total e ainda assim ganha
        )
        self.assertEqual(escolhido.agent_id, 2)

    def test_rota_curta_ignora_o_rodizio_de_longas(self):
        motoristas = [_motorista(1), _motorista(2)]
        escolhido = self.escolher(
            motoristas, rota_longa=False,
            longas_7d={1: 2, 2: 0},
            rotas_7d={1: 0, 2: 5},
        )
        self.assertEqual(escolhido.agent_id, 1, "numa rota curta vale o historico geral")


class TestNadaVirouFiltro(BaseAlocacao):
    def test_zona_continua_travando(self):
        # Zona reconhecida e nenhum motorista atende -> ninguem elegivel.
        with mock.patch.object(
            alocacao_motoristas, "classificar_rota_zona", lambda *a, **k: "ZONA SUL"
        ):
            escolhido = self.escolher([_motorista(1, zonas=("ZONA NORTE",))],
                                      rotas_7d={1: 0})
        self.assertIsNone(escolhido)

    def test_historico_nao_exclui_ninguem(self):
        # Um unico motorista, com historico pessimo: ainda assim e escolhido.
        escolhido = self.escolher([_motorista(1)], rota_longa=True,
                                  longas_7d={1: 99}, rotas_7d={1: 99},
                                  rotas_30d={1: 99})
        self.assertEqual(escolhido.agent_id, 1)


class TestCompatibilidade(BaseAlocacao):
    def test_sem_parametros_novos_escolhe_igual_a_antes(self):
        # Comportamento historico: menor carga do dia, desempate por agent_id.
        escolhido = self.escolher([_motorista(99), _motorista(1)])
        self.assertEqual(escolhido.agent_id, 1)


if __name__ == "__main__":
    unittest.main()
