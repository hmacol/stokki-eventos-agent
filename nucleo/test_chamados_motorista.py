# -*- coding: utf-8 -*-
"""
test_chamados_motorista.py

Testes do chat do motorista (aba Ajuda do app -- Hugo, 12/09/2026):
isolamento entre motoristas, triagem do assistente (modelo simulado),
escalada pra logística dentro e fora do horário, resposta da equipe
chegando no app, assunto urgente pulando a triagem e a fila do painel
separando motorista de cliente. Sem rede, sem e-mail, sem Claude:
SQLite temporário e o cliente Anthropic trocado por um duplo.
    python -m unittest nucleo.test_chamados_motorista -v
"""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "portal_cliente"))

import chamados as ch  # noqa: E402  (portal_cliente/chamados.py)
from nucleo import api_motorista, assistente_motorista, auth_motorista as auth, banco, rotas  # noqa: E402

CPF, PIN, AGENT = "12345678901", "123456", 4242
CPF2, PIN2, AGENT2 = "99999999999", "654321", 777


def _rascunho(rascunho_id=1, agent_id=AGENT, data=None):
    data = data or date.today().isoformat()
    return {
        "id": rascunho_id, "data_alvo": data, "nome": f"Planejamento - #{rascunho_id}", "agent_id": agent_id,
        "motorista_nome": "Teste", "start_at": f"{data} 07:00:00", "km_estimado": 50.0, "status": "RASCUNHO",
        "paradas": [
            {"ordem": 1, "codigo": "PS-100", "titulo": "A", "destinatario_nome": "MERCADO CENTRAL",
             "endereco": "Rua A, 1", "latitude": -23.50, "longitude": -46.60, "volume_caixas": 4},
            {"ordem": 2, "codigo": "PS-200", "titulo": "B", "destinatario_nome": "PADARIA SOL",
             "endereco": "Rua B, 2", "latitude": -23.55, "longitude": -46.65, "volume_caixas": 2},
        ],
    }


class _RespostaFake:
    """Imita o retorno de messages.create com output_config json_schema."""
    def __init__(self, dados: dict):
        self.stop_reason = "end_turn"
        self.content = [mock.Mock(type="text", text=json.dumps(dados, ensure_ascii=False))]


