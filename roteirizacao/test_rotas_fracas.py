# -*- coding: utf-8 -*-
"""
Rotas fracas (Hugo, 29/09): corte, prazo de 3 dias uteis, motivo de nao
segurar e juncao com folga. Spec: docs/superpowers/specs/
2026-09-29-rotas-fracas-design.md
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_rotas_fracas -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
import rotas_fracas as rf

BASE = (0.0, 0.0)
TERCA = date(2026, 9, 29)


def _servico(i, lat=0.10, lng=0.0, caixas=1, nivel=1, **extra):
    s = {"id": i, "code": f"#PS-{1000 + i}", "address": f"P{i}", "_nivel_dificuldade": nivel,
         "latitude": lat, "longitude": lng, "dimension_3": caixas, "sender_id": 1,
         "created_at": "2026-09-28 15:00:00"}  # segunda, 12h de Brasilia
    s.update(extra)
    return s


def _ids(sublotes):
    return sorted(s["id"] for sub in sublotes for s in sub)


class TestCorte(unittest.TestCase):
    def setUp(self):
        self.horas = 1.0
        self.chamadas = []

        def _estimar(sub, k=None, b=None):
            self.chamadas.append((len(sub), k, b))
            return self.horas
        p = mock.patch.object(rf, "estimar_tempo_rota", _estimar)
        p.start()
        self.addCleanup(p.stop)

    def _sete_com_quarenta(self):
        rota = [_servico(i, caixas=5) for i in range(7)]
        rota[0]["dimension_3"] = 10  # 10 + 6*5 = 40
        return rota

    def test_sete_pedidos_e_quarenta_caixas_e_fraca(self):
        self.assertTrue(rf.eh_rota_fraca(self._sete_com_quarenta()))

    def test_sete_pedidos_quarenta_caixas_e_4h54_e_fraca(self):
        self.horas = 4.9
        self.assertTrue(rf.eh_rota_fraca(self._sete_com_quarenta(), "chave", BASE))
        self.assertEqual(self.chamadas, [(7, "chave", BASE)])

    def test_com_5h_nao_e_fraca(self):
        self.horas = 5.0
        self.assertFalse(rf.eh_rota_fraca(self._sete_com_quarenta()))

    def test_rota_de_uma_parada_usa_o_mesmo_estimador(self):
        self.horas = 5.2  # 1 parada longe da base: a perna da base pesa
        self.assertFalse(rf.eh_rota_fraca([_servico(1)], None, BASE))
        self.assertEqual(self.chamadas, [(1, None, BASE)])

    def test_oito_pedidos_nao_e_fraca(self):
        self.assertFalse(rf.eh_rota_fraca([_servico(i) for i in range(8)]))

    def test_quarenta_e_uma_caixas_nao_e_fraca(self):
        self.assertFalse(rf.eh_rota_fraca([_servico(1, caixas=41)]))

    def test_um_pedido_com_muita_carga_nao_e_fraca(self):
        self.assertFalse(rf.eh_rota_fraca([_servico(1, caixas=294)]))

    def test_rota_vazia_nao_e_fraca(self):
        self.assertFalse(rf.eh_rota_fraca([]))

    def test_resumo_da_rota(self):
        self.assertEqual(rf.resumo_da_rota([_servico(1, caixas=9), _servico(2, caixas=8)]), "2 pedidos, 17 caixas")
        self.assertEqual(rf.resumo_da_rota([_servico(1, caixas=1)]), "1 pedido, 1 caixa")


class TestPrazo(unittest.TestCase):
    def test_entrada_segunda_vence_quinta(self):
        self.assertEqual(rf.prazo_final(date(2026, 9, 28)), date(2026, 10, 1))

    def test_entrada_sexta_vence_quarta(self):
        self.assertEqual(rf.prazo_final(date(2026, 10, 2)), date(2026, 10, 7))

    def test_entrada_sabado_conta_a_partir_de_segunda(self):
        self.assertEqual(rf.prazo_final(date(2026, 10, 3)), date(2026, 10, 8))

    def test_proximo_dia_util_pula_fim_de_semana(self):
        self.assertEqual(rf.proximo_dia_util(date(2026, 10, 2)), date(2026, 10, 5))
        self.assertEqual(rf.proximo_dia_util(date(2026, 9, 29)), date(2026, 9, 30))

    def test_entrada_converte_utc_para_brasilia(self):
        # 01:30 UTC de terca = 22:30 de segunda em Brasilia
        self.assertEqual(rf.data_entrada({"created_at": "2026-09-29 01:30:00"}), date(2026, 9, 28))

    def test_entrada_respeita_fuso_explicito(self):
        self.assertEqual(rf.data_entrada({"created_at": "2026-09-28T23:30:00-03:00"}), date(2026, 9, 28))

    def test_entrada_ausente_ou_invalida(self):
        self.assertIsNone(rf.data_entrada({}))
        self.assertIsNone(rf.data_entrada({"created_at": "ontem"}))


class TestMotivoNaoSegurar(unittest.TestCase):
    def setUp(self):
        for alvo, valor in ((("macro_regiao_do_servico"), lambda s, k=None: rd.MACRO_GRANDE_SP),
                            (("regra_dia_fixo_do_servico"), lambda s: None)):
            p = mock.patch.object(rf, alvo, valor)
            p.start()
            self.addCleanup(p.stop)

    def test_pedido_comum_pode_esperar(self):
        self.assertIsNone(rf.motivo_nao_segurar(_servico(1), TERCA, set()))

    def test_agendamento(self):
        s = _servico(1, scheduled_start="2026-09-29T09:00:00-03:00")
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, set()), "tem agendamento")

    def test_reentrega_pelo_campo(self):
        s = _servico(1, recreated_order_origin_id=555)
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, set()), "é reentrega")

    def test_reentrega_pelo_sufixo(self):
        s = _servico(1, code="#PS-1001-R1")
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, set()), "é reentrega")

    def test_ja_segurado(self):
        self.assertEqual(rf.motivo_nao_segurar(_servico(1), TERCA, {"PS-1001"}), "já foi segurado uma vez")

    def test_codigo_combinado_basta_um_ja_segurado(self):
        s = _servico(1, code="#PS-1001, PS-2002")
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, {"PS-2002"}), "já foi segurado uma vez")

    def test_dia_fixo(self):
        with mock.patch.object(rf, "regra_dia_fixo_do_servico", lambda s: {"nome": "Americana", "dias": [2]}):
            self.assertEqual(rf.motivo_nao_segurar(_servico(1), TERCA, set()), "é de região de dia fixo")

    def test_viagem(self):
        with mock.patch.object(rf, "macro_regiao_do_servico", lambda s, k=None: "Campinas"):
            self.assertEqual(rf.motivo_nao_segurar(_servico(1), TERCA, set()), "é viagem")

    def test_sem_data_de_entrada(self):
        s = _servico(1)
        del s["created_at"]
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, set()), "sem data de entrada")

    def test_prazo_estouraria(self):
        # entrou quarta 23/09 -> prazo segunda 28/09; rota de terca 29/09 ja esta atrasada
        s = _servico(1, created_at="2026-09-23 15:00:00")
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, set()), "prazo vence em 28/09")

    def test_prazo_no_limite_pode(self):
        # entrou sexta 25/09 -> prazo quarta 30/09; rota de terca 29/09 adiada vai pra quarta 30/09
        s = _servico(1, created_at="2026-09-25 15:00:00")
        self.assertIsNone(rf.motivo_nao_segurar(s, TERCA, set()))


class TestAbsorver(unittest.TestCase):
    """Receptoras levam 25 caixas por parada pra NAO serem fracas (senao
    elas mesmas tentariam se juntar e o teste mediria outra coisa)."""

    def setUp(self):
        import otimizacao_rotas as ot
        import polimento_rotas as pr

        def _coords(s, k=None):
            lat, lng = s.get("latitude"), s.get("longitude")
            return (lat, lng) if lat is not None and lng is not None else None

        # obter_coordenadas e importada POR NOME em cada modulo: sem trocar
        # em todos, o pedido sem coordenada cairia na geocodificacao real
        patches = [mock.patch.object(m, "obter_coordenadas", _coords) for m in (rd, ot, pr, rf)]
        patches.append(mock.patch.object(rf, "macro_regiao_predominante_do_sublote",
                                         lambda sub, k=None: "GRANDE_SP"))
        # macro de cada parada (filtro da receptora): sem isso a
        # geocodificacao real rodaria
        patches.append(mock.patch.object(rf, "macro_regiao_do_servico", lambda s, k=None: "GRANDE_SP"))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        rd.COORDS_BASE = BASE
        rd.HORA_SAIDA_BASE = 6.0

    def _absorver(self, sublotes, **kw):
        params = dict(tamanho_maximo=16, volume_maximo=100, distancia_maxima_km=15, km_acumulado_maximo=60)
        params.update(kw)
        return rf.absorver_rotas_fracas(sublotes, *BASE, None, **params)

    def test_junta_com_folga_de_distancia(self):
        # 1 esta a ~17,8 km de 3 e 4: nao cabia com 15 km, cabe com 20
        fraca = [_servico(1, 0.10, 0.00)]
        vizinha = [_servico(3, 0.10, 0.16, caixas=25), _servico(4, 0.10, 0.161, caixas=25)]
        saida, rel = self._absorver([fraca, vizinha])
        self.assertEqual(len(saida), 1)
        self.assertEqual(_ids(saida), [1, 3, 4])
        self.assertEqual(rel["juntadas"], 1)
        self.assertEqual(rel["motivos"], {})
        self.assertEqual(rel["receptoras"], {id(saida[0])})

    def test_nao_junta_alem_da_folga(self):
        # ~22 km: passa dos 20
        fraca = [_servico(1, 0.10, 0.00)]
        vizinha = [_servico(3, 0.10, 0.20, caixas=25), _servico(4, 0.10, 0.201, caixas=25)]
        saida, rel = self._absorver([fraca, vizinha])
        self.assertEqual(sorted(len(s) for s in saida), [1, 2])
        self.assertEqual(_ids(saida), [1, 3, 4])
        self.assertEqual(rel["juntadas"], 0)
        rota_fraca = next(s for s in saida if len(s) == 1)
        self.assertEqual(rel["motivos"], {id(rota_fraca): "vizinha mais próxima a 22 km"})
        self.assertEqual(rel["receptoras"], set())

    def test_nao_cede_em_caixas_alem_da_folga(self):
        # 30 + 90 = 120: passa ate dos 110 da receptora
        fraca = [_servico(1, 0.10, 0.00, caixas=30)]
        vizinha = [_servico(3, 0.10, 0.01, caixas=45), _servico(4, 0.10, 0.011, caixas=45)]
        saida, rel = self._absorver([fraca, vizinha])
        self.assertEqual(len(saida), 2)
        self.assertEqual(rel["juntadas"], 0)
        self.assertEqual(list(rel["motivos"].values()),
                         ["não coube nas vizinhas (distância, paradas, caixas, tempo ou janela)"])

    def test_receptora_aceita_110_caixas(self):
        fraca = [_servico(1, 0.10, 0.00, caixas=10)]
        vizinha = [_servico(3, 0.10, 0.01, caixas=50), _servico(4, 0.10, 0.011, caixas=50)]
        saida, rel = self._absorver([fraca, vizinha])
        self.assertEqual(len(saida), 1)
        self.assertEqual(sum(s["dimension_3"] for s in saida[0]), 110)
        self.assertEqual(rel["juntadas"], 1)
        self.assertEqual(rel["receptoras"], {id(saida[0])})

    def test_receptora_recusa_111_caixas(self):
        fraca = [_servico(1, 0.10, 0.00, caixas=11)]
        vizinha = [_servico(3, 0.10, 0.01, caixas=50), _servico(4, 0.10, 0.011, caixas=50)]
        saida, rel = self._absorver([fraca, vizinha])
        self.assertEqual(len(saida), 2)
        self.assertEqual(rel["juntadas"], 0)

    def _tempo_fixo(self, horas_receptora):
        """Rota de 3+ paradas leva `horas_receptora`; as menores, 1h."""
        import polimento_rotas as pr
        return mock.patch.object(pr, "estimar_tempo_rota",
                                 lambda sub, k=None, b=None: horas_receptora if len(sub) >= 3 else 1.0)

    def test_receptora_aceita_10h30(self):
        fraca = [_servico(1, 0.10, 0.00)]
        vizinha = [_servico(3, 0.10, 0.01, caixas=25), _servico(4, 0.10, 0.011, caixas=25)]
        with self._tempo_fixo(10.5):
            saida, rel = self._absorver([fraca, vizinha])
        self.assertEqual(len(saida), 1)
        self.assertEqual(rel["juntadas"], 1)

    def test_receptora_recusa_10h31(self):
        fraca = [_servico(1, 0.10, 0.00)]
        vizinha = [_servico(3, 0.10, 0.01, caixas=25), _servico(4, 0.10, 0.011, caixas=25)]
        with self._tempo_fixo(10.5 + 1 / 60):
            saida, rel = self._absorver([fraca, vizinha])
        self.assertEqual(len(saida), 2)
        self.assertEqual(rel["juntadas"], 0)

    def test_receptora_que_passou_de_100_continua_recebendo(self):
        # participantes sao calculados UMA vez no inicio: a vizinha que
        # chegou a 105 com a 1a fraca (101-110 seria VAN_HR pela regra
        # antiga, fora de _rota_polivel) recebe a 2a e fecha 110
        fraca_a = [_servico(1, 0.10, 0.009, caixas=5)]
        fraca_b = [_servico(2, 0.10, 0.012, caixas=5)]
        vizinha = [_servico(3, 0.10, 0.01, caixas=50), _servico(4, 0.10, 0.011, caixas=50)]
        saida, rel = self._absorver([fraca_a, fraca_b, vizinha])
        self.assertEqual(len(saida), 1)
        self.assertEqual(_ids(saida), [1, 2, 3, 4])
        self.assertEqual(sum(s["dimension_3"] for s in saida[0]), 110)
        self.assertEqual(rel["receptoras"], {id(saida[0])})

    def test_folga_de_paradas_e_teto_mais_dois(self):
        fraca = [_servico(1, 0.10, 0.00)]
        tres = [_servico(10 + i, 0.10, 0.01 + 0.001 * i, caixas=25) for i in range(3)]
        saida, rel = self._absorver([list(fraca), tres], tamanho_maximo=2)  # folga: 4 paradas
        self.assertEqual(len(saida), 1)
        self.assertEqual(rel["juntadas"], 1)
        # 20 caixas cada (80 + 1 = 81): quem barra aqui e o teto de paradas, nao o de caixas
        quatro = [_servico(20 + i, 0.10, 0.01 + 0.001 * i, caixas=20) for i in range(4)]
        saida, rel = self._absorver([list(fraca), quatro], tamanho_maximo=2)
        self.assertEqual(len(saida), 2)
        self.assertEqual(rel["juntadas"], 0)

    def test_reparte_entre_duas_vizinhas(self):
        fraca = [_servico(1, 0.10, 0.00), _servico(2, 0.10, 0.30)]
        oeste = [_servico(3, 0.10, 0.001, caixas=25), _servico(4, 0.10, 0.002, caixas=25)]
        leste = [_servico(5, 0.10, 0.301, caixas=25), _servico(6, 0.10, 0.302, caixas=25)]
        saida, rel = self._absorver([fraca, oeste, leste])
        grupos = sorted(sorted(s["id"] for s in sub) for sub in saida)
        self.assertEqual(grupos, [[1, 3, 4], [2, 5, 6]])
        self.assertEqual(rel["juntadas"], 1)
        self.assertEqual(rel["receptoras"], {id(sub) for sub in saida})

    def test_tudo_ou_nada(self):
        # 1 caberia na vizinha, 9 nao cabe em lugar nenhum: nada muda
        fraca = [_servico(1, 0.10, 0.00), _servico(9, 0.10, 0.60)]
        vizinha = [_servico(3, 0.10, 0.001, caixas=25), _servico(4, 0.10, 0.002, caixas=25)]
        saida, rel = self._absorver([fraca, vizinha])
        grupos = sorted(sorted(s["id"] for s in sub) for sub in saida)
        self.assertEqual(grupos, [[1, 9], [3, 4]])
        self.assertEqual(rel["juntadas"], 0)
        self.assertEqual(len(rel["motivos"]), 1)
        # a vizinha chegou a receber o 1, mas a juncao foi desfeita
        self.assertEqual(rel["receptoras"], set())

    def test_nivel4_nunca_participa(self):
        exclusiva = [_servico(7, 0.10, 0.00, nivel=4)]
        vizinha = [_servico(3, 0.10, 0.001, caixas=25), _servico(4, 0.10, 0.002, caixas=25)]
        saida, rel = self._absorver([exclusiva, vizinha])
        self.assertEqual(sorted(len(s) for s in saida), [1, 2])
        self.assertEqual(rel, {"juntadas": 0, "motivos": {}, "receptoras": set()})

    def test_macro_regiao_diferente_nao_junta(self):
        fraca = [_servico(1, 0.10, 0.00)]
        vizinha = [_servico(3, 0.10, 0.001, caixas=25), _servico(4, 0.10, 0.002, caixas=25)]
        with mock.patch.object(rf, "macro_regiao_predominante_do_sublote",
                               lambda sub, k=None: "Campinas" if sub[0]["id"] == 1 else "GRANDE_SP"):
            saida, rel = self._absorver([fraca, vizinha])
        self.assertEqual(len(saida), 2)
        self.assertEqual(list(rel["motivos"].values()), ["sem rota vizinha na mesma região"])

    def test_duas_fracas_vizinhas_viram_uma(self):
        saida, rel = self._absorver([[_servico(1, 0.10, 0.00)], [_servico(2, 0.10, 0.001)]])
        self.assertEqual(len(saida), 1)
        self.assertEqual(_ids(saida), [1, 2])
        self.assertEqual(rel["juntadas"], 1)
        # a que sobrou continua fraca e nao tem mais vizinha
        self.assertEqual(rel["motivos"], {id(saida[0]): "sem rota vizinha na mesma região"})
        # recebeu e continuou fraca: esta nos dois
        self.assertEqual(rel["receptoras"], {id(saida[0])})

    def test_fraca_absorvida_depois_de_receber_nao_conta(self):
        # 1 entra na 2 (as duas fracas); a 2, ainda fraca, entra na vizinha
        # cheia. So a vizinha devolvida e receptora
        um, dois = [_servico(1, 0.10, 0.00)], [_servico(2, 0.10, 0.001), _servico(5, 0.10, 0.0011)]
        vizinha = [_servico(3, 0.10, 0.002, caixas=25), _servico(4, 0.10, 0.0021, caixas=25)]
        saida, rel = self._absorver([um, dois, vizinha])
        self.assertEqual(len(saida), 1)
        self.assertEqual(_ids(saida), [1, 2, 3, 4, 5])
        self.assertEqual(rel["receptoras"], {id(saida[0])})

    def test_parada_de_fora_nao_entra_em_receptora_da_grande_sp(self):
        # fraca quase toda Grande SP com 1 parada de Campinas: a vizinha
        # Grande SP caberia pela distancia, mas a parada de fora nao pode
        # entrar nela -- tudo ou nada, a fraca fica com o motivo
        fraca = [_servico(1, 0.10, 0.00), _servico(2, 0.10, 0.002)]
        vizinha = [_servico(3, 0.10, 0.001, caixas=25), _servico(4, 0.10, 0.0011, caixas=25)]
        with mock.patch.object(rf, "macro_regiao_do_servico",
                               lambda s, k=None: "Campinas" if s["id"] == 2 else "GRANDE_SP"):
            saida, rel = self._absorver([fraca, vizinha])
        grupos = sorted(sorted(s["id"] for s in sub) for sub in saida)
        self.assertEqual(grupos, [[1, 2], [3, 4]])
        self.assertEqual(rel["juntadas"], 0)
        self.assertEqual(rel["receptoras"], set())
        rota_fraca = next(s for s in saida if s[0]["id"] in (1, 2))
        self.assertIn(id(rota_fraca), rel["motivos"])

    def test_pedido_sem_coordenada_nao_quebra(self):
        fraca = [_servico(1, None, None)]
        vizinha = [_servico(3, 0.10, 0.001, caixas=25), _servico(4, 0.10, 0.002, caixas=25)]
        saida, _ = self._absorver([fraca, vizinha])
        self.assertEqual(_ids(saida), [1, 3, 4])

    def test_rota_unica_do_dia(self):
        saida, rel = self._absorver([[_servico(1, 0.10, 0.00)]])
        self.assertEqual(_ids(saida), [1])
        self.assertEqual(list(rel["motivos"].values()), ["sem rota vizinha na mesma região"])


class TestRotaValidaTempo(unittest.TestCase):
    """_rota_valida: teto de horas parametrizavel; o padrao (polimento)
    continua 9h."""

    def setUp(self):
        import polimento_rotas as pr
        self.pr = pr
        p = mock.patch.object(pr, "estimar_tempo_rota", lambda sub, k=None, b=None: 9.5)
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(pr, "obter_coordenadas", lambda s, k=None: (s["latitude"], s["longitude"]))
        p2.start()
        self.addCleanup(p2.stop)

    def _valida(self, **kw):
        rota = [_servico(1, 0.10, 0.00), _servico(2, 0.10, 0.001)]
        return self.pr._rota_valida(rota, None, 16, 100, 15, None, None, BASE, **kw)

    def test_padrao_continua_9h(self):
        self.assertFalse(self._valida())

    def test_teto_informado_vale(self):
        self.assertTrue(self._valida(tempo_maximo_horas=10.5))

    def test_caixas_padrao_continua_100(self):
        rota = [_servico(1, 0.10, 0.00, caixas=51), _servico(2, 0.10, 0.001, caixas=50)]
        self.assertFalse(self.pr._rota_valida(rota, None, 16, 100, 15, None, None, BASE, tempo_maximo_horas=10.5))


if __name__ == "__main__":
    unittest.main()
