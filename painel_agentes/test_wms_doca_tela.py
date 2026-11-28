# -*- coding: utf-8 -*-
"""
Rota de temperatura do produto (recebimento por etapas).

    py -3.11 -m unittest test_wms_doca_tela   (de dentro de painel_agentes/)
"""
import copy
import sys
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
for _p in (_AQUI.parent, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import acesso_telas  # noqa: E402
import wms  # noqa: E402
import wms_caixas  # noqa: E402
import wms_chegada  # noqa: E402
import wms_doca  # noqa: E402
from test_fila_etiquetas_tela import painel_agentes  # noqa: E402

ORIGEM = {"Origin": "http://localhost"}
CFG = {"painel_agentes": {"usuario": "u", "senha": "s"}, "wms": {}}
PILOTO_ETAPAS = [{"id": 48, "nome": "X", "caixas": True, "etapas": True}]
POS = "C1-E1-N1"
UUID = "11111111-2222-3333-4444-555555555555"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        for alvo, nome, valor in ((wms, "DB_PATH", Path(self._tmp.name) / "dados.db"),
                                  (acesso_telas, "DB_PATH", Path(self._tmp.name) / "a.db")):
            p = mock.patch.object(alvo, nome, valor)
            p.start()
            self.addCleanup(p.stop)
        self.cfg = copy.deepcopy(CFG)   # cada teste liga as etapas na sua copia
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value=self.cfg)
        p.start()
        self.addCleanup(p.stop)
        conn = wms.conectar()
        conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (1, 900, '72400017', "
            "'PRODUTO', 'EMB', 48, '724000170000', 6, 'UN', 0, '2026-09-21 10:00:00')")
        conn.commit()
        conn.close()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def entrar(self, nivel):
        with self.cliente.session_transaction() as s:
            s["nivel_acesso"] = nivel
            s["usuario"] = "teste"

    def post(self, corpo):
        return self.cliente.post("/api/wms/produtos/1/temperatura", json=corpo, headers=ORIGEM)

    def operador(self):
        with self.cliente.session_transaction() as s:
            s["wms_operador"] = {"id": 7, "nome": "Ana", "em": datetime.now().isoformat(timespec="seconds")}

    def etapas(self):
        self.cfg["wms"]["pilotos"] = copy.deepcopy(PILOTO_ETAPAS)

    def enviar(self, url, corpo):
        return self.cliente.post(url, json=corpo, headers=ORIGEM)


