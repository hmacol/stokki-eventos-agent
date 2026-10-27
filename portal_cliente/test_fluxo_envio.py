# -*- coding: utf-8 -*-
"""Portal: fluxo (Armazenagem / Full cross / Cross) escolhido por pedido no envio.

    py -3.11 -m unittest portal_cliente.test_fluxo_envio -v
"""
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

import envio_pedidos as ep  # noqa: E402


def _nfe_fake(nf: str) -> dict:
    return {"chave_nfe": f"352609681469760001005500100000000{nf.zfill(2)}1000000001"[:44], "numero_nf": nf, "serie": "1",
            "emitida_em": "2026-10-25 10:00:00", "destinatario_doc": "09014480000467", "destinatario_nome": "Cliente X",
            "destinatario_endereco": "Rua A, 1", "destinatario_bairro": "Centro", "destinatario_municipio": "Sao Paulo",
            "destinatario_uf": "SP", "destinatario_cep": "01001000", "destinatario_telefone": "", "volumes": 2,
            "peso_kg": 1.0, "valor_nf": 10.0, "itens": "1x Produto"}


class EscolherFluxo(unittest.TestCase):
    def test_escolher_fluxo_unico_nao_exige_campo(self):
        self.assertEqual(ep.escolher_fluxo({}, ["cross"], ep.ORIGEM_XML, "NF 1"), "cross")
        self.assertEqual(ep.escolher_fluxo({"fluxo": "armazenagem"}, ["cross"], ep.ORIGEM_XML, "NF 1"), "cross")

    def test_escolher_fluxo_dois_disponiveis_sem_campo_levanta(self):
        with self.assertRaises(ep.ErroEnvio) as c:
            ep.escolher_fluxo({}, ["armazenagem", "cross"], ep.ORIGEM_XML, "NF 1")
        self.assertEqual(str(c.exception), "NF 1: informe o fluxo (Armazenagem ou Cross).")
        self.assertEqual(ep.escolher_fluxo({"fluxo": "cross"}, ["armazenagem", "cross"], ep.ORIGEM_XML, "NF 1"), "cross")

    def test_escolher_fluxo_planilha_cross_levanta(self):
        with self.assertRaises(ep.ErroEnvio) as c:
            ep.escolher_fluxo({"fluxo": "cross"}, ["armazenagem", "cross"], ep.ORIGEM_PLANILHA, "Pedido P1")
        self.assertEqual(str(c.exception), "Pedido P1: pedido de planilha não pode ir em Cross; envie o XML da NF-e.")

    def test_planilha_ignora_cross_quando_sobra_um_fluxo(self):
        self.assertEqual(ep.escolher_fluxo({}, ["armazenagem", "cross"], ep.ORIGEM_PLANILHA, "Pedido P1"), "armazenagem")
        with self.assertRaises(ep.ErroEnvio):
            ep.escolher_fluxo({}, ["cross"], ep.ORIGEM_PLANILHA, "Pedido P1")

    def test_sem_disponiveis_vale_armazenagem(self):
        self.assertEqual(ep.escolher_fluxo({}, [], ep.ORIGEM_XML, "NF 1"), "armazenagem")


