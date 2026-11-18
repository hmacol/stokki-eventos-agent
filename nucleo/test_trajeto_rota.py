# -*- coding: utf-8 -*-
"""
test_trajeto_rota.py

Trajeto da rota pro mapa do app: cache por assinatura, pontos validos,
queda pra linha reta e as rotas novas da API do motorista. Sem rede.
    py -3.11 -m unittest nucleo.test_trajeto_rota -v
"""
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

import km_rodoviario                                    # noqa: E402
from nucleo import api_motorista, auth_motorista as auth, banco, rotas, trajeto_rota  # noqa: E402
from regras.km_cobrado import COORD_CD                  # noqa: E402

CPF = "12345678901"
PIN = "123456"
AGENT = 4242


def _rascunho(rascunho_id=1, agent_id=AGENT, data=None, km=50.0):
    data = data or date.today().isoformat()
    return {
        "id": rascunho_id, "data_alvo": data, "lote_id": "L", "nome": f"Planejamento - #{rascunho_id}",
        "agent_id": agent_id, "vehicle_id": None, "motorista_nome": "Teste", "tipo_veiculo": None,
        "start_location_base_id": 1, "end_location_base_id": 1, "start_at": f"{data} 07:00:00",
        "km_estimado": km, "status": "RASCUNHO", "vuupt_route_id": None,
        "paradas": [
            {"ordem": 1, "service_id": None, "codigo": "PS-A", "titulo": "Cliente A", "endereco": "Rua A, 1",
             "latitude": -23.50, "longitude": -46.60, "nivel_dificuldade": 1, "volume_caixas": 2},
            {"ordem": 2, "service_id": None, "codigo": "PS-B", "titulo": "Cliente B", "endereco": "Rua B, 2",
             "latitude": -23.55, "longitude": -46.65, "nivel_dificuldade": 2, "volume_caixas": 1},
        ],
    }


