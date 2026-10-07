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


if __name__ == "__main__":
    unittest.main()
