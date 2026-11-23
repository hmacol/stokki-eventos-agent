# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest documentos_pedido.test_selecionar_pedidos
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "documentos_pedido"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import selecionar_pedidos as sel  # noqa: E402


def _linha(id_stokki: int, stkkc: str, nome: str = "CLIENTE X") -> dict:
    return {"id": f'<a href="/provider/outbound/show/{id_stokki}">{id_stokki}</a>',
            "client": f'{nome} <span class="text-muted">#stkkc-{stkkc}</span>'}


class ExpedidosRecentes(unittest.TestCase):
    def _rodar(self, linhas, com_nf=frozenset()):
        paginas = [{"aaData": linhas}, {"aaData": []}]
        with patch.object(sel.stokki_pedidos, "listar_pedidos", side_effect=paginas) as listar, \
             patch("fingerprint_documentos.pedidos_com_documento_enviado", return_value=set(com_nf)):
            return sel.descobrir_expedidos_recentes(object()), listar

    def test_todos_os_embarcadores_sem_filtro_de_cliente(self):
        res, listar = self._rodar([_linha(40598, "70")])
        self.assertEqual(res, [("PS-40598", False)])
        self.assertEqual(listar.call_args_list[0].kwargs.get("cliente", ""), "")
        self.assertEqual(listar.call_args_list[0].kwargs["status"], "Sent")

    def test_quem_ja_tem_nf_fica_fora(self):
        res, _ = self._rodar([_linha(1, "70"), _linha(2, "70")], com_nf={"PS-1"})
        self.assertEqual([c for c, _ in res], ["PS-2"])

    def test_embarcador_sem_nf_fica_fora(self):
        res, _ = self._rodar([_linha(1, "23"), _linha(2, "96"), _linha(3, "70")])
        self.assertEqual([c for c, _ in res], ["PS-3"])

    def test_dourado_marca_danfe_bloqueada(self):
        res, _ = self._rodar([_linha(5, "18")])
        self.assertEqual(res, [("PS-5", True)])

    def test_erro_da_stokki_nao_derruba(self):
        with patch.object(sel.stokki_pedidos, "listar_pedidos", side_effect=RuntimeError("500")), \
             patch("fingerprint_documentos.pedidos_com_documento_enviado", return_value=set()):
            self.assertEqual(sel.descobrir_expedidos_recentes(object()), [])


class ListaQualquerStatus(unittest.TestCase):
    def test_decisao_do_hugo_05_10(self):
        self.assertEqual(set(sel.EMBARCADORES_QUALQUER_STATUS), {"23", "18", "98"})


if __name__ == "__main__":
    unittest.main()
