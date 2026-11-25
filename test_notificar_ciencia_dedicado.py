# -*- coding: utf-8 -*-
"""
Rodada de aviso da ciencia do dedicado (e-mail, WhatsApp e chat do portal),
com canais falsos. Rodar (da raiz): py -3.11 -m unittest test_notificar_ciencia_dedicado
"""
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

import ciencia_dedicado as cd
import controle_notificacoes as cn
import notificar_ciencia_dedicado as ncd
import notificar_whatsapp as nw
import preferencias_notificacao as pn
from portal_cliente import auth_cliente
from portal_cliente import chamados as ch
from portal_cliente import link_ciencia

AGORA = datetime(2026, 11, 17, 10, 0, 0)
CNPJ = "12345678000199"
SENDER = 5
NUMERO = "5511988887777"
CONFIG = {"portal_cliente": {"secret_key": "segredo", "url_base": "https://portal.teste/cliente"},
          "ciencia_dedicado": {"ativo": True}}
LINK = link_ciencia.url(SENDER, "segredo", "https://portal.teste/cliente", [1], "email")
_RE_LINK = re.compile(r"https://portal\.teste/cliente/ciencia/([\w.-]+)")


def _item(codigo="#PS-1", sid=10, valor=250.0, valor_pendente=False):
    return {
        "servico": {"id": sid, "code": codigo, "sender_id": SENDER,
                    "title": f"{codigo} - 123 / EMB / Padaria do Ze",
                    "address": "Rua A, 10, Campinas - SP, 13000-000, Brasil"},
        "regra": {"nome": "Campinas", "regiao": "Interior", "dias": [1, 3]},
        "data": date(2026, 11, 20), "valor": valor, "valor_pendente": valor_pendente,
    }


class CanaisFalsos:
    """Grava as chamadas; o resultado de cada canal e configuravel."""

    def __init__(self, email_ok=True, wpp="enviado", conta=True, alerta_ok=True):
        self.email_ok, self.wpp, self.conta, self.alerta_ok = email_ok, wpp, conta, alerta_ok
        self.emails, self.whats, self.abertos, self.mensagens, self.alertas = [], [], [], [], []
        self.resolvidos = set()
        self._proximo_id = 100

    def canais(self) -> ncd.Canais:
        return ncd.Canais(email=self._email, whatsapp=self._whatsapp, chat_abrir=self._abrir,
                          chat_mensagem=self._mensagem, tem_conta_portal=lambda cnpj: self.conta,
                          alerta_equipe=self._alerta)

    def _alerta(self, texto):
        self.alertas.append(texto)
        return self.alerta_ok

    def _email(self, destinos, assunto, html):
        if isinstance(self.email_ok, Exception):
            raise self.email_ok
        self.emails.append((destinos, assunto, html))
        return self.email_ok

    def _whatsapp(self, assinatura, telefone, texto, destino_forcado):
        self.whats.append((assinatura, telefone, texto, destino_forcado))
        return self.wpp

    def _abrir(self, cnpj, assunto, texto, pedido_ref):
        self._proximo_id += 1
        self.abertos.append((self._proximo_id, cnpj, assunto, texto, pedido_ref))
        return self._proximo_id

    def _mensagem(self, chamado_id, texto):
        self.mensagens.append((chamado_id, texto))
        return chamado_id not in self.resolvidos


