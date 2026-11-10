"""Guarda de rede da homologacao: nada sai do processo.

instalar() troca, no processo inteiro, os pontos por onde algo sairia:
- smtplib.SMTP / SMTP_SSL -> SMTPFalso (registra a mensagem e finge sucesso)
- imaplib.IMAP4 / IMAP4_SSL -> recusa (registra e levanta)
- requests.adapters.HTTPAdapter.send -> decide por host e metodo (ve cada salto de redirecionamento)
- httpx (HTTPTransport / AsyncHTTPTransport) -> so api.anthropic.com
- playwright sync_playwright / async_playwright -> recusa (nenhum Chromium sobe)
- socket (rede de seguranca) -> getaddrinfo so resolve NOMES_PERMITIDOS; connect/sendto so para
  IP vindo dessa resolucao, AF_UNIX ou loopback:8100. Segura urllib, http.client, urllib3 cru, LMTP, POP3.
Cada bloqueio vai para registro.registrar_envio. Na duvida, bloqueia.
Novas camadas entram em _INSTALADORES; _trocar() guarda o original para desinstalar().
Camada opcional (httpx, playwright) cujo pacote nao existe devolve False e nao entra em CAMADAS.
"""
import copy
import email
import errno
import imaplib
import ipaddress
import json
import smtplib
import socket
import threading
import time
from urllib.parse import urlsplit

import requests
import requests.adapters
from urllib3.util import parse_url

from homologacao import registro

INSTALADA = False
CAMADAS = []  # nome de cada camada instalada, na ordem

_ORIGINAIS = []  # (objeto, atributo, valor original), restaurados ao contrario
_trava_instalar = threading.RLock()

PORTA_OPENWA = 2785
PORTA_FRESHHUB = 8100
PORTA_FRESHHUB_HML = 8101

# host externo -> (metodos liberados, caminho exigido ou None)
_LIBERADOS = {
    "api.vuupt.com": ({"GET", "HEAD"}, None),
    "maps.googleapis.com": ({"GET"}, None),
    "storage.googleapis.com": ({"GET", "HEAD"}, None),
    "oauth2.googleapis.com": ({"POST"}, "/token"),
    "viacep.com.br": ({"GET"}, None),
    "brasilapi.com.br": ({"GET"}, None),
    "api.anthropic.com": (None, None),  # qualquer metodo
}

INTERVALO_VUUPT = 1.0
_trava_vuupt = threading.Lock()
_ultimo_vuupt = None


class EnvioBloqueadoHomolog(requests.exceptions.ConnectionError):
    """Subclasse de ConnectionError: cai no tratamento de 'servico fora' que ja existe."""


def _mensagem(alvo):
    return "indisponível na homologação: %s" % alvo


# ---------------------------------------------------------------- regras

def _alvo(url):
    """(esquema, host, porta, caminho) normalizados; porta padrao 80/443 vira None.

    Usa o parser do proprio urllib3 (o host em que o adapter conecta). Levanta ValueError
    (-> bloqueia) para barra invertida, espaco ou controle na URL, ou se urlsplit e urllib3
    discordarem do host/porta (truque de URL que engana um parser e nao o outro)."""
    if any(c == "\\" or c.isspace() or ord(c) < 0x20 or ord(c) == 0x7f for c in url):
        raise ValueError("url suspeita")
    partes = parse_url(url)
    host = (partes.host or "").lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    porta = partes.port
    outra = urlsplit(url)
    if (outra.hostname or "").lower() != host or outra.port != porta:
        raise ValueError("parsers discordam")
    if porta in (80, 443):
        porta = None
    return (partes.scheme or "").lower(), host, porta, partes.path or "/"


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def decidir(metodo, url):
    """'passa', 'falso_openwa' ou 'bloqueia'. Qualquer duvida ou erro: 'bloqueia'."""
    try:
        metodo = (metodo or "").upper()
        esquema, host, porta, caminho = _alvo(url)
        if esquema not in ("http", "https") or not host:
            return "bloqueia"
        if _loopback(host):
            if porta == PORTA_OPENWA:
                return "falso_openwa"
            if porta == PORTA_FRESHHUB and metodo == "GET":
                return "passa"
            return "bloqueia"
        regra = _LIBERADOS.get(host)
        if regra is None or porta is not None:
            return "bloqueia"
        metodos, caminho_exigido = regra
        if metodos is not None and metodo not in metodos:
            return "bloqueia"
        if caminho_exigido is not None and caminho != caminho_exigido:
            return "bloqueia"
        return "passa"
    except Exception:
        return "bloqueia"


