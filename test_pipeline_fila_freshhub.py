# -*- coding: utf-8 -*-
"""Fila de replica do sistema novo atendida pelo pipeline (plano 1.2 do freshhub, Task 6): pra cada
pendente destino vuupt roda o mesmo processar_pedido (payload, geocodificacao, registrar_importacao) e
devolve o id do servico; erro de um item nao derruba o lote; --modo-teste nao cria nem devolve.

    py -3.11 -m unittest test_pipeline_fila_freshhub -v
"""
import unittest
from unittest import mock

import pipeline
from nucleo.freshhub_client import ErroFreshhub

SERVICO = {"id": 777, "code": "#PS-41679", "title": "#PS-41679 - 1 / EMB / DEST", "status": "not_assigned",
           "sender_id": 4321,
           "address": "RUA A 1, CENTRO, Sao Paulo - SP, 01001-000, Brasil", "address_complement": None,
           "latitude": -23.5, "longitude": -46.6, "dimension_3": 2, "note": None, "customer_id": 99,
           "scheduled_start": "2026-10-27 12:00:00", "scheduled_end": None,
           "created_at": "2026-10-25 09:00:00", "updated_at": "2026-10-25 09:00:00",
           "recreated_order_origin_id": None,
           "customer": {"name": "DEST", "code": "12345678000199", "phone_number": "+5511999990000",
                        "operating_hour_start": "08:00", "operating_hour_end": "17:00"}}


def _item(id_="r1", id_stokki=41679, codigo="PS-41679"):
    return {"id": id_, "codigo": codigo, "destino": "vuupt", "tentativas": 0,
            "dados": {"id_stokki": id_stokki, "codigo": codigo}}


def _res(acao="criado", sid=777, servico=SERVICO, erro=None, observacao=""):
    return {"codigo_ps": "PS-41679", "acao": acao, "erro": erro, "observacao": observacao,
            "vuupt_service_id": sid, "servico_vuupt": servico}


class AtenderFilaTest(unittest.TestCase):
    def test_cria_o_servico_e_devolve_o_id_com_o_espelho(self):
        cliente = mock.Mock()
        cliente.pendentes.return_value = [_item()]
        processar = mock.Mock(return_value=_res())
        saida = pipeline.atender_fila_freshhub(cliente, processar, modo_teste=False)
        cliente.pendentes.assert_called_once_with("vuupt", limite=50)
        processar.assert_called_once_with(41679, "PS-41679")
        cliente.resultado.assert_called_once()
        args, kwargs = cliente.resultado.call_args
        self.assertEqual(args, ("r1", True))
        resultado = kwargs["resultado"]
        self.assertEqual((resultado["vuupt_service_id"], resultado["acao"]), (777, "criado"))
        self.assertEqual(resultado["servico"]["title"], SERVICO["title"])
        self.assertEqual(resultado["servico"]["customer_code"], "12345678000199")
        self.assertEqual(resultado["servico"]["operating_hour_start"], "08:00")
        self.assertEqual(saida, [{"id": "r1", "codigo": "PS-41679", "ok": True, "vuupt_service_id": 777,
                                  "acao": "criado"}])

    def test_servico_ja_existente_devolve_o_id_sem_criar(self):
        # Focus 2: a Stokki liberou e o pipeline normal (ou a Vuupt) ja tem o servico; so devolve o id
        cliente = mock.Mock()
        cliente.pendentes.return_value = [_item()]
        processar = mock.Mock(return_value=_res(acao="pulado_atribuido", sid=500, servico={**SERVICO, "id": 500}))
        pipeline.atender_fila_freshhub(cliente, processar, modo_teste=False)
        args, kwargs = cliente.resultado.call_args
        self.assertEqual(args, ("r1", True))
        self.assertEqual(kwargs["resultado"]["vuupt_service_id"], 500)

    def test_erro_de_um_item_nao_derruba_o_lote(self):
        cliente = mock.Mock()
        cliente.pendentes.return_value = [_item("r1"), _item("r2", 2, "PS-2"), _item("r3", 3, "PS-3")]
        processar = mock.Mock(side_effect=[
            RuntimeError("Stokki: situacao invalida"),
            _res(acao="criado", sid=1),
            _res(acao="pulado_cancelado_vuupt", sid=None, servico=None, observacao="Servico CANCELADO na VUUPT"),
        ])
        saida = pipeline.atender_fila_freshhub(cliente, processar, modo_teste=False)
        chamadas = cliente.resultado.call_args_list
        self.assertEqual([c.args for c in chamadas], [("r1", False), ("r2", True), ("r3", False)])
        self.assertIn("Stokki: situacao invalida", chamadas[0].kwargs["erro"])
        self.assertIn("pulado_cancelado_vuupt", chamadas[2].kwargs["erro"])
        self.assertEqual([s["ok"] for s in saida], [False, True, False])

    def test_item_sem_id_stokki_volta_como_erro_sem_processar(self):
        cliente = mock.Mock()
        cliente.pendentes.return_value = [{"id": "r9", "codigo": "PS-9", "destino": "vuupt", "dados": {}}]
        processar = mock.Mock()
        pipeline.atender_fila_freshhub(cliente, processar, modo_teste=False)
        processar.assert_not_called()
        args, kwargs = cliente.resultado.call_args
        self.assertEqual(args, ("r9", False))
        self.assertIn("id_stokki", kwargs["erro"])

    def test_modo_teste_simula_e_nao_devolve_resultado(self):
        cliente = mock.Mock()
        cliente.pendentes.return_value = [_item()]
        processar = mock.Mock(return_value=_res(acao="simulado", sid=None, servico=None))
        saida = pipeline.atender_fila_freshhub(cliente, processar, modo_teste=True)
        processar.assert_called_once_with(41679, "PS-41679")
        cliente.resultado.assert_not_called()
        self.assertEqual(saida[0]["acao"], "simulado")

    def test_api_do_freshhub_fora_so_loga(self):
        cliente = mock.Mock()
        cliente.pendentes.side_effect = ErroFreshhub("GET /api/replicacao/pendentes: HTTP 503")
        with self.assertLogs(pipeline.logger, level="ERROR") as logs:
            saida = pipeline.atender_fila_freshhub(cliente, mock.Mock(), modo_teste=False)
        self.assertEqual(saida, [])
        self.assertIn("fila do freshhub", logs.output[0])

    def test_sem_cliente_configurado_nao_faz_nada(self):
        self.assertEqual(pipeline.atender_fila_freshhub(None, mock.Mock(), modo_teste=False), [])


