# -*- coding: utf-8 -*-
"""
Materializacao do servico na Vuupt so ao enviar (pool direto da Stokki, Task 5):
trava por codigo, idempotencia por code, recusa com motivo. Vuupt falsa, banco
temporario, sem rede. Rodar (da raiz):
    py -3.11 -m unittest nucleo.test_materializar -v
"""
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from nucleo import banco, materializar, normalizacao, pedidos


def _payload(codigo="PS-1", **extra):
    p = {"title": f"#{codigo} - NF1 / EMB / DEST", "code": f"#{codigo}", "type": "delivery", "dimension_3": 2,
         "sender_id": 11, "customer": {"name": "DEST", "code": "123", "address": "RUA A 1, Sao Paulo - SP"},
         "scheduled_start": "2026-11-27T07:00:00-03:00", "scheduled_end": "2026-11-27T10:00:00-03:00",
         "_skill_nome": "Seco-2"}
    p.update(extra)
    return p


class FakeVuupt:
    """So o que a materializacao usa; criar_ou_atualizar_servico guarda o servico criado
    (buscar_servico_por_code passa a acha-lo, como na Vuupt de verdade)."""

    def __init__(self, demora=0.0, acao="criado", falhar_em=()):
        self.demora, self.acao, self.falhar_em = demora, acao, set(falhar_em)
        self.existentes: dict[str, dict] = {}
        self.criados: list[dict] = []
        self.fingerprints: list[dict] = []
        self.chamadas: list[str] = []
        self._lock = threading.Lock()
        self._proximo = 900

    def buscar_servico_por_code(self, code):
        self.chamadas.append(f"buscar {code}")
        return self.existentes.get(normalizacao.normalizar_codigo(code))

    def skill_ids_por_nome(self, *nomes):
        self.chamadas.append(f"skills {nomes}")
        return [55]

    def salvar_fingerprint_importacao(self, payload, customer_id=None, sender_id=None):
        self.chamadas.append(f"fingerprint {payload['code']}")
        self.fingerprints.append(payload)

    def criar_ou_atualizar_servico(self, payload):
        self.chamadas.append(f"criar {payload['code']}")
        time.sleep(self.demora)
        codigo = normalizacao.normalizar_codigo(payload["code"])
        if codigo in self.falhar_em:
            raise RuntimeError("Status 500: erro da Vuupt")
        if self.acao != "criado":
            return None, self.acao
        with self._lock:
            sid = self._proximo
            self._proximo += 1
            servico = {"id": sid, "code": payload["code"], "status": "not_assigned"}
            self.existentes[codigo] = servico
            self.criados.append(payload)
        return {"service": servico}, "criado"


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "t.db"
        p = mock.patch.object(banco, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)
        self.conn = banco.conectar()
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE rascunhos_parada (id INTEGER PRIMARY KEY, rascunho_id INTEGER NOT NULL,
                ordem INTEGER, service_id INTEGER NOT NULL, codigo TEXT, UNIQUE(rascunho_id, service_id));
        """)

    def _pedido(self, codigo="PS-1", status=None, **payload_extra) -> int:
        """Pedido sem servico como o pipeline grava com fonte stokki; devolve o id sintetico."""
        pedidos.registrar_importacao(_payload(codigo, **payload_extra), None, "nucleo_sem_servico", conn=self.conn)
        if status:
            self.conn.execute("UPDATE nucleo_pedidos SET status = ? WHERE codigo = ?", (status, codigo))
            self.conn.commit()
        return pedidos.id_sintetico(pedidos.buscar_pedido(codigo, conn=self.conn))

    def _no_rascunho(self, sid, codigo, rascunho_id=1):
        self.conn.execute("INSERT INTO rascunhos_parada (rascunho_id, ordem, service_id, codigo) VALUES (?, 0, ?, ?)",
                          (rascunho_id, sid, f"#{codigo}"))
        self.conn.commit()

    def _travas(self):
        return self.conn.execute("SELECT COUNT(*) FROM nucleo_materializacao").fetchone()[0]


class MaterializarTest(_Base):
    def test_cria_servico_e_adota_id(self):
        sid = self._pedido()
        self._no_rascunho(sid, "PS-1")
        # o Planejamento editou o endereco e a data no nucleo: as colunas valem
        self.conn.execute("UPDATE nucleo_pedidos SET endereco = 'RUA NOVA 2', agendamento_inicio = '2026-11-28 08:00:00', "
                          "latitude = -23.5, longitude = -46.6 WHERE codigo = 'PS-1'")
        self.conn.commit()
        vuupt = FakeVuupt()
        res = materializar.materializar_servicos([{"id": sid, "code": "#PS-1"}], vuupt, {}, self.conn)
        self.assertEqual(res.ok, {"PS-1": 900})
        self.assertEqual(res.falhas, {})
        self.assertEqual(pedidos.buscar_pedido("PS-1", conn=self.conn)["vuupt_service_id"], 900)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT service_id FROM rascunhos_parada")], [900])
        (enviado,) = vuupt.criados
        self.assertEqual(enviado["code"], "#PS-1")
        self.assertEqual(enviado["customer"]["address"], "RUA NOVA 2")
        self.assertEqual((enviado["latitude"], enviado["customer"]["longitude"]), (-23.5, -46.6))
        self.assertEqual(enviado["scheduled_start"], "2026-11-28T08:00:00-03:00")
        self.assertEqual(enviado["scheduled_end"], "2026-11-27T10:00:00-03:00")
        self.assertEqual(enviado["skills"], [{"id": 55}])
        self.assertFalse([k for k in enviado if k.startswith("_")])
        self.assertEqual(self._travas(), 0)

    def test_clique_duplo_cria_um_servico_so(self):
        sid = self._pedido()
        vuupt = FakeVuupt(demora=0.4)
        resultados = []

        def enviar():
            resultados.append(materializar.materializar_servicos([{"id": sid, "code": "#PS-1"}], vuupt, {}))

        threads = [threading.Thread(target=enviar) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(vuupt.criados), 1)
        self.assertEqual([r.ok for r in resultados], [{"PS-1": 900}, {"PS-1": 900}])
        self.assertEqual(self._travas(), 0)

    def test_falha_de_um_nao_derruba_os_outros(self):
        ids = [self._pedido(c) for c in ("PS-1", "PS-2", "PS-3")]
        vuupt = FakeVuupt(falhar_em={"PS-2"})
        res = materializar.materializar_servicos(
            [{"id": i, "code": f"#PS-{n}"} for n, i in enumerate(ids, 1)], vuupt, {}, self.conn)
        self.assertEqual(res.ok, {"PS-1": 900, "PS-3": 901})
        self.assertIn("500", res.falhas["PS-2"])
        self.assertIsNone(pedidos.buscar_pedido("PS-2", conn=self.conn)["vuupt_service_id"])
        self.assertEqual(self._travas(), 0)   # trava liberada mesmo na falha

    def test_pulado_atribuido_falha_com_motivo(self):
        sid = self._pedido()
        res = materializar.materializar_servicos([{"id": sid}], FakeVuupt(acao="pulado_atribuido"), {}, self.conn)
        self.assertEqual(res.ok, {})
        self.assertIn("pulado_atribuido", res.falhas["PS-1"])
        self.assertIsNone(pedidos.buscar_pedido("PS-1", conn=self.conn)["vuupt_service_id"])

    def test_servico_existente_fechado_falha_sem_criar(self):
        sid = self._pedido()
        vuupt = FakeVuupt()
        vuupt.existentes["PS-1"] = {"id": 77, "code": "#PS-1", "status": "done"}
        res = materializar.materializar_servicos([{"id": sid}], vuupt, {}, self.conn)
        self.assertIn("'done'", res.falhas["PS-1"])
        self.assertEqual(vuupt.criados, [])

    def test_servico_ja_existente_livre_e_adotado_sem_criar(self):
        sid = self._pedido()
        self._no_rascunho(sid, "PS-1")
        vuupt = FakeVuupt()
        vuupt.existentes["PS-1"] = {"id": 77, "code": "#PS-1", "status": "not_assigned"}
        res = materializar.materializar_servicos([{"id": sid}], vuupt, {}, self.conn)
        self.assertEqual(res.ok, {"PS-1": 77})
        self.assertEqual(vuupt.criados, [])
        self.assertEqual(pedidos.buscar_pedido("PS-1", conn=self.conn)["vuupt_service_id"], 77)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT service_id FROM rascunhos_parada")], [77])

    def test_reentrega_R_sem_id_falha_com_motivo(self):
        sid = self._pedido("PS-1-R1")
        vuupt = FakeVuupt()
        res = materializar.materializar_servicos([{"id": sid, "code": "#PS-1-R1"}], vuupt, {}, self.conn)
        self.assertIn("reentrega", res.falhas["PS-1-R1"])
        self.assertEqual(vuupt.chamadas, [])

    def test_pedido_nao_aberto_recusado(self):
        sid = self._pedido(status=banco.PEDIDO_CANCELADO)
        vuupt = FakeVuupt()
        res = materializar.materializar_servicos([{"id": sid}], vuupt, {}, self.conn)
        self.assertIn("CANCELADO", res.falhas["PS-1"])
        self.assertEqual(vuupt.chamadas, [])

    def test_modo_teste_nao_chama_a_vuupt(self):
        sid = self._pedido()
        vuupt = FakeVuupt()
        res = materializar.materializar_servicos([{"id": sid}], vuupt, {"modo_teste": True}, self.conn)
        self.assertIn("modo teste", res.falhas["PS-1"])
        self.assertEqual(vuupt.chamadas, [])
        res = materializar.materializar_servicos([{"id": sid}], vuupt, {}, self.conn, modo_teste=True)
        self.assertIn("modo teste", res.falhas["PS-1"])
        self.assertEqual(vuupt.chamadas, [])

    def test_id_real_e_ignorado(self):
        vuupt = FakeVuupt()
        res = materializar.materializar_servicos([{"id": 5, "code": "#PS-9"}], vuupt, {}, self.conn)
        self.assertEqual((res.ok, res.falhas, vuupt.chamadas), ({}, {}, []))

    def test_sem_id_resolve_pelo_codigo(self):
        self._pedido()
        res = materializar.materializar_servicos([{"code": "PS-1"}], FakeVuupt(), {}, self.conn)
        self.assertEqual(res.ok, {"PS-1": 900})

    def test_trava_ocupada_espera_e_desiste_com_motivo(self):
        sid = self._pedido()
        self.conn.execute("INSERT INTO nucleo_materializacao VALUES ('PS-1', ?, 'outro')", (banco.agora(),))
        self.conn.commit()
        vuupt = FakeVuupt()
        res = materializar.materializar_servicos([{"id": sid}], vuupt, {}, self.conn, espera_s=0.3)
        self.assertIn("outro envio", res.falhas["PS-1"])
        self.assertEqual(vuupt.criados, [])
        self.assertEqual(self._travas(), 1)   # a do outro processo fica

    def test_trava_velha_expira(self):
        sid = self._pedido()
        self.conn.execute("INSERT INTO nucleo_materializacao VALUES ('PS-1', '2026-01-01 00:00:00', 'morto')")
        self.conn.commit()
        res = materializar.materializar_servicos([{"id": sid}], FakeVuupt(), {}, self.conn, espera_s=0.3)
        self.assertEqual(res.ok, {"PS-1": 900})
        self.assertEqual(self._travas(), 0)

    def test_origem_da_data_vai_pro_servico_real(self):
        from roteirizacao import registro_dia_fixo
        sid = self._pedido()
        c = registro_dia_fixo.conectar(self.db)
        try:
            registro_dia_fixo.registrar_origem(c, {"code": "#PS-1"}, __import__("datetime").date(2026, 11, 27),
                                               registro_dia_fixo.ORIGEM_AUTOMATICO)
        finally:
            c.close()
        materializar.materializar_servicos([{"id": sid}], FakeVuupt(), {}, self.conn)
        linhas = self.conn.execute("SELECT codigo, service_id, origem FROM agendamentos_origem "
                                   "ORDER BY service_id").fetchall()
        self.assertEqual([tuple(r) for r in linhas], [("PS-1", 0, "AUTOMATICO"), ("PS-1", 900, "AUTOMATICO")])

    def test_dados_json_guarda_o_id_e_mantem_o_nome_da_skill(self):
        sid = self._pedido()
        materializar.materializar_servicos([{"id": sid}], FakeVuupt(), {}, self.conn)
        dados = json.loads(pedidos.buscar_pedido("PS-1", conn=self.conn)["dados_json"])
        self.assertEqual(dados["acao"], "criado")
        self.assertEqual(dados["payload"]["_skill_nome"], "Seco-2")


class _ConnQueFalhaAoSoltar:
    """Conexao de verdade, mas o DELETE que solta a trava levanta 'database is locked'."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        if sql.startswith("DELETE FROM nucleo_materializacao WHERE codigo = ? AND processo"):
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args)

    def __getattr__(self, nome):
        return getattr(self._conn, nome)


