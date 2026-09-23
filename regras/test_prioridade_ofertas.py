# -*- coding: utf-8 -*-
"""
Testes de regras/prioridade_ofertas.py (ondas de prioridade do
marketplace, Hugo 03/09) e das partes de regras/ofertas_rota.py que
elas usam (fila de avisos por onda).

    py -3.11 -m unittest regras.test_prioridade_ofertas -v
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from regras import ofertas_rota, prioridade_ofertas
from regras.preferencias_motoristas import MotoristaPreferencias
from regras.prioridade_ofertas import contar_rotas_recentes, priorizar, resumo_ondas, formatar_utc


def _motorista(agent_id, placa=None, max_rotas_dia=1):
    return MotoristaPreferencias(
        agent_id=agent_id, vehicle_id=None, nome=f"M{agent_id}", aceita_viagens=True,
        dias_disponiveis=[0, 1, 2, 3, 4], max_rotas_dia=max_rotas_dia, ativo=True,
        zonas_preferidas=["ZONA SUL"], telefone=f"1199999{agent_id:04d}", placa=placa,
    )


QUARTA = date(2026, 9, 2)   # quarta-feira: rodízio de finais 5 e 6
AGORA = datetime(2026, 9, 1, 20, 0, 0, tzinfo=timezone.utc)
CFG = {"marketplace_rotas": {"tamanho_onda": 2, "intervalo_minutos": 15, "max_ondas": 3}}


class TestPriorizar(unittest.TestCase):
    def test_ordena_por_rodizio_depois_historico(self):
        # 105: placa final 5 -> restrita na quarta; rota FORA do centro -> prioridade (leitura B)
        elegiveis = [_motorista(101), _motorista(102), _motorista(105, placa="ABC1D25"), _motorista(103)]
        curta = {101: 3, 102: 0, 103: 1, 105: 5}
        longa = {101: 10, 102: 4, 103: 4, 105: 20}
        saida = priorizar(elegiveis, QUARTA, rota_em_area_rodizio=False, contagem_alocacoes_dia={},
                          config=CFG, agora=AGORA, contagem_curta=curta, contagem_longa=longa)
        self.assertEqual([p.motorista.agent_id for p in saida], [105, 102, 103, 101])
        self.assertTrue(saida[0].prioridade_rodizio)
        self.assertEqual([p.onda for p in saida], [0, 0, 1, 1])
        self.assertEqual(saida[0].visivel_a_partir_de, formatar_utc(AGORA))
        self.assertEqual(saida[2].visivel_a_partir_de, formatar_utc(AGORA + timedelta(minutes=15)))
        self.assertEqual(resumo_ondas(saida), {0: 2, 1: 2})

    def test_rota_dentro_do_centro_nao_da_prioridade_por_rodizio(self):
        # dentro do centro o restrito nem seria elegível; se chegou aqui (placa
        # de final liberado), o critério não separa ninguém
        elegiveis = [_motorista(105, placa="ABC1D25"), _motorista(102)]
        saida = priorizar(elegiveis, QUARTA, rota_em_area_rodizio=True, contagem_alocacoes_dia={},
                          config=CFG, agora=AGORA, contagem_curta={105: 0, 102: 0}, contagem_longa={})
        self.assertEqual([p.motorista.agent_id for p in saida], [102, 105])
        self.assertFalse(any(p.prioridade_rodizio for p in saida))

    def test_fim_de_semana_sem_rodizio(self):
        sabado = date(2026, 9, 5)
        saida = priorizar([_motorista(105, placa="ABC1D25"), _motorista(102)], sabado, False, {},
                          config=CFG, agora=AGORA, contagem_curta={}, contagem_longa={})
        self.assertEqual([p.motorista.agent_id for p in saida], [102, 105])

    def test_ultima_onda_recebe_o_resto_e_ondas_desligadas(self):
        elegiveis = [_motorista(i) for i in range(1, 9)]
        saida = priorizar(elegiveis, QUARTA, False, {}, config=CFG, agora=AGORA, contagem_curta={}, contagem_longa={})
        self.assertEqual([p.onda for p in saida], [0, 0, 1, 1, 2, 2, 2, 2])

        desligado = priorizar(elegiveis, QUARTA, False, {}, config={"marketplace_rotas": {"tamanho_onda": 0}},
                              agora=AGORA, contagem_curta={}, contagem_longa={})
        self.assertTrue(all(p.onda == 0 for p in desligado))
        self.assertTrue(all(p.visivel_a_partir_de == formatar_utc(AGORA) for p in desligado))

    def test_vagas_restantes_e_json(self):
        m = _motorista(7, max_rotas_dia=2)
        saida = priorizar([m], QUARTA, False, {7: 1}, config=CFG, agora=AGORA, contagem_curta={}, contagem_longa={})
        j = saida[0].para_json()
        self.assertEqual(j["vagas_restantes"], 1)
        self.assertEqual(j["telefone_ultimos4"], "0007")
        self.assertEqual(j["onda"], 0)
        self.assertIn("visivel_a_partir_de", j)
        # nunca menos que 1 (elegível implica que ainda cabe pelo menos 1)
        saida = priorizar([m], QUARTA, False, {7: 5}, config=CFG, agora=AGORA, contagem_curta={}, contagem_longa={})
        self.assertEqual(saida[0].vagas_restantes, 1)

    def test_config_invalida_cai_no_padrao(self):
        cfg = prioridade_ofertas.carregar_config({"marketplace_rotas": {"tamanho_onda": "x", "intervalo_minutos": -5}})
        self.assertEqual(cfg["tamanho_onda"], 3)
        self.assertEqual(cfg["intervalo_minutos"], 0)
        self.assertEqual(prioridade_ofertas.carregar_config(None), prioridade_ofertas.PADRAO_CONFIG)


class TestContarRotasRecentes(unittest.TestCase):
    def test_conta_nucleo_e_rascunhos_sem_duplicar(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE nucleo_rotas (id INTEGER PRIMARY KEY, data_rota TEXT, agent_id INTEGER,
                                       rascunho_id INTEGER, vuupt_route_id INTEGER, status TEXT);
            CREATE TABLE rascunhos_rota (id INTEGER PRIMARY KEY, data_alvo TEXT, agent_id INTEGER,
                                         vuupt_route_id INTEGER, status TEXT);
        """)
        conn.executemany("INSERT INTO nucleo_rotas (data_rota, agent_id, rascunho_id, vuupt_route_id, status) VALUES (?,?,?,?,?)", [
            ("2026-08-28", 1, 10, 900, "CONCLUIDA"),
            ("2026-08-30", 1, None, 901, "CONCLUIDA"),
            ("2026-08-30", 2, None, 902, "CANCELADA"),   # não conta
            ("2026-08-20", 1, None, 903, "CONCLUIDA"),   # fora da janela de 7d, dentro da de 30d
        ])
        conn.executemany("INSERT INTO rascunhos_rota (id, data_alvo, agent_id, vuupt_route_id, status) VALUES (?,?,?,?,?)", [
            (10, "2026-08-28", 1, 900, "ENVIADO"),      # já está no núcleo -> não duplica
            (11, "2026-09-02", 2, None, "RASCUNHO"),    # alocada hoje, ainda sem Vuupt -> conta
            (12, "2026-09-02", 3, None, "OFERTADA"),    # escolhida no marketplace -> conta
            (13, "2026-09-01", 3, None, "DESCARTADO"),  # não conta
        ])
        conn.commit()
        self.assertEqual(contar_rotas_recentes(date(2026, 9, 2), 7, conn), {1: 2, 2: 1, 3: 1})
        self.assertEqual(contar_rotas_recentes(date(2026, 9, 2), 30, conn), {1: 3, 2: 1, 3: 1})

    def test_tabelas_ausentes(self):
        self.assertEqual(contar_rotas_recentes(date(2026, 9, 2), 7, sqlite3.connect(":memory:")), {})