class Temperatura(Base):
    def test_galpao_recebe_403(self):
        self.entrar("galpao")
        self.assertEqual(self.post({"temperatura": "CONGELADO"}).status_code, 403)

    def test_total_grava(self):
        self.entrar("total")
        r = self.post({"temperatura": "CONGELADO"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["produto"]["temperatura"], "CONGELADO")

    def test_grava_faixa_e_mantem_sem_as_chaves(self):
        self.entrar("total")
        r = self.post({"temperatura": "CONGELADO", "temp_min": "-25", "temp_max": "-20"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.get_json()["produto"]["temp_min"], r.get_json()["produto"]["temp_max"]), (-25.0, -20.0))
        r = self.post({"temperatura": "CONGELADO"})
        self.assertEqual((r.get_json()["produto"]["temp_min"], r.get_json()["produto"]["temp_max"]), (-25.0, -20.0))

    def test_chaves_vazias_limpam_a_faixa(self):
        self.entrar("total")
        self.post({"temperatura": "CONGELADO", "temp_min": "-25", "temp_max": "-20"})
        r = self.post({"temperatura": "CONGELADO", "temp_min": "", "temp_max": ""})
        self.assertIsNone(r.get_json()["produto"]["temp_min"])

    def test_valor_invalido_400(self):
        self.entrar("total")
        self.assertEqual(self.post({"temperatura": "MORNO"}).status_code, 400)

    def test_produto_inexistente_404(self):
        self.entrar("total")
        for corpo in ({"temperatura": "CONGELADO"}, {"temperatura": "CONGELADO", "temp_min": "-25", "temp_max": "-20"}):
            r = self.cliente.post("/api/wms/produtos/999/temperatura", json=corpo, headers=ORIGEM)
            self.assertEqual(r.status_code, 404, corpo)
            self.assertEqual(r.get_json()["erro"], "Produto não encontrado.")


class Rotas(Base):
    def setUp(self):
        super().setUp()
        self.entrar("galpao")
        conn = wms.conectar()
        wms.criar_area(conn, "C1", "CONTAINER", "Container 1")
        wms.criar_posicao(conn, POS)
        conn.close()

    def na_doca(self, qtd=6, lote="L1"):
        conn = wms.conectar()
        try:
            wms_doca.garantir_doca(conn)
            wms.registrar_movimento(conn, tipo="ENTRADA", produto_id=1, quantidade=qtd, lote=lote,
                                    destino=wms_doca.DOCA_POSICAO, uuid=f"e-{lote}")
        finally:
            conn.close()

    def boot(self):
        return self.cliente.get("/api/wms/bootstrap").get_json()

    def area_doca(self):
        conn = wms.conectar()
        try:
            return wms.obter_area(conn, "DOC1")
        finally:
            conn.close()

    def recebimento_de_uma_caixa(self):
        conn = wms_caixas.conectar()
        conn.execute("INSERT INTO wms_recebimentos (id, id_stokki, codigo, lido_em, atualizado_em) "
                     "VALUES (1, 5001, '#PE-5001', '2026-11-01 10:00:00', '2026-11-01 10:00:00')")
        conn.execute("INSERT INTO wms_recebimento_itens (id, recebimento_id, linha, descricao, qtd_embalagem, "
                     "qtd_un, produto_id) VALUES (10, 1, 1, 'PRODUTO', 1, 6, 1)")
        conn.commit()
        conn.close()

    def nova_caixa(self):
        conn = wms_caixas.conectar()
        try:
            return wms_caixas.criar_caixas(conn, produto_id=1, lote="L1", validade=None, qtd_un=6,
                                           quantidade=1)["seriais"][0]
        finally:
            conn.close()

    def posicao_da_caixa(self, serial):
        conn = wms_caixas.conectar()
        try:
            return wms_caixas.obter_caixa(conn, serial)["posicao"]
        finally:
            conn.close()

    def test_bootstrap_sem_etapas_nao_cria_doca(self):
        j = self.boot()
        self.assertFalse(j["etapas_ativo"])
        self.assertEqual(j["guarda_pendente"], 0)
        self.assertIsNone(self.area_doca())

    def test_bootstrap_com_etapas_cria_doca(self):
        self.etapas()
        j = self.boot()
        self.assertTrue(j["etapas_ativo"])
        self.assertEqual(j["etapas_embarcadores"], [48])
        self.assertEqual(j["doca"], "DOC1-P1")
        self.assertIsNotNone(self.area_doca())

    def test_bootstrap_guarda_pendente_sem_etapas(self):
        self.na_doca()
        j = self.boot()
        self.assertFalse(j["etapas_ativo"])
        self.assertEqual(j["guarda_pendente"], 1)

    def test_recebimento_marca_usa_etapas(self):
        self.recebimento_de_uma_caixa()
        self.assertFalse(self.cliente.get("/api/wms/recebimentos/1").get_json()["itens"][0]["usa_etapas"])
        self.etapas()
        self.assertTrue(self.cliente.get("/api/wms/recebimentos/1").get_json()["itens"][0]["usa_etapas"])

    def test_ativar_caixa_de_etapas_vai_para_doca(self):
        self.operador()
        self.etapas()
        self.recebimento_de_uma_caixa()
        conn = wms_caixas.conectar()   # a conferencia com etapas exige o Receber liberado
        wms_chegada.garantir_tabelas(conn)
        conn.execute("INSERT INTO wms_chegadas (id, uuid, estado) VALUES (1, 'u-doca', 'LIBERADA')")
        conn.execute("INSERT INTO wms_chegada_recebimentos (chegada_id, recebimento_id) VALUES (1, 1)")
        conn.commit()
        conn.close()
        s = self.nova_caixa()
        r = self.enviar(f"/api/wms/caixas/{s}/ativar", {"posicao": POS, "recebimento_id": 1, "item_id": 10})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.posicao_da_caixa(s), "DOC1-P1")

    def test_ativar_caixa_sem_etapas_respeita_posicao(self):
        self.operador()
        self.recebimento_de_uma_caixa()
        s = self.nova_caixa()
        r = self.enviar(f"/api/wms/caixas/{s}/ativar", {"posicao": POS, "recebimento_id": 1, "item_id": 10})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.posicao_da_caixa(s), POS)

    def test_fila_e_item(self):
        self.na_doca()
        fila = self.cliente.get("/api/wms/guarda/fila").get_json()["itens"]
        self.assertEqual([i["produto_id"] for i in fila], [1])
        j = self.cliente.get("/api/wms/guarda/item?produto_id=1&lote=L1&validade=").get_json()
        self.assertEqual(j["item"]["lote"], "L1")
        self.assertEqual(j["caixas"], [])
        self.assertIn("sugestoes", j)
        self.assertEqual(self.cliente.get("/api/wms/guarda/item?produto_id=1&lote=ZZ").status_code, 404)
        self.assertEqual(self.cliente.get("/api/wms/guarda/item").status_code, 404)

    def corpo(self, **extra):
        return dict({"produto_id": 1, "lote": "L1", "validade": "", "destino": POS, "uuid": UUID}, **extra)

    def test_guardar_exige_operador(self):
        self.na_doca()
        r = self.enviar("/api/wms/guarda/guardar", self.corpo())
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["sem_operador"])

    def test_guardar_ok(self):
        self.operador()
        self.na_doca()
        r = self.enviar("/api/wms/guarda/guardar", self.corpo())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["quantidade"], 6)
        self.assertEqual(r.get_json()["restante_na_doca"], 0)

    def test_guardar_temperatura_409(self):
        self.operador()
        self.na_doca()
        conn = wms.conectar()
        conn.execute("UPDATE wms_produtos SET temperatura = 'CONGELADO' WHERE id = 1")
        conn.commit()
        conn.close()
        r = self.enviar("/api/wms/guarda/guardar", self.corpo())
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["temperatura"]["produto"], "CONGELADO")
        r = self.enviar("/api/wms/guarda/guardar", self.corpo(confirmar_temperatura=True))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_guardar_diferente_com_mesmo_uuid_409(self):
        # a 1a guarda chegou e a resposta se perdeu; outra guarda do item com o uuid preso
        self.operador()
        self.na_doca(qtd=10)
        conn = wms.conectar()
        wms.criar_posicao(conn, "C1-E1-N2")
        conn.close()
        r = self.enviar("/api/wms/guarda/guardar", self.corpo(quantidade=4))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = self.enviar("/api/wms/guarda/guardar", self.corpo(quantidade=6, destino="C1-E1-N2"))
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        j = r.get_json()
        self.assertEqual(j["guarda_ja_feita"], {"destino": POS, "quantidade": 4.0, "seriais": []})
        self.assertTrue(j["erro"].startswith("Essa guarda já foi registrada em C1-E1-N1 (4 UN)."))
        r = self.enviar("/api/wms/guarda/guardar", self.corpo(quantidade=4))   # retry identico segue 200
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_guardar_uuid_reservado_recusa(self):
        self.operador()
        self.na_doca()
        r = self.enviar("/api/wms/guarda/guardar", self.corpo(uuid="cx-ABC-mover-1"))
        self.assertEqual(r.status_code, 400)


