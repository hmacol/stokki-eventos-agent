# -*- coding: utf-8 -*-
"""
Rota de temperatura do produto (recebimento por etapas).

    py -3.11 -m unittest test_wms_doca_tela   (de dentro de painel_agentes/)
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
for _p in (_AQUI.parent, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import acesso_telas  # noqa: E402
import wms  # noqa: E402
from test_fila_etiquetas_tela import painel_agentes  # noqa: E402

ORIGEM = {"Origin": "http://localhost"}
CFG = {"painel_agentes": {"usuario": "u", "senha": "s"}, "wms": {}}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        for alvo, nome, valor in ((wms, "DB_PATH", Path(self._tmp.name) / "dados.db"),
                                  (acesso_telas, "DB_PATH", Path(self._tmp.name) / "a.db")):
            p = mock.patch.object(alvo, nome, valor)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value=CFG)
        p.start()
        self.addCleanup(p.stop)
        conn = wms.conectar()
        conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (1, 900, '72400017', "
            "'PRODUTO', 'EMB', 48, '724000170000', 6, 'UN', 0, '2026-09-21 10:00:00')")
        conn.commit()
        conn.close()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def entrar(self, nivel):
        with self.cliente.session_transaction() as s:
            s["nivel_acesso"] = nivel
            s["usuario"] = "teste"

    def post(self, corpo):
        return self.cliente.post("/api/wms/produtos/1/temperatura", json=corpo, headers=ORIGEM)


class Temperatura(Base):
    def test_galpao_recebe_403(self):
        self.entrar("galpao")
        self.assertEqual(self.post({"temperatura": "CONGELADO"}).status_code, 403)

    def test_total_grava(self):
        self.entrar("total")
        r = self.post({"temperatura": "CONGELADO"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["produto"]["temperatura"], "CONGELADO")

    def test_valor_invalido_400(self):
        self.entrar("total")
        self.assertEqual(self.post({"temperatura": "MORNO"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
