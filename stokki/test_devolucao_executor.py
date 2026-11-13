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

LIGADO = {"stokki": {"devolucao_cancelamento": {"ativo": True}}}


# --- Executor da fila ---

def _item(pid=1, sku="A", qtd=4, destinos=(("E1", 4),), situacao="mover"):
    return {"product_id": pid, "sku": sku, "nome": f"prod {sku}", "qtd": qtd, "destinos": list(destinos),
            "situacao": situacao}


def _plano(status="MOVER", itens=None, motivo=""):
    return {"status": status, "cliente_id": 100, "itens": itens if itens is not None else [_item()], "motivo": motivo}


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.galpao, self.operacao, self.chamadas_mover, self.planos = [], [], [], []
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

    def _montar_plano(self, sess, id_stokki, agora, ja_estava=False, ignorar_movimentos=frozenset()):
        self.planos.append((id_stokki, ja_estava, frozenset(ignorar_movimentos)))
        if self.plano_levanta:
            raise self.plano_levanta
        return self.plano

    @contextlib.contextmanager
    def abrir_admin(self):
        if self.admin_ocupado:
            raise ex.ContaOcupada("ocupada")
        self.admin_abriu += 1
        yield object()

    def mover(self, config, cliente_id, movimentos, motivo, headless=True, ao_concluir=None):
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

    def test_c1_id_nao_confirmado_nunca_entra_no_ignorar(self):
        self.fila(1, 2)
        de.gravar(self.conn, 2, itens=[{"product_id": 1, "movimentos": [
            {"adjust_id": 777, "movido": 0}, {"adjust_id": 778, "movido": 4}]}])
        self.assertEqual(ex.adjust_ids_do_robo(self.conn, 1), frozenset({778}))

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
