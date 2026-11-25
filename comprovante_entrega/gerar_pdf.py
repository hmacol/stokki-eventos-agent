# -*- coding: utf-8 -*-
"""
comprovante_entrega/gerar_pdf.py

Comprovante de entrega PROPRIO em PDF (pedido do Hugo, 17/11: nao depender
do PDF de checklist da Vuupt). Modelo "A - recibo" escolhido por ele: faixa
de status, grade de dados e a foto do canhoto grande.

Mesmo padrao dos romaneios e da cotacao: pagina A4 desenhada com Pillow a
150 dpi e salva como PDF (sem reportlab).

    gerar(dados, fotos=[(rotulo, bytes)], link="") -> bytes

`dados` e o dict de notificacao_entregas.montar_email_entrega.dados_do_servico
(tipo, codigo, sucesso, embarcador, destinatario, endereco, concluido_em, nf,
volumes, motivo, quem_retira, reentrega), mais os opcionais "motorista" e
"rota". Variacoes: entregue (com ou sem foto), nao entregue (com o motivo) e
retirado. A primeira foto sai na pagina 1; cada foto a mais, numa pagina.
"""
import io
import logging
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent.parent
LOGO_PATH = _RAIZ / "assets" / "logo_freshlog.png"

A4 = (1240, 1754)
DPI = 150.0
MARGEM = 90
NAVY = (23, 29, 51)
TEAL = (34, 220, 160)
VERDE = (13, 148, 108)
VERDE_FUNDO = (232, 250, 243)
VERMELHO = (190, 60, 40)
VERMELHO_FUNDO = (253, 240, 236)
CINZA_FUNDO = (243, 246, 249)
CINZA_LINHA = (215, 221, 229)
CINZA_TXT = (108, 115, 130)
PRETO = (31, 41, 55)
BRANCO = (255, 255, 255)

RODAPE = ("Freshlog · R. Zilda, 288 - Casa Verde Alta, São Paulo - SP · "
          "CNPJ 29.009.901/0001-26 · entregas@freshlogbr.com")
ALTURA_QR = 150


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


def _quebrar(d: ImageDraw.ImageDraw, texto: str, fonte, largura: int) -> list[str]:
    linhas, atual = [], ""
    for palavra in (texto or "").split():
        tentativa = f"{atual} {palavra}".strip()
        if d.textlength(tentativa, font=fonte) <= largura or not atual:
            atual = tentativa
        else:
            linhas.append(atual)
            atual = palavra
    return linhas + ([atual] if atual else [])


def _texto(d, xy, texto: str, fonte, cor=PRETO, largura: int | None = None, max_linhas: int = 0) -> int:
    """Desenha (com quebra, se `largura`) e devolve o y logo abaixo."""
    x, y = xy
    linhas = _quebrar(d, texto, fonte, largura) if largura else [texto]
    if max_linhas and len(linhas) > max_linhas:
        linhas = linhas[:max_linhas]
        linhas[-1] = linhas[-1].rstrip(" .,") + "…"
    for linha in linhas:
        d.text((x, y), linha, font=fonte, fill=cor)
        y += int(fonte.size * 1.3)
    return y


def _campo(d, x: int, y: int, rotulo: str, valor: str, largura: int, tam: int = 26, negrito: bool = True) -> int:
    y = _texto(d, (x, y), rotulo.upper(), _fonte(17, True), CINZA_TXT)
    return _texto(d, (x, y + 4), valor or "—", _fonte(tam, negrito), PRETO, largura, max_linhas=3)


def _colar_logo(img: Image.Image, x: int, y: int, altura: int) -> None:
    try:
        logo = Image.open(LOGO_PATH).convert("RGBA")
    except OSError:
        return
    logo = logo.resize((int(logo.width * altura / logo.height), altura), Image.Resampling.LANCZOS)
    img.paste(logo, (x, y), logo)


