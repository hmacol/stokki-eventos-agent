"""
Testes da separacao em onda, distribuicao, conferencia cega e volumes (dados).
Rodar (de dentro de painel_agentes): py -3.11 -m unittest test_wms_separacao -v
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import wms  # noqa: E402
import wms_caixas as cx  # noqa: E402
import wms_fila_impressao as fila  # noqa: E402
import wms_pedidos  # noqa: E402
import wms_separacao as sep  # noqa: E402

EMB = 22
ANA = {"id": "1", "nome": "Ana"}
BIA = {"id": "2", "nome": "Bia"}
P1, P2 = "C9-E1-N1", "C9-E1-N2"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        self.conn = sep.conectar(self.db)
        self.addCleanup(self.conn.close)
        for pid, sku, emb, por_caixa in ((1, "CAFE", EMB, 12), (2, "LEITE", EMB, 6), (3, "OUTRO", 99, 6)):
            self.conn.execute(
                "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, "
                "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (?, ?, ?, ?, 'EMB', ?, ?, ?, "
                "'UN', 0, '2026-09-21 10:00:00')",
                (pid, 900 + pid, sku, "PRODUTO " + sku, emb, "78900000000" + str(pid), por_caixa))
        wms.criar_area(self.conn, "C9", "CONTAINER", "Container 9")
        for pos in (P1, P2):
            wms.criar_posicao(self.conn, pos)
        self.conn.commit()
        self._uuid = 0
        self._stokki = 70000

    def u(self):
        self._uuid += 1
        return f"uuid-teste-{self._uuid:04d}"

    def caixas(self, produto_id, n, posicao=P1, lote="L1", qtd=None):
        r = cx.criar_caixas(self.conn, produto_id=produto_id, lote=lote, validade=None, qtd_un=qtd, quantidade=n)
        for s in r["seriais"]:
            cx.ativar_caixa(self.conn, s, posicao)
        return r["seriais"]

    def pedido(self, itens, embarcador_id=EMB):
        """itens = [(sku, qtd_un)]. Registra e reserva como o sync faz com pedido aberto."""
        self._stokki += 1
        linhas = [{"linha": i + 1, "sku": sku, "ean_linha": "", "descricao": sku, "qtd_embalagem": q,
                   "unidade_do_produto": True} for i, (sku, q) in enumerate(itens)]
        pid = wms_pedidos.registrar_pedido(self.conn, {
            "id_stokki": self._stokki, "codigo_ps": f"PS-{self._stokki}", "embarcador": "EMB", "situacao": "Open",
            "embarcador_id": embarcador_id, "destinatario": "MERCADO", "cidade": "Santos", "uf": "SP",
            "data_entrega_calc": "2026-11-24"}, linhas)
        wms_pedidos.reservar_pedido(self.conn, pid)
        self.conn.commit()
        return pid

    def execucao(self, pedido_id):
        return self.conn.execute("SELECT execucao FROM wms_pedidos WHERE id = ?", (pedido_id,)).fetchone()[0]

    def situacao(self, serial):
        return cx.obter_caixa(self.conn, serial)["situacao"]

    def reservas(self, pedido_id):
        rows = self.conn.execute(
            "SELECT origem, posicao, lote, quantidade_un FROM wms_reservas WHERE pedido_id = ? AND estado = 'ATIVA' "
            "ORDER BY origem, posicao, lote", (pedido_id,)).fetchall()
        return [tuple(r) for r in rows]

    def onda(self, *pedidos):
        onda_id = sep.montar_onda(self.conn, EMB, list(pedidos), "escritorio")
        sep.iniciar_onda(self.conn, onda_id, ANA)
        return onda_id

    def separar_tudo(self, onda_id, seriais, operador=ANA):
        for s in seriais:
            sep.separar_caixa(self.conn, onda_id, s, uuid=self.u(), operador=operador)

    def pronto_pra_conferir(self, itens=(("CAFE", 24),), n_caixas=2):
        seriais = self.caixas(1, n_caixas)
        pid = self.pedido(list(itens))
        onda_id = sep.onda_de_um_pedido(self.conn, pid, ANA)
        self.separar_tudo(onda_id, seriais)
        sep.fechar_separacao(self.conn, onda_id, ANA)
        return pid, seriais


class TestTabelas(Base):
    def test_garantir_duas_vezes_e_colunas_novas(self):
        sep.garantir_tabelas(self.conn)
        colunas = {r[1] for r in self.conn.execute("PRAGMA table_info(wms_pedidos)")}
        self.assertTrue({"execucao", "onda_id", "embarcador_id"} <= colunas)
        self.assertIn("SEPARADA", cx.SITUACOES_CAIXA)

    def test_banco_antigo_sem_as_colunas_ganha_no_garantir(self):
        outro = Path(self._tmp.name) / "antigo.db"
        c = sqlite3.connect(outro)
        c.execute("CREATE TABLE wms_pedidos (id INTEGER PRIMARY KEY, codigo_ps TEXT)")
        sep.garantir_tabelas(c)
        self.assertIn("execucao", {r[1] for r in c.execute("PRAGMA table_info(wms_pedidos)")})
        c.close()


class TestDataDeEntrega(Base):
    def test_sem_nucleo_vale_a_expedicao_calculada(self):
        self.assertEqual(sep.data_entrega_do_pedido(self.conn, {"codigo_ps": "PS-1", "data_entrega_calc": "2026-11-24"}),
                         ("2026-11-24", "expedicao"))
        self.assertEqual(sep.data_entrega_do_pedido(self.conn, {"codigo_ps": "PS-1"}), ("", ""))

    def test_agendamento_vence_a_expedicao(self):
        self.conn.execute("CREATE TABLE nucleo_pedidos (id INTEGER PRIMARY KEY, codigo TEXT, agendamento_inicio TEXT)")
        self.conn.execute("INSERT INTO nucleo_pedidos (codigo, agendamento_inicio) VALUES ('#PS-1', '2026-11-26 08:00:00')")
        self.conn.execute("INSERT INTO nucleo_pedidos (codigo, agendamento_inicio) VALUES ('#PS-2', '')")
        self.assertEqual(sep.data_entrega_do_pedido(self.conn, {"codigo_ps": "PS-1", "data_entrega_calc": "2026-11-24"}),
                         ("2026-11-26", "agendamento"))
        self.assertEqual(sep.data_entrega_do_pedido(self.conn, {"codigo_ps": "PS-2", "data_entrega_calc": "2026-11-24"}),
                         ("2026-11-24", "expedicao"))


class TestOnda(Base):
    def test_pedidos_para_onda_filtra_por_embarcador_e_data(self):
        a = self.pedido([("CAFE", 12)])
        self.pedido([("OUTRO", 6)], embarcador_id=99)
        self.conn.execute("UPDATE wms_pedidos SET data_entrega_calc = '' WHERE id = ?", (a,))
        b = self.pedido([("CAFE", 12)])
        todos = sep.pedidos_para_onda(self.conn, EMB)
        self.assertEqual([p["id"] for p in todos], [a, b])
        self.assertEqual([p["id"] for p in sep.pedidos_para_onda(self.conn, EMB, "2026-11-24")], [b])
        self.assertEqual([p["id"] for p in sep.pedidos_para_onda(self.conn, EMB, "sem")], [a])
        self.assertEqual(sep.datas_para_onda(self.conn, EMB),
                         [{"data": "2026-11-24", "pedidos": 1}, {"data": "", "pedidos": 1}])
        self.assertEqual((todos[1]["fonte_data"], todos[1]["falta_un"], todos[1]["linhas"]), ("expedicao", 12, 1))

    def test_montar_iniciar_e_cancelar(self):
        a, b = self.pedido([("CAFE", 12)]), self.pedido([("CAFE", 12)])
        onda_id = sep.montar_onda(self.conn, EMB, [a, b], "hugo")
        self.assertEqual((self.execucao(a), self.execucao(b)), ("NA_ONDA", "NA_ONDA"))
        self.assertEqual(sep.pedidos_para_onda(self.conn, EMB), [])
        sep.iniciar_onda(self.conn, onda_id, ANA)
        sep.iniciar_onda(self.conn, onda_id, ANA)  # repetir nao quebra
        self.assertEqual(self.execucao(a), "SEPARANDO")
        self.assertEqual([o["id"] for o in sep.ondas_vivas(self.conn, {EMB})], [onda_id])
        sep.cancelar_onda(self.conn, onda_id, "hugo")
        self.assertEqual((self.execucao(a), sep.ondas_vivas(self.conn)), ("", []))

    def test_montar_recusa_lista_vazia_outro_embarcador_e_pedido_ja_em_onda(self):
        a = self.pedido([("CAFE", 12)])
        outro = self.pedido([("OUTRO", 6)], embarcador_id=99)
        sem_cadastro = self.pedido([("NAOEXISTE", 1)])
        for ids in ([], [a, outro], [sem_cadastro]):
            with self.assertRaises(wms.ErroWMS):
                sep.montar_onda(self.conn, EMB, ids, "hugo")
        sep.montar_onda(self.conn, EMB, [a], "hugo")
        with self.assertRaises(wms.ErroWMS):
            sep.montar_onda(self.conn, EMB, [a], "hugo")

    def test_detalhe_soma_os_pedidos_e_mostra_a_caminhada(self):
        self.caixas(1, 3)
        a, b = self.pedido([("CAFE", 24)]), self.pedido([("CAFE", 12)])
        d = sep.detalhe_onda(self.conn, self.onda(a, b))
        self.assertEqual([(p["sku"], p["pedido_un"], p["separado_un"]) for p in d["produtos"]], [("CAFE", 36, 0)])
        self.assertEqual([(c["posicao"], c["lote"], c["qtd_un"]) for c in d["caminhada"]], [(P1, "L1", 36)])


class TestSeparar(Base):
    def test_onda_de_um_pedido_separa_e_ja_distribui(self):
        s = self.caixas(1, 2)
        pid = self.pedido([("CAFE", 24)])
        onda_id = sep.onda_de_um_pedido(self.conn, pid, ANA)
        r = sep.separar_caixa(self.conn, onda_id, s[0], uuid=self.u(), operador=ANA)
        self.assertEqual((r["codigo_ps"], r["qtd_un"], r["caixa_inteira"], r["falta_un"]),
                         (f"PS-{self._stokki}", 12, True, 12))
        self.assertEqual(self.situacao(s[0]), "DISTRIBUIDA")
        # a reserva da caixa bipada virou MANUAL; o resto segue como sugestao
        self.assertEqual(self.reservas(pid), [("FEFO", P1, "L1", 12), ("MANUAL", P1, "L1", 12)])
        # separar nao gera movimento: o saldo da posicao continua inteiro
        self.assertEqual(wms._saldo_atual(self.conn, P1, 1, "L1", ""), 24)

    def test_bipe_repetido_conta_uma_vez(self):
        s = self.caixas(1, 2)
        onda_id = sep.onda_de_um_pedido(self.conn, self.pedido([("CAFE", 24)]), ANA)
        uuid = self.u()
        a = sep.separar_caixa(self.conn, onda_id, s[0], uuid=uuid, operador=ANA)
        b = sep.separar_caixa(self.conn, onda_id, s[0], uuid=uuid, operador=ANA)
        self.assertEqual((a["item_id"], b["repetido"]), (b["item_id"], True))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wms_separacao_itens").fetchone()[0], 1)
        # leitor mandou de novo com uuid novo: a caixa ja saiu
        with self.assertRaisesRegex(wms.ErroWMS, "ja foi separada"):
            sep.separar_caixa(self.conn, onda_id, s[0], uuid=self.u(), operador=ANA)

    def test_caixa_errada_e_recusada_com_motivo(self):
        cafe = self.caixas(1, 3)
        leite = self.caixas(2, 1)
        outro = self.caixas(3, 1)
        impressa = cx.criar_caixas(self.conn, produto_id=1, lote="L1", validade=None, qtd_un=None, quantidade=1)["seriais"][0]
        onda_id = sep.onda_de_um_pedido(self.conn, self.pedido([("CAFE", 24)]), ANA)
        casos = ((outro[0], "outro embarcador"), (leite[0], "nao pede este produto"),
                 (impressa, "ainda nao foi enderecada"), ("C9999999", "nao existe"))
        for serial, motivo in casos:
            with self.assertRaisesRegex(wms.ErroWMS, motivo):
                sep.separar_caixa(self.conn, onda_id, serial, uuid=self.u(), operador=ANA)
        self.separar_tudo(onda_id, cafe[:2])
        with self.assertRaisesRegex(wms.ErroWMS, "ja tem tudo"):
            sep.separar_caixa(self.conn, onda_id, cafe[2], uuid=self.u(), operador=ANA)
        with self.assertRaises(wms.ErroWMS):
            sep.separar_caixa(self.conn, onda_id, cafe[2], uuid="", operador=ANA)

    def test_parte_de_caixa_fica_no_lugar_e_respeita_o_que_sobrou(self):
        s = self.caixas(1, 1)
        pid = self.pedido([("CAFE", 5)])
        onda_id = sep.onda_de_um_pedido(self.conn, pid, ANA)
        with self.assertRaisesRegex(wms.ErroWMS, "informe quanto tirou"):
            sep.separar_caixa(self.conn, onda_id, s[0], uuid=self.u(), operador=ANA)
        with self.assertRaisesRegex(wms.ErroWMS, "so precisa de 5"):
            sep.separar_caixa(self.conn, onda_id, s[0], qtd_un=6, uuid=self.u(), operador=ANA)
        r = sep.separar_caixa(self.conn, onda_id, s[0], qtd_un=5, uuid=self.u(), operador=ANA)
        self.assertEqual((r["caixa_inteira"], r["qtd_un"], r["falta_un"]), (False, 5, 0))
        self.assertEqual(self.situacao(s[0]), "EM_ESTOQUE")
        # outro pedido so pode tirar o que sobrou na caixa (12 - 5)
        onda2 = sep.onda_de_um_pedido(self.conn, self.pedido([("CAFE", 10)]), ANA)
        with self.assertRaisesRegex(wms.ErroWMS, "so tem 7"):
            sep.separar_caixa(self.conn, onda2, s[0], qtd_un=8, uuid=self.u(), operador=ANA)

    def test_lote_diferente_do_reservado_e_aceito_e_a_reserva_segue_a_caixa(self):
        self.caixas(1, 1, posicao=P1, lote="L1")
        outra = self.caixas(1, 1, posicao=P2, lote="L2")
        pid = self.pedido([("CAFE", 12)])
        self.assertEqual(self.reservas(pid), [("FEFO", P1, "L1", 12)])
        onda_id = sep.onda_de_um_pedido(self.conn, pid, ANA)
        sep.separar_caixa(self.conn, onda_id, outra[0], uuid=self.u(), operador=ANA)
        self.assertEqual(self.reservas(pid), [("MANUAL", P2, "L2", 12)])

    def test_dois_aparelhos_na_mesma_caixa_so_um_vence(self):
        s = self.caixas(1, 2)
        onda_id = sep.onda_de_um_pedido(self.conn, self.pedido([("CAFE", 24)]), ANA)
        outra = sep.conectar(self.db)
        self.addCleanup(outra.close)
        sep.separar_caixa(self.conn, onda_id, s[0], uuid=self.u(), operador=ANA)
        with self.assertRaisesRegex(wms.ErroWMS, "ja foi separada"):
            sep.separar_caixa(outra, onda_id, s[0], uuid=self.u(), operador=BIA)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wms_separacao_itens").fetchone()[0], 1)

    def test_devolver_item_solta_a_reserva_e_a_caixa_espera_ser_guardada(self):
        s = self.caixas(1, 2)
        pid = self.pedido([("CAFE", 12)])
        onda_id = sep.onda_de_um_pedido(self.conn, pid, ANA)
        r = sep.separar_caixa(self.conn, onda_id, s[0], uuid=self.u(), operador=ANA)
        sep.devolver_item(self.conn, r["item_id"], operador=ANA, motivo="caixa amassada")
        self.assertEqual((self.situacao(s[0]), self.reservas(pid)), ("DEVOLVIDA", []))
        self.assertEqual(cx.divergencias(self.conn, EMB), [])   # devolvida continua no saldo da posicao
        cx.mover_caixa(self.conn, s[0], P1, uuid=self.u(), operador=ANA)   # guardou no mesmo lugar
        self.assertEqual(self.situacao(s[0]), "EM_ESTOQUE")
        # devolvida tambem pode voltar direto pra onda, sem passar pelo Mover
        r = sep.separar_caixa(self.conn, onda_id, s[1], uuid=self.u(), operador=ANA)
        sep.devolver_item(self.conn, r["item_id"], operador=ANA)
        self.assertEqual(self.situacao(s[1]), "DEVOLVIDA")
        sep.separar_caixa(self.conn, onda_id, s[1], uuid=self.u(), operador=ANA)
        self.assertEqual((self.situacao(s[1]), self.reservas(pid)), ("DISTRIBUIDA", [("MANUAL", P1, "L1", 12)]))
        self.assertEqual(cx.divergencias(self.conn, EMB), [])

class TestDistribuir(Base):
    def test_onda_de_dois_pedidos_a_tela_diz_de_quem_e_a_caixa(self):
        s = self.caixas(1, 3)
        a, b = self.pedido([("CAFE", 24)]), self.pedido([("CAFE", 12)])
        onda_id = self.onda(a, b)
        self.separar_tudo(onda_id, s)
        self.assertEqual(self.situacao(s[0]), "SEPARADA")
        with self.assertRaisesRegex(wms.ErroWMS, "sem pedido"):
            sep.fechar_separacao(self.conn, onda_id, ANA)
        destinos = [sep.distribuir_caixa(self.conn, onda_id, x, uuid=self.u(), operador=ANA)["pedido_id"] for x in s]
        self.assertEqual(destinos, [a, a, b])
        self.assertEqual(self.situacao(s[2]), "DISTRIBUIDA")
        self.assertEqual(self.reservas(b), [("MANUAL", P1, "L1", 12)])
        r = sep.fechar_separacao(self.conn, onda_id, ANA)
        self.assertEqual((len(r["prontos"]), r["faltando"], r["onda_concluida"]), (2, [], True))
        self.assertEqual((self.execucao(a), self.execucao(b)), ("SEPARADO", "SEPARADO"))

    def test_bipe_repetido_e_caixa_de_fora(self):
        s = self.caixas(1, 3)
        onda_id = self.onda(self.pedido([("CAFE", 12)]), self.pedido([("CAFE", 12)]))
        self.separar_tudo(onda_id, s[:2])
        uuid = self.u()
        a = sep.distribuir_caixa(self.conn, onda_id, s[0], uuid=uuid, operador=ANA)
        b = sep.distribuir_caixa(self.conn, onda_id, s[0], uuid=uuid, operador=ANA)
        self.assertEqual((a["pedido_id"], b["repetido"]), (b["pedido_id"], True))
        with self.assertRaisesRegex(wms.ErroWMS, "ja foi distribuida"):
            sep.distribuir_caixa(self.conn, onda_id, s[0], uuid=self.u(), operador=ANA)
        with self.assertRaisesRegex(wms.ErroWMS, "nao foi separado"):
            sep.distribuir_caixa(self.conn, onda_id, s[2], uuid=self.u(), operador=ANA)

    def test_parte_solta_e_distribuida_pelo_codigo_do_produto_e_dividida_entre_pedidos(self):
        s = self.caixas(1, 1)
        a, b = self.pedido([("CAFE", 4)]), self.pedido([("CAFE", 6)])
        onda_id = self.onda(a, b)
        sep.separar_caixa(self.conn, onda_id, s[0], qtd_un=10, uuid=self.u(), operador=ANA)
        r1 = sep.distribuir_caixa(self.conn, onda_id, "789000000001", uuid=self.u(), operador=ANA)
        self.assertEqual((r1["pedido_id"], r1["qtd_un"], r1["sobra_un"]), (a, 4, 6))
        r2 = sep.distribuir_caixa(self.conn, onda_id, "CAFE", uuid=self.u(), operador=ANA)
        self.assertEqual((r2["pedido_id"], r2["qtd_un"], r2["sobra_un"], r2["restam_do_pedido"]), (b, 6, 0, 0))

    def test_fechar_com_falta_so_segue_se_mandar(self):
        s = self.caixas(1, 1)
        pid = self.pedido([("CAFE", 24)])
        onda_id = sep.onda_de_um_pedido(self.conn, pid, ANA)
        self.separar_tudo(onda_id, s)
        r = sep.fechar_separacao(self.conn, onda_id, ANA)
        self.assertEqual((r["prontos"], r["faltando"][0]["falta_un"], self.execucao(pid)), ([], 12, "SEPARANDO"))
        r = sep.fechar_separacao(self.conn, onda_id, ANA, mesmo_faltando=True)
        self.assertEqual((r["onda_concluida"], self.execucao(pid)), (True, "SEPARADO"))


class TestConferencia(Base):
    def test_bateu_vira_conferido_e_a_resposta_nao_mostra_o_esperado(self):
        pid, s = self.pronto_pra_conferir()
        lista = sep.pedidos_para_conferir(self.conn, {EMB})
        self.assertEqual(set(lista[0]) & {"itens", "un", "pedido_un", "linhas"}, set())
        c = sep.iniciar_conferencia(self.conn, pid, BIA)
        r = sep.conferir_bipe(self.conn, c["conferencia_id"], s[0], uuid=self.u())
        self.assertEqual((r["caixas_bipadas"], r["bipes"]), (1, 1))
        self.assertFalse({"faltas", "esperado", "esperado_un"} & set(r))
        sep.conferir_bipe(self.conn, c["conferencia_id"], s[1], uuid=self.u())
        f = sep.fechar_conferencia(self.conn, c["conferencia_id"])
        self.assertEqual((f["resultado"], f["faltas"], self.execucao(pid)), ("OK", [], "CONFERIDO"))
        self.assertEqual(self.situacao(s[0]), "CONFERIDA")

    def test_quem_separou_ou_distribuiu_nao_confere(self):
        pid, _ = self.pronto_pra_conferir()
        with self.assertRaisesRegex(wms.ErroWMS, "nao pode conferir"):
            sep.iniciar_conferencia(self.conn, pid, ANA)
        with self.assertRaises(wms.ErroWMS):
            sep.iniciar_conferencia(self.conn, pid, None)
        c = sep.iniciar_conferencia(self.conn, pid, BIA)
        self.assertEqual(sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"], c["conferencia_id"])
        with self.assertRaisesRegex(wms.ErroWMS, "ja esta sendo conferido por Bia"):
            sep.iniciar_conferencia(self.conn, pid, {"id": "3", "nome": "Caio"})

    def test_so_quem_abriu_a_conferencia_bipa_e_fecha(self):
        # revisao de seguranca 17/11: quem separou (Ana) nao pode bipar nem fechar, pelo id,
        # a conferencia que a Bia abriu
        pid, s = self.pronto_pra_conferir()
        c = sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"]
        with self.assertRaisesRegex(wms.ErroWMS, "so quem abriu"):
            sep.conferir_bipe(self.conn, c, s[0], uuid=self.u(), operador=ANA)
        with self.assertRaisesRegex(wms.ErroWMS, "so quem abriu"):
            sep.fechar_conferencia(self.conn, c, ANA)
        self.assertEqual((sep._estado_conferencia(self.conn, c)["bipes"], self.execucao(pid)), (0, "CONFERINDO"))
        for x in s:
            sep.conferir_bipe(self.conn, c, x, uuid=self.u(), operador=BIA)
        self.assertEqual(sep.fechar_conferencia(self.conn, c, BIA)["resultado"], "OK")

    def test_uuid_de_bipe_de_outra_conferencia_nao_vale_como_repetido(self):
        pid, s = self.pronto_pra_conferir()
        outro, s_outro = self.pronto_pra_conferir()
        c1 = sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"]
        c2 = sep.iniciar_conferencia(self.conn, outro, BIA)["conferencia_id"]
        uuid = self.u()
        sep.conferir_bipe(self.conn, c1, s[0], uuid=uuid, operador=BIA)
        with self.assertRaises(Exception):
            sep.conferir_bipe(self.conn, c2, s_outro[0], uuid=uuid, operador=BIA)
        self.assertEqual(sep._estado_conferencia(self.conn, c2)["bipes"], 0)

    def test_bipe_repetido_caixa_repetida_e_caixa_de_outro_pedido(self):
        pid, s = self.pronto_pra_conferir()
        outro, s_outro = self.pronto_pra_conferir()
        c = sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"]
        uuid = self.u()
        sep.conferir_bipe(self.conn, c, s[0], uuid=uuid)
        self.assertTrue(sep.conferir_bipe(self.conn, c, s[0], uuid=uuid)["repetido"])
        with self.assertRaisesRegex(wms.ErroWMS, "ja foi conferida"):
            sep.conferir_bipe(self.conn, c, s[0], uuid=self.u())
        with self.assertRaisesRegex(wms.ErroWMS, "nao e deste pedido"):
            sep.conferir_bipe(self.conn, c, s_outro[0], uuid=self.u())
        with self.assertRaisesRegex(wms.ErroWMS, "nao reconhecido"):
            sep.conferir_bipe(self.conn, c, "XYZ", uuid=self.u())
        self.assertEqual(sep._estado_conferencia(self.conn, c)["bipes"], 1)

    def test_parte_de_caixa_e_conferida_pelo_codigo_com_quantidade(self):
        s = self.caixas(1, 1)
        pid = self.pedido([("CAFE", 5)])
        onda_id = sep.onda_de_um_pedido(self.conn, pid, ANA)
        sep.separar_caixa(self.conn, onda_id, s[0], qtd_un=5, uuid=self.u(), operador=ANA)
        sep.fechar_separacao(self.conn, onda_id, ANA)
        c = sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"]
        with self.assertRaisesRegex(wms.ErroWMS, "quantidade"):
            sep.conferir_bipe(self.conn, c, "789000000001", uuid=self.u())
        with self.assertRaisesRegex(wms.ErroWMS, "quantidade"):
            sep.conferir_bipe(self.conn, c, s[0], uuid=self.u())
        sep.conferir_bipe(self.conn, c, "789000000001", qtd_un=5, uuid=self.u())
        self.assertEqual(sep.fechar_conferencia(self.conn, c)["resultado"], "OK")

    def test_falta_trava_e_o_escritorio_manda_voltar(self):
        pid, s = self.pronto_pra_conferir()
        c = sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"]
        sep.conferir_bipe(self.conn, c, s[0], uuid=self.u())
        f = sep.fechar_conferencia(self.conn, c)
        self.assertEqual((f["resultado"], f["faltas"][0]["esperado_un"], f["faltas"][0]["conferido_un"]), ("FALTA", 24, 12))
        self.assertEqual(self.execucao(pid), "AGUARDANDO_DECISAO")
        pend = sep.faltas_pendentes(self.conn)
        self.assertEqual((pend[0]["pedido_id"], pend[0]["conferente"], pend[0]["faltas"][0]["sku"]), (pid, "Bia", "CAFE"))
        for decisao, motivo in (("TALVEZ", "motivo"), ("VOLTAR", "")):
            with self.assertRaises(wms.ErroWMS):
                sep.decidir_falta(self.conn, pid, decisao, motivo, "hugo")
        r = sep.decidir_falta(self.conn, pid, "voltar", "faltou bipar uma caixa", "hugo")
        self.assertEqual((self.execucao(pid), sep._onda(self.conn, r["onda_id"])["situacao"]), ("SEPARANDO", "EM_SEPARACAO"))
        with self.assertRaises(wms.ErroWMS):
            sep.decidir_falta(self.conn, pid, "VOLTAR", "de novo", "hugo")
        # a caixa que a conferencia nao achou foi devolvida: a onda nova so pede o que falta (12, nao 24)
        self.assertEqual((self.situacao(s[0]), self.situacao(s[1])), ("DISTRIBUIDA", "DEVOLVIDA"))
        d = sep.detalhe_onda(self.conn, r["onda_id"])
        self.assertEqual((d["produtos"][0]["pedido_un"], d["produtos"][0]["separado_un"]), (24, 12))
        self.assertEqual(sep.fechar_separacao(self.conn, r["onda_id"], ANA)["prontos"], [])
        sep.separar_caixa(self.conn, r["onda_id"], s[1], uuid=self.u(), operador=ANA)   # achou a caixa
        with self.assertRaisesRegex(wms.ErroWMS, "ja tem tudo"):
            sep.separar_caixa(self.conn, r["onda_id"], self.caixas(1, 1)[0], uuid=self.u(), operador=ANA)
        sep.fechar_separacao(self.conn, r["onda_id"], ANA)
        c2 = sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"]
        for x in s:
            sep.conferir_bipe(self.conn, c2, x, uuid=self.u())
        self.assertEqual(sep.fechar_conferencia(self.conn, c2)["resultado"], "OK")
        # a baixa tira exatamente as 24 do pedido
        wms_pedidos.baixar_por_expedicao(self.conn, f"PS-{self._stokki}")
        self.assertEqual(wms._saldo_atual(self.conn, P1, 1, "L1", ""), 12)
        self.assertEqual(cx.divergencias(self.conn, EMB), [])

    def test_liberar_parcial_segue_so_com_o_conferido_e_nao_volta_a_reservar(self):
        s = self.caixas(1, 2)
        pid = self.pedido([("CAFE", 24)])
        onda_id = sep.onda_de_um_pedido(self.conn, pid, ANA)
        self.separar_tudo(onda_id, s[:1])
        sep.fechar_separacao(self.conn, onda_id, ANA, mesmo_faltando=True)
        c = sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"]
        sep.conferir_bipe(self.conn, c, s[0], uuid=self.u())
        self.assertEqual(sep.fechar_conferencia(self.conn, c)["resultado"], "FALTA")
        sep.decidir_falta(self.conn, pid, "PARCIAL", "cliente aceitou receber menos", "hugo")
        self.assertEqual((self.execucao(pid), self.reservas(pid)), ("CONFERIDO", [("MANUAL", P1, "L1", 12)]))
        self.assertTrue(wms_pedidos.reservar_pedido(self.conn, pid).get("ignorado"))
        self.assertEqual(self.reservas(pid), [("MANUAL", P1, "L1", 12)])
        decisao = self.conn.execute("SELECT decisao, motivo, por FROM wms_decisoes_falta").fetchone()
        self.assertEqual(tuple(decisao), ("PARCIAL", "cliente aceitou receber menos", "hugo"))

    def test_item_removido_do_pedido_depois_da_separacao_trava_e_o_voltar_devolve_a_caixa(self):
        s = self.caixas(1, 1) + self.caixas(2, 1)
        pid = self.pedido([("CAFE", 12), ("LEITE", 6)])
        onda_id = sep.onda_de_um_pedido(self.conn, pid, ANA)
        self.separar_tudo(onda_id, s)
        sep.fechar_separacao(self.conn, onda_id, ANA)
        # o sync tira o LEITE do pedido (registrar_pedido cancela a reserva da linha que sumiu)
        wms_pedidos.registrar_pedido(self.conn, {"id_stokki": self._stokki, "codigo_ps": f"PS-{self._stokki}",
                                                  "embarcador": "EMB", "situacao": "Open"},
                                     [{"linha": 1, "sku": "CAFE", "ean_linha": "", "descricao": "CAFE",
                                       "qtd_embalagem": 12, "unidade_do_produto": True, "embarcador_id": EMB}])
        self.conn.commit()
        c = sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"]
        for x in s:
            sep.conferir_bipe(self.conn, c, x, uuid=self.u())
        f = sep.fechar_conferencia(self.conn, c)
        self.assertEqual((f["resultado"], [x["sku"] for x in f["sobras"]]), ("FALTA", ["LEITE"]))   # sobra tambem trava
        r = sep.decidir_falta(self.conn, pid, "VOLTAR", "leite saiu do pedido", "hugo")
        self.assertEqual((self.situacao(s[0]), self.situacao(s[1])), ("DISTRIBUIDA", "DEVOLVIDA"))
        sep.fechar_separacao(self.conn, r["onda_id"], ANA)
        c2 = sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"]
        sep.conferir_bipe(self.conn, c2, s[0], uuid=self.u())
        self.assertEqual(sep.fechar_conferencia(self.conn, c2)["resultado"], "OK")
        wms_pedidos.baixar_por_expedicao(self.conn, f"PS-{self._stokki}")
        self.assertEqual((self.situacao(s[0]), self.situacao(s[1])), ("EXPEDIDA", "DEVOLVIDA"))
        self.assertEqual(cx.divergencias(self.conn, EMB), [])

    def test_pedido_cancelado_no_meio_para_a_conferencia_e_devolve_as_caixas(self):
        pid, s = self.pronto_pra_conferir()
        c = sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"]
        sep.conferir_bipe(self.conn, c, s[0], uuid=self.u())
        wms_pedidos.cancelar_reservas(self.conn, pid, "cancelado na Stokki")
        self.conn.commit()
        self.assertEqual((self.execucao(pid), self.situacao(s[0]), self.situacao(s[1])), ("", "DEVOLVIDA", "DEVOLVIDA"))
        with self.assertRaises(wms.ErroWMS):
            sep.conferir_bipe(self.conn, c, s[1], uuid=self.u())
        with self.assertRaises(wms.ErroWMS):
            sep.fechar_conferencia(self.conn, c)
        # caixa devolvida continua contando no saldo da posicao ate ser guardada
        self.assertEqual(cx.divergencias(self.conn, EMB), [])


class TestVolumes(Base):
    def conferido(self):
        pid, s = self.pronto_pra_conferir()
        c = sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"]
        for x in s:
            sep.conferir_bipe(self.conn, c, x, uuid=self.u())
        sep.fechar_conferencia(self.conn, c)
        return pid, s

    def test_fechar_volumes_cria_etiquetas_na_fila_e_embala(self):
        pid, _ = self.conferido()
        ps = f"PS-{self._stokki}"
        r = sep.fechar_volumes(self.conn, pid, 3, BIA)
        job = fila.obter(self.conn, r["impressao_id"])
        self.assertEqual((job["tipo"], job["tamanho"], job["itens"]),
                         ("VOLUME", "10x15", [f"FV|{ps}|1|3", f"FV|{ps}|2|3", f"FV|{ps}|3|3"]))
        self.assertEqual(self.execucao(pid), "EMBALADO")
        de_novo = sep.fechar_volumes(self.conn, pid, 3, BIA)
        self.assertEqual((de_novo["impressao_id"], de_novo["repetido"]), (r["impressao_id"], True))
        dados = sep.dados_do_volume(self.conn, f"FV|{ps}|2|3")
        self.assertEqual((dados["numero"], dados["total"], dados["destinatario"], dados["cidade"]), (2, 3, "MERCADO", "Santos"))
        self.assertIsNone(sep.dados_do_volume(self.conn, "FL|x|y|z"))
        self.assertEqual(sep.pedidos_para_volumes(self.conn, {EMB})[0]["volumes"], 3)

    def test_recusa_pedido_nao_conferido_e_total_fora_da_faixa(self):
        pid, _ = self.pronto_pra_conferir()
        with self.assertRaisesRegex(wms.ErroWMS, "ainda nao foi conferido"):
            sep.fechar_volumes(self.conn, pid, 2, BIA)
        pid, _ = self.conferido()
        for total in (0, 100, "x"):
            with self.assertRaises(wms.ErroWMS):
                sep.fechar_volumes(self.conn, pid, total, BIA)

    def test_reimprimir_um_volume(self):
        pid, _ = self.conferido()
        r = sep.fechar_volumes(self.conn, pid, 2, BIA)
        novo = sep.reimprimir_volume(self.conn, pid, 2, "Bia")
        self.assertNotEqual(novo["impressao_id"], r["impressao_id"])
        self.assertEqual(fila.obter(self.conn, novo["impressao_id"])["itens"], [f"FV|PS-{self._stokki}|2|2"])
        self.assertEqual(self.conn.execute("SELECT reimpressoes FROM wms_volumes WHERE numero = 2").fetchone()[0], 1)
        with self.assertRaises(wms.ErroWMS):
            sep.reimprimir_volume(self.conn, pid, 9, "Bia")


class TestBaixaECancelamento(Base):
    def test_baixa_expede_exatamente_as_caixas_do_pedido(self):
        s = self.caixas(1, 4)
        pid = self.pedido([("CAFE", 24)])
        onda_id = sep.onda_de_um_pedido(self.conn, pid, ANA)
        escolhidas = [s[3], s[1]]   # nao sao as primeiras do FEFO/serial
        self.separar_tudo(onda_id, escolhidas)
        sep.fechar_separacao(self.conn, onda_id, ANA)
        r = wms_pedidos.baixar_por_expedicao(self.conn, f"PS-{self._stokki}")
        self.assertEqual(r["erros"], [])
        self.assertEqual([self.situacao(x) for x in s], ["EM_ESTOQUE", "EXPEDIDA", "EM_ESTOQUE", "EXPEDIDA"])
        self.assertEqual(wms._saldo_atual(self.conn, P1, 1, "L1", ""), 24)
        self.assertEqual(cx.divergencias(self.conn, EMB), [])
        etapas = {r[0] for r in self.conn.execute("SELECT etapa FROM wms_separacao_itens")}
        self.assertEqual(etapas, {"EXPEDIDA"})
        # rodar de novo nao baixa nem consome em dobro
        wms_pedidos.baixar_por_expedicao(self.conn, f"PS-{self._stokki}")
        self.assertEqual(wms._saldo_atual(self.conn, P1, 1, "L1", ""), 24)

    def test_baixa_de_parte_de_caixa_desconta_a_caixa_de_origem(self):
        s = self.caixas(1, 1)
        pid = self.pedido([("CAFE", 5)])
        onda_id = sep.onda_de_um_pedido(self.conn, pid, ANA)
        sep.separar_caixa(self.conn, onda_id, s[0], qtd_un=5, uuid=self.u(), operador=ANA)
        wms_pedidos.baixar_por_expedicao(self.conn, f"PS-{self._stokki}")
        c = cx.obter_caixa(self.conn, s[0])
        self.assertEqual((c["situacao"], c["qtd_un"]), ("ABERTA", 7))
        self.assertEqual(cx.divergencias(self.conn, EMB), [])

    def test_baixa_de_pedido_sem_separacao_nao_toca_caixa_separada_de_outro(self):
        s = self.caixas(1, 2)
        com = self.pedido([("CAFE", 12)])
        ps_com = f"PS-{self._stokki}"
        sem = self.pedido([("CAFE", 12)])
        ps_sem = f"PS-{self._stokki}"
        onda_id = sep.onda_de_um_pedido(self.conn, com, ANA)
        sep.separar_caixa(self.conn, onda_id, s[0], uuid=self.u(), operador=ANA)   # a 1a do serial vai pro "com"
        wms_pedidos.baixar_por_expedicao(self.conn, ps_sem)
        self.assertEqual([self.situacao(x) for x in s], ["DISTRIBUIDA", "EXPEDIDA"])
        wms_pedidos.baixar_por_expedicao(self.conn, ps_com)
        self.assertEqual([self.situacao(x) for x in s], ["EXPEDIDA", "EXPEDIDA"])
        self.assertEqual(cx.divergencias(self.conn, EMB), [])

    def test_checagem_da_zero_com_caixas_separadas(self):
        s = self.caixas(1, 2)
        onda_id = self.onda(self.pedido([("CAFE", 12)]), self.pedido([("CAFE", 12)]))
        self.separar_tudo(onda_id, s)
        self.assertEqual(cx.divergencias(self.conn, EMB), [])

    def test_caixa_separada_nao_pode_ser_movida(self):
        s = self.caixas(1, 1)
        onda_id = sep.onda_de_um_pedido(self.conn, self.pedido([("CAFE", 12)]), ANA)
        self.separar_tudo(onda_id, s)
        with self.assertRaisesRegex(wms.ErroWMS, "nao pode ser movida"):
            cx.mover_caixa(self.conn, s[0], P2, uuid=self.u(), operador=ANA)

    def test_devolvida_guardada_em_outra_posicao_leva_o_saldo(self):
        s = self.caixas(1, 1)
        pid = self.pedido([("CAFE", 12)])
        onda_id = sep.onda_de_um_pedido(self.conn, pid, ANA)
        self.separar_tudo(onda_id, s)
        wms_pedidos.cancelar_reservas(self.conn, pid, "cancelado")
        self.conn.commit()
        cx.mover_caixa(self.conn, s[0], P2, uuid=self.u(), operador=ANA)
        c = cx.obter_caixa(self.conn, s[0])
        self.assertEqual((c["situacao"], c["posicao"]), ("EM_ESTOQUE", P2))
        self.assertEqual((wms._saldo_atual(self.conn, P1, 1, "L1", ""), wms._saldo_atual(self.conn, P2, 1, "L1", "")), (0, 12))
        self.assertEqual(cx.divergencias(self.conn, EMB), [])


class TestAndamento(Base):
    def test_lista_por_pedido_com_etapa_e_nomes(self):
        pid, s = self.pronto_pra_conferir()
        self.pedido([("CAFE", 12)])   # fora de onda: nao aparece
        c = sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"]
        for x in s:
            sep.conferir_bipe(self.conn, c, x, uuid=self.u())
        sep.fechar_conferencia(self.conn, c)
        sep.fechar_volumes(self.conn, pid, 2, BIA)
        linhas = sep.andamento(self.conn, EMB)
        self.assertEqual(len(linhas), 1)
        a = linhas[0]
        self.assertEqual((a["execucao"], a["separado_por"], a["conferente"], a["conferencia"], a["volumes"],
                          a["data_entrega"]), ("EMBALADO", "Ana", "Bia", "OK", 2, "2026-11-24"))


class TestRevisaoIndependente(Base):
    """Cenarios da revisao de 17/11: a baixa segue o CONFERIDO, e nada fica preso."""

    def separado(self, itens, n_caixas, **kw):
        seriais = self.caixas(1, n_caixas, **kw)
        pid = self.pedido(list(itens))
        return pid, seriais, sep.onda_de_um_pedido(self.conn, pid, ANA)

    def conferir(self, pid, seriais, operador=BIA):
        c = sep.iniciar_conferencia(self.conn, pid, operador)["conferencia_id"]
        for x in seriais:
            sep.conferir_bipe(self.conn, c, x, uuid=self.u(), operador=operador)
        return sep.fechar_conferencia(self.conn, c, operador)

    def test_parcial_nao_baixa_a_caixa_que_o_conferente_nao_achou(self):
        pid, s, onda = self.separado([("CAFE", 24)], 2)
        self.separar_tudo(onda, s)
        sep.fechar_separacao(self.conn, onda, ANA)
        self.assertEqual(self.conferir(pid, s[:1])["resultado"], "FALTA")
        sep.decidir_falta(self.conn, pid, "PARCIAL", "cliente aceitou 12", "hugo")
        self.assertEqual((self.situacao(s[0]), self.situacao(s[1]), self.reservas(pid)),
                         ("CONFERIDA", "DEVOLVIDA", [("MANUAL", P1, "L1", 12)]))
        wms_pedidos.baixar_por_expedicao(self.conn, f"PS-{self._stokki}")
        self.assertEqual((self.situacao(s[0]), self.situacao(s[1])), ("EXPEDIDA", "DEVOLVIDA"))
        self.assertEqual(wms._saldo_atual(self.conn, P1, 1, "L1", ""), 12)   # a caixa que ficou continua no saldo
        self.assertEqual(cx.divergencias(self.conn, EMB), [])

    def test_conferencia_ok_devolve_o_que_estava_separado_e_nao_foi_bipado(self):
        # nao deveria acontecer (a conta nao fecharia), mas se o pedido diminui na Stokki a sobra separada volta
        pid, s, onda = self.separado([("CAFE", 24)], 2)
        self.separar_tudo(onda, s)
        sep.fechar_separacao(self.conn, onda, ANA)
        self.conn.execute("UPDATE wms_pedido_itens SET qtd_un = 12, qtd_embalagem = 12 WHERE pedido_id = ?", (pid,))
        self.conn.commit()
        self.assertEqual(self.conferir(pid, s[:1])["resultado"], "OK")
        self.assertEqual((self.situacao(s[0]), self.situacao(s[1])), ("CONFERIDA", "DEVOLVIDA"))
        wms_pedidos.baixar_por_expedicao(self.conn, f"PS-{self._stokki}")
        self.assertEqual((wms._saldo_atual(self.conn, P1, 1, "L1", ""), cx.divergencias(self.conn, EMB)), (12, []))

    def test_caixa_maior_que_a_sugestao_partida_em_dois_lotes_expede_a_caixa_certa(self):
        self.caixas(1, 1, posicao=P1, lote="L1", qtd=5)
        c2, c3 = self.caixas(1, 2, posicao=P2, lote="L2")
        pid = self.pedido([("CAFE", 12)])
        self.assertEqual(self.reservas(pid), [("FEFO", P1, "L1", 5), ("FEFO", P2, "L2", 7)])
        onda = sep.onda_de_um_pedido(self.conn, pid, ANA)
        sep.separar_caixa(self.conn, onda, c2, uuid=self.u(), operador=ANA)
        self.assertEqual(self.reservas(pid), [("MANUAL", P2, "L2", 12)])
        sep.fechar_separacao(self.conn, onda, ANA)
        self.assertEqual(self.conferir(pid, [c2])["resultado"], "OK")
        wms_pedidos.baixar_por_expedicao(self.conn, f"PS-{self._stokki}")
        self.assertEqual((self.situacao(c2), self.situacao(c3)), ("EXPEDIDA", "EM_ESTOQUE"))
        self.assertEqual(cx.divergencias(self.conn, EMB), [])

    def test_fechar_sem_nada_separado_tira_o_pedido_da_onda_e_ninguem_confere_vazio(self):
        pid, s, onda = self.separado([("CAFE", 24)], 2)
        r = sep.fechar_separacao(self.conn, onda, ANA, mesmo_faltando=True)
        self.assertEqual((r["prontos"], r["sem_nada"], r["onda_concluida"], self.execucao(pid)),
                         ([], [f"PS-{self._stokki}"], True, ""))
        with self.assertRaisesRegex(wms.ErroWMS, "nao esta pronto"):
            sep.iniciar_conferencia(self.conn, pid, ANA)
        self.assertEqual([p["id"] for p in sep.pedidos_para_onda(self.conn, EMB)], [pid])

    def test_conferente_nao_inventa_quantidade_nem_usa_caixa_que_nao_foi_separada(self):
        pid, s, onda = self.separado([("CAFE", 24)], 3)
        self.separar_tudo(onda, s[:1])
        sep.fechar_separacao(self.conn, onda, ANA, mesmo_faltando=True)
        c = sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"]
        for qtd in (12, None):   # serial de caixa que nunca foi separada pra este pedido
            with self.assertRaisesRegex(wms.ErroWMS, "nao e deste pedido"):
                sep.conferir_bipe(self.conn, c, s[1], qtd_un=qtd, uuid=self.u(), operador=BIA)
        sep.conferir_bipe(self.conn, c, s[0], uuid=self.u(), operador=BIA)
        # EAN + quantidade no lugar da caixa que falta: o bipe entra (sem dica), mas o fechamento nao aceita
        sep.conferir_bipe(self.conn, c, "789000000001", qtd_un=12, uuid=self.u(), operador=BIA)
        f = sep.fechar_conferencia(self.conn, c, BIA)
        self.assertEqual((f["resultado"], f["faltas"], f["nao_separado"][0]["contado_solto_un"]), ("FALTA", [], 12))
        self.assertEqual(self.execucao(pid), "AGUARDANDO_DECISAO")

    def test_bipe_de_quantidade_nao_da_dica_e_a_diferenca_aparece_so_no_fechamento(self):
        pid, s, onda = self.separado([("CAFE", 8)], 1)
        sep.separar_caixa(self.conn, onda, s[0], qtd_un=4, uuid=self.u(), operador=ANA)
        sep.fechar_separacao(self.conn, onda, ANA, mesmo_faltando=True)
        c = sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"]
        # qualquer quantidade e aceita igual: a resposta nao depende do esperado nem do separado
        respostas = [sep.conferir_bipe(self.conn, c, "CAFE", qtd_un=q, uuid=self.u(), operador=BIA) for q in (4, 4, 500)]
        for r in respostas:
            self.assertFalse({"faltas", "esperado", "esperado_un", "separado", "restam"} & set(r))
        self.assertEqual([r["bipes"] for r in respostas], [1, 2, 3])
        f = sep.fechar_conferencia(self.conn, c, BIA)
        self.assertEqual((f["resultado"], [x["sku"] for x in f["sobras"]]), ("FALTA", ["CAFE"]))

    def test_linha_sem_cadastro_impede_o_conferido(self):
        s = self.caixas(1, 1)
        pid = self.pedido([("CAFE", 12), ("NAOEXISTE", 3)])
        onda = sep.onda_de_um_pedido(self.conn, pid, ANA)
        self.separar_tudo(onda, s)
        sep.fechar_separacao(self.conn, onda, ANA)
        f = self.conferir(pid, s)
        self.assertEqual((f["resultado"], f["faltas"], [x["sku"] for x in f["sem_cadastro"]]), ("FALTA", [], ["NAOEXISTE"]))
        self.assertEqual(sep.faltas_pendentes(self.conn)[0]["sem_cadastro"][0]["sku"], "NAOEXISTE")

    def test_pedido_cancelado_sai_da_onda_e_nao_recebe_mais_caixa(self):
        s = self.caixas(1, 3)
        a, b = self.pedido([("CAFE", 12)]), self.pedido([("CAFE", 12)])
        onda = self.onda(a, b)
        self.separar_tudo(onda, s[:2])
        wms_pedidos.cancelar_reservas(self.conn, b, "cancelado na Stokki")
        self.conn.commit()
        self.assertEqual([p["id"] for p in sep._pedidos_da_onda(self.conn, onda)], [a])
        destino = sep.distribuir_caixa(self.conn, onda, s[0], uuid=self.u(), operador=ANA)
        self.assertEqual(destino["pedido_id"], a)
        with self.assertRaisesRegex(wms.ErroWMS, "Devolver"):
            sep.distribuir_caixa(self.conn, onda, s[1], uuid=self.u(), operador=ANA)
        with self.assertRaisesRegex(wms.ErroWMS, "ja tem tudo"):
            sep.separar_caixa(self.conn, onda, s[2], uuid=self.u(), operador=ANA)
        # a caixa que sobrou tem saida pela tela: devolver, e a onda fecha
        solto = [i for i in sep.detalhe_onda(self.conn, onda)["itens"] if not i["codigo_ps"]]
        sep.devolver_item(self.conn, solto[0]["id"], operador=ANA)
        self.assertTrue(sep.fechar_separacao(self.conn, onda, ANA)["onda_concluida"])
        self.assertEqual((self.execucao(a), self.execucao(b), self.situacao(s[1])), ("SEPARADO", "", "DEVOLVIDA"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wms_reservas WHERE pedido_id = ? AND estado = 'ATIVA'",
                                           (b,)).fetchone()[0], 0)

    def test_onda_que_perde_todos_os_pedidos_encerra_e_devolve_o_que_estava_solto(self):
        s = self.caixas(1, 2)
        a, b = self.pedido([("CAFE", 12)]), self.pedido([("CAFE", 12)])
        onda = self.onda(a, b)
        self.separar_tudo(onda, s)
        for pid in (a, b):
            wms_pedidos.cancelar_reservas(self.conn, pid, "cancelado")
        self.conn.commit()
        self.assertEqual(sep._onda(self.conn, onda)["situacao"], "CANCELADA")
        self.assertEqual([self.situacao(x) for x in s], ["DEVOLVIDA", "DEVOLVIDA"])
        self.assertEqual(cx.divergencias(self.conn, EMB), [])

    def test_pedido_baixado_no_meio_da_separacao_nao_pede_mais_nada(self):
        pid, s, onda = self.separado([("CAFE", 24)], 2)
        sep.separar_caixa(self.conn, onda, s[0], uuid=self.u(), operador=ANA)
        wms_pedidos.baixar_por_expedicao(self.conn, f"PS-{self._stokki}")   # a expedicao chegou antes
        with self.assertRaises(wms.ErroWMS):
            sep.separar_caixa(self.conn, onda, s[1], uuid=self.u(), operador=ANA)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wms_reservas WHERE pedido_id = ? AND estado = 'ATIVA'",
                                           (pid,)).fetchone()[0], 0)

    def test_conferencia_esquecida_aberta_pode_ser_abandonada(self):
        pid, s = self.pronto_pra_conferir()
        c = sep.iniciar_conferencia(self.conn, pid, BIA)["conferencia_id"]
        sep.conferir_bipe(self.conn, c, s[0], uuid=self.u(), operador=BIA)
        caio = {"id": "3", "nome": "Caio"}
        with self.assertRaises(wms.ErroWMS):
            sep.abandonar_conferencia(self.conn, c, caio)
        self.assertEqual(sep.conferencia_aberta_do_pedido(self.conn, pid), c)
        sep.abandonar_conferencia(self.conn, c, None, por="hugo")     # o escritorio libera
        self.assertEqual((self.execucao(pid), sep.conferencia_aberta_do_pedido(self.conn, pid)), ("SEPARADO", None))
        self.assertEqual(self.conferir(pid, s, operador=caio)["resultado"], "OK")   # os bipes antigos nao valem

    def test_caixa_dividida_entre_pedidos_nao_fica_presa(self):
        s = self.caixas(1, 1)
        a, b = self.pedido([("CAFE", 6)]), self.pedido([("CAFE", 6)])
        onda = self.onda(a, b)
        sep.separar_caixa(self.conn, onda, s[0], uuid=self.u(), operador=ANA)
        for _ in range(2):
            sep.distribuir_caixa(self.conn, onda, s[0], uuid=self.u(), operador=ANA)
        wms_pedidos.cancelar_reservas(self.conn, b, "cancelado")
        self.conn.commit()
        sep.fechar_separacao(self.conn, onda, ANA)
        c = sep.iniciar_conferencia(self.conn, a, BIA)["conferencia_id"]
        sep.conferir_bipe(self.conn, c, "CAFE", qtd_un=6, uuid=self.u(), operador=BIA)
        self.assertEqual(sep.fechar_conferencia(self.conn, c, BIA)["resultado"], "OK")
        wms_pedidos.baixar_por_expedicao(self.conn, sep._pedido(self.conn, a)["codigo_ps"])
        caixa = cx.obter_caixa(self.conn, s[0])
        self.assertEqual((caixa["situacao"], caixa["qtd_un"]), ("DEVOLVIDA", 6))   # sobrou metade: guardar
        self.assertEqual(cx.divergencias(self.conn, EMB), [])
        cx.mover_caixa(self.conn, s[0], P1, uuid=self.u(), operador=ANA)
        self.assertEqual(self.situacao(s[0]), "EM_ESTOQUE")

    def test_caixa_com_parte_separada_nao_move_e_a_baixa_antiga_respeita_a_parte(self):
        s = self.caixas(1, 2)
        com = self.pedido([("CAFE", 4)])
        ps_com = f"PS-{self._stokki}"
        onda = sep.onda_de_um_pedido(self.conn, com, ANA)
        sep.separar_caixa(self.conn, onda, s[0], qtd_un=4, uuid=self.u(), operador=ANA)
        with self.assertRaisesRegex(wms.ErroWMS, "unidades separadas"):
            cx.mover_caixa(self.conn, s[0], P2, uuid=self.u(), operador=ANA)
        sem = self.pedido([("CAFE", 12)])      # pedido que nao passa pela separacao
        wms_pedidos.baixar_por_expedicao(self.conn, f"PS-{self._stokki}")
        # a baixa antiga tirou 8 da primeira caixa (as 4 do outro pedido ficam) e 4 da segunda
        self.assertEqual([(cx.obter_caixa(self.conn, x)["situacao"], cx.obter_caixa(self.conn, x)["qtd_un"]) for x in s],
                         [("ABERTA", 4), ("ABERTA", 8)])
        self.assertEqual(cx.divergencias(self.conn, EMB), [])
        wms_pedidos.baixar_por_expedicao(self.conn, ps_com)
        self.assertEqual(self.situacao(s[0]), "EXPEDIDA")
        self.assertEqual((wms._saldo_atual(self.conn, P1, 1, "L1", ""), cx.divergencias(self.conn, EMB)), (8, []))
        self.assertIsNotNone(sem)

    def test_devolver_so_enquanto_o_pedido_esta_separando(self):
        pid, s = self.pronto_pra_conferir()
        item = self.conn.execute("SELECT id FROM wms_separacao_itens WHERE pedido_id = ? LIMIT 1", (pid,)).fetchone()[0]
        with self.assertRaisesRegex(wms.ErroWMS, "ja saiu da separacao"):
            sep.devolver_item(self.conn, item, operador=ANA)


class TestLoteEValidade(Base):
    """Regra do Hugo (17/11): sem lote indicado pelo cliente, FEFO sempre."""

    def caixa(self, lote, validade, posicao=P1, produto_id=1):
        r = cx.criar_caixas(self.conn, produto_id=produto_id, lote=lote, validade=validade, qtd_un=None, quantidade=1)
        cx.ativar_caixa(self.conn, r["seriais"][0], posicao)
        return r["seriais"][0]

    def onda_de(self, qtd=12):
        pid = self.pedido([("CAFE", qtd)])
        return pid, sep.onda_de_um_pedido(self.conn, pid, ANA)

    def test_recusa_a_caixa_que_nao_vence_primeiro_e_diz_onde_esta_a_certa(self):
        nova = self.caixa("L-NOVO", "2027-06-30", P1)
        velha = self.caixa("L-VELHO", "2027-01-15", P2)
        pid, onda = self.onda_de()
        with self.assertRaises(wms.ErroWMS) as erro:
            sep.separar_caixa(self.conn, onda, nova, uuid=self.u(), operador=ANA)
        msg = str(erro.exception)
        self.assertIn("Pegue o lote L-VELHO (val. 15/01/2027) na posicao C9-E1-N2", msg)
        self.assertIn("Esta caixa e do lote L-NOVO, val. 30/06/2027", msg)
        self.assertNotIn("FEFO", msg)   # a tela mostra o lote, nao a sigla
        self.assertEqual((self.situacao(nova), self.conn.execute("SELECT COUNT(*) FROM wms_separacao_itens").fetchone()[0]),
                         ("EM_ESTOQUE", 0))
        r = sep.separar_caixa(self.conn, onda, velha, uuid=self.u(), operador=ANA)
        self.assertEqual((r["lote"], r["validade"]), ("L-VELHO", "2027-01-15"))

    def test_depois_que_o_lote_velho_sai_o_novo_e_aceito(self):
        velha = self.caixa("L-VELHO", "2027-01-15")
        nova = self.caixa("L-NOVO", "2027-06-30")
        pid, onda = self.onda_de(24)
        sep.separar_caixa(self.conn, onda, velha, uuid=self.u(), operador=ANA)
        sep.separar_caixa(self.conn, onda, nova, uuid=self.u(), operador=ANA)
        self.assertEqual(self.situacao(nova), "DISTRIBUIDA")

    def test_mesma_validade_em_lotes_diferentes_tanto_faz(self):
        a = self.caixa("L-A", "2027-01-15")
        b = self.caixa("L-B", "2027-01-15")
        pid, onda = self.onda_de()
        sep.separar_caixa(self.conn, onda, b, uuid=self.u(), operador=ANA)
        self.assertEqual((self.situacao(a), self.situacao(b)), ("EM_ESTOQUE", "DISTRIBUIDA"))

    def test_caixa_vencida_e_recusada_e_nao_conta_no_fefo(self):
        vencida = self.caixa("L-VENC", "2026-01-10")
        boa = self.caixa("L-BOA", "2027-06-30")
        pid, onda = self.onda_de()
        with self.assertRaisesRegex(wms.ErroWMS, "VENCIDA em 10/01/2026"):
            sep.separar_caixa(self.conn, onda, vencida, uuid=self.u(), operador=ANA)
        sep.separar_caixa(self.conn, onda, boa, uuid=self.u(), operador=ANA)   # a vencida nao trava a boa
        self.assertEqual(self.situacao(boa), "DISTRIBUIDA")
        self.assertEqual([x["lote"] for x in sep.lotes_do_produto(self.conn, 1)], [])   # vencida fora, boa ja saiu

    def test_caixa_sem_validade_so_sai_depois_das_que_tem(self):
        sem = self.caixas(1, 1, lote="L-SEM")[0]
        com = self.caixa("L-COM", "2027-06-30")
        pid, onda = self.onda_de()
        with self.assertRaisesRegex(wms.ErroWMS, "Pegue o lote L-COM .* Esta caixa e do lote L-SEM, sem validade"):
            sep.separar_caixa(self.conn, onda, sem, uuid=self.u(), operador=ANA)
        sep.separar_caixa(self.conn, onda, com, uuid=self.u(), operador=ANA)

    def test_caixa_velha_toda_prometida_a_outro_pedido_nao_trava(self):
        velha = self.caixa("L-VELHO", "2027-01-15")
        nova = self.caixa("L-NOVO", "2027-06-30")
        _, onda1 = self.onda_de(12)
        sep.separar_caixa(self.conn, onda1, velha, uuid=self.u(), operador=ANA)
        _, onda2 = self.onda_de(12)
        sep.separar_caixa(self.conn, onda2, nova, uuid=self.u(), operador=ANA)
        self.assertEqual(self.situacao(nova), "DISTRIBUIDA")

    def test_lote_indicado_pelo_cliente_vence_o_fefo_e_so_ele_e_aceito(self):
        velha = self.caixa("L-VELHO", "2027-01-15")
        nova = self.caixa("L-NOVO", "2027-06-30")
        pid = self.pedido([("CAFE", 12)])
        op = sep.opcoes_de_lote(self.conn, pid)
        self.assertEqual([(l["lote"], l["validade"], l["livre_un"], l["posicoes"]) for l in op["produtos"][0]["lotes"]],
                         [("L-VELHO", "2027-01-15", 12, [P1]), ("L-NOVO", "2027-06-30", 12, [P1])])
        for produto, lote in ((1, "L-NAOEXISTE"), (2, "L-NOVO")):
            with self.assertRaises(wms.ErroWMS):
                sep.indicar_lote(self.conn, pid, produto, lote, "hugo")
        self.assertEqual(sep.indicar_lote(self.conn, pid, 1, " l-novo ", "hugo")["produtos"][0]["indicado"], "L-NOVO")
        onda = sep.onda_de_um_pedido(self.conn, pid, ANA)
        self.assertEqual(sep.detalhe_onda(self.conn, onda)["lotes_indicados"][0]["lote"], "L-NOVO")
        with self.assertRaisesRegex(wms.ErroWMS, "cliente indicou o lote L-NOVO"):
            sep.separar_caixa(self.conn, onda, velha, uuid=self.u(), operador=ANA)
        sep.separar_caixa(self.conn, onda, nova, uuid=self.u(), operador=ANA)
        self.assertEqual(self.reservas(pid), [("MANUAL", P1, "L-NOVO", 12)])
        # tirar a indicacao volta pro FEFO
        outro = self.pedido([("CAFE", 12)])
        sep.indicar_lote(self.conn, outro, 1, "L-VELHO", "hugo")
        self.assertEqual(sep.indicar_lote(self.conn, outro, 1, "", "hugo")["produtos"][0]["indicado"], "")

    def test_na_onda_de_varios_pedidos_o_lote_indicado_vai_pro_pedido_que_pediu(self):
        velha = self.caixa("L-VELHO", "2027-01-15")
        nova = self.caixa("L-NOVO", "2027-06-30")
        a, b = self.pedido([("CAFE", 12)]), self.pedido([("CAFE", 12)])
        sep.indicar_lote(self.conn, b, 1, "L-NOVO", "hugo")
        onda = self.onda(a, b)
        sep.separar_caixa(self.conn, onda, nova, uuid=self.u(), operador=ANA)    # fora do FEFO, mas o B pediu
        sep.separar_caixa(self.conn, onda, velha, uuid=self.u(), operador=ANA)
        destinos = {x: sep.distribuir_caixa(self.conn, onda, x, uuid=self.u(), operador=ANA)["pedido_id"] for x in (velha, nova)}
        self.assertEqual(destinos, {velha: a, nova: b})

    def test_nao_indica_lote_depois_que_o_pedido_saiu_da_separacao(self):
        self.caixa("L-A", "2027-01-15")
        pid, onda = self.onda_de()
        s = self.conn.execute("SELECT serial FROM wms_caixas").fetchone()[0]
        sep.separar_caixa(self.conn, onda, s, uuid=self.u(), operador=ANA)
        sep.fechar_separacao(self.conn, onda, ANA)
        with self.assertRaisesRegex(wms.ErroWMS, "ja saiu da separacao"):
            sep.indicar_lote(self.conn, pid, 1, "L-A", "hugo")

    def test_andamento_mostra_os_lotes_que_foram_pro_pedido(self):
        velha = self.caixa("L-VELHO", "2027-01-15")
        pid, onda = self.onda_de()
        sep.separar_caixa(self.conn, onda, velha, uuid=self.u(), operador=ANA)
        lotes = sep.andamento(self.conn, EMB)[0]["lotes"]
        self.assertEqual(lotes, [{"sku": "CAFE", "lote": "L-VELHO", "validade": "2027-01-15", "un": 12}])


if __name__ == "__main__":
    unittest.main()
