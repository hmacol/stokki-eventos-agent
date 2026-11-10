"""Testes do gerar_config: so dicionarios inventados, nunca o config real."""
import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path

import yaml

from homologacao import gerar_config


def _gerar(prod):
    return gerar_config.gerar(
        prod,
        url_base_painel="https://homolog.freshhub.com.br/painel",
        url_base_portal="https://homolog.freshhub.com.br/cliente",
        caminho_gcs="/opt/stokki-homolog/conta_gcs.json",
    )


class TestGerar(unittest.TestCase):
    def test_remove_segredos_em_qualquer_nivel(self):
        prod = {"email": {"senha_app": "x", "remetente": "a@b"},
                "lalamove": {"api_secret": "y", "ativo": True}}
        cfg, removidas, _ = _gerar(prod)
        self.assertNotIn("senha_app", cfg["email"])
        self.assertNotIn("api_secret", cfg["lalamove"])
        self.assertEqual(cfg["email"]["remetente"], "a@b")
        self.assertIs(cfg["lalamove"]["ativo"], True)
        self.assertIn("email.senha_app", removidas)
        self.assertIn("lalamove.api_secret", removidas)

    def test_remove_segredo_dentro_de_lista(self):
        prod = {"contas": [{"login": "a", "password": "p"}]}
        cfg, removidas, _ = _gerar(prod)
        self.assertEqual(cfg["contas"], [{"login": "a"}])
        self.assertIn("contas[0].password", removidas)

    def test_nao_altera_o_dicionario_de_producao(self):
        prod = {"email": {"senha_app": "x"}}
        _gerar(prod)
        self.assertEqual(prod, {"email": {"senha_app": "x"}})

    def test_reinsere_so_os_liberados(self):
        prod = {"vuupt_api": {"token": "tv", "base": "u"},
                "anthropic": {"api_key": "ta"},
                "stokki": {"senha": "ts", "usuario": "u"},
                "gcs": {"credenciais_json": "/opt/stokki-eventos/sa.json", "bucket_name": "b"}}
        cfg, removidas, mantidas = _gerar(prod)
        self.assertEqual(cfg["vuupt_api"]["token"], "tv")
        self.assertEqual(cfg["anthropic"]["api_key"], "ta")
        self.assertNotIn("senha", cfg["stokki"])
        self.assertEqual(cfg["gcs"]["credenciais_json"], "/opt/stokki-homolog/conta_gcs.json")
        self.assertEqual(cfg["gcs"]["bucket_name"], "b")
        self.assertIn("stokki.senha", removidas)
        self.assertEqual(sorted(mantidas),
                         ["anthropic.api_key", "gcs.credenciais_json", "vuupt_api.token"])
        self.assertNotIn("vuupt_api.token", removidas)

    def test_liberado_ausente_na_producao_nao_e_criado(self):
        cfg, _, mantidas = _gerar({"vuupt_api": {"base": "u"}})
        self.assertNotIn("token", cfg["vuupt_api"])
        self.assertNotIn("google_maps", cfg)
        self.assertEqual(mantidas, [])

    def test_secret_keys_novos_e_diferentes_da_producao(self):
        prod = {"painel_agentes": {"secret_key": "PROD-PAINEL"},
                "portal_cliente": {"secret_key": "PROD-PORTAL"}}
        cfg, removidas, _ = _gerar(prod)
        painel = cfg["painel_agentes"]["secret_key"]
        portal = cfg["portal_cliente"]["secret_key"]
        self.assertNotEqual(painel, "PROD-PAINEL")
        self.assertNotEqual(portal, "PROD-PORTAL")
        self.assertNotEqual(painel, portal)
        self.assertEqual(len(painel), 64)
        int(painel, 16)
        self.assertIn("painel_agentes.secret_key", removidas)

    def test_openwa_ganha_chave_falsa(self):
        prod = {"whatsapp_notificacoes": {"base_url": "http://127.0.0.1:2785",
                                          "api_key": "real", "sessao": "s"}}
        cfg, _, _ = _gerar(prod)
        self.assertEqual(cfg["whatsapp_notificacoes"]["api_key"], "HOMOLOG-SEM-CHAVE")
        self.assertEqual(cfg["whatsapp_notificacoes"]["sessao"], "s")

    def test_sem_secao_openwa_nao_cria(self):
        cfg, _, _ = _gerar({"email": {}})
        self.assertNotIn("whatsapp_notificacoes", cfg)

    def test_url_base_vira_homolog(self):
        prod = {"portal_cliente": {"url_base": "https://app.freshhub.com.br/cliente"},
                "outro": {"fundo": {"url_base": "https://app.freshhub.com.br/insucesso/x"}},
                "externo": {"url_base": "https://confirmacao.freshhub.com.br/"}}
        cfg, _, _ = _gerar(prod)
        self.assertEqual(cfg["portal_cliente"]["url_base"], "https://homolog.freshhub.com.br/cliente")
        self.assertEqual(cfg["outro"]["fundo"]["url_base"],
                         "https://homolog.freshhub.com.br/insucesso/x")
        self.assertEqual(cfg["externo"]["url_base"], "https://confirmacao.freshhub.com.br/")

    def test_secao_homologacao_com_cliente_teste(self):
        cfg, _, _ = _gerar({})
        self.assertEqual(cfg["homologacao"], {"portal_cnpjs_permitidos": ["00000000000191"]})


