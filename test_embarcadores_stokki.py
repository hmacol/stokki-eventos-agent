# -*- coding: utf-8 -*-
"""Rotina de embarcadores novos da Stokki (embarcadores_stokki.py). Sem
rede: Stokki, Vuupt, OpenWA e e-mail substituidos.

Rodar: py -3.11 -m unittest test_embarcadores_stokki
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import cadastro_embarcador as cad
import embarcadores_stokki as rot
import notificar_whatsapp as nw

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


DDL_INTERNO = ("CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, habilidade TEXT, nome_remetente TEXT, "
               "apelido TEXT, fator_ponderado REAL, observacoes TEXT, criado_em TEXT, atualizado_em TEXT, "
               "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)")
DDL_NUCLEO = ("CREATE TABLE nucleo_pedidos (codigo TEXT PRIMARY KEY, remetente_codigo TEXT, sender_id INTEGER, "
              "criado_em TEXT)")
AGORA = datetime(2026, 10, 10, 9, 10, 0)
COGUMELADO = {"stkkc_id": 7, "apelido": "COGUMELADO", "razao": "COGUMELADO ALIMENTOS LTDA", "cnpj": "29909190000146"}
NONNA = {"stkkc_id": 100, "apelido": "FABENNE", "razao": "NONNA DISTRIBUICAO E COMERCIO DIGITAL LTDA",
         "cnpj": "49874051000131"}
RIC = {"stkkc_id": 56, "apelido": "TERRA NUTRI", "razao": "RIC INDUSTRIA E COMERCIO LTDA", "cnpj": "02368118000607"}
ANTIGO = {"stkkc_id": 23, "apelido": None, "razao": "ANTIGO COMERCIO LTDA", "cnpj": "33333333000133"}
CONTATOS = {
    100: '<input name="cellphone" value="(11) 98888-7777"><input name="email" value="mariana@fabenne.com">',
    56: '<input name="cellphone" value="(11) 999 999 999"><input name="email" value="contato@ric.com">',
}


def _config(**extra):
    c = {"whatsapp_notificacoes": {"ativo": True, "base_url": "http://x/api", "api_key": "k", "sessao": "s",
                                   "grupo_id": "1@g.us", "teto_diario": 50, "intervalo_min_seg": 0,
                                   "clientes": {"ativo": True, "teto_diario": 30, "forcar_destino": ""}},
         "portal_cliente": {"url_base": "https://app.freshhub.com.br/cliente", "secret_key": "segredo-de-teste"},
         "cadastro_embarcador": {"forcar_destino_email": ""}, "email": {}}
    c.update(extra)
    return c


class _ComBanco(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        mock.patch.object(rot.auth, "DB_PATH", Path(self._tmp.name) / "t.db").start()
        self.enviar = mock.patch.object(nw.integracao_openwa, "enviar_texto", return_value=(True, "m1")).start()
        self.existe = mock.patch.object(nw.integracao_openwa, "numero_existe", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        self.conn = rot.auth.conectar()
        self.addCleanup(self.conn.close)
        self.conn.execute(DDL_INTERNO)
        self.conn.execute(DDL_NUCLEO)
        self.conn.execute("INSERT INTO interno (cnpj_embarcador, nome_remetente, apelido, sender_id, stkkc_id) "
                          "VALUES ('29909190000146', 'COGUMELADO ALIMENTOS LTDA', 'COGUMELADO', 21000001, 7)")
        self.conn.commit()
        self.email = mock.MagicMock(return_value=True)
        self.dormir = mock.MagicMock()

    def pedido(self, codigo, cnpj_formatado, sender, dias_atras):
        quando = (AGORA - timedelta(days=dias_atras)).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute("INSERT INTO nucleo_pedidos VALUES (?, ?, ?, ?)", (codigo, cnpj_formatado, sender, quando))
        self.conn.commit()

    def interno(self, cnpj):
        cur = self.conn.execute("SELECT * FROM interno WHERE cnpj_embarcador = ?", (cnpj,))
        row = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], tuple(row))) if row else None


class TestDeteccao(_ComBanco):
    def test_separar(self):
        self.conn.execute("INSERT INTO interno (cnpj_embarcador, nome_remetente, stkkc_id) VALUES ('02368118000607', 'RIC', NULL)")
        self.conn.execute("INSERT INTO interno (cnpj_embarcador, nome_remetente, stkkc_id) VALUES ('33333333000133', 'OUTRO', 99)")
        sem_cnpj = {"stkkc_id": 70, "apelido": None, "razao": "SEM CNPJ", "cnpj": ""}
        g = rot.separar([COGUMELADO, NONNA, RIC, ANTIGO, sem_cnpj], self.conn)
        self.assertEqual([c["stkkc_id"] for c in g["novos"]], [100])
        self.assertEqual([c["stkkc_id"] for c in g["sem_stkkc"]], [56])
        self.assertEqual([(c["stkkc_id"], outro) for c, outro in g["conflitos"]], [(23, 99)])
        self.assertEqual([c["stkkc_id"] for c in g["ignorados"]], [70])

    def test_pedido_recente_compara_cnpj_formatado_e_30_dias(self):
        self.pedido("PS-1", "49.874.051/0001-31", 23084712, 29)
        self.pedido("PS-2", "02.368.118/0006-07", 22872019, 31)
        self.assertTrue(rot.tem_pedido_recente(self.conn, NONNA["cnpj"], AGORA))
        self.assertFalse(rot.tem_pedido_recente(self.conn, RIC["cnpj"], AGORA))

    def test_pedido_recente_sem_tabela_do_nucleo(self):
        self.conn.execute("DROP TABLE nucleo_pedidos")
        self.assertFalse(rot.tem_pedido_recente(self.conn, NONNA["cnpj"], AGORA))
        self.assertIsNone(rot.sender_do_nucleo(self.conn, NONNA["cnpj"]))

    def test_sender_do_pedido_mais_antigo(self):
        self.pedido("PS-2", "49.874.051/0001-31", 23084712, 1)
        self.pedido("PS-1", "49.874.051/0001-31", 23072631, 5)
        self.pedido("PS-0", "49.874.051/0001-31", None, 9)
        vuupt = mock.MagicMock()
        self.assertEqual(rot.resolver_sender(self.conn, NONNA["cnpj"], NONNA["razao"], vuupt), 23072631)
        vuupt.criar_customer.assert_not_called()

    def test_sem_pedido_cria_customer(self):
        vuupt = mock.MagicMock()
        vuupt.criar_customer.return_value = {"customer": {"id": 555}}
        self.assertEqual(rot.resolver_sender(self.conn, ANTIGO["cnpj"], ANTIGO["razao"], vuupt), 555)
        vuupt.criar_customer.assert_called_once_with({"name": "ANTIGO COMERCIO LTDA", "code": "33.333.333/0001-33"})

    def test_falha_ou_sem_vuupt_devolve_none(self):
        vuupt = mock.MagicMock()
        vuupt.criar_customer.side_effect = RuntimeError("422")
        self.assertIsNone(rot.resolver_sender(self.conn, ANTIGO["cnpj"], ANTIGO["razao"], vuupt))
        self.assertIsNone(rot.resolver_sender(self.conn, ANTIGO["cnpj"], ANTIGO["razao"], None))

    def test_pre_cadastrar_ativo_e_inativo(self):
        rot.pre_cadastrar(self.conn, NONNA, rot.extrair_contato(CONTATOS[100]), True, AGORA)
        rot.pre_cadastrar(self.conn, ANTIGO, rot.extrair_contato(""), False, AGORA)
        n = self.interno(NONNA["cnpj"])
        self.assertEqual((n["stkkc_id"], n["nome_remetente"], n["apelido"], n["email"], n["habilidade"],
                          n["notificar_email"], n["sender_id"], n["fator_ponderado"]),
                         (100, NONNA["razao"], "FABENNE", "mariana@fabenne.com", None, 1, None, 1.0))
        self.assertIn("Pré-cadastro automático 10/10/2026 (Stokki #stkkc-100)", n["observacoes"])
        a = self.interno(ANTIGO["cnpj"])
        self.assertEqual((a["notificar_email"], a["email"]), (0, None))
        self.assertIn("inativo", a["observacoes"])
        self.assertEqual(cad.buscar(self.conn, NONNA["cnpj"])["situacao"], "PENDENTE")
        self.assertEqual(cad.buscar(self.conn, NONNA["cnpj"])["celular_stokki"], "(11) 98888-7777")
        self.assertEqual(cad.buscar(self.conn, ANTIGO["cnpj"])["situacao"], "INATIVO")


if __name__ == "__main__":
    unittest.main()
