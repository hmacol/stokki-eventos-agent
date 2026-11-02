# -*- coding: utf-8 -*-
"""
APIs /api/wms/caixas* e trava do movimento avulso pro produto do piloto.

    py -3.11 -m unittest test_wms_caixas_tela   (de dentro de painel_agentes/)
"""
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
from test_fila_etiquetas_tela import painel_agentes  # noqa: E402

ORIGEM = {"Origin": "http://localhost"}
CFG = {"painel_agentes": {"usuario": "u", "senha": "s"}, "wms": {"caixas_ativo": True}}
CFG_OFF = {"painel_agentes": {"usuario": "u", "senha": "s"}, "wms": {}}
POS = "C9-E1-N1"
MSG = "controlado por caixa"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        for alvo, nome, valor in ((wms, "DB_PATH", Path(self._tmp.name) / "dados.db"),
                                  (acesso_telas, "DB_PATH", Path(self._tmp.name) / "a.db")):
            p = mock.patch.object(alvo, nome, valor)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value=CFG)
        p.start()
        self.addCleanup(p.stop)
        conn = wms_caixas.conectar()
        for pid, stokki, sku, emb in ((1, 900, "72400017", 48), (2, 901, "11100017", 99)):
            conn.execute(
                "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, "
                "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (?, ?, ?, "
                "'PRODUTO', 'EMB', ?, ?, 6, 'UN', 0, '2026-09-21 10:00:00')",
                (pid, stokki, sku, emb, sku + "0000"))
        wms.criar_area(conn, "C9", "CONTAINER", "Container 9")
        for pos in (POS, "C9-E1-N2"):
            wms.criar_posicao(conn, pos)
        conn.commit()
        conn.close()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()
        with self.cliente.session_transaction() as s:
            s["nivel_acesso"] = "galpao"
            s["usuario"] = "teste"

    def operador(self):
        with self.cliente.session_transaction() as s:
            s["wms_operador"] = {"id": 7, "nome": "Ana", "em": datetime.now().isoformat(timespec="seconds")}

    def post(self, url, corpo):
        return self.cliente.post(url, json=corpo, headers=ORIGEM)

    def mov(self, produto_id, uuid, qtd=6):
        return {"tipo": "ENTRADA", "produto_id": produto_id, "quantidade": qtd, "destino": POS,
                "lote": "L1", "uuid": uuid}

    def saldo(self, produto_id=1):
        conn = wms.conectar()
        try:
            return sum(s["quantidade"] for s in wms.saldos_produto(conn, produto_id))
        finally:
            conn.close()


class Trava(Base):
    def test_movimento_avulso_de_produto_do_piloto_recusa(self):
        self.operador()
        r = self.post("/api/wms/movimentos", self.mov(1, "x00000001"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["erro"], "Produto controlado por caixa: use Inventario, Receber ou Mover caixa.")
        self.assertEqual(self.saldo(1), 0)

    def test_movimento_de_outro_embarcador_continua_igual(self):
        self.operador()
        r = self.post("/api/wms/movimentos", self.mov(2, "x00000002"))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.saldo(2), 6)

    def test_lote_recusa_o_piloto_e_processa_o_outro(self):
        self.operador()
        r = self.post("/api/wms/movimentos/lote", {"movimentos": [self.mov(1, "x0000000a"), self.mov(2, "x0000000b", 4)]})
        self.assertEqual(r.status_code, 200)
        res = r.get_json()["resultados"]
        self.assertFalse(res[0]["ok"])
        self.assertIn(MSG, res[0]["erro"])
        self.assertTrue(res[1]["ok"])
        self.assertEqual((self.saldo(1), self.saldo(2)), (0, 4))

    def test_piloto_vem_do_config(self):
        self.operador()
        cfg = {**CFG, "wms": {"embarcador_piloto_id": 99, "caixas_ativo": True}}
        with mock.patch.object(painel_agentes, "_carregar_config", return_value=cfg):
            self.assertEqual(self.post("/api/wms/movimentos", self.mov(2, "x00000003")).status_code, 400)
            self.assertEqual(self.post("/api/wms/movimentos", self.mov(1, "x00000004")).status_code, 200)

    def test_bootstrap_traz_piloto_id(self):
        j = self.cliente.get("/api/wms/bootstrap").get_json()
        self.assertEqual((j["piloto_id"], j["caixas_ativo"]), (48, True))


