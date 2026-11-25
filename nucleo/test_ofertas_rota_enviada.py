# -*- coding: utf-8 -*-
"""
test_ofertas_rota_enviada.py

Rota enviada sem aceite vira oferta no app (nucleo/ofertas_rota_enviada.py).
SQLite temporario, sem rede: elegibilidade, resumo e WhatsApp sao de mentira.
    python -m unittest nucleo.test_ofertas_rota_enviada -v
"""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import api_motorista, auth_motorista as auth, banco, ofertas_rota_enviada as ofertas, sincronizar_vuupt

PIN = "123456"
ORIGINAL, OUTRO, TERCEIRO = 4242, 4343, 4444
CPFS = {ORIGINAL: "12345678901", OUTRO: "22345678902", TERCEIRO: "32345678903"}
ROTA_VUUPT = 900
HOJE = date.today()
ID_OFERTA = ofertas.ID_BASE   # + rota_id
GRUPO_ROTAS_ID = "120363-rotas@g.us"
GRUPOS = [{"id": "120363-alertas@g.us", "name": "ALERTAS FRESH"}, {"id": GRUPO_ROTAS_ID, "name": "Rotas Freshlog"}]


def _servico(service_id, status="assigned"):
    return {"id": service_id, "code": f"#PS-{service_id}", "title": f"PS-{service_id} - 1 / REM / CLIENTE", "status": status,
            "status_done": None, "type": "delivery", "route_id": ROTA_VUUPT, "address": "Rua A, 1, Centro, Santos - SP",
            "latitude": -23.5, "longitude": -46.6, "sender_id": 11, "dimension_3": 3}


def _rota_vuupt(servicos, agent_id=ORIGINAL, status="assigned", assigned_at=None):
    return {"id": ROTA_VUUPT, "name": "Planejamento - #13", "status": status, "agent_id": agent_id, "vehicle_id": 7,
            "start_at": f"{HOJE.isoformat()} 09:00:00", "assigned_at": assigned_at, "services": {"data": servicos}}


def _as(hora: str) -> datetime:
    return datetime.fromisoformat(f"{HOJE.isoformat()} {hora}:00")


