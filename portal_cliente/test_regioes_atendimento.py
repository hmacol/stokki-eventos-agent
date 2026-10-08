# -*- coding: utf-8 -*-
"""
Página pública /regioes do portal: dados montados da configuração real.
    py -3.11 -m unittest portal_cliente.test_regioes_atendimento -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path

_AQUI = Path(__file__).parent
for _p in (_AQUI.parent, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import regioes_atendimento as ra  # noqa: E402

HOJE = date(2026, 10, 7)


class TestOutubro2026(unittest.TestCase):
    def setUp(self):
        self.d = ra.montar(2026, 10, hoje=HOJE)

    def _regiao(self, nome):
        return next(r for r in self.d["regioes"] if r["nome"].startswith(nome))

    def test_regioes_na_ordem_com_grande_sp_primeiro(self):
        self.assertEqual(self.d["regioes"][0]["nome"], "Grande São Paulo")
        self.assertEqual(self.d["regioes"][0]["frequencia"], "Diária")
        self.assertEqual(self.d["regioes"][0]["prazo"], "até 3 dias úteis")
        self.assertEqual(len(self.d["regioes"]), 1 + 7)
        self.assertEqual(len(self.d["galpoes"]), 4)

    def test_abcd_pula_o_feriado_e_vai_pra_terca(self):
        abcd = self._regiao("ABCD")
        dias = [d.day for d in abcd["datas"]]
        self.assertEqual(dias, [1, 5, 8, 13, 15, 19, 22, 26, 29])
        self.assertEqual((abcd["dias_texto"], abcd["frequencia"]), ("Segunda e quinta", "2x por semana"))

    def test_sorocaba_quinzenal(self):
        s = self._regiao("Sorocaba")
        self.assertEqual([d.day for d in s["datas"]], [6, 20])
        self.assertEqual((s["frequencia"], s["prazo"], s["quinzenal"]), ("Quinzenal", "até 15 dias corridos", True))

    def test_piracicaba_quinzenal_14_e_28(self):
        p = self._regiao("Piracicaba")
        self.assertEqual([d.day for d in p["datas"]], [14, 28])
        self.assertEqual((p["frequencia"], p["quinzenal"]), ("Quinzenal", True))
        celulas = {c["dia"]: c for semana in self.d["semanas"] for c in semana if c}
        self.assertNotIn("Piracicaba", [c["nome"] for c in celulas[21]["chips"]])
        self.assertIn("Piracicaba", [c["nome"] for c in celulas[28]["chips"]])

    def test_galpao_tem_endereco_da_configuracao(self):
        taff = next(g for g in self.d["galpoes"] if g["nome"] == "TAFF")
        self.assertIn("Vila Lobos Quero", taff["endereco"])
        self.assertEqual([d.day for d in taff["datas"]], [1, 6, 8, 13, 15, 20, 22, 27, 29])

    def test_calendario_feriado_sem_chips_e_terca_herda(self):
        celulas = {c["dia"]: c for semana in self.d["semanas"] for c in semana if c}
        self.assertEqual(celulas[12]["feriado"], "Nossa Senhora Aparecida")
        self.assertEqual(celulas[12]["chips"], [])
        terca = celulas[13]
        nomes = {c["nome"]: c["transferida"] for c in terca["chips"]}
        self.assertEqual(nomes["Vale do Paraíba"], True)
        self.assertEqual(nomes["ABCD"], True)
        self.assertEqual(nomes["Barueri"], False)
        self.assertTrue(celulas[7]["hoje"])
        self.assertEqual(len(self.d["semanas"]), 5)
        self.assertIsNone(self.d["semanas"][0][0])   # 1/10 e quinta: seg a qua vazios

    def test_nota_do_feriado(self):
        self.assertEqual(len(self.d["notas"]), 1)
        self.assertIn("12/10 (segunda) é feriado", self.d["notas"][0])
        self.assertIn("terça 13/10", self.d["notas"][0])
        self.assertIn("Vale do Paraíba, ABCD, Superfrio/TAC", self.d["notas"][0])

    def test_navegacao_de_meses(self):
        self.assertEqual((self.d["mes_anterior"], self.d["mes_seguinte"]), ("2026-09", "2026-11"))
        self.assertTrue(self.d["pode_voltar"] and self.d["pode_avancar"])
        self.assertFalse(ra.montar(2027, 1, hoje=HOJE)["pode_avancar"])


class TestValidarMes(unittest.TestCase):
    def test_aceita_dentro_da_janela(self):
        self.assertEqual(ra.validar_mes("2026-11", HOJE), (2026, 11))
        self.assertEqual(ra.validar_mes("2026-09", HOJE), (2026, 9))

    def test_fora_da_janela_ou_lixo_cai_no_mes_atual(self):
        for lixo in ("2026-08", "2027-03", "abc", "", None, "2026-13", "2026-1x"):
            self.assertEqual(ra.validar_mes(lixo, HOJE), (2026, 10), lixo)


if __name__ == "__main__":
    unittest.main()
