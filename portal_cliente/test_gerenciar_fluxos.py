# -*- coding: utf-8 -*-
"""CLI `gerenciar_clientes.py fluxos` (carga da página e definição manual).

    py -3.11 -m unittest portal_cliente.test_gerenciar_fluxos -v
"""
import contextlib
import io
import json
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

import auth_cliente as auth  # noqa: E402
import gerenciar_clientes as gc  # noqa: E402


class CliFluxos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "d.db"
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, apelido TEXT, nome_remetente TEXT, stkkc_id INTEGER,
                                  sender_id INTEGER, email TEXT, atualizado_em TEXT);
            CREATE TABLE wms_produtos (sku TEXT, embarcador_id INTEGER, ativo INTEGER);
            INSERT INTO interno (cnpj_embarcador, apelido, stkkc_id) VALUES ('66259821000155', 'DOURADO', 18);
            INSERT INTO interno (cnpj_embarcador, apelido, stkkc_id) VALUES ('35539067000183', 'MUAI', 99);
            INSERT INTO wms_produtos VALUES ('XDDOURADO', 18, 1);
        """)
        conn.commit()
        conn.close()
        for p in (mock.patch.object(auth, "DB_PATH", self.db), mock.patch.object(gc, "_config", return_value={})):
            p.start()
            self.addCleanup(p.stop)

    def _rodar(self, *args) -> tuple[int, str]:
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            rc = gc.main(["fluxos", *args])
        return rc, saida.getvalue()

    def _fluxos(self, cnpj):
        c = sqlite3.connect(self.db)
        try:
            return c.execute("SELECT fluxos FROM interno WHERE cnpj_embarcador = ?", (cnpj,)).fetchone()[0]
        finally:
            c.close()

    def test_carga_grava_e_mostra_xd(self):
        arq = Path(self.tmp.name) / "fluxos.json"
        arq.write_text(json.dumps([
            {"id": "66259821000155", "data": {"cnpj": "66259821000155", "armazenagem": False, "cross": True, "outro": ""}},
            {"id": "35539067000183", "data": {"cnpj": "35539067000183", "armazenagem": False, "cross": False, "outro": "Full cross"}},
        ]), encoding="utf-8")
        rc, out = self._rodar("--carga", str(arq))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._fluxos("66259821000155"), "cross")
        self.assertEqual(self._fluxos("35539067000183"), "full_cross")
        self.assertIn("XD XDDOURADO", out)
        self.assertIn("2 embarcador(es) gravado(s).", out)

    def test_definir_sem_xd_avisa_nao_encontrado(self):
        rc, out = self._rodar("--cnpj", "35539067000183", "--definir", "cross,armazenagem")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._fluxos("35539067000183"), "armazenagem,cross")
        self.assertIn("XD NÃO ENCONTRADO", out)

    def test_definir_invalido_nao_grava(self):
        rc, out = self._rodar("--cnpj", "66259821000155", "--definir", "xpto")
        self.assertEqual(rc, 2)
        self.assertIn("Nada gravado", out)


if __name__ == "__main__":
    unittest.main()