def _no_dominio(host, dominio):
    return host == dominio or host.endswith("." + dominio)


def canal_de(url):
    try:
        _esquema, host, porta, _caminho = _alvo(url)
    except Exception:
        return "outro"
    if _loopback(host):
        if porta == PORTA_OPENWA:
            return "whatsapp"
        if porta in (PORTA_FRESHHUB, PORTA_FRESHHUB_HML):
            return "freshhub"
        return "outro"
    if _no_dominio(host, "stokki.com.br"):
        return "stokki"
    if _no_dominio(host, "vuupt.com"):
        return "vuupt"
    if _no_dominio(host, "lalamove.com"):
        return "lalamove"
    if host == "storage.googleapis.com":
        return "gcs"
    if _no_dominio(host, "supabase.co") or _no_dominio(host, "freshhub.com.br"):
        return "supabase"
    if _no_dominio(host, "anthropic.com"):
        return "anthropic"
    if _no_dominio(host, "googleapis.com") or _no_dominio(host, "google.com"):
        return "google"
    return "outro"


# ---------------------------------------------------------------- smtp

def _texto_da_mensagem(msg):
    """msg.as_string() com as partes de texto decodificadas (base64/qp -> 8bit), para a tela ler o HTML."""
    try:
        copia = copy.deepcopy(msg)
        for parte in copia.walk():
            if parte.is_multipart() or parte.get_content_maintype() != "text":
                continue
            if (parte.get("Content-Transfer-Encoding") or "").lower() not in ("base64", "quoted-printable"):
                continue
            dados = parte.get_payload(decode=True) or b""
            texto = dados.decode(parte.get_content_charset() or "utf-8", errors="replace")
            del parte["Content-Transfer-Encoding"]
            parte["Content-Transfer-Encoding"] = "8bit"
            parte.set_payload(texto)
        return copia.as_string()
    except Exception:
        try:
            return msg.as_string()
        except Exception:
            return repr(msg)


def _destinos(to_addrs, msg=None):
    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]
    if to_addrs:
        return ", ".join(str(a) for a in to_addrs)
    if msg is not None:
        nomes = [str(v) for campo in ("To", "Cc", "Bcc") for v in (msg.get_all(campo) or [])]
        return ", ".join(nomes)
    return ""


class SMTPFalso:
    """Substitui smtplib.SMTP e SMTP_SSL: registra a mensagem inteira e devolve sucesso."""

    def __init__(self, host="", port=0, *a, **k):
        self.host = host
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, host="localhost", port=0, *a, **k):
        return 220, b"homolog"

    def ehlo(self, *a, **k):
        return 250, b"homolog"

    helo = ehlo

    def starttls(self, *a, **k):
        return 220, b"homolog"

    def login(self, *a, **k):
        return 235, b"homolog"

    def quit(self):
        return 221, b"homolog"

    def close(self):
        pass

    def set_debuglevel(self, *a, **k):
        pass

    def send_message(self, msg, from_addr=None, to_addrs=None, *a, **k):
        registro.registrar_envio("email", "SMTP", _destinos(to_addrs, msg),
                                 str(msg.get("Subject") or ""), _texto_da_mensagem(msg))
        return {}

    def sendmail(self, from_addr, to_addrs, msg, *a, **k):
        try:
            if isinstance(msg, (bytes, bytearray)):
                obj = email.message_from_bytes(bytes(msg))
            else:
                obj = email.message_from_string(msg)
            assunto = str(obj.get("Subject") or "")
        except Exception:
            assunto = ""
        registro.registrar_envio("email", "SMTP", _destinos(to_addrs), assunto, msg)
        return {}


def _instalar_smtp():
    _trocar(smtplib, "SMTP", SMTPFalso)
    _trocar(smtplib, "SMTP_SSL", SMTPFalso)


