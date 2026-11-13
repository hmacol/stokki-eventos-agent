# -*- coding: utf-8 -*-
"""
Tela /expedicao/coletas: saida de pedido de retirada com folha, nome, placa e
foto obrigatorios (Hugo, 12/11).

    py -3.11 -m unittest test_coletas_expedicao   (de dentro de painel_agentes/)
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco  # noqa: E402
from coletas import banco as coletas_banco, regras  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)

ORIGEM = {"Origin": "http://localhost"}


def _foto():
    b = io.BytesIO()
    Image.new("RGB", (60, 80), "white").save(b, format="JPEG")
    return io.BytesIO(b.getvalue())


class TestColetasExpedicao(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.pasta = Path(self._tmp.name)
        db = self.pasta / "dados.db"
        self.chamadas = []

        def iniciar(agente, modo_teste, ids_do_grupo, args_extra=None):
            self.chamadas.append(args_extra)
            return 500 + len(self.chamadas), False
        for p in (mock.patch.object(banco, "DB_PATH", db),
                  mock.patch.object(coletas_banco, "DB_PATH", db),
                  mock.patch.object(painel_agentes.acesso_telas, "DB_PATH", db),
                  mock.patch.object(painel_agentes.contadores_menu, "contadores", return_value={}),
                  mock.patch.object(painel_agentes, "_LOGS_EXECUCOES", self.pasta),
                  mock.patch.object(painel_agentes, "iniciar_execucao_com_fila", side_effect=iniciar),
                  mock.patch.object(painel_agentes.acoes_pedidos.tratativas, "registrar_evento")):
            p.start()
            self.addCleanup(p.stop)
        banco.conectar().close()
        conn = coletas_banco.conectar()
        for i, (transp, emb) in enumerate((("J3 FLEX", "NONNA ... #stkkc-1"), ("J3 FLEX", "NONNA ... #stkkc-1"),
                                           ("FRESHLOG", "ATW")), start=1):
            conn.execute("INSERT INTO expedicao_coletas_fila (id_stokki, codigo, embarcador, transportadora, retirada,"
                         " carga_inicial, visto_primeiro_em) VALUES (?, ?, ?, ?, ?, 0, '2026-11-12 08:00:00')",
                         (i, f"PS-{i}", emb, transp, int(transp != "FRESHLOG")))
        conn.execute("INSERT INTO expedicao_coletas_leituras VALUES ('2026-11-12 08:00:00', 'ok', '')")
        conn.commit()
        conn.close()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"

    def _tela(self):
        """O HTML da tela sem o script do fim (que cita os seletores dos botoes)."""
        r = self.cliente.get("/expedicao/coletas")
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True).split("const COLETAS_API")[0]

    def _folha(self, ids=(1, 2)):
        r = self.cliente.post("/api/expedicao/coletas/folha", json={"ids": list(ids)}, headers=ORIGEM)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()["coleta_id"]

    def _confirmar(self, cid, ids="1,2", nome="JOSE DA SILVA", placa="abc1d23", foto=True):
        dados = {"ids": ids, "motorista_nome": nome, "placa": placa}
        if foto is True:
            dados["foto"] = (_foto(), "folha.jpg")
        elif foto:
            dados["foto"] = foto
        return self.cliente.post(f"/api/expedicao/coletas/{cid}/confirmar", data=dados, headers=ORIGEM,
                                 content_type="multipart/form-data")

    def _coleta(self, cid):
        conn = coletas_banco.conectar()
        try:
            return regras.coleta(conn, cid)
        finally:
            conn.close()

    def test_tela_abre_para_galpao_e_expedicao_e_leitura_so_ve(self):
        for nivel in ("galpao", "expedicao"):
            self._logar(nivel)
            html = self._tela()
            self.assertIn("J3 FLEX", html)
            self.assertIn("PS-1", html)
            self.assertIn('data-acao="gerar"', html)
            self.assertNotIn("stkkc", html)
            self.assertIn("FRESHLOG: 1", html)
        self._logar("leitura")
        html = self._tela()
        self.assertIn("PS-1", html)
        self.assertNotIn('data-acao="gerar"', html)
        r = self.cliente.post("/api/expedicao/coletas/folha", json={"ids": [1]}, headers=ORIGEM)
        self.assertIn(r.status_code, (401, 403))

    def test_gerar_folha_abre_pdf_e_tira_o_pedido_da_lista(self):
        self._logar("expedicao")
        cid = self._folha([1])
        r = self.cliente.get(f"/expedicao/coletas/{cid}/folha.pdf")
        self.assertEqual((r.status_code, r.mimetype), (200, "application/pdf"))
        self.assertTrue(r.data.startswith(b"%PDF"))
        html = self._tela()
        self.assertIn("Folhas em andamento", html)
        self.assertIn("c-foto", html)
        self.assertEqual(html.count('class="vai" value="1"'), 0)
        self.assertEqual(html.count('class="vai" value="2"'), 1)
        # pedido que nao e retirada, ja em folha, sem origem
        self.assertEqual(self.cliente.post("/api/expedicao/coletas/folha", json={"ids": [3]}, headers=ORIGEM).status_code, 400)
        self.assertEqual(self.cliente.post("/api/expedicao/coletas/folha", json={"ids": [1]}, headers=ORIGEM).status_code, 400)
        self.assertEqual(self.cliente.post("/api/expedicao/coletas/folha", json={"ids": [2]}).status_code, 403)
        self.assertEqual(self.cliente.get("/expedicao/coletas/999/folha.pdf").status_code, 404)

    def test_confirmar_exige_foto_nome_e_placa(self):
        self._logar("galpao")
        cid = self._folha()
        casos = [({"foto": False}, "foto"), ({"nome": " "}, "nome"), ({"placa": ""}, "placa"),
                 ({"foto": (io.BytesIO(b"isto nao e imagem"), "x.jpg")}, "não é uma foto")]
        for kw, trecho in casos:
            r = self._confirmar(cid, **kw)
            self.assertEqual(r.status_code, 400, kw)
            self.assertIn(trecho, r.get_json()["erro"])
        self.assertEqual(self._coleta(cid)["status"], "ABERTA")
        self.assertEqual(self.chamadas, [])
        self.assertEqual(list((self.pasta / "coletas").glob("*.jpg")) if (self.pasta / "coletas").exists() else [], [])

    def test_confirmar_poe_o_robo_na_fila_com_a_coleta_e_acompanha(self):
        self._logar("expedicao")
        cid = self._folha()
        r = self._confirmar(cid, ids="1")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(self.chamadas), 1)
        args = self.chamadas[0]
        self.assertEqual(args[:5], ["--pedido", "PS-1", "--tela-pedidos", "--coleta", str(cid)])
        c = self._coleta(cid)
        self.assertEqual((c["status"], c["motorista_nome"], c["placa"], c["execucao_id"]),
                         ("CONFIRMADA", "JOSE DA SILVA", "ABC1D23", 501))
        self.assertEqual({p["codigo"]: p["estado"] for p in c["pedidos"]}, {"PS-1": "EXPEDINDO", "PS-2": "NAO_SAIU"})
        self.assertTrue((self.pasta / "coletas" / c["foto_arquivo"]).is_file())
        # segundo clique: recusa, sem nova execucao nem nova foto
        self.assertEqual(self._confirmar(cid, ids="1").status_code, 400)
        self.assertEqual(len(self.chamadas), 1)
        self.assertEqual(len(list((self.pasta / "coletas").glob("*.jpg"))), 1)
        # foto so com login
        self.assertEqual(self.cliente.get(f"/expedicao/coletas/{cid}/foto").status_code, 200)
        # robo rodando
        with mock.patch.object(painel_agentes, "buscar_execucao",
                               return_value={"id": 501, "agente_id": "somente_expedicao", "status": "RODANDO"}):
            d = self.cliente.get(f"/api/expedicao/coletas/{cid}/andamento").get_json()
        self.assertTrue(d["rodando"])
        # robo terminou e falhou neste pedido
        Path(args[args.index("--resultado-json") + 1]).write_text(
            json.dumps({"PS-1": {"ok": False, "mensagem": "situação inválida"}}), encoding="utf-8")
        with mock.patch.object(painel_agentes, "buscar_execucao",
                               return_value={"id": 501, "agente_id": "somente_expedicao", "status": "CONCLUIDO"}):
            d = self.cliente.get(f"/api/expedicao/coletas/{cid}/andamento").get_json()
        self.assertEqual((d["rodando"], d["status"], d["pedidos"][0]["estado"]), (False, "CONFIRMADA", "FALHOU"))
        html = self._tela()
        self.assertIn("situação inválida", html)
        self.assertIn('data-acao="tentar"', html)
        # tentar de novo: nova execucao com a mesma coleta, sem pedir foto
        r = self.cliente.post(f"/api/expedicao/coletas/{cid}/tentar-de-novo", json={}, headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.chamadas[1][:5], ["--pedido", "PS-1", "--tela-pedidos", "--coleta", str(cid)])
        Path(self.chamadas[1][-1]).write_text(json.dumps({"PS-1": {"ok": True, "mensagem": "expedido"}}), encoding="utf-8")
        with mock.patch.object(painel_agentes, "buscar_execucao",
                               return_value={"id": 502, "agente_id": "somente_expedicao", "status": "CONCLUIDO"}):
            d = self.cliente.get(f"/api/expedicao/coletas/{cid}/andamento").get_json()
        self.assertEqual(d["status"], "CONCLUIDA")
        html = self._tela()
        self.assertIn("Coletas concluídas", html)
        self.assertIn("JOSE DA SILVA", html)

    def test_nenhum_marcado_cancela_e_cancelar_devolve_pra_lista(self):
        self._logar("expedicao")
        cid = self._folha()
        r = self._confirmar(cid, ids="", foto=False)
        self.assertEqual((r.status_code, r.get_json().get("cancelada")), (200, True))
        self.assertEqual(self._coleta(cid)["status"], "CANCELADA")
        cid2 = self._folha()
        self.assertEqual(self.cliente.post(f"/api/expedicao/coletas/{cid2}/cancelar", json={}, headers=ORIGEM).status_code, 200)
        self.assertEqual(self.cliente.post(f"/api/expedicao/coletas/{cid2}/cancelar", json={}, headers=ORIGEM).status_code, 400)
        self.assertEqual(self.chamadas, [])

    def test_homologacao_conclui_simulada_sem_stokki(self):
        self._logar("expedicao")
        cid = self._folha()
        # na bancada quem recusa e a fila de agentes (DesligadoNaHomologacao)
        with mock.patch.object(painel_agentes, "iniciar_execucao_com_fila",
                               side_effect=painel_agentes._DesligadoNaHomologacao("Agentes desligado")):
            r = self._confirmar(cid)
        self.assertEqual((r.status_code, r.get_json().get("simulada")), (200, True))
        c = self._coleta(cid)
        self.assertEqual((c["status"], c["simulada"]), ("CONCLUIDA", 1))

    def test_homologacao_preenche_lista_de_exemplo_uma_vez(self):
        """A bancada nunca le a Stokki: a lista vem da copia dos dados (Hugo, 12/11)."""
        from galpao3d import banco as galpao
        conn = coletas_banco.conectar()
        conn.execute("DELETE FROM expedicao_coletas_fila")
        conn.execute("DELETE FROM expedicao_coletas_leituras")
        galpao.garantir_esquema(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS batimento_pedidos (id_stokki INTEGER, transportadora TEXT, visto_em TEXT)")
        for i, sit, transp in ((800, "Aguardando Transportador", "CLIENTE RETIRA 15.060.670/0001-70"),
                               (801, "Aguardando Transportador", "FRESHLOG"), (802, "Aberto", "CLIENTE RETIRA"),
                               (803, "Aguardando Transportador", None)):
            conn.execute("INSERT INTO galpao_pedidos (id_stokki, codigo, embarcador, situacao, atualizado_em) "
                         "VALUES (?, ?, 'PADRAO PURO ... #stkkc-2', ?, '2026-11-10 10:00:00')", (i, f"PS-{i}", sit))
            if transp:
                conn.execute("INSERT INTO batimento_pedidos VALUES (?, ?, '2026-11-10')", (i, transp))
        conn.commit()
        conn.close()
        self._logar("expedicao")
        html = self._tela()                      # fora da homologacao: nada e preenchido
        self.assertNotIn("PS-800", html)
        with mock.patch.object(painel_agentes, "_homolog_ativo", return_value=True):
            html = self._tela()
            self.assertIn("lista abaixo é de exemplo", html)
            self.assertEqual(html.count('class="vai" value="800"'), 1)
            self.assertNotIn('value="801"', html)    # nao e retirada
            self.assertNotIn('value="802"', html)    # nao esta aguardando transportador
            self.assertNotIn("leitura da Stokki está atrasada", html)
            self._tela()                             # segunda abertura nao duplica nem reabre
        conn = coletas_banco.conectar()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM expedicao_coletas_fila").fetchone()[0], 2)   # 803 nao tem transportadora na copia
        conn.close()

    def test_falha_ao_enfileirar_deixa_a_coleta_confirmada_pra_tentar_de_novo(self):
        self._logar("expedicao")
        cid = self._folha()
        with mock.patch.object(painel_agentes, "iniciar_execucao_com_fila", side_effect=RuntimeError("x")):
            r = self._confirmar(cid)
        self.assertEqual(r.status_code, 200)
        c = self._coleta(cid)
        self.assertEqual((c["status"], {p["estado"] for p in c["pedidos"]}), ("CONFIRMADA", {"FALHOU"}))
        r = self.cliente.post(f"/api/expedicao/coletas/{cid}/retirar", json={"id_stokki": 1}, headers=ORIGEM)
        self.assertEqual(r.status_code, 200)

    def test_tela_nova_esta_no_cadastro_de_acessos(self):
        chaves = [t[0] for t in painel_agentes.acesso_telas.TELAS]
        self.assertIn("expedicao_coletas", chaves)


if __name__ == "__main__":
    unittest.main()
