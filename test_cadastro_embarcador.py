# -*- coding: utf-8 -*-
"""Estado do pre-cadastro automatico de embarcador (cadastro_embarcador.py).

Rodar: py -3.11 -m unittest test_cadastro_embarcador
"""
import sqlite3
import unittest
from datetime import datetime

import cadastro_embarcador as cad

DDL_INTERNO = ("CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, habilidade TEXT, nome_remetente TEXT, "
               "apelido TEXT, fator_ponderado REAL, observacoes TEXT, criado_em TEXT, atualizado_em TEXT, "
               "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)")
CNPJ = "49874051000131"
AGORA = datetime(2026, 10, 10, 8, 0, 0)


class _Base(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(DDL_INTERNO)
        self.conn.execute("INSERT INTO interno (cnpj_embarcador, nome_remetente, apelido, sender_id, stkkc_id) "
                          "VALUES (?, 'NONNA DISTRIBUICAO', 'FABENNE', 23084712, 100)", (CNPJ,))
        self.conn.commit()

    def habilidade_interno(self):
        return self.conn.execute("SELECT habilidade FROM interno WHERE cnpj_embarcador = ?", (CNPJ,)).fetchone()[0]


class TestRegistro(_Base):
    def test_registrar_normaliza_cnpj_e_devolve_a_linha(self):
        c = cad.registrar(self.conn, "49.874.051/0001-31", 100, cad.PENDENTE, celular="(11) 98888-7777",
                          email="m@fabenne.com", agora=AGORA)
        self.assertEqual((c["cnpj_embarcador"], c["stkkc_id"], c["situacao"], c["celular_stokki"]),
                         (CNPJ, 100, "PENDENTE", "(11) 98888-7777"))
        self.assertEqual(c["criado_em"], "2026-10-10 08:00:00")

    def test_registrar_de_novo_nao_sobrescreve(self):
        cad.registrar(self.conn, CNPJ, 100, cad.PENDENTE, agora=AGORA)
        c = cad.registrar(self.conn, CNPJ, 100, cad.INATIVO, agora=AGORA)
        self.assertEqual(c["situacao"], "PENDENTE")

    def test_atualizar_recusa_coluna_desconhecida(self):
        cad.registrar(self.conn, CNPJ, 100, cad.PENDENTE, agora=AGORA)
        with self.assertRaises(ValueError):
            cad.atualizar(self.conn, CNPJ, AGORA, cnpj_embarcador="1")

    def test_buscar_inexistente(self):
        self.assertIsNone(cad.buscar(self.conn, "11111111000111"))


class TestPrecisaCompletar(_Base):
    def test_situacoes(self):
        self.assertFalse(cad.precisa_completar(self.conn, CNPJ))
        cad.registrar(self.conn, CNPJ, 100, cad.CONVIDADO, agora=AGORA)
        self.assertTrue(cad.precisa_completar(self.conn, CNPJ))
        for situacao, esperado in ((cad.REENVIADO, True), (cad.SEM_CONTATO, True), (cad.PENDENTE, True),
                                   (cad.PREENCHIDO, False), (cad.APROVADO, False), (cad.INATIVO, False)):
            cad.atualizar(self.conn, CNPJ, AGORA, situacao=situacao)
            self.assertEqual(cad.precisa_completar(self.conn, CNPJ), esperado, situacao)


class TestPreenchimentoEAprovacao(_Base):
    def setUp(self):
        super().setUp()
        cad.registrar(self.conn, CNPJ, 100, cad.CONVIDADO, agora=AGORA)

    def test_preencher_nao_mexe_em_interno(self):
        c = cad.salvar_preenchimento(self.conn, CNPJ, " refrigerado ", agora=AGORA)
        self.assertEqual((c["situacao"], c["habilidade_informada"]), ("PREENCHIDO", "Refrigerado"))
        self.assertIsNone(self.habilidade_interno())

    def test_tipo_invalido(self):
        with self.assertRaises(cad.CadastroInvalido):
            cad.salvar_preenchimento(self.conn, CNPJ, "Quente", agora=AGORA)
        self.assertEqual(cad.buscar(self.conn, CNPJ)["situacao"], "CONVIDADO")

    def test_aprovar_grava_habilidade_e_quem(self):
        cad.salvar_preenchimento(self.conn, CNPJ, "Seco", agora=AGORA)
        c = cad.aprovar(self.conn, CNPJ, "hugo", agora=AGORA)
        self.assertEqual((c["situacao"], c["aprovado_por"]), ("APROVADO", "hugo"))
        self.assertEqual(self.habilidade_interno(), "Seco")

    def test_aprovar_com_outro_tipo(self):
        cad.salvar_preenchimento(self.conn, CNPJ, "Seco", agora=AGORA)
        c = cad.aprovar(self.conn, CNPJ, "hugo", habilidade="congelado", agora=AGORA)
        self.assertEqual((c["habilidade_informada"], self.habilidade_interno()), ("Congelado", "Congelado"))

    def email_interno(self):
        return self.conn.execute("SELECT email FROM interno WHERE cnpj_embarcador = ?", (CNPJ,)).fetchone()[0]

    def test_aprovar_preenche_interno_email_vazio_com_o_da_stokki(self):
        self.conn.execute("UPDATE cadastro_embarcador SET email_stokki = 'mariana@fabenne.com' WHERE cnpj_embarcador = ?",
                          (CNPJ,))
        self.conn.commit()
        self.assertIsNone(self.email_interno())
        cad.salvar_preenchimento(self.conn, CNPJ, "Seco", agora=AGORA)
        cad.aprovar(self.conn, CNPJ, "hugo", agora=AGORA)
        self.assertEqual(self.email_interno(), "mariana@fabenne.com")

    def test_aprovar_nao_sobrescreve_interno_email_existente(self):
        self.conn.execute("UPDATE cadastro_embarcador SET email_stokki = 'mariana@fabenne.com' WHERE cnpj_embarcador = ?",
                          (CNPJ,))
        self.conn.execute("UPDATE interno SET email = 'antigo@fabenne.com' WHERE cnpj_embarcador = ?", (CNPJ,))
        self.conn.commit()
        cad.salvar_preenchimento(self.conn, CNPJ, "Seco", agora=AGORA)
        cad.aprovar(self.conn, CNPJ, "hugo", agora=AGORA)
        self.assertEqual(self.email_interno(), "antigo@fabenne.com")

    def test_preencher_troca_interno_email_pelo_do_cliente_sem_esperar_aprovacao(self):
        import preferencias_notificacao as pn
        self.conn.execute("UPDATE interno SET email = 'stokki@fabenne.com' WHERE cnpj_embarcador = ?", (CNPJ,))
        self.conn.commit()
        pn.salvar(self.conn, CNPJ, ["compras@fabenne.com", "log@fabenne.com"], {}, "cliente")
        cad.salvar_preenchimento(self.conn, CNPJ, "Seco", agora=AGORA)
        self.assertEqual(self.email_interno(), "compras@fabenne.com, log@fabenne.com")
        self.assertIsNone(self.habilidade_interno())

    def test_aprovar_sem_preenchimento_da_409(self):
        with self.assertRaises(cad.CadastroInvalido) as ctx:
            cad.aprovar(self.conn, CNPJ, "hugo", agora=AGORA)
        self.assertEqual(ctx.exception.codigo, 409)
        self.assertIsNone(self.habilidade_interno())

    def test_aprovar_inexistente_da_404(self):
        with self.assertRaises(cad.CadastroInvalido) as ctx:
            cad.aprovar(self.conn, "11111111000111", "hugo", habilidade="Seco")
        self.assertEqual(ctx.exception.codigo, 404)

    def test_listar_abertos_exclui_aprovado_e_inativo_e_poe_preenchido_primeiro(self):
        self.conn.execute("INSERT INTO interno (cnpj_embarcador, nome_remetente, stkkc_id) VALUES ('22222222000122', 'BETA', 5)")
        self.conn.execute("INSERT INTO interno (cnpj_embarcador, nome_remetente, stkkc_id) VALUES ('33333333000133', 'GAMA', 6)")
        cad.registrar(self.conn, "22222222000122", 5, cad.PREENCHIDO, agora=AGORA)
        cad.registrar(self.conn, "33333333000133", 6, cad.INATIVO, agora=AGORA)
        nomes = [(l["nome"], l["situacao"]) for l in cad.listar_abertos(self.conn)]
        self.assertEqual(nomes, [("BETA", "PREENCHIDO"), ("FABENNE", "CONVIDADO")])


if __name__ == "__main__":
    unittest.main()
