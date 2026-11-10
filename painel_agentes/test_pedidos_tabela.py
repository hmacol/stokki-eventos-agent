# -*- coding: utf-8 -*-
"""
Tela /pedidos: todos os pedidos em tabela (Hugo, 09/11).

    py -3.11 -m unittest test_pedidos_tabela   (de dentro de painel_agentes/)
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes  # o Flask acha a pasta templates/ pelo modulo registrado
_spec.loader.exec_module(painel_agentes)


class TestTelaPedidos(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "dados.db")
        p.start()
        self.addCleanup(p.stop)
        conn = banco.conectar()
        hoje = date.today().isoformat()
        for codigo, status, dest in (("PS-1", "ENTREGUE", "MERCADO CENTRAL"), ("PS-2", "INSUCESSO", "PADARIA")):
            conn.execute("""INSERT INTO nucleo_pedidos (codigo, titulo, status, destinatario_nome, remetente_nome,
                                                        sender_id, caixas, criado_em_provedor)
                            VALUES (?, ?, ?, ?, 'FRUTA FINA', 10, 4, ?)""",
                         (codigo, f"#{codigo} - NF9 / FF / {dest}", status, dest, f"{hoje} 08:00:00"))
        conn.commit()
        conn.close()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"
            # produção já teve "Derrubar todas as sessões": sessão de rodada antiga cai pro login
            sess["rodada_sessoes"] = painel_agentes.acesso_telas.rodada_sessoes()["rodada"]

    def test_lista_com_filtro_de_coluna_e_ordenacao_preservada(self):
        self._logar("leitura")
        r = self.cliente.get("/pedidos?f_destinatario=pada&ordem=caixas&direcao=asc")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("PS-2", html)
        self.assertNotIn(">PS-1<", html)
        self.assertIn("Destinatário: “pada”", html)
        # Link de página/ordem mantém o filtro de coluna
        self.assertIn("f_destinatario=pada", html)

    def test_csv_com_colunas_escolhidas(self):
        self._logar("atendimento")
        r = self.cliente.get("/pedidos/exportar.csv?colunas=codigo,referencia,status&ordem=codigo&direcao=asc")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        linhas = r.get_data(as_text=True).lstrip("﻿").splitlines()
        self.assertEqual(linhas, ["Código;Referência;Status", "PS-1;NF9;Entregue", "PS-2;NF9;Insucesso"])

    def test_niveis_sem_acesso_nao_entram(self):
        self._logar("galpao")
        self.assertNotEqual(self.cliente.get("/pedidos").status_code, 200)
        self.assertNotEqual(self.cliente.get("/pedidos/exportar.csv").status_code, 200)


if __name__ == "__main__":
    unittest.main()
