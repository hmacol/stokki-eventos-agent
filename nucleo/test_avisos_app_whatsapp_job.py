# -*- coding: utf-8 -*-
"""
test_avisos_app_whatsapp_job.py

O que vira aviso no WhatsApp do motorista que usa o app pelo navegador (PWA, iPhone):
rota nova, alterada, cancelada e resposta da logistica no chat de Ajuda.
O canal (trava, teto, origem) esta em test_avisos_app_whatsapp.py.
    py -3.11 -m unittest nucleo.test_avisos_app_whatsapp_job -v
"""
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "portal_cliente"))

import chamados as ch  # noqa: E402
from nucleo import auth_motorista as auth, avisos_app_whatsapp as av, banco  # noqa: E402

CPF, AGENT = "11122233344", 777
CFG_PILOTO = {"avisos_app_motoristas": {"forcar_destino_whatsapp": "11988887777"}}


class JobTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "t.db"
        self._patches = [mock.patch.object(banco, "DB_PATH", db), mock.patch.object(ch, "DB_PATH", db)]
        for p in self._patches:
            p.start()
        self.conn = banco.conectar()
        ch.conectar().close()          # cria as tabelas do chat
        auth.criar_ou_atualizar_motorista(self.conn, CPF, "Maria Souza", "482913", agent_id=AGENT, telefone="11911112222")
        self.conn.execute("UPDATE motoristas SET plataforma = 'web' WHERE cpf = ?", (CPF,))
        self.conn.commit()
        self.hoje = date.today()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.conn.close()
        self._tmp.cleanup()

    # ── apoio ────────────────────────────────────────────────────────────────────
    def rota(self, codigos, quando=None, status="PLANEJADA", start="07:00:00"):
        quando = quando or self.hoje
        cur = self.conn.execute(
            "INSERT INTO nucleo_rotas (data_rota, nome, provedor, agent_id, motorista_cpf, start_at, status, total_paradas) "
            "VALUES (?, 'R', 'APP', ?, ?, ?, ?, ?)",
            (quando.isoformat(), AGENT, CPF, f"{quando.isoformat()} {start}", status, len(codigos)))
        self.trocar_paradas(cur.lastrowid, codigos)
        return cur.lastrowid

    def trocar_paradas(self, rid, codigos):
        self.conn.execute("DELETE FROM nucleo_paradas WHERE rota_id = ?", (rid,))
        for i, c in enumerate(codigos, 1):
            self.conn.execute("INSERT INTO nucleo_paradas (rota_id, ordem, codigo, situacao) VALUES (?, ?, ?, 'PENDENTE')", (rid, i, c))
        self.conn.commit()

    def coletar(self, config=CFG_PILOTO, agora=None):
        avisos = av.coletar(self.conn, config, agora=agora)
        self.conn.commit()                       # como o CLI: estado interno fica gravado
        return avisos

    def aplicar(self, avisos):
        for a in avisos:
            a["aplicar"]()
        self.conn.commit()

    @staticmethod
    def tipos(avisos):
        return sorted(a["tipo"] for a in avisos)

    # ── rotas ────────────────────────────────────────────────────────────────────
    def test_primeira_vez_nao_avisa_o_que_ja_existia(self):
        self.rota(["PS-1", "PS-2"])
        self.assertEqual(self.coletar(), [])                    # semeia em silencio
        self.assertEqual(self.coletar(), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM avisos_app_rotas").fetchone()[0], 1)

    def test_rota_nova_so_avisa_depois_de_ficar_igual_por_duas_rodadas(self):
        self.coletar()                                           # semente (sem rota)
        self.rota(["PS-1", "PS-2", "PS-3"])
        self.assertEqual(self.coletar(), [])                     # 1a vez que ve: espera estabilizar
        avisos = self.coletar()                                  # 2a rodada igual: avisa
        self.assertEqual(self.tipos(avisos), ["nova_rota"])
        self.assertIn("3 paradas", avisos[0]["texto"])
        self.assertIn("07:00", avisos[0]["texto"])
        self.assertIn("hoje", avisos[0]["texto"])
        self.aplicar(avisos)
        self.assertEqual(self.coletar(), [])                     # nao repete

    def test_rota_que_ainda_muda_espera(self):
        self.coletar()
        rid = self.rota(["PS-1"])
        self.coletar()
        self.trocar_paradas(rid, ["PS-1", "PS-2"])               # operacao ainda mexendo
        self.assertEqual(self.coletar(), [])
        self.assertEqual(self.tipos(self.coletar()), ["nova_rota"])   # estabilizou

    def test_rota_alterada_diz_o_que_mudou(self):
        self.coletar()
        rid = self.rota(["PS-1", "PS-2"])
        self.coletar()
        self.aplicar(self.coletar())
        self.trocar_paradas(rid, ["PS-2", "PS-3", "PS-4"])       # saiu PS-1, entraram PS-3 e PS-4
        self.assertEqual(self.coletar(), [])
        avisos = self.coletar()
        self.assertEqual(self.tipos(avisos), ["rota_alterada"])
        self.assertIn("entraram 2 pedidos", avisos[0]["texto"])
        self.assertIn("saiu 1 pedido", avisos[0]["texto"])
        self.assertIn("3 paradas", avisos[0]["texto"])
        self.aplicar(avisos)
        self.assertEqual(self.coletar(), [])

    def test_baixa_do_proprio_motorista_nao_vira_aviso(self):
        self.coletar()
        rid = self.rota(["PS-1", "PS-2"])
        self.coletar()
        self.aplicar(self.coletar())
        self.conn.execute("UPDATE nucleo_rotas SET status = 'EM_ROTA' WHERE id = ?", (rid,))
        self.conn.execute("UPDATE nucleo_paradas SET situacao = 'ENTREGUE' WHERE rota_id = ? AND codigo = 'PS-1'", (rid,))
        self.conn.commit()
        self.assertEqual(self.coletar(), [])
        self.assertEqual(self.coletar(), [])

    def test_rota_cancelada_avisa_uma_vez(self):
        self.coletar()
        rid = self.rota(["PS-1"])
        self.coletar()
        self.aplicar(self.coletar())
        self.conn.execute("UPDATE nucleo_rotas SET status = 'CANCELADA' WHERE id = ?", (rid,))
        self.conn.commit()
        avisos = self.coletar()
        self.assertEqual(self.tipos(avisos), ["rota_cancelada"])
        self.assertIn("cancelada", avisos[0]["texto"])
        self.aplicar(avisos)
        self.assertEqual(self.coletar(), [])

    def test_rota_cancelada_antes_de_ser_avisada_nao_gera_nada(self):
        self.coletar()
        rid = self.rota(["PS-1"])
        self.coletar()
        self.conn.execute("UPDATE nucleo_rotas SET status = 'CANCELADA' WHERE id = ?", (rid,))
        self.conn.commit()
        self.assertEqual(self.coletar(), [])
        self.assertEqual(self.coletar(), [])

    def test_rota_de_amanha_diz_amanha_e_rota_sem_paradas_espera(self):
        self.coletar()
        self.rota(["PS-9"], quando=self.hoje + timedelta(days=1))
        vazia = self.rota([])
        self.coletar()
        avisos = self.coletar()
        self.assertEqual(self.tipos(avisos), ["nova_rota"])
        self.assertIn("amanhã", avisos[0]["texto"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM avisos_app_rotas WHERE rota_id = ?", (vazia,)).fetchone()[0], 0)

    def test_motorista_android_tambem_recebe(self):
        self.conn.execute("UPDATE motoristas SET plataforma = 'android' WHERE cpf = ?", (CPF,))
        self.conn.commit()
        self.coletar()
        self.rota(["PS-1"])
        self.coletar()
        self.assertEqual(self.tipos(self.coletar()), ["nova_rota"])

    def test_so_motorista_do_app_ativo_recebe(self):
        self.conn.execute("UPDATE motoristas SET plataforma = NULL WHERE cpf = ?", (CPF,))     # nunca abriu o app
        self.conn.commit()
        self.coletar()
        self.rota(["PS-1"])
        self.coletar()
        self.assertEqual(self.coletar(), [])
        self.conn.execute("UPDATE motoristas SET plataforma = 'web', ativo = 0 WHERE cpf = ?", (CPF,))
        self.conn.commit()
        self.assertEqual(self.coletar(), [])

    def test_desligado_sem_trava_nao_faz_nada_nem_semeia(self):
        self.rota(["PS-1"])
        self.assertEqual(av.coletar(self.conn, {}), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM avisos_app_motoristas").fetchone()[0], 0)

    # ── chat de Ajuda ────────────────────────────────────────────────────────────
    def _chamado(self):
        conn = ch.conectar()
        c = ch.criar_chamado(conn, {"tipo": ch.TIPO_MOTORISTA, "cpf": CPF, "agent_id": AGENT, "nome": "Maria"},
                             origem="chat", status=ch.STATUS_NA_FILA)
        return conn, c

    def test_resposta_da_logistica_vira_aviso_depois_de_alguns_minutos(self):
        conn, c = self._chamado()
        self.coletar()                                           # semente (chamado existe, sem mensagem)
        ch.adicionar_mensagem(conn, c, ch.ORIGEM_EQUIPE, "Ana (logística)", "Pode entregar na portaria, já avisamos o cliente.")
        agora = datetime.now()
        self.assertEqual(self.coletar(agora=agora), [])          # recem chegou: pode estar lendo no app
        avisos = self.coletar(agora=agora + timedelta(minutes=5))
        self.assertEqual(self.tipos(avisos), ["chamado"])
        self.assertIn("Pode entregar na portaria", avisos[0]["texto"])
        self.aplicar(avisos)
        self.assertEqual(self.coletar(agora=agora + timedelta(minutes=10)), [])

    def test_chat_so_equipe_e_nao_lida(self):
        conn, c = self._chamado()
        self.coletar()
        ch.adicionar_mensagem(conn, c, ch.ORIGEM_ASSISTENTE, "Assistente", "Oi, como posso ajudar?")
        ch.adicionar_mensagem(conn, c, ch.ORIGEM_CLIENTE, "Maria", "Preciso de ajuda")
        m = ch.adicionar_mensagem(conn, c, ch.ORIGEM_EQUIPE, "Ana", "Resposta ja lida no app")
        conn.execute("UPDATE portal_chamados_mensagens SET lido_cliente = 1 WHERE id = ?", (m["id"],))
        conn.commit()
        self.assertEqual(self.coletar(agora=datetime.now() + timedelta(minutes=10)), [])

    def test_mensagem_antiga_nao_e_reavisada_depois_da_semente(self):
        conn, c = self._chamado()
        ch.adicionar_mensagem(conn, c, ch.ORIGEM_EQUIPE, "Ana", "Mensagem de antes de virar web")
        self.coletar()                                           # semente marca o que ja existe
        self.assertEqual(self.coletar(agora=datetime.now() + timedelta(minutes=10)), [])


class EnvioTest(unittest.TestCase):
    """O estado so avanca quando o WhatsApp saiu; teto/pausa do canal interrompem o lote."""

    def _avisos(self, n=3):
        self.aplicados = []
        return [{"cpf": f"{i}", "nome": f"M{i}", "telefone": "11999990000", "tipo": "nova_rota", "assinatura": f"a{i}",
                 "texto": "t", "aplicar": (lambda i=i: self.aplicados.append(i))} for i in range(n)]

    def _enviar(self, situacoes, n=3):
        conn = mock.Mock()
        avisar = mock.Mock(side_effect=[(s, False) for s in situacoes])
        res = av.enviar(conn, {}, self._avisos(n), avisar=avisar, dormir=lambda s: None)
        return [r["situacao"] for r in res]

    def test_so_marca_como_avisado_quando_enviou(self):
        self.assertEqual(self._enviar(["enviado", "falhou", "enviado"]), ["enviado", "falhou", "enviado"])
        self.assertEqual(self.aplicados, [0, 2])                 # o que falhou fica pra proxima rodada

    def test_numero_sem_whatsapp_nao_fica_tentando_pra_sempre(self):
        self.assertEqual(self._enviar(["numero_sem_whatsapp"], n=1), ["numero_sem_whatsapp"])
        self.assertEqual(self.aplicados, [0])

    def test_teto_para_o_lote_e_nada_alem_do_enviado_e_marcado(self):
        self.assertEqual(self._enviar(["enviado", "teto"]), ["enviado", "teto", "nao_tentado"])
        self.assertEqual(self.aplicados, [0])

    def test_excecao_no_envio_vira_falhou_e_o_lote_segue(self):
        avisar = mock.Mock(side_effect=[RuntimeError("gateway"), ("enviado", False)])
        res = av.enviar(mock.Mock(), {}, self._avisos(2), avisar=avisar, dormir=lambda s: None)
        self.assertEqual([r["situacao"] for r in res], ["falhou", "enviado"])
        self.assertEqual(self.aplicados, [1])

    def test_exemplos_tem_os_quatro_tipos_e_link_do_app(self):
        ex = dict(av.exemplos())
        self.assertEqual(sorted(ex), ["chamado", "nova_rota", "rota_alterada", "rota_cancelada"])
        for texto in ex.values():
            self.assertTrue(texto.startswith("Olá, Maria!"))
        self.assertIn(av.LINK_APP, ex["nova_rota"])
        self.assertIn(av.LINK_APP, ex["chamado"])


if __name__ == "__main__":
    unittest.main()
