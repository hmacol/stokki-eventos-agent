# -*- coding: utf-8 -*-
"""
Textos ao embarcador dizem "(quinzenal)" nas regiões quinzenais (Hugo, 07/10).
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_texto_quinzenal -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "roteirizacao", _RAIZ / "insucesso_entrega"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import regioes_dia_fixo as rdf  # noqa: E402
import notificar_agendamento_dia_fixo as ag  # noqa: E402
import notificar_insucesso_aguardando_resposta as ins  # noqa: E402

AMERICANA = {"id": 1, "code": "#PS-1", "title": "Mercado X",
             "address": "Rua A 1, Centro, Americana - SP, 13465-000, Brasil"}
CAMPINAS = {"id": 2, "code": "#PS-2", "title": "Mercado Y",
            "address": "Rua B 2, Centro, Campinas - SP, 13015-001, Brasil"}


class TestEmailAgendamentoDiaFixo(unittest.TestCase):
    def test_item_com_regra_quinzenal_diz_quinzenal(self):
        regra = rdf.regra_dia_fixo_do_servico(AMERICANA)
        html = ag._montar_conteudo("Remetente", [{"servico": AMERICANA, "regiao": regra["nome"], "dias": regra["dias"],
                                                  "regra": regra, "data": date(2026, 10, 14)}])
        self.assertIn("Americana (Quartas (quinzenal))", html)

    def test_item_antigo_so_com_dias_continua_funcionando(self):
        html = ag._montar_conteudo("Remetente", [{"servico": CAMPINAS, "regiao": "Campinas", "dias": [2],
                                                  "data": date(2026, 10, 14)}])
        self.assertIn("Campinas (Quartas)", html)

    def test_aplicar_devolve_a_regra_no_item(self):
        class Vuupt:
            def atualizar_servico(self, sid, payload):
                pass
        import tempfile
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            itens = rdf.aplicar_regioes_dia_fixo([dict(AMERICANA)], Vuupt(), hoje=date(2026, 10, 7),
                                                 db_path=Path(tmp) / "t.db")
        self.assertEqual(itens[0]["regra"]["frequencia"], rdf.FREQUENCIA_QUINZENAL)
        self.assertEqual(itens[0]["data"], date(2026, 10, 14))


class TestEmailInsucesso(unittest.TestCase):
    def test_dias_fixos_do_grupo_com_quinzenal(self):
        dias, todos = ins._dias_fixos_do_grupo([AMERICANA, CAMPINAS])
        self.assertEqual(dias, {"Americana": "Quartas (quinzenal)", "Campinas": "Quartas"})
        self.assertTrue(todos)


if __name__ == "__main__":
    unittest.main()
