# -*- coding: utf-8 -*-
"""Rodar (da raiz): py -3.11 -m unittest test_reagendar_feriado -v"""
import unittest
from datetime import date

import reagendar_feriado as rf

FERIADO = date(2026, 10, 12)   # segunda, N. Sra. Aparecida


def svc(sid, code, address, scheduled):
    return {"id": sid, "code": code, "address": address, "scheduled_start": scheduled}


class TestPlano(unittest.TestCase):
    def test_so_pega_quem_esta_no_feriado(self):
        pool = [
            svc(1, "PS-1", "Rua A 1, Centro, Jacareí - SP, 12300-000, Brasil", "2026-10-12T08:00:00-03:00"),
            svc(2, "PS-2", "Rua B 2, Centro, Santo André - SP, 09000-000, Brasil", "2026-10-12 11:00:00"),  # UTC = 08h local
            svc(3, "PS-3", "Rua C 3, Centro, São Paulo - SP, 01000-000, Brasil", "2026-10-12T08:00:00-03:00"),
            svc(4, "PS-4", "Rua D 4, Centro, Santo André - SP, 09000-000, Brasil", "2026-10-15T08:00:00-03:00"),
            svc(5, "PS-5", "Rua E 5, Centro, São Paulo - SP, 01000-000, Brasil", None),
        ]
        itens = rf.plano(pool, FERIADO)
        self.assertEqual([i["servico"]["code"] for i in itens], ["PS-1", "PS-2", "PS-3"])
        # Vale (segunda), ABCD (segunda/quinta) e Grande SP: todos pro dia util seguinte
        self.assertEqual([i["para"] for i in itens], [date(2026, 10, 13)] * 3)
        self.assertEqual([i["regiao"] for i in itens], ["Vale do Paraíba", "ABCD", "Grande SP"])

    def test_quinzenal_fora_da_semana_espera_a_visita(self):
        # Sorocaba (terca quinzenal, ancora 06/10): feriado numa terca de visita hipotetica
        s = svc(9, "PS-9", "Rua X 1, Centro, Sorocaba - SP, 18000-000, Brasil", "2026-11-02T08:00:00-03:00")
        # 02/11 e segunda e feriado; Sorocaba so visita terca 03/11 (semana par) -> 03/11
        self.assertEqual(rf.nova_data(s, date(2026, 11, 2)), date(2026, 11, 3))

    def test_proximo_feriado_em_dia_de_semana(self):
        self.assertEqual(rf.proximo_feriado(date(2026, 10, 7)), date(2026, 10, 12))
        self.assertEqual(rf.proximo_feriado(date(2026, 11, 3)), date(2026, 11, 20))   # 15/11 cai no domingo
        self.assertIsNone(rf.proximo_feriado(date(2026, 8, 1), dias=10))


