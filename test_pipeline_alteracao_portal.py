"""Testes de pipeline.aplicar_alteracoes_do_cliente (sem rede; banco em memoria)."""
import contextlib
import sqlite3
import unittest
from unittest import mock

import pipeline

NF = {"logradouro": "Rua da NF, 1", "complemento": "", "bairro": "Centro NF",
      "cidade": "Sao Paulo", "uf": "SP", "cep": "01000-000"}
HOR_NF = {"inicio": "08:00", "fim": "12:00"}
DOC = "12.345.678/0001-90"
VIGENTE = {"endereco": {"logradouro": "Av Nova", "numero": "250", "complemento": "sala 3",
                        "bairro": "Bela Vista", "municipio": "Campinas", "uf": "SP",
                        "cep": "13000-000", "cmun": "3509502"},
           "latitude": -22.9, "longitude": -47.06}


class AplicarAlteracoesDoClienteTest(unittest.TestCase):
    def test_endereco_do_cliente_vence_o_da_nf(self):
        with mock.patch.object(pipeline.alteracoes_entrega, "endereco_vigente", return_value=VIGENTE):
            end, hor, coords = pipeline.aplicar_alteracoes_do_cliente(
                "PS-1234567", 77, DOC, NF, HOR_NF, conn=object(), ajustes={})
        self.assertEqual(end["logradouro"], "Av Nova, 250")
        self.assertEqual(end["cidade"], "Campinas")
        self.assertEqual(end["bairro"], "Bela Vista")
        self.assertEqual(end["complemento"], "sala 3")
        self.assertEqual(end["cep"], "13000-000")
        self.assertEqual(coords, (-22.9, -47.06))
        self.assertEqual(hor, HOR_NF)
        self.assertEqual(NF["logradouro"], "Rua da NF, 1")  # nao muta o original

    def test_sem_alteracao_mantem_nf(self):
        with mock.patch.object(pipeline.alteracoes_entrega, "endereco_vigente", return_value=None):
            end, hor, coords = pipeline.aplicar_alteracoes_do_cliente(
                "PS-1234567", 77, DOC, NF, HOR_NF, conn=object(), ajustes={})
        self.assertEqual((end, hor, coords), (NF, HOR_NF, None))

    def test_sem_sender_id_mantem_endereco_da_nf(self):
        with mock.patch.object(pipeline.alteracoes_entrega, "endereco_vigente", return_value=VIGENTE) as m:
            end, _, coords = pipeline.aplicar_alteracoes_do_cliente(
                "PS-1234567", None, DOC, NF, HOR_NF, conn=object(), ajustes={})
        m.assert_not_called()
        self.assertEqual((end, coords), (NF, None))

    def test_ajuste_manual_vence_horario_extraido(self):
        ajustes = {"12345678000190": {"nivel_dificuldade": 2, "horario_atendimento_inicio": "09:00",
                                      "horario_atendimento_fim": "17:00"}}
        with mock.patch.object(pipeline.alteracoes_entrega, "endereco_vigente", return_value=None):
            _, hor, _ = pipeline.aplicar_alteracoes_do_cliente(
                "PS-1234567", 77, DOC, NF, HOR_NF, conn=object(), ajustes=ajustes)
        self.assertEqual(hor, {"inicio": "09:00", "fim": "17:00"})
        self.assertEqual(HOR_NF, {"inicio": "08:00", "fim": "12:00"})

    def test_tabela_ausente_nao_derruba(self):
        with mock.patch.object(pipeline.alteracoes_entrega, "endereco_vigente",
                               side_effect=sqlite3.OperationalError("no such table")), \
             mock.patch.object(pipeline, "carregar_ajustes_manuais",
                               side_effect=sqlite3.OperationalError("no such table")), \
             self.assertLogs(pipeline.logger, level="WARNING"):
            end, hor, coords = pipeline.aplicar_alteracoes_do_cliente(
                "PS-1234567", 77, DOC, NF, HOR_NF, conn=object())
        self.assertEqual((end, hor, coords), (NF, HOR_NF, None))

    def test_fecha_a_conexao_que_abriu_sozinha(self):
        conn = mock.Mock()
        with mock.patch.object(pipeline.alteracoes_entrega, "conectar", return_value=conn),              mock.patch.object(pipeline.alteracoes_entrega, "endereco_vigente", return_value=None):
            pipeline.aplicar_alteracoes_do_cliente("PS-1234567", 77, DOC, NF, HOR_NF, ajustes={})
        conn.close.assert_called_once()

    def test_nao_fecha_conexao_recebida(self):
        conn = mock.Mock()
        with mock.patch.object(pipeline.alteracoes_entrega, "endereco_vigente", return_value=None):
            pipeline.aplicar_alteracoes_do_cliente("PS-1234567", 77, DOC, NF, HOR_NF, conn=conn, ajustes={})
        conn.close.assert_not_called()

    def test_conexao_propria_falha_nao_derruba(self):
        with mock.patch.object(pipeline.alteracoes_entrega, "conectar", side_effect=OSError("sem banco")), \
             mock.patch.object(pipeline, "carregar_ajustes_manuais", return_value={}), \
             self.assertLogs(pipeline.logger, level="WARNING"):
            end, hor, coords = pipeline.aplicar_alteracoes_do_cliente("PS-1234567", 77, DOC, NF, HOR_NF)
        self.assertEqual((end, hor, coords), (NF, HOR_NF, None))


