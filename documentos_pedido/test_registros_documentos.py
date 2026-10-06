# -*- coding: utf-8 -*-
"""
Registros de documentos (05/10/2026): revisão manual guarda o pedido
quando ele é conhecido, não duplica a mesma revisão a cada rodada, e
falha de GCS/erro inesperado vira revisão (a retentativa pega) em vez
de sumir. Índice NF->pedido só com ENVIADO.
Rodar:  py -3.11 -m unittest documentos_pedido.test_registros_documentos
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "documentos_pedido"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import fingerprint_documentos as fp  # noqa: E402
import processar_documentos as pdoc  # noqa: E402


class _BancoTemp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = patch.object(fp, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()


class Fingerprint(_BancoTemp):
    def test_indice_nf_ignora_revisao_manual(self):
        fp.marcar_processado("h1", "stokki", "PS-1_DANFE.pdf", "Nota Fiscal", "PS-1", "ENVIADO", numero_nf="10")
        fp.marcar_processado("h2", "stokki", "PS-2_DANFE.pdf", "Nota Fiscal", "PS-2", "REVISAO_MANUAL",
                             motivo="suspeita", numero_nf="20")
        self.assertEqual({r["numero_nf"] for r in fp.carregar_indice_nf()}, {"10"})

    def test_ja_em_revisao(self):
        fp.marcar_processado("h1", "stokki", "PS-1_DANFE.pdf", "Nota Fiscal", "PS-1", "REVISAO_MANUAL",
                             motivo="XML errado", numero_nf="24944")
        self.assertTrue(fp.ja_em_revisao("PS-1", "Nota Fiscal", "24944", "XML errado"))
        self.assertFalse(fp.ja_em_revisao("PS-1", "Nota Fiscal", "24945", "XML errado"))
        self.assertFalse(fp.ja_em_revisao("PS-9", "Nota Fiscal", "24944", "XML errado"))

    def test_coluna_cobranca(self):
        fp.marcar_processado("h1", "stokki", "PS-1_DANFE.pdf", "Nota Fiscal", "PS-1", "ENVIADO",
                             numero_nf="10", cobranca=1)
        con = fp._conectar()
        self.assertEqual(con.execute("select cobranca from documentos_processados").fetchone()[0], 1)
        con.close()


class ProcessarUmDocumento(unittest.TestCase):
    """Bordas mockadas: hash, classificação, texto, casamento, GCS, banco."""

    def _rodar(self, tipo="Nota Fiscal", casou="PS-12345", texto="", gcs_erro=None, ja_rev=False,
               casar_erro=None, nome="PS-12345_DANFE.pdf"):
        item = {"caminho_local": Path(nome), "nome_arquivo": nome, "assunto_email": None}
        with patch.object(pdoc, "calcular_hash", return_value="hX"), \
             patch.object(pdoc, "ja_processado", return_value=False), \
             patch.object(pdoc, "classificar_documento", return_value={"tipo": tipo, "confianca": "alta"}), \
             patch.object(pdoc, "_extrair_texto_pdf_completo", return_value=texto), \
             patch.object(pdoc, "extrair_nf_da_danfe", return_value=("24944", None)), \
             patch.object(pdoc, "_validar_danfe_do_stokki", return_value=None), \
             patch.object(pdoc, "casar_documento_com_pedido", side_effect=casar_erro,
                          return_value={"codigo_pedido": casou, "metodo": "nome_arquivo",
                                        "motivo_falha": None if casou else "não casou"}), \
             patch.object(pdoc.storage_gcs, "enviar_documento", side_effect=gcs_erro, return_value="gs://x"), \
             patch.object(pdoc, "ja_em_revisao", return_value=ja_rev), \
             patch.object(pdoc, "marcar_processado") as marcar:
            status = pdoc.processar_um_documento(item, MagicMock(), {}, modo_teste=False)
        return status, marcar

    def test_placeholder_grava_codigo_do_pedido(self):
        status, marcar = self._rodar(texto="CHAVE DE ACESSO 9908 2623 0000")
        self.assertEqual(status, "REVISAO_MANUAL")
        self.assertEqual(marcar.call_args.args[4], "PS-12345")

    def test_mesma_revisao_nao_duplica(self):
        status, marcar = self._rodar(texto="CHAVE DE ACESSO 9908 2623 0000", ja_rev=True)
        self.assertEqual(status, "REVISAO_MANUAL")
        marcar.assert_not_called()

    def test_falha_no_gcs_vira_revisao_com_pedido(self):
        status, marcar = self._rodar(gcs_erro=RuntimeError("403"))
        self.assertEqual(status, "ERRO")
        self.assertEqual(marcar.call_args.args[5], "REVISAO_MANUAL")
        self.assertEqual(marcar.call_args.args[4], "PS-12345")
        self.assertIn("GCS", marcar.call_args.kwargs["motivo"])

    def test_erro_inesperado_nao_derruba_e_vira_revisao(self):
        status, marcar = self._rodar(casar_erro=ConnectionError("vuupt fora"))
        self.assertEqual(status, "ERRO")
        self.assertEqual(marcar.call_args.args[5], "REVISAO_MANUAL")
        self.assertIn("vuupt fora", marcar.call_args.kwargs["motivo"])


if __name__ == "__main__":
    unittest.main()
