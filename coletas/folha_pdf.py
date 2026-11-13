# -*- coding: utf-8 -*-
"""
coletas/folha_pdf.py

PDF da folha de coleta (COL-n): uma pagina A4 em pe pra imprimir na doca. O
motorista marca o que levou, preenche nome, documento, placa e assina; a foto
dessa folha e obrigatoria pra confirmar a coleta (Hugo, 12/11).

Desenhada com PIL, como a capa do romaneio (roteirizacao/gerar_pdf_romaneios).
Cabem 30 pedidos (o maximo de uma folha).
"""
import io
import re
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

A4 = (1240, 1754)   # 150 dpi
MARGEM = 60
PRETO, CINZA, LINHA = (20, 20, 20), (95, 95, 95), (170, 170, 170)
Y_LINHAS = 270          # onde comeca a 1a linha de pedido
ALT_LINHA = 34
Y_RODAPE = A4[1] - 390  # declaracao + campos do motorista
MAX_LINHAS = 30


def _fonte(tamanho_px: int, negrito: bool = False):
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


def _truncar(draw, texto, fonte, largura) -> str:
    texto = str(texto or "")
    if draw.textlength(texto, font=fonte) <= largura:
        return texto
    while texto and draw.textlength(texto + "…", font=fonte) > largura:
        texto = texto[:-1]
    return texto + "…"


def _data_br(texto) -> str:
    try:
        return datetime.strptime(texto, "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return texto or ""


LADO_QR = 150


def _qr(conteudo: str):
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=1)
    qr.add_data(conteudo)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((LADO_QR, LADO_QR))


def gerar(coleta: dict, url: str | None = None) -> bytes:
    """coleta = coletas.regras.coleta(conn, id). url = endereco da coleta no
    painel: vira o QR code do canto, que o celular da doca le pra abrir direto
    a confirmacao (foto, nome e placa). Devolve o PDF (1 pagina)."""
    img = Image.new("RGB", A4, "white")
    d = ImageDraw.Draw(img)
    largura = A4[0] - 2 * MARGEM
    direita = A4[0] - MARGEM
    pedidos = coleta.get("pedidos") or []

    d.text((MARGEM, 50), "Fresh Log", font=_fonte(26, True), fill=CINZA)
    d.text((MARGEM, 84), f"Folha de coleta {coleta['numero']}", font=_fonte(46, True), fill=PRETO)
    largura_topo = largura
    if url:
        img.paste(_qr(url), (direita - LADO_QR, 36))
        d.text((direita - LADO_QR / 2, 36 + LADO_QR + 4), "Confirmar pelo celular", font=_fonte(15), fill=CINZA, anchor="ma")
        largura_topo = largura - LADO_QR - 24
    y = 152
    transportadora = coleta.get("transportadora") or ""
    if coleta.get("embarcador"):   # Cliente Retira: o CNPJ colado no nome so rouba o espaco do embarcador
        transportadora = re.sub(r"\s*\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}", "", transportadora).strip()
        transportadora += f"  |  {coleta['embarcador']}"
    d.text((MARGEM, y), _truncar(d, f"Transportadora: {transportadora}", _fonte(28, True), largura_topo),
           font=_fonte(28, True), fill=PRETO)
    d.text((MARGEM, y + 38), f"Gerada por {coleta.get('criada_por') or ''} em {_data_br(coleta.get('criada_em'))}",
           font=_fonte(22), fill=CINZA)
    y += 84

    # tabela: Levou | Pedido | Embarcador | NF | Destinatario | Unid.
    colunas = [("Levou", 90), ("Pedido", 150), ("Embarcador", 320), ("NF", 120), ("Destinatário", 350), ("Unid.", 90)]
    f_cab, f_lin = _fonte(20, True), _fonte(21)
    d.rectangle([(MARGEM, y), (direita, y + 34)], fill=(230, 230, 230))
    x = MARGEM
    for titulo, w in colunas:
        d.text((x + 8, y + 6), titulo, font=f_cab, fill=PRETO)
        x += w
    y += 34
    assert y == Y_LINHAS
    alt = ALT_LINHA
    for p in pedidos:
        d.line([(MARGEM, y + alt), (direita, y + alt)], fill=LINHA, width=1)
        d.rectangle([(MARGEM + 28, y + 5), (MARGEM + 52, y + 29)], outline=PRETO, width=2)
        embarcador = re.sub(r"\s*\.\.\.\s*#stkkc-\d+\s*$", "", p.get("embarcador") or "")
        valores = [None, p.get("codigo"), embarcador, p.get("nf"), p.get("destinatario"),
                   "" if p.get("unidades") is None else p.get("unidades")]
        x = MARGEM
        for (titulo, w), valor in zip(colunas, valores):
            if valor is not None:
                d.text((x + 8, y + 6), _truncar(d, valor, f_lin, w - 14), font=f_lin, fill=PRETO)
            x += w
        y += alt
    total_un = sum(p.get("unidades") or 0 for p in pedidos)
    y += 12
    d.text((MARGEM, y), f"Total: {len(pedidos)} pedido(s)" + (f", {total_un} unidade(s)" if total_un else ""),
           font=_fonte(24, True), fill=PRETO)

    # rodape fixo: declaracao e campos do motorista
    y = Y_RODAPE
    d.line([(MARGEM, y), (direita, y)], fill=PRETO, width=2)
    d.text((MARGEM, y + 16), "Recebi os pedidos marcados acima.", font=_fonte(26, True), fill=PRETO)
    y += 86
    f_campo = _fonte(22)
    meio = MARGEM + largura // 2 + 20
    for (rot_a, rot_b) in (("Nome completo", None), ("CPF ou RG", "Placa"), ("Data e hora", None)):
        d.text((MARGEM, y), rot_a + ":", font=f_campo, fill=PRETO)
        fim_a = (meio - 40) if rot_b else direita
        d.line([(MARGEM + 190, y + 28), (fim_a, y + 28)], fill=PRETO, width=1)
        if rot_b:
            d.text((meio, y), rot_b + ":", font=f_campo, fill=PRETO)
            d.line([(meio + 90, y + 28), (direita, y + 28)], fill=PRETO, width=1)
        y += 62
    y += 30
    d.line([(MARGEM + 190, y + 28), (direita, y + 28)], fill=PRETO, width=1)
    d.text((MARGEM, y), "Assinatura:", font=f_campo, fill=PRETO)

    saida = io.BytesIO()
    img.save(saida, format="PDF", resolution=150.0)
    return saida.getvalue()
