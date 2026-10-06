# -*- coding: utf-8 -*-
"""
test_incrementar_rotas.py

Testes dos padrões do incremento de rotas (pedido do Hugo, 10/09):
  - corte por pedido (19h até 05/10, agora 22h): só entra pedido que
    chegou na VUUPT até as 22h do último dia útil anterior à data alvo
    (incrementar_rotas.limite_corte_pedidos / chegou_dentro_do_corte);
  - sem teto de pedidos por rota: _cabe_na_rota só olha caixas (rota
    comum) ou os limites do tipo de veículo grande;
  - 05/10: sobra vai pro rascunho aberto, pedido já em rascunho não é
    "novo" (main() com Vuupt/banco/e-mail mockados).

Rodar (da raiz):
    python -m unittest roteirizacao.test_incrementar_rotas -v
"""
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import incrementar_rotas as inc
from criar_rotas_diarias import TZ_BRASILIA, VOLUME_MAXIMO_ROTA
from regras.tipo_veiculo import classificar_tipo_veiculo


class TestCorte22h(unittest.TestCase):

    def test_limite_dia_util_seguinte(self):
        # quinta 11/09 -> corte quarta 10/09 22:00 (Brasília; era 19h até 05/10)
        limite = inc.limite_corte_pedidos(date(2026, 9, 11))
        self.assertEqual(limite, datetime(2026, 9, 10, 22, 0, tzinfo=TZ_BRASILIA))

    def test_limite_segunda_volta_pra_sexta(self):
        # segunda 14/09 -> corte sexta 11/09 22:00 (pedido do fim de
        # semana espera o rascunho de segunda 16h)
        limite = inc.limite_corte_pedidos(date(2026, 9, 14))
        self.assertEqual(limite, datetime(2026, 9, 11, 22, 0, tzinfo=TZ_BRASILIA))

    def test_pedido_antes_do_corte_entra(self):
        s = {"id": 1, "created_at": "2026-09-10T21:59:00-03:00"}
        self.assertTrue(inc.chegou_dentro_do_corte(s, date(2026, 9, 11)))

    def test_pedido_exatamente_22h_entra(self):
        s = {"id": 1, "created_at": "2026-09-10T22:00:00-03:00"}
        self.assertTrue(inc.chegou_dentro_do_corte(s, date(2026, 9, 11)))

    def test_pedido_depois_do_corte_fica_de_fora(self):
        s = {"id": 1, "created_at": "2026-09-10T22:01:00-03:00"}
        self.assertFalse(inc.chegou_dentro_do_corte(s, date(2026, 9, 11)))

    def test_pedido_de_sabado_fica_de_fora_da_segunda(self):
        s = {"id": 1, "created_at": "2026-09-12T10:00:00-03:00"}
        self.assertFalse(inc.chegou_dentro_do_corte(s, date(2026, 9, 14)))

    def test_pedido_de_sexta_antes_das_22h_entra_na_segunda(self):
        s = {"id": 1, "created_at": "2026-09-11T17:30:00-03:00"}
        self.assertTrue(inc.chegou_dentro_do_corte(s, date(2026, 9, 14)))

    def test_formato_sem_fuso_e_utc(self):
        # Formato real da API (confirmado 10/09): 'AAAA-MM-DD HH:MM:SS'
        # sem fuso, em UTC. 22:56 UTC = 19:56 Brasília (caso real
        # PS-38969) -> entra com o corte de 22h; 01:30 UTC do dia 11 =
        # 22:30 Brasília -> sai.
        entra = {"id": 1, "created_at": "2026-09-10 22:56:20"}
        sai = {"id": 2, "created_at": "2026-09-11 01:30:00"}
        self.assertTrue(inc.chegou_dentro_do_corte(entra, date(2026, 9, 11)))
        self.assertFalse(inc.chegou_dentro_do_corte(sai, date(2026, 9, 11)))

    def test_19h16_utc_e_16h16_brasilia_entra(self):
        # Caso real PS-38515: '2026-09-10 19:16:52' (UTC) = 16:16 Brasília
        s = {"id": 1, "created_at": "2026-09-10 19:16:52"}
        self.assertTrue(inc.chegou_dentro_do_corte(s, date(2026, 9, 11)))

    def test_formato_utc_e_convertido(self):
        # 00:30Z = 21:30 Brasília -> entra; 01:30Z = 22:30 -> sai
        entra = {"id": 1, "created_at": "2026-09-11T00:30:00Z"}
        sai = {"id": 2, "created_at": "2026-09-11T01:30:00Z"}
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


