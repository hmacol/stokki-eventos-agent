# -*- coding: utf-8 -*-
"""
gerar_informativo_mensal.py

Virada de mês (Hugo, 07/10/2026): no dia 1 (stokki-informativo-mensal.timer,
07h) gera o informativo de dias de entrega do mês -- o MESMO HTML da página
pública app.freshhub.com.br/cliente/regioes (portal_cliente/templates/
regioes.html + regioes_atendimento.py), com o logo embutido -- grava HTML e
PDF em dados/informativos/, manda por e-mail (anexos prontos pra encaminhar
aos embarcadores) e avisa no grupo de WhatsApp com o link da página.

Nada é enviado aos clientes: o destino do e-mail segue a regra das outras
rotinas (informativo_mensal.forcar_destino; sem a chave, vai pro Hugo).

    py -3.11 gerar_informativo_mensal.py --modo-teste        # gera, não envia
    py -3.11 gerar_informativo_mensal.py --mes 2026-11
    py -3.11 gerar_informativo_mensal.py --proximo             # mês seguinte
"""
import argparse
import base64
import logging
import sys
import time
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent
for _p in (_RAIZ, _RAIZ / "portal_cliente", _RAIZ / "roteirizacao"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402
from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402

import regioes_atendimento as ra  # noqa: E402

logger = logging.getLogger("informativo_mensal")

CONFIG_PATH = _RAIZ / "config.yaml"
PASTA_SAIDA = _RAIZ / "dados" / "informativos"
LOGO = _RAIZ / "assets" / "logo_freshlog.png"
TEMPLATES = _RAIZ / "portal_cliente" / "templates"
EMAIL_TESTE = "hugo@freshlogbr.com"
DIA_VIRADA = 25   # a partir daqui, sem --mes, o alvo já é o mês seguinte


def mes_alvo(hoje: date, proximo: bool = False) -> tuple[int, int]:
    if proximo or hoje.day >= DIA_VIRADA:
        return ra.mes_seguinte(hoje.year, hoje.month)
    return hoje.year, hoje.month


def logo_data_uri() -> str:
    return "data:image/png;base64," + base64.b64encode(LOGO.read_bytes()).decode()


def renderizar_html(ano: int, mes: int, hoje: date | None = None) -> tuple[str, dict]:
    """HTML completo e autossuficiente (logo embutido, links absolutos)."""
    d = ra.montar(ano, mes, hoje)
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]))
    html = env.get_template("regioes.html").render(
        d=d, logo_src=logo_data_uri(),
        url_mes_anterior=f"{ra.URL_PUBLICA}?mes={d['mes_anterior']}",
        url_mes_seguinte=f"{ra.URL_PUBLICA}?mes={d['mes_seguinte']}",
        url_portal=ra.URL_PORTAL)
    return html, d


