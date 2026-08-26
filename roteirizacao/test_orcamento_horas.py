# -*- coding: utf-8 -*-
"""
test_orcamento_horas.py

Testes do orçamento de horas calibrado (25/08): estimador com perna da
base, fator estrada e duas velocidades (roteirizacao_dados.
estimar_tempo_rota), rede de segurança pós-2opt (reparar_sublotes_por_
horas) e a PARIDADE da trava nos modelos alternativos (Clarke-Wright
via _fusao_valida, Sweep via _empacotar_ganancioso) -- antes só o
modelo Atual tinha a trava e a seleção diária premiava quem a ignorava.

Nenhum teste geocodifica nada: obter_coordenadas é substituída por uma
leitura direta de latitude/longitude dos dicts. Rodar (da raiz):
    python -m unittest roteirizacao.test_orcamento_horas -v
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
GRAU_KM = 111.0  # 1 grau de latitude ~ 111 km


def _coords(servico):
    lat, lng = servico.get("latitude"), servico.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


def _servico(i, nivel=1, lat=0.0, lng=0.0):
    return {"id": i, "address": f"P{i}", "_nivel_dificuldade": nivel,
            "latitude": lat, "longitude": lng, "dimension_3": 1}


class EstimadorTestCase(unittest.TestCase):

    def setUp(self):
        rd.COORDS_BASE = None

    def test_so_paradas_sem_coordenada(self):
        rota = [_servico(1, 1), _servico(2, 2), _servico(3, 3)]
        for s in rota:
            s["latitude"] = s["longitude"] = None
        esperado = 2 * rd.TEMPO_PARADA_NORMAL_HORAS + rd.TEMPO_NIVEL3_HORAS
        self.assertAlmostEqual(rd.estimar_tempo_rota(rota, coords_fn=_coords), esperado)

    def test_perna_da_base_com_fator_estrada(self):
        # 1 parada a ~11,1 km da base (0,1 grau), velocidade urbana
        rota = [_servico(1, 1, lat=0.1, lng=0.0)]
        km = rd._distancia_km(*BASE, 0.1, 0.0)
        esperado = rd.TEMPO_PARADA_NORMAL_HORAS + km * rd.FATOR_ESTRADA / rd.VELOCIDADE_MEDIA_KMH
        self.assertAlmostEqual(rd.estimar_tempo_rota(rota, coords_base=BASE, coords_fn=_coords), esperado)
        # sem base registrada nem informada: só a parada
        self.assertAlmostEqual(rd.estimar_tempo_rota(rota, coords_fn=_coords), rd.TEMPO_PARADA_NORMAL_HORAS)

    def test_perna_longa_usa_velocidade_de_rodovia(self):
        rota = [_servico(1, 1, lat=1.0, lng=0.0)]  # ~111 km
        km = rd._distancia_km(*BASE, 1.0, 0.0)
        self.assertGreater(km, rd.PERNA_RODOVIA_KM)
        esperado = rd.TEMPO_PARADA_NORMAL_HORAS + rd._tempo_perna_horas(km)
        self.assertAlmostEqual(rd.estimar_tempo_rota(rota, coords_base=BASE, coords_fn=_coords), esperado)

    def test_perna_horas_e_continua_e_monotona(self):
        # cruza PERNA_RODOVIA_KM (30km) -- achado da revisão de 25/08: a
        # versão em degrau tinha uma queda de ~4x logo depois dos 30km
        anterior = None
        for km in [x / 2 for x in range(2, 121)]:  # 1..60km, passo 0.5
            tempo = rd._tempo_perna_horas(km)
            if anterior is not None:
                self.assertGreaterEqual(tempo, anterior, f"não-monotônico em {km}km")
            anterior = tempo
        # contínua exatamente no limiar (mesmo valor dos dois lados)
        no_limiar = rd._tempo_perna_horas(rd.PERNA_RODOVIA_KM)
        pouco_antes = rd._tempo_perna_horas(rd.PERNA_RODOVIA_KM - 0.001)
        pouco_depois = rd._tempo_perna_horas(rd.PERNA_RODOVIA_KM + 0.001)
        self.assertAlmostEqual(no_limiar, pouco_antes, places=3)
        self.assertAlmostEqual(no_limiar, pouco_depois, places=3)

    def test_definir_coords_base_vale_como_padrao(self):
        rd.definir_coords_base(*BASE)
        rota = [_servico(1, 1, lat=0.1, lng=0.0)]
        com_base = rd.estimar_tempo_rota(rota, coords_fn=_coords)
        self.assertGreater(com_base, rd.TEMPO_PARADA_NORMAL_HORAS)

    def test_exige_orcamento_horas(self):
        self.assertFalse(rd.exige_orcamento_horas([_servico(1, 3)]))
        self.assertFalse(rd.exige_orcamento_horas([_servico(1, 4), _servico(2, 4)]))
        self.assertTrue(rd.exige_orcamento_horas([_servico(1, 1), _servico(2, 3)]))

    def test_exige_orcamento_horas_falso_quando_inviavel_por_distancia(self):
        # destino tão longe que a perna da base sozinha já estoura --
        # não é excesso de paradas, então a trava não deve reprovar
        rd.definir_coords_base(*BASE)
        longe = [_servico(1, 1, lat=4.0, lng=0.0), _servico(2, 1, lat=4.001, lng=0.0)]  # ~444km
        with mock.patch.object(rd, "obter_coordenadas", lambda s, k: _coords(s)):
            self.assertTrue(rd._orcamento_inviavel_por_distancia(longe))
            self.assertFalse(rd.exige_orcamento_horas(longe))


class InviabilidadePorDistanciaTestCase(unittest.TestCase):
    """Achado da revisão de 25/08: pedidos vizinhos MUITO longe da base
    (perna da base sozinha já > 9h) viravam N rotas de 1, cada uma AINDA
    acima do orçamento -- pior que ficarem juntos. As travas de horas
    não devem mais fragmentar esse caso (as outras travas continuam
    valendo)."""

    def setUp(self):
        rd.COORDS_BASE = None
        rd.definir_coords_base(*BASE)
        self._patches = [
            mock.patch.object(rd, "obter_coordenadas", lambda s, k: _coords(s)),
            mock.patch.object(ot, "obter_coordenadas", lambda s, k: _coords(s)),
        ]
        for p in self._patches:
            p.start()
        # ~444km da base, 3 pedidos a 100m entre si -- cada um sozinho
        # já estoura o orçamento só pela perna da base
        self.longe = [_servico(i, 1, lat=4.0 + i * 0.001, lng=0.0) for i in range(3)]

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_dividir_em_sublotes_nao_fragmenta_em_rotas_de_1(self):
        self.assertGreater(rd.estimar_tempo_rota([self.longe[0]]), rd.ROTA_TEMPO_MAXIMO_HORAS)
        sublotes = rd.dividir_em_sublotes(self.longe, tamanho_minimo=1, tamanho_maximo=16,
                                          volume_maximo=100, distancia_maxima_km=None, api_key=None)
        self.assertEqual(len(sublotes), 1)
        self.assertEqual(len(sublotes[0]), 3)

    def test_savings_nao_fragmenta_em_rotas_de_1(self):
        sublotes = ot.agrupar_por_savings(self.longe, *BASE, tamanho_maximo=16, volume_maximo=100,
                                          distancia_maxima_km=None, api_key=None)
        self.assertEqual(len(sublotes), 1)

    def test_reparar_nao_fragmenta_inviavel_por_distancia(self):
        novos, reparadas = rd.reparar_sublotes_por_horas([self.longe])
        self.assertEqual(reparadas, 0)
        self.assertEqual(novos, [self.longe])


class RepararTestCase(unittest.TestCase):

    def setUp(self):
        rd.COORDS_BASE = None
        self._patch = mock.patch.object(rd, "obter_coordenadas", lambda s, k: _coords(s))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_quebra_rota_acima_do_orcamento_e_mantem_pedidos(self):
        # 8 nível 3 vizinhos = 10h de parada -> não cabe em 9h
        rota = [_servico(i, 3, lat=0.01 + i * 0.001, lng=0.01) for i in range(8)]
        novos, reparadas = rd.reparar_sublotes_por_horas([rota])
        self.assertEqual(reparadas, 1)
        self.assertEqual(len(novos), 2)
        self.assertEqual(sorted(s["id"] for sub in novos for s in sub), list(range(8)))
        for sub in novos:
            self.assertLessEqual(rd.estimar_tempo_rota(sub), rd.ROTA_TEMPO_MAXIMO_HORAS)

    def test_nao_toca_rota_que_cabe(self):
        rota = [_servico(i, 1, lat=0.01, lng=0.01 + i * 0.001) for i in range(10)]
        novos, reparadas = rd.reparar_sublotes_por_horas([rota])
        self.assertEqual(reparadas, 0)
        self.assertEqual(novos, [rota])


class ParidadeModelosAlternativosTestCase(unittest.TestCase):
    """Dois clusters de 5 nível 3 a ~11 km um do outro (todo par dentro
    dos 20 km): cabem cada um numa rota (6,25h + deslocamento), mas a
    fusão dos dois (12,5h) NÃO cabe em 9h -- sem a trava de horas o
    savings fundiria tudo numa rota só de 10 pedidos (<= 18, <= 100
    caixas, <= 20 km par-a-par)."""

    def setUp(self):
        rd.COORDS_BASE = None
        self._patches = [
            mock.patch.object(rd, "obter_coordenadas", lambda s, k: _coords(s)),
            mock.patch.object(ot, "obter_coordenadas", lambda s, k: _coords(s)),
        ]
        for p in self._patches:
            p.start()
        self.cluster_a = [_servico(i, 3, lat=0.02 + i * 0.001, lng=0.02) for i in range(5)]
        self.cluster_b = [_servico(10 + i, 3, lat=0.02 + i * 0.001, lng=0.12) for i in range(5)]

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _confere(self, sublotes, pedidos):
        self.assertEqual(sorted(s["id"] for sub in sublotes for s in sub), sorted(s["id"] for s in pedidos))
        for sub in sublotes:
            if rd.exige_orcamento_horas(sub):
                self.assertLessEqual(rd.estimar_tempo_rota(sub), rd.ROTA_TEMPO_MAXIMO_HORAS)

    def test_savings_nao_funde_acima_do_orcamento(self):
        pedidos = self.cluster_a + self.cluster_b
        sublotes = ot.agrupar_por_savings(pedidos, *BASE, tamanho_maximo=18, volume_maximo=100,
                                          distancia_maxima_km=20, api_key=None)
        self.assertEqual(len(sublotes), 2)
        self._confere(sublotes, pedidos)

    def test_sweep_respeita_orcamento(self):
        pedidos = self.cluster_a + self.cluster_b
        sublotes = ot.agrupar_por_sweep(pedidos, *BASE, tamanho_maximo=18, volume_maximo=100,
                                        distancia_maxima_km=20, api_key=None)
        self.assertGreaterEqual(len(sublotes), 2)
        self._confere(sublotes, pedidos)

    def test_dividir_em_sublotes_continua_respeitando(self):
        pedidos = self.cluster_a + self.cluster_b
        sublotes = rd.dividir_em_sublotes(pedidos, tamanho_minimo=1, tamanho_maximo=18,
                                          volume_maximo=100, distancia_maxima_km=20, api_key=None)
        self.assertGreaterEqual(len(sublotes), 2)
        self._confere(sublotes, pedidos)


class FusaoCrossRegiaoTestCase(unittest.TestCase):
    """Achado da revisão de 25/08: _fundir_sublotes_entre_macrorregioes
    aceitava a fusão pelo tempo estimado na ordem de CONCATENAÇÃO e
    depois resequenciava com ordenar_2opt (farthest-first) sem checar de
    novo -- uma fusão que cabia em 9h podia estourar na ordem final e
    sair assim mesmo pro rascunho."""

    def setUp(self):
        self._patches = [
            mock.patch.object(rd, "obter_coordenadas", lambda s, k: _coords(s)),
            mock.patch.object(ot, "obter_coordenadas", lambda s, k: _coords(s)),
        ]
        for p in self._patches:
            p.start()
        rd.COORDS_BASE = None

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_fusao_cross_regiao_repara_estouro_na_ordem_final(self):
        import criar_rotas_diarias as crd
        rd.definir_coords_base(*BASE)
        # receptor: 10 nível 1 pertinho da base; pequeno: 3 nível 3 a
        # ~20km -- funde na concatenação (perto->longe, cabe em 9h: ver
        # asserção abaixo) mas o 2-opt poe o longe primeiro e estoura
        # (9,5h+); sem o reparo pós-fusão, isso saía direto pro rascunho.
        receptor = [_servico(i, 1, lat=0.02, lng=0.0) for i in range(10)]
        pequeno = [_servico(100 + i, 3, lat=0.18, lng=0.0) for i in range(3)]
        self.assertLessEqual(rd.estimar_tempo_rota(receptor + pequeno), rd.ROTA_TEMPO_MAXIMO_HORAS)
        # capturado ANTES da chamada: fundir_sublotes_pequenos muta a
        # lista `receptor` in-place (receptor.extend(pequeno))
        ids_esperados = sorted(s["id"] for s in receptor + pequeno)

        with mock.patch.object(crd, "macro_regiao_predominante_do_sublote", return_value="GRANDE_SP"), \
             mock.patch.object(crd, "classificar_rota_viagem", return_value=False):
            fundidos = crd._fundir_sublotes_entre_macrorregioes(
                [receptor, pequeno], BASE, None, "teste",
            )
        # a fusão pode ter sido desfeita pelo reparo (a ordem 2-opt
        # final não cabia em 9h) -- o que importa é que NENHUMA rota
        # final estoura o orçamento e nenhum pedido se perde/duplica.
        ids_obtidos = sorted(s["id"] for sub in fundidos for s in sub)
        self.assertEqual(ids_obtidos, ids_esperados)
        for sub in fundidos:
            if rd.exige_orcamento_horas(sub):
                self.assertLessEqual(rd.estimar_tempo_rota(sub), rd.ROTA_TEMPO_MAXIMO_HORAS)


if __name__ == "__main__":
    unittest.main()
