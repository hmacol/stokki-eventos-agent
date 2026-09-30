# -*- coding: utf-8 -*-
"""
Testes da conciliacao do PS-xxxxx dos envios do portal
(enviar_stokki.reconciliar_codigos), motivados pelo achado de 21/09/2026:
o numero da NF se repete entre embarcadores (no banco local, 7 de 1209 NFs
apontam pra mais de um pedido; a NF 245699 pra 11), e a busca em
documentos_processados casava so por numero_nf pegando a linha mais nova.

    py -3.11 -m unittest portal_cliente.test_reconciliar_codigos
"""
import sqlite3
import sys
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import enviar_stokki as worker  # noqa: E402

EMB_A = "68146976000100"
EMB_B = "11222333000144"
DEST_X = "09014480000467"
DEST_Y = "33793898000151"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE portal_envios (
            id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, numero_nf TEXT,
            destinatario_doc TEXT, status TEXT, codigo_pedido TEXT,
            agendamento_data TEXT, agendamento_pendente INTEGER,
            agendamento_aplicado_em TEXT, atualizado_em TEXT);
        CREATE TABLE documentos_processados (
            hash_conteudo TEXT PRIMARY KEY, tipo TEXT, codigo_pedido TEXT,
            numero_nf TEXT, cnpj_contraparte TEXT);
        CREATE TABLE pedidos_historico (
            id_pedido TEXT, numero_nfe TEXT, cliente_cnpj TEXT, atualizado_em TEXT);
    """)
    return conn


def _envio(conn, envio_id: int, numero_nf: str, embarcador: str = EMB_A,
           destinatario: str = DEST_X, status: str = "CRIADO") -> None:
    conn.execute("INSERT INTO portal_envios (id, cnpj_embarcador, numero_nf, destinatario_doc, status) "
                 "VALUES (?, ?, ?, ?, ?)", (envio_id, embarcador, numero_nf, destinatario, status))
    conn.commit()


def _danfe(conn, numero_nf: str, codigo_pedido: str | None, contraparte: str | None,
           hash_conteudo: str | None = None) -> None:
    conn.execute("INSERT INTO documentos_processados (hash_conteudo, tipo, codigo_pedido, numero_nf, cnpj_contraparte) "
                 "VALUES (?, 'Nota Fiscal', ?, ?, ?)",
                 (hash_conteudo or f"h{numero_nf}{codigo_pedido}{contraparte}", codigo_pedido, numero_nf, contraparte))
    conn.commit()


def _codigo(conn, envio_id: int) -> str | None:
    return conn.execute("SELECT codigo_pedido FROM portal_envios WHERE id = ?", (envio_id,)).fetchone()[0]


class TestReconciliarCodigos(unittest.TestCase):

    def test_nf_repetida_usa_o_destinatario_pra_desempatar(self):
        """NF 646 de dois embarcadores: vale o pedido do MESMO destinatario,
        nao a linha mais recente (que era de outro cliente)."""
        conn = _conn()
        _envio(conn, 1, "646", embarcador=EMB_A, destinatario=DEST_X)
        _danfe(conn, "646", "PS-36205", DEST_X)
        _danfe(conn, "646", "PS-36327", DEST_Y)   # mais nova, de outro destinatario
        self.assertEqual(worker.reconciliar_codigos(conn), 1)
        self.assertEqual(_codigo(conn, 1), "PS-36205")

    def test_nf_repetida_sem_desempate_nao_inventa_codigo(self):
        """Mesmo numero de NF e mesmo destinatario em dois pedidos: nao da
        pra saber qual e, entao deixa vazio pra proxima rodada."""
        conn = _conn()
        _envio(conn, 1, "16178", destinatario=DEST_X)
        _danfe(conn, "16178", "PS-36654", DEST_X)
        _danfe(conn, "16178", "PS-37057", DEST_X)
        self.assertEqual(worker.reconciliar_codigos(conn), 0)
        self.assertIsNone(_codigo(conn, 1))

    def test_contraparte_vazia_continua_conciliando(self):
        """23% das linhas de NF em producao tem cnpj_contraparte vazia --
        sem ambiguidade, elas precisam continuar casando."""
        conn = _conn()
        _envio(conn, 1, "16198", destinatario=DEST_X)
        _danfe(conn, "16198", "PS-35654", None)
        self.assertEqual(worker.reconciliar_codigos(conn), 1)
        self.assertEqual(_codigo(conn, 1), "PS-35654")

    def test_linha_sem_codigo_nao_atrapalha_a_mais_antiga_que_tem(self):
        """DANFE em REVISAO_MANUAL (codigo_pedido nulo) e a linha mais nova:
        nao pode esconder o pedido que a linha anterior ja conhecia."""
        conn = _conn()
        _envio(conn, 1, "16218", destinatario=DEST_X)
        _danfe(conn, "16218", "PS-36192", DEST_X)
        _danfe(conn, "16218", None, DEST_X, hash_conteudo="h-revisao")
        self.assertEqual(worker.reconciliar_codigos(conn), 1)
        self.assertEqual(_codigo(conn, 1), "PS-36192")

    def test_cai_no_historico_quando_documentos_nao_sabe(self):
        """Fallback por pedidos_historico (filtrado por embarcador) intacto."""
        conn = _conn()
        _envio(conn, 1, "35897", embarcador=EMB_A)
        conn.execute("INSERT INTO pedidos_historico (id_pedido, numero_nfe, cliente_cnpj, atualizado_em) "
                     "VALUES ('36418', '35897', '68.146.976/0001-00', '2026-09-21 10:00:00')")
        conn.commit()
        self.assertEqual(worker.reconciliar_codigos(conn), 1)
        self.assertEqual(_codigo(conn, 1), "PS-36418")

    def test_ambiguo_em_documentos_resolve_pelo_historico(self):
        """Documentos empatado, mas o historico sabe pelo embarcador."""
        conn = _conn()
        _envio(conn, 1, "35919", embarcador=EMB_A, destinatario=DEST_X)
        _danfe(conn, "35919", "PS-33236", DEST_X)
        _danfe(conn, "35919", "PS-36542", DEST_X)
        conn.execute("INSERT INTO pedidos_historico (id_pedido, numero_nfe, cliente_cnpj, atualizado_em) "
                     "VALUES ('PS-33236', '35919', '68146976000100', '2026-09-21 10:00:00')")
        conn.commit()
        self.assertEqual(worker.reconciliar_codigos(conn), 1)
        self.assertEqual(_codigo(conn, 1), "PS-33236")

    def test_envio_de_outro_embarcador_nao_pega_o_pedido_do_vizinho(self):
        """Dois envios com a mesma NF, um de cada embarcador: cada um fica
        com o seu."""
        conn = _conn()
        _envio(conn, 1, "646", embarcador=EMB_A, destinatario=DEST_X)
        _envio(conn, 2, "646", embarcador=EMB_B, destinatario=DEST_Y)
        _danfe(conn, "646", "PS-36205", DEST_X)
        _danfe(conn, "646", "PS-36327", DEST_Y)
        self.assertEqual(worker.reconciliar_codigos(conn), 2)
        self.assertEqual(_codigo(conn, 1), "PS-36205")
        self.assertEqual(_codigo(conn, 2), "PS-36327")


if __name__ == "__main__":
    unittest.main()
