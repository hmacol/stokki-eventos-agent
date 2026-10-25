# -*- coding: utf-8 -*-
"""
test_eventos_freshhub.py -- o legado registra no sistema novo o que decide no planejamento (roteirizado,
nao_aprontou, rota_enviada; plano 1.2, decisao 5). Falha da API nao derruba a rodada: o lote fica num
arquivo pendente e vai na proxima chamada que der certo (idempotente por codigo+tipo+ocorrido_em na API).

    py -3.11 -m unittest nucleo.test_eventos_freshhub -v
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import eventos_freshhub as ev  # noqa: E402
from nucleo.freshhub_client import ErroFreshhub  # noqa: E402

CONFIG = {"freshhub_api": {"url": "http://127.0.0.1:8100", "token": "fhs_x"}}


class TestEvento(unittest.TestCase):
    def test_evento_leva_codigo_normalizado_e_hora_com_fuso(self):
        e = ev.evento("#PS-41679", "roteirizado", {"rascunho_id": 5})
        self.assertEqual((e["codigo"], e["tipo"], e["dados"]), ("PS-41679", "roteirizado", {"rascunho_id": 5}))
        quando = datetime.fromisoformat(e["ocorrido_em"])
        self.assertIsNotNone(quando.tzinfo)
        self.assertEqual(quando.utcoffset().total_seconds(), -3 * 3600)  # Sao Paulo
        fixo = datetime(2026, 10, 25, 9, 30, tzinfo=ev.SP)
        self.assertEqual(ev.evento("PS-1", "nao_aprontou", None, ocorrido_em=fixo)["ocorrido_em"],
                         "2026-10-25T09:30:00-03:00")

    def test_tipo_fora_do_contrato_e_erro(self):
        with self.assertRaises(ValueError):
            ev.evento("PS-1", "retornou_ao_galpao")


class TestRegistrar(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.pendentes = Path(self._tmp.name) / "pendentes.jsonl"
        mock.patch.object(ev, "PENDENTES", self.pendentes).start()
        self.cliente = mock.Mock()
        self.cliente.eventos.return_value = {"aceitos": 1, "repetidos": 0, "desconhecidos": []}
        mock.patch.object(ev.freshhub_client, "de_config", return_value=self.cliente).start()
        self.addCleanup(mock.patch.stopall)
        self.lote = [ev.evento("PS-1", "roteirizado", {"rascunho_id": 5})]

    def test_envia_o_lote_e_devolve_a_resposta(self):
        self.assertEqual(ev.registrar(CONFIG, self.lote)["aceitos"], 1)
        self.cliente.eventos.assert_called_once_with(self.lote)
        self.assertFalse(self.pendentes.exists())

    def test_modo_teste_e_lista_vazia_nao_mandam_nada(self):
        self.assertIsNone(ev.registrar(CONFIG, self.lote, modo_teste=True))
        self.assertIsNone(ev.registrar(CONFIG, []))
        self.cliente.eventos.assert_not_called()

    def test_sem_freshhub_api_no_config_nao_manda_nem_guarda(self):
        with mock.patch.object(ev.freshhub_client, "de_config", return_value=None):
            self.assertIsNone(ev.registrar({}, self.lote))
        self.assertFalse(self.pendentes.exists())

    def test_falha_guarda_o_lote_e_a_proxima_chamada_manda_os_pendentes_primeiro(self):
        self.cliente.eventos.side_effect = ErroFreshhub("GET: HTTP 503")
        with self.assertLogs("nucleo.eventos_freshhub", level="ERROR"):
            self.assertIsNone(ev.registrar(CONFIG, self.lote))
        guardado = [json.loads(linha) for linha in self.pendentes.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(guardado, self.lote)
        # a API voltou: pendentes vao antes do lote novo, e o arquivo e apagado
        self.cliente.eventos.side_effect = None
        novo = [ev.evento("PS-2", "rota_enviada", {"vuupt_route_id": 9})]
        self.assertEqual(ev.registrar(CONFIG, novo)["aceitos"], 1)
        self.assertEqual([c.args[0] for c in self.cliente.eventos.call_args_list[1:]], [self.lote, novo])
        self.assertFalse(self.pendentes.exists())

    def test_pendentes_que_falham_de_novo_continuam_guardados_sem_duplicar(self):
        self.cliente.eventos.side_effect = ErroFreshhub("fora")
        with self.assertLogs("nucleo.eventos_freshhub", level="ERROR"):
            ev.registrar(CONFIG, self.lote)
            ev.registrar(CONFIG, [ev.evento("PS-3", "nao_aprontou", {})])
        linhas = self.pendentes.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(x)["codigo"] for x in linhas], ["PS-1", "PS-3"])

    def test_atalhos_montam_os_tres_tipos(self):
        paradas = [{"codigo": "#PS-1", "service_id": 11}, {"codigo": "#PS-2", "service_id": 12}]
        ev.roteirizados(CONFIG, paradas, rascunho_id=5, rota_nome="Planejamento - 27/10/2026 - #1",
                        data_alvo="2026-10-27")
        enviados = self.cliente.eventos.call_args.args[0]
        self.assertEqual([(e["codigo"], e["tipo"]) for e in enviados], [("PS-1", "roteirizado"), ("PS-2", "roteirizado")])
        self.assertEqual(enviados[0]["dados"], {"rascunho_id": 5, "rota_nome": "Planejamento - 27/10/2026 - #1",
                                                "data": "2026-10-27", "service_id": 11})
        ev.nao_aprontaram(CONFIG, [{"codigo": "#PS-2", "service_id": 12, "situacao_stokki": "Em conferência"}],
                          contexto={"rascunho_id": 5, "origem": "envio"})
        e = self.cliente.eventos.call_args.args[0][0]
        self.assertEqual((e["tipo"], e["dados"]), ("nao_aprontou", {"rascunho_id": 5, "origem": "envio",
                                                                   "situacao_stokki": "Em conferência",
                                                                   "service_id": 12}))
        ev.rota_enviada(CONFIG, [{"codigo": "#PS-1", "service_id": 11}], vuupt_route_id=777, rascunho_id=5,
                        rota_nome="R")
        e = self.cliente.eventos.call_args.args[0][0]
        self.assertEqual((e["tipo"], e["dados"]), ("rota_enviada", {"vuupt_route_id": 777, "rascunho_id": 5,
                                                                   "rota_nome": "R", "service_id": 11}))


class TestRevisaoLegado(unittest.TestCase):
    """Revisao independente (25/10): a fila de pendentes nao pode se envenenar nem travar o canal."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.pendentes = Path(self._tmp.name) / "pendentes.jsonl"
        mock.patch.object(ev, "PENDENTES", self.pendentes).start()
        self.cliente = mock.Mock()
        self.cliente.eventos.return_value = {"aceitos": 1, "repetidos": 0, "desconhecidos": []}
        mock.patch.object(ev.freshhub_client, "de_config", return_value=self.cliente).start()
        self.addCleanup(mock.patch.stopall)

    def _guardar(self, n, prefixo="PS-"):
        with self.pendentes.open("a", encoding="utf-8") as f:
            for i in range(n):
                f.write(json.dumps(ev.evento(f"{prefixo}{i}", "roteirizado", {})) + "\n")

    def test_pendentes_vao_em_fatias_de_ate_500(self):
        self._guardar(501)
        novo = [ev.evento("PS-N", "rota_enviada", {})]
        self.assertEqual(ev.registrar(CONFIG, novo)["aceitos"], 1)
        tamanhos = [len(c.args[0]) for c in self.cliente.eventos.call_args_list]
        self.assertEqual(tamanhos, [500, 1, 1])
        self.assertFalse(self.pendentes.exists())

    def test_lote_recusado_pela_api_e_descartado_com_log_nao_guardado(self):
        # 4xx nao melhora repetindo (422 de validacao, 401 de token): guardar travaria tudo que vem depois
        self._guardar(2)
        self.cliente.eventos.side_effect = [ErroFreshhub("POST /api/eventos: HTTP 422 ...", status=422),
                                            {"aceitos": 1, "repetidos": 0, "desconhecidos": []}]
        with self.assertLogs("nucleo.eventos_freshhub", level="ERROR") as logs:
            r = ev.registrar(CONFIG, [ev.evento("PS-N", "rota_enviada", {})])
        self.assertEqual(r["aceitos"], 1)
        self.assertIn("descartado", logs.output[0])
        self.assertFalse(self.pendentes.exists())
        # o lote NOVO recusado com 4xx tambem nao e guardado
        self.cliente.eventos.side_effect = ErroFreshhub("POST: HTTP 401", status=401)
        with self.assertLogs("nucleo.eventos_freshhub", level="ERROR"):
            self.assertIsNone(ev.registrar(CONFIG, [ev.evento("PS-X", "rota_enviada", {})]))
        self.assertFalse(self.pendentes.exists())
        # 5xx e rede continuam guardando
        self.cliente.eventos.side_effect = ErroFreshhub("POST: HTTP 503", status=503)
        with self.assertLogs("nucleo.eventos_freshhub", level="ERROR"):
            ev.registrar(CONFIG, [ev.evento("PS-Y", "rota_enviada", {})])
        self.assertEqual(len(self.pendentes.read_text(encoding="utf-8").splitlines()), 1)

    def test_linha_corrompida_e_pulada_e_as_validas_vao(self):
        self._guardar(1)
        with self.pendentes.open("a", encoding="utf-8") as f:
            f.write("isso nao e json\n")
        self._guardar(1, prefixo="PS-B")
        ev.registrar(CONFIG, [ev.evento("PS-N", "rota_enviada", {})])
        primeiro = self.cliente.eventos.call_args_list[0].args[0]
        self.assertEqual([e["codigo"] for e in primeiro], ["PS-0", "PS-B0"])
        self.assertFalse(self.pendentes.exists())

    def test_codigo_combinado_vira_um_evento_por_pedido(self):
        paradas = [{"codigo": "#PS-1, #PS-2", "service_id": 11}, {"codigo": "#PS-3-R1", "service_id": 12}]
        ev.roteirizados(CONFIG, paradas, rascunho_id=5, rota_nome="R", data_alvo="2026-10-27")
        enviados = self.cliente.eventos.call_args.args[0]
        self.assertEqual([e["codigo"] for e in enviados], ["PS-1", "PS-2", "PS-3-R1"])
        self.assertEqual([e["dados"]["service_id"] for e in enviados], [11, 11, 12])
        self.assertEqual(enviados[0]["dados"]["codigo_vuupt"], "PS-1, PS-2")
        self.assertNotIn("codigo_vuupt", enviados[2]["dados"])


if __name__ == "__main__":
    unittest.main()
