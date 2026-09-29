# -*- coding: utf-8 -*-
"""
Testes do atualizar_agendamentos_confirmados.py.

Regra do Hugo (28/09, caso PS-40316): data de agendamento informada
(portal, planilha, resposta de e-mail) vale SEMPRE -- o dia fixo da
regiao nao empurra mais essa data.

Rodar: py -3.11 -m unittest test_atualizar_agendamentos_confirmados
"""
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import atualizar_agendamentos_confirmados as aac

QUINTA = 3


def _proxima_quinta() -> date:
    hoje = date.today()
    return hoje + timedelta(days=((QUINTA - hoje.weekday()) % 7) or 7)


class TestAtualizarAgendamentosConfirmados(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "dados.db"
        conn = sqlite3.connect(self.db)
        conn.execute("""
            CREATE TABLE agendamentos_pedido (
                pedido TEXT PRIMARY KEY, status TEXT, data_agendada TEXT,
                horario_inicio_agendado TEXT, horario_fim_agendado TEXT,
                aplicado_vuupt_em TEXT)
        """)
        conn.commit()
        conn.close()

        self.vuupt = MagicMock()
        for p in (
            patch.object(aac, "DB_PATH", self.db),
            patch.object(aac, "_carregar_config", return_value={}),
            patch.object(aac, "VuuptClient", return_value=self.vuupt),
            patch.object(aac, "notificar_execucao"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def _gravar(self, pedido: str, data: date, inicio="06:00", fim="11:00"):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO agendamentos_pedido VALUES (?, 'RESPONDIDO', ?, ?, ?, NULL)",
                     (pedido, data.strftime("%d/%m/%Y"), inicio, fim))
        conn.commit()
        conn.close()

    def _aplicado_em(self, pedido: str):
        conn = sqlite3.connect(self.db)
        valor = conn.execute("SELECT aplicado_vuupt_em FROM agendamentos_pedido WHERE pedido = ?",
                             (pedido,)).fetchone()[0]
        conn.close()
        return valor

    def test_data_informada_vence_o_dia_fixo(self):
        # Santo Andre (ABCD) so recebe seg/qua/sex; o cliente agendou quinta.
        quinta = _proxima_quinta()
        self._gravar("PS-40316", quinta)
        self.vuupt.buscar_servico_por_code.return_value = {
            "id": 1, "status": "not_assigned",
            "address": "RUA DAS FIGUEIRAS 735, CAMPESTRE, Santo Andre - SP, 09080-370, Brasil",
        }

        aac.main()

        self.vuupt.atualizar_servico.assert_called_once_with(1, {
            "scheduled_start": f"{quinta.isoformat()}T06:00:00-03:00",
            "scheduled_end": f"{quinta.isoformat()}T11:00:00-03:00",
        })
        self.assertIsNotNone(self._aplicado_em("PS-40316"))

    def test_servico_que_ja_saiu_do_pool_nao_e_alterado(self):
        self._gravar("PS-1", _proxima_quinta())
        self.vuupt.buscar_servico_por_code.return_value = {
            "id": 2, "status": "done", "address": "Rua X, 1, Sao Paulo - SP, 01000-000, Brasil"}

        aac.main()

        self.vuupt.atualizar_servico.assert_not_called()
        self.assertIsNone(self._aplicado_em("PS-1"))

    def test_data_ja_passada_nem_consulta_a_vuupt(self):
        self._gravar("PS-2", date.today() - timedelta(days=3))

        aac.main()

        self.vuupt.buscar_servico_por_code.assert_not_called()
        self.vuupt.atualizar_servico.assert_not_called()


if __name__ == "__main__":
    unittest.main()