# ---------------------------------------------------------------- imap

class IMAPRecusado:
    """Substitui imaplib.IMAP4 e IMAP4_SSL: registra e levanta. Mantem error/abort/readonly
    para quem escreve `except imaplib.IMAP4.error`."""
    error = imaplib.IMAP4.error
    abort = imaplib.IMAP4.abort
    readonly = imaplib.IMAP4.readonly

    def __init__(self, host="", port=0, *a, **k):
        destino = "%s:%s" % (host, port)
        registro.registrar_envio("imap", "IMAP", destino, None, None)
        raise EnvioBloqueadoHomolog(_mensagem("IMAP %s" % destino))


def _instalar_imap():
    _trocar(imaplib, "IMAP4", IMAPRecusado)
    _trocar(imaplib, "IMAP4_SSL", IMAPRecusado)


# ---------------------------------------------------------------- requests

_send_original = None


def _esperar_vez_vuupt():
    """No maximo 1 requisicao por INTERVALO_VUUPT por processo (conta compartilhada com a producao)."""
    global _ultimo_vuupt
    with _trava_vuupt:
        agora = time.monotonic()
        if _ultimo_vuupt is not None and _ultimo_vuupt + INTERVALO_VUUPT > agora:
            proximo = _ultimo_vuupt + INTERVALO_VUUPT
            time.sleep(proximo - agora)
            _ultimo_vuupt = proximo
        else:
            _ultimo_vuupt = agora


def _resposta_falsa(request, status, dados):
    r = requests.Response()
    r.status_code = status
    r.reason = "Created"
    r.url = request.url
    r.request = request
    r.headers["Content-Type"] = "application/json"
    r._content = json.dumps(dados).encode("utf-8")
    r._content_consumed = True
    r.encoding = "utf-8"
    return r


def _falso_openwa(request, metodo, url):
    try:
        corpo = json.loads(request.body) if request.body else {}
        if not isinstance(corpo, dict):
            corpo = {}
    except Exception:
        corpo = {}
    texto = corpo.get("text")
    resumo = str(texto).splitlines()[0] if texto else None
    envio_id = registro.registrar_envio("whatsapp", metodo, corpo.get("chatId") or url,
                                        resumo, request.body)
    return _resposta_falsa(request, 201, {"messageId": "homolog-%s" % envio_id, "exists": True})


def _send_guardado(self, request, **kw):
    metodo = (request.method or "").upper()
    url = request.url or ""
    decisao = decidir(metodo, url)
    if decisao == "passa":
        if canal_de(url) == "vuupt":
            _esperar_vez_vuupt()
        return _send_original(self, request, **kw)
    if decisao == "falso_openwa":
        return _falso_openwa(request, metodo, url)
    registro.registrar_envio(canal_de(url), metodo, url, None, request.body)
    try:
        partes = urlsplit(url)
        alvo = "%s %s%s" % (metodo, partes.netloc.rpartition("@")[2], partes.path)  # sem query nem usuario
    except Exception:
        alvo = metodo
    raise EnvioBloqueadoHomolog(_mensagem(alvo), request=request)


def _instalar_requests():
    global _send_original
    _send_original = requests.adapters.HTTPAdapter.send
    _trocar(requests.adapters.HTTPAdapter, "send", _send_guardado)


# ---------------------------------------------------------------- httpx

_handle_original = None
_handle_async_original = None


def _httpx_barrar(request):
    """Volta se o destino for api.anthropic.com; senao registra e levanta httpx.ConnectError."""
    import httpx
    url = str(request.url)
    metodo = (request.method or "").upper()
    try:
        host = _alvo(url)[1]
    except Exception:
        host = ""
    if host == "api.anthropic.com" and decidir(metodo, url) == "passa":
        return
    try:
        corpo = request.content
    except Exception:
        corpo = None  # corpo em streaming ainda nao lido
    registro.registrar_envio(canal_de(url), metodo, url, None, corpo)
    try:
        partes = urlsplit(url)
        alvo = "%s://%s%s" % (partes.scheme, partes.netloc.rpartition("@")[2], partes.path)  # sem query
    except Exception:
        alvo = metodo
    raise httpx.ConnectError(_mensagem(alvo), request=request)


