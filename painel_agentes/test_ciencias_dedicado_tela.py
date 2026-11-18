# -*- coding: utf-8 -*-
"""
Tela /ciencias-dedicado (equipe acompanha a ciencia do dedicado fora do dia fixo).

    py -3.11 -m unittest test_ciencias_dedicado_tela   (de dentro de painel_agentes/)
"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import ciencia_dedicado as cd  # noqa: E402
import preferencias_notificacao as pn  # noqa: E402
from portal_cliente import link_ciencia  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)
import acesso_telas  # noqa: E402

SEGREDO = "segredo-de-teste"
URL_BASE = "https://portal.teste/cliente"
AGORA = datetime(2026, 11, 17, 10, 0, 0)


class TestCienciasDedicadoTela(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        for alvo in (cd, pn, acesso_telas):
            p = mock.patch.object(alvo, "DB_PATH", self.db)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value={
            "painel_agentes": {"usuario": "u", "senha": "s"},
            "portal_cliente": {"secret_key": SEGREDO, "url_base": URL_BASE}})
        p.start()
        self.addCleanup(p.stop)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
                     "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)")
        conn.executemany("INSERT INTO interno VALUES (?,?,?,?,?,?,?)", [
            ("11111111000111", "Alfa Ltda", "Alfa Frutas", "a@a.com", 1, 101, 9001),
            ("22222222000122", "Beta Ltda", "", "b@b.com", 1, 102, 9002),
        ])
        conn.commit()
        conn.close()

        conn = cd.conectar(self.db)
        self.ids = {}
        for cod, sender, cnpj in (("PS-1", 101, "11111111000111"), ("PS-2", 102, "22222222000122"),
                                  ("PS-3", 103, "33333333000133")):
            self.ids[cod] = conn.execute(
                "INSERT INTO ciencias_dedicado (codigo, service_id, sender_id, cnpj, regiao, regra_nome, "
                "dias_visita, destinatario, cidade, data_entrega, valor, criado_em, situacao) "
                "VALUES (?, ?, ?, ?, 'Santos', 'Santos', 'ter e qui', 'Cliente', 'Santos', '2026-11-20', 123.5, "
                "'2026-11-17 08:00:00', 'PENDENTE')", (cod, int(cod[3:]), sender, cnpj)).lastrowid
        conn.commit()
        i1, i2, i3 = self.ids["PS-1"], self.ids["PS-2"], self.ids["PS-3"]
        # PS-1 pendente: 2 e-mails reais, 1 whatsapp real, 1 copia de piloto e 1 chat nao enviado
        cd.registrar_envio(conn, i1, "email", "enviado", "a@a.com", rodada=0, agora=datetime(2026, 11, 17, 8, 1))
        cd.registrar_envio(conn, i1, "whatsapp", "enviado", "5511", rodada=0, agora=datetime(2026, 11, 17, 8, 2))
        cd.registrar_envio(conn, i1, "email", "enviado", "a@a.com", rodada=1, agora=datetime(2026, 11, 17, 12, 1))
        cd.registrar_envio(conn, i1, "whatsapp", "piloto", "hugo", rodada=1, agora=datetime(2026, 11, 17, 12, 2))
        cd.registrar_envio(conn, i1, "chat", "nao_enviado", "", motivo="sem grupo", rodada=1,
                           agora=datetime(2026, 11, 17, 12, 3))
        # PS-2 respondida; PS-3 encerrada pelo sistema
        cd.responder(conn, i2, cd.CIENTE, "Maria", "1.2.3.4", AGORA)
        cd.encerrar(conn, i3, "pedido entregue", AGORA)
        conn.close()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as s:
            s["nivel_acesso"] = nivel
            s["usuario"] = "teste"

    def test_tela_lista_pendentes_e_respondidos_com_filtro_de_situacao(self):
        self._logar("operador")
        html = self.cliente.get("/ciencias-dedicado").get_data(as_text=True)
        for esperado in ("PS-1", "PS-2", "PS-3", "Alfa Frutas", "Beta Ltda", "33333333000133",
                         "Aguardando resposta", "Ciente do dedicado", "Encerrado: pedido entregue", "Maria"):
            self.assertIn(esperado, html)
        # contagem por canal: so 'enviado' (2 e-mails, 1 whatsapp, 0 chat); piloto e nao_enviado ficam fora
        self.assertIn('data-contagem="email:2 whatsapp:1 chat:0"', html)
        so_pendentes = self.cliente.get("/ciencias-dedicado?situacao=PENDENTE").get_data(as_text=True)
        self.assertIn("PS-1", so_pendentes)
        self.assertNotIn("PS-2", so_pendentes)
        self.assertNotIn("PS-3", so_pendentes)
        por_nome = self.cliente.get("/ciencias-dedicado?embarcador=FRUTAS").get_data(as_text=True)
        self.assertIn("PS-1", por_nome)
        self.assertNotIn("PS-2", por_nome)

    def test_encerrado_nao_aparece_como_resposta_do_embarcador(self):
        self._logar("operador")
        html = self.cliente.get("/ciencias-dedicado?situacao=ENCERRADO").get_data(as_text=True)
        self.assertIn("encerrado em 17/11", html)
        self.assertNotIn("respondeu 17/11", html)

    def test_api_devolve_historico_de_envios_em_ordem(self):
        self._logar("logistica")
        r = self.cliente.get(f"/api/ciencias-dedicado/{self.ids['PS-1']}")
        self.assertEqual(r.status_code, 200)
        dados = r.get_json()
        self.assertEqual(dados["ciencia"]["codigo"], "PS-1")
        self.assertEqual([(e["canal"], e["resultado"]) for e in dados["envios"]],
                         [("email", "enviado"), ("whatsapp", "enviado"), ("email", "enviado"),
                          ("whatsapp", "piloto"), ("chat", "nao_enviado")])
        self.assertEqual(dados["envios"][-1]["motivo"], "sem grupo")
        self.assertEqual(dados["envios"][0]["rodada"], 0)
        self.assertEqual(self.cliente.get("/api/ciencias-dedicado/9999").status_code, 404)

    def test_tela_exige_login_e_nivel(self):
        self.assertEqual(self.cliente.get("/ciencias-dedicado").status_code, 302)
        r = self.cliente.get(f"/api/ciencias-dedicado/{self.ids['PS-1']}")
        self.assertEqual(r.status_code, 401)
        self._logar("leitura")
        self.assertIn(self.cliente.get("/ciencias-dedicado").status_code, (302, 401, 403))
        self.assertIn(self.cliente.get(f"/api/ciencias-dedicado/{self.ids['PS-1']}").status_code, (302, 401, 403))

    def test_api_mostra_quem_respondeu_quando_e_ip_como_prova(self):
        self._logar("operador")
        d = self.cliente.get(f"/api/ciencias-dedicado/{self.ids['PS-2']}").get_json()
        self.assertEqual(d["ciencia"]["situacao"], "CIENTE")
        self.assertEqual(d["resposta"], {"por": "Maria", "em": "2026-11-17 10:00:00",
                                         "ip": "1.2.3.4", "data_nova": None})
        self.assertIsNone(d["encerramento"])

    def test_api_reagendado_traz_data_nova(self):
        conn = cd.conectar(self.db)
        cd.responder(conn, self.ids["PS-1"], cd.REAGENDADO, "Joao", "9.9.9.9", AGORA, date(2026, 11, 24))
        conn.close()
        self._logar("operador")
        d = self.cliente.get(f"/api/ciencias-dedicado/{self.ids['PS-1']}").get_json()
        self.assertEqual(d["resposta"]["por"], "Joao")
        self.assertEqual(d["resposta"]["ip"], "9.9.9.9")
        self.assertEqual(d["resposta"]["data_nova"], "2026-11-24")

    def test_api_encerrado_nao_tem_resposta_do_embarcador(self):
        self._logar("operador")
        d = self.cliente.get(f"/api/ciencias-dedicado/{self.ids['PS-3']}").get_json()
        self.assertIsNone(d["resposta"])
        self.assertEqual(d["encerramento"], {"motivo": "pedido entregue", "em": "2026-11-17 10:00:00"})
        self.assertNotIn("respondido_por", d["ciencia"])
        self.assertNotIn("respondido_ip", d["ciencia"])

    def test_api_pendente_nao_tem_resposta_nem_encerramento(self):
        self._logar("operador")
        d = self.cliente.get(f"/api/ciencias-dedicado/{self.ids['PS-1']}").get_json()
        self.assertIsNone(d["resposta"])
        self.assertIsNone(d["encerramento"])

    def test_tela_esta_em_acesso_telas(self):
        telas = {t[0]: t for t in acesso_telas.TELAS}
        self.assertIn("ciencias_dedicado", telas)
        caminhos = telas["ciencias_dedicado"][3]
        self.assertTrue(acesso_telas.caminho_liberado("/ciencias-dedicado", ["ciencias_dedicado"]))
        self.assertTrue(acesso_telas.caminho_liberado("/api/ciencias-dedicado/7", ["ciencias_dedicado"]))
        self.assertEqual(len(caminhos), 2)

    def test_link_copiado_e_o_do_embarcador_da_linha(self):
        self._logar("total")
        html = self.cliente.get("/ciencias-dedicado").get_data(as_text=True)
        self.assertIn(f'data-link="{link_ciencia.url(101, SEGREDO, URL_BASE)}"', html)
        self.assertIn(f'data-link="{link_ciencia.url(102, SEGREDO, URL_BASE)}"', html)
        self.assertNotEqual(link_ciencia.url(101, SEGREDO, URL_BASE), link_ciencia.url(102, SEGREDO, URL_BASE))


if __name__ == "__main__":
    unittest.main()
