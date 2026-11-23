# -*- coding: utf-8 -*-
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import openpyxl

import migrar_transportadoras_planilha as mig
import transportadoras_cadastro as cad


def _planilha(pasta: Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["TRANSPORTADORA", "Tipo"])
    t = ["TRANSFRIOS LTDA", "TERCEIROS", "SP", "Guarulhos", "Cumbica", "Rua A", 10, "Galpao 2", "07000000"]
    t += [None] * 7 + ["Guarulhos;Osasco", "SP", None, "12345678000190", None]
    t[19:20] = ["a@x.com, b@x.com"]
    ws.append(t[:20])
    r = ["LOGGI RETIRA", "RETIRADA"] + [None] * 7 + [None] * 9 + [1234567000190]
    ws.append(r[:19] + [None])
    ws.append(["ESTRANHA", "XPTO"])
    p = pasta / "t.xlsx"
    wb.save(p)
    return p


class TestMigrar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pasta = Path(self.tmp.name)
        self.xlsx = _planilha(self.pasta)
        self.db = self.pasta / "d.db"
        self.conn = cad.conectar(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_importar_traz_validas_e_lista_ignoradas(self):
        r = mig.importar(self.xlsx, self.conn)
        self.assertEqual(r["importadas"], 2)
        self.assertIn("ESTRANHA", r["ignoradas"])
        lin = self.conn.execute("SELECT * FROM transportadoras WHERE nome='TRANSFRIOS LTDA'").fetchone()
        self.assertEqual((lin["origem"], lin["situacao"], lin["cidades_atendidas"]), ("PLANILHA", "ATIVA", "Guarulhos;Osasco"))
        self.assertEqual(lin["email"], "a@x.com, b@x.com")

    def test_importar_duas_vezes_nao_duplica(self):
        mig.importar(self.xlsx, self.conn)
        r = mig.importar(self.xlsx, self.conn)
        self.assertEqual((r["importadas"], r["ja_existiam"]), (0, 2))

    def test_comparar_sem_diferencas(self):
        mig.importar(self.xlsx, self.conn)
        self.assertEqual(mig.comparar(self.xlsx, self.db, []), [])

    def test_comparar_acusa_diferenca(self):
        mig.importar(self.xlsx, self.conn)
        self.conn.execute("UPDATE transportadoras SET tipo='ENTREGA' WHERE nome='LOGGI RETIRA'")
        self.conn.commit()
        dif = mig.comparar(self.xlsx, self.db, [])
        self.assertEqual(len(dif), 1)
        self.assertIn("LOGGI RETIRA", dif[0])

    def _batimento(self, *textos):
        self.conn.execute("CREATE TABLE batimento_pedidos (transportadora TEXT, visto_em TEXT)")
        for t in textos:
            self.conn.execute("INSERT INTO batimento_pedidos VALUES (?, datetime('now'))", (t,))
        self.conn.commit()

    def test_pre_cadastro_cria_pendentes_sem_consulta(self):
        mig.importar(self.xlsx, self.conn)
        self._batimento("TRANSFRIOS LTDA", "NOVA TRANSP 11222333000144", "Não informado", "NOVA TRANSP 11222333000144")
        criadas = mig.pre_cadastrar(self.conn, self.db)
        self.assertEqual(criadas, ["NOVA TRANSP"])
        lin = self.conn.execute("SELECT * FROM transportadoras WHERE nome='NOVA TRANSP'").fetchone()
        self.assertEqual((lin["situacao"], lin["origem"], lin["tipo"], lin["cnpj"], lin["atualizado_por"]),
                         ("PENDENTE", "AUTOMATICO", None, "11222333000144", "pre-cadastro"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transportadoras WHERE situacao='PENDENTE'").fetchone()[0], 1)
        self.assertEqual(mig.pre_cadastrar(self.conn, self.db), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transportadoras WHERE situacao='PENDENTE'").fetchone()[0], 1)
        tabelas = self.conn.execute("SELECT name FROM sqlite_master WHERE name IN "
                                    "('transportadora_consultas','transportadora_pedidos_parados')").fetchall()
        self.assertEqual(tabelas, [])

    def _main(self, *args):
        saida = io.StringIO()
        with mock.patch("sys.argv", ["migrar", *args]), redirect_stdout(saida):
            try:
                mig.main()
                codigo = 0
            except SystemExit as e:
                codigo = e.code
        return codigo, saida.getvalue()

    def test_pre_cadastro_modo_teste_nao_cria_tabela_no_db(self):
        db = self.pasta / "sem_tabela.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE batimento_pedidos (transportadora TEXT, visto_em TEXT)")
        c.commit()
        c.close()
        codigo, saida = self._main("--pre-cadastro", "--modo-teste", "--db", str(db))
        self.assertEqual(codigo, 1)
        self.assertIn("migrar_transportadoras_planilha.py", saida)
        c = sqlite3.connect(db)
        tabelas = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        c.close()
        self.assertEqual(tabelas, ["batimento_pedidos"])

    def test_pre_cadastro_modo_teste_so_lista(self):
        mig.importar(self.xlsx, self.conn)
        self._batimento("NOVA TRANSP 11222333000144")
        codigo, saida = self._main("--pre-cadastro", "--modo-teste", "--db", str(self.db))
        self.assertEqual(codigo, 0)
        self.assertIn("NOVA TRANSP", saida)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transportadoras WHERE situacao='PENDENTE'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
