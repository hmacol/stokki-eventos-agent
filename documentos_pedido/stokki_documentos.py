# -*- coding: utf-8 -*-
"""
stokki_documentos.py

Busca documentos de cada pedido na Stokki -- pedido do Hugo, 05/08 e
10/08. Confirmado com o Hugo: navega pela MESMA página que já usamos
pra ANEXAR o canhoto em expedir_pedidos.py::anexar_canhoto() (login +
/provider/inventory/outbound/show/{id}), já testada em produção.

FASE 1 (10/08, validada contra pedido real PS-27776/Padrão Puro) --
DANFE: quando o pedido tem NF-e com XML anexado, a página tem um
botão ".btn_danfe" (data-url = caminho do XML) que dispara um POST
pra /pt-br/document/danfe (form_danfe: _token + file_url) e devolve o
PDF do DANFE pronto. gerar_danfe() replica esse POST direto via
fetch() no contexto da página (evita abrir a popup target=_blank que
o botão abre de verdade). Pedido sem NF-e/XML não tem esse botão --
gerar_danfe() retorna None nesse caso, sem erro.

FASE 2 (10/08, validada contra pedidos reais de Marchef-Itaueira e
Cogumelado, que têm Boleto anexado -- pedido do Hugo, "vamos seguir
pros boletos") -- documentos JÁ anexados na aba "Documentos" do
pedido (Boleto, Comprovante de Entrega, Romaneio Interno, DANFE
subido manualmente pelo cliente, etc). Confirmado: o elemento é
"#document" (já presente no DOM assim que a página carrega, SEM
precisar clicar em nenhuma aba -- é um tab-pane escondido por CSS,
não removido do DOM), com um ".callout" por documento. O RÓTULO
visível é texto LIVRE digitado por quem anexou (visto: "BOLETO 8482",
"BO 8553", "nota", "NOTA", "Boleto") -- nada padronizado, então
listar_documentos_da_aba() não tenta adivinhar o tipo pelo rótulo: só
baixa o arquivo e deixa classificador.py (que já reconhece Boleto
pelo CONTEÚDO do PDF -- linha digitável, vencimento, código de
barras) decidir. O único item que listar_documentos_da_aba() ignora
de propósito é o XML da NF-e (link "/xml/nfe/...", não é PDF) -- esse
já é tratado à parte por gerar_danfe().

XML PLACEHOLDER DA STOKKI (achado 10/09, investigando a Fruta Fina):
quando o cliente cria o pedido SEM subir a NF-e no campo próprio, a
Stokki fabrica um XML de mentira pra ele -- chave de acesso começando
com "99" (cUF 99 não existe), nNF = id interno de venda da Stokki,
vNF 0, tpNF 0 (entrada), destinatário = o próprio embarcador. O botão
".btn_danfe" renderiza esse lixo numa DANFE "válida" na aparência
(R$ 0,00, "RECEBEMOS DE <cliente>... DESTINATÁRIO: <embarcador>"), que
o agente aceitava como NF e imprimia no romaneio (Fruta Fina, DANSKEN,
CIAO, TIE, BURIN, AÇAÍ MOTION -- confirmado por amostragem no GCS). Foi
isso que motivou o bloqueio da Fruta Fina em 20/08 (c03622b), sem que
a causa tivesse sido identificada na época.
Nesse cenário, alguns clientes (Fruta Fina, sempre) sobem o XML REAL
como anexo genérico da aba Documentos (rótulo "xml"/"XML", href
/document/download/document/<base64 de users/.../<id>.xml>). O
gerador de DANFE da Stokki aceita QUALQUER caminho de XML em file_url
(testado ao vivo em 10/09 com PS-38851: DANFE certa, NF 24059) -- então
xml_placeholder() + localizar_xmls_anexados() fazem a DANFE (e o XML
anexado na notificação de transportadoras) sair do XML real em vez do
placeholder. Sem XML real anexado, o pedido fica SEM NF (pendência
honesta no romaneio) em vez de uma NF falsa.
"""
import base64
import logging
import re
from pathlib import Path

from fingerprint_documentos import ja_enviado_para_pedido

logger = logging.getLogger(__name__)

STOKKI_BASE = "https://freshlog.stokki.com.br"
URL_PROVIDER_SHW = f"{STOKKI_BASE}/pt-br/provider/inventory/outbound/show"
URL_DANFE = f"{STOKKI_BASE}/pt-br/document/danfe"

PASTA_TEMP_DOWNLOADS = Path(__file__).parent / "dados" / "downloads_stokki_temp"

