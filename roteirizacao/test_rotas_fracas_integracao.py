# -*- coding: utf-8 -*-
"""
Rotas fracas dentro do criador de rotas: a juncao roda em
planejar_sublotes (depois do polimento) e o segurar em _aplicar_segurar.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_rotas_fracas_integracao -v
"""
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
import rotas_fracas as rf
import criar_rotas_diarias as crd

BASE = (-23.55, -46.63)
TERCA, QUARTA = date(2026, 9, 29), date(2026, 9, 30)


def _coords(s):
    lat, lng = s.get("latitude"), s.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


def _servico(i, dlat=0.01, dlng=0.0, caixas=1):
    return {"id": i, "code": f"#PS-{1000 + i}", "address": f"Rua {i}, Sao Paulo - SP, 01000-000, Brasil",
            "_nivel_dificuldade": 1, "_tipo_carga": "Seco", "latitude": BASE[0] + dlat,
            "longitude": BASE[1] + dlng, "dimension_3": caixas, "sender_id": 1,
            "created_at": "2026-09-28 15:00:00"}


class TestPlanejarSublotes(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(rd, "obter_coordenadas", lambda s, k=None: _coords(s)),
                  mock.patch.object(ot, "obter_coordenadas", lambda s, k=None: _coords(s)),
                  mock.patch.object(sm, "_registrar_historico")):
            p.start()
            self.addCleanup(p.stop)
        rd.COORDS_BASE = BASE
        self.servicos = [_servico(i, 0.01 + 0.001 * i) for i in range(12)]

    def test_plano_traz_o_relatorio_e_cobre_tudo(self):
        planos = crd.planejar_sublotes(self.servicos, BASE, None, TERCA, registrar_historico=False)
        self.assertEqual(set(planos[0]["rotas_fracas"]), {"juntadas", "motivos", "receptoras"})
        ids = sorted(s["id"] for p in planos for sub in p["sublotes"] for s in sub)
        self.assertEqual(ids, sorted(s["id"] for s in self.servicos))

    def test_chave_desligada_nao_chama_a_juncao(self):
        with mock.patch.object(rf, "ROTAS_FRACAS_ATIVO", False), \
             mock.patch.object(rf, "absorver_rotas_fracas", side_effect=AssertionError("nao deveria rodar")):
            planos = crd.planejar_sublotes(self.servicos, BASE, None, TERCA, registrar_historico=False)
        self.assertEqual(planos[0]["rotas_fracas"], {"juntadas": 0, "motivos": {}, "receptoras": set()})

    def test_falha_na_juncao_nao_derruba_o_plano(self):
        with mock.patch.object(rf, "absorver_rotas_fracas", side_effect=RuntimeError("boom")):
            planos = crd.planejar_sublotes(self.servicos, BASE, None, TERCA, registrar_historico=False)
        ids = sorted(s["id"] for p in planos for sub in p["sublotes"] for s in sub)
        self.assertEqual(ids, sorted(s["id"] for s in self.servicos))
        self.assertEqual(planos[0]["rotas_fracas"], {"juntadas": 0, "motivos": {}, "receptoras": set()})

    def test_sem_base_nao_chama_a_juncao(self):
        with mock.patch.object(rf, "absorver_rotas_fracas", side_effect=AssertionError("nao deveria rodar")):
            crd.planejar_sublotes(self.servicos, None, None, TERCA, registrar_historico=False)


