"""Testes do e-mail de resumo diario por embarcador (resumo_diario_embarcador.py).

Rodar: py -3.11 -m unittest test_resumo_diario_embarcador
"""

import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

import preferencias_notificacao as pn
import resumo_diario_embarcador as mod
from portal_cliente import dados_cliente, link_canhoto

DATA = date(2026, 9, 17)
SENDER_A, SENDER_B, SENDER_C = 101, 102, 103
SEGREDO = "segredo-de-teste"
CONFIG = {"resumo_diario_embarcador": {"forcar_destino": ""},
          "portal_cliente": {"secret_key": SEGREDO, "url_base": "https://portal.test/cliente"}}


def _servico(sid, code, sender, status="done", status_done="success", motivo=None, nome="Mercado Bom"):
    return {"id": sid, "code": code, "sender_id": sender, "status": status, "status_done": status_done,
            "failed_reason_id": motivo, "completed_at": "2026-09-17 17:30:00" if status == "done" else None,
            "address": "Rua A, 10 - Centro, Sao Paulo - SP", "title": nome, "customer": {"name": nome}}


ROTAS = [{
    "id": 1, "name": "Planejamento - 17/09/2026 - #1", "status": "started", "agent_id": None,
    "start_at": "2026-09-17 09:00:00", "services": [
        _servico(11, "#PS-11", SENDER_A),
        _servico(12, "#PS-12", SENDER_A, status_done="failed", motivo=1),
        _servico(13, "#PS-13", SENDER_A, status="on_route"),
        _servico(14, "#PS-14", SENDER_A, status="assigned"),
        _servico(21, "#PS-21", SENDER_B),
        _servico(99, "#PS-99", SENDER_A, status="canceled"),
    ],
}]


class TestTratativaParaCliente(unittest.TestCase):
    def _t(self, motivo_torre=None, eventos=(), aguardando=False):
        return mod.tratativa_para_cliente(motivo_torre, set(eventos), aguardando)

    def test_resposta_padronizada_da_torre(self):
        self.assertEqual(self._t("Reagendado"), "Reagendado")
        self.assertEqual(self._t("Devolvido ao embarcador"), "Devolvido ao embarcador")

    def test_textos_livres_antigos_que_casam_com_um_rotulo(self):
        casos = {
            "Já havia sido enviado!": "Já havia sido enviado",
            "COLETADO PELO CLIENTE": "Coletado pelo cliente",
            "já entregue": "Já entregue",
            "Local Fechado / Reagendado": "Reagendado",
            "Cliente ausente / Reagendado": "Reagendado",
            "Reagendado para dia seguinte": "Reagendado",
        }
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(self._t(texto), esperado)

    def test_texto_livre_que_nao_casa_nao_vaza_pro_cliente(self):
        for texto in ("Informado ao cliente via WhatsApp", "Outro", "Foi coletada",
                      "Transportadora não faria região essa semana. Solicitaram entregar na próxima semana."):
            with self.subTest(texto=texto):
                self.assertEqual(self._t(texto), mod.TRATATIVA_GENERICA)

    def test_duplicado_pela_torre_vira_reentrega_programada(self):
        self.assertEqual(self._t("Duplicado → #PS-12-R1"), "Reentrega programada")
        self.assertEqual(self._t("Duplicado por engano"), "Duplicado por engano")

    def test_eventos_automaticos(self):
        self.assertEqual(self._t(eventos=["INSUCESSO_DETECTADO", "REENVIO_AUTOMATICO"]), "Reentrega programada")
        self.assertEqual(self._t(eventos=["AVISO_ENVIADO"], aguardando=True), "Aguardando seu retorno")
        self.assertEqual(self._t(eventos=["RESPOSTA_RECEBIDA"]), "Retorno recebido, em andamento")

    def test_torre_vence_os_eventos_e_sem_nada_e_generica(self):
        self.assertEqual(self._t("Pedido cancelado", eventos=["REENVIO_AUTOMATICO"], aguardando=True), "Pedido cancelado")
        self.assertEqual(self._t(), mod.TRATATIVA_GENERICA)