# Chave de acesso de NF-e tem 44 dígitos e começa pelo código IBGE da
# UF (11..53). A Stokki nomeia o XML placeholder com uma "chave" que
# começa em 99 -- ver docstring do módulo.
_RE_CHAVE_PLACEHOLDER = re.compile(r"(?:^|/)99\d{42}\.xml$", re.IGNORECASE)
_RE_ID_NFE = re.compile(r'Id="NFe(\d{44})"')


def _decodificar_caminho(href_ou_caminho: str) -> str:
    """Os links de documento da Stokki terminam num base64 do caminho
    interno do arquivo ('users/clients/inventories/.../x.xml'). Devolve
    esse caminho; se o valor já for um caminho (data-url do botão DANFE)
    ou não decodificar, devolve como veio."""
    valor = (href_ou_caminho or "").strip()
    ultimo = valor.rstrip("/").rsplit("/", 1)[-1]
    try:
        decodificado = base64.b64decode(ultimo + "=" * (-len(ultimo) % 4)).decode("utf-8")
    except Exception:
        return valor
    return decodificado if decodificado.startswith("users/") else valor


def xml_placeholder(href_ou_caminho: str) -> bool:
    """True se o XML da NF-e apontado (href do link "/xml/nfe/" ou o
    data-url do botão DANFE) é o placeholder gerado pela Stokki."""
    return bool(_RE_CHAVE_PLACEHOLDER.search(_decodificar_caminho(href_ou_caminho)))


def _chave_do_xml(caminho_xml: Path) -> str | None:
    """Chave de acesso (Id="NFe...") do XML em disco; None se não for
    NF-e ou se for placeholder (chave 99...)."""
    try:
        texto = caminho_xml.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = _RE_ID_NFE.search(texto)
    if not m or m.group(1).startswith("99"):
        return None
    return m.group(1)


def localizar_xmls_anexados(page) -> list[dict]:
    """
    XMLs de NF-e subidos como ANEXO GENÉRICO na aba Documentos (não o
    link "/xml/nfe/" do campo próprio). Identificado pelo CAMINHO
    decodificado terminar em ".xml", não pelo rótulo (texto livre --
    visto "xml", "XML"). Retorna [{"href", "caminho"}] na ordem da aba.
    """
    if not page.query_selector("#document"):
        return []
    resultado = []
    for callout in page.query_selector_all("#document .callout"):
        link_el = callout.query_selector("a.btn")
        if not link_el:
            continue
        href = link_el.get_attribute("href") or ""
        if not href or "/xml/nfe/" in href:
            continue
        caminho = _decodificar_caminho(href)
        if caminho.lower().endswith(".xml"):
            resultado.append({"href": href, "caminho": caminho})
    return resultado


def baixar_xmls_reais_anexados(page, codigo_ps: str) -> list[dict]:
    """Baixa os XMLs anexados (localizar_xmls_anexados) e devolve só os
    que são NF-e de verdade, sem repetir chave: [{"caminho_local",
    "caminho", "chave"}]. Anexo que não é NF-e (ou é placeholder) fica
    de fora com aviso."""
    resultado = []
    vistas: set[str] = set()
    for i, anexo in enumerate(localizar_xmls_anexados(page), start=1):
        caminho_local = baixar_documento(page, anexo["href"], f"{codigo_ps}_anexo{i}.xml")
        if not caminho_local:
            continue
        chave = _chave_do_xml(caminho_local)
        if not chave:
            logger.warning(f"  {codigo_ps}: anexo XML {anexo['caminho']!r} não é NF-e válida -- ignorado.")
            continue
        if chave in vistas:
            continue
        vistas.add(chave)
        resultado.append({"caminho_local": caminho_local, "caminho": anexo["caminho"], "chave": chave})
    return resultado


def _extrair_id(codigo_ps: str) -> str:
    # Primeira sequência de dígitos, NÃO a última: reentrega tem código
    # 'PS-36327-R1' e r'(\d+)$' extraía o '1' do sufixo -- a página
    # aberta era /show/1 (pedido MOMBAK de 2024) e a DANFE/boleto DELE
    # entravam como documentos do pedido errado (caso real, 12/08).
    m = re.search(r"(\d+)", codigo_ps)
    return m.group(1) if m else ""


def _credenciais_provider(config: dict) -> tuple[str, str]:
    """Mesmo padrão de expedir_pedidos.py::_credenciais_provider."""
    provider = config.get("stokki", {}).get("provider", {})
    usuario = provider.get("usuario") or config.get("stokki", {}).get("usuario", "")
    senha = provider.get("senha") or config.get("stokki", {}).get("senha", "")
    return usuario, senha


