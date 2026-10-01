# -*- coding: utf-8 -*-
"""
Rotas fracas (Hugo, 29/09): corte, prazo de 3 dias uteis, motivo de nao
segurar e juncao com folga. Spec: docs/superpowers/specs/
2026-09-29-rotas-fracas-design.md
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_rotas_fracas -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
import rotas_fracas as rf

BASE = (0.0, 0.0)
TERCA = date(2026, 9, 29)


def _servico(i, lat=0.10, lng=0.0, caixas=1, nivel=1, **extra):
    s = {"id": i, "code": f"#PS-{1000 + i}", "address": f"P{i}", "_nivel_dificuldade": nivel,
         "latitude": lat, "longitude": lng, "dimension_3": caixas, "sender_id": 1,
         "created_at": "2026-09-28 15:00:00"}  # segunda, 12h de Brasilia
    s.update(extra)
    return s


def _ids(sublotes):
    return sorted(s["id"] for sub in sublotes for s in sub)


class TestCorte(unittest.TestCase):
    def test_sete_pedidos_e_quarenta_caixas_e_fraca(self):
        rota = [_servico(i, caixas=5) for i in range(7)] + []
        rota[0]["dimension_3"] = 10  # 10 + 6*5 = 40
        self.assertTrue(rf.eh_rota_fraca(rota))

    def test_oito_pedidos_nao_e_fraca(self):
        self.assertFalse(rf.eh_rota_fraca([_servico(i) for i in range(8)]))

    def test_quarenta_e_uma_caixas_nao_e_fraca(self):
        self.assertFalse(rf.eh_rota_fraca([_servico(1, caixas=41)]))

    def test_um_pedido_com_muita_carga_nao_e_fraca(self):
        self.assertFalse(rf.eh_rota_fraca([_servico(1, caixas=294)]))

    def test_rota_vazia_nao_e_fraca(self):
        self.assertFalse(rf.eh_rota_fraca([]))

    def test_resumo_da_rota(self):
        self.assertEqual(rf.resumo_da_rota([_servico(1, caixas=9), _servico(2, caixas=8)]), "2 pedidos, 17 caixas")
        self.assertEqual(rf.resumo_da_rota([_servico(1, caixas=1)]), "1 pedido, 1 caixa")


class TestPrazo(unittest.TestCase):
    def test_entrada_segunda_vence_quinta(self):
        self.assertEqual(rf.prazo_final(date(2026, 9, 28)), date(2026, 10, 1))

    def test_entrada_sexta_vence_quarta(self):
        self.assertEqual(rf.prazo_final(date(2026, 10, 2)), date(2026, 10, 7))

    def test_entrada_sabado_conta_a_partir_de_segunda(self):
        self.assertEqual(rf.prazo_final(date(2026, 10, 3)), date(2026, 10, 8))

    def test_proximo_dia_util_pula_fim_de_semana(self):
        self.assertEqual(rf.proximo_dia_util(date(2026, 10, 2)), date(2026, 10, 5))
        self.assertEqual(rf.proximo_dia_util(date(2026, 9, 29)), date(2026, 9, 30))

    def test_entrada_converte_utc_para_brasilia(self):
        # 01:30 UTC de terca = 22:30 de segunda em Brasilia
        self.assertEqual(rf.data_entrada({"created_at": "2026-09-29 01:30:00"}), date(2026, 9, 28))

    def test_entrada_respeita_fuso_explicito(self):
        self.assertEqual(rf.data_entrada({"created_at": "2026-09-28T23:30:00-03:00"}), date(2026, 9, 28))

    def test_entrada_ausente_ou_invalida(self):
        self.assertIsNone(rf.data_entrada({}))
        self.assertIsNone(rf.data_entrada({"created_at": "ontem"}))


class TestMotivoNaoSegurar(unittest.TestCase):
    def setUp(self):
        for alvo, valor in ((("macro_regiao_do_servico"), lambda s, k=None: rd.MACRO_GRANDE_SP),
                            (("regra_dia_fixo_do_servico"), lambda s: None)):
            p = mock.patch.object(rf, alvo, valor)
            p.start()
            self.addCleanup(p.stop)

    def test_pedido_comum_pode_esperar(self):
        self.assertIsNone(rf.motivo_nao_segurar(_servico(1), TERCA, set()))

    def test_agendamento(self):
        s = _servico(1, scheduled_start="2026-09-29T09:00:00-03:00")
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, set()), "tem agendamento")

    def test_reentrega_pelo_campo(self):
        s = _servico(1, recreated_order_origin_id=555)
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, set()), "é reentrega")

    def test_reentrega_pelo_sufixo(self):
        s = _servico(1, code="#PS-1001-R1")
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, set()), "é reentrega")

    def test_ja_segurado(self):
        self.assertEqual(rf.motivo_nao_segurar(_servico(1), TERCA, {"PS-1001"}), "já foi segurado uma vez")

    def test_codigo_combinado_basta_um_ja_segurado(self):
        s = _servico(1, code="#PS-1001, PS-2002")
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, {"PS-2002"}), "já foi segurado uma vez")

    def test_dia_fixo(self):
        with mock.patch.object(rf, "regra_dia_fixo_do_servico", lambda s: {"nome": "Americana", "dias": [2]}):
            self.assertEqual(rf.motivo_nao_segurar(_servico(1), TERCA, set()), "é de região de dia fixo")

    def test_viagem(self):
        with mock.patch.object(rf, "macro_regiao_do_servico", lambda s, k=None: "Campinas"):
            self.assertEqual(rf.motivo_nao_segurar(_servico(1), TERCA, set()), "é viagem")

    def test_sem_data_de_entrada(self):
        s = _servico(1)
        del s["created_at"]
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, set()), "sem data de entrada")

    def test_prazo_estouraria(self):
        # entrou quarta 23/09 -> prazo segunda 28/09; rota de terca 29/09 ja esta atrasada
        s = _servico(1, created_at="2026-09-23 15:00:00")
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, set()), "prazo vence em 28/09")

    def test_prazo_no_limite_pode(self):
        # entrou sexta 25/09 -> prazo quarta 30/09; rota de terca 29/09 adiada vai pra quarta 30/09
        s = _servico(1, created_at="2026-09-25 15:00:00")
        self.assertIsNone(rf.motivo_nao_segurar(s, TERCA, set()))


if __name__ == "__main__":
    unittest.main()
