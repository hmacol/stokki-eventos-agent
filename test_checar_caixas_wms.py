# -*- coding: utf-8 -*-
"""
Testes da checagem diaria caixas x saldo e do CLI de wms_caixas.
Rodar (da raiz): py -3.11 -m unittest test_checar_caixas_wms -v
"""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "painel_agentes"))

import checar_caixas_wms as chk  # noqa: E402
import wms  # noqa: E402
import wms_caixas as cx  # noqa: E402

CONFIG = {"wms": {"embarcador_piloto_id": "48", "alertas_email": ["ops@x.com"]}, "email": {"remetente": "r@x.com"}}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        self.conn = cx.conectar(self.db)
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, qtd_por_caixa, "
            "unidade, controla_validade, atualizado_em) VALUES (1, 900, '72400017<X>', 'COXINHA', 'MARIA DOLORES', "
            "48, 6, 'UN', 1, '2026-09-21 10:00:00')")
        wms.criar_area(self.conn, "C9", "CONTAINER", "Container 9")
        wms.gerar_posicoes(self.conn, "C9", estantes=2, niveis=2)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def saldo(self, q=10):
        self.conn.execute("INSERT INTO wms_saldos (posicao, produto_id, lote, validade, quantidade, atualizado_em) "
                          "VALUES ('C9-E1-N1', 1, 'L1', '2026-12-15', ?, 'x')", (q,))
        self.conn.commit()


class TestChecar(Base):
    def test_sem_divergencia_nao_manda_email(self):
        with mock.patch.object(chk.email_utils, "enviar_email") as env:
            r = chk.checar(self.conn, CONFIG)
        self.assertEqual(r, {"divergencias": 0, "email": "nada"})
        env.assert_not_called()

    def test_com_divergencia_manda_um_email_com_as_linhas(self):
        self.saldo()
        with mock.patch.object(chk.email_utils, "enviar_email", return_value=True) as env:
            r = chk.checar(self.conn, CONFIG)
        self.assertEqual(r, {"divergencias": 1, "email": "enviado"})
        env.assert_called_once()
        dest, assunto, corpo, cfg = env.call_args.args
        self.assertEqual(dest, ["ops@x.com"])
        self.assertIn("1 divergencia", assunto)
        self.assertIn("C9-E1-N1", corpo)
        self.assertIn("72400017&lt;X&gt;", corpo)   # escapado

    def test_modo_teste_nao_manda(self):
        self.saldo()
        with mock.patch.object(chk.email_utils, "enviar_email") as env:
            r = chk.checar(self.conn, CONFIG, modo_teste=True)
        self.assertEqual(r, {"divergencias": 1, "email": "teste"})
        env.assert_not_called()

    def test_falha_no_envio_vira_falhou(self):
        self.saldo()
        with mock.patch.object(chk.email_utils, "enviar_email", return_value=False):
            self.assertEqual(chk.checar(self.conn, CONFIG)["email"], "falhou")

    def test_destinatario_cai_no_remetente_ou_nada(self):
        self.saldo()
        with mock.patch.object(chk.email_utils, "enviar_email", return_value=True) as env:
            chk.checar(self.conn, {"email": {"remetente": "r@x.com"}})
        self.assertEqual(env.call_args.args[0], ["r@x.com"])
        with mock.patch.object(chk.email_utils, "enviar_email") as env:
            self.assertEqual(chk.checar(self.conn, {})["email"], "nada")
        env.assert_not_called()


class TestCLI(Base):
    def run_cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cx.main(["--db", str(self.db), *args])
        return rc, buf.getvalue()

    def saldo_total(self):
        return self.conn.execute("SELECT COALESCE(SUM(quantidade),0) FROM wms_saldos").fetchone()[0]

    def test_divergencias_imprime_linhas(self):
        self.saldo()
        rc, out = self.run_cli("divergencias", "--embarcador-id", "48")
        self.assertEqual(rc, 0)
        self.assertIn("C9-E1-N1", out)
        self.assertIn("1 divergencia", out)

    def test_zerar_saldo_sem_aplicar_nao_escreve_e_com_aplicar_zera(self):
        self.saldo(10)
        rc, out = self.run_cli("zerar-saldo", "--embarcador-id", "48", "--motivo", "limpeza")
        self.assertEqual(rc, 0)
        self.assertIn("Nada foi alterado", out)
        self.assertEqual(self.saldo_total(), 10)
        rc, out = self.run_cli("zerar-saldo", "--embarcador-id", "48", "--motivo", "limpeza", "--aplicar")
        self.assertEqual(rc, 0)
        self.assertIn("1 linha(s) ajustada(s)", out)
        self.assertEqual(self.saldo_total(), 0)

    def test_zerar_saldo_com_caixa_ativa_da_erro_e_codigo_1(self):
        self.saldo(6)
        self.conn.execute(
            "INSERT INTO wms_caixas (serial, produto_id, lote, validade, qtd_un, posicao, situacao, criado_em, "
            "atualizado_em) VALUES ('C0000001', 1, 'L1', '2026-12-15', 6, 'C9-E1-N1', 'EM_ESTOQUE', 'x', 'x')")
        self.conn.commit()
        rc, out = self.run_cli("zerar-saldo", "--embarcador-id", "48", "--motivo", "x", "--aplicar")
        self.assertEqual(rc, 1)
        self.assertIn("Erro", out)
        self.assertEqual(self.saldo_total(), 6)


if __name__ == "__main__":
    unittest.main()
