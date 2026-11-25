# -*- coding: utf-8 -*-
"""
Encerrar o recebimento no /wms atualiza a entrada do portal (Pedidos de Entrada)
nos dois desfechos: sem falta (ENDERECADO) e com falta (DIVERGENCIA).

    py -3.11 -m unittest test_wms_encerrar_portal   (de dentro de painel_agentes/)
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
for _p in (_AQUI.parent, _AQUI, _AQUI.parent / "portal_cliente"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import entradas  # noqa: E402
import wms_caixas  # noqa: E402
from test_wms_caixas_tela import Base as BaseTela, painel_agentes  # noqa: E402


class EncerrarAtualizaPortal(BaseTela):
    def _recebimento_com_entrada(self, enderecado: float):
        conn = wms_caixas.conectar()
        conn.execute("INSERT INTO wms_recebimentos (id, id_stokki, codigo, estado, lido_em, atualizado_em) "
                     "VALUES (1, 5001, '#PE-5001', 'ESPERADO', '2026-11-01 10:00:00', '2026-11-01 10:00:00')")
        conn.execute("INSERT INTO wms_recebimento_itens (id, recebimento_id, linha, descricao, qtd_embalagem, "
                     "qtd_un, qtd_enderecada, produto_id) VALUES (10, 1, 1, 'PRODUTO', 1, 6, ?, 1)", (enderecado,))
        entradas.garantir_tabelas(conn)
        conn.execute(
            "INSERT INTO portal_entradas (cnpj_embarcador, origem, chave_nfe, data_prevista, arquivo_path, status, "
            "stokki_status, wms_recebimento_id, criado_em, atualizado_em) "
            "VALUES ('11111111000111', 'XML', 'K1', '2026-11-30', 'x.xml', 'CHEGOU', 'CRIADO', 1, "
            "'2026-11-01 10:00:00', '2026-11-01 10:00:00')")
        conn.commit()
        conn.close()

    def _status_do_portal(self) -> str:
        conn = wms_caixas.conectar()
        try:
            return conn.execute("SELECT status FROM portal_entradas WHERE chave_nfe = 'K1'").fetchone()[0]
        finally:
            conn.close()

    def _encerrar(self):
        self.operador()
        with mock.patch.object(painel_agentes.wms_faltas_recebimento, "notificar_faltas",
                               return_value={"enviado": False, "motivo": "teste"}):
            return self.post("/api/wms/recebimentos/1/encerrar", {"observacao": ""})

    def test_encerrar_sem_falta_atualiza_portal(self):
        self._recebimento_com_entrada(6)
        r = self._encerrar()
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._status_do_portal(), "ENDERECADO")

    def test_encerrar_com_falta_atualiza_portal(self):
        self._recebimento_com_entrada(2)
        r = self._encerrar()
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._status_do_portal(), "DIVERGENCIA")


if __name__ == "__main__":
    unittest.main()
