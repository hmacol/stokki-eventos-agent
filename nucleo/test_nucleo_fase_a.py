# -*- coding: utf-8 -*-
"""
test_nucleo_fase_a.py

Testes da Fase A do núcleo próprio (DOC_EXECUCAO_CLAUDE_APP_MOTORISTAS.md):
esquema, tarifa do motorista, espelho de rota enviada, sincronização a
partir do formato bruto da VUUPT e extrato financeiro. Nenhum teste
bate na VUUPT nem no dados.db real -- cada teste usa um SQLite
temporário. Rodar (a partir da raiz do repo):
    python -m unittest nucleo.test_nucleo_fase_a -v
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco, financeiro, metricas, pedidos, rotas, sincronizar_vuupt, tempos
from regras import km_cobrado, tarifa_motorista

sys.path.insert(0, str(_RAIZ / "roteirizacao"))
import km_rodoviario  # noqa: E402


class _BaseTemp(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors: no Windows o SQLite segura o handle do
        # arquivo até o GC, e o rmtree do tearDown falha à toa.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "teste.db"
        self._patch = mock.patch.object(banco, "DB_PATH", self.db)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()


class TestEsquema(_BaseTemp):
    def test_cria_tabelas_e_migra_motoristas_legada(self):
        # Tabela `motoristas` do desenho de junho já existe (sem as colunas novas)
        raw = sqlite3.connect(self.db)
        raw.execute("""CREATE TABLE motoristas (cpf TEXT PRIMARY KEY, nome TEXT NOT NULL,
                       pin_hash TEXT NOT NULL, pin_salt TEXT NOT NULL, telefone TEXT, ativo INTEGER NOT NULL DEFAULT 1)""")
        raw.commit()
        raw.close()

        conn = banco.conectar()
        tabelas = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("nucleo_pedidos", "nucleo_rotas", "nucleo_paradas", "nucleo_eventos",
                  "nucleo_comprovantes", "tarifas_motorista", "motoristas"):
            self.assertIn(t, tabelas)
        colunas = {r[1] for r in conn.execute("PRAGMA table_info(motoristas)")}
        self.assertTrue({"agent_id", "tipo_veiculo", "push_token", "tentativas_pin", "perfil"} <= colunas)
        # idempotente
        banco.garantir_esquema(conn)
        conn.close()


class TestTarifa(unittest.TestCase):
    def test_fiorino_dentro_e_fora_da_franquia(self):
        self.assertEqual(tarifa_motorista.calcular_valor_rota("FIORINO", 50).valor_total, 340.0)
        r = tarifa_motorista.calcular_valor_rota("FIORINO", 80)
        self.assertEqual(r.km_excedente, 15.0)
        self.assertEqual(r.valor_total, 355.0)

    def test_vazio_e_none_sao_fiorino(self):
        self.assertEqual(tarifa_motorista.calcular_valor_rota(None, 65).valor_total, 340.0)
        self.assertEqual(tarifa_motorista.calcular_valor_rota("", 66).valor_total, 341.0)

    def test_van_hr(self):
        self.assertEqual(tarifa_motorista.calcular_valor_rota("VAN_HR", 100).valor_total, 550.0)
        self.assertEqual(tarifa_motorista.calcular_valor_rota("VAN/HR", 120).valor_total, 570.0)
        self.assertEqual(tarifa_motorista.calcular_valor_rota("HR", 20).valor_total, 550.0)

    def test_vuc(self):
        # Hugo, 11/09: R$ 700 até 120 km + R$ 1,25/km
        self.assertEqual(tarifa_motorista.calcular_valor_rota("VUC", 120).valor_total, 700.0)
        self.assertEqual(tarifa_motorista.calcular_valor_rota("vuc", 160).valor_total, 750.0)

    def test_sem_tarifa_definida(self):
        for tipo in ("TRES_QUARTOS", "3/4", "TRUCK", "ZEPPELIN"):
            self.assertIsNone(tarifa_motorista.calcular_valor_rota(tipo, 10), tipo)

    def test_km_desconhecido_paga_so_base(self):
        r = tarifa_motorista.calcular_valor_rota("FIORINO", None)
        self.assertTrue(r.km_desconhecido)
        self.assertEqual(r.valor_total, 340.0)

    def test_tarifas_do_banco_prevalecem(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            conn = banco.conectar(Path(d) / "t.db")
            tarifa_motorista.semear_tarifas_padrao(conn)
            tarifa_motorista.semear_tarifas_padrao(conn)  # idempotente
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tarifas_motorista").fetchone()[0], 3)
            conn.execute("UPDATE tarifas_motorista SET valor_base = 400 WHERE tipo_veiculo = 'FIORINO'")
            tarifas = tarifa_motorista.carregar_tarifas(conn)
            self.assertEqual(tarifa_motorista.calcular_valor_rota("FIORINO", 10, tarifas).valor_total, 400.0)
            conn.close()


def _rascunho_fake(rascunho_id=7, agent_id=1234, km=42.5):
    return {
        "id": rascunho_id, "data_alvo": "2026-08-26", "lote_id": "L1", "nome": "Planejamento - 26/08/2026 - #3",
        "particao": "MANHA", "tipo_rota": "normal", "zona": "ZONA NORTE", "tipo_veiculo": None,
        "agent_id": agent_id, "vehicle_id": 55, "motorista_nome": "João",
        "start_location_base_id": 1, "end_location_base_id": 1, "start_at": "2026-08-26 07:00:00",
        "km_estimado": km, "status": "RASCUNHO", "vuupt_route_id": None,
        "paradas": [
            {"ordem": 1, "service_id": 9001, "codigo": "PS-1", "titulo": "Cliente A", "endereco": "Rua A, 1",
             "latitude": -23.5, "longitude": -46.6, "sender_id": 11, "remetente_nome": "BRAZO",
             "nivel_dificuldade": 1, "volume_caixas": 3, "destinatario_nome": "A",
             "horario_atendimento_inicio": "08:00", "horario_atendimento_fim": "17:00"},
            {"ordem": 2, "service_id": 9002, "codigo": "PS-2", "titulo": "Cliente B", "endereco": "Rua B, 2",
             "latitude": -23.6, "longitude": -46.7, "sender_id": 11, "remetente_nome": "BRAZO",
             "nivel_dificuldade": 3, "volume_caixas": 1, "destinatario_nome": "B"},
        ],
    }


class TestRotaEnviada(_BaseTemp):
    def test_registrar_rota_enviada_espelha_e_e_idempotente(self):
        with mock.patch("rascunhos_rota.buscar_rascunho", return_value=_rascunho_fake()):
            rota_id = rotas.registrar_rota_enviada(7, 777001)
            rota_id_2 = rotas.registrar_rota_enviada(7, 777001)
        self.assertEqual(rota_id, rota_id_2)

        rota = rotas.buscar_rota(rota_id)
        self.assertEqual(rota["provedor"], "VUUPT")
        self.assertEqual(rota["vuupt_route_id"], 777001)
        self.assertEqual(rota["data_rota"], "2026-08-26")
        self.assertEqual(rota["km_estimado"], 42.5)
        self.assertEqual(rota["km_fonte"], "ESTIMADO")
        self.assertEqual(rota["total_paradas"], 2)
        self.assertEqual([p["service_id"] for p in rota["paradas"]], [9001, 9002])
        self.assertEqual(rota["paradas"][0]["janela_inicio"], "08:00")
        self.assertEqual(rota["paradas"][0]["situacao"], "PENDENTE")

        conn = banco.conectar()
        eventos = conn.execute("SELECT tipo FROM nucleo_eventos WHERE rota_id = ?", (rota_id,)).fetchall()
        conn.close()
        self.assertEqual([e[0] for e in eventos], ["ROTA_ENVIADA"])

    def test_rascunho_inexistente_nao_quebra(self):
        with mock.patch("rascunhos_rota.buscar_rascunho", return_value=None):
            self.assertIsNone(rotas.registrar_rota_enviada(99, 1))


def _rota_vuupt(route_id=777001, status="accepted", servicos=None, agent_id=1234):
    return {
        "id": route_id, "name": "Planejamento - 26/08/2026 - #3", "status": status,
        "start_at": "2026-08-26 07:00:00", "agent_id": agent_id, "vehicle_id": 55,
        "services": {"data": servicos if servicos is not None else [
            {"id": 9001, "code": "PS-1", "title": "Cliente A", "status": "accepted", "status_done": None,
             "latitude": -23.5, "longitude": -46.6, "sender_id": 11, "dimension_3": 3},
            {"id": 9002, "code": "PS-2", "title": "Cliente B", "status": "accepted", "status_done": None,
             "latitude": -23.6, "longitude": -46.7, "sender_id": 11, "dimension_3": 1},
        ]},
    }


class TestSincronizarVuupt(_BaseTemp):
    def _conn_com_motivos(self):
        conn = banco.conectar()
        conn.execute("""CREATE TABLE IF NOT EXISTS motivos_ocorrencia (id INTEGER PRIMARY KEY, vuupt_failed_reason_id INTEGER,
                        motivo_texto TEXT)""")
        conn.execute("INSERT OR IGNORE INTO motivos_ocorrencia VALUES (1, 5431, 'Local fechado')")
        conn.commit()
        return conn

    def test_backfill_cria_rota_e_paradas_e_progressao_gera_eventos_uma_vez(self):
        conn = self._conn_com_motivos()
        stats = sincronizar_vuupt.sincronizar_rotas([_rota_vuupt()], conn, {1234: "João"})
        self.assertEqual(stats["rotas_novas"], 1)
        self.assertEqual(stats["paradas_novas"], 2)

        rota = rotas.listar_rotas_dia("2026-08-26", conn=conn)[0]
        self.assertEqual(rota["status"], "ACEITA")
        self.assertEqual(rota["motorista_nome"], "João")
        self.assertEqual(rota["total_paradas"], 2)

        # Sincronizar de novo sem mudança: nenhum evento novo
        antes = conn.execute("SELECT COUNT(*) FROM nucleo_eventos").fetchone()[0]
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt()], conn, {1234: "João"})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM nucleo_eventos").fetchone()[0], antes)

        # Motorista entrega a 1ª e falha a 2ª
        servicos = [
            {"id": 9001, "code": "PS-1", "title": "Cliente A", "status": "done", "status_done": "success",
             "completed_at": "2026-08-26 09:10:00", "latitude": -23.5, "longitude": -46.6},
            {"id": 9002, "code": "PS-2", "title": "Cliente B", "status": "done", "status_done": "failed",
             "failed_reason_id": 5431, "completed_at": "2026-08-26 10:00:00"},
        ]
        stats = sincronizar_vuupt.sincronizar_rotas([_rota_vuupt(status="done", servicos=servicos)], conn)
        rota = rotas.buscar_rota(rota["id"], conn=conn)
        self.assertEqual(rota["status"], "CONCLUIDA")
        self.assertEqual(rota["entregues"], 1)
        self.assertEqual(rota["insucessos"], 1)
        self.assertEqual(rota["concluida_em"], "2026-08-26 10:00:00")
        p2 = rota["paradas"][1]
        self.assertEqual(p2["situacao"], "INSUCESSO")
        self.assertEqual(p2["motivo_texto"], "Local fechado")
        self.assertEqual(p2["motivo_id"], 1)

        tipos = [r[0] for r in conn.execute("SELECT tipo FROM nucleo_eventos WHERE rota_id = ? ORDER BY id", (rota["id"],))]
        self.assertEqual(tipos, ["ROTA_IMPORTADA_VUUPT", "ROTA_ACEITA", "ENTREGUE", "INSUCESSO", "ROTA_CONCLUIDA"])

        pedido = pedidos.buscar_pedido("PS-2", conn=conn)
        self.assertEqual(pedido["status"], "INSUCESSO")
        self.assertEqual(pedido["vuupt_service_id"], 9002)
        conn.close()

    def test_rota_ja_espelhada_pelo_painel_e_reaproveitada_com_km(self):
        with mock.patch("rascunhos_rota.buscar_rascunho", return_value=_rascunho_fake()):
            rota_id = rotas.registrar_rota_enviada(7, 777001)
        conn = banco.conectar()
        stats = sincronizar_vuupt.sincronizar_rotas([_rota_vuupt()], conn)
        self.assertEqual(stats["rotas_novas"], 0)
        self.assertEqual(stats["paradas_novas"], 0)   # mesmas service_ids -> atualizadas, não duplicadas
        rota = rotas.buscar_rota(rota_id, conn=conn)
        self.assertEqual(rota["km_estimado"], 42.5)
        self.assertEqual(rota["status"], "ACEITA")
        conn.close()

    def test_rota_do_app_nao_e_tocada_pela_vuupt(self):
        conn = banco.conectar()
        rota_id = rotas.materializar_rascunho(_rascunho_fake(), banco.PROVEDOR_APP, vuupt_route_id=777001, conn=conn)
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt(status="canceled")], conn)
        self.assertEqual(rotas.buscar_rota(rota_id, conn=conn)["status"], "PLANEJADA")
        conn.close()

    def test_rota_cancelada_nao_conta_entregas_feitas_em_outra_rota(self):
        # Achado real (26/08): rota cancelada lista os serviços com status
        # GLOBAL (já entregues noutra rota). Aqui: parada CANCELADA nesta
        # rota, contadores zerados, nenhum evento ENTREGUE, pedido intacto.
        conn = banco.conectar()
        entregues = [
            {"id": 9001, "code": "PS-1", "title": "Cliente A", "status": "done", "status_done": "success",
             "completed_at": "2026-08-26 09:10:00"},
        ]
        rota_cancelada = {**_rota_vuupt(route_id=1, status="canceled", servicos=entregues),
                          "canceled_at": "2026-08-25 23:15:17"}
        rota_ativa = _rota_vuupt(route_id=2, status="finished", servicos=entregues)
        rota_ativa["finished_at"] = "2026-08-26 11:00:00"
        sincronizar_vuupt.sincronizar_rotas([rota_cancelada, rota_ativa], conn)

        por_id = {r["vuupt_route_id"]: r for r in rotas.listar_rotas_dia("2026-08-26", conn=conn)}
        cancelada, ativa = por_id[1], por_id[2]
        self.assertEqual(cancelada["status"], "CANCELADA")
        self.assertEqual(cancelada["cancelada_em"], "2026-08-25 23:15:17")
        self.assertEqual((cancelada["entregues"], cancelada["insucessos"]), (0, 0))
        self.assertEqual(cancelada["paradas"][0]["situacao"], "CANCELADA")
        self.assertEqual(ativa["status"], "CONCLUIDA")
        self.assertEqual(ativa["concluida_em"], "2026-08-26 11:00:00")
        self.assertEqual(ativa["entregues"], 1)

        n_entregue = conn.execute("SELECT COUNT(*) FROM nucleo_eventos WHERE tipo = 'ENTREGUE'").fetchone()[0]
        self.assertEqual(n_entregue, 1)   # só na rota ativa
        self.assertEqual(pedidos.buscar_pedido("PS-1", conn=conn)["status"], "ENTREGUE")
        conn.close()


class TestPedidos(_BaseTemp):
    def test_registrar_importacao_e_coalesce(self):
        payload = {"title": "Cliente A", "code": "PS-10", "type": "delivery",
                   "customer": {"name": "A", "code": "12.345.678/0001-90", "address": "Rua A, 1",
                                "operating_hour_start": "08:00", "operating_hour_end": "17:00",
                                "phone_number": "+5511999990000", "latitude": -23.5, "longitude": -46.6},
                   "sender": {"name": "BRAZO", "code": "29920346000190"}, "sender_id": 11249526, "dimension_3": 4,
                   "scheduled_start": "2026-08-27 08:00:00"}
        pedidos.registrar_importacao(payload, {"service": {"id": 5001}}, "criado")
        p = pedidos.buscar_pedido("PS-10")
        self.assertEqual(p["vuupt_service_id"], 5001)
        self.assertEqual(p["destinatario_telefone"], "+5511999990000")
        self.assertEqual(p["caixas"], 4)
        self.assertEqual(p["status"], "ABERTO")

        # Atualização parcial (sync) não apaga telefone nem service_id
        conn = banco.conectar()
        pedidos.upsert_pedido(conn, "PS-10", {"titulo": "Cliente A (novo)"}, origem="VUUPT_SYNC", status="ENTREGUE")
        conn.commit()
        p = pedidos.buscar_pedido("PS-10", conn=conn)
        conn.close()
        self.assertEqual(p["titulo"], "Cliente A (novo)")
        self.assertEqual(p["destinatario_telefone"], "+5511999990000")
        self.assertEqual(p["vuupt_service_id"], 5001)
        self.assertEqual(p["status"], "ENTREGUE")

    def test_payload_sem_code_e_ignorado(self):
        pedidos.registrar_importacao({"title": "x"}, None, "pulado_atribuido")
        conn = banco.conectar()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM nucleo_pedidos").fetchone()[0], 0)
        conn.close()


class TestTempos(_BaseTemp):
    def test_parse_e_diferenca(self):
        self.assertEqual(tempos.diferenca_s("2026-08-26 08:10:00", "2026-08-26 08:20:00"), 600)
        self.assertEqual(tempos.diferenca_s("2026-08-26T08:10:00.000000Z", "2026-08-26 08:15:30"), 330)
        self.assertEqual(tempos.diferenca_s("2026-08-26T08:10:00-03:00", "2026-08-26T08:11:00-03:00"), 60)
        self.assertIsNone(tempos.diferenca_s("2026-08-26 08:20:00", "2026-08-26 08:10:00"))   # fora de ordem
        self.assertIsNone(tempos.diferenca_s(None, "2026-08-26 08:10:00"))
        self.assertIsNone(tempos.parse_ts("ontem"))

    def test_sync_vuupt_grava_duracoes_e_metricas_filtram_lote(self):
        conn = banco.conectar()
        conn.execute("CREATE TABLE motivos_ocorrencia (id INTEGER PRIMARY KEY, vuupt_failed_reason_id INTEGER, motivo_texto TEXT)")
        servicos = [
            # 20 min no local (plausível) -- título no formato real da VUUPT
            {"id": 1, "code": "PS-1", "title": "#PS-1 - 036076 / DE TOMMASO / HORTIFRUTI DCE PRECO", "customer_id": 500,
             "status": "done", "status_done": "success", "started_at": "2026-08-26 07:00:00",
             "arrived_at": "2026-08-26 07:30:00", "completed_at": "2026-08-26 07:50:00"},
            # 10 s no local = confirmação em lote -> gravado, mas fora da média
            {"id": 2, "code": "PS-2", "status": "done", "status_done": "success", "arrived_at": "2026-08-26 08:00:00",
             "completed_at": "2026-08-26 08:00:10"},
            # 10 min, insucesso (conta também) -- mesmo destinatário (customer_id) da PS-1, nome escrito diferente
            {"id": 3, "code": "PS-3", "title": "#PS-3 - 036099 / DE TOMMASO / HORTIFRUTI DCE PREÇO LTDA", "customer_id": 500,
             "status": "done", "status_done": "failed", "failed_reason_id": 99,
             "arrived_at": "2026-08-26 09:00:00", "completed_at": "2026-08-26 09:10:00"},
            # sem arrived_at -> NULL
            {"id": 4, "code": "PS-4", "status": "done", "status_done": "success", "completed_at": "2026-08-26 10:00:00"},
        ]
        rota = {**_rota_vuupt(status="finished", servicos=servicos), "finished_at": "2026-08-26 10:00:00"}
        sincronizar_vuupt.sincronizar_rotas([rota], conn, {1234: "João"})
        valores = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT codigo, tempo_deslocamento_s, tempo_no_local_s FROM nucleo_paradas")}
        self.assertEqual(valores["PS-1"], (1800, 1200))
        self.assertEqual(valores["PS-2"], (None, 10))
        self.assertEqual(valores["PS-3"], (None, 600))
        self.assertEqual(valores["PS-4"], (None, None))

        de = ate = __import__("datetime").date(2026, 8, 26)
        geral = metricas.tempo_por_grupo(conn, de, ate, "geral")
        self.assertEqual(geral[0]["n"], 2)                      # PS-1 e PS-3; PS-2 (10s) e PS-4 (NULL) fora
        self.assertEqual(geral[0]["media_min"], 15.0)
        self.assertEqual(geral[0]["mediana_min"], 15.0)
        por_motorista = metricas.tempo_por_grupo(conn, de, ate, "motorista")
        self.assertEqual(por_motorista[0]["grupo"], "João")
        # Destinatário parseado do título e agrupado pelo customer_id (PS-1 e PS-3 juntas)
        p1 = conn.execute("SELECT destinatario_nome, remetente_nome, customer_id FROM nucleo_paradas WHERE codigo = 'PS-1'").fetchone()
        self.assertEqual(tuple(p1), ("HORTIFRUTI DCE PRECO", "DE TOMMASO", 500))
        por_dest = metricas.tempo_por_grupo(conn, de, ate, "destinatario")
        self.assertEqual(len(por_dest), 1)
        self.assertEqual((por_dest[0]["grupo"], por_dest[0]["chave"], por_dest[0]["n"]), ("HORTIFRUTI DCE PRECO", 500, 2))
        self.assertEqual(sincronizar_vuupt.partes_do_titulo("Cliente A"), (None, "Cliente A"))
        cob = metricas.cobertura(conn, de, ate)
        self.assertEqual((cob["concluidas"], cob["com_duracao"], cob["plausiveis"], cob["abaixo_30s"]), (4, 3, 2, 1))

        # Backfill: zera e recalcula
        conn.execute("UPDATE nucleo_paradas SET tempo_no_local_s = NULL, tempo_deslocamento_s = NULL")
        conn.commit()
        self.assertEqual(tempos.recalcular_todas(conn), 3)
        conn.close()


class TestKmCobrado(unittest.TestCase):
    """regras/km_cobrado.py -- decisão do Hugo (11/09): a volta ao CD só
    conta com insucesso/parcial ou parada fora da Grande SP."""
    PARADAS_SP = [{"situacao": "ENTREGUE", "latitude": -23.5, "longitude": -46.6},
                  {"situacao": "ENTREGUE", "latitude": -23.6, "longitude": -46.7}]

    def test_estimado_sem_volta_desconta_trecho_de_volta(self):
        r = km_cobrado.calcular_km_cobrado({"km_estimado": 80.0, "km_volta_estimado": 10.0}, self.PARADAS_SP)
        self.assertEqual((r.km, r.fonte, r.provisorio, r.volta_conta, r.km_volta), (70.0, "ESTIMADO", True, False, 10.0))

    def test_insucesso_e_parcial_contam_a_volta(self):
        paradas = [{**self.PARADAS_SP[0], "situacao": "INSUCESSO"}, self.PARADAS_SP[1]]
        r = km_cobrado.calcular_km_cobrado({"km_estimado": 80.0, "km_volta_estimado": 10.0}, paradas)
        self.assertEqual((r.km, r.volta_conta, r.motivo_volta), (80.0, True, "INSUCESSO"))
        paradas = [{**self.PARADAS_SP[0], "situacao": "PARCIAL"}, self.PARADAS_SP[1]]
        self.assertEqual(km_cobrado.calcular_km_cobrado({"km_estimado": 80.0, "km_volta_estimado": 10.0}, paradas).motivo_volta, "PARCIAL")

    def test_parada_fora_da_grande_sp_conta_a_volta(self):
        paradas = [self.PARADAS_SP[0], {"situacao": "ENTREGUE", "latitude": -23.02, "longitude": -47.05}]   # ~50 km (Campinas)
        r = km_cobrado.calcular_km_cobrado({"km_estimado": 150.0, "km_volta_estimado": 60.0}, paradas)
        self.assertEqual((r.km, r.motivo_volta), (150.0, "FORA_GRANDE_SP"))

    def test_gps_para_na_ultima_parada_e_soma_volta_estimada_quando_conta(self):
        rota = {"km_real": 40.0, "km_fonte": "GPS_APP", "km_estimado": 55.0, "km_volta_estimado": 10.0}
        r = km_cobrado.calcular_km_cobrado(rota, self.PARADAS_SP)
        self.assertEqual((r.km, r.fonte, r.provisorio, r.volta_conta), (40.0, "GPS_APP", False, False))
        paradas = [{**self.PARADAS_SP[0], "situacao": "INSUCESSO"}, self.PARADAS_SP[1]]
        r = km_cobrado.calcular_km_cobrado(rota, paradas)
        self.assertEqual((r.km, r.volta_estimada), (50.0, True))

    def test_sem_km_volta_gravado_usa_linha_reta_ate_o_cd(self):
        r = km_cobrado.calcular_km_cobrado({"km_estimado": 80.0}, self.PARADAS_SP)
        self.assertIsNotNone(r.km_volta)
        self.assertLess(r.km, 80.0)
        # última parada (-23.6, -46.7) -> CD (-23.497, -46.66): ~12 km
        self.assertAlmostEqual(r.km_volta, 12.2, delta=0.5)

    def test_km_desconhecido(self):
        r = km_cobrado.calcular_km_cobrado({}, self.PARADAS_SP)
        self.assertIsNone(r.km)


class TestKmRodoviario(unittest.TestCase):
    """roteirizacao/km_rodoviario.py com a Routes API simulada."""
    def test_google_separa_volta_e_encadeia_acima_de_25_paradas(self):
        chamadas = []

        def fake_post(corpo, api_key, timeout):
            n = len(corpo["intermediates"])
            chamadas.append(n)
            return {"routes": [{"distanceMeters": 1000 * (n + 1), "legs": [{"distanceMeters": 1000}] * (n + 1)}]}

        with mock.patch.object(km_rodoviario, "_post", side_effect=fake_post):
            r = km_rodoviario.calcular_km((-23.5, -46.66), [(-23.5, -46.6), (-23.55, -46.65)], "chave")
            self.assertEqual((r.total_km, r.ida_km, r.volta_km, r.fonte), (3.0, 2.0, 1.0, "GOOGLE_ROUTES"))
            self.assertEqual(chamadas, [2])
            chamadas.clear()
            coords = [(-23.5 + i * 0.001, -46.6) for i in range(30)]
            r = km_rodoviario.calcular_km((-23.5, -46.66), coords, "chave")
            self.assertEqual(chamadas, [25, 4])       # 32 pontos = 31 pernas: 26 na 1ª chamada + 5 na 2ª
            self.assertEqual(r.total_km, 31.0)
            self.assertEqual(r.volta_km, 1.0)

    def test_sem_chave_ou_api_fora_cai_na_linha_reta(self):
        r = km_rodoviario.calcular_km((-23.5, -46.66), [(-23.5, -46.6)], None)
        self.assertEqual(r.fonte, "HAVERSINE")
        self.assertAlmostEqual(r.ida_km, r.volta_km)
        with mock.patch.object(km_rodoviario, "_post", side_effect=RuntimeError("403")):
            r = km_rodoviario.calcular_km((-23.5, -46.66), [(-23.5, -46.6)], "chave")
        self.assertEqual(r.fonte, "HAVERSINE")
        self.assertIsNone(km_rodoviario.calcular_km(None, [(-23.5, -46.6)], "chave"))


class TestFinanceiro(_BaseTemp):
    def test_extrato_soma_por_dia_e_ignora_cancelada(self):
        conn = banco.conectar()
        # r1: 80 km estimados, volta de 10 km NÃO conta (paradas pendentes, dentro da Grande SP) -> 70 km -> 340 + 5
        r1 = rotas.materializar_rascunho({**_rascunho_fake(1, km=80.0), "km_volta_estimado": 10.0, "km_fonte_estimativa": "GOOGLE_ROUTES"}, "VUUPT", 1, conn=conn)
        r2 = rotas.materializar_rascunho({**_rascunho_fake(2, km=30.0), "data_alvo": "2026-08-27"}, "VUUPT", 2, conn=conn)
        r3 = rotas.materializar_rascunho(_rascunho_fake(3, km=999.0), "VUUPT", 3, conn=conn)
        rotas.marcar_cancelada_por_vuupt_route_id(3, conn=conn)
        conn.execute("UPDATE nucleo_rotas SET km_real = 70.0, km_fonte = 'GPS_APP' WHERE id = ?", (r2,))   # 340 + 5
        conn.commit()
        self.assertEqual(rotas.buscar_rota(r1, conn=conn)["km_fonte_estimativa"], "GOOGLE_ROUTES")

        extrato = financeiro.extrato_motorista(1234, "2026-08-26", "2026-08-31", "FIORINO", conn=conn)
        self.assertEqual(len(extrato["linhas"]), 2)
        self.assertEqual(extrato["total"], 345.0 + 345.0)
        self.assertEqual(extrato["linhas"][0]["km_detalhe"]["km_volta"], 10.0)
        por_dia = {d["data"]: d for d in extrato["por_dia"]}
        self.assertEqual(por_dia["2026-08-26"]["valor"], 345.0)
        self.assertTrue(por_dia["2026-08-26"]["provisorio"])      # km estimado
        self.assertFalse(por_dia["2026-08-27"]["provisorio"])     # km real do app
        self.assertEqual(por_dia["2026-08-27"]["valor"], 345.0)

        # Insucesso na r1: a volta passa a contar (80 km -> 340 + 15); rota paga integral
        conn.execute("UPDATE nucleo_paradas SET situacao = 'INSUCESSO' WHERE rota_id = ? AND ordem = 2", (r1,))
        conn.commit()
        extrato = financeiro.extrato_motorista(1234, "2026-08-26", "2026-08-31", "FIORINO", conn=conn)
        self.assertEqual(extrato["linhas"][0]["valor"], 355.0)
        self.assertEqual(extrato["linhas"][0]["km_detalhe"]["motivo_volta"], "INSUCESSO")

        # Pedágio: só o aprovado entra no total; pendente fica à parte
        conn.execute("INSERT INTO nucleo_pedagios (uuid, rota_id, agent_id, valor_informado, status) VALUES ('a', ?, 1234, 20.0, 'PENDENTE')", (r1,))
        conn.execute("INSERT INTO nucleo_pedagios (uuid, rota_id, agent_id, valor_informado, status, valor_aprovado) VALUES ('b', ?, 1234, 8.0, 'APROVADO', 7.5)", (r2,))
        conn.execute("INSERT INTO nucleo_pedagios (uuid, rota_id, agent_id, valor_informado, status) VALUES ('c', ?, 1234, 99.0, 'REJEITADO')", (r2,))
        conn.commit()
        extrato = financeiro.extrato_motorista(1234, "2026-08-26", "2026-08-31", "FIORINO", conn=conn)
        self.assertEqual((extrato["total"], extrato["total_rotas"], extrato["total_pedagio"], extrato["pedagio_pendente"]), (707.5, 700.0, 7.5, 20.0))
        por_dia = {d["data"]: d for d in extrato["por_dia"]}
        self.assertEqual((por_dia["2026-08-27"]["valor"], por_dia["2026-08-27"]["pedagio_aprovado"]), (352.5, 7.5))

        sem = financeiro.extrato_motorista(1234, "2026-08-26", "2026-08-31", "TRUCK", conn=conn)
        self.assertEqual(sem["total"], 7.5)      # só o pedágio aprovado
        self.assertEqual(sem["rotas_sem_tarifa"], 2)

        fechamento = financeiro.fechamento_periodo("2026-08-26", "2026-08-31", {1234: "VAN_HR"}, conn=conn)
        self.assertEqual(len(fechamento), 1)
        self.assertEqual(fechamento[0]["total_rotas"], 550.0 + 550.0)
        conn.close()


if __name__ == "__main__":
    unittest.main()