class _Base(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = cd.conectar(self.db)
        self.addCleanup(os.remove, self.db)
        self.addCleanup(self.conn.close)
        self.emb = {"nome": "Emb Teste", "emails": ["a@emb.com"], "cnpj": CNPJ, "desligado": False}

    def _tela(self, estado=None, piloto_email="piloto@x.com", piloto_whatsapp="5511977776666"):
        c = cn.conectar(self.db)
        try:
            if estado:
                cn.definir_estado(c, "ciencia_dedicado", estado, "t")
            cn.definir_piloto(c, piloto_email, piloto_whatsapp, "t")
        finally:
            c.close()

    def _numero(self, numero=NUMERO, aviso_ligado=False):
        pn._garantir_tabela(self.conn)
        self.conn.execute("INSERT INTO preferencias_notificacao (cnpj_embarcador, whatsapp, chamado_sem_resposta) "
                          "VALUES (?, ?, ?)", (CNPJ, numero, int(aviso_ligado)))
        self.conn.commit()

    def _abrir(self, codigo="#PS-1", sid=10, **kw):
        return cd.abrir(self.conn, _item(codigo, sid, **kw), CNPJ, AGORA)

    def _rodada(self, falsos, agora=AGORA, config=CONFIG):
        pendentes = cd.pendentes_do_sender(self.conn, SENDER)
        return ncd.enviar_embarcador(self.conn, SENDER, pendentes, self.emb, config, falsos.canais(), agora,
                                     db_path=self.db)

    def assertLinkDoEmbarcador(self, texto, origem=None, ids=None):
        """Link do embarcador; com `origem`/`ids`, confere tambem o canal e os pedidos que ele cobre."""
        tokens = _RE_LINK.findall(texto)
        self.assertTrue(tokens, "texto sem o link da ciencia")
        for token in tokens:
            link = link_ciencia.ler(token, "segredo")
            self.assertEqual(link.sender_id, SENDER)
            if origem:
                self.assertEqual(link.origem, origem)
            if ids is not None:
                self.assertEqual(link.ids, frozenset(ids))

    def _envios(self, cid, campos=("canal", "resultado", "motivo", "rodada")):
        return [tuple(e[c] for c in campos) for e in cd.envios(self.conn, cid)]


class TestRodada(_Base):
    def test_um_email_por_embarcador_com_todos_os_pedidos_e_link(self):
        self._tela("LIGADO")
        self._numero()
        ids = [self._abrir("#PS-1", 10), self._abrir("#PS-2", 11, valor=None, valor_pendente=True)]
        f = CanaisFalsos()
        res = self._rodada(f)
        self.assertEqual(res, {"email": 1, "whatsapp": 1, "chat": 1, "nao_enviado": 0})
        self.assertEqual(len(f.emails), 1)
        destinos, assunto, html = f.emails[0]
        self.assertEqual(destinos, ["a@emb.com"])
        self.assertEqual(assunto, "[Freshlog] Confirme a ciência: 2 pedido(s) com entrega dedicada")
        for trecho in ("PS-1", "PS-2", "Padaria do Ze", "Campinas", "20/11/2026", "Terças e Quintas",
                       "R$ 250,00", "a confirmar", "Responder"):
            self.assertIn(trecho, html)
        # um link por canal (a resposta grava de qual veio), cobrindo so os pedidos da mensagem
        self.assertLinkDoEmbarcador(html, "email", ids)
        self.assertEqual(len(f.whats), 1)
        self.assertLinkDoEmbarcador(f.whats[0][2], "whatsapp", ids)
        self.assertEqual(len(f.abertos), 1)
        self.assertLinkDoEmbarcador(f.abertos[0][3], "chat", ids)
        self.assertIn("PS-2", f.abertos[0][3])

    def test_email_sai_mesmo_com_preferencia_desligada(self):
        self._tela("LIGADO")
        cid = self._abrir()
        self.emb["desligado"] = True
        f = CanaisFalsos()
        self._rodada(f)
        self.assertEqual(len(f.emails), 1)
        self.assertIn(("email", "enviado", "", 0), self._envios(cid))

    def test_whatsapp_usa_numero_mesmo_com_aviso_desligado(self):
        self._tela("LIGADO")
        self._numero(aviso_ligado=False)
        self.assertIsNone(pn.whatsapp_do_embarcador(self.conn, CNPJ))   # o aviso dele esta desligado
        self.assertEqual(pn.numero_whatsapp(self.conn, CNPJ), NUMERO)
        cid = self._abrir()
        f = CanaisFalsos()
        self._rodada(f)
        self.assertEqual(len(f.whats), 1)
        _, telefone, _, destino_forcado = f.whats[0]
        self.assertEqual((telefone, destino_forcado), (NUMERO, ""))
        wpp = [e for e in cd.envios(self.conn, cid) if e["canal"] == "whatsapp"]
        self.assertEqual([(e["resultado"], e["destino"]) for e in wpp], [("enviado", NUMERO)])

    def test_teto_do_whatsapp_registra_nao_enviado_e_outros_canais_saem(self):
        self._tela("LIGADO")
        self._numero()
        cid = self._abrir()
        f = CanaisFalsos(wpp="teto")
        res = self._rodada(f)
        self.assertEqual(res, {"email": 1, "whatsapp": 0, "chat": 1, "nao_enviado": 1})
        self.assertEqual(sorted(self._envios(cid)), [("chat", "enviado", "", 0), ("email", "enviado", "", 0),
                                                     ("whatsapp", "nao_enviado", "teto", 0)])

    def test_falha_no_email_nao_impede_whatsapp_nem_chat(self):
        self._tela("LIGADO")
        self._numero()
        for falha in (False, RuntimeError("smtp fora")):
            with self.subTest(falha=falha):
                self.conn.execute("DELETE FROM ciencias_dedicado")
                self.conn.execute("DELETE FROM ciencias_dedicado_envios")
                self.conn.commit()
                cid = self._abrir()
                f = CanaisFalsos(email_ok=falha)
                with self.assertLogs("notificar_ciencia_dedicado", "INFO"):
                    self._rodada(f)
                self.assertEqual(sorted(self._envios(cid)), [("chat", "enviado", "", 0),
                                                             ("email", "nao_enviado", "falha", 0),
                                                             ("whatsapp", "enviado", "", 0)])

    def test_chat_sem_conta_no_portal_registra_sem_conta_portal(self):
        self._tela("LIGADO")
        cid = self._abrir()
        f = CanaisFalsos(conta=False)
        self._rodada(f)
        self.assertEqual(f.abertos, [])
        self.assertEqual(f.mensagens, [])
        self.assertIn(("chat", "nao_enviado", "sem_conta_portal", 0), self._envios(cid))
        self.assertEqual(len(f.emails), 1)

    def test_lembrete_usa_a_mesma_conversa_e_abre_nova_se_resolvida(self):
        self._tela("LIGADO")
        cid = self._abrir()
        f = CanaisFalsos()
        self._rodada(f, AGORA)
        self.assertEqual([a[0] for a in f.abertos], [101])
        self.assertEqual(f.abertos[0][2], "Entrega dedicada: confirme a ciência")
        self.assertEqual(f.abertos[0][4], "PS-1")
        self._rodada(f, AGORA + timedelta(hours=4))
        self.assertEqual([m[0] for m in f.mensagens], [101])
        self.assertEqual(len(f.abertos), 1)
        self.assertTrue(f.emails[1][1].startswith("Lembrete: "))
        f.resolvidos.add(101)
        self._rodada(f, AGORA + timedelta(hours=8))
        self.assertEqual([a[0] for a in f.abertos], [101, 102])
        self._rodada(f, AGORA + timedelta(hours=12))
        self.assertEqual([m[0] for m in f.mensagens], [101, 101, 102])
        chat = [(e["destino"], e["rodada"]) for e in cd.envios(self.conn, cid) if e["canal"] == "chat"]
        self.assertEqual(chat, [("101", 0), ("101", 1), ("102", 2), ("102", 3)])

    def test_piloto_manda_email_e_whatsapp_ao_piloto_e_nao_abre_conversa(self):
        self._tela("PILOTO")
        self._numero()
        cid = self._abrir()
        f = CanaisFalsos()
        self._rodada(f)
        destinos, _, html = f.emails[0]
        self.assertEqual(destinos, ["piloto@x.com"])
        self.assertIn("PILOTO", html)
        self.assertIn("a@emb.com", html)
        _, telefone, _, destino_forcado = f.whats[0]
        self.assertEqual((telefone, destino_forcado), (NUMERO, "5511977776666"))
        # a copia que vai pro piloto leva link de piloto (a pagina simula por ele, mesmo apos virar ENVIAR)
        self.assertLinkDoEmbarcador(html, "email-piloto", [cid])
        self.assertLinkDoEmbarcador(f.whats[0][2], "whatsapp-piloto", [cid])
        self.assertEqual((f.abertos, f.mensagens), ([], []))
        self.assertEqual(sorted(self._envios(cid)), [("chat", "piloto", "", 0), ("email", "piloto", "", 0),
                                                     ("whatsapp", "piloto", "", 0)])

    def test_sem_linha_na_tela_vale_piloto_com_forcar_destino_ou_hugo(self):
        self._tela(None)   # so o numero do piloto, sem linha do tipo
        self._numero()
        cid = self._abrir()
        f = CanaisFalsos()
        self._rodada(f)
        self.assertEqual(f.emails[0][0], ["hugo@freshlogbr.com"])
        self.assertEqual(f.whats[0][3], "5511977776666")
        self.assertEqual(f.abertos, [])
        config = {**CONFIG, "ciencia_dedicado": {"ativo": True, "forcar_destino": "outro@x.com"}}
        self._rodada(f, AGORA + timedelta(hours=4), config=config)
        self.assertEqual(f.emails[1][0], ["outro@x.com"])
        self.assertTrue(all(r == "piloto" for _, r, _, _ in self._envios(cid)))

    def test_tela_nao_registra_desligado_e_agenda_proximo(self):
        self._tela("DESLIGADO")
        self._numero()
        cid = self._abrir()
        f = CanaisFalsos()
        res = self._rodada(f)
        self.assertEqual(res, {"email": 0, "whatsapp": 0, "chat": 0, "nao_enviado": 3})
        self.assertEqual((f.emails, f.whats, f.abertos, f.alertas), ([], [], [], []))
        self.assertEqual(sorted(self._envios(cid)), [("chat", "nao_enviado", "desligado", 0),
                                                     ("email", "nao_enviado", "desligado", 0),
                                                     ("whatsapp", "nao_enviado", "desligado", 0)])
        c = cd.buscar(self.conn, cid)
        self.assertEqual((c["situacao"], c["proximo_lembrete_em"]), (cd.PENDENTE, "2026-11-17 14:00:00"))

    def test_virada_de_piloto_para_real_recomeca_na_rodada_0_e_mantem_envios_de_piloto(self):
        # sem WhatsApp cadastrado: a rodada de piloto tambem tem um nao_enviado e mesmo assim nao conta
        self._tela("PILOTO")
        cid = self._abrir()
        f = CanaisFalsos()
        self._rodada(f, AGORA)
        self._rodada(f, AGORA + timedelta(hours=4))
        self.assertEqual(cd.rodadas_enviadas(self.conn, cid), 0)
        self._tela("LIGADO")
        self._rodada(f, AGORA + timedelta(hours=8))
        self.assertEqual(f.emails[2][0], ["a@emb.com"])
        self.assertFalse(f.emails[2][1].startswith("Lembrete"))
        self.assertEqual(len(f.abertos), 1)
        reais = [(c, r, rod) for c, r, _, rod in self._envios(cid) if r != "piloto"]
        self.assertIn(("email", "enviado", 0), reais)
        self.assertIn(("chat", "enviado", 0), reais)
        self.assertEqual(len([1 for _, r, _, _ in self._envios(cid) if r == "piloto"]), 4)
        self._rodada(f, AGORA + timedelta(hours=12))
        self.assertTrue(f.emails[3][1].startswith("Lembrete: "))
        self.assertIn(("email", "enviado", "", 1), self._envios(cid))

    def test_sem_canal_nenhum_tres_nao_enviado_alerta_equipe_uma_vez_e_segue_pendente(self):
        self._tela("LIGADO")
        self.emb["emails"] = []
        cid = self._abrir()
        f = CanaisFalsos(conta=False)
        self._rodada(f, AGORA)
        self.assertEqual(sorted(self._envios(cid)), [("chat", "nao_enviado", "sem_canal", 0),
                                                     ("email", "nao_enviado", "sem_canal", 0),
                                                     ("whatsapp", "nao_enviado", "sem_canal", 0)])
        self.assertEqual(len(f.alertas), 1)
        self.assertIn("PS-1", f.alertas[0])
        self.assertIn("Emb Teste", f.alertas[0])
        self.assertLinkDoEmbarcador(f.alertas[0], "equipe", [cid])
        self._rodada(f, AGORA + timedelta(hours=4))
        self.assertEqual(len(f.alertas), 1)
        self.assertIn(("email", "nao_enviado", "sem_canal", 1), self._envios(cid))
        self.assertEqual(cd.buscar(self.conn, cid)["situacao"], cd.PENDENTE)

    def test_cada_pedido_ganha_envio_por_canal_com_a_rodada_certa(self):
        self._tela("LIGADO")
        self._numero()
        antigo = self._abrir("#PS-1", 10)
        f = CanaisFalsos()
        self._rodada(f, AGORA)
        novo = self._abrir("#PS-2", 11)
        self._rodada(f, AGORA + timedelta(hours=4))
        self.assertEqual(sorted(self._envios(antigo, ("canal", "rodada"))),
                         [("chat", 0), ("chat", 1), ("email", 0), ("email", 1), ("whatsapp", 0), ("whatsapp", 1)])
        self.assertEqual(sorted(self._envios(novo, ("canal", "rodada"))), [("chat", 0), ("email", 0), ("whatsapp", 0)])
        self.assertEqual(len(f.emails), 2)
        self.assertIn("PS-2", f.emails[1][2])
        self.assertIn("PS-1", f.emails[1][2])
        self.assertEqual(f.mensagens[0][0], 101)   # o pedido novo entra na conversa que ja existe

    def test_proximo_lembrete_fica_4h_depois(self):
        self._tela("LIGADO")
        a, b = self._abrir("#PS-1", 10), self._abrir("#PS-2", 11)
        self._rodada(CanaisFalsos(), datetime(2026, 11, 17, 10, 15, 0))
        for cid in (a, b):
            self.assertEqual(cd.buscar(self.conn, cid)["proximo_lembrete_em"], "2026-11-17 14:15:00")
        self.assertEqual(cd.pendentes_vencidos(self.conn, datetime(2026, 11, 17, 14, 14)), [])


    def test_reivindicar_lembrete_falhando_nada_sai(self):
        self._tela("LIGADO")
        self._numero()
        cid = self._abrir()
        f = CanaisFalsos()
        travado = mock.patch.object(cd, "reivindicar_lembrete",
                                    side_effect=sqlite3.OperationalError("database is locked"))
        with travado, self.assertLogs("notificar_ciencia_dedicado", "ERROR"):
            res = self._rodada(f)
        self.assertEqual(res, {"email": 0, "whatsapp": 0, "chat": 0, "nao_enviado": 0})
        self.assertEqual((f.emails, f.whats, f.abertos, f.mensagens), ([], [], [], []))
        self.assertEqual(cd.envios(self.conn, cid), [])

    def test_falha_antes_de_enviar_nao_adia_o_lembrete(self):
        # tela/banco falhando ao montar a mensagem: o pedido nao pode ficar 4h adiado sem envio nem registro
        self._tela("LIGADO")
        cid = self._abrir()
        for alvo, nome in ((ncd, "decisao_email"), (cd, "rodadas_enviadas")):
            with self.subTest(nome), mock.patch.object(alvo, nome, side_effect=sqlite3.OperationalError("locked")):
                f = CanaisFalsos()
                with self.assertRaises(sqlite3.OperationalError):
                    self._rodada(f)
                self.assertEqual((f.emails, f.whats, f.abertos), ([], [], []))
                self.assertEqual([p["id"] for p in cd.pendentes_vencidos(self.conn, AGORA)], [cid])
                self.assertEqual(cd.envios(self.conn, cid), [])

    def test_lembrete_tomado_por_outra_rodada_nao_sai_de_novo(self):
        self._tela("LIGADO")
        self._numero()
        tomado, livre = self._abrir("#PS-1", 10), self._abrir("#PS-2", 11)
        # a outra rodada (timer x roteirizacao) tomou o PS-1 depois da selecao
        self.assertTrue(cd.reivindicar_lembrete(self.conn, tomado, AGORA, 4))
        f = CanaisFalsos()
        res = self._rodada(f)
        self.assertEqual(res, {"email": 1, "whatsapp": 1, "chat": 1, "nao_enviado": 0})
        for texto in (f.emails[0][2], f.whats[0][2], f.abertos[0][3]):
            self.assertIn("PS-2", texto)
            self.assertNotIn("PS-1", texto)
        self.assertEqual(cd.envios(self.conn, tomado), [])
        self.assertEqual(len(cd.envios(self.conn, livre)), 3)

    def test_todos_tomados_por_outra_rodada_nada_sai(self):
        self._tela("LIGADO")
        self._numero()
        cid = self._abrir()
        f = CanaisFalsos()
        self.assertTrue(cd.reivindicar_lembrete(self.conn, cid, AGORA, 4))
        res = self._rodada(f)
        self.assertEqual(res, {"email": 0, "whatsapp": 0, "chat": 0, "nao_enviado": 0})
        self.assertEqual((f.emails, f.whats, f.abertos, f.mensagens, f.alertas), ([], [], [], [], []))
        self.assertEqual(cd.envios(self.conn, cid), [])

    def test_sem_canal_com_cnpj_desconhecido_nao_alerta_a_equipe(self):
        # CNPJ desconhecido conta como piloto: ninguem do lado do cliente pode responder
        self._tela("LIGADO")
        cid = cd.abrir(self.conn, _item(), None, AGORA)
        f = CanaisFalsos(conta=False)
        ncd.enviar_embarcador(self.conn, SENDER, cd.pendentes_do_sender(self.conn, SENDER),
                              {"nome": "Sem Cadastro", "emails": []}, CONFIG, f.canais(), AGORA, db_path=self.db)
        self.assertEqual(f.alertas, [])
        self.assertIn(("email", "nao_enviado", "sem_canal", 0), self._envios(cid))

    def test_alerta_sem_canal_que_nao_saiu_fica_no_log(self):
        self._tela("LIGADO")
        self.emb["emails"] = []
        self._abrir()
        f = CanaisFalsos(conta=False, alerta_ok=False)
        with self.assertLogs("notificar_ciencia_dedicado", "WARNING") as logs:
            self._rodada(f)
        self.assertEqual(len(f.alertas), 1)
        self.assertTrue(any("WARNING" in l and "nao saiu" in l for l in logs.output), logs.output)

    def test_falha_ao_gravar_um_pedido_grava_os_outros_sem_levantar(self):
        self._tela("LIGADO")
        a, b = self._abrir("#PS-1", 10), self._abrir("#PS-2", 11)
        original = cd.registrar_envio

        def registrar(conn, ciencia_id, *args, **kw):
            if ciencia_id == a:
                raise sqlite3.OperationalError("database is locked")
            return original(conn, ciencia_id, *args, **kw)

        f = CanaisFalsos()
        with mock.patch.object(cd, "registrar_envio", registrar), \
                self.assertLogs("notificar_ciencia_dedicado", "ERROR") as logs:
            self._rodada(f)
        self.assertIn("PS-1", " ".join(logs.output))
        self.assertEqual(cd.envios(self.conn, a), [])
        self.assertEqual(sorted(e["canal"] for e in cd.envios(self.conn, b)), ["chat", "email", "whatsapp"])
        for cid in (a, b):
            self.assertEqual(cd.buscar(self.conn, cid)["proximo_lembrete_em"], "2026-11-17 14:00:00")

    def test_rodada_desligada_nao_conta_e_o_primeiro_envio_real_e_a_rodada_0(self):
        self._tela("DESLIGADO")
        self.emb["emails"] = []
        cid = self._abrir()
        f = CanaisFalsos(conta=False)
        self._rodada(f, AGORA)
        self.assertEqual(cd.rodadas_enviadas(self.conn, cid), 0)
        self._tela("LIGADO")
        self._rodada(f, AGORA + timedelta(hours=4))
        self.assertEqual(len(f.alertas), 1)   # o alerta da rodada 0 ainda sai
        self.assertIn(("email", "nao_enviado", "sem_canal", 0), self._envios(cid))
        self.emb["emails"] = ["a@emb.com"]
        self._rodada(f, AGORA + timedelta(hours=8))
        self.assertTrue(f.emails[0][1].startswith("Lembrete: "))   # rodada 1: a sem canal contou

    def test_copia_de_piloto_vira_lembrete_a_partir_da_2a_rodada(self):
        self._tela("PILOTO")
        self._numero()
        cid = self._abrir()
        f = CanaisFalsos()
        self._rodada(f, AGORA)
        self.assertFalse(f.emails[0][1].startswith("Lembrete"))
        self.assertTrue(f.whats[0][2].startswith("Fresh Log: "))
        self._rodada(f, AGORA + timedelta(hours=4))
        self.assertTrue(f.emails[1][1].startswith("Lembrete: "))
        self.assertTrue(f.whats[1][2].startswith("Lembrete Fresh Log: "))
        self.assertIn("LEMBRETE", f.emails[1][2])
        self.assertEqual((cd.rodadas_enviadas(self.conn, cid), cd.rodadas_piloto(self.conn, cid)), (0, 2))
        self.assertTrue(all(rod == 0 for _, _, _, rod in self._envios(cid)))


class TestConversaReal(_Base):
    def test_conversa_nasce_aguardando_o_cliente(self):
        with mock.patch.object(ch, "DB_PATH", self.db):
            ch.conectar().close()   # cria as tabelas do chat no banco temporario
        self.conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, sender_id INTEGER, nome_remetente TEXT, "
                          "apelido TEXT, email TEXT)")
        self.conn.execute("INSERT INTO interno VALUES (?, ?, 'Emb Teste Ltda', 'Emb Teste', 'a@emb.com')",
                          (CNPJ, SENDER))
        self.conn.commit()
        canais = ncd.canais_reais(CONFIG, db_path=self.db)
        chamado_id = canais.chat_abrir(CNPJ, "Entrega dedicada: confirme a ciência", "texto do aviso", "PS-1")
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            chamado = ch.buscar_chamado(conn, chamado_id, CNPJ)
            self.assertEqual((chamado["status"], chamado["origem"], chamado["pedido_ref"]),
                             (ch.STATUS_RESPONDIDO, ch.ORIGEM_SISTEMA, "PS-1"))
            self.assertNotIn(chamado_id, [c["id"] for c in ch.listar_fila(conn, "fila")])
            msgs = conn.execute("SELECT origem, texto FROM portal_chamados_mensagens WHERE chamado_id = ?",
                                (chamado_id,)).fetchall()
            self.assertEqual([tuple(m) for m in msgs], [(ch.ORIGEM_SISTEMA, "texto do aviso")])
            self.assertTrue(canais.chat_mensagem(chamado_id, "lembrete"))
            ch.atualizar_chamado(conn, chamado_id, status=ch.STATUS_RESOLVIDO)
            self.assertFalse(canais.chat_mensagem(chamado_id, "outro lembrete"))
        finally:
            conn.close()


    def test_membro_de_grupo_sem_conta_propria_nao_tem_conta_no_chat(self):
        """O chat do portal filtra pelo CNPJ do login (g.cliente["cnpj"], chamados_web.py): a
        conversa aberta no CNPJ do membro nao aparece pra quem entra pelo login do grupo."""
        login, membro = "11111111000111", CNPJ
        self.conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, sender_id INTEGER, nome_remetente TEXT, "
                          "apelido TEXT, email TEXT)")
        self.conn.executemany("INSERT INTO interno VALUES (?, ?, ?, ?, '')",
                              [(login, 7, "Grupo Ltda", "Grupo"), (membro, SENDER, "Emb Teste Ltda", "Emb Teste")])
        self.conn.commit()
        with mock.patch.object(ch, "DB_PATH", self.db), mock.patch.object(auth_cliente, "DB_PATH", self.db):
            ch.conectar().close()
            conn = auth_cliente.conectar()
        try:
            auth_cliente.definir_pin(conn, login, "482915")
            auth_cliente.definir_grupo(conn, login, [membro])
            canais = ncd.canais_reais(CONFIG, db_path=self.db)
            self.assertTrue(canais.tem_conta_portal(login))
            self.assertFalse(canais.tem_conta_portal(membro))
            chamado_id = canais.chat_abrir(membro, "assunto", "texto", "PS-1")
            self.assertNotIn(chamado_id, [c["id"] for c in ch.listar_chamados_cliente(conn, login)])
            self.assertIsNone(ch.chamado_ativo_cliente(conn, login))
        finally:
            conn.close()


