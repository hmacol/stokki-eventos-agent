# -*- coding: utf-8 -*-
"""Telefone de WhatsApp e chave "chamado sem resposta" nas preferencias de
notificacao do embarcador (preferencias_notificacao.py).

Rodar: py -3.11 -m unittest test_preferencias_whatsapp
"""
import sqlite3
import unittest

import preferencias_notificacao as pn

CNPJ = "11111111000111"


def _banco():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
        "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)")
    conn.execute("INSERT INTO interno VALUES (?,?,?,?,?,?,?)", (CNPJ, "Alfa LTDA", "Alfa", "a@alfa.com", 1, 101, 9001))
    conn.commit()
    return conn


class TestNormalizarWhatsapp(unittest.TestCase):
    def test_formatos_aceitos_viram_55_ddd_numero(self):
        for bruto in ("11999990000", "(11) 99999-0000", "+55 11 99999-0000", "5511999990000", " 11 9 9999 0000 "):
            with self.subTest(bruto=bruto):
                self.assertEqual(pn.normalizar_whatsapp(bruto), "5511999990000")

    def test_fixo_com_oito_digitos_tambem_vale(self):
        self.assertEqual(pn.normalizar_whatsapp("(11) 3333-4444"), "551133334444")

    def test_vazio_apaga(self):
        for bruto in ("", None, "   "):
            self.assertEqual(pn.normalizar_whatsapp(bruto), "")

    def test_invalidos(self):
        for bruto in ("999990000", "abc", "55119999900001", "0011999990000", "11 0999 90000"):
            with self.subTest(bruto=bruto):
                with self.assertRaises(ValueError) as cm:
                    pn.normalizar_whatsapp(bruto)
                self.assertIn("DDD", str(cm.exception))


class TestTabela(unittest.TestCase):
    def test_tipo_novo_no_grupo_whatsapp_e_ligado_por_padrao(self):
        self.assertEqual(pn.TIPOS["chamado_sem_resposta"]["grupo"], "whatsapp")
        self.assertTrue(pn.TIPOS["chamado_sem_resposta"]["default"])
        self.assertEqual(list(pn.TIPOS)[-1], "chamado_sem_resposta")

    def test_banco_antigo_ganha_a_coluna_whatsapp(self):
        conn = _banco()
        conn.execute("CREATE TABLE preferencias_notificacao (cnpj_embarcador TEXT PRIMARY KEY, "
                     "emails TEXT NOT NULL DEFAULT '', nfs_em_rota INTEGER NOT NULL DEFAULT 1, "
                     "atualizado_em TEXT, atualizado_por TEXT)")
        conn.execute("INSERT INTO preferencias_notificacao (cnpj_embarcador) VALUES (?)", (CNPJ,))
        prefs = pn.ler(conn, CNPJ)
        self.assertEqual(prefs["whatsapp"], "")
        self.assertTrue(prefs["tipos"]["chamado_sem_resposta"])


class TestSalvarELer(unittest.TestCase):
    def setUp(self):
        self.conn = _banco()
        self.addCleanup(self.conn.close)

    def test_sem_linha_devolve_vazio(self):
        self.assertEqual(pn.ler(self.conn, CNPJ)["whatsapp"], "")

    def test_salvar_normaliza_e_ler_devolve(self):
        prefs = pn.salvar(self.conn, CNPJ, [], {}, "cliente", whatsapp="(11) 99999-0000")
        self.assertEqual(prefs["whatsapp"], "5511999990000")
        self.assertEqual(pn.ler(self.conn, CNPJ)["whatsapp"], "5511999990000")

    def test_none_mantem_e_vazio_apaga(self):
        pn.salvar(self.conn, CNPJ, [], {}, "cliente", whatsapp="11999990000")
        self.assertEqual(pn.salvar(self.conn, CNPJ, ["x@alfa.com"], {}, "cliente")["whatsapp"], "5511999990000")
        self.assertEqual(pn.salvar(self.conn, CNPJ, [], {}, "cliente", whatsapp="")["whatsapp"], "")

    def test_invalido_nao_grava_nada(self):
        pn.salvar(self.conn, CNPJ, [], {}, "cliente", whatsapp="11999990000")
        with self.assertRaises(ValueError):
            pn.salvar(self.conn, CNPJ, ["novo@alfa.com"], {"insucesso": False}, "cliente", whatsapp="abc")
        prefs = pn.ler(self.conn, CNPJ)
        self.assertEqual((prefs["whatsapp"], prefs["emails"], prefs["tipos"]["insucesso"]),
                         ("5511999990000", [], True))


class TestWhatsappDoEmbarcador(unittest.TestCase):
    def setUp(self):
        self.conn = _banco()
        self.addCleanup(self.conn.close)

    def test_sem_linha_nenhuma(self):
        self.assertIsNone(pn.whatsapp_do_embarcador(self.conn, CNPJ))

    def test_sem_tabela(self):
        self.assertIsNone(pn.whatsapp_do_embarcador(self.conn, CNPJ))
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='preferencias_notificacao'").fetchone())

    def test_com_numero_e_chave_ligada(self):
        pn.salvar(self.conn, CNPJ, [], {}, "cliente", whatsapp="11999990000")
        self.assertEqual(pn.whatsapp_do_embarcador(self.conn, CNPJ), "5511999990000")

    def test_chave_desligada(self):
        pn.salvar(self.conn, CNPJ, [], {"chamado_sem_resposta": False}, "cliente", whatsapp="11999990000")
        self.assertIsNone(pn.whatsapp_do_embarcador(self.conn, CNPJ))

    def test_sem_numero(self):
        pn.salvar(self.conn, CNPJ, [], {}, "cliente")
        self.assertIsNone(pn.whatsapp_do_embarcador(self.conn, CNPJ))

    def test_cnpj_formatado(self):
        pn.salvar(self.conn, CNPJ, [], {}, "cliente", whatsapp="11999990000")
        self.assertEqual(pn.whatsapp_do_embarcador(self.conn, "11.111.111/0001-11"), "5511999990000")

    def test_numero_estranho_no_banco_e_ignorado(self):
        pn.salvar(self.conn, CNPJ, [], {}, "cliente", whatsapp="11999990000")
        self.conn.execute("UPDATE preferencias_notificacao SET whatsapp = '123'")
        self.assertIsNone(pn.whatsapp_do_embarcador(self.conn, CNPJ))


if __name__ == "__main__":
    unittest.main()
