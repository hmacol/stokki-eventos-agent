# -*- coding: utf-8 -*-
"""
portal_cliente/cotacao_pdf.py

PDF da proposta de frete dedicado (portal_cliente/cotacao.py), no mesmo
padrão dos romaneios: página A4 desenhada com Pillow a 150 dpi e salva
como PDF (sem reportlab -- ver roteirizacao/gerar_pdf_romaneios.py).
Uma página, mais uma de continuação quando a lista de entregas não cabe.

    gerar(cotacao: dict, regras: dict) -> bytes
"""
import io
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_AQUI = Path(__file__).parent
if str(_AQUI) not in sys.path:
    sys.path.insert(0, str(_AQUI))

import cotacao  # noqa: E402  (regras da composição do valor; sem ciclo -- cotacao só importa este módulo lá dentro de gerar_pdf)

_RAIZ = Path(__file__).parent.parent
LOGO_PATH = _RAIZ / "assets" / "logo_freshlog.png"

A4 = (1240, 1754)
DPI = 150.0
MARGEM = 100
NAVY = (23, 29, 51)
TEAL = (34, 220, 160)
CINZA_ZEBRA = (243, 246, 249)
CINZA_LINHA = (215, 221, 229)
CINZA_TXT = (108, 115, 130)
PRETO = (31, 41, 55)


