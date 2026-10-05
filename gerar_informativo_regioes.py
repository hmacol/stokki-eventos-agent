# -*- coding: utf-8 -*-
"""
gerar_informativo_regioes.py

Informativo aos embarcadores (Hugo, 03/10/2026 -- spec dias fixos v2,
seção 7): regiões, cidades, dias de visita, frequência e prazo, gerado da
configuração de roteirizacao/regioes_dia_fixo.py (pra não desatualizar).
PDF desenhado com Pillow, mesmo padrão de portal_cliente/cotacao_pdf.py
(reportlab não está no projeto). Nada é enviado: o envio é do Hugo.

    py -3.11 gerar_informativo_regioes.py
        -> dados/informativos/informativo_regioes_AAAA-MM-DD.pdf
    py -3.11 gerar_informativo_regioes.py --saida caminho.pdf
"""
import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from PIL import Image, ImageDraw

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_RAIZ = Path(__file__).parent
for _p in (_RAIZ, _RAIZ / "roteirizacao", _RAIZ / "portal_cliente"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import regioes_dia_fixo as rdf  # noqa: E402
from cotacao_pdf import (  # noqa: E402
    A4, CINZA_LINHA, CINZA_TXT, CINZA_ZEBRA, DPI, MARGEM, NAVY, PRETO, TEAL, _fonte, _logo, _quebrar,
)

PASTA_SAIDA = _RAIZ / "dados" / "informativos"
CONTATO = "entregas@freshlogbr.com"

# Acento e preposição pros nomes de REGIOES (gravados sem acento, em maiúsculas).
_PALAVRAS = {"SAO": "São", "JOSE": "José", "JACAREI": "Jacareí", "CUBATAO": "Cubatão", "GUARUJA": "Guarujá",
             "JUNDIAI": "Jundiaí", "CABREUVA": "Cabreúva", "HORTOLANDIA": "Hortolândia", "SUMARE": "Sumaré",
             "PARNAIBA": "Parnaíba", "ANDRE": "André", "MAUA": "Mauá", "RIBEIRAO": "Ribeirão"}
_MINUSCULAS = {"DA", "DAS", "DE", "DO", "DOS"}

def _texto_prazo_entrega() -> str:
    """Monta o parágrafo de prazos a partir de PRAZO_POR_NIVEL (em vez de
    valores fixos no texto), pra não desatualizar se o prazo mudar."""
    def fmt(nivel):
        dias, uteis = rdf.PRAZO_POR_NIVEL[nivel]
        return f"{dias} dias {'úteis' if uteis else 'corridos'}"
    return (f"Até {fmt(rdf.NIVEL_INTERNA)} na Grande São Paulo e nas regiões internas; "
            f"até {fmt(rdf.NIVEL_SEMANAL)} nas regiões de visita semanal; "
            f"até {fmt(rdf.NIVEL_QUINZENAL)} na região de visita quinzenal.")


def _textos() -> list[tuple[str, str]]:
    return [
        ("Como funciona",
         "Fora da Grande São Paulo, cada região é visitada em dias fixos da semana. Pedido sem data de entrega é "
         "agendado automaticamente para o próximo dia de visita da região."),
        ("Data escolhida pelo embarcador",
         "Se o pedido chega com data de entrega fora dos dias de visita da região, a data é respeitada e o pedido "
         "é tratado como envio dedicado, com valor calculado pela tabela de frete dedicado da Fresh Log. Você "
         "recebe um aviso por e-mail (e por WhatsApp, se cadastrado no portal) e pode pedir outra data dentro dos "
         "dias de visita. No envio pelo portal, o aviso aparece antes de confirmar."),
        ("Prazo de entrega", _texto_prazo_entrega()),
        ("Contato", f"Dúvidas e pedidos de data: {CONTATO} ou pelo chat do portal do cliente."),
    ]


def nome_bonito(cidade: str) -> str:
    palavras = []
    for p in str(cidade or "").split():
        if p in _MINUSCULAS and palavras:
            palavras.append(p.lower())
        else:
            palavras.append(_PALAVRAS.get(p, p.capitalize()))
    return " ".join(palavras)


def _frequencia(cfg: dict) -> str:
    if cfg.get("frequencia") == rdf.FREQUENCIA_QUINZENAL:
        return "Quinzenal"
    n = len(cfg["dias"])
    return "Semanal" if n == 1 else f"{n}x por semana"


def _prazo(cfg: dict, externa: bool) -> str:
    nivel = rdf.nivel_da_regra({"frequencia": cfg.get("frequencia", rdf.FREQUENCIA_SEMANAL), "externa": externa})
    dias, uteis = rdf.PRAZO_POR_NIVEL[nivel]
    return f"até {dias} dias {'úteis' if uteis else 'corridos'}"


def _dias(cfg: dict) -> str:
    texto = rdf.nomes_dias(cfg["dias"])
    if cfg.get("frequencia") == rdf.FREQUENCIA_QUINZENAL and cfg.get("ancora"):
        texto += f" (a cada 15 dias, a partir de {date.fromisoformat(cfg['ancora']):%d/%m/%Y})"
    return texto


def linhas_informativo() -> list[dict]:
    linhas = []
    for r in rdf.REGIOES:
        linhas.append({"regiao": r["nome"], "cidades": ", ".join(nome_bonito(c) for c in r["cidades"]),
                       "dias": _dias(r), "frequencia": _frequencia(r), "prazo": _prazo(r, r.get("externa", False))})
    for e in rdf.ENDERECOS_DIA_FIXO:
        linhas.append({"regiao": e["nome"], "cidades": f"Galpão em {nome_bonito(e['cidade'])}",
                       "dias": _dias(e), "frequencia": _frequencia(e), "prazo": _prazo(e, False)})
    return linhas


# Colunas da tabela: (chave, título, largura em px a 150 dpi).
_COLUNAS = [("regiao", "Região", 200), ("cidades", "Cidades", 380), ("dias", "Dias de visita", 220),
            ("frequencia", "Frequência", 120), ("prazo", "Prazo", 120)]


def _nova_pagina() -> tuple[Image.Image, ImageDraw.ImageDraw, int]:
    img = Image.new("RGB", A4, "white")
    draw = ImageDraw.Draw(img)
    logo = _logo()
    if logo is not None:
        img.paste(logo, (MARGEM, 78), logo)
    draw.text((img.width - MARGEM, 105), "REGIÕES E DIAS DE VISITA", font=_fonte(40, True), fill=NAVY, anchor="rm")
    draw.text((img.width - MARGEM, 160), f"Informativo aos embarcadores · {date.today():%d/%m/%Y}",
              font=_fonte(22), fill=CINZA_TXT, anchor="rm")
    draw.rectangle([(MARGEM, 205), (img.width - MARGEM, 212)], fill=TEAL)
    return img, draw, 250


def gerar_pdf(linhas: list[dict], caminho: Path) -> Path:
    paginas = []
    img, draw, y = _nova_pagina()
    fonte, negrito = _fonte(19), _fonte(19, True)
    altura_util = A4[1] - 140

    def cabecalho_tabela(draw, y):
        x = MARGEM
        draw.rectangle([(MARGEM, y), (A4[0] - MARGEM, y + 34)], fill=NAVY)
        for _chave, titulo, largura in _COLUNAS:
            draw.text((x + 8, y + 7), titulo, font=negrito, fill="white")
            x += largura
        return y + 40

    y = cabecalho_tabela(draw, y)
    for i, linha in enumerate(linhas):
        quebras = {chave: _quebrar(draw, linha[chave], fonte, largura - 16) for chave, _t, largura in _COLUNAS}
        altura = 26 * max(len(v) for v in quebras.values()) + 12
        if y + altura > altura_util:
            paginas.append(img)
            img, draw, y = _nova_pagina()
            y = cabecalho_tabela(draw, y)
        if i % 2:
            draw.rectangle([(MARGEM, y), (A4[0] - MARGEM, y + altura)], fill=CINZA_ZEBRA)
        x = MARGEM
        for chave, _t, largura in _COLUNAS:
            for n, texto in enumerate(quebras[chave]):
                draw.text((x + 8, y + 6 + 26 * n), texto, font=negrito if chave == "regiao" else fonte, fill=PRETO)
            x += largura
        y += altura
        draw.line([(MARGEM, y), (A4[0] - MARGEM, y)], fill=CINZA_LINHA, width=1)

    y += 30
    for titulo, texto in _textos():
        corpo = _quebrar(draw, texto, _fonte(21), A4[0] - 2 * MARGEM)
        if y + 40 + 30 * len(corpo) > altura_util:
            paginas.append(img)
            img, draw, y = _nova_pagina()
        draw.text((MARGEM, y), titulo, font=_fonte(24, True), fill=NAVY)
        y += 38
        for t in corpo:
            draw.text((MARGEM, y), t, font=_fonte(21), fill=PRETO)
            y += 30
        y += 16
    paginas.append(img)

    for n, pg in enumerate(paginas, start=1):
        ImageDraw.Draw(pg).text((A4[0] // 2, A4[1] - 60),
                                f"Fresh Log · gerado em {datetime.now():%d/%m/%Y %H:%M} · página {n}/{len(paginas)}",
                                font=_fonte(18), fill=CINZA_TXT, anchor="mm")
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    paginas[0].save(caminho, format="PDF", save_all=True, append_images=paginas[1:], resolution=DPI)
    return caminho


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Gera o informativo de regiões e dias de visita (PDF)")
    parser.add_argument("--saida", default=None, help="caminho do PDF (padrão: dados/informativos/)")
    args = parser.parse_args(argv)
    destino = Path(args.saida) if args.saida else PASTA_SAIDA / f"informativo_regioes_{date.today():%Y-%m-%d}.pdf"
    print(f"Informativo gravado em {gerar_pdf(linhas_informativo(), destino)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
