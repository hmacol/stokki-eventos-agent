# -*- coding: utf-8 -*-
"""py -3.11 -m unittest stokki.test_devolucao_executor"""
import contextlib
import json
import sqlite3
import unittest
from datetime import datetime
from unittest import mock

from stokki import devolucao_estoque as de
from stokki import devolucao_executor as ex
from stokki import mover_virtual as mv
from stokki.test_mover_virtual import ApiQueLevanta

LIGADO = {"stokki": {"devolucao_cancelamento": {"ativo": True}}}


# --- Executor da fila ---

def _item(pid=1, sku="A", qtd=4, destinos=(("E1", 4),), situacao="mover"):
    return {"product_id": pid, "sku": sku, "nome": f"prod {sku}", "qtd": qtd, "destinos": list(destinos),
            "situacao": situacao}


CANCELADO = datetime(2026, 11, 3, 12, 12)


def _plano(status="MOVER", itens=None, motivo=""):
    return {"status": status, "cliente_id": 100, "itens": itens if itens is not None else [_item()], "motivo": motivo,
            "cancelado_em": CANCELADO}


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.galpao, self.operacao, self.chamadas_mover, self.planos, self.agoras = [], [], [], [], []
        self.admin_abriu = 0
        self.admin_ocupado = False
        self.plano = _plano()
        self.retorno_mover = None
        self.mover_levanta = None
        self.plano_levanta = None
        p = mock.patch.object(ex, "montar_plano", side_effect=self._montar_plano)
        p.start()
        self.addCleanup(p.stop)
        adq = mock.patch.object(ex.sessao_uso, "adquirir", return_value="provider")
        lib = mock.patch.object(ex.sessao_uso, "liberar")
        self.mock_adq, self.mock_lib = adq.start(), lib.start()
        self.addCleanup(adq.stop)
        self.addCleanup(lib.stop)
        ocup = mock.patch.object(ex.sessao_uso, "ocupante_externo", return_value=None)
        self.mock_ocup = ocup.start()
        self.addCleanup(ocup.stop)
        self.admin_aberta = False
        self.reconferencias, self.devolucoes_novas = [], {}   # product_id -> linhas que a reconferencia ve
        rec = mock.patch.object(ex, "ler_devolucoes", side_effect=self._ler_devolucoes)
        rec.start()
        self.addCleanup(rec.stop)

    def _ler_devolucoes(self, sess, product_id, desde, ate=None, ignorar=frozenset()):
        assert self.admin_aberta, "reconferencia fora da sessao admin"
        self.reconferencias.append((product_id, desde, ate, frozenset(ignorar)))
        return self.devolucoes_novas.get(product_id, [])

    def _montar_plano(self, sess, id_stokki, agora, ja_estava=False, ignorar_movimentos=frozenset()):
        self.planos.append((id_stokki, ja_estava, frozenset(ignorar_movimentos)))
        self.agoras.append(agora)
        if self.plano_levanta:
            raise self.plano_levanta
        return self.plano

    @contextlib.contextmanager
    def abrir_admin(self):
        if self.admin_ocupado:
            raise ex.ContaOcupada("ocupada")
        self.admin_abriu += 1
        self.admin_aberta = True
        try:
            yield object()
        finally:
            self.admin_aberta = False

    def mover(self, config, cliente_id, movimentos, motivo, headless=True, ao_concluir=None):
        assert not self.admin_aberta, "admin aberta durante o mover"
        self.chamadas_mover.append((cliente_id, movimentos, motivo))
        if self.retorno_mover is None:
            res = [{"product_id": m["product_id"], "destino": m["destinos"][0][0], "pedido": m["qtd"],
                    "movido": m["qtd"], "adjust_id": 900 + m["product_id"], "erro": ""} for m in movimentos]
        else:
            res = self.retorno_mover(movimentos)
        for r in res:
            if ao_concluir:
                ao_concluir(r)
        if self.mover_levanta:
            raise self.mover_levanta
        return res

    def rodar(self, config=LIGADO, simular=False, mover=None):
        return ex.processar_pendentes(
            self.conn, config, simular=simular, abrir_admin=self.abrir_admin, mover=mover or self.mover,
            avisar_galpao=lambda *a: self.galpao.append(a), avisar_operacao=lambda *a: self.operacao.append(a),
            agora=datetime(2026, 11, 3, 13, 0))

    def fila(self, *ids, ja_estava=False):
        for i in ids:
            de.registrar(i, "portal", LIGADO, ja_estava=ja_estava, conn=self.conn)

    def linha(self, i):
        return dict(self.conn.execute("SELECT * FROM stokki_devolucoes WHERE id_stokki = ?", (i,)).fetchone())

    def test_desligado_nao_chama_nada(self):
        self.fila(1)
        r = self.rodar(config={})
        self.assertEqual(r, {"feitas": 0, "parciais": 0, "nada": 0, "falhas": 0, "esperando": 0})
        self.assertEqual((self.admin_abriu, self.planos, self.chamadas_mover), (0, [], []))
        self.mock_adq.assert_not_called()

    def test_sem_pendente_nao_abre_nada(self):
        self.rodar()
        self.assertEqual(self.admin_abriu, 0)
        self.mock_adq.assert_not_called()

    def test_nada_jadevolvido_antigo_sem_aviso(self):
        for status in ("NADA", "JA_DEVOLVIDO", "ANTIGO"):
            de.garantir_tabela(self.conn)
            self.conn.execute("DELETE FROM stokki_devolucoes")
            self.fila(1)
            self.plano = _plano(status, [_item(situacao="nada")])
            r = self.rodar()
            self.assertEqual(self.linha(1)["status"], status)
            self.assertEqual(r["nada"], 1)
        self.assertEqual((self.chamadas_mover, self.galpao, self.operacao), ([], [], []))

    def test_mover_tudo_vira_feita_e_avisa_o_galpao(self):
        self.fila(43101)
        r = self.rodar()
        self.assertEqual(r["feitas"], 1)
        l = self.linha(43101)
        self.assertEqual(l["status"], "FEITA")
        itens = json.loads(l["itens_json"])
        self.assertEqual(itens[0]["movimentos"][0]["adjust_id"], 901)
        self.assertEqual(self.chamadas_mover[0][0], 100)
        self.assertEqual(self.chamadas_mover[0][2], "Retorno ao estoque do PS-43101 cancelado")
        self.assertEqual(self.chamadas_mover[0][1], [{"product_id": 1, "sku": "A", "qtd": 4, "destinos": [("E1", 4)]}])
        self.assertEqual(self.galpao, [(43101, 100, [{"sku": "A", "nome": "prod A", "movido": 4, "destino": "E1"}])])
        self.assertEqual(self.operacao, [])
        self.mock_adq.assert_called_once()
        self.assertEqual(self.mock_adq.call_args.kwargs["esperar_segundos"], ex.ESPERA_CONTA_SEGUNDOS)
        self.assertEqual(self.mock_adq.call_args.kwargs["ttl_segundos"], 600)
        self.mock_lib.assert_called_once()

    def test_movido_a_menos_vira_parcial_com_os_dois_avisos(self):
        self.fila(1)
        self.retorno_mover = lambda ms: [{"product_id": 1, "destino": "E1", "pedido": 4, "movido": 3,
                                          "adjust_id": 7, "erro": ""}]
        r = self.rodar()
        self.assertEqual((r["parciais"], self.linha(1)["status"]), (1, "PARCIAL"))
        self.assertEqual(len(self.galpao), 1)
        self.assertEqual(self.galpao[0][2][0]["movido"], 3)
        (op,) = self.operacao
        self.assertEqual(op[:2], (1, 100))
        self.assertIn("A", op[2])

    def test_item_sem_destino_deixa_parcial(self):
        self.fila(1)
        self.plano = _plano(itens=[_item(), _item(2, "B", 2, (), "sem_destino")])
        self.rodar()
        self.assertEqual(self.linha(1)["status"], "PARCIAL")
        self.assertEqual(len(self.operacao), 1)
        self.assertEqual([m["product_id"] for m in self.chamadas_mover[0][1]], [1])

    def test_nada_movido_sem_excecao_e_parcial_com_aviso(self):
        self.fila(1)
        self.retorno_mover = lambda ms: [{"product_id": 1, "destino": "E1", "pedido": 4, "movido": 0,
                                          "adjust_id": None, "erro": "recusado"}]
        self.rodar()
        self.assertEqual(self.linha(1)["status"], "PARCIAL")
        self.assertEqual((len(self.galpao), len(self.operacao)), (0, 1))

    def test_itens_ja_devolvidos_dentro_do_mover_nao_sao_movidos(self):
        self.fila(1)
        self.plano = _plano(itens=[_item(), _item(2, "B", 2, (("E2", 2),), "ja_devolvido")])
        self.rodar()
        self.assertEqual([m["product_id"] for m in self.chamadas_mover[0][1]], [1])
        self.assertEqual(self.linha(1)["status"], "FEITA")

    def test_duvida(self):
        self.fila(1)
        self.plano = _plano("DUVIDA", motivo="nao fecha")
        r = self.rodar()
        self.assertEqual((self.linha(1)["status"], r["falhas"]), ("DUVIDA", 1))
        self.assertEqual(self.chamadas_mover, [])
        self.assertEqual(self.operacao, [(1, 100, "nao fecha")])
        self.mock_adq.assert_not_called()

    def test_admin_ocupada_nao_conta_tentativa(self):
        self.fila(1)
        self.admin_ocupado = True
        r = self.rodar()
        self.assertEqual(r["esperando"], 1)
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"]), ("PENDENTE", 0))
        self.assertEqual(self.planos, [])

    def test_provider_ocupada_nao_conta_tentativa(self):
        self.fila(1)
        self.mock_adq.return_value = False
        r = self.rodar()
        self.assertEqual(r["esperando"], 1)
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"]), ("PENDENTE", 0))
        self.assertEqual(self.chamadas_mover, [])
        self.mock_lib.assert_not_called()

    def test_erro_tecnico_tres_ciclos_vira_falha_e_avisa_uma_vez(self):
        self.fila(1)
        self.plano_levanta = ValueError("leitura quebrou")
        for esperado in (1, 2, 3):
            self.rodar()
            self.assertEqual(self.linha(1)["tentativas"], esperado)
        self.assertEqual(self.linha(1)["status"], "FALHA")
        self.assertEqual(len(self.operacao), 1)
        self.rodar()   # FALHA nao volta pra fila
        self.assertEqual(len(self.operacao), 1)
        self.assertEqual(len(self.planos), 3)

    def test_excecao_no_mover_libera_a_provider_e_conta_tentativa(self):
        self.fila(1)
        self.mover_levanta = RuntimeError("navegador caiu")
        self.rodar()
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"]), ("PENDENTE", 1))
        self.mock_lib.assert_called_once()

    def test_excecao_num_pedido_nao_impede_o_seguinte(self):
        self.fila(1, 2)

        def plano(sess, id_stokki, agora, ja_estava=False, ignorar_movimentos=frozenset()):
            if id_stokki == 1:
                raise ValueError("x")
            return self.plano
        ex.montar_plano.side_effect = plano
        r = self.rodar()
        self.assertEqual((self.linha(1)["tentativas"], self.linha(2)["status"], r["feitas"]), (1, "FEITA", 1))

    def test_retomada_so_move_o_que_falta_e_ignora_os_proprios_movimentos(self):
        self.fila(1)
        self.plano = _plano(itens=[_item(1, "A", 4), _item(2, "B", 4, (("E2", 4),))])
        feito = [{"product_id": 1, "sku": "A", "nome": "prod A", "qtd": 4, "situacao": "mover", "movido": 4,
                  "movimentos": [{"destino": "E1", "pedido": 4, "movido": 4, "adjust_id": 555, "erro": ""}]}]
        de.gravar(self.conn, 1, itens=feito, mais_uma_tentativa=True)
        self.rodar()
        self.assertEqual([m["product_id"] for m in self.chamadas_mover[0][1]], [2])
        self.assertIn(555, self.planos[0][2])
        l = self.linha(1)
        self.assertEqual(l["status"], "FEITA")
        itens = {i["product_id"]: i for i in json.loads(l["itens_json"])}
        self.assertEqual((itens[1]["movido"], itens[2]["movido"]), (4, 4))
        self.assertEqual({g["sku"] for g in self.galpao[0][2]}, {"A", "B"})   # o aviso soma as duas tentativas

    def test_retomada_desconta_o_ja_movido_do_mesmo_produto(self):
        self.fila(1)
        feito = [{"product_id": 1, "sku": "A", "nome": "prod A", "qtd": 4, "situacao": "mover", "movido": 1,
                  "movimentos": [{"destino": "E1", "pedido": 1, "movido": 1, "adjust_id": 555, "erro": ""}]}]
        de.gravar(self.conn, 1, itens=feito)
        self.rodar()
        (m,) = self.chamadas_mover[0][1]
        self.assertEqual(m["qtd"], 3)

    def test_movimento_sem_confirmacao_vira_duvida_sem_nova_tentativa(self):
        self.fila(1)
        self.retorno_mover = lambda ms: [{"product_id": 1, "destino": "E1", "pedido": 4, "movido": 0,
                                          "adjust_id": 777, "erro": "movimentacao 777 ainda em processamento"}]
        r = self.rodar()
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"], r["falhas"]), ("DUVIDA", 0, 1))
        self.assertEqual(json.loads(l["itens_json"])[0]["movimentos"][0]["adjust_id"], 777)
        (op,) = self.operacao
        self.assertIn("777", op[2])
        self.assertIn("sem confirmacao", op[2])
        self.assertEqual(self.galpao, [])

    def test_nunca_conta_mais_que_o_bloqueio(self):
        self.fila(1)
        self.retorno_mover = lambda ms: [
            {"product_id": 1, "destino": "E1", "pedido": 4, "movido": 4, "adjust_id": 1, "erro": ""},
            {"product_id": 1, "destino": "E2", "pedido": 4, "movido": 4, "adjust_id": 2, "erro": ""}]
        self.rodar()
        self.assertEqual(self.linha(1)["status"], "DUVIDA")
        self.assertEqual(len(self.operacao), 1)

    def test_simular_so_loga(self):
        self.fila(1)
        with self.assertLogs("stokki.devolucao_executor", level="INFO") as cap:
            r = self.rodar(simular=True)
        self.assertTrue(any("PS-1" in l for l in cap.output))
        self.assertEqual(self.chamadas_mover, [])
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"]), ("PENDENTE", 0))
        self.assertEqual(r, {"feitas": 0, "parciais": 0, "nada": 0, "falhas": 0, "esperando": 0})
        self.mock_adq.assert_not_called()

    def test_no_maximo_cinco_pedidos_por_chamada(self):
        self.fila(*range(1, 9))
        r = self.rodar()
        self.assertEqual(r["feitas"], ex.MAX_POR_CICLO)
        self.assertEqual(len(de.pendentes(self.conn)), 3)

    def test_aviso_que_levanta_nao_desfaz_o_status(self):
        self.fila(1)
        self.plano = _plano("DUVIDA", motivo="x")
        r = ex.processar_pendentes(self.conn, LIGADO, simular=False, abrir_admin=self.abrir_admin, mover=self.mover,
                                   avisar_galpao=lambda *a: None, avisar_operacao=mock.Mock(side_effect=OSError("smtp")))
        self.assertEqual(self.linha(1)["status"], "DUVIDA")
        self.assertEqual(r["falhas"], 1)


    # --- rodada de correcao 1 ---

    def tecnico(self, ms):
        return [{"product_id": ms[0]["product_id"], "destino": "E1", "pedido": 4, "movido": 0, "adjust_id": None,
                 "erro": "sessao caiu (status 401)", "tecnico": True}]

    def feito(self, pid, adjust_id=None, qtd=4):
        return {"product_id": pid, "destino": f"E{pid}", "pedido": qtd, "movido": qtd, "adjust_id": adjust_id or 900 + pid,
                "erro": ""}

    def itens_dois(self):
        return [_item(1, "A", 4), _item(2, "B", 4, (("E2", 4),))]

    def test_c1_retomada_com_movimento_sem_confirmacao_vai_a_duvida_sem_sessao(self):
        self.fila(1)
        de.gravar(self.conn, 1, itens=[{"product_id": 1, "sku": "A", "nome": "a", "qtd": 4, "adjust_id": 777, "movido": 0,
                                        "movimentos": [], "cliente_id": 100}], mais_uma_tentativa=True)
        r = self.rodar()
        self.assertEqual(self.linha(1)["status"], "DUVIDA")
        self.assertEqual((self.admin_abriu, self.planos, self.chamadas_mover), (0, [], []))
        self.mock_adq.assert_not_called()
        (op,) = self.operacao
        self.assertEqual(op[:2], (1, 100))
        self.assertIn("777", op[2])
        self.assertIn("conferir na Stokki antes de qualquer nova devolucao", op[2])
        self.assertEqual(r["falhas"], 1)

    def test_c1_movimento_incerto_sem_id_tambem_trava_a_retomada(self):
        self.fila(1)
        itens = [{"product_id": 1, "sku": "A", "nome": "a", "qtd": 4, "movido": 0, "cliente_id": 100,
                  "movimentos": [{"destino": "E1", "pedido": 4, "movido": 0, "adjust_id": None, "erro": "x", "incerto": True}]}]
        de.gravar(self.conn, 1, itens=itens)
        self.rodar()
        self.assertEqual(self.linha(1)["status"], "DUVIDA")
        self.assertEqual((self.planos, self.chamadas_mover), ([], []))
        self.assertIn("produto A", self.operacao[0][2])

    def test_m1_ids_de_outro_pedido_entram_no_ignorar_mesmo_sem_confirmacao(self):
        # movimento do robo para o pedido 2 nunca prova devolucao do 1; o do proprio pedido so confirmado (R15)
        self.fila(1, 2)
        itens = [{"product_id": 1, "movimentos": [{"adjust_id": 777, "movido": 0}, {"adjust_id": "778", "movido": 4}]}]
        de.gravar(self.conn, 2, itens=itens)
        self.assertEqual(ex.adjust_ids_do_robo(self.conn, 1), frozenset({777, 778}))
        self.assertEqual(ex._ids_dos_itens(itens), {778})

    def test_i1_erro_tecnico_do_mover_conta_tentativa_e_continua_pendente(self):
        self.fila(1)
        self.retorno_mover = self.tecnico
        r = self.rodar()
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"], r["parciais"]), ("PENDENTE", 1, 0))
        self.assertEqual((self.galpao, self.operacao), ([], []))

    def test_i1_tres_rodadas_tecnicas_viram_falha_com_um_aviso(self):
        self.fila(1)
        self.retorno_mover = self.tecnico
        for _ in range(3):
            self.rodar()
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"]), ("FALHA", 3))
        self.assertEqual(len(self.operacao), 1)
        self.rodar()
        self.assertEqual(len(self.operacao), 1)

    def test_i1_o_que_ja_foi_movido_fica_gravado_e_e_descontado_na_retomada(self):
        self.fila(1)
        self.plano = _plano(itens=self.itens_dois())
        self.retorno_mover = lambda ms: [self.feito(1), {**self.tecnico(ms)[0], "product_id": 2}]
        self.rodar()
        self.assertEqual(self.linha(1)["status"], "PENDENTE")
        self.retorno_mover = None
        self.rodar()
        self.assertEqual([m["product_id"] for m in self.chamadas_mover[1][1]], [2])
        self.assertEqual(self.linha(1)["status"], "FEITA")
        self.assertEqual(len(self.galpao), 1)          # um aviso so, com os dois produtos
        self.assertEqual({g["sku"] for g in self.galpao[0][2]}, {"A", "B"})

    def test_i2_post_sem_resposta_clara_vai_a_duvida_nunca_parcial(self):
        self.fila(1)
        self.retorno_mover = lambda ms: [{"product_id": 1, "destino": "E1", "pedido": 4, "movido": 0, "adjust_id": None,
                                          "erro": "store/process sem resposta clara (status 500)", "incerto": True}]
        r = self.rodar()
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"], r["parciais"]), ("DUVIDA", 0, 0))
        (op,) = self.operacao
        self.assertIn("movimentacao do produto A enviada sem resposta clara da Stokki", op[2])
        self.rodar()   # nova passada: a linha nao e mais PENDENTE
        self.assertEqual(len(self.chamadas_mover), 1)

    def test_i3_adjust_id_em_texto_conta(self):
        self.assertEqual(ex._ids_dos_itens([{"movimentos": [{"adjust_id": "110832", "movido": 4},
                                                            {"adjust_id": 5, "movido": 1}, {"adjust_id": "x", "movido": 1}]}]),
                         {110832, 5})

    def test_i5_duvida_depois_de_item_concluido_avisa_o_galpao_e_a_operacao(self):
        self.fila(1)
        self.plano = _plano(itens=self.itens_dois())
        self.retorno_mover = lambda ms: [self.feito(1), {"product_id": 2, "destino": "E2", "pedido": 4, "movido": 0,
                                                         "adjust_id": 777, "erro": "movimentacao 777 ainda em processamento",
                                                         "incerto": True}]
        self.rodar()
        self.assertEqual(self.linha(1)["status"], "DUVIDA")
        self.assertEqual(self.galpao, [(1, 100, [{"sku": "A", "nome": "prod A", "movido": 4, "destino": "E1"}])])
        (op,) = self.operacao
        self.assertIn("777", op[2])
        self.assertIn("A: movido 4 de 4", op[2])
        self.assertTrue(json.loads(self.linha(1)["itens_json"])[0]["movimentos"][0]["avisado"])
        self.rodar()
        self.assertEqual((len(self.galpao), len(self.operacao)), (1, 1))

    def test_i5_falha_depois_de_movimento_avisa_o_galpao(self):
        self.fila(1)
        de.gravar(self.conn, 1, itens=[{"product_id": 1, "sku": "A", "nome": "prod A", "qtd": 4, "situacao": "mover",
                                        "destinos": [], "cliente_id": 100, "movido": 1,
                                        "movimentos": [{"destino": "E1", "pedido": 1, "movido": 1, "adjust_id": 5, "erro": ""}]}])
        self.conn.execute("UPDATE stokki_devolucoes SET tentativas = 2")
        self.conn.commit()
        self.retorno_mover = self.tecnico
        self.rodar()
        self.assertEqual(self.linha(1)["status"], "FALHA")
        self.assertEqual(self.galpao[0][2], [{"sku": "A", "nome": "prod A", "movido": 1, "destino": "E1"}])
        self.assertIn("A: movido 1 de 4", self.operacao[0][2])

    def test_i6_simular_nao_grava_nada_nem_em_excecao(self):
        self.fila(1)
        self.plano_levanta = ValueError("quebrou")
        antes = dict(self.linha(1))
        for _ in range(4):
            self.rodar(simular=True)
        self.assertEqual(self.linha(1), antes)
        self.assertEqual(self.operacao, [])

    def test_i6_simular_com_movimento_incerto_so_loga(self):
        self.fila(1)
        de.gravar(self.conn, 1, itens=[{"product_id": 1, "adjust_id": 777, "movido": 0}])
        antes = dict(self.linha(1))
        self.rodar(simular=True)
        self.assertEqual((self.linha(1), self.operacao), (antes, []))

    def test_i7_dono_da_provider_e_por_processo(self):
        import os
        self.fila(1)
        self.rodar()
        self.assertEqual(self.mock_adq.call_args.args[0], f"devolucao-estoque-{os.getpid()}")
        self.mock_lib.assert_called_once_with(f"devolucao-estoque-{os.getpid()}")

    def test_i7_linha_que_muda_entre_o_plano_e_a_provider_nao_move(self):
        self.fila(1, 2)

        def adquirir(*a, **k):
            if not getattr(self, "_mudou", False):
                self._mudou = True
                self.conn.execute("UPDATE stokki_devolucoes SET status = 'FEITA' WHERE id_stokki = 1")
                self.conn.commit()
            return "provider"
        self.mock_adq.side_effect = adquirir
        r = self.rodar()
        self.assertEqual(len(self.chamadas_mover), 1)
        self.assertEqual((self.linha(1)["status"], self.linha(2)["status"]), ("FEITA", "FEITA"))
        self.assertEqual((r["esperando"], r["feitas"]), (1, 1))
        self.assertEqual(self.mock_lib.call_count, 2)

    def test_i7_itens_que_mudam_entre_o_plano_e_a_provider_nao_movem(self):
        self.fila(1)

        def adquirir(*a, **k):
            de.gravar(self.conn, 1, itens=[{"product_id": 1, "movimentos": []}])
            return "provider"
        self.mock_adq.side_effect = adquirir
        r = self.rodar()
        self.assertEqual(self.chamadas_mover, [])
        self.assertEqual((r["esperando"], self.linha(1)["status"]), (1, "PENDENTE"))
        self.mock_lib.assert_called_once()

    def test_d1_so_conta_ocupada_e_espera_outro_runtime_error_e_tecnico(self):
        self.fila(1)

        @contextlib.contextmanager
        def admin():
            raise RuntimeError("outra coisa")
            yield
        r = ex.processar_pendentes(
            self.conn, LIGADO, simular=False, abrir_admin=admin, mover=self.mover,
            avisar_galpao=lambda *a: None, avisar_operacao=lambda *a: None)
        l = self.linha(1)
        self.assertEqual((r["esperando"], l["tentativas"]), (0, 1))

    def test_d2_provider_ocupada_por_outro_nao_le_nada(self):
        self.fila(1, 2)
        self.mock_ocup.return_value = "estacao-impressao"
        r = self.rodar()
        self.assertEqual((r["esperando"], self.admin_abriu, self.planos), (1, 0, []))
        self.assertEqual(self.linha(1)["tentativas"], 0)

    def test_d2_falha_ao_consultar_a_ocupacao_nao_impede(self):
        self.fila(1)
        self.mock_ocup.side_effect = OSError("banco")
        r = self.rodar()
        self.assertEqual(r["feitas"], 1)

    # --- revisao final ---

    def rodar_sem_agora(self, mover=None, **kw):
        return ex.processar_pendentes(
            self.conn, LIGADO, simular=False, abrir_admin=self.abrir_admin, mover=mover or self.mover,
            avisar_galpao=lambda *a: self.galpao.append(a), avisar_operacao=lambda *a: self.operacao.append(a), **kw)

    def test_c1_agora_e_recalculado_por_pedido(self):
        self.fila(1, 2)
        horas = iter([datetime(2026, 11, 3, 13, 0), datetime(2026, 11, 3, 13, 5), datetime(2026, 11, 3, 13, 9)])
        with mock.patch.object(ex, "datetime") as dt:
            dt.now.side_effect = lambda: next(horas)
            self.rodar_sem_agora()
        self.assertEqual(len(self.agoras), 2)
        self.assertNotEqual(self.agoras[0], self.agoras[1])

    def test_c1_agora_passado_vale_para_todos(self):
        self.fila(1, 2)
        self.rodar()
        self.assertEqual(self.agoras, [datetime(2026, 11, 3, 13, 0)] * 2)

    def mover_pela_tela(self, api):
        """mover de verdade (mover_virtual._executar) contra uma tela falsa."""
        def mover(config, cliente_id, movimentos, motivo, headless=True, ao_concluir=None):
            self.chamadas_mover.append((cliente_id, movimentos, motivo))
            return mv._executar(api, cliente_id, movimentos, motivo, ao_concluir, dormir=lambda s: None)
        self.plano = _plano(itens=[_item(2278, "FABMLT", 4, (("FAB-1", 4), ("FAB-2", 4))),
                                   _item(2282, "FABMO1", 2, (("FAB-1", 2),))])
        return mover

    def test_c2_poll_que_levanta_vira_duvida_com_o_adjust_id(self):
        self.fila(1)
        api = ApiQueLevanta("adjust/processed/")
        r = self.rodar(mover=self.mover_pela_tela(api))
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"], r["falhas"]), ("DUVIDA", 0, 1))
        self.assertEqual(len(api.posts), 1)   # nem outro destino nem o outro produto
        (m,) = json.loads(l["itens_json"])[0]["movimentos"]
        self.assertEqual((m["adjust_id"], m["movido"], m.get("incerto")), (110001, 0, True))
        (op,) = self.operacao
        self.assertIn("110001", op[2])
        self.rodar(mover=self.mover_pela_tela(api))
        self.assertEqual(len(api.posts), 1)

    def test_c2_post_que_levanta_vira_duvida_sem_adjust_id(self):
        self.fila(1)
        api = ApiQueLevanta("post")
        self.rodar(mover=self.mover_pela_tela(api))
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"]), ("DUVIDA", 0))
        self.assertIn("produto FABMLT", self.operacao[0][2])

    def gravar_que_falha(self, falha):
        real = de.gravar

        def gravar(conn, id_stokki, **kw):
            if falha(kw):
                raise sqlite3.OperationalError("database is locked")
            return real(conn, id_stokki, **kw)
        p = mock.patch.object(ex, "gravar", side_effect=gravar)
        p.start()
        self.addCleanup(p.stop)

    def test_c2_gravar_que_falha_no_aplicar_e_regravado_no_encerramento(self):
        self.fila(1)
        n = []
        self.gravar_que_falha(lambda kw: not n and not n.append(1))   # so a primeira chamada falha
        r = self.rodar()
        l = self.linha(1)
        self.assertEqual((l["status"], r["feitas"]), ("FEITA", 1))
        self.assertEqual(json.loads(l["itens_json"])[0]["movimentos"][0]["adjust_id"], 901)

    def test_c2_fila_que_nao_grava_nem_no_encerramento_vai_a_duvida_com_aviso(self):
        self.fila(1)
        self.gravar_que_falha(lambda kw: kw.get("status") != "DUVIDA")
        r = self.rodar()
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"], r["falhas"]), ("DUVIDA", 0, 1))
        self.assertEqual(json.loads(l["itens_json"])[0]["movimentos"][0]["adjust_id"], 901)
        (op,) = self.operacao
        self.assertIn("movimentacao feita mas a fila nao gravou; conferir na Stokki", op[2])
        self.assertEqual(len(self.galpao), 1)

    def test_c2_mover_que_cai_depois_de_movimento_nao_gravado_vai_a_duvida(self):
        self.fila(1)
        self.gravar_que_falha(lambda kw: kw.get("status") is None and "erro" not in kw)
        self.mover_levanta = RuntimeError("navegador caiu")
        self.rodar()
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"]), ("DUVIDA", 0))
        self.assertEqual(json.loads(l["itens_json"])[0]["movimentos"][0]["adjust_id"], 901)

    def test_c2_fila_sem_gravar_nada_avisa_e_sobe_como_tecnico(self):
        self.fila(1)
        self.gravar_que_falha(lambda kw: True)
        self.rodar()
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"]), ("PENDENTE", 0))
        (op,) = self.operacao
        self.assertIn("a fila nao gravou", op[2])
        self.assertIn("A: movido 4 de 4", op[2])

    # I1: reconferencia da prova com a provider na mao, logo antes de mover
    def devolucao(self, id_):
        return {"tipo": "Desbloqueio", "movimento": "Interno", "referencia": "", "quantidade": 4,
                "quando": datetime(2026, 11, 3, 13, 1), "concluido": True, "id": id_}

    def test_i1_reconfere_cada_produto_desde_o_cancelamento_com_o_mesmo_ignorar(self):
        self.fila(1, 2)
        de.gravar(self.conn, 2, itens=[{"product_id": 7, "movimentos": [{"adjust_id": 555, "movido": 1}]}])
        self.conn.execute("UPDATE stokki_devolucoes SET status = 'FEITA' WHERE id_stokki = 2")
        self.plano = _plano(itens=self.itens_dois())
        self.rodar()
        self.assertEqual([(p, d, a) for p, d, a, _ in self.reconferencias], [(1, CANCELADO, None), (2, CANCELADO, None)])
        self.assertEqual(self.reconferencias[0][3], self.planos[0][2])
        self.assertIn(555, self.reconferencias[0][3])
        self.assertEqual(self.admin_abriu, 2)   # plano + reconferencia
        self.assertEqual(self.linha(1)["status"], "FEITA")

    def test_i1_devolucao_nova_na_reconferencia_nao_move(self):
        self.fila(1)
        self.devolucoes_novas = {1: [self.devolucao(110900)]}
        r = self.rodar()
        self.assertEqual(self.chamadas_mover, [])
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"], r["falhas"]), ("DUVIDA", 0, 1))
        (op,) = self.operacao
        self.assertIn("devolucao manual apareceu entre o plano e a movimentacao (produto A)", op[2])
        self.mock_adq.assert_called_once()
        self.mock_lib.assert_called_once()

    def test_i1_linha_ja_vista_no_plano_nao_impede(self):
        self.fila(1)
        self.plano = _plano(itens=[{**_item(), "devolucoes_vistas": [110900]}])
        self.devolucoes_novas = {1: [self.devolucao(110900)]}
        self.rodar()
        self.assertEqual(self.linha(1)["status"], "FEITA")

    def test_i1_admin_ocupada_na_reconferencia_espera_sem_tentativa(self):
        self.fila(1, 2)
        abrir = self.abrir_admin

        def admin():
            if self.admin_abriu >= 1:
                self.admin_ocupado = True
            return abrir()
        r = ex.processar_pendentes(
            self.conn, LIGADO, simular=False, abrir_admin=admin, mover=self.mover,
            avisar_galpao=lambda *a: self.galpao.append(a), avisar_operacao=lambda *a: self.operacao.append(a),
            agora=datetime(2026, 11, 3, 13, 0))
        self.assertEqual(self.chamadas_mover, [])
        self.assertEqual(r["esperando"], 1)
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"]), ("PENDENTE", 0))
        self.mock_lib.assert_called_once()
        self.assertEqual(len(self.planos), 1)   # conta ocupada para o ciclo

    def test_i1_erro_na_reconferencia_e_tecnico_e_libera_a_provider(self):
        self.fila(1)
        ex.ler_devolucoes.side_effect = ValueError("tabela sem aaData")
        self.rodar()
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"]), ("PENDENTE", 1))
        self.assertEqual(self.chamadas_mover, [])
        self.mock_lib.assert_called_once()
        self.assertFalse(self.admin_aberta)

    # I2: AGUARDAR = esperando, sem tentativa, sem aviso, e o laco segue
    def test_i2_aguardar_fica_pendente_e_segue_para_o_proximo(self):
        self.fila(1, 2)

        def plano(sess, id_stokki, agora, ja_estava=False, ignorar_movimentos=frozenset()):
            self.planos.append(id_stokki)
            return _plano("AGUARDAR", [_item(situacao="nada")]) if id_stokki == 1 else self.plano
        ex.montar_plano.side_effect = plano
        r = self.rodar()
        l = self.linha(1)
        self.assertEqual((l["status"], l["tentativas"], l["itens_json"]), ("PENDENTE", 0, None))
        self.assertEqual((r["esperando"], r["feitas"], r["nada"]), (1, 1, 0))
        self.assertEqual(self.linha(2)["status"], "FEITA")
        self.assertEqual(self.operacao, [])
        self.assertEqual([c[0] for c in self.chamadas_mover], [100])

    # AGUARDAR nao rele a Stokki a cada ciclo: so depois de RECONFERIR_AGUARDAR_SEGUNDOS
    def _aguardar_para(self, *ids_aguardar):
        def plano(sess, id_stokki, agora, ja_estava=False, ignorar_movimentos=frozenset()):
            self.planos.append(id_stokki)
            return _plano("AGUARDAR", [_item(situacao="nada")]) if id_stokki in ids_aguardar else self.plano
        ex.montar_plano.side_effect = plano

    def _envelhecer(self, id_stokki, segundos):
        from datetime import timedelta
        antes = (datetime.now() - timedelta(seconds=segundos)).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute("UPDATE stokki_devolucoes SET atualizado_em = ? WHERE id_stokki = ?", (antes, id_stokki))

    def test_aguardar_nao_e_relido_no_ciclo_seguinte(self):
        self.fila(1)
        self._aguardar_para(1)
        self.rodar()
        r = self.rodar()
        self.assertEqual((self.planos, self.admin_abriu, r["esperando"]), ([1], 1, 1))
        self.assertEqual((self.linha(1)["status"], self.linha(1)["tentativas"]), ("PENDENTE", 0))

    def test_aguardar_e_relido_depois_do_intervalo(self):
        self.fila(1)
        self._aguardar_para(1)
        self.rodar()
        self._envelhecer(1, ex.RECONFERIR_AGUARDAR_SEGUNDOS + 1)
        self._aguardar_para()   # o Bloqueio apareceu
        r = self.rodar()
        self.assertEqual((self.planos, r["feitas"]), ([1, 1], 1))
        self.assertEqual((self.linha(1)["status"], self.linha(1)["erro"]), ("FEITA", ""))

    def test_aguardar_nao_ocupa_lugar_do_ciclo(self):
        ids = list(range(1, ex.MAX_POR_CICLO + 3))   # 5 aguardando na frente + 2 de verdade
        self.fila(*ids)
        self._aguardar_para(*ids[:ex.MAX_POR_CICLO])
        r = self.rodar()
        self.assertEqual((r["esperando"], r["feitas"]), (ex.MAX_POR_CICLO, 2))
        r = self.rodar()   # ciclo seguinte: ninguem e relido
        self.assertEqual((len(self.planos), r["esperando"]), (ex.MAX_POR_CICLO + 2, ex.MAX_POR_CICLO))

    def test_erro_tecnico_nao_e_confundido_com_aguardar(self):
        self.fila(1)
        self.plano_levanta = RuntimeError("sessao caiu")
        ex.montar_plano.side_effect = self._montar_plano
        self.rodar()
        self.rodar()
        self.assertEqual(self.linha(1)["tentativas"], 2)

    # I4: orcamento de tempo por ciclo: nao comeca pedido novo depois de 60 s
    def test_i4_passou_do_orcamento_nao_comeca_o_proximo(self):
        self.fila(1, 2, 3)
        relogio = [1000.0]

        def plano(sess, id_stokki, agora, ja_estava=False, ignorar_movimentos=frozenset()):
            self.planos.append(id_stokki)
            relogio[0] += 70   # o primeiro pedido demora 70 s
            return self.plano
        ex.montar_plano.side_effect = plano
        r = self.rodar_sem_agora(relogio=lambda: relogio[0])
        self.assertEqual((self.planos, r["feitas"]), ([1], 1))
        self.assertEqual(self.linha(1)["status"], "FEITA")
        for i in (2, 3):
            l = self.linha(i)
            self.assertEqual((l["status"], l["tentativas"], l["itens_json"]), ("PENDENTE", 0, None))

    def test_i4_dentro_do_orcamento_segue(self):
        self.fila(1, 2)
        relogio = [0.0]

        def plano(sess, id_stokki, agora, ja_estava=False, ignorar_movimentos=frozenset()):
            self.planos.append(id_stokki)
            relogio[0] += 25
            return self.plano
        ex.montar_plano.side_effect = plano
        r = self.rodar_sem_agora(relogio=lambda: relogio[0])
        self.assertEqual(r["feitas"], 2)
        self.assertEqual(ex.ORCAMENTO_SEGUNDOS, 60)

    # I5: fechamento sem plano de mover depois de ja ter movido avisa o galpao e a operacao
    def test_i5_retomada_que_vira_ja_devolvido_avisa_o_galpao_uma_vez(self):
        self.fila(1)
        feito = [{"product_id": 1, "sku": "A", "nome": "prod A", "qtd": 4, "situacao": "mover", "movido": 4,
                  "cliente_id": 100,
                  "movimentos": [{"destino": "E1", "pedido": 4, "movido": 4, "adjust_id": 555, "erro": ""}]}]
        de.gravar(self.conn, 1, itens=feito, mais_uma_tentativa=True)
        self.plano = _plano("JA_DEVOLVIDO", [_item(1, "A", 4, (), "ja_devolvido"), _item(2, "B", 0, (), "nada")])
        r = self.rodar()
        l = self.linha(1)
        self.assertEqual((l["status"], r["nada"]), ("JA_DEVOLVIDO", 1))
        self.assertEqual(self.galpao, [(1, 100, [{"sku": "A", "nome": "prod A", "movido": 4, "destino": "E1"}])])
        (op,) = self.operacao
        self.assertIn("A: movido 4 de 4", op[2])
        self.assertTrue(json.loads(l["itens_json"])[0]["movimentos"][0]["avisado"])
        self.assertEqual(self.chamadas_mover, [])
        self.rodar()
        self.assertEqual((len(self.galpao), len(self.operacao)), (1, 1))

    def test_i5_movimento_ja_avisado_nao_repete(self):
        self.fila(1)
        feito = [{"product_id": 1, "sku": "A", "nome": "prod A", "qtd": 4, "movido": 4, "cliente_id": 100,
                  "movimentos": [{"destino": "E1", "movido": 4, "adjust_id": 555, "erro": "", "avisado": True}]}]
        de.gravar(self.conn, 1, itens=feito)
        self.plano = _plano("NADA", [_item(1, "A", 0, (), "nada")])
        self.rodar()
        self.assertEqual((self.linha(1)["status"], self.galpao, self.operacao), ("NADA", [], []))

    # M5: o motivo fica gravado na fila em todo status final
    def test_m5_status_sem_mover_grava_o_motivo(self):
        for status in ("NADA", "JA_DEVOLVIDO", "ANTIGO"):
            de.garantir_tabela(self.conn)
            self.conn.execute("DELETE FROM stokki_devolucoes")
            self.fila(1)
            self.plano = _plano(status, [_item(situacao="nada")], motivo=f"motivo de {status}")
            self.rodar()
            self.assertEqual((self.linha(1)["status"], self.linha(1)["erro"]), (status, f"motivo de {status}"))

    def test_m5_parcial_grava_o_resumo_e_diz_quando_nao_ha_endereco(self):
        self.fila(1)
        self.plano = _plano(itens=[_item(), _item(2, "B", 2, (), "sem_destino")])
        self.rodar()
        l = self.linha(1)
        self.assertEqual(l["status"], "PARCIAL")
        self.assertIn("A: movido 4 de 4", l["erro"])
        self.assertIn("B: movido 0 de 2 (ficaram 2 no Virtual; nenhum endereco fisico encontrado)", l["erro"])
        self.assertIn("nenhum endereco fisico encontrado", self.operacao[0][2])

    def test_m5_feita_nao_grava_erro(self):
        self.fila(1)
        self.rodar()
        self.assertEqual((self.linha(1)["status"], self.linha(1)["erro"]), ("FEITA", ""))


class TestAdjustIdsDoRobo(unittest.TestCase):
    def test_junta_os_ids_das_outras_linhas_de_qualquer_status(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        for i, st, ids in ((1, "FEITA", [10, 11]), (2, "DUVIDA", [12]), (3, "PENDENTE", [13]), (4, "NADA", [])):
            de.registrar(i, "portal", LIGADO, conn=conn)
            itens = [{"product_id": 1, "movimentos": [{"adjust_id": a, "movido": 1} for a in ids] + [{"adjust_id": None}]}]
            de.gravar(conn, i, status=st, itens=itens)
        self.assertEqual(ex.adjust_ids_do_robo(conn, 3), frozenset({10, 11, 12}))
        self.assertEqual(ex.adjust_ids_do_robo(conn, 99), frozenset({10, 11, 12, 13}))

    def test_itens_json_ruim_nao_derruba(self):
        conn = sqlite3.connect(":memory:")
        de.garantir_tabela(conn)
        conn.execute("INSERT INTO stokki_devolucoes (id_stokki, status, itens_json) VALUES (1, 'FEITA', 'lixo')")
        self.assertEqual(ex.adjust_ids_do_robo(conn, 2), frozenset())


if __name__ == "__main__":
    unittest.main()
