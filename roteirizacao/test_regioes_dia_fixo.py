# -*- coding: utf-8 -*-
"""
Regras de região de dia fixo v2 (Hugo, 03/10/2026): configuração nova,
frequência quinzenal, nível/prazo e data de visita.
Spec: docs/superpowers/specs/2026-10-03-dias-fixos-v2-design.md
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_regioes_dia_fixo -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import regioes_dia_fixo as rdf

ABCD = {"address": "Rua das Figueiras 100, Jardim, Santo André - SP, 09080-300, Brasil"}
TRANSFRIOS = {"address": "Estrada Francisco Hengles, 591, Potuvera, Itapecerica da Serra - SP, 06885-160, Brasil"}
SOROCABA = {"address": "Rua XV de Novembro 10, Centro, Sorocaba - SP, 18010-080, Brasil"}
CAMPINAS = {"address": "Rua Barão de Jaguara 900, Centro, Campinas - SP, 13015-001, Brasil"}
SAO_PAULO = {"address": "Rua Augusta 100, Consolação, São Paulo - SP, 01304-000, Brasil"}

QUINZENAL = {"nome": "Sorocaba", "dias": [rdf.TERCA], "frequencia": rdf.FREQUENCIA_QUINZENAL,
             "ancora": "2026-10-13"}


class TestConfiguracao(unittest.TestCase):
    def test_abcd_segunda_e_quinta_nivel_interno(self):
        regra = rdf.regra_dia_fixo_do_servico(ABCD)
        self.assertEqual(regra["dias"], [rdf.SEGUNDA, rdf.QUINTA])
        self.assertEqual(regra["regiao"], "ABCD")
        self.assertEqual(regra["nome"], "Santo André")
        self.assertEqual(regra["nivel"], rdf.NIVEL_INTERNA)
        self.assertEqual((regra["prazo_dias"], regra["prazo_dias_uteis"]), (3, True))

    def test_transfrios_terca_e_quinta(self):
        regra = rdf.regra_dia_fixo_do_servico(TRANSFRIOS)
        self.assertEqual(regra["dias"], [rdf.TERCA, rdf.QUINTA])
        self.assertEqual((regra["origem"], regra["regiao"], regra["nivel"]), ("endereco", "Transfrios", rdf.NIVEL_INTERNA))

    def test_sorocaba_quinzenal_com_ancora_numa_terca(self):
        regra = rdf.regra_dia_fixo_do_servico(SOROCABA)
        self.assertEqual(regra["frequencia"], rdf.FREQUENCIA_QUINZENAL)
        self.assertEqual(date.fromisoformat(regra["ancora"]).weekday(), rdf.TERCA)
        self.assertEqual(regra["nivel"], rdf.NIVEL_QUINZENAL)
        self.assertEqual((regra["prazo_dias"], regra["prazo_dias_uteis"]), (15, False))

    def test_campinas_semanal(self):
        regra = rdf.regra_dia_fixo_do_servico(CAMPINAS)
        self.assertEqual((regra["dias"], regra["frequencia"]), ([rdf.QUARTA], rdf.FREQUENCIA_SEMANAL))
        self.assertEqual((regra["nivel"], regra["prazo_dias"], regra["prazo_dias_uteis"]), (rdf.NIVEL_SEMANAL, 7, False))

    def test_grande_sp_sem_regra(self):
        self.assertIsNone(rdf.regra_dia_fixo_do_servico(SAO_PAULO))


class TestDataValida(unittest.TestCase):
    def test_semanal(self):
        regra = rdf.regra_dia_fixo_do_servico(CAMPINAS)
        self.assertTrue(rdf.data_valida_na_regiao(regra, date(2026, 10, 7)))    # quarta
        self.assertFalse(rdf.data_valida_na_regiao(regra, date(2026, 10, 8)))   # quinta

    def test_duas_vezes_por_semana(self):
        regra = rdf.regra_dia_fixo_do_servico(ABCD)
        self.assertTrue(rdf.data_valida_na_regiao(regra, date(2026, 10, 5)))    # segunda
        self.assertTrue(rdf.data_valida_na_regiao(regra, date(2026, 10, 8)))    # quinta
        self.assertFalse(rdf.data_valida_na_regiao(regra, date(2026, 10, 7)))   # quarta
        self.assertFalse(rdf.data_valida_na_regiao(regra, date(2026, 10, 9)))   # sexta

    def test_quinzenal_semana_par_desde_a_ancora(self):
        self.assertTrue(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 10, 13)))   # a própria âncora
        self.assertFalse(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 10, 20)))  # semana ímpar
        self.assertTrue(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 10, 27)))
        self.assertFalse(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 11, 3)))   # virada do mês
        self.assertTrue(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 11, 10)))
        self.assertFalse(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 10, 6)))   # antes da âncora, ímpar
        self.assertTrue(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 9, 29)))    # antes da âncora, par

    def test_quinzenal_dia_da_semana_errado(self):
        self.assertFalse(rdf.data_valida_na_regiao(QUINZENAL, date(2026, 10, 14)))  # quarta da semana válida

    def test_regra_sem_frequencia_e_semanal(self):
        self.assertTrue(rdf.data_valida_na_regiao({"nome": "X", "dias": [rdf.TERCA]}, date(2026, 10, 20)))


class TestProximaData(unittest.TestCase):
    def test_quinzenal_pula_a_semana_sem_visita(self):
        self.assertEqual(rdf.proxima_data_valida(QUINZENAL, date(2026, 10, 13)), date(2026, 10, 27))
        self.assertEqual(rdf.proxima_data_valida(QUINZENAL, date(2026, 10, 14)), date(2026, 10, 27))
        self.assertEqual(rdf.proxima_data_valida(QUINZENAL, date(2026, 10, 12)), date(2026, 10, 13))

    def test_ajustar_data_usa_os_dias_novos(self):
        data, regra = rdf.ajustar_data_por_dia_fixo(ABCD, date(2026, 10, 7))   # quarta
        self.assertEqual(data, date(2026, 10, 8))                               # quinta
        self.assertEqual(regra["regiao"], "ABCD")
        self.assertEqual(rdf.ajustar_data_por_dia_fixo(ABCD, date(2026, 10, 8)), (date(2026, 10, 8), None))

    def test_descricao_dias(self):
        self.assertEqual(rdf.descricao_dias(QUINZENAL), "Terças (quinzenal)")
        self.assertEqual(rdf.descricao_dias(rdf.regra_dia_fixo_do_servico(ABCD)), "Segundas e Quintas")


SJC_TRUNCADO = {"address": "Av. Cassiano Ricardo 601, Jardim Aquarius, SAO JOSE DOS CA - SP, 12246-870, Brasil"}


class TestCidadeTruncada(unittest.TestCase):
    def test_nome_truncado_reconhece_a_cidade(self):
        regra = rdf.regra_dia_fixo_do_servico(SJC_TRUNCADO)
        self.assertEqual(regra["regiao"], "Vale do Paraíba")
        self.assertEqual(regra["nome"], "Sao Jose Dos Campos")
        self.assertEqual(regra["dias"], [rdf.SEGUNDA])

    def test_truncado_vale_pra_viagem_e_mensagens(self):
        self.assertEqual(rdf.regiao_externa_da_cidade("SAO JOSE DOS CA"), "Vale do Paraíba")
        self.assertEqual(rdf.regiao_da_cidade("Sao Jose dos Ca"), "Vale do Paraíba")
        self.assertEqual(rdf.dias_fixos_da_cidade("SAO JOSE DOS CA"), [rdf.SEGUNDA])

    def test_prefixo_curto_nao_reconhece(self):
        self.assertIsNone(rdf.regiao_da_cidade("SAO JOSE"))      # 7 letras
        self.assertIsNone(rdf.regiao_da_cidade("SANTO"))

    def test_prefixo_ambiguo_nao_reconhece(self):
        extra = {"SAO JOSE DOS CAMPINHOS": {"dias": [rdf.SEXTA], "regiao": "Teste", "externa": True,
                                            "frequencia": rdf.FREQUENCIA_SEMANAL, "ancora": None,
                                            "cidade": "SAO JOSE DOS CAMPINHOS"}}
        with mock.patch.dict(rdf._INDICE_CIDADES, extra):
            self.assertIsNone(rdf.regiao_da_cidade("SAO JOSE DOS CA"))

    def test_nome_completo_continua_igual(self):
        self.assertEqual(rdf.regiao_da_cidade("São José dos Campos"), "Vale do Paraíba")
        self.assertEqual(rdf.regra_dia_fixo_do_servico(CAMPINAS)["nome"], "Campinas")


if __name__ == "__main__":
    unittest.main()
