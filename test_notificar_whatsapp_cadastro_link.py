# -*- coding: utf-8 -*-
"""py -3.11 -m unittest test_notificar_whatsapp_cadastro_link"""
import sqlite3
import unittest
from datetime import datetime
from unittest import mock

import notificar_whatsapp as nw

AGORA = datetime(2026, 11, 2, 10, 0)
CAD = {"id": 7, "nome": "Joao " + "Silva " * 10, "tipo_veiculo": "Fiorino", "refrigerado": 1,
       "posicao": "FIXO", "celular": "11998765432", "agent_id": 321}
LINK = "https://app.freshhub.com.br/painel/motoristas?cadastro=7"


class TextosGrupo(unittest.TestCase):
    def test_cabe_em_200_com_nome_e_pendencia_longos(self):
        cad = dict(CAD, nome="N" * 60)
        t = nw.texto_cadastro_link(cad, LINK, "P" * 300, AGORA)
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
        self.assertIn(LINK, t)

    def test_conteudo(self):
        t = nw.texto_cadastro_link(dict(CAD, nome="Joao"), LINK, None, AGORA)
        for parte in ("Motorista novo pelo link", "02/11 10:00", "Joao", "Fiorino", "refrigerado", "FIXO"):
            self.assertIn(parte, t)
        self.assertNotIn("⚠️", t)
        self.assertIn("seco", nw.texto_cadastro_link(dict(CAD, refrigerado=0), LINK, None, AGORA))
        self.assertIn("iPhone", nw.texto_cadastro_link(dict(CAD, iphone=True), LINK, None, AGORA))
        self.assertIn("⚠️ sem CRLV", nw.texto_cadastro_link(CAD, LINK, "sem CRLV", AGORA))

    def test_concluido(self):
        t = nw.texto_cadastro_link_concluido(dict(CAD, nome="N" * 80), AGORA)
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
        self.assertIn("agente 321", t)
        self.assertIn("concluído", t)


