# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest regras.test_clientes_boleto
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from regras import clientes_boleto as cb  # noqa: E402


def _banco(caminho: Path):
    c = sqlite3.connect(caminho)
    c.executescript("""
        CREATE TABLE nucleo_pedidos (codigo TEXT, sender_id INTEGER, destinatario_codigo TEXT,
                                     destinatario_nome TEXT, remetente_nome TEXT);
        CREATE TABLE documentos_processados (hash_conteudo TEXT PRIMARY KEY, tipo TEXT, codigo_pedido TEXT,
                                             status TEXT, cobranca INTEGER, processado_em TEXT);
        INSERT INTO nucleo_pedidos VALUES ('PS-1', 11, '12.345.678/0001-90', 'MERCADO A', 'NUU'),
                                          ('PS-2', 11, '12345678000190', 'MERCADO A', 'NUU'),
                                          ('PS-3', 22, '98765432000110', 'PADARIA B', 'SOTILLE'),
                                          ('PS-4', 22, '11122233344', 'FULANO', 'SOTILLE');
    """)
    c.commit()
    return c


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        self.con = _banco(self.db)
        self.addCleanup(self.con.close)
        p = patch.object(cb, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)

    def _doc(self, h, tipo, codigo, cobranca=None, quando="2099-01-01 10:00:00", status="ENVIADO"):
        self.con.execute("INSERT INTO documentos_processados VALUES (?,?,?,?,?,?)",
                         (h, tipo, codigo, status, cobranca, quando))
        self.con.commit()


class Sugerir(_Base):
    def test_uma_danfe_com_duplicata_vira_pendente(self):
        self._doc("h1", "Nota Fiscal", "PS-1", 1)
        r = cb.sugerir()
        self.assertEqual(r["novos"], [(11, "12345678000190", "MERCADO A")])
        linha = cb.listar()[0]
        self.assertEqual((linha["situacao"], linha["nfs_com_duplicata"]), (cb.PENDENTE, 1))

    def test_boleto_recebido_tambem_sugere(self):
        self._doc("h1", "Boleto", "PS-3")
        self.assertEqual(cb.sugerir()["novos"], [(22, "98765432000110", "PADARIA B")])

    def test_so_a_vista_nao_sugere(self):
        self._doc("h1", "Nota Fiscal", "PS-3", 0)
        self._doc("h2", "Nota Fiscal", "PS-4", -1)
        self.assertEqual(cb.sugerir()["novos"], [])
        self.assertEqual(cb.listar(), [])

    def test_revisao_manual_nao_conta(self):
        self._doc("h1", "Nota Fiscal", "PS-1", 1, status="REVISAO_MANUAL")
        self.assertEqual(cb.sugerir()["novos"], [])

    def test_decidido_nao_volta_a_pendente_mas_evidencia_atualiza(self):
        self._doc("h1", "Nota Fiscal", "PS-1", 1)
        cb.sugerir()
        cb.decidir(11, "12345678000190", tem_boleto=False, por="hugo")
        self._doc("h2", "Nota Fiscal", "PS-2", 1)
        r = cb.sugerir()
        self.assertEqual(r["novos"], [])
        linha = cb.listar()[0]
        self.assertEqual(linha["situacao"], cb.NAO_TEM_BOLETO)
        self.assertEqual(linha["nfs_com_duplicata"], 2)

    def test_modo_teste_nao_grava(self):
        self._doc("h1", "Nota Fiscal", "PS-1", 1)
        self.assertEqual(len(cb.sugerir(modo_teste=True)["novos"]), 1)
        self.assertEqual(cb.listar(), [])

    def test_fora_da_janela_nao_conta(self):
        self._doc("h1", "Nota Fiscal", "PS-1", 1, quando="2000-01-01 10:00:00")
        self.assertEqual(cb.sugerir(dias=90)["novos"], [])


class DecidirEConsultar(_Base):
    def test_decidir_e_trocar(self):
        self._doc("h1", "Nota Fiscal", "PS-1", 1)
        cb.sugerir()
        self.assertEqual(cb.decidir(11, "12.345.678/0001-90", True, "hugo")["situacao"], cb.TEM_BOLETO)
        self.assertEqual(cb.decidir(11, "12345678000190", False, "gabriel")["situacao"], cb.NAO_TEM_BOLETO)
        self.assertEqual(cb.listar()[0]["decidido_por"], "gabriel")

    def test_decidir_par_inexistente(self):
        with self.assertRaises(ValueError):
            cb.decidir(99, "1", True, "hugo")

    def test_situacao_por_pedido(self):
        self._doc("h1", "Nota Fiscal", "PS-1", 1)
        cb.sugerir()
        cb.decidir(11, "12345678000190", True, "hugo")
        r = cb.situacao_por_pedido(["#PS-2-R1", "PS-3", "PS-1"])
        self.assertEqual(r, {"PS-2": cb.TEM_BOLETO, "PS-1": cb.TEM_BOLETO})

    def test_situacao_sem_tabela(self):
        self.assertEqual(cb.situacao_por_pedido(["PS-1"]), {})

    def test_codigo_base(self):
        self.assertEqual(cb.codigo_base("#PS-36327-R2"), "PS-36327")
        self.assertEqual(cb.codigo_base("PS-1, PS-2"), "PS-1")


if __name__ == "__main__":
    unittest.main()