class TestAplicar(unittest.TestCase):
    def test_grava_na_vuupt_e_registra_origem(self):
        import tempfile
        from pathlib import Path
        import registro_dia_fixo

        class Vuupt:
            def __init__(self):
                self.chamadas = []

            def atualizar_servico(self, sid, payload):
                self.chamadas.append((sid, payload))

        s = svc(7, "PS-7", "Rua A 1, Centro, Jacareí - SP, 12300-000, Brasil", "2026-10-12T08:00:00-03:00")
        v = Vuupt()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "t.db"
            n = rf.aplicar(rf.plano([s], FERIADO), v, db_path=db)
            conn = registro_dia_fixo.conectar(db)
            try:
                self.assertTrue(registro_dia_fixo.data_nao_e_do_cliente(conn, s, date(2026, 10, 13)))
            finally:
                conn.close()
        self.assertEqual(n, 1)
        self.assertEqual(v.chamadas[0][0], 7)
        self.assertEqual(v.chamadas[0][1]["scheduled_start"], "2026-10-13T08:00:00-03:00")

    def test_feriado_preserva_origem_automatica(self):
        import tempfile
        from pathlib import Path
        import registro_dia_fixo

        class Vuupt:
            def atualizar_servico(self, sid, payload):
                pass

        auto = svc(8, "PS-8", "Rua A 1, Centro, Jacareí - SP, 12300-000, Brasil", "2026-10-12T08:00:00-03:00")
        manual = svc(9, "PS-9", "Rua A 2, Centro, Jacareí - SP, 12300-000, Brasil", "2026-10-12T08:00:00-03:00")
        nova = date(2026, 10, 13)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "t.db"
            registro_dia_fixo.registrar_origens([{"servico": auto, "data": FERIADO}],
                                                registro_dia_fixo.ORIGEM_AUTOMATICO, db)
            n = rf.aplicar(rf.plano([auto, manual], FERIADO), Vuupt(), db_path=db)
            conn = registro_dia_fixo.conectar(db)
            try:
                self.assertTrue(registro_dia_fixo.data_e_automatica(conn, auto, nova))
                self.assertFalse(registro_dia_fixo.data_e_automatica(conn, manual, nova))
                self.assertTrue(registro_dia_fixo.data_nao_e_do_cliente(conn, manual, nova))
            finally:
                conn.close()
        self.assertEqual(n, 2)

    def test_falha_na_consulta_de_origem_nao_para_o_reagendamento(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        import registro_dia_fixo

        class Vuupt:
            def __init__(self):
                self.chamadas = []

            def atualizar_servico(self, sid, payload):
                self.chamadas.append(sid)

        s = svc(11, "PS-11", "Rua A 1, Centro, Jacareí - SP, 12300-000, Brasil", "2026-10-12T08:00:00-03:00")
        nova = date(2026, 10, 13)
        for alvo in ("data_e_automatica", "conectar"):
            with self.subTest(alvo=alvo), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                db = Path(tmp) / "t.db"
                original = registro_dia_fixo.conectar
                v = Vuupt()
                if alvo == "conectar":
                    # falha so na 1a chamada (a de aplicar); o registro posterior conecta normal
                    chamadas = []

                    def conectar_quebrado(*a, **k):
                        chamadas.append(1)
                        if len(chamadas) == 1:
                            raise RuntimeError("banco travado")
                        return original(*a, **k)
                    patch = mock.patch.object(registro_dia_fixo, "conectar", conectar_quebrado)
                else:
                    patch = mock.patch.object(registro_dia_fixo, "data_e_automatica",
                                              side_effect=RuntimeError("consulta falhou"))
                with patch:
                    n = rf.aplicar(rf.plano([s], FERIADO), v, db_path=db)
                self.assertEqual(n, 1)
                self.assertEqual(v.chamadas, [11])
                conn = original(db)
                try:
                    self.assertFalse(registro_dia_fixo.data_e_automatica(conn, s, nova))
                    self.assertTrue(registro_dia_fixo.data_nao_e_do_cliente(conn, s, nova))
                finally:
                    conn.close()


class TestPoolDiretoDaStokki(unittest.TestCase):
    """Fonte stokki (Task 7): o pool vem do nucleo e o pedido sem servico na Vuupt (id sintetico)
    ganha a data nova nas colunas do nucleo (editar_sem_servico), nunca na Vuupt."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from nucleo import banco, pedidos
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(banco, "DB_PATH", Path(self.tmp.name) / "n.db")
        p.start()
        self.addCleanup(p.stop)
        self.pedidos = pedidos
        conn = banco.conectar()
        pedidos.registrar_importacao({"code": "#PS-20", "title": "#PS-20", "sender_id": 1,
                                      "address": "Rua A 1, Centro, Jacareí - SP, 12300-000, Brasil",
                                      "scheduled_start": "2026-10-12T08:00:00-03:00",
                                      "scheduled_end": "2026-10-12T16:00:00-03:00"}, None, "nucleo_sem_servico",
                                     conn=conn)
        conn.close()

    def test_pool_vem_do_nucleo_com_a_chave(self):
        from unittest import mock
        vuupt = mock.Mock()
        pool = rf.ler_pool({"planejamento": {"fonte_pool": "stokki"}}, vuupt)
        self.assertEqual([s["code"] for s in pool], ["#PS-20"])
        self.assertLess(pool[0]["id"], 0)
        vuupt.listar_servicos.assert_not_called()

    def test_chave_desligada_igual_a_hoje(self):
        from unittest import mock
        vuupt = mock.Mock()
        vuupt.listar_servicos.return_value = ["x"]
        self.assertEqual(rf.ler_pool({}, vuupt), ["x"])
        vuupt.listar_servicos.assert_called_once_with([{"field": "status", "operator": "eq", "value": "not_assigned"}])

    def test_sem_servico_grava_no_nucleo_e_nao_chama_a_vuupt(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        pool = rf.ler_pool({"planejamento": {"fonte_pool": "stokki"}}, mock.Mock())
        vuupt = mock.Mock()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            n = rf.aplicar(rf.plano(pool, FERIADO), vuupt, db_path=Path(tmp) / "t.db")
        self.assertEqual(n, 1)
        vuupt.atualizar_servico.assert_not_called()
        linha = self.pedidos.buscar_pedido("PS-20")
        self.assertEqual((linha["agendamento_inicio"], linha["agendamento_fim"]),
                         ("2026-10-13 08:00:00", "2026-10-13 16:00:00"))
        self.assertIn("agendamento_inicio", self.pedidos.campos_editados(linha["edicao_local_json"]))


if __name__ == "__main__":
    unittest.main()