def _novo_contexto_disfarcado(browser):
    """Contexto do Chromium com UA de navegador real e navigator.webdriver
    mascarado. Sem isso a Stokki bloqueia com 403 ("Acesso automatizado
    nao e permitido") -- o Chromium headless por padrao expoe
    "HeadlessChrome" no User-Agent e navigator.webdriver=true (mesmo
    problema e fix de stokki/auth.py e stokki/estacao_impressao.py, 20/08:
    aqui o sintoma era timeout no wait_for_selector do e-mail porque a
    pagina carregada era a de bloqueio, nao o formulario de login)."""
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return context


def nova_pagina(browser):
    """Página do Chromium já disfarçada de navegador real (ver
    _novo_contexto_disfarcado) -- usar no lugar de
    browser.new_context().new_page() antes de chamar _login()."""
    return _novo_contexto_disfarcado(browser).new_page()


def _preencher_form_login(page, usuario: str, senha: str, tentativas: int = 3, timeout_ms: int = 15_000):
    """Preenche e envia o formulário de login do /provider/, com
    retentativas -- mesmo helper de stokki/estacao_impressao.py, 20/08:
    o Stokki às vezes demora mais que o timeout pra renderizar o
    formulário (lento/instável). Cada tentativa recarrega a página antes
    de tentar de novo."""
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
            logger.warning(f"  Falha no formulario de login (tentativa {tentativa}/{tentativas}): {e}")
            if tentativa < tentativas:
                try:
                    page.reload(timeout=timeout_ms)
                except Exception:
                    pass
                page.wait_for_timeout(1000)
    raise ultimo_erro


def _login(page, config: dict):
    """Mesmo login de expedir_pedidos.py::_setup_playwright. A page
    precisa vir de nova_pagina() (contexto disfarçado) -- ver acima."""
    usuario, senha = _credenciais_provider(config)
    logger.info(f"Login provider com usuario: {usuario!r}")
    page.goto(f"{STOKKI_BASE}/pt-br/login", wait_until="domcontentloaded", timeout=30_000)
    _preencher_form_login(page, usuario, senha)
    page.wait_for_timeout(400)


def listar_documentos_da_aba(page) -> list[dict]:
    """
    Lista os documentos já anexados na aba Documentos do pedido (a
    page já precisa estar na tela /provider/.../show/{id} -- ver
    buscar_documentos_do_pedido). Retorna [{"nome_visivel", "href"}]
    pra cada documento, exceto o XML da NF-e (ver docstring do
    módulo) -- esse fica de fora, é tratado à parte por gerar_danfe().
    """
    if not page.query_selector("#document"):
        return []

    documentos = []
    for callout in page.query_selector_all("#document .callout"):
        link_el = callout.query_selector("a.btn")
        if not link_el:
            continue
        href = link_el.get_attribute("href") or ""
        if not href or "/xml/nfe/" in href:
            continue
        label_el = callout.query_selector("p.mb-auto")
        texto = (label_el.inner_text().strip() if label_el else "") or href.split("/")[-1]
        documentos.append({"nome_visivel": texto, "href": href})
    return documentos


def baixar_documento(page, url_documento: str, nome_arquivo: str) -> Path | None:
    """Baixa um documento pelo link direto, usando os cookies de sessão
    já autenticados da page (mesma técnica de fetch() nativo usada em
    expedir_na_stokki, evita reautenticar)."""
    PASTA_TEMP_DOWNLOADS.mkdir(parents=True, exist_ok=True)
    # Nome-base apenas -- nome_arquivo vem do texto/href de um link
    # escaneado na página, e não deve poder escrever fora da pasta.
    caminho_local = PASTA_TEMP_DOWNLOADS / Path(nome_arquivo).name

    try:
        conteudo_base64 = page.evaluate("""async (url) => {
            const resp = await fetch(url, { credentials: 'include' });
            const buffer = await resp.arrayBuffer();
            const bytes = new Uint8Array(buffer);
            let binario = '';
            for (let i = 0; i < bytes.length; i++) binario += String.fromCharCode(bytes[i]);
            return btoa(binario);
        }""", url_documento)
        import base64
        with open(caminho_local, "wb") as f:
            f.write(base64.b64decode(conteudo_base64))
        return caminho_local
    except Exception as e:
        logger.warning(f"  Falha ao baixar documento '{nome_arquivo}': {e}")
        return None


