# -*- coding: utf-8 -*-
"""Testes do miolo puro do verificador de botoes (sem navegador).

Rodar: py -3.11 -m unittest verificador_botoes.test_classificador
"""
import unittest

from werkzeug.routing import Map, Rule

from verificador_botoes.classificador import (
    classificar,
    conferir_rota,
    decidir_intercepcao,
    separar_erros,
    telas_do_mapa,
)


def _mapa():
    return Map([
        Rule("/", endpoint="index", methods=["GET"]),
        Rule("/torre", endpoint="torre", methods=["GET"]),
        Rule("/login", endpoint="login", methods=["GET", "POST"]),
        Rule("/logout", endpoint="logout", methods=["POST"]),
        Rule("/static/<path:filename>", endpoint="static", methods=["GET"]),
        Rule("/api/torre/tratar", endpoint="torre_tratar", methods=["POST"]),
        Rule("/api/torre/rotas", endpoint="torre_rotas", methods=["GET"]),
        Rule("/romaneio/<data>/<nome>", endpoint="romaneio", methods=["GET"]),
        Rule("/planejamento", endpoint="planejamento", methods=["GET"]),
    ])


class TestConferirRota(unittest.TestCase):
    def test_rota_e_metodo_existentes(self):
        self.assertEqual(conferir_rota(_mapa(), "/api/torre/tratar", "POST"), "OK")

    def test_rota_com_query_string(self):
        self.assertEqual(conferir_rota(_mapa(), "/api/torre/rotas?data=2026-09-16", "GET"), "OK")

    def test_rota_inexistente(self):
        self.assertEqual(conferir_rota(_mapa(), "/api/torre/apagada", "POST"), "INEXISTENTE")

    def test_metodo_errado(self):
        self.assertEqual(conferir_rota(_mapa(), "/api/torre/tratar", "GET"), "METODO")

    def test_rota_com_parametro(self):
        self.assertEqual(conferir_rota(_mapa(), "/romaneio/2026-09-16/x.pdf", "GET"), "OK")


class TestDecidirIntercepcao(unittest.TestCase):
    """O navegador so deixa passar a carga da pagina (GET de navegacao) e
    arquivos estaticos. Tudo o mais e respondido com resposta falsa, pra
    nada rodar no servidor."""

    def test_navegacao_get_passa(self):
        self.assertEqual(decidir_intercepcao("/torre", "GET", "document", navegacao=True), "PASSAR")

    def test_estatico_passa(self):
        self.assertEqual(decidir_intercepcao("/static/app.js", "GET", "script", navegacao=False), "PASSAR")

    def test_api_get_e_falsificada(self):
        self.assertEqual(decidir_intercepcao("/api/torre/rotas", "GET", "fetch", navegacao=False), "FALSIFICAR")

    def test_post_de_formulario_e_falsificado(self):
        self.assertEqual(decidir_intercepcao("/logout", "POST", "document", navegacao=True), "FALSIFICAR")

    def test_xhr_get_fora_da_api_e_falsificado(self):
        self.assertEqual(decidir_intercepcao("/romaneio/2026-09-16/x.pdf", "GET", "fetch", navegacao=False), "FALSIFICAR")

    def test_api_get_passa_no_modo_leitura(self):
        """Leitura de API local (banco congelado) da dados reais as telas e
        evita erro falso de 'undefined.filter' nos botoes que dependem deles."""
        self.assertEqual(decidir_intercepcao("/api/torre/dados", "GET", "fetch", navegacao=False,
                                             api_leitura=True), "PASSAR")

    def test_api_get_proibida_e_falsificada_mesmo_no_modo_leitura(self):
        self.assertEqual(decidir_intercepcao("/api/torre/stokki?x=1", "GET", "fetch", navegacao=False,
                                             api_leitura=True, proibidas={"/api/torre/stokki"}), "FALSIFICAR")

    def test_post_de_api_nunca_passa(self):
        self.assertEqual(decidir_intercepcao("/api/torre/tratar", "POST", "fetch", navegacao=False,
                                             api_leitura=True), "FALSIFICAR")


class TestTelasDoMapa(unittest.TestCase):
    def test_lista_so_paginas_get_sem_parametro(self):
        telas = telas_do_mapa(_mapa())
        self.assertEqual(telas, [("/", "index"), ("/planejamento", "planejamento"), ("/torre", "torre")])