class SaidaDaDoca(Base):
    """A doca so esvazia pelo Guardar, pela separacao/baixa ou por Saida (o Mover leva a reserva para tras)."""
    na_doca, nova_caixa, posicao_da_caixa, corpo = Rotas.na_doca, Rotas.nova_caixa, Rotas.posicao_da_caixa, Rotas.corpo

    def setUp(self):
        super().setUp()
        self.entrar("galpao")
        self.operador()
        conn = wms.conectar()
        wms.criar_area(conn, "C1", "CONTAINER", "Container 1")
        wms.criar_posicao(conn, POS)
        conn.close()

    def mov(self, tipo, rota="/api/wms/movimentos", **mud):
        m = dict({"uuid": "x" + uuid.uuid4().hex, "tipo": tipo, "produto_id": 1,
                  "lote": "L1", "quantidade": 2}, **mud)
        return self.enviar(rota, {"movimentos": [m]} if rota.endswith("lote") else m)

    def saldo(self, pos):
        conn = wms.conectar()
        try:
            return sum(s["quantidade"] for s in wms.saldos_produto(conn, 1) if s["posicao"] == pos)
        finally:
            conn.close()

    def caixa_na_doca(self):
        s = self.nova_caixa()
        conn = wms_caixas.conectar()
        try:
            wms_doca.garantir_doca(conn)
            wms_caixas.ativar_caixa(conn, s, wms_doca.DOCA_POSICAO)
        finally:
            conn.close()
        return s

    def test_transferencia_da_doca_e_barrada(self):
        self.na_doca()
        r = self.mov("TRANSFERENCIA", origem="doc1-p1", destino=POS)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["erro"], "Produto na doca: use Guardar.")
        res = self.mov("TRANSFERENCIA", "/api/wms/movimentos/lote", origem="DOC1-P1", destino=POS).get_json()
        self.assertEqual(res["resultados"][0]["erro"], "Produto na doca: use Guardar.")
        self.assertEqual((self.saldo("DOC1-P1"), self.saldo(POS)), (6, 0))

    def test_saida_da_doca_continua_livre(self):
        self.na_doca()
        r = self.mov("SAIDA", origem="DOC1-P1", observacao="Perda / avaria")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.saldo("DOC1-P1"), 4)

    def test_transferencia_para_a_doca_continua_livre(self):
        conn = wms.conectar()
        wms_doca.garantir_doca(conn)
        wms.registrar_movimento(conn, tipo="ENTRADA", produto_id=1, quantidade=5, lote="L1", destino=POS)
        conn.close()
        r = self.mov("TRANSFERENCIA", origem=POS, destino="DOC1-P1")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.saldo("DOC1-P1"), 2)

    def test_mover_caixa_da_doca_e_barrado(self):
        s = self.caixa_na_doca()
        r = self.enviar(f"/api/wms/caixas/{s.lower()}/mover", {"destino": POS, "uuid": UUID})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["erro"], "Caixa na doca: use Guardar.")
        self.assertEqual((self.posicao_da_caixa(s), self.saldo("DOC1-P1"), self.saldo(POS)), ("DOC1-P1", 6, 0))

    def test_guardar_caixa_continua(self):
        s = self.caixa_na_doca()
        r = self.enviar("/api/wms/guarda/guardar", self.corpo())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual((r.get_json()["movidas"], self.posicao_da_caixa(s)), ([s], POS))