def _abrir_foto(conteudo: bytes) -> Image.Image | None:
    try:
        foto = Image.open(io.BytesIO(conteudo))
        return ImageOps.exif_transpose(foto).convert("RGB")
    except Exception as e:  # foto corrompida nao pode derrubar o comprovante
        logger.warning(f"Foto do comprovante ilegivel, ficou de fora: {e}")
        return None


def _colar_foto(img: Image.Image, foto: Image.Image, caixa: tuple[int, int, int, int]) -> None:
    """Encaixa a foto inteira (sem cortar) na caixa, centralizada."""
    x0, y0, x1, y1 = caixa
    escala = min((x1 - x0) / foto.width, (y1 - y0) / foto.height)
    foto = foto.resize((max(1, int(foto.width * escala)), max(1, int(foto.height * escala))), Image.Resampling.LANCZOS)
    px, py = x0 + (x1 - x0 - foto.width) // 2, y0 + (y1 - y0 - foto.height) // 2
    img.paste(foto, (px, py))
    ImageDraw.Draw(img).rectangle((px, py, px + foto.width - 1, py + foto.height - 1), outline=CINZA_LINHA, width=2)


def _colar_qr(img: Image.Image, link: str, x: int, y: int, lado: int) -> bool:
    try:
        import qrcode
        qr = qrcode.QRCode(border=0, box_size=10)
        qr.add_data(link)
        qr.make(fit=True)
        imagem = qr.make_image(fill_color=NAVY, back_color="white").convert("RGB")
    except Exception as e:
        logger.warning(f"QR do comprovante nao gerado: {e}")
        return False
    img.paste(imagem.resize((lado, lado), Image.Resampling.NEAREST), (x, y))
    return True


