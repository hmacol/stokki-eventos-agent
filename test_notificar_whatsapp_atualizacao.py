# -*- coding: utf-8 -*-
"""py -3.11 -m unittest test_notificar_whatsapp_atualizacao"""
import sqlite3
import unittest
from datetime import date, datetime
from unittest import mock

import notificar_whatsapp as nw

AGORA = datetime(2026, 11, 16, 10, 0)
WA = {"ativo": True, "base_url": "http://x", "api_key": "k", "sessao": "s", "grupo_id": "g@g.us",
      "intervalo_min_seg": 0}


def _config(forcar="ausente", **extra):
    config = {"whatsapp_notificacoes": dict(WA)}
    if forcar != "ausente":
        config["atualizacao_motoristas"] = {"forcar_destino_whatsapp": forcar, **extra}
    elif extra:
        config["atualizacao_motoristas"] = dict(extra)
    return config


def _avisar(config, link_id=5, conn=None, **kw):
    return nw.avisar_motorista_link_atualizacao(link_id, "grp@g.us", "FRESHLOG + ANA", "oi", config,
                                                conn=conn, agora=AGORA, dormir=mock.MagicMock(), **kw)


class Texto(unittest.TestCase):
    def test_texto_fixo(self):
        self.assertEqual(
            nw.texto_link_atualizacao("Ana", "https://x/atualizar/t", date(2026, 12, 1)),
            "Olá, Ana! Estamos atualizando o cadastro dos motoristas da Fresh Log. Preencha pelo link "
            "(leva 5 minutos, tenha CNH e documento do veículo em mãos): https://x/atualizar/t. Vale até 01/12.")


class Envio(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_sem_chave_forcar_destino_desligado(self):
        for config in (_config(), _config(ativo=True), _config(None), _config("abc")):
            with mock.patch.object(nw, "despachar") as d:
                self.assertEqual(_avisar(config), ("desligado", False))
            d.assert_not_called()

    def test_grupo_vazio_nao_envia(self):
        with mock.patch.object(nw, "despachar") as d:
            self.assertEqual(nw.avisar_motorista_link_atualizacao(5, "", "x", "oi", _config("")),
                             ("desligado", False))
        d.assert_not_called()

    def test_gateway_desligado_nao_envia(self):
        config = _config("")
        config["whatsapp_notificacoes"]["ativo"] = False
        with mock.patch.object(nw, "despachar") as d:
            self.assertEqual(_avisar(config), ("desligado", False))
        d.assert_not_called()

    def test_piloto_manda_para_o_numero_com_cabecalho_e_marca_piloto(self):
        with mock.patch.object(nw, "despachar", return_value="enviado") as d:
            self.assertEqual(_avisar(_config("+55 (11) 90000-0000")), ("enviado", True))
        self.assertEqual(d.call_args.kwargs["grupo_id"], "5511900000000@c.us")
        self.assertEqual(d.call_args.args[3], "[teste → grupo FRESHLOG + ANA]\noi")
        self.assertEqual(d.call_args.args[1], nw.ORIGEM_MOTORISTA_ATUALIZACAO_PILOTO)
        self.assertEqual(d.call_args.args[4], "link_atualizacao:5")

    def test_real_manda_no_grupo(self):
        with mock.patch.object(nw, "despachar", return_value="enviado") as d:
            self.assertEqual(_avisar(_config("")), ("enviado", False))
        self.assertEqual(d.call_args.kwargs["grupo_id"], "grp@g.us")
        self.assertEqual(d.call_args.args[3], "oi")
        self.assertEqual(d.call_args.args[1], nw.ORIGEM_MOTORISTA_ATUALIZACAO)

    def test_assinatura_impede_segunda_mensagem_do_mesmo_link(self):
        config = _config("")
        with mock.patch.object(nw.integracao_openwa, "enviar_texto", return_value=(True, "m")) as env:
            self.assertEqual(_avisar(config, conn=self.conn), ("enviado", False))
            self.assertEqual(_avisar(config, conn=self.conn), ("nao_enviado", False))
            self.assertEqual(env.call_count, 1)
            self.assertEqual(_avisar(config, link_id=6, conn=self.conn), ("enviado", False))

    def test_teto_proprio_15(self):
        config = _config("")
        with mock.patch.object(nw.integracao_openwa, "enviar_texto", return_value=(True, "m")):
            for i in range(15):
                self.assertEqual(_avisar(config, link_id=i, conn=self.conn)[0], "enviado", i)
            self.assertEqual(_avisar(config, link_id=99, conn=self.conn), ("teto", False))
            # teto proprio: nao trava o teto interno (alertas)
            self.assertEqual(nw.despachar(config, "outra", "t", "t", "x", conn=self.conn, agora=AGORA,
                                          dormir=mock.MagicMock()), "enviado")
            # ...e o envio real nao consome o teto interno (15 do link x teto interno 5)
            interno = _config("")
            interno["whatsapp_notificacoes"]["teto_diario"] = 5
            self.assertEqual(nw.despachar(interno, "outra", "t", "t", "y", conn=self.conn, agora=AGORA,
                                          dormir=mock.MagicMock()), "enviado")
        self.assertEqual(nw._teto(WA, nw.ORIGEM_MOTORISTA_ATUALIZACAO, _config("", teto_diario=3)), 3)
        self.assertEqual(nw._teto(WA, nw.ORIGEM_MOTORISTA_ATUALIZACAO, _config("")), 15)

    def test_piloto_nao_gasta_o_teto_do_envio_real(self):
        with mock.patch.object(nw.integracao_openwa, "enviar_texto", return_value=(True, "m")):
            for i in range(15):
                self.assertEqual(_avisar(_config("5511900000000"), link_id=i, conn=self.conn)[0], "enviado")
            self.assertEqual(_avisar(_config(""), link_id=50, conn=self.conn), ("enviado", False))

    def test_modo_teste_nao_envia_e_devolve_modo_teste(self):
        with mock.patch.object(nw.integracao_openwa, "enviar_texto") as env:
            self.assertEqual(_avisar(_config(""), conn=self.conn, modo_teste=True), ("modo_teste", False))
        env.assert_not_called()


if __name__ == "__main__":
    unittest.main()