def localizar_link_xml_nfe(page) -> str | None:
    """
    Acha o link do XML da NF-e (href contendo "/xml/nfe/") na aba
    Documentos do pedido -- mesma varredura de listar_documentos_da_aba(),
    mas devolvendo esse link em vez de descartá-lo (usado pela notificação
    de transportadoras, que precisa do XML cru, não do DANFE em PDF --
    pedido do Hugo, 13/08, ver notificacao_transportadoras/). A page já
    precisa estar na tela /provider/.../show/{id} (ver buscar_documentos_do_pedido).
    """
    if not page.query_selector("#document"):
        return None
    for callout in page.query_selector_all("#document .callout"):
        link_el = callout.query_selector("a.btn")
        if not link_el:
            continue
        href = link_el.get_attribute("href") or ""
        if href and "/xml/nfe/" in href:
            return href
    return None


def baixar_xml_nfe(page, codigo_ps: str) -> Path | None:
    """Baixa o XML cru da NF-e do pedido (a page já precisa estar na tela
    /provider/.../show/{id} desse pedido). Retorna None se o pedido não
    tem XML anexado -- não é erro, só não tem o que baixar.

    Se o XML do campo próprio é o placeholder da Stokki (ver docstring
    do módulo), usa o primeiro XML REAL anexado na aba Documentos; sem
    nenhum, devolve None -- mandar o placeholder pra transportadora
    (aconteceu com a TAFF em 24-25/08, Fruta Fina) é pior que não
    mandar nada."""
    href = localizar_link_xml_nfe(page)
    if href and not xml_placeholder(href):
        return baixar_documento(page, href, f"{codigo_ps}_NFe.xml")

    reais = baixar_xmls_reais_anexados(page, codigo_ps)
    if not reais:
        if href:
            logger.warning(f"  {codigo_ps}: XML da NF-e na Stokki é placeholder e não há XML real "
                           f"anexado na aba Documentos -- sem XML.")
        return None
    if len(reais) > 1:
        logger.warning(f"  {codigo_ps}: {len(reais)} XMLs reais anexados -- usando o primeiro "
                       f"(chave {reais[0]['chave']}).")
    caminho_final = PASTA_TEMP_DOWNLOADS / f"{codigo_ps}_NFe.xml"
    caminho_final.write_bytes(reais[0]["caminho_local"].read_bytes())
    logger.info(f"  {codigo_ps}: XML da NF-e tomado do anexo da aba Documentos (placeholder no campo próprio).")
    return caminho_final


def gerar_danfes(page, codigo_ps: str) -> list[Path]:
    """
    Gera o(s) PDF(s) de DANFE pro pedido (a page já precisa estar na tela
    /provider/.../show/{id} desse pedido -- ver buscar_documentos_do_pedido).
    Lista vazia se o pedido não tem NF-e/XML anexado (botão ".btn_danfe"
    não existe na página nesse caso -- não é erro, só não tem o que gerar).

    XML do campo próprio placeholder (ver docstring do módulo): NUNCA
    gera a DANFE dele; gera uma DANFE por XML real anexado na aba
    Documentos (normalmente 1; PS-37130 tinha o mesmo XML subido 2x --
    chave repetida é deduplicada em baixar_xmls_reais_anexados). Sem XML
    real, lista vazia -- o pedido fica sem NF de verdade.
    """
    if not page.query_selector(".btn_danfe"):
        return []

    file_url = page.eval_on_selector(".btn_danfe", "el => el.dataset.url")
    token = page.eval_on_selector("#form_danfe input[name='_token']", "el => el.value")
    if not file_url or not token:
        logger.warning(f"  {codigo_ps}: botão DANFE presente mas sem data-url/_token -- pulando.")
        return []

    if not xml_placeholder(file_url):
        caminho = _gerar_danfe_por_file_url(page, codigo_ps, file_url, token, f"{codigo_ps}_DANFE.pdf")
        return [caminho] if caminho else []

    reais = baixar_xmls_reais_anexados(page, codigo_ps)
    if not reais:
        logger.warning(f"  {codigo_ps}: XML da NF-e na Stokki é placeholder (cliente não subiu a NF-e) "
                       f"e não há XML real anexado na aba Documentos -- DANFE não gerada.")
        return []
    logger.info(f"  {codigo_ps}: XML da NF-e na Stokki é placeholder -- DANFE gerada a partir de "
                f"{len(reais)} XML real(is) anexado(s) na aba Documentos.")
    caminhos = []
    for i, real in enumerate(reais, start=1):
        sufixo = "" if i == 1 else f"_{i}"
        caminho = _gerar_danfe_por_file_url(page, codigo_ps, real["caminho"], token,
                                            f"{codigo_ps}_DANFE{sufixo}.pdf")
        if caminho:
            caminhos.append(caminho)
    return caminhos


