# -*- coding: utf-8 -*-
"""
Rota de temperatura do produto (recebimento por etapas).

    py -3.11 -m unittest test_wms_doca_tela   (de dentro de painel_agentes/)
"""
import copy
import sys
import tempfile
import unittest
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

    def test_valor_invalido_400(self):
        self.entrar("total")
        self.assertEqual(self.post({"temperatura": "MORNO"}).status_code, 400)


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

    def test_guardar_uuid_reservado_recusa(self):
        self.operador()
        self.na_doca()
        r = self.enviar("/api/wms/guarda/guardar", self.corpo(uuid="cx-ABC-mover-1"))
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
