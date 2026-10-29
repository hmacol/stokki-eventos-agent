# -*- coding: utf-8 -*-
"""
Leitura de BD_MOTORISTAS.xlsx: célula vazia não pode virar o texto "nan"
(achado 14/09 -- e-mail "nan" em 24 de 28 motoristas).
    python -m unittest regras.test_preferencias_motoristas -v
"""
import dataclasses
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from regras.preferencias_motoristas import CatalogoMotoristas, _construir_motorista, agent_ids_sem_app  # noqa: E402


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


class TestCelulaVaziaNaoViraNan(unittest.TestCase):
    """Celula vazia do Excel chega como NaN e str() virava "nan": aviso
    de TIPO_VEICULO 'nan' e de dia 'nan' a cada carga da planilha, e uma
    zona fantasma "NAN" (Hugo, 23/09)."""

    def test_nan_nao_gera_aviso_nem_zona(self):
        with self.assertNoLogs("regras.preferencias_motoristas", level="WARNING"):
            m = _construir_motorista({
                "AGENT_ID_VUUPT": 1, "NOME_MOTORISTA": "Virtual", "ATIVO": "SIM",
                "TIPO_VEICULO": float("nan"), "DIAS_DISPONIVEIS": float("nan"),
                "ZONAS_PREFERIDAS": float("nan"),
            })
        self.assertEqual(m.tipo_veiculo, "FIORINO")
        self.assertEqual(m.dias_disponiveis, [])
        self.assertEqual(m.zonas_preferidas, [])


class TestSemApp(unittest.TestCase):
    def _planilha(self, tmp, com_coluna=True):
        caminho = Path(tmp) / "m.xlsx"
        linhas = [{"AGENT_ID_VUUPT": 1, "NOME_MOTORISTA": "Iphone", "ATIVO": "SIM"},
                  {"AGENT_ID_VUUPT": 2, "NOME_MOTORISTA": "Android", "ATIVO": "SIM"}]
        if com_coluna:
            linhas[0]["SEM_APP"] = "SIM"
            linhas[1]["SEM_APP"] = None
        pd.DataFrame(linhas).to_excel(caminho, index=False)
        return caminho

    def test_coluna_sim_vira_sem_app(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            caminho = self._planilha(tmp)
            por_id = {m.agent_id: m for m in CatalogoMotoristas.carregar(str(caminho), "").motoristas}
            self.assertTrue(por_id[1].sem_app)
            self.assertFalse(por_id[2].sem_app)
            self.assertEqual(agent_ids_sem_app(caminho), {1})

    def test_sem_coluna_ou_sem_planilha_e_vazio(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            self.assertEqual(agent_ids_sem_app(self._planilha(tmp, com_coluna=False)), set())
            self.assertEqual(agent_ids_sem_app(Path(tmp) / "nao_existe.xlsx"), set())


class TestCatalogoDoBanco(unittest.TestCase):
    """CatalogoMotoristas.carregar le da tabela bd_motoristas quando o caminho
    e o do espelho padrao e a tabela tem linhas; senao, comportamento antigo."""

    def setUp(self):
        from nucleo import banco
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self._tmp.name)
        p = mock.patch.object(banco, "DB_PATH", self.dir / "t.db")
        p.start()
        self.addCleanup(p.stop)
        self.conn = banco.conectar()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.conn.close)
        self.planilha = self.dir / "fixture.xlsx"
        pd.DataFrame([
            {"AGENT_ID_VUUPT": 7, "VEHICLE_ID_VUUPT": 3, "NOME_MOTORISTA": "Ana", "ACEITA_VIAGENS": "SIM",
             "DIAS_DISPONIVEIS": "SEGUNDA,TERCA", "MAX_ROTAS_DIA": 2, "ATIVO": "SIM", "ZONAS_PREFERIDAS": "ZONA SUL,ABCD",
             "TELEFONE_MOTORISTA": 11999998888, "PLACA": "abc-1d23", "CPF_MOTORISTA": "09474142742",
             "TIPO_VEICULO": "VUC", "APENAS_CARGA_SECA": "SIM", "SEM_APP": "SIM"},
            {"AGENT_ID_VUUPT": 8, "NOME_MOTORISTA": "Beto", "ATIVO": "NAO"},
        ]).to_excel(self.planilha, index=False)

    def _espelho(self):
        from regras import motoristas_banco
        return motoristas_banco.caminho_espelho()

    def test_carregar_do_banco_igual_planilha(self):
        from regras import motoristas_banco
        motoristas_banco.importar_planilha(self.conn, self.planilha)
        da_planilha = CatalogoMotoristas.carregar(self.planilha)
        do_banco = CatalogoMotoristas.carregar(self._espelho(), conn=self.conn)
        self.assertEqual(da_planilha.fonte, "planilha")
        self.assertEqual(do_banco.fonte, "banco")
        self.assertEqual([dataclasses.asdict(m) for m in do_banco.motoristas],
                         [dataclasses.asdict(m) for m in da_planilha.motoristas])
        self.assertEqual(len(do_banco.motoristas), 2)

    def test_tabela_vazia_le_planilha(self):
        shutil.copy(self.planilha, self._espelho())
        cat = CatalogoMotoristas.carregar(self._espelho(), conn=self.conn)
        self.assertEqual(cat.fonte, "planilha")
        self.assertEqual(len(cat.motoristas), 2)

    def test_caminho_de_teste_nao_vai_pro_banco(self):
        from regras import motoristas_banco
        motoristas_banco.importar_planilha(self.conn, self.planilha)
        cat = CatalogoMotoristas.carregar(self.planilha, conn=self.conn)
        self.assertEqual(cat.fonte, "planilha")

    def test_erro_no_banco_cai_na_planilha(self):
        shutil.copy(self.planilha, self._espelho())
        with mock.patch("nucleo.banco.conectar", side_effect=RuntimeError("banco fora")):
            cat = CatalogoMotoristas.carregar(self._espelho())
        self.assertEqual(cat.fonte, "planilha")
        self.assertEqual(len(cat.motoristas), 2)

    def test_agent_ids_sem_app_le_do_banco(self):
        from regras import motoristas_banco
        motoristas_banco.importar_planilha(self.conn, self.planilha)
        self.assertEqual(agent_ids_sem_app(self._espelho()), {7})
        self.conn.execute("UPDATE bd_motoristas SET SEM_APP = 'SIM', atualizado_em = '2999-01-01' WHERE AGENT_ID_VUUPT = 8")
        self.conn.commit()
        self.assertEqual(agent_ids_sem_app(self._espelho()), {7, 8})


if __name__ == "__main__":
    unittest.main()
