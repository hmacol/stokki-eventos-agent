# -*- coding: utf-8 -*-
"""
test_sincronizar_servicos.py

Testes do espelho do pedido FORA da rota (pool, retirada, reentrega,
cancelamento) -- sem rede e sem tocar no dados.db real.

    python -m unittest nucleo.test_sincronizar_servicos -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco, pedidos, sincronizar_servicos_vuupt as sinc


def _servico(service_id=5001, code="#PS-100", status="not_assigned", **extra):
    """Formato real de GET /services (horários em UTC sem fuso)."""
    base = {"id": service_id, "code": code, "title": f"{code} - 12 / BRAZO / CLIENTE", "status": status,
            "status_done": None, "address": "Rua A, 1", "address_complement": "sala 2",
            "latitude": -23.5, "longitude": -46.6, "sender_id": 11, "dimension_3": 4, "customer_id": 77,
            "route_id": None, "driver_id": None, "note": None, "deleted_at": None,
            "recreated_order_origin_id": None, "type": "delivery",
            "created_at": "2026-09-16 12:00:00", "updated_at": "2026-09-16 13:00:00",
            "customer": {"name": "Cliente A", "code": "12.345.678/0001-90", "phone_number": "+5511999990000",
                         "operating_hour_start": "08:00", "operating_hour_end": "17:00"}}
    base.update(extra)
    return base


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _pedido(self, codigo):
        return self.conn.execute("SELECT * FROM nucleo_pedidos WHERE codigo = ?", (codigo,)).fetchone()

    def _eventos(self):
        return [r["tipo"] for r in self.conn.execute("SELECT tipo FROM nucleo_eventos ORDER BY id")]


class TestPool(_Base):
    def test_pedido_do_pool_entra_com_dados_do_contato_e_hora_local(self):
        stats = sinc.sincronizar_servicos([_servico()], self.conn)
        p = self._pedido("PS-100")
        self.assertEqual((stats["servicos"], stats["novos"]), (1, 1))
        self.assertEqual(p["status"], "ABERTO")
        self.assertEqual(p["status_provedor"], "not_assigned")
        self.assertEqual(p["fluxo"], "ENTREGA")
        self.assertEqual(p["destinatario_telefone"], "+5511999990000")
        self.assertEqual(p["complemento"], "sala 2")
        self.assertEqual(p["criado_em_provedor"], "2026-09-16 09:00:00")     # 12:00 UTC
        self.assertEqual(p["atualizado_em_provedor"], "2026-09-16 10:00:00")
        self.assertEqual(p["customer_id"], 77)

    def test_pedido_em_rota_e_entregue(self):
        sinc.sincronizar_servicos([_servico(status="assigned", route_id=900, driver_id=42)], self.conn)
        self.assertEqual(self._pedido("PS-100")["status"], "EM_ROTA")
        self.assertEqual(self._pedido("PS-100")["vuupt_route_id"], 900)
        sinc.sincronizar_servicos([_servico(status="done", status_done="success")], self.conn)
        self.assertEqual(self._pedido("PS-100")["status"], "ENTREGUE")
        sinc.sincronizar_servicos([_servico(status="done", status_done="failed")], self.conn)
        self.assertEqual(self._pedido("PS-100")["status"], "INSUCESSO")

    def test_codigo_que_nao_e_pedido_nao_vira_linha(self):
        """'COLETA QUATRO ESTRELAS' é código fixo, repetido todo dia: viraria
        uma linha só, sobrescrita. Fica como parada da rota, não como pedido."""
        stats = sinc.sincronizar_servicos([_servico(code="COLETA QUATRO ESTRELAS", service_id=9)], self.conn)
        self.assertEqual(stats["ignorados_sem_codigo"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nucleo_pedidos").fetchone()[0], 0)

    def test_servico_com_varios_codigos_entra_como_uma_linha(self):
        """'#PS-1, PS-2' (dois pedidos no mesmo serviço, achado 20/08) era
        rejeitado pela regex e ficava fora do núcleo -- logo fora do pool."""
        stats = sinc.sincronizar_servicos([_servico(code="#PS-10, PS-20", service_id=7)], self.conn)
        self.assertEqual(stats["ignorados_sem_codigo"], 0)
        p = self._pedido("PS-10, PS-20")
        self.assertIsNotNone(p)
        self.assertEqual(p["vuupt_service_id"], 7)

    def test_cancelado_e_recriado_com_o_mesmo_codigo_volta_a_ficar_vivo(self):
        """Revisão 24/09: o upsert nunca apagava campo (COALESCE), então um
        pedido cancelado e depois recriado com o mesmo código ficava com
        excluido_em preenchido pra sempre -- e sumia do pool do núcleo."""
        sinc.sincronizar_servicos([_servico(service_id=1, code="#PS-10", deleted_at="2026-09-16 14:00:00")], self.conn)
        self.assertIsNotNone(self._pedido("PS-10")["excluido_em"])
        sinc.sincronizar_servicos([_servico(service_id=2, code="#PS-10")], self.conn)
        p = self._pedido("PS-10")
        self.assertEqual(p["status"], "ABERTO")
        self.assertIsNone(p["excluido_em"])
        self.assertEqual(p["vuupt_service_id"], 2)

    def test_campo_esvaziado_na_vuupt_e_esvaziado_no_nucleo(self):
        """Agendamento removido e serviço que voltou pro pool depois de sair
        de uma rota: o valor antigo não pode continuar valendo."""
        sinc.sincronizar_servicos([_servico(status="assigned", route_id=9, driver_id=4,
                                            scheduled_start="2026-09-25 11:00:00", scheduled_end="2026-09-25 15:00:00")],
                                  self.conn)
        sinc.sincronizar_servicos([_servico(status="not_assigned", route_id=None, driver_id=None,
                                            scheduled_start=None, scheduled_end=None)], self.conn)
        p = self._pedido("PS-100")
        self.assertIsNone(p["agendamento_inicio"])
        self.assertIsNone(p["agendamento_fim"])
        self.assertIsNone(p["vuupt_route_id"])
        self.assertIsNone(p["driver_id"])
        self.assertEqual(p["status"], "ABERTO")

    def test_servico_que_absorveu_outro_codigo_desliga_a_linha_antiga(self):
        """Revisão 24/09: PS-10 e PS-20 entram como serviços 1 e 2; depois o
        serviço 1 é editado na Vuupt pra "PS-10, PS-20". Sem isto a linha
        PS-10 continuava ABERTA com o id 1 -- fantasma no pool, pra sempre."""
        sinc.sincronizar_servicos([_servico(service_id=1, code="#PS-10"), _servico(service_id=2, code="#PS-20")], self.conn)
        sinc.sincronizar_servicos([_servico(service_id=1, code="#PS-10, PS-20")], self.conn)
        self.assertEqual(self._pedido("PS-10, PS-20")["vuupt_service_id"], 1)
        self.assertIsNone(self._pedido("PS-10")["vuupt_service_id"])
        self.assertEqual(self._pedido("PS-20")["vuupt_service_id"], 2)
        ids = [r[0] for r in self.conn.execute("SELECT vuupt_service_id FROM nucleo_pedidos WHERE vuupt_service_id = 1")]
        self.assertEqual(ids, [1])


class TestRetiradaEReentrega(_Base):
    def test_retirada_e_marcada_pelo_titulo_e_guarda_a_nota(self):
        nota = "Previsão de expedição (Stokki): 17/09/2026"
        s = _servico(code="#PS-200", status="assigned", driver_id=50259, note=nota,
                     title="[RETIRADA] #PS-200 - 33 / BRAZO / CLIENTE / via TRANSPORTADORA X")
        stats = sinc.sincronizar_servicos([s], self.conn)
        p = self._pedido("PS-200")
        self.assertEqual(stats["retiradas"], 1)
        self.assertEqual(p["fluxo"], "RETIRADA")
        self.assertEqual(p["nota"], nota)
        self.assertEqual(p["driver_id"], 50259)
        self.assertEqual(p["status"], "EM_ROTA")     # atribuída ao agente do galpão, sem rota

    def test_reentrega_liga_no_pedido_original_e_gera_evento(self):
        sinc.sincronizar_servicos([_servico(service_id=5001, code="#PS-300",
                                            status="done", status_done="failed")], self.conn)
        stats = sinc.sincronizar_servicos([_servico(service_id=5002, code="#PS-300-R1",
                                                    recreated_order_origin_id=5001)], self.conn)
        p = self._pedido("PS-300-R1")
        self.assertEqual(stats["reentregas"], 1)
        self.assertEqual(p["reentrega_de_service_id"], 5001)
        self.assertEqual(p["reentrega_de_codigo"], "PS-300")
        self.assertIn("REENTREGA_CRIADA", self._eventos())

    def test_servico_recriado_com_o_mesmo_codigo_nao_aponta_pra_si(self):
        """A VUUPT usa recreated_order_origin_id também quando o MESMO pedido
        é recriado (reimportação): aí não há reentrega nenhuma."""
        sinc.sincronizar_servicos([_servico(service_id=5801, code="#PS-800")], self.conn)
        sinc.sincronizar_servicos([_servico(service_id=5802, code="#PS-800",
                                            recreated_order_origin_id=5801)], self.conn)
        self.assertIsNone(self._pedido("PS-800")["reentrega_de_codigo"])

    def test_reentrega_de_pedido_que_o_nucleo_nao_conhece_nao_quebra(self):
        sinc.sincronizar_servicos([_servico(code="#PS-400-R1", recreated_order_origin_id=999)], self.conn)
        self.assertIsNone(self._pedido("PS-400-R1")["reentrega_de_codigo"])


class TestCancelamento(_Base):
    def test_cancelado_gera_evento_uma_vez_so(self):
        sinc.sincronizar_servicos([_servico()], self.conn)
        sinc.sincronizar_servicos([_servico(status="canceled")], self.conn)
        sinc.sincronizar_servicos([_servico(status="canceled")], self.conn)
        self.assertEqual(self._pedido("PS-100")["status"], "CANCELADO")
        self.assertEqual(self._eventos().count("PEDIDO_CANCELADO"), 1)

    def test_deleted_at_tambem_cancela(self):
        sinc.sincronizar_servicos([_servico(deleted_at="2026-09-16 15:00:00")], self.conn)
        p = self._pedido("PS-100")
        self.assertEqual(p["status"], "CANCELADO")
        self.assertEqual(p["excluido_em"], "2026-09-16 12:00:00")


class TestReconciliarPool(_Base):
    def _abrir(self, codigo, service_id):
        sinc.sincronizar_servicos([_servico(service_id=service_id, code=codigo)], self.conn)

    def test_pedido_que_sumiu_da_vuupt_vira_cancelado(self):
        self._abrir("#PS-500", 5500)
        stats = sinc.reconciliar_pool([], self.conn, buscar_por_id=lambda sid: None)
        self.assertEqual((stats["conferidos"], stats["sumidos"]), (1, 1))
        self.assertEqual(self._pedido("PS-500")["status"], "CANCELADO")
        self.assertIn("PEDIDO_SUMIU_DA_VUUPT", self._eventos())

    def test_pedido_que_saiu_do_pool_por_ter_entrado_em_rota_e_atualizado(self):
        self._abrir("#PS-501", 5501)
        achado = _servico(service_id=5501, code="#PS-501", status="assigned", route_id=900)
        stats = sinc.reconciliar_pool([], self.conn, buscar_por_id=lambda sid: achado)
        self.assertEqual((stats["sumidos"], stats["atualizados"]), (0, 1))
        self.assertEqual(self._pedido("PS-501")["status"], "EM_ROTA")

    def test_falha_de_rede_nao_cancela_pedido(self):
        """A consulta devolve um dicionário sem código quando não dá pra
        concluir nada (timeout, 500). O pedido tem que ficar como estava."""
        self._abrir("#PS-502", 5502)
        stats = sinc.reconciliar_pool([], self.conn, buscar_por_id=lambda sid: {"id": sid})
        self.assertEqual(stats["sumidos"], 0)
        self.assertEqual(self._pedido("PS-502")["status"], "ABERTO")
        self.assertNotIn("PEDIDO_SUMIU_DA_VUUPT", self._eventos())

    def test_pedido_que_continua_no_pool_nem_e_conferido(self):
        self._abrir("#PS-503", 5503)
        chamou = []
        stats = sinc.reconciliar_pool([_servico(service_id=5503, code="#PS-503")], self.conn,
                                      buscar_por_id=lambda sid: chamou.append(sid))
        self.assertEqual((stats["conferidos"], chamou), (0, []))


class TestVincularSemServiceId(_Base):
    def _pedido_do_pipeline_sem_service_id(self, codigo):
        pedidos.registrar_importacao({"code": codigo, "title": "t"}, None, "pulado_atribuido", conn=self.conn)
        self.conn.commit()

    def test_liga_pelo_codigo_quando_o_servico_existe(self):
        self._pedido_do_pipeline_sem_service_id("#PS-700")
        self.assertIsNone(self._pedido("PS-700")["vuupt_service_id"])
        achado = _servico(service_id=5700, code="#PS-700")
        stats = sinc.vincular_sem_service_id(self.conn, buscar_por_codigo=lambda c: achado)
        self.assertEqual((stats["sem_service_id"], stats["vinculados"]), (1, 1))
        self.assertEqual(self._pedido("PS-700")["vuupt_service_id"], 5700)

    def test_pedido_que_nunca_foi_pra_vuupt_fica_como_esta(self):
        self._pedido_do_pipeline_sem_service_id("#PS-701")
        stats = sinc.vincular_sem_service_id(self.conn, buscar_por_codigo=lambda c: None)
        self.assertEqual((stats["vinculados"], stats["nao_achados"]), (0, 1))
        p = self._pedido("PS-701")
        self.assertEqual((p["status"], p["vuupt_service_id"]), ("ABERTO", None))


class TestCursor(_Base):
    def test_grava_e_le(self):
        self.assertIsNone(sinc.ler_cursor(self.conn))
        sinc.gravar_cursor(self.conn, "2026-09-16 18:00:00", {"servicos": 3})
        self.assertEqual(sinc.ler_cursor(self.conn), "2026-09-16 18:00:00")
        sinc.gravar_cursor(self.conn, "2026-09-16 19:00:00", {"servicos": 5})
        self.assertEqual(sinc.ler_cursor(self.conn), "2026-09-16 19:00:00")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nucleo_sincronismos").fetchone()[0], 1)


class TestNaoAtrapalhaOEspelhoDeRotas(_Base):
    def test_dados_do_pipeline_sobrevivem(self):
        pedidos.registrar_importacao({"code": "#PS-600", "title": "t", "customer": {"phone_number": "+551188887777"}},
                                     {"service": {"id": 5600}}, "criado", conn=self.conn)
        sinc.sincronizar_servicos([_servico(service_id=5600, code="#PS-600", status="assigned")], self.conn)
        p = self._pedido("PS-600")
        self.assertEqual(p["destinatario_telefone"], "+5511999990000")   # o serviço traz o contato atualizado
        self.assertEqual(p["origem"], "PIPELINE")                        # quem criou a linha
        self.assertIn('"payload"', p["dados_json"])                      # payload do pipeline preservado


class _Resp:
    def __init__(self, status_code, corpo=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._corpo = corpo

    def json(self):
        return self._corpo


class _VuuptFalso:
    """Só o que _buscar_servico usa: session.get(url, timeout=...)."""
    def __init__(self, respostas: dict):
        self.respostas = respostas          # {service_id: _Resp | Exception}
        self.session = self

    def get(self, url, timeout=None):
        sid = int(url.rsplit("/", 1)[1])
        r = self.respostas[sid]
        if isinstance(r, Exception):
            raise r
        return r


class TestRessincronizarIds(_Base):
    def test_aplica_o_servico_que_voltou_e_cancela_o_404(self):
        sinc.sincronizar_servicos([_servico(service_id=1, code="#PS-10"), _servico(service_id=2, code="#PS-20")], self.conn)
        vuupt = _VuuptFalso({
            1: _Resp(200, {"service": _servico(service_id=1, code="#PS-10", status="assigned", route_id=9)}),
            2: _Resp(404),
        })
        n = sinc.ressincronizar_ids(vuupt, [1, 2, None], self.conn)
        self.assertEqual(n, 2)
        self.assertEqual(self._pedido("PS-10")["status"], "EM_ROTA")
        self.assertEqual(self._pedido("PS-20")["status"], "CANCELADO")
        self.assertIn("PEDIDO_SUMIU_DA_VUUPT", self._eventos())

    def test_falha_de_rede_nao_estoura_nem_muda_nada(self):
        sinc.sincronizar_servicos([_servico(service_id=1, code="#PS-10")], self.conn)
        vuupt = _VuuptFalso({1: ConnectionError("rede fora")})
        self.assertEqual(sinc.ressincronizar_ids(vuupt, [1], self.conn), 0)
        self.assertEqual(self._pedido("PS-10")["status"], "ABERTO")

    def test_lista_vazia_nao_abre_conexao(self):
        with mock.patch.object(banco, "conectar", side_effect=AssertionError("não devia conectar")):
            self.assertEqual(sinc.ressincronizar_ids(object(), [], None), 0)


class TestMarcarEmRotaNoEnvio(_Base):
    def test_envio_da_rota_marca_os_pedidos_em_rota_sem_ir_na_vuupt(self):
        """Revisão 24/09: no envio do rascunho a rota e o route_id já são
        conhecidos -- gravar direto, sem N GETs, e só o que estava ABERTO."""
        sinc.sincronizar_servicos([_servico(service_id=1, code="#PS-10"), _servico(service_id=2, code="#PS-20"),
                                   _servico(service_id=3, code="#PS-30", status="done", status_done="success")],
                                  self.conn)
        n = pedidos.marcar_em_rota_por_service_ids([1, 2, 3, None], 777, self.conn)
        self.assertEqual(n, 2)
        for codigo in ("PS-10", "PS-20"):
            p = self._pedido(codigo)
            self.assertEqual((p["status"], p["status_provedor"], p["vuupt_route_id"]), ("EM_ROTA", "assigned", 777))
        self.assertEqual(self._pedido("PS-30")["status"], "ENTREGUE")
        self.assertEqual(pedidos.marcar_em_rota_por_service_ids([], 777, self.conn), 0)


class TestExecutar(_Base):
    def test_executar_roda_incremental_e_pool_e_grava_cursor(self):
        vuupt = mock.Mock()
        vuupt.listar_servicos.side_effect = [
            [_servico(service_id=1, code="#PS-10")],          # incremental
            [_servico(service_id=1, code="#PS-10")],          # pool
        ]
        vuupt.buscar_servico_por_code.return_value = None
        resultado = sinc.executar(vuupt, self.conn, token="t", inicio="2026-09-16 00:00:00")
        self.assertEqual(resultado["incremental"]["novos"], 1)
        self.assertEqual(resultado["pool_vuupt"], 1)
        self.assertEqual(resultado["abertos"], 1)
        self.assertIsNotNone(sinc.ler_cursor(self.conn))

    def test_executar_sem_pool_so_faz_o_incremental(self):
        vuupt = mock.Mock()
        vuupt.listar_servicos.return_value = []
        resultado = sinc.executar(vuupt, self.conn, token="t", inicio="2026-09-16 00:00:00", sem_pool=True)
        self.assertIsNone(resultado["pool"])
        vuupt.listar_servicos.assert_called_once()


if __name__ == "__main__":
    unittest.main()