class TestContarRotasLongasRecentes(unittest.TestCase):
    """Rodizio de rotas longas (Hugo, 22/09): quem pegou rota pesada na
    semana nao pega a proxima. Longa = acima do limiar (7h por padrao)."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE nucleo_rotas (id INTEGER PRIMARY KEY, data_rota TEXT, agent_id INTEGER,
                                       status TEXT, rascunho_id INTEGER, vuupt_route_id INTEGER,
                                       horas_estimadas REAL);
            CREATE TABLE rascunhos_rota (id INTEGER PRIMARY KEY, data_alvo TEXT, agent_id INTEGER,
                                         status TEXT, vuupt_route_id INTEGER, horas_estimadas REAL);
        """)
        self.addCleanup(self.conn.close)

    def _rota(self, agent_id, dias_atras, horas, status="CONCLUIDA"):
        data = (date(2026, 9, 22) - timedelta(days=dias_atras)).isoformat()
        self.conn.execute(
            "INSERT INTO nucleo_rotas (data_rota, agent_id, status, horas_estimadas) VALUES (?,?,?,?)",
            (data, agent_id, status, horas),
        )

    def _contar(self, dias=7, limiar=7.0):
        return prioridade_ofertas.contar_rotas_longas_recentes(date(2026, 9, 22), dias, limiar, self.conn)

    def test_conta_so_acima_do_limiar(self):
        self._rota(1, 1, 8.5)
        self._rota(1, 2, 6.0)
        self._rota(2, 1, 7.0)   # exatamente no limiar nao conta
        contagem = self._contar()
        self.assertEqual(contagem.get(1), 1)
        self.assertIsNone(contagem.get(2))

    def test_ignora_cancelada(self):
        self._rota(1, 1, 9.0, status="CANCELADA")
        self.assertEqual(self._contar(), {})

    def test_ignora_rota_sem_horas_gravadas(self):
        self._rota(1, 1, None)
        self.assertEqual(self._contar(), {})

    def test_respeita_a_janela(self):
        self._rota(1, 3, 8.0)
        self._rota(1, 20, 8.0)
        self.assertEqual(self._contar(dias=7).get(1), 1)
        self.assertEqual(self._contar(dias=30).get(1), 2)

    def test_nao_conta_duas_vezes_a_mesma_rota(self):
        # Mesma rota nas duas tabelas (rascunho que virou rota na Vuupt).
        self.conn.execute(
            "INSERT INTO nucleo_rotas (data_rota, agent_id, status, rascunho_id, horas_estimadas) "
            "VALUES ('2026-09-21', 1, 'CONCLUIDA', 55, 8.0)"
        )
        self.conn.execute(
            "INSERT INTO rascunhos_rota (id, data_alvo, agent_id, status, horas_estimadas) "
            "VALUES (55, '2026-09-21', 1, 'ENVIADO', 8.0)"
        )
        self.assertEqual(self._contar().get(1), 1)

    def test_conta_rascunho_que_ainda_nao_virou_rota(self):
        self.conn.execute(
            "INSERT INTO rascunhos_rota (id, data_alvo, agent_id, status, horas_estimadas) "
            "VALUES (77, '2026-09-21', 3, 'RASCUNHO', 8.0)"
        )
        self.assertEqual(self._contar().get(3), 1)

    def test_banco_sem_a_coluna_nao_quebra(self):
        # Primeira noite apos o deploy: o job pode contar antes de qualquer
        # migracao criar horas_estimadas. Tem que devolver vazio, nao
        # derrubar a roteirizacao.
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE nucleo_rotas (id INTEGER PRIMARY KEY, data_rota TEXT, agent_id INTEGER,
                                       rascunho_id INTEGER, vuupt_route_id INTEGER, status TEXT);
            CREATE TABLE rascunhos_rota (id INTEGER PRIMARY KEY, data_alvo TEXT, agent_id INTEGER,
                                         vuupt_route_id INTEGER, status TEXT);
            INSERT INTO nucleo_rotas (data_rota, agent_id, status) VALUES ('2026-09-21', 1, 'CONCLUIDA');
        """)
        self.assertEqual(prioridade_ofertas.contar_rotas_longas_recentes(date(2026, 9, 22), 7, 7.0, conn), {})


class TestFilaDeAvisos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "dados.db"
        self.patch = mock.patch.object(ofertas_rota, "_DB_PATH", self.db)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_liberacoes_por_onda_e_marcacao(self):
        t0 = formatar_utc(AGORA)
        t1 = formatar_utc(AGORA + timedelta(minutes=15))
        ofertas_rota.criar_ou_atualizar_oferta(50, QUARTA, {"regiao": "ZONA SUL"}, [
            {"agent_id": 1, "onda": 0, "visivel_a_partir_de": t0},
            {"agent_id": 2, "onda": 1, "visivel_a_partir_de": t1},
            {"agent_id": 3},  # formato antigo, sem onda -> visível desde sempre
        ])
        pend = ofertas_rota.listar_liberacoes_pendentes(t0)
        self.assertEqual(sorted(p["agent_id"] for p in pend), [1, 3])
        self.assertEqual(pend[0]["data_alvo"], "2026-09-02")

        ofertas_rota.marcar_avisados(pend)
        self.assertEqual(ofertas_rota.listar_liberacoes_pendentes(t0), [])
        # onda 1 abre
        pend = ofertas_rota.listar_liberacoes_pendentes(t1)
        self.assertEqual([p["agent_id"] for p in pend], [2])
        self.assertEqual(pend[0]["onda"], 1)
        # restrição ao conjunto de quem chama
        self.assertEqual(ofertas_rota.listar_liberacoes_pendentes(t1, apenas_agent_ids={99}), [])

        # republicar zera os avisos
        ofertas_rota.criar_ou_atualizar_oferta(50, QUARTA, {}, [{"agent_id": 1, "onda": 0, "visivel_a_partir_de": t0}])
        self.assertEqual([p["agent_id"] for p in ofertas_rota.listar_liberacoes_pendentes(t0)], [1])

        # oferta cancelada some da fila
        ofertas_rota.cancelar_oferta(50)
        self.assertEqual(ofertas_rota.listar_liberacoes_pendentes(t1), [])

    def test_contar_escolhidas_no_dia(self):
        ofertas_rota.criar_ou_atualizar_oferta(60, QUARTA, {}, [{"agent_id": 1}])
        ofertas_rota.criar_ou_atualizar_oferta(61, QUARTA, {}, [{"agent_id": 1}])
        ofertas_rota.aplicar_resposta_remota(60, 1, "2026-09-01 20:00:00")
        self.assertEqual(ofertas_rota.contar_escolhidas_no_dia(1, "2026-09-02"), 1)
        self.assertEqual(ofertas_rota.contar_escolhidas_no_dia(1, "2026-09-03"), 0)


if __name__ == "__main__":
    unittest.main()
