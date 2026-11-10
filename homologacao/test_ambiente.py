import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from homologacao import ambiente


class TestAmbiente(unittest.TestCase):
    def setUp(self):
        ambiente._cache_cnpjs = None

    def tearDown(self):
        ambiente._cache_cnpjs = None

    def test_inativo_sem_variavel(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ambiente.VARIAVEL, None)
            self.assertIs(ambiente.homolog_ativo(), False)

    def test_coerencia_aborta_homolog_na_raiz_de_producao(self):
        guarda = mock.Mock(INSTALADA=True)
        with mock.patch.dict(os.environ, {ambiente.VARIAVEL: "homolog"}), \
                mock.patch.dict(sys.modules, {"homologacao.guarda": guarda}), \
                mock.patch.object(Path, "resolve", return_value=ambiente.RAIZ_PRODUCAO):
            with self.assertRaises(SystemExit):
                ambiente.exigir_coerencia(Path("qualquer"))

    def test_coerencia_aborta_sem_guarda(self):
        with mock.patch.dict(os.environ, {ambiente.VARIAVEL: "homolog"}):
            sys.modules.pop("homologacao.guarda", None)
            with mock.patch.dict(sys.modules, {}):
                sys.modules.pop("homologacao.guarda", None)
                with self.assertRaises(SystemExit):
                    ambiente.exigir_coerencia(Path("/tmp/outra"))
        guarda = mock.Mock(INSTALADA=False)
        with mock.patch.dict(os.environ, {ambiente.VARIAVEL: "homolog"}), \
                mock.patch.dict(sys.modules, {"homologacao.guarda": guarda}):
            with self.assertRaises(SystemExit):
                ambiente.exigir_coerencia(Path("/tmp/outra"))

    def test_coerencia_aborta_raiz_homolog_sem_variavel(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(ambiente.VARIAVEL, None)
            with mock.patch.object(Path, "resolve", return_value=ambiente.RAIZ_HOMOLOG):
                with self.assertRaises(SystemExit):
                    ambiente.exigir_coerencia(Path("qualquer"))

    def test_coerencia_ok_em_producao_sem_variavel(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(ambiente.VARIAVEL, None)
            ambiente.exigir_coerencia(Path("/tmp/qualquer"))

    def test_recusar_levanta_so_quando_ativo(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(ambiente.VARIAVEL, None)
            ambiente.recusar_na_homologacao("Agentes")
        with mock.patch.dict(os.environ, {ambiente.VARIAVEL: "homolog"}):
            with self.assertRaises(ambiente.DesligadoNaHomologacao) as ctx:
                ambiente.recusar_na_homologacao("Agentes")
            self.assertEqual(str(ctx.exception), "Agentes desligado na homologação")

    def test_cnpjs_padrao_e_cliente_teste(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "cfg.yaml"
            cfg.write_text("outra_secao:\n  x: 1\n", encoding="utf-8")
            with mock.patch.object(ambiente, "CAMINHO_CONFIG", cfg):
                self.assertEqual(ambiente.cnpjs_portal_permitidos(), {"00000000000191"})

    def test_cnpjs_da_config_normaliza_digitos(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "cfg.yaml"
            cfg.write_text(
                "homologacao:\n  portal_cnpjs_permitidos:\n    - '12.345.678/0001-95'\n",
                encoding="utf-8")
            with mock.patch.object(ambiente, "CAMINHO_CONFIG", cfg):
                self.assertEqual(ambiente.cnpjs_portal_permitidos(), {"12345678000195"})

    def test_informacao_versao_sem_arquivos_devolve_interrogacao(self):
        with tempfile.TemporaryDirectory() as d:
            info = ambiente.informacao_versao(Path(d))
        self.assertEqual(info, {"ramo": "?", "commit": "?", "master": "?",
                                "quando": "?", "copia": "?"})

    def test_informacao_versao_le_arquivos(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "dados").mkdir()
            (Path(d) / "dados" / "HOMOLOG_VERSAO").write_text(
                '{"ramo": "r", "commit": "c"}', encoding="utf-8")
            (Path(d) / "dados" / "HOMOLOG_COPIA").write_text("10/11\n", encoding="utf-8")
            info = ambiente.informacao_versao(Path(d))
        self.assertEqual(info["ramo"], "r")
        self.assertEqual(info["master"], "?")
        self.assertEqual(info["copia"], "10/11")


if __name__ == "__main__":
    unittest.main()
