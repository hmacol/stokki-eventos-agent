# -*- coding: utf-8 -*-
"""
test_espelho_fiel.py

Um teste por defeito medido em produção em 12/09 (Etapa 2 do
DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md): status que voltava pra ABERTO, parada
que saía da rota e continuava viva, código com e sem '#', horário da VUUPT
gravado em UTC, rota de teste mexendo em pedido real, motorista retirado
da rota. Mais a migração do que já estava gravado e o comparador.

Sem rede e sem tocar no dados.db real. Rodar (raiz do repo):
    python -m unittest nucleo.test_espelho_fiel -v
"""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import (banco, comparar_vuupt, consulta, migrar_espelho_15_09 as migracao, normalizacao,
                    operacao, pedidos, rotas, sincronizar_vuupt)


def _rota_vuupt(route_id=900, status="finished", servicos=None, **extra):
    """Formato bruto de GET /routes?include=services -- horários em UTC sem
    fuso, como a VUUPT manda de verdade."""
    return {"id": route_id, "name": f"Planejamento - #{route_id}", "status": status,
            "agent_id": 1234, "vehicle_id": 55, "start_at": "2026-09-14 09:00:00",
            "services": {"data": servicos if servicos is not None else [_servico()]}, **extra}


def _servico(service_id=9001, code="#PS-1", status="done", status_done="success", **extra):
    return {"id": service_id, "code": code, "title": f"{code} - 123 / BRAZO / CLIENTE", "status": status,
            "status_done": status_done, "address": "Rua A, 1", "latitude": -23.5, "longitude": -46.6,
            "sender_id": 11, "dimension_3": 3, "started_at": "2026-09-14 11:00:00",
            "arrived_at": "2026-09-14 12:30:00", "completed_at": "2026-09-14 13:00:00", **extra}


class _BaseTemp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "teste.db"
        self._patch = mock.patch.object(banco, "DB_PATH", self.db)
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()


class TestUpsertPedido(_BaseTemp):
    def test_status_none_mantem_o_que_estava(self):
        pedidos.upsert_pedido(self.conn, "PS-1", {}, origem="VUUPT_SYNC", status=banco.PEDIDO_ENTREGUE)
        pedidos.upsert_pedido(self.conn, "PS-1", {"titulo": "x"}, origem="VUUPT_SYNC", status=None)
        self.assertEqual(pedidos.buscar_pedido("PS-1", conn=self.conn)["status"], "ENTREGUE")

    def test_insert_sem_status_nasce_aberto(self):
        pedidos.upsert_pedido(self.conn, "PS-2", {}, origem="PIPELINE")
        self.assertEqual(pedidos.buscar_pedido("PS-2", conn=self.conn)["status"], "ABERTO")

    def test_reimportacao_do_pipeline_nao_reabre_pedido_entregue(self):
        """O caso real: o pipeline roda 18h e 22h e reimporta pedido que já
        está entregue; antes de 15/09 isso devolvia o pedido pra ABERTO."""
        pedidos.upsert_pedido(self.conn, "PS-3", {}, origem="VUUPT_SYNC", status=banco.PEDIDO_ENTREGUE)
        self.conn.commit()
        pedidos.registrar_importacao({"code": "#PS-3", "title": "t"}, None, "pulado_atribuido", conn=self.conn)
        self.assertEqual(pedidos.buscar_pedido("PS-3", conn=self.conn)["status"], "ENTREGUE")

    def test_codigo_normalizado_e_uma_linha_so(self):
        pedidos.upsert_pedido(self.conn, "#PS-4", {"titulo": "com cerquilha"}, origem="VUUPT_SYNC")
        pedidos.upsert_pedido(self.conn, " ps-4 ", {"endereco": "Rua B"}, origem="PIPELINE")
        linhas = self.conn.execute("SELECT codigo, titulo, endereco FROM nucleo_pedidos").fetchall()
        self.assertEqual([tuple(l) for l in linhas], [("PS-4", "com cerquilha", "Rua B")])
        self.assertIsNotNone(pedidos.buscar_pedido("#ps-4", conn=self.conn))

    def test_dados_json_e_mesclado_e_origem_e_de_quem_criou(self):
        pedidos.registrar_importacao({"code": "PS-5", "title": "t"}, {"service": {"id": 77}}, "criado", conn=self.conn)
        pedidos.upsert_pedido(self.conn, "PS-5", {}, origem="VUUPT_SYNC", dados_json={"service": {"status": "done"}})
        p = pedidos.buscar_pedido("PS-5", conn=self.conn)
        self.assertEqual(p["origem"], "PIPELINE")                      # quem criou
        dados = json.loads(p["dados_json"])
        self.assertEqual(dados["payload"]["code"], "PS-5")             # payload do pipeline não foi apagado
        self.assertEqual(dados["service"], {"id": 77, "status": "done"})   # e o serviço foi mesclado

    def test_tipo_pickup_nao_vira_delivery(self):
        pedidos.upsert_pedido(self.conn, "PS-6", {"tipo": "pickup"}, origem="VUUPT_SYNC")
        pedidos.upsert_pedido(self.conn, "PS-6", {"titulo": "x"}, origem="VUUPT_SYNC")
        self.assertEqual(pedidos.buscar_pedido("PS-6", conn=self.conn)["tipo"], "pickup")


