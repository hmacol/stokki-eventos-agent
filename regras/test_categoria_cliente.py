# -*- coding: utf-8 -*-
"""
Testes de regras/categoria_cliente.py (categoria do destinatario pelo
CNAE, Hugo 29/09).

    py -3.11 -m unittest regras.test_categoria_cliente -v
"""
import sqlite3
import sys
import unittest
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from regras import categoria_cliente as cc

CNPJ_BB = "00000000000191"          # empresa de verdade, mas fecha tambem como CPF com zeros
CPF_VALIDO = "52998224725"


class TipoDocumento(unittest.TestCase):
    def test_cnpj_valido(self):
        self.assertEqual(cc.tipo_documento("11.222.333/0001-81"), "cnpj")

    def test_cnpj_que_tambem_fecha_como_cpf(self):
        self.assertEqual(cc.tipo_documento("00.000.000/0001-91"), "ambiguo")

    def test_cpf_de_11_digitos(self):
        self.assertEqual(cc.tipo_documento("529.982.247-25"), "cpf")

    def test_cpf_preenchido_com_zeros_ate_14(self):
        self.assertEqual(cc.tipo_documento("000" + CPF_VALIDO), "cpf")

    def test_14_digitos_que_nao_sao_cnpj_nem_cpf(self):
        self.assertEqual(cc.tipo_documento("12345678000100"), "invalido")

    def test_vazio(self):
        self.assertEqual(cc.tipo_documento(""), "invalido")
        self.assertEqual(cc.tipo_documento(None), "invalido")


class CategoriaPorCnae(unittest.TestCase):
    def test_principais(self):
        casos = {
            5611201: "restaurante",
            5611203: "restaurante",
            5620102: "restaurante",
            5612100: "restaurante",
            4711302: "supermercado",
            4711301: "supermercado",
            4712100: "comercio_pequeno",
            4721102: "comercio_pequeno",
            4722901: "comercio_pequeno",
            4724500: "comercio_pequeno",
            4729699: "comercio_pequeno",
            1091102: "comercio_pequeno",
            4639701: "atacado",
            4634603: "atacado",
            4691500: "atacado",
        }
        for cnae, esperado in casos.items():
            with self.subTest(cnae=cnae):
                self.assertEqual(cc.categoria_por_cnae(cnae), (esperado, "cnae_principal"))

    def test_cnae_com_zero_a_esquerda_e_mascara(self):
        # 0153-9/01 (criacao de caprinos) chega da API como 153901
        self.assertEqual(cc.categoria_por_cnae(153901), ("outros", "cnae_principal"))
        self.assertEqual(cc.categoria_por_cnae("5611-2/01"), ("restaurante", "cnae_principal"))

    def test_principal_fora_usa_secundario(self):
        self.assertEqual(
            cc.categoria_por_cnae(5510801, [9313100, 5611201, 4711302]),
            ("restaurante", "cnae_secundario"),
        )

    def test_principal_reconhecido_ignora_secundario(self):
        self.assertEqual(
            cc.categoria_por_cnae(4711302, [5611201]),
            ("supermercado", "cnae_principal"),
        )

    def test_nada_reconhecido(self):
        self.assertEqual(cc.categoria_por_cnae(5510801, [9313100]), ("outros", "cnae_principal"))
        self.assertEqual(cc.categoria_por_cnae(None), ("outros", "cnae_principal"))


class Gravacao(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        cc.criar_tabela(self.conn)

    def tearDown(self):
        self.conn.close()

    def _resposta(self, cnae=5611201, secundarios=()):
        return {
            "cnae_fiscal": cnae,
            "cnae_fiscal_descricao": "Restaurantes e similares",
            "cnaes_secundarios": [{"codigo": s, "descricao": "x"} for s in secundarios],
            "porte": "MICRO EMPRESA",
            "descricao_situacao_cadastral": "ATIVA",
        }

    def test_grava_consulta(self):
        cc.gravar_consulta(self.conn, CNPJ_BB, self._resposta())
        linha = self.conn.execute("SELECT * FROM clientes_cnae").fetchone()
        self.assertEqual(linha["documento"], CNPJ_BB)
        self.assertEqual(linha["categoria"], "restaurante")
        self.assertEqual(linha["origem"], "cnae_principal")
        self.assertEqual(linha["cnae"], "5611201")
        self.assertEqual(linha["porte"], "MICRO EMPRESA")

    def test_pessoa_fisica_nao_precisa_de_consulta(self):
        cc.gravar_pessoa_fisica(self.conn, "000" + CPF_VALIDO)
        self.assertEqual(cc.carregar_categorias(self.conn), {"000" + CPF_VALIDO: "pessoa_fisica"})

    def test_manual_nao_e_sobrescrito_pela_carga(self):
        cc.definir_categoria_manual(self.conn, CNPJ_BB, "supermercado")
        cc.gravar_consulta(self.conn, CNPJ_BB, self._resposta())
        linha = self.conn.execute("SELECT * FROM clientes_cnae").fetchone()
        self.assertEqual(linha["categoria"], "supermercado")
        self.assertEqual(linha["origem"], "manual")
        # o dado bruto da Receita entra mesmo assim
        self.assertEqual(linha["cnae"], "5611201")

    def test_manual_em_cima_de_consulta_existente(self):
        cc.gravar_consulta(self.conn, CNPJ_BB, self._resposta())
        cc.definir_categoria_manual(self.conn, CNPJ_BB, "atacado")
        linha = self.conn.execute("SELECT * FROM clientes_cnae").fetchone()
        self.assertEqual((linha["categoria"], linha["origem"], linha["cnae"]), ("atacado", "manual", "5611201"))

    def test_categoria_manual_invalida(self):
        with self.assertRaises(ValueError):
            cc.definir_categoria_manual(self.conn, CNPJ_BB, "padaria")

    def test_erro_de_consulta_fica_sem_categoria(self):
        cc.gravar_erro(self.conn, CNPJ_BB, "http 404")
        self.assertEqual(cc.carregar_categorias(self.conn), {})
        self.assertEqual(cc.documentos_ja_resolvidos(self.conn), set())

    def test_documentos_ja_resolvidos(self):
        cc.gravar_consulta(self.conn, CNPJ_BB, self._resposta())
        self.assertEqual(cc.documentos_ja_resolvidos(self.conn), {CNPJ_BB})


if __name__ == "__main__":
    unittest.main()