class TestOfertasRotaEnviada(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.raiz = Path(self._tmp.name)
        self._patch_db = mock.patch.object(banco, "DB_PATH", self.raiz / "t.db")
        self._patch_db.start()
        self.addCleanup(ofertas.ponte_vuupt.definir_config, None)
        self.config = {"api_motorista": {
            "secret_key": "segredo-de-teste", "pasta_comprovantes": str(self.raiz / "fotos"), "gcs_ativo": False,
            "ponte_vuupt": {"ativo": True, "agentes": [ORIGINAL]},
            "rota_sem_aceite": {"ativo": True, "horario_limite": "05:30"}}}
        self.cli = api_motorista.criar_app(self.config).test_client()
        conn = banco.conectar()
        for agente, cpf in CPFS.items():
            auth.criar_ou_atualizar_motorista(conn, cpf, f"Motorista {agente}", PIN, agent_id=agente, tipo_veiculo="FIORINO")
        self.servicos = [_servico(9001), _servico(9002)]
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt(self.servicos)], conn)
        conn.commit()
        self.rota_id = conn.execute("SELECT id FROM nucleo_rotas WHERE vuupt_route_id = ?", (ROTA_VUUPT,)).fetchone()["id"]
        # rota da madrugada: enviada na vespera
        conn.execute("UPDATE nucleo_rotas SET criado_em = ? WHERE id = ?",
                     (f"{(HOJE - timedelta(days=1)).isoformat()} 22:00:00", self.rota_id))
        conn.commit()
        conn.close()
        self.enviados = []

    def tearDown(self):
        self._patch_db.stop()
        self._tmp.cleanup()

    # helpers -----------------------------------------------------------------
    def _h(self, agente):
        r = self.cli.post("/api/login", json={"cpf": CPFS[agente], "pin": PIN})
        return {"Authorization": "Bearer " + r.get_json()["acesso"]}

    def _despachar(self, config, origem, tipo, texto, assinatura=None, modo_teste=False, grupo_id=None, **kw):
        self.enviados.append({"tipo": tipo, "texto": texto, "grupo_id": grupo_id})
        return "enviado"

    def _rodar(self, hora, config=None, grupos=GRUPOS):
        conn = banco.conectar()
        try:
            return ofertas.rodar(conn, config or self.config, agora=_as(hora),
                                 resumo=lambda c, rota, chave: {"regiao": "BAIXADA SANTISTA", "total_paradas": 2, "saida": "06:00"},
                                 despachar=self._despachar, listar_grupos=lambda cfg: grupos)
        finally:
            conn.close()

    def _sql(self, sql, params=()):
        conn = banco.conectar()
        conn.execute(sql, params)
        conn.commit()
        conn.close()

    def _oferta(self):
        conn = banco.conectar()
        try:
            o = conn.execute("SELECT * FROM nucleo_ofertas_rota WHERE rota_id = ?", (self.rota_id,)).fetchone()
            return dict(o) if o else None
        finally:
            conn.close()

    def _sync(self, **kw):
        conn = banco.conectar()
        sincronizar_vuupt.sincronizar_rotas([_rota_vuupt(self.servicos, **kw)], conn)
        conn.commit()
        conn.close()

    # testes ------------------------------------------------------------------
    def test_desligado_ou_fora_do_piloto_nao_publica(self):
        desligado = {"api_motorista": {**self.config["api_motorista"], "rota_sem_aceite": {"ativo": False}}}
        self.assertEqual(self._rodar("05:40", config=desligado)["publicadas"], 0)
        fora = {"api_motorista": {**self.config["api_motorista"], "ponte_vuupt": {"ativo": True, "agentes": [999]}}}
        self.assertEqual(self._rodar("05:40", config=fora)["publicadas"], 0)
        todos = {"api_motorista": {**fora["api_motorista"], "rota_sem_aceite": {"ativo": True, "todos": True}}}
        self.assertEqual(self._rodar("05:40", config=todos)["publicadas"], 1)

    def test_prazo_so_publica_depois_das_0530_e_sem_aceite(self):
        self.assertEqual(self._rodar("05:29")["publicadas"], 0)
        self.assertIsNone(self._oferta())
        self.assertEqual(self._rodar("05:30")["publicadas"], 1)
        self.assertEqual(self._oferta()["motivo"], "PRAZO")
        self.assertEqual(self._rodar("05:32")["publicadas"], 0)          # uma oferta por rota
        self.assertEqual([e["tipo"] for e in self.enviados], ["rota_disponivel"])   # um aviso so

    def test_rota_enviada_depois_das_0530_so_vira_oferta_se_recusar(self):
        # caso real 19/11: Planejamento #21 enviado direto ao motorista 14h14, ofertado 14h16
        self._sql("UPDATE nucleo_rotas SET criado_em = ? WHERE id = ?", (f"{HOJE.isoformat()} 14:14:47", self.rota_id))
        self.assertEqual(self._rodar("14:16")["publicadas"], 0)
        self.assertEqual(self._rodar("18:00")["publicadas"], 0)
        self.cli.post(f"/api/rotas/{self.rota_id}/recusar", json={"motivo": "nao consigo"}, headers=self._h(ORIGINAL))
        self.assertEqual(self._rodar("18:02")["publicadas"], 1)
        self.assertEqual(self._oferta()["motivo"], "RECUSA")

    def test_troca_de_motorista_depois_das_0530_vale_o_assigned_at_da_vuupt(self):
        self._sync(assigned_at=f"{HOJE.isoformat()} 12:00:00")   # UTC = 09h00 local
        self.assertEqual(self._rodar("09:05")["publicadas"], 0)
        self._sync(assigned_at=f"{HOJE.isoformat()} 08:00:00")   # UTC = 05h00 local
        self.assertEqual(self._rodar("09:05")["publicadas"], 1)

    def test_aceite_no_app_ou_na_vuupt_nao_publica(self):
        self.cli.post(f"/api/rotas/{self.rota_id}/aceitar", json={}, headers=self._h(ORIGINAL))
        self.assertEqual(self._rodar("05:40")["publicadas"], 0)

    def test_aceite_so_na_vuupt_nao_publica(self):
        self.servicos = [_servico(9001, "accepted"), _servico(9002, "accepted")]
        self._sync(status="not_started")
        self.assertEqual(self._rodar("05:40")["publicadas"], 0)

    def test_recusa_publica_na_hora_e_some_da_lista_de_quem_recusou(self):
        h = self._h(ORIGINAL)
        self.cli.post(f"/api/rotas/{self.rota_id}/recusar", json={"motivo": "carro quebrou"}, headers=h)
        self.assertEqual(self._rodar("01:00")["publicadas"], 1)
        self.assertEqual(self._oferta()["motivo"], "RECUSA")
        hoje = HOJE.isoformat()
        self.assertEqual(self.cli.get(f"/api/rotas?de={hoje}&ate={hoje}", headers=h).get_json()["rotas"], [])
        # quem recusou nao e elegivel: nao ve a oferta
        self.assertEqual(self.cli.get("/api/ofertas", headers=h).get_json(), {"abertas": [], "minhas": []})

    def test_aviso_do_grupo_cabe_em_200_e_leva_link_valor_e_entregas(self):
        self._rodar("05:40")
        aviso = self.enviados[0]
        self.assertLessEqual(len(aviso["texto"]), 200)
        self.assertIn("2 entregas", aviso["texto"])
        self.assertIn("Valor aprox. R$ ", aviso["texto"])
        id_oferta = self._oferta()["id"]
        self.assertTrue(aviso["texto"].endswith(f"{ofertas.LINK_PADRAO}/{ofertas.token_oferta('segredo-de-teste', id_oferta)}"))
        oferta = {"data_rota": "2026-11-17", "resumo_json": '{"regiao": "%s", "total_paradas": 11, "saida": "06:00"}' % ("Campinas, " * 40)}
        longo = ofertas.texto_disponivel(oferta, 1234.5, ofertas.LINK_PADRAO)
        self.assertLessEqual(len(longo), 200)
        self.assertTrue(longo.endswith(ofertas.LINK_PADRAO))

    def test_outro_motorista_escolhe_equipe_e_avisada_e_troca_na_vuupt_aplica(self):
        self._rodar("05:40")
        h_outro, h_terceiro = self._h(OUTRO), self._h(TERCEIRO)
        abertas = self.cli.get("/api/ofertas", headers=h_outro).get_json()["abertas"]
        self.assertEqual(len(abertas), 1)
        self.assertEqual(abertas[0]["rascunho_id"], ID_OFERTA + self.rota_id)
        self.assertIn("valor_aproximado", abertas[0]["resumo"])
        self.assertNotIn("endereco", str(abertas[0]))      # so o resumo, nunca a rota

        r = self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher", headers=h_outro)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(len(r.get_json()["minhas"]), 1)
        self.assertFalse(r.get_json()["minhas"][0]["aplicada"])
        self.assertEqual(self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher", headers=h_terceiro).status_code, 409)
        self.assertEqual(self.cli.get("/api/ofertas", headers=h_terceiro).get_json()["abertas"], [])
        # o original nao pode mais aceitar: ja tem dono na fila
        self.assertEqual(self.cli.post(f"/api/rotas/{self.rota_id}/aceitar", json={}, headers=self._h(ORIGINAL)).status_code, 409)

        self._rodar("05:44")
        escolha = [e for e in self.enviados if e["tipo"] == "rota_escolhida"]
        self.assertEqual(len(escolha), 1)
        self.assertIsNone(escolha[0]["grupo_id"])           # grupo interno padrao
        self.assertIn(f"Motorista {OUTRO}", escolha[0]["texto"])
        self.assertLessEqual(len(escolha[0]["texto"]), 200)

        self._sync(agent_id=OUTRO)                           # a equipe trocou o motorista na Vuupt
        self.assertEqual(self._rodar("05:50")["aplicadas"], 1)
        self.assertEqual(self._oferta()["status"], "APLICADA")
        self.assertTrue(self.cli.get("/api/ofertas", headers=h_outro).get_json()["minhas"][0]["aplicada"])
        self.assertEqual(self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/cancelar", headers=h_outro).status_code, 409)

    def test_desistir_da_escolha_reabre_a_oferta(self):
        self._rodar("05:40")
        h = self._h(OUTRO)
        self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher", headers=h)
        r = self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/cancelar", headers=h)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(len(r.get_json()["abertas"]), 1)
        self.assertEqual(self._oferta()["status"], "ABERTA")

    def test_no_prazo_o_original_ainda_aceita_e_a_oferta_sai(self):
        self._rodar("05:40")
        h = self._h(ORIGINAL)
        hoje = HOJE.isoformat()
        self.assertEqual(len(self.cli.get(f"/api/rotas?de={hoje}&ate={hoje}", headers=h).get_json()["rotas"]), 1)
        self.assertEqual(self.cli.post(f"/api/rotas/{self.rota_id}/aceitar", json={}, headers=h).status_code, 200)
        self.assertEqual(self._oferta()["status"], "RETIRADA")
        self.assertEqual(self.cli.get("/api/ofertas", headers=self._h(OUTRO)).get_json()["abertas"], [])

    def test_rota_cancelada_ou_aceita_na_vuupt_retira_a_oferta(self):
        self._rodar("05:40")
        self.servicos = [_servico(9001, "accepted"), _servico(9002, "accepted")]
        self._sync(status="not_started")
        self.assertEqual(self._rodar("05:44")["retiradas"], 1)
        self.assertEqual(self._oferta()["motivo_retirada"], "motorista original aceitou")

    def test_aviso_vai_pro_grupo_rotas_freshlog_e_espera_se_nao_achar(self):
        self._rodar("05:40", grupos=[GRUPOS[0]])                 # numero fora do grupo: nada sai, fica pendente
        self.assertEqual(self.enviados, [])
        self.assertIsNone(self._oferta()["aviso_grupo_em"])
        self._rodar("05:42")
        self.assertEqual([(e["tipo"], e["grupo_id"]) for e in self.enviados], [("rota_disponivel", GRUPO_ROTAS_ID)])
        self.assertIsNotNone(self._oferta()["aviso_grupo_em"])

    def test_todo_motorista_com_app_ve_menos_o_original(self):
        self._rodar("05:40")
        self.assertEqual(sorted(json.loads(self._oferta()["elegiveis_json"])), [OUTRO, TERCEIRO])
        self.assertEqual(len(self.cli.get("/api/ofertas", headers=self._h(TERCEIRO)).get_json()["abertas"]), 1)
        self.assertEqual(self.cli.get("/api/ofertas", headers=self._h(ORIGINAL)).get_json()["abertas"], [])
        r = self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher", headers=self._h(ORIGINAL))
        self.assertEqual(r.status_code, 409)

    def test_fiorino_nao_pega_rota_de_van_hr_e_recebe_o_motivo(self):
        self._sql("UPDATE nucleo_rotas SET tipo_veiculo = 'VAN_HR' WHERE id = ?", (self.rota_id,))
        self._sql("UPDATE motoristas SET tipo_veiculo = 'VAN_HR' WHERE agent_id = ?", (TERCEIRO,))
        self._rodar("05:40")
        url = f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher"
        r = self.cli.post(url, headers=self._h(OUTRO))           # Fiorino
        self.assertEqual(r.status_code, 409)
        self.assertIn("VAN/HR", r.get_json()["erro"])
        self.assertIn("Fiorino", r.get_json()["erro"])
        self.assertEqual(self._oferta()["status"], "ABERTA")
        self.assertEqual(self.cli.post(url, headers=self._h(TERCEIRO)).status_code, 200)   # VAN/HR pega

    def _pagina(self):
        link = self.enviados[0]["texto"].rsplit(" ", 1)[-1]
        return self.cli.get("/abrir/ofertas/" + link.rsplit("/", 1)[-1])

    def test_pagina_da_oferta_mostra_coleta_pagamento_e_cadastro_sem_endereco(self):
        self._sql("UPDATE nucleo_paradas SET janela_inicio = '14:00' WHERE rota_id = ? AND ordem = (SELECT MAX(ordem) "
                  "FROM nucleo_paradas WHERE rota_id = ?)", (self.rota_id, self.rota_id))
        self._rodar("05:40")
        r = self._pagina()
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        for trecho in ("Rua Zilda, 288", "Casa Verde Alta", "06:00", "janela a partir das 14:00", "Alinhar no WhatsApp.",
                       "Valor aproximado", "Disponível", "freshlogmotorista://ofertas", "/cadastro"):
            self.assertIn(trecho, html)
        self.assertNotIn("Rua A, 1", html)                  # endereco de entrega nunca
        self.assertNotIn("PS-9001", html)

    def test_pagina_com_token_errado_da_404_e_escolhida_mostra_que_saiu(self):
        self._rodar("05:40")
        id_oferta = self._oferta()["id"]
        self.assertEqual(self.cli.get(f"/abrir/ofertas/{id_oferta}-0000000000000000").status_code, 404)
        self.assertEqual(self.cli.get(f"/abrir/ofertas/{id_oferta + 1}-{ofertas.token_oferta('segredo-de-teste', id_oferta).split('-')[1]}").status_code, 404)
        self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher", headers=self._h(OUTRO))
        html = self._pagina().get_data(as_text=True)
        self.assertIn("Já foi escolhida", html)
        self.assertNotIn("Pegar a rota no app", html)

    def test_pagina_do_link_abre_o_app(self):
        r = self.cli.get("/abrir/ofertas")
        self.assertEqual(r.status_code, 200)
        self.assertIn("freshlogmotorista://ofertas", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