class TestSincronizarCorrigido(_BaseTemp):
    def test_horarios_viram_hora_local_e_data_rota_sai_do_local(self):
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt()], self.conn)
        rota = self.conn.execute("SELECT * FROM nucleo_rotas").fetchone()
        parada = self.conn.execute("SELECT * FROM nucleo_paradas").fetchone()
        self.assertEqual(rota["start_at"], "2026-09-14 06:00:00")      # 09:00 UTC
        self.assertEqual(rota["data_rota"], "2026-09-14")
        self.assertEqual(parada["completed_at"], "2026-09-14 10:00:00")  # 13:00 UTC
        self.assertEqual(parada["arrived_at"], "2026-09-14 09:30:00")
        self.assertEqual(parada["codigo"], "PS-1")                     # sem '#'
        self.assertEqual(parada["tempo_no_local_s"], 1800)             # diferença não muda

    def test_rota_da_noite_fica_no_dia_certo(self):
        # 22:00 de SP = 01:00 UTC do dia seguinte: pela data UTC a rota caía no dia errado.
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt(start_at="2026-09-15 01:00:00")], self.conn)
        self.assertEqual(self.conn.execute("SELECT data_rota FROM nucleo_rotas").fetchone()[0], "2026-09-14")

    def test_parada_que_sai_da_rota_e_cancelada_uma_vez(self):
        dois = [_servico(9001, "#PS-1"), _servico(9002, "#PS-2", status="on_route", status_done=None)]
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt(servicos=dois)], self.conn)
        stats = sincronizar_vuupt.sincronizar_rotas([_rota_vuupt(servicos=[dois[0]])], self.conn)
        self.assertEqual(stats["paradas_removidas"], 1)
        p = self.conn.execute("SELECT situacao, status_provedor FROM nucleo_paradas WHERE service_id = 9002").fetchone()
        self.assertEqual((p["situacao"], p["status_provedor"]), ("CANCELADA", "fora_da_rota"))
        eventos = self.conn.execute("SELECT COUNT(*) FROM nucleo_eventos WHERE tipo = 'PARADA_REMOVIDA'").fetchone()[0]
        self.assertEqual(eventos, 1)
        # de novo: não repete o evento
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt(servicos=[dois[0]])], self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nucleo_eventos WHERE tipo = 'PARADA_REMOVIDA'"
                                           ).fetchone()[0], 1)

    def test_motorista_retirado_na_vuupt_sai_daqui(self):
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt()], self.conn)
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt(agent_id=None, status="not_started")], self.conn)
        rota = self.conn.execute("SELECT agent_id, motorista_nome FROM nucleo_rotas").fetchone()
        self.assertIsNone(rota["agent_id"])
        self.assertIsNone(rota["motorista_nome"])

    def test_rota_cancelada_nao_reabre_pedido_de_outra_rota(self):
        entregue = _servico(9001, "#PS-1")
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt(route_id=1, servicos=[entregue])], self.conn)
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt(route_id=2, status="canceled", servicos=[entregue],
                                                         canceled_at="2026-09-14 20:00:00")], self.conn)
        self.assertEqual(pedidos.buscar_pedido("PS-1", conn=self.conn)["status"], "ENTREGUE")


