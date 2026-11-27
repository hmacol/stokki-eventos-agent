# -*- coding: utf-8 -*-
"""
test_planejamento_sem_servico.py -- pool direto da Stokki (Task 6): os botoes do
Planejamento e da tela /pedidos em pedido SEM servico na Vuupt (id sintetico
negativo) agem LOCAL no nucleo e nunca chamam a Vuupt com id <= 0. Cancelar =
CANCELADO no nucleo + sai dos rascunhos nao enviados; reagendar/endereco = colunas
do nucleo (valem na materializacao). Id positivo continua chamando a Vuupt como hoje.
Banco temporario, Vuupt e Google mockados, sem rede.

    py -3.11 -m unittest test_planejamento_sem_servico -v   (de dentro de painel_agentes/)
"""
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import acoes_pedidos  # noqa: E402
import planejamento_rotas  # noqa: E402
import rascunhos_rota  # noqa: E402
import registro_dia_fixo as reg  # noqa: E402
from nucleo import banco, pedidos  # noqa: E402
from nucleo.test_materializar import _payload  # noqa: E402
from roteirizacao import cancelar_servico as cs  # noqa: E402

HOJE = date.today().isoformat()
FUTURO = (date.today() + timedelta(days=5)).isoformat()


def _rascunho(paradas, status="RASCUNHO"):
    conn = rascunhos_rota._conectar()
    try:
        cur = conn.execute(
            "INSERT INTO rascunhos_rota (data_alvo, lote_id, nome, agent_id, vehicle_id, motorista_nome, "
            "start_location_base_id, end_location_base_id, start_at, status) "
            "VALUES (?, 'lote-teste', 'Planejamento - #1', 47084, 1, 'Vinicius', 6950, 6950, ?, ?)",
            (HOJE, f"{HOJE}T09:00:00Z", status))
        rid = cur.lastrowid
        for ordem, (sid, codigo) in enumerate(paradas):
            conn.execute("INSERT INTO rascunhos_parada (rascunho_id, ordem, service_id, codigo, endereco, latitude, "
                         "longitude) VALUES (?, ?, ?, ?, 'RUA A 1', -23.5, -46.6)", (rid, ordem, sid, codigo))
        conn.commit()
        return rid
    finally:
        conn.close()


