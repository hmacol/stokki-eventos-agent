# -*- coding: utf-8 -*-
"""py -3.11 -m unittest test_consulta_transportadora -v"""
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import consulta_transportadora as ct
import transportadoras_cadastro as tc
from regras.transportadoras import CatalogoTransportadoras

END = {"uf": "SP", "municipio": "Embu", "bairro": "Centro", "logradouro": "Rua Osaka",
       "numero": "880", "complemento": "", "cep": "06800000"}


class ConsultaTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "t.db"
        self.conn = ct.conectar_consultas(self.db)

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def _tres(self):
        a = ct.registrar_desconhecida(self.conn, "ACME LTDA", "", "PS-1", "11111111000191", 5)
        ct.registrar_desconhecida(self.conn, "ACME LTDA", "", "PS-2", "11111111000191", 5)
        b = ct.registrar_desconhecida(self.conn, "ACME LTDA", "", "PS-3", "stkkc:9", 9)
        return a, b

    def test_mesma_transportadora_varios_pedidos_uma_pendente(self):
        a, b = self._tres()
        self.assertTrue(a["nova"])
        self.assertFalse(b["nova"])
        self.assertEqual(a["transportadora"]["id"], b["transportadora"]["id"])
        self.assertEqual(len(tc.listar(self.conn, "PENDENTE")), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transportadora_consultas").fetchone()[0], 2)
        self.assertEqual(len(ct.pedidos_parados(self.conn, a["transportadora"]["id"])), 3)
        self.assertEqual(len(ct.pedidos_parados(self.conn, a["transportadora"]["id"], "stkkc:9")), 1)

    def test_reusa_pendente_por_cnpj_com_grafia_diferente(self):
        a = ct.registrar_desconhecida(self.conn, "ACME LTDA", "11.222.333/0001-81", "PS-1", "e1", 1)
        b = ct.registrar_desconhecida(self.conn, "Acme Transportes", "11222333000181", "PS-2", "e1", 1)
        self.assertEqual(a["transportadora"]["id"], b["transportadora"]["id"])

    def test_reusa_pendente_por_nome_sem_cnpj(self):
        a = ct.registrar_desconhecida(self.conn, "Acme Ltda", "", "PS-1", "e1", 1)
        b = ct.registrar_desconhecida(self.conn, "ACME LTDA", "", "PS-2", "e1", 1)
        self.assertEqual(a["transportadora"]["id"], b["transportadora"]["id"])

    def test_registrar_mesmo_pedido_nao_duplica(self):
        ct.registrar_desconhecida(self.conn, "Acme", "", "PS-1", "e1", 1)
        ct.registrar_desconhecida(self.conn, "Acme", "", "PS-1", "e1", 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transportadora_pedidos_parados").fetchone()[0], 1)

    def test_deve_avisar_hoje_uma_vez(self):
        c = ct.registrar_desconhecida(self.conn, "Acme", "", "PS-1", "e1", 1)["consulta"]
        hoje = date(2026, 10, 26)
        self.assertTrue(ct.deve_avisar_hoje(c, hoje))
        c["enviado_em"] = datetime(2026, 10, 26, 7, 0).isoformat(timespec="seconds")
        self.assertFalse(ct.deve_avisar_hoje(c, hoje))
        self.assertTrue(ct.deve_avisar_hoje(c, hoje + timedelta(days=1)))
        c2 = {"enviado_em": "2026-10-20T07:00:00", "ultimo_lembrete_em": "2026-10-26T07:00:00"}
        self.assertFalse(ct.deve_avisar_hoje(c2, hoje))

    def test_responder_retirada_libera_tudo(self):
        a, _ = self._tres()
        tid = a["transportadora"]["id"]
        r = ct.responder(self.conn, tid, "RETIRADA", None, "", "Fulano (ACME) pelo link", "link")
        self.assertEqual(r["situacao"], "ATIVA")
        rows = self.conn.execute("SELECT * FROM transportadora_consultas").fetchall()
        self.assertTrue(all(x["respondido_em"] and x["canal_resposta"] == "link"
                            and x["respondido_por"] == "Fulano (ACME) pelo link" for x in rows))
        self.assertEqual(ct.pedidos_parados(self.conn, tid), [])
        self.assertEqual(ct.consultas_abertas(self.conn), [])
        cat = CatalogoTransportadoras.carregar_do_banco(self.db)
        self.assertEqual(cat.resolver("ACME LTDA").tipo, "RETIRADA")

    def test_responder_terceiros_sem_endereco_recusado(self):
        a, _ = self._tres()
        tid = a["transportadora"]["id"]
        with self.assertRaises(tc.ErroCadastro):
            ct.responder(self.conn, tid, "TERCEIROS", None, "", "x", "link")
        self.assertEqual(tc.buscar(self.conn, tid)["situacao"], "PENDENTE")
        self.assertEqual(len(ct.pedidos_parados(self.conn, tid)), 3)

    def test_responder_terceiros_com_endereco_e_email(self):
        a, _ = self._tres()
        tid = a["transportadora"]["id"]
        r = ct.responder(self.conn, tid, "TERCEIROS", END, "a@acme.com", "x", "link")
        self.assertEqual((r["tipo"], r["logradouro"], r["email"]), ("TERCEIROS", "Rua Osaka", "a@acme.com"))

    def test_responder_entrega(self):
        a, _ = self._tres()
        ct.responder(self.conn, a["transportadora"]["id"], "ENTREGA", None, "", "x", "link")
        cat = CatalogoTransportadoras.carregar_do_banco(self.db)
        self.assertEqual(cat.resolver("ACME LTDA").tipo, "ENTREGA")

    def test_responder_conflito_nao_fecha_nada(self):
        tc.criar(self.conn, {"nome": "Acme Ltda", "tipo": "ENTREGA", "situacao": "ATIVA"}, "TELA", "t")
        a = ct.registrar_desconhecida(self.conn, "ACME  LTDA.", "", "PS-1", "e1", 1)
        tid = a["transportadora"]["id"]
        with self.assertRaises(tc.ErroCadastro):
            ct.responder(self.conn, tid, "RETIRADA", None, "", "x", "link")
        self.assertEqual(len(ct.pedidos_parados(self.conn, tid)), 1)
        self.assertEqual(len(ct.consultas_abertas(self.conn)), 1)

    def test_responder_duas_vezes_recusado(self):
        a, _ = self._tres()
        tid = a["transportadora"]["id"]
        ct.responder(self.conn, tid, "RETIRADA", None, "", "x", "link")
        with self.assertRaises(tc.ErroCadastro):
            ct.responder(self.conn, tid, "ENTREGA", None, "", "x", "link")

    def test_responder_inativa_recusado(self):
        a, _ = self._tres()
        tid = a["transportadora"]["id"]
        tc.atualizar(self.conn, tid, {"situacao": "INATIVA"}, "t")
        with self.assertRaises(tc.ErroCadastro):
            ct.responder(self.conn, tid, "RETIRADA", None, "", "x", "link")
        self.assertEqual(tc.buscar(self.conn, tid)["situacao"], "INATIVA")

    def test_consultas_abertas_traz_nome_e_contagem(self):
        self._tres()
        ab = ct.consultas_abertas(self.conn)
        self.assertEqual(len(ab), 2)
        por = {x["embarcador_cnpj"]: x for x in ab}
        self.assertEqual(por["11111111000191"]["pedidos_parados"], 2)
        self.assertEqual(por["stkkc:9"]["pedidos_parados"], 1)
        self.assertEqual(por["stkkc:9"]["transportadora_nome"], "ACME LTDA")

    def test_token_ida_e_volta_e_expirado(self):
        t = ct.gerar_token("segredo", 42)
        self.assertEqual(ct.ler_token("segredo", t, 7), 42)
        self.assertIsNone(ct.ler_token("outro", t, 7))
        self.assertIsNone(ct.ler_token("segredo", "lixo", 7))
        import time
        time.sleep(1.1)
        self.assertEqual(ct.ler_token("segredo", t, 0), -1)


if __name__ == "__main__":
    unittest.main()


class AvisoTest(unittest.TestCase):
    CNPJ = "11111111000191"

    def setUp(self):
        from unittest.mock import patch
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "t.db"
        self.conn = ct.conectar_consultas(self.db)
        self.conn.execute("CREATE TABLE interno (stkkc_id INTEGER, cnpj_embarcador TEXT, email TEXT, "
                          "notificar_email INTEGER, apelido TEXT, nome_remetente TEXT)")
        self.conn.execute("INSERT INTO interno VALUES (5, ?, 'a@x.com; b@x.com', 1, 'Fruta', 'Fruta Fina')",
                          (self.CNPJ,))
        self.conn.commit()
        self.config = {"consulta_transportadora": {"ativo": True, "forcar_destino_email": "",
                                                   "forcar_destino_whatsapp": ""},
                       "portal_cliente": {"secret_key": "s3", "url_base": "https://app.test/cliente/"},
                       "email": {"remetente": "x@y"}}
        self.email = patch.object(ct.email_utils, "enviar_email", return_value=True).start()
        self.zap = patch.object(ct.notificar_whatsapp, "avisar_cliente_transportadora",
                                return_value="enviado").start()
        self.numero = patch.object(ct.preferencias_notificacao, "whatsapp_do_embarcador",
                                   return_value="5511999990000").start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(self.conn.close)
        r = ct.registrar_desconhecida(self.conn, "ACME <LTDA>", "", "PS-1", self.CNPJ, 5,
                                      resumo={"destinatario": "Maria", "cidade": "Embu", "uf": "SP"})
        self.consulta = r["consulta"]

    def test_resumo_volta_decodificado(self):
        p = ct.pedidos_parados(self.conn, self.consulta["transportadora_id"])[0]
        self.assertEqual(p["resumo"]["cidade"], "Embu")

    def test_coluna_resumo_adicionada_em_banco_antigo(self):
        db = Path(self._dir.name) / "velho.db"
        c = ct.tc.conectar(db)
        c.executescript("CREATE TABLE transportadora_pedidos_parados (pedido TEXT PRIMARY KEY, "
                        "transportadora_id INTEGER, embarcador_cnpj TEXT, stkkc_id INTEGER, desde TEXT, liberado_em TEXT);")
        c.close()
        c = ct.conectar_consultas(db)
        cols = [r[1] for r in c.execute("PRAGMA table_info(transportadora_pedidos_parados)")]
        c.close()
        self.assertIn("resumo", cols)

    def test_texto_consulta_cabe_em_200(self):
        from notificar_whatsapp import texto_consulta_transportadora, MAX_MENSAGEM
        link = ct.link(self.config, 1)
        for lemb in (False, True):
            t = texto_consulta_transportadora("T" * 80, 12, link, lemb)
            self.assertLessEqual(len(t), MAX_MENSAGEM)
            self.assertIn(link, t)

    def test_link(self):
        l = ct.link(self.config, 7)
        self.assertTrue(l.startswith("https://app.test/cliente/transportadora/"))
        self.assertEqual(ct.ler_token("s3", l.rsplit("/", 1)[1], 30), 7)

    def test_enviar_aviso_usa_email_do_interno(self):
        r = ct.enviar_aviso(self.conn, self.consulta, self.config, False, False)
        self.assertTrue(r["email"])
        self.assertEqual(r["whatsapp"], "enviado")
        self.assertFalse(r["sem_contato"])
        args = self.email.call_args[0]
        self.assertEqual(args[0], ["a@x.com", "b@x.com"])
        self.assertIn("ACME", args[1])
        self.assertIn("&lt;LTDA&gt;", args[2])
        self.assertIn("Maria", args[2])
        c = self.conn.execute("SELECT enviado_em FROM transportadora_consultas").fetchone()[0]
        self.assertTrue(c)

    def test_enviar_aviso_respeita_forcar_destino_email(self):
        self.config["consulta_transportadora"]["forcar_destino_email"] = "hugo@x.com"
        ct.enviar_aviso(self.conn, self.consulta, self.config, False, False)
        self.assertEqual(self.email.call_args[0][0], ["hugo@x.com"])

    def test_enviar_aviso_sem_contato(self):
        self.conn.execute("UPDATE interno SET email=''")
        self.numero.return_value = None
        r = ct.enviar_aviso(self.conn, self.consulta, self.config, False, False)
        self.assertTrue(r["sem_contato"])
        self.email.assert_not_called()
        self.zap.assert_not_called()
        self.assertIsNone(self.conn.execute("SELECT enviado_em FROM transportadora_consultas").fetchone()[0])

    def test_enviar_aviso_marca_lembrete(self):
        ct.enviar_aviso(self.conn, self.consulta, self.config, True, False)
        row = self.conn.execute("SELECT enviado_em, ultimo_lembrete_em FROM transportadora_consultas").fetchone()
        self.assertIsNone(row[0])
        self.assertTrue(row[1])

    def test_desligado_nao_envia(self):
        self.config["consulta_transportadora"]["ativo"] = False
        ct.enviar_aviso(self.conn, self.consulta, self.config, False, False)
        self.email.assert_not_called()
        self.zap.assert_not_called()

    def test_modo_teste_nao_envia_nem_marca(self):
        ct.enviar_aviso(self.conn, self.consulta, self.config, False, True)
        self.email.assert_not_called()
        self.assertIsNone(self.conn.execute("SELECT enviado_em FROM transportadora_consultas").fetchone()[0])

    def test_embarcador_stkkc_busca_por_id(self):
        r = ct.registrar_desconhecida(self.conn, "Outra", "", "PS-9", "stkkc:5", 5)
        ct.enviar_aviso(self.conn, r["consulta"], self.config, False, False)
        self.assertEqual(self.email.call_args[0][0], ["a@x.com", "b@x.com"])
        self.numero.assert_not_called()