class TestRotaDeTeste(_BaseTemp):
    def _montar_replica(self):
        self.conn.execute("""INSERT INTO nucleo_rotas (id, data_rota, nome, provedor, agent_id, status, dados_json)
                             VALUES (1, '2026-09-15', '[TESTE] cópia', 'APP', 999001, 'PLANEJADA',
                                     '{"replica_de": {"rota_id": 42}}')""")
        self.conn.execute("""INSERT INTO nucleo_paradas (id, rota_id, ordem, codigo, situacao)
                             VALUES (1, 1, 1, 'PS-9', 'PENDENTE')""")
        pedidos.upsert_pedido(self.conn, "PS-9", {}, origem="VUUPT_SYNC", status=banco.PEDIDO_ENTREGUE)
        self.conn.commit()

    def test_entrega_na_replica_nao_mexe_no_pedido_real(self):
        self._montar_replica()
        operacao.registrar_evento_parada(self.conn, 1, 999001,
                                         {"uuid": "u1", "tipo": "ENTREGUE", "ocorrido_em": "2026-09-15 10:00:00"})
        self.assertEqual(self.conn.execute("SELECT situacao FROM nucleo_paradas WHERE id = 1").fetchone()[0],
                         "ENTREGUE")
        self.assertEqual(pedidos.buscar_pedido("PS-9", conn=self.conn)["status"], "ENTREGUE")  # não virou APP/outro
        evento = self.conn.execute("SELECT origem FROM nucleo_eventos WHERE tipo = 'ENTREGUE'").fetchone()
        self.assertEqual(evento["origem"], "APP")

    def test_rota_normal_do_app_atualiza_o_pedido(self):
        self.conn.execute("""INSERT INTO nucleo_rotas (id, data_rota, nome, provedor, agent_id, status)
                             VALUES (2, '2026-09-15', 'Rota real', 'APP', 777, 'PLANEJADA')""")
        self.conn.execute("""INSERT INTO nucleo_paradas (id, rota_id, ordem, codigo, situacao)
                             VALUES (2, 2, 1, 'PS-10', 'PENDENTE')""")
        pedidos.upsert_pedido(self.conn, "PS-10", {}, origem="PIPELINE")
        self.conn.commit()
        operacao.registrar_evento_parada(self.conn, 2, 777, {"uuid": "u2", "tipo": "ENTREGUE",
                                                             "ocorrido_em": "2026-09-15 10:00:00"})
        self.assertEqual(pedidos.buscar_pedido("PS-10", conn=self.conn)["status"], "ENTREGUE")


class TestConsultaCodigo(unittest.TestCase):
    def test_classificar_aceita_cerquilha_espaco_e_reentrega(self):
        for termo, esperado in (("PS-38552", "PS-38552"), ("#PS-38552", "PS-38552"), ("ps 38552", "PS-38552"),
                                ("#PS-38552-R1", "PS-38552-R1")):
            tipo, limpo = consulta.classificar_termo(termo)
            self.assertEqual((tipo, limpo), (consulta.TERMO_PEDIDO, esperado), termo)

    def test_placa_e_texto_continuam_iguais(self):
        self.assertEqual(consulta.classificar_termo("ABC1D23")[0], consulta.TERMO_PLACA)
        self.assertEqual(consulta.classificar_termo("Casa Santa Luzia")[0], consulta.TERMO_TEXTO)
        self.assertEqual(consulta.classificar_termo("609")[0], consulta.TERMO_ROTA)


class TestConsultaBusca(_BaseTemp):
    def test_acha_pedido_gravado_com_cerquilha(self):
        pedidos.upsert_pedido(self.conn, "PS-38552", {}, origem="VUUPT_SYNC")
        self.conn.execute("UPDATE nucleo_pedidos SET codigo = '#PS-38552'")      # estado pré-migração
        self.conn.commit()
        achado = consulta.buscar("PS-38552", conn=self.conn)
        self.assertEqual([p["codigo"] for p in achado["pedidos"]], ["#PS-38552"])
        self.assertIsNotNone(consulta.detalhar_pedido("#ps-38552", conn=self.conn))


