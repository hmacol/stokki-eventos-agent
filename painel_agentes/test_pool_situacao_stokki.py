# -*- coding: utf-8 -*-
"""
Bloco "Em separação na Stokki" do pool do planejamento (Hugo, 19/10):
cada item do pool ganha `situacao_stokki` a partir do retrato que o
pipeline grava em vigia_stokki_abertos.

    py -3.11 -m unittest test_pool_situacao_stokki   (de dentro de painel_agentes/)
"""
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import planejamento_rotas as pr  # noqa: E402
from vigia import banco as vigia_banco  # noqa: E402


class TestAnotarSituacao(unittest.TestCase):

    def _pool(self, *codigos):
        return [{"codigo": c} for c in codigos]

    def test_rotulos_por_status(self):
        retrato = {"PS-40001": "Open", "PS-40002": "Separating", "PS-40003": "Ready to Pack",
                   "PS-40004": "On hold", "PS-40005": "Em espera", "PS-40006": "Waiting for Carrier"}
        pool = self._pool("#PS-40001", "#PS-40002", "#PS-40003", "#PS-40004", "#PS-40005", "#PS-40006", "#PS-40007")
        pr.anotar_situacao_stokki(pool, retrato)
        self.assertEqual([p["situacao_stokki"] for p in pool],
                         ["Em aberto", "Em separação", "Em conferência", "Em espera", "Em espera", None, None])

    def test_reentrega_e_codigo_combinado_casam_pelo_codigo_base(self):
        retrato = {"PS-41010": "Waiting for Carrier", "PS-41011": "Separating"}
        pool = self._pool("#PS-41010-R1", "#PS-41010, #PS-41011", "PS-41011-R2")
        pr.anotar_situacao_stokki(pool, retrato)
        self.assertEqual([p["situacao_stokki"] for p in pool], [None, "Em separação", "Em separação"])

    def test_retrato_vazio_nao_quebra(self):
        pool = self._pool("#PS-40001")
        pr.anotar_situacao_stokki(pool, {})
        self.assertIsNone(pool[0]["situacao_stokki"])


class TestCarregarRetrato(unittest.TestCase):

    def test_le_codigos_e_hora_da_ultima_listagem(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "dados.db"
            conn = vigia_banco.conectar(db)
            conn.execute("INSERT INTO vigia_stokki_abertos (codigo, status_stokki, primeira_vez_em, ultima_vez_em) "
                         "VALUES ('PS-40001', 'Open', '2026-10-19 08:05:00', '2026-10-19 22:18:28')")
            conn.execute("INSERT INTO vigia_listagens (concluida_em, completa, qtd) VALUES ('2026-10-19 16:27:49', 1, 1)")
            conn.execute("INSERT INTO vigia_listagens (concluida_em, completa, qtd) VALUES ('2026-10-19 22:18:28', 1, 1)")
            conn.commit()
            conn.close()
            retrato, quando = pr.carregar_retrato_stokki(db)
        self.assertEqual(retrato, {"PS-40001": "Open"})
        self.assertEqual(quando, "19/10 22:18")

    def test_banco_vazio_devolve_sem_hora(self):
        with tempfile.TemporaryDirectory() as d:
            retrato, quando = pr.carregar_retrato_stokki(Path(d) / "dados.db")
        self.assertEqual((retrato, quando), ({}, None))


if __name__ == "__main__":
    unittest.main()
