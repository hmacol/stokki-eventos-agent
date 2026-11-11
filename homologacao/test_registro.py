import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from homologacao import registro


class TestRegistro(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.banco = Path(self.dir.name) / "dados.db"
        p = mock.patch.object(registro, "CAMINHO_BANCO", self.banco)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.dir.cleanup)

    def test_registra_e_lista(self):
        eid = registro.registrar_envio("email", "SMTP", "a@b.com", "assunto", "corpo")
        self.assertIsNotNone(eid)
        itens = registro.listar_envios()
        self.assertEqual(len(itens), 1)
        self.assertEqual(itens[0]["canal"], "email")
        self.assertEqual(itens[0]["pid"], os.getpid())
        self.assertEqual(registro.buscar_envio(eid)["destino"], "a@b.com")
        self.assertEqual(registro.listar_envios(canal="http"), [])

    def test_trunca_conteudo_em_64kb(self):
        eid = registro.registrar_envio("email", None, None, None, "x" * 100000)
        self.assertEqual(len(registro.buscar_envio(eid)["conteudo"]), 65536)

    def test_bytes_viram_texto(self):
        eid = registro.registrar_envio("http", None, None, None, b"ol\xe1")
        self.assertIsInstance(registro.buscar_envio(eid)["conteudo"], str)

    def test_origem_aponta_para_quem_chamou(self):
        eid = registro.registrar_envio("email", None, None, None, "x")
        self.assertRegex(registro.buscar_envio(eid)["origem"], r"test_registro\.py:\d+$")

    def test_origem_falhando_nao_perde_registro(self):
        with mock.patch.object(registro, "_origem", side_effect=ValueError("x")):
            eid = registro.registrar_envio("email", None, None, None, "x")
        self.assertIsNotNone(eid)
        self.assertEqual(registro.buscar_envio(eid)["origem"], "?")

    def test_banco_travado_cai_no_jsonl(self):
        registro.registrar_envio("email", None, None, None, "primeiro")
        trava = sqlite3.connect(str(self.banco), isolation_level=None)
        trava.execute("BEGIN EXCLUSIVE")
        try:
            with mock.patch.object(registro, "TIMEOUT", 0.1):
                r = registro.registrar_envio("email", None, None, None, "x")
        finally:
            trava.execute("ROLLBACK")
            trava.close()
        self.assertIsNone(r)
        linhas = (Path(self.dir.name) / "homolog_envios.jsonl").read_text(
            encoding="utf-8").splitlines()
        self.assertEqual(len(linhas), 1)
        self.assertEqual(json.loads(linhas[0])["canal"], "email")


if __name__ == "__main__":
    unittest.main()