def _paradas(rid):
    conn = rascunhos_rota._conectar()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT service_id, codigo, endereco, latitude, longitude, ordem FROM rascunhos_parada "
            "WHERE rascunho_id = ? ORDER BY ordem", (rid,))]
    finally:
        conn.close()


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "dados.db"
        self.geo = mock.Mock(return_value=(-23.6, -46.7))
        patches = [
            mock.patch.object(banco, "DB_PATH", self.db),
            mock.patch.object(rascunhos_rota, "DB_PATH", self.db),
            mock.patch.object(reg, "DB_PATH", self.db),
            mock.patch.object(rascunhos_rota, "_recalcular_km_silencioso"),
            mock.patch.object(planejamento_rotas, "_carregar_config",
                              return_value={"vuupt_api": {"token": "tok"}, "google_maps": {"api_key": "key"}}),
            mock.patch.object(planejamento_rotas, "_ressincronizar"),
            mock.patch("geocodificacao.geocodificar", self.geo),
            mock.patch.object(cs, "ressincronizar_ids"),
            mock.patch.object(acoes_pedidos.tratativas, "registrar_evento"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(planejamento_rotas, "VuuptClient")
        self.vuupt_cls = p.start()
        self.addCleanup(p.stop)
        self.vuupt = self.vuupt_cls.return_value
        self.vuupt.buscar_servico_por_id.return_value = {"id": 555, "customer_id": 999, "status": "not_assigned"}
        p = mock.patch.object(cs, "VuuptClient")
        self.cs_vuupt_cls = p.start()
        self.addCleanup(p.stop)
        p = mock.patch("vuupt_client.VuuptClient")
        self.vuupt_client_cls = p.start()
        self.addCleanup(p.stop)
        banco.conectar().close()   # cria o esquema do nucleo no banco temporario
        rascunhos_rota._conectar().close()

    def _sem_servico(self, codigo="PS-1", status=None) -> int:
        conn = banco.conectar()
        try:
            pedidos.registrar_importacao(_payload(codigo), None, "nucleo_sem_servico", conn=conn)
            if status:
                conn.execute("UPDATE nucleo_pedidos SET status = ? WHERE codigo = ?", (status, codigo))
                conn.commit()
            return pedidos.id_sintetico(pedidos.buscar_pedido(codigo, conn=conn))
        finally:
            conn.close()

    @staticmethod
    def _linha(codigo="PS-1"):
        return pedidos.buscar_pedido(codigo)

    def _sem_vuupt(self):
        self.vuupt_cls.assert_not_called()
        self.cs_vuupt_cls.assert_not_called()
        self.vuupt_client_cls.assert_not_called()
        self.assertEqual(self.vuupt.mock_calls, [])
        planejamento_rotas._ressincronizar.assert_not_called()
        cs.ressincronizar_ids.assert_not_called()

    def _equipe(self, codigo, dia):
        conn = reg.conectar(self.db)
        try:
            return conn.execute("SELECT origem, service_id FROM agendamentos_origem WHERE codigo = ? AND data = ?",
                                (codigo, dia)).fetchall()
        finally:
            conn.close()


class CancelarSemServicoTest(_Base):
    def test_cancelar_sem_servico_marca_cancelado_local(self):
        sid = self._sem_servico()
        rid = _rascunho([(sid, "#PS-1"), (101, "#PS-9")])
        outro = _rascunho([(sid, "#PS-1")])
        self.assertEqual(planejamento_rotas.cancelar_pedido(sid, rid, usuario="hugo"), {"ok": True})
        self.assertEqual(self._linha()["status"], banco.PEDIDO_CANCELADO)
        self.assertEqual([p["service_id"] for p in _paradas(rid)], [101])
        self.assertEqual(_paradas(outro), [])
        conn = banco.conectar()
        try:
            ev = conn.execute("SELECT origem, dados_json FROM nucleo_eventos WHERE tipo = 'PEDIDO_CANCELADO'").fetchone()
        finally:
            conn.close()
        self.assertEqual(ev["origem"], banco.ORIGEM_PAINEL)
        self.assertIn("hugo", json.loads(ev["dados_json"])["motivo"])
        self._sem_vuupt()

    def test_cancelar_do_pool_e_repetido(self):
        sid = self._sem_servico()
        self.assertEqual(planejamento_rotas.cancelar_pedido(sid), {"ok": True})
        self.assertEqual(planejamento_rotas.cancelar_pedido(sid), {"ok": True})   # ja estava: nao e erro
        self._sem_vuupt()

    def test_cancelar_pedido_que_saiu_do_pool_recusa_com_motivo(self):
        sid = self._sem_servico(status=banco.PEDIDO_EM_ROTA)
        r = planejamento_rotas.cancelar_pedido(sid)
        self.assertFalse(r["ok"])
        self.assertIn("EM_ROTA", r["erro"])
        self._sem_vuupt()

    def test_cancelar_servico_completo_sem_servico(self):
        sid = self._sem_servico()
        rid = _rascunho([(sid, "#PS-1")])
        self.assertEqual(cs.cancelar_servico_completo("tok", sid), {"ok": True, "ja_estava": False, "erro": ""})
        self.assertEqual(self._linha()["status"], banco.PEDIDO_CANCELADO)
        self.assertEqual(_paradas(rid), [])
        self.assertEqual(cs.cancelar_servico_completo("tok", sid), {"ok": True, "ja_estava": True, "erro": ""})
        self._sem_vuupt()


class ReagendarSemServicoTest(_Base):
    def test_reagendar_sem_servico_grava_no_nucleo(self):
        sid = self._sem_servico()
        self.assertEqual(planejamento_rotas.reagendar_pedido(sid, "2026-12-01", "08:00", "12:00"), {"ok": True})
        linha = self._linha()
        self.assertEqual((linha["agendamento_inicio"], linha["agendamento_fim"]),
                         ("2026-12-01 08:00:00", "2026-12-01 12:00:00"))
        self.assertEqual(set(json.loads(linha["edicao_local_json"])), {"agendamento_inicio", "agendamento_fim"})
        # data da equipe registrada pelo codigo (vale tambem depois de materializar)
        self.assertEqual([r["origem"] for r in self._equipe("PS-1", "2026-12-01")], [reg.ORIGEM_EQUIPE])
        self._sem_vuupt()

    def test_reagendar_manter_horarios_usa_a_janela_do_nucleo(self):
        sid = self._sem_servico()   # payload 07:00-10:00
        r = planejamento_rotas.reagendar_pedido(sid, "2026-12-01", "13:00", "17:00", manter_horarios=True)
        self.assertEqual(r, {"ok": True})
        linha = self._linha()
        self.assertEqual((linha["agendamento_inicio"], linha["agendamento_fim"]),
                         ("2026-12-01 07:00:00", "2026-12-01 10:00:00"))
        self._sem_vuupt()

    def test_reagendar_sem_servico_cancelado_recusa_com_motivo(self):
        sid = self._sem_servico(status=banco.PEDIDO_CANCELADO)
        r = planejamento_rotas.reagendar_pedido(sid, "2026-12-01", "08:00", "12:00")
        self.assertFalse(r["ok"])
        self.assertIn("CANCELADO", r["erro"])
        self._sem_vuupt()

    def test_reagendar_lote_misto(self):
        sid = self._sem_servico()
        r = planejamento_rotas.reagendar_pedidos([{"service_id": sid}, {"service_id": 555}],
                                                 "2026-12-01", "08:00", "12:00")
        self.assertEqual(r, {"ok": True, "falhas": []})
        self.vuupt.atualizar_servico.assert_called_once_with(555, {
            "scheduled_start": "2026-12-01T08:00:00-03:00", "scheduled_end": "2026-12-01T12:00:00-03:00"})
        planejamento_rotas._ressincronizar.assert_called_once_with(self.vuupt, [555])
        self.assertEqual(self._linha()["agendamento_inicio"], "2026-12-01 08:00:00")
        self.assertEqual([r["origem"] for r in self._equipe("PS-1", "2026-12-01")], [reg.ORIGEM_EQUIPE])

    def test_reagendar_lote_so_sem_servico_com_falha(self):
        sid = self._sem_servico()
        cancelado = self._sem_servico("PS-2", status=banco.PEDIDO_CANCELADO)
        r = planejamento_rotas.reagendar_pedidos([{"service_id": sid}, {"service_id": cancelado}],
                                                 "2026-12-01", "08:00", "12:00", manter_horarios=True)
        self.assertTrue(r["ok"])
        self.assertEqual([f["service_id"] for f in r["falhas"]], [cancelado])
        self.assertIn("CANCELADO", r["falhas"][0]["erro"])
        self.assertEqual(self.vuupt.mock_calls, [])
        planejamento_rotas._ressincronizar.assert_not_called()

    def test_checar_dia_fixo_sem_servico_le_o_nucleo(self):
        sid = self._sem_servico()
        import fora_dia_fixo
        with mock.patch.object(planejamento_rotas, "regra_dia_fixo_do_servico",
                               return_value={"nome": "Sorocaba", "regiao": "Sorocaba", "dias": [3]}) as regra, \
                mock.patch.object(planejamento_rotas, "data_valida_na_regiao", return_value=False), \
                mock.patch.object(planejamento_rotas, "descricao_dias", return_value="quintas"), \
                mock.patch.object(fora_dia_fixo, "carregar_tipos_carga", return_value={}), \
                mock.patch.object(fora_dia_fixo, "calcular_valor", return_value=150.0):
            r = planejamento_rotas.checar_reagendamento_dia_fixo([sid], "2026-12-01")
        self.assertTrue(r["ok"])
        self.assertEqual([(f["service_id"], f["valor"]) for f in r["fora_do_dia"]], [(sid, 150.0)])
        self.assertIn("PS-1", r["fora_do_dia"][0]["codigo"])
        servico = regra.call_args.args[0]
        self.assertEqual((servico["id"], servico["address"]), (sid, "RUA A 1, Sao Paulo - SP"))
        self.vuupt.buscar_servico_por_id.assert_not_called()


class EnderecoSemServicoTest(_Base):
    def test_alterar_endereco_sem_servico_grava_no_nucleo(self):
        sid = self._sem_servico()
        rid = _rascunho([(sid, "#PS-1")])
        self.assertEqual(planejamento_rotas.editar_endereco_pedido(sid, "RUA NOVA 2", rid), {"ok": True})
        linha = self._linha()
        self.assertEqual((linha["endereco"], linha["latitude"], linha["longitude"]), ("RUA NOVA 2", -23.6, -46.7))
        self.assertEqual(set(json.loads(linha["edicao_local_json"])), {"endereco", "latitude", "longitude"})
        p = _paradas(rid)[0]
        self.assertEqual((p["endereco"], p["latitude"], p["longitude"]), ("RUA NOVA 2", -23.6, -46.7))
        self.geo.assert_called_once_with("RUA NOVA 2", "key")
        self._sem_vuupt()

    def test_endereco_sem_servico_que_nao_geocodifica_recusa_com_motivo(self):
        sid = self._sem_servico()
        self.geo.return_value = None
        r = planejamento_rotas.editar_endereco_pedido(sid, "RUA QUE NAO EXISTE")
        self.assertFalse(r["ok"])
        self.assertIn("localizar", r["erro"])
        self.assertEqual(self._linha()["endereco"], "RUA A 1, Sao Paulo - SP")
        self._sem_vuupt()

    def test_endereco_lote_misto(self):
        sid = self._sem_servico()
        r = planejamento_rotas.editar_endereco_pedidos(
            [{"service_id": sid, "rascunho_id": None}, {"service_id": 555, "rascunho_id": None}], "RUA NOVA 2")
        self.assertEqual(r, {"ok": True, "falhas": []})
        self.vuupt.atualizar_servico.assert_called_once_with(
            555, {"address": "RUA NOVA 2", "latitude": -23.6, "longitude": -46.7})
        self.vuupt.buscar_servico_por_id.assert_called_once_with(555)
        planejamento_rotas._ressincronizar.assert_called_once_with(self.vuupt, [555])
        self.assertEqual(self._linha()["endereco"], "RUA NOVA 2")

    def test_transportadora_sem_servico(self):
        sid = self._sem_servico()
        catalogo = mock.Mock()
        catalogo.endereco_terceiros.return_value = "GALPAO X, Sao Paulo - SP"
        with mock.patch.object(planejamento_rotas, "_catalogo_transportadoras", return_value=catalogo):
            r = planejamento_rotas.editar_transportadora_pedidos([{"service_id": sid, "rascunho_id": None}], "X")
        self.assertEqual(r, {"ok": True, "falhas": [], "endereco": "GALPAO X, Sao Paulo - SP"})
        self.assertEqual(self._linha()["endereco"], "GALPAO X, Sao Paulo - SP")
        self.assertEqual(self.vuupt.mock_calls, [])

    def test_nivel_horario_sem_servico_usa_o_documento_do_nucleo(self):
        sid = self._sem_servico()
        rid = _rascunho([(sid, "#PS-1")])
        with mock.patch.object(planejamento_rotas, "definir_ajuste_manual") as definir, \
                mock.patch.object(rascunhos_rota, "atualizar_nivel_horario_parada") as parada:
            r = planejamento_rotas.editar_nivel_horario_pedido(sid, 2, "08:00", "12:00", rid)
        self.assertEqual(r, {"ok": True})
        definir.assert_called_once_with("123", 2, "08:00", "12:00")
        parada.assert_called_once_with(rid, sid, 2, "08:00", "12:00")
        self._sem_vuupt()


class RascunhoSemServicoTest(_Base):
    def test_mover_reordenar_e_remover_nao_tocam_a_vuupt(self):
        a, b = self._sem_servico("PS-1"), self._sem_servico("PS-2")
        origem = _rascunho([(a, "#PS-1"), (b, "#PS-2"), (101, "#PS-9")])
        destino = _rascunho([])
        rascunhos_rota.reordenar_paradas(origem, [101, b, a])
        self.assertEqual([p["service_id"] for p in _paradas(origem)], [101, b, a])
        rascunhos_rota.mover_parada(a, origem, destino, 0)
        rascunhos_rota.mover_paradas([{"service_id": b, "rascunho_origem_id": origem}], destino)
        self.assertEqual(sorted(p["service_id"] for p in _paradas(destino)), sorted([a, b]))
        self.assertEqual(rascunhos_rota.remover_parada(destino, a)["service_id"], a)
        self.assertEqual([p["service_id"] for p in _paradas(destino)], [b])
        self._sem_vuupt()


class AcoesPedidosSemServicoTest(_Base):
    def setUp(self):
        super().setUp()
        self.vuupt_pedidos = mock.Mock()

    def _sem_vuupt_pedidos(self):
        self.assertEqual(self.vuupt_pedidos.mock_calls, [])
        self._sem_vuupt()

    def test_cancelar_vuupt_sem_servico(self):
        sid = self._sem_servico()
        r = acoes_pedidos.cancelar_vuupt(sid, "hugo", token="tok", vuupt=self.vuupt_pedidos)
        self.assertEqual(r, {"ok": True, "ja_estava": False, "erro": "", "aviso": ""})
        self.assertEqual(self._linha()["status"], banco.PEDIDO_CANCELADO)
        codigo = acoes_pedidos.tratativas.registrar_evento.call_args.args[0]
        self.assertEqual(codigo, "PS-1")
        self._sem_vuupt_pedidos()

    def test_alterar_endereco_sem_servico(self):
        sid = self._sem_servico()
        r = acoes_pedidos.alterar_endereco(sid, "RUA NOVA 2", "hugo", vuupt=self.vuupt_pedidos)
        self.assertEqual(r, {"ok": True, "erro": ""})
        self.assertEqual(self._linha()["endereco"], "RUA NOVA 2")
        self.assertEqual(acoes_pedidos.tratativas.registrar_evento.call_args.args[0], "PS-1")
        self._sem_vuupt_pedidos()

    def test_agendar_sem_servico(self):
        sid = self._sem_servico()
        r = acoes_pedidos.agendar(sid, FUTURO, "08:00", "12:00", "hugo", vuupt=self.vuupt_pedidos)
        self.assertEqual(r, {"ok": True, "erro": ""})
        self.assertEqual(self._linha()["agendamento_inicio"], f"{FUTURO} 08:00:00")
        self._sem_vuupt_pedidos()

    def test_reenviar_sem_servico_recusa_com_motivo(self):
        sid = self._sem_servico()
        r = acoes_pedidos.reenviar(sid, "motivo do teste", "hugo", vuupt=self.vuupt_pedidos,
                                   stokki=mock.Mock(side_effect=AssertionError("nao consulta a Stokki")))
        self.assertFalse(r["ok"])
        self.assertIn("sem serviço na Vuupt", r["erro"])
        self._sem_vuupt_pedidos()


class ComServicoTest(_Base):
    def test_acao_com_servico_continua_chamando_vuupt(self):
        self.assertEqual(planejamento_rotas.cancelar_pedido(555), {"ok": True})
        self.vuupt.cancelar_servico_oficial.assert_called_once_with(555)
        self.assertEqual(planejamento_rotas.reagendar_pedido(555, "2026-12-01", "08:00", "12:00"), {"ok": True})
        self.vuupt.atualizar_servico.assert_called_once_with(555, {
            "scheduled_start": "2026-12-01T08:00:00-03:00", "scheduled_end": "2026-12-01T12:00:00-03:00"})
        self.vuupt.atualizar_servico.reset_mock()
        self.assertEqual(planejamento_rotas.editar_endereco_pedido(555, "RUA NOVA 2"), {"ok": True})
        self.vuupt.atualizar_servico.assert_called_once_with(
            555, {"address": "RUA NOVA 2", "latitude": -23.6, "longitude": -46.7})
        self.vuupt.atualizar_customer.assert_called_once_with(
            999, {"address": "RUA NOVA 2", "latitude": -23.6, "longitude": -46.7})
        # cancelar_servico_completo (portal / tela Pedidos) le e cancela na Vuupt
        vuupt = mock.Mock()
        vuupt.buscar_servico_por_id.return_value = {"id": 555, "status": "not_assigned"}
        self.assertTrue(cs.cancelar_servico_completo("tok", 555, vuupt=vuupt)["ok"])
        vuupt.cancelar_servico_oficial.assert_called_once_with(555)
        # tela Pedidos le o servico na Vuupt antes de agir
        vuupt = mock.Mock()
        vuupt.buscar_servico_por_id.return_value = {"id": 555, "status": "not_assigned", "code": "#PS-5"}
        self.assertEqual(acoes_pedidos.agendar(555, FUTURO, "08:00", "12:00", "hugo", vuupt=vuupt),
                         {"ok": True, "erro": ""})
        vuupt.buscar_servico_por_id.assert_called_once_with(555)


if __name__ == "__main__":
    unittest.main()
