# -*- coding: utf-8 -*-
"""
Remessa Itau (Pix, Sispag CNAB 240): chave Pix, arquivo e retorno. Modulo puro (sem banco).

    py -3.11 -m unittest test_remessa_itau   (de dentro de painel_agentes/)
"""
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

_AQUI = Path(__file__).parent
for _p in (_AQUI.parent, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import remessa_itau as ri  # noqa: E402

CRLF = chr(13) + chr(10)
CPF_OK = "52998224725"          # CPF valido de teste
CNPJ_OK = "11222333000181"      # CNPJ valido de teste


def cpf_com_dv(base9: str) -> str:
    """CPF valido a partir dos 9 primeiros digitos (calculo independente do modulo testado)."""
    nums = [int(c) for c in base9]
    for n in (9, 10):
        soma = sum(d * p for d, p in zip(nums, range(n + 1, 1, -1)))
        nums.append((soma * 10 % 11) % 10)
    return "".join(map(str, nums))


class TestChave(unittest.TestCase):
    def test_email_normaliza_e_usa_cpf_do_motorista(self):
        c = ri.classificar_chave(" Ana@Pix.com ", CPF_OK)
        self.assertEqual((c.tipo, c.valor, c.documento, c.tipo_inscricao), ("02", "ana@pix.com", CPF_OK, 1))

    def test_cpf_igual_ao_do_motorista(self):
        c = ri.classificar_chave(CPF_OK, CPF_OK)
        self.assertEqual((c.tipo, c.valor, c.tipo_inscricao), ("03", CPF_OK, 1))

    def test_cpf_com_pontuacao_igual_ao_do_motorista(self):
        c = ri.classificar_chave("529.982.247-25", CPF_OK)
        self.assertEqual((c.tipo, c.valor), ("03", CPF_OK))

    def test_cpf_de_outro_titular(self):
        outro = cpf_com_dv("123456789")
        r = ri.classificar_chave(outro, CPF_OK)
        self.assertIsInstance(r, str)
        self.assertIn("outro titular", r)

    def test_cnpj_do_mei_entra_com_documento_do_cnpj(self):
        c = ri.classificar_chave(CNPJ_OK, CPF_OK)
        self.assertEqual((c.tipo, c.valor, c.documento, c.tipo_inscricao), ("03", CNPJ_OK, CNPJ_OK, 2))

    def test_celular_com_cpf_invalido_vira_telefone(self):
        c = ri.classificar_chave("11987654321", CPF_OK)
        self.assertEqual((c.tipo, c.valor, c.documento), ("01", "+5511987654321", CPF_OK))

    def test_celular_com_55_com_e_sem_mais(self):
        for entrada in ("5511987654321", "+5511987654321"):
            c = ri.classificar_chave(entrada, CPF_OK)
            self.assertEqual((c.tipo, c.valor), ("01", "+5511987654321"), entrada)

    def test_foco1_onze_digitos_valido_como_cpf_e_como_celular_e_ambigua(self):
        ambigua = cpf_com_dv("119123456")          # DDD 11, 3o digito 9, DV de CPF valido
        self.assertNotEqual(ambigua, CPF_OK)
        r = ri.classificar_chave(ambigua, CPF_OK)
        self.assertIsInstance(r, str)
        self.assertIn("ambígua", r)

    def test_uuid_e_chave_aleatoria(self):
        c = ri.classificar_chave("123E4567-e89b-12d3-a456-426614174000", CPF_OK)
        self.assertEqual((c.tipo, c.valor), ("04", "123e4567-e89b-12d3-a456-426614174000"))

    def test_motivos_de_nao_elegivel(self):
        self.assertIn("sem chave", ri.classificar_chave("", CPF_OK))
        self.assertIn("sem chave", ri.classificar_chave(None, CPF_OK))
        for ruim in ("1234567", "abc", "12345678901234567"):
            self.assertIn("formato inválido", ri.classificar_chave(ruim, CPF_OK), ruim)

    def test_email_com_acento_nao_entra_e_diz_o_motivo(self):
        r = ri.classificar_chave("josé@exemplo.com.br", CPF_OK)
        self.assertIsInstance(r, str)
        self.assertIn("caracteres", r)

    def test_motorista_sem_cpf_valido_nao_entra_mesmo_com_email(self):
        r = ri.classificar_chave("ana@pix.com", "")
        self.assertIsInstance(r, str)
        self.assertIn("CPF", r)


def campo(linha: str, ini: int, fim: int) -> str:
    """Posicoes do manual (base 1, inclusivas)."""
    return linha[ini - 1:fim]


CONTA = ri.ContaPagadora(cnpj="12345678000195", agencia="1234", conta="123456", dac="7",
                         nome_empresa="FRESH LOG TRANSPORTES LTDA")
AGORA = datetime(2026, 11, 28, 10, 30, 15)


def itens_exemplo():
    return [
        ri.ItemRemessa(pagamento_id=17, nome="Ana Maria da Silva", valor=Decimal("340.10"),
                       chave=ri.classificar_chave("ana@pix.com", CPF_OK)),
        ri.ItemRemessa(pagamento_id=18, nome="Bruno Lima", valor=Decimal("1000.00"),
                       chave=ri.classificar_chave(CNPJ_OK, CPF_OK)),
    ]


class TestArquivo(unittest.TestCase):
    def setUp(self):
        self.texto = ri.montar_arquivo(CONTA, itens_exemplo(), date(2026, 11, 30), AGORA)
        self.linhas = self.texto.split(CRLF)
        self.assertEqual(self.linhas[-1], "")        # termina em CRLF
        self.linhas = self.linhas[:-1]

    def test_cada_registro_tem_240_colunas(self):
        self.assertEqual(len(self.linhas), 8)         # 0, 1, 2x(A+B), 5, 9
        for i, l in enumerate(self.linhas, 1):
            self.assertEqual(len(l), 240, f"linha {i}")

    def test_header_arquivo(self):
        h = self.linhas[0]
        self.assertEqual((campo(h, 1, 3), campo(h, 4, 7), campo(h, 8, 8)), ("341", "0000", "0"))
        self.assertEqual(campo(h, 15, 17), "080")
        self.assertEqual((campo(h, 18, 18), campo(h, 19, 32)), ("2", "12345678000195"))
        self.assertEqual((campo(h, 53, 57), campo(h, 59, 70), campo(h, 72, 72)), ("01234", "000000123456", "7"))
        self.assertEqual(campo(h, 73, 102).strip(), "FRESH LOG TRANSPORTES LTDA")
        self.assertEqual(campo(h, 143, 143), "1")
        self.assertEqual((campo(h, 144, 151), campo(h, 152, 157)), ("28112026", "103015"))

    def test_header_lote(self):
        h = self.linhas[1]
        self.assertEqual((campo(h, 4, 7), campo(h, 8, 8), campo(h, 9, 9)), ("0001", "1", "C"))
        self.assertEqual((campo(h, 10, 11), campo(h, 12, 13), campo(h, 14, 16)), ("20", "45", "040"))
        self.assertEqual((campo(h, 18, 18), campo(h, 19, 32)), ("2", "12345678000195"))
        self.assertEqual((campo(h, 53, 57), campo(h, 59, 70), campo(h, 72, 72)), ("01234", "000000123456", "7"))

    def test_tipo_de_pagamento_vem_da_conta(self):
        conta = ri.ContaPagadora(**{**CONTA.__dict__, "tipo_pagamento": "98"})
        h = ri.montar_arquivo(conta, itens_exemplo(), date(2026, 11, 30), AGORA).split(CRLF)[1]
        self.assertEqual(campo(h, 10, 11), "98")

    def test_segmento_a_e_b_do_primeiro_pagamento(self):
        a, b = self.linhas[2], self.linhas[3]
        self.assertEqual((campo(a, 4, 7), campo(a, 8, 8), campo(a, 9, 13), campo(a, 14, 14)), ("0001", "3", "00001", "A"))
        self.assertEqual((campo(a, 15, 17), campo(a, 18, 20), campo(a, 21, 23)), ("000", "009", "000"))
        self.assertEqual(campo(a, 44, 73).strip(), "ANA MARIA DA SILVA")
        self.assertEqual(campo(a, 74, 93).strip(), "17")
        self.assertEqual((campo(a, 94, 101), campo(a, 102, 104)), ("30112026", "REA"))
        self.assertEqual(campo(a, 113, 114), "04")
        self.assertEqual(campo(a, 120, 134), "000000000034010")
        self.assertEqual(campo(a, 204, 217), CPF_OK.rjust(14, "0"))
        self.assertEqual((campo(a, 155, 162), campo(a, 163, 177)), ("0" * 8, "0" * 15))
        self.assertEqual((campo(b, 9, 13), campo(b, 14, 14)), ("00001", "B"))
        self.assertEqual((campo(b, 15, 16), campo(b, 18, 18), campo(b, 19, 32)), ("02", "1", CPF_OK.rjust(14, "0")))
        self.assertEqual(campo(b, 128, 227).strip(), "ana@pix.com")

    def test_segundo_pagamento_cnpj_e_sequencial(self):
        a, b = self.linhas[4], self.linhas[5]
        self.assertEqual((campo(a, 9, 13), campo(b, 9, 13)), ("00002", "00002"))
        self.assertEqual(campo(a, 74, 93).strip(), "18")
        self.assertEqual(campo(a, 120, 134), "000000000100000")
        self.assertEqual(campo(a, 204, 217), CNPJ_OK)
        self.assertEqual((campo(b, 15, 16), campo(b, 18, 18), campo(b, 19, 32)), ("03", "2", CNPJ_OK))

    def test_trailers(self):
        tl, ta = self.linhas[6], self.linhas[7]
        self.assertEqual((campo(tl, 8, 8), campo(tl, 18, 23), campo(tl, 24, 41)), ("5", "000006", "000000000000134010"[-18:]))
        self.assertEqual(campo(tl, 24, 41), "000000000000134010")
        self.assertEqual((campo(ta, 4, 7), campo(ta, 8, 8), campo(ta, 18, 23), campo(ta, 24, 29)), ("9999", "9", "000001", "000008"))

    def test_foco2_nome_com_acento_cedilha_e_mais_de_30(self):
        n = ri.limpar_nome("José da Conceição Ñandú Filho Júnior da Silva", 30)
        self.assertLessEqual(len(n), 30)
        self.assertEqual(n, "JOSE DA CONCEICAO NANDU FILHO")
        self.assertTrue(n.isascii() and n == n.upper())
        self.assertEqual(ri.limpar_nome("  maria   d'ávila-souza ", 30), "MARIA D AVILA SOUZA")

    def test_foco3_centavos_exatos(self):
        self.assertEqual(ri.para_centavos(Decimal("340.10")), 34010)
        self.assertEqual(ri.para_centavos(0.1 + 0.2), 30)
        self.assertEqual(ri.para_centavos(340.1), 34010)
        self.assertEqual(ri.para_centavos(Decimal("0.005")), 1)

    def test_data_anterior_a_hoje_e_lista_vazia_rejeitam(self):
        with self.assertRaises(ValueError):
            ri.montar_arquivo(CONTA, itens_exemplo(), date(2026, 11, 27), AGORA)
        with self.assertRaises(ValueError):
            ri.montar_arquivo(CONTA, [], date(2026, 11, 30), AGORA)

    def test_valor_zero_ou_negativo_rejeita(self):
        item = itens_exemplo()[0]
        item.valor = Decimal("0")
        with self.assertRaises(ValueError):
            ri.montar_arquivo(CONTA, [item], date(2026, 11, 30), AGORA)


if __name__ == "__main__":
    unittest.main()
