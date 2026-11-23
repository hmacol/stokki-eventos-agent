# -*- coding: utf-8 -*-
"""
Tabela pedidos_segurados (rota fraca adiada por 1 dia util, Hugo 29/09).
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_pedidos_segurados -v
"""
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pedidos_segurados as ps

TERCA, QUARTA, QUINTA = date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1)


def _servico(i, code=None):
    return {"id": i, "code": code or f"#PS-{1000 + i}"}


class TestPedidosSegurados(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"
        self.conn = ps.conectar(self.db)
        self.addCleanup(self.conn.close)

    def _marcar(self, servicos, data_alvo=TERCA, data_nova=QUARTA):
        return ps.marcar(self.conn, [(s, QUINTA) for s in servicos], data_alvo, data_nova,
                         "2 pedidos, 9 caixas", agora=datetime(2026, 9, 28, 18, 5))

    def test_marca_e_le(self):
        self.assertEqual(self._marcar([_servico(1), _servico(2)]), 2)
        self.assertEqual(ps.codigos_segurados(self.conn), {"PS-1001", "PS-1002"})
        linha = ps.segurados_ativos(self.conn, TERCA)["PS-1001"]
        self.assertEqual((linha["service_id"], linha["data_alvo_original"], linha["data_nova"],
                          linha["prazo_final"], linha["motivo"], linha["segurado_em"]),
                         (1, "2026-09-29", "2026-09-30", "2026-10-01", "2 pedidos, 9 caixas",
                          "2026-09-28 18:05:00"))

    def test_segunda_marcacao_do_mesmo_pedido_e_ignorada(self):
        self._marcar([_servico(1)])
        self.assertEqual(self._marcar([_servico(1)], data_alvo=QUARTA, data_nova=QUINTA), 0)
        self.assertEqual(ps.segurados_ativos(self.conn, TERCA)["PS-1001"]["data_nova"], "2026-09-30")

    def test_codigo_combinado_grava_os_dois(self):
        self.assertEqual(self._marcar([_servico(1, "#PS-1001, PS-2002")]), 2)
        self.assertEqual(ps.codigos_segurados(self.conn), {"PS-1001", "PS-2002"})

    def test_reentrega_cai_no_codigo_base(self):
        self._marcar([_servico(1, "#PS-1001-R1")])
        self.assertEqual(ps.codigos_segurados(self.conn), {"PS-1001"})

    def test_ativo_so_enquanto_a_data_nova_nao_chegou(self):
        self._marcar([_servico(1)])
        self.assertIn("PS-1001", ps.segurados_ativos(self.conn, TERCA))
        self.assertEqual(ps.segurados_ativos(self.conn, QUARTA), {})

    def test_separar_tira_so_os_ativos(self):
        # job rodado de novo na mesma noite: o segurado nao volta pra rota de terca
        self._marcar([_servico(1)])
        servicos = [_servico(1), _servico(2)]
        restantes, segurados = ps.separar_segurados(servicos, TERCA, self.db)
        self.assertEqual([s["id"] for s in restantes], [2])
        self.assertEqual([s["id"] for s in segurados], [1])
        restantes, segurados = ps.separar_segurados(servicos, QUARTA, self.db)
        self.assertEqual([s["id"] for s in restantes], [1, 2])
        self.assertEqual(segurados, [])

    def test_segurados_por_servico(self):
        self._marcar([_servico(1)])
        self.assertEqual(ps.segurados_por_servico([_servico(1), _servico(2)], TERCA, self.db),
                         {1: {"data_nova": "2026-09-30", "prazo_final": "2026-10-01"}})

    def test_banco_inacessivel_nao_derruba(self):
        ruim = Path(self.tmp.name) / "nao_existe" / "t.db"
        servicos = [_servico(1)]
        self.assertEqual(ps.separar_segurados(servicos, TERCA, ruim), (servicos, []))
        self.assertEqual(ps.segurados_por_servico(servicos, TERCA, ruim), {})


if __name__ == "__main__":
    unittest.main()
