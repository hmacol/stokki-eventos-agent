# -*- coding: utf-8 -*-
"""
test_nivel4_veiculo_grande.py

Testes da junção de pedidos nível 4 (pedido do Hugo, 09/09 -- caso real:
7 pedidos pro CD do GPA na R. Aloisio Magalhães saíam em 6 rotas):
  - grupo de nível 4 do MESMO endereço + MESMO embarcador cujo volume
    somado cabe num tipo de veículo grande sai como 1 rota só, sem teto
    de 100 caixas nem de 4 pedidos (roteirizacao_dados.
    separar_pedidos_exclusivos / _empacotar_grupo_nivel4);
  - nível 4 de embarcadores diferentes nunca divide rota, mesmo endereço
    igual (_chave_nivel4);
  - fusão pós-hoc de rotas pequenas (fundir_sublotes_pequenos) nunca
    encosta em sublote com nível 4.

Nenhum teste geocodifica nada: obter_coordenadas é substituída por uma
leitura direta de latitude/longitude dos dicts. Rodar (da raiz):
    python -m unittest roteirizacao.test_nivel4_veiculo_grande -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
from regras.tipo_veiculo import classificar_tipo_veiculo

CD_GPA = "R ALOISIO MAGALHAES, 0, ARMAZEM, SAO PAULO - SP, 05275-050, Brasil"
KHAPPY, NUU, AMAZONIKA = 48000, 40000, 18000


def _coords(servico, api_key=None):
    lat, lng = servico.get("latitude"), servico.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


def _servico(i, caixas, nivel=4, address=CD_GPA, sender=KHAPPY, lat=-23.4158, lng=-46.8039):
    return {"id": i, "code": f"#PS-{i}", "address": address, "sender_id": sender,
            "_nivel_dificuldade": nivel, "dimension_3": caixas,
            "latitude": lat, "longitude": lng}


def _separar(servicos, volume_maximo=100):
    return rd.separar_pedidos_exclusivos(servicos, volume_maximo, 15, api_key=None)


def _codigos(sublote):
    return sorted(s["code"] for s in sublote)


class GrupoNivel4VeiculoGrandeTestCase(unittest.TestCase):

    def setUp(self):
        self._patch = mock.patch.object(rd, "obter_coordenadas", side_effect=_coords)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_caso_real_khappy_sai_em_uma_rota_de_vuc(self):
        # 225 + 123 + 75 + 75 + 75 = 573 caixas, mesmo endereço/embarcador
        khappy = [_servico(38405, 225), _servico(38408, 123), _servico(38404, 75),
                  _servico(38406, 75), _servico(38407, 75)]
        prontos, demais = _separar(khappy)
        self.assertEqual(demais, [])
        self.assertEqual(len(prontos), 1)
        self.assertEqual(_codigos(prontos[0]), _codigos(khappy))
        self.assertEqual(classificar_tipo_veiculo(*rd.caixas_e_enderecos(prontos[0])).codigo, "VUC")

    def test_embarcadores_diferentes_no_mesmo_endereco_ficam_separados(self):
        khappy = [_servico(38404, 75), _servico(38406, 75), _servico(38407, 75)]
        nuu = _servico(38284, 68, sender=NUU)
        amazonika = _servico(38010, 1, sender=AMAZONIKA)
        prontos, demais = _separar(khappy + [nuu, amazonika])
        self.assertEqual(demais, [])
        # KHAPPY: 225 caixas -> 1 rota VAN/HR; NUU e AMAZONIKA: 1 rota cada
        self.assertEqual(len(prontos), 3)
        por_tamanho = sorted(prontos, key=len)
        self.assertEqual(_codigos(por_tamanho[-1]), _codigos(khappy))
        self.assertEqual({_codigos(por_tamanho[0])[0], _codigos(por_tamanho[1])[0]},
                         {"#PS-38284", "#PS-38010"})

    def test_grupo_abaixo_de_veiculo_grande_segue_regra_de_ultima_milha(self):
        # 40 + 40 + 40 = 120 caixas: não chega a VAN/HR (150) -> trava de
        # 100 caixas continua valendo (2 + 1), como sempre foi
        grupo = [_servico(1, 40), _servico(2, 40), _servico(3, 40)]
        prontos, _ = _separar(grupo)
        self.assertEqual(sorted(len(s) for s in prontos), [1, 2])

    def test_gigante_nivel4_sozinho_sem_par_continua_isolado(self):
        prontos, _ = _separar([_servico(38408, 123)])
        self.assertEqual(len(prontos), 1)
        self.assertIsNone(classificar_tipo_veiculo(*rd.caixas_e_enderecos(prontos[0])))

    def test_grupo_maior_que_qualquer_veiculo_fecha_o_maior_prefixo(self):
        # 13 x 100 = 1300 caixas: 3/4 vai até 1200, Truck começa em 1500
        grupo = [_servico(i, 100) for i in range(1, 14)]
        prontos, _ = _separar(grupo)
        self.assertEqual(sorted(len(s) for s in prontos), [1, 12])
        maior = max(prontos, key=len)
        self.assertEqual(classificar_tipo_veiculo(*rd.caixas_e_enderecos(maior)).codigo, "TRES_QUARTOS")

    def test_nivel4_nunca_junta_com_nivel_1_2_3_do_mesmo_endereco(self):
        prontos, demais = _separar([_servico(1, 200), _servico(2, 10, nivel=1)])
        self.assertEqual(len(prontos), 1)
        self.assertEqual(_codigos(prontos[0]), ["#PS-1"])
        self.assertEqual(_codigos(demais), ["#PS-2"])

    def test_agendamentos_diferentes_nao_juntam(self):
        a = _servico(1, 100)
        a["scheduled_start"] = "2026-09-10T09:00:00-03:00"
        b = _servico(2, 100)
        b["scheduled_start"] = "2026-09-11T09:00:00-03:00"
        prontos, _ = _separar([a, b])
        self.assertEqual(len(prontos), 2)


class FusaoRespeitaNivel4TestCase(unittest.TestCase):

    def setUp(self):
        self._patch = mock.patch.object(rd, "obter_coordenadas", side_effect=_coords)
        self._patch.start()
        rd.COORDS_BASE = None

    def tearDown(self):
        self._patch.stop()

    def test_nivel4_pequeno_nao_e_fundido_com_ninguem(self):
        # caso real de 09/09: nível 4 de 1 caixa (AMAZONIKA) foi parar na
        # rota do nível 4 de 75 caixas (KHAPPY) pela fusão pós-hoc
        amazonika = [_servico(38010, 1, sender=AMAZONIKA)]
        khappy = [_servico(38404, 75)]
        comum = [_servico(i, 1, nivel=1, address=f"P{i}", sender=1, lat=-23.41 + i * 0.001, lng=-46.80)
                 for i in range(1, 4)]
        resultado = rd.fundir_sublotes_pequenos(
            [amazonika, khappy, comum], tamanho_minimo=10, tamanho_maximo=16, volume_maximo=100,
            distancia_maxima_km=15,
        )
        self.assertEqual(sorted(_codigos(s) for s in resultado),
                         sorted([_codigos(amazonika), _codigos(khappy), _codigos(comum)]))

    def test_rota_comum_pequena_nao_entra_em_rota_de_nivel4(self):
        khappy = [_servico(38404, 75)]
        pequena = [_servico(9, 1, nivel=1, address="P9", sender=1)]
        resultado = rd.fundir_sublotes_pequenos(
            [pequena, khappy], tamanho_minimo=10, tamanho_maximo=16, volume_maximo=100,
            distancia_maxima_km=15,
        )
        self.assertEqual(len(resultado), 2)

    def test_rotas_comuns_pequenas_continuam_fundindo(self):
        a = [_servico(1, 1, nivel=1, address="P1", sender=1)]
        b = [_servico(2, 1, nivel=1, address="P2", sender=1)]
        resultado = rd.fundir_sublotes_pequenos(
            [a, b], tamanho_minimo=10, tamanho_maximo=16, volume_maximo=100, distancia_maxima_km=15,
        )
        self.assertEqual(len(resultado), 1)


if __name__ == "__main__":
    unittest.main()
