# -*- coding: utf-8 -*-
"""
stokki/estacao_impressao.py

Leitura dos pedidos em espera via Estacao de Impressao do Stokki.

O endpoint fica na area /provider/ que usa sessao separada da area
/administrator/. Por isso, as chamadas usam Playwright diretamente
em vez de requests.

Endpoints:
  GET  /provider/operation/order/printing/order  -- lista em espera
  POST /provider/operation/order/printing/search -- busca por NF/PO
"""
import logging
import re
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

BASE_URL     = "https://freshlog.stokki.com.br"
URL_PRINTING = f"{BASE_URL}/pt-br/provider/operation/order/printing"
URL_ORDER    = f"{BASE_URL}/pt-br/provider/operation/order/printing/order"
URL_SEARCH   = f"{BASE_URL}/pt-br/provider/operation/order/printing/search"
LIMITE_SEGURANCA_IMPRESSAO = 300  # trava de segurança: máx. de cliques em "Imprimir" por execução

_RAIZ_DADOS = Path(__file__).resolve().parent.parent / "dados"


def _credenciais_provider(config: dict) -> tuple[str, str]:
    """Retorna (usuario, senha) para login na area /provider/ do Stokki."""
    provider = config.get("stokki", {}).get("provider", {})
    usuario = provider.get("usuario") or config.get("stokki", {}).get("usuario", "")
    senha   = provider.get("senha")   or config.get("stokki", {}).get("senha", "")
    return usuario, senha


