# -*- coding: utf-8 -*-
"""
capturar_dashboard.py

Captura o dashboard como imagem (PNG, página inteira) e PDF, usando
Playwright headless — mesma biblioteca já usada no resto do projeto
pra automação da Stokki (nenhuma dependência nova). Usado tanto pelo
botão "Tirar foto" no Flask quanto pela rotina semanal de e-mail
(sexta-feira 17h, ver enviar_relatorio_dashboard.py).

Autentica via HTTP Basic Auth (mesmas credenciais do dashboard,
config.yaml dashboard_embarcadores.usuario/.senha).
"""
import logging
from pathlib import Path

from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)


def capturar_dashboard(
    url: str,
    usuario: str,
    senha: str,
    caminho_png: Path,
    caminho_pdf: Path,
    largura: int = 1400,
    espera_graficos_ms: int = 2000,
) -> tuple[Path, Path]:
    """
    Abre o dashboard num Chromium headless (autenticado via HTTP Basic
    Auth), espera a página carregar + os gráficos (Chart.js, que
    desenham via fetch() assíncrono) terminarem de renderizar, e salva
    um PNG (página inteira) e um PDF.

    Levanta a exceção original se o Playwright falhar (ex: dashboard
    fora do ar) — quem chama decide como tratar/logar.
    """
    caminho_png = Path(caminho_png)
    caminho_pdf = Path(caminho_pdf)
    caminho_png.parent.mkdir(parents=True, exist_ok=True)
    caminho_pdf.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        navegador = p.chromium.launch()
        try:
            contexto = navegador.new_context(
                viewport={"width": largura, "height": 1000},
                http_credentials={"username": usuario, "password": senha},
            )
            pagina = contexto.new_page()
            pagina.goto(url, wait_until="networkidle", timeout=30000)
            pagina.wait_for_timeout(espera_graficos_ms)

            pagina.screenshot(path=str(caminho_png), full_page=True)
            pagina.pdf(
                path=str(caminho_pdf),
                format="A4",
                print_background=True,
                margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"},
            )
        finally:
            navegador.close()

    logger.info(f"Dashboard capturado: {caminho_png.name} ({caminho_png.stat().st_size} bytes), "
               f"{caminho_pdf.name} ({caminho_pdf.stat().st_size} bytes)")
    return caminho_png, caminho_pdf
