# -*- coding: utf-8 -*-
"""
Trava própria do e-mail às transportadoras (Hugo, 23/10): ligar a
chave-mestra notificacoes_automaticas.ativo pros embarcadores NÃO pode
ligar junto o envio às transportadoras. Envio real exige também
notificacao_transportadoras.ativo: true (ausente = desligado).

Rodar (da raiz):
    py -3.11 -m unittest notificacao_transportadoras.test_trava_transportadoras -v
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import controle_notificacoes as cn
import email_utils
import notificar_transportadoras as nt
from stokki import sessao_uso


class TravaTransportadorasTestCase(unittest.TestCase):

    def setUp(self):
        # banco da tela isolado: nunca le/grava o dados.db local
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"

    def test_ausente_e_desligada(self):
        self.assertFalse(email_utils.notificacao_transportadoras_ativa({}))
        self.assertFalse(email_utils.notificacao_transportadoras_ativa({"notificacao_transportadoras": {}}))

    def test_ligada_so_com_true_explicito(self):
        self.assertTrue(email_utils.notificacao_transportadoras_ativa(
            {"notificacao_transportadoras": {"ativo": True}}))

    def _rodar_main(self, config, modo_teste=False):
        grupos = {"transp": ["PS-1"]}
        with mock.patch.object(nt, "_carregar_config", return_value=config), \
             mock.patch.object(nt.CatalogoTransportadoras, "carregar_do_banco", return_value=mock.Mock()), \
             mock.patch.object(nt, "buscar_rotas_do_dia", return_value=[]), \
             mock.patch.object(nt, "identificar_pedidos_transportadora", return_value=(grupos, {}, {})), \
             mock.patch.object(nt, "_carregar_embarcadores_por_sender_id",
                               side_effect=AssertionError("passou da trava")) as embarcadores, \
             mock.patch.object(nt, "notificar_execucao") as notificar:
            nt.main(modo_teste=modo_teste, data_str="hoje", db_path=self.db)
        return embarcadores, notificar

    def test_mestra_ligada_sem_trava_propria_nao_envia(self):
        embarcadores, notificar = self._rodar_main({"notificacoes_automaticas": {"ativo": True}})
        embarcadores.assert_not_called()
        resumo = notificar.call_args[0][0]
        self.assertIn("notificacao_transportadoras.ativo", resumo["Resumo geral"]["detalhe"])

    def test_trava_propria_ligada_sem_mestra_nao_envia(self):
        embarcadores, _ = self._rodar_main({"notificacoes_automaticas": {"ativo": False},
                                            "notificacao_transportadoras": {"ativo": True}})
        embarcadores.assert_not_called()

    # ---- fluxo completo (tela por cima do config) -------------------------

    def _rodar_fluxo(self, config, modo_teste=False, nao_enviar=False):
        """Roda main ate o envio, com tudo externo simulado. Devolve
        (enviar_email, registrar_notificacao, notificar_execucao)."""
        ponto = mock.Mock(chave="k", nome="Transp X", nome_normalizado="transp x",
                          nao_enviar=nao_enviar, emails=["transp@x.com"],
                          endereco="Rua X, 1", nomes=["Transp X"])
        pontos = {"k": ponto}
        grupos = {"k": ["PS-1"]}
        detalhes = {"PS-1": {"sender_id": 5, "endereco_vuupt": ""}}
        embs = {5: {"nome": "Emb", "emails": ["emb@x.com"]}}
        fake_docs = types.SimpleNamespace(
            URL_PROVIDER_SHW="http://x", _extrair_id=lambda c: 1, _login=mock.Mock(),
            baixar_xml_nfe=mock.Mock(return_value="/tmp/x.xml"), nova_pagina=mock.Mock())
        fake_pw = types.SimpleNamespace(sync_playwright=mock.MagicMock())
        mods = {"stokki_documentos": fake_docs, "playwright": types.ModuleType("playwright"),
                "playwright.sync_api": fake_pw}
        with mock.patch.object(nt, "_carregar_config", return_value=config), \
             mock.patch.object(nt.CatalogoTransportadoras, "carregar_do_banco", return_value=mock.Mock()), \
             mock.patch.object(nt, "buscar_rotas_do_dia", return_value=[]), \
             mock.patch.object(nt, "identificar_pedidos_transportadora", return_value=(grupos, pontos, detalhes)), \
             mock.patch.object(nt, "_carregar_embarcadores_por_sender_id", return_value=embs), \
             mock.patch.object(nt, "pedidos_ja_notificados_hoje", return_value=set()), \
             mock.patch.object(nt, "conferir_na_stokki", return_value=({}, "")), \
             mock.patch.object(nt, "_extrair_nnf", return_value="123"), \
             mock.patch.object(nt, "StokkiSession") as stokki_session, \
             mock.patch.object(nt, "registrar_notificacao") as registrar, \
             mock.patch.object(nt, "enviar_email", return_value=True) as enviar, \
             mock.patch.object(nt, "notificar_execucao") as notificar, \
             mock.patch.object(sessao_uso, "adquirir", return_value=True) as adquirir, \
             mock.patch.object(sessao_uso, "renovar"), \
             mock.patch.object(sessao_uso, "liberar"), \
             mock.patch.dict(sys.modules, mods):
            nt.main(modo_teste=modo_teste, data_str="hoje", db_path=self.db)
        # o que so pode acontecer se algum ponto for realmente enviar
        self.recursos = {"adquirir": adquirir, "StokkiSession": stokki_session,
                         "sync_playwright": fake_pw.sync_playwright}
        return enviar, registrar, notificar

    def _tela(self, estado=None, geral=None, liberado=None):
        conn = cn.conectar(self.db)
        try:
            if geral is not None:
                cn.definir_geral(conn, geral, "teste")
            if estado:
                cn.definir_estado(conn, "transportadoras", estado, "teste")
            if liberado:
                cn.definir_excecao(conn, "transportadoras", liberado, "LIBERADO", "teste")
        finally:
            conn.close()

    CONFIG_LIGADO = {"notificacoes_automaticas": {"ativo": True}, "notificacao_transportadoras": {"ativo": True}}

    def test_sem_linha_na_tela_vale_o_config(self):
        enviar, registrar, _ = self._rodar_fluxo(self.CONFIG_LIGADO)
        self.assertEqual(enviar.call_args[0][0], ["transp@x.com"])
        registrar.assert_called_once_with("PS-1", "transp x")

    def test_tela_ligado_envia_mesmo_sem_trava_do_config(self):
        self._tela(estado="LIGADO")
        enviar, registrar, _ = self._rodar_fluxo({})
        self.assertEqual(enviar.call_args[0][0], ["transp@x.com"])
        self.assertEqual(enviar.call_args.kwargs["cc"], ["emb@x.com"])
        registrar.assert_called_once_with("PS-1", "transp x")

    def test_tela_piloto_vai_pro_piloto_sem_cc(self):
        self._tela(estado="PILOTO")
        enviar, registrar, _ = self._rodar_fluxo(self.CONFIG_LIGADO)
        self.assertEqual(enviar.call_args[0][0], ["hugo@freshlogbr.com"])
        self.assertEqual(enviar.call_args.kwargs["cc"], [])
        registrar.assert_not_called()

    def test_liberado_no_piloto_envia_pra_transportadora(self):
        self._tela(estado="PILOTO", liberado="  Transp X ")
        enviar, registrar, _ = self._rodar_fluxo({})
        self.assertEqual(enviar.call_args[0][0], ["transp@x.com"])
        self.assertEqual(enviar.call_args.kwargs["cc"], ["emb@x.com"])
        registrar.assert_called_once_with("PS-1", "transp x")

    def test_tela_desligado_nao_envia_e_consome_o_pedido(self):
        self._tela(estado="DESLIGADO")
        enviar, registrar, _ = self._rodar_fluxo(self.CONFIG_LIGADO)
        enviar.assert_not_called()
        registrar.assert_called_once_with("PS-1", "transp x")

    def test_tela_desligado_nem_pega_a_trava_da_stokki(self):
        self._tela(estado="DESLIGADO")
        _, registrar, _ = self._rodar_fluxo(self.CONFIG_LIGADO)
        registrar.assert_called_once_with("PS-1", "transp x")
        for nome, recurso in self.recursos.items():
            recurso.assert_not_called(), nome

    def test_tela_ligado_pega_a_trava_e_abre_a_stokki(self):
        self._tela(estado="LIGADO")
        self._rodar_fluxo({})
        for nome, recurso in self.recursos.items():
            self.assertTrue(recurso.called, nome)

    def test_geral_desligado_na_tela_retorna_cedo(self):
        self._tela(geral=False, estado="LIGADO")
        enviar, registrar, notificar = self._rodar_fluxo(self.CONFIG_LIGADO)
        enviar.assert_not_called()
        registrar.assert_not_called()
        self.assertIn("tela", notificar.call_args[0][0]["Resumo geral"]["detalhe"])

    def test_nao_enviar_do_cadastro_vale_mesmo_com_tela_ligada(self):
        self._tela(estado="LIGADO")
        enviar, registrar, _ = self._rodar_fluxo({}, nao_enviar=True)
        enviar.assert_not_called()
        registrar.assert_not_called()

    def test_modo_teste_ignora_a_tela(self):
        self._tela(geral=False, estado="DESLIGADO")
        enviar, registrar, _ = self._rodar_fluxo({}, modo_teste=True)
        self.assertEqual(enviar.call_args[0][0], ["hugo@freshlogbr.com"])
        self.assertEqual(enviar.call_args.kwargs["cc"], [])
        registrar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