class ProcessarPedidoUsaAlteracaoDoClienteTest(unittest.TestCase):
    def test_payload_recebe_endereco_horario_e_coordenadas_do_cliente(self):
        from types import SimpleNamespace
        res_end = SimpleNamespace(
            endereco=dict(NF), horario_entrega=dict(HOR_NF), fonte="destino", requer_revisao=False,
            observacao="", transportadora={"tipo": "ENTREGA", "nome": ""})
        detalhe = {"destino": {"documento": DOC, "nome": "Dest"}, "referencia": "1", "stkkc_id": 5}
        vuupt = mock.Mock()
        vuupt.buscar_servico_por_code.return_value = None
        vuupt.criar_ou_atualizar_servico.return_value = ({}, "criado")
        captura = {}

        def _montar(**kw):
            captura.update(kw)
            return {"customer": {"address": "x"}, "title": "t"}

        patches = [
            mock.patch.object(pipeline, "ja_confirmado_atribuido", return_value=False),
            mock.patch.object(pipeline.stokki_pedidos, "obter_detalhe", return_value=detalhe),
            mock.patch.object(pipeline, "resolver_endereco_entrega", return_value=res_end),
            mock.patch.object(pipeline, "config_consulta", return_value={"ativo": False}),
            mock.patch.object(pipeline, "_buscar_dados_embarcador_banco", return_value={"sender_id": 77}),
            mock.patch.object(pipeline.alteracoes_entrega, "conectar", return_value=mock.Mock()),
            mock.patch.object(pipeline.alteracoes_entrega, "endereco_vigente", return_value=VIGENTE),
            mock.patch.object(pipeline, "carregar_ajustes_manuais", return_value={
                "12345678000190": {"nivel_dificuldade": 2, "horario_atendimento_inicio": "09:00",
                                   "horario_atendimento_fim": "17:00"}}),
            mock.patch.object(pipeline, "buscar_confirmacao", return_value=None),
            mock.patch.object(pipeline, "montar_payload_vuupt", side_effect=_montar),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            geo = stack.enter_context(mock.patch.object(pipeline, "geocodificar"))
            r = pipeline.processar_pedido(1, "PS-1234567", object(), object(), vuupt, "", False)
        self.assertIsNone(r["erro"], r)
        self.assertTrue(r["endereco_cliente_portal"])
        self.assertEqual(captura["endereco_resolvido"]["logradouro"], "Av Nova, 250")
        self.assertEqual(captura["horario_entrega"], {"inicio": "09:00", "fim": "17:00"})
        geo.assert_not_called()
        enviado = vuupt.criar_ou_atualizar_servico.call_args[0][0]
        self.assertEqual((enviado["latitude"], enviado["longitude"]), (-22.9, -47.06))


if __name__ == "__main__":
    unittest.main()
