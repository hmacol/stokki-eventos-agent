# -*- coding: utf-8 -*-
"""
Informativo de regiões aos embarcadores (dias fixos v2, Hugo 03/10).
Rodar (da raiz): py -3.11 -m unittest test_gerar_informativo_regioes -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import gerar_informativo_regioes as gir
import regioes_dia_fixo as rdf


class TestConteudo(unittest.TestCase):
    def test_tem_todas_as_regioes_e_pontos(self):
        nomes = [l["regiao"] for l in gir.linhas_informativo()]
        for r in rdf.REGIOES + rdf.ENDERECOS_DIA_FIXO:
            self.assertIn(r["nome"], nomes)

    def test_dias_frequencia_e_prazo(self):
        linhas = {l["regiao"]: l for l in gir.linhas_informativo()}
        self.assertEqual((linhas["ABCD"]["dias"], linhas["ABCD"]["frequencia"], linhas["ABCD"]["prazo"]),
                         ("Segundas e Quintas", "2x por semana", "até 3 dias úteis"))
        self.assertEqual((linhas["Campinas"]["frequencia"], linhas["Campinas"]["prazo"]),
                         ("Semanal", "até 7 dias corridos"))
        self.assertEqual((linhas["Sorocaba"]["frequencia"], linhas["Sorocaba"]["prazo"]),
                         ("Quinzenal", "até 15 dias corridos"))
        self.assertIn("a cada 15 dias", linhas["Sorocaba"]["dias"])
        self.assertIn("São José dos Campos", linhas["Vale do Paraíba"]["cidades"])

    def test_nome_bonito(self):
        self.assertEqual(gir.nome_bonito("SAO JOSE DOS CAMPOS"), "São José dos Campos")
        self.assertEqual(gir.nome_bonito("SANTANA DO PARNAIBA"), "Santana do Parnaíba")
        self.assertEqual(gir.nome_bonito("JACAREI"), "Jacareí")


class TestPdf(unittest.TestCase):
    def test_gera_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            caminho = gir.gerar_pdf(gir.linhas_informativo(), Path(tmp) / "x" / "informativo.pdf")
            self.assertTrue(caminho.read_bytes().startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