class TestClassificar(unittest.TestCase):
    def _evento(self, **kw):
        base = {"desabilitado": False, "chamadas": [], "erros_js": [], "navegou": False,
                "mutacoes": 0, "href": None, "onclick": None}
        base.update(kw)
        return base

    def test_desabilitado(self):
        r = classificar(_mapa(), self._evento(desabilitado=True))
        self.assertEqual(r["status"], "DESABILITADO")

    def test_erro_js_ganha_de_tudo(self):
        r = classificar(_mapa(), self._evento(erros_js=["rodarX is not defined"],
                                              chamadas=[{"metodo": "POST", "caminho": "/api/torre/tratar"}]))
        self.assertEqual(r["status"], "ERRO_JS")
        self.assertIn("rodarX", r["detalhe"])

    def test_chamada_para_rota_inexistente(self):
        r = classificar(_mapa(), self._evento(chamadas=[{"metodo": "POST", "caminho": "/api/torre/apagada"}]))
        self.assertEqual(r["status"], "ROTA_INEXISTENTE")
        self.assertIn("/api/torre/apagada", r["detalhe"])

    def test_chamada_com_metodo_errado_tambem_e_rota_inexistente(self):
        r = classificar(_mapa(), self._evento(chamadas=[{"metodo": "GET", "caminho": "/api/torre/tratar"}]))
        self.assertEqual(r["status"], "ROTA_INEXISTENTE")
        self.assertIn("GET", r["detalhe"])

    def test_chamada_boa_e_ok(self):
        r = classificar(_mapa(), self._evento(chamadas=[{"metodo": "POST", "caminho": "/api/torre/tratar"}]))
        self.assertEqual(r["status"], "OK")

    def test_navegou(self):
        r = classificar(_mapa(), self._evento(navegou=True))
        self.assertEqual(r["status"], "NAVEGOU")

    def test_so_mexeu_na_tela_e_ok(self):
        r = classificar(_mapa(), self._evento(mutacoes=3))
        self.assertEqual(r["status"], "OK")

    def test_nada_aconteceu(self):
        r = classificar(_mapa(), self._evento())
        self.assertEqual(r["status"], "SEM_EFEITO")

    def test_link_interno_bom_sem_clique(self):
        r = classificar(_mapa(), self._evento(href="/torre"))
        self.assertEqual(r["status"], "LINK_OK")

    def test_link_interno_quebrado(self):
        r = classificar(_mapa(), self._evento(href="/torre-velha"))
        self.assertEqual(r["status"], "LINK_QUEBRADO")

    def test_link_ancora_conta_como_clique_normal(self):
        r = classificar(_mapa(), self._evento(href="#", onclick="abrir()", mutacoes=1))
        self.assertEqual(r["status"], "OK")

    def test_dialogo_de_validacao_e_efeito(self):
        r = classificar(_mapa(), self._evento(dialogos=["alert: Selecione o motorista"]))
        self.assertEqual(r["status"], "OK")
        self.assertIn("Selecione o motorista", r["detalhe"])

    def test_form_invalido_sem_efeito_e_precisa_dados(self):
        r = classificar(_mapa(), self._evento(form_invalido=True))
        self.assertEqual(r["status"], "PRECISA_DADOS")

    def test_campo_vazio_ao_lado_sem_efeito_e_precisa_dados(self):
        r = classificar(_mapa(), self._evento(campo_vazio_perto="#p-codigo"))
        self.assertEqual(r["status"], "PRECISA_DADOS")
        self.assertIn("#p-codigo", r["detalhe"])

    def test_campo_vazio_ao_lado_mas_com_efeito_e_ok(self):
        r = classificar(_mapa(), self._evento(campo_vazio_perto="#p-codigo", mutacoes=2))
        self.assertEqual(r["status"], "OK")

    def test_chamada_externa_nao_e_conferida(self):
        r = classificar(_mapa(), self._evento(chamadas=[
            {"metodo": "GET", "caminho": "https://maps.googleapis.com/x", "externo": True}]))
        self.assertEqual(r["status"], "OK")

    def test_post_de_form_que_navegou_e_ok_pela_chamada(self):
        r = classificar(_mapa(), self._evento(navegou=True,
                                              chamadas=[{"metodo": "POST", "caminho": "/logout"}]))
        self.assertEqual(r["status"], "OK")
        self.assertIn("POST /logout", r["detalhe"])


class TestSepararErros(unittest.TestCase):
    """Erro de JS depois da primeira chamada de rede e quase sempre o codigo
    engasgando na resposta falsa da trava, nao bug do botao."""

    def test_erro_antes_da_chamada_e_do_botao(self):
        erros = [{"t": 1.0, "msg": "rodarX is not defined"}]
        chamadas = [{"t": 2.0, "metodo": "POST", "caminho": "/x"}]
        do_botao, pos_resposta = separar_erros(erros, chamadas)
        self.assertEqual(do_botao, ["rodarX is not defined"])
        self.assertEqual(pos_resposta, [])

    def test_erro_depois_da_chamada_e_artefato(self):
        erros = [{"t": 3.0, "msg": "Cannot read properties of undefined"}]
        chamadas = [{"t": 2.0, "metodo": "POST", "caminho": "/x"}]
        do_botao, pos_resposta = separar_erros(erros, chamadas)
        self.assertEqual(do_botao, [])
        self.assertEqual(pos_resposta, ["Cannot read properties of undefined"])

    def test_sem_chamada_todo_erro_e_do_botao(self):
        do_botao, pos_resposta = separar_erros([{"t": 3.0, "msg": "x"}], [])
        self.assertEqual(do_botao, ["x"])


if __name__ == "__main__":
    unittest.main()
