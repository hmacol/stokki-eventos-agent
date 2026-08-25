# -*- coding: utf-8 -*-
"""
freshhub/auth.py

Gerenciamento de autenticação com o Fresh Hub (freshhub.com.br) --
sistema interno da Freshlog (Pedidos Parados, Demandas etc.), separado
do Stokki. É um SPA que fala direto com um projeto Supabase
(Postgres+PostgREST) -- não existe backend próprio nosso pra mapear.

Achados (24/08, ver TRATATIVAS_PEDIDOS_PARADOS.md na raiz do projeto):
  - A `apikey` pública (anon) do Supabase, sozinha, NÃO basta -- testado
    direto via curl: devolve 200 OK com lista vazia (RLS bloqueando
    silenciosamente, sem erro visível). Precisa de sessão de usuário
    autenticado de verdade.
  - A sessão do Supabase fica no localStorage do navegador (chave
    `sb-<project-ref>-auth-token`), não em cookie -- padrão comum do
    supabase-js.
  - Login é CPF/celular ("identifier") + PIN de 6 dígitos ("pin-in"),
    confirmado inspecionando a página real de login.

Fluxo:
  1. Playwright faz login (CPF+PIN) uma vez, lê o token de sessão do
     localStorage e salva em sessao_freshhub.json.
  2. Chamadas HTTP seguintes usam requests.Session com esse
     access_token como Authorization Bearer + a apikey pública.
  3. Se o access_token expirar, tenta renovar via refresh_token (uma
     chamada HTTP simples, sem abrir browser); só refaz o login
     completo via Playwright se isso falhar.

Uso:
    from freshhub.auth import FreshHubSession, SUPABASE_URL

    sess = FreshHubSession(config)
    resp = sess.get(f"{SUPABASE_URL}/rest/v1/stalled_orders",
                     params={"select": "*", "order": "created_at.desc", "limit": 200})
"""
import json
import logging
import os
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

# Projeto Supabase por trás do Fresh Hub -- descoberto via HAR (24/08).
SUPABASE_URL = "https://qhujqhzkwvbfsauxgepf.supabase.co"
# Chave "anon" pública, vem embutida no bundle JS público do site --
# não é segredo (qualquer visitante do site a vê), mas sozinha não dá
# acesso a nada (RLS exige sessão autenticada além dela).
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFo"
    "dWpxaHprd3ZiZnNhdXhnZXBmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzg2NDI3MjUsImV4"
    "cCI6MjA5NDIxODcyNX0.y1YBNM2TWEZqF-SIgdYvE3eeSoyhP5g_n0visgOZB1U"
)
URL_LOGIN = "https://freshhub.com.br/login"
LOCAL_STORAGE_KEY = "sb-qhujqhzkwvbfsauxgepf-auth-token"

# Arquivo onde a sessão é persistida entre execuções -- ao lado deste
# arquivo, dentro de freshhub/ (mesmo padrão de stokki/sessao_stokki.json).
SESSAO_PATH = Path(__file__).parent / "sessao_freshhub.json"

MAX_TENTATIVAS_RENOVACAO = 2


class SessaoExpiradaError(Exception):
    """Lançada quando não é possível obter/renovar a sessão do Fresh Hub."""


