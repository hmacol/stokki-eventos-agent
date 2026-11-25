# -*- coding: utf-8 -*-
"""NF ao lado do PS no portal do cliente: com NF, sem NF (traco) e mais de uma NF.

Rodar (da raiz): py -3.11 -m unittest portal_cliente.test_nf_ao_lado_do_ps
"""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).resolve().parent
for _p in (_AQUI.parent, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import dados_cliente  # noqa: E402


def _banco_com_nfs():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE documentos_pedido (id_pedido, tipo_doc, status, numero_nf)")
    c.execute("CREATE TABLE documentos_processados (codigo_pedido, tipo, status, numero_nf)")
    c.executemany("INSERT INTO documentos_pedido VALUES (?,?,?,?)", [
        ("PS-1", "nf", "casado", "111"),
        ("PS-3", "nf", "casado", "333"), ("#PS-3", "nf", "baixado", "222")])
    return c


class _SemFechar:
    """Conexao de teste que ignora close() (o codigo fecha a que abre)."""
    def __init__(self, c):
        self._c = c

    def __getattr__(self, nome):
        return getattr(self._c, nome)

    def close(self):
        pass


class PsENf(unittest.TestCase):
    def test_com_sem_e_varias_nf(self):
        c = _banco_com_nfs()
        self.assertEqual(dados_cliente.ps_e_nf("PS-1", c), "PS-1 · NF 111")
        self.assertEqual(dados_cliente.ps_e_nf("#PS-2", c), "#PS-2 · NF —")
        self.assertEqual(dados_cliente.ps_e_nf("PS-3", c), "PS-3 · NF 222, 333")

    def test_banco_quebrado_cai_no_traco(self):
        c = sqlite3.connect(":memory:")  # sem as tabelas
        self.assertEqual(dados_cliente.ps_e_nf("PS-1", c), "PS-1 · NF —")

    def test_atencao_traz_nf_de_cada_codigo(self):
        c = _banco_com_nfs()
        c.execute("CREATE TABLE insucessos_aguardando_resposta (service_id, code, sender_id, failed_reason_id, "
                  "primeira_notificacao_em, status)")
        c.executemany("INSERT INTO insucessos_aguardando_resposta VALUES (?,?,?,?,?,'PENDENTE')",
                      [(1, "#PS-1", 7, 5, "2026-11-20"), (2, "#PS-2", 7, 5, "2026-11-20"), (3, "#PS-3", 7, 5, "2026-11-20")])
        with mock.patch.object(dados_cliente, "_conectar", return_value=_SemFechar(c)), \
                mock.patch("nucleo.banco.conectar", return_value=_SemFechar(c)), \
                mock.patch.object(dados_cliente, "_remetentes", return_value=[7]):
            grupos = dados_cliente._coletar_atencao(7)
        self.assertEqual(grupos[0]["nfs"], {"#PS-1": "111", "#PS-2": "", "#PS-3": "222, 333"})

    def test_linha_da_tabela_junta_nfs_do_servico_com_mais_de_um_pedido(self):
        linha = dados_cliente._linha({"id": 1, "code": "#PS-1, #PS-3", "sender_id": 7}, "programado",
                                     {"PS-1": "111", "PS-3": "222, 333"})
        self.assertEqual(linha["nf"], "111, 222, 333")


if __name__ == "__main__":
    unittest.main()
