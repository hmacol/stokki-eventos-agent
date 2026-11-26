# -*- coding: utf-8 -*-
"""Publicar para motoristas com horario (Hugo, 25/11).
Rodar de dentro de painel_agentes/: py -3.11 -m unittest test_oferta_agendada -v"""
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import rascunhos_rota  # noqa: E402

HOJE = date(2026, 11, 25)
RASC = {"id": 7, "nome": "R", "data_alvo": "2026-11-25", "oferta_agendada_em": "2026-11-25 18:30"}


class TestOfertaAgendada(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(rascunhos_rota, "DB_PATH", Path(self.tmp.name) / "dados.db"),
                  mock.patch("mapa_util.carregar_remetentes_por_sender_id", lambda: {})):
            p.start()
            self.addCleanup(p.stop)
        rascunhos_rota.criar_lote_rascunhos(HOJE, [{"nome": "Rota 1", "start_location_base_id": 1,
                                                    "start_at": "2026-11-25T09:00:00Z", "sublote": []}])
        self.rid = rascunhos_rota.listar_rascunhos_do_dia(HOJE)[0]["id"]
        conn = rascunhos_rota._conectar()
        conn.execute("INSERT INTO rascunhos_parada (rascunho_id, ordem, service_id) VALUES (?, 0, 1)", (self.rid,))
        conn.commit()
        conn.close()

    def _agora(self, hm):
        return datetime.fromisoformat(f"{HOJE.isoformat()} {hm}:00")

    def _rascunho(self):
        return rascunhos_rota.buscar_rascunho(self.rid)

    def test_agenda_hoje_e_so_vence_na_hora(self):
        q = rascunhos_rota.agendar_oferta(self.rid, "18:30", agora=self._agora("10:00"))
        self.assertEqual(q, "2026-11-25 18:30")
        self.assertEqual(self._rascunho()["oferta_agendada_em"], q)
        self.assertEqual(rascunhos_rota.listar_ofertas_agendadas_vencidas("2026-11-25 18:29"), [])
        self.assertEqual([r["id"] for r in rascunhos_rota.listar_ofertas_agendadas_vencidas("2026-11-25 18:30")], [self.rid])

    def test_horario_passado_ou_invalido_e_recusado(self):
        for h in ("09:59", "10:00", "25:00", "abc"):
            with self.assertRaises(ValueError, msg=h):
                rascunhos_rota.agendar_oferta(self.rid, h, agora=self._agora("10:00"))
        self.assertIsNone(self._rascunho()["oferta_agendada_em"])

    def test_agenda_outro_dia(self):
        conn = rascunhos_rota._conectar()
        conn.execute("UPDATE rascunhos_rota SET data_alvo = '2026-11-27' WHERE id = ?", (self.rid,))
        conn.commit()
        conn.close()
        for texto in ("26/11 07:00", "2026-11-26 07:00", "26/11/2026 7:00"):
            q = rascunhos_rota.agendar_oferta(self.rid, texto, agora=self._agora("10:00"))
            self.assertEqual(q, "2026-11-26 07:00", texto)
        self.assertEqual(rascunhos_rota.listar_ofertas_agendadas_vencidas("2026-11-25 23:59"), [])
        self.assertEqual(len(rascunhos_rota.listar_ofertas_agendadas_vencidas("2026-11-26 07:00")), 1)

    def test_nao_agenda_depois_da_data_da_entrega(self):
        with self.assertRaises(ValueError):
            rascunhos_rota.agendar_oferta(self.rid, "2026-11-26 07:00", agora=self._agora("10:00"))
        self.assertIsNone(self._rascunho()["oferta_agendada_em"])

    def test_publicar_e_despublicar_limpam_agendamento(self):
        rascunhos_rota.agendar_oferta(self.rid, "18:30", agora=self._agora("10:00"))
        rascunhos_rota.publicar_oferta(self.rid)
        self.assertIsNone(self._rascunho()["oferta_agendada_em"])

    def test_data_passada_ou_inexistente_e_recusada(self):
        for h in ("24/11 07:00", "31/02 07:00"):
            with self.assertRaises(ValueError, msg=h):
                rascunhos_rota.agendar_oferta(self.rid, h, agora=self._agora("10:00"))

    def test_cancelar_limpa(self):
        rascunhos_rota.agendar_oferta(self.rid, "18:30", agora=self._agora("10:00"))
        self.assertIsNone(rascunhos_rota.agendar_oferta(self.rid, "", agora=self._agora("10:00")))
        self.assertEqual(rascunhos_rota.listar_ofertas_agendadas_vencidas("2026-11-25 23:59"), [])

    def test_nao_agenda_com_motorista(self):
        conn = rascunhos_rota._conectar()
        conn.execute("UPDATE rascunhos_rota SET agent_id = 5 WHERE id = ?", (self.rid,))
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError):
            rascunhos_rota.agendar_oferta(self.rid, "18:30", agora=self._agora("10:00"))

    def test_baixar_guarda_o_erro_no_card(self):
        rascunhos_rota.agendar_oferta(self.rid, "18:30", agora=self._agora("10:00"))
        rascunhos_rota.baixar_agendamento_oferta(self.rid, "Nenhum motorista elegível")
        r = self._rascunho()
        self.assertEqual((r["oferta_agendada_em"], r["oferta_agendada_erro"]), (None, "Nenhum motorista elegível"))
        self.assertEqual(rascunhos_rota.listar_ofertas_agendadas_vencidas("2026-11-25 23:59"), [])


