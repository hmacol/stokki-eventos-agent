import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
for _p in (_RAIZ, _RAIZ / "roteirizacao", _RAIZ / "painel_agentes"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import enviar_rascunhos_pendentes as erp


def _rascunho(nome="Rota 1", agent_id=10, vehicle_id=None, motorista="Joao"):
    return {"id": 1, "nome": nome, "agent_id": agent_id, "vehicle_id": vehicle_id,
            "motorista_nome": motorista, "paradas": [], "status": erp.STATUS_RASCUNHO, "badges": []}


def _roda_main(rascunho, modo_teste, placas):
    postados = []
    cfg = {"vuupt_api": {"token": "x"}}
    with mock.patch.object(erp, "_carregar_config", return_value=cfg), \
         mock.patch.object(erp, "listar_rascunhos_do_dia", return_value=[rascunho]), \
         mock.patch.object(erp, "_badges_trava", lambda r: []), \
         mock.patch.object(erp, "_badges_lote", lambda rs: None), \
         mock.patch.object(erp, "_placas_veiculos", return_value=placas), \
         mock.patch.object(erp, "enviar_rascunho", return_value={"ok": True, "vuupt_route_id": 9}) as env, \
         mock.patch.object(erp, "notificar_execucao"), \
         mock.patch("grupos_equipe.conectar") as conectar, \
         mock.patch("retornos.postar_alerta", side_effect=lambda c, t: postados.append(t)):
        erp.main(modo_teste=modo_teste)
    return postados, env, conectar


class TestEnvioAutomaticoSemCarro(unittest.TestCase):
    def test_envio_automatico_sem_carro_posta_alerta(self):
        postados, env, _ = _roda_main(_rascunho(vehicle_id=None), False, {})
        env.assert_called_once()  # envia mesmo assim
        self.assertEqual(postados, ["Rota Rota 1 saiu sem carro: motorista Joao sem carro cadastrado."])

    def test_envio_automatico_com_carro_nao_posta(self):
        postados, env, _ = _roda_main(_rascunho(vehicle_id=6), False, {6: "ABC1D23"})
        env.assert_called_once()
        self.assertEqual(postados, [])

    def test_envio_automatico_modo_teste_nao_posta(self):
        postados, env, conectar = _roda_main(_rascunho(vehicle_id=None), True, {})
        self.assertEqual(postados, [])
        conectar.assert_not_called()
        env.assert_not_called()


if __name__ == "__main__":
    unittest.main()
