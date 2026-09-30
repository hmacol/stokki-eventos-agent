# -*- coding: utf-8 -*-
"""
test_consulta.py

Testes da camada de consulta de rotas/pedidos (nucleo/consulta.py, Hugo
12/09). Nenhum teste bate na VUUPT nem no dados.db real -- cada um usa um
SQLite temporário. Rodar (a partir da raiz do repo):
    python -m unittest nucleo.test_consulta -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco, consulta  # noqa: E402

SENDER_A = 22812827   # embarcador do cliente de teste do portal
SENDER_B = 999001     # outro embarcador na MESMA rota


class _BaseTemp(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors: no Windows o SQLite segura o handle do
        # arquivo até o GC, e o rmtree do tearDown falha à toa.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "teste.db"
        self._patch = mock.patch.object(banco, "DB_PATH", self.db)
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    # ── fixtures ──────────────────────────────────────────────────────────────

    def _rota(self, data_rota, nome="Rota 1", agent_id=501, motorista="JOAO DA SILVA",
              status=banco.ROTA_CONCLUIDA if hasattr(banco, "ROTA_CONCLUIDA") else "CONCLUIDA",
              provedor="VUUPT"):
        cur = self.conn.execute("""
            INSERT INTO nucleo_rotas (data_rota, nome, provedor, agent_id, motorista_nome, status, start_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (data_rota, nome, provedor, agent_id, motorista, status, f"{data_rota} 06:00:00"))
        self.conn.commit()
        return cur.lastrowid

    def _parada(self, rota_id, ordem, codigo, sender_id=SENDER_A, destinatario="MERCADO CENTRAL",
                remetente="FRUTA FINA", endereco="RUA DAS FLORES, 100", situacao="ENTREGUE"):
        cur = self.conn.execute("""
            INSERT INTO nucleo_paradas (rota_id, ordem, codigo, sender_id, destinatario_nome,
                                        remetente_nome, endereco, situacao)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (rota_id, ordem, codigo, sender_id, destinatario, remetente, endereco, situacao))
        self.conn.commit()
        return cur.lastrowid

    def _pedido(self, codigo, sender_id=SENDER_A, criado_em=None, destinatario="MERCADO CENTRAL",
                remetente="FRUTA FINA", agendamento=None):
        self.conn.execute("""
            INSERT INTO nucleo_pedidos (codigo, sender_id, destinatario_nome, remetente_nome,
                                        endereco, agendamento_inicio, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now','localtime')))
        """, (codigo, sender_id, destinatario, remetente, "RUA DAS FLORES, 100", agendamento, criado_em))
        self.conn.commit()


class TestClassificarTermo(unittest.TestCase):
    def test_placa_nos_dois_formatos(self):
        for bruto in ("ABC1D23", "abc1d23", "ABC-1D23", "ABC 1234", "ABC1234"):
            tipo, limpo = consulta.classificar_termo(bruto)
            self.assertEqual(tipo, consulta.TERMO_PLACA, bruto)
            self.assertNotIn("-", limpo)
            self.assertEqual(limpo, limpo.upper())

    def test_numero_solto_e_rota(self):
        self.assertEqual(consulta.classificar_termo("609")[0], consulta.TERMO_ROTA)

    def test_codigo_de_pedido(self):
        self.assertEqual(consulta.classificar_termo("PS-12345")[0], consulta.TERMO_PEDIDO)
        self.assertEqual(consulta.classificar_termo("ps-12345")[1], "PS-12345")

    def test_texto_livre(self):
        self.assertEqual(consulta.classificar_termo("joão da silva")[0], consulta.TERMO_TEXTO)

    def test_vazio(self):
        for bruto in (None, "", "   "):
            self.assertEqual(consulta.classificar_termo(bruto)[0], consulta.TERMO_VAZIO)


class TestBusca(_BaseTemp):
    def setUp(self):
        super().setUp()
        self.rota_id = self._rota("2026-09-10")
        self._parada(self.rota_id, 1, "PS-11111")
        self._parada(self.rota_id, 2, "PS-22222", sender_id=SENDER_B, remetente="OUTRO EMBARCADOR",
                     destinatario="PADARIA DO ZE")
        self._pedido("PS-11111")
        self._pedido("PS-22222", sender_id=SENDER_B, destinatario="PADARIA DO ZE")

    def test_por_codigo_traz_pedido_e_rota(self):
        r = consulta.buscar("PS-11111", conn=self.conn)
        self.assertEqual(r["interpretado"], consulta.TERMO_PEDIDO)
        self.assertEqual([p["codigo"] for p in r["pedidos"]], ["PS-11111"])
        self.assertEqual([x["id"] for x in r["rotas"]], [self.rota_id])

    def test_numero_solto_acha_rota_por_id_e_pedido_pelo_sufixo(self):
        r = consulta.buscar(str(self.rota_id), conn=self.conn)
        self.assertIn(self.rota_id, [x["id"] for x in r["rotas"]])

        r2 = consulta.buscar("11111", conn=self.conn)
        self.assertEqual([p["codigo"] for p in r2["pedidos"]], ["PS-11111"])
        # e a rota onde esse pedido entrou também aparece
        self.assertIn(self.rota_id, [x["id"] for x in r2["rotas"]])

    def test_rota_por_id_ignora_o_periodo(self):
        # Rota velha, fora da janela padrão de 60 dias: quem digita o
        # número quer AQUELA rota, não "a rota se ela for recente".
        antiga = self._rota("2025-01-05", nome="Rota velha")
        r = consulta.buscar(str(antiga), conn=self.conn)
        self.assertEqual([x["id"] for x in r["rotas"]], [antiga])

    def test_texto_acha_por_motorista_e_por_destinatario(self):
        por_motorista = consulta.buscar("JOAO", de="2026-09-01", ate="2026-09-30", conn=self.conn)
        self.assertIn(self.rota_id, [x["id"] for x in por_motorista["rotas"]])

        por_destinatario = consulta.buscar("PADARIA", de="2026-09-01", ate="2026-09-30", conn=self.conn)
        self.assertIn(self.rota_id, [x["id"] for x in por_destinatario["rotas"]])
        self.assertEqual([p["codigo"] for p in por_destinatario["pedidos"]], ["PS-22222"])

    def test_termo_vazio_nao_varre_o_banco(self):
        r = consulta.buscar("  ", conn=self.conn)
        self.assertEqual((r["rotas"], r["pedidos"]), ([], []))

    def test_placa_resolvida_pelo_catalogo_de_motoristas(self):
        with mock.patch.object(consulta, "resolver_placa", return_value=[501]) as fake:
            r = consulta.buscar("ABC1D23", de="2026-09-01", ate="2026-09-30", conn=self.conn)
        fake.assert_called_once()
        self.assertEqual([x["id"] for x in r["rotas"]], [self.rota_id])

    def test_placa_sem_catalogo_nao_derruba_a_busca(self):
        # Catálogo indisponível (sem pandas, planilha ausente) devolve []
        with mock.patch.object(consulta, "resolver_placa", return_value=[]):
            r = consulta.buscar("ABC1D23", conn=self.conn)
        self.assertEqual(r["rotas"], [])


class TestRecorteDoEmbarcador(_BaseTemp):
    """O que o portal do cliente vai usar: sender_id vindo da sessão."""

    def setUp(self):
        super().setUp()
        self.rota_id = self._rota("2026-09-10")
        self.parada_a = self._parada(self.rota_id, 1, "PS-11111", sender_id=SENDER_A)
        self.parada_b = self._parada(self.rota_id, 2, "PS-22222", sender_id=SENDER_B)
        self._pedido("PS-11111", sender_id=SENDER_A)
        self._pedido("PS-22222", sender_id=SENDER_B)

    def test_detalhe_esconde_paradas_de_outro_embarcador(self):
        so_a = consulta.detalhar_rota(self.rota_id, sender_id=SENDER_A, conn=self.conn)
        self.assertEqual([p["codigo"] for p in so_a["paradas"]], ["PS-11111"])

        so_b = consulta.detalhar_rota(self.rota_id, sender_id=SENDER_B, conn=self.conn)
        self.assertEqual([p["codigo"] for p in so_b["paradas"]], ["PS-22222"])

        interno = consulta.detalhar_rota(self.rota_id, conn=self.conn)
        self.assertEqual(len(interno["paradas"]), 2)

    def test_rota_de_terceiro_nao_existe_pro_cliente(self):
        outra = self._rota("2026-09-11", nome="Rota de outro")
        self._parada(outra, 1, "PS-33333", sender_id=SENDER_B)
        self.assertIsNone(consulta.detalhar_rota(outra, sender_id=SENDER_A, conn=self.conn))
        self.assertNotIn(outra, [r["id"] for r in consulta.listar_rotas(
            de="2026-09-01", ate="2026-09-30", sender_id=SENDER_A, conn=self.conn)["rotas"]])

    def test_pedido_de_terceiro_nao_existe_pro_cliente(self):
        self.assertIsNone(consulta.detalhar_pedido("PS-22222", sender_id=SENDER_A, conn=self.conn))
        self.assertIsNotNone(consulta.detalhar_pedido("PS-22222", sender_id=SENDER_B, conn=self.conn))

    def test_busca_do_cliente_nao_vaza_pedido_alheio(self):
        r = consulta.buscar("PS-22222", sender_id=SENDER_A, conn=self.conn)
        self.assertEqual((r["rotas"], r["pedidos"]), ([], []))

        por_texto = consulta.buscar("RUA DAS FLORES", de="2026-09-01", ate="2026-09-30",
                                    sender_id=SENDER_A, conn=self.conn)
        for rota in por_texto["rotas"]:
            detalhe = consulta.detalhar_rota(rota["id"], sender_id=SENDER_A, conn=self.conn)
            self.assertTrue(all(p["sender_id"] == SENDER_A for p in detalhe["paradas"]))

    def test_listagem_conta_só_as_paradas_do_cliente(self):
        rotas = consulta.listar_rotas(de="2026-09-01", ate="2026-09-30", sender_id=SENDER_A,
                                      conn=self.conn)["rotas"]
        self.assertEqual([r["paradas_do_cliente"] for r in rotas], [1])


class TestListagens(_BaseTemp):
    def test_pedido_usa_a_data_da_rota_e_nao_criado_em(self):
        # O backfill de 60 dias (12/09) trouxe pedido antigo com
        # criado_em de HOJE -- por criado_em ele apareceria como de
        # setembro. A data que vale é a da rota em que ele entrou.
        rota = self._rota("2026-07-20")
        self._parada(rota, 1, "PS-70000")
        self._pedido("PS-70000", criado_em="2026-09-12 13:42:00")

        julho = consulta.listar_pedidos(de="2026-07-01", ate="2026-07-31", conn=self.conn)
        self.assertEqual([p["codigo"] for p in julho["pedidos"]], ["PS-70000"])
        self.assertEqual(julho["pedidos"][0]["data_referencia"], "2026-07-20")

        setembro = consulta.listar_pedidos(de="2026-09-01", ate="2026-09-30", conn=self.conn)
        self.assertEqual(setembro["pedidos"], [])

    def test_pedido_sem_rota_cai_no_agendamento(self):
        self._pedido("PS-80000", criado_em="2026-09-12 13:42:00", agendamento="2026-08-05 08:00:00")
        agosto = consulta.listar_pedidos(de="2026-08-01", ate="2026-08-31", conn=self.conn)
        self.assertEqual([p["codigo"] for p in agosto["pedidos"]], ["PS-80000"])

    def test_rotas_mais_recentes_primeiro_e_truncado(self):
        for dia in ("2026-09-08", "2026-09-09", "2026-09-10"):
            self._rota(dia, nome=f"Rota {dia}")
        r = consulta.listar_rotas(de="2026-09-01", ate="2026-09-30", conn=self.conn)
        self.assertEqual([x["data_rota"] for x in r["rotas"]], ["2026-09-10", "2026-09-09", "2026-09-08"])
        self.assertFalse(r["truncado"])

        limitado = consulta.listar_rotas(de="2026-09-01", ate="2026-09-30", limite=2, conn=self.conn)
        self.assertEqual(len(limitado["rotas"]), 2)
        self.assertTrue(limitado["truncado"])

    def test_periodo_invertido_e_corrigido(self):
        self._rota("2026-09-10")
        r = consulta.listar_rotas(de="2026-09-30", ate="2026-09-01", conn=self.conn)
        self.assertEqual(r["periodo"], {"de": "2026-09-01", "ate": "2026-09-30"})
        self.assertEqual(len(r["rotas"]), 1)

    def test_filtros_de_status_provedor_e_motorista(self):
        a = self._rota("2026-09-10", agent_id=501, status="CONCLUIDA")
        self._rota("2026-09-10", agent_id=777, status="CANCELADA", provedor="APP", motorista="MARIA")
        so_a = consulta.listar_rotas(de="2026-09-01", ate="2026-09-30", agent_id=501, conn=self.conn)
        self.assertEqual([x["id"] for x in so_a["rotas"]], [a])
        so_app = consulta.listar_rotas(de="2026-09-01", ate="2026-09-30", provedor="APP", conn=self.conn)
        self.assertEqual([x["motorista_nome"] for x in so_app["rotas"]], ["MARIA"])
        canceladas = consulta.listar_rotas(de="2026-09-01", ate="2026-09-30", status="cancelada", conn=self.conn)
        self.assertEqual(len(canceladas["rotas"]), 1)

    def test_motoristas_com_rota(self):
        self._rota("2026-09-10", agent_id=501, motorista="JOAO DA SILVA")
        self._rota("2026-09-11", agent_id=501, motorista="JOAO DA SILVA")
        self._rota("2026-09-11", agent_id=777, motorista="MARIA")
        lista = consulta.motoristas_com_rota("2026-09-01", "2026-09-30", conn=self.conn)
        self.assertEqual([(m["motorista_nome"], m["rotas"]) for m in lista],
                         [("JOAO DA SILVA", 2), ("MARIA", 1)])


class TestDetalhe(_BaseTemp):
    def test_rota_traz_paradas_na_ordem_com_fotos_eventos_e_pedagio(self):
        rota = self._rota("2026-09-10")
        p2 = self._parada(rota, 2, "PS-22222")
        p1 = self._parada(rota, 1, "PS-11111")
        self.conn.execute("""INSERT INTO nucleo_comprovantes (parada_id, rota_id, tipo, caminho_local,
                                                              resultado_validacao)
                             VALUES (?, ?, 'CANHOTO', 'fotos/a.jpg', 'APROVADO')""", (p1, rota))
        self.conn.execute("""INSERT INTO nucleo_eventos (rota_id, parada_id, tipo, origem, ocorrido_em)
                             VALUES (?, ?, 'ENTREGUE', 'APP', '2026-09-10 09:30:00')""", (rota, p1))
        self.conn.execute("""INSERT INTO nucleo_pedagios (rota_id, valor_informado, status)
                             VALUES (?, 12.5, 'APROVADO')""", (rota,))
        self.conn.commit()

        d = consulta.detalhar_rota(rota, conn=self.conn)
        self.assertEqual([p["id"] for p in d["paradas"]], [p1, p2])
        self.assertEqual(d["paradas"][0]["comprovantes"][0]["tipo"], "CANHOTO")
        self.assertEqual([e["tipo"] for e in d["paradas"][0]["eventos"]], ["ENTREGUE"])
        self.assertEqual(d["paradas"][1]["comprovantes"], [])
        self.assertEqual([float(x["valor_informado"]) for x in d["pedagios"]], [12.5])

    def test_rota_inexistente(self):
        self.assertIsNone(consulta.detalhar_rota(4242, conn=self.conn))

    def test_pedido_reentregue_aparece_nas_duas_rotas(self):
        r1 = self._rota("2026-09-09", nome="Primeira tentativa")
        r2 = self._rota("2026-09-10", nome="Reentrega")
        self._parada(r1, 1, "PS-11111", situacao="INSUCESSO")
        self._parada(r2, 1, "PS-11111", situacao="ENTREGUE")
        self._pedido("PS-11111")

        d = consulta.detalhar_pedido("ps-11111", conn=self.conn)
        self.assertEqual(len(d["paradas"]), 2)
        # mais recente primeiro
        self.assertEqual([p["data_rota"] for p in d["paradas"]], ["2026-09-10", "2026-09-09"])
        self.assertEqual([p["situacao"] for p in d["paradas"]], ["ENTREGUE", "INSUCESSO"])
        self.assertEqual(d["pedido"]["codigo"], "PS-11111")

    def test_parada_sem_linha_em_nucleo_pedidos_ainda_aparece(self):
        # Rota de julho recuperada pelo backfill: a parada existe, o
        # pedido pode não ter linha própria (nucleo_pedidos só começou
        # em 26/08). A consulta não pode devolver "não achei".
        rota = self._rota("2026-07-20")
        self._parada(rota, 1, "PS-70000")
        d = consulta.detalhar_pedido("PS-70000", conn=self.conn)
        self.assertIsNone(d["pedido"])
        self.assertEqual(len(d["paradas"]), 1)
        self.assertEqual(d["paradas"][0]["rota_nome"], "Rota 1")

    def test_pedido_inexistente(self):
        self.assertIsNone(consulta.detalhar_pedido("PS-00000", conn=self.conn))
        self.assertIsNone(consulta.detalhar_pedido("", conn=self.conn))


class TestCobertura(_BaseTemp):
    def test_janela_do_historico(self):
        self._rota("2026-07-15")
        self._rota("2026-09-11")
        c = consulta.cobertura(conn=self.conn)
        self.assertEqual((c["rotas"]["n"], c["rotas"]["de"], c["rotas"]["ate"]),
                         (2, "2026-07-15", "2026-09-11"))


if __name__ == "__main__":
    unittest.main()
