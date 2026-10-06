# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest documentos_pedido.test_arquivos_documentos
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import localizar_arquivos as la  # noqa: E402


class ResolverArquivo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.anexos = base / "anexos_temp"
        self.anexos.mkdir()
        self._p = patch.object(la, "PASTAS_BUSCA", [base / "downloads_stokki_temp", self.anexos])
        self._p2 = patch.object(la, "_PASTA_ANEXOS", self.anexos)
        self._p.start(); self._p2.start()

    def tearDown(self):
        self._p.stop(); self._p2.stop(); self._tmp.cleanup()

    def test_nome_direto(self):
        (self.anexos / "76649_NFS SP.pdf").write_bytes(b"x")
        self.assertEqual(la.resolver_arquivo_local("76649_NFS SP.pdf").name, "76649_NFS SP.pdf")

    def test_legado_sem_prefixo_uid_com_candidato_unico(self):
        (self.anexos / "76649_danfe(17).PDF").write_bytes(b"x")
        self.assertEqual(la.resolver_arquivo_local("danfe(17).PDF").name, "76649_danfe(17).PDF")

    def test_legado_ambiguo_nao_chuta(self):
        (self.anexos / "1_BOLETOS.pdf").write_bytes(b"x")
        (self.anexos / "2_BOLETOS.pdf").write_bytes(b"y")
        self.assertIsNone(la.resolver_arquivo_local("BOLETOS.pdf"))

    def test_prefixo_que_nao_e_uid_nao_conta(self):
        (self.anexos / "PS-1_BOLETOS.pdf").write_bytes(b"x")
        self.assertIsNone(la.resolver_arquivo_local("BOLETOS.pdf"))


class NomeUnico(unittest.TestCase):
    def test_rotulo_repetido_ganha_sufixo(self):
        import stokki_documentos as sd
        usados: set[str] = set()
        self.assertEqual(sd._nome_unico("PS-1_BOLETO.pdf", usados), "PS-1_BOLETO.pdf")
        self.assertEqual(sd._nome_unico("PS-1_BOLETO.pdf", usados), "PS-1_BOLETO_2.pdf")
        self.assertEqual(sd._nome_unico("PS-1_BOLETO.pdf", usados), "PS-1_BOLETO_3.pdf")


if __name__ == "__main__":
    unittest.main()
