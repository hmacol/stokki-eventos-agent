# -*- coding: utf-8 -*-
"""py -3.11 -m unittest stokki.test_devolucao_estoque"""
import json
import sqlite3
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from stokki import devolucao_estoque as de
from stokki.test_ajuste_estoque import _Resp

T = datetime(2026, 11, 3, 12, 30)   # cancelamento


def mov(tipo, ref, q, h):
    return {"tipo": tipo, "movimento": "Interno", "referencia": ref, "quantidade": q,
            "quando": datetime(2026, 11, 3, h, 0)}


class TestRegras(unittest.TestCase):
    def test_bloqueado_soma_so_o_pedido(self):
        ms = [mov("Bloqueio", "#PS-43101", 4, 12), mov("Bloqueio", "#PE-2609", 20, 11),
              mov("Bloqueio", "#PS-43102", 6, 12)]
        self.assertEqual(de.bloqueado_pelo_pedido(ms, 43101), 4)

    def test_bloqueado_nao_confunde_prefixo(self):   # PS-4310 x PS-43101
        self.assertEqual(de.bloqueado_pelo_pedido([mov("Bloqueio", "#PS-43101", 4, 12)], 4310), 0)

    def test_bloqueado_tolera_espaco_e_sem_cerquilha(self):
        ms = [mov("Bloqueio", " PS-43101 ", 4, 12), mov("Bloqueio", "#PS-43101", 1, 12)]
        self.assertEqual(de.bloqueado_pelo_pedido(ms, 43101), 5)

    def test_sem_desbloqueio_nao_devolvido(self):
        self.assertEqual(de.ja_devolvido([mov("Bloqueio", "#PS-43101", 4, 12)], 4, T), "nao")

    def test_desbloqueio_igual_sem_referencia_depois_do_cancelamento(self):
        self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "", 4, 13)], 4, T), "sim")

    def test_desbloqueio_antes_do_cancelamento_nao_conta(self):
        self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "", 4, 11)], 4, T), "nao")

    def test_desbloqueio_de_outra_quantidade_e_duvida(self):
        self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "", 3, 13)], 4, T), "duvida")

    def test_desbloqueio_com_referencia_nao_conta(self):   # "#stkkadj-" e ajuste, nao devolucao
        self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "#stkkadj-1097", 4, 13)], 4, T), "nao")

    def test_quanto_mover(self):
        self.assertEqual((de.quanto_mover(4, 12), de.quanto_mover(4, 3), de.quanto_mover(4, 0)), (4, 3, 0))
        self.assertEqual(de.quanto_mover(-1, 5), 0)

    def test_destino_origem_primeiro(self):
        r = de.escolher_destinos(4, [("FABENNE-2", 4)], [{"endereco": "FABENNE-1", "estoque": 5}], [])
        self.assertEqual(r[0], ("FABENNE-2", 4))

    def test_destino_origem_em_dois_enderecos(self):
        r = de.escolher_destinos(4, [("A-1", 3), ("A-2", 1)], [], [])
        self.assertEqual(r[:2], [("A-1", 3), ("A-2", 1)])

    def test_destino_origem_parcial_resto_vai_pro_melhor_do_produto(self):
        r = de.escolher_destinos(4, [("A-1", 3)], [{"endereco": "B-1", "estoque": 9}], [])
        self.assertEqual(r[:2], [("A-1", 3), ("B-1", 1)])

    def test_destino_sem_origem_maior_saldo_do_produto(self):
        r = de.escolher_destinos(4, [], [{"endereco": "Virtual", "estoque": 28},
                                         {"endereco": "FABENNE-1", "estoque": 5},
                                         {"endereco": "FABENNE-3", "estoque": 9}], [])
        self.assertEqual(r[0], ("FABENNE-3", 4))

    def test_destino_sem_produto_maior_saldo_do_embarcador(self):
        r = de.escolher_destinos(4, [], [{"endereco": "Virtual", "estoque": 28}],
                                 [{"endereco": "Ajuste", "estoque": 99}, {"endereco": "FABENNE-1", "estoque": 40}])
        self.assertEqual(r, [("FABENNE-1", 4)])

    def test_destino_nenhum(self):
        self.assertEqual(de.escolher_destinos(4, [], [], [{"endereco": "VIRTUAL", "estoque": 9}]), [])

    def test_origem_de_sistema_e_ignorada(self):
        self.assertEqual(de.escolher_destinos(4, [("Sobra", 4)], [{"endereco": "X-1", "estoque": 1}], [])[0],
                         ("X-1", 4))

    def test_empate_ordem_alfabetica(self):
        r = de.escolher_destinos(4, [], [{"endereco": "B-1", "estoque": 5}, {"endereco": "A-1", "estoque": 5}], [])
        self.assertEqual(r[0], ("A-1", 4))

    def test_antigo(self):
        agora = datetime(2026, 11, 5, 12, 0)
        self.assertTrue(de.cancelamento_antigo(datetime(2026, 11, 3, 12, 0), agora, True))
        self.assertFalse(de.cancelamento_antigo(datetime(2026, 11, 5, 9, 0), agora, True))
        self.assertFalse(de.cancelamento_antigo(datetime(2026, 11, 1, 9, 0), agora, False))
        self.assertTrue(de.cancelamento_antigo(None, agora, True))

    def test_bloqueado_ignora_movimento_nao_interno(self):
        externo = dict(mov("Bloqueio", "#PS-43101", 4, 12), movimento="Externo")
        self.assertEqual(de.bloqueado_pelo_pedido([externo, mov("Bloqueio", "#PS-43101", 2, 12)], 43101), 2)

    def test_desbloqueio_nao_interno_e_ignorado(self):
        externo = dict(mov("Desbloqueio", "", 4, 13), movimento="Externo")
        self.assertEqual(de.ja_devolvido([externo], 4, T), "nao")

    def test_desbloqueio_no_mesmo_minuto_conta(self):
        m = dict(mov("Desbloqueio", "", 4, 12), quando=T)
        self.assertEqual(de.ja_devolvido([m], 4, T), "sim")

    def test_sem_data_de_cancelamento_e_duvida(self):
        self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "", 4, 13)], 4, None), "duvida")

    def test_linha_sem_data_conta_como_posterior(self):
        m = dict(mov("Desbloqueio", "", 4, 13), quando=None)
        self.assertEqual(de.ja_devolvido([m], 4, T), "sim")

    def test_quantidade_com_tolerancia(self):
        self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "", 0.1 + 0.2, 13)], 0.3, T), "sim")

    def test_endereco_vazio_descartado(self):
        self.assertEqual(de.escolher_destinos(4, [("", 4), ("  ", 4)], [{"endereco": "", "estoque": 5}], []), [])

    def test_origem_que_passa_de_qtd_e_cortada(self):
        r = de.escolher_destinos(4, [("A-1", 3), ("A-2", 3)], [], [])
        self.assertEqual(r, [("A-1", 3), ("A-2", 1)])

    def test_antigo_limite_exato_24h(self):
        self.assertTrue(de.cancelamento_antigo(datetime(2026, 11, 4, 12, 0), datetime(2026, 11, 5, 12, 0), True))