class BoasVindas(unittest.TestCase):
    def test_android_tem_acesso(self):
        t = nw.texto_boas_vindas_motorista("Joao", "js@freshhub.com.br", "123456", "https://x/app.apk", False)
        for parte in ("Joao", "js@freshhub.com.br", "123456", "https://x/app.apk"):
            self.assertIn(parte, t)

    def test_iphone_sem_senha_nem_link(self):
        t = nw.texto_boas_vindas_motorista("Joao", "js@freshhub.com.br", "123456", "https://x/app.apk", True)
        self.assertNotIn("123456", t)
        self.assertNotIn("https://x/app.apk", t)
        self.assertNotIn("js@freshhub.com.br", t)
        self.assertIn("a equipe vai te chamar", t)

    def test_forcar_destino_vai_pro_hugo_com_marca(self):
        config = {"whatsapp_notificacoes": {"ativo": True, "base_url": "http://x", "api_key": "k", "sessao": "s",
                                            "grupo_id": "g@g.us"},
                  "cadastro_motorista_link": {"forcar_destino_whatsapp": "5511900000000"}}
        with mock.patch.object(nw.integracao_openwa, "numero_existe", return_value=True), \
                mock.patch.object(nw, "despachar", return_value="enviado") as d:
            self.assertEqual(nw.avisar_motorista_boas_vindas(7, "(11) 99876-5432", "oi", config), "enviado")
        kw = d.call_args.kwargs
        self.assertEqual(kw["grupo_id"], "5511900000000@c.us")
        self.assertTrue(d.call_args.args[3].startswith("[teste → +5511998765432]"))
        self.assertEqual(d.call_args.args[1], nw.ORIGEM_MOTORISTA)
        self.assertEqual(d.call_args.args[4], "cadastro_link:7:boas_vindas")

    def test_forcar_presente_vazio_manda_pro_motorista_com_55(self):
        config = {"whatsapp_notificacoes": {"ativo": True, "base_url": "http://x", "api_key": "k", "sessao": "s",
                                            "grupo_id": "g@g.us", "clientes": {"forcar_destino": "5511900000000"}},
                  "cadastro_motorista_link": {"forcar_destino_whatsapp": ""}}
        with mock.patch.object(nw.integracao_openwa, "numero_existe", return_value=True), \
                mock.patch.object(nw, "despachar", return_value="enviado") as d:
            nw.avisar_motorista_boas_vindas(7, "11998765432", "oi", config)
        self.assertEqual(d.call_args.kwargs["grupo_id"], "5511998765432@c.us")
        self.assertEqual(d.call_args.args[3], "oi")

    def test_forcar_ausente_vale_o_dos_clientes(self):
        config = {"whatsapp_notificacoes": {"ativo": True, "base_url": "http://x", "api_key": "k", "sessao": "s",
                                            "grupo_id": "g@g.us", "clientes": {"forcar_destino": "5511900000000"}},
                  "cadastro_motorista_link": {"ativo": True}}
        with mock.patch.object(nw.integracao_openwa, "numero_existe", return_value=True), \
                mock.patch.object(nw, "despachar", return_value="enviado") as d:
            self.assertEqual(nw.avisar_motorista_boas_vindas(7, "11998765432", "oi", config), "enviado")
        self.assertEqual(d.call_args.kwargs["grupo_id"], "5511900000000@c.us")
        self.assertTrue(d.call_args.args[3].startswith("[teste → +5511998765432]"))

    def test_forcar_ausente_sem_o_dos_clientes_nao_manda(self):
        base = {"ativo": True, "base_url": "http://x", "api_key": "k", "sessao": "s", "grupo_id": "g@g.us"}
        for config in ({"whatsapp_notificacoes": dict(base)},
                       {"whatsapp_notificacoes": dict(base, clientes={"forcar_destino": ""})}):
            with mock.patch.object(nw.integracao_openwa, "numero_existe", return_value=True) as existe, \
                    mock.patch.object(nw, "despachar", return_value="enviado") as d:
                self.assertEqual(nw.avisar_motorista_boas_vindas(7, "11998765432", "oi", config), "desligado")
            d.assert_not_called()
            existe.assert_not_called()

    def test_sem_telefone_numero_sem_whatsapp_e_indeterminado(self):
        config = {"whatsapp_notificacoes": {"ativo": True, "base_url": "http://x", "api_key": "k", "sessao": "s",
                                            "grupo_id": "g@g.us"},
                  "cadastro_motorista_link": {"forcar_destino_whatsapp": ""}}
        self.assertEqual(nw.avisar_motorista_boas_vindas(7, "", "oi", config), "desligado")
        with mock.patch.object(nw.integracao_openwa, "numero_existe", return_value=False):
            self.assertEqual(nw.avisar_motorista_boas_vindas(7, "11998765432", "oi", config), "numero_sem_whatsapp")
        with mock.patch.object(nw.integracao_openwa, "numero_existe", return_value=None):
            self.assertEqual(nw.avisar_motorista_boas_vindas(7, "11998765432", "oi", config), "indeterminado")


class Teto(unittest.TestCase):
    def test_teto_proprio_nao_trava_o_grupo(self):
        config = {"whatsapp_notificacoes": {"ativo": True, "base_url": "http://x", "api_key": "k", "sessao": "s",
                                            "grupo_id": "g@g.us", "teto_diario": 3, "intervalo_min_seg": 0,
                                            "motoristas": {"teto_diario": 20}}}
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        dormir = mock.MagicMock()
        with mock.patch.object(nw.integracao_openwa, "enviar_texto", return_value=(True, "m")):
            for i in range(20):
                r = nw.despachar(config, nw.ORIGEM_MOTORISTA, "boas_vindas", "t", f"a{i}", conn=conn,
                                 agora=AGORA, dormir=dormir)
                self.assertEqual(r, "enviado", i)
            self.assertEqual(nw.despachar(config, nw.ORIGEM_MOTORISTA, "boas_vindas", "t", "a21", conn=conn,
                                          agora=AGORA, dormir=dormir), "nao_enviado")
            self.assertEqual(nw.despachar(config, "cadastro_motorista", "cadastro_link", "t", "g1", conn=conn,
                                          agora=AGORA, dormir=dormir), "enviado")


if __name__ == "__main__":
    unittest.main()
