# -*- coding: utf-8 -*-
"""
test_tipo_veiculo_fiorino.py

FIORINO entrou no catalogo em 22/09/2026 (pedido do Hugo) como o veiculo
padrao da ultima milha, com 100 caixas -- o mesmo teto da rota comum
(VOLUME_MAXIMO_ROTA). Estes testes travam as duas propriedades que nao
podem quebrar:

  1. FIORINO NUNCA sai de classificar_tipo_veiculo. Esse retorno e o
     gatilho de "vira rota exclusiva de veiculo grande" em ~12 pontos do
     pipeline -- se FIORINO vazar, toda rota comum vira exclusiva.
  2. As faixas ficaram CONTIGUAS (o volume_minimo virou o teto do tipo
     anterior), fechando o buraco de 101-149 caixas que nao tinha veiculo.

Rodar (da raiz):
    py -3.11 -m unittest regras.test_tipo_veiculo_fiorino -v
"""
import unittest

from regras.tipo_veiculo import (
    TIPOS_VEICULO,
    TIPOS_VEICULO_EXCLUSIVOS,
    classificar_tipo_veiculo,
    teto_caixas_para_enderecos,
    tipo_por_codigo,
    veiculo_comporta,
)


class TestFiorinoNoCatalogo(unittest.TestCase):
    def test_fiorino_existe_e_e_o_menor(self):
        self.assertEqual(TIPOS_VEICULO[0].codigo, "FIORINO")
        self.assertEqual(TIPOS_VEICULO[0].volume_maximo_cx, 100)
        self.assertEqual(len(TIPOS_VEICULO), 5)

    def test_fiorino_fora_dos_exclusivos(self):
        codigos = [t.codigo for t in TIPOS_VEICULO_EXCLUSIVOS]
        self.assertNotIn("FIORINO", codigos)
        self.assertEqual(codigos, ["VAN_HR", "VUC", "TRES_QUARTOS", "TRUCK"])

    def test_apelidos_resolvem(self):
        for apelido in ("FIORINO", "fiorino", "FIO", "UTILITARIO"):
            self.assertEqual(tipo_por_codigo(apelido).codigo, "FIORINO", apelido)


class TestClassificacaoNuncaDevolveFiorino(unittest.TestCase):
    def test_nenhuma_entrada_classifica_fiorino(self):
        for caixas in (0, 1, 50, 99, 100, 101, 400, 401, 2500, 2501, 9999):
            for enderecos in (1, 2, 3, 4, 5):
                tipo = classificar_tipo_veiculo(caixas, enderecos)
                if tipo is not None:
                    self.assertNotEqual(tipo.codigo, "FIORINO", f"{caixas}cx/{enderecos}end")

    def test_ate_o_teto_da_rota_comum_nao_ha_veiculo_grande(self):
        for caixas in (0, 1, 50, 99, 100):
            self.assertIsNone(classificar_tipo_veiculo(caixas, 1), f"{caixas}cx")


class TestFaixasContiguas(unittest.TestCase):
    def test_faixas_por_caixas_com_um_endereco(self):
        esperado = [
            (101, "VAN_HR"), (149, "VAN_HR"), (400, "VAN_HR"),
            (401, "VUC"), (600, "VUC"),
            (601, "TRES_QUARTOS"), (1200, "TRES_QUARTOS"),
            (1201, "TRUCK"), (2500, "TRUCK"),
        ]
        for caixas, codigo in esperado:
            tipo = classificar_tipo_veiculo(caixas, 1)
            self.assertIsNotNone(tipo, f"{caixas}cx deveria classificar")
            self.assertEqual(tipo.codigo, codigo, f"{caixas}cx")

    def test_acima_do_truck_nao_classifica(self):
        self.assertIsNone(classificar_tipo_veiculo(2501, 1))

    def test_buraco_de_101_a_149_fechou(self):
        # Era o caso quebrado ate 22/09: 2 pedidos de 60cx no mesmo
        # endereco (120cx) estouravam o teto de 100 da rota comum e nao
        # alcancavam o minimo de 150 da VAN/HR -- ficavam sem veiculo.
        self.assertEqual(classificar_tipo_veiculo(120, 1).codigo, "VAN_HR")


class TestTetoPorEnderecos(unittest.TestCase):
    def test_teto_ignora_fiorino(self):
        # Truck (2500) comporta ate 2 enderecos; acima disso o teto cai
        # pro maior tipo de 4 enderecos (3/4, 1200).
        self.assertEqual(teto_caixas_para_enderecos(1), 2500)
        self.assertEqual(teto_caixas_para_enderecos(2), 2500)
        self.assertEqual(teto_caixas_para_enderecos(3), 1200)
        self.assertEqual(teto_caixas_para_enderecos(4), 1200)
        self.assertEqual(teto_caixas_para_enderecos(5), 0)


class TestVeiculoComporta(unittest.TestCase):
    def test_fiorino_serve_rota_comum(self):
        self.assertTrue(veiculo_comporta("FIORINO", None))

    def test_fiorino_nao_serve_veiculo_grande(self):
        for necessario in ("VAN_HR", "VUC", "TRES_QUARTOS", "TRUCK"):
            self.assertFalse(veiculo_comporta("FIORINO", necessario), necessario)

    def test_veiculo_grande_so_serve_o_proprio_porte(self):
        # Hugo, 25/09: quem nao e Fiorino so pega rota do porte exato.
        for codigo in ("VAN_HR", "VUC", "TRES_QUARTOS", "TRUCK"):
            self.assertTrue(veiculo_comporta(codigo, codigo), codigo)
            self.assertFalse(veiculo_comporta(codigo, None), codigo)
            self.assertFalse(veiculo_comporta(codigo, "FIORINO"), codigo)

    def test_maior_nao_cobre_menor(self):
        self.assertFalse(veiculo_comporta("TRUCK", "VUC"))
        self.assertFalse(veiculo_comporta("VUC", "VAN_HR"))

    def test_menor_nao_cobre_maior(self):
        self.assertFalse(veiculo_comporta("VAN_HR", "VUC"))

    def test_sem_tipo_so_serve_rota_comum(self):
        self.assertTrue(veiculo_comporta(None, None))
        self.assertFalse(veiculo_comporta(None, "VAN_HR"))
        self.assertTrue(veiculo_comporta("XYZ", None))
        self.assertFalse(veiculo_comporta("XYZ", "VAN_HR"))


if __name__ == "__main__":
    unittest.main()
