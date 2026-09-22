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

from regras.preferencias_motoristas import CatalogoMotoristas, _construir_motorista  # noqa: E402


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


class TestTipoVeiculoPadraoFiorino(unittest.TestCase):
    """TIPO_VEICULO vazio passa a ser FIORINO (Hugo, 22/09) -- 27 dos 30
    motoristas estavam com a celula em branco, e Fiorino e o carro da
    maioria da frota."""

    def _motorista(self, tipo_veiculo):
        return _construir_motorista({
            "AGENT_ID_VUUPT": 1,
            "NOME_MOTORISTA": "Teste",
            "ATIVO": "SIM",
            "TIPO_VEICULO": tipo_veiculo,
        })

    def test_celula_vazia_vira_fiorino(self):
        self.assertEqual(self._motorista(None).tipo_veiculo, "FIORINO")
        self.assertEqual(self._motorista("").tipo_veiculo, "FIORINO")

    def test_fiorino_explicito(self):
        self.assertEqual(self._motorista("FIORINO").tipo_veiculo, "FIORINO")
        self.assertEqual(self._motorista("Fiorino").tipo_veiculo, "FIORINO")
        self.assertEqual(self._motorista("UTILITARIO").tipo_veiculo, "FIORINO")

    def test_valor_nao_reconhecido_cai_em_fiorino(self):
        self.assertEqual(self._motorista("CARROCA").tipo_veiculo, "FIORINO")

    def test_tipos_grandes_preservados(self):
        self.assertEqual(self._motorista("VAN_HR").tipo_veiculo, "VAN_HR")
        self.assertEqual(self._motorista("VUC").tipo_veiculo, "VUC")
        self.assertEqual(self._motorista("3/4").tipo_veiculo, "TRES_QUARTOS")

    def test_tarifa_nao_muda_com_o_default(self):
        # regras/tarifa_motorista.py ja tratava "" e "FIORINO" como a
        # mesma tarifa (R$340/65km). Preencher a planilha nao pode mexer
        # em pagamento -- este teste trava isso.
        from regras import tarifa_motorista
        self.assertEqual(
            tarifa_motorista.calcular_valor_rota(None, 50).valor_total,
            tarifa_motorista.calcular_valor_rota("FIORINO", 50).valor_total,
        )


if __name__ == "__main__":
    unittest.main()