class TestReceptoraDeRotaFracaNoIncremento(unittest.TestCase):
    """Rota com 101-110 caixas pode ser receptora de rota fraca rodando de
    Fiorino (Hugo, 03/10): o incremento nao pode le-la como VAN/HR e deixar
    crescer ate 400 caixas."""

    def test_101_a_110_com_fiorino_ou_sem_motorista_e_fiorino_com_teto_110(self):
        for tipo_motorista in ("FIORINO", None):
            tipo, teto = inc._veiculo_da_rota_existente(105, 3, tipo_motorista)
            self.assertIsNone(tipo)
            self.assertEqual(teto, 110)
            rota = {"qtd": 4, "caixas": 105, "enderecos": {"a", "b", "c"}, "tipo_veiculo": tipo, "teto_caixas": teto}
            self.assertTrue(inc._cabe_na_rota(rota, 5, "d"))
            self.assertFalse(inc._cabe_na_rota(rota, 6, "d"))

    def test_101_a_110_com_van_hr_continua_van_hr(self):
        tipo, _ = inc._veiculo_da_rota_existente(105, 3, "VAN_HR")
        self.assertEqual(tipo.codigo, "VAN_HR")

    def test_acima_de_110_e_rota_comum_nao_mudam(self):
        self.assertEqual(inc._veiculo_da_rota_existente(150, 1, "FIORINO")[0].codigo, "VAN_HR")
        self.assertEqual(inc._veiculo_da_rota_existente(80, 3, "FIORINO"), (None, VOLUME_MAXIMO_ROTA))


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


def _servico(sid):
    return {"id": sid, "code": f"#PS-{sid}", "address": f"Rua {sid}, São Paulo - SP",
            "latitude": -23.5, "longitude": -46.6, "dimension_3": 1, "sender_id": 1}


class TestMainComRascunho(unittest.TestCase):
    """main() de ponta a ponta com tudo que sai do processo mockado."""

    def _rodar(self, rotas, rascunhos, modo_teste=False, res_rascunho=None):
        pool = [_servico(1), _servico(2)]
        modulos_falsos = {
            "pedidos_segurados": mock.Mock(separar_segurados=lambda s, d: (s, [])),
            "dedicados": mock.Mock(separar_dedicados=lambda s: (s, [])),
            "fora_dia_fixo": mock.Mock(),
        }
        catalogo = mock.Mock(motoristas=[])
        self.inc_rascunho = mock.Mock(return_value=res_rascunho or {"alocados": [{"codigo": "#PS-1"}], "orfaos": []})
        self.notificar = mock.Mock()
        self.adicionar = mock.Mock()
        patches = [
            mock.patch.dict(sys.modules, modulos_falsos),
            mock.patch.object(inc, "_carregar_config", return_value={}),
            mock.patch.object(inc.CatalogoMotoristas, "carregar", return_value=catalogo),
            mock.patch.object(inc, "carregar_tipos_carga_por_sender", return_value={}),
            mock.patch.object(inc, "VuuptClient"),
            mock.patch.object(inc, "_data_alvo_rotas", return_value=date(2026, 10, 6)),
            mock.patch.object(inc, "listar_pool_not_assigned", return_value=pool),
            mock.patch.object(inc, "listar_rotas", return_value=rotas),
            mock.patch.object(inc, "aplicar_regioes_dia_fixo", return_value=[]),
            mock.patch.object(inc, "identificar_area_nao_atendida", return_value=[]),
            mock.patch.object(inc, "carregar_clientes_agendamento", return_value=set()),
            mock.patch.object(inc, "identificar_pendentes", return_value=[]),
            mock.patch.object(inc, "geocodificar", return_value=None),
            mock.patch.object(inc, "macro_regiao_do_servico", return_value="GRANDE_SP"),
            mock.patch.object(inc, "classificar_rota_viagem", return_value=False),
            mock.patch.object(inc, "classificar_zona", return_value=None),
            mock.patch.object(inc, "adicionar_atividades", self.adicionar),
            mock.patch.object(inc, "agendar_documentacao_varias"),
            mock.patch.object(inc, "aguardar_documentacao"),
            mock.patch.object(inc, "notificar_execucao", self.notificar),
            mock.patch.object(inc.rascunhos_rota, "listar_rascunhos_do_dia", return_value=rascunhos),
            mock.patch("mapa_util.carregar_remetentes_por_sender_id", return_value={}),
            mock.patch("planejamento_rotas.incrementar_rascunhos_com_selecionados", self.inc_rascunho),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        inc.main(modo_teste=modo_teste)
        return self.notificar.call_args[0][0]["Incremento de rotas"]

    def test_sem_rota_enviada_vai_pro_rascunho_e_pula_quem_ja_esta_nele(self):
        rascunhos = [{"id": 7, "status": "RASCUNHO", "paradas": [{"service_id": 2}]}]
        resumo = self._rodar([], rascunhos)
        self.assertEqual(resumo["status"], "ok")
        paradas = self.inc_rascunho.call_args[0][1]
        self.assertEqual([p["service_id"] for p in paradas], [1])
        self.assertFalse(self.inc_rascunho.call_args[1]["modo_teste"])
        self.adicionar.assert_not_called()
        self.assertIn("1 em rascunho", resumo["detalhe"])

    def test_modo_teste_repassa_pro_rascunho(self):
        self._rodar([], [{"id": 7, "status": "RASCUNHO", "paradas": []}], modo_teste=True)
        self.assertTrue(self.inc_rascunho.call_args[1]["modo_teste"])

    def test_sem_rota_e_sem_rascunho_aberto_e_erro(self):
        resumo = self._rodar([], [{"id": 7, "status": "ENVIADO", "paradas": []}])
        self.assertEqual(resumo["status"], "erro")
        self.inc_rascunho.assert_not_called()


if __name__ == "__main__":
    unittest.main()
