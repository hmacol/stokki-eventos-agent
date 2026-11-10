import imaplib
import smtplib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
import requests.adapters

import email_utils
import integracao_openwa
from homologacao import guarda, registro

# capturados antes de qualquer instalar(), para conferir que desinstalar() restaura
_SMTP_ORIGINAL = smtplib.SMTP
_SMTP_SSL_ORIGINAL = smtplib.SMTP_SSL
_IMAP4_SSL_ORIGINAL = imaplib.IMAP4_SSL

_CFG_OPENWA = {"base_url": "http://127.0.0.1:2785", "api_key": "HOMOLOG-SEM-CHAVE", "sessao": "s"}


def _resposta(request, status=200, headers=None, corpo=b"{}"):
    r = requests.Response()
    r.status_code = status
    r.url = request.url
    r.request = request
    r.headers.update(headers or {})
    r._content = corpo
    r._content_consumed = True
    return r


class TestGuardaRede(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        p = mock.patch.object(registro, "CAMINHO_BANCO", Path(self.dir.name) / "dados.db")
        p.start()
        self.addCleanup(p.stop)
        # rede real nunca: o "original" que a guarda captura e um mock que devolve 200
        self.send = mock.Mock(side_effect=lambda adapter, request, **kw: _resposta(request))
        p = mock.patch.object(requests.adapters.HTTPAdapter, "send", self.send)
        p.start()
        self.addCleanup(p.stop)
        guarda.instalar()

    def tearDown(self):
        guarda.desinstalar()

    def test_decidir_regras(self):
        casos = [
            ("GET", "https://api.vuupt.com/api/v1/services", "passa"),
            ("PUT", "https://api.vuupt.com/api/v1/services/1", "bloqueia"),
            ("GET", "https://API.VUUPT.COM:443/x", "passa"),
            ("GET", "https://freshlog.stokki.com.br/pt-br/x", "bloqueia"),
            ("POST", "http://127.0.0.1:2785/sessions/s/messages/send-text", "falso_openwa"),
            ("GET", "http://localhost:2785/sessions/s/contacts/check/1", "falso_openwa"),
            ("POST", "http://127.0.0.2:2785/x", "falso_openwa"),
            ("POST", "http://[::1]:2785/x", "falso_openwa"),
            ("GET", "http://127.0.0.1:8100/api/pool", "passa"),
            ("POST", "http://127.0.0.1:8100/api/eventos", "bloqueia"),
            ("GET", "http://127.0.0.1:8101/api/pool", "bloqueia"),
            ("POST", "https://oauth2.googleapis.com/token", "passa"),
            ("POST", "https://oauth2.googleapis.com/outra", "bloqueia"),
            ("GET", "https://storage.googleapis.com/b/o", "passa"),
            ("POST", "https://storage.googleapis.com/upload/storage/v1/b/x/o", "bloqueia"),
            ("GET", "https://maps.googleapis.com/maps/api/geocode/json", "passa"),
            ("POST", "https://rest.lalamove.com/v3/orders", "bloqueia"),
            ("GET", "https://viacep.com.br/ws/01001000/json/", "passa"),
            ("get", "https://api.vuupt.com/x", "passa"),
            # extras: na duvida, bloqueia
            ("HEAD", "https://api.vuupt.com/x", "passa"),
            ("HEAD", "https://storage.googleapis.com/b/o", "passa"),
            ("GET", "https://brasilapi.com.br/api/cep/v1/01001000", "passa"),
            ("POST", "https://api.anthropic.com/v1/messages", "passa"),
            ("POST", "https://viacep.com.br/ws/01001000/json/", "bloqueia"),
            ("GET", "https://oauth2.googleapis.com/token", "bloqueia"),
            ("HEAD", "http://127.0.0.1:8100/api/pool", "bloqueia"),
            ("GET", "https://api.vuupt.com:8443/x", "bloqueia"),
            ("GET", "ftp://api.vuupt.com/x", "bloqueia"),
            ("GET", "https://api.vuupt.com@evil.example/x", "bloqueia"),
            ("GET", "https://api.vuupt.com.evil.example/x", "bloqueia"),
            ("GET", "http://0.0.0.0:2785/x", "bloqueia"),
            ("GET", "http://127.0.0.1:2786/x", "bloqueia"),
            ("GET", "https://evil.example/x", "bloqueia"),
            # parser do urllib3 (onde o adapter conecta) manda; truques de URL bloqueiam
            ("GET", "http://evil.example\\@api.vuupt.com/x", "bloqueia"),
            ("GET", "https://api.vuupt.com\\@evil.example/x", "bloqueia"),
            ("GET", "https://api.vuupt.com /x", "bloqueia"),
            ("GET", "https://api.vuupt.com\t/x", "bloqueia"),
            ("GET", "https://api.vuupt.com\n.evil.example/x", "bloqueia"),
            ("GET", "https://api.vuupt.com\x00/x", "bloqueia"),
            ("GET", "https://api.vuupt.com:abc/x", "bloqueia"),
            ("GET", "nao e url", "bloqueia"),
            ("GET", "", "bloqueia"),
            ("GET", None, "bloqueia"),
        ]
        for metodo, url, esperado in casos:
            with self.subTest(metodo=metodo, url=url):
                self.assertEqual(guarda.decidir(metodo, url), esperado)

    def test_canal_de(self):
        casos = [
            ("https://freshlog.stokki.com.br/pt-br/x", "stokki"),
            ("https://api.vuupt.com/x", "vuupt"),
            ("http://127.0.0.1:2785/x", "whatsapp"),
            ("https://rest.lalamove.com/v3/orders", "lalamove"),
            ("https://storage.googleapis.com/upload/x", "gcs"),
            ("http://127.0.0.1:8100/api/eventos", "freshhub"),
            ("http://localhost:8101/api/pool", "freshhub"),
            ("https://abc.supabase.co/rest/v1/x", "supabase"),
            ("https://freshhub.com.br/x", "supabase"),
            ("https://api.anthropic.com/v1/messages", "anthropic"),
            ("https://sheets.googleapis.com/v4/x", "google"),
            ("https://evil.example/x", "outro"),
            ("nao e url", "outro"),
        ]
        for url, esperado in casos:
            with self.subTest(url=url):
                self.assertEqual(guarda.canal_de(url), esperado)

    def test_camadas_instaladas(self):
        self.assertTrue(guarda.INSTALADA)
        self.assertEqual(guarda.CAMADAS, ["smtp", "imap", "requests"])

    def test_smtp_registra_e_finge_sucesso(self):
        ok = email_utils.enviar_email(["a@b.com"], "Assunto X", "<p>oi</p>", {"remetente": "x@y.com"})
        self.assertTrue(ok)
        r = registro.listar_envios("email")[0]
        self.assertEqual(r["destino"], "a@b.com")
        self.assertEqual(r["resumo"], "Assunto X")
        self.assertIn("<p>oi</p>", r["conteudo"])

    def test_smtp_sendmail_e_cabecalhos(self):
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login("u", "p")
            self.assertEqual(s.sendmail("x@y.com", "c@d.com", "Subject: Oi\r\n\r\ncorpo"), {})
        r = registro.listar_envios("email")[0]
        self.assertEqual((r["destino"], r["resumo"]), ("c@d.com", "Oi"))
        self.assertIn("corpo", r["conteudo"])
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"], msg["To"], msg["Cc"] = "Sem to_addrs", "e@f.com", "g@h.com"
        msg.set_content("x")
        smtplib.SMTP().send_message(msg)
        self.assertEqual(registro.listar_envios("email")[0]["destino"], "e@f.com, g@h.com")

    def test_smtp_importado_por_nome_depois_de_instalar(self):
        from smtplib import SMTP, SMTP_SSL
        self.assertIs(SMTP, guarda.SMTPFalso)
        self.assertIs(SMTP_SSL, guarda.SMTPFalso)
        self.assertEqual(SMTP.__name__, "SMTPFalso")

    def test_imap_bloqueia(self):
        with self.assertRaises(guarda.EnvioBloqueadoHomolog):
            imaplib.IMAP4_SSL("imap.gmail.com", 993)
        itens = registro.listar_envios("imap")
        self.assertEqual(len(itens), 1)
        # quem faz `except imaplib.IMAP4.error` continua funcionando
        self.assertTrue(issubclass(imaplib.IMAP4.error, Exception))

    def test_whatsapp_resposta_falsa(self):
        ok, msg_id = integracao_openwa.enviar_texto(_CFG_OPENWA, "5511999999999@c.us", "linha1\nlinha2")
        r = registro.listar_envios("whatsapp")[0]
        self.assertEqual((ok, msg_id), (True, "homolog-%d" % r["id"]))
        self.assertEqual(r["destino"], "5511999999999@c.us")
        self.assertEqual(r["resumo"], "linha1")
        self.assertIn("linha2", r["conteudo"])
        self.assertIs(integracao_openwa.numero_existe(_CFG_OPENWA, "5511999999999"), True)
        self.send.assert_not_called()

    def test_stokki_bloqueia_qualquer_metodo(self):
        with self.assertRaises(guarda.EnvioBloqueadoHomolog) as cm:
            requests.get("https://freshlog.stokki.com.br/pt-br/administrator")
        self.assertIsInstance(cm.exception, requests.ConnectionError)
        self.assertTrue(str(cm.exception).startswith("indisponível na homologação:"))
        r = registro.listar_envios()[0]
        self.assertEqual(r["canal"], "stokki")
        self.assertEqual(r["metodo"], "GET")
        self.send.assert_not_called()

    def test_vuupt_escrita_bloqueia_e_leitura_passa(self):
        with self.assertRaises(guarda.EnvioBloqueadoHomolog):
            requests.put("https://api.vuupt.com/api/v1/services/1", json={"status": "x"})
        r = registro.listar_envios()[0]
        self.assertEqual((r["canal"], r["metodo"]), ("vuupt", "PUT"))
        self.assertEqual(r["destino"], "https://api.vuupt.com/api/v1/services/1")
        self.assertIn('"status"', r["conteudo"])
        self.send.assert_not_called()
        resp = requests.get("https://api.vuupt.com/api/v1/services")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.send.call_count, 1)

    def test_vuupt_ritmo_um_por_segundo(self):
        with mock.patch("time.monotonic", return_value=100.0), mock.patch("time.sleep") as dormir:
            for _ in range(3):
                requests.get("https://api.vuupt.com/api/v1/services")
        self.assertEqual(self.send.call_count, 3)
        self.assertGreaterEqual(sum(c.args[0] for c in dormir.call_args_list), 2.0)

    def test_redirecionamento_para_host_proibido_bloqueia(self):
        self.send.side_effect = lambda adapter, request, **kw: _resposta(
            request, 302, {"Location": "https://evil.example/x"}, b"")
        with self.assertRaises(guarda.EnvioBloqueadoHomolog):
            requests.get("https://viacep.com.br/ws/01001000/json/")
        self.assertEqual(self.send.call_count, 1)
        self.assertTrue(registro.listar_envios()[0]["destino"].startswith("https://evil.example"))

    def test_url_com_barra_invertida_bloqueia_de_ponta_a_ponta(self):
        # urlsplit ve api.vuupt.com, o urllib3 conectaria em evil.example
        p = requests.Request("GET", "https://viacep.com.br/").prepare()
        p.url = "http://evil.example\\@api.vuupt.com/x"
        with self.assertRaises(guarda.EnvioBloqueadoHomolog):
            requests.Session().send(p)
        self.send.assert_not_called()
        self.assertEqual(registro.listar_envios()[0]["destino"], p.url)

    def test_instalar_idempotente(self):
        guarda.instalar()
        self.assertEqual(guarda.CAMADAS, ["smtp", "imap", "requests"])
        guarda.desinstalar()
        self.assertIs(smtplib.SMTP, _SMTP_ORIGINAL)

    def test_desinstalar_restaura(self):
        guarda.desinstalar()
        self.assertIs(smtplib.SMTP, _SMTP_ORIGINAL)
        self.assertIs(smtplib.SMTP_SSL, _SMTP_SSL_ORIGINAL)
        self.assertIs(imaplib.IMAP4_SSL, _IMAP4_SSL_ORIGINAL)
        self.assertIs(requests.adapters.HTTPAdapter.send, self.send)
        self.assertIs(guarda.INSTALADA, False)
        self.assertEqual(guarda.CAMADAS, [])


if __name__ == "__main__":
    unittest.main()
