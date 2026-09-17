# -*- coding: utf-8 -*-
"""py -3.11 -m unittest notificacao_entregas.test_fingerprint_notificacao_entrega"""
import tempfile
import unittest
from pathlib import Path

from notificacao_entregas import fingerprint_notificacao_entrega as fp

SERVICO = {"id": 111, "code": "#PS-36327", "sender_id": 900, "status_done": "success",
           "completed_at": "2026-09-17 12:22:19"}


class TestFingerprint(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = fp.conectar(Path(self._tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_sem_registro(self):
        self.assertEqual(fp.estados(self.conn, [111, 222]), {})
        self.assertEqual(fp.estados(self.conn, []), {})

    def test_registrar_e_ler(self):
        fp.registrar(self.conn, SERVICO, "AGUARDANDO_CANHOTO", motivo="aguardando canhoto")
        self.assertEqual(fp.estados(self.conn, [111]), {111: "AGUARDANDO_CANHOTO"})
        linha = self.conn.execute("SELECT * FROM notificacoes_entrega").fetchone()
        self.assertEqual((linha["codigo"], linha["sender_id"], linha["status_done"]), ("PS-36327", 900, "success"))
        self.assertIsNone(linha["enviado_em"])

    def test_transicao_preserva_primeira_vista_e_marca_envio(self):
        fp.registrar(self.conn, SERVICO, "AGUARDANDO_CANHOTO")
        antes = self.conn.execute("SELECT primeira_vista_em FROM notificacoes_entrega").fetchone()[0]
        self.conn.execute("UPDATE notificacoes_entrega SET primeira_vista_em='2026-01-01 00:00:00'")
        fp.registrar(self.conn, SERVICO, "ENVIADO", com_canhoto=True, destinatarios="a@x.com, b@x.com")
        linha = self.conn.execute("SELECT * FROM notificacoes_entrega").fetchone()
        self.assertTrue(antes)
        self.assertEqual(linha["primeira_vista_em"], "2026-01-01 00:00:00")
        self.assertEqual((linha["estado"], linha["com_canhoto"], linha["destinatarios"]),
                         ("ENVIADO", 1, "a@x.com, b@x.com"))
        self.assertIsNotNone(linha["enviado_em"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM notificacoes_entrega").fetchone()[0], 1)

    def test_tres_falhas_de_envio_viram_erro_definitivo(self):
        self.assertEqual(fp.registrar_falha_envio(self.conn, SERVICO), "FALHA_ENVIO")
        self.assertEqual(fp.registrar_falha_envio(self.conn, SERVICO), "FALHA_ENVIO")
        self.assertEqual(fp.registrar_falha_envio(self.conn, SERVICO), "ERRO_ENVIO")
        self.assertEqual(fp.estados(self.conn, [111]), {111: "ERRO_ENVIO"})

    def test_muitos_ids_passam_do_limite_de_parametros_do_sqlite(self):
        fp.registrar(self.conn, SERVICO, "ENVIADO")
        self.assertEqual(fp.estados(self.conn, list(range(1, 2500))), {111: "ENVIADO"})


if __name__ == "__main__":
    unittest.main()
