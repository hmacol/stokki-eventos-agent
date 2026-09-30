# -*- coding: utf-8 -*-
"""
Testes de montar_args_parametros (agentes.py): campos preenchidos pelo
operador na hora de rodar um agente viram argv do script. Caso de uso:
data dos romaneios (Hugo, 16/09).

    py -3.11 -m unittest painel_agentes.test_parametros_agente
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agentes import buscar_agente, montar_args_parametros  # noqa: E402


class TestMontarArgsParametros(unittest.TestCase):

    def setUp(self):
        self.agente = buscar_agente("gerar_romaneios")

    def test_gerar_romaneios_declara_campo_data(self):
        campos = [p["campo"] for p in self.agente["parametros"]]
        self.assertEqual(campos, ["data"])
        self.assertEqual(self.agente["parametros"][0]["flag"], "--data")

    def test_data_preenchida_vira_flag_data(self):
        args = montar_args_parametros(self.agente, {"data": "2026-09-17"})
        self.assertEqual(args, ["--data", "2026-09-17"])

    def test_data_em_branco_nao_passa_flag(self):
        self.assertEqual(montar_args_parametros(self.agente, {"data": ""}), [])
        self.assertEqual(montar_args_parametros(self.agente, {"data": "   "}), [])
        self.assertEqual(montar_args_parametros(self.agente, {}), [])

    def test_data_invalida_levanta_valueerror_com_mensagem(self):
        with self.assertRaises(ValueError) as ctx:
            montar_args_parametros(self.agente, {"data": "17/09/2026"})
        self.assertIn("Data das rotas", str(ctx.exception))
        with self.assertRaises(ValueError):
            montar_args_parametros(self.agente, {"data": "2026-13-40"})

    def test_agente_sem_parametros_ignora_campos(self):
        agente = buscar_agente("somente_importacao")
        self.assertEqual(montar_args_parametros(agente, {"data": "2026-09-17"}), [])

    def test_modo_teste_do_form_nao_e_confundido_com_parametro(self):
        args = montar_args_parametros(self.agente, {"data": "2026-09-17", "modo_teste": "on"})
        self.assertEqual(args, ["--data", "2026-09-17"])


if __name__ == "__main__":
    unittest.main()
