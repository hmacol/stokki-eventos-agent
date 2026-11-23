# -*- coding: utf-8 -*-
"""
Cadastro de transportadoras no dados.db (transportadoras_cadastro.py) e leitura
pelo catalogo (CatalogoTransportadoras.carregar_do_banco). Rodar com:
    py -3.11 -m unittest test_transportadoras_cadastro -v
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import transportadoras_cadastro as tc
from regras.transportadoras import CatalogoTransportadoras

KANEJO_END = {"logradouro": "Rua Osaka", "numero": "880", "municipio": "Embu", "uf": "SP"}


class CadastroTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "t.db"
        self.conn = tc.conectar(self.db)

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def test_conectar_renomeia_tabela_antiga(self):
        self.conn.close()
        db2 = Path(self._dir.name) / "antigo.db"
        c = sqlite3.connect(db2)
        c.execute("CREATE TABLE transportadoras(nome TEXT PRIMARY KEY, classificacao TEXT)")
        c.execute("INSERT INTO transportadoras VALUES ('A','X')")
        c.commit()
        c.close()
        conn = tc.conectar(db2)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transportadoras_antiga_jun26").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transportadoras").fetchone()[0], 0)
            conn.execute("SELECT id, situacao FROM transportadoras")
        finally:
            conn.close()
        self.conn = tc.conectar(self.db)

    def test_criar_e_carregar_do_banco_resolve_igual(self):
        tc.criar(self.conn, {"nome": "Mandaê Serviço de Consultoria de Logística", "tipo": "RETIRADA",
                             "situacao": "ATIVA", "cnpj": "19782476000150"}, "MANUAL", "teste")
        cat = CatalogoTransportadoras.carregar_do_banco(self.db)
        self.assertEqual(cat.resolver("Qualquer", cnpj="19782476000150").tipo, "RETIRADA")
        self.assertEqual(cat.resolver("MANDAE SERVICO DE CONSULTORIA DE LOGISTICA LTDA").tipo, "RETIRADA")

    def test_terceiros_vira_endereco_no_catalogo(self):
        tc.criar(self.conn, dict(nome="KANEJO", tipo="TERCEIROS", situacao="ATIVA", email="a@b.com",
                                 **KANEJO_END), "MANUAL", "teste")
        r = CatalogoTransportadoras.carregar_do_banco(self.db).resolver("Kanejo Logistica Ltda")
        self.assertEqual(r.endereco_redespacho.logradouro, "Rua Osaka")
        self.assertEqual(r.email, "a@b.com")

    def test_pendente_e_inativa_fora_do_catalogo(self):
        tc.criar(self.conn, {"nome": "Pendente Express", "situacao": "PENDENTE"}, "AUTO", "teste")
        tc.criar(self.conn, {"nome": "Inativa Cargas", "tipo": "ENTREGA", "situacao": "INATIVA"}, "MANUAL", "teste")
        tc.criar(self.conn, {"nome": "Ativa Log", "tipo": "ENTREGA", "situacao": "ATIVA"}, "MANUAL", "teste")
        cat = CatalogoTransportadoras.carregar_do_banco(self.db)
        self.assertTrue(cat.resolver("Pendente Express").desconhecida)
        self.assertTrue(cat.resolver("Inativa Cargas").desconhecida)

    def test_tabela_ausente_levanta_filenotfound(self):
        vazio = Path(self._dir.name) / "vazio.db"
        sqlite3.connect(vazio).close()
        with self.assertRaises(FileNotFoundError):
            CatalogoTransportadoras.carregar_do_banco(vazio)
        with self.assertRaises(FileNotFoundError):
            CatalogoTransportadoras.carregar_do_banco(Path(self._dir.name) / "naoexiste.db")

    def test_sem_nenhuma_ativa_levanta_filenotfound(self):
        # Tabela criada (conectar) mas sem a carga da planilha: catalogo vazio
        # mudaria a roteirizacao (RETIRADA/TERCEIROS viram desconhecidas).
        with self.assertRaises(FileNotFoundError) as cm:
            CatalogoTransportadoras.carregar_do_banco(self.db)
        self.assertIn("migrar_transportadoras_planilha.py", str(cm.exception))
        tc.criar(self.conn, {"nome": "So Pendente", "situacao": "PENDENTE"}, "AUTO", "teste")
        with self.assertRaises(FileNotFoundError):
            CatalogoTransportadoras.carregar_do_banco(self.db)

    def test_atualizar_sem_commit_desfaz_no_rollback(self):
        row = tc.criar(self.conn, {"nome": "Beta", "tipo": "ENTREGA", "situacao": "ATIVA"}, "MANUAL", "ana")
        tc.atualizar(self.conn, row["id"], {"email": "x@y.com"}, "bia", commit=False)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(tc.buscar(self.conn, row["id"])["email"], "")
        self.assertEqual(tc.historico(self.conn, row["id"]), [])

    def test_terceiros_sem_endereco_recusado(self):
        with self.assertRaises(tc.ErroCadastro):
            tc.criar(self.conn, {"nome": "X", "tipo": "TERCEIROS", "situacao": "ATIVA"}, "MANUAL", "teste")

    def test_tipo_invalido_recusado(self):
        with self.assertRaises(tc.ErroCadastro):
            tc.criar(self.conn, {"nome": "X", "tipo": "OUTRO", "situacao": "ATIVA"}, "MANUAL", "teste")

    def test_conflito_de_tipo_recusado(self):
        tc.criar(self.conn, dict(nome="KANEJO", tipo="TERCEIROS", situacao="ATIVA", **KANEJO_END), "MANUAL", "teste")
        with self.assertRaises(tc.ErroCadastro) as cm:
            tc.criar(self.conn, {"nome": "Kanejo Logistica Ltda", "tipo": "RETIRADA", "situacao": "ATIVA"},
                     "MANUAL", "teste")
        self.assertIn("KANEJO", str(cm.exception))

    def test_atualizar_grava_historico(self):
        row = tc.criar(self.conn, {"nome": "Beta", "tipo": "ENTREGA", "situacao": "ATIVA"}, "MANUAL", "ana")
        novo = tc.atualizar(self.conn, row["id"], {"email": "x@y.com", "tipo": "RETIRADA"}, "bia")
        self.assertEqual(novo["tipo"], "RETIRADA")
        h = {l["campo"]: l for l in tc.historico(self.conn, row["id"])}
        self.assertEqual(len(h), 2)
        self.assertEqual(h["tipo"]["antes"], "ENTREGA")
        self.assertEqual(h["tipo"]["depois"], "RETIRADA")
        self.assertEqual(h["email"]["depois"], "x@y.com")
        self.assertEqual(h["email"]["quem"], "bia")

    def test_listar_e_buscar(self):
        a = tc.criar(self.conn, {"nome": "Alfa", "tipo": "ENTREGA", "situacao": "ATIVA"}, "MANUAL", "t")
        tc.criar(self.conn, {"nome": "Gama", "situacao": "PENDENTE"}, "AUTO", "t")
        self.assertEqual(tc.buscar(self.conn, a["id"])["nome"], "Alfa")
        self.assertIsNone(tc.buscar(self.conn, 999))
        self.assertEqual(len(tc.listar(self.conn)), 2)
        self.assertEqual([r["nome"] for r in tc.listar(self.conn, situacao="PENDENTE")], ["Gama"])
        self.assertEqual([r["nome"] for r in tc.listar(self.conn, busca="alf")], ["Alfa"])

    def test_cnpj_13_digitos_ganha_zero(self):
        self.assertEqual(tc.normalizar_cnpj(1234567000190), "01234567000190")
        self.assertEqual(tc.normalizar_cnpj("12.345.670/0001-90"), "12345670000190")
        self.assertEqual(tc.normalizar_cnpj(""), "")
        with self.assertRaises(tc.ErroCadastro):
            tc.normalizar_cnpj("123")


if __name__ == "__main__":
    unittest.main()
