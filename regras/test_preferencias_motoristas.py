# -*- coding: utf-8 -*-
"""
Leitura de BD_MOTORISTAS.xlsx: célula vazia não pode virar o texto "nan"
(achado 14/09 -- e-mail "nan" em 24 de 28 motoristas).
    python -m unittest regras.test_preferencias_motoristas -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from regras.preferencias_motoristas import CatalogoMotoristas  # noqa: E402


class TestLeituraPlanilha(unittest.TestCase):
    def test_celula_vazia_vira_none_e_numero_perde_ponto_zero(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            caminho = Path(tmp) / "m.xlsx"
            pd.DataFrame([
                {"AGENT_ID_VUUPT": 1, "NOME_MOTORISTA": "Com Dados", "ATIVO": "SIM", "DIAS_DISPONIVEIS": "SEGUNDA",
                 "ZONAS_PREFERIDAS": "ZONA SUL", "EMAIL_MOTORISTA": " a@b.com ", "TELEFONE_MOTORISTA": 5511999998888},
                {"AGENT_ID_VUUPT": 2, "NOME_MOTORISTA": None, "ATIVO": "SIM", "DIAS_DISPONIVEIS": "SEGUNDA",
                 "ZONAS_PREFERIDAS": "ZONA SUL", "EMAIL_MOTORISTA": None, "TELEFONE_MOTORISTA": None},
            ]).to_excel(caminho, index=False)
            por_id = {m.agent_id: m for m in CatalogoMotoristas.carregar(str(caminho), "").motoristas}
        self.assertEqual((por_id[1].email, por_id[1].telefone), ("a@b.com", "5511999998888"))
        self.assertIsNone(por_id[2].email)
        self.assertIsNone(por_id[2].telefone)
        self.assertEqual(por_id[2].nome, "Motorista 2")


if __name__ == "__main__":
    unittest.main()
