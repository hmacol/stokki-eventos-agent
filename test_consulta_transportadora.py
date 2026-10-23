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
