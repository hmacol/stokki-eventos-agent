# -*- coding: utf-8 -*-
"""
stokki_documentos.py

Busca documentos (NF, Boleto, CC, Agendamento, etc) já anexados na
aba "Documentos" de cada pedido na Stokki -- pedido do Hugo, 05/08.
Confirmado com o Hugo: é a MESMA aba que já usamos pra ANEXAR o
canhoto em expedir_pedidos.py::anexar_canhoto() -- reaproveita a
mesma navegação (login + ir pro /provider/inventory/outbound/show/{id}
+ clicar na aba Documentos), já testada em produção.

IMPORTANTE -- parte NÃO VALIDADA: o formato de como os documentos JÁ
EXISTENTES aparecem dentro dessa aba (nomes de elemento, link de
download) ainda não foi visto de verdade -- só a navegação ATÉ a aba
foi confirmada (usada pra fazer upload, não pra listar o que já tem
lá). listar_documentos_da_aba() abaixo tenta alguns seletores
plausíveis, mas precisa ser confirmado/ajustado contra a tela real --
ver debug/investigar_aba_documentos.py, feito exatamente pra isso.
"""
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

STOKKI_BASE = "https://freshlog.stokki.com.br"
URL_PROVIDER_SHW = f"{STOKKI_BASE}/pt-br/provider/inventory/outbound/show"

PASTA_TEMP_DOWNLOADS = Path(__file__).parent / "dados" / "downloads_stokki_temp"


def _extrair_id(codigo_ps: str) -> str:
    m = re.search(r"(\d+)$", codigo_ps)
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


def ir_para_aba_documentos(page, codigo_ps: str) -> bool:
    """Navega até a aba Documentos de um pedido -- mesma sequência já
    validada em produção por anexar_canhoto(). Retorna False se não
    conseguir achar a aba (pedido não existe, layout mudou, etc)."""
    page.goto(
        f"{URL_PROVIDER_SHW}/{_extrair_id(codigo_ps)}",
        wait_until="networkidle", timeout=30_000,
    )
    page.wait_for_timeout(400)

    seletores_tab = ["#document-tab", "a[href='#document ']", "a:text('Documentos')"]
    for sel in seletores_tab:
        try:
            page.click(sel, timeout=5_000)
            return True
        except Exception:
            continue
    return False


def listar_documentos_da_aba(page) -> list[dict]:
    """
    NÃO VALIDADO contra a tela real -- ver aviso no topo do arquivo.
    Tenta achar links de download de PDF dentro da aba Documentos
    (já deve estar aberta, ver ir_para_aba_documentos). Retorna
    [{"nome_visivel", "href"}] pra cada documento encontrado.

    Depois de rodar debug/investigar_aba_documentos.py contra um
    pedido real, essa função deve ser ajustada pro seletor certo.
    """
    try:
        elementos = page.query_selector_all("a[href$='.pdf'], a[href*='/document/download']")
        documentos = []
        for el in elementos:
            href = el.get_attribute("href")
            texto = (el.inner_text() or "").strip()
            if href:
                documentos.append({"nome_visivel": texto or href.split("/")[-1], "href": href})
        return documentos
    except Exception as e:
        logger.warning(f"  Falha ao listar documentos da aba: {e}")
        return []


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


def buscar_documentos_do_pedido(page, config: dict, codigo_ps: str) -> list[dict]:
    """
    Fluxo completo pra 1 pedido: vai pra aba Documentos, lista o que
    tem, baixa cada um. Retorna [{"caminho_local", "nome_arquivo"}]
    -- mesmo formato usado por email_documentos.py, pra alimentar o
    mesmo pipeline de classificar/casar/enviar depois.
    """
    if not ir_para_aba_documentos(page, codigo_ps):
        logger.warning(f"  {codigo_ps}: não encontrou a aba Documentos.")
        return []

    documentos_na_tela = listar_documentos_da_aba(page)
    if not documentos_na_tela:
        return []

    baixados = []
    for doc in documentos_na_tela:
        nome_arquivo = doc["nome_visivel"]
        if not nome_arquivo.lower().endswith(".pdf"):
            nome_arquivo += ".pdf"
        nome_arquivo = f"{codigo_ps}_{nome_arquivo}"

        caminho_local = baixar_documento(page, doc["href"], nome_arquivo)
        if caminho_local:
            baixados.append({"caminho_local": caminho_local, "nome_arquivo": nome_arquivo})

    return baixados
