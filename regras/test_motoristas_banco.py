# -*- coding: utf-8 -*-
"""
test_motoristas_banco.py

Tabela bd_motoristas (planilha de motoristas no banco): importação, edição
com histórico, ATIVO/inativado_em e espelho xlsx. SQLite e xlsx temporários.
    py -3.11 -m unittest regras.test_motoristas_banco -v
"""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco  # noqa: E402
from regras import motoristas_banco as mb  # noqa: E402


def _linha(**kw):
    base = {c: None for c in mb.COLUNAS}
    base.update(kw)
    return base


class TestMotoristasBanco(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self._tmp.name)
        self.conn = banco.conectar(self.dir / "t.db")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _xlsx(self, linhas, nome="BD.xlsx"):
        caminho = self.dir / nome
        pd.DataFrame(linhas, columns=mb.COLUNAS).to_excel(caminho, index=False)
        return caminho

    def test_importar_preserva_cpf_telefone_e_ids(self):
        caminho = self._xlsx([
            _linha(AGENT_ID_VUUPT=10, VEHICLE_ID_VUUPT=5, NOME_MOTORISTA="Ana", ATIVO="SIM",
                   TELEFONE_MOTORISTA=11999998888, CPF_MOTORISTA="09474142742", MAX_ROTAS_DIA=2),
            _linha(AGENT_ID_VUUPT=11, NOME_MOTORISTA="Beto", ATIVO="SIM"),
        ])
        self.assertEqual(mb.importar_planilha(self.conn, caminho), 2)
        regs = mb.listar(self.conn)
        self.assertEqual(regs[0]["CPF_MOTORISTA"], "09474142742")
        self.assertEqual(regs[0]["TELEFONE_MOTORISTA"], "11999998888")
        self.assertIsInstance(regs[0]["AGENT_ID_VUUPT"], int)
        self.assertEqual(regs[0]["MAX_ROTAS_DIA"], 2)
        self.assertIsNone(regs[1]["VEHICLE_ID_VUUPT"])
        self.assertTrue(mb.tem_registros(self.conn))

    def test_importar_agent_id_repetido(self):
        caminho = self._xlsx([_linha(AGENT_ID_VUUPT=10, NOME_MOTORISTA="A", ATIVO="SIM"),
                              _linha(AGENT_ID_VUUPT=10, NOME_MOTORISTA="B", ATIVO="SIM")])
        mb.importar_planilha(self.conn, caminho)
        self.assertEqual(len(mb.listar(self.conn)), 2)

    def test_importar_recusa_tabela_cheia(self):
        caminho = self._xlsx([_linha(AGENT_ID_VUUPT=1, NOME_MOTORISTA="A", ATIVO="SIM"),
                              _linha(AGENT_ID_VUUPT=2, NOME_MOTORISTA="B", ATIVO="SIM")])
        mb.importar_planilha(self.conn, caminho)
        with self.assertRaises(RuntimeError):
            mb.importar_planilha(self.conn, caminho)
        self.assertEqual(mb.importar_planilha(self.conn, caminho, substituir=True), 2)
        self.assertEqual(len(mb.listar(self.conn)), 2)

    def _um(self, **kw):
        return mb.inserir(self.conn, _linha(AGENT_ID_VUUPT=7, NOME_MOTORISTA="Caio", ATIVO="SIM", **kw), "teste")

    def test_atualizar_registra_historico_e_origem(self):
        mid = self._um(PLACA="AAA1A11")
        mudancas = mb.atualizar(self.conn, mid, {"PLACA": "BBB2B22"}, "painel:hugo")
        self.assertEqual(mudancas, [{"campo": "PLACA", "de": "AAA1A11", "para": "BBB2B22"}])
        linhas = self.conn.execute(
            "SELECT * FROM bd_motoristas_alteracoes WHERE campo='PLACA'").fetchall()
        self.assertEqual(len(linhas), 1)
        self.assertEqual(linhas[0]["origem"], "painel:hugo")
        self.assertEqual((linhas[0]["valor_antigo"], linhas[0]["valor_novo"]), ("AAA1A11", "BBB2B22"))
        self.assertEqual(linhas[0]["agent_id"], 7)
        criado = self.conn.execute(
            "SELECT 1 FROM bd_motoristas_alteracoes WHERE campo='*criado*' AND motorista_id=?", (mid,)).fetchone()
        self.assertIsNotNone(criado)

    def test_atualizar_ativo_grava_e_limpa_inativado_em(self):
        mid = self._um()
        mb.atualizar(self.conn, mid, {"ATIVO": "NAO"}, "teste")
        self.assertIsNotNone(mb.listar(self.conn)[0]["inativado_em"])
        mb.atualizar(self.conn, mid, {"ATIVO": "SIM"}, "teste")
        self.assertIsNone(mb.listar(self.conn)[0]["inativado_em"])

    def test_atualizar_recusa_agent_id_e_cpf_preenchido(self):
        mid = self._um()
        with self.assertRaises(ValueError):
            mb.atualizar(self.conn, mid, {"AGENT_ID_VUUPT": 99}, "teste")
        mb.atualizar(self.conn, mid, {"CPF_MOTORISTA": "12345678901"}, "teste")  # vazio -> preenchido passa
        with self.assertRaises(ValueError):
            mb.atualizar(self.conn, mid, {"CPF_MOTORISTA": "99999999999"}, "teste")
        with self.assertRaises(ValueError):
            mb.atualizar(self.conn, mid, {"NOME_MOTORISTA": ""}, "teste")
        self.assertEqual(mb.por_cpf(self.conn, "12345678901")[0]["id"], mid)

    def test_por_cpf_casa_so_pelos_digitos(self):
        mid = mb.inserir(self.conn, _linha(NOME_MOTORISTA="A", CPF_MOTORISTA="123.456.789-01"), "teste")
        self.assertEqual(mb.por_cpf(self.conn, "12345678901")[0]["id"], mid)
        self.assertEqual(mb.por_cpf(self.conn, "123.456.789-01")[0]["id"], mid)
        self.assertEqual(mb.por_cpf(self.conn, ""), [])

    def test_espelho_mesmas_colunas_e_ordem(self):
        mb.inserir(self.conn, _linha(AGENT_ID_VUUPT=1, VEHICLE_ID_VUUPT=10, MAX_ROTAS_DIA=2, NOME_MOTORISTA="A",
                                     ATIVO="S", APENAS_CARGA_SECA="S", CPF_MOTORISTA="09474142742"), "teste")
        mb.inserir(self.conn, _linha(AGENT_ID_VUUPT=2, NOME_MOTORISTA="B", ATIVO="SIM"), "teste")
        destino = self.dir / "espelho.xlsx"
        self.assertTrue(mb.escrever_espelho(self.conn, destino))
        df = pd.read_excel(destino, dtype=str)
        self.assertEqual(list(df.columns), mb.COLUNAS)
        self.assertEqual(list(df["NOME_MOTORISTA"]), ["A", "B"])
        self.assertEqual(df["CPF_MOTORISTA"][0], "09474142742")
        self.assertEqual([p.name for p in self.dir.iterdir() if p.suffix == ".tmp"], [])
        # inteiro com linha vazia na coluna nao vira float; "S" fica como veio
        self.assertEqual(df["VEHICLE_ID_VUUPT"][0], "10")
        self.assertEqual(df["AGENT_ID_VUUPT"][0], "1")
        self.assertEqual(df["MAX_ROTAS_DIA"][0], "2")
        self.assertTrue(pd.isna(df["VEHICLE_ID_VUUPT"][1]))
        self.assertEqual((df["ATIVO"][0], df["APENAS_CARGA_SECA"][0]), ("S", "S"))

    def test_espelho_usa_temporario_unico_e_nao_deixa_resto(self):
        mb.inserir(self.conn, _linha(AGENT_ID_VUUPT=1, NOME_MOTORISTA="A", ATIVO="SIM"), "teste")
        destino = self.dir / "espelho.xlsx"
        nomes = []
        real = mb.pd.DataFrame.to_excel

        def espiao(df, caminho, *a, **k):
            nomes.append(Path(caminho).name)
            return real(df, caminho, *a, **k)

        with mock.patch.object(mb.pd.DataFrame, "to_excel", espiao):
            self.assertTrue(mb.escrever_espelho(self.conn, destino))
            self.assertTrue(mb.escrever_espelho(self.conn, destino))
        self.assertEqual(len(set(nomes)), 2)
        self.assertNotIn("espelho.xlsx.tmp", nomes)
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.xlsx")), ["espelho.xlsx"])
        # falha na escrita limpa o temporario
        with mock.patch.object(mb.pd.DataFrame, "to_excel", side_effect=OSError("x")), self.assertLogs(mb.logger, "ERROR"):
            self.assertFalse(mb.escrever_espelho(self.conn, destino))
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.xlsx")), ["espelho.xlsx"])

    def test_ativo_nao_para_n_nao_reinicia_relogio_de_inatividade(self):
        mid = self._um()
        mb.atualizar(self.conn, mid, {"ATIVO": "NAO"}, "teste")
        self.conn.execute("UPDATE bd_motoristas SET inativado_em = '2026-01-01 10:00:00' WHERE id = ?", (mid,))
        self.conn.commit()
        mb.atualizar(self.conn, mid, {"ATIVO": "N"}, "teste")
        self.assertEqual(mb.listar(self.conn)[0]["inativado_em"], "2026-01-01 10:00:00")

    def test_espelho_falha_nao_levanta(self):
        with self.assertLogs(mb.logger, "ERROR"):
            self.assertFalse(mb.escrever_espelho(self.conn, self.dir / "naoexiste" / "x.xlsx"))

    def test_registrar_historico_nao_muda_coluna(self):
        mid = mb.inserir(self.conn, _linha(AGENT_ID_VUUPT=1, NOME_MOTORISTA="A", ATIVO="SIM", PLACA="AAA1111"), "teste")
        mb.registrar_historico(self.conn, mid, "PLACA", "ZZZ9999", "app:1234:nao_repassada")
        self.assertEqual(mb.listar(self.conn)[0]["PLACA"], "AAA1111")
        h = self.conn.execute("SELECT campo, valor_antigo, valor_novo, origem FROM bd_motoristas_alteracoes"
                              " WHERE motorista_id = ? AND campo = 'PLACA'", (mid,)).fetchall()
        self.assertEqual([tuple(x) for x in h], [("PLACA", "AAA1111", "ZZZ9999", "app:1234:nao_repassada")])

    def test_caminho_espelho_segue_db_path(self):
        with mock.patch.object(banco, "DB_PATH", self.dir / "t.db"):
            self.assertEqual(mb.caminho_espelho(), self.dir / "BD_MOTORISTAS.xlsx")


