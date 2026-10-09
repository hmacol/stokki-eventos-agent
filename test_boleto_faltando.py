# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest test_boleto_faltando
"""
import sqlite3
import unittest
from unittest.mock import MagicMock

import boleto_faltando as bf


def _con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE nucleo_rotas (vuupt_route_id INT, data_rota TEXT, status TEXT, cancelada_em TEXT);
        CREATE TABLE nucleo_pedidos (codigo TEXT, vuupt_service_id INT, sender_id INT, destinatario_codigo TEXT,
                                     destinatario_nome TEXT, remetente_nome TEXT, tipo TEXT, vuupt_route_id INT,
                                     excluido_em TEXT, status TEXT);
        CREATE TABLE rascunhos_rota (id INTEGER PRIMARY KEY, data_alvo TEXT, status TEXT, lote_id TEXT, criado_em TEXT);
        CREATE TABLE rascunhos_parada (rascunho_id INT, service_id INT, codigo TEXT, sender_id INT);
        CREATE TABLE documentos_processados (hash_conteudo TEXT, tipo TEXT, codigo_pedido TEXT, status TEXT,
                                             numero_nf TEXT, cobranca INTEGER);
        INSERT INTO nucleo_rotas VALUES (1, '2026-10-07', 'PLANEJADA', NULL), (2, '2026-10-07', 'CANCELADA', 'x');
        INSERT INTO nucleo_pedidos VALUES
          ('PS-1', 101, 11, '12345678000190', 'MERCADO A', 'NUU', 'delivery', 1, NULL, NULL),
          ('PS-2', 102, 11, '12345678000190', 'MERCADO A', 'NUU', 'delivery', NULL, NULL, NULL),
          ('PS-3', 103, 22, '98765432000110', 'PADARIA B', 'SOTILLE', 'delivery', 2, NULL, NULL),
          ('PS-4-R1', 104, 11, '12345678000190', 'MERCADO A', 'NUU', 'delivery', 1, NULL, NULL),
          ('PS-5', 105, 11, '12345678000190', 'MERCADO A', 'NUU', 'delivery', NULL, NULL, NULL),
          ('PS-6', 106, 11, '12345678000190', 'MERCADO A', 'NUU', 'delivery', NULL, NULL, NULL),
          ('PS-7', 107, 11, '12345678000190', 'MERCADO A', 'NUU', 'delivery', NULL, NULL, NULL),
          ('PS-8', 108, 11, '12345678000190', 'MERCADO A', 'NUU', 'delivery', 9, NULL, NULL),
          ('PS-9', 109, 11, '12345678000190', 'MERCADO A', 'NUU', 'delivery', 1, NULL, 'CANCELADO'),
          ('PS-10', 110, 11, '12345678000190', 'MERCADO A', 'NUU', 'delivery', NULL, NULL, 'CANCELADO');
        INSERT INTO rascunhos_rota VALUES
          (7, '2026-10-07', 'RASCUNHO', 'L1', '2026-10-06 12:00:00'),
          (8, '2026-10-07', 'DESCARTADO', 'L1', '2026-10-06 12:00:00'),
          (9, '2026-10-07', 'OFERTADA', 'L1', '2026-10-06 12:00:00'),
          (10, '2026-10-07', 'RASCUNHO', 'L0', '2026-10-06 09:00:00'),
          (11, '2026-10-07', 'ENVIADO', 'L1', '2026-10-06 12:00:00');
        INSERT INTO rascunhos_parada VALUES (7, 102, 'PS-2', 11), (7, 101, 'PS-1', 11), (8, 103, 'PS-3', 22),
          (9, 105, 'PS-5', 11), (10, 106, 'PS-6', 11), (11, 107, 'PS-7', 11), (7, 108, 'PS-8', 11),
          (7, 110, 'PS-10', 11);
        INSERT INTO documentos_processados VALUES ('n1', 'Nota Fiscal', 'PS-1', 'ENVIADO', '40958', NULL),
                                                  ('b4', 'Boleto', 'PS-4', 'ENVIADO', NULL, NULL),
                                                  ('b2', 'Boleto', 'PS-2', 'REVISAO_MANUAL', NULL, NULL);
    """)
    return c


