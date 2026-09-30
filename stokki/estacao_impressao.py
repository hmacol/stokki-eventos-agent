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
import time
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

BASE_URL     = "https://freshlog.stokki.com.br"
URL_PRINTING = f"{BASE_URL}/pt-br/provider/operation/order/printing"
URL_ORDER    = f"{BASE_URL}/pt-br/provider/operation/order/printing/order"
URL_SEARCH   = f"{BASE_URL}/pt-br/provider/operation/order/printing/search"
LIMITE_SEGURANCA_IMPRESSAO = 300  # trava de segurança: máx. de cliques em "Imprimir" por execução
# Botões 'Imprimir' da fila de verdade ('Pedidos faturados'); ver comentário em imprimir_pedidos_pendentes.
_SEL_BOTAO_FILA = "#div_billed button.btn_finish"

_RAIZ_DADOS = Path(__file__).resolve().parent.parent / "dados"


def _credenciais_provider(config: dict) -> tuple[str, str]:
    """Retorna (usuario, senha) para login na area /provider/ do Stokki."""
    provider = config.get("stokki", {}).get("provider", {})
    usuario = provider.get("usuario") or config.get("stokki", {}).get("usuario", "")
    senha   = provider.get("senha")   or config.get("stokki", {}).get("senha", "")
    return usuario, senha


def _novo_contexto_disfarcado(browser):
    """Contexto do Chromium com UA de navegador real e navigator.webdriver
    mascarado. Sem isso a Stokki bloqueia com 403 ("Acesso automatizado
    nao e permitido") -- o Chromium headless por padrao expoe
    "HeadlessChrome" no User-Agent e navigator.webdriver=true (mesmo
    problema e fix de stokki/auth.py)."""
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return context


def _preencher_form_login(page, usuario, senha, tentativas=3, timeout_ms=15_000):
    """Preenche e envia o formulario de login do /provider/, com retentativas.

    Achado em producao na VPS (20/08): o formulario as vezes demora mais que
    o timeout pra renderizar (Stokki lento/instavel) e o wait_for_selector
    estourava sem segunda chance, derrubando a etapa inteira. Cada tentativa
    recarrega a pagina antes de tentar de novo."""
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            page.wait_for_selector("[name='email']", timeout=timeout_ms)
            page.fill("[name='email']", usuario)
            page.fill("[name='password']", senha)
            page.click("button[type='submit']")
            page.wait_for_url(lambda u: "login" not in u, timeout=30_000)
            return
        except Exception as e:
            ultimo_erro = e
            logger.warning(
                f"Falha no formulario de login (tentativa {tentativa}/{tentativas}): {e}"
            )
            if tentativa < tentativas:
                try:
                    page.reload(timeout=timeout_ms)
                except Exception:
                    pass
                page.wait_for_timeout(1000)
    raise ultimo_erro


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
        try:
            context = _novo_contexto_disfarcado(browser)
            page    = context.new_page()

            # Navega para o provider antes do login para estabelecer sessao correta
            page.goto(URL_PRINTING, wait_until="domcontentloaded", timeout=30_000)
            _preencher_form_login(page, usuario, senha)

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
        finally:
            # Garante que o Chromium headless sempre e fechado -- sem isso,
            # uma falha de login (seletor mudou, timeout, etc.) deixava o
            # processo orfao, acumulando a cada execucao agendada.
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
        try:
            context = _novo_contexto_disfarcado(browser)
            page    = context.new_page()

            page.goto(URL_PRINTING, wait_until="domcontentloaded", timeout=30_000)
            _preencher_form_login(page, usuario, senha)

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
        finally:
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
    _preencher_form_login(page, usuario, senha)

    # Recarrega a Estacao de Impressao ja autenticado, garantindo que a
    # tabela de pendentes carregue do zero (mesmo padrao usado em
    # listar_pedidos_em_espera, em vez de confiar no redirect pos-login).
    _navegar_com_retry(page, URL_PRINTING)
    _aguardar_pagina_acalmar(page)
    logger.info(f"Login OK. URL atual: {page.url}")