class TestMigracao(_BaseTemp):
    def _estado_antigo(self):
        """Banco como estava antes de 15/09: código com '#', horário em UTC,
        pedido entregue marcado ABERTO."""
        self.conn.execute("""INSERT INTO nucleo_rotas (id, data_rota, nome, provedor, vuupt_route_id, agent_id,
                                                       status, status_provedor, start_at, concluida_em, dados_json)
                             VALUES (1, '2026-09-14', 'R', 'VUUPT', 5, 1234, 'CONCLUIDA', 'finished',
                                     '2026-09-14 09:00:00', '2026-09-14 20:00:00',
                                     '{"finished_at": "2026-09-14 20:00:00"}')""")
        self.conn.execute("""INSERT INTO nucleo_paradas (id, rota_id, ordem, service_id, codigo, situacao,
                                                         arrived_at, completed_at)
                             VALUES (1, 1, 1, 9001, '#PS-1', 'ENTREGUE', '2026-09-14 12:30:00', '2026-09-14 13:00:00')""")
        self.conn.execute("""INSERT INTO nucleo_eventos (rota_id, parada_id, tipo, origem, ocorrido_em, recebido_em)
                             VALUES (1, 1, 'ENTREGUE', 'VUUPT_SYNC', '2026-09-14 13:00:00', '2026-09-14 10:15:00')""")
        self.conn.execute("""INSERT INTO nucleo_eventos (rota_id, tipo, origem, ocorrido_em, recebido_em)
                             VALUES (1, 'ROTA_IMPORTADA_VUUPT', 'VUUPT_SYNC', '2026-09-14 10:15:02', '2026-09-14 10:15:02')""")
        # o mesmo pedido gravado duas vezes, e com status errado
        self.conn.execute("""INSERT INTO nucleo_pedidos (codigo, status, origem, titulo, criado_em, atualizado_em, dados_json)
                             VALUES ('PS-1', 'ABERTO', 'PIPELINE', 'antigo', '2026-09-01', '2026-09-01',
                                     '{"payload": {"x": 1}}')""")
        self.conn.execute("""INSERT INTO nucleo_pedidos (codigo, status, origem, titulo, agendamento_inicio,
                                                         criado_em, atualizado_em, dados_json)
                             VALUES ('#PS-1', 'ABERTO', 'VUUPT_SYNC', 'novo', '2026-09-14 11:00:00',
                                     '2026-09-14', '2026-09-14',
                                     '{"service": {"scheduled_start": "2026-09-14 11:00:00"}}')""")
        self.conn.commit()

    def test_migracao_completa_e_idempotente(self):
        self._estado_antigo()
        resumo = migracao.migrar(self.conn)
        self.assertEqual(resumo["codigo_paradas"]["paradas_normalizadas"], 1)
        self.assertEqual(resumo["codigo_pedidos"]["pares_mesclados"], 1)

        pedido = self.conn.execute("SELECT * FROM nucleo_pedidos").fetchall()
        self.assertEqual(len(pedido), 1)
        pedido = pedido[0]
        self.assertEqual(pedido["codigo"], "PS-1")
        self.assertEqual(pedido["titulo"], "novo")                 # o mais recente manda
        self.assertEqual(pedido["origem"], "PIPELINE")             # origem de quem criou
        self.assertIn('"payload"', pedido["dados_json"])           # o payload antigo sobreviveu
        self.assertEqual(pedido["status"], "ENTREGUE")             # corrigido pelo resultado da rota
        self.assertEqual(pedido["agendamento_inicio"], "2026-09-14 08:00:00")

        rota = self.conn.execute("SELECT * FROM nucleo_rotas").fetchone()
        self.assertEqual(rota["start_at"], "2026-09-14 06:00:00")
        self.assertEqual(rota["concluida_em"], "2026-09-14 17:00:00")
        parada = self.conn.execute("SELECT * FROM nucleo_paradas").fetchone()
        self.assertEqual((parada["codigo"], parada["completed_at"]), ("PS-1", "2026-09-14 10:00:00"))
        eventos = {e["tipo"]: e["ocorrido_em"] for e in self.conn.execute("SELECT tipo, ocorrido_em FROM nucleo_eventos")}
        self.assertEqual(eventos["ENTREGUE"], "2026-09-14 10:00:00")
        self.assertEqual(eventos["ROTA_IMPORTADA_VUUPT"], "2026-09-14 10:15:02")   # hora do sync, não converte

        # 2ª rodada não converte de novo nem mescla nada
        antes = self.conn.execute("SELECT codigo, status, agendamento_inicio FROM nucleo_pedidos").fetchall()
        resumo2 = migracao.migrar(self.conn)
        self.assertTrue(all(v == "já aplicada" for v in resumo2.values()), resumo2)
        depois = self.conn.execute("SELECT codigo, status, agendamento_inicio FROM nucleo_pedidos").fetchall()
        self.assertEqual([tuple(l) for l in antes], [tuple(l) for l in depois])
        self.assertEqual(self.conn.execute("SELECT completed_at FROM nucleo_paradas").fetchone()[0],
                         "2026-09-14 10:00:00")

    def test_modo_teste_nao_grava(self):
        self._estado_antigo()
        migracao.migrar(self.conn, modo_teste=True)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nucleo_pedidos").fetchone()[0], 2)