class ChaveDesligada(Base):
    """wms.caixas_ativo ausente/false: sem trava, sem usa_caixas, sem criar caixa;
    ler, ativar, mover e cancelar continuam (preparar o zerar e o mutirao)."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value=CFG_OFF)
        p.start()
        self.addCleanup(p.stop)

    def test_bootstrap_e_tela_dizem_desligada(self):
        self.assertFalse(self.cliente.get("/api/wms/bootstrap").get_json()["caixas_ativo"])
        for valor in (False, "false", 0, None):
            with mock.patch.object(painel_agentes, "_carregar_config",
                                   return_value={**CFG_OFF, "wms": {"caixas_ativo": valor}}):
                self.assertFalse(self.cliente.get("/api/wms/bootstrap").get_json()["caixas_ativo"], valor)

    def test_movimento_avulso_do_piloto_passa(self):
        self.operador()
        r = self.post("/api/wms/movimentos", self.mov(1, "x00000011"))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.saldo(1), 6)

    def test_criar_caixa_recusa(self):
        self.operador()
        r = self.post("/api/wms/caixas", {"produto_id": 1, "lote": "L1", "validade": "2026-12-15", "quantidade": 1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["erro"], "Caixas desligadas (wms.caixas_ativo).")

    def test_recebimento_nao_marca_usa_caixas(self):
        conn = wms_caixas.conectar()
        conn.execute("INSERT INTO wms_recebimentos (id, id_stokki, lido_em, atualizado_em) "
                     "VALUES (1, 5001, '2026-11-01 10:00:00', '2026-11-01 10:00:00')")
        conn.execute("INSERT INTO wms_recebimento_itens (recebimento_id, linha, qtd_embalagem, qtd_un, produto_id) "
                     "VALUES (1, 1, 1, 6, 1)")
        conn.commit()
        conn.close()
        j = self.cliente.get("/api/wms/recebimentos/1").get_json()
        self.assertEqual([i["usa_caixas"] for i in j["itens"]], [False])

    def test_ler_ativar_mover_cancelar_continuam(self):
        self.operador()
        conn = wms_caixas.conectar()
        a, b = wms_caixas.criar_caixas(conn, produto_id=1, lote="L1", validade="2026-12-15", qtd_un=None,
                                       quantidade=2)["seriais"]
        conn.close()
        self.assertEqual(self.cliente.get(f"/api/wms/caixas/{a}").status_code, 200)
        self.assertEqual(self.cliente.get(f"/api/wms/ler?codigo={a}").status_code, 200)
        for s in (a, b):
            r = self.post(f"/api/wms/caixas/{s}/ativar", {"posicao": POS})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = self.post(f"/api/wms/caixas/{a}/mover", {"destino": "C9-E1-N2", "uuid": "x00000b01"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = self.post(f"/api/wms/caixas/{b}/cancelar", {"motivo": "teste"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.saldo(1), 6)


class Caixas(Base):
    def criar(self, n=2):
        r = self.post("/api/wms/caixas", {"produto_id": 1, "lote": "L1", "validade": "2026-12-15", "quantidade": n})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def test_sem_operador_da_409(self):
        for url, corpo in (("/api/wms/caixas", {"produto_id": 1, "lote": "L1", "quantidade": 1}),
                           ("/api/wms/caixas/C0000001/ativar", {"posicao": POS}),
                           ("/api/wms/caixas/C0000001/mover", {"destino": POS, "uuid": "x"}),
                           ("/api/wms/caixas/C0000001/cancelar", {"motivo": "x"})):
            r = self.post(url, corpo)
            self.assertEqual(r.status_code, 409, url)
            self.assertTrue(r.get_json()["sem_operador"])

    def test_fluxo_criar_e_ativar(self):
        self.operador()
        j = self.criar(2)
        self.assertEqual(len(j["seriais"]), 2)
        self.assertTrue(j["impressao_id"])
        for s in j["seriais"]:
            r = self.post(f"/api/wms/caixas/{s}/ativar", {"posicao": POS})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertEqual(r.get_json()["caixa"]["situacao"], "EM_ESTOQUE")
            self.assertFalse(r.get_json()["duplicado"])
        self.assertEqual(self.saldo(1), 12)
        again = self.post(f"/api/wms/caixas/{j['seriais'][0]}/ativar", {"posicao": POS})
        self.assertEqual(self.saldo(1), 12)
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.get_json()["duplicado"])

    def test_get_caixa_e_404(self):
        self.operador()
        s = self.criar(1)["seriais"][0]
        r = self.cliente.get(f"/api/wms/caixas/{s}")
        self.assertEqual(r.get_json()["caixa"]["serial"], s)
        r = self.cliente.get("/api/wms/caixas/C9999999")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json(), {"erro": "Caixa nao encontrada."})

    def test_erro_de_negocio_vira_400(self):
        self.operador()
        r = self.post("/api/wms/caixas", {"produto_id": 1, "lote": "L1", "quantidade": 0})
        self.assertEqual(r.status_code, 400)
        self.assertIn("erro", r.get_json())

    def test_mover_e_cancelar(self):
        self.operador()
        a, b = self.criar(2)["seriais"]
        self.post(f"/api/wms/caixas/{a}/ativar", {"posicao": POS})
        self.post(f"/api/wms/caixas/{b}/ativar", {"posicao": POS})
        r = self.post(f"/api/wms/caixas/{a}/mover", {"destino": "C9-E1-N2", "uuid": "x00000a01"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["caixa"]["posicao"], "C9-E1-N2")
        r = self.post(f"/api/wms/caixas/{b}/cancelar", {"motivo": "avariada"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["caixa"]["situacao"], "CANCELADA")
        self.assertEqual(self.saldo(1), 6)

    def test_recebimento_marca_usa_caixas(self):
        conn = wms_caixas.conectar()
        conn.execute("INSERT INTO wms_recebimentos (id, id_stokki, lido_em, atualizado_em) "
                     "VALUES (1, 5001, '2026-11-01 10:00:00', '2026-11-01 10:00:00')")
        for linha, pid in ((1, 1), (2, 2)):
            conn.execute("INSERT INTO wms_recebimento_itens (recebimento_id, linha, qtd_embalagem, qtd_un, produto_id) "
                         "VALUES (1, ?, 1, 6, ?)", (linha, pid))
        conn.commit()
        conn.close()
        j = self.cliente.get("/api/wms/recebimentos/1").get_json()
        self.assertEqual([i["usa_caixas"] for i in j["itens"]], [True, False])



class Endurecimento(Base):
    def test_movimento_com_uuid_reservado_recusa(self):
        self.operador()
        ruins = ("cx-C0000001-ativar", "ZERAR-1-x", "Cx-abc", "ps-123-reserva-9", " cx-C0000001-ativar",
                 "texto qualquer", "u-1", "x123", "x0123abcd\n",
                 "3F2504E0-4F89-41D3-9A0C-0305E82C3301\n")
        for uuid in ruins:
            r = self.post("/api/wms/movimentos", self.mov(2, uuid))
            self.assertEqual(r.status_code, 400, uuid)
            self.assertEqual(r.get_json()["erro"], "Identificador reservado pelo sistema.")
        r = self.post("/api/wms/movimentos/lote", {"movimentos": [self.mov(2, "cx-C0000002-ativar"),
                                                                    self.mov(2, "x0123abcd", 4)]})
        res = r.get_json()["resultados"]
        self.assertFalse(res[0]["ok"])
        self.assertEqual(res[0]["erro"], "Identificador reservado pelo sistema.")
        self.assertTrue(res[1]["ok"])
        self.assertEqual(self.saldo(2), 4)

    def test_uuid_do_aparelho_e_aceito(self):
        self.operador()
        for uuid in ("3F2504E0-4F89-41D3-9A0C-0305E82C3301", "x18c2f3a1b2c" + "3d4e5f6a7b8"):
            self.assertEqual(self.post("/api/wms/movimentos", self.mov(2, uuid, 1)).status_code, 200, uuid)
        self.assertEqual(self.post("/api/wms/movimentos", {**self.mov(2, None, 1), "uuid": None}).status_code, 200)

    def test_mover_com_uuid_reservado_recusa(self):
        self.operador()
        a, b = self.post("/api/wms/caixas", {"produto_id": 1, "lote": "L1", "validade": "2026-12-15",
                                              "quantidade": 2}).get_json()["seriais"]
        self.post(f"/api/wms/caixas/{a}/ativar", {"posicao": POS})
        r = self.post(f"/api/wms/caixas/{a}/mover", {"destino": "C9-E1-N2", "uuid": f"cx-{b}-ativar"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["erro"], "Identificador reservado pelo sistema.")

    def test_ativar_com_numero_invalido_da_400(self):
        self.operador()
        a = self.post("/api/wms/caixas", {"produto_id": 1, "lote": "L1", "validade": "2026-12-15",
                                           "quantidade": 1}).get_json()["seriais"][0]
        r = self.post(f"/api/wms/caixas/{a}/ativar", {"posicao": POS, "recebimento_id": 1, "item_id": [1]})
        self.assertEqual(r.status_code, 400)
        r = self.cliente.post(f"/api/wms/caixas/{a}/ativar", headers=ORIGEM, content_type="application/json",
                              data='{"posicao":"C9-E1-N1","recebimento_id":1,"item_id":Infinity}')
        self.assertEqual(r.status_code, 400)

    def test_qtd_un_nao_finita_recusa(self):
        self.operador()
        for q in ("nan", "inf", 0, -1, 100001, "abc"):
            r = self.post("/api/wms/caixas", {"produto_id": 1, "lote": "L1", "qtd_un": q, "quantidade": 1})
            self.assertEqual(r.status_code, 400, (q, r.get_data(as_text=True)))
        for n in ("x", 0, 201, None):
            r = self.post("/api/wms/caixas", {"produto_id": 1, "lote": "L1", "quantidade": n})
            self.assertEqual(r.status_code, 400, n)
        r = self.post("/api/wms/caixas", {"produto_id": "abc", "quantidade": 1})
        self.assertEqual(r.status_code, 400)

    def test_criar_caixa_de_produto_fora_do_piloto_recusa_na_rota(self):
        self.operador()
        r = self.post("/api/wms/caixas", {"produto_id": 2, "lote": "L1", "quantidade": 1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["erro"], "Produto fora do piloto: use Movimentos.")

    def test_leitura_nao_cria_caixa(self):
        self.operador()
        with self.cliente.session_transaction() as s:
            s["nivel_acesso"] = "leitura"
        r = self.post("/api/wms/caixas", {"produto_id": 1, "lote": "L1", "quantidade": 1})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