# Stub do print-js instalado em TODA página do contexto (add_init_script),
# antes dos scripts da Stokki rodarem -- achado em produção (15/09):
#
# O popup 'Imprimir' da Estação de Impressão é uma sequência de etapas
# (Danfe Simplificada, NF-e complementar, Danfe A4, etiqueta, declaração,
# carta de correção, ...). Em cada etapa a página da Stokki clica SOZINHA
# em 'Sim' ~1,4s depois de mostrar a etapa e chama printJS(pdf, {
# onLoadingStart, onPrintDialogClose, onError }). O onLoadingStart
# DESABILITA o botão 'Pular Impressão'; o fluxo só avança quando o
# print-js chama onPrintDialogClose -- e o print-js 1.6 só faz isso
# quando a janela recebe um evento 'focus' depois do diálogo de impressão
# fechar. No Chromium headless não existe diálogo nem evento de foco, e o
# modal fica preso pra sempre em "Imprimindo Danfe Simplificada ..." com
# o 'Pular Impressão' desabilitado. Era exatamente isso que travava os
# pedidos com DANFE simplificada habilitada (33430, 33436, 33466, 34071,
# 34792 desde 23/08; 39222 em 15/09), e os que "funcionavam" só
# funcionavam porque nosso clique em 'Pular Impressão' ganhava a corrida
# de 1,4s contra o 'Sim' automático.
#
# O stub faz o que 'Pular Impressão' faria em cada etapa: dispara
# onLoadingStart e, em seguida, onPrintDialogClose -- sem imprimir nada
# (nada é impresso na VPS de qualquer jeito). A página então percorre
# todas as etapas sozinha até 'Finalizar' (POST /printing/store) e
# recarrega a lista -- é isso que move o pedido de 'Em espera' pra
# 'Aguardando Transportador'.
#
# defineProperty com setter vazio: o vendor/print-js/print.min.js carrega
# DEPOIS do init script e tentaria sobrescrever window.printJS; com a
# propriedade não-configurável a atribuição é ignorada (ou lança dentro
# do próprio print.min.js, sem afetar o resto da página). Vale também
# depois de cada navegação/reload, sem precisar reinstalar.
_STUB_PRINTJS_JS = """
(() => {
  const stub = function (params) {
    try { if (params && params.onLoadingStart) params.onLoadingStart(); } catch (e) {}
    setTimeout(() => {
      try { if (params && params.onPrintDialogClose) params.onPrintDialogClose(); } catch (e) {}
    }, 50);
  };
  stub.__stokki_stub = true;
  try {
    Object.defineProperty(window, 'printJS', {
      get: () => stub, set: () => {}, configurable: false, enumerable: true
    });
  } catch (e) {
    window.printJS = stub;
  }
})();
"""

# Estado do modal 'Imprimir', lido a cada meio segundo por _aguardar_fluxo_impressao.
_JS_ESTADO_MODAL = """
() => {
  const m = document.querySelector('#modal_finish_hide');
  if (!m || !m.classList.contains('show')) return {aberto: false};
  const visivel = (el) => !!el && el.offsetParent !== null;
  const etapas = [...m.querySelectorAll('.div_reset')].filter(visivel).map(d => d.id);
  const pular = [...m.querySelectorAll('button')].find(
    b => visivel(b) && !b.disabled && (b.dataset.text || b.textContent || '').trim() === 'Pular Impressão');
  return {
    aberto: true,
    etapas: etapas,
    pular_id: pular ? pular.id : null,
    pagina_hide: visivel(m.querySelector('#div_page_hide')),
    texto: (m.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120),
    stub: !!(window.printJS && window.printJS.__stokki_stub),
  };
}
"""


