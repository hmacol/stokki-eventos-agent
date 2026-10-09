# -*- coding: utf-8 -*-
"""py -3.11 -m unittest test_expedir_canhoto_manual"""
import unittest
from pathlib import Path
from unittest import mock

import expedir_pedidos as ep


class PdfDoCanhoto(unittest.TestCase):
    def test_vuupt_tem_prioridade(self):
        with mock.patch.object(ep, "tem_canhoto", return_value=True), \
             mock.patch.object(ep, "extrair_checklist_id", return_value=9), \
             mock.patch.object(ep, "baixar_canhoto_pdf", return_value=Path("vuupt.pdf")), \
             mock.patch("nucleo.canhotos_manuais.caminho_canhoto_manual") as manual:
            self.assertEqual(ep.pdf_do_canhoto("t", {}, "#PS-1"), Path("vuupt.pdf"))
        manual.assert_not_called()

    def test_sem_vuupt_usa_manual_pelo_codigo_exato(self):
        with mock.patch.object(ep, "tem_canhoto", return_value=False), \
             mock.patch("nucleo.canhotos_manuais.caminho_canhoto_manual", return_value=Path("m.pdf")) as manual:
            self.assertEqual(ep.pdf_do_canhoto("t", {}, "#PS-1-R1"), Path("m.pdf"))
        manual.assert_called_once_with("#PS-1-R1")

    def test_nenhum(self):
        with mock.patch.object(ep, "tem_canhoto", return_value=False), \
             mock.patch("nucleo.canhotos_manuais.caminho_canhoto_manual", return_value=None):
            self.assertIsNone(ep.pdf_do_canhoto("t", {}, "#PS-2"))


if __name__ == "__main__":
    unittest.main()
