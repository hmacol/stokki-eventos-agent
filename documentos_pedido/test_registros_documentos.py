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
        self.assertTrue(fp.ja_em_revisao("PS-1", "Nota Fiscal", "24944", "XML errado", "PS-1_DANFE.pdf"))
        self.assertFalse(fp.ja_em_revisao("PS-1", "Nota Fiscal", "24945", "XML errado", "PS-1_DANFE.pdf"))
        self.assertFalse(fp.ja_em_revisao("PS-9", "Nota Fiscal", "24944", "XML errado", "PS-1_DANFE.pdf"))

    def test_ja_em_revisao_arquivo_diferente_nao_deduplica(self):
        # parcelas 1..N do mesmo boleto (mesma NF/motivo/pedido) são arquivos distintos
        fp.marcar_processado("h1", "email", "BOLETOS_1.pdf", "Boleto", "PS-1", "REVISAO_MANUAL",
                             motivo="Falha ao enviar pro GCS: 403", numero_nf="24944")
        self.assertTrue(fp.ja_em_revisao("PS-1", "Boleto", "24944", "Falha ao enviar pro GCS: 403",
                                         "BOLETOS_1.pdf"))
        self.assertFalse(fp.ja_em_revisao("PS-1", "Boleto", "24944", "Falha ao enviar pro GCS: 403",
                                          "BOLETOS_2.pdf"))

    def test_upsert_revisao_resolvida_preenche_tipo_e_codigo(self):
        fp.marcar_processado("h1", "email", "x.pdf", None, None, "REVISAO_MANUAL", motivo="Erro inesperado")
        fp.marcar_processado("h1", "email", "x.pdf", "Nota Fiscal", "PS-12345", "ENVIADO", gcs_path="gs://x")
        con = fp._conectar()
        row = con.execute("select tipo, codigo_pedido, status from documentos_processados").fetchone()
        con.close()
        self.assertEqual(tuple(row), ("Nota Fiscal", "PS-12345", "ENVIADO"))

    def test_marcar_substituido_so_mexe_em_revisao(self):
        fp.marcar_processado("h1", "stokki", "a.pdf", "Nota Fiscal", "PS-1", "REVISAO_MANUAL", motivo="m")
        fp.marcar_processado("h2", "stokki", "b.pdf", "Nota Fiscal", "PS-2", "ENVIADO")
        fp.marcar_substituido("h1")
        fp.marcar_substituido("h2")
        con = fp._conectar()
        st = {r[0]: r[1] for r in con.execute("select hash_conteudo, status from documentos_processados")}
        con.close()
        self.assertEqual(st, {"h1": "SUBSTITUIDO", "h2": "ENVIADO"})

    def test_atualizar_nf_pedido_ignora_revisao(self):
        fp.marcar_processado("h1", "stokki", "a.pdf", "Nota Fiscal", "PS-1", "REVISAO_MANUAL", motivo="m")
        fp.marcar_processado("h2", "stokki", "b.pdf", "Nota Fiscal", "PS-1", "ENVIADO")
        fp.atualizar_nf_pedido("PS-1", "99", None)
        con = fp._conectar()
        nf = {r[0]: r[1] for r in con.execute("select hash_conteudo, numero_nf from documentos_processados")}
        con.close()
        self.assertEqual(nf, {"h1": None, "h2": "99"})

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


class RetentarRevisao(unittest.TestCase):
    def _rodar(self, hash_disco, status="ENVIADO", modo_teste=False):
        row = {"nome_arquivo": "PS-1_DANFE.pdf", "origem": "stokki", "tipo": "Nota Fiscal",
               "hash_conteudo": "hAntigo"}
        with patch.object(pdoc, "listar_pendentes_revisao", return_value=[row]), \
             patch.object(pdoc, "resolver_arquivo_local", return_value=Path("PS-1_DANFE.pdf")), \
             patch.object(pdoc, "processar_um_documento", return_value=status), \
             patch.object(pdoc, "calcular_hash", return_value=hash_disco), \
             patch.object(pdoc, "marcar_substituido") as sub:
            pdoc.retentar_revisao_manual(MagicMock(), {}, modo_teste, MagicMock())
        return sub

    def test_hash_diferente_marca_antiga_como_substituida(self):
        self._rodar("hNovo").assert_called_once_with("hAntigo")

    def test_hash_igual_nao_marca(self):
        self._rodar("hAntigo").assert_not_called()

    def test_nao_resolvido_nao_marca(self):
        self._rodar("hNovo", status="REVISAO_MANUAL").assert_not_called()

    def test_modo_teste_nao_marca(self):
        self._rodar("hNovo", modo_teste=True).assert_not_called()


if __name__ == "__main__":
    unittest.main()