class ConferirRecebimentoNaTela(Base):
    """Marcadores da tela Conferir Recebimento no HTML do /wms (nao ha harness de JS)."""

    def html(self):
        self.entrar("galpao")
        return self.cliente.get("/wms").get_data(as_text=True)

    def test_marcadores(self):
        h = self.html()
        for m in ("Registrar conferido", "Conferir Recebimento", "Encerrar conferência",
                  "Pedir etiquetas de todas as linhas", "function rotuloPos",
                  "Conferido, está na DOCA", "vão para a fila Guardar"):
            self.assertIn(m, h)

    def test_movimento_da_conferencia_leva_recebimento_id(self):
        self.assertIn("destino: posicao, recebimento_id: r.id,", self.html())

    def test_pedir_todas_so_com_etapas_e_sem_duplicar(self):
        h = self.html()
        self.assertIn("it.usa_caixas && it.usa_etapas", h)
        self.assertIn("estadoCx(i).impressaoId) { jaPedidas.push", h)
        self.assertIn("Já pedidas: linha", h)
        self.assertIn("Falhou: linha", h)

    def test_cartao_guardar_so_com_linha_de_etapas(self):
        h = self.html()
        self.assertIn("etapasPorLinha[l.linha]", h)
        self.assertNotIn("S.etapasAtivo && nConferidos", h)

    def test_caminho_antigo_continua(self):
        h = self.html()
        for m in ("Registrar entrada", "Encerrar recebimento", "Entrada registrada em"):
            self.assertIn(m, h)


class GuardarNaTela(ConferirRecebimentoNaTela):
    """Marcadores das telas da fila Guardar no HTML do /wms."""

    def test_marcadores_guardar(self):
        h = self.html()
        for m in ("TELAS.guardar", "TELAS.guardarItem", "Guardar só uma parte aqui", "Guardar aqui mesmo",
                  "Posição não combina", "itens na doca sem lugar", "O item conferido vai para a fila Guardar."):
            self.assertIn(m, h)

    def test_uuid_da_guarda_estavel_por_item_ate_o_sucesso(self):
        h = self.html()
        self.assertIn("uuid: uuidGuarda(it) });", h)
        # sucesso e guarda ja feita (409), na tela e no envio da fila
        self.assertEqual(h.count("soltarUuidGuarda(corpo);"), 4)
        self.assertNotIn("ctx.uuid = uuid()", h)   # trocar a posicao nao troca o uuid

    def test_guarda_ja_feita_solta_o_uuid_e_recarrega_o_item(self):
        h = self.html()
        self.assertIn("e.dados.guarda_ja_feita) { soltarUuidGuarda(corpo); return abrirGuardaItem(ctx.item, e.message); }", h)
        self.assertIn("Guarda já registrada", h)
        self.assertIn("if (e.dados && e.dados.guarda_ja_feita) soltarUuidGuarda(corpo);", h)   # fila offline

    def test_mover_nao_oferece_a_doca_como_origem(self):
        h = self.html()
        self.assertIn("semDoca(p.saldos || [], b.dataset.mov)", h)
        self.assertIn("semDoca(p.saldos || [], ctx.tipo)", h)
        self.assertIn("itens.length && p.codigo !== S.doca ? '' : 'disabled'}>Mover daqui", h)


if __name__ == "__main__":
    unittest.main()
