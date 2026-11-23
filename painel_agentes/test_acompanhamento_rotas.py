# -*- coding: utf-8 -*-
"""
Tela /acompanhamento (acompanhamento detalhado das rotas, 21/11).

    cd painel_agentes && py -3.11 -m unittest test_acompanhamento_rotas
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco  # noqa: E402

import acompanhamento_rotas as ar  # noqa: E402

DIA = "2026-11-20"


def _rs(trip=600, dur=1200, espera=None):
    return json.dumps({"route_service": {"prevision_initial_trip_time": trip,
                                         "prevision_initial_duration_time": dur,
                                         "prevision_initial_waiting_time": espera}})


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        self.conn = banco.conectar(self.db)
        self.addCleanup(self.conn.close)

    def rota(self, status="EM_ROTA", data=DIA, iniciada="2026-11-20 08:00:00", nome="Rota 1", agent_id=7):
        cur = self.conn.execute(
            "INSERT INTO nucleo_rotas (data_rota, nome, motorista_nome, tipo_veiculo, status, iniciada_em, agent_id)"
            " VALUES (?,?,?,?,?,?,?)", (data, nome, "Joao", "VUC", status, iniciada, agent_id))
        self.conn.commit()
        return cur.lastrowid

    def parada(self, rota_id, ordem, concluida=None, dados=None, desloc=None, local=None, codigo=None,
               lat=-23.5, lng=-46.6, motivo=None, situacao=None):
        cur = self.conn.execute(
            "INSERT INTO nucleo_paradas (rota_id, ordem, codigo, destinatario_nome, situacao, completed_at,"
            " tempo_deslocamento_s, tempo_no_local_s, motivo_texto, latitude, longitude, dados_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rota_id, ordem, codigo or f"PS-{ordem}", f"Cliente {ordem}",
             situacao or ("ENTREGUE" if concluida else "PENDENTE"), concluida, desloc, local, motivo, lat, lng,
             _rs() if dados is None else dados))
        self.conn.commit()
        return cur.lastrowid

    def ciclos(self, rota_id, iniciada="2026-11-20 08:00:00"):
        ps = [dict(r) for r in self.conn.execute("SELECT * FROM nucleo_paradas WHERE rota_id = ?", (rota_id,))]
        return ar.ciclos_paradas(ps, iniciada)


class TestSemaforo(unittest.TestCase):
    def test_semaforo_cortes(self):
        self.assertEqual(ar.semaforo(600), "verde")
        self.assertEqual(ar.semaforo(601), "amarelo")
        self.assertEqual(ar.semaforo(1800), "amarelo")
        self.assertEqual(ar.semaforo(1801), "vermelho")
        self.assertEqual(ar.semaforo(None), "cinza")


class TestCiclos(_Base):
    def test_ciclo_pela_conclusao_anterior(self):
        r = self.rota()
        a = self.parada(r, 1, "2026-11-20 08:30:00")
        b = self.parada(r, 2, "2026-11-20 09:20:00")
        c = self.ciclos(r)
        self.assertEqual((c[a]["real_s"], c[a]["plan_s"], c[a]["dif_s"]), (1800, 1800, 0))
        self.assertEqual((c[b]["real_s"], c[b]["plan_s"], c[b]["dif_s"]), (3000, 1800, 1200))

    def test_ciclo_segue_ordem_de_conclusao_nao_ordem(self):
        r = self.rota()
        a = self.parada(r, 1, "2026-11-20 09:00:00")
        b = self.parada(r, 2, "2026-11-20 08:20:00")
        c = self.ciclos(r)
        self.assertEqual(c[b]["real_s"], 1200)
        self.assertEqual(c[a]["real_s"], 2400)

    def test_primeira_sem_iniciada_em_fica_sem_real(self):
        r = self.rota()
        a = self.parada(r, 1, "2026-11-20 08:30:00")
        c = self.ciclos(r, iniciada=None)
        self.assertIsNone(c[a]["real_s"])
        self.assertIsNone(c[a]["dif_s"])

    def test_espera_prevista_soma_no_planejado(self):
        r = self.rota()
        a = self.parada(r, 1, "2026-11-20 08:30:00", dados=_rs(600, 1200, 300))
        self.assertEqual(self.ciclos(r)[a]["plan_s"], 2100)

    def test_cliques_juntos(self):
        r = self.rota()
        a = self.parada(r, 1, "2026-11-20 08:30:00", desloc=3)
        b = self.parada(r, 2, "2026-11-20 09:00:00", desloc=60)
        d = self.parada(r, 3, "2026-11-20 09:30:00", desloc=None)
        c = self.ciclos(r)
        self.assertFalse(c[a]["cliques_ok"])
        self.assertTrue(c[b]["cliques_ok"])
        self.assertFalse(c[d]["cliques_ok"])

    def test_iniciada_depois_da_primeira_conclusao_fica_sem_real(self):
        r = self.rota()
        a = self.parada(r, 1, "2026-11-20 08:30:00")
        b = self.parada(r, 2, "2026-11-20 09:00:00")
        c = self.ciclos(r, iniciada="2026-11-20 08:40:00")
        self.assertIsNone(c[a]["real_s"])
        self.assertIsNone(c[a]["dif_s"])
        self.assertEqual(c[b]["real_s"], 1800)

    def test_previsao_invalida_nao_derruba(self):
        r = self.rota()
        a = self.parada(r, 1, "2026-11-20 08:30:00", dados=_rs("abc", 1200))
        b = self.parada(r, 2, "2026-11-20 09:00:00", dados="[1, 2]")
        c = self.ciclos(r)
        self.assertIsNone(c[a]["plan_s"])
        self.assertIsNone(c[b]["plan_s"])
        self.assertEqual(len(ar.listar_rotas(self.conn, DIA)), 1)

    def test_pendente_nao_entra(self):
        r = self.rota()
        a = self.parada(r, 1)
        self.assertNotIn(a, self.ciclos(r))


class TestLista(_Base):
    def _unica(self):
        rotas = ar.listar_rotas(self.conn, DIA)
        self.assertEqual(len(rotas), 1)
        return rotas[0]

    def test_adiantamento_abate_atraso(self):
        r = self.rota()
        self.parada(r, 1, "2026-11-20 08:45:00")   # 2700 x 1800 = +900
        self.parada(r, 2, "2026-11-20 09:05:00")   # 1200 x 1800 = -600
        rota = self._unica()
        self.assertEqual(rota["atraso_s"], 300)
        self.assertEqual(rota["semaforo"], "verde")
        self.assertEqual((rota["total_paradas"], rota["concluidas"]), (2, 2))

    def test_rota_adiantada_tem_atraso_zero(self):
        r = self.rota()
        self.parada(r, 1, "2026-11-20 08:10:00")
        rota = self._unica()
        self.assertEqual(rota["atraso_s"], 0)
        self.assertEqual(rota["semaforo"], "verde")

    def test_baixa_em_lote_nao_deixa_vermelho(self):
        r = self.rota()
        a = self.parada(r, 1, "2026-11-20 09:30:00", dados=_rs(600, 1200))
        b = self.parada(r, 2, "2026-11-20 09:30:20", dados=_rs(600, 1200))
        c = self.parada(r, 3, "2026-11-20 09:30:40", dados=_rs(600, 1200))
        rota = self._unica()
        self.assertEqual(rota["atraso_s"], 40)
        self.assertEqual(rota["semaforo"], "verde")
        ciclos = self.ciclos(r)
        self.assertFalse(ciclos[a]["junto"])
        self.assertTrue(ciclos[b]["junto"])
        self.assertTrue(ciclos[c]["junto"])

    def test_rota_sem_previsao_fica_cinza(self):
        r = self.rota()
        self.parada(r, 1, "2026-11-20 08:30:00", dados="{}")
        rota = self._unica()
        self.assertIsNone(rota["atraso_s"])
        self.assertEqual(rota["semaforo"], "cinza")

    def test_rota_nao_iniciada_fica_cinza(self):
        r = self.rota(status="PLANEJADA", iniciada=None)
        self.parada(r, 1)
        self.assertEqual(self._unica()["semaforo"], "cinza")

    def test_lista_exclui_cancelada(self):
        self.rota(status="CANCELADA")
        self.assertEqual(ar.listar_rotas(self.conn, DIA), [])

    def test_lista_so_do_dia(self):
        self.rota(data="2026-11-19")
        r = self.rota()
        self.assertEqual([x["rota_id"] for x in ar.listar_rotas(self.conn, DIA)], [r])

    def test_tem_gps(self):
        r = self.rota()
        self.assertFalse(self._unica()["tem_gps"])
        self.conn.execute("INSERT INTO nucleo_eventos (rota_id, tipo, origem, ocorrido_em, latitude, longitude)"
                          " VALUES (?, 'GPS', 'APP', '2026-11-20 09:00:00', -23.5, -46.6)", (r,))
        self.conn.commit()
        rota = self._unica()
        self.assertTrue(rota["tem_gps"])
        self.assertEqual(rota["ultimo_gps_em"], "2026-11-20 09:00:00")


AGORA = datetime(2026, 11, 20, 10, 0, 0)


class TestDetalhe(_Base):
    def _gps(self, rota_id, quando, lat=-23.61, lng=-46.71):
        self.conn.execute("INSERT INTO nucleo_eventos (rota_id, tipo, origem, ocorrido_em, latitude, longitude)"
                          " VALUES (?, 'GPS', 'APP', ?, ?, ?)", (rota_id, quando, lat, lng))
        self.conn.commit()

    def test_rota_inexistente_devolve_none(self):
        self.assertIsNone(ar.detalhe_rota(self.conn, 999))

    def test_paradas_concluidas_primeiro_na_ordem_de_conclusao(self):
        r = self.rota()
        self.parada(r, 1)
        self.parada(r, 2, "2026-11-20 09:00:00")
        self.parada(r, 3, "2026-11-20 08:30:00")
        d = ar.detalhe_rota(self.conn, r, agora=AGORA)
        self.assertEqual([p["ordem"] for p in d["paradas"]], [3, 2, 1])
        self.assertEqual(d["paradas"][0]["real_s"], 1800)
        self.assertIsNone(d["paradas"][2]["real_s"])
        self.assertEqual(d["rota"]["semaforo"], "verde")

    def test_cliques_juntos_esconde_desloc_e_espera(self):
        r = self.rota()
        self.parada(r, 1, "2026-11-20 08:30:00", desloc=2, local=3)
        self.parada(r, 2, "2026-11-20 09:10:00", desloc=900, local=600)
        ps = ar.detalhe_rota(self.conn, r, agora=AGORA)["paradas"]
        self.assertIsNone(ps[0]["desloc_s"])
        self.assertIsNone(ps[0]["no_local_s"])
        self.assertEqual((ps[1]["desloc_s"], ps[1]["no_local_s"]), (900, 600))

    def test_parada_sem_latlng_e_reentrega_ficam_na_linha_do_tempo(self):
        r = self.rota()
        self.parada(r, 1, "2026-11-20 08:30:00", codigo="PS-1-R2", lat=None, lng=None)
        self.parada(r, 2, codigo="PS-2")
        d = ar.detalhe_rota(self.conn, r, agora=AGORA)
        self.assertEqual(len(d["paradas"]), 2)
        self.assertTrue(d["paradas"][0]["reentrega"])
        self.assertIsNone(d["paradas"][0]["lat"])
        self.assertFalse(d["paradas"][1]["reentrega"])
        self.assertIsNone(d["posicao"]["fonte"])   # unica concluida nao tem coordenada

    def test_posicao_gps_recente_e_ao_vivo(self):
        r = self.rota()
        self.parada(r, 1, "2026-11-20 08:30:00")
        self._gps(r, "2026-11-20 09:00:00", lat=-23.0, lng=-46.0)
        self._gps(r, "2026-11-20 09:57:00")
        pos = ar.detalhe_rota(self.conn, r, agora=AGORA)["posicao"]
        self.assertEqual((pos["fonte"], pos["ao_vivo"], pos["ha_s"]), ("GPS", True, 180))
        self.assertEqual((pos["lat"], pos["lng"]), (-23.61, -46.71))

    def test_posicao_gps_velho_nao_e_ao_vivo(self):
        r = self.rota()
        self._gps(r, "2026-11-20 09:40:00")
        pos = ar.detalhe_rota(self.conn, r, agora=AGORA)["posicao"]
        self.assertEqual((pos["fonte"], pos["ao_vivo"], pos["ha_s"]), ("GPS", False, 1200))

    def test_gps_mais_velho_que_a_ultima_parada_perde(self):
        r = self.rota()
        self._gps(r, "2026-11-20 09:00:00")
        self.parada(r, 1, "2026-11-20 09:30:00", lat=-23.2, lng=-46.2)
        pos = ar.detalhe_rota(self.conn, r, agora=AGORA)["posicao"]
        self.assertEqual((pos["fonte"], pos["ao_vivo"]), ("ULTIMA_PARADA", False))
        self.assertEqual((pos["lat"], pos["lng"]), (-23.2, -46.2))

    def test_sem_gps_usa_ultima_parada(self):
        r = self.rota()
        self.parada(r, 1, "2026-11-20 08:30:00", lat=-23.1, lng=-46.1)
        self.parada(r, 2, "2026-11-20 09:00:00", lat=-23.2, lng=-46.2)
        self.parada(r, 3, lat=-23.3, lng=-46.3)
        pos = ar.detalhe_rota(self.conn, r, agora=AGORA)["posicao"]
        self.assertEqual((pos["fonte"], pos["ao_vivo"]), ("ULTIMA_PARADA", False))
        self.assertEqual((pos["lat"], pos["lng"]), (-23.2, -46.2))

    def test_rota_sem_nada_concluido_nao_tem_posicao(self):
        r = self.rota(status="PLANEJADA", iniciada=None)
        self.parada(r, 1)
        pos = ar.detalhe_rota(self.conn, r, agora=AGORA)["posicao"]
        self.assertIsNone(pos["fonte"])
        self.assertFalse(pos["ao_vivo"])

    def test_ocorrencias(self):
        r = self.rota()
        self.parada(r, 1, "2026-11-20 08:30:00", motivo="Cliente ausente", situacao="INSUCESSO")
        self.parada(r, 2, "2026-11-20 09:00:00")
        d = ar.detalhe_rota(self.conn, r, agora=AGORA)
        self.assertEqual(d["ocorrencias"], [{"codigo": "PS-1", "destinatario": "Cliente 1", "motivo": "Cliente ausente"}])
        self.assertEqual(d["paradas"][0]["ocorrencia"], "Cliente ausente")
        self.assertIsNone(d["paradas"][1]["ocorrencia"])


class TestDespesas(_Base):
    def _despesa(self, rota_id, valor, status="PENDENTE", aprovado=None, tipo="PEDAGIO"):
        self.conn.execute("INSERT INTO nucleo_pedagios (rota_id, valor_informado, tipo, status, valor_aprovado)"
                          " VALUES (?,?,?,?,?)", (rota_id, valor, tipo, status, aprovado))
        self.conn.commit()

    def test_sem_permissao_nao_traz_chave_despesas(self):
        r = self.rota()
        self._despesa(r, 10.0)
        d = ar.detalhe_rota(self.conn, r, agora=AGORA)
        self.assertNotIn("despesas", d)
        self.assertNotIn("total_despesas", d)

    def test_total_ignora_rejeitado_e_cancelado(self):
        r = self.rota()
        self._despesa(r, 10.0)
        self._despesa(r, 99.0, status="REJEITADO")
        self._despesa(r, 77.0, status="CANCELADO")
        d = ar.detalhe_rota(self.conn, r, agora=AGORA, com_despesas=True)
        self.assertEqual(len(d["despesas"]), 3)
        self.assertEqual(d["total_despesas"], 10.0)

    def test_total_usa_valor_aprovado_quando_ha(self):
        r = self.rota()
        self._despesa(r, 20.0, status="APROVADO", aprovado=15.5)
        self._despesa(r, 4.5, tipo="ESTACIONAMENTO")
        d = ar.detalhe_rota(self.conn, r, agora=AGORA, com_despesas=True)
        self.assertEqual(d["total_despesas"], 20.0)
        self.assertEqual(d["despesas"][1]["tipo"], "ESTACIONAMENTO")


import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes  # o Flask acha a pasta templates/ pelo modulo registrado
_spec.loader.exec_module(painel_agentes)


class TestTela(_Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(banco, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel="logistica"):
        with self.cliente.session_transaction() as s:
            s["nivel_acesso"] = nivel
            s["usuario"] = "teste"

    def _com_telas(self, telas):
        conta = {"usuario": "teste", "nome": "Teste", "nivel": "logistica", "trocar_senha": False, "ativo": True,
                 "versao_senha": 1, "equipe": "", "portal_equipe": False, "telas": telas}
        for alvo in (mock.patch.object(painel_agentes.acesso_telas, "conta", return_value=conta),
                     mock.patch.object(painel_agentes, "_sessao_na_versao", return_value=True)):
            alvo.start()
            self.addCleanup(alvo.stop)

    def _rota_com_despesa(self, data=DIA):
        r = self.rota(data=data)
        self.parada(r, 1, f"{data} 08:30:00")
        self.conn.execute("INSERT INTO nucleo_pedagios (rota_id, valor_informado) VALUES (?, 12.5)", (r,))
        self.conn.commit()
        return r

    def test_sem_login_nao_entra(self):
        self.assertIn(self.cliente.get("/acompanhamento").status_code, (302, 401, 403))
        self.assertEqual(self.cliente.get("/api/acompanhamento/rotas").status_code, 401)

    def test_pagina_abre(self):
        self._logar()
        r = self.cliente.get("/acompanhamento")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        self.assertIn("Acompanhamento", r.get_data(as_text=True))

    def test_api_rotas_devolve_lista(self):
        self._logar()
        rid = self._rota_com_despesa()
        d = self.cliente.get(f"/api/acompanhamento/rotas?data={DIA}").get_json()
        self.assertEqual(d["data"], DIA)
        self.assertRegex(d["atualizado_em"], r"^\d\d:\d\d$")
        self.assertEqual([x["rota_id"] for x in d["rotas"]], [rid])
        self.assertEqual(d["rotas"][0]["semaforo"], "verde")

    def test_data_invalida_cai_em_hoje(self):
        self._logar()
        hoje = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(self.cliente.get("/api/acompanhamento/rotas?data=31/02").get_json()["data"], hoje)
        self.assertEqual(self.cliente.get("/api/acompanhamento/rotas").get_json()["data"], hoje)

    def test_atendimento_entra_como_na_torre_mas_sem_despesas(self):
        self._logar("atendimento")
        rid = self._rota_com_despesa()
        self.assertEqual(self.cliente.get("/acompanhamento").status_code, 200)
        r = self.cliente.get(f"/api/acompanhamento/rota/{rid}")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("despesas", r.get_json())

    def test_botao_de_documentos_so_pra_quem_pode_enviar(self):
        marca = 'id="ac-modal-docs"'
        self._logar("logistica")
        self.assertIn(marca, self.cliente.get("/acompanhamento").get_data(as_text=True))
        self._logar("leitura")   # le a tela, mas nao manda e-mail pela /pedidos
        self.assertNotIn(marca, self.cliente.get("/acompanhamento").get_data(as_text=True))

    def test_documentos_com_tela_personalizada(self):
        marca = 'id="ac-modal-docs"'
        self._logar()
        self._com_telas(["acompanhamento"])
        self.assertNotIn(marca, self.cliente.get("/acompanhamento").get_data(as_text=True))

    def test_documentos_com_tela_de_pedidos_liberada(self):
        self._logar()
        self._com_telas(["acompanhamento", "pedidos_tabela"])
        self.assertIn('id="ac-modal-docs"', self.cliente.get("/acompanhamento").get_data(as_text=True))

    def _post_cancelar(self, parada_id, motivo="baixou o pedido errado"):
        return self.cliente.post(f"/api/acompanhamento/parada/{parada_id}/cancelar-finalizacao", json={"motivo": motivo},
                                 headers={"Origin": "http://localhost"})

    def test_cancelar_finalizacao_so_pra_quem_pode(self):
        for nivel in ("leitura", "atendimento"):
            self._logar(nivel)
            self.assertEqual(self._post_cancelar(1).status_code, 403, nivel)

    def test_cancelar_finalizacao_recusa_vira_400_com_a_mensagem(self):
        self._logar("logistica")
        r = self._post_cancelar(999)
        self.assertEqual(r.status_code, 400)
        self.assertIn("não encontrada", r.get_json()["erro"])

    def test_cancelar_finalizacao_chama_o_modulo(self):
        self._logar("logistica")
        rid = self.rota()
        pid = self.parada(rid, 1, f"{DIA} 08:30:00")
        with mock.patch.object(painel_agentes.cancelar_finalizacao, "cancelar_finalizacao",
                               return_value={"ok": True, "novo_codigo": "PS-1-R1", "nova_parada_id": 77}) as chamada,                 mock.patch.object(painel_agentes.tratativas, "registrar_evento") as tratativa:
            r = self._post_cancelar(pid)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["novo_codigo"], "PS-1-R1")
        self.assertEqual(chamada.call_args.args[1:4], (pid, "teste", "baixou o pedido errado"))
        self.assertIn("PS-1-R1", tratativa.call_args.kwargs["texto"])

    def test_detalhe_diz_se_pode_cancelar_finalizacao(self):
        rid = self.rota()
        pid = self.parada(rid, 1, f"{DIA} 08:30:00")
        from nucleo import ponte_vuupt
        with mock.patch.object(ponte_vuupt, "rota_operavel", return_value=True):
            self._logar("logistica")
            d = self.cliente.get(f"/api/acompanhamento/rota/{rid}").get_json()
            self.assertTrue(d["rota"]["pode_cancelar_finalizacao"])
            self.assertEqual((d["paradas"][0]["parada_id"], d["paradas"][0]["finalizacao_cancelada"]), (pid, False))
            self._logar("leitura")
            self.assertFalse(self.cliente.get(f"/api/acompanhamento/rota/{rid}").get_json()["rota"]["pode_cancelar_finalizacao"])
        with mock.patch.object(ponte_vuupt, "rota_operavel", return_value=False):
            self._logar("logistica")
            self.assertFalse(self.cliente.get(f"/api/acompanhamento/rota/{rid}").get_json()["rota"]["pode_cancelar_finalizacao"])

    def test_api_rota_inexistente_404(self):
        self._logar()
        self.assertEqual(self.cliente.get("/api/acompanhamento/rota/999").status_code, 404)

    def test_api_rota_por_nivel_traz_despesas(self):
        self._logar("logistica")
        rid = self._rota_com_despesa()
        d = self.cliente.get(f"/api/acompanhamento/rota/{rid}").get_json()
        self.assertEqual(d["total_despesas"], 12.5)
        self.assertEqual(len(d["paradas"]), 1)

    def test_api_sem_acesso_a_pedagios_omite_despesas(self):
        self._logar()
        self._com_telas(["acompanhamento"])
        rid = self._rota_com_despesa()
        r = self.cliente.get(f"/api/acompanhamento/rota/{rid}")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("despesas", r.get_json())

    def test_api_com_tela_de_pedagios_traz_despesas(self):
        self._logar()
        self._com_telas(["acompanhamento", "pedagios"])
        rid = self._rota_com_despesa()
        self.assertIn("despesas", self.cliente.get(f"/api/acompanhamento/rota/{rid}").get_json())

    def test_tela_personalizada_sem_acompanhamento_nao_entra(self):
        self._logar()
        self._com_telas(["pedagios"])
        self.assertEqual(self.cliente.get("/api/acompanhamento/rotas").status_code, 403)


if __name__ == "__main__":
    unittest.main()
