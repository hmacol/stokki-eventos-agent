# -*- coding: utf-8 -*-
"""
test_rascunhos_rota.py -- pool direto da Stokki (Task 5): parada sem servico na Vuupt (id
sintetico negativo) ganha o servico SO AO ENVIAR; quem falha sai do rascunho com o motivo e
a rota nunca vai com id negativo. Rascunho so com ids reais segue igual (nem chama a
materializacao). Vuupt falsa, banco temporario, sem rede.

    py -3.11 -m unittest test_rascunhos_rota -v   (de dentro de painel_agentes/)
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import rascunhos_rota  # noqa: E402
from nucleo import banco, materializar, pedidos  # noqa: E402
from nucleo.test_materializar import FakeVuupt, _payload  # noqa: E402


def _inserir_rascunho(paradas, status="RASCUNHO", vuupt_route_id=None):
    conn = rascunhos_rota._conectar()
    try:
        cur = conn.execute(
            "INSERT INTO rascunhos_rota (data_alvo, lote_id, nome, agent_id, vehicle_id, motorista_nome, "
            "start_location_base_id, end_location_base_id, start_at, status, vuupt_route_id) "
            "VALUES ('2026-11-27', 'lote-teste', 'Planejamento - 27/11/2026 - #1', 47084, 1, 'Vinicius', "
            "6950, 6950, '2026-11-27T09:00:00Z', ?, ?)", (status, vuupt_route_id))
        rid = cur.lastrowid
        for ordem, (sid, codigo) in enumerate(paradas):
            conn.execute("INSERT INTO rascunhos_parada (rascunho_id, ordem, service_id, codigo) VALUES (?, ?, ?, ?)",
                         (rid, ordem, sid, codigo))
        conn.commit()
        return rid
    finally:
        conn.close()


def _paradas(rid):
    conn = rascunhos_rota._conectar()
    try:
        return [(r["service_id"], r["codigo"]) for r in conn.execute(
            "SELECT service_id, codigo FROM rascunhos_parada WHERE rascunho_id = ? ORDER BY ordem", (rid,))]
    finally:
        conn.close()


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        self.vuupt = FakeVuupt()
        self.criar_rota = mock.Mock(side_effect=lambda token, nome, start_at, sublote, **kw: ({"id": 777}, list(sublote), []))
        patches = [
            mock.patch.object(rascunhos_rota, "DB_PATH", self.db),
            mock.patch.object(banco, "DB_PATH", self.db),
            mock.patch("vuupt_client.VuuptClient", return_value=self.vuupt),
            mock.patch("rotas_client.criar_rota_removendo_conflitos", self.criar_rota),
            mock.patch("fingerprint_rotas.marcar_alocado"),
            mock.patch("nucleo.rotas.registrar_rota_enviada"),
            mock.patch("documentacao_rota.agendar"),
            mock.patch.object(rascunhos_rota, "_carregar_config", return_value={"planejamento": {"fonte_pool": "stokki"}}),
            mock.patch.object(rascunhos_rota.prontidao_stokki, "carregar_retrato_stokki", return_value=({}, None)),
            mock.patch.object(rascunhos_rota.eventos_freshhub, "registrar", return_value=None),
            mock.patch.object(rascunhos_rota, "recalcular_km"),
            mock.patch.object(rascunhos_rota, "_recalcular_km_silencioso"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _sem_servico(self, codigo, **status) -> int:
        conn = banco.conectar()
        try:
            pedidos.registrar_importacao(_payload(codigo), None, "nucleo_sem_servico", conn=conn)
            if status.get("status"):
                conn.execute("UPDATE nucleo_pedidos SET status = ? WHERE codigo = ?", (status["status"], codigo))
                conn.commit()
            return pedidos.id_sintetico(pedidos.buscar_pedido(codigo, conn=conn))
        finally:
            conn.close()

    @staticmethod
    def _parada(sid, codigo):
        return {"service_id": sid, "codigo": codigo, "titulo": codigo, "endereco": "RUA A 1", "latitude": -23.5,
                "longitude": -46.6, "sender_id": 11, "remetente_nome": "EMB", "nivel_dificuldade": 2,
                "volume_caixas": 2}

    def _ids_enviados(self):
        return [s["id"] for s in self.criar_rota.call_args.args[3]]


class EnviarRascunhoSemServicoTest(_Base):
    def test_enviar_rascunho_nunca_chama_criar_rota_com_id_negativo(self):
        a, b = self._sem_servico("PS-1"), self._sem_servico("PS-2")
        rid = _inserir_rascunho([(101, "#PS-9"), (a, "#PS-1"), (b, "#PS-2")])
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(self._ids_enviados(), [101, 900, 901])
        self.assertEqual(resultado["codigos_sem_servico"], {})
        self.assertEqual(_paradas(rid), [(101, "#PS-9"), (900, "#PS-1"), (901, "#PS-2")])
        self.assertEqual(pedidos.buscar_pedido("PS-1")["vuupt_service_id"], 900)

    def test_falha_no_meio_envia_so_os_materializados(self):
        a, b, c = self._sem_servico("PS-1"), self._sem_servico("PS-2"), self._sem_servico("PS-3")
        self.vuupt.falhar_em = {"PS-2"}
        rid = _inserir_rascunho([(a, "#PS-1"), (b, "#PS-2"), (c, "#PS-3")])
        with self.assertLogs(rascunhos_rota.logger, "INFO") as logs:
            resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(self._ids_enviados(), [900, 901])
        self.assertEqual(list(resultado["codigos_sem_servico"]), ["#PS-2"])
        self.assertIn("500", resultado["codigos_sem_servico"]["#PS-2"])
        self.assertEqual(_paradas(rid), [(900, "#PS-1"), (901, "#PS-3")])   # PS-2 saiu do rascunho (volta ao pool)
        self.assertTrue(any("PS-2" in m and "500" in m for m in logs.output))

    def test_todas_falham_nao_vai_na_vuupt_e_diz_o_motivo(self):
        a = self._sem_servico("PS-1", status=banco.PEDIDO_CANCELADO)
        rid = _inserir_rascunho([(a, "#PS-1")])
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.criar_rota.assert_not_called()
        self.assertIn("CANCELADO", resultado["codigos_sem_servico"]["#PS-1"])
        self.assertIn("CANCELADO", resultado["erro"])
        self.assertEqual(rascunhos_rota.buscar_rascunho(rid)["status"], rascunhos_rota.STATUS_ERRO_ENVIO)

    def test_rascunho_com_ids_reais_nao_chama_materializar(self):
        rid = _inserir_rascunho([(101, "#PS-1"), (102, "#PS-2")])
        with mock.patch.object(materializar, "materializar_servicos") as m:
            resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        m.assert_not_called()
        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(self._ids_enviados(), [101, 102])
        self.assertEqual(self.vuupt.chamadas, [])


class AdicionarEmRotaEnviadaTest(_Base):
    def setUp(self):
        super().setUp()
        self.vuupt.buscar_servico_por_id = lambda sid: {"id": sid, "status": "not_assigned", "route_id": None}
        self.adicionar = mock.Mock(return_value={"ok": True, "falharam": []})
        for p in (mock.patch("edicao_rota.ler_rota", return_value={"status": "not_started"}),
                  mock.patch("edicao_rota.adicionar_pedidos", self.adicionar),
                  mock.patch.object(rascunhos_rota, "_recusar_se_retornando")):
            p.start()
            self.addCleanup(p.stop)

    def test_adicao_em_rota_enviada_materializa_antes(self):
        a = self._sem_servico("PS-1")
        rid = _inserir_rascunho([(101, "#PS-9")], status="ENVIADO", vuupt_route_id=555)
        resultado = rascunhos_rota.adicionar_em_rota_enviada(rid, self._parada(a, "#PS-1"), "token", "hugo")
        self.assertEqual(resultado["service_id"], 900)
        self.assertEqual(self.adicionar.call_args.args[2], [900])
        self.assertEqual(_paradas(rid), [(101, "#PS-9"), (900, "#PS-1")])

    def test_adicao_recusa_com_motivo_quando_nao_materializa(self):
        a = self._sem_servico("PS-1-R1")
        rid = _inserir_rascunho([(101, "#PS-9")], status="ENVIADO", vuupt_route_id=555)
        with self.assertRaises(ValueError) as erro:
            rascunhos_rota.adicionar_em_rota_enviada(rid, self._parada(a, "#PS-1-R1"), "token", "hugo")
        self.assertIn("reentrega", str(erro.exception))
        self.adicionar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
