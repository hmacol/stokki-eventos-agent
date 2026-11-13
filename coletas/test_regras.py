# -*- coding: utf-8 -*-
import sqlite3
import unittest

from coletas import banco, regras
from coletas.regras import ErroColeta

RETIRA = "CLIENTE RETIRA 15.060.670/0001-70"


class TestRegras(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        banco.garantir_esquema(self.conn)
        self.addCleanup(self.conn.close)

    def _fila(self, id_, transportadora="J3 FLEX", embarcador="NONNA", retirada=1, saiu=None, inicial=0):
        self.conn.execute(
            "INSERT INTO expedicao_coletas_fila (id_stokki, codigo, embarcador, transportadora, retirada,"
            " carga_inicial, visto_primeiro_em, saiu_em) VALUES (?, ?, ?, ?, ?, ?, '2026-11-12 08:00:00', ?)",
            (id_, f"PS-{id_}", embarcador, transportadora, retirada, inicial, saiu))
        self.conn.commit()

    def _folha(self, ids=(1, 2)):
        for i in ids:
            self._fila(i)
        return regras.gerar_folha(self.conn, list(ids), "expedicao")

    def _confirmar(self, cid, ids, nome="JOSE DA SILVA", placa="abc1d23", foto="COL-1.jpg"):
        return regras.confirmar(self.conn, cid, ids, nome, placa, foto, "expedicao")

    def _estados(self, cid):
        return {p["id_stokki"]: p["estado"] for p in regras.coleta(self.conn, cid)["pedidos"]}

    # ── lista a coletar ──
    def test_a_coletar_agrupa_e_tira_quem_esta_em_folha(self):
        self._fila(1)
        self._fila(2)
        self._fila(3, RETIRA, "PADRAO PURO")
        self._fila(4, RETIRA, "GOURMAR")
        self._fila(5, "FRESHLOG", retirada=0)
        self._fila(6, saiu="2026-11-12 09:00:00")
        r = regras.a_coletar(self.conn)
        self.assertEqual([(g["transportadora"], g["embarcador"], [p["codigo"] for p in g["pedidos"]])
                          for g in r["grupos"]],
                         [("J3 FLEX", None, ["PS-1", "PS-2"]), (RETIRA, "GOURMAR", ["PS-4"]),
                          (RETIRA, "PADRAO PURO", ["PS-3"])])
        self.assertEqual(r["sem_cadastro"], [{"transportadora": "FRESHLOG", "n": 1}])
        cid = regras.gerar_folha(self.conn, [1], "expedicao")
        j3 = lambda: [p["codigo"] for g in regras.a_coletar(self.conn)["grupos"]
                      if g["transportadora"] == "J3 FLEX" for p in g["pedidos"]]
        self.assertEqual(j3(), ["PS-2"])
        regras.cancelar_folha(self.conn, cid, "expedicao")
        self.assertEqual(j3(), ["PS-1", "PS-2"])

    def test_a_coletar_traz_unidades_nf_e_destinatario(self):
        self._fila(1)
        self.conn.executescript("""
            CREATE TABLE galpao_pedidos (id_stokki INTEGER PRIMARY KEY, unidades INTEGER);
            INSERT INTO galpao_pedidos VALUES (1, 48);
            CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, codigo_pedido TEXT, numero_nf TEXT,
                destinatario_nome TEXT, volumes INTEGER, status TEXT);
            INSERT INTO portal_envios VALUES (1, 'PS-1', '900', 'PADARIA', 3, 'CRIADO');
        """)
        p = regras.a_coletar(self.conn)["grupos"][0]["pedidos"][0]
        self.assertEqual((p["unidades"], p["nf"], p["destinatario"]), (48, "900", "PADARIA"))

    # ── gerar folha ──
    def test_folha_guarda_transportadora_e_pedidos(self):
        cid = self._folha()
        c = regras.coleta(self.conn, cid)
        self.assertEqual((c["numero"], c["status"], c["transportadora"], c["embarcador"], c["criada_por"]),
                         ("COL-1", "ABERTA", "J3 FLEX", None, "expedicao"))
        self.assertEqual(self._estados(cid), {1: "NA_FOLHA", 2: "NA_FOLHA"})

    def test_folha_recusa(self):
        for i in range(1, 32):
            self._fila(i)
        self._fila(40, "MANDAE")
        self._fila(41, RETIRA, "PADRAO PURO")
        self._fila(42, RETIRA, "GOURMAR")
        self._fila(43, "FRESHLOG", retirada=0)
        self._fila(44, saiu="2026-11-12 09:00:00")
        casos = [([], "Marque"), (list(range(1, 32)), "no máximo 30"), ([1, 40], "uma transportadora"),
                 ([41, 42], "um embarcador"), ([43], "não está cadastrada como retirada"),
                 ([44], "não está mais aguardando"), ([999], "não está mais aguardando")]
        for ids, trecho in casos:
            with self.assertRaises(ErroColeta) as e:
                regras.gerar_folha(self.conn, ids, "expedicao")
            self.assertIn(trecho, str(e.exception))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM expedicao_coletas").fetchone()[0], 0)
        self.assertEqual(regras.coleta(self.conn, regras.gerar_folha(self.conn, [41], "x"))["embarcador"], "PADRAO PURO")

    def test_pedido_ja_em_folha_viva_e_recusado(self):
        self._folha()
        with self.assertRaises(ErroColeta) as e:
            regras.gerar_folha(self.conn, [2], "galpao")
        self.assertIn("outra folha", str(e.exception))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM expedicao_coletas").fetchone()[0], 1)

    # ── confirmar ──
    def test_confirmar_exige_foto_nome_e_placa(self):
        cid = self._folha()
        for kw, trecho in (({"foto": ""}, "foto"), ({"nome": "  "}, "nome"), ({"placa": ""}, "placa")):
            with self.assertRaises(ErroColeta) as e:
                self._confirmar(cid, [1, 2], **kw)
            self.assertIn(trecho, str(e.exception))
        self.assertEqual(regras.coleta(self.conn, cid)["status"], "ABERTA")
        self.assertFalse(regras.tem_foto(self.conn, cid, ["PS-1"]))

    def test_confirmar_marca_quem_saiu_e_devolve_o_resto_pra_lista(self):
        cid = self._folha()
        self.assertEqual(self._confirmar(cid, [1, 999]), ["PS-1"])
        c = regras.coleta(self.conn, cid)
        self.assertEqual((c["status"], c["motorista_nome"], c["placa"], c["foto_arquivo"], c["confirmada_por"]),
                         ("CONFIRMADA", "JOSE DA SILVA", "ABC1D23", "COL-1.jpg", "expedicao"))
        self.assertEqual(self._estados(cid), {1: "EXPEDINDO", 2: "NAO_SAIU"})
        self.assertEqual([p["codigo"] for p in regras.a_coletar(self.conn)["grupos"][0]["pedidos"]], ["PS-2"])
        self.assertTrue(regras.tem_foto(self.conn, cid, ["PS-1"]))
        self.assertFalse(regras.tem_foto(self.conn, cid, ["PS-1", "PS-2"]))
        self.assertFalse(regras.tem_foto(self.conn, cid, []))
        self.assertFalse(regras.tem_foto(self.conn, 99, ["PS-1"]))

    def test_confirmar_duas_vezes_e_recusado(self):
        cid = self._folha()
        self._confirmar(cid, [1, 2])
        with self.assertRaises(ErroColeta) as e:
            self._confirmar(cid, [1, 2])
        self.assertIn("já confirmada", str(e.exception))

    def test_confirmar_sem_ninguem_cancela_a_folha(self):
        cid = self._folha()
        self.assertEqual(self._confirmar(cid, [], foto=""), [])
        self.assertEqual(regras.coleta(self.conn, cid)["status"], "CANCELADA")
        self.assertEqual(len(regras.a_coletar(self.conn)["grupos"][0]["pedidos"]), 2)
        with self.assertRaises(ErroColeta):
            regras.cancelar_folha(self.conn, cid, "x")

    # ── resultado do robo ──
    def test_resultados_parciais_tentar_de_novo_e_retirar(self):
        cid = self._folha((1, 2, 3))
        self._confirmar(cid, [1, 2, 3])
        regras.registrar_execucao(self.conn, cid, 77, "ab" * 16)
        regras.aplicar_resultados(self.conn, cid, {"PS-1": {"ok": True, "mensagem": "expedido"},
                                                   "PS-2": {"ok": False, "mensagem": "situação inválida"}})
        c = regras.coleta(self.conn, cid)
        self.assertEqual((c["status"], c["execucao_id"]), ("CONFIRMADA", 77))
        self.assertEqual(self._estados(cid), {1: "EXPEDIDO", 2: "FALHOU", 3: "FALHOU"})
        self.assertIn("sem resultado", c["pedidos"][2]["mensagem"])
        self.assertIsNotNone(self.conn.execute(
            "SELECT saiu_em FROM expedicao_coletas_fila WHERE id_stokki = 1").fetchone()[0])
        self.assertEqual(regras.abertas(self.conn)[0]["n_falhas"], 2)
        self.assertEqual(regras.tentar_de_novo(self.conn, cid), ["PS-2", "PS-3"])
        self.assertTrue(regras.tem_foto(self.conn, cid, ["PS-2", "PS-3"]))
        regras.aplicar_resultados(self.conn, cid, {"PS-2": {"ok": True, "mensagem": "ok"},
                                                   "PS-3": {"ok": False, "mensagem": "pedido cancelado"}})
        with self.assertRaises(ErroColeta):
            regras.retirar_pedido(self.conn, cid, 1, "x")
        regras.retirar_pedido(self.conn, cid, 3, "hugo")
        self.assertEqual(regras.coleta(self.conn, cid)["status"], "CONCLUIDA")
        self.assertEqual(regras.abertas(self.conn), [])
        h = regras.historico(self.conn)[0]
        self.assertEqual((h["numero"], h["n_pedidos"], h["n_expedidos"]), ("COL-1", 3, 2))
        with self.assertRaises(ErroColeta):
            regras.tentar_de_novo(self.conn, cid)

    def test_assinatura_muda_a_cada_passo(self):
        vistas = [regras.assinatura(self.conn)]
        cid = self._folha()
        vistas.append(regras.assinatura(self.conn))
        self._confirmar(cid, [1])
        vistas.append(regras.assinatura(self.conn))
        regras.aplicar_resultados(self.conn, cid, {"PS-1": {"ok": True, "mensagem": ""}})
        vistas.append(regras.assinatura(self.conn))
        self.assertEqual(len(set(vistas)), 4)
        self.assertEqual(regras.assinatura(self.conn), vistas[-1])

    def test_coleta_do_pedido(self):
        cid = self._folha()
        self._confirmar(cid, [1])
        self.assertIsNone(regras.coleta_do_pedido(self.conn, "PS-1"))
        regras.aplicar_resultados(self.conn, cid, {"PS-1": {"ok": True, "mensagem": ""}}, simulada=True)
        c = regras.coleta_do_pedido(self.conn, "PS-1")
        self.assertEqual((c["numero"], c["motorista_nome"], c["placa"]), ("COL-1", "JOSE DA SILVA", "ABC1D23"))
        self.assertEqual(regras.coleta(self.conn, cid)["simulada"], 1)
        self.assertIsNone(regras.coleta_do_pedido(self.conn, "PS-2"))


if __name__ == "__main__":
    unittest.main()
