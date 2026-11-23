"""wms_pilotos: lista de pilotos do config e fallback das chaves antigas."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import wms_pilotos  # noqa: E402


class TestPilotos(unittest.TestCase):
    def test_lista_do_config(self):
        cfg = {"wms": {"pilotos": [{"id": 48, "nome": "Maria Dolores", "caixas": False},
                                   {"id": "22", "nome": "mombak", "caixas": True}]}}
        self.assertEqual(wms_pilotos.pilotos(cfg), [
            {"id": 48, "nome": "MARIA DOLORES", "caixas": False},
            {"id": 22, "nome": "MOMBAK", "caixas": True}])
        self.assertEqual(wms_pilotos.ids_com_caixas(cfg), {22})

    def test_sem_lista_usa_chaves_antigas(self):
        self.assertEqual(wms_pilotos.pilotos({}), [{"id": 48, "nome": "MARIA DOLORES", "caixas": False}])
        cfg = {"wms": {"embarcador_piloto_id": "99", "embarcador_piloto": "outro", "caixas_ativo": "sim"}}
        self.assertEqual(wms_pilotos.pilotos(cfg), [{"id": 99, "nome": "OUTRO", "caixas": True}])
        self.assertEqual(wms_pilotos.ids_com_caixas(cfg), {99})

    def test_lista_vence_as_chaves_antigas(self):
        cfg = {"wms": {"embarcador_piloto_id": 48, "caixas_ativo": True,
                       "pilotos": [{"id": 22, "nome": "MOMBAK", "caixas": False}]}}
        self.assertEqual(wms_pilotos.ids_com_caixas(cfg), set())

    def test_item_invalido_ou_repetido_e_ignorado(self):
        cfg = {"wms": {"pilotos": [{"nome": "sem id"}, "texto", {"id": "x"}, {"id": 22}, {"id": 22, "caixas": True}]}}
        self.assertEqual(wms_pilotos.pilotos(cfg), [{"id": 22, "nome": "", "caixas": False}])

    def test_config_vazio_ou_nulo(self):
        self.assertEqual(len(wms_pilotos.pilotos(None)), 1)
        self.assertEqual(wms_pilotos.ids_com_caixas({"wms": None}), set())


if __name__ == "__main__":
    unittest.main()
