# -*- coding: utf-8 -*-
"""
test_pool.py

Pool do planejamento lido do núcleo (Entrega 1 do spec
docs/superpowers/specs/2026-09-24-pool-pelo-nucleo-pedidos-portal-rascunho-14h-design.md).
Sem rede e sem tocar no dados.db real.

    py -3.11 -m unittest nucleo.test_pool -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco, pool, sincronizar_servicos_vuupt as sinc
from nucleo.normalizacao import local_para_vuupt, normalizar_codigo, vuupt_para_local


def _servico(service_id=5001, code="#PS-100", status="not_assigned", **extra):
    """Formato real de GET /services (horários em UTC sem fuso), igual ao
    fixture de test_sincronizar_servicos."""
    base = {"id": service_id, "code": code, "title": f"{code} - 12 / BRAZO / CLIENTE", "status": status,
            "status_done": None, "address": "Rua A, 1 - Centro, São Paulo - SP, 01000-000",
            "address_complement": "sala 2", "latitude": -23.5, "longitude": -46.6, "sender_id": 11,
            "dimension_3": 4, "customer_id": 77, "route_id": None, "driver_id": None, "note": None,
            "deleted_at": None, "recreated_order_origin_id": None, "type": "delivery",
            "created_at": "2026-09-16 12:00:00", "updated_at": "2026-09-16 13:00:00",
            "scheduled_start": "2026-09-25 11:00:00", "scheduled_end": "2026-09-25 15:00:00",
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


class TestListarPool(_Base):
    def test_pedido_aberto_volta_no_formato_da_vuupt(self):
        sinc.sincronizar_servicos([_servico()], self.conn)
        itens = pool.listar_pool(self.conn)
        self.assertEqual(len(itens), 1)
        s = itens[0]
        self.assertEqual(s["id"], 5001)
        self.assertEqual(s["code"], "#PS-100")
        self.assertEqual(s["address"], "Rua A, 1 - Centro, São Paulo - SP, 01000-000")
        self.assertEqual((s["latitude"], s["longitude"]), (-23.5, -46.6))
        self.assertEqual(s["dimension_3"], 4)
        self.assertEqual(s["sender_id"], 11)
        self.assertEqual(s["status"], "not_assigned")
        # o que a Vuupt devolveu (UTC sem fuso) é o que sai daqui de novo
        self.assertEqual(s["scheduled_start"], "2026-09-25 11:00:00")
        self.assertEqual(s["scheduled_end"], "2026-09-25 15:00:00")
        self.assertEqual(s["created_at"], "2026-09-16 12:00:00")
        self.assertEqual(s["customer"]["code"], "12.345.678/0001-90")
        self.assertEqual(s["customer"]["name"], "Cliente A")
        self.assertEqual(s["customer"]["operating_hour_start"], "08:00")
        self.assertEqual(s["customer_id"], 77)
        self.assertEqual(s["_fonte"], "nucleo")

    def test_sem_agendamento_volta_none(self):
        sinc.sincronizar_servicos([_servico(scheduled_start=None, scheduled_end=None)], self.conn)
        s = pool.listar_pool(self.conn)[0]
        self.assertIsNone(s["scheduled_start"])
        self.assertIsNone(s["scheduled_end"])

    def test_ficam_fora_retirada_cancelado_excluido_e_sem_service_id(self):
        sinc.sincronizar_servicos([
            _servico(),                                                         # entra
            _servico(service_id=2, code="#PS-200", status="assigned", driver_id=50259,
                     title="[RETIRADA] #PS-200 - 33 / BRAZO / CLIENTE / via X"),  # retirada: fora
            _servico(service_id=3, code="#PS-300", status="canceled"),          # cancelado: fora
            _servico(service_id=4, code="#PS-400", deleted_at="2026-09-16 14:00:00"),  # excluído: fora
            _servico(service_id=5, code="#PS-500", status="assigned", route_id=9),     # em rota: fora
            _servico(service_id=7, code="#PS-700", status="not_assigned",             # retirada no pool
                     title="[RETIRADA] #PS-700 - 1 / BRAZO / CLIENTE / via X"),         # da Vuupt: fora
        ], self.conn)
        # pedido "pulado" pelo pipeline: ABERTO sem vuupt_service_id
        self.conn.execute("INSERT INTO nucleo_pedidos (codigo, status, origem) VALUES ('PS-600', 'ABERTO', 'PIPELINE')")
        # ABERTO com excluido_em preenchido (resto de um cancelamento): fora
        self.conn.execute("INSERT INTO nucleo_pedidos (codigo, status, origem, vuupt_service_id, excluido_em) "
                          "VALUES ('PS-800', 'ABERTO', 'VUUPT_SYNC', 8, '2026-09-16 10:00:00')")
        self.conn.commit()
        self.assertEqual([s["code"] for s in pool.listar_pool(self.conn)], ["#PS-100"])

    def test_duas_linhas_com_o_mesmo_service_id_viram_um_item_so(self):
        """Rede de segurança pro serviço que absorveu outro código (ver
        test_sincronizar_servicos): fica a linha atualizada por último."""
        self.conn.execute("INSERT INTO nucleo_pedidos (codigo, status, origem, vuupt_service_id, atualizado_em) "
                          "VALUES ('PS-10', 'ABERTO', 'VUUPT_SYNC', 1, '2026-09-16 10:00:00')")
        self.conn.execute("INSERT INTO nucleo_pedidos (codigo, status, origem, vuupt_service_id, atualizado_em) "
                          "VALUES ('PS-10, PS-20', 'ABERTO', 'VUUPT_SYNC', 1, '2026-09-16 11:00:00')")
        self.conn.commit()
        self.assertEqual([s["code"] for s in pool.listar_pool(self.conn)], ["#PS-10, PS-20"])

    def test_varios_codigos_saem_com_cerquilha_na_frente(self):
        sinc.sincronizar_servicos([_servico(code="#PS-10, PS-20", service_id=7)], self.conn)
        self.assertEqual(pool.listar_pool(self.conn)[0]["code"], "#PS-10, PS-20")

    def test_ordem_por_codigo(self):
        sinc.sincronizar_servicos([_servico(service_id=2, code="#PS-200"), _servico(service_id=1, code="#PS-100")],
                                  self.conn)
        self.assertEqual([s["id"] for s in pool.listar_pool(self.conn)], [1, 2])

class TestFonte(unittest.TestCase):
    def test_fonte_padrao_e_vuupt(self):
        self.assertEqual(pool.fonte_pool({}), "vuupt")
        self.assertEqual(pool.fonte_pool({"planejamento": {}}), "vuupt")
        self.assertEqual(pool.fonte_pool({"planejamento": {"fonte_pool": "NUCLEO"}}), "nucleo")

    def test_listar_pool_not_assigned_escolhe_pela_chave(self):
        vuupt = mock.Mock()
        vuupt.listar_servicos.return_value = [{"id": 1, "code": "#PS-1"}]
        self.assertEqual(pool.listar_pool_not_assigned({}, vuupt), [{"id": 1, "code": "#PS-1"}])
        vuupt.listar_servicos.assert_called_once_with(
            [{"field": "status", "operator": "eq", "value": "not_assigned"}], per_page=100, include=["customer"])
        with mock.patch.object(pool, "listar_pool", return_value=[{"id": 2}]) as lp:
            self.assertEqual(pool.listar_pool_not_assigned({"planejamento": {"fonte_pool": "nucleo"}}, vuupt), [{"id": 2}])
            lp.assert_called_once_with()
        vuupt.listar_servicos.assert_called_once()   # não chamou a Vuupt de novo


class TestEquivalenciaComATela(_Base):
    """O item do pool montado pela tela tem que sair IGUAL, venha o serviço
    da Vuupt ou do núcleo."""

    def test_servico_para_pool_da_o_mesmo_item(self):
        # painel_agentes/ entra no sys.path SÓ durante o import: com esse
        # diretório em qualquer posição, "painel_agentes" resolve pro ARQUIVO
        # painel_agentes.py (módulo regular ganha do pacote-namespace) e
        # quebra qualquer painel_agentes.test_* rodado na mesma sessão.
        # roteirizacao/ o próprio planejamento_rotas coloca no path.
        extra = str(_RAIZ / "painel_agentes")
        ja_tinha = extra in sys.path
        if not ja_tinha:
            sys.path.append(extra)
        try:
            import planejamento_rotas
        finally:
            if not ja_tinha:
                sys.path.remove(extra)

        original = _servico()
        sinc.sincronizar_servicos([original], self.conn)
        do_nucleo = pool.listar_pool(self.conn)[0]
        remetentes = {11: "BRAZO"}
        esperado = planejamento_rotas._servico_para_pool(original, remetentes)
        obtido = planejamento_rotas._servico_para_pool(do_nucleo, remetentes)
        self.assertEqual(obtido, esperado)
        self.assertEqual(obtido["janela_inicio"], esperado["janela_inicio"])
        self.assertEqual(obtido["agendado_para"], "2026-09-25")


class TestNormalizacao(unittest.TestCase):
    def test_local_para_vuupt_e_o_inverso_de_vuupt_para_local(self):
        self.assertEqual(local_para_vuupt("2026-09-16 09:00:00"), "2026-09-16 12:00:00")
        self.assertEqual(vuupt_para_local(local_para_vuupt("2026-09-16 09:00:00")), "2026-09-16 09:00:00")
        self.assertEqual(local_para_vuupt("2026-09-16T09:00:00-03:00"), "2026-09-16 12:00:00")
        self.assertIsNone(local_para_vuupt(None))
        self.assertIsNone(local_para_vuupt(""))
        self.assertEqual(local_para_vuupt("não é data"), "não é data")

    def test_normalizar_codigo_composto_tira_o_cerquilha_de_cada_parte(self):
        self.assertEqual(normalizar_codigo("#PS-1, #PS-2"), "PS-1, PS-2")
        self.assertEqual(normalizar_codigo("#PS-1,PS-2"), "PS-1, PS-2")
        self.assertEqual(normalizar_codigo(" #ps-12345 "), "PS-12345")   # simples: igual a antes
        self.assertIsNone(normalizar_codigo(""))


if __name__ == "__main__":
    unittest.main()
