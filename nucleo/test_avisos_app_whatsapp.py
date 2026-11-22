# -*- coding: utf-8 -*-
"""
test_avisos_app_whatsapp.py

Avisos por WhatsApp pro motorista que usa o app pelo navegador (PWA no iPhone):
rota nova, alterada, cancelada e resposta da logistica no chat de Ajuda.
O servidor nao manda push; o WhatsApp pessoal e o "push" do iPhone.
    py -3.11 -m unittest nucleo.test_avisos_app_whatsapp -v
"""
import sqlite3
import sys
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import auth_motorista as auth  # noqa: E402


class PlataformaTest(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE motoristas (cpf TEXT PRIMARY KEY, plataforma TEXT)")
        conn.execute("INSERT INTO motoristas VALUES ('123', NULL)")
        return conn

    def test_registra_web_e_android(self):
        conn = self._conn()
        auth.registrar_plataforma(conn, "123", "web")
        self.assertEqual(conn.execute("SELECT plataforma FROM motoristas").fetchone()[0], "web")
        auth.registrar_plataforma(conn, "123", "android")      # trocou de aparelho: vale o ultimo
        self.assertEqual(conn.execute("SELECT plataforma FROM motoristas").fetchone()[0], "android")

    def test_valor_invalido_ou_ausente_e_ignorado(self):
        conn = self._conn()
        auth.registrar_plataforma(conn, "123", "web")
        for lixo in (None, "", "WEB; drop table motoristas", "windows", "x" * 50):
            auth.registrar_plataforma(conn, "123", lixo)
        self.assertEqual(conn.execute("SELECT plataforma FROM motoristas").fetchone()[0], "web")


if __name__ == "__main__":
    unittest.main()


# ── canal: origem propria, trava e teto separados ────────────────────────────────────
import notificar_whatsapp as nw  # noqa: E402
from datetime import datetime  # noqa: E402
from unittest import mock  # noqa: E402


class CanalTest(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(nw._SCHEMA)
        return conn

    def _enviado(self, conn, origem, n=1):
        for i in range(n):
            nw._registrar(conn, datetime.now(), origem, "t", f"a{i}", "enviado")

    def test_teto_conta_so_a_propria_origem(self):
        conn = self._conn()
        agora = datetime.now()
        self._enviado(conn, nw.ORIGEM_MOTORISTA_ATUALIZACAO, 5)
        self._enviado(conn, "alerta_interno", 3)
        self._enviado(conn, nw.ORIGEM_AVISO_APP, 2)
        self.assertEqual(nw._enviados_hoje(conn, agora, nw.ORIGEM_AVISO_APP), 2)
        self.assertEqual(nw._enviados_hoje(conn, agora, nw.ORIGEM_AVISO_APP_PILOTO), 0)
        self.assertEqual(nw._enviados_hoje(conn, agora, nw.ORIGEM_MOTORISTA_ATUALIZACAO), 5)

    def test_aviso_do_app_nao_consome_o_teto_dos_alertas_internos(self):
        conn = self._conn()
        self._enviado(conn, "alerta_interno", 3)
        self._enviado(conn, nw.ORIGEM_AVISO_APP, 4)
        self._enviado(conn, nw.ORIGEM_AVISO_APP_PILOTO, 4)
        self.assertEqual(nw._enviados_hoje(conn, datetime.now(), "alerta_interno"), 3)

    def test_teto_padrao_e_configuravel(self):
        self.assertEqual(nw._teto({}, nw.ORIGEM_AVISO_APP, {}), nw.TETO_AVISO_APP_PADRAO)
        cfg = {"avisos_app_motoristas": {"teto_diario": 7}}
        self.assertEqual(nw._teto({}, nw.ORIGEM_AVISO_APP, cfg), 7)
        self.assertEqual(nw._teto({}, nw.ORIGEM_AVISO_APP_PILOTO, cfg), 7)

    def test_trava_ausente_nula_ou_lixo_e_desligado(self):
        for cfg in ({}, {"avisos_app_motoristas": {}}, {"avisos_app_motoristas": {"forcar_destino_whatsapp": None}},
                    {"avisos_app_motoristas": {"forcar_destino_whatsapp": "abc"}}):
            self.assertIsNone(nw.destino_aviso_app(cfg))
        self.assertEqual(nw.destino_aviso_app({"avisos_app_motoristas": {"forcar_destino_whatsapp": "(11) 98888-7777"}}), "11988887777")
        self.assertEqual(nw.destino_aviso_app({"avisos_app_motoristas": {"forcar_destino_whatsapp": ""}}), "")

    def _cfg(self, destino):
        return {"whatsapp_notificacoes": {"ativo": True}, "avisos_app_motoristas": {"forcar_destino_whatsapp": destino}}

    def test_envio_desligado_sem_trava(self):
        with mock.patch.object(nw, "gateway_clientes_ok", return_value=True), mock.patch.object(nw, "despachar") as d:
            r = nw.avisar_motorista_app_privado("Ana", "11999990000", "texto", {}, "nova_rota", "ass")
            self.assertEqual(r, ("desligado", False))
            d.assert_not_called()

    def test_piloto_vai_so_pro_numero_forcado_com_prefixo_de_teste(self):
        with mock.patch.object(nw, "gateway_clientes_ok", return_value=True), \
                mock.patch.object(nw, "despachar", return_value="enviado") as d:
            r = nw.avisar_motorista_app_privado("Ana", "11999990000", "Oi", self._cfg("11988887777"), "nova_rota", "ass")
        self.assertEqual(r, ("enviado", True))
        args, kw = d.call_args
        self.assertEqual(args[1], nw.ORIGEM_AVISO_APP_PILOTO)
        self.assertEqual(kw["grupo_id"], "11988887777@c.us")   # o numero da trava vale como esta no config (com o 55, se for o caso)
        self.assertTrue(args[3].startswith("[teste → Ana +5511999990000]\n"))

    def test_piloto_funciona_sem_telefone_do_motorista_usuario_de_teste(self):
        with mock.patch.object(nw, "gateway_clientes_ok", return_value=True), \
                mock.patch.object(nw, "despachar", return_value="enviado") as d:
            r = nw.avisar_motorista_app_privado("Hugo iPhone", None, "Oi", self._cfg("11988887777"), "nova_rota", "ass")
        self.assertEqual(r, ("enviado", True))
        self.assertIn("sem telefone", d.call_args.args[3])

    def test_real_confere_whatsapp_e_vai_pro_numero_do_motorista(self):
        with mock.patch.object(nw, "gateway_clientes_ok", return_value=True), \
                mock.patch.object(nw.integracao_openwa, "numero_existe", return_value=True), \
                mock.patch.object(nw, "despachar", return_value="enviado") as d:
            r = nw.avisar_motorista_app_privado("Ana", "(11) 99999-0000", "Oi", self._cfg(""), "nova_rota", "ass")
        self.assertEqual(r, ("enviado", False))
        args, kw = d.call_args
        self.assertEqual(args[1], nw.ORIGEM_AVISO_APP)
        self.assertEqual(kw["grupo_id"], "5511999990000@c.us")
        self.assertEqual(args[3], "Oi")

    def test_real_sem_whatsapp_e_real_sem_telefone(self):
        with mock.patch.object(nw, "gateway_clientes_ok", return_value=True), \
                mock.patch.object(nw.integracao_openwa, "numero_existe", return_value=False), \
                mock.patch.object(nw, "despachar") as d:
            self.assertEqual(nw.avisar_motorista_app_privado("Ana", "11999990000", "Oi", self._cfg(""), "t", "a")[0], "numero_sem_whatsapp")
            self.assertEqual(nw.avisar_motorista_app_privado("Ana", None, "Oi", self._cfg(""), "t", "a")[0], "desligado")
            d.assert_not_called()

    def test_teto_vira_situacao_propria_pro_lote_parar(self):
        with mock.patch.object(nw, "gateway_clientes_ok", return_value=True), \
                mock.patch.object(nw, "despachar", return_value="nao_enviado"), \
                mock.patch.object(nw, "_motivo_da_ultima_recusa", return_value=nw.MOTIVO_TETO):
            r = nw.avisar_motorista_app_privado("Ana", "11999990000", "Oi", self._cfg("11988887777"), "t", "a")
        self.assertEqual(r, ("teto", True))
