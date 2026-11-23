# -*- coding: utf-8 -*-
"""
test_verificar_entregues_nao_expedidos.py

Seções novas da checagem das 07:15, lidas do núcleo (espelho da Vuupt):
- rotas de dias anteriores que nunca terminaram (paradas pendentes ficam
  invisíveis pra expedição -- caso Rafael/Iago, 14/09 e de novo 16-24/09);
- retiradas no galpão abertas há mais de 7 dias, agrupadas por embarcador.
    py -3.11 -m unittest test_verificar_entregues_nao_expedidos -v
"""
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from nucleo import banco
import verificar_entregues_nao_expedidos as v

HOJE = date(2026, 9, 26)
LALAMOVE = 50258


class TestSecoesDoNucleo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _rota(self, id_, data, status="PLANEJADA", nome="Planejamento", agent=50191, motorista="Iago", situacoes=("PENDENTE",)):
        self.conn.execute(
            "INSERT INTO nucleo_rotas (id, data_rota, nome, agent_id, motorista_nome, status, status_provedor) "
            "VALUES (?, ?, ?, ?, ?, ?, 'assigned')", (id_, data, nome, agent, motorista, status))
        for i, sit in enumerate(situacoes, 1):
            self.conn.execute("INSERT INTO nucleo_paradas (rota_id, ordem, codigo, situacao) VALUES (?, ?, ?, ?)",
                              (id_, i, f"PS-{id_}{i}", sit))
        self.conn.commit()

    def test_rotas_paradas(self):
        self._rota(1, "2026-09-23", situacoes=("PENDENTE", "PENDENTE", "ENTREGUE"))
        self._rota(2, "2026-09-26")                                         # hoje: ainda no prazo
        self._rota(3, "2026-09-22", status="CONCLUIDA")
        self._rota(4, "2026-09-22", situacoes=("ENTREGUE", "INSUCESSO"))   # nada pendente
        self._rota(5, "2026-09-20", nome="[TESTE] Planejamento", agent=999001)
        self._rota(6, "2026-09-21", agent=LALAMOVE, motorista="LALAMOVE (virtual)")
        self._rota(7, "2026-06-01")                                         # fora dos 60 dias
        rotas = v.listar_rotas_paradas(self.conn, HOJE, LALAMOVE)
        self.assertEqual([r["id"] for r in rotas], [6, 1])                 # mais antiga primeiro
        self.assertEqual(rotas[1]["pendentes"], 2)
        self.assertIn("motorista", rotas[1]["motivo"])
        self.assertIn("Lalamove", rotas[0]["motivo"])

    def test_retiradas_velhas_agrupadas_por_embarcador(self):
        linhas = [
            ("PS-1", "RETIRADA", "EM_ROTA", "2026-09-01 10:00:00", None, "PADRAO PURO"),
            ("PS-2", "RETIRADA", "EM_ROTA", "2026-09-10 10:00:00", None, "PADRAO PURO"),
            ("PS-3", "RETIRADA", "EM_ROTA", "2026-09-24 10:00:00", None, "PADRAO PURO"),   # < 7 dias
            ("PS-4", "RETIRADA", "ENTREGUE", "2026-09-01 10:00:00", None, "DOURADO"),     # fechada
            ("PS-5", "RETIRADA", "EM_ROTA", "2026-09-01 10:00:00", "2026-09-02", "DOURADO"),  # excluída
            ("PS-6", "ENTREGA", "EM_ROTA", "2026-09-01 10:00:00", None, "DOURADO"),       # não é retirada
            ("PS-7", "RETIRADA", "EM_ROTA", "2026-09-05 10:00:00", None, "DOURADO"),
        ]
        self.conn.executemany(
            "INSERT INTO nucleo_pedidos (codigo, fluxo, status, criado_em_provedor, excluido_em, remetente_nome) "
            "VALUES (?, ?, ?, ?, ?, ?)", linhas)
        self.conn.commit()
        grupos = v.listar_retiradas_velhas(self.conn, datetime(2026, 9, 26, 10, 0, 0))
        self.assertEqual([(g["embarcador"], g["qtd"], g["mais_antiga"]) for g in grupos],
                         [("PADRAO PURO", 2, "01/09"), ("DOURADO", 1, "05/09")])
        self.assertEqual(grupos[0]["codigos"], ["PS-1", "PS-2"])


class TestEmail(unittest.TestCase):
    def test_email_so_com_rotas_e_retiradas(self):
        rotas = [{"id": 1, "data": "2026-09-23", "nome": "Planejamento - #4", "motorista": "Iago <x>",
                  "status_provedor": "assigned", "pendentes": 11, "total": 11, "motivo": "confirmar"}]
        retiradas = [{"embarcador": "PADRAO PURO", "qtd": 12, "mais_antiga": "29/08",
                      "codigos": [f"PS-{i}" for i in range(12)]}]
        corpo = v.montar_email([], 0, 0, rotas, retiradas)
        self.assertNotIn("Entregues sem expedição", corpo)
        self.assertIn("23/09", corpo)
        self.assertIn("Iago &lt;x&gt;", corpo)       # escapado
        self.assertIn("PS-9 ...", corpo)             # corta em 10 códigos
        self.assertNotIn("PS-10", corpo)


if __name__ == "__main__":
    unittest.main()