def _aguardar_fluxo_impressao(page, data_id: str, timeout_s: int = 60) -> bool:
    """
    Depois de clicar em 'Imprimir', acompanha o popup até a Stokki
    finalizar o pedido (POST /printing/store + reload da lista). Com o
    stub do print-js instalado (ver _STUB_PRINTJS_JS) o popup percorre as
    etapas sozinho; aqui só damos um empurrão onde a página espera um
    humano:
      - 'Pular Impressão' habilitado e visível -> clica (evita até baixar
        o PDF; também é o caminho antigo, caso o stub não tenha pegado);
      - etapa 'páginas impressas' (div_page_hide, sem clique automático)
        -> 'Não'.
    Retorna True quando o modal fechou (pedido finalizado), False se
    ficou preso até o timeout -- nesse caso recarrega a página pra não
    contaminar os pedidos seguintes da fila (achado de 07/08: um modal
    preso fazia TODO o resto do lote falhar).
    """
    inicio = time.time()
    ultimo_texto = None
    try:
        # O modal abre depois do POST /printing/search (~0,5s); sem esta
        # espera a primeira leitura viria antes dele existir e o loop
        # concluiria "fechou" sem nada ter acontecido.
        page.wait_for_selector("#modal_finish_hide.show", timeout=10_000)
    except Exception:
        logger.warning(f"  Popup do pedido data-id={data_id} não apareceu em 10s.")
        return False
    while time.time() - inicio < timeout_s:
        try:
            estado = page.evaluate(_JS_ESTADO_MODAL)
        except Exception:
            # Contexto destruído = a página está navegando (reload depois
            # do /printing/store). Espera e lê de novo.
            page.wait_for_timeout(500)
            continue

        if not estado.get("aberto"):
            return True

        if estado.get("texto") != ultimo_texto:
            logger.info(
                f"    +{time.time() - inicio:4.1f}s etapa={estado.get('etapas')} "
                f"pular={estado.get('pular_id')} texto={estado.get('texto')!r}"
            )
            ultimo_texto = estado.get("texto")

        try:
            if estado.get("pagina_hide"):
                page.click("#btn_no_page_hide", timeout=1000)
            elif estado.get("pular_id"):
                page.click(f"#{estado['pular_id']}", timeout=1000)
        except Exception:
            pass  # botão mudou de estado entre a leitura e o clique -- o loop lê de novo

        page.wait_for_timeout(500)

    logger.error(
        f"  Popup do pedido data-id={data_id} não fechou em {timeout_s}s "
        f"(último estado: {ultimo_texto!r}) -- recarregando a página pra não "
        f"travar os pedidos seguintes."
    )
    try:
        _RAIZ_DADOS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(_RAIZ_DADOS / f"erro_impressao_{data_id}.png"))
    except Exception:
        pass
    try:
        page.reload(wait_until="networkidle", timeout=30000)
    except Exception as e:
        logger.error(f"  Falha ao recarregar a página: {e} -- fila pode continuar travada.")
    return False


