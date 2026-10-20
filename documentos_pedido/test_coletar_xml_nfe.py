# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest documentos_pedido.test_coletar_xml_nfe
"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "documentos_pedido"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import coletar_xml_nfe as col  # noqa: E402

AGORA = datetime(2026, 10, 20, 23, 30)
DESDE = date(2026, 7, 15)


def _banco() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        create table nucleo_pedidos (codigo text primary key, status text, remetente_nome text,
            criado_em_provedor text);
        create table nucleo_rotas (id integer primary key, data_rota text);
        create table nucleo_paradas (id integer primary key, rota_id int, codigo text);
        create table portal_envios (id integer primary key, codigo_pedido text, chave_nfe text, status text);
    """)
    col.garantir_tabela(conn)
    return conn


def _ped(conn, codigo, status="ENTREGUE", criado="2026-08-10 10:00:00", remetente="EMBARCADOR X"):
    conn.execute("insert into nucleo_pedidos values (?, ?, ?, ?)", (codigo, status, remetente, criado))


class TestSelecao(unittest.TestCase):
    def test_seleciona_ativos_primeiro_depois_mais_novos(self):
        conn = _banco()
        _ped(conn, "PS-1", criado="2026-08-01 10:00:00")
        _ped(conn, "PS-2", criado="2026-09-01 10:00:00")
        _ped(conn, "PS-3", status="ABERTO", criado="2026-07-20 10:00:00")
        _ped(conn, "PS-4", status="EM_ROTA", criado="2026-07-21 10:00:00")
        self.assertEqual(col.selecionar(conn, DESDE, AGORA, 10), ["PS-4", "PS-3", "PS-2", "PS-1"])

    def test_reentrega_vira_um_codigo_base(self):
        conn = _banco()
        _ped(conn, "PS-10", status="INSUCESSO")
        _ped(conn, "PS-10-R1", status="ABERTO")
        self.assertEqual(col.selecionar(conn, DESDE, AGORA, 10), ["PS-10"])

    def test_fora_do_periodo_so_entra_com_parada_no_periodo(self):
        conn = _banco()
        _ped(conn, "PS-5", criado="2026-06-01 10:00:00")
        _ped(conn, "PS-6", criado="2026-06-02 10:00:00")
        conn.execute("insert into nucleo_rotas values (1, '2026-07-20')")
        conn.execute("insert into nucleo_paradas values (1, 1, 'PS-6')")
        self.assertEqual(col.selecionar(conn, DESDE, AGORA, 10), ["PS-6"])

    def test_pula_portal_com_chave_real_mas_nao_placeholder(self):
        conn = _banco()
        _ped(conn, "PS-7")
        _ped(conn, "PS-8")
        conn.execute("insert into portal_envios values (1, 'PS-7', ?, 'CRIADO')", ("3" * 44,))
        conn.execute("insert into portal_envios values (2, 'PS-8', ?, 'CRIADO')", ("99" + "1" * 42,))
        self.assertEqual(col.selecionar(conn, DESDE, AGORA, 10), ["PS-8"])

    def test_pula_embarcador_sem_nf(self):
        conn = _banco()
        _ped(conn, "PS-9", remetente="Padrão Puro Alimentos")
        _ped(conn, "PS-11", remetente="PEDRAMOURA LTDA")
        _ped(conn, "PS-12", remetente="Laticínios Dourado")
        self.assertEqual(col.selecionar(conn, DESDE, AGORA, 10), ["PS-12"])

    def test_respeita_limite(self):
        conn = _banco()
        for i in range(1, 6):
            _ped(conn, f"PS-{i}", criado=f"2026-08-0{i} 10:00:00")
        self.assertEqual(col.selecionar(conn, DESDE, AGORA, 2), ["PS-5", "PS-4"])

    def test_estado_ok_sai_da_selecao(self):
        conn = _banco()
        _ped(conn, "PS-1")
        col.registrar(conn, "PS-1", col.Situacao.OK, AGORA, chave="3" * 44)
        self.assertEqual(col.selecionar(conn, DESDE, AGORA, 10), [])


class TestPolitica(unittest.TestCase):
    def _estado(self, situacao, tentativas, dias_atras=0):
        return {"situacao": situacao, "tentativas": tentativas,
                "ultima_em": (AGORA - timedelta(days=dias_atras)).strftime("%Y-%m-%d %H:%M:%S")}

    def test_politica_de_nova_tentativa(self):
        casos = [
            (None, False, True),
            (self._estado("OK", 1), True, False),
            (self._estado("ERRO", 4), False, True),
            (self._estado("ERRO", 5), False, False),
            (self._estado("SEM_XML", 9), True, True),              # ativo: toda noite
            (self._estado("PLACEHOLDER", 1, dias_atras=7), False, True),
            (self._estado("PLACEHOLDER", 1, dias_atras=6), False, False),
            (self._estado("SEM_XML", 2, dias_atras=30), False, True),
            (self._estado("SEM_XML", 3, dias_atras=30), False, False),
        ]
        for estado, ativo, esperado in casos:
            with self.subTest(estado=estado, ativo=ativo):
                self.assertIs(col.deve_tentar(estado, ativo, AGORA), esperado)

    def test_registrar_conta_tentativas_e_corta_erro(self):
        conn = _banco()
        col.registrar(conn, "PS-1", col.Situacao.ERRO, AGORA, erro="x" * 500)
        col.registrar(conn, "PS-1", col.Situacao.SEM_XML, AGORA + timedelta(days=1))
        linha = conn.execute("select * from xml_stokki_coleta where codigo = 'PS-1'").fetchone()
        self.assertEqual((linha["situacao"], linha["tentativas"]), ("SEM_XML", 2))
        self.assertEqual(linha["primeira_em"], "2026-10-20 23:30:00")
        self.assertIsNone(linha["erro"])
        col.registrar(conn, "PS-2", col.Situacao.ERRO, AGORA, erro="x" * 500)
        erro = conn.execute("select erro from xml_stokki_coleta where codigo = 'PS-2'").fetchone()[0]
        self.assertEqual(len(erro), 200)


class TestEscrita(unittest.TestCase):
    def test_escrita_atomica_nao_deixa_xml_parcial(self):
        with tempfile.TemporaryDirectory() as tmp:
            pasta = Path(tmp)
            with patch.object(col.os, "replace", side_effect=OSError("disco")):
                with self.assertRaises(OSError):
                    col.salvar_xml(b"<nfe>1</nfe>", "PS-1", pasta)
            self.assertEqual([p.name for p in pasta.iterdir() if p.suffix == ".xml"], [])
            final = col.salvar_xml(b"<nfe>2</nfe>", "PS-1", pasta)
            self.assertEqual((final.name, final.read_bytes()), ("PS-1.xml", b"<nfe>2</nfe>"))
            col.salvar_xml(b"<nfe>3</nfe>", "PS-1", pasta)
            self.assertEqual(final.read_bytes(), b"<nfe>3</nfe>")
            self.assertEqual(sorted(p.name for p in pasta.iterdir()), ["PS-1.xml"])


if __name__ == "__main__":
    unittest.main()