def gerar_danfe(page, codigo_ps: str) -> Path | None:
    """Compatibilidade: primeira DANFE de gerar_danfes(), ou None."""
    caminhos = gerar_danfes(page, codigo_ps)
    return caminhos[0] if caminhos else None


def _gerar_danfe_por_file_url(page, codigo_ps: str, file_url: str, token: str,
                              nome_arquivo: str) -> Path | None:
    """POST /pt-br/document/danfe (form_danfe: _token + file_url) via
    fetch() no contexto da página; salva o PDF em PASTA_TEMP_DOWNLOADS."""
    try:
        resultado = page.evaluate("""async ({url, fileUrl, token}) => {
            const fd = new FormData();
            fd.append('_token', token);
            fd.append('file_url', fileUrl);
            const resp = await fetch(url, { method: 'POST', body: fd, credentials: 'include' });
            const buffer = await resp.arrayBuffer();
            const bytes = new Uint8Array(buffer);
            let binario = '';
            for (let i = 0; i < bytes.length; i++) binario += String.fromCharCode(bytes[i]);
            return { status: resp.status, contentType: resp.headers.get('content-type'), b64: btoa(binario) };
        }""", {"url": URL_DANFE, "fileUrl": file_url, "token": token})
    except Exception as e:
        logger.warning(f"  {codigo_ps}: falha ao gerar DANFE: {e}")
        return None

    if resultado["status"] != 200 or "pdf" not in (resultado["contentType"] or "").lower():
        logger.warning(f"  {codigo_ps}: resposta do DANFE não é um PDF "
                       f"(status={resultado['status']}, content-type={resultado['contentType']!r}).")
        return None

    PASTA_TEMP_DOWNLOADS.mkdir(parents=True, exist_ok=True)
    caminho_local = PASTA_TEMP_DOWNLOADS / nome_arquivo
    caminho_local.write_bytes(base64.b64decode(resultado["b64"]))
    return caminho_local


def buscar_documentos_do_pedido(page, config: dict, codigo_ps: str,
                                buscar_nf: bool = True) -> list[dict]:
    """
    Fluxo completo pra 1 pedido: gera o DANFE (Fase 1, pula se já foi
    enviado antes -- ele muda de hash a cada geração, então precisa
    desse controle à parte do fingerprint por conteúdo) e baixa tudo
    que já está anexado na aba Documentos (Fase 2 -- Boleto e outros).
    Retorna [{"caminho_local", "nome_arquivo"}] -- mesmo formato usado
    por email_documentos.py, pra alimentar o mesmo pipeline de
    classificar/casar/enviar depois.

    buscar_nf=False pula SÓ a geração do DANFE -- por dois motivos
    diferentes, decididos em selecionar_pedidos.py e combinados pelo
    chamador (processar_documentos.py):
      - embarcadores cujas entregas não precisam ir acompanhadas de
        Nota Fiscal (Padrão Puro, Quatro Estrelas, Pedramoura -- pedido
        do Hugo, 13/08; ver EMBARCADORES_SEM_NF);
      - embarcadores que PRECISAM de Nota Fiscal, mas cuja DANFE nunca
        pode vir do XML anexado na Stokki (Laticínios Dourado, Muai --
        pedido do Hugo, 17/08, casos reais de XML errado/divergente
        anexado no pedido; ver EMBARCADORES_DANFE_SOMENTE_EMAIL) -- pra
        esses, a DANFE só entra pelo fluxo de e-mail.
    A aba Documentos continua sendo olhada normalmente (boleto etc) nos
    dois casos.
    """
    page.goto(
        f"{URL_PROVIDER_SHW}/{_extrair_id(codigo_ps)}",
        wait_until="networkidle", timeout=30_000,
    )
    page.wait_for_timeout(400)

    baixados = []

    if buscar_nf and not ja_enviado_para_pedido(codigo_ps, "Nota Fiscal"):
        for caminho_danfe in gerar_danfes(page, codigo_ps):
            baixados.append({"caminho_local": caminho_danfe, "nome_arquivo": caminho_danfe.name})

    for doc in listar_documentos_da_aba(page):
        nome_arquivo = doc["nome_visivel"]
        if not nome_arquivo.lower().endswith(".pdf"):
            nome_arquivo += ".pdf"
        nome_arquivo = f"{codigo_ps}_{nome_arquivo}"

        caminho_local = baixar_documento(page, doc["href"], nome_arquivo)
        if caminho_local:
            baixados.append({"caminho_local": caminho_local, "nome_arquivo": nome_arquivo})

    return baixados