class Selecao(unittest.TestCase):
    def test_rascunho_ofertado_entra(self):
        cods = [p["codigo"] for p in bf.pedidos_da_data(_con(), "2026-10-07")]
        self.assertIn("PS-5", cods)

    def test_rota_e_rascunho_sem_duplicar_sem_cancelada_sem_descartado(self):
        cods = sorted(p["codigo"] for p in bf.pedidos_da_data(_con(), "2026-10-07"))
        self.assertEqual(cods, ["PS-1", "PS-2", "PS-4", "PS-5"])

    def test_so_lote_mais_recente_da_data(self):
        cods = [p["codigo"] for p in bf.pedidos_da_data(_con(), "2026-10-07")]
        self.assertNotIn("PS-6", cods)

    def test_rascunho_enviado_nao_entra_pelo_ramo_do_rascunho(self):
        cods = [p["codigo"] for p in bf.pedidos_da_data(_con(), "2026-10-07")]
        self.assertNotIn("PS-7", cods)

    def test_pedido_ja_roteado_em_parada_de_rascunho_nao_entra(self):
        cods = [p["codigo"] for p in bf.pedidos_da_data(_con(), "2026-10-07")]
        self.assertNotIn("PS-8", cods)

    def test_pedido_cancelado_nao_entra(self):
        cods = [p["codigo"] for p in bf.pedidos_da_data(_con(), "2026-10-07")]
        self.assertNotIn("PS-9", cods)
        self.assertNotIn("PS-10", cods)

    def _faltando_com_nfs(self, cobrancas):
        c = _con()
        for i, cob in enumerate(cobrancas):
            c.execute("INSERT INTO documentos_processados VALUES (?, 'Nota Fiscal', 'PS-2', 'ENVIADO', ?, ?)",
                      (f"nf{i}", f"9{i}", cob))
        peds = [p for p in bf.pedidos_da_data(c, "2026-10-07") if p["codigo"] == "PS-2"]
        return bf.faltando(c, peds, {"PS-2": "TEM_BOLETO"})

    def test_nf_a_vista_nao_gera_aviso(self):
        self.assertEqual(self._faltando_com_nfs([0]), [])
        self.assertEqual(self._faltando_com_nfs([0, 0]), [])

    def test_nf_com_cobranca_ou_indefinida_gera_aviso(self):
        self.assertEqual(len(self._faltando_com_nfs([1])), 1)
        self.assertEqual(len(self._faltando_com_nfs([-1])), 1)
        self.assertEqual(len(self._faltando_com_nfs([None])), 1)
        self.assertEqual(len(self._faltando_com_nfs([0, 1])), 1)

    def test_sem_nf_gera_aviso(self):
        self.assertEqual(len(self._faltando_com_nfs([])), 1)

    def test_faltando_so_tem_boleto_sem_boleto_enviado(self):
        c = _con()
        peds = bf.pedidos_da_data(c, "2026-10-07")
        sit = {"PS-1": "TEM_BOLETO", "PS-2": "PENDENTE", "PS-4": "TEM_BOLETO"}
        r = bf.faltando(c, peds, sit)
        self.assertEqual([(i["codigo"], i["nf"]) for i in r], [("PS-1", "40958")])

    def test_avisados_uma_vez(self):
        c = _con()
        itens = [{"codigo": "PS-1", "sender_id": 11}]
        self.assertEqual(bf.ja_avisados(c, "2026-10-07"), set())
        bf.registrar_avisados(c, itens, "2026-10-07", ["email"])
        bf.registrar_avisados(c, itens, "2026-10-07", ["email"])
        self.assertEqual(bf.ja_avisados(c, "2026-10-07"), {"PS-1"})
        self.assertEqual(bf.ja_avisados(c, "2026-10-08"), set())


class Email(unittest.TestCase):
    def test_email_tem_pedidos_e_aviso_de_piloto(self):
        itens = [{"codigo": "PS-1", "cliente": "MERCADO A", "nf": "40958"}]
        assunto, corpo = bf.email_aviso("NUU", itens, "07/10/2026", destino_original=["fin@nuu.com"])
        self.assertIn("07/10", assunto)
        self.assertIn("PS-1", corpo)
        self.assertIn("MERCADO A", corpo)
        self.assertIn("40958", corpo)
        self.assertIn("fin@nuu.com", corpo)
        self.assertIn("entregas@freshlogbr.com", corpo)


class Chamado(unittest.TestCase):
    def _ch(self, abertos):
        ch = MagicMock()
        ch.STATUS_AGUARDANDO_FL = "AGUARDANDO_FL"
        ch.ORIGEM_SISTEMA = "sistema"
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = abertos
        ch.conectar.return_value = conn
        ch.buscar_chamado.side_effect = lambda c, i: {"id": i}
        ch.criar_chamado.return_value = {"id": 77}
        return ch, conn

    def test_reaproveita_chamado_aberto(self):
        ch, conn = self._ch((5,))
        cid = bf.registrar_no_chamado({"cnpj": "1", "nome": "NUU"}, 11, "texto", "PS-1", {}, modulo_chamados=ch)
        self.assertEqual(cid, 5)
        ch.criar_chamado.assert_not_called()
        ch.mensagem_sistema.assert_called_once()

    def test_abre_quando_nao_ha(self):
        ch, conn = self._ch(None)
        cid = bf.registrar_no_chamado({"cnpj": "1", "nome": "NUU"}, 11, "texto", "PS-1", {}, modulo_chamados=ch)
        self.assertEqual(cid, 77)
        kw = ch.criar_chamado.call_args.kwargs
        self.assertEqual((kw["area"], kw["assunto"]), ("boleto", "Boleto faltando"))
        ch.whatsapp_para_atendimento.assert_called_once()
        sql = conn.execute.call_args_list[0].args[0]
        self.assertIn("area = 'boleto'", sql)
        self.assertIn("status != 'RESOLVIDO'", sql)


if __name__ == "__main__":
    unittest.main()