class FixRound1Test(_Base):
    def test_janela_telefone_titulo_e_nome_do_nucleo_vao_no_payload(self):
        sid = self._pedido(customer={"name": "DEST", "code": "123", "address": "RUA A 1",
                                     "phone_number": "+5511900000000", "operating_hour_start": "07:00",
                                     "operating_hour_end": "10:00"}, phone_number="+5511900000000")
        self.conn.execute("UPDATE nucleo_pedidos SET horario_inicio = '13:00', horario_fim = '17:00', "
                          "destinatario_telefone = '+5511988887777', titulo = '#PS-1 - NF1 / EMB / DEST NOVO', "
                          "destinatario_nome = 'DEST NOVO' WHERE codigo = 'PS-1'")
        self.conn.commit()
        vuupt = FakeVuupt()
        materializar.materializar_servicos([{"id": sid}], vuupt, {}, self.conn)
        (enviado,) = vuupt.criados
        c = enviado["customer"]
        self.assertEqual((c["operating_hour_start"], c["operating_hour_end"]), ("13:00", "17:00"))
        self.assertEqual((c["phone_number"], enviado["phone_number"]), ("+5511988887777", "+5511988887777"))
        self.assertEqual((enviado["title"], c["name"]), ("#PS-1 - NF1 / EMB / DEST NOVO", "DEST NOVO"))

    def test_coluna_vazia_nao_apaga_o_valor_do_payload(self):
        sid = self._pedido(customer={"name": "DEST", "code": "123", "address": "RUA A 1",
                                     "phone_number": "+5511900000000", "operating_hour_start": "07:00"},
                           phone_number="+5511900000000")
        self.conn.execute("UPDATE nucleo_pedidos SET horario_inicio = NULL, destinatario_telefone = NULL, "
                          "titulo = NULL, destinatario_nome = NULL WHERE codigo = 'PS-1'")
        self.conn.commit()
        vuupt = FakeVuupt()
        materializar.materializar_servicos([{"id": sid}], vuupt, {}, self.conn)
        (enviado,) = vuupt.criados
        self.assertEqual(enviado["customer"]["operating_hour_start"], "07:00")
        self.assertEqual((enviado["customer"]["phone_number"], enviado["phone_number"]),
                         ("+5511900000000", "+5511900000000"))
        self.assertEqual((enviado["title"], enviado["customer"]["name"]), ("#PS-1 - NF1 / EMB / DEST", "DEST"))

    def test_trava_ocupada_vai_em_ocupados(self):
        sid = self._pedido()
        self.conn.execute("INSERT INTO nucleo_materializacao VALUES ('PS-1', ?, 'outro')", (banco.agora(),))
        self.conn.commit()
        res = materializar.materializar_servicos([{"id": sid}], FakeVuupt(), {}, self.conn, espera_s=0.2)
        self.assertEqual(res.ocupados, {"PS-1"})
        self.assertIn("PS-1", res.falhas)
        # recusa comum nao e "ocupado"
        sid2 = self._pedido("PS-2", status=banco.PEDIDO_CANCELADO)
        res = materializar.materializar_servicos([{"id": sid2}], FakeVuupt(), {}, self.conn)
        self.assertEqual(res.ocupados, set())

    def test_resultado_diz_o_codigo_de_cada_id(self):
        sid = self._pedido()
        res = materializar.materializar_servicos([{"id": sid, "code": "qualquer"}], FakeVuupt(), {}, self.conn)
        self.assertEqual(res.codigos, {sid: "PS-1"})
        self.assertEqual(res.ok, {"PS-1": 900})

    def test_falha_ao_soltar_a_trava_nao_mascara_o_sucesso(self):
        sid = self._pedido()
        with self.assertLogs(materializar.logger, "WARNING") as logs:
            res = materializar.materializar_servicos([{"id": sid}], FakeVuupt(), {}, _ConnQueFalhaAoSoltar(self.conn))
        self.assertEqual(res.ok, {"PS-1": 900})
        self.assertEqual(res.falhas, {})
        self.assertTrue(any("trava" in m and "locked" in m for m in logs.output))