class TestChatMotorista(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        raiz = Path(self._tmp.name)
        db = raiz / "t.db"
        # O núcleo e o atendimento moram no MESMO dados.db
        self._patches = [mock.patch.object(banco, "DB_PATH", db),
                         mock.patch.object(ch, "DB_PATH", db),
                         mock.patch.object(ch, "PASTA_ANEXOS", raiz / "chamados")]
        for p in self._patches:
            p.start()
        # E-mail nunca sai no teste: em_segundo_plano vira registro do que iria
        self.emails: list[tuple] = []
        self._patch_bg = mock.patch.object(ch, "em_segundo_plano",
                                           lambda fn, *a, **k: self.emails.append((fn.__name__, a, k)))
        self._patch_bg.start()

        self.config = {"api_motorista": {"secret_key": "segredo"},
                       "anthropic": {"api_key": "chave-de-teste"},
                       "portal_cliente": {"chamados": {"email_logistica": "logistica@freshlogbr.com",
                                                       "email_atendimento": "entregas@freshlogbr.com"}}}
        self.app = api_motorista.criar_app(self.config)
        self.cli = self.app.test_client()
        conn = banco.conectar()
        auth.criar_ou_atualizar_motorista(conn, CPF, "João da Silva", PIN, agent_id=AGENT, tipo_veiculo="FIORINO")
        auth.criar_ou_atualizar_motorista(conn, CPF2, "Outro Motorista", PIN2, agent_id=AGENT2)
        rotas.materializar_rascunho(_rascunho(), banco.PROVEDOR_APP, conn=conn)
        conn.close()

    def tearDown(self):
        self._patch_bg.stop()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    # helpers -----------------------------------------------------------------
    def _auth(self, cpf=CPF, pin=PIN):
        r = self.cli.post("/api/login", json={"cpf": cpf, "pin": pin})
        self.assertEqual(r.status_code, 200, r.get_json())
        return {"Authorization": f"Bearer {r.get_json()['acesso']}"}

    def _iniciar(self, h):
        r = self.cli.post("/api/atendimento/conversas", headers=h)
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()

    def _modelo(self, **campos):
        dados = {"resposta": "Ok.", "area": "entrega_problema", "assunto": "Teste", "resumo": "resumo",
                 "resolvido": False, "precisa_atendente": False, **campos}
        cliente = mock.Mock()
        cliente.messages.create.return_value = _RespostaFake(dados)
        return mock.patch.object(assistente_motorista, "_cliente_anthropic", return_value=cliente), cliente

    def _online(self):
        conn = ch.conectar()
        ch.gravar_status_atendente(conn, "hugo", "Hugo", "ONLINE")
        conn.close()

    # testes ------------------------------------------------------------------
    def test_estado_vazio_e_abertura_da_conversa(self):
        h = self._auth()
        e = self.cli.get("/api/atendimento/estado", headers=h).get_json()
        self.assertIsNone(e["ativo"])
        self.assertEqual(e["chamados"], [])
        self.assertEqual([a["valor"] for a in e["areas"]], list(ch.AREAS_MOTORISTA))
        self.assertIn("horario", e["situacao"])

        j = self._iniciar(h)
        self.assertEqual(j["chamado"]["status"], "COM_ASSISTENTE")
        self.assertEqual(len(j["mensagens"]), 1)
        self.assertEqual(j["mensagens"][0]["origem"], "assistente")
        # a saudação já traz os assuntos como chips
        self.assertEqual([o["valor"] for o in j["mensagens"][0]["opcoes"]], list(ch.AREAS_MOTORISTA))
        self.assertIn("João", j["mensagens"][0]["texto"])

        # o chamado nasce como MOTORISTA e não vaza pra listagem de cliente
        conn = ch.conectar()
        c = ch.buscar_chamado(conn, j["chamado"]["id"])
        self.assertEqual((c["tipo"], c["motorista_cpf"], c["agent_id"]), ("MOTORISTA", CPF, AGENT))
        self.assertTrue(c["de_motorista"])
        self.assertEqual(c["solicitante"], "João da Silva")
        self.assertEqual(ch.listar_chamados_cliente(conn, ""), [])
        conn.close()

    def test_triagem_area_parada_e_conversa_com_o_modelo(self):
        h = self._auth()
        chamado_id = self._iniciar(h)["chamado"]["id"]

        # 1) assunto por chip -> pergunta qual parada, com as paradas da rota
        r = self.cli.post(f"/api/atendimento/chamados/{chamado_id}/mensagens", headers=h,
                          json={"texto": "Problema na entrega", "chip": "entrega_problema"})
        self.assertEqual(r.status_code, 200, r.get_json())
        msgs = r.get_json()["mensagens"]
        self.assertEqual(msgs[-1]["origem"], "assistente")
        rotulos = [o["rotulo"] for o in msgs[-1]["opcoes"]]
        self.assertTrue(any("MERCADO CENTRAL" in x for x in rotulos), rotulos)
        self.assertEqual(rotulos[-1], "Não é sobre uma parada")

        # 2) parada por chip -> vira a parada em foco do chamado
        r = self.cli.post(f"/api/atendimento/chamados/{chamado_id}/mensagens", headers=h,
                          json={"texto": "1. MERCADO CENTRAL", "chip": "PS-100"})
        self.assertIn("Parada 1", r.get_json()["mensagens"][-1]["texto"])
        conn = ch.conectar()
        c = ch.buscar_chamado(conn, chamado_id)
        self.assertEqual(c["pedido_ref"], "PS-100")
        self.assertEqual(c["area"], "entrega_problema")
        self.assertIsNotNone(c["rota_id"])
        conn.close()

        # 3) texto livre -> modelo responde e o resumo/assunto ficam no chamado
        patch, cliente = self._modelo(resposta="Anotei, vou chamar a logística.", assunto="Cliente fechado",
                                      resumo="Motorista na parada 1, cliente fechado", precisa_atendente=True)
        with patch:
            r = self.cli.post(f"/api/atendimento/chamados/{chamado_id}/mensagens", headers=h,
                              json={"texto": "O cliente está fechado"})
        self.assertEqual(r.get_json()["mensagens"][-1]["texto"], "Anotei, vou chamar a logística.")
        self.assertEqual(r.get_json()["mensagens"][-1]["opcoes"][0]["acao"], "atendente")
        conn = ch.conectar()
        c = ch.buscar_chamado(conn, chamado_id)
        conn.close()
        self.assertEqual(c["assunto"], "Cliente fechado")
        self.assertIn("parada 1", c["resumo_assistente"])
        # o prompt levou a rota e as paradas reais do motorista
        sistema = cliente.messages.create.call_args.kwargs["system"]
        self.assertIn("MERCADO CENTRAL", sistema)
        self.assertIn("PADARIA SOL", sistema)
        self.assertIn("R$ 340", sistema.replace("340.0", "340"))

    def test_assunto_urgente_pula_a_triagem(self):
        h = self._auth()
        chamado_id = self._iniciar(h)["chamado"]["id"]
        r = self.cli.post(f"/api/atendimento/chamados/{chamado_id}/mensagens", headers=h,
                          json={"texto": "Veículo, acidente ou atraso", "chip": "veiculo"})
        msg = r.get_json()["mensagens"][-1]
        self.assertIn("urgente", msg["texto"])
        self.assertEqual([o["acao"] for o in msg["opcoes"]], ["atendente"])   # sem "resolveu"
        self.assertEqual(r.get_json()["chamado"]["assunto"], "Veículo / acidente / atraso")

    def test_pedir_logistica_dentro_e_fora_do_horario(self):
        h = self._auth()
        chamado_id = self._iniciar(h)["chamado"]["id"]
        self.cli.post(f"/api/atendimento/chamados/{chamado_id}/mensagens", headers=h,
                      json={"texto": "quebrou o carro", "chip": "veiculo"})

        # fora do horário da logística (madrugada): vira chamado + e-mail
        with mock.patch.object(ch, "situacao_horario",
                               return_value={"dentro": False, "motivo": "depois", "volta_em": "amanhã às 6h"}):
            r = self.cli.post(f"/api/atendimento/chamados/{chamado_id}/acao", headers=h, json={"tipo": "atendente"})
        self.assertEqual(r.get_json()["chamado"]["status"], "AGUARDANDO_FL")
        self.assertEqual([e[0] for e in self.emails], ["email_para_atendimento"])
        self.emails.clear()

        # dentro do horário e com atendente online: entra na fila, sem e-mail
        self._online()
        conn = ch.conectar()
        ch.atualizar_chamado(conn, chamado_id, status=ch.STATUS_COM_ASSISTENTE)
        conn.close()
        with mock.patch.object(ch, "situacao_horario", return_value={"dentro": True, "motivo": None, "volta_em": ""}):
            r = self.cli.post(f"/api/atendimento/chamados/{chamado_id}/acao", headers=h, json={"tipo": "atendente"})
        self.assertEqual(r.get_json()["chamado"]["status"], "NA_FILA")
        self.assertEqual(self.emails, [])

    def test_resposta_da_equipe_chega_no_app_e_conta_como_nao_lida(self):
        h = self._auth()
        chamado_id = self._iniciar(h)["chamado"]["id"]
        conn = ch.conectar()
        chamado = ch.buscar_chamado(conn, chamado_id)
        ch.entrar_na_fila(conn, chamado, self.config)
        chamado = ch.buscar_chamado(conn, chamado_id)
        _, chamado, mandar = ch.registrar_mensagem_equipe(conn, chamado, "hugo", "Hugo",
                                                          "Pode dar o insucesso e seguir.", [], self.config)
        # motorista sem e-mail cadastrado: nada de e-mail, o app é o canal
        self.assertTrue(mandar)
        self.assertEqual(ch.emails_do_solicitante(conn, chamado), [])
        self.assertEqual(ch.nao_lidas_motorista(conn, CPF), 1)
        conn.close()

        self.assertEqual(self.cli.get("/api/atendimento/nao-lidas", headers=h).get_json()["nao_lidas"], 1)
        r = self.cli.get(f"/api/atendimento/chamados/{chamado_id}", headers=h).get_json()
        self.assertEqual(r["mensagens"][-1]["texto"], "Pode dar o insucesso e seguir.")
        self.assertEqual(r["mensagens"][-1]["origem"], "equipe")
        # abrir a conversa zera o não lido
        self.assertEqual(self.cli.get("/api/atendimento/nao-lidas", headers=h).get_json()["nao_lidas"], 0)

    def test_motorista_nao_ve_conversa_de_outro(self):
        h1, h2 = self._auth(), self._auth(CPF2, PIN2)
        meu = self._iniciar(h1)["chamado"]["id"]
        self.assertEqual(self.cli.get(f"/api/atendimento/chamados/{meu}", headers=h2).status_code, 404)
        self.assertEqual(self.cli.post(f"/api/atendimento/chamados/{meu}/mensagens", headers=h2,
                                       json={"texto": "oi"}).status_code, 404)
        self.assertEqual(self.cli.post(f"/api/atendimento/chamados/{meu}/acao", headers=h2,
                                       json={"tipo": "atendente"}).status_code, 404)
        self.assertEqual(self.cli.get("/api/atendimento/estado", headers=h2).get_json()["chamados"], [])
        self.assertEqual(self.cli.get("/api/atendimento/estado", headers=h1).get_json()["ativo"]["chamado"]["id"], meu)

    def test_encerrar_e_reabrir(self):
        h = self._auth()
        chamado_id = self._iniciar(h)["chamado"]["id"]
        r = self.cli.post(f"/api/atendimento/chamados/{chamado_id}/acao", headers=h, json={"tipo": "resolvido"})
        self.assertEqual(r.get_json()["chamado"]["status"], "RESOLVIDO")
        self.assertEqual(r.get_json()["chamado"]["resolvido_por"], "assistente")
        # sem e-mail cadastrado, o histórico não é disparado
        self.assertEqual(self.emails, [])
        # mensagem nova reabre
        patch, _ = self._modelo(resposta="Voltei.")
        with patch:
            r = self.cli.post(f"/api/atendimento/chamados/{chamado_id}/mensagens", headers=h, json={"texto": "voltou o problema"})
        self.assertNotEqual(r.get_json()["chamado"]["status"], "RESOLVIDO")

    def test_fila_do_painel_separa_motorista_de_cliente(self):
        h = self._auth()
        chamado_id = self._iniciar(h)["chamado"]["id"]
        conn = ch.conectar()
        ch.entrar_na_fila(conn, ch.buscar_chamado(conn, chamado_id), self.config)
        ch.criar_chamado(conn, {"cnpj": "00000000000191", "nome": "CLIENTE X"}, origem="chat",
                         status=ch.STATUS_NA_FILA, assunto="Pedido atrasado")
        motoristas = ch.listar_fila(conn, "motoristas")
        self.assertEqual([c["id"] for c in motoristas], [chamado_id])
        self.assertTrue(motoristas[0]["de_motorista"])
        self.assertEqual(motoristas[0]["solicitante"], "João da Silva")
        fila = ch.listar_fila(conn, "fila")
        self.assertEqual(len(fila), 2)                                  # uma fila só
        contagens = ch.contagens_fila(conn)
        self.assertEqual((contagens["motoristas"], contagens["fila"]), (1, 2))
        # área do motorista é traduzida pelo catálogo dele, não pelo do cliente
        ch.atualizar_chamado(conn, chamado_id, area="pagamento")
        self.assertEqual(ch.buscar_chamado(conn, chamado_id)["area_rotulo"], "Pagamento (rota, km, pedágio)")
        conn.close()

    def test_horario_da_logistica_e_mais_largo_que_o_do_cliente(self):
        cedo = datetime(2026, 9, 14, 6, 30)         # segunda, 6h30
        sabado = datetime(2026, 9, 19, 10, 0)
        self.assertFalse(ch.situacao_horario(self.config, cedo)["dentro"])
        self.assertTrue(ch.situacao_horario(self.config, cedo, ch.PERFIL_LOGISTICA)["dentro"])
        self.assertFalse(ch.situacao_horario(self.config, sabado)["dentro"])
        self.assertTrue(ch.situacao_horario(self.config, sabado, ch.PERFIL_LOGISTICA)["dentro"])
        # almoço do cliente não vale pra logística
        almoco = datetime(2026, 9, 14, 13, 30)
        self.assertEqual(ch.situacao_horario(self.config, almoco)["motivo"], "almoco")
        self.assertTrue(ch.situacao_horario(self.config, almoco, ch.PERFIL_LOGISTICA)["dentro"])


if __name__ == "__main__":
    unittest.main()
