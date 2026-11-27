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

    def test_falha_tecnica_no_meio_nao_envia_e_mantem_todas_as_paradas(self):
        """Revisao Fable FIX 1: erro da Vuupt (500/429/timeout) num pedido do lote NAO tira a parada nem
        manda rota parcial: o envio para (ERRO_ENVIO, 'tente de novo') e nada e descartado."""
        a, b, c = self._sem_servico("PS-1"), self._sem_servico("PS-2"), self._sem_servico("PS-3")
        self.vuupt.falhar_em = {"PS-2"}
        rid = _inserir_rascunho([(a, "#PS-1"), (b, "#PS-2"), (c, "#PS-3")])
        with self.assertLogs(rascunhos_rota.logger, "INFO") as logs:
            resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"], resultado)
        self.criar_rota.assert_not_called()
        # fix round 1: quem FICOU no rascunho vai em codigos_pendentes_tecnico; codigos_sem_servico = quem saiu
        self.assertEqual(resultado["codigos_sem_servico"], {})
        self.assertEqual(list(resultado["codigos_pendentes_tecnico"]), ["#PS-2"])
        self.assertIn("500", resultado["codigos_pendentes_tecnico"]["#PS-2"])
        self.assertEqual(_paradas(rid), [(900, "#PS-1"), (b, "#PS-2"), (901, "#PS-3")])   # nada descartado
        self.assertIn("nenhuma parada descartada", resultado["erro"])
        rascunho = rascunhos_rota.buscar_rascunho(rid)
        self.assertEqual(rascunho["status"], rascunhos_rota.STATUS_ERRO_ENVIO)
        self.assertIn("falha tecnica ao criar servico na Vuupt", rascunho["erro_envio"])
        self.assertIn("tente de novo", rascunho["erro_envio"])
        self.assertEqual(resultado["erro"], rascunho["erro_envio"])
        self.assertTrue(any("PS-2" in m and "500" in m for m in logs.output))

    def _vuupt_com_status(self, status_por_code: dict):
        from vuupt_client import VuuptAPIError
        original = self.vuupt.criar_ou_atualizar_servico

        def criar(payload):
            s = status_por_code.get(payload["code"])
            if s == "timeout":
                raise TimeoutError("read timed out")
            if s:
                raise VuuptAPIError(f"Status {s}: {{'message': 'erro {s} da Vuupt'}}")
            return original(payload)
        self.vuupt.criar_ou_atualizar_servico = criar

    def test_422_tira_so_essa_parada_e_envia_as_demais_com_o_motivo(self):
        """Fix round 1 da onda 2: erro PERMANENTE da Vuupt e recusa: sai so aquela parada, a rota vai."""
        a, b, c = self._sem_servico("PS-1"), self._sem_servico("PS-2"), self._sem_servico("PS-3")
        self._vuupt_com_status({"#PS-2": 422})
        rid = _inserir_rascunho([(a, "#PS-1"), (b, "#PS-2"), (c, "#PS-3")])
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(self._ids_enviados(), [900, 901])
        self.assertEqual(_paradas(rid), [(900, "#PS-1"), (901, "#PS-3")])
        self.assertEqual(list(resultado["codigos_sem_servico"]), ["#PS-2"])
        self.assertTrue(resultado["codigos_sem_servico"]["#PS-2"].startswith("a Vuupt recusou o pedido (Status 422): "))
        self.assertIn("erro 422 da Vuupt", resultado["codigos_sem_servico"]["#PS-2"])
        self.assertEqual(resultado["codigos_pendentes_tecnico"], {})

    def test_todos_recusados_com_o_mesmo_422_e_sistemico_e_nada_sai(self):
        """Onda 3 (Hugo): o mesmo erro permanente em todos = problema nosso/da Vuupt, nao dos pedidos."""
        a, b, c = self._sem_servico("PS-1"), self._sem_servico("PS-2"), self._sem_servico("PS-3")
        self._vuupt_com_status({"#PS-1": 422, "#PS-2": 422, "#PS-3": 422})
        rid = _inserir_rascunho([(a, "#PS-1"), (b, "#PS-2"), (c, "#PS-3")])
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.criar_rota.assert_not_called()
        self.assertEqual(_paradas(rid), [(a, "#PS-1"), (b, "#PS-2"), (c, "#PS-3")])
        self.assertEqual(resultado["codigos_sem_servico"], {})
        self.assertEqual(set(resultado["codigos_pendentes_tecnico"]), {"#PS-1", "#PS-2", "#PS-3"})
        self.assertIn("possivel problema sistemico", resultado["erro"])
        self.assertEqual(rascunhos_rota.buscar_rascunho(rid)["status"], rascunhos_rota.STATUS_ERRO_ENVIO)

    def test_um_422_e_dois_ok_tira_so_o_422(self):
        a, b, c = self._sem_servico("PS-1"), self._sem_servico("PS-2"), self._sem_servico("PS-3")
        self._vuupt_com_status({"#PS-1": 422})
        rid = _inserir_rascunho([(a, "#PS-1"), (b, "#PS-2"), (c, "#PS-3")])
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(list(resultado["codigos_sem_servico"]), ["#PS-1"])
        self.assertEqual(_paradas(rid), [(900, "#PS-2"), (901, "#PS-3")])

    def test_recusa_mais_ocupado_tem_texto_fiel(self):
        """Onda 3 (nit): so ocupado (sem falha tecnica) nao pode dizer "as com falha tecnica ficaram"."""
        a = self._sem_servico("PS-1", status=banco.PEDIDO_CANCELADO)
        b = self._sem_servico("PS-2")
        conn = banco.conectar()
        try:
            conn.execute("INSERT INTO nucleo_materializacao VALUES ('PS-2', ?, 'outro clique')", (banco.agora(),))
            conn.commit()
        finally:
            conn.close()
        rid = _inserir_rascunho([(a, "#PS-1"), (b, "#PS-2")])
        with mock.patch.object(materializar, "ESPERA_TRAVA_S", 0.2):
            resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.assertNotIn("falha tecnica", resultado["erro"])
        self.assertIn("as em materializacao por outro envio ficaram no rascunho", resultado["erro"])
        self.assertEqual(_paradas(rid), [(b, "#PS-2")])

    def test_429_e_timeout_mantem_todas_as_paradas_e_nao_criam_rota(self):
        a, b, c = self._sem_servico("PS-1"), self._sem_servico("PS-2"), self._sem_servico("PS-3")
        self._vuupt_com_status({"#PS-2": 429, "#PS-3": "timeout"})
        rid = _inserir_rascunho([(a, "#PS-1"), (b, "#PS-2"), (c, "#PS-3")])
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.criar_rota.assert_not_called()
        self.assertEqual(_paradas(rid), [(900, "#PS-1"), (b, "#PS-2"), (c, "#PS-3")])
        self.assertEqual(set(resultado["codigos_pendentes_tecnico"]), {"#PS-2", "#PS-3"})
        self.assertEqual(resultado["codigos_sem_servico"], {})
        self.assertEqual(rascunhos_rota.buscar_rascunho(rid)["status"], rascunhos_rota.STATUS_ERRO_ENVIO)

    def test_401_tem_mensagem_propria_e_mantem_a_parada(self):
        a = self._sem_servico("PS-1")
        self._vuupt_com_status({"#PS-1": 401})
        rid = _inserir_rascunho([(a, "#PS-1")])
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.criar_rota.assert_not_called()
        self.assertEqual(_paradas(rid), [(a, "#PS-1")])
        self.assertIn("credencial/permissao da Vuupt", resultado["erro"])
        self.assertIn("avise o Hugo", resultado["erro"])

    def test_429_mantem_a_parada_e_reenvio_depois_funciona(self):
        a, b = self._sem_servico("PS-1"), self._sem_servico("PS-2")
        rid = _inserir_rascunho([(a, "#PS-1"), (b, "#PS-2")])
        original = self.vuupt.criar_ou_atualizar_servico

        def com_429(payload):
            if payload["code"] == "#PS-2":
                raise RuntimeError("429 Client Error: Too Many Requests")
            return original(payload)
        self.vuupt.criar_ou_atualizar_servico = com_429
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.criar_rota.assert_not_called()
        self.assertEqual(_paradas(rid), [(900, "#PS-1"), (b, "#PS-2")])
        self.assertIn("429", rascunhos_rota.buscar_rascunho(rid)["erro_envio"])
        # a Vuupt voltou: o reenvio (ERRO_ENVIO e aceito) sai com as duas paradas
        self.vuupt.criar_ou_atualizar_servico = original
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(self._ids_enviados(), [900, 901])

    def test_recusa_de_regra_sai_e_falha_tecnica_fica(self):
        a = self._sem_servico("PS-1", status=banco.PEDIDO_CANCELADO)
        b = self._sem_servico("PS-2")
        self.vuupt.falhar_em = {"PS-2"}
        rid = _inserir_rascunho([(101, "#PS-9"), (a, "#PS-1"), (b, "#PS-2")])
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.criar_rota.assert_not_called()
        self.assertEqual(_paradas(rid), [(101, "#PS-9"), (b, "#PS-2")])   # so o CANCELADO saiu
        self.assertEqual(list(resultado["codigos_sem_servico"]), ["#PS-1"])
        self.assertIn("CANCELADO", resultado["codigos_sem_servico"]["#PS-1"])
        self.assertEqual(list(resultado["codigos_pendentes_tecnico"]), ["#PS-2"])
        # o texto nao pode dizer "nenhuma parada descartada": o CANCELADO saiu
        self.assertNotIn("nenhuma parada descartada", resultado["erro"])
        self.assertIn("1 parada(s) recusada(s) saiu(ram) do rascunho", resultado["erro"])
        self.assertIn("#PS-1", resultado["erro"])

    def test_recusa_de_regra_ainda_tira_so_a_parada_recusada(self):
        a, b = self._sem_servico("PS-1"), self._sem_servico("PS-2", status=banco.PEDIDO_CANCELADO)
        rid = _inserir_rascunho([(a, "#PS-1"), (b, "#PS-2")])
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(self._ids_enviados(), [900])
        self.assertEqual(_paradas(rid), [(900, "#PS-1")])

    def test_modo_teste_nao_esvazia_o_rascunho(self):
        a = self._sem_servico("PS-1")
        rid = _inserir_rascunho([(a, "#PS-1")])
        with mock.patch.object(rascunhos_rota, "_carregar_config",
                               return_value={"planejamento": {"fonte_pool": "stokki"}, "modo_teste": True}):
            resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.criar_rota.assert_not_called()
        self.assertEqual(_paradas(rid), [(a, "#PS-1")])

    def test_adicao_em_rota_enviada_com_falha_tecnica_diz_tente_de_novo(self):
        a = self._sem_servico("PS-1")
        self.vuupt.falhar_em = {"PS-1"}
        rid = _inserir_rascunho([(101, "#PS-9")], status="ENVIADO", vuupt_route_id=555)
        with mock.patch("edicao_rota.ler_rota", return_value={"status": "not_started"}), \
                mock.patch("edicao_rota.adicionar_pedidos") as adicionar, \
                mock.patch.object(rascunhos_rota, "_recusar_se_retornando"):
            with self.assertRaises(ValueError) as erro:
                rascunhos_rota.adicionar_em_rota_enviada(rid, self._parada(a, "#PS-1"), "token", "hugo")
        self.assertIn("falha tecnica", str(erro.exception))
        self.assertIn("tente de novo", str(erro.exception))
        adicionar.assert_not_called()
        self.assertEqual(_paradas(rid), [(101, "#PS-9")])

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


