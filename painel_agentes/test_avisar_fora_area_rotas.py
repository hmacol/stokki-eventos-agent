# -*- coding: utf-8 -*-
"""
Rotas do botao "Avisar clientes" (planejamento, 30/09/2026).

    py -3.11 -m unittest painel_agentes.test_avisar_fora_area_rotas
"""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import avisar_fora_area as afa  # noqa: E402

# "painel_agentes" e o nome da pasta: o modulo do app precisa ser carregado pelo caminho
if "painel_agentes_app" in sys.modules:
    pa = sys.modules["painel_agentes_app"]
else:
    _spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
    pa = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = pa  # o Flask acha a pasta templates/ pelo modulo registrado
    _spec.loader.exec_module(pa)

S1 = {"id": 1, "code": "PS-1", "sender_id": 10, "address": "Rua A, 1 - Centro, Curitiba - PR, 80000-000"}
S2 = {"id": 2, "code": "PS-2", "sender_id": 10, "address": "Rua B, 2 - Centro, Bauru - SP, 17000-000"}
TIPOS = {1: afa.TIPO_FORA_SP}


class Rotas(unittest.TestCase):
    def setUp(self):
        pa.app.config["TESTING"] = True
        self.cliente = pa.app.test_client()
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = "operador"
            sess["usuario"] = "maria"
        self.conn = afa.conectar(":memory:")
        self.conn.executescript("""
            CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, email TEXT, sender_id INTEGER);
            INSERT INTO interno VALUES ('1', 'ACME', 'ACME', 'a@acme.com', 10);""")
        self.addCleanup(self.conn.close)
        config = {**pa._carregar_config(), "whatsapp_notificacoes": {"ativo": False}, "email": {}}
        for p in (patch.object(afa, "conectar", return_value=self.conn),
                  patch.object(pa, "servicos_fora_area", return_value=([S1, S2], TIPOS)),
                  patch.object(pa, "_carregar_config", return_value=config),
                  patch.object(pa.integracao_openwa, "listar_grupos", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def _post(self, url, body):
        return self.cliente.post(url, json=body, headers={"Origin": "http://localhost"})

    def test_previa_filtra_ids_e_ignora_quem_nao_esta_fora(self):
        resp = self._post("/api/planejamento/avisar-fora-area/previa", {"service_ids": [1, 2, 7]})
        self.assertEqual(resp.status_code, 200, resp.data)
        dados = resp.get_json()
        self.assertEqual([b["sender_id"] for b in dados["blocos"]], [10])
        self.assertEqual(dados["blocos"][0]["pedidos"][0]["codigo"], "PS-1")
        self.assertEqual(dados["ignorados"], [2])          # 7 nao esta no pool: nem entra
        self.assertFalse(dados["whatsapp_disponivel"])

    def test_previa_sem_ids_e_400(self):
        self.assertEqual(self._post("/api/planejamento/avisar-fora-area/previa", {}).status_code, 400)

    def test_enviar_chama_o_nucleo_com_o_usuario(self):
        with patch.object(afa, "enviar", return_value={"resultados": [], "ignorados": []}) as env:
            resp = self._post("/api/planejamento/avisar-fora-area",
                              {"itens": [{"sender_id": 10, "tipo": afa.TIPO_FORA_SP, "service_ids": [1], "canais": ["email"]}]})
        self.assertEqual(resp.status_code, 200, resp.data)
        args = env.call_args
        self.assertEqual(args.args[0][0]["sender_id"], 10)
        self.assertEqual(args.args[4], "maria")

    def test_enviar_sem_itens_e_400(self):
        self.assertEqual(self._post("/api/planejamento/avisar-fora-area", {"itens": []}).status_code, 400)

    def test_nivel_leitura_nao_acessa(self):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = "leitura"
        self.assertEqual(self._post("/api/planejamento/avisar-fora-area/previa", {"service_ids": [1]}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
