# -*- coding: utf-8 -*-
"""
test_janelas_horario.py

Testes da janela de horário de entrega do cliente na roteirização
(pedido do Hugo, 09/09): resolução da janela por pedido
(resolver_janela), simulação da linha do tempo (simular_horarios),
sequenciamento com janelas (ordenar_com_janelas) e as travas de
formação/ordem final (janela_viavel/janela_respeitada) -- inclusive a
garantia de que SEM janela nada muda em relação ao 2-opt de sempre.

Nenhum teste geocodifica nada: os dicts carregam latitude/longitude.
Rodar (da raiz):
    python -m unittest roteirizacao.test_janelas_horario -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
import otimizacao_rotas as ot

BASE = (0.0, 0.0)


def _coords(servico):
    lat, lng = servico.get("latitude"), servico.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


def _servico(i, lat, lng, janela=None, nivel=1, code=None):
    s = {"id": i, "code": code or f"PS-{i}", "address": f"P{i}", "_nivel_dificuldade": nivel,
         "latitude": lat, "longitude": lng, "dimension_3": 1}
    if janela:
        s["_janela_inicio"], s["_janela_fim"], s["_janela_fonte"] = janela[0], janela[1], "teste"
    return s


class HorasTestCase(unittest.TestCase):

    def test_parse_formatos(self):
        self.assertEqual(rd._hhmm_para_horas("14:30"), 14.5)
        self.assertEqual(rd._hhmm_para_horas("8:00"), 8.0)
        self.assertEqual(rd._hhmm_para_horas("14h"), 14.0)
        self.assertEqual(rd._hhmm_para_horas("14h30"), 14.5)
        self.assertEqual(rd._hhmm_para_horas("14:30:00"), 14.5)
        self.assertIsNone(rd._hhmm_para_horas("25:00"))
        self.assertIsNone(rd._hhmm_para_horas("manhã"))
        self.assertIsNone(rd._hhmm_para_horas(None))
        self.assertEqual(rd.normalizar_hhmm("8h"), "08:00")
        self.assertEqual(rd._horas_para_hhmm(13.75), "13:45")

    def test_start_at_rota_segue_hora_de_inicio(self):
        from datetime import date
        with mock.patch.object(rd, "HORA_INICIO_ROTA", "06:00"):
            self.assertEqual(rd.start_at_rota(date(2026, 9, 10)), "2026-09-10T09:00:00Z")
        with mock.patch.object(rd, "HORA_INICIO_ROTA", "10:00"):
            self.assertEqual(rd.start_at_rota(date(2026, 9, 10)), "2026-09-10T13:00:00Z")
        # padrão de produção: início às 06:00 e simulador saindo na mesma hora
        self.assertEqual(rd.HORA_INICIO_ROTA, "06:00")
        self.assertEqual(rd.HORA_SAIDA_BASE, rd._hhmm_para_horas(rd.HORA_INICIO_ROTA))

    def test_janela_util_ignora_padroes(self):
        self.assertIsNone(rd._janela_util("08:00", "18:00"))
        self.assertIsNone(rd._janela_util("08:00", "16:00"))
        self.assertIsNone(rd._janela_util("00:00", "23:59"))
        self.assertIsNone(rd._janela_util("15:00", "12:00"))
        self.assertEqual(rd._janela_util("8h", "12h"), ("08:00", "12:00"))
        # scheduled_start == scheduled_end ("às 11h") vira janela de 1h
        self.assertEqual(rd._janela_util("11:00", "11:00"), ("11:00", "12:00"))


class ResolverJanelaTestCase(unittest.TestCase):

    def test_sem_nada_sem_janela(self):
        s = {"code": "PS-1"}
        self.assertEqual(rd.resolver_janela(s), (None, None, None))

    def test_agendamento_confirmado_vence(self):
        s = {"code": "PS-1", "scheduled_start": "2026-09-10T08:00:00-03:00",
             "scheduled_end": "2026-09-10T18:00:00-03:00",
             "_horario_atendimento_inicio": "07:00", "_horario_atendimento_fim": "17:00"}
        ini, fim, fonte = rd.resolver_janela(s, {"PS-1": ("14:00", "15:00")})
        self.assertEqual((ini, fim, fonte), ("14:00", "15:00", rd.FONTE_JANELA_AGENDAMENTO))

    def test_scheduled_padrao_cai_no_atendimento(self):
        s = {"code": "PS-1", "scheduled_start": "2026-09-10T08:00:00-03:00",
             "scheduled_end": "2026-09-10T18:00:00-03:00",
             "_horario_atendimento_inicio": "07:00", "_horario_atendimento_fim": "12:00"}
        self.assertEqual(rd.resolver_janela(s), ("07:00", "12:00", rd.FONTE_JANELA_ATENDIMENTO))

    def test_scheduled_real_intersecta_atendimento(self):
        s = {"code": "PS-1", "scheduled_start": "2026-09-10T09:00:00-03:00",
             "scheduled_end": "2026-09-10T14:00:00-03:00",
             "_horario_atendimento_inicio": "11:00", "_horario_atendimento_fim": "17:00"}
        self.assertEqual(rd.resolver_janela(s), ("11:00", "14:00", rd.FONTE_JANELA_AGENDAMENTO))

    def test_intersecao_vazia_vale_o_agendamento(self):
        s = {"code": "PS-1", "scheduled_start": "2026-09-10T15:00:00-03:00",
             "scheduled_end": "2026-09-10T16:00:00-03:00",
             "_horario_atendimento_inicio": "08:00", "_horario_atendimento_fim": "12:00"}
        self.assertEqual(rd.resolver_janela(s), ("15:00", "16:00", rd.FONTE_JANELA_AGENDAMENTO))

    def test_operating_hour_do_customer_como_reserva(self):
        s = {"code": "PS-1", "customer": {"operating_hour_start": "13:00", "operating_hour_end": "17:00"}}
        self.assertEqual(rd.resolver_janela(s), ("13:00", "17:00", rd.FONTE_JANELA_ATENDIMENTO))

    def test_injetar_janelas(self):
        lote = [{"code": "#PS-1"}, {"code": "PS-2", "_horario_atendimento_inicio": "09:00",
                                    "_horario_atendimento_fim": "11:00"}]
        n = rd.injetar_janelas(lote, {"PS-1": ("14:00", "16:00")})
        self.assertEqual(n, 2)
        self.assertEqual(rd.extrair_janela(lote[0]), (14.0, 16.0))
        self.assertEqual(rd.extrair_janela(lote[1]), (9.0, 11.0))
        self.assertIsNone(rd.extrair_janela({"_janela_inicio": "00:00", "_janela_fim": "23:59"}))


class _SemGeocodificar(unittest.TestCase):
    """obter_coordenadas lê latitude/longitude do próprio dict -- nenhum
    teste bate no cache/Google (mesmo padrão de test_orcamento_horas).
    Saída da base fixada em 10:00 nos cenários (as janelas dos testes
    foram desenhadas em torno dela; produção sai às HORA_INICIO_ROTA)."""

    def setUp(self):
        self._patches = [
            mock.patch.object(rd, "obter_coordenadas", lambda s, k=None: _coords(s)),
            mock.patch.object(ot, "obter_coordenadas", lambda s, k=None: _coords(s)),
            mock.patch.object(rd, "HORA_SAIDA_BASE", 10.0),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])


class SimuladorTestCase(_SemGeocodificar):

    def setUp(self):
        super().setUp()
        rd.COORDS_BASE = None

    def test_espera_e_atraso(self):
        # duas paradas coladas na base (sem deslocamento relevante): a 1ª
        # só abre às 12h (espera 2h a partir das 10h), a 2ª fecha às 11h
        # (chega ~12:15, atraso ~1,25h)
        rota = [_servico(1, 0.0, 0.0, ("12:00", "13:00")), _servico(2, 0.0, 0.0, ("08:00", "11:00"))]
        sim = rd.simular_horarios(rota, coords_base=BASE)
        self.assertAlmostEqual(sim["chegadas"][0], 10.0)
        self.assertAlmostEqual(sim["espera_h"], 2.0)
        self.assertAlmostEqual(sim["chegadas"][1], 12.0 + rd.TEMPO_PARADA_NORMAL_HORAS)
        self.assertAlmostEqual(sim["atraso_h"], 1.0 + rd.TEMPO_PARADA_NORMAL_HORAS)
        self.assertEqual(sim["fora_janela"], [1])
        self.assertAlmostEqual(sim["duracao_h"], sim["fim_h"] - 10.0)

    def test_sem_janela_duracao_bate_com_estimador(self):
        rota = [_servico(1, 0.1, 0.0), _servico(2, 0.2, 0.0, nivel=3), _servico(3, 0.1, 0.1)]
        sim = rd.simular_horarios(rota, coords_base=BASE)
        self.assertAlmostEqual(sim["duracao_h"], rd.estimar_tempo_rota(rota, coords_base=BASE, coords_fn=_coords))
        self.assertTrue(rd.janela_respeitada(rota, coords_base=BASE))
        self.assertTrue(rd.janela_viavel(rota, coords_base=BASE))

    def test_atraso_intrinseco_nao_bloqueia(self):
        # janela que já fechou antes da saída da base: nenhum agrupamento resolve
        rota = [_servico(1, 0.0, 0.0, ("06:00", "08:00")), _servico(2, 0.01, 0.0)]
        self.assertTrue(rd.janela_respeitada(rota, coords_base=BASE))


class SequenciamentoTestCase(_SemGeocodificar):

    def setUp(self):
        super().setUp()
        rd.COORDS_BASE = BASE

    def test_sem_janela_igual_ao_2opt_antigo(self):
        # ordem farthest-first + 2-opt por km, posição 0 fixa (mais distante)
        rota = [_servico(1, 0.05, 0.0), _servico(2, 0.30, 0.0), _servico(3, 0.10, 0.10),
                _servico(4, 0.20, 0.05), _servico(5, 0.02, 0.02)]
        ordem = ot.ordenar_2opt(rota, *BASE)
        self.assertEqual(ordem[0]["id"], 2)
        self.assertEqual({s["id"] for s in ordem}, {1, 2, 3, 4, 5})
        # 2-opt puro nunca piora o trajeto fechado em relação ao farthest-first
        def _km(seq):
            pts = [BASE] + [(s["latitude"], s["longitude"]) for s in seq] + [BASE]
            return sum(rd._distancia_km(*pts[i], *pts[i + 1]) for i in range(len(pts) - 1))
        farthest = sorted(rota, key=lambda s: rd._distancia_km(s["latitude"], s["longitude"], *BASE), reverse=True)
        self.assertLessEqual(_km(ordem), _km(farthest) + 1e-9)

    def test_cliente_que_fecha_cedo_vai_pro_comeco(self):
        # 4 paradas próximas; a mais PERTO da base fecha às 11h -- no
        # farthest-first ela seria a última (chegaria ~12h). Com janela,
        # o sequenciador a traz pra frente.
        rota = [_servico(1, 0.20, 0.0), _servico(2, 0.15, 0.02), _servico(3, 0.10, 0.0),
                _servico(4, 0.03, 0.0, ("08:00", "11:00"))]
        sem = rd.simular_horarios(sorted(rota, key=lambda s: -s["latitude"]), coords_base=BASE)
        self.assertGreater(sem["atraso_h"], 0.5)  # premissa: farthest-first atrasa mesmo
        ordem = ot.ordenar_2opt(rota, *BASE)
        sim = rd.simular_horarios(ordem, coords_base=BASE)
        self.assertLessEqual(sim["atraso_h"], rd.TOLERANCIA_JANELA_HORAS)
        self.assertLess(ordem.index(next(s for s in ordem if s["id"] == 4)), 3)

    def test_cliente_que_abre_tarde_vai_pro_fim(self):
        rota = [_servico(1, 0.20, 0.0, ("14:00", "17:00")), _servico(2, 0.15, 0.02),
                _servico(3, 0.10, 0.0), _servico(4, 0.03, 0.0)]
        ordem = ot.ordenar_2opt(rota, *BASE)
        sim = rd.simular_horarios(ordem, coords_base=BASE)
        # sem esperar 4h parado: o pedido das 14h deixa de ser o 1º
        self.assertNotEqual(ordem[0]["id"], 1)
        self.assertLess(sim["espera_h"], 2.5)
        self.assertEqual(sim["atraso_h"], 0.0)

    # Geometria dos testes de trava: A a leste e B ao norte da base, ambos
    # a ~28 km (0,25 grau, ~2,4h de perna) e a ~39 km entre si. Cada um
    # SOZINHO chega ~12:24 (janela até 12:30 ok); os dois na mesma rota, o
    # segundo chega ~15:30 -- 3h fora da janela.
    _A = (0.0, 0.25)
    _B = (0.25, 0.0)

    def test_janela_viavel_bloqueia_combinacao_impossivel(self):
        a = _servico(1, *self._A, ("08:00", "12:30"))
        b = _servico(2, *self._B, ("08:00", "12:30"))
        self.assertTrue(rd.janela_viavel([a]))
        self.assertTrue(rd.janela_viavel([b]))
        self.assertFalse(rd.janela_viavel([a, b]))
        # mesmos dois sem janela: passa
        c, d = _servico(3, *self._A), _servico(4, *self._B)
        self.assertTrue(rd.janela_viavel([c, d]))

    def test_dividir_em_sublotes_separa_por_janela(self):
        # A e vizinho de A (sem janela) cabem juntos; B precisa de rota própria
        rota = [_servico(1, *self._A, ("08:00", "12:30")), _servico(2, 0.01, 0.25),
                _servico(3, *self._B, ("08:00", "12:30"))]
        sublotes = rd.dividir_em_sublotes(rota, tamanho_minimo=1, tamanho_maximo=18,
                                          volume_maximo=100, distancia_maxima_km=None)
        ids = [sorted(s["id"] for s in sub) for sub in sublotes]
        self.assertEqual(sorted(ids), [[1, 2], [3]])
        self.assertTrue(all(rd.janela_viavel(sub) for sub in sublotes), ids)

    def test_savings_e_sweep_respeitam_janela(self):
        rota = [_servico(1, *self._A, ("08:00", "12:30")), _servico(2, 0.01, 0.25),
                _servico(3, *self._B, ("08:00", "12:30"))]
        for agrupar in (ot.agrupar_por_savings, ot.agrupar_por_sweep):
            sublotes = agrupar(rota, *BASE, tamanho_maximo=18, volume_maximo=100, distancia_maxima_km=None)
            ids = sorted(sorted(s["id"] for s in sub) for sub in sublotes)
            self.assertEqual(ids, [[1, 2], [3]], agrupar.__name__)

    def test_reparar_quebra_por_janela_na_ordem_final(self):
        rota = [_servico(1, *self._A, ("08:00", "12:30")), _servico(2, *self._B, ("08:00", "12:30"))]
        reparados, qtd = rd.reparar_sublotes_por_horas([rota])
        self.assertEqual(qtd, 1)
        self.assertEqual(len(reparados), 2)


if __name__ == "__main__":
    unittest.main()
