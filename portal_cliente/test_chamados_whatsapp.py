# -*- coding: utf-8 -*-
"""
test_chamados_whatsapp.py

Aviso no grupo de WhatsApp do atendimento quando um chamado passa a
depender de gente (pedido do Hugo, 29/09/2026). Sem rede: SQLite
temporario e em_segundo_plano trocado por um registro do que sairia.
    py -3.11 -m unittest portal_cliente.test_chamados_whatsapp -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "portal_cliente"))

import chamados as ch  # noqa: E402

CLIENTE = {"cnpj": "00000000000191", "sender_id": 5, "nome": "QUATRO ESTRELAS"}
WHATSAPP = {"ativo": True, "base_url": "http://x/api", "api_key": "k", "sessao": "s",
            "grupo_id": "1@g.us", "grupo_atendimento_id": "2@g.us"}


def _config(**extra):
    return {"whatsapp_notificacoes": {**WHATSAPP, **extra},
            "portal_cliente": {"chamados": {"email_atendimento": "entregas@x"}}}


class _ComBanco(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        for alvo, valor in (("DB_PATH", Path(self._tmp.name) / "t.db"),
                            ("PASTA_ANEXOS", Path(self._tmp.name) / "chamados")):
            p = mock.patch.object(ch, alvo, valor)
            p.start()
            self.addCleanup(p.stop)
        self.saidas: list[tuple] = []
        p = mock.patch.object(ch, "em_segundo_plano", lambda fn, *a, **k: self.saidas.append((fn.__name__, a, k)))
        p.start()
        self.addCleanup(p.stop)
        self.conn = ch.conectar()
        self.addCleanup(self.conn.close)

    def chamado(self, status=ch.STATUS_COM_ASSISTENTE, **kw):
        return ch.criar_chamado(self.conn, CLIENTE, origem="chat", status=status, **kw)

    def avisados(self):
        return [a[0]["id"] for nome, a, _ in self.saidas if nome == "_whatsapp_chamado"]


class TestQuandoAvisa(_ComBanco):
    def test_pediu_atendente_fora_do_horario(self):
        c = self.chamado()
        r = ch.entrar_na_fila(self.conn, c, _config())
        self.assertFalse(r["online"])
        self.assertEqual(self.avisados(), [c["id"]])

    def test_pediu_atendente_com_equipe_online(self):
        c = self.chamado()
        with mock.patch.object(ch, "situacao_atendimento",
                               return_value={"estado": "online", "nomes_online": ["Hugo"]}):
            r = ch.entrar_na_fila(self.conn, c, _config())
        self.assertTrue(r["online"])
        self.assertEqual(self.avisados(), [c["id"]])

    def test_mensagem_do_cliente_reabre_chamado_resolvido(self):
        c = ch.resolver(self.conn, self.chamado(ch.STATUS_EM_ATENDIMENTO), "Hugo")
        ch.registrar_mensagem_cliente(self.conn, c, CLIENTE, "voltou o problema", [], _config())
        self.assertEqual(self.avisados(), [c["id"]])


class TestQuandoNaoAvisa(_ComBanco):
    def test_conversa_com_o_assistente(self):
        self.chamado()
        self.assertEqual(self.saidas, [])

    def test_mensagem_em_chamado_que_ja_esta_com_a_equipe(self):
        for status in (ch.STATUS_EM_ATENDIMENTO, ch.STATUS_RESPONDIDO, ch.STATUS_AGUARDANDO_FL, ch.STATUS_NA_FILA):
            ch.registrar_mensagem_cliente(self.conn, self.chamado(status), CLIENTE, "oi", [], _config())
        self.assertEqual(self.avisados(), [])

    def test_atendente_devolve_pra_fila_ou_reabre(self):
        c = self.chamado(ch.STATUS_EM_ATENDIMENTO)
        ch.transferir(self.conn, c, "Hugo")
        ch.reabrir(self.conn, ch.resolver(self.conn, c, "Hugo"), _config(), "por Hugo")
        self.assertEqual(self.avisados(), [])

    def test_desligado_ou_sem_grupo_nem_abre_thread(self):
        for config in (_config(ativo=False), _config(avisar_chamados=False), _config(grupo_atendimento_id=""),
                       {"portal_cliente": {}}, {}):
            ch.entrar_na_fila(self.conn, self.chamado(), config)
        self.assertEqual(self.saidas, [])


class TestEnvio(_ComBanco):
    def test_manda_o_link_da_tela_de_atendimento(self):
        c = self.chamado(ch.STATUS_AGUARDANDO_FL, area="entrega", pedido_ref="PS-40316")
        with mock.patch("notificar_whatsapp.avisar_chamado", return_value="enviado") as avisar:
            ch._whatsapp_chamado(self.conn, c, _config())
        chamado, link, _ = avisar.call_args.args
        self.assertEqual(chamado["id"], c["id"])
        self.assertEqual(link, f"https://app.freshhub.com.br/painel/atendimento?chamado={c['id']}")
        self.assertIs(avisar.call_args.kwargs["conn"], self.conn)

    def test_ponta_a_ponta_ate_o_gateway(self):
        import notificar_whatsapp as nw
        c = self.chamado(ch.STATUS_AGUARDANDO_FL, pedido_ref="PS-40316")
        with mock.patch.object(nw.integracao_openwa, "enviar_texto", return_value=(True, "m1")) as enviar:
            ch._whatsapp_chamado(self.conn, c, _config())
        _, grupo, texto = enviar.call_args.args
        self.assertEqual(grupo, "2@g.us")
        self.assertIn(f"Chamado #{c['id']} · QUATRO ESTRELAS", texto)
        self.assertIn(f"/painel/atendimento?chamado={c['id']}", texto)
        self.assertEqual(self.conn.execute("SELECT situacao FROM notificacoes_whatsapp").fetchone()[0], "enviado")


if __name__ == "__main__":
    unittest.main()