class TestMotivoParaCliente(unittest.TestCase):
    def test_tira_os_avisos_internos(self):
        self.assertEqual(mod.motivo_para_cliente("Avaria (novo -- sem regra de duplicação)"), "Avaria")
        self.assertEqual(mod.motivo_para_cliente("Motivo #77 (ainda não cadastrado no de-para)"), "Motivo não informado")
        self.assertEqual(mod.motivo_para_cliente("Cliente ausente"), "Cliente ausente")
        self.assertEqual(mod.motivo_para_cliente(""), "Motivo não informado")


class _ComBanco(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
                     "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)")
        conn.executemany("INSERT INTO interno VALUES (?,?,?,?,?,?,?)", [
            ("1", "Cliente A Ltda", "Cliente A", "log@a.com", 1, SENDER_A, None),
            ("2", "Cliente B Ltda", None, "b@b.com", 1, SENDER_B, None),      # nao ligou o resumo
            ("3", "Cliente C Ltda", None, "c@c.com", 1, SENDER_C, None),      # ligou, mas nada em rota
        ])
        conn.execute("CREATE TABLE torre_excecoes_tratadas (id TEXT PRIMARY KEY, data_alvo TEXT, tipo TEXT, "
                     "descricao TEXT, motivo TEXT, tratado_em TEXT)")
        conn.execute("INSERT INTO torre_excecoes_tratadas VALUES ('insucesso:PS-12', '2026-09-17', 'Insucesso', '', "
                     "'Local fechado / Reagendado', '2026-09-17 18:00:00')")
        conn.commit()
        pn.salvar(conn, "1", [], {"resumo_diario": True}, "cliente")
        pn.salvar(conn, "3", [], {"resumo_diario": True}, "cliente")
        conn.close()
        self.enviados = []
        patcher = mock.patch.object(dados_cliente, "nf_por_codigo", return_value={"PS-11": "5001", "PS-12": "5002"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _enviar(self, destinos, assunto, corpo, config_email, **kw):
        self.enviados.append((destinos, assunto, corpo))
        return True

    def _executar(self, config=None, **kw):
        return mod.executar(CONFIG if config is None else config, DATA, rotas=ROTAS, motoristas={},
                            enviar=self._enviar, db_path=self.db, **kw)


class TestBlocosEEmail(_ComBanco):
    def _email(self):
        self._executar()
        return self.enviados[0]

    def test_separa_em_tres_blocos_e_ignora_cancelado(self):
        linhas = dados_cliente.linhas_das_rotas(ROTAS, SENDER_A, {}, {})
        blocos = mod.separar_blocos(linhas)
        self.assertEqual([p["codigo"] for p in blocos["entregues"]], ["#PS-11"])
        self.assertEqual([p["codigo"] for p in blocos["falhas"]], ["#PS-12"])
        self.assertEqual([p["codigo"] for p in blocos["abertos"]], ["#PS-13", "#PS-14"])

    def test_assunto_conta_os_blocos_e_omite_os_zerados(self):
        _, assunto, _ = self._email()
        self.assertEqual(assunto, "[Freshlog] Entregas de 17/09: 1 entregue, 1 com falha, 2 em aberto")
        so_entregue = mod.montar_email("X", {"entregues": [{"codigo": "#PS-1", "service_id": 1, "sender_id": 1}] * 2,
                                             "falhas": [], "abertos": []}, DATA, {}, lambda p: "")[0]
        self.assertEqual(so_entregue, "[Freshlog] Entregas de 17/09: 2 entregues")

    def test_entregue_leva_link_assinado_do_canhoto(self):
        _, _, corpo = self._email()
        inicio = corpo.index("https://portal.test/cliente/c/")
        token = corpo[inicio:].split('"')[0].rsplit("/", 1)[1]
        self.assertEqual(link_canhoto.ler(token, SEGREDO), {"service_id": 11, "sender_id": SENDER_A})
        self.assertEqual(corpo.count("https://portal.test/cliente/c/"), 1)   # so a entregue tem link

    def test_falha_mostra_motivo_e_tratativa_padronizada(self):
        _, _, corpo = self._email()
        self.assertTrue("5002" in corpo)
        self.assertTrue(">Reagendado<" in corpo)
        self.assertFalse("Local fechado / Reagendado" in corpo)   # texto interno da Torre nao vaza

    def test_nf_vazia_vira_traco_e_texto_de_fora_e_escapado(self):
        rotas = [{**ROTAS[0], "services": [_servico(15, "#PS-15", SENDER_A, nome="<script>alert(1)</script>")]}]
        mod.executar(CONFIG, DATA, rotas=rotas, motoristas={}, enviar=self._enviar, db_path=self.db)
        corpo = self.enviados[0][2]
        self.assertFalse("<script>alert(1)</script>" in corpo)
        self.assertTrue("&lt;script&gt;" in corpo)
        self.assertTrue(">—<" in corpo)

    def test_sem_segredo_do_portal_sai_sem_link_em_vez_de_quebrar(self):
        self._executar(config={"resumo_diario_embarcador": {"forcar_destino": ""}})
        self.assertFalse("/c/" in self.enviados[0][2])
        self.assertTrue("portal do cliente" in self.enviados[0][2])


class TestExecutar(_ComBanco):
    def test_so_recebe_quem_ligou_no_portal_e_teve_nota_em_rota(self):
        r = self._executar()
        self.assertEqual([e[0] for e in self.enviados], [["log@a.com"]])
        self.assertEqual((r["enviados"], r["falhas"], r["sem_notas"]), (1, 0, 1))

    def test_segunda_rodada_no_mesmo_dia_nao_repete(self):
        self._executar()
        r = self._executar()
        self.assertEqual(len(self.enviados), 1)
        self.assertEqual(r["ja_enviados"], 1)

    def test_sem_secao_no_config_redireciona_pro_hugo(self):
        self._executar(config={"portal_cliente": CONFIG["portal_cliente"]})
        self.assertEqual(self.enviados[0][0], [mod.EMAIL_TESTE])
        self.assertTrue("log@a.com" in self.enviados[0][2])   # mostra pra quem iria

    def test_modo_teste_redireciona_nao_marca_e_aceita_embarcador_desligado(self):
        self._executar(modo_teste=True, sender_id=SENDER_B)
        self.assertEqual([e[0] for e in self.enviados], [[mod.EMAIL_TESTE]])
        self._executar()
        self.assertEqual(self.enviados[1][0], ["log@a.com"])

    def test_chave_mestra_desligada_bloqueia_envio_real_mas_nao_o_piloto(self):
        r = self._executar(config={**CONFIG, "notificacoes_automaticas": {"ativo": False}})
        self.assertEqual(self.enviados, [])
        self.assertTrue(r["desativado"])
        self._executar(config={"notificacoes_automaticas": {"ativo": False}})
        self.assertEqual(self.enviados[0][0], [mod.EMAIL_TESTE])

    def test_flag_propria_desligada(self):
        r = self._executar(config={"resumo_diario_embarcador": {"ativo": False}})
        self.assertEqual(self.enviados, [])
        self.assertTrue(r["desativado"])

    def test_falha_num_embarcador_nao_derruba_os_outros(self):
        conn = sqlite3.connect(self.db)
        pn.salvar(conn, "2", [], {"resumo_diario": True}, "cliente")
        conn.close()

        def enviar(destinos, assunto, corpo, config_email, **kw):
            if destinos == ["b@b.com"]:
                raise RuntimeError("smtp caiu")
            return self._enviar(destinos, assunto, corpo, config_email)

        r = mod.executar(CONFIG, DATA, rotas=ROTAS, motoristas={}, enviar=enviar, db_path=self.db)
        self.assertEqual((r["enviados"], r["falhas"]), (1, 1))

    def test_banco_sem_as_tabelas_de_tratativa_nao_quebra(self):
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TABLE torre_excecoes_tratadas")
        conn.commit()
        conn.close()
        self._executar()
        self.assertTrue(mod.TRATATIVA_GENERICA in self.enviados[0][2])


if __name__ == "__main__":
    unittest.main()
