# -*- coding: utf-8 -*-
"""
test_janela_horario_email.py

Lado dos E-MAILS da janela de horário de entrega (Hugo, 09/09): a hora
que o cliente informa precisa chegar inteira ao banco -- e a hora que
ele NÃO informa não pode virar chute gravado como se fosse janela.

Cobre: extração de hora da coluna AGENDA da planilha NUU
(ler_planilha_entregas_nuu.extrair_hora_agenda), normalização do que a
IA devolve na resposta de agendamento (ler_respostas_agendamento.
_janela_informada), marcadores de pedido (um e vários), janela das
mensagens da Stokki (regras.endereco._normalizar_horario_entrega) e o
e-mail urgente carregando os marcadores. Nada bate em rede/banco.
Rodar (da raiz):
    python -m unittest test_janela_horario_email -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "roteirizacao"))

from ler_planilha_entregas_nuu import extrair_hora_agenda
import ler_respostas_agendamento as lr
from regras.endereco import _normalizar_horario_entrega
import notificar_agendamento_pendente as nap


class HoraPlanilhaNuuTestCase(unittest.TestCase):

    def test_sem_hora_fica_none(self):
        for texto in ("AGENDADO 13/08", "agendado 13.08", "2026-08-12 00:00:00", "X", "", None, "13/08 - 14/08"):
            self.assertIsNone(extrair_hora_agenda(texto), texto)

    def test_hora_unica_vira_janela_de_1h(self):
        self.assertEqual(extrair_hora_agenda("AGENDADO 13/08 às 14h"), ("14:00", "15:00"))
        self.assertEqual(extrair_hora_agenda("13/08 14:30"), ("14:30", "15:30"))
        self.assertEqual(extrair_hora_agenda("13/08 14h30"), ("14:30", "15:30"))
        self.assertEqual(extrair_hora_agenda("2026-08-12 14:00:00"), ("14:00", "15:00"))

    def test_intervalos_e_limites(self):
        self.assertEqual(extrair_hora_agenda("entre 8h e 12h dia 13/08"), ("08:00", "12:00"))
        self.assertEqual(extrair_hora_agenda("13/08 das 8 as 12h"), ("08:00", "12:00"))
        self.assertEqual(extrair_hora_agenda("agendado 13.08 até 11h"), ("00:00", "11:00"))
        self.assertEqual(extrair_hora_agenda("após 14h 13/08"), ("14:00", "23:59"))
        self.assertEqual(extrair_hora_agenda("manhã 13/08"), ("08:00", "12:00"))
        self.assertEqual(extrair_hora_agenda("13/08 tarde"), ("13:00", "18:00"))


class RespostaAgendamentoTestCase(unittest.TestCase):

    def test_hora_nao_informada_fica_null(self):
        self.assertEqual(lr._janela_informada({"data": "10/09/2026", "inicio": "", "fim": ""}), (None, None))
        self.assertEqual(lr._janela_informada({"data": "10/09/2026"}), (None, None))

    def test_hora_informada_normalizada(self):
        self.assertEqual(lr._janela_informada({"inicio": "8:00", "fim": "12:00"}), ("08:00", "12:00"))
        self.assertEqual(lr._janela_informada({"inicio": "14h", "fim": ""}), ("14:00", "15:00"))
        self.assertEqual(lr._janela_informada({"inicio": "", "fim": "11:00"}), ("00:00", "11:00"))
        self.assertEqual(lr._janela_informada({"inicio": "14:00", "fim": "14:00"}), ("14:00", "15:00"))
        self.assertEqual(lr._janela_informada({"inicio": "abc", "fim": "25:00"}), (None, None))

    def test_marcadores_um_e_varios(self):
        self.assertEqual(lr._extrair_pedidos_do_corpo("oi [[PEDIDO:PS-1]] tchau"), ["PS-1"])
        self.assertEqual(lr._extrair_pedidos_do_corpo("[[PEDIDO:PS-1]][[PEDIDO:#PS-2]][[PEDIDO:PS-1]]"), ["PS-1", "#PS-2"])
        self.assertEqual(lr._extrair_pedidos_do_corpo("sem marcador"), [])


class MensagensStokkiTestCase(unittest.TestCase):

    def test_normaliza_formatos_do_llm(self):
        self.assertEqual(_normalizar_horario_entrega({"inicio": "8h", "fim": "12:00h", "observacao": "manhã"}),
                         {"inicio": "08:00", "fim": "12:00", "observacao": "manhã"})
        self.assertEqual(_normalizar_horario_entrega({"inicio": "", "fim": "11h"}),
                         {"inicio": "00:00", "fim": "11:00", "observacao": ""})

    def test_janela_invalida_vira_none(self):
        self.assertIsNone(_normalizar_horario_entrega(None))
        self.assertIsNone(_normalizar_horario_entrega({"inicio": "15:00", "fim": "12:00"}))
        self.assertIsNone(_normalizar_horario_entrega({"inicio": "manhã", "fim": ""}))


class EmailUrgenteTestCase(unittest.TestCase):

    def test_conteudo_leva_marcadores_de_todos_os_pedidos(self):
        pedidos = [{"code": "PS-10", "title": "A"}, {"code": "#PS-11", "title": "B"}]
        html = nap._montar_conteudo("NUU", pedidos, "12345678000199")
        self.assertIn("[[AGENTE_AGENDAMENTO_EMBARCADOR:12345678000199]]", html)
        self.assertIn("[[PEDIDO:PS-10]]", html)
        self.assertIn("[[PEDIDO:PS-11]]", html)
        self.assertIn("responda este e-mail", html)
        # o leitor reconhece os dois marcadores de volta
        self.assertEqual(lr._extrair_pedidos_do_corpo(html), ["PS-10", "PS-11"])


if __name__ == "__main__":
    unittest.main()