class TestComparador(_BaseTemp):
    def test_sem_divergencia_depois_do_sync(self):
        rota = _rota_vuupt()
        sincronizar_vuupt.sincronizar_rotas([rota], self.conn)
        self.conn.commit()
        resultado = comparar_vuupt.comparar_dia(date(2026, 9, 14), [rota], self.conn)
        self.assertEqual({c: len(v) for c, v in resultado["divergencias"].items() if v}, {})

    def test_aponta_horario_em_utc_e_parada_sobrando(self):
        rota = _rota_vuupt()
        sincronizar_vuupt.sincronizar_rotas([rota], self.conn)
        # simula o estado antigo: horário cru e uma parada que a VUUPT não tem mais
        self.conn.execute("UPDATE nucleo_paradas SET completed_at = '2026-09-14 13:00:00'")
        self.conn.execute("""INSERT INTO nucleo_paradas (rota_id, ordem, service_id, codigo, situacao)
                             VALUES (1, 2, 9999, 'PS-X', 'PENDENTE')""")
        self.conn.commit()
        d = comparar_vuupt.comparar_dia(date(2026, 9, 14), [rota], self.conn)["divergencias"]
        self.assertEqual(len(d["horario_parada_em_utc"]), 1)
        self.assertEqual(len(d["parada_sobrando"]), 1)
        self.assertEqual(len(d["rota_ausente_no_nucleo"]), 0)


class TestNormalizacao(unittest.TestCase):
    def test_codigo(self):
        self.assertEqual(normalizacao.normalizar_codigo("#ps-1"), "PS-1")
        self.assertEqual(normalizacao.normalizar_codigo("  PS-1-R2 "), "PS-1-R2")
        self.assertIsNone(normalizacao.normalizar_codigo("  #  "))
        self.assertIsNone(normalizacao.normalizar_codigo(None))

    def test_horario(self):
        self.assertEqual(normalizacao.vuupt_para_local("2026-09-14 13:00:00"), "2026-09-14 10:00:00")
        self.assertEqual(normalizacao.vuupt_para_local("2026-09-14T13:00:00.000000Z"), "2026-09-14 10:00:00")
        self.assertEqual(normalizacao.vuupt_para_local("2026-09-14T10:00:00-03:00"), "2026-09-14 10:00:00")
        self.assertEqual(normalizacao.para_local("2026-09-14 10:00:00"), "2026-09-14 10:00:00")
        self.assertEqual(normalizacao.para_local("2026-09-14T08:00:00-03:00"), "2026-09-14 08:00:00")
        self.assertIsNone(normalizacao.vuupt_para_local(""))
        self.assertEqual(normalizacao.vuupt_para_local("qualquer coisa"), "qualquer coisa")

    def test_janela_do_dia_local_em_utc(self):
        self.assertEqual(normalizacao.janela_utc_do_dia(date(2026, 9, 14)),
                         ("2026-09-14 03:00:00", "2026-09-15 03:00:00"))


if __name__ == "__main__":
    unittest.main()