class FalhaTransitoriaTest(_Base):
    """Revisao Fable FIX 1: so recusa de REGRA (_Recusa) tira o pedido do envio; rede/HTTP/429/erro
    inesperado e falha tecnica (transitoria): quem chama mantem a parada e tenta de novo."""

    def test_classificacao_recusa_ocupado_transitorio(self):
        c = materializar.classificar_falha
        self.assertEqual(c(materializar._Recusa("pedido CANCELADO")), materializar.FALHA_RECUSA)
        self.assertEqual(c(materializar._Ocupado("outro envio")), materializar.FALHA_OCUPADO)
        for e in (RuntimeError("Status 429: Too Many Requests"), TimeoutError("read timed out"),
                  ConnectionError("reset"), sqlite3.OperationalError("database is locked"),
                  materializar._Transitorio("modo teste")):
            self.assertEqual(c(e), materializar.FALHA_TRANSITORIA, e)

    def test_erro_http_vai_em_transitorios_e_nao_derruba_os_outros(self):
        ids = [self._pedido(c) for c in ("PS-1", "PS-2", "PS-3")]
        vuupt = FakeVuupt(falhar_em={"PS-2"})
        res = materializar.materializar_servicos([{"id": i} for i in ids], vuupt, {}, self.conn)
        self.assertEqual(res.ok, {"PS-1": 900, "PS-3": 901})
        self.assertEqual(list(res.transitorios), ["PS-2"])
        self.assertIn("500", res.transitorios["PS-2"])
        self.assertIn("falha tecnica ao criar servico na Vuupt", res.falhas["PS-2"])
        self.assertIn("tente de novo", res.falhas["PS-2"])
        self.assertEqual(res.ocupados, set())

    def test_timeout_na_busca_e_transitorio(self):
        sid = self._pedido()
        vuupt = FakeVuupt()
        vuupt.buscar_servico_por_code = mock.Mock(side_effect=TimeoutError("read timed out"))
        res = materializar.materializar_servicos([{"id": sid}], vuupt, {}, self.conn)
        self.assertIn("timed out", res.transitorios["PS-1"])
        self.assertEqual(vuupt.criados, [])
        self.assertEqual(self._travas(), 0)

    def test_recusa_de_regra_nao_e_transitoria(self):
        a = self._pedido("PS-1", status=banco.PEDIDO_CANCELADO)
        b = self._pedido("PS-2-R1")
        res = materializar.materializar_servicos([{"id": a}, {"id": b}], FakeVuupt(), {}, self.conn)
        self.assertEqual(set(res.falhas), {"PS-1", "PS-2-R1"})
        self.assertEqual(res.transitorios, {})
        res = materializar.materializar_servicos([{"id": self._pedido("PS-3")}], FakeVuupt(acao="pulado_atribuido"),
                                                 {}, self.conn)
        self.assertIn("PS-3", res.falhas)
        self.assertEqual(res.transitorios, {})

    def test_modo_teste_e_transitorio(self):
        """Modo teste nao e recusa do pedido: a parada nao pode sair do rascunho."""
        sid = self._pedido()
        res = materializar.materializar_servicos([{"id": sid}], FakeVuupt(), {"modo_teste": True}, self.conn)
        self.assertIn("modo teste", res.transitorios["PS-1"])

    def test_criado_na_vuupt_mas_nao_gravado_no_nucleo_e_transitorio(self):
        sid = self._pedido()
        with mock.patch.object(pedidos, "registrar_importacao", side_effect=sqlite3.OperationalError("database is locked")):
            res = materializar.materializar_servicos([{"id": sid}], FakeVuupt(), {}, self.conn)
        self.assertIn("PS-1", res.transitorios)
        self.assertIn("servico 900 criado na Vuupt", res.falhas["PS-1"])

    def test_lote_pula_o_transitorio_nesta_rodada(self):
        a, b = self._pedido("PS-1"), self._pedido("PS-2")
        saida, falhas = materializar.materializar_lote([{"id": a}, {"id": b}], FakeVuupt(falhar_em={"PS-2"}), {},
                                                       self.conn)
        self.assertEqual([s["id"] for s in saida], [900])
        self.assertIn("falha tecnica", falhas["PS-2"])
        self.assertIsNone(pedidos.buscar_pedido("PS-2", conn=self.conn)["vuupt_service_id"])   # nada gravado


