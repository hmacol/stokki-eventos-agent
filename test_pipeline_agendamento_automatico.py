# -*- coding: utf-8 -*-
"""
Data automatica na criacao do servico (pipeline). Rodar (da raiz):
py -3.11 -m unittest test_pipeline_agendamento_automatico -v
"""
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import pipeline


class TestAgendamentoAutomatico(unittest.TestCase):
    INICIO = datetime(2026, 11, 9, 15, 5)

    def test_servico_novo_ganha_data_e_horario(self):
        r = pipeline.agendamento_automatico(None, None, "123", "", self.INICIO, {"123": ("07:00", "10:00")}, {})
        self.assertEqual(r, (pipeline._converter_data_para_iso("11/11/2026 07:00"),
                             pipeline._converter_data_para_iso("11/11/2026 10:00")))

    def test_servico_que_ja_existe_nao_ganha_nem_muda(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            for existente in ({"id": 1, "scheduled_start": None},
                              {"id": 1, "scheduled_start": "2026-11-10 03:00:00",
                               "scheduled_end": "2026-11-10 13:00:00"}):
                self.assertIsNone(pipeline.agendamento_automatico(
                    existente, None, "123", "", self.INICIO, {}, {}, db_path=db))

    def test_conversao_vazia_devolve_none(self):
        with mock.patch.object(pipeline, "_converter_data_para_iso", return_value=""):
            self.assertIsNone(pipeline.agendamento_automatico(None, None, "123", "", self.INICIO, {}, {}))

    def test_antes_de_03_11_ou_sem_inicio_nao_age(self):
        self.assertIsNone(pipeline.agendamento_automatico(None, None, "1", "", datetime(2026, 11, 2, 22), {}, {}))
        self.assertIsNone(pipeline.agendamento_automatico(None, None, "1", "", None, {}, {}))

    def test_registra_origem_automatica(self):
        from roteirizacao import registro_dia_fixo as reg
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            servico = {"id": 9, "code": "#PS-1"}
            n = pipeline.registrar_data_automatica(servico, "2026-11-11T07:00:00-03:00", db_path=db)
            self.assertEqual(n, 1)
            conn = reg.conectar(db)
            try:
                self.assertTrue(reg.data_e_automatica(conn, servico, date(2026, 11, 11)))
                self.assertFalse(reg.data_e_automatica(conn, servico, date(2026, 11, 12)))
            finally:
                conn.close()

    def test_registro_tolerante_a_data_invalida(self):
        self.assertEqual(pipeline.registrar_data_automatica({"id": 1}, "lixo"), 0)


def _detalhe():
    return {"destino": {"documento": "123", "nome": "DEST", "telefone": "11999990000"},
            "local_entrega": {}, "itens": [], "referencia": "NF1", "stkkc_id": 1,
            "transportadora": {}, "volumes": 1}


def _res_end():
    return mock.Mock(endereco={"logradouro": "RUA A 1", "bairro": "CENTRO", "cidade": "Sao Paulo", "uf": "SP", "cep": "01001-000"}, horario_entrega=None,
                     fonte="destino", requer_revisao=False, observacao="", transportadora={})


class ProcessarPedidoAutomaticoTest(unittest.TestCase):
    """Caminho completo de processar_pedido com as dependencias pesadas trocadas por mocks."""
    INICIO = datetime(2026, 11, 9, 15, 5)  # depois do corte -> 11/11

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "t.db"
        patches = [
            mock.patch("roteirizacao.registro_dia_fixo.DB_PATH", self.db),
            mock.patch.object(pipeline, "ja_confirmado_atribuido", return_value=False),
            mock.patch.object(pipeline.stokki_pedidos, "obter_detalhe", return_value=_detalhe()),
            mock.patch.object(pipeline, "resolver_endereco_entrega", return_value=_res_end()),
            mock.patch.object(pipeline, "config_consulta", return_value={"ativo": False}),
            mock.patch.object(pipeline, "_buscar_dados_embarcador_banco", return_value={}),
            mock.patch.object(pipeline, "buscar_confirmacao", return_value=None),
            mock.patch.object(pipeline, "tem_agendamento", return_value=False),
            mock.patch.object(pipeline, "geocodificar", return_value=None),
            mock.patch("nucleo.pedidos.registrar_importacao"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _vuupt(self, existente=None, acao="criado"):
        v = mock.Mock()
        v.STATUS_ATUALIZAVEL = "not_assigned"
        v.buscar_servico_por_code.return_value = existente
        v.skill_ids_por_nome.return_value = []
        v.criar_ou_atualizar_servico.return_value = ({"service": {"id": 5, "code": "#PS-1"}}, acao)
        return v

    def _processar(self, vuupt, modo_teste=False):
        res = pipeline.processar_pedido(
            41679, "PS-1", sess_stokki=mock.Mock(), catalogo=mock.Mock(), vuupt=vuupt,
            anthropic_api_key="", modo_teste=modo_teste, mapa_horarios={"123": ("07:00", "10:00")},
            ajustes_manuais={}, inicio_rodada=self.INICIO)
        self.assertIsNone(res["erro"])
        return res

    def _origem(self, servico, data):
        from roteirizacao import registro_dia_fixo as reg
        conn = reg.conectar(self.db)
        try:
            return reg.data_e_automatica(conn, servico, data)
        finally:
            conn.close()

    def test_servico_novo_payload_com_data_e_origem(self):
        vuupt = self._vuupt()
        res = self._processar(vuupt)
        payload = vuupt.criar_ou_atualizar_servico.call_args[0][0]
        self.assertTrue(payload["scheduled_start"].startswith("2026-11-11T07:00"))
        self.assertFalse(res["tem_agendamento"])
        self.assertEqual(res["fonte_agendamento"], pipeline.FONTE_PROXIMO_DIA_UTIL)
        self.assertTrue(self._origem({"id": 5, "code": "#PS-1"}, date(2026, 11, 11)))

    def test_origem_registrada_com_a_data_depois_do_dia_fixo(self):
        vuupt = self._vuupt()
        ajuste = mock.Mock(return_value=(date(2026, 11, 12), {"nome": "R", "dias": [3]}))
        with mock.patch("roteirizacao.regioes_dia_fixo.ajustar_data_por_dia_fixo", ajuste), \
                mock.patch("roteirizacao.regioes_dia_fixo.descricao_dias", return_value="qui"):
            self._processar(vuupt)
        payload = vuupt.criar_ou_atualizar_servico.call_args[0][0]
        self.assertTrue(payload["scheduled_start"].startswith("2026-11-12"))
        self.assertTrue(self._origem({"id": 5, "code": "#PS-1"}, date(2026, 11, 12)))
        self.assertFalse(self._origem({"id": 5, "code": "#PS-1"}, date(2026, 11, 11)))

    def test_modo_teste_nao_registra_nada(self):
        vuupt = self._vuupt()
        res = self._processar(vuupt, modo_teste=True)
        self.assertEqual(res["acao"], "simulado")
        vuupt.criar_ou_atualizar_servico.assert_not_called()
        self.assertFalse(self.db.exists())

    def test_servico_existente_com_data_automatica_repete_a_mesma_data(self):
        pipeline.registrar_data_automatica({"id": 5, "code": "#PS-1"}, "2026-11-11T07:00:00-03:00", db_path=self.db)
        # a Vuupt devolve UTC sem fuso: 07:00 local = 10:00 UTC
        existente = {"id": 5, "code": "#PS-1", "status": "not_assigned",
                     "scheduled_start": "2026-11-11 10:00:00", "scheduled_end": "2026-11-11 13:00:00"}
        vuupt = self._vuupt(existente, acao="pulado_sem_alteracao")
        with mock.patch.object(pipeline, "registrar_data_automatica") as reg:
            self._processar(vuupt)
        payload = vuupt.criar_ou_atualizar_servico.call_args[0][0]
        self.assertEqual(payload["scheduled_start"], pipeline._converter_data_para_iso("11/11/2026 07:00"))
        self.assertEqual(payload["scheduled_end"], pipeline._converter_data_para_iso("11/11/2026 10:00"))
        reg.assert_not_called()

    def test_servico_existente_sem_data_ou_data_nao_automatica_sai_sem_data(self):
        for ini, fim in ((None, None), ("2026-11-11 10:00:00", "2026-11-11 13:00:00")):
            existente = {"id": 5, "code": "#PS-1", "status": "not_assigned",
                         "scheduled_start": ini, "scheduled_end": fim}
            vuupt = self._vuupt(existente, acao="atualizado")
            self._processar(vuupt)
            payload = vuupt.criar_ou_atualizar_servico.call_args[0][0]
            self.assertFalse(payload.get("scheduled_start"))

    def test_excecao_na_regra_nova_nao_impede_a_criacao(self):
        vuupt = self._vuupt()
        with mock.patch.object(pipeline, "agendamento_automatico", side_effect=RuntimeError("boom")):
            res = self._processar(vuupt)
        vuupt.criar_ou_atualizar_servico.assert_called_once()
        self.assertEqual(res["acao"], "criado")
        self.assertEqual(res["fonte_agendamento"], "nao_exigido")
        self.assertFalse(vuupt.criar_ou_atualizar_servico.call_args[0][0].get("scheduled_start"))


if __name__ == "__main__":
    unittest.main()
