# -*- coding: utf-8 -*-
"""
investigar_apis_internas_stokki.py

Ferramenta de investigação: faz login no Stokki, abre o navegador
VISÍVEL, e fica escutando TODO o tráfego de rede relevante (JSON — as
chamadas internas que o site faz — e HTML de páginas de conteúdo real,
que pode ter a tabela/dados renderizados no servidor) enquanto VOCÊ
navega manualmente pelas telas que quer investigar.

Isso serve para descobrir se existe uma API interna (não documentada,
mas usada pelo próprio site) que a gente pode chamar diretamente — em
vez de depender de baixar relatórios em Excel — para viabilizar um
fluxo orientado a eventos.

COMO USAR:
  1. set LOGIN_USER=seu_email@freshlogbr.com
     set LOGIN_PASS=sua_senha_aqui
     (ou configure 'login: usuario/senha' em config.yaml)
  2. python investigar_apis_internas_stokki.py
  3. O navegador vai abrir e fazer login sozinho.
  4. NAVEGUE NORMALMENTE pelas telas que te interessam:
       - Relatórios (Todos Extratos para Transporte)
       - Pedidos aguardando transportador / em espera
       - Estação de Impressão
       - Estação de Expedição
       - Abrir um pedido específico
     Quanto mais você navegar e interagir (filtrar, clicar, abrir
     pedidos, paginar), mais chamadas internas são capturadas — e são
     processadas EM TEMPO REAL (não só no final), então nada se perde.
  5. Quando terminar, volte ao terminal e aperte ENTER.
  6. O script salva um relatório em debug/saida_investigacao_stokki/
     com todas as chamadas capturadas, organizadas e sem duplicar.

O QUE ELE CAPTURA:
  - Respostas JSON (as chamadas de dados internas) — sempre.
  - Respostas HTML de páginas de conteúdo real (não fragmentos
    pequenos) — porque algumas telas do Stokki renderizam a tabela
    direto no HTML do servidor, sem chamada JSON separada.
  Ignora CSS, imagens, fontes, JS estático e fragmentos HTML pequenos
  (prováveis redirecionamentos).

  Para os endpoints já confirmados como relevantes nesta investigação
  (listagem e detalhe de pedido — ver PADROES_SEM_TRUNCAR), o corpo é
  salvo POR COMPLETO, sem truncar, mesmo que seja grande — porque o
  dado que procuramos (telefone/endereço) pode estar bem adiante no
  HTML da página.

LIMITAÇÃO IMPORTANTE:
  Isso não é uma API "aberta" nem documentada — são chamadas internas
  que o próprio site usa. Usar isso para automação tem os mesmos
  riscos de qualquer engenharia reversa: pode quebrar sem aviso se o
  Stokki mudar o front-end, e vale confirmar que não fere os termos de
  uso da plataforma antes de depender disso em produção.
"""
import json
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_DIR = Path(__file__).parent
CONFIG_PATH = _DIR.parent / "config.yaml"
SAIDA_DIR = _DIR / "saida_investigacao_stokki"

URL_LOGIN = "https://freshlog.stokki.com.br/pt-br/administrator/login"

# Tamanho máximo (em caracteres) do corpo de cada resposta salva no
# relatório — respostas maiores são truncadas, para o arquivo final não
# ficar gigante. O tamanho real (não truncado) sempre fica registrado.
LIMITE_CORPO_RESPOSTA = 20000

# Extensões/tipos que NÃO nos interessam (ativos estáticos, não dados)
CONTENT_TYPES_IGNORADOS = ("text/css", "image/", "font/")

# Tamanho mínimo (em caracteres) para uma resposta HTML valer a pena
# guardar — páginas de conteúdo real costumam ser grandes; descarta
# HTML pequeno (provavelmente fragmentos irrelevantes ou redirecionamentos).
TAMANHO_MINIMO_HTML = 3000

# URLs que contêm qualquer um destes trechos são salvas SEM TRUNCAR,
# mesmo que ultrapassem o limite acima — usado para os endpoints que já
# confirmamos serem relevantes (listagem e detalhe de pedido), onde o
# dado que procuramos (telefone/endereço) pode estar bem mais adiante
# no HTML do que o limite padrão alcançaria.
PADROES_SEM_TRUNCAR = ("outbound/show/", "outbound/table")


