# -*- coding: utf-8 -*-
"""
Tela /etiquetas/fila (fila de impressao das etiquetas de caixa, computador da equipe).

    py -3.11 -m unittest test_fila_etiquetas_tela   (de dentro de painel_agentes/)
"""
import importlib.util
import re
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

import acesso_telas  # noqa: E402
import wms  # noqa: E402
import wms_caixas  # noqa: E402

def _carregar_painel():
    """Importa painel_agentes.py. Sem config.yaml (worktree limpo, CI) usa um
    config de mentira so pro import; os testes trocam _carregar_config depois."""
    import builtins
    import io
    spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    abrir = builtins.open

    def _open(arq, *a, **kw):
        if str(arq).endswith("config.yaml") and not Path(arq).exists():
            return io.StringIO("painel_agentes: {secret_key: teste}\n")
        return abrir(arq, *a, **kw)
    with mock.patch.object(builtins, "open", _open):
        spec.loader.exec_module(mod)
    return mod


painel_agentes = sys.modules.get("painel_agentes_app") or _carregar_painel()

ORIGEM = {"Origin": "http://localhost"}
CFG = {"painel_agentes": {"usuario": "u", "senha": "s"}, "wms": {}}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        for alvo, nome, valor in ((wms, "DB_PATH", self.db), (acesso_telas, "DB_PATH", Path(self._tmp.name) / "a.db")):
            p = mock.patch.object(alvo, nome, valor)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value=CFG)
        p.start()
        self.addCleanup(p.stop)
        conn = wms_caixas.conectar()
        conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (1, 900, '72400017', "
            "'COXINHA FESTA', 'MARIA DOLORES', 48, '724000170000', 6, 'UN', 1, '2026-09-21 10:00:00')")
        conn.commit()
        self.r = wms_caixas.criar_caixas(conn, produto_id=1, lote="L1", validade="2026-12-15", qtd_un=None,
                                         quantidade=2, operador={"nome": "Ana"})
        conn.close()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def logar(self, nivel):
        with self.cliente.session_transaction() as s:
            s["nivel_acesso"] = nivel
            s["usuario"] = "teste"

    def get(self, url):
        return self.cliente.get(url)


class Fila(Base):
    def test_galpao_nao_ve_a_fila(self):
        self.logar("galpao")
        self.assertIn(self.get("/etiquetas/fila").status_code, (302, 403))
        self.assertEqual(self.get("/api/etiquetas/fila").status_code, 403)

    def test_expedicao_abre_a_tela(self):
        self.logar("expedicao")
        r = self.get("/etiquetas/fila")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Fila de etiquetas", r.get_data(as_text=True))

    def test_expedicao_lista_e_baixa_o_pdf(self):
        self.logar("expedicao")
        j = self.get("/api/etiquetas/fila").get_json()
        self.assertEqual(len(j["trabalhos"]), 1)
        self.assertEqual(j["trabalhos"][0]["pedido_por"], "Ana")
        r = self.get(f"/etiquetas/fila/{j['trabalhos'][0]['id']}.pdf")
        self.assertEqual(r.mimetype, "application/pdf")
        self.assertTrue(r.data.startswith(b"%PDF"))

    def test_marcar_impresso_tira_da_lista(self):
        self.logar("operador")
        tid = self.get("/api/etiquetas/fila").get_json()["trabalhos"][0]["id"]
        r = self.cliente.post(f"/api/etiquetas/fila/{tid}/impresso", headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        t = r.get_json()["trabalho"]
        self.assertEqual((t["situacao"], t["impresso_por"]), ("IMPRESSO", "teste"))
        self.assertEqual(self.get("/api/etiquetas/fila").get_json()["trabalhos"], [])
        feitos = self.get("/api/etiquetas/fila?situacao=IMPRESSO").get_json()["trabalhos"]
        self.assertEqual(len(feitos), 1)

    def test_situacao_invalida_da_400(self):
        self.logar("total")
        r = self.get("/api/etiquetas/fila?situacao=XYZ")
        self.assertEqual(r.status_code, 400)
        self.assertIn("erro", r.get_json())

    def test_pdf_de_trabalho_inexistente_da_404(self):
        self.logar("total")
        self.assertEqual(self.get("/etiquetas/fila/9999.pdf").status_code, 404)

    def test_marcar_impresso_inexistente_da_400_json(self):
        self.logar("total")
        r = self.cliente.post("/api/etiquetas/fila/9999/impresso", headers=ORIGEM)
        self.assertEqual(r.status_code, 400)
        self.assertIn("erro", r.get_json())

    def test_pdf_com_parametro_invalido_da_400(self):
        self.logar("total")
        tid = self.get("/api/etiquetas/fila").get_json()["trabalhos"][0]["id"]
        self.assertEqual(self.get(f"/etiquetas/fila/{tid}.pdf?margem=abc").status_code, 400)
        for ruim in ("nan", "inf", "-inf", "1e999"):
            self.assertEqual(self.get(f"/etiquetas/fila/{tid}.pdf?dx={ruim}").status_code, 400, ruim)

    def test_tela_fila_esta_em_acesso_telas(self):
        telas = {t[0]: t for t in acesso_telas.TELAS}
        self.assertIn("fila_etiquetas", telas)
        padroes = telas["fila_etiquetas"][3]
        casa = lambda caminho: any(re.search(p, caminho) for p in padroes)  # noqa: E731
        self.assertTrue(casa("/etiquetas/fila"))
        self.assertTrue(casa("/etiquetas/fila/3.pdf"))
        self.assertTrue(casa("/api/etiquetas/fila/3/impresso"))
        self.assertFalse(casa("/wms"))
        self.assertFalse(casa("/wms/etiquetas.pdf"))


if __name__ == "__main__":
    unittest.main()
