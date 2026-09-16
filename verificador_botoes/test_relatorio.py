# -*- coding: utf-8 -*-
"""Testes do relatorio em Markdown do verificador de botoes.

Rodar: py -3.11 -m unittest verificador_botoes.test_relatorio
"""
import unittest

from verificador_botoes.relatorio import gerar_markdown, resumir


def _resultado():
    return {
        "gerado_em": "2026-09-16 10:00",
        "servico": "painel_agentes",
        "telas": [
            {"caminho": "/torre", "endpoint": "torre", "botoes": [
                {"descricao": "button 'Tratar'", "seletor": "#b1", "pai": None,
                 "status": "OK", "detalhe": "POST /api/torre/tratar"},
                {"descricao": "button 'Rodar'", "seletor": "#b2", "pai": None,
                 "status": "ERRO_JS", "detalhe": "rodarX is not defined"},
                {"descricao": "button 'Fechar'", "seletor": "#b3", "pai": "button 'Tratar'",
                 "status": "SEM_EFEITO", "detalhe": ""},
            ]},
            {"caminho": "/planejamento", "endpoint": "planejamento", "botoes": [
                {"descricao": "a 'Mapa'", "seletor": "#l1", "pai": None,
                 "status": "LINK_QUEBRADO", "detalhe": "/mapa-velho"},
            ]},
            {"caminho": "/pdf", "endpoint": "pdf", "pulada": "nao e HTML (application/pdf)", "botoes": []},
        ],
    }


class TestResumir(unittest.TestCase):
    def test_conta_por_status(self):
        resumo = resumir(_resultado())
        self.assertEqual(resumo["total"], 4)
        self.assertEqual(resumo["por_status"]["OK"], 1)
        self.assertEqual(resumo["por_status"]["ERRO_JS"], 1)
        self.assertEqual(resumo["falhas"], 3)


class TestGerarMarkdown(unittest.TestCase):
    def test_tem_resumo_falhas_e_tela_pulada(self):
        md = gerar_markdown(_resultado())
        self.assertIn("4 botoes", md)
        self.assertIn("3 falhas", md)
        self.assertIn("## /torre", md)
        self.assertIn("rodarX is not defined", md)
        self.assertIn("/mapa-velho", md)
        self.assertIn("/pdf", md)
        self.assertIn("nao e HTML", md)

    def test_falhas_vem_antes_dos_ok(self):
        md = gerar_markdown(_resultado())
        self.assertLess(md.index("ERRO_JS"), md.index("| OK |"))

    def test_botao_de_modal_mostra_o_pai(self):
        md = gerar_markdown(_resultado())
        self.assertIn("via button 'Tratar'", md)


if __name__ == "__main__":
    unittest.main()
