# -*- coding: utf-8 -*-
"""
coletas/folha_pdf.py

PDF da folha de coleta (COL-n): uma pagina A4 em pe pra imprimir na doca. O
motorista marca o que levou, preenche nome, documento, placa e assina; a foto
dessa folha e obrigatoria pra confirmar a coleta (Hugo, 12/11). O QR code do
canto abre a coleta no celular da doca (13/11).

Desenhada com PIL, como a capa do romaneio (roteirizacao/gerar_pdf_romaneios),
com a mesma identidade: logo Freshlog, azul-marinho e verde-agua (o Hugo
escolheu esta entre 3 opcoes, 13/11). Cabem 30 pedidos (o maximo de uma folha).
"""
import io
import re
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

A4 = (1240, 1754)   # 150 dpi
MARGEM = 60
LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "logo_freshlog.png"
# Identidade visual (as mesmas cores do romaneio, tiradas do logo)
NAVY, TEAL = (23, 29, 51), (34, 220, 160)
PRETO, CINZA, LINHA, ZEBRA = (20, 20, 20), (95, 102, 115), (190, 196, 205), (243, 246, 249)
Y_CAB_TABELA = 286      # faixa de titulos da tabela
Y_LINHAS = 320          # onde comeca a 1a linha de pedido
ALT_LINHA = 32
Y_RODAPE = A4[1] - 390  # declaracao + campos do motorista
MAX_LINHAS = 30
LADO_QR = 150


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


def _qr(conteudo: str):
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=1)
    qr.add_data(conteudo)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((LADO_QR, LADO_QR))


def _logo(altura: int):
    """Logo na altura pedida, ou None se o arquivo nao estiver la (a folha sai com o nome escrito)."""
    try:
        logo = Image.open(LOGO_PATH).convert("RGBA")
    except OSError:
        return None
    return logo.resize((round(logo.width * altura / logo.height), altura), Image.LANCZOS)


def _transportadora(coleta: dict) -> str:
    texto = coleta.get("transportadora") or ""
    if coleta.get("embarcador"):   # Cliente Retira: o CNPJ colado no nome so rouba o espaco do embarcador
        texto = re.sub(r"\s*\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}", "", texto).strip()
        texto += f"  |  {coleta['embarcador']}"
    return texto


def gerar(coleta: dict, url: str | None = None) -> bytes:
    """coleta = coletas.regras.coleta(conn, id). url = endereco da coleta no
    painel: vira o QR code do canto, que o celular da doca le pra abrir direto
    a confirmacao (foto, nome e placa). Devolve o PDF (1 pagina)."""
    img = Image.new("RGB", A4, "white")
    d = ImageDraw.Draw(img)
    largura = A4[0] - 2 * MARGEM
    direita = A4[0] - MARGEM
    pedidos = coleta.get("pedidos") or []

    # cabecalho: logo, titulo, transportadora, QR e o filete verde-agua
    logo = _logo(72)
    if logo is None:
        d.text((MARGEM, 40), "Fresh Log", font=_fonte(34, True), fill=NAVY)
    else:
        img.paste(logo, (MARGEM, 30), logo)
    largura_txt = largura - (LADO_QR + 24 if url else 0)
    d.text((MARGEM, 120), f"Folha de coleta {coleta['numero']}", font=_fonte(40, True), fill=NAVY)
    d.text((MARGEM, 176), _truncar(d, "Transportadora: " + _transportadora(coleta), _fonte(26, True), largura_txt),
           font=_fonte(26, True), fill=NAVY)
    d.text((MARGEM, 214), f"Gerada por {coleta.get('criada_por') or ''} em {_data_br(coleta.get('criada_em'))}",
           font=_fonte(20), fill=CINZA)
    d.rectangle([(MARGEM, 264), (direita, 270)], fill=TEAL)
    if url:
        img.paste(_qr(url), (direita - LADO_QR, 36))
        d.text((direita - LADO_QR / 2, 36 + LADO_QR + 4), "Confirmar pelo celular", font=_fonte(15), fill=CINZA,
               anchor="ma")

    # tabela: Levou | Pedido | Embarcador | NF | Destinatario | Unid.
    colunas = [("Levou", 90), ("Pedido", 150), ("Embarcador", 320), ("NF", 120), ("Destinatário", 350), ("Unid.", 90)]
    f_cab, f_lin = _fonte(20, True), _fonte(21)
    y = Y_CAB_TABELA
    d.rectangle([(MARGEM, y), (direita, y + 34)], fill=NAVY)
    x = MARGEM
    for titulo, w in colunas:
        d.text((x + 8, y + 6), titulo, font=f_cab, fill="white")
        x += w
    y = Y_LINHAS
    alt = ALT_LINHA
    for i, p in enumerate(pedidos):
        if i % 2:
            d.rectangle([(MARGEM, y), (direita, y + alt)], fill=ZEBRA)
        d.line([(MARGEM, y + alt), (direita, y + alt)], fill=LINHA, width=1)
        d.rectangle([(MARGEM + 28, y + 4), (MARGEM + 52, y + 28)], outline=NAVY, width=2, fill="white")
        embarcador = re.sub(r"\s*\.\.\.\s*#stkkc-\d+\s*$", "", p.get("embarcador") or "")
        valores = [None, p.get("codigo"), embarcador, p.get("nf"), p.get("destinatario"),
                   "" if p.get("unidades") is None else p.get("unidades")]
        x = MARGEM
        for (titulo, w), valor in zip(colunas, valores):
            if valor is not None:
                d.text((x + 8, y + 5), _truncar(d, valor, f_lin, w - 14), font=f_lin, fill=PRETO)
            x += w
        y += alt
    total_un = sum(p.get("unidades") or 0 for p in pedidos)
    y += 12
    d.text((MARGEM, y), f"Total: {len(pedidos)} pedido(s)" + (f", {total_un} unidade(s)" if total_un else ""),
           font=_fonte(24, True), fill=NAVY)

    # rodape fixo: declaracao e campos do motorista (fundo branco: e o que a foto precisa mostrar)
    y = Y_RODAPE
    d.line([(MARGEM, y), (direita, y)], fill=NAVY, width=4)
    d.text((MARGEM, y + 16), "Recebi os pedidos marcados acima.", font=_fonte(26, True), fill=NAVY)
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