class FixRound1EnvioTest(_Base):
    def test_codigo_da_parada_diferente_do_nucleo_casa_pelo_id(self):
        a, b = self._sem_servico("PS-100"), self._sem_servico("PS-200")
        rid = _inserir_rascunho([(a, "#PS-100"), (b, "#PS-999")])   # codigo velho/diferente na parada
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(resultado["codigos_sem_servico"], {})
        self.assertEqual(self._ids_enviados(), [900, 901])

    def test_trava_ocupada_nao_remove_a_parada(self):
        a, b = self._sem_servico("PS-1"), self._sem_servico("PS-2")
        conn = banco.conectar()
        try:
            conn.execute("INSERT INTO nucleo_materializacao VALUES ('PS-2', ?, 'outro clique')", (banco.agora(),))
            conn.commit()
        finally:
            conn.close()
        rid = _inserir_rascunho([(a, "#PS-1"), (b, "#PS-2")])
        with mock.patch.object(materializar, "ESPERA_TRAVA_S", 0.2):
            resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.criar_rota.assert_not_called()
        self.assertIn("outro envio", resultado["codigos_pendentes_tecnico"]["#PS-2"])
        self.assertEqual(resultado["codigos_sem_servico"], {})
        self.assertIn("tente de novo", resultado["erro"])
        self.assertEqual(_paradas(rid), [(900, "#PS-1"), (b, "#PS-2")])   # nada descartado
        self.assertEqual(rascunhos_rota.buscar_rascunho(rid)["status"], rascunhos_rota.STATUS_ERRO_ENVIO)


