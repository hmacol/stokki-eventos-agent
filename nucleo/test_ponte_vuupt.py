# -*- coding: utf-8 -*-
"""
test_ponte_vuupt.py

Ponte nucleo -> Vuupt (nucleo/ponte_vuupt.py): rota da Vuupt operada pelo
nosso app. Tudo num SQLite temporario, com uma Vuupt de mentira (sem rede).
    python -m unittest nucleo.test_ponte_vuupt -v
"""
import io
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import api_motorista, auth_motorista as auth, banco, ponte_vuupt, sincronizar_vuupt

CPF, PIN, AGENT = "12345678901", "123456", 4242
ROTA_VUUPT = 900


def _servico(service_id, code, status="assigned", **extra):
    return {"id": service_id, "code": code, "title": f"{code} - 123 / BRAZO / CLIENTE", "status": status,
            "status_done": None, "type": "delivery", "route_id": ROTA_VUUPT, "address": "Rua A, 1",
            "latitude": -23.5, "longitude": -46.6, "sender_id": 11, "dimension_3": 3, **extra}


def _rota_vuupt(servicos, status="assigned", agent_id=AGENT):
    hoje = date.today().isoformat()
    return {"id": ROTA_VUUPT, "name": "Rota #900", "status": status, "agent_id": agent_id, "vehicle_id": 7,
            "start_at": f"{hoje} 11:00:00", "services": {"data": servicos}}


class VuuptFalsa:
    """So o que a ponte usa: le o servico, avanca o ciclo e conclui."""

    def __init__(self, servicos):
        self.servicos = {s["id"]: dict(s) for s in servicos}
        self.chamadas = []
        self.falhar = False

    def buscar_servico_por_id(self, service_id):
        return dict(self.servicos[service_id]) if service_id in self.servicos else None

    def avancar_como_agente(self, service_id, alvo, status_atual=""):
        if self.falhar:
            raise RuntimeError("Vuupt fora do ar")
        self.chamadas.append(("avancar", service_id, alvo, status_atual))
        self.servicos[service_id]["status"] = alvo

    def concluir_como_agente(self, service_id, sucesso=True, failed_reason_id=None, status_atual="",
                             fotos_checklist=None, servico=None):
        if self.falhar:
            raise RuntimeError("Vuupt fora do ar")
        self.chamadas.append(("concluir", service_id, sucesso, failed_reason_id, sorted(fotos_checklist or {})))
        self.servicos[service_id].update(status="done", status_done="success" if sucesso else "failed")
        return 555 if fotos_checklist else None


def _jpeg() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, "JPEG")
    return buf.getvalue()