class TestTimer(unittest.TestCase):
    def test_publica_avisa_e_baixa_agendamento(self):
        import publicar_ofertas_agendadas as t
        with mock.patch.object(rascunhos_rota, "listar_ofertas_agendadas_vencidas", return_value=[RASC]), \
             mock.patch.object(rascunhos_rota, "baixar_agendamento_oferta") as baixar, \
             mock.patch("planejamento_rotas._carregar_config", return_value={}), \
             mock.patch("planejamento_rotas.publicar_oferta_rascunho", return_value={"ok": True, "elegiveis": [1, 2]}) as pub, \
             mock.patch("avisar_motoristas_rotas.push_ofertas_vps") as push, \
             mock.patch("avisar_motoristas_rotas.notificar_oferta_motoristas", return_value={}) as aviso:
            stats = t.rodar()
        self.assertEqual(stats, {"vencidas": 1, "publicadas": 1, "falhas": 0})
        baixar.assert_called_once_with(7)
        pub.assert_called_once_with(7)
        push.assert_called_once()
        aviso.assert_called_once()

    def test_falha_vira_erro_no_card_e_nao_avisa(self):
        import publicar_ofertas_agendadas as t
        with mock.patch.object(rascunhos_rota, "listar_ofertas_agendadas_vencidas", return_value=[RASC]), \
             mock.patch.object(rascunhos_rota, "baixar_agendamento_oferta") as baixar, \
             mock.patch("planejamento_rotas._carregar_config", return_value={}), \
             mock.patch("planejamento_rotas.publicar_oferta_rascunho", return_value={"ok": False, "erro": "ninguém elegível"}), \
             mock.patch("avisar_motoristas_rotas.notificar_oferta_motoristas") as aviso:
            stats = t.rodar()
        self.assertEqual(stats["falhas"], 1)
        baixar.assert_called_with(7, "ninguém elegível")
        aviso.assert_not_called()

    def test_modo_teste_nao_mexe_em_nada(self):
        import publicar_ofertas_agendadas as t
        with mock.patch.object(rascunhos_rota, "listar_ofertas_agendadas_vencidas", return_value=[RASC]), \
             mock.patch.object(rascunhos_rota, "baixar_agendamento_oferta") as baixar, \
             mock.patch("planejamento_rotas._carregar_config", return_value={}), \
             mock.patch("planejamento_rotas.publicar_oferta_rascunho") as pub:
            t.rodar(modo_teste=True)
        baixar.assert_not_called()
        pub.assert_not_called()


if __name__ == "__main__":
    unittest.main()
