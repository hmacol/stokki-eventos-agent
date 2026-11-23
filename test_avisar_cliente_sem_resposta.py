# -*- coding: utf-8 -*-
"""Rotina de WhatsApp pro embarcador que ficou 10 min sem responder a equipe
(avisar_cliente_sem_resposta.py). Sem rede: SQLite temporario, gateway
substituido por mocks.

Rodar: py -3.11 -m unittest test_avisar_cliente_sem_resposta
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import avisar_cliente_sem_resposta as rot
import notificar_whatsapp as nw
import preferencias_notificacao as pn

ch = rot.ch  # portal_cliente/chamados.py, ja no sys.path pela rotina
CNPJ = "11111111000111"
CNPJ_B = "22222222000122"
CLIENTE = {"cnpj": CNPJ, "sender_id": 101, "nome": "Alfa"}
CLIENTE_B = {"cnpj": CNPJ_B, "sender_id": 102, "nome": "Beta"}
MOTORISTA = {"tipo": ch.TIPO_MOTORISTA, "cpf": "12345678900", "agent_id": 9, "nome": "Zé"}
TEL = "5511999990000"
QUARTA_10H = datetime(2026, 9, 30, 10, 0, 0)


def _config(**clientes):
    c = {"ativo": True, "teto_diario": 30, "forcar_destino": ""}
    c.update(clientes)
    return {"whatsapp_notificacoes": {"ativo": True, "base_url": "http://x/api", "api_key": "k", "sessao": "s",
                                      "grupo_id": "1@g.us", "teto_diario": 50, "clientes": c},
            "portal_cliente": {"url_base": "https://app.freshhub.com.br/cliente"}}


class _ComBanco(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        for alvo, valor in (("DB_PATH", Path(self._tmp.name) / "t.db"),
                            ("PASTA_ANEXOS", Path(self._tmp.name) / "chamados")):
            mock.patch.object(ch, alvo, valor).start()
        self.enviar = mock.patch.object(nw.integracao_openwa, "enviar_texto", return_value=(True, "m1")).start()
        self.existe = mock.patch.object(nw.integracao_openwa, "numero_existe", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        self.dormir = mock.MagicMock()
        self.conn = ch.conectar()
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
            "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)")
        self.conn.executemany("INSERT INTO interno VALUES (?,?,?,?,?,?,?)", [
            (CNPJ, "Alfa LTDA", "Alfa", "a@alfa.com", 1, 101, 9001),
            (CNPJ_B, "Beta SA", "Beta", "b@beta.com", 1, 102, 9002)])
        self.conn.execute(nw._SCHEMA)
        self.conn.commit()
        pn.salvar(self.conn, CNPJ, [], {}, "cliente", whatsapp=TEL)

    # -- montagem --------------------------------------------------------------
    def chamado(self, solicitante=CLIENTE, status=ch.STATUS_EM_ATENDIMENTO):
        return ch.criar_chamado(self.conn, solicitante, origem="chat", status=status)

    def mensagem(self, chamado, origem, minutos_atras, canal=ch.CANAL_PORTAL, agora=QUARTA_10H):
        quando = (agora - timedelta(minutes=minutos_atras)).strftime("%Y-%m-%d %H:%M:%S")
        cur = self.conn.execute(
            "INSERT INTO portal_chamados_mensagens (chamado_id, origem, autor, canal, texto, anexos, criado_em) "
            "VALUES (?, ?, ?, ?, ?, '[]', ?)", (chamado["id"], origem, origem, canal, "oi", quando))
        if origem != ch.ORIGEM_SISTEMA:
            self.conn.execute("UPDATE portal_chamados SET ultima_origem = ?, ultima_msg_em = ? WHERE id = ?",
                              (origem, quando, chamado["id"]))
        self.conn.commit()
        return cur.lastrowid

    def equipe_respondeu(self, chamado, minutos_atras=11, **kw):
        return self.mensagem(chamado, ch.ORIGEM_EQUIPE, minutos_atras, **kw)

    def executar(self, config=None, agora=QUARTA_10H, **kw):
        return rot.executar(config or _config(), conn=self.conn, agora=agora, dormir=self.dormir, **kw)

    def registros(self):
        return [tuple(r) for r in self.conn.execute(
            "SELECT assinatura, situacao, motivo FROM notificacoes_whatsapp ORDER BY id")]


class TestElegivel(_ComBanco):
    def test_equipe_respondeu_ha_11_min_envia_pro_cliente(self):
        c = self.chamado()
        msg = self.equipe_respondeu(c, 11)
        r = self.executar()
        self.assertEqual((r["avaliados"], r["enviados"], r["fora_horario"]), (1, 1, False))
        self.assertEqual(self.enviar.call_args[0][1], f"{TEL}@c.us")
        self.assertIn(f"https://app.freshhub.com.br/cliente/?chamado={c['id']}", self.enviar.call_args[0][2])
        self.assertEqual(self.registros(), [(f"msg:{msg}", "enviado", None)])

    def test_resposta_da_equipe_por_email_tambem_conta(self):
        c = self.chamado(status=ch.STATUS_RESPONDIDO)
        self.equipe_respondeu(c, 15, canal=ch.CANAL_EMAIL)
        self.assertEqual(self.executar()["enviados"], 1)

    def test_mensagem_do_sistema_depois_da_equipe_nao_e_resposta_do_cliente(self):
        c = self.chamado()
        self.equipe_respondeu(c, 11)
        self.mensagem(c, ch.ORIGEM_SISTEMA, 5)
        self.assertEqual(self.executar()["enviados"], 1)

    def test_mais_antigo_primeiro(self):
        pn.salvar(self.conn, CNPJ_B, [], {}, "cliente", whatsapp="5511988880000")
        a, b = self.chamado(), self.chamado(CLIENTE_B)
        self.equipe_respondeu(a, 12)
        self.equipe_respondeu(b, 40)
        r = self.executar()
        self.assertEqual([cid for cid, _ in r["situacoes"]], [b["id"], a["id"]])
        self.assertEqual(r["enviados"], 2)


class TestNaoElegivel(_ComBanco):
    def test_casos_que_nao_avisam(self):
        casos = {}
        c = self.chamado(); self.equipe_respondeu(c, 9); casos["9 min"] = c
        c = self.chamado(); self.equipe_respondeu(c, 20); self.mensagem(c, ch.ORIGEM_CLIENTE, 15); casos["cliente respondeu"] = c
        c = self.chamado(status=ch.STATUS_RESOLVIDO); self.equipe_respondeu(c, 30); casos["resolvido"] = c
        c = self.chamado(MOTORISTA); self.equipe_respondeu(c, 30); casos["motorista"] = c
        c = self.chamado(CLIENTE_B); self.equipe_respondeu(c, 30); casos["sem telefone"] = c
        c = self.chamado(); self.equipe_respondeu(c, 3 * 24 * 60 + 1); casos["3 dias e 1 min"] = c
        c = self.chamado(); self.mensagem(c, ch.ORIGEM_CLIENTE, 30); casos["ultima e do cliente"] = c
        c = self.chamado(); self.mensagem(c, ch.ORIGEM_ASSISTENTE, 30); casos["so assistente"] = c
        r = self.executar()
        self.assertEqual((r["avaliados"], r["enviados"]), (0, 0), r)
        self.enviar.assert_not_called()
        self.assertEqual(self.registros(), [])

    def test_chave_desligada_no_portal(self):
        pn.salvar(self.conn, CNPJ, [], {"chamado_sem_resposta": False}, "cliente")
        c = self.chamado()
        self.equipe_respondeu(c, 30)
        self.assertEqual(self.executar()["enviados"], 0)
        self.enviar.assert_not_called()

    def test_fora_do_horario_nao_grava_nada(self):
        c = self.chamado()
        self.equipe_respondeu(c, 30)
        for agora in (datetime(2026, 10, 3, 10, 0), datetime(2026, 9, 30, 13, 30), datetime(2026, 9, 30, 17, 5),
                      datetime(2026, 9, 30, 8, 20)):
            with self.subTest(agora=agora):
                r = self.executar(agora=agora)
                self.assertTrue(r["fora_horario"])
        self.enviar.assert_not_called()
        self.assertEqual(self.registros(), [])

    def test_resposta_de_ontem_a_tarde_avisa_na_abertura(self):
        c = self.chamado()
        self.equipe_respondeu(c, 0, agora=datetime(2026, 9, 29, 16, 55))
        self.assertTrue(self.executar(agora=datetime(2026, 9, 29, 17, 5))["fora_horario"])
        self.assertEqual(self.executar(agora=datetime(2026, 9, 30, 8, 31))["enviados"], 1)


class TestRepeticaoETeto(_ComBanco):
    def test_uma_tentativa_por_mensagem_da_equipe(self):
        c = self.chamado()
        self.equipe_respondeu(c, 11)
        self.assertEqual(self.executar()["enviados"], 1)
        self.assertEqual(self.executar(agora=QUARTA_10H + timedelta(minutes=2))["avaliados"], 0)
        self.assertEqual(self.executar(agora=QUARTA_10H + timedelta(hours=3))["avaliados"], 0)
        # cliente fala, equipe responde de novo e ele some de novo -> aviso novo
        self.mensagem(c, ch.ORIGEM_CLIENTE, -10)
        self.equipe_respondeu(c, -20)
        r = self.executar(agora=QUARTA_10H + timedelta(minutes=31))
        self.assertEqual(r["enviados"], 1)
        self.assertEqual(len(self.registros()), 2)

    def test_falha_do_envio_nao_e_tentada_de_novo(self):
        self.enviar.return_value = (False, None)
        c = self.chamado()
        self.equipe_respondeu(c, 11)
        self.assertEqual(self.executar()["situacoes"], [(c["id"], "falhou")])
        self.assertEqual(self.executar(agora=QUARTA_10H + timedelta(minutes=2))["avaliados"], 0)

    def test_numero_sem_whatsapp_registra_e_nao_repete(self):
        self.existe.return_value = False
        c = self.chamado()
        self.equipe_respondeu(c, 11)
        self.assertEqual(self.executar()["sem_whatsapp"], 1)
        self.assertEqual(self.registros()[0][1:], ("nao_enviado", "numero sem whatsapp"))
        self.assertEqual(self.executar(agora=QUARTA_10H + timedelta(minutes=2))["avaliados"], 0)

    def test_gateway_sem_resposta_tenta_na_proxima(self):
        self.existe.return_value = None
        c = self.chamado()
        self.equipe_respondeu(c, 11)
        self.assertEqual(self.executar()["situacoes"], [(c["id"], "indeterminado")])
        self.assertEqual(self.registros(), [])
        self.existe.return_value = True
        self.assertEqual(self.executar(agora=QUARTA_10H + timedelta(minutes=2))["enviados"], 1)

    def test_teto_para_sem_gravar(self):
        pn.salvar(self.conn, CNPJ_B, [], {}, "cliente", whatsapp="5511988880000")
        a, b = self.chamado(), self.chamado(CLIENTE_B)
        self.equipe_respondeu(a, 12)
        self.equipe_respondeu(b, 11)
        r = self.executar(_config(teto_diario=1))
        self.assertEqual((r["enviados"], r["pulados"]), (1, 1))
        self.assertEqual(len(self.registros()), 1)
        # amanha o segundo sai
        r = self.executar(_config(teto_diario=1), agora=QUARTA_10H + timedelta(days=1))
        self.assertEqual(r["enviados"], 1)

    def test_modo_teste_nao_grava(self):
        c = self.chamado()
        self.equipe_respondeu(c, 11)
        r = self.executar(modo_teste=True)
        self.assertEqual(r["situacoes"], [(c["id"], "modo_teste")])
        self.enviar.assert_not_called()
        self.existe.assert_not_called()
        self.assertEqual(self.registros(), [])

    def test_desligado_nao_envia(self):
        c = self.chamado()
        self.equipe_respondeu(c, 11)
        r = self.executar(_config(ativo=False))
        self.assertEqual(r["situacoes"], [(c["id"], "desligado")])
        self.enviar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
