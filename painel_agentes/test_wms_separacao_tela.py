"""
Rotas /api/wms/separacao/* (app do galpao) e /ondas + /api/ondas/* (escritorio).
Rodar (de dentro de painel_agentes): py -3.11 -m unittest test_wms_separacao_tela -v
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
import wms_caixas  # noqa: E402
import wms_pedidos  # noqa: E402
import wms_separacao as sep  # noqa: E402
from test_fila_etiquetas_tela import painel_agentes  # noqa: E402

ORIGEM = {"Origin": "http://localhost"}
CFG = {"painel_agentes": {"usuario": "u", "senha": "s"},
       "wms": {"pilotos": [{"id": 22, "nome": "MOMBAK", "caixas": True},
                           {"id": 48, "nome": "MARIA DOLORES", "caixas": False}]}}
POS = "C9-E1-N1"
ANA, BIA = {"id": "1", "nome": "Ana"}, {"id": "2", "nome": "Bia"}


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
        conn = sep.conectar()
        conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, qtd_por_caixa, "
            "unidade, controla_validade, atualizado_em) VALUES (1, 901, 'CAFE', 'CAFE', 'MOMBAK', 22, "
            "'7890000000011', 12, 'UN', 0, '2026-09-21 10:00:00')")
        wms.criar_area(conn, "C9", "CONTAINER", "Container 9")
        wms.criar_posicao(conn, POS)
        conn.commit()
        self.seriais = wms_caixas.criar_caixas(conn, produto_id=1, lote="L1", validade=None, qtd_un=None,
                                               quantidade=2)["seriais"]
        for s in self.seriais:
            wms_caixas.ativar_caixa(conn, s, POS)
        self.pedido_id = wms_pedidos.registrar_pedido(conn, {
            "id_stokki": 70001, "codigo_ps": "PS-70001", "embarcador": "MOMBAK", "situacao": "Open",
            "embarcador_id": 22, "destinatario": "MERCADO", "data_entrega_calc": "2026-11-24"},
            [{"linha": 1, "sku": "CAFE", "ean_linha": "", "descricao": "CAFE", "qtd_embalagem": 24,
              "unidade_do_produto": True}])
        wms_pedidos.reservar_pedido(conn, self.pedido_id)
        conn.commit()
        conn.close()
        painel_agentes.app.config["TESTING"] = True
        self._n = 0

    def cliente(self, nivel="galpao"):
        c = painel_agentes.app.test_client()
        with c.session_transaction() as s:
            s["nivel_acesso"] = nivel
            s["usuario"] = "teste"
        return c

    def operador(self, op):
        """Operador do PIN na sessao (o mesmo helper que o /wms usa)."""
        return mock.patch.object(painel_agentes, "_wms_operador_atual", return_value=op)

    def u(self):
        self._n += 1
        return f"11111111-1111-4111-8111-{self._n:012d}"

    def post(self, c, url, corpo=None):
        return c.post(url, json=corpo or {}, headers=ORIGEM)


class TestRotasDoGalpao(Base):
    def test_fluxo_inteiro_pelas_rotas(self):
        c = self.cliente()
        with self.operador(ANA):
            painel = c.get("/api/wms/separacao/painel").get_json()
            self.assertEqual([p["codigo_ps"] for p in painel["pedidos_soltos"]], ["PS-70001"])
            onda = self.post(c, f"/api/wms/separacao/ondas/pedido/{self.pedido_id}").get_json()["onda_id"]
            d = c.get(f"/api/wms/separacao/ondas/{onda}").get_json()
            self.assertEqual((d["produtos"][0]["pedido_un"], d["caminhada"][0]["posicao"]), (24, POS))
            for s in self.seriais:
                r = self.post(c, f"/api/wms/separacao/ondas/{onda}/separar", {"serial": s, "uuid": self.u()})
                self.assertEqual((r.status_code, r.get_json()["codigo_ps"]), (200, "PS-70001"))
            r = self.post(c, f"/api/wms/separacao/ondas/{onda}/separar", {"serial": self.seriais[0], "uuid": self.u()})
            self.assertEqual(r.status_code, 400)
            self.assertIn("ja foi separada", r.get_json()["erro"])
            self.assertTrue(self.post(c, f"/api/wms/separacao/ondas/{onda}/fechar").get_json()["onda_concluida"])
            # quem separou nao confere
            self.assertEqual(self.post(c, f"/api/wms/separacao/conferir/{self.pedido_id}/iniciar").status_code, 400)
        with self.operador(BIA):
            conf = self.post(c, f"/api/wms/separacao/conferir/{self.pedido_id}/iniciar").get_json()
            self.assertFalse({"itens", "esperado", "faltas"} & set(conf))
            for s in self.seriais:
                r = self.post(c, f"/api/wms/separacao/conferencias/{conf['conferencia_id']}/bipe",
                              {"codigo": s, "uuid": self.u()})
                self.assertEqual(r.status_code, 200)
            fim = self.post(c, f"/api/wms/separacao/conferencias/{conf['conferencia_id']}/fechar").get_json()
            self.assertEqual(fim["resultado"], "OK")
            v = self.post(c, f"/api/wms/separacao/pedidos/{self.pedido_id}/volumes", {"total": 2}).get_json()
            self.assertEqual(v["total"], 2)
            r = self.post(c, f"/api/wms/separacao/pedidos/{self.pedido_id}/volumes/2/reimprimir")
            self.assertEqual(r.status_code, 200)
            # o celular do galpao nao imprime: quem abre o PDF e a estacao de impressao
            self.assertEqual(c.get(f"/etiquetas/fila/{v['impressao_id']}.pdf").status_code, 403)
        estacao = self.cliente("expedicao")
        self.assertEqual(estacao.get(f"/etiquetas/fila/{v['impressao_id']}.pdf").mimetype, "application/pdf")

    def test_conferencia_de_outro_operador_nao_aceita_bipe_nem_fechamento(self):
        c = self.cliente()
        with self.operador(ANA):
            onda = self.post(c, f"/api/wms/separacao/ondas/pedido/{self.pedido_id}").get_json()["onda_id"]
            for s in self.seriais:
                self.post(c, f"/api/wms/separacao/ondas/{onda}/separar", {"serial": s, "uuid": self.u()})
            self.post(c, f"/api/wms/separacao/ondas/{onda}/fechar")
        with self.operador(BIA):
            conf = self.post(c, f"/api/wms/separacao/conferir/{self.pedido_id}/iniciar").get_json()["conferencia_id"]
        with self.operador(ANA):   # quem separou tenta usar a conferencia da Bia pelo id
            r = self.post(c, f"/api/wms/separacao/conferencias/{conf}/bipe", {"codigo": self.seriais[0], "uuid": self.u()})
            self.assertEqual(r.status_code, 400)
            self.assertIn("so quem abriu", r.get_json()["erro"])
            self.assertEqual(self.post(c, f"/api/wms/separacao/conferencias/{conf}/fechar").status_code, 400)

    def test_sem_operador_do_pin_recusa_o_bipe(self):
        c = self.cliente()
        with self.operador(None):
            r = self.post(c, f"/api/wms/separacao/ondas/pedido/{self.pedido_id}")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["sem_operador"])

    def test_bipe_sem_uuid_corpo_estranho_e_onda_inexistente_viram_400(self):
        c = self.cliente()
        with self.operador(ANA):
            onda = self.post(c, f"/api/wms/separacao/ondas/pedido/{self.pedido_id}").get_json()["onda_id"]
            sem_uuid = self.post(c, f"/api/wms/separacao/ondas/{onda}/separar", {"serial": self.seriais[0]})
            self.assertEqual(sem_uuid.status_code, 400)
            lista = c.post(f"/api/wms/separacao/ondas/{onda}/separar", json=["lista"], headers=ORIGEM)
            self.assertEqual(lista.status_code, 400)
            self.assertEqual(c.get("/api/wms/separacao/ondas/9999").status_code, 400)

    def test_sem_login_nao_entra(self):
        r = painel_agentes.app.test_client().get("/api/wms/separacao/painel")
        self.assertIn(r.status_code, (302, 401, 403))


class TestEscritorio(Base):
    def test_login_do_galpao_nao_ve_as_telas_do_escritorio(self):
        c = self.cliente("galpao")
        for url in ("/ondas", "/api/ondas/pedidos?embarcador_id=22", "/api/ondas/andamento", "/api/ondas/faltas"):
            self.assertEqual(c.get(url).status_code, 403, url)
        montar = self.post(c, "/api/ondas", {"embarcador_id": 22, "pedido_ids": [self.pedido_id]})
        self.assertEqual(montar.status_code, 403)

    def test_montar_desfazer_e_acompanhar(self):
        c = self.cliente("operador")
        pagina = c.get("/ondas")
        self.assertEqual(pagina.status_code, 200)
        html = pagina.get_data(as_text=True)
        self.assertIn(">MOMBAK</option>", html)
        self.assertNotIn(">MARIA DOLORES</option>", html)   # nao opera por caixa
        r = c.get("/api/ondas/pedidos?embarcador_id=22").get_json()
        self.assertEqual((r["datas"], [p["codigo_ps"] for p in r["pedidos"]]),
                         ([{"data": "2026-11-24", "pedidos": 1}], ["PS-70001"]))
        self.assertEqual(c.get("/api/ondas/pedidos?embarcador_id=22&data_entrega=2026-11-25").get_json()["pedidos"], [])
        self.assertEqual(c.get("/api/ondas/pedidos?embarcador_id=48").status_code, 400)
        self.assertEqual(self.post(c, "/api/ondas", {"embarcador_id": 48, "pedido_ids": [self.pedido_id]}).status_code, 400)
        self.assertEqual(self.post(c, "/api/ondas", {"embarcador_id": 22, "pedido_ids": []}).status_code, 400)
        onda = self.post(c, "/api/ondas", {"embarcador_id": 22, "pedido_ids": [self.pedido_id]}).get_json()["onda_id"]
        self.assertEqual(c.get("/api/ondas/pedidos?embarcador_id=22").get_json()["ondas"][0]["id"], onda)
        self.assertEqual(c.get("/api/ondas/andamento").get_json()["pedidos"][0]["execucao"], "NA_ONDA")
        self.assertEqual(self.post(c, f"/api/ondas/{onda}/cancelar").status_code, 200)
        self.assertEqual(c.get("/api/ondas/andamento").get_json()["pedidos"], [])

    def test_decidir_falta_pela_rota(self):
        conn = sep.conectar()
        onda = sep.onda_de_um_pedido(conn, self.pedido_id, ANA)
        sep.separar_caixa(conn, onda, self.seriais[0], uuid="uuid-0000-0001", operador=ANA)
        sep.fechar_separacao(conn, onda, None, mesmo_faltando=True)
        conf = sep.iniciar_conferencia(conn, self.pedido_id, BIA)["conferencia_id"]
        sep.conferir_bipe(conn, conf, self.seriais[0], uuid="uuid-0000-0002")
        sep.fechar_conferencia(conn, conf)
        conn.close()
        c = self.cliente("total")
        faltas = c.get("/api/ondas/faltas").get_json()["faltas"]
        self.assertEqual((faltas[0]["codigo_ps"], faltas[0]["faltas"][0]["conferido_un"]), ("PS-70001", 12))
        sem_motivo = self.post(c, f"/api/ondas/faltas/{self.pedido_id}/decidir", {"decisao": "PARCIAL", "motivo": ""})
        self.assertEqual(sem_motivo.status_code, 400)
        r = self.post(c, f"/api/ondas/faltas/{self.pedido_id}/decidir", {"decisao": "PARCIAL", "motivo": "cliente aceitou"})
        self.assertEqual((r.status_code, r.get_json()["decisao"]), (200, "PARCIAL"))
        self.assertEqual(c.get("/api/ondas/faltas").get_json()["faltas"], [])


if __name__ == "__main__":
    unittest.main()
