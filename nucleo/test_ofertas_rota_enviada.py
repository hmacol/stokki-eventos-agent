# -*- coding: utf-8 -*-
"""
test_ofertas_rota_enviada.py

Rota enviada sem aceite vira oferta no app (nucleo/ofertas_rota_enviada.py).
SQLite temporario, sem rede: elegibilidade, resumo e WhatsApp sao de mentira.
    python -m unittest nucleo.test_ofertas_rota_enviada -v
"""
import json
import sqlite3
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


def RESUMO(conn, rota, chave):
    return {"regiao": "BAIXADA SANTISTA", "total_paradas": 2, "saida": "06:00"}


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
        conn.close()
        self.enviados = []
        self.particulares = []          # (agent_id, texto) das mensagens particulares
        self.falha_para = set()         # agent_id cujo envio "falhou"
        self.andamento_quebra = False
        self.situacao_grupo = "enviado"   # o que o gateway devolve pro aviso do grupo Rotas FRESHLOG
        self.situacao_envio = None      # forca a situacao devolvida pelo envio (ex.: "nao_enviado")

    def tearDown(self):
        self._patch_db.stop()
        self._tmp.cleanup()

    # helpers -----------------------------------------------------------------
    def _h(self, agente):
        r = self.cli.post("/api/login", json={"cpf": CPFS[agente], "pin": PIN})
        return {"Authorization": "Bearer " + r.get_json()["acesso"]}

    def _despachar(self, config, origem, tipo, texto, assinatura=None, modo_teste=False, grupo_id=None, **kw):
        if tipo == "rota_disponivel" and self.situacao_grupo != "enviado":
            return self.situacao_grupo
        self.enviados.append({"tipo": tipo, "texto": texto, "grupo_id": grupo_id})
        return "enviado"

    def _enviar(self, oferta_id, agent_id, telefone, texto, config, modo_teste=False, **kw):
        if agent_id in self.falha_para:
            return "falhou"
        if self.situacao_envio:
            return self.situacao_envio
        self.particulares.append((agent_id, texto))
        return "enviado"

    def _atualizar(self, conn):
        if self.andamento_quebra:
            raise RuntimeError("Vuupt fora")

    def _etapas(self, posicoes=None):
        """Oferta da equipe com aviso do grupo as 10:00; OUTRO fixo e TERCEIRO esporadico, com telefone."""
        self._abrir("10:00")
        self._sql("UPDATE nucleo_ofertas_rota SET aviso_grupo_em = ?", (_as("10:00").strftime("%Y-%m-%d %H:%M:%S"),))
        self._sql("UPDATE motoristas SET telefone = '11999990000' WHERE agent_id IN (?, ?)", (OUTRO, TERCEIRO))
        for agente, posicao in (posicoes or {OUTRO: "FIXO", TERCEIRO: "ESPORADICO"}).items():
            self._sql("INSERT INTO bd_motoristas (AGENT_ID_VUUPT, NOME_MOTORISTA, POSICAO, criado_em, atualizado_em) VALUES (?, 'M', ?, datetime('now'), datetime('now'))", (agente, posicao))

    def _rota_de_hoje(self, agente, status, pendentes):
        conn = banco.conectar()
        rid = conn.execute("INSERT INTO nucleo_rotas (data_rota, nome, provedor, agent_id, status) VALUES (?, 'outra', 'VUUPT', ?, ?)",
                           (HOJE.isoformat(), agente, status)).lastrowid
        for i in range(4):
            conn.execute("INSERT INTO nucleo_paradas (rota_id, ordem, situacao) VALUES (?, ?, ?)",
                         (rid, i, "PENDENTE" if i < pendentes else "ENTREGUE"))
        conn.commit()
        conn.close()

    def _quem(self):
        return [a for a, _ in self.particulares]

    def _mensagens(self):
        conn = banco.conectar()
        try:
            return {r["agent_id"]: (r["etapa"], r["situacao"]) for r in conn.execute("SELECT * FROM nucleo_ofertas_rota_mensagens")}
        finally:
            conn.close()

    def _rodar(self, hora, config=None, grupos=GRUPOS):
        conn = banco.conectar()
        try:
            return ofertas.rodar(conn, config or self.config, agora=_as(hora),
                                 resumo=RESUMO,
                                 despachar=self._despachar, listar_grupos=lambda cfg: grupos,
                                 enviar=self._enviar, atualizar_andamento=self._atualizar)
        finally:
            conn.close()

    def _ofertar(self, usuario="hugo"):
        conn = banco.conectar()
        try:
            return ofertas.ofertar_pela_equipe(conn, self.rota_id, usuario, None, resumo=RESUMO)
        finally:
            conn.close()

    def _abrir(self, hora="10:00"):
        """A equipe oferta e a rodada manda o aviso do grupo."""
        self.assertTrue(self._ofertar()["ok"])
        return self._rodar(hora)

    def _recusar(self):
        self.cli.post(f"/api/rotas/{self.rota_id}/recusar", json={"motivo": "carro quebrou"}, headers=self._h(ORIGINAL))

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
        self._recusar()
        desligado = {"api_motorista": {**self.config["api_motorista"], "rota_sem_aceite": {"ativo": False}}}
        self.assertEqual(self._rodar("05:40", config=desligado)["publicadas"], 0)
        fora = {"api_motorista": {**self.config["api_motorista"], "ponte_vuupt": {"ativo": True, "agentes": [999]}}}
        self.assertEqual(self._rodar("05:40", config=fora)["publicadas"], 0)
        todos = {"api_motorista": {**fora["api_motorista"], "rota_sem_aceite": {"ativo": True, "todos": True}}}
        self.assertEqual(self._rodar("05:40", config=todos)["publicadas"], 1)

    def test_nunca_abre_por_horario_so_por_recusa(self):
        for hora in ("05:29", "05:30", "09:00", "23:59"):
            self.assertEqual(self._rodar(hora)["publicadas"], 0)
        self.assertIsNone(self._oferta())
        self._recusar()
        self.assertEqual(self._rodar("09:02")["publicadas"], 1)
        self.assertEqual(self._oferta()["motivo"], "RECUSA")
        self.assertEqual(self._rodar("09:04")["publicadas"], 0)          # uma oferta por rota
        self.assertEqual([e["tipo"] for e in self.enviados], ["rota_disponivel"])   # um aviso so

    def test_equipe_oferta_rota_enviada_e_o_aviso_sai_no_grupo(self):
        r = self._ofertar()
        self.assertTrue(r["ok"], r)
        o = self._oferta()
        self.assertEqual((o["motivo"], o["status"], o["ofertada_por"], r["oferta_id"]), ("EQUIPE", "ABERTA", "hugo", o["id"]))
        self._rodar("10:00")
        self.assertEqual([e["tipo"] for e in self.enviados], ["rota_disponivel"])

    def test_oferta_agendada_so_abre_e_avisa_na_hora_escolhida(self):
        conn = banco.conectar()
        r = ofertas.ofertar_pela_equipe(conn, self.rota_id, "hugo", None, resumo=RESUMO,
                                        publicar_em=_as("18:30"), agora=_as("10:00"))
        conn.close()
        self.assertEqual((r["ok"], r["agendada_para"]), (True, "18:30"), r)
        self.assertEqual(self._oferta()["status"], "AGENDADA")
        self._rodar("18:00")
        self.assertEqual((self._oferta()["status"], self.enviados), ("AGENDADA", []))     # nada no app nem no grupo
        self._rodar("18:31")
        self.assertEqual(self._oferta()["status"], "ABERTA")
        self.assertEqual([e["tipo"] for e in self.enviados], ["rota_disponivel"])

    def test_agendada_pode_ser_cancelada_e_nao_abre(self):
        conn = banco.conectar()
        ofertas.ofertar_pela_equipe(conn, self.rota_id, "hugo", None, resumo=RESUMO, publicar_em=_as("18:30"), agora=_as("10:00"))
        self.assertTrue(ofertas.retirar_pela_equipe(conn, self.rota_id, "hugo")["ok"])
        conn.close()
        self._rodar("19:00")
        self.assertEqual((self._oferta()["status"], self.enviados), ("RETIRADA", []))

    def test_agendada_com_rota_cancelada_ate_la_e_retirada(self):
        conn = banco.conectar()
        ofertas.ofertar_pela_equipe(conn, self.rota_id, "hugo", None, resumo=RESUMO, publicar_em=_as("18:30"), agora=_as("10:00"))
        conn.close()
        self._sql("UPDATE nucleo_rotas SET status = 'CANCELADA' WHERE id = ?", (self.rota_id,))
        self._rodar("18:31")
        self.assertEqual((self._oferta()["status"], self.enviados), ("RETIRADA", []))

    def test_equipe_nao_oferta_duas_vezes_nem_rota_iniciada_ou_passada(self):
        self.assertTrue(self._ofertar()["ok"])
        self.assertIn("já foi ofertada", self._ofertar()["erro"])
        self._sql("DELETE FROM nucleo_ofertas_rota")
        for status in ("EM_ROTA", "CONCLUIDA", "CANCELADA"):
            self._sql("UPDATE nucleo_rotas SET status = ? WHERE id = ?", (status, self.rota_id))
            self.assertFalse(self._ofertar()["ok"], status)
        self._sql("UPDATE nucleo_rotas SET status = 'PLANEJADA', data_rota = ? WHERE id = ?",
                  ((HOJE - timedelta(days=1)).isoformat(), self.rota_id))
        self.assertFalse(self._ofertar()["ok"])
        conn = banco.conectar()
        self.assertFalse(ofertas.ofertar_pela_equipe(conn, 99999, "hugo", None, resumo=RESUMO)["ok"])
        conn.close()
        self.assertIsNone(self._oferta())

    def test_oferta_da_equipe_nao_cai_quando_a_rota_ja_estava_aceita(self):
        h = self._h(ORIGINAL)
        self.assertEqual(self.cli.post(f"/api/rotas/{self.rota_id}/aceitar", json={}, headers=h).status_code, 200)
        self._abrir()
        self._rodar("10:02")
        self.assertEqual(self._oferta()["status"], "ABERTA")
        hoje = HOJE.isoformat()
        self.assertEqual(len(self.cli.get(f"/api/rotas?de={hoje}&ate={hoje}", headers=h).get_json()["rotas"]), 1)   # segue dele
        self.assertEqual(len(self.cli.get("/api/ofertas", headers=self._h(OUTRO)).get_json()["abertas"]), 1)

    def test_original_aceitar_nao_retira_a_oferta_da_equipe(self):
        self._abrir()
        self.assertEqual(self.cli.post(f"/api/rotas/{self.rota_id}/aceitar", json={}, headers=self._h(ORIGINAL)).status_code, 200)
        self._rodar("10:02")
        self.assertEqual(self._oferta()["status"], "ABERTA")

    def test_equipe_retira_oferta_aberta_e_nao_retira_escolhida_nem_recusa(self):
        self._abrir()
        conn = banco.conectar()
        self.assertTrue(ofertas.retirar_pela_equipe(conn, self.rota_id, "ana")["ok"])
        o = self._oferta()
        self.assertEqual(o["status"], "RETIRADA")
        self.assertIn("ana", o["motivo_retirada"])
        self.assertEqual(self.cli.get("/api/ofertas", headers=self._h(OUTRO)).get_json()["abertas"], [])
        self.assertFalse(self._ofertar()["ok"])                          # retirada nao reabre
        self.assertFalse(ofertas.retirar_pela_equipe(conn, self.rota_id, "ana")["ok"])
        conn.execute("UPDATE nucleo_ofertas_rota SET status = 'ESCOLHIDA', escolhido_por = ?", (OUTRO,))
        conn.commit()
        self.assertFalse(ofertas.retirar_pela_equipe(conn, self.rota_id, "ana")["ok"])
        conn.execute("UPDATE nucleo_ofertas_rota SET status = 'ABERTA', escolhido_por = NULL, motivo = 'RECUSA'")
        conn.commit()
        self.assertFalse(ofertas.retirar_pela_equipe(conn, self.rota_id, "ana")["ok"])
        conn.close()

    def test_aceite_no_app_sem_recusa_nao_publica(self):
        self.cli.post(f"/api/rotas/{self.rota_id}/aceitar", json={}, headers=self._h(ORIGINAL))
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
        self._abrir()
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
        self._abrir()
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
        self._abrir()
        h = self._h(OUTRO)
        self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher", headers=h)
        r = self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/cancelar", headers=h)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(len(r.get_json()["abertas"]), 1)
        self.assertEqual(self._oferta()["status"], "ABERTA")

    def test_rota_cancelada_ou_iniciada_retira_a_oferta(self):
        self._abrir()
        self._sql("UPDATE nucleo_rotas SET status = 'EM_ROTA' WHERE id = ?", (self.rota_id,))
        self.assertEqual(self._rodar("10:04")["retiradas"], 1)
        self.assertEqual(self._oferta()["motivo_retirada"], "rota em_rota")

    def test_aviso_vai_pro_grupo_rotas_freshlog_e_espera_se_nao_achar(self):
        self._ofertar()
        self._rodar("05:40", grupos=[GRUPOS[0]])                 # numero fora do grupo: nada sai, fica pendente
        self.assertEqual(self.enviados, [])
        self.assertIsNone(self._oferta()["aviso_grupo_em"])
        self._rodar("05:42")
        self.assertEqual([(e["tipo"], e["grupo_id"]) for e in self.enviados], [("rota_disponivel", GRUPO_ROTAS_ID)])
        self.assertIsNotNone(self._oferta()["aviso_grupo_em"])

    def test_todo_motorista_com_app_ve_menos_o_original(self):
        self._abrir()
        self.assertEqual(sorted(json.loads(self._oferta()["elegiveis_json"])), [OUTRO, TERCEIRO])
        self.assertEqual(len(self.cli.get("/api/ofertas", headers=self._h(TERCEIRO)).get_json()["abertas"]), 1)
        self.assertEqual(self.cli.get("/api/ofertas", headers=self._h(ORIGINAL)).get_json()["abertas"], [])
        r = self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher", headers=self._h(ORIGINAL))
        self.assertEqual(r.status_code, 409)

    def test_fiorino_nao_pega_rota_de_van_hr_e_recebe_o_motivo(self):
        self._sql("UPDATE nucleo_rotas SET tipo_veiculo = 'VAN_HR' WHERE id = ?", (self.rota_id,))
        self._sql("UPDATE motoristas SET tipo_veiculo = 'VAN_HR' WHERE agent_id = ?", (TERCEIRO,))
        self._abrir()
        url = f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher"
        r = self.cli.post(url, headers=self._h(OUTRO))           # Fiorino
        self.assertEqual(r.status_code, 409)
        self.assertIn("VAN/HR", r.get_json()["erro"])
        self.assertIn("Fiorino", r.get_json()["erro"])
        self.assertEqual(self._oferta()["status"], "ABERTA")
        self.assertEqual(self.cli.post(url, headers=self._h(TERCEIRO)).status_code, 200)   # VAN/HR pega

    # confirmar ou recusar quem pegou (Hugo, 21/11) -----------------------------
    def test_confirmar_passa_a_rota_pro_novo_motorista_no_app_na_hora(self):
        self.cli.post(f"/api/rotas/{self.rota_id}/aceitar", json={}, headers=self._h(ORIGINAL))
        self._abrir()
        self._sql("UPDATE motoristas SET vehicle_id = 77 WHERE agent_id = ?", (OUTRO,))
        self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher", headers=self._h(OUTRO))
        conn = banco.conectar()
        self.assertEqual(ofertas.escolha_pendente(conn, self.rota_id),
                         {"agent_id": OUTRO, "vehicle_id": 77, "nome": f"Motorista {OUTRO}"})
        ofertas.aplicar_escolha(conn, self.rota_id, "hugo")
        rota = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (self.rota_id,)).fetchone()
        self.assertEqual((rota["agent_id"], rota["vehicle_id"], rota["motorista_nome"], rota["status"]),
                         (OUTRO, 77, f"Motorista {OUTRO}", "PLANEJADA"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM nucleo_eventos WHERE rota_id = ? AND tipo = 'ROTA_TROCADA_OFERTA'",
                                      (self.rota_id,)).fetchone()[0], 1)
        conn.close()
        self.assertEqual(self._oferta()["status"], "APLICADA")
        hoje = HOJE.isoformat()
        self.assertEqual(len(self.cli.get(f"/api/rotas?de={hoje}&ate={hoje}", headers=self._h(OUTRO)).get_json()["rotas"]), 1)
        self.assertEqual(self.cli.get(f"/api/rotas?de={hoje}&ate={hoje}", headers=self._h(ORIGINAL)).get_json()["rotas"], [])
        self._rodar("10:10")                                             # a rodada nao desfaz
        self.assertEqual(self._oferta()["status"], "APLICADA")

    def test_confirmar_aplica_quem_foi_pra_vuupt_mesmo_se_ele_desistir_no_meio(self):
        # revisao 21/11: a Vuupt ja trocou pro OUTRO; ele desiste e o TERCEIRO pega antes de o nucleo gravar
        self._abrir()
        url = f"/api/ofertas/{ID_OFERTA + self.rota_id}"
        self.cli.post(url + "/escolher", headers=self._h(OUTRO))
        conn = banco.conectar()
        escolha = ofertas.escolha_pendente(conn, self.rota_id)
        self.cli.post(url + "/cancelar", headers=self._h(OUTRO))
        self.cli.post(url + "/escolher", headers=self._h(TERCEIRO))
        ofertas.aplicar_escolha(conn, self.rota_id, "hugo", escolha)
        rota = conn.execute("SELECT agent_id FROM nucleo_rotas WHERE id = ?", (self.rota_id,)).fetchone()
        conn.close()
        self.assertEqual(rota["agent_id"], OUTRO)                         # igual a Vuupt
        o = self._oferta()
        self.assertEqual((o["status"], o["escolhido_por"]), ("APLICADA", OUTRO))
        self.assertEqual(self.cli.get("/api/ofertas", headers=self._h(TERCEIRO)).get_json()["minhas"], [])

    def _confirmar(self, trocar):
        conn = banco.conectar()
        try:
            return ofertas.confirmar_escolha(conn, self.rota_id, "hugo", trocar)
        finally:
            conn.close()

    def _agente_da_rota(self):
        conn = banco.conectar()
        try:
            return conn.execute("SELECT agent_id FROM nucleo_rotas WHERE id = ?", (self.rota_id,)).fetchone()["agent_id"]
        finally:
            conn.close()

    def test_confirmar_troca_na_vuupt_antes_e_so_depois_passa_no_app(self):
        self._abrir()
        self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher", headers=self._h(OUTRO))
        vistos = []

        def trocar(escolha):
            vistos.append((escolha["agent_id"], self._agente_da_rota()))     # na hora da Vuupt o app ainda nao mudou
            return {"aviso": "sem carro"}
        r = self._confirmar(trocar)
        self.assertEqual(r, {"ok": True, "motorista": f"Motorista {OUTRO}", "aviso": "sem carro"})
        self.assertEqual(vistos, [(OUTRO, ORIGINAL)])
        self.assertEqual((self._agente_da_rota(), self._oferta()["status"]), (OUTRO, "APLICADA"))

    def test_vuupt_recusa_a_troca_e_nada_muda(self):
        self._abrir()
        self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher", headers=self._h(OUTRO))

        def trocar(escolha):
            raise ValueError("Rota já começou: só o carro pode mudar.")
        r = self._confirmar(trocar)
        self.assertEqual(r, {"ok": False, "erro": "Rota já começou: só o carro pode mudar."})
        self.assertEqual((self._agente_da_rota(), self._oferta()["status"]), (ORIGINAL, "ESCOLHIDA"))
        self.assertTrue(self._confirmar(lambda e: {})["ok"])                 # clicar de novo funciona

    def test_confirmar_sem_escolha_nem_chama_a_vuupt(self):
        self._abrir()
        chamadas = []
        r = self._confirmar(lambda e: chamadas.append(e))
        self.assertFalse(r["ok"])
        self.assertEqual(chamadas, [])

    def test_banco_ocupado_depois_da_vuupt_tenta_de_novo_e_avisa_se_nao_der(self):
        # revisao 21/11: "database is locked" depois de a Vuupt trocar nao pode virar erro mudo
        self._abrir()
        self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher", headers=self._h(OUTRO))
        real = ofertas.aplicar_escolha
        ocupado = sqlite3.OperationalError("database is locked")
        with mock.patch.object(ofertas, "aplicar_escolha", side_effect=[ocupado, ocupado]):
            r = self._confirmar(lambda e: {})
        self.assertFalse(r["ok"])
        self.assertIn("Confirmar motorista de novo", r["erro"])
        falhas = [ocupado]                                                 # so a 1a tentativa falha

        def uma_falha(*a, **kw):
            if falhas:
                raise falhas.pop()
            return real(*a, **kw)
        with mock.patch.object(ofertas, "aplicar_escolha", side_effect=uma_falha):
            r = self._confirmar(lambda e: {})
        self.assertTrue(r["ok"], r)
        self.assertEqual((self._agente_da_rota(), self._oferta()["status"]), (OUTRO, "APLICADA"))

    def test_erro_inesperado_na_troca_avisa_pra_conferir_o_card(self):
        self._abrir()
        self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher", headers=self._h(OUTRO))

        def trocar(escolha):
            raise sqlite3.OperationalError("database is locked")              # rascunho local nao gravou
        r = self._confirmar(trocar)
        self.assertFalse(r["ok"])
        self.assertIn("confira o motorista no card", r["erro"])
        self.assertEqual(self._oferta()["status"], "ESCOLHIDA")

    def test_recusar_reabre_e_o_recusado_nao_ve_mais(self):
        self._abrir()
        url = f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher"
        self.cli.post(url, headers=self._h(OUTRO))
        conn = banco.conectar()
        self.assertTrue(ofertas.recusar_escolha(conn, self.rota_id, "hugo")["ok"])
        self.assertFalse(ofertas.recusar_escolha(conn, self.rota_id, "hugo")["ok"])      # ja nao ha escolha
        conn.close()
        o = self._oferta()
        self.assertEqual((o["status"], o["escolhido_por"]), ("ABERTA", None))
        self.assertEqual(self.cli.get("/api/ofertas", headers=self._h(OUTRO)).get_json(), {"abertas": [], "minhas": []})
        self.assertEqual(self.cli.post(url, headers=self._h(OUTRO)).status_code, 409)
        self.assertEqual(self.cli.post(url, headers=self._h(TERCEIRO)).status_code, 200)

    def test_aplicar_sem_escolha_nao_faz_nada(self):
        self._abrir()
        conn = banco.conectar()
        self.assertIsNone(ofertas.escolha_pendente(conn, self.rota_id))
        with self.assertRaises(ValueError):
            ofertas.aplicar_escolha(conn, self.rota_id, "hugo")
        conn.close()
        self.assertEqual(self._oferta()["status"], "ABERTA")

    def test_aviso_da_escolha_manda_confirmar_no_card(self):
        self._abrir()
        self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher", headers=self._h(OUTRO))
        self._rodar("10:02")
        texto = [e for e in self.enviados if e["tipo"] == "rota_escolhida"][0]["texto"]
        self.assertIn("Confirme no card da rota, no planejamento.", texto)
        self.assertNotIn("Vuupt", texto)
        self.assertLessEqual(len(texto), 200)

    def test_grupo_primeiro_sem_aviso_no_grupo_ninguem_recebe_particular(self):
        # revisao 21/11: teto do dia ou canal em pausa no aviso do grupo nao conta como "ja avisado"
        self._sql("UPDATE motoristas SET telefone = '11999990000' WHERE agent_id IN (?, ?)", (OUTRO, TERCEIRO))
        self._ofertar()
        for situacao in ("nao_enviado", "desligado", "falhou"):
            self.situacao_grupo = situacao
            self.assertEqual(self._rodar("23:59")["mensagens"], 0, situacao)
            self.assertIsNone(self._oferta()["aviso_grupo_em"], situacao)
        self.assertEqual(self._quem(), [])
        self.situacao_grupo = "enviado"
        self._rodar("23:59")
        self.assertEqual([e["tipo"] for e in self.enviados], ["rota_disponivel"])
        self.assertIsNotNone(self._oferta()["aviso_grupo_em"])

    def test_aviso_do_grupo_atrasado_nao_sai_pra_rota_de_ontem(self):
        self._ofertar()
        self.situacao_grupo = "nao_enviado"
        self._rodar("23:59")
        self._sql("UPDATE nucleo_ofertas_rota SET data_rota = ?", ((HOJE - timedelta(days=1)).isoformat(),))
        self.situacao_grupo = "enviado"
        self._rodar("00:01")
        self.assertEqual(self.enviados, [])

    # etapas por mensagem particular (Hugo, 21/11) ------------------------------
    def test_nada_antes_de_15_min_do_grupo(self):
        self._etapas()
        self.assertEqual(self._rodar("10:14")["mensagens"], 0)
        self.assertEqual(self._quem(), [])

    def test_fixo_antes_do_esporadico_com_10_min(self):
        self._etapas()
        self.assertEqual(self._rodar("10:15")["mensagens"], 1)
        self.assertEqual(self._quem(), [OUTRO])
        self._rodar("10:24")
        self.assertEqual(self._quem(), [OUTRO])
        self._rodar("10:25")
        self.assertEqual(self._quem(), [OUTRO, TERCEIRO])
        self._rodar("10:40")
        self.assertEqual(self._quem(), [OUTRO, TERCEIRO])                 # cada um recebe uma vez
        self.assertEqual(self._mensagens(), {OUTRO: ("FIXO", "enviado"), TERCEIRO: ("ESPORADICO", "enviado")})

    def test_perto_do_fim_vem_primeiro(self):
        self._etapas()
        self._rota_de_hoje(TERCEIRO, "EM_ROTA", pendentes=2)             # esporadico, mas quase livre
        self._rodar("10:15")
        self.assertEqual(self._quem(), [TERCEIRO])
        self._rodar("10:20")
        self.assertEqual(self._quem(), [TERCEIRO])
        self._rodar("10:25")
        self.assertEqual(self._quem(), [TERCEIRO, OUTRO])
        self.assertEqual(self._mensagens()[TERCEIRO][0], "PERTO_DO_FIM")

    def test_tres_paradas_pendentes_nao_e_perto_do_fim_e_concluida_e(self):
        self._etapas()
        self._rota_de_hoje(TERCEIRO, "EM_ROTA", pendentes=3)
        self._rodar("10:15")
        self.assertEqual(self._quem(), [OUTRO])                           # etapa vazia pulada: fixo ja aos 15 min
        conn = banco.conectar()
        self.assertEqual(ofertas.etapa_do_motorista(conn, TERCEIRO, HOJE), "ESPORADICO")
        conn.execute("UPDATE nucleo_rotas SET status = 'CONCLUIDA' WHERE agent_id = ?", (TERCEIRO,))
        self.assertEqual(ofertas.etapa_do_motorista(conn, TERCEIRO, HOJE), "PERTO_DO_FIM")
        self.assertEqual(ofertas.etapa_do_motorista(conn, 777, HOJE), "ESPORADICO")      # sem cadastro
        conn.close()

    def test_para_quando_alguem_pega(self):
        self._etapas()
        self._rodar("10:15")
        self.cli.post(f"/api/ofertas/{ID_OFERTA + self.rota_id}/escolher", headers=self._h(OUTRO))
        self._rodar("10:25")
        self._rodar("10:40")
        self.assertEqual(self._quem(), [OUTRO])

    def test_sem_telefone_registra_e_nao_trava(self):
        self._etapas()
        self._sql("UPDATE motoristas SET telefone = NULL WHERE agent_id = ?", (OUTRO,))
        self._rodar("10:15")
        self.assertEqual(self._quem(), [])
        self.assertEqual(self._mensagens(), {OUTRO: ("FIXO", "sem_telefone")})
        self._rodar("10:25")
        self.assertEqual(self._quem(), [TERCEIRO])

    def test_telefone_do_cadastro_serve_quando_o_login_nao_tem(self):
        self._etapas()
        self._sql("UPDATE motoristas SET telefone = NULL WHERE agent_id = ?", (OUTRO,))
        self._sql("UPDATE bd_motoristas SET TELEFONE_MOTORISTA = '11988887777' WHERE AGENT_ID_VUUPT = ?", (OUTRO,))
        self._rodar("10:15")
        self.assertEqual(self._quem(), [OUTRO])

    def test_falha_tenta_de_novo_so_quem_falhou(self):
        self._etapas({OUTRO: "FIXO", TERCEIRO: "FIXO"})
        self.falha_para = {TERCEIRO}
        self._rodar("10:15")
        self.assertEqual(self._quem(), [OUTRO])
        self.falha_para = set()
        self._rodar("10:17")
        self.assertEqual(self._quem(), [OUTRO, TERCEIRO])

    def test_canal_em_pausa_ou_desligado_nao_queima_o_motorista(self):
        # revisao 21/11: nao_enviado (teto/canal em pausa) e desligado nao sao definitivos
        self._etapas()
        for situacao in ("nao_enviado", "desligado"):
            self.situacao_envio = situacao
            self.assertEqual(self._rodar("10:15")["mensagens"], 0)
            self.assertEqual(self._mensagens(), {})
        self.situacao_envio = None
        self._rodar("10:17")
        self.assertEqual(self._quem(), [OUTRO])                           # recebe quando o canal volta

    def test_falha_permanente_desiste_depois_de_3_e_nao_trava_as_etapas(self):
        self._etapas()
        self.falha_para = {OUTRO}
        for hora in ("10:15", "10:17", "10:19"):
            self._rodar(hora)
        self.assertEqual(self._mensagens(), {OUTRO: ("FIXO", "falhou")})
        self._rodar("10:21")                                             # 4a rodada: nao tenta mais o OUTRO
        self._rodar("10:25")                                             # 10 min depois da abertura dos fixos
        self.assertEqual(self._quem(), [TERCEIRO])

    def test_tentativa_que_falha_conta_no_limite_da_rodada(self):
        self._etapas()
        conn = banco.conectar()
        for i in range(7):
            auth.criar_ou_atualizar_motorista(conn, f"5234567890{i}", f"Extra {i}", PIN, agent_id=6000 + i, tipo_veiculo="FIORINO")
            conn.execute("UPDATE motoristas SET telefone = '11999990000' WHERE agent_id = ?", (6000 + i,))
            conn.execute("INSERT INTO bd_motoristas (AGENT_ID_VUUPT, NOME_MOTORISTA, POSICAO, criado_em, atualizado_em) "
                         "VALUES (?, 'X', 'FIXO', datetime('now'), datetime('now'))", (6000 + i,))
        conn.commit()
        conn.close()
        self.falha_para = {OUTRO} | {6000 + i for i in range(7)}
        tentativas = []
        original = self._enviar
        self._enviar = lambda *a, **kw: (tentativas.append(a[1]), original(*a, **kw))[1]
        self._rodar("10:15")
        self.assertEqual(len(tentativas), 5)

    def test_vuupt_fora_segue_com_o_nucleo(self):
        self._etapas()
        self.andamento_quebra = True
        self.assertEqual(self._rodar("10:15")["mensagens"], 1)

    def test_sync_que_quebra_no_meio_nao_deixa_escrita_pela_metade(self):
        # revisao 21/11: o sync parcial nao pode ser gravado pelo commit da mensagem seguinte
        self._etapas()

        def quebra_no_meio(conn):
            conn.execute("UPDATE nucleo_rotas SET nome = 'PELA METADE' WHERE id = ?", (self.rota_id,))
            raise RuntimeError("Vuupt caiu no meio")
        self._atualizar = quebra_no_meio
        self.assertEqual(self._rodar("10:15")["mensagens"], 1)
        conn = banco.conectar()
        nome = conn.execute("SELECT nome FROM nucleo_rotas WHERE id = ?", (self.rota_id,)).fetchone()["nome"]
        conn.close()
        self.assertNotEqual(nome, "PELA METADE")

    def test_veiculo_barrado_e_original_nao_recebem(self):
        self._etapas()
        self._sql("UPDATE motoristas SET telefone = '11999990000' WHERE agent_id = ?", (ORIGINAL,))
        self._sql("UPDATE nucleo_rotas SET tipo_veiculo = 'VAN_HR' WHERE id = ?", (self.rota_id,))
        self._sql("UPDATE motoristas SET tipo_veiculo = 'VAN_HR' WHERE agent_id = ?", (TERCEIRO,))
        self._rodar("10:15")
        self._rodar("10:25")
        self._rodar("10:35")
        self.assertEqual(self._quem(), [TERCEIRO])                        # OUTRO e Fiorino; ORIGINAL e o dono

    def test_no_maximo_5_mensagens_por_rodada(self):
        self._etapas()
        conn = banco.conectar()
        for i in range(7):
            auth.criar_ou_atualizar_motorista(conn, f"5234567890{i}", f"Extra {i}", PIN, agent_id=6000 + i, tipo_veiculo="FIORINO")
            conn.execute("UPDATE motoristas SET telefone = '11999990000' WHERE agent_id = ?", (6000 + i,))
            conn.execute("INSERT INTO bd_motoristas (AGENT_ID_VUUPT, NOME_MOTORISTA, POSICAO, criado_em, atualizado_em) VALUES (?, 'X', 'FIXO', datetime('now'), datetime('now'))", (6000 + i,))
        conn.commit()
        conn.close()
        self.assertEqual(self._rodar("10:15")["mensagens"], 5)
        self.assertEqual(self._rodar("10:17")["mensagens"], 3)             # resto dos 8 fixos, sem esperar os 10 min
        self.assertEqual(self._rodar("10:19")["mensagens"], 0)             # esporadico so 10 min depois de abrir os fixos
        self.assertEqual(self._rodar("10:25")["mensagens"], 1)

    def test_texto_particular_cabe_em_200_e_leva_o_link(self):
        self._etapas()
        self._rodar("10:15")
        texto = self.particulares[0][1]
        self.assertLessEqual(len(texto), 200)
        self.assertTrue(texto.startswith("Olá, Motorista!"))
        self.assertIn("2 entregas", texto)
        self.assertTrue(texto.endswith(f"{ofertas.LINK_PADRAO}/{ofertas.token_oferta('segredo-de-teste', self._oferta()['id'])}"))
        oferta = {"data_rota": "2026-11-17", "resumo_json": '{"regiao": "%s", "total_paradas": 11, "saida": "06:00"}' % ("Campinas, " * 40)}
        longo = ofertas.texto_particular(oferta, "Bartolomeu Maximiliano", 1234.5, ofertas.LINK_PADRAO)
        self.assertLessEqual(len(longo), 200)
        self.assertTrue(longo.endswith(ofertas.LINK_PADRAO))

    def test_modo_teste_nao_grava_nem_manda_particular(self):
        self._etapas()
        conn = banco.conectar()
        ofertas.rodar(conn, self.config, agora=_as("10:30"), modo_teste=True, resumo=RESUMO, despachar=self._despachar,
                      listar_grupos=lambda cfg: GRUPOS, enviar=self._enviar, atualizar_andamento=self._atualizar)
        conn.close()
        self.assertEqual(self._mensagens(), {})
        self.assertEqual(self._quem(), [])

    def _pagina(self):
        link = self.enviados[0]["texto"].rsplit(" ", 1)[-1]
        return self.cli.get("/abrir/ofertas/" + link.rsplit("/", 1)[-1])

    def test_pagina_da_oferta_mostra_coleta_pagamento_e_cadastro_sem_endereco(self):
        self._sql("UPDATE nucleo_paradas SET janela_inicio = '14:00' WHERE rota_id = ? AND ordem = (SELECT MAX(ordem) "
                  "FROM nucleo_paradas WHERE rota_id = ?)", (self.rota_id, self.rota_id))
        self._abrir()
        r = self._pagina()
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        for trecho in ("Rua Zilda, 288", "Casa Verde Alta", "06:00", "janela a partir das 14:00", "Alinhar no WhatsApp.",
                       "Valor aproximado", "Disponível", "freshlogmotorista://ofertas", "/cadastro"):
            self.assertIn(trecho, html)
        self.assertNotIn("Rua A, 1", html)                  # endereco de entrega nunca
        self.assertNotIn("PS-9001", html)

    def test_pagina_com_token_errado_da_404_e_escolhida_mostra_que_saiu(self):
        self._abrir()
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
