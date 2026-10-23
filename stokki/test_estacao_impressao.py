# -*- coding: utf-8 -*-
"""Testes da leitura e conferência da fila da Estação de Impressão (tela por abas, 22/10)."""
import json
import unittest

from stokki.estacao_impressao import _conferir_fila, _extrair_pagina_lista

# Linha real da aba 'Faturados para imprimir' (22/10/2026), encurtada.
_LINHA = """<tr>
    <td>
  <span class="st-t-id">#PS-42384</span>
  <span class="st-t-sub">7719</span>
</td>
<td class="st-center">
  22/10/2026
  <span class="st-t-sub">12:12</span>
</td>
<td>
  <span class="st-t-strong">ITAUEIRA CAMAROES LTDA</span>
  <span class="st-t-sub">#stkkc-67</span>
</td>
<td>
      <span class="st-t-strong">FRESHLOG</span>
    <span class="st-t-sub">54.039.899/0001-85</span>
  </td>
    <td class="st-center">22/10/2026 <span class="st-t-sub">16:03</span></td>
    <td class="st-center"><span class="st-badge-days">hoje</span></td>
    <td>
      <button type="button" class="btn btn-success btn-sm btn_finish" data-id="42384">
        Iniciar Impressão <i class="fas fa-arrow-right ml-1"></i>
      </button>
    </td>
  </tr>"""


def _resposta(data="", total=0, tab="billed", success=True):
    return json.dumps({"success": success, "message": "", "content": {
        "tab": tab, "sort": "order", "direction": "desc", "data": data,
        "counts": {"awaiting_billing": 6, "billed": total, "printed": 0},
        "pagination": {"total": total, "per_page": 100, "current_page": 1,
                       "last_page": 1, "from": 1 if total else 0, "to": total},
    }})


class TestExtrairPaginaLista(unittest.TestCase):
    def test_linha_da_tela_nova(self):
        pedidos, paginacao = _extrair_pagina_lista(_resposta(_LINHA, 1), "billed")
        self.assertEqual(paginacao["total"], 1)
        self.assertEqual(len(pedidos), 1)
        p = pedidos[0]
        self.assertEqual(p["id_stokki"], 42384)
        self.assertEqual(p["codigo_ps"], "#PS-42384")
        self.assertEqual(p["referencia"], "7719")
        self.assertEqual(p["cliente"], "ITAUEIRA CAMAROES LTDA")
        self.assertEqual(p["codigo_cli"], "#stkkc-67")

    def test_fila_vazia(self):
        pedidos, paginacao = _extrair_pagina_lista(_resposta("", 0), "billed")
        self.assertEqual((pedidos, paginacao["total"]), ([], 0))

    def test_pagina_de_login_no_lugar_do_json_levanta(self):
        with self.assertRaises(RuntimeError):
            _extrair_pagina_lista("<!DOCTYPE html><html><title>3PL - Acesse sua conta aqui</title>", "billed")

    def test_formato_inesperado_levanta(self):
        for texto in (_resposta(success=False), _resposta(tab="printed"),
                      json.dumps({"success": True, "content": {"tab": "billed", "data": ""}})):
            with self.assertRaises(RuntimeError):
                _extrair_pagina_lista(texto, "billed")


class TestConferirFila(unittest.TestCase):
    def test_zero_de_verdade_passa(self):
        _conferir_fila(0, [], "0", 0)

    def test_fila_com_pedidos_passa(self):
        _conferir_fila(2, ["42384", "42383"], "2", 2)

    def test_contador_sumiu_levanta(self):
        # o caso de 22/10: tela trocada, nada encontrado e tudo "0"
        with self.assertRaises(RuntimeError):
            _conferir_fila(0, [], None, None)

    def test_contador_diferente_levanta(self):
        with self.assertRaises(RuntimeError):
            _conferir_fila(0, [], "2", 0)

    def test_lidos_diferente_do_total_levanta(self):
        with self.assertRaises(RuntimeError):
            _conferir_fila(2, ["42384"], "2", 2)

    def test_fila_com_pedido_e_aba_sem_botao_levanta(self):
        with self.assertRaises(RuntimeError):
            _conferir_fila(2, ["42384", "42383"], "2", 0)

    def test_lista_zero_e_aba_com_botao_levanta(self):
        with self.assertRaises(RuntimeError):
            _conferir_fila(0, [], "0", 1)


if __name__ == "__main__":
    unittest.main()
