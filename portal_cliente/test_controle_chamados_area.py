# -*- coding: utf-8 -*-
"""
Chamados (e-mails ao CLIENTE) e area nao atendida (e-mail ao cliente)
obedecem a tela de Notificacoes (controle_notificacoes.py). A tela vale por
cima do config; sem linha na tela vale o config de hoje.

So o e-mail ao cliente passa pela trava: o aviso a equipe, o e-mail ao
motorista e a copia interna do bloqueio de area seguem como antes, mesmo
com o geral desligado. Sem rede: SQLite temporario e envio trocado por duble.

    py -3.11 -m unittest portal_cliente.test_controle_chamados_area -v
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import chamados as ch  # noqa: E402
import controle_notificacoes as cn  # noqa: E402

HUGO = "hugo@freshlogbr.com"
CNPJ = "00000000000191"
CLIENTE = {"cnpj": CNPJ, "sender_id": 5, "nome": "QUATRO ESTRELAS"}
MOTORISTA = {"tipo": ch.TIPO_MOTORISTA, "cpf": "12345678901", "nome": "Joao", "rota_id": 9}


class _ComControle(unittest.TestCase):
    """Banco do controle temporario; cn.DB_PATH aponta pra ele (nunca pro dados.db)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db_controle = Path(self._tmp.name) / "controle.db"
        for obj, alvo, valor in ((cn, "DB_PATH", self.db_controle),
                                 (ch, "DB_PATH", Path(self._tmp.name) / "chamados.db"),
                                 (ch, "PASTA_ANEXOS", Path(self._tmp.name) / "anexos")):
            p = mock.patch.object(obj, alvo, valor)
            p.start()
            self.addCleanup(p.stop)

    def _controle(self, fn, *args):
        c = cn.conectar(self.db_controle)
        try:
            fn(c, *args, "teste")
        finally:
            c.close()


class TestChamados(_ComControle):
    def setUp(self):
        super().setUp()
        self.conn = ch.conectar()
        self.addCleanup(self.conn.close)
        p = mock.patch.object(ch, "enviar_email", return_value=True)
        self.envio = p.start()
        self.addCleanup(p.stop)
        self.config = {"email": {"remetente": "entregas@freshlogbr.com"},
                       "portal_cliente": {"chamados": {"email_atendimento": "equipe@x", "forcar_destino": ""}}}

    def _chamado(self, solicitante=CLIENTE):
        c = ch.criar_chamado(self.conn, solicitante, "chat", ch.STATUS_AGUARDANDO_FL, assunto="Entrega atrasada")
        return c, ch.mensagem_sistema(self.conn, c, "Recebemos")

    def _destinos(self):
        return [c.args[0] for c in self.envio.call_args_list]

    def test_chamado_cliente_bloqueado_nao_recebe_mas_equipe_recebe(self):
        self._controle(cn.definir_geral, False)
        c, m = self._chamado()
        self.assertTrue(ch.email_para_atendimento(self.conn, c, m, self.config, novo=True))
        self.assertFalse(ch.email_confirmacao_cliente(self.conn, c, m, ["cli@x"], self.config))
        self.assertFalse(ch.email_resposta_cliente(self.conn, c, m, ["cli@x"], self.config))
        self.assertFalse(ch.email_historico(self.conn, c, ["cli@x"], self.config))
        self.assertEqual(self._destinos(), [["equipe@x"]])

    def test_chamado_tipo_desligado_ou_excecao_bloqueado_nao_manda_ao_cliente(self):
        self._controle(cn.definir_estado, "chamados", "DESLIGADO")
        c, m = self._chamado()
        self.assertFalse(ch.email_resposta_cliente(self.conn, c, m, ["cli@x"], self.config))
        self._controle(cn.definir_estado, "chamados", "LIGADO")
        self._controle(cn.definir_excecao, "chamados", CNPJ, "BLOQUEADO")
        self.assertFalse(ch.email_resposta_cliente(self.conn, c, m, ["cli@x"], self.config))
        self.envio.assert_not_called()

    def test_chamado_historico_barrado_nao_marca_historico_enviado(self):
        self._controle(cn.definir_estado, "chamados", "DESLIGADO")
        c, _ = self._chamado()
        ch.email_historico(self.conn, c, ["cli@x"], self.config)
        self.assertFalse(ch.buscar_chamado(self.conn, c["id"]).get("historico_enviado_em"))

    def test_chamado_tela_ligado_manda_ao_cliente_mesmo_com_forcar_destino_no_config(self):
        self._controle(cn.definir_estado, "chamados", "LIGADO")
        self.config["portal_cliente"]["chamados"]["forcar_destino"] = HUGO
        c, m = self._chamado()
        self.assertTrue(ch.email_resposta_cliente(self.conn, c, m, ["cli@x"], self.config))
        self.assertEqual(self._destinos(), [["cli@x"]])

    def test_chamado_piloto_manda_so_pro_piloto_da_tela(self):
        self._controle(cn.definir_estado, "chamados", "PILOTO")
        self._controle(cn.definir_piloto, "piloto@freshlogbr.com", "")
        c, m = self._chamado()
        self.assertTrue(ch.email_resposta_cliente(self.conn, c, m, ["cli@x"], self.config))
        self.assertEqual(self._destinos(), [["piloto@freshlogbr.com"]])

    def test_chamado_piloto_com_excecao_liberado_manda_ao_cliente(self):
        self._controle(cn.definir_estado, "chamados", "PILOTO")
        self._controle(cn.definir_excecao, "chamados", CNPJ, "LIBERADO")
        c, m = self._chamado()
        ch.email_resposta_cliente(self.conn, c, m, ["cli@x"], self.config)
        self.assertEqual(self._destinos(), [["cli@x"]])

    def test_chamado_sem_linha_na_tela_vale_o_config(self):
        c, m = self._chamado()
        ch.email_resposta_cliente(self.conn, c, m, ["cli@x"], self.config)      # forcar_destino vazio: real
        self.config["portal_cliente"]["chamados"]["forcar_destino"] = HUGO
        ch.email_resposta_cliente(self.conn, c, m, ["cli@x"], self.config)      # config redireciona
        self.assertEqual(self._destinos(), [["cli@x"], [HUGO]])

    def test_chamado_de_motorista_nao_passa_pela_tela(self):
        self._controle(cn.definir_geral, False)
        c, m = self._chamado(MOTORISTA)
        self.assertTrue(ch.email_resposta_cliente(self.conn, c, m, ["mot@x"], self.config))
        self.assertTrue(ch.email_historico(self.conn, c, ["mot@x"], self.config))
        self.assertEqual(self._destinos(), [["mot@x"], ["mot@x"]])

    def test_mandar_sem_tipo_controle_nao_consulta_a_tela(self):
        self._controle(cn.definir_geral, False)
        c, m = self._chamado()
        with mock.patch.object(ch, "decidir") as decidir:
            self.assertTrue(ch._mandar(self.conn, c, None, ["x@x"], "assunto", "<p>oi</p>", self.config))
        decidir.assert_not_called()


