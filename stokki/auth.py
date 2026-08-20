# -*- coding: utf-8 -*-
"""
stokki/auth.py

Gerenciamento de autenticação com o Stokki via cookies de sessão.

Fluxo:
  1. Playwright faz login (uma vez) e salva os cookies em sessao_stokki.json.
  2. Todas as chamadas HTTP seguintes usam uma requests.Session carregada
     com esses cookies — sem abrir browser de novo.
  3. Se qualquer resposta vier com status 401/403 ou redirecionar para /login,
     a sessão é renovada automaticamente (novo login via Playwright).

Uso:
    from stokki.auth import StokkiSession

    sess = StokkiSession(config)
    resp = sess.get("https://freshlog.stokki.com.br/pt-br/administrator/inventory/outbound/table",
                    params={"draw": 1, "start": 0, "length": 50})
"""
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

BASE_URL = "https://freshlog.stokki.com.br"
URL_LOGIN = f"{BASE_URL}/pt-br/administrator/login"

# Arquivo onde os cookies são persistidos entre execuções.
# Fica ao lado deste arquivo, dentro de stokki/.
COOKIES_PATH = Path(__file__).parent / "sessao_stokki.json"

# Quantas vezes tentar renovar a sessão antes de desistir.
MAX_TENTATIVAS_RENOVACAO = 2


class SessaoExpiradaError(Exception):
    """Lançada quando não é possível renovar a sessão após MAX_TENTATIVAS_RENOVACAO."""