class TestCliMotoristasBanco(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "t.db"
        self.xlsx = self.dir / "BD.xlsx"
        pd.DataFrame([
            _linha(AGENT_ID_VUUPT=10, NOME_MOTORISTA="Ana", ATIVO="SIM", CPF_MOTORISTA="09474142742", PLACA="AAA1111"),
            _linha(AGENT_ID_VUUPT=11, NOME_MOTORISTA="Beto", ATIVO="SIM", PLACA="BBB2222"),
        ], columns=mb.COLUNAS).to_excel(self.xlsx, index=False)

    def tearDown(self):
        self._tmp.cleanup()

    def _rodar(self, *args):
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            codigo = mb.main(list(args))
        return codigo, saida.getvalue()

    def _importar(self):
        return self._rodar("importar", "--planilha", str(self.xlsx), "--db", str(self.db))

    def test_importar_imprime_contagem_e_grava_espelho_ao_lado_do_db(self):
        codigo, saida = self._importar()
        self.assertEqual(codigo, 0)
        self.assertIn("2 motoristas", saida)
        self.assertTrue((self.dir / "BD_MOTORISTAS.xlsx").exists())

    def test_conferir_igual(self):
        self._importar()
        codigo, saida = self._rodar("conferir", "--planilha", str(self.xlsx), "--db", str(self.db))
        self.assertEqual((codigo, saida.strip()), (0, "IGUAL (2 motoristas)"))

    def test_conferir_campo_diferente_sai_com_1(self):
        self._importar()
        conn = banco.conectar(self.db)
        conn.execute("UPDATE bd_motoristas SET PLACA = 'ZZZ9999' WHERE AGENT_ID_VUUPT = 11")
        conn.commit()
        conn.close()
        codigo, saida = self._rodar("conferir", "--planilha", str(self.xlsx), "--db", str(self.db))
        self.assertEqual(codigo, 1)
        self.assertIn("11 placa xlsx='BBB2222' banco='ZZZ9999'", saida)


if __name__ == "__main__":
    unittest.main()