class ConfirmarComFluxo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raiz = Path(self.tmp.name)
        self._patches = [mock.patch.object(ep, "DB_PATH", raiz / "d.db"),
                         mock.patch.object(ep, "PASTA_XMLS", raiz / "x"),
                         mock.patch.object(ep, "PASTA_TEMP", raiz / "x" / "_temporarios"),
                         mock.patch.object(ep, "_RAIZ", raiz),
                         mock.patch.object(ep, "ler_nfe", side_effect=lambda conteudo, *a, **k: _nfe_fake(conteudo.decode()[4:-5])),
                         mock.patch.object(ep, "validar_item", return_value={"ok": True, "erros": [], "envio_existente": None}),
                         mock.patch.object(ep, "info_destinatarios", return_value={}),
                         mock.patch.object(ep, "gravar_destinatario"),
                         mock.patch.object(ep, "classificar_area_envio", return_value=None)]
        for p in self._patches:
            p.start()
        (raiz / "x" / "_temporarios").mkdir(parents=True)
        self.conn = ep.conectar()

    def tearDown(self):
        self.conn.close()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _item(self, nf, **kw):
        tk = f"tokenteste{nf}"
        (ep.PASTA_TEMP / f"{tk}.xml").write_bytes(f"<nf>{nf}</nf>".encode())
        return {"token": tk, "horario_inicio": "08:00", "horario_fim": "17:00", **kw}

    def _fluxo_gravado(self, nf):
        return self.conn.execute("SELECT fluxo FROM portal_envios WHERE numero_nf = ?", (nf,)).fetchone()[0]

    def test_confirmar_envios_grava_fluxo_cross(self):
        ep.confirmar_envios(self.conn, "23155000000166", [self._item(1, fluxo="cross"), self._item(2, fluxo="armazenagem")],
                            "cliente", {}, "nenhuma", fluxos_disponiveis=["armazenagem", "cross"])
        self.assertEqual((self._fluxo_gravado("1"), self._fluxo_gravado("2")), ("cross", "armazenagem"))

    def test_confirmar_envios_dois_fluxos_sem_escolha_nao_grava_nada(self):
        with self.assertRaises(ep.ErroEnvio):
            ep.confirmar_envios(self.conn, "23155000000166", [self._item(1, fluxo="cross"), self._item(2)],
                                "cliente", {}, "nenhuma", fluxos_disponiveis=["armazenagem", "cross"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM portal_envios").fetchone()[0], 0)

    def test_confirmar_envios_lista_vazia_recusa_por_xd_ausente(self):
        with self.assertRaises(ep.ErroEnvio) as c:
            ep.confirmar_envios(self.conn, "66259821000155", [self._item(4)], "cliente", {}, "nenhuma",
                                fluxos_disponiveis=[])
        self.assertEqual(str(c.exception), ep.MSG_XD_AUSENTE)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM portal_envios").fetchone()[0], 0)

    def test_confirmar_envios_sem_disponiveis_grava_armazenagem(self):
        ep.confirmar_envios(self.conn, "68146976000100", [self._item(3)], "cliente", {}, "nenhuma")
        self.assertEqual(self._fluxo_gravado("3"), "armazenagem")

    def test_conectar_cria_colunas_novas(self):
        colunas = {r[1] for r in self.conn.execute("PRAGMA table_info(portal_envios)")}
        self.assertTrue({"fluxo", "id_stokki", "troca_nf_status", "troca_nf_em", "troca_nf_erro"} <= colunas, colunas)


class FluxosParaEnvio(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, apelido TEXT, nome_remetente TEXT, stkkc_id INTEGER,
                                  atualizado_em TEXT, fluxos TEXT);
            CREATE TABLE wms_produtos (sku TEXT, embarcador_id INTEGER, ativo INTEGER);
            INSERT INTO interno VALUES ('66259821000155', 'DOURADO', NULL, 18, NULL, 'cross');
            INSERT INTO interno VALUES ('23155000000166', 'FRUTA FINA', NULL, 94, NULL, 'armazenagem,cross');
            INSERT INTO interno VALUES ('29920346000190', 'BRAZO', NULL, 1, NULL, NULL);
            INSERT INTO wms_produtos VALUES ('XDDOURADO', 18, 1);
        """)

    def tearDown(self):
        self.conn.close()

    def test_cross_com_xd(self):
        self.assertEqual(ep.fluxos_para_envio(self.conn, "66259821000155"), (["cross"], {"codigo": "XDDOURADO", "erro": None}))

    def test_cross_sem_xd_sai_da_lista_e_traz_erro(self):
        disp, xd = ep.fluxos_para_envio(self.conn, "23155000000166")
        self.assertEqual(disp, ["armazenagem"])
        self.assertEqual(xd["erro"], "nenhum produto XD no catálogo do embarcador")

    def test_so_cross_sem_xd_fica_vazio(self):
        self.conn.execute("DELETE FROM wms_produtos")
        self.assertEqual(ep.fluxos_para_envio(self.conn, "66259821000155")[0], [])

    def test_sem_fluxo_e_armazenagem_e_sem_cadastro_tambem(self):
        self.assertEqual(ep.fluxos_para_envio(self.conn, "29920346000190"), (["armazenagem"], {"codigo": None, "erro": None}))
        self.assertEqual(ep.fluxos_para_envio(self.conn, "11111111000111"), (["armazenagem"], {"codigo": None, "erro": None}))


class AvisoXdUmPorDia(unittest.TestCase):
    def test_primeiro_aviso_do_dia_por_embarcador(self):
        import sqlite3
        from datetime import date
        conn = sqlite3.connect(":memory:")
        try:
            d1, d2 = date(2026, 10, 27), date(2026, 10, 28)
            self.assertTrue(ep.primeiro_aviso_xd_do_dia(conn, "66259821000155", d1))
            self.assertFalse(ep.primeiro_aviso_xd_do_dia(conn, "66259821000155", d1))
            self.assertTrue(ep.primeiro_aviso_xd_do_dia(conn, "23155000000166", d1))
            self.assertTrue(ep.primeiro_aviso_xd_do_dia(conn, "66259821000155", d2))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