class FreshHubSession:
    """
    Wrapper sobre requests.Session que mantém a autenticação com o
    Fresh Hub. Transparente para o chamador: use .get()/.post()/.patch()
    normalmente, apontando pra URLs do Supabase REST.
    """

    def __init__(self, config: dict):
        """
        config: dicionário lido do config.yaml. Espera a seção
            freshhub:
              cpf: ...
              pin: ...
        """
        fh_config = config.get("freshhub", {})
        self._cpf = fh_config.get("cpf", "")
        self._pin = fh_config.get("pin", "")
        if not self._cpf or not self._pin:
            raise ValueError(
                "Credenciais do Fresh Hub não encontradas em config.yaml "
                "(seção 'freshhub: cpf / pin')."
            )
        self._session = requests.Session()
        self._session.headers.update({
            "apikey": SUPABASE_ANON_KEY,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self.user_id = None  # uuid de profiles do usuário logado (preenchido abaixo)
        self._carregar_ou_renovar_sessao()

    # ── API pública ────────────────────────────────────────────────────────────

    def get(self, url: str, **kwargs) -> requests.Response:
        return self._executar("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self._executar("POST", url, **kwargs)

    def patch(self, url: str, **kwargs) -> requests.Response:
        return self._executar("PATCH", url, **kwargs)

    # ── Internos ───────────────────────────────────────────────────────────────

    def _executar(self, metodo: str, url: str, **kwargs) -> requests.Response:
        """Executa a requisição, renovando a sessão automaticamente em 401/403.

        Atenção: um token inválido/ausente NÃO garante 401/403 aqui --
        o RLS do Supabase pode devolver 200 com lista vazia em vez de
        erro (comportamento confirmado por teste direto, 24/08). Quem
        chama isso e recebe uma lista vazia inesperada deve considerar
        essa possibilidade (ver aviso em pedidos_parados.py)."""
        for tentativa in range(1, MAX_TENTATIVAS_RENOVACAO + 1):
            resp = self._session.request(metodo, url, timeout=30, **kwargs)

            if resp.status_code in (401, 403):
                logger.warning(
                    f"Sessão Fresh Hub inválida (status={resp.status_code}). "
                    f"Renovando... (tentativa {tentativa}/{MAX_TENTATIVAS_RENOVACAO})"
                )
                self._renovar_sessao()
                continue

            return resp

        raise SessaoExpiradaError(
            f"Não foi possível renovar a sessão do Fresh Hub após {MAX_TENTATIVAS_RENOVACAO} tentativas."
        )

    def _carregar_ou_renovar_sessao(self):
        """Tenta carregar sessão salva; renova via refresh_token se expirada;
        só faz login completo via Playwright se as duas anteriores falharem."""
        if SESSAO_PATH.exists():
            try:
                dados = json.loads(SESSAO_PATH.read_text(encoding="utf-8"))
                if dados.get("expires_at", 0) > time.time() + 60:
                    self._aplicar_token(dados["access_token"], dados.get("user_id"))
                    logger.info("Sessão Fresh Hub carregada do cache (ainda válida).")
                    return
                logger.info("Sessão Fresh Hub salva expirada -- tentando renovar via refresh_token.")
                if self._tentar_refresh(dados.get("refresh_token", "")):
                    return
            except Exception as e:
                logger.warning(f"Erro ao carregar sessão salva do Fresh Hub: {e} -- fazendo novo login.")

        self._fazer_login_playwright()

    def _renovar_sessao(self):
        """Chamado quando uma requisição já em andamento voltou 401/403."""
        if SESSAO_PATH.exists():
            try:
                dados = json.loads(SESSAO_PATH.read_text(encoding="utf-8"))
                if self._tentar_refresh(dados.get("refresh_token", "")):
                    return
            except Exception:
                pass
        self._fazer_login_playwright()

    def _tentar_refresh(self, refresh_token: str) -> bool:
        """Renova o access_token via refresh_token -- só uma chamada HTTP,
        sem precisar abrir o browser. Retorna False se falhar por qualquer
        motivo (refresh_token vazio, revogado, erro de rede etc.)."""
        if not refresh_token:
            return False
        try:
            resp = requests.post(
                f"{SUPABASE_URL}/auth/v1/token",
                params={"grant_type": "refresh_token"},
                headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
                json={"refresh_token": refresh_token},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"Refresh do token Fresh Hub falhou (status={resp.status_code}).")
                return False
            dados = resp.json()
            user_id = (dados.get("user") or {}).get("id")
            self._salvar_sessao(dados["access_token"], dados["refresh_token"], dados["expires_at"], user_id)
            self._aplicar_token(dados["access_token"], user_id)
            logger.info("Sessão Fresh Hub renovada via refresh_token (sem browser).")
            return True
        except Exception as e:
            logger.warning(f"Falha ao renovar sessão Fresh Hub via refresh_token: {e}")
            return False

    def _aplicar_token(self, access_token: str, user_id: str | None = None):
        self._session.headers["Authorization"] = f"Bearer {access_token}"
        if user_id:
            self.user_id = user_id

    def _salvar_sessao(self, access_token: str, refresh_token: str, expires_at, user_id: str | None = None):
        dados = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "user_id": user_id,
        }
        SESSAO_PATH.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            # Restringe leitura/escrita ao dono -- o access_token/refresh_token
            # é o bearer da sessão. Mesmo racional de stokki/auth.py.
            os.chmod(SESSAO_PATH, 0o600)
        except OSError:
            pass

    def _fazer_login_playwright(self):
        """Abre o Playwright em modo headless, preenche CPF+PIN no
        formulário real e lê o token de sessão resultante do
        localStorage (não tem cookie de sessão aqui, é tudo
        localStorage -- padrão supabase-js)."""
        logger.info("Fazendo login no Fresh Hub via Playwright...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36"
                )
                page = context.new_page()

                try:
                    page.goto(URL_LOGIN, wait_until="networkidle", timeout=30_000)
                    page.fill("#identifier", self._cpf)
                    page.fill("#pin-in", self._pin)
                    page.click("button[type='submit']")
                    page.wait_for_url(lambda url: "/login" not in url, timeout=20_000)
                    logger.info(f"Login Fresh Hub OK. URL: {page.url}")

                    raw = page.evaluate(
                        "(key) => window.localStorage.getItem(key)", LOCAL_STORAGE_KEY
                    )
                    if not raw:
                        raise SessaoExpiradaError(
                            "Login pareceu funcionar (saiu de /login) mas não "
                            f"encontrei a chave '{LOCAL_STORAGE_KEY}' no localStorage."
                        )
                    dados = json.loads(raw)
                except PlaywrightTimeout as e:
                    raise SessaoExpiradaError(f"Timeout durante o login no Fresh Hub: {e}") from e
            finally:
                browser.close()

        user_id = (dados.get("user") or {}).get("id")
        self._salvar_sessao(dados["access_token"], dados["refresh_token"], dados["expires_at"], user_id)
        self._aplicar_token(dados["access_token"], user_id)
        logger.info("Sessão Fresh Hub renovada com sucesso (login completo).")
