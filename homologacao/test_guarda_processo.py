import asyncio
import http.client
import importlib.util
import ipaddress
import os
import poplib
import shutil
import smtplib
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import urllib3

from homologacao import guarda, registro

_TEM_HTTPX = importlib.util.find_spec("httpx") is not None
_TEM_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None
if _TEM_HTTPX:
    import httpx

_TEM_ALGUM_HTTPX = _TEM_HTTPX or importlib.util.find_spec("httpx2") is not None
_CAMADAS = (["smtp", "imap", "requests"] + (["httpx"] if _TEM_ALGUM_HTTPX else [])
            + (["playwright"] if _TEM_PLAYWRIGHT else []) + ["socket"])

_RAIZ = Path(__file__).resolve().parent.parent
_SITE = _RAIZ / "homologacao" / "site"
_IP_FALSO = "93.184.216.34"  # IP publico (so publico entra em _IPS_RESOLVIDOS); connect e mock, nunca conecta
_CONNECT_REAL = socket.socket.connect


def _resolver_falso(host, port, family=0, type=0, proto=0, flags=0):
    """getaddrinfo 'original' dos testes: nunca consulta DNS."""
    if isinstance(host, bytes):
        host = host.decode()
    if host == "localhost":
        host = "127.0.0.1"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = ipaddress.ip_address(_IP_FALSO)
    if ip.version == 6:
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (str(ip), port, 0, 0))]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (str(ip), port))]


def _connect_falso(sock, endereco):
    """connect 'original' dos testes. Loopback fora da 8100 so chega aqui pelo socketpair
    do proprio processo (Windows/asyncio): esse conecta de verdade, no listener local."""
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        ip = ipaddress.ip_address(endereco[0])
        if ip.is_loopback and endereco[1] != guarda.PORTA_FRESHHUB:
            return _CONNECT_REAL(sock, endereco)
    return None