class EspelhoTest(unittest.TestCase):
    def test_achata_o_cliente_e_mantem_as_datas_como_a_vuupt_devolve(self):
        espelho = pipeline._espelho_para_freshhub(SERVICO)
        self.assertEqual(espelho, {
            "title": SERVICO["title"], "address": SERVICO["address"], "address_complement": None,
            "latitude": -23.5, "longitude": -46.6, "dimension_3": 2, "note": None, "customer_id": 99,
            "phone_number": "+5511999990000", "customer_name": "DEST", "customer_code": "12345678000199",
            "operating_hour_start": "08:00", "operating_hour_end": "17:00",
            "scheduled_start": "2026-10-27 12:00:00", "scheduled_end": None,
            "created_at": "2026-10-25 09:00:00", "updated_at": "2026-10-25 09:00:00",
            "recreated_order_origin_id": None, "sender_id": 4321,
        })
        self.assertIsNone(pipeline._espelho_para_freshhub(None))
        self.assertIsNone(pipeline._espelho_para_freshhub({}))


class ProcessarPedidoDevolveServicoTest(unittest.TestCase):
    def test_pulado_atribuido_traz_o_servico_existente(self):
        vuupt = mock.Mock()
        vuupt.STATUS_ATUALIZAVEL = "not_assigned"
        vuupt.buscar_servico_por_code.return_value = {"id": 500, "status": "assigned", "title": "x"}
        with mock.patch.object(pipeline, "ja_confirmado_atribuido", return_value=False), \
                mock.patch.object(pipeline, "marcar_atribuido"):
            res = pipeline.processar_pedido(41679, "PS-41679", sess_stokki=mock.Mock(), catalogo=mock.Mock(),
                                            vuupt=vuupt, anthropic_api_key="", modo_teste=False)
        self.assertEqual(res["acao"], "pulado_atribuido")
        self.assertEqual(res["vuupt_service_id"], 500)
        self.assertEqual(res["servico_vuupt"]["id"], 500)

    def test_pulado_pelo_fingerprint_fica_sem_servico(self):
        with mock.patch.object(pipeline, "ja_confirmado_atribuido", return_value=True):
            res = pipeline.processar_pedido(41679, "PS-41679", sess_stokki=mock.Mock(), catalogo=mock.Mock(),
                                            vuupt=mock.Mock(), anthropic_api_key="", modo_teste=False)
        self.assertEqual(res["acao"], "pulado_atribuido")
        self.assertIsNone(res["vuupt_service_id"])


class RevisaoFilaTest(unittest.TestCase):
    """Revisao independente (25/10): I1 servico ja existente sem alteracao, I2 falha no POST do resultado."""

    def test_pulado_sem_alteracao_busca_o_servico_existente_e_devolve_ok(self):
        # criar_ou_atualizar_servico devolve (None, "pulado_sem_alteracao") quando o pipeline normal acabou de criar
        cliente = mock.Mock()
        cliente.pendentes.return_value = [_item()]
        processar = mock.Mock(return_value=_res(acao="pulado_sem_alteracao", sid=None, servico=None))
        buscar = mock.Mock(return_value={**SERVICO, "id": 500})
        saida = pipeline.atender_fila_freshhub(cliente, processar, modo_teste=False, buscar_servico=buscar)
        buscar.assert_called_once_with("PS-41679")
        args, kwargs = cliente.resultado.call_args
        self.assertEqual(args, ("r1", True))
        self.assertEqual(kwargs["resultado"]["vuupt_service_id"], 500)
        self.assertEqual(kwargs["resultado"]["servico"]["title"], SERVICO["title"])
        self.assertEqual(saida[0]["vuupt_service_id"], 500)
        # sem servico na Vuupt mesmo buscando: erro de verdade
        buscar.return_value = None
        cliente.resultado.reset_mock()
        pipeline.atender_fila_freshhub(cliente, processar, modo_teste=False, buscar_servico=buscar)
        self.assertEqual(cliente.resultado.call_args.args, ("r1", False))

    def test_falha_ao_devolver_o_resultado_nao_derruba_o_lote(self):
        cliente = mock.Mock()
        cliente.pendentes.return_value = [_item("r1"), _item("r2", 2, "PS-2")]
        cliente.resultado.side_effect = [ErroFreshhub("POST: HTTP 503", status=503), None]
        processar = mock.Mock(return_value=_res())
        with self.assertLogs(pipeline.logger, level="ERROR") as logs:
            saida = pipeline.atender_fila_freshhub(cliente, processar, modo_teste=False)
        self.assertEqual(cliente.resultado.call_count, 2)
        self.assertEqual([s["ok"] for s in saida], [False, True])
        self.assertIn("r1", logs.output[0])


if __name__ == "__main__":
    unittest.main()