def _icone(d, cx: int, cy: int, r: int, cor, sucesso: bool) -> None:
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=cor)
    esp = max(4, r // 5)
    if sucesso:
        d.line([(cx - r * 0.45, cy), (cx - r * 0.1, cy + r * 0.35), (cx + r * 0.5, cy - r * 0.35)],
               fill=BRANCO, width=esp, joint="curve")
    else:
        k = r * 0.38
        d.line([(cx - k, cy - k), (cx + k, cy + k)], fill=BRANCO, width=esp)
        d.line([(cx - k, cy + k), (cx + k, cy - k)], fill=BRANCO, width=esp)


def _status(dados: dict, tem_foto: bool) -> dict:
    """Titulo do documento, palavra da faixa, frase e cores de cada variacao."""
    if dados.get("tipo") == "retirada":
        quem = (dados.get("quem_retira") or "").strip()
        return {"documento": "COMPROVANTE DE RETIRADA", "palavra": "RETIRADO", "sucesso": True,
                "frase": f"Retirado no galpão da Freshlog por {quem}." if quem else "Retirado no galpão da Freshlog.",
                "secao": "FOTO DA RETIRADA", "sem_foto": ""}
    if dados.get("sucesso"):
        return {"documento": "COMPROVANTE DE ENTREGA", "palavra": "ENTREGUE", "sucesso": True,
                "frase": ("Mercadoria recebida no destino, canhoto assinado." if tem_foto
                          else "Mercadoria recebida no destino."),
                "secao": "CANHOTO ASSINADO", "sem_foto": "A foto do canhoto não foi registrada nesta entrega."}
    return {"documento": "TENTATIVA DE ENTREGA", "palavra": "NÃO ENTREGUE", "sucesso": False,
            "frase": f"Motivo: {dados.get('motivo') or 'não informado'}",
            "secao": "FOTO DA OCORRÊNCIA", "sem_foto": "Nenhuma foto foi registrada nesta tentativa."}


def _rodape(img: Image.Image, d, link: str, emitido_em: datetime) -> int:
    """Desenha o rodape e devolve o y onde ele comeca (limite da area de cima)."""
    L, A = img.size
    emitido = f"Emitido em {emitido_em.strftime('%d/%m/%Y às %H:%M')}"
    d.line((MARGEM, A - 80, L - MARGEM, A - 80), fill=CINZA_LINHA, width=2)
    d.text((L // 2, A - 52), RODAPE, font=_fonte(16), fill=CINZA_TXT, anchor="mm")
    y = A - 105 - ALTURA_QR
    if link and _colar_qr(img, link, MARGEM, y, ALTURA_QR):
        x = MARGEM + ALTURA_QR + 30
        d.text((x, y + 6), "Confira este comprovante", font=_fonte(22, True), fill=NAVY)
        _texto(d, (x, y + 40), "Aponte a câmera para o código para abrir o registro original da entrega "
                               "no Portal do Cliente.", _fonte(19), PRETO, largura=L - MARGEM - x)
        d.text((x, y + 112), emitido, font=_fonte(19, True), fill=CINZA_TXT)
        return y - 30
    d.text((MARGEM, A - 122), emitido, font=_fonte(19, True), fill=CINZA_TXT)
    return A - 150


def _secao_foto(img: Image.Image, d, y: int, base: int, titulo: str, direita: str,
                foto: Image.Image | None, sem_foto: str) -> None:
    L = img.width
    d.rectangle((MARGEM, y, L - MARGEM, y + 44), fill=NAVY)
    d.text((MARGEM + 18, y + 22), titulo, font=_fonte(20, True), fill=BRANCO, anchor="lm")
    if direita:
        d.text((L - MARGEM - 18, y + 22), direita, font=_fonte(18), fill=BRANCO, anchor="rm")
    y += 44
    if foto is None:
        base = min(base, y + 150)
        d.rectangle((MARGEM, y, L - MARGEM, base), fill=CINZA_FUNDO, outline=CINZA_LINHA, width=2)
        d.text((L // 2, (y + base) // 2), sem_foto, font=_fonte(22), fill=CINZA_TXT, anchor="mm")
        return
    d.rectangle((MARGEM, y, L - MARGEM, base), fill=CINZA_FUNDO, outline=CINZA_LINHA, width=2)
    _colar_foto(img, foto, (MARGEM + 14, y + 14, L - MARGEM - 14, base - 14))


def _cabecalho(img: Image.Image, d, documento: str, subtitulo: str) -> None:
    L = img.width
    _colar_logo(img, MARGEM, 70, 96)
    d.text((L - MARGEM, 78), documento, font=_fonte(34, True), fill=NAVY, anchor="ra")
    d.text((L - MARGEM, 124), subtitulo, font=_fonte(24), fill=CINZA_TXT, anchor="ra")
    d.rectangle((MARGEM, 196, L - MARGEM, 201), fill=TEAL)


def _pagina_principal(dados: dict, st: dict, foto, rotulo_foto: str, n_fotos: int,
                      link: str, emitido_em: datetime) -> Image.Image:
    L = A4[0]
    img = Image.new("RGB", A4, BRANCO)
    d = ImageDraw.Draw(img)
    codigo = dados.get("codigo") or ""
    nf = (dados.get("nf") or "").strip()
    _cabecalho(img, d, st["documento"], f"Pedido {codigo}" + (f" · NF {nf}" if nf else ""))

    # faixa de status
    cor, fundo = (VERDE, VERDE_FUNDO) if st["sucesso"] else (VERMELHO, VERMELHO_FUNDO)
    data, _, hora = (dados.get("concluido_em") or "").partition(" ")
    y = 236
    d.rounded_rectangle((MARGEM, y, L - MARGEM, y + 130), radius=16, fill=fundo, outline=cor, width=2)
    _icone(d, MARGEM + 75, y + 65, 36, cor, st["sucesso"])
    d.text((MARGEM + 135, y + 26), st["palavra"], font=_fonte(46, True), fill=cor)
    _texto(d, (MARGEM + 135, y + 84), st["frase"], _fonte(21), PRETO, largura=L - 2 * MARGEM - 380, max_linhas=1)
    d.text((L - MARGEM - 30, y + 30), data, font=_fonte(34, True), fill=NAVY, anchor="ra")
    if hora:
        d.text((L - MARGEM - 30, y + 76), f"às {hora}", font=_fonte(26), fill=PRETO, anchor="ra")

    # grade de dados
    y = 410
    meio = L // 2 + 10
    col = meio - MARGEM - 40
    retirada = dados.get("tipo") == "retirada"
    pedido = codigo + (" (reentrega)" if dados.get("reentrega") else "")
    for (r1, v1), (r2, v2) in [
            (("Pedido", pedido), ("Nota fiscal", nf)),
            (("Remetente", dados.get("embarcador")), ("Destinatário final" if retirada else "Destinatário",
                                                        dados.get("destinatario")))]:
        y = max(_campo(d, MARGEM, y, r1, v1, col), _campo(d, meio, y, r2, v2, col)) + 20
        d.line((MARGEM, y, L - MARGEM, y), fill=CINZA_LINHA, width=2)
        y += 20
    if dados.get("endereco"):
        y = _campo(d, MARGEM, y, "Endereço de entrega", dados["endereco"], L - 2 * MARGEM, negrito=False) + 20
        d.line((MARGEM, y, L - MARGEM, y), fill=CINZA_LINHA, width=2)
        y += 20
    volumes = dados.get("volumes")
    extras = [("Volumes", f"{volumes} volume{'' if volumes == 1 else 's'}" if volumes else ""),
              ("Retirado por" if retirada else "Motorista",
               dados.get("quem_retira") if retirada else dados.get("motorista")),
              ("Rota", dados.get("rota"))]
    extras = [(r, v) for r, v in extras if v]
    if extras:
        terco = (L - 2 * MARGEM) // 3
        y = max(_campo(d, MARGEM + i * terco, y, r, v, terco - 20, tam=23) for i, (r, v) in enumerate(extras)) + 30

    base = _rodape(img, d, link, emitido_em)
    direita = f"Foto 1 de {n_fotos}" if n_fotos > 1 else ""
    if foto is None and retirada:
        return img  # retirada no galpao nao tem canhoto: sem secao de foto vazia
    _secao_foto(img, d, y, base, (rotulo_foto or st["secao"]).upper(), direita, foto, st["sem_foto"])
    return img


def _pagina_foto(dados: dict, st: dict, foto, rotulo: str, indice: int, n_fotos: int,
                 link: str, emitido_em: datetime) -> Image.Image:
    img = Image.new("RGB", A4, BRANCO)
    d = ImageDraw.Draw(img)
    _cabecalho(img, d, st["documento"], f"Pedido {dados.get('codigo') or ''} · continuação")
    base = _rodape(img, d, link, emitido_em)
    _secao_foto(img, d, 236, base, (rotulo or st["secao"]).upper(), f"Foto {indice} de {n_fotos}", foto, "")
    return img


def gerar(dados: dict, fotos: list[tuple[str, bytes]] | None = None, link: str = "",
          emitido_em: datetime | None = None) -> bytes:
    """PDF do comprovante. `fotos`: [(rotulo, conteudo)], a primeira e o canhoto.
    `link`: URL publica do registro (vira o QR do rodape); vazio = sem QR."""
    emitido_em = emitido_em or datetime.now()
    abertas = [(rotulo, img) for rotulo, img in ((r, _abrir_foto(c)) for r, c in (fotos or [])) if img is not None]
    st = _status(dados, bool(abertas))
    n = len(abertas)
    primeira = abertas[0] if abertas else ("", None)
    paginas = [_pagina_principal(dados, st, primeira[1], primeira[0], n, link, emitido_em)]
    for i, (rotulo, foto) in enumerate(abertas[1:], start=2):
        paginas.append(_pagina_foto(dados, st, foto, rotulo, i, n, link, emitido_em))
    buf = io.BytesIO()
    paginas[0].save(buf, format="PDF", save_all=True, append_images=paginas[1:], resolution=DPI, quality=90)
    return buf.getvalue()
