# -*- coding: utf-8 -*-
"""planejar_sublotes (18/09): miolo unico do criador de rotas (particao
-> selecao de modelo -> fusao -> polimento), usado pelo job das 22h e
pelo botao Roteirizar. Testa cobertura, particao unica, chave de
polimento e o repasse de registrar_historico.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_planejar_sublotes -v"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
import otimizacao_rotas as ot
import selecao_modelo as sm
import criar_rotas_diarias as crd

BASE = (-23.55, -46.63)


def _coords(s):
    lat, lng = s.get("latitude"), s.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


def _servico(i, dlat, dlng, tipo="Seco"):
    return {"id": i, "code": f"PS-{i}", "address": f"Rua {i}, Sao Paulo - SP, 01000-000, Brasil",
            "_nivel_dificuldade": 1, "_tipo_carga": tipo, "latitude": BASE[0] + dlat,
            "longitude": BASE[1] + dlng, "dimension_3": 1, "sender_id": 1}


class PlanejarSublotesTestCase(unittest.TestCase):

    def setUp(self):
        self._patches = [
            mock.patch.object(rd, "obter_coordenadas", lambda s, k=None: _coords(s)),
            mock.patch.object(ot, "obter_coordenadas", lambda s, k=None: _coords(s)),
            mock.patch.object(sm, "_registrar_historico"),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        rd.COORDS_BASE = BASE
        self.servicos = ([_servico(i, 0.01 + 0.001 * i, 0.0, "Seco") for i in range(8)]
                         + [_servico(20 + i, 0.01 + 0.001 * i, 0.12, "Refrigerado") for i in range(8)])

    def test_particao_unica_e_cobertura(self):
        planos = crd.planejar_sublotes(self.servicos, BASE, None, date(2026, 9, 18))
        self.assertEqual([p["label"] for p in planos], [crd.PARTICAO_GERAL])
        ids = sorted(s["id"] for p in planos for sub in p["sublotes"] for s in sub)
        self.assertEqual(ids, sorted(s["id"] for s in self.servicos))
        self.assertIn(planos[0]["modelo"], ("Atual (Grade+Greedy)", "Sweep Polar", "Clarke-Wright", "CEP real", "K-means geográfico"))

    def test_polimento_desligado_tambem_cobre_tudo(self):
        with mock.patch.object(crd, "POLIMENTO_ATIVO", False), \
             mock.patch.object(crd, "polir_entre_rotas", side_effect=AssertionError("nao deveria polir")):
            planos = crd.planejar_sublotes(self.servicos, BASE, None, date(2026, 9, 18))
        ids = sorted(s["id"] for p in planos for sub in p["sublotes"] for s in sub)
        self.assertEqual(ids, sorted(s["id"] for s in self.servicos))

    def test_repassa_registrar_historico(self):
        crd.planejar_sublotes(self.servicos, BASE, None, date(2026, 9, 18), registrar_historico=False)
        sm._registrar_historico.assert_not_called()
        crd.planejar_sublotes(self.servicos, BASE, None, date(2026, 9, 18))
        sm._registrar_historico.assert_called()

    def test_sem_base_usa_fluxo_de_reserva(self):
        planos = crd.planejar_sublotes(self.servicos, None, None, date(2026, 9, 18))
        self.assertEqual(planos[0]["modelo"], "Atual (Grade+Greedy)")
        ids = sorted(s["id"] for p in planos for sub in p["sublotes"] for s in sub)
        self.assertEqual(ids, sorted(s["id"] for s in self.servicos))

    # Fix round 1 (20/09, achado do controlador): a ORDEM importa -- a fusao
    # de sublotes pequenos tem que rodar ANTES do polimento entre rotas. Se
    # a ordem fosse invertida (ou o polimento rodasse 2x), a fusao desfaria
    # o trabalho do polimento (junta sublotes pequenos de novo, embaralhando
    # o que o polimento acabou de arrumar) e o sintoma -- rotas pequenas
    # reaparecendo -- seria dificil de rastrear meses depois, sem nenhum
    # teste do pacote quebrando. Mocka as 3 etapas internas (selecao de
    # modelo, fusao, polimento) e registra a ordem em que cada uma roda.
    # O polimento e mockado no nivel de `polir_entre_rotas` (nao de
    # `_polir_particao`) pra manter real o `if not POLIMENTO_ATIVO: return`
    # que mora dentro de `_polir_particao` -- e o mesmo ponto que o teste
    # `test_polimento_desligado_tambem_cobre_tudo` ja usa.
    def test_ordem_selecao_fusao_polimento_uma_vez_cada(self):
        chamadas = []

        def fake_escolher(*args, **kwargs):
            chamadas.append("selecao")
            return ("Modelo Fake", [[self.servicos[0]], [self.servicos[1]]])

        def fake_fundir(sublotes, coords_base, gmaps_key, label):
            chamadas.append("fusao")
            return sublotes

        def fake_polir(sublotes, base_lat, base_lng, api_key, **kwargs):
            chamadas.append("polimento")
            resumo = {"realocacoes": 0, "trocas": 0, "esvaziadas": 0,
                      "km_antes": 0.0, "km_depois": 0.0, "tempo_s": 0.0, "estourou_tempo": False}
            return sublotes, resumo

        with mock.patch.object(crd, "escolher_melhor_modelo", side_effect=fake_escolher), \
             mock.patch.object(crd, "_fundir_sublotes_entre_macrorregioes", side_effect=fake_fundir), \
             mock.patch.object(crd, "polir_entre_rotas", side_effect=fake_polir):
            crd.planejar_sublotes(self.servicos, BASE, None, date(2026, 9, 18))

        # ordem exata (implica tambem 1 chamada por etapa: se alguma
        # rodasse 2x ou fora de ordem, a lista nao bateria com esta).
        self.assertEqual(chamadas, ["selecao", "fusao", "polimento"])

    def test_ordem_sem_polimento_nao_aparece_na_sequencia_quando_desligado(self):
        chamadas = []

        def fake_escolher(*args, **kwargs):
            chamadas.append("selecao")
            return ("Modelo Fake", [[self.servicos[0]], [self.servicos[1]]])

        def fake_fundir(sublotes, coords_base, gmaps_key, label):
            chamadas.append("fusao")
            return sublotes

        def fake_polir(*args, **kwargs):
            chamadas.append("polimento")
            return args[0], {}

        with mock.patch.object(crd, "POLIMENTO_ATIVO", False), \
             mock.patch.object(crd, "escolher_melhor_modelo", side_effect=fake_escolher), \
             mock.patch.object(crd, "_fundir_sublotes_entre_macrorregioes", side_effect=fake_fundir), \
             mock.patch.object(crd, "polir_entre_rotas", side_effect=fake_polir):
            crd.planejar_sublotes(self.servicos, BASE, None, date(2026, 9, 18))

        # com o polimento desligado, a etapa nao roda -- "polimento" nunca
        # entra na lista de chamadas (prova pela SEQUENCIA, nao so pela
        # cobertura de ids, que test_polimento_desligado_tambem_cobre_tudo
        # ja cobre).
        self.assertEqual(chamadas, ["selecao", "fusao"])


if __name__ == "__main__":
    unittest.main()