class TestAreaNaoAtendida(_ComControle):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, status TEXT, bloqueio_chamado_id INTEGER, atualizado_em TEXT);"
                           "INSERT INTO portal_envios VALUES (1,'AGUARDANDO_LIBERACAO',NULL,NULL);")
        self.addCleanup(conn.close)
        return conn

    def _abrir(self, forcar="", **kwargs):
        import bloqueio_area as ba
        criados = [{"id": 1, "numero_nf": "10", "status": "AGUARDANDO_LIBERACAO",
                    "destinatario_municipio": "Curitiba", "destinatario_uf": "PR"}]
        config = {"email": {"email_atendimento": "entregas@x"}, "portal_cliente": {"envios": {"forcar_destino": forcar}}}
        fake_ch = mock.MagicMock()
        fake_ch.criar_chamado.return_value = {"id": 77}
        fake_ch.emails_do_cliente.return_value = ["cliente@x"]
        with mock.patch.object(ba, "_chamados", return_value=fake_ch), \
                mock.patch.object(ba, "enviar_email", return_value=True) as env:
            ba.abrir_bloqueio(self._conn(), {"cnpj": CNPJ, "nome": "ACME"}, criados, config, **kwargs)
        return env

    def test_area_cliente_piloto_e_interno_inalterado(self):
        self._controle(cn.definir_estado, "area_nao_atendida", "PILOTO")
        self._controle(cn.definir_piloto, "piloto@freshlogbr.com", "")
        env = self._abrir(forcar="")
        self.assertEqual([c.args[0] for c in env.call_args_list], [["piloto@freshlogbr.com"], ["entregas@x"]])
        self.assertIn("Redirecionado (piloto)", env.call_args_list[0].args[2])
        self.assertIn("cliente@x", env.call_args_list[0].args[2])

    def test_area_interno_continua_no_forcar_atual_com_a_tela_ligada(self):
        self._controle(cn.definir_estado, "area_nao_atendida", "LIGADO")
        env = self._abrir(forcar="hugo@x")
        # cliente: a tela liga o envio real; interno: segue o forcar_destino do config
        self.assertEqual([c.args[0] for c in env.call_args_list], [["cliente@x"], ["hugo@x"]])
        self.assertNotIn("Redirecionado (piloto)", env.call_args_list[0].args[2])

    def test_area_cliente_bloqueado_pela_tela_mas_interno_sai(self):
        self._controle(cn.definir_geral, False)
        env = self._abrir(forcar="")
        self.assertEqual([c.args[0] for c in env.call_args_list], [["entregas@x"]])

    def test_area_excecao_bloqueado_nao_manda_ao_cliente(self):
        self._controle(cn.definir_estado, "area_nao_atendida", "LIGADO")
        self._controle(cn.definir_excecao, "area_nao_atendida", CNPJ, "BLOQUEADO")
        env = self._abrir(forcar="")
        self.assertEqual([c.args[0] for c in env.call_args_list], [["entregas@x"]])

    def test_area_sem_linha_na_tela_vale_o_config(self):
        env = self._abrir(forcar="hugo@x")
        self.assertEqual([c.args[0] for c in env.call_args_list], [["hugo@x"], ["hugo@x"]])
        self.assertIn("Redirecionado (piloto)", env.call_args_list[0].args[2])
        env = self._abrir(forcar="")
        self.assertEqual([c.args[0] for c in env.call_args_list], [["cliente@x"], ["entregas@x"]])

    def test_area_db_path_injetavel(self):
        outro = Path(self._tmp.name) / "outro.db"
        c = cn.conectar(outro)
        cn.definir_estado(c, "area_nao_atendida", "DESLIGADO", "teste")
        c.close()
        env = self._abrir(forcar="", db_path=outro)
        self.assertEqual([x.args[0] for x in env.call_args_list], [["entregas@x"]])


if __name__ == "__main__":
    unittest.main()