class TestCli(unittest.TestCase):
    def _rodar(self, prod):
        with tempfile.TemporaryDirectory() as tmp:
            origem = Path(tmp) / "origem.yaml"
            destino = Path(tmp) / "destino.yaml"
            origem.write_text(yaml.safe_dump(prod), encoding="utf-8")
            saida = io.StringIO()
            with contextlib.redirect_stdout(saida), contextlib.redirect_stderr(saida):
                codigo = gerar_config.main(["--origem", str(origem), "--destino", str(destino),
                                            "--caminho-gcs", "/opt/stokki-homolog/conta_gcs.json"])
            gerado = yaml.safe_load(destino.read_text(encoding="utf-8"))
            modo = stat.S_IMODE(os.stat(destino).st_mode)
        return codigo, saida.getvalue(), gerado, modo

    def test_cli_nao_imprime_valores(self):
        prod = {"email": {"senha_app": "VALOR-SECRETO-123"},
                "vuupt_api": {"token": "VALOR-SECRETO-123"},
                "painel_agentes": {"secret_key": "VALOR-SECRETO-123"}}
        codigo, saida, gerado, _ = self._rodar(prod)
        self.assertEqual(codigo, 0)
        self.assertNotIn("VALOR-SECRETO-123", saida)
        self.assertNotIn(gerado["painel_agentes"]["secret_key"], saida)
        self.assertIn("removidas:", saida)
        self.assertIn("mantidas:", saida)
        self.assertIn("email.senha_app", saida)
        self.assertIn("vuupt_api.token", saida)
        self.assertEqual(gerado["vuupt_api"]["token"], "VALOR-SECRETO-123")

    def test_cli_yaml_invalido_nao_cita_o_trecho(self):
        with tempfile.TemporaryDirectory() as tmp:
            origem = Path(tmp) / "origem.yaml"
            origem.write_text("email:\n  senha_app: VALOR-SECRETO-123\n   : [\n", encoding="utf-8")
            saida = io.StringIO()
            with contextlib.redirect_stdout(saida), contextlib.redirect_stderr(saida):
                codigo = gerar_config.main(["--origem", str(origem),
                                            "--destino", str(Path(tmp) / "d.yaml"),
                                            "--caminho-gcs", "x"])
        self.assertEqual(codigo, 1)
        self.assertNotIn("VALOR-SECRETO-123", saida.getvalue())

    @unittest.skipIf(os.name == "nt", "permissao 0600 so vale fora do Windows")
    def test_cli_grava_0600(self):
        _, _, _, modo = self._rodar({"email": {"senha_app": "x"}})
        self.assertEqual(modo, 0o600)


if __name__ == "__main__":
    unittest.main()
