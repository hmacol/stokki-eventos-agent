# -*- coding: utf-8 -*-
"""
test_rota_enviada_edicao.py -- editar rota ja enviada pela tela Planejamento (Hugo, 10/11):
trocar motorista/carro, incluir pedido do pool e tirar pedido nao comecado (com motivo), mesmo
com a rota iniciada. Tirado de rota iniciada vira Retornando (fica no carro ate o galpao receber).
O "Cancelar pedido" em rota enviada usa a mesma regra (pedido nao comecado sai, mesmo com a rota
iniciada) e nao vira Retornando. Rota da Lalamove com corrida lancada nao aceita edicao.

Nenhum teste toca na Vuupt nem no dados.db real: o banco vai pra uma pasta temporaria e o
rotas_client e um duble com a rota "ao vivo" em memoria (edicao_rota roda de verdade por cima).

    py -3.11 -m unittest test_rota_enviada_edicao -v   (de dentro de painel_agentes/)
"""
import copy
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import rascunhos_rota  # noqa: E402
import expedicao  # noqa: E402
import planejamento_rotas  # noqa: E402
import pedidos_retornando  # noqa: E402

ROTA_ID = 900
AGENTE, AGENTE_NOVO = 47084, 51111


def _parada(sid, codigo):
    return {"service_id": sid, "codigo": codigo, "titulo": "", "endereco": f"Rua {sid}, São Paulo - SP",
            "latitude": -23.5, "longitude": -46.6, "sender_id": 10, "remetente_nome": "X",
            "destinatario_nome": "", "nivel_dificuldade": 1, "volume_caixas": 2,
            "horario_atendimento_inicio": "00:00", "horario_atendimento_fim": "23:59",
            "janela_inicio": None, "janela_fim": None, "janela_fonte": None}