def _travas():
    conn = rascunhos_rota._conectar()
    try:
        return [tuple(r) for r in conn.execute("SELECT rascunho_id, processo FROM rascunho_envio_trava")]
    finally:
        conn.close()


class EnvioConcorrenteTest(_Base):
    """Revisao final I2: com o envio sincrono (materializar leva minutos) um 2o clique rodava
    enviar_rascunho em paralelo no mesmo rascunho e criava rota parcial/duplicada."""

    def test_segundo_envio_durante_o_primeiro_e_recusado_sem_mexer_em_nada(self):
        a = self._sem_servico("PS-1")
        rid = _inserir_rascunho([(101, "#PS-9"), (a, "#PS-1")])
        segundo = {}

        def criar_rota(token, nome, start_at, sublote, **kw):
            # o 2o clique chega enquanto o 1o ainda esta criando a rota na Vuupt
            chamadas_antes = list(self.vuupt.chamadas)
            segundo["resultado"] = rascunhos_rota.enviar_rascunho(rid, "token")
            segundo["paradas"] = _paradas(rid)
            segundo["rascunho"] = rascunhos_rota.buscar_rascunho(rid)
            segundo["vuupt_igual"] = self.vuupt.chamadas == chamadas_antes
            return {"id": 777}, list(sublote), []

        self.criar_rota.side_effect = criar_rota
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(self.criar_rota.call_count, 1)
        self.assertEqual(segundo["resultado"], {"rascunho_id": rid, "ok": False, "codigos_nao_prontos": [],
                                                "erro": "envio deste rascunho ja em andamento"})
        self.assertEqual(segundo["paradas"], [(101, "#PS-9"), (900, "#PS-1")])
        self.assertEqual(segundo["rascunho"]["status"], rascunhos_rota.STATUS_RASCUNHO)
        self.assertIsNone(segundo["rascunho"]["erro_envio"])
        self.assertTrue(segundo["vuupt_igual"])

    def test_trava_de_outro_processo_recusa_antes_de_tudo(self):
        rid = _inserir_rascunho([(101, "#PS-1")])
        self.assertIsNotNone(rascunhos_rota._travar_envio(rid))
        with mock.patch.object(rascunhos_rota, "buscar_rascunho") as buscar:
            resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["erro"], "envio deste rascunho ja em andamento")
        buscar.assert_not_called()
        self.criar_rota.assert_not_called()

    def test_trava_solta_depois_do_sucesso(self):
        rid = _inserir_rascunho([(101, "#PS-1")])
        self.assertTrue(rascunhos_rota.enviar_rascunho(rid, "token")["ok"])
        self.assertEqual(_travas(), [])

    def test_trava_solta_depois_de_excecao(self):
        rid = _inserir_rascunho([(101, "#PS-1")])
        with mock.patch.object(rascunhos_rota.prontidao_stokki, "carregar_retrato_stokki",
                               side_effect=RuntimeError("explodiu")):
            with self.assertRaises(RuntimeError):
                rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertEqual(_travas(), [])
        self.assertTrue(rascunhos_rota.enviar_rascunho(rid, "token")["ok"])

    def test_trava_velha_de_processo_morto_e_assumida(self):
        rid = _inserir_rascunho([(101, "#PS-1")])
        conn = rascunhos_rota._conectar()
        try:
            conn.execute("INSERT INTO rascunho_envio_trava VALUES (?, datetime('now','localtime','-16 minutes'), "
                         "'pid=1 morto')", (rid,))
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(rascunhos_rota.enviar_rascunho(rid, "token")["ok"])
        self.assertEqual(_travas(), [])

    def test_trava_e_por_rascunho(self):
        r1, r2 = _inserir_rascunho([(101, "#PS-1")]), _inserir_rascunho([(102, "#PS-2")])
        self.assertIsNotNone(rascunhos_rota._travar_envio(r1))
        self.assertTrue(rascunhos_rota.enviar_rascunho(r2, "token")["ok"])


