"""
Rotas /api/wms/etiquetar/* (app do galpao): etiqueta de volume pra todo pedido da Stokki.
Rodar (de dentro de painel_agentes): py -3.11 -m unittest test_expedicao_volumes_tela -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import acesso_telas  # noqa: E402
import wms  # noqa: E402
import wms_separacao as sep  # noqa: E402
from test_fila_etiquetas_tela import painel_agentes  # noqa: E402

ORIGEM = {"Origin": "http://localhost"}
CFG = {"painel_agentes": {"usuario": "u", "senha": "s"}}
ANA = {"id": "1", "nome": "Ana"}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        for alvo, valor in ((wms, Path(self._tmp.name) / "dados.db"), (acesso_telas, Path(self._tmp.name) / "a.db")):
            p = mock.patch.object(alvo, "DB_PATH", valor)
            p.start()
            self.addCleanup(p.stop)
        for nome, valor in (("_carregar_config", mock.Mock(return_value=CFG)), ("_wms_gravar_operador", mock.Mock())):
            p = mock.patch.object(painel_agentes, nome, valor)
            p.start()
            self.addCleanup(p.stop)
        sep.conectar().close()
        painel_agentes.app.config["TESTING"] = True

    def cliente(self, nivel="galpao"):
        c = painel_agentes.app.test_client()
        with c.session_transaction() as s:
            s["nivel_acesso"] = nivel
            s["usuario"] = "teste"
        return c

    def operador(self, op):
        return mock.patch.object(painel_agentes, "_wms_operador_atual", return_value=op)

    def post(self, c, url, corpo=None):
        return c.post(url, json=corpo or {}, headers=ORIGEM)


class TestRotas(Base):
    def test_fluxo_do_operador(self):
        c = self.cliente("galpao")
        with self.operador(ANA):
            self.assertIsNone(c.get("/api/wms/etiquetar/pedido?codigo=45352").get_json()["volumes"])
            r = self.post(c, "/api/wms/etiquetar/pedido", {"codigo": "45352", "total": 3})
            self.assertEqual((r.status_code, r.get_json()["total"]), (200, 3))
            pede = self.post(c, "/api/wms/etiquetar/pedido", {"codigo": "45352", "total": 4}).get_json()
            self.assertIn("confirmar", pede)
            ok = self.post(c, "/api/wms/etiquetar/pedido", {"codigo": "45352", "total": 4, "confirmado": True}).get_json()
            self.assertEqual(ok["anuladas"], 3)
            self.assertEqual(self.post(c, "/api/wms/etiquetar/reimprimir", {"codigo": "45352", "numero": 2}).get_json()["quantidade"], 1)
            self.assertEqual(c.get("/api/wms/etiquetar/pedido?codigo=45352").get_json()["volumes"]["total"], 4)
            self.assertEqual(c.get("/api/wms/etiquetar/prontos").get_json(), {"total": 0, "itens": []})

    def test_erros_viram_400_com_o_motivo(self):
        c = self.cliente("galpao")
        with self.operador(ANA):
            r = self.post(c, "/api/wms/etiquetar/pedido", {"codigo": "abc", "total": 3})
            self.assertEqual(r.status_code, 400)
            self.assertIn("pedido invalido", r.get_json()["erro"])
            self.assertEqual(self.post(c, "/api/wms/etiquetar/pedido", {"codigo": "45352", "total": 0}).status_code, 400)
            self.assertEqual(c.get("/api/wms/etiquetar/pedido?codigo=").status_code, 400)

    def test_sem_operador_e_sem_nivel(self):
        c = self.cliente("galpao")
        with self.operador(None):
            r = self.post(c, "/api/wms/etiquetar/pedido", {"codigo": "45352", "total": 3})
            self.assertEqual(r.status_code, 409)
            self.assertTrue(r.get_json()["sem_operador"])
        with self.operador(ANA):
            self.assertEqual(self.post(self.cliente("expedicao"), "/api/wms/etiquetar/pedido",
                                       {"codigo": "45352", "total": 3}).status_code, 403)

    def test_estacao_imprime_o_pdf_do_trabalho(self):
        with self.operador(ANA):
            r = self.post(self.cliente("galpao"), "/api/wms/etiquetar/pedido", {"codigo": "45352", "total": 2}).get_json()
        pdf = self.cliente("expedicao").get(f"/etiquetas/fila/{r['impressao_id']}.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF"))


class TestPdfDiretoNaTela(Base):
    """Hugo, 28/11: no computador o PDF abre direto depois do Imprimir, com o login do galpao."""

    def test_galpao_abre_o_pdf_e_marca_impresso(self):
        c = self.cliente("galpao")
        with self.operador(ANA):
            r = self.post(c, "/api/wms/etiquetar/pedido", {"codigo": "45352", "total": 2}).get_json()
            self.assertEqual(r["impressao_ids"], [r["impressao_id"]])
            pdf = c.get(f"/api/wms/etiquetar/pdf/{r['impressao_id']}")
            self.assertEqual((pdf.status_code, pdf.mimetype), (200, "application/pdf"))
            self.assertTrue(pdf.data.startswith(b"%PDF"))
            m = self.post(c, "/api/wms/etiquetar/impresso", {"impressao_id": r["impressao_id"]})
            self.assertEqual((m.status_code, m.get_json()["situacao"]), (200, "IMPRESSO"))
        pendentes = self.cliente("expedicao").get("/api/etiquetas/fila").get_json()["trabalhos"]
        self.assertEqual(pendentes, [])                                   # saiu da fila da estacao

    def test_a_fila_da_estacao_continua_fechada_pro_galpao(self):
        c = self.cliente("galpao")
        with self.operador(ANA):
            r = self.post(c, "/api/wms/etiquetar/pedido", {"codigo": "45352", "total": 2}).get_json()
            self.assertEqual(c.get(f"/etiquetas/fila/{r['impressao_id']}.pdf").status_code, 403)
            self.assertEqual(self.cliente("expedicao").get(f"/api/wms/etiquetar/pdf/{r['impressao_id']}").status_code, 403)

    def test_trabalho_que_nao_e_da_tela_e_recusado_com_o_motivo(self):
        import wms_fila_impressao as fila
        conn = sep.conectar()
        piloto = fila.enfileirar(conn, tipo="VOLUME", tamanho="10x15", itens=["FV|PS-44009|1|1"], titulo="piloto")
        conn.commit()
        conn.close()
        c = self.cliente("galpao")
        with self.operador(ANA):
            r = c.get(f"/api/wms/etiquetar/pdf/{piloto}")
            self.assertEqual(r.status_code, 400)
            self.assertIn("nao e de etiqueta de volume", r.get_json()["erro"])
            self.assertEqual(self.post(c, "/api/wms/etiquetar/impresso", {"impressao_id": piloto}).status_code, 400)
            self.assertEqual(self.post(c, "/api/wms/etiquetar/impresso", {"impressao_id": "x"}).status_code, 400)
        with self.operador(None):
            self.assertEqual(self.post(c, "/api/wms/etiquetar/impresso", {"impressao_id": piloto}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
