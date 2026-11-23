# -*- coding: utf-8 -*-
"""
test_tipo_veiculo_folga.py

Rota que recebeu pedido de rota fraca pode chegar a 110 caixas (folga da
juncao, roteirizacao/rotas_fracas.py) e continua sendo Fiorino (decisao
do Hugo, 03/10): classificar_tipo_veiculo_com_folga so manda pro veiculo
grande acima de 100 + folga.

Rodar (da raiz):
    py -3.11 -m unittest regras.test_tipo_veiculo_folga -v
"""
import unittest

from regras.tipo_veiculo import classificar_tipo_veiculo, classificar_tipo_veiculo_com_folga


class TestClassificarComFolga(unittest.TestCase):
    def test_100_e_110_continuam_fiorino_com_folga_10(self):
        self.assertIsNone(classificar_tipo_veiculo_com_folga(100, 3, folga_fiorino_cx=10))
        self.assertIsNone(classificar_tipo_veiculo_com_folga(110, 3, folga_fiorino_cx=10))

    def test_111_vira_van_hr_com_folga_10(self):
        self.assertEqual(classificar_tipo_veiculo_com_folga(111, 3, folga_fiorino_cx=10).codigo, "VAN_HR")

    def test_folga_zero_e_identica_a_classificacao_antiga(self):
        for caixas in (0, 50, 100, 101, 105, 110, 111, 400, 401, 1300, 2600):
            for enderecos in (1, 2, 3, 4, 5):
                self.assertEqual(classificar_tipo_veiculo_com_folga(caixas, enderecos),
                                 classificar_tipo_veiculo(caixas, enderecos), (caixas, enderecos))

    def test_acima_da_folga_segue_a_regra_de_enderecos(self):
        # 111 caixas em 5 enderecos nao cabe em VAN/HR (max 4): mesma resposta de antes
        self.assertEqual(classificar_tipo_veiculo_com_folga(111, 5, folga_fiorino_cx=10),
                         classificar_tipo_veiculo(111, 5))


if __name__ == "__main__":
    unittest.main()
