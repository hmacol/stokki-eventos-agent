# -*- coding: utf-8 -*-
"""
test_comparar_pool.py -- comparador do pool Vuupt × núcleo (sombra da Entrega 1).

    py -3.11 -m unittest nucleo.test_comparar_pool -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco, comparar_pool as cp


def _s(code="#PS-10", **extra):
    # endereço no formato real da Vuupt ("..., Cidade - UF, CEP"), que é o que extrair_cidade lê
    base = {"id": 1, "code": code, "address": "Rua A, 1, Centro, Sorocaba - SP, 18010-000", "latitude": -23.5, "longitude": -46.6,
            "dimension_3": 4, "sender_id": 11, "scheduled_start": "2026-09-25 11:00:00",
            "scheduled_end": "2026-09-25 15:00:00",
            "customer": {"code": "12.345.678/0001-90", "operating_hour_start": "08:00", "operating_hour_end": "17:00"}}
    base.update(extra)
    return base


class TestComparar(unittest.TestCase):
    def test_iguais_nao_dao_divergencia(self):
        r = cp.comparar([_s()], [_s()])
        self.assertEqual(r["total_divergencias"], 0)
        self.assertEqual((r["total_vuupt"], r["total_nucleo"]), (1, 1))

    def test_codigo_com_e_sem_cerquilha_e_o_mesmo_pedido(self):
        self.assertEqual(cp.comparar([_s(code="#PS-10")], [_s(code="PS-10")])["total_divergencias"], 0)

    def test_so_de_um_lado(self):
        r = cp.comparar([_s(code="#PS-10"), _s(code="#PS-20", id=2)], [_s(code="#PS-10"), _s(code="#PS-30", id=3)])
        self.assertEqual(r["so_na_vuupt"], ["PS-20"])
        self.assertEqual(r["so_no_nucleo"], ["PS-30"])
        self.assertEqual(r["total_divergencias"], 2)

    def test_campo_diferente_aponta_o_campo(self):
        r = cp.comparar([_s()], [_s(dimension_3=9, latitude=-23.50000001)])
        self.assertEqual(len(r["diferentes"]), 1)
        self.assertEqual(list(r["diferentes"][0]["campos"]), ["dimension_3"])   # lat arredondada a 5 casas: igual
        self.assertEqual(r["diferentes"][0]["campos"]["dimension_3"], [4, 9])

    def test_codigo_repetido_na_vuupt_e_apontado_como_duplicado(self):
        """Achado 24/09 (PS-39958): dois serviços not_assigned com o MESMO
        código na Vuupt. O núcleo, chaveado por código, guarda um só e mescla
        os campos -- a tela mostraria 2 cards pela Vuupt e 1 pelo núcleo.
        É divergência, mas com nome próprio pra explicar o placar."""
        r = cp.comparar([_s(id=1), _s(id=2, scheduled_start=None, scheduled_end=None)], [_s(id=2)])
        self.assertEqual(r["duplicados_na_vuupt"], ["PS-10"])
        self.assertEqual(r["total_vuupt_bruto"], 2)
        self.assertEqual(r["total_vuupt"], 1)
        self.assertGreaterEqual(r["total_divergencias"], 1)
        self.assertIn("duplicad", cp.resumir(r))

    def test_duplicado_e_explicado_nao_conta_como_divergencia_propria(self):
        """Hugo, 24/09: duplicado na Vuupt não some sozinho (a sequência das
        18h só relata); entra no relatório, mas não no placar dos 3 dias."""
        r = cp.comparar([_s(id=1), _s(id=2)], [_s(id=2)])
        self.assertEqual(r["duplicados_na_vuupt"], ["PS-10"])
        self.assertEqual(r["divergencias_proprias"], 0)
        self.assertEqual(r["total_divergencias"], 1)

    def test_janela_e_dia_fixo_entram_na_projecao(self):
        p = cp.projetar(_s())
        self.assertEqual(p["janela"], ("11:00", "15:00"))
        self.assertEqual(p["agendado_para"], "2026-09-25")
        self.assertEqual(p["dia_fixo"], "Sorocaba")


class TestPlacar(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def test_dias_limpos_seguidos_conta_por_dia_e_para_na_primeira_divergencia(self):
        limpo = cp.comparar([_s()], [_s()])
        sujo = cp.comparar([_s()], [])
        for rodado_em, r in (("2026-09-20 08:10:00", sujo), ("2026-09-21 08:10:00", limpo),
                             ("2026-09-21 09:10:00", limpo), ("2026-09-22 08:10:00", limpo)):
            cp.salvar(r, self.conn, rodado_em=rodado_em)
        self.assertEqual(cp.dias_limpos_seguidos(self.conn), 2)
        self.assertEqual(len(cp.historico(self.conn, 10)), 3)   # uma linha por dia

    def test_dia_so_com_duplicado_na_vuupt_conta_como_limpo(self):
        so_duplicado = cp.comparar([_s(id=1), _s(id=2)], [_s(id=2)])
        cp.salvar(so_duplicado, self.conn, rodado_em="2026-09-23 08:10:00")
        self.assertEqual(cp.dias_limpos_seguidos(self.conn), 1)
        self.assertEqual(cp.historico(self.conn, 1)[0]["duplicados"], 1)


if __name__ == "__main__":
    unittest.main()
