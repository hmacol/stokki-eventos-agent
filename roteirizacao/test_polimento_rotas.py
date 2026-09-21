# -*- coding: utf-8 -*-
"""
Polimento entre rotas (18/09): move/troca paradas entre rotas vizinhas
da mesma macro-regiao enquanto o km total cair e as travas continuarem
valendo; esvazia rota de 1-2 paradas nas vizinhas quando cabe.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_polimento_rotas -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
import otimizacao_rotas as ot
import polimento_rotas as pr

BASE = (0.0, 0.0)


def _coords(s):
    lat, lng = s.get("latitude"), s.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


def _servico(i, lat, lng, caixas=1, nivel=1):
    return {"id": i, "code": f"PS-{i}", "address": f"P{i}", "_nivel_dificuldade": nivel,
            "latitude": lat, "longitude": lng, "dimension_3": caixas, "sender_id": 1}


def _ids(sublotes):
    return sorted(s["id"] for sub in sublotes for s in sub)


class PolimentoTestCase(unittest.TestCase):

    def setUp(self):
        self._patches = [
            mock.patch.object(rd, "obter_coordenadas", lambda s, k=None: _coords(s)),
            mock.patch.object(ot, "obter_coordenadas", lambda s, k=None: _coords(s)),
            mock.patch.object(pr, "macro_regiao_predominante_do_sublote", lambda sub, k=None: "GRANDE_SP"),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        rd.COORDS_BASE = BASE
        # travado explicitamente (nao so o default do modulo) pros testes
        # de orcamento de horas/janela ficarem deterministicos mesmo que
        # outro modulo de teste, rodando no mesmo processo, tenha mudado
        # a global via definir_hora_saida_base
        rd.HORA_SAIDA_BASE = 6.0

    def _polir(self, sublotes, **kw):
        params = dict(tamanho_maximo=16, volume_maximo=100, distancia_maxima_km=20)
        params.update(kw)
        return pr.polir_entre_rotas(sublotes, *BASE, None, **params)

    def test_realoca_parada_perdida_pra_rota_vizinha(self):
        # a rota A fica com 3 paradas depois da realocacao (nao entra no
        # esvaziamento, que so olha rota de 1-2 paradas)
        a = [_servico(1, 0.10, 0.00), _servico(2, 0.10, 0.001), _servico(5, 0.10, 0.002), _servico(9, 0.10, 0.10)]
        b = [_servico(3, 0.10, 0.10), _servico(4, 0.10, 0.101)]
        polidos, resumo = self._polir([a, b])
        rota_do_9 = next(sub for sub in polidos if any(s["id"] == 9 for s in sub))
        self.assertEqual({s["id"] for s in rota_do_9}, {3, 4, 9})
        self.assertGreaterEqual(resumo["realocacoes"], 1)
        self.assertLess(resumo["km_depois"], resumo["km_antes"])
        self.assertEqual(_ids(polidos), [1, 2, 3, 4, 5, 9])

    def test_troca_paradas_cruzadas(self):
        a = [_servico(1, 0.10, 0.00), _servico(2, 0.10, 0.20)]
        b = [_servico(3, 0.10, 0.20), _servico(4, 0.10, 0.00)]
        polidos, resumo = self._polir([a, b])
        grupos = sorted(sorted(s["id"] for s in sub) for sub in polidos)
        self.assertEqual(grupos, [[1, 4], [2, 3]])
        self.assertGreaterEqual(resumo["realocacoes"] + resumo["trocas"], 1)

    def test_nao_estoura_tamanho_maximo(self):
        # 9 fica a 0,099 (e nao 0,10) pra nenhuma troca empatar em km
        a = [_servico(1, 0.10, 0.00), _servico(9, 0.10, 0.099)]
        b = [_servico(3, 0.10, 0.10), _servico(4, 0.10, 0.101)]
        polidos, resumo = self._polir([a, b], tamanho_maximo=2)
        self.assertEqual(sorted(len(sub) for sub in polidos), [2, 2])
        self.assertEqual(resumo["realocacoes"], 0)
        self.assertEqual(_ids(polidos), [1, 3, 4, 9])

    def test_nao_estoura_caixas(self):
        a = [_servico(1, 0.10, 0.00), _servico(9, 0.10, 0.099, caixas=50)]
        b = [_servico(3, 0.10, 0.10, caixas=30), _servico(4, 0.10, 0.101, caixas=30)]
        polidos, _ = self._polir([a, b], volume_maximo=100)
        rota_do_9 = next(sub for sub in polidos if any(s["id"] == 9 for s in sub))
        self.assertTrue({3, 4}.isdisjoint({s["id"] for s in rota_do_9}))
        for sub in polidos:
            self.assertLessEqual(sum(s["dimension_3"] for s in sub), 100)

    def test_nao_estoura_distancia_par_a_par(self):
        # 9 esta a ~22 km das paradas 3 e 4: com teto de 20 km nunca divide
        # rota com elas (a parada 1 pode ir pra rota b -- isso reduz km e e valido)
        a = [_servico(1, 0.10, 0.00), _servico(9, 0.10, 0.30)]
        b = [_servico(3, 0.10, 0.10), _servico(4, 0.10, 0.101)]
        polidos, _ = self._polir([a, b], distancia_maxima_km=20)
        rota_do_9 = next(sub for sub in polidos if any(s["id"] == 9 for s in sub))
        self.assertTrue({3, 4}.isdisjoint({s["id"] for s in rota_do_9}))
        self.assertEqual(_ids(polidos), [1, 3, 4, 9])

    def test_rota_nivel4_fica_intocada(self):
        exclusiva = [_servico(7, 0.10, 0.10, nivel=4)]
        b = [_servico(3, 0.10, 0.10), _servico(4, 0.10, 0.101)]
        polidos, _ = self._polir([exclusiva, b])
        self.assertIn(exclusiva, polidos)
        self.assertTrue(any(sub is exclusiva or sub == exclusiva for sub in polidos))

    def test_esvazia_rota_pequena(self):
        a = [_servico(1, 0.10, 0.00)]
        b = [_servico(3, 0.10, 0.01), _servico(4, 0.10, 0.02)]
        polidos, resumo = self._polir([a, b])
        self.assertEqual(len(polidos), 1)
        self.assertEqual(_ids(polidos), [1, 3, 4])
        self.assertEqual(resumo["esvaziadas"], 1)

    def test_deterministico(self):
        a = [_servico(1, 0.10, 0.00), _servico(2, 0.10, 0.001), _servico(9, 0.10, 0.10)]
        b = [_servico(3, 0.10, 0.10), _servico(4, 0.10, 0.101)]
        p1, _ = self._polir([list(a), list(b)])
        p2, _ = self._polir([list(a), list(b)])
        self.assertEqual([[s["id"] for s in sub] for sub in p1], [[s["id"] for s in sub] for sub in p2])

    def test_teto_de_tempo_zero_devolve_igual(self):
        a = [_servico(1, 0.10, 0.00), _servico(9, 0.10, 0.10)]
        b = [_servico(3, 0.10, 0.10)]
        polidos, resumo = self._polir([a, b], tempo_maximo_s=0.0)
        self.assertEqual(_ids(polidos), [1, 3, 9])
        self.assertTrue(resumo["estourou_tempo"])
        self.assertEqual(resumo["realocacoes"], 0)

    def test_macro_regioes_diferentes_nao_trocam(self):
        a = [_servico(1, 0.10, 0.00), _servico(9, 0.10, 0.10)]
        b = [_servico(3, 0.10, 0.10)]
        macros = {id(a): "GRANDE_SP", id(b): "Campinas"}
        with mock.patch.object(pr, "macro_regiao_predominante_do_sublote", lambda sub, k=None: macros.get(id(sub), "X")):
            polidos, resumo = self._polir([a, b])
        self.assertEqual(resumo["realocacoes"], 0)
        self.assertEqual(sorted(len(sub) for sub in polidos), [1, 2])

    # -- fix round 1 (18/09): bug de duplicacao no esvaziamento de rota
    # pequena, achado pela revisao -- ver comentario em polir_entre_rotas
    # (bloco 3) e task-6-report.md, secao "Fix round 1".

    def test_esvazia_com_tres_rotas_centroide_desloca(self):
        # rota I (2 paradas) so tem B dentro do alcance da media das duas
        # (C fica de fora, ~112 km da media); depois que a primeira sai,
        # o centroide de I desloca pra perto so da parada que sobrou --
        # mas como as duas cabem em B (par a par <= 20 km), o esvaziamento
        # tem que dar certo INTEIRO dentro da vizinhanca congelada, sem
        # tocar em C. 3 rotas na entrada, C e rota de 1 parada.
        p1 = _servico(301, 0.10, -0.05)
        p2 = _servico(302, 0.10, 0.03)
        i = [p1, p2]
        b = [_servico(303, 0.10, -0.02)]
        c = [_servico(304, 0.10, 1.0)]
        entrada_ids = _ids([i, b, c])
        polidos, resumo = self._polir([i, b, c])
        self.assertEqual(_ids(polidos), entrada_ids)
        self.assertEqual(len(polidos), 2)  # I esvaziou pra dentro de B; C fica sozinha
        self.assertFalse(resumo["descartado_por_piora"])
        rota_de_c = next(sub for sub in polidos if any(s["id"] == 304 for s in sub))
        self.assertEqual({s["id"] for s in rota_de_c}, {304})  # C nunca foi tocada

    def test_esvaziamento_revertido_nao_duplica(self):
        # Reproducao do bug do fix round 1. Geometria: rota I tem 2
        # paradas -- p1 perto de B, p2 perto de C -- posicionadas de modo
        # que a MEDIA das duas fica a ~39 km de B (dentro do alcance,
        # 2x20=40) e a ~46 km de C (fora do alcance): so B entra na
        # vizinhanca inicial. p1 cabe em B (~5,6 km). Depois que p1 sai,
        # sobra so p2 (perto de C) -- se a vizinhanca fosse recalculada
        # nesse ponto (codigo antigo), C entraria no alcance (so ~1,1 km
        # de p2) e receberia p2 escrevendo numa rota FORA do backup
        # (que so cobria I e B). Como p2 NAO cabe em B (~83 km dos
        # membros de B depois que p1 entrou), o esvaziamento tem que
        # falhar e reverter -- com ganho_minimo_km gigante forcamos a
        # reversao mesmo se a colocacao desse certo.
        #
        # Confirmado ANTES do fix (fix round 1): rodando esta geometria
        # contra o codigo antigo (copiado verbatim do brief da Task 6),
        # este teste FALHAVA -- saida tinha [401, 402, 402, 403, 404]
        # (id 402 duplicado: sobrava em rotas[I] restaurada pelo backup
        # E tinha vazado pra dentro de rotas[C], que nao fazia parte do
        # backup). Ver task-6-report.md, secao "Fix round 1".
        p1 = _servico(401, 0.10, -0.25)
        p2 = _servico(402, 0.10, 0.55)
        i = [p1, p2]
        b = [_servico(403, 0.10, -0.20)]
        c = [_servico(404, 0.10, 0.56)]
        entrada_ids = _ids([i, b, c])
        polidos, resumo = self._polir([i, b, c], ganho_minimo_km=1000.0)
        self.assertEqual(_ids(polidos), entrada_ids)
        self.assertEqual(sorted(len(sub) for sub in polidos), [1, 1, 2])
        self.assertFalse(resumo["descartado_por_piora"])
        rota_de_c = next(sub for sub in polidos if any(s["id"] == 404 for s in sub))
        self.assertEqual({s["id"] for s in rota_de_c}, {404})  # C nunca foi tocada

    def test_invariante_cobertura_de_ids_varios_cenarios(self):
        # multiconjunto de ids da saida tem que ser IDENTICO ao da
        # entrada em varias geometrias -- inclusive as duas de cima (3+
        # rotas, rota de 1 parada) e as classicas do brief (2 rotas).
        cenarios = [
            ([[_servico(501, 0.10, -0.25), _servico(502, 0.10, 0.55)],
              [_servico(503, 0.10, -0.20)],
              [_servico(504, 0.10, 0.56)]], dict(ganho_minimo_km=1000.0)),
            ([[_servico(601, 0.10, -0.05), _servico(602, 0.10, 0.03)],
              [_servico(603, 0.10, -0.02)],
              [_servico(604, 0.10, 1.0)]], {}),
            ([[_servico(701, 0.10, 0.00), _servico(702, 0.10, 0.20)],
              [_servico(703, 0.10, 0.20), _servico(704, 0.10, 0.00)],
              [_servico(705, 0.10, 0.50)],
              [_servico(706, 0.10, 10.0)]], {}),
            ([[_servico(801, 0.10, 0.00), _servico(802, 0.10, 0.001),
               _servico(803, 0.10, 0.002), _servico(804, 0.10, 0.10)],
              [_servico(805, 0.10, 0.10), _servico(806, 0.10, 0.101)]], {}),
        ]
        for idx, (sublotes, kwargs) in enumerate(cenarios):
            with self.subTest(cenario=idx):
                entrada_ids = _ids(sublotes)
                polidos, _ = self._polir(sublotes, **kwargs)
                self.assertEqual(_ids(polidos), entrada_ids)

    def test_nao_estoura_orcamento_de_horas(self):
        # rota B ja tem 4 paradas nivel 3 (1h15 de atendimento cada) no
        # MESMO ponto -- deslocamento zero entre elas, so a perna da
        # base conta (~3,15h). Total antes: ~3,15 + 4*1,25 = ~8,15h,
        # dentro do orcamento de 9h. Rota A tem 1 parada IDENTICA (mesma
        # coordenada) -- mover ela pra B custa 0 km extra (maximamente
        # atraente), mas a 5a parada nivel 3 estoura o orcamento
        # (~3,15 + 5*1,25 = ~9,4h > 9h). Viola SO por horas -- caixas,
        # tamanho e distancia par-a-par continuam OK.
        ponto = (0.0, 0.50)
        b = [_servico(20 + i, *ponto, nivel=3) for i in range(4)]
        a = [_servico(30, *ponto, nivel=3)]
        entrada_ids = _ids([a, b])
        polidos, resumo = self._polir([a, b])
        self.assertEqual(_ids(polidos), entrada_ids)
        self.assertEqual(sorted(len(sub) for sub in polidos), [1, 4])
        rota_de_30 = next(sub for sub in polidos if any(s["id"] == 30 for s in sub))
        self.assertEqual({s["id"] for s in rota_de_30}, {30})
        self.assertEqual(resumo["realocacoes"], 0)

    def test_nao_estoura_janela_de_horario(self):
        # D (rota B, nivel 3) e X (rota A) ficam quase colados (~1,1 km
        # de distancia entre si -- movimento maximamente atraente em
        # km, senao o teste nao prova nada) mas cada um com janela
        # apertada: D fecha 09:12, X fecha 09:18. So existem 2
        # sequencias possiveis pra rota fundida -- [D,X] ou [X,D] -- e
        # as DUAS violam (D primeiro atrasa X em ~1h12; X primeiro
        # atrasa D em ~20min), entao nao ha ordenacao que o
        # sequenciador (ordenar_com_janelas, que TENTA respeitar janela)
        # consiga achar -- so assim a trava de janela do polimento fica
        # exercitada de verdade (sozinho, direto da base, nenhum dos
        # dois estoura a propria janela -- nao e atraso intrinseco).
        d = _servico(10, 0.0, 0.50, nivel=3)
        x = _servico(11, 0.0, 0.51, nivel=1)
        d["_janela_inicio"], d["_janela_fim"] = "06:00", "09:12"
        x["_janela_inicio"], x["_janela_fim"] = "06:00", "09:18"
        a = [x]
        b = [d]
        entrada_ids = _ids([a, b])
        polidos, resumo = self._polir([a, b])
        self.assertEqual(_ids(polidos), entrada_ids)
        self.assertEqual(sorted(len(sub) for sub in polidos), [1, 1])
        rota_de_x = next(sub for sub in polidos if any(s["id"] == 11 for s in sub))
        self.assertEqual({s["id"] for s in rota_de_x}, {11})
        self.assertEqual(resumo["realocacoes"], 0)

    def test_veiculo_grande_fica_intocada(self):
        # pedido gigante sozinho (200 caixas, 1 endereco) cai na faixa
        # do VAN_HR (150-400 cx, ate 4 enderecos -- regras/
        # tipo_veiculo.py) -- classificar_tipo_veiculo devolve
        # nao-None, _rota_polivel exclui a rota, entao ela nunca entra
        # no laco de realocar/trocar/esvaziar, mesmo geometricamente
        # colada na vizinha (movimento seria maximamente atraente em km).
        grande = [_servico(90, 0.10, 0.10, caixas=200)]
        vizinha = [_servico(91, 0.10, 0.1001), _servico(92, 0.10, 0.1002)]
        entrada_ids = _ids([grande, vizinha])
        polidos, resumo = self._polir([grande, vizinha])
        self.assertIn(grande, polidos)
        self.assertEqual(_ids(polidos), entrada_ids)
        self.assertEqual(resumo["realocacoes"], 0)
        self.assertEqual(resumo["trocas"], 0)
        self.assertEqual(resumo["esvaziadas"], 0)

    def test_rede_de_seguranca_km_nunca_piora(self):
        # com ganho_minimo_km gigante (forca reversao sempre que o
        # esvaziamento tenta), o resumo nunca pode reportar km_depois
        # pior que km_antes -- e o caminho normal (sem bug) nunca deveria
        # precisar descartar por piora.
        cenarios = [
            [[_servico(901, 0.10, -0.25), _servico(902, 0.10, 0.55)],
             [_servico(903, 0.10, -0.20)],
             [_servico(904, 0.10, 0.56)]],
            [[_servico(911, 0.10, 0.00), _servico(912, 0.10, 0.20)],
             [_servico(913, 0.10, 0.20), _servico(914, 0.10, 0.00)]],
        ]
        for idx, sublotes in enumerate(cenarios):
            with self.subTest(cenario=idx):
                _, resumo = self._polir(sublotes, ganho_minimo_km=1000.0)
                self.assertLessEqual(resumo["km_depois"], resumo["km_antes"] + 1e-6)
                self.assertFalse(resumo["descartado_por_piora"])


if __name__ == "__main__":
    unittest.main()