class TestTrajetoRota(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        raiz = Path(self._tmp.name)
        self._patch_db = mock.patch.object(banco, "DB_PATH", raiz / "t.db")
        self._patch_db.start()
        self.app = api_motorista.criar_app({
            "api_motorista": {"secret_key": "segredo-de-teste", "pasta_comprovantes": str(raiz / "fotos"), "gcs_ativo": False},
        })
        self.cli = self.app.test_client()
        conn = banco.conectar()
        auth.criar_ou_atualizar_motorista(conn, CPF, "Motorista Teste", PIN, agent_id=AGENT, tipo_veiculo="VAN_HR")
        conn.close()

    def tearDown(self):
        self._patch_db.stop()
        self._tmp.cleanup()

    def _auth(self):
        r = self.cli.post("/api/login", json={"cpf": CPF, "pin": PIN})
        self.assertEqual(r.status_code, 200, r.get_json())
        return {"Authorization": f"Bearer {r.get_json()['acesso']}"}

    def _rota_app(self, **kw):
        conn = banco.conectar()
        rota_id = rotas.materializar_rascunho(_rascunho(**kw), banco.PROVEDOR_APP, conn=conn)
        conn.close()
        return rota_id

    def _desenho(self, pontos, *a, **k):
        return list(pontos) + [(-23.0, -46.0)]

    def test_trajeto_google_e_guardado(self):
        rota_id = self._rota_app()
        conn = banco.conectar()
        with mock.patch.object(km_rodoviario, "tracar_google", side_effect=self._desenho) as m:
            t1 = trajeto_rota.trajeto(conn, rota_id, "chave")
            t2 = trajeto_rota.trajeto(conn, rota_id, "chave")
        conn.close()
        self.assertEqual(t1["fonte"], "GOOGLE_ROUTES")
        self.assertEqual(m.call_count, 1)
        self.assertEqual(len(m.call_args[0][0]), 4)
        self.assertEqual(m.call_args[0][0][0], COORD_CD)
        self.assertEqual(m.call_args[0][0][3], COORD_CD)
        self.assertEqual(t2["fonte"], "GOOGLE_ROUTES")
        self.assertEqual(t2["pontos"], t1["pontos"])

    def test_parada_sem_coordenada_e_cancelada_ficam_fora(self):
        rota_id = self._rota_app()
        conn = banco.conectar()
        conn.execute("UPDATE nucleo_paradas SET latitude = NULL, longitude = NULL WHERE rota_id = ? AND ordem = 1", (rota_id,))
        conn.commit()
        self.assertEqual(trajeto_rota.pontos_da_rota(conn, rota_id), [COORD_CD, (-23.55, -46.65), COORD_CD])
        conn.execute("UPDATE nucleo_paradas SET latitude = -23.5, longitude = -46.6 WHERE rota_id = ? AND ordem = 1", (rota_id,))
        conn.execute("UPDATE nucleo_paradas SET situacao = ? WHERE rota_id = ? AND ordem = 2", (banco.PARADA_CANCELADA, rota_id))
        conn.commit()
        with mock.patch.object(km_rodoviario, "tracar_google", side_effect=self._desenho) as m:
            trajeto_rota.trajeto(conn, rota_id, "chave")
        conn.close()
        self.assertEqual(m.call_args[0][0], [COORD_CD, (-23.5, -46.6), COORD_CD])

    def test_reordenar_recalcula_reagendar_nao(self):
        rota_id = self._rota_app()
        conn = banco.conectar()
        with mock.patch.object(km_rodoviario, "tracar_google", side_effect=self._desenho) as m:
            trajeto_rota.trajeto(conn, rota_id, "chave")
            conn.execute("UPDATE nucleo_paradas SET situacao = 'EM_DESLOCAMENTO' WHERE rota_id = ? AND ordem = 1", (rota_id,))
            conn.commit()
            trajeto_rota.trajeto(conn, rota_id, "chave")
            self.assertEqual(m.call_count, 1)
            conn.execute("UPDATE nucleo_paradas SET ordem = CASE ordem WHEN 1 THEN 2 ELSE 1 END WHERE rota_id = ?", (rota_id,))
            conn.commit()
            trajeto_rota.trajeto(conn, rota_id, "chave")
            self.assertEqual(m.call_count, 2)
        conn.close()

    def test_google_falha_devolve_linha_reta_sem_gravar(self):
        rota_id = self._rota_app()
        conn = banco.conectar()
        with mock.patch.object(km_rodoviario, "tracar_google", side_effect=RuntimeError("fora")) as m:
            t = trajeto_rota.trajeto(conn, rota_id, "chave")
            self.assertEqual(t["fonte"], "LINHA_RETA")
            self.assertEqual(t["pontos"], [list(p) for p in trajeto_rota.pontos_da_rota(conn, rota_id)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM nucleo_rotas_trajeto").fetchone()[0], 0)
            trajeto_rota.trajeto(conn, rota_id, "chave")
            self.assertEqual(m.call_count, 2)
        conn.close()

    def test_sem_chave_ou_sem_pontos_nao_chama_google(self):
        rota_id = self._rota_app()
        conn = banco.conectar()
        with mock.patch.object(km_rodoviario, "tracar_google") as m:
            self.assertEqual(trajeto_rota.trajeto(conn, rota_id, None)["fonte"], "LINHA_RETA")
            conn.execute("UPDATE nucleo_paradas SET latitude = NULL WHERE rota_id = ?", (rota_id,))
            conn.commit()
            t = trajeto_rota.trajeto(conn, rota_id, "chave")
            self.assertEqual(t["pontos"], [])
            self.assertEqual(t["fonte"], "LINHA_RETA")
            m.assert_not_called()
        conn.close()

    def test_api_trajeto_404_de_outro_motorista(self):
        h = self._auth()
        outra = self._rota_app(rascunho_id=3, agent_id=999)
        self.assertEqual(self.cli.get(f"/api/rotas/{outra}/trajeto", headers=h).status_code, 404)

    def test_api_trajeto_rota_de_dia_futuro_nao_entrega_pontos_nem_chama_google(self):
        h = self._auth()
        futuro = (date.today() + timedelta(days=2)).isoformat()
        rota_id = self._rota_app(data=futuro)
        self.app.config["CONFIG_PROJETO"]["google_maps"] = {"api_key": "chave"}
        with mock.patch.object(km_rodoviario, "tracar_google", side_effect=self._desenho) as m:
            r = self.cli.get(f"/api/rotas/{rota_id}/trajeto", headers=h)
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["pontos"], [])
        self.assertEqual(j["fonte"], "LINHA_RETA")
        self.assertEqual(j["base"], [-23.497039, -46.6605211])
        m.assert_not_called()

    def test_parada_em_zero_zero_fica_fora(self):
        rota_id = self._rota_app()
        conn = banco.conectar()
        conn.execute("UPDATE nucleo_paradas SET latitude = 0, longitude = 0 WHERE rota_id = ? AND ordem = 1", (rota_id,))
        conn.commit()
        self.assertEqual(trajeto_rota.pontos_da_rota(conn, rota_id), [COORD_CD, (-23.55, -46.65), COORD_CD])
        conn.close()

    def test_api_trajeto_200(self):
        h = self._auth()
        rota_id = self._rota_app()
        with mock.patch.object(km_rodoviario, "tracar_google", side_effect=self._desenho):
            r = self.cli.get(f"/api/rotas/{rota_id}/trajeto", headers=h)
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertIn("pontos", j)
        self.assertIn("fonte", j)
        self.assertIn("assinatura", j)
        self.assertEqual(j["base"], [-23.497039, -46.6605211])

    def test_app_versao_sem_login(self):
        r = self.cli.get("/api/app-versao")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.get_json()["versao_atual"])
        self.assertEqual(r.get_json()["link"], api_motorista.APK_LINK)


if __name__ == "__main__":
    unittest.main()
