# -*- coding: utf-8 -*-
"""
Tabela pedidos_retornando (pedido tirado de rota iniciada fica retido ate o galpao receber).
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_pedidos_retornando -v
"""
import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pedidos_retornando as pr


class TestPedidosRetornando(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"
        self.conn = pr.conectar(self.db)
        self.addCleanup(self.conn.close)
        # warnings esperados nao poluem o stderr; testes de log usam assertLogs
        nivel = pr.logger.level
        pr.logger.setLevel(logging.ERROR)
        self.addCleanup(pr.logger.setLevel, nivel)

    def _marcar(self, service_id=5, codigo="#PS-1", route_id=99):
        return pr.marcar(self.conn, service_id=service_id, codigo=codigo, vuupt_route_id=route_id,
                         agent_id=7, motorista_nome="Joao", motivo="cliente fechado",
                         observacao="obs", usuario="hugo")

    def _nucleo(self):
        self.conn.executescript("""
            CREATE TABLE nucleo_rotas (id INTEGER PRIMARY KEY, vuupt_route_id INTEGER);
            CREATE TABLE nucleo_paradas (id INTEGER PRIMARY KEY, rota_id INTEGER, service_id INTEGER);
        """)

    def test_marcar_e_listar_ativo(self):
        novo_id = self._marcar()
        self.assertIsInstance(novo_id, int)
        linha = pr.ativos(self.conn)[5]
        self.assertEqual((linha["codigo"], linha["vuupt_route_id"], linha["agent_id"],
                          linha["motorista_nome"], linha["motivo"], linha["observacao"],
                          linha["usuario"], linha["recebido_em"]),
                         ("#PS-1", 99, 7, "Joao", "cliente fechado", "obs", "hugo", None))
        self.assertTrue(linha["criado_em"])

    def test_marcar_duas_vezes_mantem_um_ativo(self):
        self.assertIsNotNone(self._marcar())
        self.assertIsNone(self._marcar())
        self.assertEqual(len(pr.ativos(self.conn)), 1)

    def test_recebido_sai_dos_ativos(self):
        self._nucleo()
        self.conn.execute("INSERT INTO nucleo_rotas VALUES (1, 99)")
        self.conn.execute("INSERT INTO nucleo_paradas VALUES (10, 1, 5)")
        self._marcar()
        pr.marcar_recebidos(self.conn, [10], 7)
        self.conn.commit()
        self.assertNotIn(5, pr.ativos(self.conn))
        linha = self.conn.execute("SELECT recebido_em, descarga_id FROM pedidos_retornando").fetchone()
        self.assertTrue(linha["recebido_em"])
        self.assertEqual(linha["descarga_id"], 7)
        self.assertIsNotNone(self._marcar())
        self.assertIn(5, pr.ativos(self.conn))

    def test_separa_por_service_id_em_codigo_combinado(self):
        self._marcar(service_id=5, codigo="#PS-1, #PS-2")
        servicos = [{"id": 5, "code": "#PS-1, #PS-2"}, {"id": 6, "code": "#PS-3"}]
        ficam, retornando = pr.separar_retornando(servicos, self.db)
        self.assertEqual([s["id"] for s in ficam], [6])
        self.assertEqual([s["id"] for s in retornando], [5])
        self.assertEqual(list(pr.retornando_por_servico(servicos, self.db)), [5])

    def test_resolve_parada_id_pelo_nucleo(self):
        self._nucleo()
        self.conn.execute("INSERT INTO nucleo_rotas VALUES (1, 99)")
        self.conn.execute("INSERT INTO nucleo_paradas VALUES (10, 1, 5)")
        self.conn.execute("INSERT INTO nucleo_paradas VALUES (11, 1, 5)")
        self.conn.commit()
        self.assertEqual(pr.resolver_parada_id(self.conn, 99, 5), 11)
        self._marcar()
        self.assertEqual(pr.ativos(self.conn)[5]["parada_id"], 11)

    def test_sem_tabelas_do_nucleo_parada_id_none(self):
        self.assertIsNone(pr.resolver_parada_id(self.conn, 99, 5))

    def test_banco_com_erro_nao_derruba_rodada(self):
        servicos = [{"id": 5, "code": "#PS-1"}]
        invalido = Path(self.tmp.name) / "nao_existe" / "x.db"
        with self.assertLogs(pr.logger, level="WARNING"):
            ficam, retornando = pr.separar_retornando(servicos, invalido)
        self.assertEqual((ficam, retornando), (servicos, []))
        with self.assertLogs(pr.logger, level="WARNING"):
            self.assertEqual(pr.retornando_por_servico(servicos, invalido), {})

    def test_sem_tabelas_do_nucleo_loga_aviso(self):
        with self.assertLogs(pr.logger, level="WARNING") as cm:
            self.assertIsNone(pr.resolver_parada_id(self.conn, 99, 5))
        self.assertIn("nao resolveu parada", cm.output[0])

    def test_marcar_sem_parada_loga_aviso(self):
        with self.assertLogs(pr.logger, level="WARNING") as cm:
            self._marcar()
        self.assertTrue(any("resolver_pendentes" in m for m in cm.output))

    def test_resolver_pendentes_preenche_parada_depois(self):
        self._marcar()
        self.assertIsNone(pr.ativos(self.conn)[5]["parada_id"])
        self._nucleo()
        self.conn.execute("INSERT INTO nucleo_rotas VALUES (1, 99)")
        self.conn.execute("INSERT INTO nucleo_paradas VALUES (10, 1, 5)")
        self.conn.commit()
        self.assertEqual(pr.resolver_pendentes(self.conn), 1)
        self.assertEqual(pr.ativos(self.conn)[5]["parada_id"], 10)
        pr.marcar_recebidos(self.conn, [10], 3)
        self.conn.commit()
        self.assertNotIn(5, pr.ativos(self.conn))

    def test_resolver_pendentes_sem_nucleo_nao_quebra(self):
        self._marcar()
        self.assertEqual(pr.resolver_pendentes(self.conn), 0)
        self.assertIn(5, pr.ativos(self.conn))

    def test_marcar_recebidos_nao_comita_e_lista_vazia_e_noop(self):
        self._nucleo()
        self.conn.execute("INSERT INTO nucleo_rotas VALUES (1, 99)")
        self.conn.execute("INSERT INTO nucleo_paradas VALUES (10, 1, 5)")
        self.conn.commit()
        self._marcar()
        pr.marcar_recebidos(self.conn, [], 7)
        self.assertIn(5, pr.ativos(self.conn))
        pr.marcar_recebidos(self.conn, [10], 7)
        self.conn.rollback()
        self.assertIn(5, pr.ativos(self.conn))


if __name__ == "__main__":
    unittest.main()
