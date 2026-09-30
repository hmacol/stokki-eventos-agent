# -*- coding: utf-8 -*-
"""
test_torre_tendencia.py

Testes do grafico "Entregas finalizadas - ultimos 7 dias uteis" da Torre
(Hugo, 16/09: cada coluna dividida em sucesso (verde) e falha (vermelho)).

Nenhum teste toca na rede: o VuuptClient e substituido por um falso que
responde as contagens a partir dos filtros recebidos.
Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_torre_tendencia -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import torre_controle  # noqa: E402


class VuuptFalso:
    """Devolve total/falha por dia (chave = valor do filtro gte de
    completed_at). Guarda os filtros recebidos pra conferir a chamada."""

    def __init__(self, por_dia: dict, quebrar_em: str | None = None):
        self.por_dia = por_dia
        self.quebrar_em = quebrar_em
        self.chamadas = []

    def contar_servicos(self, filtros):
        self.chamadas.append(filtros)
        dia = next(f["value"] for f in filtros if f["field"] == "completed_at" and f["operator"] == "gte")
        if dia == self.quebrar_em:
            raise RuntimeError("API fora")
        total, falha = self.por_dia.get(dia, (0, 0))
        so_falha = any(f["field"] == "status_done" and f["value"] == "failed" for f in filtros)
        return falha if so_falha else total


class TestColetarTendencia(unittest.TestCase):

    def test_divide_cada_dia_em_sucesso_e_falha(self):
        # 15/09 (ter) e 16/09 (qua): numeros reais da prova de 16/09
        vuupt = VuuptFalso({"2026-09-15": (197, 11), "2026-09-16": (173, 18)})
        dados = torre_controle._coletar_tendencia(vuupt, date(2026, 9, 16), dias=2)
        self.assertEqual(
            dados,
            [
                {"rotulo": "15/09", "data": "2026-09-15", "total": 197, "sucesso": 186, "falha": 11},
                {"rotulo": "16/09", "data": "2026-09-16", "total": 173, "sucesso": 155, "falha": 18},
            ],
        )

    def test_duas_contagens_por_dia_e_filtro_de_falha(self):
        vuupt = VuuptFalso({"2026-09-16": (10, 2)})
        torre_controle._coletar_tendencia(vuupt, date(2026, 9, 16), dias=1)
        self.assertEqual(len(vuupt.chamadas), 2)
        total, falha = vuupt.chamadas
        self.assertFalse(any(f["field"] == "status_done" for f in total))
        self.assertIn({"field": "status_done", "operator": "eq", "value": "failed"}, falha)
        # os dois filtros de completed_at continuam na 2a chamada
        self.assertEqual([f for f in falha if f["field"] == "completed_at"], total)

    def test_pula_fim_de_semana(self):
        vuupt = VuuptFalso({"2026-09-14": (5, 1), "2026-09-11": (7, 0)})
        dados = torre_controle._coletar_tendencia(vuupt, date(2026, 9, 14), dias=2)  # seg -> sex anterior
        self.assertEqual([d["data"] for d in dados], ["2026-09-11", "2026-09-14"])
        self.assertEqual(dados[0], {"rotulo": "11/09", "data": "2026-09-11", "total": 7, "sucesso": 7, "falha": 0})

    def test_falha_da_api_zera_o_dia_sem_derrubar_o_resto(self):
        vuupt = VuuptFalso({"2026-09-15": (4, 1), "2026-09-16": (9, 3)}, quebrar_em="2026-09-15")
        dados = torre_controle._coletar_tendencia(vuupt, date(2026, 9, 16), dias=2)
        self.assertEqual(dados[0], {"rotulo": "15/09", "data": "2026-09-15", "total": 0, "sucesso": 0, "falha": 0})
        self.assertEqual(dados[1]["sucesso"], 6)

    def test_falha_nunca_passa_do_total(self):
        vuupt = VuuptFalso({"2026-09-16": (3, 5)})  # dado inconsistente da API
        dados = torre_controle._coletar_tendencia(vuupt, date(2026, 9, 16), dias=1)
        self.assertEqual((dados[0]["sucesso"], dados[0]["falha"]), (0, 3))


if __name__ == "__main__":
    unittest.main()
