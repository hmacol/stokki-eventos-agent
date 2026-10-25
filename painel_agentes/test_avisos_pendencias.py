# -*- coding: utf-8 -*-
"""Testes do catalogo e das contagens do aviso de pendencias no topo."""
import sqlite3
import unittest
from unittest import mock

import avisos_pendencias as ap

TODAS_FONTES_NOVAS = ("solicitacoes_portal", "retornos", "cadastros_embarcador", "canhotos_pendentes",
                      "clientes_agenda", "rascunhos_erro", "batimento", "vigia_vencidos",
                      "rotinas_falhas", "wms_pendencias", "wms_divergencias")


def _montar(nivel="total", contadores=None, novos=None, telas=None, aprova=False):
    """Roda montar() com as contagens novas mockadas (dict chave->n)."""
    novos = novos or {}

    def _contar(chave):
        v = novos.get(chave, 0)
        if isinstance(v, Exception):
            raise v
        return v

    with mock.patch.object(ap, "contar", side_effect=_contar):
        return ap.montar(nivel, contadores or {}, telas, aprova)


def _c(**kw):
    return {k: {"qtd": v, "criticas": 0} for k, v in kw.items()}


class TestMontar(unittest.TestCase):
    def test_ordem_fixa_vermelhas_primeiro(self):
        r = _montar("total", _c(pedagios=2, atendimento=3), {"retornos": 4})
        self.assertEqual([a["chave"] for a in r], ["chamados", "retornos", "pedagios"])

    def test_texto_singular_e_plural(self):
        um = _montar("total", _c(atendimento=1))
        doze = _montar("total", _c(atendimento=12))
        self.assertEqual(um[0]["texto"], "1 chamado esperando a Fresh Log")
        self.assertEqual(doze[0]["texto"], "12 chamados esperando a Fresh Log")
        self.assertEqual(doze[0]["qtd"], 12)
        self.assertTrue(doze[0]["vermelho"])
        self.assertEqual(doze[0]["destino"], "/atendimento")

    def test_leitura_nao_e_cobrado(self):
        cont = _c(atendimento=1, pedagios=1, canhotos=1, transportadoras=1, motoristas=1,
                  pedidos_parados=1, torre=1)
        novos = {c: 1 for c in TODAS_FONTES_NOVAS}
        self.assertEqual(_montar("leitura", cont, novos, aprova=True), [])

    def test_login_personalizado_sem_tela(self):
        cont = _c(torre=2, pedagios=3, atendimento=1)
        r = _montar("operador", cont, telas={"torre"})
        self.assertEqual([a["chave"] for a in r], ["torre"])

    def test_embarcador_so_com_permissao(self):
        novos = {"cadastros_embarcador": 2}
        self.assertEqual(_montar("operador", None, novos, aprova=False), [])
        r = _montar("operador", None, novos, aprova=True)
        self.assertEqual([a["chave"] for a in r], ["cadastros_embarcador"])
        r = _montar("total", None, novos, aprova=False)
        self.assertEqual([a["chave"] for a in r], ["cadastros_embarcador"])

    def test_fonte_que_falha_nao_derruba_as_outras(self):
        r = _montar("total", _c(pedagios=1), {"retornos": RuntimeError("x"), "rascunhos_erro": 2})
        self.assertEqual([a["chave"] for a in r], ["pedagios", "rascunhos_erro"])

    def test_fonte_none_some(self):
        r = _montar("total", _c(pedagios=1), {"retornos": None})
        self.assertEqual([a["chave"] for a in r], ["pedagios"])

    def test_zero_nao_vira_aviso(self):
        self.assertEqual(_montar("total", _c(pedagios=0, atendimento=0), {"retornos": 0}), [])

    def test_nivel_sem_acesso_a_tela_nao_e_cobrado(self):
        # galpao so ve retornos/wms; pedagios nao
        r = _montar("galpao", _c(pedagios=5), {"retornos": 1})
        self.assertEqual([a["chave"] for a in r], ["retornos"])

    def test_catalogo_consistente(self):
        chaves = [i["chave"] for i in ap.CATALOGO]
        self.assertEqual(len(chaves), 18)
        self.assertEqual(len(set(chaves)), 18)
        for i in ap.CATALOGO:
            self.assertNotIn("leitura", i["niveis"])
            self.assertIn("total", i["niveis"])
            self.assertTrue(i["endpoint_destino"])
            self.assertTrue(i["destino"].startswith("/"))
        self.assertEqual(ap.IGNORAR_MINUTOS, 30)