def imprimir_pedidos_pendentes(config: dict, dry_run: bool = False,
                               visivel: bool = False,
                               apenas_ids: set | None = None) -> dict:
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

    apenas_ids: se informado, processa só esses data-ids da fila (uso
    avulso/diagnóstico: `somente_impressao.py --id 39222`).

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
        context = _novo_contexto_disfarcado(browser)
        # Stub do print-js em toda página deste contexto (ver _STUB_PRINTJS_JS).
        context.add_init_script(_STUB_PRINTJS_JS)
        page = context.new_page()

        # Registra as respostas da Stokki que decidem o resultado de cada
        # pedido (busca ao abrir o popup e o 'Finalizar' que grava) -- sem
        # isso um erro do /printing/store só aparecia como toastr na tela.
        def _registrar_resposta(response):
            url = response.url
            if "/printing/store" in url or "/printing/search" in url:
                try:
                    corpo = response.text()[:300].replace("\n", " ")
                except Exception:
                    corpo = "<sem corpo>"
                nivel = logging.INFO if response.ok else logging.WARNING
                logger.log(nivel, f"    Stokki {url.rsplit('/', 1)[-1]}: HTTP {response.status} {corpo}")

        page.on("response", _registrar_resposta)
        try:
            _login_provider(page, usuario, senha)
            try:
                stub_ok = page.evaluate("() => !!(window.printJS && window.printJS.__stokki_stub)")
            except Exception:
                stub_ok = False
            if stub_ok:
                logger.info("Stub do print-js instalado na página (etapas de impressão fecham sozinhas).")
            else:
                logger.warning(
                    "Stub do print-js NÃO pegou -- a página pode travar em "
                    "'Imprimindo Danfe...' (fluxo antigo de 'Pular Impressão' segue como fallback)."
                )

            # Só a tabela 'Pedidos faturados' (#div_billed). A tabela
            # 'Últimos pedidos impressos' (#div_printing) também tem botão
            # 'Imprimir' (reimpressão) com a mesma classe btn_finish -- sem
            # o escopo, um pedido já impresso hoje era contado como
            # pendente, clicado de novo e, na conferência, dado como
            # "ainda na fila" (achado 15/09 com o 39222).
            botoes = page.query_selector_all(_SEL_BOTAO_FILA)
            resultado["pendentes"] = len(botoes)
            logger.info(f"{len(botoes)} pedido(s) pendente(s) na Estação de Impressão.")

            if dry_run or not botoes:
                return resultado

            ids_para_processar = [b.get_attribute("data-id") for b in botoes]
            if apenas_ids:
                apenas = {str(i) for i in apenas_ids}
                ids_para_processar = [i for i in ids_para_processar if i in apenas]
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
                if resultado["processados"] >= LIMITE_SEGURANCA_IMPRESSAO:
                    # Freio de segurança verificado A CADA pedido, não só
                    # depois que a fila inteira já foi percorrida -- sem
                    # isso, uma fila de milhares de pedidos travados clicava
                    # em todos antes do aviso, e o "limite" não limitava nada.
                    logger.warning(
                        f"ATENÇÃO: atingiu o limite de segurança de "
                        f"{LIMITE_SEGURANCA_IMPRESSAO} pedido(s) processados nesta "
                        f"execução -- parando aqui. Confira manualmente se sobrou "
                        f"pedido na fila."
                    )
                    break

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
                    _aguardar_fluxo_impressao(page, "anterior", timeout_s=30)

                # re-busca o botão pelo data-id específico — se já saiu da
                # fila (processado por outra via nesse meio-tempo), pula
                botao = page.query_selector(f"{_SEL_BOTAO_FILA}[data-id='{data_id}']")
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
                    inicio_pedido = time.time()
                    fechou = _aguardar_fluxo_impressao(page, data_id)
                    # Depois do 'Finalizar' a própria Stokki recarrega a
                    # lista -- confirma que o pedido realmente saiu da fila
                    # em vez de confiar só no modal ter sumido (o modal
                    # também some se o /printing/store falhar e a página
                    # for recarregada pelo fallback).
                    _aguardar_pagina_acalmar(page)
                    ainda_na_fila = page.query_selector(f"{_SEL_BOTAO_FILA}[data-id='{data_id}']")
                    if fechou and not ainda_na_fila:
                        resultado["processados"] += 1
                        logger.info(
                            f"  Pedido data-id={data_id} finalizado "
                            f"({time.time() - inicio_pedido:.0f}s) -- saiu da fila."
                        )
                    else:
                        resultado["falhas"].append(data_id)
                        logger.warning(
                            f"  AVISO: pedido data-id={data_id} não processou corretamente "
                            f"(modal fechou={fechou}, ainda na fila={bool(ainda_na_fila)}). "
                            f"Conferir manualmente."
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

        finally:
            browser.close()

    logger.info(
        f"{resultado['processados']}/{resultado['pendentes']} pedido(s) "
        f"processado(s) na Estação de Impressão."
    )
    if resultado["falhas"]:
        logger.warning(f"  {len(resultado['falhas'])} falha(s): {resultado['falhas']}")
    return resultado