class MaterializarLoteTest(_Base):
    def test_lote_troca_o_id_e_tira_quem_falhou(self):
        a, b = self._pedido("PS-1"), self._pedido("PS-2", status=banco.PEDIDO_CANCELADO)
        lote = [{"id": 5, "code": "#PS-9"}, {"id": a, "code": "#PS-1"}, {"id": b, "code": "#PS-2"}]
        saida, falhas = materializar.materializar_lote(lote, FakeVuupt(), {}, self.conn)
        self.assertEqual([s["id"] for s in saida], [5, 900])
        self.assertEqual(list(falhas), ["PS-2"])
        self.assertEqual(lote[1]["id"], a)   # a lista de quem chamou nao muda

    def test_lote_so_com_id_real_nao_chama_nada(self):
        vuupt = FakeVuupt()
        lote = [{"id": 5, "code": "#PS-9"}]
        with mock.patch.object(materializar, "materializar_servicos") as m:
            saida, falhas = materializar.materializar_lote(lote, vuupt, {})
        m.assert_not_called()
        self.assertEqual((saida, falhas), (lote, {}))


class PedidoAgrupadoTest(_Base):
    """Fix round 1 da Task 7: alguem agrupou o pedido num servico da Vuupt ("PS-10" virou "#PS-10, #PS-20").
    O sync tira o vuupt_service_id da linha antiga PS-10 (fica ABERTO, sem servico); a busca por code
    "#PS-10" nao acha o composto e o envio criaria um SEGUNDO servico (entrega em dobro)."""

    def _agrupar(self, status_composto="not_assigned"):
        from nucleo import sincronizar_servicos_vuupt as sinc

        def servico(code, status="not_assigned"):
            return {"id": 5010, "code": code, "title": code, "status": status, "sender_id": 11,
                    "address": "RUA A 1", "customer": {"name": "DEST"}}
        # o pipeline criou o PS-10 (caminho antigo, payload no nucleo) e alguem agrupou na Vuupt
        pedidos.registrar_importacao(_payload("PS-10"), {"id": 5010}, "criado", conn=self.conn)
        sinc.sincronizar_servicos([servico("#PS-10, #PS-20", status_composto)], self.conn)
        linha = pedidos.buscar_pedido("PS-10", conn=self.conn)
        self.assertEqual((linha["vuupt_service_id"], linha["status"]), (None, banco.PEDIDO_ABERTO))  # o diagnostico
        return pedidos.id_sintetico(linha)

    def test_pedido_agrupado_recusa_com_motivo_e_nao_cria(self):
        sid = self._agrupar()
        vuupt = FakeVuupt()
        res = materializar.materializar_servicos([{"id": sid, "code": "#PS-10"}], vuupt, {}, self.conn)
        self.assertEqual(res.ok, {})
        self.assertIn("agrupado no servico 5010 ('PS-10, PS-20') na Vuupt", res.falhas["PS-10"])
        self.assertEqual((vuupt.criados, vuupt.chamadas), ([], []))
        self.assertEqual(self._travas(), 0)

    def test_composto_ja_entregue_tambem_recusa(self):
        sid = self._agrupar(status_composto="done")
        res = materializar.materializar_servicos([{"id": sid}], FakeVuupt(), {}, self.conn)
        self.assertIn("agrupado no servico 5010", res.falhas["PS-10"])

    def test_composto_cancelado_nao_segura_o_pedido(self):
        sid = self._agrupar(status_composto="canceled")
        vuupt = FakeVuupt()
        res = materializar.materializar_servicos([{"id": sid}], vuupt, {}, self.conn)
        self.assertEqual((res.ok, res.falhas), ({"PS-10": 900}, {}))

    def test_codigo_parecido_nao_e_agrupado(self):
        from nucleo import sincronizar_servicos_vuupt as sinc
        sinc.sincronizar_servicos([{"id": 5100, "code": "#PS-100, #PS-200", "title": "x", "status": "not_assigned",
                                    "sender_id": 11, "customer": {}}], self.conn)
        sid = self._pedido("PS-10")
        vuupt = FakeVuupt()
        res = materializar.materializar_servicos([{"id": sid}], vuupt, {}, self.conn)
        self.assertEqual(res.ok, {"PS-10": 900})
        self.assertEqual(len(vuupt.criados), 1)


if __name__ == "__main__":
    unittest.main()
