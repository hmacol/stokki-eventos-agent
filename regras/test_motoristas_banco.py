# -*- coding: utf-8 -*-
"""
test_motoristas_banco.py

Tabela bd_motoristas (planilha de motoristas no banco): importação, edição
com histórico, ATIVO/inativado_em e espelho xlsx. SQLite e xlsx temporários.
    py -3.11 -m unittest regras.test_motoristas_banco -v
"""
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

    def test_espelho_mesmas_colunas_e_ordem(self):
        mb.inserir(self.conn, _linha(AGENT_ID_VUUPT=1, NOME_MOTORISTA="A", ATIVO="SIM",
                                     CPF_MOTORISTA="09474142742"), "teste")
        mb.inserir(self.conn, _linha(AGENT_ID_VUUPT=2, NOME_MOTORISTA="B", ATIVO="SIM"), "teste")
        destino = self.dir / "espelho.xlsx"
        self.assertTrue(mb.escrever_espelho(self.conn, destino))
        df = pd.read_excel(destino, dtype=str)
        self.assertEqual(list(df.columns), mb.COLUNAS)
        self.assertEqual(list(df["NOME_MOTORISTA"]), ["A", "B"])
        self.assertEqual(df["CPF_MOTORISTA"][0], "09474142742")
        self.assertEqual([p.name for p in self.dir.iterdir() if p.suffix == ".tmp"], [])

    def test_espelho_falha_nao_levanta(self):
        self.assertFalse(mb.escrever_espelho(self.conn, self.dir / "naoexiste" / "x.xlsx"))

    def test_caminho_espelho_segue_db_path(self):
        with mock.patch.object(banco, "DB_PATH", self.dir / "t.db"):
            self.assertEqual(mb.caminho_espelho(), self.dir / "BD_MOTORISTAS.xlsx")


if __name__ == "__main__":
    unittest.main()
