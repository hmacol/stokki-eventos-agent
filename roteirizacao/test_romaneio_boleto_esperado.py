# -*- coding: utf-8 -*-
"""
Rodar (da raiz):  py -3.11 -m unittest roteirizacao.test_romaneio_boleto_esperado
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import gerar_pdf_romaneios as gpr  # noqa: E402

# SENDER_COMUM e PADRAO_PURO sao da canhoteira (NF dispensada); SENDER_COMUM nao.
DOURADO, PADRAO_PURO, SENDER_COMUM = 11426239, 12887364, 999


def _nf(cobranca, codigo="PS-1"):
    return {"tipo": "Nota Fiscal", "codigo_pedido": codigo, "numero_nf": "10", "origem": "stokki",
            "processado_em": "2026-10-05", "hash_conteudo": "h", "nome_arquivo": "PS-1_DANFE.pdf",
            "cobranca": cobranca}


def _abrir_tudo(rows):
    return [(r, None) for r in rows], []


class AvaliarDocumentos(unittest.TestCase):
    def _av(self, sender, docs):
        with patch.object(gpr, "_abrir_documentos", side_effect=_abrir_tudo):
            return gpr.avaliar_documentos_pedido("PS-1", sender, {"PS-1": docs})

    def test_nf_com_duplicata_sem_boleto_acusa(self):
        self.assertIn("sem boleto", self._av(SENDER_COMUM, [_nf(1)])["faltas"])

    def test_nf_sem_quadro_fora_da_lista_nao_acusa(self):
        r = self._av(SENDER_COMUM, [_nf(-1)])
        self.assertNotIn("sem boleto", r["faltas"])
        self.assertFalse(r["boleto_esperado"])

    def test_dourado_sem_quadro_acusa(self):
        self.assertIn("sem boleto", self._av(DOURADO, [_nf(None)])["faltas"])

    def test_nf_dispensada_continua_dispensada(self):
        r = self._av(PADRAO_PURO, [])
        self.assertTrue(r["nf_dispensada"])
        self.assertNotIn("sem nota fiscal", r["faltas"])

    def test_sem_nf_acusa_nf(self):
        self.assertIn("sem nota fiscal", self._av(SENDER_COMUM, [])["faltas"])


if __name__ == "__main__":
    unittest.main()