class TestGuardaProcesso(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self._patch(registro, "CAMINHO_BANCO", Path(self.dir.name) / "dados.db")
        # originais que a guarda captura: mocks, rede real nunca
        self.resolver = mock.Mock(side_effect=_resolver_falso)
        self.connect = mock.Mock(side_effect=_connect_falso)
        self.connect_ex = mock.Mock(return_value=0)
        self.sendto = mock.Mock(return_value=1)
        self._patch(socket, "getaddrinfo", self.resolver)
        self._patch(socket.socket, "connect", self.connect)
        self._patch(socket.socket, "connect_ex", self.connect_ex)
        self._patch(socket.socket, "sendto", self.sendto)
        if _TEM_HTTPX:
            self.handle = mock.Mock(side_effect=lambda t, req: httpx.Response(200, request=req))
            self.handle_async = mock.AsyncMock(side_effect=lambda t, req: httpx.Response(200, request=req))
            self._patch(httpx.HTTPTransport, "handle_request", self.handle)
            self._patch(httpx.AsyncHTTPTransport, "handle_async_request", self.handle_async)
        guarda.instalar()

    def tearDown(self):
        guarda.desinstalar()

    def _patch(self, objeto, atributo, valor):
        p = mock.patch.object(objeto, atributo, valor)
        p.start()
        self.addCleanup(p.stop)

    def _conexoes_externas(self):
        """chamadas ao connect original fora do loopback (o socketpair local nao conta)."""
        saida = []
        for c in self.connect.call_args_list:
            sock, endereco = c.args
            if sock.family in (socket.AF_INET, socket.AF_INET6) and \
                    not ipaddress.ip_address(endereco[0]).is_loopback:
                saida.append(endereco)
        return saida

    def test_camadas_instaladas(self):
        self.assertTrue(guarda.INSTALADA)
        self.assertEqual(guarda.CAMADAS, _CAMADAS)

    # ------------------------------------------------------------ httpx

    @unittest.skipUnless(_TEM_HTTPX, "httpx ausente")
    def test_httpx_bloqueia_fora_da_anthropic(self):
        with self.assertRaises(httpx.ConnectError) as cm:
            httpx.get("https://example.com")
        self.assertTrue(str(cm.exception).startswith("indisponível na homologação:"))
        self.handle.assert_not_called()
        r = registro.listar_envios()[0]
        self.assertEqual((r["canal"], r["metodo"]), ("outro", "GET"))
        resp = httpx.Client().post("https://api.anthropic.com/v1/messages", json={"x": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.handle.call_count, 1)

    @unittest.skipUnless(_TEM_HTTPX, "httpx ausente")
    def test_httpx_bloqueia_stokki_e_porta_estranha(self):
        for url in ("https://freshlog.stokki.com.br/pt-br/administrator",
                    "https://api.anthropic.com:8443/v1/messages",
                    "http://127.0.0.1:2785/sessions/s/messages/send-text"):
            with self.subTest(url=url), self.assertRaises(httpx.ConnectError):
                httpx.Client().post(url, content=b"corpo")
        self.handle.assert_not_called()
        self.assertEqual(registro.listar_envios()[-1]["canal"], "stokki")
        self.assertEqual(registro.listar_envios()[-1]["conteudo"], "corpo")

    def test_httpx2_do_sdk_da_anthropic(self):
        """O anthropic 1.x usa o httpx2 (mesma API): a guarda troca o transporte dele tambem."""
        class ConnectError(Exception):
            def __init__(self, msg, request=None):
                super().__init__(msg)

        class HTTPTransport:
            def handle_request(self, request):
                return "resposta"

        class AsyncHTTPTransport:
            async def handle_async_request(self, request):
                return "resposta"

        class Pedido:
            def __init__(self, metodo, url):
                self.method, self.url, self.content = metodo, url, b"corpo"

        falso = type(sys)("httpx2")
        falso.ConnectError, falso.HTTPTransport, falso.AsyncHTTPTransport = \
            ConnectError, HTTPTransport, AsyncHTTPTransport
        guarda.desinstalar()
        with mock.patch.dict(sys.modules, {"httpx2": falso}):
            guarda.instalar()
            self.assertIn("httpx", guarda.CAMADAS)
            with self.assertRaises(ConnectError) as cm:
                HTTPTransport().handle_request(Pedido("POST", "https://api.vuupt.com/api/v1/services"))
            self.assertTrue(str(cm.exception).startswith("indisponível na homologação:"))
            self.assertEqual(registro.listar_envios()[-1]["canal"], "vuupt")
            with self.assertRaises(ConnectError):
                asyncio.run(AsyncHTTPTransport().handle_async_request(Pedido("GET", "https://example.com")))
            self.assertEqual(HTTPTransport().handle_request(
                Pedido("POST", "https://api.anthropic.com/v1/messages")), "resposta")
            guarda.desinstalar()
        # desinstalar devolve o transporte original
        self.assertEqual(HTTPTransport().handle_request(Pedido("POST", "https://example.com")), "resposta")

    @unittest.skipUnless(_TEM_HTTPX, "httpx ausente")
    def test_httpx_async(self):
        async def rodar():
            async with httpx.AsyncClient() as c:
                with self.assertRaises(httpx.ConnectError):
                    await c.get("https://example.com")
                return await c.post("https://api.anthropic.com/v1/messages", json={})

        resp = asyncio.run(rodar())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.handle_async.await_count, 1)
        self.assertEqual(registro.listar_envios()[0]["canal"], "outro")
        self.assertEqual(self._conexoes_externas(), [])

    # ------------------------------------------------------------ playwright

    @unittest.skipUnless(_TEM_PLAYWRIGHT, "playwright ausente")
    def test_playwright_importado_depois_bloqueia(self):
        from playwright.sync_api import sync_playwright
        with self.assertRaises(guarda.EnvioBloqueadoHomolog):
            with sync_playwright() as p:
                p.chromium.launch()
        r = registro.listar_envios()[0]
        self.assertEqual(r["canal"], "playwright")
        from playwright.async_api import async_playwright
        with self.assertRaises(guarda.EnvioBloqueadoHomolog):
            async_playwright()
        self.assertEqual(len(registro.listar_envios("playwright")), 2)

    # ------------------------------------------------------------ socket

    def test_socket_nome_fora_da_lista(self):
        for nome in ("freshlog.stokki.com.br", "FRESHLOG.STOKKI.COM.BR", b"rest.lalamove.com", "127.1"):
            with self.subTest(nome=nome), self.assertRaises(socket.gaierror):
                socket.getaddrinfo(nome, 443)
        self.resolver.assert_not_called()
        r = registro.listar_envios()[-1]
        self.assertEqual(r["canal"], "socket")
        self.assertIn("freshlog.stokki.com.br", r["destino"])

    def test_socket_nome_da_lista_resolve_e_conecta(self):
        info = socket.getaddrinfo("API.VUUPT.COM.", 443)
        self.assertEqual(info[0][4][0], _IP_FALSO)
        self.assertIn(_IP_FALSO, {str(ip) for ip in guarda._IPS_RESOLVIDOS})
        s = socket.socket()
        self.addCleanup(s.close)
        s.connect((_IP_FALSO, 443))
        self.assertEqual(self._conexoes_externas(), [(_IP_FALSO, 443)])
        self.assertEqual(s.connect_ex((_IP_FALSO, 443)), 0)

    def test_socket_ip_literal_em_getaddrinfo_passa(self):
        socket.getaddrinfo("8.8.8.8", 443)
        socket.getaddrinfo("::1", 2785)
        self.assertEqual(self.resolver.call_count, 2)
        self.assertEqual(guarda._IPS_RESOLVIDOS, set())

    def test_socket_loopback_so_8100(self):
        for endereco in (("127.0.0.1", 2785), ("::1", 2785), ("127.0.0.2", 5432), ("localhost", 2785)):
            with self.subTest(endereco=endereco), self.assertRaises(ConnectionRefusedError):
                socket.create_connection(endereco)
        self.assertEqual(self.connect.call_count, 0)
        socket.create_connection(("127.0.0.1", 8100)).close()
        self.assertEqual(self.connect.call_args.args[1], ("127.0.0.1", 8100))
        self.assertEqual(registro.listar_envios()[0]["canal"], "socket")

    def test_socket_loopback_disfarcado(self):
        casos = ((socket.AF_INET6, ("::ffff:127.0.0.1", 2785)), (socket.AF_INET, ("0.0.0.0", 2785)),
                 (socket.AF_INET, ("0.0.0.0", 25)), (socket.AF_INET6, ("::", 2785)),
                 (socket.AF_INET6, ("::127.0.0.1", 2785)), (socket.AF_INET6, ("2002:7f00:1::1", 2785)))
        for familia, endereco in casos:
            s = socket.socket(familia)
            self.addCleanup(s.close)
            with self.subTest(endereco=endereco), self.assertRaises(ConnectionRefusedError):
                s.connect(endereco)
        self.connect.assert_not_called()
        s6 = socket.socket(socket.AF_INET6)
        self.addCleanup(s6.close)
        s6.connect(("::ffff:127.0.0.1", 8100))
        self.assertEqual(self.connect.call_args.args[1], ("::ffff:127.0.0.1", 8100))

    def test_socket_resolve_app_vuupt(self):
        """vuupt_client.py usa app.vuupt.com: o nome resolve (o metodo e filtrado no requests)."""
        socket.getaddrinfo("app.vuupt.com", 443)
        with self.assertRaises(socket.gaierror):
            socket.getaddrinfo("outro.vuupt.com", 443)

    def test_socket_resolucao_para_endereco_interno_nao_libera(self):
        for ip in ("0.0.0.0", "::ffff:127.0.0.1", "::", "10.0.0.5", "169.254.169.254", "224.0.0.1"):
            self.resolver.side_effect = lambda host, port, *a, ip=ip, **k: _resolver_falso(ip, port)
            socket.getaddrinfo("api.vuupt.com", 443)
        self.assertEqual(guarda._IPS_RESOLVIDOS, set())
        casos = ((socket.AF_INET, ("0.0.0.0", 2785)), (socket.AF_INET, ("0.0.0.0", 25)),
                 (socket.AF_INET6, ("::ffff:127.0.0.1", 2785)), (socket.AF_INET, ("10.0.0.5", 443)),
                 (socket.AF_INET, ("169.254.169.254", 80)))
        for familia, endereco in casos:
            s = socket.socket(familia)
            self.addCleanup(s.close)
            with self.subTest(endereco=endereco), self.assertRaises(ConnectionRefusedError):
                s.connect(endereco)
        self.connect.assert_not_called()

    def test_socket_ip_literal_nao_resolvido(self):
        s = socket.socket()
        self.addCleanup(s.close)
        with self.assertRaises(ConnectionRefusedError):
            s.connect(("8.8.8.8", 443))
        with self.assertRaises(ConnectionRefusedError):
            s.connect_ex(("8.8.8.8", 443))
        with self.assertRaises(ConnectionRefusedError):
            s.connect(("example.com", 443))  # nome direto no connect: na duvida, bloqueia
        self.connect.assert_not_called()
        self.connect_ex.assert_not_called()
        r = registro.listar_envios()[-1]
        self.assertEqual((r["canal"], r["metodo"], r["destino"]), ("socket", "connect", "8.8.8.8:443"))

    def test_socket_udp_sendto(self):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(u.close)
        with self.assertRaises(ConnectionRefusedError):
            u.sendto(b"x", ("8.8.8.8", 53))
        with self.assertRaises(ConnectionRefusedError):
            u.sendto(b"x", 0, ("127.0.0.53", 53))
        self.sendto.assert_not_called()

    def test_ssl_socket_passa_pelo_connect_guardado(self):
        ctx = ssl.create_default_context()
        s = ctx.wrap_socket(socket.socket(), server_hostname="freshlog.stokki.com.br")
        self.addCleanup(s.close)
        with self.assertRaises(ConnectionRefusedError):
            s.connect(("8.8.8.8", 443))
        self.connect.assert_not_called()

    def test_socket_segura_quem_foge_do_requests(self):
        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen("http://example.com", timeout=5)
        with self.assertRaises(ConnectionRefusedError):
            http.client.HTTPConnection("127.0.0.1", 2785, timeout=5).request("POST", "/x", body=b"{}")
        with self.assertRaises(urllib3.exceptions.HTTPError):
            urllib3.PoolManager(retries=False).request("GET", "http://example.com/")
        with self.assertRaises(socket.gaierror):
            smtplib.LMTP("smtp.gmail.com", 25)
        with self.assertRaises(socket.gaierror):
            poplib.POP3("pop.gmail.com", 110, timeout=5)
        # so o IP literal do http.client chega no resolver; nenhum nome, nenhuma conexao
        self.assertEqual([c.args[0] for c in self.resolver.call_args_list], ["127.0.0.1"])
        self.connect.assert_not_called()
        canais = {r["canal"] for r in registro.listar_envios()}
        self.assertEqual(canais, {"socket"})

    def test_servidor_continua_funcionando(self):
        s = socket.socket()
        self.addCleanup(s.close)
        s.bind(("127.0.0.1", 0))
        s.listen()
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.sendall(b"ok")
        self.assertEqual(b.recv(2), b"ok")
        with sqlite3.connect(":memory:") as con:
            self.assertEqual(con.execute("select 1").fetchone(), (1,))

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "sem AF_UNIX")
    def test_af_unix_passa(self):
        u = socket.socket(socket.AF_UNIX)
        self.addCleanup(u.close)
        u.connect("/run/qualquer.sock")
        self.assertEqual(self.connect.call_args.args[1], "/run/qualquer.sock")

    def test_desinstalar_restaura(self):
        socket.getaddrinfo("api.vuupt.com", 443)
        guarda.desinstalar()
        self.assertIs(socket.getaddrinfo, self.resolver)
        self.assertIs(socket.socket.connect, self.connect)
        self.assertIs(socket.socket.connect_ex, self.connect_ex)
        self.assertEqual(guarda._IPS_RESOLVIDOS, set())
        if _TEM_HTTPX:
            self.assertIs(httpx.HTTPTransport.handle_request, self.handle)
            self.assertIs(httpx.AsyncHTTPTransport.handle_async_request, self.handle_async)
        if _TEM_PLAYWRIGHT:
            from playwright.sync_api import sync_playwright
            self.assertEqual(sync_playwright.__module__, "playwright.sync_api")
        self.assertEqual(guarda.CAMADAS, [])