def gerar_pdf(html: str, caminho: Path) -> Path | None:
    """PDF A4 pelo Chromium do Playwright (já instalado pra Stokki). Falha
    não derruba a rotina: o HTML e o link seguem."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            navegador = p.chromium.launch()
            try:
                pagina = navegador.new_page(viewport={"width": 1000, "height": 1200})
                pagina.set_content(html, wait_until="load")
                pagina.emulate_media(media="print")
                pagina.pdf(path=str(caminho), format="A4", print_background=True,
                           margin={"top": "8mm", "bottom": "8mm", "left": "8mm", "right": "8mm"})
            finally:
                navegador.close()
        return caminho
    except Exception as e:
        logger.warning(f"PDF nao gerado ({e}); segue so com o HTML.")
        return None


def corpo_email(d: dict, link: str, tem_pdf: bool) -> str:
    from email_utils import COR_PRIMARIA, COR_TEXTO, COR_TEXTO_SUAVE, envelope_html
    import html as _h
    notas = "".join(f"<li style='margin:0 0 6px'>{_h.escape(n)}</li>" for n in d["notas"]) \
        or "<li>Nenhum feriado em dia útil neste mês.</li>"
    quinzenais = "".join(
        f"<li style='margin:0 0 6px'><strong>{_h.escape(r['nome'])}</strong>: "
        f"{', '.join(x.strftime('%d/%m') for x in r['datas'])}</li>"
        for r in d["regioes"] if r["quinzenal"])
    anexos = "PDF e HTML em anexo" if tem_pdf else "HTML em anexo (o PDF não pôde ser gerado)"
    conteudo = f"""
    <p style="margin:0 0 4px;font-size:20px;font-weight:700;color:{COR_PRIMARIA};">Dias de entrega: {_h.escape(d['titulo_mes'])}</p>
    <p style="margin:0 0 16px;color:{COR_TEXTO_SUAVE};">Informativo mensal gerado da configuração de regiões, pronto pra encaminhar aos embarcadores ({anexos}).</p>
    <p style="margin:0 0 16px;color:{COR_TEXTO};">Página pública, sempre atualizada: <a href="{link}">{link}</a></p>
    <p style="margin:0 0 6px;font-weight:700;color:{COR_PRIMARIA};">Feriados e visitas transferidas</p>
    <ul style="margin:0 0 16px;padding-left:18px;color:{COR_TEXTO};">{notas}</ul>
    <p style="margin:0 0 6px;font-weight:700;color:{COR_PRIMARIA};">Regiões quinzenais no mês</p>
    <ul style="margin:0 0 16px;padding-left:18px;color:{COR_TEXTO};">{quinzenais}</ul>
    """
    return envelope_html(conteudo)


def destinos(config: dict, modo_teste: bool) -> list[str]:
    secao = config.get("informativo_mensal") or {}
    if modo_teste:
        return [EMAIL_TESTE]
    forcar = str(secao.get("forcar_destino", EMAIL_TESTE) or "").strip()
    if forcar:
        return [forcar]
    return [e for e in (secao.get("destinatarios") or []) if e] or [EMAIL_TESTE]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Informativo mensal de dias de entrega.")
    parser.add_argument("--mes", help="AAAA-MM (padrão: mês atual; a partir do dia 25, o seguinte)")
    parser.add_argument("--proximo", action="store_true", help="mês seguinte ao atual")
    parser.add_argument("--modo-teste", action="store_true", help="gera os arquivos, não envia e-mail nem WhatsApp")
    parser.add_argument("--sem-pdf", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    inicio = time.time()

    hoje = date.today()
    if args.mes:
        ano, mes = (int(p) for p in args.mes.split("-", 1))
    else:
        ano, mes = mes_alvo(hoje, args.proximo)
    html, d = renderizar_html(ano, mes, hoje)
    PASTA_SAIDA.mkdir(parents=True, exist_ok=True)
    base = PASTA_SAIDA / f"informativo_regioes_{ano:04d}-{mes:02d}"
    caminho_html = base.with_suffix(".html")
    caminho_html.write_text(html, encoding="utf-8")
    caminho_pdf = None if args.sem_pdf else gerar_pdf(html, base.with_suffix(".pdf"))
    logger.info(f"{d['titulo_mes']}: {caminho_html.name}" + (f" + {caminho_pdf.name}" if caminho_pdf else ""))
    for nota in d["notas"]:
        logger.info(f"  {nota}")

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    link = ra.url_do_mes(ano, mes)
    anexos = ([(caminho_pdf, caminho_pdf.name)] if caminho_pdf else []) + [(caminho_html, caminho_html.name)]
    para = destinos(config, args.modo_teste)
    assunto = f"Informativo de dias de entrega: {d['titulo_mes']}"
    if args.modo_teste:
        logger.info(f"[MODO TESTE] e-mail nao enviado (iria para {para}); WhatsApp nao enviado.")
    else:
        from email_utils import enviar_email
        ok = enviar_email(para, assunto, corpo_email(d, link, caminho_pdf is not None), config.get("email", {}),
                          anexos=anexos)
        logger.info(f"E-mail {'enviado' if ok else 'FALHOU'} para {para}.")
    import notificar_whatsapp
    feriados = [f["data"] for f in ra.feriados_uteis_do_mes(ano, mes)]
    situacao = notificar_whatsapp.avisar_informativo_mensal(d["titulo_mes"], feriados, link, config,
                                                             modo_teste=args.modo_teste)
    logger.info(f"WhatsApp: {situacao}. Concluido em {time.time() - inicio:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
