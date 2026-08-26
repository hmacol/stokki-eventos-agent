# -*- coding: utf-8 -*-
"""
test_api_motorista.py

Testes da API do app de motoristas (Fase B): login CPF+PIN com bloqueio,
tokens, rota do dia, aceitar/iniciar, eventos de parada idempotentes,
comprovante em disco, GPS -> km real, finalizar, financeiro, ofertas e
disponibilidade. Tudo num SQLite temporário, sem rede, sem GCS.
    python -m unittest nucleo.test_api_motorista -v
"""
import io
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import api_motorista, auth_motorista as auth, banco, rotas

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


class TestApiMotorista(unittest.TestCase):
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
        conn.execute("CREATE TABLE motivos_ocorrencia (id INTEGER PRIMARY KEY, vuupt_failed_reason_id INTEGER, motivo_texto TEXT, categoria TEXT)")
        conn.execute("INSERT INTO motivos_ocorrencia VALUES (1, 5431, 'Local fechado', 'CLIENTE')")
        conn.execute("""CREATE TABLE checklist_modelo (id INTEGER PRIMARY KEY, fluxo TEXT, chave TEXT, rotulo TEXT, tipo_campo TEXT,
                        obrigatorio INTEGER, opcoes TEXT, aviso TEXT, ordem INTEGER, ativo INTEGER DEFAULT 1)""")
        conn.execute("INSERT INTO checklist_modelo (fluxo, chave, rotulo, tipo_campo, obrigatorio, opcoes, ordem) VALUES ('ENTREGUE','nome_recebedor','Nome','TEXTO',1,NULL,1)")
        conn.commit()
        auth.criar_ou_atualizar_motorista(conn, CPF, "Motorista Teste", PIN, agent_id=AGENT, tipo_veiculo="VAN_HR")
        conn.close()

    def tearDown(self):
        self._patch_db.stop()
        self._tmp.cleanup()

    # helpers -----------------------------------------------------------------
    def _login(self, cpf=CPF, pin=PIN):
        return self.cli.post("/api/login", json={"cpf": cpf, "pin": pin})

    def _auth(self):
        r = self._login()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.tokens = r.get_json()
        return {"Authorization": f"Bearer {self.tokens['acesso']}"}

    def _rota_app(self, **kw):
        conn = banco.conectar()
        rota_id = rotas.materializar_rascunho(_rascunho(**kw), banco.PROVEDOR_APP, conn=conn)
        conn.close()
        return rota_id

    # testes ------------------------------------------------------------------
    def test_saude_sem_login(self):
        self.assertEqual(self.cli.get("/api/saude").status_code, 200)
        self.assertEqual(self.cli.get("/api/rotas").status_code, 401)

    def test_login_bloqueio_e_refresh(self):
        self.assertEqual(self._login(pin="000000").status_code, 401)
        self.assertEqual(self._login(cpf="99999999999").status_code, 401)
        for _ in range(4):
            self._login(pin="000000")
        self.assertEqual(self._login().status_code, 429)   # 5 erros -> bloqueado mesmo com PIN certo

        conn = banco.conectar()
        conn.execute("UPDATE motoristas SET bloqueado_ate = NULL WHERE cpf = ?", (CPF,))
        conn.commit()
        conn.close()
        h = self._auth()
        self.assertEqual(self.cli.get("/api/eu", headers=h).get_json()["agent_id"], AGENT)
        r = self.cli.post("/api/refresh", json={"refresh": self.tokens["refresh"]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("acesso", r.get_json())

        # Trocar o PIN invalida o token antigo
        conn = banco.conectar()
        auth.criar_ou_atualizar_motorista(conn, CPF, "Motorista Teste", "654321")
        conn.close()
        self.assertEqual(self.cli.get("/api/eu", headers=h).status_code, 401)

    def test_fluxo_completo_da_rota(self):
        h = self._auth()
        rota_id = self._rota_app()
        amanha = (date.today() + timedelta(days=1)).isoformat()
        self._rota_app(rascunho_id=2, data=amanha)
        self._rota_app(rascunho_id=3, agent_id=999)   # de outro motorista

        r = self.cli.get("/api/rotas", headers=h).get_json()
        self.assertEqual([x["id"] for x in r["rotas"]], [rota_id, 2])
        self.assertTrue(r["rotas"][0]["editavel"])
        self.assertEqual(self.cli.get("/api/rotas/3", headers=h).status_code, 404)

        r = self.cli.post(f"/api/rotas/{rota_id}/aceitar", json={"uuid": "u-aceite"}, headers=h)
        self.assertEqual(r.get_json()["status"], "ACEITA")
        r = self.cli.post(f"/api/rotas/{rota_id}/iniciar", json={"latitude": -23.49, "longitude": -46.66}, headers=h)
        self.assertEqual(r.get_json()["status"], "EM_ROTA")
        paradas = r.get_json()["paradas"]

        # GPS em lote (idempotente por uuid)
        pontos = [{"uuid": f"g{i}", "rota_id": rota_id, "ocorrido_em": f"2026-08-26 08:0{i}:00",
                   "latitude": -23.49 - 0.01 * i, "longitude": -46.66 + 0.01 * i, "precisao_m": 10} for i in range(5)]
        self.assertEqual(self.cli.post("/api/gps", json={"pontos": pontos}, headers=h).get_json()["novos"], 5)
        self.assertEqual(self.cli.post("/api/gps", json={"pontos": pontos}, headers=h).get_json()["novos"], 0)

        # Deslocamento -> chegada -> entrega na 1ª parada, com checklist; reenvio idempotente
        p1, p2 = paradas[0]["id"], paradas[1]["id"]
        r = self.cli.post(f"/api/paradas/{p1}/eventos", json={"uuid": "e0", "tipo": "DESLOCAMENTO", "ocorrido_em": "2026-08-26 07:50:00"}, headers=h)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["parada"]["situacao"], "EM_DESLOCAMENTO")
        self.assertEqual(r.get_json()["parada"]["started_at"], "2026-08-26 07:50:00")
        self.assertEqual(r.get_json()["contadores"]["pendentes"], 2)
        r = self.cli.post(f"/api/paradas/{p1}/eventos", json={"uuid": "e1", "tipo": "CHEGADA", "ocorrido_em": "2026-08-26 08:10:00"}, headers=h)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["parada"]["situacao"], "EM_ROTA")
        self.assertEqual(r.get_json()["parada"]["arrived_at"], "2026-08-26 08:10:00")
        ev = {"uuid": "e2", "tipo": "ENTREGUE", "ocorrido_em": "2026-08-26 08:20:00",
              "checklist": {"nome_recebedor": "Maria", "vinculo": "Porteiro"}, "latitude": -23.50, "longitude": -46.60}
        r = self.cli.post(f"/api/paradas/{p1}/eventos", json=ev, headers=h)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["parada"]["situacao"], "ENTREGUE")
        # Durações gravadas: saiu 07:50 -> chegou 08:10 (1200s) -> recebeu 08:20 (600s)
        self.assertEqual(r.get_json()["parada"]["tempo_deslocamento_s"], 1200)
        self.assertEqual(r.get_json()["parada"]["tempo_no_local_s"], 600)
        r = self.cli.post(f"/api/paradas/{p1}/eventos", json=ev, headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ja_registrado"])

        # Comprovante (foto) em disco
        foto = (io.BytesIO(b"\xff\xd8\xff conteudo"), "canhoto.jpg")
        r = self.cli.post(f"/api/paradas/{p1}/comprovantes", headers=h,
                          data={"arquivo": foto, "tipo": "CANHOTO", "uuid": "f1"}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertFalse(r.get_json()["gcs"])
        arquivos = list((Path(self._tmp.name) / "fotos" / "PS-A").glob("*.jpg"))
        self.assertEqual(len(arquivos), 1)
        r = self.cli.post(f"/api/paradas/{p1}/comprovantes", headers=h,
                          data={"arquivo": (io.BytesIO(b"x"), "c.jpg"), "tipo": "CANHOTO", "uuid": "f1"}, content_type="multipart/form-data")
        self.assertTrue(r.get_json()["ja_registrado"])

        # Reagendar na 2ª parada: esteve no local 5 min, marca retorno; parada volta a PENDENTE
        self.cli.post(f"/api/paradas/{p2}/eventos", json={"uuid": "r0", "tipo": "DESLOCAMENTO", "ocorrido_em": "2026-08-26 08:30:00"}, headers=h)
        self.cli.post(f"/api/paradas/{p2}/eventos", json={"uuid": "r1", "tipo": "CHEGADA", "ocorrido_em": "2026-08-26 08:40:00"}, headers=h)
        r = self.cli.post(f"/api/paradas/{p2}/eventos", json={"uuid": "r2", "tipo": "REAGENDAR", "ocorrido_em": "2026-08-26 08:45:00"}, headers=h)
        self.assertEqual(r.status_code, 400)   # sem novo_horario
        r = self.cli.post(f"/api/paradas/{p2}/eventos", json={"uuid": "r2", "tipo": "REAGENDAR", "ocorrido_em": "2026-08-26 08:45:00",
                                                              "novo_horario": "2026-08-26 11:00", "observacoes": "voltar às 11"}, headers=h)
        self.assertEqual(r.status_code, 201, r.get_json())
        pr = r.get_json()["parada"]
        self.assertEqual((pr["situacao"], pr["reagendado_para"], pr["tentativas"], pr["arrived_at"]), ("PENDENTE", "2026-08-26 11:00", 1, None))
        self.assertEqual(r.get_json()["rota_status"], "EM_ROTA")
        conn = banco.conectar()
        ev = conn.execute("SELECT dados_json FROM nucleo_eventos WHERE uuid = 'r2'").fetchone()[0]
        conn.close()
        self.assertIn('"tempo_no_local_s": 300', ev)
        # 'FIM' também vale; volta, chega de novo e aí sim conclui
        r = self.cli.post(f"/api/paradas/{p2}/eventos", json={"uuid": "r3", "tipo": "REAGENDAR", "novo_horario": "fim"}, headers=h)
        self.assertEqual(r.get_json()["parada"]["reagendado_para"], "FIM")
        self.assertEqual(r.get_json()["parada"]["tentativas"], 2)

        # Insucesso sem motivo -> 400; com motivo -> conclui a rota sozinha
        r = self.cli.post(f"/api/paradas/{p2}/eventos", json={"uuid": "e3", "tipo": "INSUCESSO"}, headers=h)
        self.assertEqual(r.status_code, 400)
        r = self.cli.post(f"/api/paradas/{p2}/eventos", json={"uuid": "e3", "tipo": "INSUCESSO", "motivo_id": 1,
                                                              "ocorrido_em": "2026-08-26 09:00:00"}, headers=h)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["parada"]["motivo_texto"], "Local fechado")
        self.assertEqual(r.get_json()["rota_status"], "CONCLUIDA")

        rota = self.cli.get(f"/api/rotas/{rota_id}", headers=h).get_json()
        self.assertEqual((rota["entregues"], rota["insucessos"]), (1, 1))
        self.assertEqual(rota["km_fonte"], "GPS_APP")
        self.assertGreater(rota["km_real"], 3.0)       # 5 pontos GPS + chegada/entrega
        self.assertEqual(len(rota["paradas"][0]["comprovantes"]), 1)

        # Finalizar de novo é idempotente (já concluída)
        r = self.cli.post(f"/api/rotas/{rota_id}/finalizar", json={"pedagio": 12.5}, headers=h)
        self.assertEqual(r.get_json()["status"], "CONCLUIDA")

        # Financeiro: VAN_HR, km real ~3-4 km -> base 550
        fin = self.cli.get(f"/api/financeiro?de={date.today()}&ate={date.today()}", headers=h).get_json()
        self.assertEqual(fin["total"], 550.0)
        self.assertFalse(fin["valores_provisorios"])
        self.assertEqual(fin["tarifa"]["tipo_tarifa"], "VAN_HR")

    def test_rota_vuupt_e_somente_leitura_mas_aceite_alimenta_confirmacao(self):
        h = self._auth()
        conn = banco.conectar()
        conn.execute("""CREATE TABLE confirmacoes_rota (id INTEGER PRIMARY KEY, vuupt_route_id INTEGER, agent_id INTEGER,
                        data_rota TEXT, token TEXT, status TEXT DEFAULT 'AGUARDANDO', motivo_recusa TEXT, respondido_em TEXT)""")
        conn.execute("INSERT INTO confirmacoes_rota (vuupt_route_id, agent_id, data_rota, token) VALUES (777, ?, ?, 't')", (AGENT, date.today().isoformat()))
        conn.commit()
        rota_id = rotas.materializar_rascunho(_rascunho(), banco.PROVEDOR_VUUPT, vuupt_route_id=777, conn=conn)
        conn.close()

        r = self.cli.get(f"/api/rotas/{rota_id}", headers=h).get_json()
        self.assertFalse(r["editavel"])
        self.assertEqual(r["confirmacao"]["status"], "AGUARDANDO")
        self.assertEqual(self.cli.post(f"/api/rotas/{rota_id}/iniciar", json={}, headers=h).status_code, 409)
        p1 = r["paradas"][0]["id"]
        self.assertEqual(self.cli.post(f"/api/paradas/{p1}/eventos", json={"uuid": "x", "tipo": "ENTREGUE"}, headers=h).status_code, 409)

        r = self.cli.post(f"/api/rotas/{rota_id}/recusar", json={"motivo": "carro quebrado"}, headers=h).get_json()
        self.assertEqual(r["confirmacao"]["status"], "RECUSADO")
        self.assertEqual(r["confirmacao"]["motivo_recusa"], "carro quebrado")
        r = self.cli.post(f"/api/rotas/{rota_id}/aceitar", json={}, headers=h).get_json()
        self.assertEqual(r["confirmacao"]["status"], "CONFIRMADO")
        self.assertEqual(r["status"], "ACEITA")

    def test_ofertas_claim_atomico_e_cancelamento(self):
        h = self._auth()
        import json as _json
        conn = banco.conectar()
        conn.execute("""CREATE TABLE ofertas_rota (id INTEGER PRIMARY KEY, rascunho_id INTEGER UNIQUE, data_alvo TEXT,
                        resumo_json TEXT, agent_ids_elegiveis TEXT, status TEXT DEFAULT 'ABERTA', escolhido_por INTEGER,
                        escolhido_em TEXT, criado_em TEXT, sincronizado_em TEXT, aplicado_em TEXT)""")
        hoje = date.today().isoformat()
        conn.execute("INSERT INTO ofertas_rota (rascunho_id, data_alvo, resumo_json, agent_ids_elegiveis, sincronizado_em) VALUES (10, ?, ?, ?, 'x')",
                     (hoje, _json.dumps({"regiao": "ZONA NORTE", "paradas": 12}), _json.dumps([{"agent_id": AGENT, "cpf": CPF}])))
        conn.execute("INSERT INTO ofertas_rota (rascunho_id, data_alvo, resumo_json, agent_ids_elegiveis) VALUES (11, ?, '{}', ?)",
                     (hoje, _json.dumps([{"agent_id": 1}])))
        conn.commit()
        conn.close()

        r = self.cli.get("/api/ofertas", headers=h).get_json()
        self.assertEqual([o["rascunho_id"] for o in r["abertas"]], [10])
        self.assertEqual(self.cli.post("/api/ofertas/11/escolher", headers=h).status_code, 404)
        r = self.cli.post("/api/ofertas/10/escolher", headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["minhas"][0]["rascunho_id"], 10)
        self.assertEqual(self.cli.post("/api/ofertas/10/escolher", headers=h).status_code, 409)  # já escolhida

        conn = banco.conectar()
        row = conn.execute("SELECT status, escolhido_por, sincronizado_em FROM ofertas_rota WHERE rascunho_id = 10").fetchone()
        self.assertEqual((row[0], row[1], row[2]), ("ESCOLHIDA", AGENT, None))  # sincronizado_em=NULL -> push pra VPS
        conn.close()

        r = self.cli.post("/api/ofertas/10/cancelar", headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()["abertas"]), 1)

    def test_disponibilidade(self):
        h = self._auth()
        hoje = date.today()
        r = self.cli.put("/api/disponibilidade", json={"de": hoje.isoformat(), "ate": (hoje + timedelta(days=2)).isoformat(),
                                                       "disponivel": False, "motivo": "férias"}, headers=h)
        self.assertEqual(r.get_json()["dias"], 3)
        r = self.cli.get(f"/api/disponibilidade?de={hoje}&ate={hoje + timedelta(days=5)}", headers=h).get_json()
        self.assertEqual(len(r["ajustes"]), 3)
        self.assertFalse(r["ajustes"][0]["disponivel"])
        r = self.cli.put("/api/disponibilidade", json={"data": hoje.isoformat(), "disponivel": None}, headers=h)
        self.assertEqual(len(r.get_json()["ajustes"]), 0)

    def test_checklist(self):
        h = self._auth()
        r = self.cli.get("/api/checklist", headers=h).get_json()
        self.assertEqual(r["fluxos"]["ENTREGUE"][0]["chave"], "nome_recebedor")
        self.assertEqual(r["motivos"][0]["motivo_texto"], "Local fechado")


if __name__ == "__main__":
    unittest.main()