class _VuuptFalsa:
    """Rota ao vivo em memoria + registro das chamadas de escrita."""

    def __init__(self, status_rota="assigned", servicos=None, agent_id=AGENTE):
        self.rota = {"id": ROTA_ID, "status": status_rota, "agent_id": agent_id, "vehicle_id": 1,
                     "services": [dict(s) for s in (servicos or [])]}
        self.chamadas = []
        self.recusar_add = []

    def buscar_rota(self, token, route_id, include=None):
        return {"route": copy.deepcopy(self.rota)}

    def remover_servico_da_rota(self, token, route_id, service_id):
        self.chamadas.append(("DELETE", service_id))
        self.rota["services"] = [s for s in self.rota["services"] if s["id"] != service_id]
        return {}

    def cancelar_rota(self, token, route_id, services_action="keep"):
        self.chamadas.append(("CANCELAR", services_action))
        self.rota["status"] = "canceled"
        return {}

    def atualizar_motorista_veiculo(self, token, route_id, agent_id, vehicle_id):
        self.chamadas.append(("PUT", agent_id, vehicle_id))
        self.rota.update(agent_id=agent_id, vehicle_id=vehicle_id)
        return {"id": route_id, "agent_id": agent_id, "vehicle_id": vehicle_id}

    def adicionar_atividades(self, token, route_id, service_ids, position="end"):
        self.chamadas.append(("POST", list(service_ids), position))
        falhas = [sid for sid in service_ids if sid in self.recusar_add]
        self.rota["services"] += [{"id": sid, "code": f"#PS-{sid}", "status": "assigned"}
                                  for sid in service_ids if sid not in falhas]
        return {"data": {"info": {"failed": [{"id": sid} for sid in falhas]}}}

    def escritas(self):
        return [c for c in self.chamadas if c[0] in ("DELETE", "CANCELAR", "PUT", "POST")]


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        self.vuupt = _VuuptFalsa()
        self.retrato = ({}, None)
        self.agendar = mock.Mock(return_value=True)
        self.descartar_doc = mock.Mock(return_value=0)
        self.rota_enviada = mock.Mock()
        self.marcar_alocado = mock.Mock()
        self.marcar_em_rota = mock.Mock()
        patches = [
            mock.patch.object(rascunhos_rota, "DB_PATH", self.db),
            mock.patch.object(expedicao, "_DB_PATH", self.db),
            mock.patch.object(rascunhos_rota, "_carregar_config", return_value={}),
            mock.patch.object(rascunhos_rota, "_recalcular_km_silencioso"),
            mock.patch("rotas_client.buscar_rota", self.vuupt.buscar_rota),
            mock.patch("rotas_client.remover_servico_da_rota", self.vuupt.remover_servico_da_rota),
            mock.patch("rotas_client.cancelar_rota", self.vuupt.cancelar_rota),
            mock.patch("rotas_client.atualizar_motorista_veiculo", self.vuupt.atualizar_motorista_veiculo),
            mock.patch("rotas_client.adicionar_atividades", self.vuupt.adicionar_atividades),
            mock.patch("rotas_client.atualizar_rota", side_effect=AssertionError("PUT com lista nao deve ser usado")),
            mock.patch("documentacao_rota.agendar", self.agendar),
            mock.patch("documentacao_rota.descartar", self.descartar_doc),
            mock.patch("fingerprint_rotas.marcar_alocado", self.marcar_alocado),
            mock.patch("nucleo.pedidos.marcar_em_rota_por_service_ids", self.marcar_em_rota),
            mock.patch.object(rascunhos_rota.eventos_freshhub, "rota_enviada", self.rota_enviada),
            mock.patch.object(rascunhos_rota.prontidao_stokki, "carregar_retrato_stokki",
                              side_effect=lambda *a, **k: self.retrato),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _rota_viva(self, status_rota, servicos):
        self.vuupt.rota["status"] = status_rota
        self.vuupt.rota["services"] = [dict(s) for s in servicos]

    def _inserir_rascunho(self, paradas, status="ENVIADO", lalamove_order_id=None, agent_id=AGENTE):
        conn = rascunhos_rota._conectar()
        try:
            cur = conn.execute(
                "INSERT INTO rascunhos_rota (data_alvo, lote_id, nome, agent_id, vehicle_id, motorista_nome, "
                "start_location_base_id, end_location_base_id, start_at, status, vuupt_route_id, lalamove_order_id) "
                "VALUES ('2026-11-10', 'lote-teste', 'Planejamento - 10/11/2026 - #3', ?, 1, 'Vinicius', "
                "6950, 6950, '2026-11-10T09:00:00Z', ?, ?, ?)",
                (agent_id, status, ROTA_ID if status == "ENVIADO" else None, lalamove_order_id))
            rid = cur.lastrowid
            for ordem, p in enumerate(paradas):
                rascunhos_rota._inserir_parada(conn, rid, ordem, p)
            conn.commit()
            return rid
        finally:
            conn.close()

    def _rascunho(self, rid):
        return rascunhos_rota.buscar_rascunho(rid)

    def _retornando(self):
        conn = pedidos_retornando.conectar(self.db)
        try:
            return pedidos_retornando.ativos(conn)
        finally:
            conn.close()

    def _exclusoes(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT rota_id, service_id, codigo_pedido, motivo, observacao, usuario "
                                "FROM expedicao_exclusoes").fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()


class TrocarMotoristaTestCase(_Base):
    def test_troca_motorista_enviada_chama_vuupt_e_grava(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1")])
        self._rota_viva("assigned", [{"id": 101, "code": "#PS-1", "status": "assigned"}])
        rascunhos_rota.trocar_motorista(rid, AGENTE_NOVO, 77, "Caio", token="tk")
        self.assertEqual(self.vuupt.escritas(), [("PUT", AGENTE_NOVO, 77)])
        r = self._rascunho(rid)
        self.assertEqual((r["agent_id"], r["vehicle_id"], r["motorista_nome"]), (AGENTE_NOVO, 77, "Caio"))

    def test_troca_motorista_enviada_erro_nao_grava(self):
        # rota iniciada: motorista nao muda (so o carro)
        rid = self._inserir_rascunho([_parada(101, "#PS-1"), _parada(102, "#PS-2")])
        self._rota_viva("started", [{"id": 101, "code": "#PS-1", "status": "done"},
                                    {"id": 102, "code": "#PS-2", "status": "assigned"}])
        with self.assertRaisesRegex(ValueError, "Rota já começou: só o carro pode mudar."):
            rascunhos_rota.trocar_motorista(rid, AGENTE_NOVO, 77, "Caio", token="tk")
        self.assertEqual(self.vuupt.escritas(), [])
        r = self._rascunho(rid)
        self.assertEqual((r["agent_id"], r["vehicle_id"], r["motorista_nome"]), (AGENTE, 1, "Vinicius"))

    def test_troca_so_carro_em_rota_iniciada_passa(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1")])
        self._rota_viva("started", [{"id": 101, "code": "#PS-1", "status": "on_route"}])
        rascunhos_rota.trocar_motorista(rid, AGENTE, 77, "Vinicius", token="tk")
        self.assertEqual(self.vuupt.escritas(), [("PUT", AGENTE, 77)])
        self.assertEqual(self._rascunho(rid)["vehicle_id"], 77)

    def test_troca_em_rascunho_nao_chama_vuupt(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1")], status="RASCUNHO")
        rascunhos_rota.trocar_motorista(rid, AGENTE_NOVO, 77, "Caio")
        self.assertEqual(self.vuupt.escritas(), [])
        self.assertEqual(self._rascunho(rid)["agent_id"], AGENTE_NOVO)


class AdicionarTestCase(_Base):
    def test_adicionar_em_enviada_grava_parada_e_agenda_romaneio(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1")])
        self._rota_viva("started", [{"id": 101, "code": "#PS-1", "status": "done"}])
        nova = _parada(105, "#PS-5")
        resultado = rascunhos_rota.adicionar_em_rota_enviada(rid, nova, "tk", "hugo")
        self.assertTrue(resultado["ok"])
        self.assertEqual(self.vuupt.escritas(), [("POST", [105], "end")])
        self.assertEqual([p["service_id"] for p in self._rascunho(rid)["paradas"]], [101, 105])
        self.marcar_alocado.assert_called_once_with(105, ROTA_ID)
        self.marcar_em_rota.assert_called_once_with([105], ROTA_ID)
        self.agendar.assert_called_once_with(ROTA_ID, "2026-11-10", motivo="pedido incluido na rota")
        self.rota_enviada.assert_called_once()
        paradas_evento = self.rota_enviada.call_args.args[1]
        self.assertEqual([p["service_id"] for p in paradas_evento], [105])
        self.assertEqual(self.rota_enviada.call_args.kwargs["vuupt_route_id"], ROTA_ID)
        self.assertEqual(self.rota_enviada.call_args.kwargs["rascunho_id"], rid)

    def test_adicionar_recusa_em_espera_stokki(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1")])
        self._rota_viva("assigned", [{"id": 101, "code": "#PS-1", "status": "assigned"}])
        self.retrato = ({"PS-5": "On hold"}, "10/11 10:00")
        with self.assertRaisesRegex(ValueError, "Em espera"):
            rascunhos_rota.adicionar_em_rota_enviada(rid, _parada(105, "#PS-5"), "tk", "hugo")
        self.assertEqual(self.vuupt.escritas(), [])
        self.assertEqual([p["service_id"] for p in self._rascunho(rid)["paradas"]], [101])

    def test_adicionar_em_separacao_stokki_passa(self):
        # Hugo, 09/11: so "Em espera" barra; separacao/conferencia vao pra rota
        rid = self._inserir_rascunho([_parada(101, "#PS-1")])
        self._rota_viva("assigned", [{"id": 101, "code": "#PS-1", "status": "assigned"}])
        self.retrato = ({"PS-5": "Separating"}, "10/11 10:00")
        rascunhos_rota.adicionar_em_rota_enviada(rid, _parada(105, "#PS-5"), "tk", "hugo")
        self.assertEqual(self.vuupt.escritas(), [("POST", [105], "end")])

    def test_adicionar_recusa_retornando(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1")])
        conn = pedidos_retornando.conectar(self.db)
        try:
            pedidos_retornando.marcar(conn, service_id=105, codigo="#PS-5", vuupt_route_id=555, agent_id=1,
                                      motorista_nome="Joao", motivo="Outros", observacao="", usuario="x")
        finally:
            conn.close()
        with self.assertRaisesRegex(ValueError, "Pedido retornando ao galpão"):
            rascunhos_rota.adicionar_em_rota_enviada(rid, _parada(105, "#PS-5"), "tk", "hugo")
        self.assertEqual(self.vuupt.escritas(), [])

    def test_adicionar_recusa_rota_encerrada(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1")])
        self._rota_viva("finished", [{"id": 101, "code": "#PS-1", "status": "done"}])
        with self.assertRaisesRegex(ValueError, f"Rota #{ROTA_ID} já foi encerrada — não dá pra incluir pedido."):
            rascunhos_rota.adicionar_em_rota_enviada(rid, _parada(105, "#PS-5"), "tk", "hugo")
        self.assertEqual(self.vuupt.escritas(), [])

    def test_adicionar_recusado_pela_vuupt_nao_grava(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1")])
        self._rota_viva("assigned", [{"id": 101, "code": "#PS-1", "status": "assigned"}])
        self.vuupt.recusar_add = [105]
        with self.assertRaises(ValueError):
            rascunhos_rota.adicionar_em_rota_enviada(rid, _parada(105, "#PS-5"), "tk", "hugo")
        self.assertEqual([p["service_id"] for p in self._rascunho(rid)["paradas"]], [101])
        self.agendar.assert_not_called()


class RemoverTestCase(_Base):
    def test_remover_em_rota_iniciada_marca_retornando(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1"), _parada(102, "#PS-2")])
        self._rota_viva("started", [{"id": 101, "code": "#PS-1", "status": "done"},
                                    {"id": 102, "code": "#PS-2", "status": "assigned"}])
        resultado = rascunhos_rota.remover_de_rota_enviada(rid, 102, "Não coube", "sem espaco", "tk", "hugo")
        self.assertTrue(resultado["ok"])
        self.assertTrue(resultado["retornando"])
        self.assertFalse(resultado["rota_cancelada"])
        self.assertEqual(self.vuupt.escritas(), [("DELETE", 102)])
        self.assertEqual([p["service_id"] for p in self._rascunho(rid)["paradas"]], [101])
        ativos = self._retornando()
        self.assertEqual(list(ativos), [102])
        self.assertEqual((ativos[102]["vuupt_route_id"], ativos[102]["agent_id"], ativos[102]["motorista_nome"],
                          ativos[102]["motivo"], ativos[102]["usuario"]),
                         (ROTA_ID, AGENTE, "Vinicius", "Não coube", "hugo"))
        self.assertEqual(self._exclusoes(), [(ROTA_ID, 102, "#PS-2", "Não coube", "sem espaco", "hugo")])
        self.agendar.assert_called_once()
        self.assertEqual(self._rascunho(rid)["status"], "ENVIADO")

    def test_remover_em_rota_nao_iniciada_nao_marca_retornando(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1"), _parada(102, "#PS-2")])
        self._rota_viva("assigned", [{"id": 101, "code": "#PS-1", "status": "accepted"},
                                     {"id": 102, "code": "#PS-2", "status": "assigned"}])
        resultado = rascunhos_rota.remover_de_rota_enviada(rid, 102, "Outros", "", "tk", "hugo")
        self.assertTrue(resultado["ok"])
        self.assertFalse(resultado["retornando"])
        self.assertEqual(self.vuupt.escritas(), [("DELETE", 102)])
        self.assertEqual(self._retornando(), {})
        self.assertEqual(len(self._exclusoes()), 1)
        self.assertEqual(resultado["parada_removida"]["service_id"], 102)

    def test_remover_ultimo_pedido_de_rota_nao_iniciada_cancela_e_volta_rascunho(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1")])
        self._rota_viva("assigned", [{"id": 101, "code": "#PS-1", "status": "assigned"}])
        resultado = rascunhos_rota.remover_de_rota_enviada(rid, 101, "Outros", "", "tk", "hugo")
        self.assertTrue(resultado["rota_cancelada"])
        self.assertEqual(self.vuupt.escritas(), [("CANCELAR", "unassign")])
        r = self._rascunho(rid)
        self.assertEqual((r["status"], r["vuupt_route_id"], r["paradas"]), ("RASCUNHO", None, []))
        self.descartar_doc.assert_called_once()
        self.agendar.assert_not_called()

    def test_remover_sem_motivo_valido_recusa(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1"), _parada(102, "#PS-2")])
        self._rota_viva("assigned", [{"id": 101, "code": "#PS-1", "status": "assigned"},
                                     {"id": 102, "code": "#PS-2", "status": "assigned"}])
        for motivo in (None, "", "qualquer"):
            with self.assertRaisesRegex(ValueError, "Motivo"):
                rascunhos_rota.remover_de_rota_enviada(rid, 102, motivo, "", "tk", "hugo")
        self.assertEqual(self.vuupt.escritas(), [])
        self.assertEqual(len(self._rascunho(rid)["paradas"]), 2)
        self.assertEqual(self._exclusoes(), [])

    def test_remover_pedido_comecado_recusa_pelo_status_ao_vivo(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1"), _parada(102, "#PS-2")])
        self._rota_viva("started", [{"id": 101, "code": "#PS-1", "status": "assigned"},
                                    {"id": 102, "code": "#PS-2", "status": "on_route"}])
        with self.assertRaisesRegex(ValueError, "já começou"):
            rascunhos_rota.remover_de_rota_enviada(rid, 102, "Outros", "", "tk", "hugo")
        self.assertEqual(self.vuupt.escritas(), [])
        self.assertEqual(self._retornando(), {})
        self.assertEqual(self._exclusoes(), [])


class LalamoveTestCase(_Base):
    def test_lalamove_com_corrida_recusa_edicao(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1"), _parada(102, "#PS-2")], lalamove_order_id="LL-1")
        self._rota_viva("assigned", [{"id": 101, "code": "#PS-1", "status": "assigned"},
                                     {"id": 102, "code": "#PS-2", "status": "assigned"}])
        with self.assertRaisesRegex(ValueError, "Lalamove"):
            rascunhos_rota.trocar_motorista(rid, AGENTE_NOVO, 77, "Caio", token="tk")
        with self.assertRaisesRegex(ValueError, "Lalamove"):
            rascunhos_rota.adicionar_em_rota_enviada(rid, _parada(105, "#PS-5"), "tk", "hugo")
        with self.assertRaisesRegex(ValueError, "Lalamove"):
            rascunhos_rota.remover_de_rota_enviada(rid, 102, "Outros", "", "tk", "hugo")
        self.assertEqual(self.vuupt.escritas(), [])
        r = self._rascunho(rid)
        self.assertEqual((r["agent_id"], len(r["paradas"])), (AGENTE, 2))


class CancelarPedidoTestCase(_Base):
    def setUp(self):
        super().setUp()
        self.vuupt_client = mock.Mock()
        for p in (mock.patch.object(planejamento_rotas, "_carregar_config", return_value={}),
                  mock.patch.object(planejamento_rotas, "VuuptClient", return_value=self.vuupt_client),
                  mock.patch.object(planejamento_rotas, "_ressincronizar")):
            p.start()
            self.addCleanup(p.stop)

    def test_cancelar_pedido_em_rota_iniciada_usa_regra_nova(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1"), _parada(102, "#PS-2"), _parada(103, "#PS-3")])
        self._rota_viva("started", [{"id": 101, "code": "#PS-1", "status": "done"},
                                    {"id": 102, "code": "#PS-2", "status": "assigned"},
                                    {"id": 103, "code": "#PS-3", "status": "on_route"}])
        # pendente sai (DELETE antes do cancelamento) e nao vira Retornando
        resultado = planejamento_rotas.cancelar_pedido(102, rid)
        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(self.vuupt.escritas(), [("DELETE", 102)])
        self.vuupt_client.cancelar_servico_oficial.assert_called_once_with(102)
        self.assertEqual([p["service_id"] for p in self._rascunho(rid)["paradas"]], [101, 103])
        self.assertEqual(self._retornando(), {})
        # on_route recusa e nao cancela
        resultado = planejamento_rotas.cancelar_pedido(103, rid)
        self.assertFalse(resultado["ok"])
        self.assertIn("já começou", resultado["erro"])
        self.assertEqual(self.vuupt.escritas(), [("DELETE", 102)])
        self.vuupt_client.cancelar_servico_oficial.assert_called_once_with(102)
        self.assertEqual([p["service_id"] for p in self._rascunho(rid)["paradas"]], [101, 103])


class RascunhoPorRotaTestCase(_Base):
    def test_rascunho_por_vuupt_route_id_so_enviado(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1")])
        self.assertEqual(rascunhos_rota.rascunho_por_vuupt_route_id(ROTA_ID)["id"], rid)
        self.assertIsNone(rascunhos_rota.rascunho_por_vuupt_route_id(12345))
        conn = rascunhos_rota._conectar()
        try:
            conn.execute("UPDATE rascunhos_rota SET status = 'DESCARTADO' WHERE id = ?", (rid,))
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(rascunhos_rota.rascunho_por_vuupt_route_id(ROTA_ID))


class StatusVuuptCardTestCase(_Base):
    def _espelho(self, status_rota, paradas):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("CREATE TABLE nucleo_rotas (id INTEGER PRIMARY KEY, vuupt_route_id INTEGER, "
                         "status_provedor TEXT)")
            conn.execute("CREATE TABLE nucleo_paradas (id INTEGER PRIMARY KEY, rota_id INTEGER, "
                         "service_id INTEGER, status_provedor TEXT)")
            conn.execute("INSERT INTO nucleo_rotas (id, vuupt_route_id, status_provedor) VALUES (1, ?, ?)",
                         (ROTA_ID, status_rota))
            for sid, st in paradas:
                conn.execute("INSERT INTO nucleo_paradas (rota_id, service_id, status_provedor) VALUES (1, ?, ?)",
                             (sid, st))
            conn.commit()
        finally:
            conn.close()

    def test_card_enviado_ganha_status_vuupt_e_rota_iniciada(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1"), _parada(102, "#PS-2"), _parada(103, "#PS-3")])
        self._espelho("assigned", [(101, "done"), (102, "assigned")])
        r = self._rascunho(rid)
        planejamento_rotas._anotar_rota_enviada(r)
        self.assertEqual([p["status_vuupt"] for p in r["paradas"]], ["done", "assigned", None])
        self.assertTrue(r["rota_iniciada"])

    def test_card_enviado_sem_espelho_nao_quebra(self):
        rid = self._inserir_rascunho([_parada(101, "#PS-1")])
        r = self._rascunho(rid)
        planejamento_rotas._anotar_rota_enviada(r)
        self.assertEqual(r["paradas"][0]["status_vuupt"], None)
        self.assertFalse(r["rota_iniciada"])


if __name__ == "__main__":
    unittest.main()