class TestAplicarSegurar(unittest.TestCase):
    def setUp(self):
        self.fraca = [_servico(1, caixas=4), _servico(2, caixas=5)]
        self.normal = [_servico(10 + i, caixas=6) for i in range(10)]
        self.planos = [{"label": "Geral", "modelo": "X", "sublotes": [self.fraca, self.normal],
                        "rotas_fracas": {"juntadas": 2, "motivos": {id(self.fraca): "vizinha mais próxima a 27 km"}}}]
        self.marcar = mock.Mock(return_value=2)
        for p in (mock.patch.object(crd.pedidos_segurados, "conectar", return_value=mock.MagicMock()),
                  mock.patch.object(crd.pedidos_segurados, "codigos_segurados", return_value=set()),
                  mock.patch.object(crd.pedidos_segurados, "marcar", self.marcar),
                  mock.patch.object(rf, "motivo_nao_segurar", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def test_segurar_desligado_so_conta(self):
        with mock.patch.object(rf, "SEGURAR_ATIVO", False):
            resumo = crd._aplicar_segurar(self.planos, TERCA, None, modo_teste=False)
        self.assertEqual(self.planos[0]["sublotes"], [self.fraca, self.normal])
        self.marcar.assert_not_called()
        self.assertEqual(resumo, {"juntadas": 2, "seguradas": 0, "pedidos_segurados": 0, "data_nova": QUARTA,
                                  "sobraram": 1, "pedidos_sobraram": 2, "caixas_sobraram": 9})

    def test_segura_a_rota_inteira(self):
        with mock.patch.object(rf, "SEGURAR_ATIVO", True):
            resumo = crd._aplicar_segurar(self.planos, TERCA, None, modo_teste=False)
        self.assertEqual(self.planos[0]["sublotes"], [self.normal])
        self.assertNotIn(id(self.fraca), self.planos[0]["rotas_fracas"]["motivos"])
        itens, data_alvo, data_nova, motivo = self.marcar.call_args.args[1:5]
        self.assertEqual([s["id"] for s, _ in itens], [1, 2])
        self.assertEqual({prazo for _, prazo in itens}, {date(2026, 10, 1)})
        self.assertEqual((data_alvo, data_nova, motivo), (TERCA, QUARTA, "2 pedidos, 9 caixas"))
        self.assertEqual((resumo["seguradas"], resumo["pedidos_segurados"], resumo["sobraram"]), (1, 2, 0))

    def test_modo_teste_tira_da_lista_mas_nao_grava(self):
        with mock.patch.object(rf, "SEGURAR_ATIVO", True):
            resumo = crd._aplicar_segurar(self.planos, TERCA, None, modo_teste=True)
        self.assertEqual(self.planos[0]["sublotes"], [self.normal])
        self.marcar.assert_not_called()
        self.assertEqual(resumo["seguradas"], 1)

    def test_um_pedido_que_nao_pode_esperar_trava_a_rota(self):
        with mock.patch.object(rf, "SEGURAR_ATIVO", True), \
             mock.patch.object(rf, "motivo_nao_segurar",
                               side_effect=lambda s, d, ja, k=None: "tem agendamento" if s["id"] == 2 else None):
            resumo = crd._aplicar_segurar(self.planos, TERCA, None, modo_teste=False)
        self.assertEqual(self.planos[0]["sublotes"], [self.fraca, self.normal])
        self.marcar.assert_not_called()
        self.assertEqual(self.planos[0]["rotas_fracas"]["motivos"][id(self.fraca)],
                         "vizinha mais próxima a 27 km; PS-1002 tem agendamento")
        self.assertEqual((resumo["seguradas"], resumo["sobraram"]), (0, 1))

    def test_falha_ao_ler_segurados_nao_segura(self):
        with mock.patch.object(rf, "SEGURAR_ATIVO", True), \
             mock.patch.object(crd.pedidos_segurados, "conectar", side_effect=RuntimeError("banco travado")):
            resumo = crd._aplicar_segurar(self.planos, TERCA, None, modo_teste=False)
        self.assertEqual(self.planos[0]["sublotes"], [self.fraca, self.normal])
        self.assertEqual((resumo["seguradas"], resumo["sobraram"]), (0, 1))

    def test_falha_ao_gravar_nao_segura(self):
        self.marcar.side_effect = RuntimeError("disco cheio")
        with mock.patch.object(rf, "SEGURAR_ATIVO", True):
            resumo = crd._aplicar_segurar(self.planos, TERCA, None, modo_teste=False)
        self.assertEqual(self.planos[0]["sublotes"], [self.fraca, self.normal])
        self.assertIn("falha ao registrar o adiamento",
                      self.planos[0]["rotas_fracas"]["motivos"][id(self.fraca)])
        self.assertEqual(resumo["sobraram"], 1)

    def test_plano_sem_relatorio_passa_direto(self):
        planos = [{"label": "Geral", "modelo": "X", "sublotes": [self.normal]}]
        resumo = crd._aplicar_segurar(planos, TERCA, None, modo_teste=False)
        self.assertEqual(planos[0]["sublotes"], [self.normal])
        self.assertEqual((resumo["juntadas"], resumo["sobraram"]), (0, 0))


class TestRascunhoMarcaReceptora(unittest.TestCase):
    """roteirizar_para_rascunhos leva pro rascunho quem recebeu pedido de
    rota fraca (o painel usa o teto com folga nessas)."""

    def test_recebeu_rota_fraca_no_rascunho(self):
        receptora = [_servico(1), _servico(2)]
        comum = [_servico(3), _servico(4)]
        plano = {"label": "Geral", "modelo": "X", "sublotes": [receptora, comum],
                 "rotas_fracas": {"juntadas": 1, "motivos": {}, "receptoras": {id(receptora)}}}
        historico = mock.Mock()
        historico.parametros_alocacao.return_value = {}
        patches = dict(
            CatalogoMotoristas=mock.Mock(**{"carregar.return_value": mock.Mock(motoristas=[])}),
            carregar_ajustes_dia=mock.Mock(return_value={}), carregar_niveis=mock.Mock(return_value={}),
            carregar_horarios=mock.Mock(return_value={}), carregar_ajustes_manuais=mock.Mock(return_value={}),
            carregar_tipos_carga_por_sender=mock.Mock(return_value={}), _preparar_janelas=mock.Mock(),
            geocodificar=mock.Mock(return_value=None), planejar_sublotes=mock.Mock(return_value=[plano]),
            carregar_historico_justica=mock.Mock(return_value=historico),
            classificar_rota_viagem=mock.Mock(return_value=False), classificar_rota_zona=mock.Mock(return_value=None),
            estimar_tempo_rota=mock.Mock(return_value=1.0), selecionar_motorista_equitativo=mock.Mock(return_value=None),
        )
        with mock.patch.multiple(crd, **patches):
            rascunhos = crd.roteirizar_para_rascunhos(receptora + comum, TERCA, config={"google_maps": {}})
        self.assertEqual([r["recebeu_rota_fraca"] for r in rascunhos], [True, False])


class TestFrase(unittest.TestCase):
    def test_frase_completa(self):
        r = {"juntadas": 2, "seguradas": 1, "pedidos_segurados": 3, "data_nova": QUARTA,
             "sobraram": 1, "pedidos_sobraram": 2, "caixas_sobraram": 9}
        self.assertEqual(crd._frase_rotas_fracas(r),
                         " Rotas fracas: 2 juntada(s) em vizinhas, 1 segurada(s) para 30/09 (3 pedido(s)), "
                         "1 sem solução (2 pedido(s), 9 caixa(s)).")

    def test_sem_rota_fraca_nao_escreve_nada(self):
        r = {"juntadas": 0, "seguradas": 0, "pedidos_segurados": 0, "data_nova": QUARTA,
             "sobraram": 0, "pedidos_sobraram": 0, "caixas_sobraram": 0}
        self.assertEqual(crd._frase_rotas_fracas(r), "")


if __name__ == "__main__":
    unittest.main()