class TestPlaywrightFalhaFechada(unittest.TestCase):
    """Pacote playwright presente e um submodulo que nao importa: instalar() levanta e desfaz tudo."""

    def setUp(self):
        self.addCleanup(guarda.desinstalar)
        self.smtp_antes = smtplib.SMTP

    def _instalar_com_falha_em(self, quebrado):
        real = guarda.import_module

        def importar(nome, *a, **k):
            if nome == quebrado:
                raise ImportError("quebrado no teste: %s" % nome)
            return real(nome, *a, **k)

        with mock.patch.object(guarda, "find_spec", return_value=object()), \
                mock.patch.object(guarda, "import_module", side_effect=importar):
            with self.assertRaises(RuntimeError) as cm:
                guarda.instalar()
        self.assertIn(quebrado, str(cm.exception))
        self.assertFalse(guarda.INSTALADA)
        self.assertEqual(guarda.CAMADAS, [])
        self.assertIs(smtplib.SMTP, self.smtp_antes)

    @unittest.skipUnless(_TEM_PLAYWRIGHT, "playwright ausente")
    def test_sync_api_quebrado_aborta(self):
        self._instalar_com_falha_em("playwright.sync_api")

    @unittest.skipUnless(_TEM_PLAYWRIGHT, "playwright ausente")
    def test_async_api_quebrado_aborta(self):
        self._instalar_com_falha_em("playwright.async_api")
        from playwright.sync_api import sync_playwright
        self.assertEqual(sync_playwright.__module__, "playwright.sync_api")  # o sync trocado foi desfeito

    def test_pacote_ausente_e_camada_opcional(self):
        with mock.patch.object(guarda, "find_spec", return_value=None):
            guarda.instalar()  # nada conecta: so troca funcoes, desfeitas no cleanup
        self.assertTrue(guarda.INSTALADA)
        self.assertNotIn("playwright", guarda.CAMADAS)