class TestContagens(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        # nao fecha: _conectar devolve sempre a mesma conexao de teste
        p = mock.patch.object(ap, "_conectar", return_value=mock.Mock(wraps=self.conn, close=lambda: None))
        p.start()
        self.addCleanup(p.stop)

    def test_conta_clientes_agenda_pendentes(self):
        self.conn.execute("CREATE TABLE clientes_agenda_marcados (documento TEXT, situacao TEXT)")
        self.conn.executemany("INSERT INTO clientes_agenda_marcados VALUES (?,?)",
                              [("1", "PENDENTE"), ("2", "PENDENTE"), ("3", "MARCADO")])
        self.assertEqual(ap.contar("clientes_agenda"), 2)

    def test_conta_solicitacoes_portal_pendentes(self):
        self.conn.execute("CREATE TABLE portal_solicitacoes (id INTEGER, status TEXT)")
        self.conn.executemany("INSERT INTO portal_solicitacoes VALUES (?,?)",
                              [(1, "PENDENTE"), (2, "CONCLUIDA"), (3, "PENDENTE"), (4, "PENDENTE")])
        self.assertEqual(ap.contar("solicitacoes_portal"), 3)

    def test_conta_rascunhos_com_erro(self):
        self.conn.execute("CREATE TABLE rascunhos_rota (id INTEGER, status TEXT)")
        self.conn.executemany("INSERT INTO rascunhos_rota VALUES (?,?)",
                              [(1, "ERRO_ENVIO"), (2, "ENVIADO"), (3, "ERRO_ENVIO")])
        self.assertEqual(ap.contar("rascunhos_erro"), 2)

    def test_conta_cadastros_embarcador_preenchidos(self):
        self.conn.execute("CREATE TABLE cadastro_embarcador (cnpj_embarcador TEXT, situacao TEXT)")
        self.conn.executemany("INSERT INTO cadastro_embarcador VALUES (?,?)",
                              [("1", "PREENCHIDO"), ("2", "APROVADO"), ("3", "PREENCHIDO")])
        self.assertEqual(ap.contar("cadastros_embarcador"), 2)

    def test_conta_rotinas_com_falha_em_24h(self):
        from datetime import datetime, timedelta
        agora = datetime.now()
        fmt = "%Y-%m-%d %H:%M:%S"
        execs = [
            {"status": "ERRO", "finalizado_em": (agora - timedelta(hours=2)).strftime(fmt), "iniciado_em": None},
            {"status": "SUCESSO", "finalizado_em": agora.strftime(fmt), "iniciado_em": None},
            {"status": "RODANDO", "finalizado_em": None, "iniciado_em": agora.strftime(fmt)},
            {"status": "ERRO", "finalizado_em": (agora - timedelta(hours=30)).strftime(fmt), "iniciado_em": None},
            {"status": "TIMEOUT", "finalizado_em": None, "iniciado_em": (agora - timedelta(hours=1)).strftime(fmt)},
        ]
        self.assertEqual(ap._contar_rotinas_falhas(execs, agora), 2)

    def test_chave_desconhecida_levanta(self):
        with self.assertRaises(KeyError):
            ap.contar("pedagios")   # vem do contadores_menu, nao daqui


if __name__ == "__main__":
    unittest.main()