class _Relogio:
    """datetime do rascunhos_rota com o now() controlado pelo teste (o resto e o datetime de verdade)."""

    def __init__(self, inicio):
        from datetime import datetime as _dt
        relogio = self
        self.atual = inicio

        class _DT(_dt):
            @classmethod
            def now(cls, tz=None):
                return relogio.atual
        self.classe = _DT

    def andar(self, minutos):
        from datetime import timedelta
        self.atual += timedelta(minutes=minutos)


class EnvioLongoTest(_Base):
    """Revisao Fable FIX 3: envio com mais de 15 min (429s) perdia a trava para o 2o clique, que depois
    rebaixava o ENVIADO para ERRO_ENVIO e tirava as paradas. Agora: heartbeat na trava, status
    reconferido antes de criar a rota e marcar_erro_envio nunca rebaixa ENVIADO/DESCARTADO."""

    def _status(self, rid):
        r = rascunhos_rota.buscar_rascunho(rid)
        return r["status"], r["vuupt_route_id"], r["erro_envio"]

    def test_marcar_erro_envio_nao_rebaixa_enviado_nem_descartado(self):
        r1 = _inserir_rascunho([(101, "#PS-1")], status="ENVIADO", vuupt_route_id=555)
        r2 = _inserir_rascunho([(102, "#PS-2")], status="DESCARTADO")
        r3 = _inserir_rascunho([(103, "#PS-3")])
        self.assertFalse(rascunhos_rota.marcar_erro_envio(r1, "x"))
        self.assertFalse(rascunhos_rota.marcar_erro_envio(r2, "x"))
        self.assertTrue(rascunhos_rota.marcar_erro_envio(r3, "x"))
        self.assertEqual(self._status(r1), ("ENVIADO", 555, None))
        self.assertEqual(self._status(r2)[0], "DESCARTADO")
        self.assertEqual(self._status(r3), ("ERRO_ENVIO", None, "x"))

    def test_envio_que_acha_tudo_em_outra_rota_deixa_o_enviado_do_outro_intacto(self):
        rid = _inserir_rascunho([(101, "#PS-1"), (102, "#PS-2")])

        def criar_rota(token, nome, start_at, sublote, **kw):
            rascunhos_rota.marcar_enviado(rid, 999)   # o outro envio terminou antes
            return None, [], ["#PS-1", "#PS-2"]
        self.criar_rota.side_effect = criar_rota
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.assertEqual(self._status(rid), ("ENVIADO", 999, None))
        self.assertEqual(_paradas(rid), [(101, "#PS-1"), (102, "#PS-2")])

    def test_rota_parcial_com_rascunho_ja_enviado_pelo_outro_nao_mexe_nas_paradas(self):
        rid = _inserir_rascunho([(101, "#PS-1"), (102, "#PS-2")])

        def criar_rota(token, nome, start_at, sublote, **kw):
            rascunhos_rota.marcar_enviado(rid, 999)
            return {"id": 778}, [sublote[0]], ["#PS-2"]
        self.criar_rota.side_effect = criar_rota
        with self.assertLogs(rascunhos_rota.logger, "ERROR") as logs:
            resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.assertIn("778", resultado["erro"])
        self.assertEqual(self._status(rid), ("ENVIADO", 999, None))
        self.assertEqual(_paradas(rid), [(101, "#PS-1"), (102, "#PS-2")])
        self.assertTrue(any("778" in m for m in logs.output))

    def test_status_mudou_durante_a_materializacao_nao_cria_a_rota(self):
        a = self._sem_servico("PS-1")
        rid = _inserir_rascunho([(a, "#PS-1")])
        original = self.vuupt.criar_ou_atualizar_servico

        def criar(payload):
            conn = rascunhos_rota._conectar()
            try:
                conn.execute("UPDATE rascunhos_rota SET status = 'DESCARTADO' WHERE id = ?", (rid,))
                conn.commit()
            finally:
                conn.close()
            return original(payload)
        self.vuupt.criar_ou_atualizar_servico = criar
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.criar_rota.assert_not_called()
        self.assertIn("DESCARTADO", resultado["erro"])
        self.assertEqual(self._status(rid)[0], "DESCARTADO")

    def test_heartbeat_segura_a_trava_de_envio_longo(self):
        from datetime import datetime as _dt
        relogio = _Relogio(_dt(2026, 11, 27, 10, 0, 0))
        ids = [self._sem_servico(c) for c in ("PS-1", "PS-2", "PS-3")]
        rid = _inserir_rascunho([(i, f"#PS-{n}") for n, i in enumerate(ids, 1)])
        original = self.vuupt.criar_ou_atualizar_servico
        segundo = {}

        def criar_devagar(payload):
            relogio.andar(10)   # cada servico leva 10 min (429 + backoff): o envio passa de 15 min
            if payload["code"] == "#PS-3":
                segundo["resultado"] = rascunhos_rota.enviar_rascunho(rid, "token")
            return original(payload)
        self.vuupt.criar_ou_atualizar_servico = criar_devagar
        with mock.patch.object(rascunhos_rota, "datetime", relogio.classe), \
                mock.patch.object(materializar, "ESPERA_TRAVA_S", 0.1):
            resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertEqual(segundo["resultado"]["erro"], "envio deste rascunho ja em andamento")
        self.assertTrue(resultado["ok"], resultado)
        self.assertEqual(self.criar_rota.call_count, 1)
        self.assertEqual(self._status(rid), ("ENVIADO", 777, None))
        self.assertEqual(_travas(), [])

    def test_trava_assumida_no_meio_para_sem_criar_a_rota(self):
        ids = [self._sem_servico(c) for c in ("PS-1", "PS-2", "PS-3")]
        rid = _inserir_rascunho([(i, f"#PS-{n}") for n, i in enumerate(ids, 1)])
        original = self.vuupt.criar_ou_atualizar_servico

        def criar(payload):
            if payload["code"] == "#PS-2":   # outro processo assumiu a trava (achou que a nossa era velha)
                conn = rascunhos_rota._conectar()
                try:
                    conn.execute("UPDATE rascunho_envio_trava SET processo = 'pid=9 outro' WHERE rascunho_id = ?",
                                 (rid,))
                    conn.commit()
                finally:
                    conn.close()
            return original(payload)
        self.vuupt.criar_ou_atualizar_servico = criar
        resultado = rascunhos_rota.enviar_rascunho(rid, "token")
        self.assertFalse(resultado["ok"])
        self.assertIn("assumido", resultado["erro"])
        self.criar_rota.assert_not_called()
        self.assertEqual(self._status(rid), ("RASCUNHO", None, None))   # o envio do outro decide
        self.assertEqual(_paradas(rid), [(900, "#PS-1"), (901, "#PS-2"), (ids[2], "#PS-3")])
        self.assertEqual(_travas(), [(rid, "pid=9 outro")])               # a trava do outro fica


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

    def test_adicao_com_codigo_diferente_do_nucleo_nao_estoura_depois_de_criar(self):
        a = self._sem_servico("PS-100")
        rid = _inserir_rascunho([(101, "#PS-9")], status="ENVIADO", vuupt_route_id=555)
        resultado = rascunhos_rota.adicionar_em_rota_enviada(rid, self._parada(a, "#PS-999"), "token", "hugo")
        self.assertEqual(resultado["service_id"], 900)
        self.assertEqual(self.adicionar.call_args.args[2], [900])

    def test_adicao_recusa_com_motivo_quando_nao_materializa(self):
        a = self._sem_servico("PS-1-R1")
        rid = _inserir_rascunho([(101, "#PS-9")], status="ENVIADO", vuupt_route_id=555)
        with self.assertRaises(ValueError) as erro:
            rascunhos_rota.adicionar_em_rota_enviada(rid, self._parada(a, "#PS-1-R1"), "token", "hugo")
        self.assertIn("reentrega", str(erro.exception))
        self.adicionar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