class TestSitecustomize(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.banco = Path(self.dir.name) / "dados.db"
        self.env = {k: v for k, v in os.environ.items() if k not in ("FRESH_AMBIENTE", "PYTHONPATH")}
        self.env["HOMOLOG_REGISTRO_BANCO"] = str(self.banco)

    def _rodar(self, codigo, env, site=_SITE):
        return subprocess.run([sys.executable, "-c", codigo], env={**env, "PYTHONPATH": str(site)},
                              cwd=self.dir.name, capture_output=True, text=True, timeout=180)

    def test_subprocesso_herda_guarda(self):
        # sem a guarda no filho o SMTP seria real: sai antes de conectar
        codigo = ("import smtplib, sys\n"
                  "if smtplib.SMTP.__name__ != 'SMTPFalso': sys.exit('sem guarda no filho')\n"
                  "s=smtplib.SMTP('smtp.gmail.com',587); s.sendmail('a@x','b@y','Subject: filho\\n\\nx')")
        r = self._rodar(codigo, {**self.env, "FRESH_AMBIENTE": "homolog"})
        self.assertEqual(r.returncode, 0, r.stderr)
        con = sqlite3.connect(str(self.banco))
        try:
            linhas = con.execute("SELECT canal, resumo FROM homolog_envios").fetchall()
        finally:
            con.close()
        self.assertEqual(linhas, [("email", "filho")])

    def test_subprocesso_tem_todas_as_camadas(self):
        codigo = "from homologacao import guarda; print(','.join(guarda.CAMADAS))"
        r = self._rodar(codigo, {**self.env, "FRESH_AMBIENTE": "homolog"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), ",".join(_CAMADAS))

    def test_sitecustomize_sem_variavel_nao_instala(self):
        codigo = "import smtplib, sys; print(smtplib.SMTP.__name__); print('homologacao' in sys.modules)"
        r = self._rodar(codigo, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["SMTP", "False"])

    def test_sitecustomize_falha_na_instalacao_aborta(self):
        # copia sem o pacote homologacao ao lado: a instalacao falha e o processo nao segue
        site = Path(self.dir.name) / "a" / "b"
        site.mkdir(parents=True)
        shutil.copy(_SITE / "sitecustomize.py", site / "sitecustomize.py")
        r = self._rodar("print('SEGUIU')", {**self.env, "FRESH_AMBIENTE": "homolog"}, site=site)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("SEGUIU", r.stdout)


if __name__ == "__main__":
    unittest.main()
