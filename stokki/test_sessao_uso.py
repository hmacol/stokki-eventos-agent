# -*- coding: utf-8 -*-
"""
test_sessao_uso.py

Trava da Stokki por conta (05/10/2026, "segunda alternativa de login para
o caso de congestionamento" e "um agente na fila do outro"): cada conta (principal/reserva/provider) tem
a sua linha; com alternativa=True quem acha a principal ocupada fica com
a reserva (2ª conta admin); a provider não tem alternativa (não entra na
área admin). Sem alternativa (agente_importacao_stokki) ou sem reserva no
config, tudo funciona como antes. Banco temporário, nenhuma rede.

Rodar (da raiz):
    py -3.11 -m unittest stokki.test_sessao_uso -v
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from stokki import contas, sessao_uso

CONFIG = {"stokki": {"usuario": "a@x", "senha": "1",
                     "provider": {"usuario": "p@y", "senha": "2"},
                     "reserva": {"usuario": "r@x", "senha": "3"}}}


class TravaPorContaTestCase(unittest.TestCase):

    def setUp(self):
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.db = Path(pasta.name) / "dados.db"
        for p in (mock.patch.object(sessao_uso, "DB_PATH", self.db),
                  mock.patch.object(contas, "_ler_config", return_value=CONFIG),
                  mock.patch.dict(sessao_uso._DONOS_DESTE_PROCESSO, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def _outro_processo_segura(self, conta, dono="outro"):
        """Linha gravada por outro processo (não entra em _DONOS_DESTE_PROCESSO)."""
        sessao_uso._conectar().close()
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT OR REPLACE INTO stokki_sessao_uso VALUES (?, ?, '2026-01-01 00:00:00', '2999-01-01 00:00:00')",
                     (conta, dono))
        conn.commit()
        conn.close()

    # -- adquirir ---------------------------------------------------------

    def test_livre_fica_com_a_principal(self):
        self.assertEqual(sessao_uso.adquirir("a", alternativa=True), "principal")
        self.assertEqual(sessao_uso.conta_do_processo(), "principal")

    def test_principal_ocupada_usa_a_reserva(self):
        self._outro_processo_segura("principal")
        self.assertEqual(sessao_uso.adquirir("a", alternativa=True), "reserva")
        self.assertEqual(sessao_uso.conta_do_processo(), "reserva")

    def test_sem_alternativa_continua_esperando_a_principal(self):
        # agente_importacao_stokki chama sem alternativa: comportamento antigo
        self._outro_processo_segura("principal")
        self.assertFalse(sessao_uso.adquirir("a"))

    def test_sem_reserva_no_config_nao_tem_alternativa(self):
        self._outro_processo_segura("principal")
        with mock.patch.object(contas, "_ler_config", return_value={"stokki": {"usuario": "a@x", "senha": "1"}}):
            self.assertFalse(sessao_uso.adquirir("a", alternativa=True))

    def test_provider_nao_tem_alternativa(self):
        self._outro_processo_segura("provider")
        self.assertFalse(sessao_uso.adquirir("a", conta="provider", alternativa=True))

    def test_provider_e_principal_nao_se_esperam(self):
        # expedição (provider) x pipeline (principal): travas separadas
        self._outro_processo_segura("principal")
        self.assertEqual(sessao_uso.adquirir("expedicao", conta="provider"), "provider")
        self.assertIsNone(sessao_uso.conta_do_processo())  # provider não é conta admin

    def test_painel_rodando_nao_ocupa_mais_as_contas(self):
        # 05/10: o script que o painel dispara entra na fila como qualquer
        # outro -- antes ficava esperando a própria execução do painel.
        sessao_uso._conectar().close()
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE painel_execucoes (id INTEGER PRIMARY KEY, agente_nome TEXT, status TEXT)")
        conn.execute("INSERT INTO painel_execucoes (agente_nome, status) VALUES ('Expedição', 'RODANDO')")
        conn.commit()
        conn.close()
        self.assertEqual(sessao_uso.adquirir("a"), "principal")

    # -- fila por ordem de chegada ------------------------------------------

    def _senha_na_fila(self, conta, dono, visto_em=None):
        sessao_uso._conectar().close()
        visto = visto_em or sessao_uso._agora().strftime(sessao_uso._FMT)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO stokki_fila (conta, dono, entrou_em, visto_em) VALUES (?, ?, ?, ?)",
                     (conta, dono, visto, visto))
        conn.commit()
        conn.close()

    def _soltar(self, conta):
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM stokki_sessao_uso WHERE chave = ?", (conta,))
        conn.commit()
        conn.close()

    def test_quem_chegou_depois_nao_fura_a_fila(self):
        # conta livre, mas "outro" está esperando desde antes
        self._senha_na_fila("principal", "outro")
        self.assertFalse(sessao_uso.adquirir("a"))
        self.assertEqual(sessao_uso.fila("principal"), ["outro"])

    def test_senha_de_processo_morto_sai_da_fila(self):
        self._senha_na_fila("principal", "morto", visto_em="2026-01-01 00:00:00")
        self.assertEqual(sessao_uso.adquirir("a"), "principal")
        self.assertEqual(sessao_uso.fila("principal"), [])

    def test_espera_na_fila_ate_liberar(self):
        import threading
        import time
        self._outro_processo_segura("principal")

        def soltar():
            time.sleep(0.4)
            self._soltar("principal")
        threading.Thread(target=soltar).start()
        self.assertEqual(sessao_uso.adquirir("a", esperar_segundos=None, intervalo=0.1), "principal")
        self.assertEqual(sessao_uso.fila("principal"), [])  # a senha saiu ao pegar a vez

    def test_desiste_no_limite_e_sai_da_fila(self):
        self._outro_processo_segura("principal")
        self.assertFalse(sessao_uso.adquirir("a", esperar_segundos=0.3, intervalo=0.1))
        self.assertEqual(sessao_uso.fila("principal"), [])

    def test_na_fila_com_reserva_pega_a_primeira_que_vagar(self):
        self._outro_processo_segura("principal")
        self._outro_processo_segura("reserva", "outro2")
        self.assertFalse(sessao_uso.adquirir("a", esperar_segundos=0.2, intervalo=0.1, alternativa=True))
        self._soltar("reserva")
        self.assertEqual(sessao_uso.adquirir("a", esperar_segundos=1, intervalo=0.1, alternativa=True), "reserva")

    def test_liberar_solta_so_a_linha_do_dono(self):
        self._outro_processo_segura("principal")
        sessao_uso.adquirir("a", alternativa=True)
        sessao_uso.liberar("a")
        self.assertIsNone(sessao_uso.em_uso(conta="reserva"))
        self.assertEqual(sessao_uso.em_uso(conta="principal"), "outro")
        self.assertIsNone(sessao_uso.conta_do_processo())

    # -- escolher_conta_login ---------------------------------------------

    def test_login_sem_trava_vai_pra_reserva_livre(self):
        self._outro_processo_segura("principal")
        self.assertEqual(sessao_uso.escolher_conta_login("principal", 0), ("reserva", None))

    def test_login_com_as_duas_admin_ocupadas_devolve_o_ocupante(self):
        self._outro_processo_segura("principal")
        self._outro_processo_segura("reserva", "outro2")
        self.assertEqual(sessao_uso.escolher_conta_login("principal", 0), ("principal", "outro"))

    def test_processo_que_pegou_a_reserva_loga_pela_reserva(self):
        self._outro_processo_segura("principal")
        sessao_uso.adquirir("pipeline", alternativa=True)
        self.assertEqual(sessao_uso.escolher_conta_login("principal", 0), ("reserva", None))

    def test_processo_com_trava_provider_nao_usa_ela_na_area_admin(self):
        # documentos-incremental segura a provider e abre a StokkiSession
        sessao_uso.adquirir("documentos-incremental", conta="provider")
        self._outro_processo_segura("principal")
        self.assertEqual(sessao_uso.escolher_conta_login("principal", 0), ("reserva", None))

    # -- Estação / documentos (provider) ------------------------------------

    def test_estacao_pega_a_vez_e_libera_ao_terminar(self):
        visto = {}

        @sessao_uso.na_vez_da_provider
        def listar(config):
            visto["dono"] = sessao_uso.em_uso(conta="provider")
            return "ok"
        self.assertEqual(listar({}), "ok")
        self.assertIn("/listar", visto["dono"])
        self.assertIsNone(sessao_uso.em_uso(conta="provider"))

    def test_estacao_desiste_sem_a_vez_em_vez_de_derrubar_a_expedicao(self):
        self._outro_processo_segura("provider", "expedicao")
        chamada = mock.Mock(__name__="listar")
        with mock.patch.object(sessao_uso, "ESPERA_MAXIMA_SEGUNDOS", 0.2):
            with self.assertRaises(RuntimeError):
                sessao_uso.na_vez_da_provider(chamada)({})
        chamada.assert_not_called()

    def test_quem_ja_segura_a_provider_nao_espera_por_si(self):
        sessao_uso.adquirir("documentos-incremental", conta="provider")
        chamada = mock.Mock(return_value=1, __name__="listar")
        self.assertEqual(sessao_uso.na_vez_da_provider(chamada)({}), 1)
        sessao_uso.vez_da_provider_para_login()  # não espera nem levanta

    def test_aguardar_vez_para_login_continua_por_conta(self):
        self._outro_processo_segura("principal")
        self.assertEqual(sessao_uso.aguardar_vez_para_login(0), "outro")
        self.assertIsNone(sessao_uso.aguardar_vez_para_login(0, conta="provider"))


class CredenciaisTestCase(unittest.TestCase):

    def test_credenciais_por_conta(self):
        self.assertEqual(contas.credenciais(CONFIG, "principal"), ("a@x", "1"))
        self.assertEqual(contas.credenciais(CONFIG, "provider"), ("p@y", "2"))
        self.assertEqual(contas.credenciais(CONFIG, "reserva"), ("r@x", "3"))

    def test_provider_vazio_cai_no_principal(self):
        self.assertEqual(contas.credenciais({"stokki": {"usuario": "a@x", "senha": "1"}}, "provider"), ("a@x", "1"))

    def test_reserva_igual_a_outra_conta_nao_vale(self):
        self.assertTrue(contas.reserva_disponivel(CONFIG))
        repetida = {"stokki": {**CONFIG["stokki"], "reserva": {"usuario": "A@x ", "senha": "9"}}}
        self.assertFalse(contas.reserva_disponivel(repetida))
        self.assertFalse(contas.reserva_disponivel({"stokki": {"usuario": "a@x", "senha": "1"}}))


class StokkiSessionContaTestCase(unittest.TestCase):

    def test_tela_do_painel_com_principal_ocupada_troca_usuario_e_cookies(self):
        # thread do waitress: não entra na fila, usa a conta livre
        import threading
        from stokki import auth
        sess = auth.StokkiSession.__new__(auth.StokkiSession)
        sess._config = CONFIG
        sess._usar_conta("principal")
        with mock.patch.object(sessao_uso, "escolher_conta_login", return_value=("reserva", None)):
            t = threading.Thread(target=sess._aguardar_trava_stokki)
            t.start()
            t.join()
        self.assertEqual((sess._conta, sess._usuario), ("reserva", "r@x"))
        self.assertEqual(sess._cookies_path, auth.COOKIES_PATH_RESERVA)

    def test_script_entra_na_fila_e_usa_a_conta_que_pegou(self):
        from stokki import auth
        with mock.patch.object(sessao_uso, "garantir_vez", return_value="reserva") as garantir, \
             mock.patch.object(auth.StokkiSession, "_carregar_ou_renovar_sessao"):
            sess = auth.StokkiSession(CONFIG)
        self.assertEqual(sess._usuario, "r@x")
        garantir.assert_called_once_with("principal", sessao_uso.ESPERA_MAXIMA_SEGUNDOS)

    def test_script_sem_a_vez_desiste_sem_logar(self):
        from stokki import auth
        with mock.patch.object(sessao_uso, "garantir_vez", return_value=None), \
             mock.patch.object(sessao_uso, "em_uso", return_value="pipeline"), \
             mock.patch.object(auth.StokkiSession, "_carregar_ou_renovar_sessao") as login:
            with self.assertRaises(auth.SessaoExpiradaError):
                auth.StokkiSession(CONFIG)
        login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
