# -*- coding: utf-8 -*-
"""Indicação de lote pelo cliente no envio de pedidos do portal (18/11/2026).

    py -3.11 -m unittest portal_cliente.test_lotes_cliente -v
"""
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import openpyxl  # noqa: E402

import envio_pedidos as ep  # noqa: E402
import lotes_cliente as lc  # noqa: E402
from test_anexos_pedido import Base  # noqa: E402
from test_endereco_entrega_api import CLIENTE, CNPJ, _importar_app  # noqa: E402
from test_endereco_entrega_envio import _xml_nf  # noqa: E402

PILOTOS = {"wms": {"pilotos": [{"id": 48, "nome": "MARIA DOLORES", "caixas": False}]}}
DET = ("<det nItem=\"1\"><prod><cProd>MNCXDCHF300</cProd><xProd>MINI CX DADINHO 300G</xProd><uCom>Caixa</uCom>"
       "<qCom>6.0000</qCom></prod></det><det nItem=\"2\"><prod><cProd>MNDDTP</cProd><xProd>DADINHO TP</xProd>"
       "<uCom>UN</uCom><qCom>30.0000</qCom></prod></det>")


def xml_com_produtos(n: int) -> bytes:
    return _xml_nf(n).replace(b"<transp>", DET.encode() + b"<transp>")