class StokkiSession:
    """
    Wrapper sobre requests.Session que mantém a autenticação com o Stokki.
    Transparente para o chamador: use .get() / .post() normalmente.
    """

    def __init__(self, config: dict):
        """
        config: dicionário lido do config.yaml. Espera as chaves:
            stokki.usuario  — e-mail de login
            stokki.senha    — senha
        """
        self._usuario = config.get("stokki", {}).get("usuario", "")
        self._senha = config.get("stokki", {}).get("senha", "")
        if not self._usuario or not self._senha:
            raise ValueError(
                "Credenciais do Stokki não encontradas em config.yaml "
                "(seção 'stokki: usuario / senha')."
            )
        self._csrf_token = ""
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })
        self._carregar_ou_renovar_sessao()

    # ── API pública ────────────────────────────────────────────────────────────

    def get(self, url: str, **kwargs) -> requests.Response:
        return self._executar("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self._executar("POST", url, **kwargs)

    # ── Internos ───────────────────────────────────────────────────────────────

    def _executar(self, metodo: str, url: str, **kwargs) -> requests.Response:
        """Executa a requisição, renovando a sessão automaticamente se necessário."""
        for tentativa in range(1, MAX_TENTATIVAS_RENOVACAO + 1):
            resp = self._session.request(metodo, url, timeout=30, **kwargs)

            if self._sessao_invalida(resp):
                logger.warning(
                    f"Sessão expirada detectada (status={resp.status_code}, "
                    f"url={resp.url}). Renovando... (tentativa {tentativa}/{MAX_TENTATIVAS_RENOVACAO})"
                )
                self._fazer_login_playwright()
                continue  # tenta de novo com a sessão nova

            return resp

        raise SessaoExpiradaError(
            f"Não foi possível renovar a sessão após {MAX_TENTATIVAS_RENOVACAO} tentativas."
        )

    def _sessao_invalida(self, resp: requests.Response) -> bool:
        """Retorna True se a resposta indica sessão expirada/não autenticada."""
        if resp.status_code in (401, 403):
            return True
        # O Stokki redireciona para /login quando a sessão expira -- checa
        # o PATH da URL terminando em "/login", não uma substring solta
        # em qualquer lugar da URL (isso pegava falsos positivos em
        # páginas legítimas cujo path/query só continha "login" em algum
        # lugar, ex.: um relatório de "último login", disparando um
        # re-login via Playwright caro à toa).
        if resp.status_code in (200, 302):
            caminho = urlparse(resp.url).path.rstrip("/")
            if caminho.endswith("/login"):
                return True
        return False

    def _carregar_ou_renovar_sessao(self):
        """Tenta carregar cookies salvos; faz login se não existirem ou estiverem expirados."""
        if COOKIES_PATH.exists():
            try:
                self._carregar_cookies()
                # Verifica rapidamente se a sessão ainda é válida
                resp = self._session.get(
                    f"{BASE_URL}/pt-br/administrator", timeout=15, allow_redirects=False
                )
                if not self._sessao_invalida(resp):
                    logger.info("Sessão carregada dos cookies salvos.")
                    return
                logger.info("Cookies salvos expirados — fazendo novo login.")
            except Exception as e:
                logger.warning(f"Erro ao carregar cookies: {e} — fazendo novo login.")

        self._fazer_login_playwright()

    def _carregar_cookies(self):
        """Carrega cookies e CSRF token do arquivo JSON para a requests.Session."""
        with open(COOKIES_PATH, encoding="utf-8") as f:
            dados = json.load(f)

        # Suporta formato novo {cookies, csrf_token} e antigo [lista]
        if isinstance(dados, list):
            cookies = dados
        else:
            cookies = dados.get("cookies", [])
            csrf = dados.get("csrf_token", "")
            if csrf:
                self._csrf_token = csrf
                self._session.headers["X-CSRF-Token"] = csrf

        for cookie in cookies:
            self._session.cookies.set(
                cookie["name"], cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )

    def _salvar_cookies(self, playwright_cookies: list):
        """Persiste os cookies e CSRF token para uso em chamadas requests futuras."""
        dados = {"cookies": playwright_cookies, "csrf_token": self._csrf_token}
        COOKIES_PATH.write_text(
            json.dumps(dados, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        try:
            # Restringe o arquivo (cookies + CSRF token = bearer da sessão
            # automatizada) a leitura/escrita só pelo dono -- defesa extra
            # num host compartilhado. Sem efeito real no Windows (ACLs, não
            # bits POSIX), mas inofensivo lá e efetivo se isso um dia rodar
            # em Linux/Mac.
            os.chmod(COOKIES_PATH, 0o600)
        except OSError:
            pass
        logger.info(f"Cookies salvos em {COOKIES_PATH} ({len(playwright_cookies)} cookies).")

    def _fazer_login_playwright(self):
        """
        Abre o Playwright em modo headless, faz login no Stokki e salva
        os cookies resultantes. Também captura o CSRF token da página
        e o adiciona como header padrão da session (exigido pelo Laravel).
        """
        logger.info("Fazendo login no Stokki via Playwright...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                # O Chromium headless por padrao expoe "HeadlessChrome" no
                # User-Agent e navigator.webdriver=true -- a Stokki passou a
                # bloquear isso com 403 ("Acesso automatizado nao e
                # permitido"), fazendo o wait_for_selector do e-mail estourar
                # timeout porque a pagina carregada era a de erro, nao o
                # formulario de login. UA de Chrome real + mascarar
                # navigator.webdriver contorna a deteccao.
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36"
                )
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                page = context.new_page()

                try:
                    page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=30_000)
                    page.wait_for_selector("[name='email']", timeout=15_000)
                    page.fill("[name='email']", self._usuario)
                    page.fill("[name='password']", self._senha)
                    page.click("button[type='submit']")
                    page.wait_for_url(lambda url: "login" not in url, timeout=30_000)
                    logger.info(f"Login OK. URL: {page.url}")

                    # Visita a area /provider/ dentro do Playwright para que
                    # os cookies de sessao dessa area sejam estabelecidos antes
                    # de salvar -- sem isso, chamadas requests para /provider/
                    # redirecionam para /login mesmo com sessao valida no /administrator/.
                    try:
                        page.goto(
                            f"{BASE_URL}/pt-br/provider",
                            wait_until="domcontentloaded",
                            timeout=15_000,
                        )
                        logger.info("Area /provider/ visitada para estabelecer cookies.")
                    except Exception as e:
                        logger.debug(f"Aviso ao visitar /provider/: {e}")

                    # Captura o CSRF token da meta tag
                    csrf_token = page.evaluate(
                        "() => document.querySelector('meta[name=csrf-token]')?.content || ''"
                    )
                    if csrf_token:
                        self._csrf_token = csrf_token
                        logger.info("CSRF token capturado.")
                    else:
                        logger.warning("CSRF token não encontrado na página.")

                except PlaywrightTimeout as e:
                    raise SessaoExpiradaError(f"Timeout durante o login no Stokki: {e}") from e

                cookies = context.cookies()
            finally:
                # Garante que o Chromium headless sempre e fechado, mesmo
                # se um seletor mudar e o login falhar com um erro que nao
                # seja PlaywrightTimeout -- sem isso, o processo vazava e
                # se acumulava a cada execucao agendada com falha.
                browser.close()

        self._salvar_cookies(cookies)

        # Recarrega a requests.Session com cookies + headers obrigatórios
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })
        if self._csrf_token:
            self._session.headers["X-CSRF-Token"] = self._csrf_token
        self._carregar_cookies()
        logger.info("Sessão renovada com sucesso.")