def _fonte(tamanho_px: int, negrito: bool = False) -> ImageFont.FreeTypeFont:
    candidatos = (["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"] if negrito
                  else ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf",
                        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"])
    for caminho in candidatos:
        try:
            return ImageFont.truetype(caminho, tamanho_px)
        except OSError:
            continue
    return ImageFont.load_default()


_LOGO: list = []


def _logo() -> Image.Image | None:
    if not _LOGO:
        try:
            _LOGO.append(Image.open(LOGO_PATH).convert("RGBA"))
        except Exception:
            _LOGO.append(None)
    return _LOGO[0]


def _brl(v) -> str:
    if v is None:
        return "—"
    s = f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def _km(v) -> str:
    return f"{float(v or 0):.1f} km".replace(".", ",")


def _data(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return iso


def _quebrar(draw: ImageDraw.ImageDraw, texto: str, fonte, largura: int) -> list[str]:
    """Quebra por palavras pelo comprimento renderizado."""
    linhas, atual = [], ""
    for palavra in str(texto or "").split():
        tent = f"{atual} {palavra}".strip()
        if draw.textlength(tent, font=fonte) <= largura:
            atual = tent
        else:
            if atual:
                linhas.append(atual)
            atual = palavra
    if atual:
        linhas.append(atual)
    return linhas or [""]


def _nova_pagina() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", A4, "white")
    return img, ImageDraw.Draw(img)


def _cabecalho(img, draw, titulo: str, subtitulo: str) -> int:
    logo = _logo()
    if logo is not None:
        img.paste(logo, (MARGEM, 78), logo)
    draw.text((img.width - MARGEM, 105), titulo, font=_fonte(44, True), fill=NAVY, anchor="rm")
    draw.text((img.width - MARGEM, 160), subtitulo, font=_fonte(24), fill=CINZA_TXT, anchor="rm")
    draw.rectangle([(MARGEM, 205), (img.width - MARGEM, 212)], fill=TEAL)
    return 250


def _rodape(img, draw, pagina: int, total: int):
    draw.text((img.width // 2, img.height - 60),
              f"Fresh Log · proposta gerada pelo portal do cliente em {datetime.now().strftime('%d/%m/%Y %H:%M')} · página {pagina}/{total}",
              font=_fonte(18), fill=CINZA_TXT, anchor="mm")


def _rotulo_valor(draw, x: int, y: int, rotulo: str, valor: str) -> None:
    draw.text((x, y), rotulo.upper(), font=_fonte(16, True), fill=CINZA_TXT)
    draw.text((x, y + 24), valor, font=_fonte(24), fill=PRETO)


def gerar(cot: dict, regras: dict) -> bytes:
    r = cot["resultado"]
    e = cot["entrada"]
    paradas = e.get("paradas") or []
    paginas: list[Image.Image] = []

    img, draw = _nova_pagina()
    y = _cabecalho(img, draw, "PROPOSTA DE FRETE DEDICADO", f"{cot['numero']} · válida até {_data(cot.get('valido_ate'))}")

    # Bloco do cliente / resumo
    _rotulo_valor(draw, MARGEM, y, "Cliente", str(cot.get("nome_cliente") or ""))
    _rotulo_valor(draw, MARGEM + 560, y, "CNPJ", str(cot.get("cnpj_embarcador") or ""))
    y += 76
    _rotulo_valor(draw, MARGEM, y, "Veículo", f"{r['veiculo_nome']} (até {r['capacidade']['caixas_max']} cx / {r['capacidade']['peso_max_kg']:.0f} kg)")
    _rotulo_valor(draw, MARGEM + 560, y, "Carga", f"{r['caixas']} caixas · {r['peso_kg']:.0f} kg · {r['tipo_carga_rotulo']}"
                                                  + (" · SAME-DAY" if r.get("urgente") else ""))
    y += 76
    _rotulo_valor(draw, MARGEM, y, "Origem", str(r.get("origem_endereco") or regras.get("origem_endereco") or ""))
    rot_km = "Distância (ida e volta)" if r.get("km_volta") else "Distância (ida)"
    _rotulo_valor(draw, MARGEM + 560, y, rot_km,
                  f"{_km(r['km_total'])} · franquia {r['franquia_km']:.0f} km · {r['km_excedente']} km adicionais")
    y += 90

    # Entregas: o km ao lado de cada uma é a perna ATÉ ela; a volta ao galpão
    # sai como última linha, pra que as pernas somem o total da proposta.
    draw.text((MARGEM, y), "ENTREGAS", font=_fonte(20, True), fill=NAVY)
    y += 34
    fonte_l = _fonte(20)
    fonte_n = _fonte(20, True)
    largura_end = img.width - 2 * MARGEM - 60 - 160
    for p in paradas:
        linhas = _quebrar(draw, p["endereco"] + (f" ({p['complemento']})" if p.get("complemento") else ""), fonte_l, largura_end)
        if p.get("referencia"):
            linhas.append(f"Ref.: {p['referencia']}")
        alt = 30 * len(linhas) + 12
        if y + alt > img.height - 620:
            # continua na próxima página (só lista); composição fica na última
            paginas.append(img)
            img, draw = _nova_pagina()
            y = _cabecalho(img, draw, "PROPOSTA DE FRETE DEDICADO", f"{cot['numero']} · entregas (continuação)")
        draw.rectangle([(MARGEM, y - 4), (img.width - MARGEM, y + alt - 4)], fill=CINZA_ZEBRA if p["ordem"] % 2 else "white")
        draw.text((MARGEM + 12, y + 4), f"{p['ordem']}.", font=fonte_n, fill=NAVY)
        for i, l in enumerate(linhas):
            draw.text((MARGEM + 60, y + 4 + 30 * i), l, font=fonte_l, fill=PRETO if i == 0 or not l.startswith("Ref.") else CINZA_TXT)
        if p.get("km_perna") is not None:
            draw.text((img.width - MARGEM - 12, y + 4), _km(p["km_perna"]), font=fonte_l, fill=CINZA_TXT, anchor="ra")
        y += alt
    if r.get("km_volta"):
        if y + 42 > img.height - 620:
            paginas.append(img)
            img, draw = _nova_pagina()
            y = _cabecalho(img, draw, "PROPOSTA DE FRETE DEDICADO", f"{cot['numero']} · entregas (continuação)")
        draw.rectangle([(MARGEM, y - 4), (img.width - MARGEM, y + 38)], fill=CINZA_ZEBRA if len(paradas) % 2 else "white")
        draw.text((MARGEM + 60, y + 4), "Retorno ao galpão Fresh Log", font=fonte_l, fill=CINZA_TXT)
        draw.text((img.width - MARGEM - 12, y + 4), _km(r["km_volta"]), font=fonte_l, fill=CINZA_TXT, anchor="ra")
        y += 42
    y += 30

    # Composição
    if y > img.height - 600:
        paginas.append(img)
        img, draw = _nova_pagina()
        y = _cabecalho(img, draw, "PROPOSTA DE FRETE DEDICADO", f"{cot['numero']} · composição do valor")
    draw.text((MARGEM, y), "COMPOSIÇÃO DO VALOR", font=_fonte(20, True), fill=NAVY)
    y += 36
    # Frete base, desconto de carga seca, km adicional e pedágio vão numa
    # linha só, já somada (Hugo, 11/09) -- cotacao.linhas_composicao().
    linhas = cotacao.linhas_composicao(r)
    fonte_c = _fonte(22)
    for i, (k, v) in enumerate(linhas):
        draw.rectangle([(MARGEM, y - 6), (img.width - MARGEM, y + 34)], fill=CINZA_ZEBRA if i % 2 == 0 else "white")
        draw.text((MARGEM + 12, y), k, font=fonte_c, fill=PRETO)
        draw.text((img.width - MARGEM - 12, y), v, font=fonte_c, fill=PRETO, anchor="ra")
        y += 40
    y += 10
    draw.rectangle([(MARGEM, y), (img.width - MARGEM, y + 78)], fill=NAVY)
    draw.text((MARGEM + 20, y + 39), "TOTAL DA PROPOSTA", font=_fonte(24, True), fill="white", anchor="lm")
    draw.text((img.width - MARGEM - 20, y + 39), _brl(r["total"]), font=_fonte(36, True), fill=TEAL, anchor="rm")
    y += 110

    # Condições
    draw.text((MARGEM, y), "CONDIÇÕES", font=_fonte(20, True), fill=NAVY)
    y += 32
    trajeto = ("distância rodoviária do galpão até as entregas, na ordem informada, mais o retorno ao galpão"
               if r.get("km_volta") else "distância rodoviária de ida, na ordem das entregas informada")
    ida_volta = "ida e volta" if r.get("km_volta") else "de ida"
    pct_av = f"{float(r.get('ad_valorem_pct') or 0.005) * 100:.1f}".replace(".", ",").replace(",0", "")
    condicoes = [
        f"Proposta válida até {_data(cot.get('valido_ate'))}. Aprovação pelo botão do e-mail ou pelo portal do cliente.",
        f"Veículo dedicado, saindo do galpão Fresh Log; {trajeto}.",
        f"A linha do frete já inclui a diária do veículo, o km adicional além da franquia de "
        f"{r['franquia_km']:.0f} km (por km inteiro, arredondado pra cima) e o pedágio do trajeto {ida_volta}.",
        "Pedágio repassado pelo valor real do trajeto; o valor incluso acima é a estimativa do Google Maps.",
        f"Ad valorem de {pct_av}% sobre o valor da(s) nota(s) fiscal(is) transportada(s).",
        f"Impostos de {r['impostos_pct'] * 100:.0f}% já inclusos no total.",
        "Carga acima da capacidade do veículo (caixas ou peso) altera o veículo e o valor.",
    ]
    if e.get("observacoes"):
        condicoes.append(f"Observações do cliente: {e['observacoes']}")
    fonte_o = _fonte(19)
    for c in condicoes:
        for l in _quebrar(draw, "• " + c, fonte_o, img.width - 2 * MARGEM):
            draw.text((MARGEM, y), l, font=fonte_o, fill=CINZA_TXT)
            y += 27
        y += 4
    if cot.get("status") == "ACEITA":
        y += 16
        draw.rectangle([(MARGEM, y), (img.width - MARGEM, y + 56)], outline=TEAL, width=3)
        draw.text((img.width // 2, y + 28), f"ACEITA em {cot.get('aceita_em') or ''} por {cot.get('aceita_por') or ''}",
                  font=_fonte(22, True), fill=NAVY, anchor="mm")
    paginas.append(img)

    for i, pg in enumerate(paginas, start=1):
        _rodape(pg, ImageDraw.Draw(pg), i, len(paginas))
    buf = io.BytesIO()
    paginas[0].save(buf, format="PDF", save_all=True, append_images=paginas[1:], resolution=DPI)
    return buf.getvalue()