def _handle_guardado(self, request):
    _httpx_barrar(request)
    return _handle_original(self, request)


async def _handle_async_guardado(self, request):
    _httpx_barrar(request)
    return await _handle_async_original(self, request)


def _instalar_httpx():
    global _handle_original, _handle_async_original
    try:
        import httpx
    except ImportError:
        return False  # sem httpx no ambiente, nada a trocar
    _handle_original = httpx.HTTPTransport.handle_request
    _handle_async_original = httpx.AsyncHTTPTransport.handle_async_request
    _trocar(httpx.HTTPTransport, "handle_request", _handle_guardado)
    _trocar(httpx.AsyncHTTPTransport, "handle_async_request", _handle_async_guardado)


# ---------------------------------------------------------------- playwright

def _playwright_recusado(nome):
    def recusar(*a, **k):
        registro.registrar_envio("playwright", None, nome, None, None)
        raise EnvioBloqueadoHomolog(_mensagem("Playwright (%s)" % nome))
    recusar.__name__ = recusar.__qualname__ = nome
    return recusar


def _instalar_playwright():
    # troca o atributo do modulo: quem faz `from playwright.sync_api import sync_playwright` depois leva o falso
    try:
        import playwright.async_api as async_api
        import playwright.sync_api as sync_api
    except ImportError:
        return False
    _trocar(sync_api, "sync_playwright", _playwright_recusado("sync_playwright"))
    _trocar(async_api, "async_playwright", _playwright_recusado("async_playwright"))


# ---------------------------------------------------------------- socket

NOMES_PERMITIDOS = {"api.vuupt.com", "maps.googleapis.com", "storage.googleapis.com", "oauth2.googleapis.com",
                    "api.anthropic.com", "viacep.com.br", "brasilapi.com.br", "localhost"}

_IPS_RESOLVIDOS = set()  # ipaddress dos nomes permitidos ja resolvidos neste processo
_trava_ips = threading.Lock()
_AF_UNIX = getattr(socket, "AF_UNIX", None)
_no_socketpair = threading.local()  # socketpair do Windows conecta no proprio listener de loopback

_getaddrinfo_original = None
_connect_original = None
_connect_ex_original = None
_sendto_original = None
_sendmsg_original = None
_socketpair_original = None


def _ip(host):
    """ipaddress do host (str/bytes) ou None se nao for IP literal."""
    try:
        if isinstance(host, (bytes, bytearray)):
            host = bytes(host).decode("ascii")
        if not isinstance(host, str):
            return None
        return ipaddress.ip_address(host)
    except (ValueError, UnicodeDecodeError):
        return None


def _getaddrinfo_guardado(host, port, *a, **k):
    # IP literal e None (bind) passam: quem decide e o connect
    if host is None or _ip(host) is not None:
        return _getaddrinfo_original(host, port, *a, **k)
    nome = bytes(host).decode("ascii", "replace") if isinstance(host, (bytes, bytearray)) else str(host)
    if nome.lower().rstrip(".") not in NOMES_PERMITIDOS:
        registro.registrar_envio("socket", "getaddrinfo", "%s:%s" % (nome, port), None, None)
        raise socket.gaierror(socket.EAI_NONAME, _mensagem(nome))
    resultado = _getaddrinfo_original(host, port, *a, **k)
    with _trava_ips:
        for item in resultado:
            try:
                ip = _ip(item[4][0])
            except Exception:
                ip = None
            if ip is not None:
                _IPS_RESOLVIDOS.add(ip)
    return resultado


def _conexao_permitida(sock, endereco):
    familia = sock.family
    if _AF_UNIX is not None and familia == _AF_UNIX:
        return True
    if familia not in (socket.AF_INET, socket.AF_INET6):
        return False
    try:
        ip, porta = _ip(endereco[0]), endereco[1]
    except Exception:
        return False
    if ip is None:
        return False  # nome direto no connect: nao passou pela resolucao guardada
    if ip.is_loopback:
        return porta == PORTA_FRESHHUB or getattr(_no_socketpair, "ativo", False)
    with _trava_ips:
        return ip in _IPS_RESOLVIDOS


