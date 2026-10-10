# -*- coding: utf-8 -*-
"""
Tela /canhotos-pendentes (canhoto de entregue sem checklist, so guardar; 09/10).

    py -3.11 -m unittest painel_agentes.test_canhotos_pendentes_tela
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco  # noqa: E402
from nucleo import canhotos_manuais as cm  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)


class TestCanhotosPendentes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        p = mock.patch.object(banco, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)
        conn = banco.conectar(self.db)
        for cod, status in (("PS-1", "ENTREGUE"), ("PS-2", "INSUCESSO")):
            conn.execute("INSERT INTO nucleo_pedidos (codigo, vuupt_service_id, status, qtd_checklists, remetente_nome, "
                         "destinatario_nome, origem) VALUES (?, ?, ?, 0, 'EMB A', 'Cliente', 'VUUPT')",
                         (cod, int(cod[3:]), status))
            rid = conn.execute("INSERT INTO nucleo_rotas (data_rota, provedor, vuupt_route_id, agent_id, motorista_nome, "
                               "status) VALUES (date('now','-3 days'), 'VUUPT', ?, 1, 'Iago', 'CONCLUIDA')",
                               (int(cod[3:]),)).lastrowid
            conn.execute("INSERT INTO nucleo_paradas (rota_id, ordem, codigo, situacao) VALUES (?, 0, ?, ?)",
                         (rid, cod, status))
        conn.commit()
        conn.close()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as s:
            s["nivel_acesso"] = nivel
            s["usuario"] = "teste"

    def _enviar(self, codigo, conteudo):
        return self.cliente.post("/api/canhotos-pendentes",
                                 data={"codigo": codigo, "arquivo": (io.BytesIO(conteudo), "c.pdf")},
                                 content_type="multipart/form-data", headers={"Origin": "http://localhost"})

    def test_lista_entregue_sem_comprovante(self):
        self._logar("operador")
        html = self.cliente.get("/canhotos-pendentes").get_data(as_text=True)
        self.assertIn("PS-1", html)
        self.assertNotIn("PS-2", html)
        self.assertIn('class="canhoto"', html)

    def test_envio_grava_e_sai_da_lista(self):
        self._logar("operador")
        r = self._enviar("PS-1", b"%PDF-1.4 x")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNotNone(cm.caminho_canhoto_manual("PS-1", db_path=self.db))
        self.assertNotIn("PS-1", self.cliente.get("/canhotos-pendentes").get_data(as_text=True))

    def test_recusa_pedido_que_nao_esta_pendente_e_arquivo_invalido(self):
        self._logar("operador")
        self.assertEqual(self._enviar("PS-2", b"%PDF-1.4 x").status_code, 400)     # insucesso
        self.assertEqual(self._enviar("../../X", b"%PDF-1.4 x").status_code, 400)
        self.assertEqual(self._enviar("PS-1", b"lixo").status_code, 400)
        self.assertIsNone(cm.caminho_canhoto_manual("PS-1", db_path=self.db))

    def test_leitura_nao_entra(self):
        self._logar("leitura")
        self.assertIn(self.cliente.get("/canhotos-pendentes").status_code, (302, 401, 403))
        self.assertIn(self._enviar("PS-1", b"%PDF-1.4 x").status_code, (302, 401, 403))


if __name__ == "__main__":
    unittest.main()
