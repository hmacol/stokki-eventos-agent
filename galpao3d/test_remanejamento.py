# -*- coding: utf-8 -*-
import unittest
from datetime import datetime

from galpao3d.remanejamento import sugerir

DIA = datetime(2026, 10, 22, 10, 0)    # quinta, dentro do turno
NOITE = datetime(2026, 10, 22, 19, 0)  # fora do turno


def _cenario():
    filas = {"separacao": 1, "conferencia": 14, "estoque": None}
    esperas = [{"id_stokki": i, "etapa": "separacao→conferencia", "minutos": 125} for i in range(5)]
    pessoas = {"separacao": 3, "conferencia": 3, "estoque": 1}
    return filas, esperas, pessoas


class TestRemanejamento(unittest.TestCase):
    def test_sugere_mover_para_conferencia(self):
        filas, esperas, pessoas = _cenario()
        r = sugerir(filas, esperas, pessoas, {}, DIA)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["de"], r[0]["para"]), ("separacao", "conferencia"))
        self.assertIn("14", r[0]["motivo"])
        self.assertIn("2h05", r[0]["motivo"])

    def test_sem_sugestao_fora_do_turno(self):
        filas, esperas, pessoas = _cenario()
        self.assertEqual(sugerir(filas, esperas, pessoas, {}, NOITE), [])

    def test_fila_desconhecida_nao_sugere(self):
        filas, esperas, pessoas = _cenario()
        filas["separacao"] = None
        self.assertEqual(sugerir(filas, esperas, pessoas, {}, DIA), [])

    def test_sem_pessoa_na_vizinha(self):
        filas, esperas, pessoas = _cenario()
        pessoas["separacao"] = 0
        self.assertEqual(sugerir(filas, esperas, pessoas, {}, DIA), [])

    def test_cfg_muda_limites(self):
        filas, esperas, pessoas = _cenario()
        self.assertEqual(sugerir(filas, esperas, pessoas, {"fila_min": 20}, DIA), [])


if __name__ == "__main__":
    unittest.main()