LIGADO = {"stokki": {"devolucao_cancelamento": {"ativo": True}}}


class TestFila(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def linhas(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM stokki_devolucoes")]

    def test_ligado_so_com_chave_verdadeira(self):
        self.assertFalse(de.ligado({}))
        self.assertFalse(de.ligado({"stokki": {}}))
        self.assertFalse(de.ligado({"stokki": {"devolucao_cancelamento": {"ativo": False}}}))
        self.assertTrue(de.ligado(LIGADO))

    def test_desligado_nao_grava_nem_cria_tabela(self):
        self.assertFalse(de.registrar(1, "portal", {}, conn=self.conn))
        existe = self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='stokki_devolucoes'").fetchone()
        self.assertIsNone(existe)

    def test_desligado_nao_abre_o_banco(self):
        with mock.patch("sqlite3.connect") as c:
            self.assertFalse(de.registrar(1, "portal", {}))
        c.assert_not_called()

    def test_ligado_grava_pendente_com_origem(self):
        self.assertTrue(de.registrar(43101, "pedidos", LIGADO, ja_estava=True, conn=self.conn))
        (l,) = self.linhas()
        self.assertEqual((l["id_stokki"], l["origem"], l["ja_estava"], l["status"], l["tentativas"]),
                         (43101, "pedidos", 1, "PENDENTE", 0))

    def test_segundo_registro_do_mesmo_id_e_ignorado(self):
        self.assertTrue(de.registrar(5, "portal", LIGADO, conn=self.conn))
        self.assertFalse(de.registrar(5, "pedidos", LIGADO, conn=self.conn))
        (l,) = self.linhas()
        self.assertEqual(l["origem"], "portal")

    def test_pedido_ja_feito_nao_volta_para_pendente(self):
        de.registrar(5, "portal", LIGADO, conn=self.conn)
        de.gravar(self.conn, 5, status="FEITA")
        self.assertFalse(de.registrar(5, "portal", LIGADO, conn=self.conn))
        self.assertEqual(self.linhas()[0]["status"], "FEITA")

    def test_conn_que_levanta_devolve_false(self):
        ruim = mock.Mock()
        ruim.execute.side_effect = sqlite3.OperationalError("database is locked")
        self.assertFalse(de.registrar(1, "portal", LIGADO, conn=ruim))

    def test_pendentes_e_gravar(self):
        de.registrar(1, "portal", LIGADO, conn=self.conn)
        de.registrar(2, "portal", LIGADO, conn=self.conn)
        de.gravar(self.conn, 2, status="NADA")
        de.gravar(self.conn, 1, itens=[{"sku": "Maca", "obs": "açaí"}], erro="x", mais_uma_tentativa=True)
        (p,) = de.pendentes(self.conn)
        self.assertEqual((p["id_stokki"], p["tentativas"], p["erro"]), (1, 1, "x"))
        self.assertEqual(p["itens"], [{"sku": "Maca", "obs": "açaí"}])
        self.assertIn("açaí", self.linhas()[0]["itens_json"])   # ensure_ascii=False

    def test_pendentes_sem_itens_devolve_lista_vazia(self):
        de.registrar(1, "portal", LIGADO, conn=self.conn)
        self.assertEqual(de.pendentes(self.conn)[0]["itens"], [])


# --- Leituras e plano, com as respostas REAIS de stokki/amostras_devolucao/ ---

AMOSTRAS = Path(__file__).parent / "amostras_devolucao"
AGORA = datetime(2026, 11, 12, 10, 0)
VAZIA = {"draw": 1, "iTotalRecords": 0, "iTotalDisplayRecords": 0, "aaData": []}
SEM_CANCELADO = ('<h3 class="timeline-header">Cancelado</h3>', '<h3 class="timeline-header">Aberto</h3>')


def _texto(nome):
    return (AMOSTRAS / nome).read_text(encoding="utf-8")


def _json(nome):
    return json.loads(_texto(nome))


class _Stokki:
    """Sessao falsa: responde por trecho da URL (e por produto/mes nas tabelas) e guarda os GETs."""
    def __init__(self, show=None, movimentos=None, origem=None, enderecos=None):
        self.show = show if show is not None else _texto("show_43101.html")
        self.movimentos = movimentos if movimentos is not None else {(2278, 11): _json("movimentos_2278_2026_11.json")}
        self.origem = origem if origem is not None else {(2278, 11): _json("origem_2278_2026_11.json"),
                                                         (2278, 10): _json("origem_2278_2026_10.json")}
        self.enderecos = enderecos if enderecos is not None else _texto("enderecos_2278.html")
        self.gets = []

    def get(self, url, params=None, headers=None, **kw):
        self.gets.append((url, params or {}))
        p = params or {}
        if "/outbound/show/43101" in url:
            return _Resp(self.show)
        if "/v2/moviment/table" in url:
            return _Resp(json_=self.movimentos.get((int(p["product_id"]), int(p["month"])), VAZIA))
        if "addressing/table/moviment/table" in url:
            return _Resp(json_=self.origem.get((int(p["product_id"]), int(p["month"])), VAZIA))
        if "/moviment/addressing/2278/1" in url:
            return _Resp(self.enderecos)
        raise AssertionError(f"GET inesperado: {url}")

    def urls(self, trecho):
        return [g for g in self.gets if trecho in g[0]]


def _sem_devolucoes(j):
    return {**j, "iTotalDisplayRecords": len([r for r in j["aaData"] if r["origin"] != "Virtual"]),
            "aaData": [r for r in j["aaData"] if r["origin"] != "Virtual"]}


class TestLeituras(unittest.TestCase):
    def test_ler_pedido(self):
        p = de.ler_pedido(_Stokki(), 43101)
        self.assertEqual(p["cliente_id"], 100)
        self.assertEqual(p["cancelado_em"], datetime(2026, 11, 3, 12, 12))
        self.assertEqual(p["separado_em"], datetime(2026, 11, 3, 11, 41))   # "Em separacao", a mais antiga
        self.assertEqual(p["itens"][0], {"product_id": 2278, "sku": "FABMLT", "nome": "FABENNE MERLOT"})
        self.assertEqual([i["product_id"] for i in p["itens"]], [2278, 2282])

    def test_ler_pedido_sem_cancelamento_no_historico(self):
        show = _texto("show_43101.html").replace(*SEM_CANCELADO)
        self.assertIsNone(de.ler_pedido(_Stokki(show=show), 43101)["cancelado_em"])

    def test_ler_pedido_pagina_estranha_levanta(self):
        with self.assertRaises(ValueError) as c:
            de.ler_pedido(_Stokki(show="<html>login</html>"), 43101)
        self.assertIn("43101", str(c.exception))

    def test_ler_movimentos(self):
        ms = de.ler_movimentos(_Stokki(), 2278, 2026, 11)
        self.assertIn({"tipo": "Bloqueio", "movimento": "Interno", "referencia": "#PS-43101", "quantidade": 4.0,
                       "quando": datetime(2026, 11, 3, 12, 12)}, ms)
        self.assertIn({"tipo": "Bloqueio", "movimento": "Interno", "referencia": "#PE-2609", "quantidade": 20.0,
                       "quando": datetime(2026, 11, 3, 11, 56)}, ms)
        self.assertIn({"tipo": "Desbloqueio", "movimento": "Interno", "referencia": "", "quantidade": 4.0,
                       "quando": datetime(2026, 11, 3, 12, 48)}, ms)
        self.assertEqual(de.bloqueado_pelo_pedido(ms, 43101), 4)

    def test_ler_movimentos_pede_mes_e_produto(self):
        s = _Stokki()
        de.ler_movimentos(s, 2278, 2026, 11)
        params = s.urls("v2/moviment/table")[0][1]
        self.assertEqual((params["product_id"], params["year"], params["month"], params["warehouse_id"]),
                         (2278, 2026, 11, 1))

    def test_ler_movimentos_sem_tabela_levanta_com_a_url(self):
        s = _Stokki(movimentos={(2278, 11): {"message": "Unauthenticated."}})
        with self.assertRaises(ValueError) as c:
            de.ler_movimentos(s, 2278, 2026, 11)
        self.assertIn("v2/moviment/table", str(c.exception))

    def test_erro_http_sobe(self):
        class Fora(_Stokki):
            def get(self, url, **kw):
                return _Resp(status=401)
        with self.assertRaises(RuntimeError):
            de.ler_movimentos(Fora(), 2278, 2026, 11)

    def test_ler_saldo_enderecos(self):
        saldo = de.ler_saldo_enderecos(_Stokki(), 2278)
        self.assertEqual(saldo, [{"endereco": "FABENNE-1", "estoque": 2.0, "bloqueado": 0.0, "disponivel": 2.0},
                                 {"endereco": "Virtual", "estoque": 31.0, "bloqueado": 23.0, "disponivel": 8.0}])

    def test_ler_saldo_enderecos_sem_tabela_levanta(self):
        with self.assertRaises(ValueError):
            de.ler_saldo_enderecos(_Stokki(enderecos="<html>login</html>"), 2278)

    def test_origem_real_da_separacao(self):
        origem = de.ler_origem(_Stokki(), 2278, datetime(2026, 11, 3, 11, 41), datetime(2026, 11, 3, 12, 12), 4)
        self.assertEqual(origem, [("FABENNE-1", 4.0)])

    def test_origem_soma_diferente_e_desconhecida(self):
        s = _Stokki()
        self.assertEqual(de.ler_origem(s, 2278, datetime(2026, 11, 3, 11, 41), datetime(2026, 11, 3, 12, 12), 5), [])

    def test_origem_fora_da_janela_ou_sem_janela(self):
        s = _Stokki()
        self.assertEqual(de.ler_origem(s, 2278, datetime(2026, 11, 5, 8, 0), datetime(2026, 11, 5, 9, 0), 4), [])
        n = len(s.gets)
        self.assertEqual(de.ler_origem(s, 2278, None, datetime(2026, 11, 3, 12, 12), 4), [])
        self.assertEqual(de.ler_origem(s, 2278, datetime(2026, 11, 3, 11, 41), None, 4), [])
        self.assertEqual(len(s.gets), n)   # sem janela nem le

    def test_origem_le_os_dois_meses_da_janela(self):
        s = _Stokki()
        self.assertEqual(de.ler_origem(s, 2278, datetime(2026, 10, 31, 23, 59), datetime(2026, 11, 1, 0, 10), 4), [])
        self.assertEqual(sorted(p["month"] for _, p in s.urls("addressing/table")), ["10", "11"])

    def test_origem_parametros_do_leia(self):
        s = _Stokki()
        de.ler_origem(s, 2278, datetime(2026, 11, 3, 11, 41), datetime(2026, 11, 3, 12, 12), 4)
        p = s.urls("addressing/table")[0][1]
        self.assertEqual((p["product_id"], p["provider_id"], p["month"], p["year"], p["origin_id"], p["destination_id"]),
                         (2278, 1, "11", 2026, "", ""))
        self.assertEqual(p["columns[4][data]"], "origin")

    def test_origem_duas_partes_agregadas_por_endereco(self):
        def linha(i, o, q, h):
            return {"id": f"#A-{i}", "created": f'<div>03/11/2026<br><span class="text-muted">{h}</span></div>',
                    "concluded": "x 03/11/2026 11:44", "user": "Carla", "origin": o, "destination": "Virtual",
                    "quantity": f'<h6><span class="badge badge-success">{q}</span></h6>'}
        origem = {(2278, 11): {"draw": 1, "iTotalDisplayRecords": 3, "aaData": [
            linha(1, "A-1", 3, "11:44"), linha(2, "B-1", 1, "11:44"), linha(3, "A-1", 1, "11:45")]}}
        r = de.ler_origem(_Stokki(origem=origem), 2278, datetime(2026, 11, 3, 11, 41), datetime(2026, 11, 3, 12, 12), 5)
        self.assertEqual(sorted(r), [("A-1", 4.0), ("B-1", 1.0)])


class TestPlano(unittest.TestCase):
    def _sem_devolucao(self, origem=None, **kw):
        """Amostra real intacta (movimentacoes com o Desbloqueio de 20 das 16:52 do recebimento), mas sem
        as linhas `Virtual -> endereco` dos movimentos entre enderecos: ninguem devolveu ainda."""
        if origem is None:
            origem = {(2278, 11): _sem_devolucoes(_json("origem_2278_2026_11.json")),
                      (2278, 10): _sem_devolucoes(_json("origem_2278_2026_10.json"))}
        return _Stokki(origem=origem, **kw)

    def test_amostra_real_ja_foi_devolvido(self):
        # na amostra a linha #A-110832 (Virtual -> FABENNE-1, 4, 12:48) vem DEPOIS do cancelamento (12:12)
        s = _Stokki()
        p = de.montar_plano(s, 43101, AGORA)
        self.assertEqual(p["status"], "JA_DEVOLVIDO")
        self.assertEqual(p["cliente_id"], 100)
        item = p["itens"][0]
        self.assertEqual((item["product_id"], item["sku"], item["qtd"], item["situacao"], item["destinos"]),
                         (2278, "FABMLT", 4, "ja_devolvido", []))
        self.assertEqual(p["itens"][1]["situacao"], "nada")   # 2282 sem movimentos na amostra
        self.assertEqual(s.urls("/moviment/addressing/2278/1"), [])   # ja devolvido: nem le o saldo

    def test_mover_com_origem_da_separacao(self):
        s = self._sem_devolucao()
        with mock.patch("stokki.ajuste_estoque.saldo_por_endereco", side_effect=AssertionError("regra 3 sem precisar")):
            p = de.montar_plano(s, 43101, AGORA)
        self.assertEqual(p["status"], "MOVER")
        item = p["itens"][0]
        self.assertEqual((item["situacao"], item["qtd"]), ("mover", 4))
        self.assertEqual(item["destinos"][0], ("FABENNE-1", 4))   # veio da origem (#A-110830)

    def test_mover_sem_origem_vai_pro_maior_saldo_fisico_do_produto(self):
        s = self._sem_devolucao(origem={})
        with mock.patch("stokki.ajuste_estoque.saldo_por_endereco", side_effect=AssertionError("regra 3 sem precisar")):
            p = de.montar_plano(s, 43101, AGORA)
        self.assertEqual(p["status"], "MOVER")
        self.assertEqual(p["itens"][0]["destinos"], [("FABENNE-1", 4)])   # Virtual nunca e destino

    def test_regra_3_so_quando_precisa(self):
        so_virtual = _texto("enderecos_2278.html").replace("FABENNE-1", "Falta")
        s = self._sem_devolucao(origem={}, enderecos=so_virtual)
        linhas = [{"endereco": "CONG1-1", "product_id": 9, "estoque": 7, "comprometido": 0, "disponivel": 7},
                  {"endereco": "CONG1-1", "product_id": 8, "estoque": 1, "comprometido": 0, "disponivel": 1},
                  {"endereco": "Virtual", "product_id": 9, "estoque": 50, "comprometido": 50, "disponivel": 0}]
        with mock.patch("stokki.ajuste_estoque.saldo_por_endereco", return_value=linhas) as m:
            p = de.montar_plano(s, 43101, AGORA)
        m.assert_called_once_with(s, 100)
        self.assertEqual(p["itens"][0]["destinos"], [("CONG1-1", 4)])

    def test_sem_nenhum_destino_vira_duvida_com_motivo(self):
        so_virtual = _texto("enderecos_2278.html").replace("FABENNE-1", "Falta")
        s = self._sem_devolucao(origem={}, enderecos=so_virtual)
        with mock.patch("stokki.ajuste_estoque.saldo_por_endereco", return_value=[]):
            p = de.montar_plano(s, 43101, AGORA)
        self.assertEqual(p["status"], "DUVIDA")
        self.assertEqual(p["itens"][0]["situacao"], "sem_destino")
        self.assertIn("FABMLT", p["motivo"])

    def test_sem_bloqueio_do_pedido_e_nada(self):
        p = de.montar_plano(_Stokki(movimentos={}), 43101, AGORA)
        self.assertEqual(p["status"], "NADA")
        self.assertEqual([i["situacao"] for i in p["itens"]], ["nada", "nada"])

    def test_desbloqueio_de_outra_quantidade_torna_o_plano_duvida(self):
        j = _json("origem_2278_2026_11.json")
        for r in j["aaData"]:
            if r["id"] == "#A-110832":
                r["quantity"] = '<h6><span class="badge badge-success">5</span></h6>'
        p = de.montar_plano(_Stokki(origem={(2278, 11): j}), 43101, AGORA)
        self.assertEqual(p["status"], "DUVIDA")
        self.assertEqual(p["itens"][0]["situacao"], "duvida")
        self.assertIn("nao fecha com a quantidade do pedido", p["motivo"])
        self.assertIn("produto FABMLT", p["motivo"])

    def test_antigo_nao_le_movimentos(self):
        s = _Stokki()
        p = de.montar_plano(s, 43101, AGORA, ja_estava=True)
        self.assertEqual(p["status"], "ANTIGO")
        self.assertEqual(len(s.gets), 1)   # so o show do pedido

    def test_recente_que_ja_estava_segue_o_fluxo(self):
        p = de.montar_plano(_Stokki(), 43101, datetime(2026, 11, 3, 20, 0), ja_estava=True)
        self.assertEqual(p["status"], "JA_DEVOLVIDO")

    def test_data_ilegivel_sem_ja_estava_e_duvida_sem_ler_movimentos(self):
        s = _Stokki(show=_texto("show_43101.html").replace(*SEM_CANCELADO))
        p = de.montar_plano(s, 43101, AGORA)
        self.assertEqual(p["status"], "DUVIDA")
        self.assertEqual(len(s.gets), 1)

    def test_data_ilegivel_com_ja_estava_e_antigo(self):
        s = _Stokki(show=_texto("show_43101.html").replace(*SEM_CANCELADO))
        self.assertEqual(de.montar_plano(s, 43101, AGORA, ja_estava=True)["status"], "ANTIGO")

    def test_erro_de_leitura_sobe_em_vez_de_virar_nada(self):
        s = _Stokki(movimentos={(2278, 11): {"message": "Unauthenticated."}})
        with self.assertRaises(ValueError):
            de.montar_plano(s, 43101, AGORA)

    def test_meses_dos_movimentos_limita_a_3_para_tras(self):
        self.assertEqual(de._meses_dos_movimentos(datetime(2026, 11, 3), datetime(2026, 11, 12)), [(2026, 11)])
        self.assertEqual(de._meses_dos_movimentos(datetime(2026, 9, 28), datetime(2026, 11, 12)),
                         [(2026, 9), (2026, 10), (2026, 11)])
        self.assertEqual(de._meses_dos_movimentos(datetime(2026, 1, 5), datetime(2026, 11, 12)),
                         [(2026, 8), (2026, 9), (2026, 10), (2026, 11)])


class _Paginada:
    """Tabela DataTables falsa: `paginas` = lista de listas de linhas, uma por chamada; `total` informado."""
    def __init__(self, paginas, total, ignora_start=False):
        self.paginas, self.total, self.ignora_start, self.starts = paginas, total, ignora_start, []

    def get(self, url, params=None, headers=None, **kw):
        self.starts.append(params["start"])
        i = 0 if self.ignora_start else len(self.starts) - 1
        rows = self.paginas[i] if i < len(self.paginas) else []
        return _Resp(json_={"draw": 1, "iTotalDisplayRecords": self.total, "aaData": rows})


def _linha_mov(n):
    return {"date": f"03/11/2026 10:{n:02d}", "conclusion": f"03/11/2026 10:{n:02d}", "reference": "",
            "type": "Desbloqueio", "motion": "Interno", "quantity": str(n)}


class TestCorrecoesRevisao(unittest.TestCase):
    # C1: produto repetido no pedido
    def test_produto_repetido_no_pedido_vira_um_item_so(self):
        show = _texto("show_43101.html").replace("#ITM-2282", "#ITM-2278")
        self.assertEqual([i["product_id"] for i in de.ler_pedido(_Stokki(show=show), 43101)["itens"]], [2278])

    def test_produto_repetido_nao_dobra_o_plano(self):
        show = _texto("show_43101.html").replace("#ITM-2282", "#ITM-2278")
        s = TestPlano()._sem_devolucao(show=show)
        p = de.montar_plano(s, 43101, AGORA)
        self.assertEqual([(i["product_id"], i["qtd"], i["destinos"]) for i in p["itens"]],
                         [(2278, 4, [("FABENNE-1", 4)])])
        self.assertEqual(len(s.urls("/v2/moviment/table")), 1)

    # I1: cancelamento fora da janela de leitura
    def test_cancelamento_fora_da_janela_e_duvida_sem_ler_movimentos(self):
        s = _Stokki()
        p = de.montar_plano(s, 43101, datetime(2027, 3, 10, 10, 0))
        self.assertEqual(p["status"], "DUVIDA")
        self.assertIn("antigo demais", p["motivo"])
        self.assertEqual(len(s.gets), 1)

    # I2: coluna faltando nunca vira "nada"
    def test_movimentos_com_linha_sem_coluna_levanta_com_a_url(self):
        j = _json("movimentos_2278_2026_11.json")
        del j["aaData"][0]["motion"]
        with self.assertRaises(ValueError) as c:
            de.ler_movimentos(_Stokki(movimentos={(2278, 11): j}), 2278, 2026, 11)
        self.assertIn("v2/moviment/table", str(c.exception))

    def test_origem_com_linha_sem_coluna_levanta_com_a_url(self):
        j = _json("origem_2278_2026_11.json")
        del j["aaData"][0]["origin"]
        with self.assertRaises(ValueError) as c:
            de.ler_origem(_Stokki(origem={(2278, 11): j}), 2278, datetime(2026, 11, 3, 11, 41),
                          datetime(2026, 11, 3, 12, 12), 4)
        self.assertIn("addressing/table", str(c.exception))

    def test_devolucoes_com_linha_sem_coluna_levanta(self):
        j = _json("origem_2278_2026_11.json")
        del j["aaData"][0]["concluded"]
        with self.assertRaises(ValueError):
            de.ler_devolucoes(_Stokki(origem={(2278, 11): j}), 2278, datetime(2026, 11, 3, 12, 12), AGORA)

    # I3: paginacao
    def test_paginacao_duas_paginas(self):
        s = _Paginada([[_linha_mov(1), _linha_mov(2)], [_linha_mov(3)]], total=3)
        rows = de._linhas_tabela(s, "http://x/tabela", {}, "r", ("type",))
        self.assertEqual(len(rows), 3)
        self.assertEqual(s.starts, [0, 2])

    def test_paginacao_servidor_que_ignora_start_levanta(self):
        s = _Paginada([[_linha_mov(1), _linha_mov(2)]], total=4, ignora_start=True)
        with self.assertRaises(ValueError) as c:
            de._linhas_tabela(s, "http://x/tabela", {}, "r")
        self.assertIn("http://x/tabela", str(c.exception))

    def test_paginacao_total_que_nao_fecha_levanta(self):
        s = _Paginada([[_linha_mov(1), _linha_mov(2), _linha_mov(3)]], total=5)
        with self.assertRaises(ValueError) as c:
            de._linhas_tabela(s, "http://x/tabela", {}, "r")
        self.assertIn("http://x/tabela", str(c.exception))

    def test_paginacao_linhas_iguais_na_mesma_pagina_sao_validas(self):
        s = _Paginada([[_linha_mov(1), _linha_mov(1)]], total=2)
        self.assertEqual(len(de._linhas_tabela(s, "http://x/tabela", {}, "r")), 2)

    # I4: prova de devolucao pelos movimentos entre enderecos
    def test_ler_devolucoes_real(self):
        r = de.ler_devolucoes(_Stokki(), 2278, datetime(2026, 11, 3, 12, 12), AGORA)
        self.assertEqual(r, [{"tipo": "Desbloqueio", "movimento": "Interno", "referencia": "", "quantidade": 4.0,
                              "quando": datetime(2026, 11, 3, 12, 48), "concluido": True, "id": 110832}])

    def test_ler_devolucoes_limites_de_horario(self):
        s = _Stokki()
        self.assertEqual(len(de.ler_devolucoes(s, 2278, datetime(2026, 11, 3, 12, 48), AGORA)), 1)   # mesmo minuto
        self.assertEqual(de.ler_devolucoes(s, 2278, datetime(2026, 11, 3, 12, 49), AGORA), [])

    def test_ler_devolucoes_ignora_a_separacao_fisico_para_virtual(self):
        r = de.ler_devolucoes(_Stokki(), 2278, datetime(2026, 11, 3, 11, 0), datetime(2026, 11, 3, 12, 0))
        self.assertEqual(r, [])   # #A-110830 (FABENNE-1 -> Virtual, 11:44) nao e devolucao

    def test_ler_devolucoes_nao_concluida(self):
        j = _json("origem_2278_2026_11.json")
        for r in j["aaData"]:
            if r["id"] == "#A-110832":
                r["concluded"] = ""
        r = de.ler_devolucoes(_Stokki(origem={(2278, 11): j}), 2278, datetime(2026, 11, 3, 12, 12), AGORA)
        self.assertIs(r[0]["concluido"], False)

    def test_desbloqueio_do_recebimento_nao_causa_duvida_com_a_amostra_real(self):
        # movimentacoes REAIS intactas: o Desbloqueio de 20 as 16:52 (par do #PE-2609) e o de 100 (09/11)
        # nao entram; sem linha Virtual -> endereco o plano e MOVER, nao DUVIDA
        p = de.montar_plano(TestPlano()._sem_devolucao(), 43101, AGORA)
        self.assertEqual(p["status"], "MOVER")

    def test_devolucao_em_partes_somando_a_qtd_e_sim(self):
        ms = [{"tipo": "Desbloqueio", "movimento": "Interno", "referencia": "", "quantidade": q,
               "quando": datetime(2026, 11, 3, 13, 0), "concluido": True} for q in (1, 3)]
        self.assertEqual(de.ja_devolvido(ms, 4, T), "sim")

    def test_partes_que_nao_fecham_sao_duvida(self):
        ms = [{"tipo": "Desbloqueio", "movimento": "Interno", "referencia": "", "quantidade": q,
               "quando": datetime(2026, 11, 3, 13, 0)} for q in (1, 2)]
        self.assertEqual(de.ja_devolvido(ms, 4, T), "duvida")

    def test_linha_nao_concluida_nunca_da_sim(self):
        base = {"tipo": "Desbloqueio", "movimento": "Interno", "referencia": "", "quando": datetime(2026, 11, 3, 13, 0)}
        self.assertEqual(de.ja_devolvido([{**base, "quantidade": 4, "concluido": False}], 4, T), "duvida")
        self.assertEqual(de.ja_devolvido([{**base, "quantidade": 1, "concluido": True},
                                          {**base, "quantidade": 3, "concluido": False}], 4, T), "duvida")
        self.assertEqual(de.ja_devolvido([{**base, "quantidade": 4, "concluido": True}], 4, T), "sim")

    def test_plano_com_devolucao_nao_concluida_e_duvida(self):
        j = _json("origem_2278_2026_11.json")
        for r in j["aaData"]:
            if r["id"] == "#A-110832":
                r["concluded"] = ""
        p = de.montar_plano(_Stokki(origem={(2278, 11): j}), 43101, AGORA)
        self.assertEqual(p["status"], "DUVIDA")

    # M1 / M2
    def test_importar_o_modulo_nao_carrega_ajuste_estoque_nem_bs4(self):
        import subprocess
        import sys
        raiz = Path(__file__).resolve().parent.parent
        codigo = ("import sys; from stokki import devolucao_estoque as d; "
                  "print('stokki.ajuste_estoque' in sys.modules, 'bs4' in sys.modules)")
        r = subprocess.run([sys.executable, "-c", codigo], cwd=raiz, capture_output=True, text=True)
        self.assertEqual(r.stdout.split(), ["False", "False"], r.stderr)

    def test_recebimento_nunca_e_destino(self):
        saldo = [{"endereco": "Recebimento", "estoque": 99}, {"endereco": "A-1", "estoque": 1}]
        self.assertEqual(de.escolher_destinos(4, [("Recebimento", 4)], saldo, []), [("A-1", 4)])


def _origem_com(**mudancas):
    """Amostra real de origem com a linha #A-110832 alterada (campo -> valor)."""
    j = _json("origem_2278_2026_11.json")
    for r in j["aaData"]:
        if r["id"] == "#A-110832":
            r.update(mudancas)
    return {(2278, 11): j}


class TestRodada2(unittest.TestCase):
    DESDE = datetime(2026, 11, 3, 12, 12)

    # N1: data ou id ilegivel nunca vira plano
    def test_criacao_ilegivel_levanta_em_ler_devolucoes(self):
        s = _Stokki(origem=_origem_com(created="<div>ontem</div>"))
        with self.assertRaises(ValueError) as c:
            de.ler_devolucoes(s, 2278, self.DESDE, AGORA)
        self.assertIn("addressing/table", str(c.exception))

    def test_criacao_ilegivel_nao_vira_mover_no_plano(self):
        with self.assertRaises(ValueError):
            de.montar_plano(_Stokki(origem=_origem_com(created="<div>ontem</div>")), 43101, AGORA)

    def test_criacao_ilegivel_levanta_em_ler_origem(self):
        s = _Stokki(origem=_origem_com(created=""))
        with self.assertRaises(ValueError):
            de.ler_origem(s, 2278, datetime(2026, 11, 3, 11, 41), datetime(2026, 11, 3, 12, 12), 4)

    def test_id_ilegivel_levanta(self):
        with self.assertRaises(ValueError) as c:
            de.ler_devolucoes(_Stokki(origem=_origem_com(id="sem numero")), 2278, self.DESDE, AGORA)
        self.assertIn("addressing/table", str(c.exception))

    # N2: devolucoes do proprio robo (outros pedidos) nao contam
    def test_ler_devolucoes_ignora_ids(self):
        s = _Stokki()
        self.assertEqual(de.ler_devolucoes(s, 2278, self.DESDE, AGORA, ignorar={110832}), [])
        self.assertEqual(de.ler_devolucoes(s, 2278, self.DESDE, AGORA, ignorar=frozenset())[0]["id"], 110832)

    def test_plano_com_o_movimento_ignorado_volta_a_mover(self):
        s = _Stokki()
        self.assertEqual(de.montar_plano(s, 43101, AGORA)["status"], "JA_DEVOLVIDO")
        p = de.montar_plano(_Stokki(), 43101, AGORA, ignorar_movimentos=frozenset({110832}))
        self.assertEqual(p["status"], "MOVER")
        self.assertEqual(p["itens"][0]["destinos"][0], ("FABENNE-1", 4))

    # N3: o motivo diz o que foi visto
    def test_motivo_quando_o_movimento_nao_fecha(self):
        p = de.montar_plano(_Stokki(origem=_origem_com(quantity="5")), 43101, AGORA)
        self.assertEqual(p["status"], "DUVIDA")
        self.assertIn("nao fecha com a quantidade do pedido", p["motivo"])
        self.assertIn("produto FABMLT", p["motivo"])

    def test_motivo_quando_a_devolucao_nao_concluiu(self):
        p = de.montar_plano(_Stokki(origem=_origem_com(concluded="")), 43101, AGORA)
        self.assertEqual(p["status"], "DUVIDA")
        self.assertIn("ainda em andamento", p["motivo"])
        self.assertIn("produto FABMLT", p["motivo"])


if __name__ == "__main__":
    unittest.main()
