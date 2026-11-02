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
from unittest import mock

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
            agendamento_aplicado_em TEXT, atualizado_em TEXT,
            criado_em TEXT DEFAULT (datetime('now','localtime')));
        CREATE TABLE documentos_processados (
            hash_conteudo TEXT PRIMARY KEY, tipo TEXT, codigo_pedido TEXT, status TEXT,
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
    conn.execute("INSERT INTO documentos_processados (hash_conteudo, tipo, status, codigo_pedido, numero_nf, cnpj_contraparte) "
                 "VALUES (?, 'Nota Fiscal', 'ENVIADO', ?, ?, ?)",
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


def _conn_com_embarcadores() -> sqlite3.Connection:
    conn = _conn()
    conn.executescript(f"""
        CREATE TABLE interno (cnpj_embarcador TEXT, apelido TEXT, nome_remetente TEXT, stkkc_id INTEGER,
                              sender_id TEXT, email TEXT);
        CREATE TABLE portal_clientes_envio (cnpj TEXT);
        INSERT INTO interno (cnpj_embarcador, stkkc_id) VALUES ('{EMB_A}', 101), ('{EMB_B}', 202);
    """)
    return conn


@mock.patch.object(worker.sessao_uso, "liberar")
@mock.patch.object(worker.sessao_uso, "adquirir", return_value="principal")
class TestPsDiretoDaStokki(unittest.TestCase):
    """29/10: o PS que a criacao nao trouxe vem da listagem da Stokki (NF +
    embarcador), nao mais esperando a DANFE passar pelo fluxo de documentos."""

    def setUp(self):
        worker._ultima_busca_stokki.clear()
        self.consultas = []

    def _localizar(self, respostas):
        def localizar(sessao, numero_nf, client_id):
            self.consultas.append((numero_nf, client_id))
            return respostas.get((numero_nf, client_id))
        return localizar

    def _reconciliar(self, conn, respostas):
        return worker.reconciliar_codigos(conn, {"x": 1}, localizar=self._localizar(respostas),
                                          fabrica_sessao=lambda: object())

    def test_stokki_vence_a_danfe_e_filtra_pelo_embarcador(self, adquirir, liberar):
        conn = _conn_com_embarcadores()
        _envio(conn, 1, "646", embarcador=EMB_A)
        _envio(conn, 2, "646", embarcador=EMB_B, destinatario=DEST_Y)
        _danfe(conn, "646", "PS-1", DEST_X)
        self.assertEqual(self._reconciliar(conn, {("646", "101"): {"codigo": "PS-500"},
                                                  ("646", "202"): {"codigo": "PS-600"}}), 2)
        self.assertEqual(_codigo(conn, 1), "PS-500")
        self.assertEqual(_codigo(conn, 2), "PS-600")
        liberar.assert_called_once()

    def test_stokki_sem_o_pedido_cai_na_danfe(self, adquirir, liberar):
        conn = _conn_com_embarcadores()
        _envio(conn, 1, "16198")
        _danfe(conn, "16198", "PS-35654", None)
        self.assertEqual(self._reconciliar(conn, {}), 1)
        self.assertEqual(_codigo(conn, 1), "PS-35654")

    def test_stokki_ocupada_nao_consulta_e_usa_a_danfe(self, adquirir, liberar):
        adquirir.return_value = False
        conn = _conn_com_embarcadores()
        _envio(conn, 1, "16198")
        _danfe(conn, "16198", "PS-35654", None)
        self.assertEqual(self._reconciliar(conn, {("16198", "101"): {"codigo": "PS-9"}}), 1)
        self.assertEqual(_codigo(conn, 1), "PS-35654")
        self.assertEqual(self.consultas, [])
        liberar.assert_not_called()

    def test_mesmo_envio_so_volta_a_stokki_depois_do_intervalo(self, adquirir, liberar):
        conn = _conn_com_embarcadores()
        _envio(conn, 1, "777")
        self.assertEqual(self._reconciliar(conn, {}), 0)
        self.assertEqual(self._reconciliar(conn, {}), 0)
        self.assertEqual(len(self.consultas), 1)
        worker._ultima_busca_stokki[1] -= worker.INTERVALO_BUSCA_STOKKI_SEGUNDOS
        self._reconciliar(conn, {})
        self.assertEqual(len(self.consultas), 2)

    def test_teto_de_buscas_por_ciclo(self, adquirir, liberar):
        conn = _conn_com_embarcadores()
        for i in range(worker.MAX_BUSCAS_STOKKI_POR_CICLO + 3):
            _envio(conn, i + 1, str(1000 + i))
        self._reconciliar(conn, {})
        self.assertEqual(len(self.consultas), worker.MAX_BUSCAS_STOKKI_POR_CICLO)

    def test_envio_antigo_nao_vai_a_stokki(self, adquirir, liberar):
        """Duplicado de setembro / pedido cancelado na Stokki: sumiu da
        listagem e nunca vai aparecer -- nao consulta a cada 10 min pra sempre."""
        conn = _conn_com_embarcadores()
        _envio(conn, 1, "176929", status="DUPLICADO")
        conn.execute("UPDATE portal_envios SET criado_em = '2026-09-10 19:07:14' WHERE id = 1")
        conn.commit()
        self.assertEqual(self._reconciliar(conn, {("176929", "101"): {"codigo": "PS-1"}}), 0)
        self.assertEqual(self.consultas, [])
        adquirir.assert_not_called()

    def test_sem_config_nao_abre_a_stokki(self, adquirir, liberar):
        conn = _conn_com_embarcadores()
        _envio(conn, 1, "16198")
        worker.reconciliar_codigos(conn)
        adquirir.assert_not_called()


if __name__ == "__main__":
    unittest.main()
