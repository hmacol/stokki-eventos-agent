# -*- coding: utf-8 -*-
"""
Testes da posicao DOCA (espera do recebimento por etapas).
Rodar (da raiz): py -3.11 -m unittest painel_agentes.test_wms_doca -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

from unittest import mock  # noqa: E402

import wms  # noqa: E402
import wms_caixas as cx  # noqa: E402
import wms_doca  # noqa: E402
import wms_pedidos  # noqa: E402
import wms_separacao as sep  # noqa: E402


class TestDoca(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        self.conn = wms_doca.conectar(self.db)
        self.pid = 1
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, dun, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (1, 900, '72400017', "
            "'COXINHA FESTA', 'MARIA DOLORES', 48, '724000170000', NULL, 6, 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_garantir_doca_cria_uma_vez(self):
        self.assertEqual(wms_doca.garantir_doca(self.conn), "DOC1-P1")
        self.assertEqual(wms_doca.garantir_doca(self.conn), "DOC1-P1")
        self.assertEqual(wms.obter_area(self.conn, "DOC1")["tipo"], "DOCA")
        self.assertEqual(len(wms.listar_posicoes(self.conn, area="DOC1")), 1)

    def test_e_doca(self):
        self.assertTrue(wms_doca.e_doca("doc1-p1"))
        self.assertFalse(wms_doca.e_doca("C1-E1-N1"))
        self.assertFalse(wms_doca.e_doca(None))

    def test_saldo_na_doca_atende_pedido(self):
        # decisao do Hugo 22/11: a doca vale como posicao comum na reserva
        wms_doca.garantir_doca(self.conn)
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=self.pid, quantidade=10,
                                lote="L1", validade="2027-01-01", destino="DOC1-P1")
        lotes = wms_pedidos.disponivel_por_lote(self.conn, self.pid)
        self.assertEqual([(l["posicao"], l["disponivel"]) for l in lotes], [("DOC1-P1", 10.0)])

    def test_temperatura_do_cadastro(self):
        wms.definir_temperatura_produto(self.conn, self.pid, "REFRIGERADO")
        self.assertEqual(wms_doca.temperatura_do_produto(self.conn, self.pid), "REFRIGERADO")

    def test_temperatura_deduzida_de_onde_esta(self):
        wms.criar_area(self.conn, "CG1", "CAM_CONGELADA"); wms.criar_posicao(self.conn, "CG1-P1")
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=self.pid, quantidade=5, destino="CG1-P1", validade="2027-01-01")
        self.assertEqual(wms_doca.temperatura_do_produto(self.conn, self.pid), "CONGELADO")

    def test_temperatura_desconhecida_e_doca_nao_conta(self):
        wms_doca.garantir_doca(self.conn)
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=self.pid, quantidade=5, destino="DOC1-P1", validade="2027-01-01")
        self.assertEqual(wms_doca.temperatura_do_produto(self.conn, self.pid), "")

    def test_temperatura_invalida(self):
        with self.assertRaises(wms.ErroWMS):
            wms.definir_temperatura_produto(self.conn, self.pid, "MORNO")

    def test_temperatura_vazia_limpa(self):
        wms.definir_temperatura_produto(self.conn, self.pid, "CONGELADO")
        self.assertEqual(wms.definir_temperatura_produto(self.conn, self.pid, "")["temperatura"], "")


class TestSugerirPosicoes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = wms_doca.conectar(Path(self._tmp.name) / "dados.db")
        self.pid = 1
        self._produto(1, 900, 48)
        wms.criar_area(self.conn, "C1", "CONTAINER")
        wms.gerar_posicoes(self.conn, "C1", estantes=3, niveis=3)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _produto(self, pid, stokki, emb):
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, dun, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (?, ?, ?, 'PROD', 'EMB', ?, NULL, NULL, "
            "6, 'UN', 1, '2026-09-21 10:00:00')", (pid, stokki, f"SKU{pid}", emb))
        self.conn.commit()

    def _entrada(self, destino, qtd=10, lote="L2611", validade="2027-06-30", pid=None):
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=pid or self.pid, quantidade=qtd,
                                lote=lote, validade=validade, destino=destino)

    def _sug(self, lote="L2611", validade="2027-06-30", limite=3):
        return wms_doca.sugerir_posicoes(self.conn, self.pid, lote, validade, limite)

    def test_sugere_mesmo_lote_primeiro(self):
        self._entrada("C1-E1-N1", 80)
        s = self._sug()
        self.assertEqual(s[0], {"posicao": "C1-E1-N1", "criterio": "MESMO_LOTE",
                                "motivo": "Já tem este produto aqui (lote L2611, 80 UN)"})
        self.assertEqual(s[1]["criterio"], "AO_LADO")
        self.assertLessEqual(len(s), 3)

    def test_lote_diferente_cai_para_ao_lado(self):
        self._entrada("C1-E1-N1", lote="OUTRO")
        s = self._sug()
        self.assertNotIn("MESMO_LOTE", [x["criterio"] for x in s])
        self.assertEqual(s[0]["criterio"], "AO_LADO")
        self.assertEqual(s[0]["motivo"], "Ao lado do mesmo produto")

    def test_ao_lado_prefere_mesma_estante(self):
        self._entrada("C1-E2-N2", lote="OUTRO")
        s = self._sug(limite=4)
        self.assertEqual([x["posicao"] for x in s],
                         ["C1-E2-N1", "C1-E2-N3", "C1-E1-N2", "C1-E3-N2"])

    def test_embarcador_quando_produto_e_novo(self):
        self._produto(2, 901, 48)
        self._produto(3, 902, 99)
        wms.criar_area(self.conn, "C2", "CONTAINER"); wms.gerar_posicoes(self.conn, "C2", pallets=2)
        self._entrada("C1-E1-N1", pid=2)
        self._entrada("C1-E1-N2", pid=2, lote="X")
        self._entrada("C2-P1", pid=3)
        s = self._sug()
        self.assertEqual(s[0]["criterio"], "EMBARCADOR")
        self.assertTrue(s[0]["posicao"].startswith("C1-"))
        self.assertEqual(s[0]["motivo"], "Perto de outros produtos do mesmo embarcador")

    def test_vazia_como_ultimo_recurso(self):
        s = self._sug()
        self.assertEqual(s[0], {"posicao": "C1-E1-N1", "criterio": "VAZIA",
                                "motivo": "Posição vazia na área certa"})

    def test_respeita_temperatura(self):
        wms.criar_area(self.conn, "CG1", "CAM_CONGELADA"); wms.criar_posicao(self.conn, "CG1-P1")
        wms.definir_temperatura_produto(self.conn, self.pid, "CONGELADO")
        self.assertEqual([x["posicao"] for x in self._sug()], ["CG1-P1"])

    def test_sem_temperatura_nao_filtra(self):
        wms.criar_area(self.conn, "CG1", "CAM_CONGELADA"); wms.criar_posicao(self.conn, "CG1-P1")
        pos = [x["posicao"] for x in self._sug(limite=100)]
        self.assertIn("CG1-P1", pos)
        self.assertIn("C1-E1-N1", pos)

    def test_nunca_sugere_doca(self):
        wms_doca.garantir_doca(self.conn)
        self._entrada("DOC1-P1")
        pos = [x["posicao"] for x in self._sug(limite=100)]
        self.assertNotIn("DOC1-P1", pos)
        self.assertTrue(pos)

    def test_sem_posicao_vazia_devolve_lista_vazia(self):
        wms.criar_area(self.conn, "C9", "CONTAINER"); wms.criar_posicao(self.conn, "C9-P1")
        self.conn.execute("UPDATE wms_posicoes SET ativo = 0 WHERE codigo <> 'C9-P1'")
        self.conn.commit()
        self._entrada("C9-P1", lote="OUTRO")
        self.assertEqual(self._sug(), [])

    def test_lote_vazio_e_validade_nula(self):
        self.conn.execute("UPDATE wms_produtos SET controla_validade = 0 WHERE id = ?", (self.pid,))
        self.conn.commit()
        self._entrada("C1-E1-N1", 5, lote="", validade=None)
        s = self._sug(lote="", validade=None)
        self.assertEqual(s[0], {"posicao": "C1-E1-N1", "criterio": "MESMO_LOTE",
                                "motivo": "Já tem este produto aqui (5 UN)"})


DOC, DEST, VAL = "DOC1-P1", "C1-E1-N1", "2027-01-01"
ANA = {"id": "1", "nome": "Ana"}


class TestGuardar(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.conn = sep.conectar(Path(self._tmp.name) / "dados.db")
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, dun, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (1, 900, '72400017', "
            "'COXINHA FESTA', 'MARIA DOLORES', 48, '724000170000', NULL, 12, 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()
        wms_doca.garantir_doca(self.conn)
        wms.criar_area(self.conn, "C1", "CONTAINER")
        wms.gerar_posicoes(self.conn, "C1", estantes=2, niveis=2)
        self._stokki = 5000

    def entrada(self, qtd, lote="L1", validade=VAL):
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=qtd,
                                lote=lote, validade=validade, destino=DOC)

    def caixas(self, n, lote="L1"):
        seriais = cx.criar_caixas(self.conn, produto_id=1, lote=lote, validade=VAL, qtd_un=12,
                                  quantidade=n)["seriais"]
        for s in seriais:
            cx.ativar_caixa(self.conn, s, DOC)
        return seriais

    def saldo(self, pos, lote="L1", validade=VAL):
        return wms._saldo_atual(self.conn, pos, 1, lote, validade or "")

    def pedido(self, qtd):
        self._stokki += 1
        pid = wms_pedidos.registrar_pedido(self.conn, {
            "id_stokki": self._stokki, "codigo_ps": f"PS-{self._stokki}", "embarcador": "MARIA DOLORES",
            "situacao": "Open", "embarcador_id": 48},
            [{"linha": 1, "sku": "72400017", "ean_linha": "", "descricao": "X", "qtd_embalagem": qtd,
              "unidade_do_produto": True}])
        wms_pedidos.reservar_pedido(self.conn, pid)
        self.conn.commit()
        return pid

    def reservas(self, pid):
        return [tuple(r) for r in self.conn.execute(
            "SELECT posicao, quantidade_un, item_id FROM wms_reservas WHERE pedido_id = ? AND estado = 'ATIVA' "
            "ORDER BY posicao", (pid,))]

    def guardar(self, **kw):
        args = dict(produto_id=1, lote="L1", validade=VAL, destino=DEST, uuid="u1", operador=ANA)
        args.update(kw)
        return wms_doca.guardar(self.conn, **args)

    def test_guardar_sem_caixa_tudo(self):
        self.entrada(120)
        r = self.guardar()
        self.assertEqual((self.saldo(DOC), self.saldo(DEST)), (0, 120))
        self.assertEqual(r, {"movidas": [], "quantidade": 120.0, "restante_na_doca": 0.0, "nao_movidas": [],
                             "reservas_levadas": 0, "destino": DEST})
        mov = self.conn.execute("SELECT tipo, observacao, operador_nome FROM wms_movimentos WHERE uuid = 'g-u1'").fetchone()
        self.assertEqual(tuple(mov), ("TRANSFERENCIA", "Guarda da doca", "Ana"))

    def test_guardar_sem_caixa_parte(self):
        self.entrada(120)
        r = self.guardar(quantidade=40)
        self.assertEqual((self.saldo(DOC), self.saldo(DEST)), (80, 40))
        self.assertEqual((r["quantidade"], r["restante_na_doca"]), (40, 80))

    def test_guardar_caixas_todas(self):
        s = self.caixas(3)
        r = self.guardar(quantidade=5)   # quantidade e ignorada com caixa
        self.assertEqual(r["movidas"], s)
        for serial in s:
            self.assertEqual(cx.obter_caixa(self.conn, serial)["posicao"], DEST)
        self.assertEqual((self.saldo(DOC), self.saldo(DEST), r["quantidade"]), (0, 36, 36))
        self.assertEqual(cx.divergencias(self.conn, 48), [])

    def test_guardar_caixas_so_as_bipadas(self):
        s = self.caixas(3)
        r = self.guardar(seriais=[s[0].lower()])
        self.assertEqual(r["movidas"], [s[0]])
        self.assertEqual([cx.obter_caixa(self.conn, x)["posicao"] for x in s], [DEST, DOC, DOC])
        self.assertEqual(r["restante_na_doca"], 24)
        self.assertEqual(cx.divergencias(self.conn, 48), [])

    def test_serial_de_outro_lote_recusa_sem_mover_nada(self):
        s = self.caixas(2)
        outro = self.caixas(1, lote="L2")[0]
        with self.assertRaises(wms.ErroWMS):
            self.guardar(seriais=[s[0], outro])
        self.assertEqual([cx.obter_caixa(self.conn, x)["posicao"] for x in s + [outro]], [DOC, DOC, DOC])
        self.assertEqual(self.saldo(DEST), 0)
        self.assertFalse(self.conn.in_transaction)

    def test_reserva_acompanha_inteira(self):
        s = self.caixas(2)
        pid = self.pedido(24)
        item = self.reservas(pid)[0][2]
        r = self.guardar()
        self.assertEqual(r["movidas"], s)
        self.assertEqual(r["reservas_levadas"], 1)
        self.assertEqual(self.reservas(pid), [(DEST, 24.0, item)])
        b = wms_pedidos.baixar_por_expedicao(self.conn, f"PS-{self._stokki}")
        self.assertEqual((b["baixas"], b["erros"], b["negativos"]), (1, [], []))
        self.assertEqual(self.saldo(DEST), 0)
        self.assertEqual(cx.divergencias(self.conn, 48), [])

    def test_reserva_divide_quando_guarda_parte(self):
        self.entrada(36)
        pid = self.pedido(30)
        item = self.reservas(pid)[0][2]
        r = self.guardar(quantidade=12)
        self.assertEqual(self.saldo(DOC), 24)
        self.assertEqual(self.reservas(pid), [(DEST, 6.0, item), (DOC, 24.0, item)])
        self.assertEqual(r["reservas_levadas"], 1)
        b = wms_pedidos.baixar_por_expedicao(self.conn, f"PS-{self._stokki}")
        self.assertEqual((b["baixas"], b["erros"], b["negativos"]), (2, [], []))
        self.assertEqual((self.saldo(DOC), self.saldo(DEST)), (0, 6))

    def test_reserva_que_cabe_na_doca_nao_muda(self):
        self.entrada(36)
        pid = self.pedido(10)
        antes = self.conn.execute("SELECT * FROM wms_reservas WHERE pedido_id = ?", (pid,)).fetchall()
        r = self.guardar(quantidade=12)
        self.assertEqual(r["reservas_levadas"], 0)
        depois = self.conn.execute("SELECT * FROM wms_reservas WHERE pedido_id = ?", (pid,)).fetchall()
        self.assertEqual([tuple(x) for x in antes], [tuple(x) for x in depois])

    def test_retry_mesmo_uuid_nao_move_duas_vezes(self):
        self.entrada(36)
        pid = self.pedido(30)
        self.guardar(quantidade=12)
        r = self.guardar(quantidade=12)
        self.assertEqual((self.saldo(DOC), self.saldo(DEST)), (24, 12))
        self.assertEqual((r["quantidade"], r["restante_na_doca"], r["reservas_levadas"]), (12, 24, 0))
        self.assertEqual([x[:2] for x in self.reservas(pid)], [(DEST, 6.0), (DOC, 24.0)])

    def test_retry_depois_de_queda_antes_da_reserva(self):
        # caiu entre o movimento e a reserva; o aparelho repete o POST (ate com outra posicao)
        s = self.caixas(2)
        pid = self.pedido(24)
        with mock.patch.object(wms_doca, "_levar_reservas", side_effect=RuntimeError("queda")):
            with self.assertRaises(RuntimeError):
                self.guardar()
        self.assertEqual(self.reservas(pid)[0][0], DOC)
        r = self.guardar(destino="C1-E2-N1")
        self.assertEqual((r["movidas"], r["destino"], r["quantidade"], r["reservas_levadas"]), (s, DEST, 24, 1))
        self.assertEqual([x[:2] for x in self.reservas(pid)], [(DEST, 24.0)])
        self.assertEqual((self.saldo(DOC), self.saldo(DEST), self.saldo("C1-E2-N1")), (0, 24, 0))
        self.assertEqual(cx.divergencias(self.conn, 48), [])

    def test_pedir_mais_do_que_sobrou(self):
        self.entrada(10)
        with self.assertRaises(wms.ErroWMS):
            self.guardar(quantidade=20)
        self.assertEqual((self.saldo(DOC), self.saldo(DEST)), (10, 0))
        self.assertFalse(self.conn.in_transaction)

    def test_lote_vazio_validade_nula(self):
        self.conn.execute("UPDATE wms_produtos SET controla_validade = 0 WHERE id = 1")
        self.conn.commit()
        self.entrada(5, lote="", validade=None)
        pid = self.pedido(5)
        r = self.guardar(lote="", validade=None)
        self.assertEqual((self.saldo(DOC, "", None), self.saldo(DEST, "", None)), (0, 5))
        self.assertEqual((r["quantidade"], r["reservas_levadas"]), (5, 1))
        self.assertEqual([x[:2] for x in self.reservas(pid)], [(DEST, 5.0)])

    def test_caixa_aberta_guarda_com_quantidade_atual(self):
        s = self.caixas(1)[0]
        wms.registrar_movimento(self.conn, tipo="SAIDA", produto_id=1, quantidade=5, lote="L1", validade=VAL,
                                origem=DOC, uuid="m-saida")
        self.conn.execute("BEGIN IMMEDIATE")
        cx.consumir_caixas(self.conn, posicao=DOC, produto_id=1, lote="L1", validade=VAL, qtd_un=5,
                           movimento_uuid="m-saida")
        self.conn.commit()
        r = self.guardar()
        c = cx.obter_caixa(self.conn, s)
        self.assertEqual((r["movidas"], c["situacao"], c["qtd_un"], c["posicao"]), ([s], "ABERTA", 7, DEST))
        self.assertEqual((self.saldo(DOC), self.saldo(DEST), r["quantidade"]), (0, 7, 7))
        self.assertEqual(cx.divergencias(self.conn, 48), [])

    def test_caixa_com_unidade_separada_nao_move(self):
        s = self.caixas(2)
        pid = self.pedido(5)
        onda = sep.onda_de_um_pedido(self.conn, pid, ANA)
        sep.separar_caixa(self.conn, onda, s[1], qtd_un=5, uuid="sep-uuid-1", operador=ANA)
        r = self.guardar()
        self.assertEqual(r["movidas"], [s[0]])
        self.assertEqual(r["nao_movidas"], [{"serial": s[1], "motivo": "unidades separadas para pedido"}])
        self.assertEqual(cx.obter_caixa(self.conn, s[1])["posicao"], DOC)
        self.assertEqual(r["restante_na_doca"], 12)
        self.assertEqual(cx.divergencias(self.conn, 48), [])

    def test_reserva_de_caixa_separada_fica_na_doca(self):
        # B separou a caixa inteira s[2] (reserva MANUAL na doca); A reservou depois (id maior).
        # A guarda leva a reserva de A, nunca a de B, que sai da doca com a caixa.
        s = self.caixas(3)
        pid_b = self.pedido(12)
        onda = sep.onda_de_um_pedido(self.conn, pid_b, ANA)
        sep.separar_caixa(self.conn, onda, s[2], uuid="sep-uuid-b", operador=ANA)
        codigo_b = f"PS-{self._stokki}"
        pid_a = self.pedido(12)
        r = self.guardar()
        self.assertEqual((r["movidas"], r["nao_movidas"][0]["serial"], r["reservas_levadas"]), (s[:2], s[2], 1))
        self.assertEqual([x[:2] for x in self.reservas(pid_b)], [(DOC, 12.0)])
        self.assertEqual([x[:2] for x in self.reservas(pid_a)], [(DEST, 12.0)])
        for codigo in (codigo_b, f"PS-{self._stokki}"):
            b = wms_pedidos.baixar_por_expedicao(self.conn, codigo)
            self.assertEqual((b["erros"], b["negativos"]), ([], []))
        self.assertEqual(cx.obter_caixa(self.conn, s[2])["situacao"], "EXPEDIDA")
        self.assertEqual((self.saldo(DOC), self.saldo(DEST)), (0, 12))
        self.assertEqual(cx.divergencias(self.conn, 48), [])

    def test_temperatura_diferente_pede_confirmacao(self):
        wms.definir_temperatura_produto(self.conn, 1, "REFRIGERADO")
        wms.criar_area(self.conn, "CR1", "CAM_REFRIGERADA"); wms.criar_posicao(self.conn, "CR1-P1")
        self.entrada(10)
        with self.assertRaises(wms_doca.TemperaturaDiferente) as ctx:
            self.guardar()
        self.assertEqual(ctx.exception.dados, {"produto": "REFRIGERADO", "area": "AMBIENTE", "sugerida": "CR1-P1"})
        self.assertEqual(self.saldo(DOC), 10)
        self.guardar(confirmar_temperatura=True)
        self.assertEqual(self.saldo(DEST), 10)
        obs = self.conn.execute("SELECT observacao FROM wms_movimentos WHERE uuid = 'g-u1'").fetchone()[0]
        self.assertEqual(obs, "Guarda da doca · temperatura diferente confirmada")

    def test_destino_doca_recusa(self):
        self.entrada(10)
        with self.assertRaises(wms.ErroWMS) as ctx:
            self.guardar(destino="doc1-p1")
        self.assertEqual(str(ctx.exception), "Bipe a posição de destino, não a doca.")
        self.assertEqual(self.saldo(DOC), 10)

    def test_temperatura_confirmada_fica_no_movimento_da_caixa(self):
        wms.definir_temperatura_produto(self.conn, 1, "REFRIGERADO")
        s = self.caixas(2)
        # a primeira caixa cai entre o movimento e a caixa; o retry tem que aceitar a observacao com sufixo
        real = cx._mudar_caixa
        chamadas = []

        def cai_na_primeira(*a, **kw):
            chamadas.append(1)
            if len(chamadas) == 1:
                raise RuntimeError("queda")
            return real(*a, **kw)

        with mock.patch.object(cx, "_mudar_caixa", side_effect=cai_na_primeira):
            with self.assertRaises(RuntimeError):
                self.guardar(confirmar_temperatura=True)
        r = self.guardar(confirmar_temperatura=True)
        self.assertEqual(r["movidas"], s)
        obs = [x[0] for x in self.conn.execute(
            "SELECT observacao FROM wms_movimentos WHERE uuid LIKE 'g-u1-%' ORDER BY uuid")]
        self.assertEqual(obs, [f"Caixa {x} · temperatura diferente confirmada" for x in s])
        self.assertEqual([cx.obter_caixa(self.conn, x)["posicao"] for x in s], [DEST, DEST])
        self.assertEqual(cx.divergencias(self.conn, 48), [])

    def test_reserva_a_mais_que_ja_havia_na_doca_nao_vai_alem_do_movido(self):
        self.entrada(36)
        pid = self.pedido(30)
        wms.registrar_movimento(self.conn, tipo="AJUSTE", produto_id=1, quantidade=20, lote="L1", validade=VAL,
                                origem=DOC, observacao="avaria")   # doca 16 com 30 reservado: 14 a mais
        r = self.guardar(quantidade=6)
        self.assertEqual(r["reservas_levadas"], 1)
        self.assertEqual([x[:2] for x in self.reservas(pid)], [(DEST, 6.0), (DOC, 24.0)])

    def test_guarda_que_falha_sem_mover_nao_reaponta(self):
        self.entrada(36)
        pid = self.pedido(30)
        wms.registrar_movimento(self.conn, tipo="AJUSTE", produto_id=1, quantidade=20, lote="L1", validade=VAL,
                                origem=DOC, observacao="avaria")
        with self.assertRaises(wms.ErroWMS):
            self.guardar(quantidade=50)
        self.assertEqual([x[:2] for x in self.reservas(pid)], [(DOC, 30.0)])
        self.assertFalse(self.conn.in_transaction)

class TestFilaGuarda(unittest.TestCase):
    setUp = TestGuardar.setUp
    entrada = TestGuardar.entrada
    caixas = TestGuardar.caixas
    pedido = TestGuardar.pedido

    def produto(self, pid, emb=48, nome="PROD"):
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, dun, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (?, ?, ?, ?, 'EMB', ?, NULL, NULL, "
            "12, 'UN', 1, '2026-09-21 10:00:00')", (pid, 900 + pid, f"SKU{pid}", nome, emb))
        self.conn.commit()

    def entrada_em(self, pid, qtd, criado_em, lote="L1", validade=VAL):
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=pid, quantidade=qtd, lote=lote,
                                validade=validade, destino=DOC, criado_em=criado_em)

    def test_fila_agrupa_por_produto_lote_validade(self):
        self.entrada(10)
        self.entrada(5)
        self.entrada(7, lote="L2")
        fila = wms_doca.fila_guarda(self.conn)
        self.assertEqual([(i["lote"], i["quantidade"]) for i in fila], [("L1", 15.0), ("L2", 7.0)])
        i = fila[0]
        self.assertEqual((i["produto_id"], i["descricao"], i["sku"], i["embarcador"], i["embarcador_id"],
                          i["validade"], i["unidade"]),
                         (1, "COXINHA FESTA", "72400017", "MARIA DOLORES", 48, VAL, "UN"))
        self.assertEqual(set(i), {"produto_id", "descricao", "sku", "embarcador", "embarcador_id", "lote",
                                  "validade", "quantidade", "unidade", "caixas", "temperatura", "desde",
                                  "recebimentos", "pedidos_reservados", "sugestao"})

    def test_fila_frio_primeiro_depois_mais_antigo(self):
        for pid in (2, 3, 4):
            self.produto(pid)
        self.entrada_em(1, 5, "2026-11-20 08:00:00")
        self.entrada_em(2, 5, "2026-11-20 09:00:00")
        self.entrada_em(3, 5, "2026-11-20 10:00:00")
        self.entrada_em(4, 5, "2026-11-20 07:00:00")
        wms.definir_temperatura_produto(self.conn, 3, "CONGELADO")
        wms.definir_temperatura_produto(self.conn, 4, "REFRIGERADO")
        fila = wms_doca.fila_guarda(self.conn)
        self.assertEqual([i["produto_id"] for i in fila], [4, 3, 1, 2])
        self.assertEqual(fila[0]["desde"], "2026-11-20 07:00:00")
        self.assertEqual(fila[1]["temperatura"], "CONGELADO")

    def test_fila_conta_caixas_e_pedidos_reservados(self):
        self.caixas(3)
        self.pedido(12)
        self.pedido(12)
        i = wms_doca.fila_guarda(self.conn)[0]
        self.assertEqual((i["caixas"], i["pedidos_reservados"], i["quantidade"]), (3, 2, 36.0))

    def test_fila_caixa_com_unidade_separada_nao_conta(self):
        s = self.caixas(2)
        self.conn.execute("UPDATE wms_caixas SET situacao = 'SEPARADA' WHERE serial = ?", (s[0],))
        self.conn.commit()
        self.assertEqual(wms_doca.fila_guarda(self.conn)[0]["caixas"], 1)

    def test_fila_sem_caixa_e_zero(self):
        self.entrada(10)
        self.assertEqual(wms_doca.fila_guarda(self.conn)[0]["caixas"], 0)

    def test_fila_traz_sugestao_ou_none(self):
        self.entrada(10)
        s = wms_doca.fila_guarda(self.conn)[0]["sugestao"]
        self.assertEqual(s, {"posicao": "C1-E1-N1", "motivo": "Posição vazia na área certa"})
        self.conn.execute("UPDATE wms_posicoes SET ativo = 0 WHERE codigo <> ?", (DOC,))
        self.conn.commit()
        self.assertIsNone(wms_doca.fila_guarda(self.conn)[0]["sugestao"])

    def test_fila_vazia_sem_doca(self):
        conn = sep.conectar(Path(self._tmp.name) / "outro.db")
        self.addCleanup(conn.close)
        self.assertEqual(wms_doca.fila_guarda(conn), [])

    def test_fila_so_saldo_positivo_na_doca(self):
        self.entrada(10)
        self.entrada(7, lote="L2")
        wms.registrar_movimento(self.conn, tipo="TRANSFERENCIA", produto_id=1, quantidade=10, lote="L1",
                                validade=VAL, origem=DOC, destino=DEST)
        self.assertEqual([i["lote"] for i in wms_doca.fila_guarda(self.conn)], ["L2"])

    def test_fila_mostra_qualquer_embarcador(self):
        self.produto(2, emb=77)
        self.entrada(10)
        self.entrada_em(2, 4, "2026-11-21 10:00:00")
        self.assertEqual({i["embarcador_id"] for i in wms_doca.fila_guarda(self.conn)}, {48, 77})

    def test_fila_lote_vazio_validade_nula(self):
        self.conn.execute("UPDATE wms_produtos SET controla_validade = 0 WHERE id = 1")
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=5, lote="", validade=None,
                                destino=DOC)
        i = wms_doca.fila_guarda(self.conn)[0]
        self.assertEqual((i["lote"], i["validade"], i["quantidade"]), ("", "", 5.0))

    def test_fila_recebimentos_pelos_enderecamentos(self):
        self.entrada(10)
        self.entrada(1)
        movs = [r["uuid"] for r in self.conn.execute("SELECT uuid FROM wms_movimentos ORDER BY id")]
        for rid, cod, mov in ((1, "REC-B", movs[0]), (2, "REC-A", movs[1])):
            self.conn.execute(
                "INSERT INTO wms_recebimentos (id, id_stokki, codigo, lido_em, atualizado_em) "
                "VALUES (?, ?, ?, '2026-11-20', '2026-11-20')", (rid, rid, cod))
            self.conn.execute(
                "INSERT INTO wms_recebimento_itens (id, recebimento_id, linha, qtd_embalagem) VALUES (?, ?, 1, 1)",
                (rid, rid))
            self.conn.execute(
                "INSERT INTO wms_recebimento_enderecamentos (uuid, recebimento_id, item_id, quantidade, criado_em) "
                "VALUES (?, ?, ?, 5, '2026-11-20')", (mov, rid, rid))
        self.conn.commit()
        self.assertEqual(wms_doca.fila_guarda(self.conn)[0]["recebimentos"], ["REC-A", "REC-B"])

    def test_fila_sem_vinculo_recebimentos_vazio(self):
        self.entrada(10)
        self.assertEqual(wms_doca.fila_guarda(self.conn)[0]["recebimentos"], [])


if __name__ == "__main__":
    unittest.main()
