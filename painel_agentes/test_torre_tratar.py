# -*- coding: utf-8 -*-
"""
test_torre_tratar.py

Testes da Fila de acao da Torre (Hugo, 16/09):
  - duplicar um pedido pela Torre marca a excecao como tratada (o item
    sai da fila na hora, sem precisar clicar "Tratar" depois);
  - a lista de respostas prontas do botao "Tratar" vem do backend e
    termina em "Outro" (campo livre).

Nenhum teste toca na VUUPT nem no dados.db real: o banco vai pra uma
pasta temporaria e o cliente da VUUPT e um dublê.
Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_torre_tratar -v
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import torre_controle  # noqa: E402


class _VuuptFalso:
    def __init__(self, token):
        self.token = token

    def buscar_servico_por_code(self, codigo):
        return {"id": 555, "code": codigo, "title": "Cliente Exemplo"}


def _fingerprint_falso():
    modulo = types.ModuleType("fingerprint_duplicacao_insucesso")
    modulo.marcados = []
    modulo.ja_duplicado = lambda sid: False
    modulo.buscar_novo_code = lambda sid: None
    modulo.marcar_duplicado = lambda sid, novo: modulo.marcados.append((sid, novo))
    return modulo


class TestDuplicarMarcaTratado(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(torre_controle, "_RAIZ", Path(self._tmp.name)),
            mock.patch.object(torre_controle, "VuuptClient", _VuuptFalso),
            mock.patch.object(torre_controle, "_carregar_config", lambda: {"vuupt_api": {"token": "x"}}),
            mock.patch.object(torre_controle, "tratativas"),
        ]
        for p in self._patches:
            p.start()
        self._fingerprint = _fingerprint_falso()
        sys.modules["fingerprint_duplicacao_insucesso"] = self._fingerprint

    def tearDown(self):
        for p in self._patches:
            p.stop()
        sys.modules.pop("fingerprint_duplicacao_insucesso", None)
        self._tmp.cleanup()

    def _expedir(self, novo):
        modulo = types.SimpleNamespace(duplicar_servico_por_insucesso=lambda vuupt, servico: novo)
        return mock.patch.object(torre_controle, "_expedir_pedidos_raiz", lambda: modulo)

    def test_duplicacao_ok_marca_excecao_como_tratada(self):
        with self._expedir({"id": 999, "code": "#PS-1-R2"}):
            resultado = torre_controle.duplicar_pedido_manual(
                555, "#PS-1", motorista="Joao", rota="Planejamento - 16/09/2026 - #1")

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["novo_code"], "#PS-1-R2")
        tratadas = torre_controle._buscar_tratadas(["insucesso:#PS-1"])
        self.assertIn("insucesso:#PS-1", tratadas)
        registro = tratadas["insucesso:#PS-1"]
        self.assertEqual(registro["tipo"], "Insucesso")
        self.assertIn("#PS-1-R2", registro["motivo"])
        self.assertIn("Cliente Exemplo", registro["descricao"])

    def test_duplicacao_ok_nao_registra_evento_tratada_duplicado(self):
        # REENVIO_MANUAL ja conta a historia no log de tratativas; nao
        # precisa de um EXCECAO_TRATADA em cima com o mesmo texto.
        with self._expedir({"id": 999, "code": "#PS-1-R2"}):
            torre_controle.duplicar_pedido_manual(555, "#PS-1")
        tipos = [c.args[2] for c in torre_controle.tratativas.registrar_evento.call_args_list]
        self.assertEqual(tipos, ["REENVIO_MANUAL"])

    def test_falha_na_vuupt_nao_marca_tratada(self):
        with self._expedir(None):
            resultado = torre_controle.duplicar_pedido_manual(555, "#PS-1")
        self.assertFalse(resultado["ok"])
        self.assertEqual(torre_controle._buscar_tratadas(["insucesso:#PS-1"]), {})

    def test_ja_duplicado_antes_nao_marca_tratada(self):
        self._fingerprint.ja_duplicado = lambda sid: True
        self._fingerprint.buscar_novo_code = lambda sid: "#PS-1-R2"
        with self._expedir({"id": 999, "code": "#PS-1-R3"}):
            resultado = torre_controle.duplicar_pedido_manual(555, "#PS-1")
        self.assertFalse(resultado["ok"])
        self.assertEqual(torre_controle._buscar_tratadas(["insucesso:#PS-1"]), {})


class TestRespostasTratativa(unittest.TestCase):

    def test_lista_vem_do_backend_e_termina_em_outro(self):
        respostas = torre_controle.RESPOSTAS_TRATATIVA
        self.assertGreaterEqual(len(respostas), 5)
        self.assertEqual(respostas[-1], "Outro")
        self.assertEqual(len(respostas), len(set(respostas)))


class TestVersaoTela(unittest.TestCase):
    """Aba da torre aberta antes de um deploy ficava com o JS antigo pra
    sempre (17/09: backend novo gravando 'Duplicado -> ...' e a aba velha
    ainda no prompt() de texto livre). A tela compara a versao que veio
    no payload com a primeira que viu e se recarrega quando muda."""

    def test_versao_muda_quando_um_arquivo_da_tela_muda(self):
        import os
        with tempfile.TemporaryDirectory() as pasta:
            a, b = Path(pasta) / "a.html", Path(pasta) / "b.py"
            a.write_text("x"); b.write_text("y")
            os.utime(a, (1000, 1000)); os.utime(b, (2000, 2000))
            antes = torre_controle._calcular_versao_tela([a, b])
            self.assertEqual(antes, torre_controle._calcular_versao_tela([a, b]))
            os.utime(a, (3000, 3000))
            self.assertNotEqual(antes, torre_controle._calcular_versao_tela([a, b]))

    def test_arquivo_ausente_nao_quebra(self):
        with tempfile.TemporaryDirectory() as pasta:
            versao = torre_controle._calcular_versao_tela([Path(pasta) / "nao_existe.html"])
        self.assertIsInstance(versao, str)
        self.assertTrue(versao)

    def test_versao_do_processo_esta_definida(self):
        self.assertIsInstance(torre_controle.VERSAO_TELA, str)
        self.assertTrue(torre_controle.VERSAO_TELA)


if __name__ == "__main__":
    unittest.main()
