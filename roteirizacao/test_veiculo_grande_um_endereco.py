# -*- coding: utf-8 -*-
"""
test_veiculo_grande_um_endereco.py

Regra nova de 22/09/2026 (Hugo): veiculo grande e acionado SO por
endereco -- quando os pedidos de um MESMO endereco somam mais que o teto
da rota comum (100 caixas). O crescimento guloso por enderecos vizinhos
(ate 4 enderecos, 2 no Truck) que existia antes saiu.

Caso que motivou a mudanca: 2 pedidos de 60 caixas pro mesmo endereco
saiam em rotas DIFERENTES -- 120 estourava o teto de 100 da rota comum e
nao alcancava o minimo de 150 da VAN/HR.

Nenhum teste geocodifica: obter_coordenadas e substituida por leitura
direta de latitude/longitude dos dicts.

Rodar (da raiz):
    py -3.11 -m unittest roteirizacao.test_veiculo_grande_um_endereco -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

import roteirizacao_dados as rd  # noqa: E402


def _pedido(codigo, endereco, caixas, lat=-23.5, lng=-46.6, nivel=1):
    return {
        "code": codigo,
        "address": endereco,
        "dimension_3": caixas,
        "latitude": lat,
        "longitude": lng,
        "difficulty_level": nivel,
    }


def _coords(servico, api_key=None):
    lat, lng = servico.get("latitude"), servico.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


class BaseSemGeocodificar(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(rd, "obter_coordenadas", _coords)
        patcher.start()
        self.addCleanup(patcher.stop)

    def separar(self, pedidos):
        return rd.separar_pedidos_exclusivos(
            pedidos, volume_maximo=100, distancia_maxima_km=15, api_key=None,
        )


class TestUmEnderecoAcimaDoTeto(BaseSemGeocodificar):
    def test_dois_pedidos_mesmo_endereco_viram_uma_rota(self):
        # O caso que falhava ate 22/09.
        pedidos = [
            _pedido("A", "Rua X, 100", 60),
            _pedido("B", "Rua X, 100", 60),
        ]
        prontos, demais = self.separar(pedidos)
        self.assertEqual(len(prontos), 1, "os dois deviam sair na mesma rota exclusiva")
        self.assertEqual({s["code"] for s in prontos[0]}, {"A", "B"})
        self.assertEqual(demais, [])

    def test_o_tipo_classificado_e_van_hr(self):
        pedidos = [_pedido("A", "Rua X, 100", 60), _pedido("B", "Rua X, 100", 60)]
        prontos, _ = self.separar(pedidos)
        tipo = rd.classificar_tipo_veiculo(*rd.caixas_e_enderecos(prontos[0]))
        self.assertEqual(tipo.codigo, "VAN_HR")

    def test_endereco_dentro_do_teto_volta_pro_pool(self):
        pedidos = [_pedido("A", "Rua X, 100", 40), _pedido("B", "Rua X, 100", 50)]
        prontos, demais = self.separar(pedidos)
        self.assertEqual(prontos, [])
        self.assertEqual({s["code"] for s in demais}, {"A", "B"})

    def test_exatamente_no_teto_nao_aciona(self):
        pedidos = [_pedido("A", "Rua X, 100", 100)]
        prontos, demais = self.separar(pedidos)
        self.assertEqual(prontos, [])
        self.assertEqual(len(demais), 1)


class TestSemCrescimentoPorVizinhos(BaseSemGeocodificar):
    def test_enderecos_diferentes_nao_se_juntam(self):
        # Ate 22/09 estes dois viravam 1 cluster de veiculo grande.
        pedidos = [
            _pedido("A", "Rua X, 100", 80, lat=-23.50, lng=-46.60),
            _pedido("B", "Rua Y, 200", 80, lat=-23.501, lng=-46.601),
        ]
        prontos, demais = self.separar(pedidos)
        self.assertEqual(prontos, [], "enderecos diferentes nao formam mais veiculo grande")
        self.assertEqual({s["code"] for s in demais}, {"A", "B"})

    def test_um_endereco_grande_nao_arrasta_o_vizinho(self):
        pedidos = [
            _pedido("A", "Rua X, 100", 120, lat=-23.50, lng=-46.60),
            _pedido("B", "Rua Y, 200", 30, lat=-23.501, lng=-46.601),
        ]
        prontos, demais = self.separar(pedidos)
        self.assertEqual(len(prontos), 1)
        self.assertEqual({s["code"] for s in prontos[0]}, {"A"})
        self.assertEqual({s["code"] for s in demais}, {"B"})


class TestPedidoGigante(BaseSemGeocodificar):
    def test_pedido_individual_acima_do_teto_sai_isolado(self):
        pedidos = [_pedido("A", "Rua X, 100", 150)]
        prontos, demais = self.separar(pedidos)
        self.assertEqual(len(prontos), 1)
        self.assertEqual(prontos[0][0]["code"], "A")
        self.assertEqual(demais, [])


class TestAcimaDaMaiorCapacidade(BaseSemGeocodificar):
    def test_endereco_acima_do_truck_sai_exclusivo_com_alerta(self):
        # 40 pedidos de 80cx no mesmo endereco = 3200cx, acima do Truck.
        # Nao pode virar 32 rotas pequenas silenciosas.
        pedidos = [_pedido(f"P{i}", "Rua X, 100", 80) for i in range(40)]
        with self.assertLogs(rd.logger, level="WARNING") as captura:
            prontos, demais = self.separar(pedidos)
        self.assertEqual(len(prontos), 1)
        self.assertEqual(len(prontos[0]), 40)
        self.assertEqual(demais, [])
        self.assertTrue(any("ALERTA_ALOCACAO" in linha for linha in captura.output))


class TestNivel4Intocado(BaseSemGeocodificar):
    def test_nivel4_continua_agrupando_por_endereco_e_embarcador(self):
        pedidos = [
            _pedido("A", "Rua X, 100", 60, nivel=4) | {"sender_id": 1},
            _pedido("B", "Rua X, 100", 60, nivel=4) | {"sender_id": 1},
        ]
        prontos, demais = self.separar(pedidos)
        self.assertEqual(len(prontos), 1)
        self.assertEqual({s["code"] for s in prontos[0]}, {"A", "B"})
        self.assertEqual(demais, [])


if __name__ == "__main__":
    unittest.main()