def planilha_lotes(linhas, lotes=("247", "254", "FIFO"), aba="Lotes") -> bytes:
    """Modelo da Maria Dolores: Nome do contato | Nota Fiscal | Descrição | Código (SKU) | lotes... | Total Geral."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = aba
    ws.append(["Nome do contato", "Nota Fiscal", "Descrição", "Código (SKU)"] + [int(l) if l.isdigit() else l for l in lotes]
              + ["Total Geral"])
    for cliente, nf, sku, qtds in linhas:
        ws.append([cliente, nf, "DESC", sku] + list(qtds) + [sum(q or 0 for q in qtds)])
    ws.append(["Total Geral"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class BaseLotes(Base):
    def _conn(self):
        conn = super()._conn()
        conn.execute("CREATE TABLE IF NOT EXISTS interno (cnpj_embarcador TEXT, apelido TEXT, nome_remetente TEXT, stkkc_id INTEGER, "
                     "sender_id INTEGER, email TEXT)")
        if not conn.execute("SELECT 1 FROM interno WHERE cnpj_embarcador = ?", (CNPJ,)).fetchone():
            conn.execute("INSERT INTO interno (cnpj_embarcador, apelido, stkkc_id, sender_id) VALUES (?, 'MD', 48, 22812827)", (CNPJ,))
        conn.execute("CREATE TABLE IF NOT EXISTS wms_produtos (id INTEGER PRIMARY KEY, sku TEXT, embarcador_id INTEGER, ativo INTEGER)")
        conn.executemany("INSERT OR IGNORE INTO wms_produtos VALUES (?, ?, 48, 1)", [(1, "MNCXDCHF300"), (2, "MNDDTP")])
        conn.execute("CREATE TABLE IF NOT EXISTS wms_caixas (serial TEXT, produto_id INTEGER, lote TEXT, validade TEXT, situacao TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS wms_pedidos (id INTEGER PRIMARY KEY, codigo_ps TEXT, nf TEXT, embarcador_id TEXT, "
                     "execucao TEXT, estado_reserva TEXT)")
        conn.commit()
        return conn

    def indicacoes(self, conn):
        return [tuple(r) for r in conn.execute(
            "SELECT nf, sku, lote, qtd_un, inteiro FROM wms_lotes_cliente ORDER BY nf, sku, lote").fetchall()]


class Leitura(BaseLotes):
    def test_xml_traz_os_produtos_da_nota(self):
        nfe = ep.ler_nfe(xml_com_produtos(42430))
        self.assertEqual(nfe["produtos"], [
            {"sku": "MNCXDCHF300", "descricao": "MINI CX DADINHO 300G", "quantidade": 6.0, "unidade": "Caixa"},
            {"sku": "MNDDTP", "descricao": "DADINHO TP", "quantidade": 30.0, "unidade": "UN"}])

    def test_so_piloto_do_wms_indica_lote(self):
        conn = self._conn()
        self.assertEqual(lc.embarcador_wms(conn, "22.135.070/0001-90", PILOTOS), 48)
        self.assertIsNone(lc.embarcador_wms(conn, CNPJ, {"wms": {"pilotos": [{"id": 22, "nome": "MOMBAK"}]}}))
        self.assertIsNone(lc.embarcador_wms(conn, "11111111000111", PILOTOS))

    def test_reconhece_a_planilha_de_lotes_e_deixa_passar_a_de_pedidos(self):
        lido = lc.ler_planilha_de_lotes("LOTES.xlsx", planilha_lotes([("DUFRY", 42430, "MNCXDCHF300", (6, None, None))]))
        self.assertEqual([(l["nf"], l["sku"], l["lote"], l["qtd_un"]) for l in lido["linhas"]], [("42430", "MNCXDCHF300", "247", 6)])
        # pela aba, mesmo com outro nome de arquivo
        self.assertIsNotNone(lc.ler_planilha_de_lotes("x.xlsx", planilha_lotes([("A", 1, "MNDDTP", (1, None, None))])))
        # planilha de pedidos com NF e SKU seguidos de Quantidade: não é de lotes
        pedidos = planilha_lotes([("A", 1, "MNDDTP", (3, 10))], lotes=("Quantidade", "Valor"), aba="Pedidos")
        self.assertIsNone(lc.ler_planilha_de_lotes("pedidos.xlsx", pedidos))
        with self.assertRaisesRegex(lc.ErroLote, "uma coluna por lote"):
            lc.ler_planilha_de_lotes("lotes da semana.xlsx", pedidos)
        sem_tabela = openpyxl.Workbook()
        sem_tabela.active.append(["Pedido", "SKU", "Quantidade"])
        buf = io.BytesIO()
        sem_tabela.save(buf)
        self.assertIsNone(lc.ler_planilha_de_lotes("pedidos.xlsx", buf.getvalue()))
        with self.assertRaisesRegex(lc.ErroLote, "Nota Fiscal"):
            lc.ler_planilha_de_lotes("LOTES.xlsx", buf.getvalue())


class Gravacao(BaseLotes):
    def test_planilha_grava_e_avisa(self):
        conn = self._conn()
        conn.execute("INSERT INTO wms_pedidos VALUES (1, 'PS-1', '42430', '48', '', 'RESERVADO')")
        conn.execute("INSERT INTO wms_pedidos VALUES (2, 'PS-2', '42431', '48', 'CONFERIDO', 'RESERVADO')")
        conn.execute("INSERT INTO wms_caixas VALUES ('C1', 2, '300', '2099-01-01', 'EM_ESTOQUE')")
        conn.commit()
        lido = lc.ler_planilha_de_lotes("LOTES.xlsx", planilha_lotes([
            ("A", 42430, "MNCXDCHF300", (6, None, None)), ("B", 42431, "MNDDTP", (None, 30, None)),
            ("C", 50000, "MNDDTP", (1, None, None)), ("D", 50001, "OUTRO", (1, None, None))]))
        r = lc.importar_planilha(conn, 48, lido, "LOTES.xlsx", "cliente")
        self.assertEqual([(l["nf"], l["codigo_ps"], l["situacao"]) for l in r["linhas"]],
                         [("42430", "PS-1", "ok"), ("42431", "PS-2", "chegou_tarde"), ("50000", "", "aguardando_pedido"),
                          ("50001", "", "produto_desconhecido")])
        self.assertTrue(any("lote 247 de MNDDTP" in a for a in r["avisos"]), r["avisos"])   # há caixa do produto, não desse lote
        self.assertFalse(any("MNCXDCHF300" in a for a in r["avisos"]))                       # produto sem caixa: não avisa
        self.assertEqual(len(self.indicacoes(conn)), 4)

    def test_lote_da_previa_vale_pro_produto_inteiro_por_cima_da_planilha(self):
        conn = self._conn()
        produtos = ep.ler_nfe(xml_com_produtos(42430))["produtos"]
        lc.importar_planilha(conn, 48, lc.ler_planilha_de_lotes("LOTES.xlsx", planilha_lotes(
            [("A", 42430, "MNCXDCHF300", (6, None, None)), (None, None, "MNDDTP", (None, 30, None))])), "LOTES.xlsx", "x")
        r = lc.gravar_lotes_da_nota(conn, 48, "042430", {"mncxdchf300": " l9 ", "MNDDTP": ""}, produtos, "cliente")
        self.assertEqual(r, {"gravados": 1, "avisos": []})
        self.assertEqual(self.indicacoes(conn), [("42430", "MNCXDCHF300", "L9", 0.0, 1), ("42430", "MNDDTP", "254", 30.0, 0)])

    def test_lote_da_previa_recusa_produto_fora_da_nota_lote_estranho_e_pedido_separado(self):
        conn = self._conn()
        produtos = ep.ler_nfe(xml_com_produtos(42430))["produtos"]
        with self.assertRaisesRegex(lc.ErroLote, "não está nesta nota"):
            lc.gravar_lotes_da_nota(conn, 48, "42430", {"OUTRO": "1"}, produtos, "c")
        with self.assertRaisesRegex(lc.ErroLote, "Lote invalido"):
            lc.gravar_lotes_da_nota(conn, 48, "42430", {"MNDDTP": "<script>"}, produtos, "c")
        conn.execute("INSERT INTO wms_pedidos VALUES (1, 'PS-1', '42430', '48', 'EMBALADO', 'RESERVADO')")
        conn.commit()
        with self.assertRaisesRegex(lc.ErroLote, "já foi separado"):
            lc.gravar_lotes_da_nota(conn, 48, "42430", {"MNDDTP": "1"}, produtos, "c")
        lc.planilha.garantir_tabela(conn)
        self.assertEqual(self.indicacoes(conn), [])


class Rotas(BaseLotes):
    def setUp(self):
        super().setUp()
        self.app = app = _importar_app()
        patches = [
            mock.patch.object(ep, "validar_item", return_value={"ok": True, "erros": [], "avisos": [], "envio_existente": None}),
            mock.patch.object(ep, "config_stokki_cliente", return_value={"regra_xml": "nenhuma", "envio_ativo": True, "sender_id": 1,
                                                                        "client_id": 48}),
            mock.patch.object(ep, "fluxos_para_envio", return_value=(["armazenagem"], {"erro": None})),
            mock.patch.object(ep, "info_destinatarios", return_value={}),
            mock.patch.object(ep, "_tipo_transportadora", return_value=None),
            mock.patch.object(ep, "classificar_area_envio", return_value=None),
            mock.patch.object(ep, "avisos_fora_dia_fixo", return_value=[]),
            mock.patch.object(app.auth, "sessao_valida", lambda c, cnpj, v: dict(CLIENTE)),
            mock.patch.object(app, "_CONFIG", PILOTOS),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        app.app.config["TESTING"] = True
        self.tc = app.app.test_client()
        with self.tc.session_transaction() as s:
            s["cnpj"] = CNPJ
            s["v"] = "x"
        conn = self._conn()
        conn.execute("INSERT INTO wms_caixas VALUES ('C1', 1, '247', '2099-01-01', 'EM_ESTOQUE')")
        conn.execute("INSERT INTO wms_caixas VALUES ('C2', 1, '240', '2000-01-01', 'EM_ESTOQUE')")   # vencida: não sugere
        conn.commit()

    def _analisar(self, arquivos):
        r = self.tc.post("/api/envios/analisar", data={"arquivos": arquivos}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def test_previa_traz_produtos_e_lotes_e_a_planilha_de_lotes_e_gravada(self):
        j = self._analisar([(io.BytesIO(xml_com_produtos(42430)), "nf.xml"),
                            (io.BytesIO(planilha_lotes([("A", 42430, "MNDDTP", (None, 30, None))])), "LOTES.xlsx")])
        self.assertEqual(j["rejeitados"], [])
        it = j["itens"][0]
        self.assertTrue(it["lotes_ativo"])
        self.assertEqual([p["sku"] for p in it["produtos"]], ["MNCXDCHF300", "MNDDTP"])
        self.assertEqual(it["lotes_estoque"], {"MNCXDCHF300": [{"lote": "247", "validade": "2099-01-01"}]})
        self.assertEqual([(r["arquivo"], r["gravadas"]) for r in j["lotes_planilhas"]], [("LOTES.xlsx", 1)])
        self.assertEqual(self.indicacoes(self._conn()), [("42430", "MNDDTP", "254", 30.0, 0)])

    def test_so_a_planilha_de_lotes_sem_pedido(self):
        j = self._analisar([(io.BytesIO(planilha_lotes([("A", 42430, "MNDDTP", (1, None, None))])), "LOTES.xlsx")])
        self.assertEqual((j["itens"], j["rejeitados"], j["lotes_planilhas"][0]["gravadas"]), ([], [], 1))

    def test_cliente_fora_do_piloto_nao_ve_lote_e_a_planilha_segue_como_de_pedidos(self):
        with mock.patch.object(self.app, "_CONFIG", {"wms": {"pilotos": [{"id": 22, "nome": "MOMBAK"}]}}):
            j = self._analisar([(io.BytesIO(xml_com_produtos(42430)), "nf.xml"),
                                (io.BytesIO(planilha_lotes([("A", 42430, "MNDDTP", (1, None, None))])), "LOTES.xlsx")])
        self.assertFalse(j["itens"][0]["lotes_ativo"])
        self.assertEqual(j["lotes_planilhas"], [])
        self.assertNotIn("lotes_estoque", j["itens"][0])

    def _confirmar(self, lotes):
        j = self._analisar([(io.BytesIO(xml_com_produtos(42430)), "nf.xml")])
        item = {"token": j["itens"][0]["token"], "horario_inicio": "08:00", "horario_fim": "12:00", "lotes": lotes}
        return self.tc.post("/api/envios/confirmar", json={"itens": [item]})

    def test_confirmar_grava_o_lote_marcado(self):
        r = self._confirmar({"MNCXDCHF300": "247"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["lotes_gravados"], 1)
        self.assertIn("Lote indicado em 1 produto", r.get_json()["mensagem"])
        self.assertEqual(self.indicacoes(self._conn()), [("42430", "MNCXDCHF300", "247", 0.0, 1)])

    def test_confirmar_com_lote_estranho_nao_grava_nada(self):
        r = self._confirmar({"MNCXDCHF300": "247; DROP"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._conn().execute("SELECT COUNT(*) FROM portal_envios").fetchone()[0], 0)

    def test_produto_que_nao_esta_na_nota_vira_aviso_e_o_envio_segue(self):
        r = self._confirmar({"OUTRO": "1"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any("não está nesta nota" in a for a in r.get_json()["lotes_avisos"]))
        self.assertEqual(self._conn().execute("SELECT COUNT(*) FROM portal_envios").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
