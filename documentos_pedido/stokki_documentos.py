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


def _login(page, config: dict):
    """Mesmo login de expedir_pedidos.py::_setup_playwright."""
    usuario, senha = _credenciais_provider(config)
    logger.info(f"Login provider com usuario: {usuario!r}")
    page.goto(f"{STOKKI_BASE}/pt-br/login", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_selector("[name='email']", timeout=15_000)
    page.fill("[name='email']", usuario)
    page.fill("[name='password']", senha)
    page.click("button[type='submit']")
    page.wait_for_url(lambda u: "login" not in u, timeout=30_000)
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


def gerar_danfe(page, codigo_ps: str) -> Path | None:
    """
    Gera o PDF do DANFE pro pedido (a page já precisa estar na tela
    /provider/.../show/{id} desse pedido -- ver buscar_documentos_do_pedido).
    Retorna None se o pedido não tem NF-e/XML anexado (botão ".btn_danfe"
    não existe na página nesse caso -- não é erro, só não tem o que gerar).
    """
    if not page.query_selector(".btn_danfe"):
        return None

    file_url = page.eval_on_selector(".btn_danfe", "el => el.dataset.url")
    token = page.eval_on_selector("#form_danfe input[name='_token']", "el => el.value")
    if not file_url or not token:
        logger.warning(f"  {codigo_ps}: botão DANFE presente mas sem data-url/_token -- pulando.")
        return None

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
    caminho_local = PASTA_TEMP_DOWNLOADS / f"{codigo_ps}_DANFE.pdf"
    caminho_local.write_bytes(base64.b64decode(resultado["b64"]))
    return caminho_local


def buscar_documentos_do_pedido(page, config: dict, codigo_ps: str) -> list[dict]:
    """
    Fluxo completo pra 1 pedido: gera o DANFE (Fase 1, pula se já foi
    enviado antes -- ele muda de hash a cada geração, então precisa
    desse controle à parte do fingerprint por conteúdo) e baixa tudo
    que já está anexado na aba Documentos (Fase 2 -- Boleto e outros).
    Retorna [{"caminho_local", "nome_arquivo"}] -- mesmo formato usado
    por email_documentos.py, pra alimentar o mesmo pipeline de
    classificar/casar/enviar depois.
    """
    page.goto(
        f"{URL_PROVIDER_SHW}/{_extrair_id(codigo_ps)}",
        wait_until="networkidle", timeout=30_000,
    )
    page.wait_for_timeout(400)

    baixados = []

    if not ja_enviado_para_pedido(codigo_ps, "Nota Fiscal"):
        caminho_danfe = gerar_danfe(page, codigo_ps)
        if caminho_danfe:
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
