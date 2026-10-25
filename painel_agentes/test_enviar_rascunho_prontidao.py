# -*- coding: utf-8 -*-
"""
test_enviar_rascunho_prontidao.py -- a rota so sai com pedido liberado pela Stokki (plano 1.2 do freshhub,
Task 7): no envio do rascunho, parada cujo pedido ainda esta em separacao/conferencia/espera no retrato do
pipeline sai da rota (volta ao pool) e vira evento nao_aprontou no sistema novo; o que foi enviado vira
rota_enviada; o rascunho gravado vira roteirizado por parada. Falha da API nova nunca impede o envio.

    py -3.11 -m unittest test_enviar_rascunho_prontidao -v   (de dentro de painel_agentes/)
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import rascunhos_rota  # noqa: E402

PARADAS = [(101, "#PS-1"), (102, "#PS-2"), (103, "#PS-3")]


def _inserir_rascunho():
    conn = rascunhos_rota._conectar()
    try:
        cur = conn.execute(
            "INSERT INTO rascunhos_rota (data_alvo, lote_id, nome, agent_id, vehicle_id, motorista_nome, "
            "start_location_base_id, end_location_base_id, start_at, status) "
            "VALUES ('2026-10-27', 'lote-teste', 'Planejamento - 27/10/2026 - #3', 47084, 1, 'Vinicius', "
            "6950, 6950, '2026-10-27T09:00:00Z', 'RASCUNHO')")
        rid = cur.lastrowid
        for ordem, (sid, codigo) in enumerate(PARADAS):
            conn.execute("INSERT INTO rascunhos_parada (rascunho_id, ordem, service_id, codigo) VALUES (?, ?, ?, ?)",
                         (rid, ordem, sid, codigo))
        conn.commit()
        return rid
    finally:
        conn.close()


def _paradas(rid):
    conn = rascunhos_rota._conectar()
    try:
        return [r["codigo"] for r in conn.execute(
            "SELECT codigo FROM rascunhos_parada WHERE rascunho_id = ? ORDER BY ordem", (rid,))]
    finally:
        conn.close()


class EnviarRascunhoProntidaoTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.criar_rota = mock.Mock(side_effect=lambda token, nome, start_at, sublote, **kw: ({"id": 777}, list(sublote), []))
        self.registrar = mock.Mock(return_value={"aceitos": 1, "repetidos": 0, "desconhecidos": []})
        self.retrato = ({"PS-2": "Ready to Pack", "PS-1": "Waiting for Carrier"}, "25/10 10:00")
        patches = [
            mock.patch.object(rascunhos_rota, "DB_PATH", Path(self._tmp.name) / "dados.db"),
            mock.patch("rotas_client.criar_rota_removendo_conflitos", self.criar_rota),
            mock.patch("fingerprint_rotas.marcar_alocado"),
            mock.patch("nucleo.rotas.registrar_rota_enviada"),
            mock.patch("nucleo.pedidos.marcar_em_rota_por_service_ids"),
            mock.patch("documentacao_rota.agendar"),
            mock.patch.object(rascunhos_rota, "_carregar_config", return_value={"freshhub_api": {"url": "u", "token": "t"}}),
            mock.patch.object(rascunhos_rota.prontidao_stokki, "carregar_retrato_stokki", side_effect=lambda *a, **k: self.retrato),
            mock.patch.object(rascunhos_rota.eventos_freshhub, "registrar", self.registrar),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _eventos(self, tipo):
        return [e for c in self.registrar.call_args_list for e in c.args[1] if e["tipo"] == tipo]

    def test_parada_nao_pronta_sai_da_rota_e_vira_nao_aprontou(self):
        rid = _inserir_rascunho()
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(resultado["codigos_nao_prontos"], ["#PS-2"])
        sublote = self.criar_rota.call_args.args[3]
        self.assertEqual([s["id"] for s in sublote], [101, 103])
        self.assertEqual(_paradas(rid), ["#PS-1", "#PS-3"])  # a parada voltou ao pool (saiu do rascunho)
        nao = self._eventos("nao_aprontou")
        self.assertEqual([e["codigo"] for e in nao], ["PS-2"])
        self.assertEqual(nao[0]["dados"]["situacao_stokki"], "Em conferência")
        self.assertEqual(nao[0]["dados"]["rascunho_id"], rid)
        enviadas = self._eventos("rota_enviada")
        self.assertEqual([e["codigo"] for e in enviadas], ["PS-1", "PS-3"])
        self.assertEqual(enviadas[0]["dados"]["vuupt_route_id"], 777)

    def test_todas_nao_prontas_nao_vai_na_vuupt(self):
        self.retrato = ({"PS-1": "Open", "PS-2": "Separating", "PS-3": "On hold"}, "25/10 10:00")
        rid = _inserir_rascunho()
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.assertIn("Stokki", resultado["erro"])
        self.criar_rota.assert_not_called()
        self.assertEqual(sorted(e["codigo"] for e in self._eventos("nao_aprontou")), ["PS-1", "PS-2", "PS-3"])
        self.assertEqual(_paradas(rid), [])

    def test_sem_retrato_todas_seguem_e_falha_da_api_nao_impede_o_envio(self):
        self.retrato = ({}, None)
        self.registrar.return_value = None  # API nova fora: eventos_freshhub.registrar so loga
        rid = _inserir_rascunho()
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(resultado["codigos_nao_prontos"], [])
        self.assertEqual([s["id"] for s in self.criar_rota.call_args.args[3]], [101, 102, 103])
        self.assertEqual([e["codigo"] for e in self._eventos("rota_enviada")], ["PS-1", "PS-2", "PS-3"])

    def test_rascunho_gravado_vira_roteirizado_por_parada(self):
        servico = {"id": 201, "code": "#PS-9", "title": "#PS-9 - 1 / EMB / DEST", "address": "Rua A, 1",
                   "latitude": -23.5, "longitude": -46.6, "sender_id": 11, "dimension_3": 2,
                   "customer": {"name": "DEST", "code": "123", "operating_hour_start": "08:00",
                                "operating_hour_end": "17:00"}}
        rascunho = {"nome": "Planejamento - 27/10/2026 - #1", "particao": "Grande SP", "tipo_rota": "COMUM",
                    "start_location_base_id": 6950, "end_location_base_id": 6950,
                    "start_at": "2026-10-27T09:00:00Z", "sublote": [servico]}
        with mock.patch("mapa_util.carregar_remetentes_por_sender_id", return_value={11: "EMB"}):
            lote = rascunhos_rota.criar_lote_rascunhos(date(2026, 10, 27), [rascunho])
        self.assertTrue(lote)
        (e,) = self._eventos("roteirizado")
        self.assertEqual(e["codigo"], "PS-9")
        self.assertEqual(e["dados"]["rota_nome"], "Planejamento - 27/10/2026 - #1")
        self.assertEqual(e["dados"]["data"], "2026-10-27")
        self.assertIsInstance(e["dados"]["rascunho_id"], int)


class RevisaoTestCase(EnviarRascunhoProntidaoTestCase):
    """Revisao (25/10): a tela precisa saber das paradas que sairam mesmo quando a Vuupt falha (I4); lote de
    rascunho em --modo-teste nao manda evento de verdade."""

    def test_falha_da_vuupt_depois_de_tirar_paradas_devolve_quem_saiu_e_a_hora_do_retrato(self):
        self.criar_rota.side_effect = RuntimeError("Vuupt 500")
        rid = _inserir_rascunho()
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["codigos_nao_prontos"], ["#PS-2"])
        self.assertEqual(resultado["retrato_stokki_em"], "25/10 10:00")
        self.assertEqual(_paradas(rid), ["#PS-1", "#PS-3"])

    def test_retornos_cedo_tambem_trazem_a_chave(self):
        self.assertEqual(rascunhos_rota.enviar_rascunho(999, "token")["codigos_nao_prontos"], [])

    def test_envio_ok_traz_a_hora_do_retrato(self):
        rid = _inserir_rascunho()
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["retrato_stokki_em"], "25/10 10:00")

    def test_lote_em_modo_teste_repassa_a_flag_pros_eventos(self):
        servico = {"id": 201, "code": "#PS-9", "title": "t", "address": "Rua A, 1", "latitude": -23.5,
                   "longitude": -46.6, "sender_id": 11, "dimension_3": 2,
                   "customer": {"name": "D", "code": "1", "operating_hour_start": "08:00", "operating_hour_end": "17:00"}}
        rascunho = {"nome": "Planejamento - 27/10/2026 - #1", "start_location_base_id": 6950,
                    "end_location_base_id": 6950, "start_at": "2026-10-27T09:00:00Z", "sublote": [servico]}
        with mock.patch("mapa_util.carregar_remetentes_por_sender_id", return_value={}):
            rascunhos_rota.criar_lote_rascunhos(date(2026, 10, 27), [rascunho], modo_teste=True)
        self.assertTrue(self.registrar.call_args_list)
        self.assertTrue(all(c.kwargs.get("modo_teste") is True for c in self.registrar.call_args_list))


if __name__ == "__main__":
    unittest.main()