def listar_pedidos_em_espera(config: dict) -> list:
    """
    Retorna a lista de pedidos em espera na Estacao de Impressao.
    Usa Playwright diretamente para contornar a separacao de sessao
    entre /administrator/ e /provider/.

    config: dict lido do config.yaml (precisa de stokki.usuario/senha)

    Cada item: {id_stokki, codigo_ps, referencia, data, hora, cliente, codigo_cli}
    """
    usuario, senha = _credenciais_provider(config)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page    = context.new_page()

        # Navega para o provider antes do login para estabelecer sessao correta
        page.goto(URL_PRINTING, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector("[name='email']", timeout=15_000)
        page.fill("[name='email']", usuario)
        page.fill("[name='password']", senha)
        page.click("button[type='submit']")
        page.wait_for_url(lambda u: "login" not in u, timeout=30_000)

        # Captura a resposta JSON da estacao de impressao
        resultado_json = {}

        def capturar(response):
            if "printing/order" in response.url:
                try:
                    resultado_json["data"] = response.json()
                except Exception:
                    pass

        page.on("response", capturar)
        page.goto(URL_PRINTING, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(2000)
        browser.close()

    html_tabela = resultado_json.get("data", {}).get("div_billing", "")
    if not html_tabela:
        logger.info("Estacao de Impressao: nenhum pedido em espera.")
        return []

    pedidos = _parsear_tabela_impressao(html_tabela)
    logger.info(f"Estacao de Impressao: {len(pedidos)} pedido(s) em espera.")
    return pedidos


def buscar_pedido_por_nf(config: dict, numero_nf: str):
    """
    Busca um pedido especifico pelo numero da NF ou PO.
    Retorna o dict da resposta ou None se nao encontrado.
    """
    usuario, senha = _credenciais_provider(config)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page    = context.new_page()

        page.goto(URL_PRINTING, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector("[name='email']", timeout=15_000)
        page.fill("[name='email']", usuario)
        page.fill("[name='password']", senha)
        page.click("button[type='submit']")
        page.wait_for_url(lambda u: "login" not in u, timeout=30_000)

        csrf = page.evaluate(
            "() => document.querySelector('meta[name=csrf-token]')?.content || ''"
        )
        response = context.request.post(
            URL_SEARCH,
            form={"po": numero_nf},
            headers={
                "X-CSRF-Token": csrf,
                "Referer": URL_PRINTING,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        dados = response.json() if response.ok else {}
        browser.close()

    if not dados.get("success"):
        logger.warning(f"Pedido {numero_nf!r} nao encontrado.")
        return None
    return dados


def _parsear_tabela_impressao(html: str) -> list:
    """Parseia o HTML da tabela retornada pelo endpoint /printing/order."""
    soup    = BeautifulSoup(html, "html.parser")
    pedidos = []

    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue

        linhas_id  = [l.strip() for l in tds[0].get_text("\n",strip=True).split("\n") if l.strip()]
        codigo_ps  = linhas_id[0] if linhas_id else ""
        referencia = linhas_id[1] if len(linhas_id) > 1 else ""

        linhas_data = [l.strip() for l in tds[1].get_text("\n",strip=True).split("\n") if l.strip()]
        data = linhas_data[0] if linhas_data else ""
        hora = linhas_data[1] if len(linhas_data) > 1 else ""

        linhas_cli = [l.strip() for l in tds[2].get_text("\n",strip=True).split("\n") if l.strip()]
        cliente    = linhas_cli[0] if linhas_cli else ""
        codigo_cli = linhas_cli[1] if len(linhas_cli) > 1 else ""

        botao     = tds[3].find("button")
        id_stokki = None
        if botao:
            data_id = botao.get("data-id", "")
            if data_id and str(data_id).isdigit():
                id_stokki = int(data_id)
            else:
                m = re.search(r"/show/(\d+)", botao.get("data-href", ""))
                if m:
                    id_stokki = int(m.group(1))

        if id_stokki:
            pedidos.append({
                "id_stokki":  id_stokki,
                "codigo_ps":  codigo_ps,
                "referencia": referencia,
                "data":       data,
                "hora":       hora,
                "cliente":    cliente,
                "codigo_cli": codigo_cli,
            })

    return pedidos


def _navegar_com_retry(page, url, tentativas=3, timeout_ms=45000):
    """Navega com retentativas — tolera lentidão/instabilidade transitória
    de rede em automação longa (porta de stokki_common.navegar, do
    agente_relatorio)."""
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            page.goto(url, timeout=timeout_ms)
            return
        except Exception as e:
            ultimo_erro = e
            logger.warning(f"Falha ao navegar para {url} (tentativa {tentativa}/{tentativas}): {e}")
            if tentativa < tentativas:
                page.wait_for_timeout(1000)
    raise ultimo_erro


def _aguardar_pagina_acalmar(page, timeout=8000):
    """Espera best-effort a página 'acalmar' após navegação/ação — nunca
    levanta exceção (porta de stokki_common.aguardar_pagina)."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass


def _login_provider(page, usuario, senha):
    """
    Login direto na área /provider/ — mesmo padrão já usado e comprovado
    neste arquivo por listar_pedidos_em_espera/buscar_pedido_por_nf, sem
    passar por login administrador + impersonação do WMS (abordagem
    anterior, descartada a pedido do Hugo em favor do acesso direto).
    """
    _navegar_com_retry(page, URL_PRINTING)
    logger.info(f"Fazendo login direto na área /provider/ como {usuario}...")
    page.wait_for_selector("[name='email']", timeout=15_000)
    page.fill("[name='email']", usuario)
    page.fill("[name='password']", senha)
    page.click("button[type='submit']")
    page.wait_for_url(lambda u: "login" not in u, timeout=30_000)

    # Recarrega a Estacao de Impressao ja autenticado, garantindo que a
    # tabela de pendentes carregue do zero (mesmo padrao usado em
    # listar_pedidos_em_espera, em vez de confiar no redirect pos-login).
    _navegar_com_retry(page, URL_PRINTING)
    _aguardar_pagina_acalmar(page)
    logger.info(f"Login OK. URL atual: {page.url}")


def _fechar_popup_pular_impressao(page, max_cliques=5):
    """
    Depois de clicar em 'Imprimir', aguarda o popup aparecer e clica em
    'Pular Impressão' — é isso que efetivamente aciona a transição de
    status. Retorna True se pelo menos um popup foi fechado (ou já não
    havia nenhum aberto), False se ficou preso mesmo depois de Esc E de
    recarregar a página.

    Timeouts alargados (06/08, achado em produção): o clique em 'Pular
    Impressão' e o fechamento do modal em si podem levar mais que os
    3-5s originais (o modal tem uma barra de progresso — parece um
    processamento interno do Stokki, não uma simples animação) — com
    timeout curto, a função desistia achando que falhou, mas o popup
    continuava ABERTO de verdade, bloqueando o clique de TODOS os
    pedidos seguintes por até 30s cada (Playwright ficava tentando a
    mesma ação até estourar o timeout, "intercepts pointer events").

    RELOAD como último recurso (07/08, achado em produção): confirmado
    que existe modal GENUINAMENTE travado -- nem o fluxo normal nem Esc
    fecham (barra de progresso trava de vez, não é só demora). Sem
    reload, TODO pedido seguinte da fila herdava o mesmo modal preso e
    pagava os mesmos ~30-40s tentando (em vão) fechar + mais 30s
    tentando clicar -- lote inteiro efetivamente perdido. Reload limpa
    qualquer modal preso incondicionalmente; o próprio pedido que
    disparou o modal preso fica com resultado incerto (contado como
    falha), mas os pedidos SEGUINTES voltam a processar normalmente.
    """
    fechou_algum = False
    for _ in range(max_cliques):
        try:
            page.wait_for_selector("#modal_finish_hide.show", timeout=5000)
            page.click("button:has-text('Pular Impressão')", timeout=5000)
            page.wait_for_selector("#modal_finish_hide.show", state="hidden", timeout=15000)
            page.wait_for_timeout(300)
            fechou_algum = True
        except Exception:
            break

    # Último recurso se o modal ainda estiver aberto de verdade -- Esc
    # costuma fechar esse padrão de modal (role="dialog" aria-modal,
    # visual de Bootstrap). Evita deixar um popup preso bloqueando o
    # resto da fila.
    if page.query_selector("#modal_finish_hide.show"):
        logger.warning("  Popup não fechou pelo fluxo normal -- tentando Esc como recuperação.")
        try:
            page.keyboard.press("Escape")
            page.wait_for_selector("#modal_finish_hide.show", state="hidden", timeout=5000)
            fechou_algum = True
            logger.warning("  Popup fechou via Esc.")
        except Exception:
            logger.error("  Popup CONTINUA aberto mesmo depois de Esc -- recarregando a página.")
            try:
                page.reload(wait_until="networkidle", timeout=30000)
                fechou_algum = False  # resultado do pedido que causou o travamento fica incerto
                logger.warning("  Página recarregada -- modal preso foi limpo, seguindo pros próximos pedidos.")
            except Exception as e2:
                logger.error(f"  Falha ao recarregar a página: {e2} -- fila pode continuar travada.")

    return fechou_algum


def imprimir_pedidos_pendentes(config: dict, dry_run: bool = False,
                               visivel: bool = False) -> dict:
    """
    Processa TODOS os pedidos pendentes na Estação de Impressão, clicando
    de verdade em 'Imprimir' (botão button.btn_finish) e fechando o popup
    'Pular Impressão' que segue — é isso que move o pedido de 'Em espera'
    para 'Aguardando Transportador'.

    Acesso direto à área /provider/ com as credenciais de provider
    (mesmo padrão já usado neste arquivo por listar_pedidos_em_espera) —
    sem passar por login administrador + impersonação do WMS (abordagem
    inicial, portada do agente_relatorio, descartada a pedido do Hugo em
    favor do acesso direto que já era usado e comprovado neste projeto).

    Substitui a tentativa anterior de processar_pedidos_faturados (via
    API: POST /printing/search + GET do endpoint do DANFE), que não se
    mostrou confiável. Este fluxo reproduz o clique real do operador:
    'Imprimir' -> popup 'Pular Impressão'.

    NÃO recebe lista de códigos — processa o que estiver na fila. A
    própria fila da Estação de Impressão já É o conjunto de pedidos
    faturados prontos para imprimir; não há filtro adicional a fazer
    aqui, EXCETO os IDs explicitamente ignorados via config.yaml (ver
    `estacao_impressao.ignorar_ids` — pedido do Hugo, 07/08: pedido
    35970 trava o popup de vez, mesmo com reload; ignorar de propósito
    é mais seguro que insistir automaticamente).

    Sem confirmação interativa (diferente do script original do
    agente_relatorio) — este projeto roda via pipeline agendado, sem
    terminal interativo; quem chama esta função já decidiu processar.

    Retorna dict: {"pendentes": int, "processados": int, "ignorados": list[str], "falhas": list[str]}
    """
    usuario, senha = _credenciais_provider(config)
    if not usuario or not senha:
        raise RuntimeError(
            "stokki.provider.usuario/senha (ou stokki.usuario/senha, como "
            "fallback) ausentes no config.yaml — necessários para o "
            "acesso direto à área /provider/."
        )

    ids_ignorados_config = {
        str(i) for i in config.get("estacao_impressao", {}).get("ignorar_ids", [])
    }

    resultado = {"pendentes": 0, "processados": 0, "ignorados": [], "falhas": []}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not visivel)
        page = browser.new_page()
        try:
            _login_provider(page, usuario, senha)

            botoes = page.query_selector_all("button.btn_finish")
            resultado["pendentes"] = len(botoes)
            logger.info(f"{len(botoes)} pedido(s) pendente(s) na Estação de Impressão.")

            if dry_run or not botoes:
                return resultado

            ids_para_processar = [b.get_attribute("data-id") for b in botoes]
            logger.info(f"IDs a processar: {ids_para_processar}")

            if ids_ignorados_config:
                ids_ignorados_nesta_fila = [i for i in ids_para_processar if i in ids_ignorados_config]
                if ids_ignorados_nesta_fila:
                    logger.warning(
                        f"Ignorando de propósito (config.yaml estacao_impressao.ignorar_ids): "
                        f"{ids_ignorados_nesta_fila}"
                    )
                    resultado["ignorados"] = ids_ignorados_nesta_fila
                    ids_para_processar = [i for i in ids_para_processar if i not in ids_ignorados_config]

            for data_id in ids_para_processar:
                # Verificação defensiva (achado em produção, 06/08): se o
                # popup do pedido ANTERIOR ficou preso aberto (fechamento
                # falhou silenciosamente), ele bloqueia o clique de TODO
                # pedido seguinte por até 30s cada -- o Playwright fica
                # tentando a mesma ação até estourar o timeout, sem saber
                # que o problema é um modal de OUTRO pedido no caminho.
                # Mais barato resolver aqui (checagem rápida) do que deixar
                # o Playwright descobrir isso sozinho, pedido por pedido.
                if page.query_selector("#modal_finish_hide.show"):
                    logger.warning(
                        f"  Popup de um pedido anterior ainda estava aberto -- "
                        f"tentando fechar antes de continuar com data-id={data_id}."
                    )
                    _fechar_popup_pular_impressao(page)

                # re-busca o botão pelo data-id específico — se já saiu da
                # fila (processado por outra via nesse meio-tempo), pula
                botao = page.query_selector(f"button.btn_finish[data-id='{data_id}']")
                if not botao:
                    logger.info(f"  Pedido data-id={data_id} já saiu da fila, pulando.")
                    continue

                logger.info(f"  Imprimindo pedido data-id={data_id}...")
                # Cada pedido é isolado num try/except (achado em produção,
                # 06/08: "ElementHandle.click: Timeout 30000ms exceeded" num
                # ÚNICO pedido derrubava a função INTEIRA -- todos os outros
                # pedidos da fila, que talvez processassem numa boa, nem
                # chegavam a ser tentados, e o "processados" contado até ali
                # se perdia junto por causa da exceção não capturada). Agora
                # 1 pedido problemático vira uma falha registrada, com
                # screenshot pra diagnóstico, e o loop continua pro próximo.
                try:
                    botao.click()
                    sucesso = _fechar_popup_pular_impressao(page)
                    if sucesso:
                        resultado["processados"] += 1
                    else:
                        resultado["falhas"].append(data_id)
                        logger.warning(
                            f"  AVISO: pedido data-id={data_id} não processou corretamente "
                            f"— popup não apareceu ou não fechou. Conferir manualmente."
                        )
                except Exception as e:
                    resultado["falhas"].append(data_id)
                    logger.error(f"  ERRO ao processar pedido data-id={data_id}: {e}")
                    try:
                        _RAIZ_DADOS.mkdir(parents=True, exist_ok=True)
                        caminho_print = _RAIZ_DADOS / f"erro_impressao_{data_id}.png"
                        page.screenshot(path=str(caminho_print))
                        logger.error(f"  Screenshot salvo em {caminho_print} pra diagnóstico.")
                    except Exception as e2:
                        logger.warning(f"  Não foi possível salvar screenshot: {e2}")

            if resultado["processados"] >= LIMITE_SEGURANCA_IMPRESSAO:
                logger.warning(
                    "ATENÇÃO: atingiu o limite de segurança — confira manualmente "
                    "se sobrou pedido na fila."
                )
        finally:
            browser.close()

    logger.info(
        f"{resultado['processados']}/{resultado['pendentes']} pedido(s) "
        f"processado(s) na Estação de Impressão."
    )
    if resultado["falhas"]:
        logger.warning(f"  {len(resultado['falhas'])} falha(s): {resultado['falhas']}")
    return resultado
