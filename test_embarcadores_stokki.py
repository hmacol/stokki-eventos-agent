# -*- coding: utf-8 -*-
"""Rotina de embarcadores novos da Stokki (embarcadores_stokki.py). Sem
rede: Stokki, Vuupt, OpenWA e e-mail substituidos.

Rodar: py -3.11 -m unittest test_embarcadores_stokki
"""
import unittest

import embarcadores_stokki as rot

HTML_EDIT = '''<form><input type="text" class="form-control" name="name" value="FABENNE">
<input type="text" name="cellphone" class="form-control phone" value="(11) 999 999 999">
<input type="text" name="phone" class="form-control phone"  value="" >
<input type="email" name="email" value="Mariana@Fabenne.com">
<input type="text" name="company_phone" value="(11) 7646 5184">
<input type="text" name="company_email[]" value="mariana@fabenne.com">
<input type="text" name="city" value="S&atilde;o Paulo"></form>'''


class TestRotulo(unittest.TestCase):
    def test_com_apelido(self):
        self.assertEqual(
            rot.interpretar_rotulo("TERRA NUTRI (RIC INDUSTRIA E COMERCIO LTDA - 02.368.118/0006-07)",
                                   "RIC INDUSTRIA E COMERCIO LTDA"),
            ("TERRA NUTRI", "RIC INDUSTRIA E COMERCIO LTDA", "02368118000607"))

    def test_sem_apelido_quando_repete_a_razao(self):
        self.assertEqual(
            rot.interpretar_rotulo("COGUMELADO ALIMENTOS LTDA (COGUMELADO ALIMENTOS LTDA - 29.909.190/0001-46)",
                                   "COGUMELADO ALIMENTOS LTDA"),
            (None, "COGUMELADO ALIMENTOS LTDA", "29909190000146"))

    def test_parenteses_dentro_da_razao(self):
        self.assertEqual(
            rot.interpretar_rotulo("PADOCA (PADARIA (FILIAL 2) LTDA - 11.111.111/0001-11)", "PADARIA (FILIAL 2) LTDA"),
            ("PADOCA", "PADARIA (FILIAL 2) LTDA", "11111111000111"))

    def test_fora_do_padrao_fica_sem_cnpj(self):
        self.assertEqual(rot.interpretar_rotulo("QUALQUER COISA", " X LTDA "), (None, "X LTDA", ""))

    def test_clientes_do_select(self):
        bruto = {"success": True, "units": [], "clients": [
            {"id": 100, "name": "NONNA DISTRIBUICAO E COMERCIO DIGITAL LTDA",
             "select_label": "FABENNE (NONNA DISTRIBUICAO E COMERCIO DIGITAL LTDA - 49.874.051/0001-31)"}]}
        self.assertEqual(rot.clientes_do_select(bruto), [
            {"stkkc_id": 100, "apelido": "FABENNE", "razao": "NONNA DISTRIBUICAO E COMERCIO DIGITAL LTDA",
             "cnpj": "49874051000131"}])
        self.assertEqual(rot.clientes_do_select({"clients": []}), [])


class TestContato(unittest.TestCase):
    def test_extrai_da_tela_de_edicao(self):
        self.assertEqual(rot.extrair_contato(HTML_EDIT), {
            "celular": "(11) 999 999 999", "telefone": "(11) 7646 5184",
            "email": "mariana@fabenne.com", "cidade": "São Paulo"})

    def test_html_vazio(self):
        self.assertEqual(rot.extrair_contato(""), {"celular": "", "telefone": "", "email": "", "cidade": ""})

    def test_celular_whatsapp(self):
        casos = {"(11) 999 999 999": "", "(11) 98888-7777": "5511988887777", "": "",
                 "(11) 7646 5184": "", "+55 21 99876-5432": "5521998765432", "abc": ""}
        for entrada, esperado in casos.items():
            self.assertEqual(rot.celular_whatsapp(entrada), esperado, entrada)


if __name__ == "__main__":
    unittest.main()
