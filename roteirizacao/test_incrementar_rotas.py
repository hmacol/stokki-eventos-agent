# -*- coding: utf-8 -*-
"""
test_incrementar_rotas.py

Testes dos padrões do incremento de rotas (pedido do Hugo, 10/09):
  - corte de 19h por pedido: só entra pedido que chegou na VUUPT até
    as 19h do último dia útil anterior à data alvo
    (incrementar_rotas.limite_corte_pedidos / chegou_dentro_do_corte);
  - sem teto de pedidos por rota: _cabe_na_rota só olha caixas (rota
    comum) ou os limites do tipo de veículo grande.

Rodar (da raiz):
    python -m unittest roteirizacao.test_incrementar_rotas -v
"""
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import incrementar_rotas as inc
from criar_rotas_diarias import TZ_BRASILIA, VOLUME_MAXIMO_ROTA
from regras.tipo_veiculo import classificar_tipo_veiculo


class TestCorte19h(unittest.TestCase):

    def test_limite_dia_util_seguinte(self):
        # quinta 11/09 -> corte quarta 10/09 19:00 (Brasília)
        limite = inc.limite_corte_pedidos(date(2026, 9, 11))
        self.assertEqual(limite, datetime(2026, 9, 10, 19, 0, tzinfo=TZ_BRASILIA))

    def test_limite_segunda_volta_pra_sexta(self):
        # segunda 14/09 -> corte sexta 11/09 19:00 (pedido do fim de
        # semana espera o rascunho de segunda 18h)
        limite = inc.limite_corte_pedidos(date(2026, 9, 14))
        self.assertEqual(limite, datetime(2026, 9, 11, 19, 0, tzinfo=TZ_BRASILIA))

    def test_pedido_antes_do_corte_entra(self):
        s = {"id": 1, "created_at": "2026-09-10T18:59:00-03:00"}
        self.assertTrue(inc.chegou_dentro_do_corte(s, date(2026, 9, 11)))

    def test_pedido_exatamente_19h_entra(self):
        s = {"id": 1, "created_at": "2026-09-10T19:00:00-03:00"}
        self.assertTrue(inc.chegou_dentro_do_corte(s, date(2026, 9, 11)))

    def test_pedido_depois_do_corte_fica_de_fora(self):
        s = {"id": 1, "created_at": "2026-09-10T19:01:00-03:00"}
        self.assertFalse(inc.chegou_dentro_do_corte(s, date(2026, 9, 11)))

    def test_pedido_de_sabado_fica_de_fora_da_segunda(self):
        s = {"id": 1, "created_at": "2026-09-12T10:00:00-03:00"}
        self.assertFalse(inc.chegou_dentro_do_corte(s, date(2026, 9, 14)))

    def test_pedido_de_sexta_antes_das_19h_entra_na_segunda(self):
        s = {"id": 1, "created_at": "2026-09-11T17:30:00-03:00"}
        self.assertTrue(inc.chegou_dentro_do_corte(s, date(2026, 9, 14)))

    def test_formato_sem_fuso_e_utc(self):
        # Formato real da API (confirmado 10/09): 'AAAA-MM-DD HH:MM:SS'
        # sem fuso, em UTC. 21:30 UTC = 18:30 Brasília -> entra;
        # 22:56 UTC = 19:56 Brasília (caso real PS-38969) -> sai.
        entra = {"id": 1, "created_at": "2026-09-10 21:30:00"}
        sai = {"id": 2, "created_at": "2026-09-10 22:56:20"}
        self.assertTrue(inc.chegou_dentro_do_corte(entra, date(2026, 9, 11)))
        self.assertFalse(inc.chegou_dentro_do_corte(sai, date(2026, 9, 11)))

    def test_19h16_utc_e_16h16_brasilia_entra(self):
        # Caso real PS-38515: '2026-09-10 19:16:52' (UTC) = 16:16 Brasília
        s = {"id": 1, "created_at": "2026-09-10 19:16:52"}
        self.assertTrue(inc.chegou_dentro_do_corte(s, date(2026, 9, 11)))

    def test_formato_utc_e_convertido(self):
        # 21:30Z = 18:30 Brasília -> entra; 22:30Z = 19:30 -> sai
        entra = {"id": 1, "created_at": "2026-09-10T21:30:00Z"}
        sai = {"id": 2, "created_at": "2026-09-10T22:30:00Z"}
        self.assertTrue(inc.chegou_dentro_do_corte(entra, date(2026, 9, 11)))
        self.assertFalse(inc.chegou_dentro_do_corte(sai, date(2026, 9, 11)))

    def test_sem_created_at_nao_barra(self):
        self.assertTrue(inc.chegou_dentro_do_corte({"id": 1}, date(2026, 9, 11)))
        self.assertTrue(inc.chegou_dentro_do_corte({"id": 1, "created_at": "lixo"}, date(2026, 9, 11)))

    def test_rodada_a_tarde_corte_no_futuro_nao_barra_ninguem(self):
        # quarta 15h mirando quinta: corte = quarta 19h, ainda no futuro
        # -- qualquer pedido já existente chegou antes dele.
        agora = datetime(2026, 9, 10, 15, 0, tzinfo=TZ_BRASILIA)
        s = {"id": 1, "created_at": agora.isoformat()}
        self.assertTrue(inc.chegou_dentro_do_corte(s, date(2026, 9, 11)))


def _rota_comum(qtd, caixas):
    return {"qtd": qtd, "caixas": caixas, "enderecos": {f"end{i}" for i in range(qtd)},
            "tipo_veiculo": None}


class TestSemTetoDePedidos(unittest.TestCase):

    def test_rota_comum_com_muitos_pedidos_ainda_cabe(self):
        # 30 pedidos e só 40 caixas: antes barrava por qtd >= 16, agora cabe
        self.assertTrue(inc._cabe_na_rota(_rota_comum(30, 40), 1, "novo"))

    def test_rota_comum_respeita_teto_de_caixas(self):
        self.assertTrue(inc._cabe_na_rota(_rota_comum(5, VOLUME_MAXIMO_ROTA - 2), 2, "novo"))
        self.assertFalse(inc._cabe_na_rota(_rota_comum(5, VOLUME_MAXIMO_ROTA - 2), 3, "novo"))

    def test_rota_veiculo_grande_segue_limites_do_tipo(self):
        tipo = classificar_tipo_veiculo(150, 1)
        self.assertIsNotNone(tipo, "150 caixas num endereço deveria classificar como veículo grande")
        rota = {"qtd": 8, "caixas": 150, "enderecos": {"cd"}, "tipo_veiculo": tipo}
        # mesmo endereço, dentro do volume do tipo: cabe
        self.assertTrue(inc._cabe_na_rota(rota, 1, "cd"))
        # estoura o volume do tipo: não cabe
        self.assertFalse(inc._cabe_na_rota(rota, tipo.volume_maximo_cx, "cd"))


if __name__ == "__main__":
    unittest.main()
