import unittest

from regras import veiculos as v


class TestVeiculos(unittest.TestCase):
    def setUp(self):
        v.limpar_cache()

    def test_generico_por_ponto(self):
        self.assertTrue(v.eh_generico(".Seco"))
        self.assertTrue(v.eh_generico("..Carro_Grande"))
        self.assertTrue(v.eh_generico(".REFRI & CONG-1&2"))
        self.assertFalse(v.eh_generico("ABC1D23"))
        self.assertFalse(v.eh_generico(None))
        self.assertTrue(v.falta_carro(10, 5, ".Seco"))
        self.assertFalse(v.falta_carro(10, 5, "ABC1D23"))

    def test_virtual_nao_falta_carro(self):
        self.assertFalse(v.falta_carro(50258, None, None))
        self.assertFalse(v.falta_carro(50259, None, ".Seco"))
        self.assertFalse(v.falta_carro(None, None, None))

    def test_sem_vehicle_falta(self):
        self.assertTrue(v.falta_carro(10, None, None))

    def test_seletor_tira_generico_e_excluido_e_poe_dono_primeiro(self):
        vs = [
            {"id": 1, "license_plate": "ZZZ9Z99"},
            {"id": 2, "license_plate": ".Seco"},
            {"id": 3, "license_plate": "AAA1A11", "deleted_at": "2026-01-01"},
            {"id": 4, "license_plate": "BBB2B22"},
            {"id": 5, "license_plate": "CCC3C33"},
        ]
        ms = [{"nome": "Joao", "vehicle_id": 5}]
        r = v.listar_para_seletor(vs, ms)
        self.assertEqual([i["vehicle_id"] for i in r], [5, 4, 1])
        self.assertEqual(r[0]["dono"], "Joao")
        self.assertIsNone(r[1]["dono"])

    def test_anotar_falta_carro_nos_rascunhos(self):
        rs = [{"agent_id": 10, "vehicle_id": 5}, {"agent_id": 10, "vehicle_id": 6},
              {"agent_id": 50259, "vehicle_id": None}, {"agent_id": None, "vehicle_id": None}]
        v.anotar_falta_carro(rs, {5: ".Seco", 6: "ABC1D23"})
        self.assertEqual([r["falta_carro"] for r in rs], [True, False, False, False])

    def test_seletor_marca_rodizio_do_dia(self):
        import sys
        from datetime import date
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "roteirizacao"))
        vs = [{"id": 1, "license_plate": "AAA1A13"}, {"id": 2, "license_plate": "BBB2B25"}]
        terca = date(2026, 11, 10)  # terca: placas finais 3 e 4
        r = {i["vehicle_id"]: i["rodizio_no_dia"] for i in v.listar_para_seletor(vs, [], terca)}
        self.assertEqual(r, {1: True, 2: False})
        domingo = date(2026, 11, 15)
        r = {i["vehicle_id"]: i["rodizio_no_dia"] for i in v.listar_para_seletor(vs, [], domingo)}
        self.assertEqual(r, {1: False, 2: False})

    def test_verificar_carro_texto_e_casos(self):
        placas = {5: ".Seco", 6: "ABC1D23"}
        txt = v.verificar_carro({"agent_id": 10, "vehicle_id": 5, "motorista_nome": "Joao"}, placas)
        self.assertEqual(txt, "Motorista Joao sem carro cadastrado: escolha o carro no card ou cadastre o veículo dele.")
        self.assertIsNone(v.verificar_carro({"agent_id": 10, "vehicle_id": 6, "motorista_nome": "J"}, placas))
        self.assertIsNone(v.verificar_carro({"agent_id": 50258, "vehicle_id": None}, placas))
        self.assertIsNone(v.verificar_carro({"agent_id": None, "vehicle_id": None}, placas))
        # lista nao carregou: so vehicle_id vazio conta
        self.assertIsNotNone(v.verificar_carro({"agent_id": 10, "vehicle_id": None, "motorista_nome": "J"}, None))
        self.assertIsNone(v.verificar_carro({"agent_id": 10, "vehicle_id": 5, "motorista_nome": "J"}, None))

    def test_cache_10_minutos(self):
        chamadas = []
        relogio = [0.0]

        def carregar():
            chamadas.append(1)
            return [{"id": len(chamadas)}]

        def agora():
            return relogio[0]

        v.listar_veiculos_cacheado(carregar, agora)
        relogio[0] = 599
        v.listar_veiculos_cacheado(carregar, agora)
        self.assertEqual(len(chamadas), 1)
        relogio[0] = 601
        v.listar_veiculos_cacheado(carregar, agora)
        self.assertEqual(len(chamadas), 2)


if __name__ == "__main__":
    unittest.main()