def _carregar_credenciais():
    usuario = os.environ.get("LOGIN_USER", "")
    senha = os.environ.get("LOGIN_PASS", "") or os.environ.get("STOKKI_PASSWORD", "")
    if usuario and senha:
        return usuario, senha

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        login_cfg = config.get("login", {})
        usuario = usuario or login_cfg.get("usuario", "")
        senha = senha or login_cfg.get("senha", "")

    return usuario, senha


def _fazer_login(page, usuario: str, senha: str):
    logger.info("Fazendo login no Stokki...")
    page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_selector("[name='email']", timeout=15_000)
    page.fill("[name='email']", usuario)
    page.fill("[name='password']", senha)
    page.click("button[type='submit']")
    page.wait_for_url(lambda url: "login" not in url, timeout=30_000)
    logger.info(f"Login OK. URL atual: {page.url}")


def main():
    usuario, senha = _carregar_credenciais()
    if not usuario or not senha:
        raise SystemExit(
            "Usuário/senha não encontrados. Configure LOGIN_USER e LOGIN_PASS "
            "(ou STOKKI_PASSWORD) como variável de ambiente, ou preencha "
            "'login: usuario/senha' em config.yaml."
        )

    SAIDA_DIR.mkdir(parents=True, exist_ok=True)

    chamadas_capturadas = []
    urls_vistas = set()  # (metodo, url) — evita salvar a mesma chamada repetida várias vezes

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # VISÍVEL de propósito — você vai navegar
        context = browser.new_context()
        page = context.new_page()

        def registrar_resposta(response):
            try:
                content_type = response.headers.get("content-type", "")
                if any(ct in content_type for ct in CONTENT_TYPES_IGNORADOS):
                    return

                eh_json = "json" in content_type
                eh_html = "text/html" in content_type

                if not eh_json and not eh_html:
                    # Só nos interessam respostas JSON (dados de verdade)
                    # ou HTML (pode ser a tabela renderizada no servidor)
                    # — descarta JS, imagens, fontes, etc.
                    return

                request = response.request
                metodo = request.method
                url = response.url

                if "login" in url.lower():
                    # Nunca captura o request/response do próprio login —
                    # o corpo da requisição de login carrega a senha em
                    # texto puro (page.fill("[name='password']", senha)),
                    # e não é dado relevante para a investigação de APIs.
                    return

                # Lê o corpo ANTES de decidir se guarda — para HTML,
                # precisamos saber o tamanho antes de aplicar o filtro de
                # tamanho mínimo (descarta fragmentos HTML pequenos e
                # irrelevantes, mantém páginas de conteúdo real).
                corpo_resposta = None
                tamanho_real = None
                texto = None
                try:
                    texto = response.text()
                    tamanho_real = len(texto)
                except Exception as e:
                    corpo_resposta = f"[não foi possível ler o corpo: {e}]"

                if eh_html and tamanho_real is not None and tamanho_real < TAMANHO_MINIMO_HTML:
                    return  # HTML pequeno demais — provavelmente irrelevante

                chave = (metodo, url.split("?")[0])  # ignora query string na deduplicação
                if chave in urls_vistas:
                    return
                urls_vistas.add(chave)

                corpo_requisicao = None
                try:
                    corpo_requisicao = request.post_data
                except Exception:
                    pass

                if texto is not None:
                    sem_truncar = any(padrao in url for padrao in PADROES_SEM_TRUNCAR)
                    if sem_truncar:
                        corpo_resposta = texto  # salva completo, sem truncar
                    else:
                        corpo_resposta = texto[:LIMITE_CORPO_RESPOSTA]
                        if tamanho_real > LIMITE_CORPO_RESPOSTA:
                            corpo_resposta += f"\n... [TRUNCADO — tamanho real: {tamanho_real} caracteres]"

                chamadas_capturadas.append({
                    "tipo": "html" if eh_html else "json",
                    "metodo": metodo,
                    "url": url,
                    "status": response.status,
                    "content_type": content_type,
                    "corpo_requisicao": corpo_requisicao,
                    "corpo_resposta": corpo_resposta,
                    "tamanho_real_resposta": tamanho_real,
                    "capturado_em": datetime.now().strftime("%H:%M:%S"),
                })
                logger.info(f"  [{len(chamadas_capturadas)}] ({'HTML' if eh_html else 'JSON'}) "
                            f"{metodo} {url} -> {response.status} ({tamanho_real} chars)")
            except Exception as e:
                logger.debug(f"Falha ao registrar uma resposta (ignorada): {e}")

        page.on("response", registrar_resposta)

        try:
            _fazer_login(page, usuario, senha)
        except PlaywrightTimeout:
            logger.error("Timeout no login — confira usuário/senha e conexão.")
            browser.close()
            return

        print("\n" + "=" * 70)
        print("NAVEGADOR ABERTO — navegue manualmente pelas telas que quer investigar.")
        print("Sugestões: Relatórios, Pedidos Aguardando Transportador/Em Espera,")
        print("Estação de Impressão, Estação de Expedição, abrir um pedido específico.")
        print("Quanto mais você clicar e filtrar, mais chamadas internas são capturadas.")
        print("=" * 70)

        # IMPORTANTE: não usamos input() diretamente aqui, porque ele
        # bloqueia o Python inteiro e impede o Playwright de processar os
        # eventos de rede em tempo real — isso fazia os corpos das
        # respostas mais antigas serem descartados pelo navegador antes
        # de conseguirmos lê-los. Em vez disso, a espera pelo ENTER roda
        # numa thread separada, enquanto o processo principal continua
        # "alimentando" o Playwright (wait_for_timeout) a cada 300ms —
        # assim cada resposta é lida quase imediatamente, assim que chega.
        sinal_parar = threading.Event()

        def esperar_enter():
            input("\nQuando terminar de navegar, aperte ENTER aqui para salvar o relatório...\n")
            sinal_parar.set()

        thread_enter = threading.Thread(target=esperar_enter, daemon=True)
        thread_enter.start()

        while not sinal_parar.is_set():
            page.wait_for_timeout(300)

        # Pausa curta extra: garante que a última rajada de respostas
        # (disparadas bem perto do ENTER) termine de ser processada.
        logger.info("Aguardando processamento das últimas respostas antes de fechar...")
        page.wait_for_timeout(1500)

        browser.close()

    if not chamadas_capturadas:
        logger.warning("Nenhuma chamada relevante foi capturada. Talvez o site não use "
                        "chamadas internas via XHR/Fetch para as telas visitadas, "
                        "ou a navegação foi rápida demais. Tente de novo navegando mais.")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    caminho_json = SAIDA_DIR / f"chamadas_capturadas_{timestamp}.json"
    with open(caminho_json, "w", encoding="utf-8") as f:
        json.dump(chamadas_capturadas, f, indent=2, ensure_ascii=False)

    # Relatório resumido, fácil de ler rapidamente
    caminho_resumo = SAIDA_DIR / f"resumo_{timestamp}.txt"
    with open(caminho_resumo, "w", encoding="utf-8") as f:
        f.write(f"Investigação de APIs internas do Stokki — {timestamp}\n")
        f.write(f"Total de chamadas únicas capturadas: {len(chamadas_capturadas)}\n")
        f.write("=" * 70 + "\n\n")
        for c in chamadas_capturadas:
            f.write(f"[{c['tipo'].upper()}] {c['metodo']} {c['url']}\n")
            f.write(f"  Status: {c['status']} | Content-Type: {c['content_type']}\n")
            if c["corpo_requisicao"]:
                f.write(f"  Corpo enviado: {c['corpo_requisicao'][:300]}\n")
            f.write(f"  Tamanho da resposta: {c['tamanho_real_resposta']} caracteres\n")
            f.write("\n")

    logger.info(f"\n✅ {len(chamadas_capturadas)} chamada(s) única(s) capturada(s).")
    logger.info(f"Relatório completo (com corpo das respostas): {caminho_json}")
    logger.info(f"Resumo rápido (só URLs e status): {caminho_resumo}")
    logger.info("\nPróximo passo: me manda esses dois arquivos (ou pelo menos o resumo) "
                "para a gente analisar juntos quais chamadas valem a pena investigar "
                "como possível 'API interna' para o novo fluxo.")


if __name__ == "__main__":
    main()