class TestPonteVuupt(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.raiz = Path(self._tmp.name)
        self._patch_db = mock.patch.object(banco, "DB_PATH", self.raiz / "t.db")
        self._patch_db.start()
        self.addCleanup(ponte_vuupt.definir_config, None)
        self._app(ponte={"ativo": True, "agentes": [AGENT]})
        conn = banco.conectar()
        conn.execute("CREATE TABLE motivos_ocorrencia (id INTEGER PRIMARY KEY, vuupt_failed_reason_id INTEGER, motivo_texto TEXT, categoria TEXT)")
        conn.execute("INSERT INTO motivos_ocorrencia VALUES (1, 5431, 'Local fechado', 'CLIENTE')")
        conn.commit()
        auth.criar_ou_atualizar_motorista(conn, CPF, "Motorista Teste", PIN, agent_id=AGENT, tipo_veiculo="VAN_HR")
        self.servicos = [_servico(9001, "#PS-1"), _servico(9002, "#PS-2")]
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt(self.servicos)], conn)
        conn.commit()
        self.rota_id = conn.execute("SELECT id FROM nucleo_rotas WHERE vuupt_route_id = ?", (ROTA_VUUPT,)).fetchone()["id"]
        self.p1, self.p2 = [r["id"] for r in conn.execute(
            "SELECT id FROM nucleo_paradas WHERE rota_id = ? ORDER BY ordem", (self.rota_id,))]
        conn.close()
        self.vuupt = VuuptFalsa(self.servicos)
        self.h = {"Authorization": "Bearer " + self.cli.post("/api/login", json={"cpf": CPF, "pin": PIN}).get_json()["acesso"]}

    def tearDown(self):
        self._patch_db.stop()
        self._tmp.cleanup()

    # helpers -----------------------------------------------------------------
    def _app(self, ponte):
        self.config = {"api_motorista": {"secret_key": "segredo-de-teste", "pasta_comprovantes": str(self.raiz / "fotos"),
                                         "gcs_ativo": False, "ponte_vuupt": ponte}}
        self.cli = api_motorista.criar_app(self.config).test_client()

    def _evento(self, parada_id, tipo, uuid, **extra):
        return self.cli.post(f"/api/paradas/{parada_id}/eventos", json={"uuid": uuid, "tipo": tipo, **extra}, headers=self.h)

    def _processar(self, agora=None):
        conn = banco.conectar()
        try:
            return ponte_vuupt.processar_fila(conn, self.vuupt, ponte_vuupt.config_ponte(self.config), raiz=_RAIZ, agora=agora)
        finally:
            conn.close()

    def _fila(self):
        conn = banco.conectar()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM nucleo_ponte_vuupt ORDER BY id")]
        finally:
            conn.close()

    def _um(self, sql, *params):
        conn = banco.conectar()
        try:
            return dict(conn.execute(sql, params).fetchone())
        finally:
            conn.close()

    # testes ------------------------------------------------------------------
    def test_sem_ponte_rota_da_vuupt_segue_somente_leitura(self):
        for ponte in ({}, {"ativo": True, "agentes": [1]}, {"ativo": False, "agentes": [AGENT]}):
            self._app(ponte)
            r = self.cli.get(f"/api/rotas/{self.rota_id}", headers=self.h).get_json()
            self.assertFalse(r["editavel"], ponte)
            self.assertEqual(self._evento(self.p1, "CHEGADA", f"u-{len(ponte)}").status_code, 409, ponte)
        self.assertEqual(self._fila(), [])

    def test_entrega_pelo_app_e_repetida_na_vuupt(self):
        self.assertTrue(self.cli.get(f"/api/rotas/{self.rota_id}", headers=self.h).get_json()["editavel"])
        self.assertEqual(self.cli.post(f"/api/rotas/{self.rota_id}/aceitar", json={}, headers=self.h).status_code, 200)
        self.assertEqual(self._processar()["feitos"], 1)   # o aceite vai pra Vuupt na vespera, antes da rota rodar
        self.assertEqual(self.cli.post(f"/api/rotas/{self.rota_id}/iniciar", json={}, headers=self.h).status_code, 200)
        for tipo in ("DESLOCAMENTO", "CHEGADA", "ENTREGUE"):
            self.assertEqual(self._evento(self.p1, tipo, f"u-{tipo}").status_code, 201)
        # reenvio da fila offline nao duplica
        self.assertEqual(self._evento(self.p1, "ENTREGUE", "u-ENTREGUE").status_code, 200)
        self.assertEqual([i["acao"] for i in self._fila()], ["ACEITAR", "DESLOCAMENTO", "CHEGADA", "CONCLUIR"])

        stats = self._processar()
        self.assertEqual(stats, {"feitos": 3, "adiados": 0, "erros": 0, "desistiu": 0})
        self.assertEqual(self.vuupt.chamadas, [
            ("avancar", 9001, "accepted", "assigned"), ("avancar", 9002, "accepted", "assigned"),
            ("avancar", 9001, "on_route", "accepted"), ("avancar", 9001, "arrived", "on_route"),
            ("concluir", 9001, True, None, []),
        ])
        self.assertEqual({i["status"] for i in self._fila()}, {"FEITO"})
        self.assertEqual(self._processar()["feitos"], 0)   # nada pendente: nao chama de novo

    def test_resultado_espera_a_foto_e_manda_o_canhoto_no_checklist(self):
        self._evento(self.p1, "ENTREGUE", "u1", checklist={"fotos_qtd": {"foto_canhoto": 1}})
        self.assertEqual(self._processar()["adiados"], 1)
        self.assertEqual(self.vuupt.chamadas, [])
        # a foto chega depois pela fila do aparelho
        r = self.cli.post(f"/api/paradas/{self.p1}/comprovantes", headers=self.h, content_type="multipart/form-data",
                          data={"tipo": "CANHOTO", "uuid": "f1", "arquivo": (io.BytesIO(_jpeg()), "c.jpg")})
        self.assertEqual(r.status_code, 201, r.get_json())
        with mock.patch.object(ponte_vuupt, "_RAIZ", _RAIZ):
            stats = self._processar(agora=datetime.now() + timedelta(minutes=2))
        self.assertEqual(stats["feitos"], 1)
        self.assertEqual(self.vuupt.chamadas, [("concluir", 9001, True, None, [ponte_vuupt.CAMPO_CANHOTO])])
        self.assertIn("checklist=555", self._fila()[0]["resultado"])

    def test_foto_que_nao_chega_nao_segura_a_entrega_pra_sempre(self):
        self._evento(self.p1, "ENTREGUE", "u1", checklist={"fotos_qtd": {"foto_canhoto": 2}})
        self.assertEqual(self._processar(agora=datetime.now() + timedelta(minutes=10))["adiados"], 1)
        self.assertEqual(self._processar(agora=datetime.now() + timedelta(minutes=31))["feitos"], 1)
        self.assertEqual(self.vuupt.chamadas, [("concluir", 9001, True, None, [])])
        self.assertIn("sem_foto", self._fila()[0]["resultado"])

    def test_insucesso_leva_o_motivo_da_vuupt_e_parcial_conta_como_entregue(self):
        self._evento(self.p1, "INSUCESSO", "u1", motivo_id=1)
        self._evento(self.p2, "PARCIAL", "u2", motivo_id=1)
        self._processar()
        self.assertEqual(self.vuupt.chamadas, [("concluir", 9001, False, 5431, []), ("concluir", 9002, True, None, [])])

    def test_vuupt_fora_do_ar_tenta_de_novo_em_ordem_e_desiste_com_aviso(self):
        self._evento(self.p1, "CHEGADA", "u1")
        self._evento(self.p1, "ENTREGUE", "u2")
        self._evento(self.p2, "CHEGADA", "u3")
        self.vuupt.falhar = True
        agora = datetime.now()
        stats = self._processar(agora=agora)
        # o resultado da parada 1 espera a chegada dela; a parada 2 e tentada (e falha) por conta propria
        self.assertEqual(stats["erros"], 2)
        fila = self._fila()
        self.assertEqual([(i["tentativas"], i["status"]) for i in fila], [(1, "PENDENTE"), (0, "PENDENTE"), (1, "PENDENTE")])
        self.assertEqual(self._processar(agora=agora)["erros"], 0)   # dentro do recuo: nem tenta
        self.vuupt.falhar = False
        self.assertEqual(self._processar(agora=agora + timedelta(minutes=2))["feitos"], 3)
        self.assertEqual([c[:3] for c in self.vuupt.chamadas],
                         [("avancar", 9001, "arrived"), ("concluir", 9001, True), ("avancar", 9002, "arrived")])

        # desistencia: depois de MAX_TENTATIVAS o item sai da fila e conta em "desistiu"
        self._evento(self.p2, "ENTREGUE", "u4")
        self.vuupt.falhar = True
        total = {"desistiu": 0}
        for n in range(ponte_vuupt.MAX_TENTATIVAS):
            total["desistiu"] += self._processar(agora=agora + timedelta(days=1 + n))["desistiu"]
        self.assertEqual(total["desistiu"], 1)
        self.assertEqual(self._fila()[-1]["status"], "DESISTIU")

    def test_servico_ja_fechado_na_vuupt_nao_e_tocado(self):
        self.vuupt.servicos[9001].update(status="done", status_done="success")
        self._evento(self.p1, "ENTREGUE", "u1")
        self._processar()
        self.assertEqual(self.vuupt.chamadas, [])
        self.assertEqual(self._fila()[0]["resultado"], "ja_fechado")

    def test_sync_nao_apaga_o_que_o_app_registrou(self):
        self.cli.post(f"/api/rotas/{self.rota_id}/iniciar", json={}, headers=self.h)
        self._evento(self.p1, "ENTREGUE", "u1", ocorrido_em="2026-11-16 09:15:00")
        # a Vuupt ainda nao recebeu a copia: continua dizendo "assigned"
        conn = banco.conectar()
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt(self.servicos)], conn)
        conn.commit()
        conn.close()
        p1 = self._um("SELECT situacao, completed_at FROM nucleo_paradas WHERE id = ?", self.p1)
        self.assertEqual((p1["situacao"], p1["completed_at"]), ("ENTREGUE", "2026-11-16 09:15:00"))
        rota = self._um("SELECT status, entregues, operada_app FROM nucleo_rotas WHERE id = ?", self.rota_id)
        self.assertEqual((rota["status"], rota["entregues"], rota["operada_app"]), ("EM_ROTA", 1, 1))

        # depois da copia, a Vuupt devolve o horario DELA: o do aparelho continua valendo;
        # e o que ela fechou sozinha (parada 2 cancelada pelo painel) entra
        depois = [_servico(9001, "#PS-1", status="done", status_done="success", completed_at="2026-11-16 15:00:00"),
                  _servico(9002, "#PS-2", status="canceled")]
        conn = banco.conectar()
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt(depois)], conn)
        conn.commit()
        conn.close()
        p1 = self._um("SELECT situacao, completed_at, status_provedor FROM nucleo_paradas WHERE id = ?", self.p1)
        self.assertEqual((p1["situacao"], p1["completed_at"], p1["status_provedor"]), ("ENTREGUE", "2026-11-16 09:15:00", "done"))
        self.assertEqual(self._um("SELECT situacao FROM nucleo_paradas WHERE id = ?", self.p2)["situacao"], "CANCELADA")

    def test_rota_nao_operada_pelo_app_sincroniza_como_antes(self):
        depois = [_servico(9001, "#PS-1", status="done", status_done="success", completed_at="2026-11-16 15:00:00"),
                  _servico(9002, "#PS-2")]
        conn = banco.conectar()
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt(depois, status="started")], conn)
        conn.commit()
        conn.close()
        p1 = self._um("SELECT situacao, completed_at FROM nucleo_paradas WHERE id = ?", self.p1)
        self.assertEqual((p1["situacao"], p1["completed_at"]), ("ENTREGUE", "2026-11-16 12:00:00"))   # UTC -> local


if __name__ == "__main__":
    unittest.main()