class TestWhatsappCiencia(unittest.TestCase):
    CONFIG = {"whatsapp_notificacoes": {"ativo": True, "base_url": "http://x", "api_key": "k", "sessao": "s",
                                        "grupo_id": "g@g.us", "intervalo_min_seg": 0,
                                        "clientes": {"ativo": False, "teto_diario": 30, "forcar_destino": ""}}}

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        for alvo, valor in (("configurado", lambda cfg: True), ("numero_existe", lambda cfg, n: True)):
            p = mock.patch.object(nw.integracao_openwa, alvo, valor)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(nw.integracao_openwa, "enviar_texto", return_value=(True, "m1"))
        self.enviar = p.start()
        self.addCleanup(p.stop)

    def _avisar(self, config=None, destino_forcado="", assinatura="ciencia_dedicado:5:0"):
        return nw.avisar_cliente_ciencia_dedicado(assinatura, NUMERO, "texto", config or self.CONFIG,
                                                  destino_forcado=destino_forcado, conn=self.conn,
                                                  agora=datetime(2026, 11, 17, 10, 0), dormir=lambda s: None)

    def test_envia_ao_cliente_e_conta_no_teto_dos_clientes(self):
        self.assertEqual(self._avisar(), "enviado")
        self.assertEqual(self.enviar.call_args[0][1:], (NUMERO + "@c.us", "texto"))
        linha = self.conn.execute("SELECT origem, tipo, assinatura FROM notificacoes_whatsapp").fetchone()
        self.assertEqual(linha, (nw.ORIGEM_CLIENTE, "ciencia_dedicado", "ciencia_dedicado:5:0"))

    def test_teto_devolve_teto(self):
        cfg = {"whatsapp_notificacoes": {**self.CONFIG["whatsapp_notificacoes"],
                                         "clientes": {"teto_diario": 0}}}
        self.assertEqual(self._avisar(cfg), "teto")
        self.enviar.assert_not_called()

    def test_piloto_vai_pro_numero_do_piloto(self):
        self.assertEqual(self._avisar(destino_forcado="5511977776666"), "enviado")
        destino, texto = self.enviar.call_args[0][1:]
        self.assertEqual(destino, "5511977776666@c.us")
        self.assertTrue(texto.startswith(f"[teste → +{NUMERO}]"))

    def test_texto_cabe_e_leva_o_link(self):
        pedidos = [{"codigo": f"PS-4031{i}"} for i in range(6)]
        link = link_ciencia.url(123456, "segredo", "https://app.freshhub.com.br/cliente", [1, 2, 3], "whatsapp")
        for lembrete in (False, True):
            t = nw.texto_ciencia_dedicado("Embarcador de Nome Bem Comprido Ltda", pedidos, link, lembrete)
            self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
            self.assertIn(link, t)
            self.assertEqual(t.startswith("Lembrete"), lembrete)
        um = nw.texto_ciencia_dedicado("Emb", [{"codigo": "PS-1"}], LINK, False)
        self.assertIn("PS-1", um)
        self.assertIn("dedicad", um)


if __name__ == "__main__":
    unittest.main()
