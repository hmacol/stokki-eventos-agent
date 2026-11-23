# -*- coding: utf-8 -*-
"""
Reagendamento pela equipe fora do dia de visita (Hugo, 03/10 -- spec dias
fixos v2, 5.4): a tela pergunta "Marcar como dedicado?" antes de salvar.
Testa a função que acha os pedidos fora do dia (com o valor da calculadora)
e a rota /api/planejamento/checar-dia-fixo (permissão igual à do reagendar).
Rodar (da raiz): py -3.11 -m unittest painel_agentes.test_reagendar_fora_dia_fixo -v
"""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import planejamento_rotas  # noqa: E402
import fora_dia_fixo  # noqa: E402  (roteirizacao/ entra no sys.path pelo planejamento_rotas)

# "painel_agentes" e o nome da pasta: o modulo do app precisa ser carregado pelo caminho
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)

CAMPINAS = "Rua Barão de Jaguara 900, Centro, Campinas - SP, 13015-001, Brasil"   # só quarta
SAO_PAULO = "Rua Augusta 100, Consolação, São Paulo - SP, 01304-000, Brasil"
QUINTA, QUARTA = "2026-10-08", "2026-10-07"
SERVICOS = {
    1: {"id": 1, "code": "#PS-1001", "address": CAMPINAS, "sender_id": 7, "dimension_3": 5,
        "latitude": -22.905, "longitude": -47.060},
    2: {"id": 2, "code": "#PS-1002", "address": SAO_PAULO, "sender_id": 7, "dimension_3": 5,
        "latitude": -23.55, "longitude": -46.65},
}
CONFIG_PAINEL = {
    "usuario": "u_total", "senha": "s_total",
    "usuario_operador": "u_op", "senha_operador": "s_op",
    "usuario_leitura": "u_le", "senha_leitura": "s_le",
}


class ChecarReagendamento(unittest.TestCase):
    def setUp(self):
        for alvo, obj, kw in (("_carregar_config", planejamento_rotas, {"return_value": {"vuupt_api": {"token": "t"}}}),
                              ("carregar_tipos_carga", fora_dia_fixo, {"return_value": {}}),
                              ("calcular_valor", fora_dia_fixo, {"return_value": 784.09})):
            p = mock.patch.object(obj, alvo, **kw)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(planejamento_rotas, "VuuptClient")
        self.vuupt = p.start().return_value
        self.addCleanup(p.stop)
        self.vuupt.buscar_servico_por_id.side_effect = lambda sid: SERVICOS.get(sid)

    def test_devolve_so_os_fora_do_dia_com_valor(self):
        r = planejamento_rotas.checar_reagendamento_dia_fixo([1, 2], QUINTA)
        self.assertEqual(r, {"ok": True, "fora_do_dia": [
            {"service_id": 1, "codigo": "PS-1001", "regiao": "Campinas", "dias": "Quartas", "valor": 784.09}]})

    def test_data_de_visita_nao_devolve_nada(self):
        self.assertEqual(planejamento_rotas.checar_reagendamento_dia_fixo([1, 2], QUARTA)["fora_do_dia"], [])

    def test_falha_da_calculadora_devolve_valor_nulo(self):
        with mock.patch.object(fora_dia_fixo, "calcular_valor", return_value=None):
            self.assertIsNone(planejamento_rotas.checar_reagendamento_dia_fixo([1], QUINTA)["fora_do_dia"][0]["valor"])
        with mock.patch.object(fora_dia_fixo, "calcular_valor", side_effect=RuntimeError("tabela")):
            self.assertIsNone(planejamento_rotas.checar_reagendamento_dia_fixo([1], QUINTA)["fora_do_dia"][0]["valor"])

    def test_pedido_que_nao_carrega_fica_de_fora(self):
        self.vuupt.buscar_servico_por_id.side_effect = lambda sid: (_ for _ in ()).throw(RuntimeError("timeout")) \
            if sid == 1 else SERVICOS.get(sid)
        self.assertEqual(planejamento_rotas.checar_reagendamento_dia_fixo([1, 2, 99], QUINTA)["fora_do_dia"], [])

    def test_data_invalida(self):
        r = planejamento_rotas.checar_reagendamento_dia_fixo([1], "08/10")
        self.assertFalse(r["ok"])
        self.vuupt.buscar_servico_por_id.assert_not_called()


class RotaChecarDiaFixo(unittest.TestCase):
    URL = "/api/planejamento/checar-dia-fixo"
    ORIGEM = {"Origin": "http://localhost"}

    def setUp(self):
        config = {**painel_agentes._carregar_config(), "painel_agentes": CONFIG_PAINEL}
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value=config)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(painel_agentes, "checar_reagendamento_dia_fixo",
                              return_value={"ok": True, "fora_do_dia": [{"service_id": 1, "valor": None}]})
        self.checar = p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"

    def _post(self, corpo=None, headers=ORIGEM):
        return self.cliente.post(self.URL, json=corpo or {"service_ids": [1, "2"], "data": QUINTA}, headers=headers)

    def test_operador_e_total_recebem_a_lista(self):
        for nivel in ("operador", "total"):
            self._logar(nivel)
            r = self._post()
            self.assertEqual(r.status_code, 200, nivel)
            self.assertEqual(r.get_json()["fora_do_dia"], [{"service_id": 1, "valor": None}])
        self.checar.assert_called_with([1, 2], QUINTA)

    def test_leitura_nao_pode(self):
        self._logar("leitura")
        self.assertEqual(self._post().status_code, 403)
        self.checar.assert_not_called()

    def test_sem_sessao_401(self):
        self.assertEqual(self._post().status_code, 401)

    def test_sem_origem_bloqueia(self):
        self._logar("operador")
        self.assertEqual(self._post(headers={}).status_code, 403)

    def test_erro_de_entrada_400(self):
        self._logar("operador")
        self.assertEqual(self._post({"service_ids": ["x"], "data": QUINTA}).status_code, 400)
        self.checar.return_value = {"ok": False, "erro": "Data inválida: 08/10"}
        self.assertEqual(self._post({"service_ids": [1], "data": "08/10"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