def _barrar_conexao(sock, endereco, metodo):
    if _conexao_permitida(sock, endereco):
        return
    try:
        host, porta = endereco[0], endereco[1]
        destino = ("[%s]:%s" if ":" in str(host) else "%s:%s") % (host, porta)
    except Exception:
        destino = repr(endereco)
    registro.registrar_envio("socket", metodo, destino, None, None)
    raise ConnectionRefusedError(errno.ECONNREFUSED, _mensagem(destino))


def _connect_guardado(self, endereco):
    _barrar_conexao(self, endereco, "connect")
    return _connect_original(self, endereco)


def _connect_ex_guardado(self, endereco):
    _barrar_conexao(self, endereco, "connect")
    return _connect_ex_original(self, endereco)


def _sendto_guardado(self, dados, *resto):
    # UDP nao passa pelo connect: sendto(dados, endereco) ou sendto(dados, flags, endereco)
    _barrar_conexao(self, resto[-1] if resto else None, "sendto")
    return _sendto_original(self, dados, *resto)


def _sendmsg_guardado(self, *args):
    if len(args) >= 4:  # sendmsg(buffers, ancdata, flags, endereco)
        _barrar_conexao(self, args[3], "sendto")
    return _sendmsg_original(self, *args)


def _socketpair_guardado(*a, **k):
    _no_socketpair.ativo = True
    try:
        return _socketpair_original(*a, **k)
    finally:
        _no_socketpair.ativo = False


def _instalar_socket():
    global _getaddrinfo_original, _connect_original, _connect_ex_original
    global _sendto_original, _sendmsg_original, _socketpair_original
    _getaddrinfo_original = socket.getaddrinfo
    _connect_original = socket.socket.connect
    _connect_ex_original = socket.socket.connect_ex
    _sendto_original = socket.socket.sendto
    _socketpair_original = socket.socketpair
    _trocar(socket, "getaddrinfo", _getaddrinfo_guardado)
    _trocar(socket.socket, "connect", _connect_guardado)
    _trocar(socket.socket, "connect_ex", _connect_ex_guardado)
    _trocar(socket.socket, "sendto", _sendto_guardado)
    if hasattr(socket.socket, "sendmsg"):
        _sendmsg_original = socket.socket.sendmsg
        _trocar(socket.socket, "sendmsg", _sendmsg_guardado)
    _trocar(socket, "socketpair", _socketpair_guardado)


# ---------------------------------------------------------------- instalar

def _trocar(objeto, atributo, novo):
    _ORIGINAIS.append((objeto, atributo, getattr(objeto, atributo)))
    setattr(objeto, atributo, novo)


_INSTALADORES = [
    ("smtp", _instalar_smtp),
    ("imap", _instalar_imap),
    ("requests", _instalar_requests),
    ("httpx", _instalar_httpx),
    ("playwright", _instalar_playwright),
    ("socket", _instalar_socket),
]


def instalar():
    """Idempotente. Se uma camada falhar, desfaz tudo e levanta (processo nao sobe sem guarda)."""
    global INSTALADA
    with _trava_instalar:
        if INSTALADA:
            return
        try:
            for nome, instalador in _INSTALADORES:
                if instalador() is not False:  # False = pacote opcional ausente
                    CAMADAS.append(nome)
        except Exception:
            desinstalar()
            raise
        INSTALADA = True


def desinstalar():
    """Restaura os originais. So para testes."""
    global INSTALADA, _send_original, _ultimo_vuupt, _handle_original, _handle_async_original
    global _getaddrinfo_original, _connect_original, _connect_ex_original
    global _sendto_original, _sendmsg_original, _socketpair_original
    with _trava_instalar:
        while _ORIGINAIS:
            objeto, atributo, valor = _ORIGINAIS.pop()
            setattr(objeto, atributo, valor)
        CAMADAS.clear()
        _send_original = None
        _ultimo_vuupt = None
        _handle_original = _handle_async_original = None
        _getaddrinfo_original = _connect_original = _connect_ex_original = None
        _sendto_original = _sendmsg_original = _socketpair_original = None
        with _trava_ips:
            _IPS_RESOLVIDOS.clear()
        INSTALADA = False
