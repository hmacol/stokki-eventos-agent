# -*- coding: utf-8 -*-
"""
Documentos anexados ao pedido no portal saem no romaneio (Hugo, 11/11/2026).

Rodar (da raiz):  py -3.11 -m unittest roteirizacao.test_romaneio_anexos_portal
"""
import io
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from PIL import Image  # noqa: E402
from pypdf import PdfReader  # noqa: E402

import gerar_pdf_romaneios as gpr  # noqa: E402

SENDER_COMUM = 999


def _doc(hash_, tipo, origem, numero_nf=None, quando="2026-11-11 10:00:00"):
    return {"hash_conteudo": hash_, "tipo": tipo, "origem": origem, "codigo_pedido": "PS-1", "numero_nf": numero_nf,
            "processado_em": quando, "nome_arquivo": hash_ + ".pdf", "cobranca": None, "numero_parcela": None}


def _abrir_tudo(rows):
    return [(r, None) for r in rows], []


class AnexosDoPortal(unittest.TestCase):
    def _av(self, docs, sender=SENDER_COMUM):
        with patch.object(gpr, "_abrir_documentos", side_effect=_abrir_tudo):
            return gpr.avaliar_documentos_pedido("PS-1", sender, {"PS-1": docs})

    def test_outro_documento_do_portal_sai_como_anexo_na_ordem_em_que_chegou(self):
        r = self._av([_doc("nf", "Nota Fiscal", "stokki", "10"),
                      _doc("b", "Outro", "portal", quando="2026-11-11 12:00:00"),
                      _doc("a", "Agendamento", "portal", quando="2026-11-11 09:00:00")])
        self.assertEqual([row["hash_conteudo"] for row, _ in r["anexos"]], ["a", "b"])

    def test_boleto_e_nf_do_portal_contam_como_boleto_e_nf_e_nao_repetem(self):
        r = self._av([_doc("nf", "Nota Fiscal", "portal", "10"), _doc("bol", "Boleto", "portal", "10")])
        self.assertEqual([row["hash_conteudo"] for row, _ in r["nfs"]], ["nf"])
        self.assertEqual([row["hash_conteudo"] for row, _ in r["boletos"]], ["bol"])
        self.assertEqual(r["anexos"], [])
        self.assertNotIn("sem nota fiscal", r["faltas"])

    def test_outro_que_nao_veio_do_portal_continua_fora(self):
        self.assertEqual(self._av([_doc("x", "Outro", "stokki")])["anexos"], [])

    def test_carga_do_banco_traz_o_outro_so_quando_e_do_portal(self):
        with tempfile.TemporaryDirectory() as pasta:
            banco = Path(pasta) / "d.db"
            con = sqlite3.connect(banco)
            con.execute("CREATE TABLE documentos_processados (hash_conteudo TEXT PRIMARY KEY, origem TEXT, nome_arquivo TEXT, "
                        "tipo TEXT, codigo_pedido TEXT, status TEXT, processado_em TEXT)")
            con.executemany("INSERT INTO documentos_processados VALUES (?,?,?,?,?,?,?)", [
                ("p", "portal", "p.pdf", "Outro", "PS-1", "ENVIADO", "x"),
                ("s", "stokki", "s.pdf", "Outro", "PS-1", "ENVIADO", "x"),
                ("n", "stokki", "n.pdf", "Nota Fiscal", "PS-1", "ENVIADO", "x")])
            con.commit()
            con.close()
            with patch.object(gpr, "DB_PATH", banco):
                docs, _rev = gpr.carregar_documentos_por_pedido({"PS-1"})
        self.assertEqual(sorted(d["hash_conteudo"] for d in docs["PS-1"]), ["n", "p"])

    def test_pdf_da_rota_leva_o_anexo_depois_da_capa(self):
        """Ponta a ponta no PDF: capa + 1 pagina do documento anexado no portal."""
        with tempfile.TemporaryDirectory() as pasta:
            anexo = Path(pasta) / "a.pdf"
            imagem = io.BytesIO()
            Image.new("RGB", (60, 80), (9, 9, 9)).save(imagem, format="PDF")
            anexo.write_bytes(imagem.getvalue())
            saida = Path(pasta) / "romaneio.pdf"
            servico = {"code": "#PS-1", "title": "#PS-1 - 123 / EMB / CLIENTE", "sender_id": SENDER_COMUM, "dimension_3": 2,
                       "address": "Rua Capote Valente, 851, Pinheiros, São Paulo - SP, 05409-002, Brasil"}
            with (patch.object(gpr, "resolver_arquivo_local", return_value=anexo),
                  patch.object(gpr, "carregar_catalogo_transportadoras", return_value=None)):
                stats = gpr.montar_pdf_rota({"name": "Planejamento - 11/11/2026 - #1", "id": 1}, [servico],
                                            {"PS-1": [_doc("a", "Outro", "portal")]}, {SENDER_COMUM: "EMB"},
                                            {SENDER_COMUM: 1.0}, "MOTORISTA", date(2026, 11, 11), saida)
            paginas = len(PdfReader(str(saida)).pages)
        self.assertEqual((stats["anexos"], stats["nfs"], paginas), (1, 0, 2))


if __name__ == "__main__":
    unittest.main()
