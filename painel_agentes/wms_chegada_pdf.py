# -*- coding: utf-8 -*-
"""
Relatorio de Recebimento em PDF (chegada do caminhao): documento formal que
vai anexado ao e-mail do cliente e pode ser baixado no painel.

Mesmo padrao do comprovante de entrega: paginas A4 desenhadas com Pillow a
150 dpi e salvas como PDF (sem reportlab).

    gerar(detalhe, fotos=[(tipo, bytes_jpeg)], emitido_em=None) -> bytes
    do_banco(conn, chegada_id, pasta=None) -> bytes
"""
import io
import logging
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

import wms_chegada

logger = logging.getLogger(__name__)

LOGO_PATH = Path(__file__).parent.parent / "assets" / "logo_freshlog.png"

A4 = (1240, 1754)
DPI = 150.0
MARGEM = 90
LARGURA = A4[0] - 2 * MARGEM
NAVY = (23, 29, 51)
VERDE = (13, 148, 108)
VERMELHO = (190, 60, 40)
CINZA_LINHA = (215, 221, 229)
CINZA_TXT = (108, 115, 130)
PRETO = (31, 41, 55)
BRANCO = (255, 255, 255)

LEGENDA_FOTO = {"CAMINHAO_ABERTO": "Caminhão aberto", "PLACA": "Placa", "NOTA_FISCAL": "Nota fiscal",
                "TERMOMETRO": "Termômetro", "CARGA": "Carga", "AVARIA": "Avaria"}
ROTULO_CLASSE = {"REFRIGERADO": "Refrigerado", "CONGELADO": "Congelado"}

_COLUNAS_FOTO = 2
_FOTO_H = 400
_LEGENDA_H = 40
_GAP = 30
_LIMITE_Y = A4[1] - 140  # acima do rodape


def _fonte(tamanho_px: int, negrito: bool = False) -> ImageFont.FreeTypeFont:
    candidatos = (["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if negrito
                  else ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf",
                        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
    for caminho in candidatos:
        try:
            return ImageFont.truetype(caminho, tamanho_px)
        except OSError:
            continue
    return ImageFont.load_default()


def _data(valor: str) -> str:
    try:
        return datetime.strptime(valor, "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return "—"


def _graus(valor) -> str:
    return f"{float(valor):.1f}".replace(".", ",") + " °C"


def _quebrar(d, texto: str, fonte, largura: int) -> list[str]:
    linhas, atual = [], ""
    for palavra in (texto or "").split():
        tentativa = f"{atual} {palavra}".strip()
        if d.textlength(tentativa, font=fonte) <= largura or not atual:
            atual = tentativa
        else:
            linhas.append(atual)
            atual = palavra
    return linhas + ([atual] if atual else [])


def _texto(d, x: int, y: int, texto: str, fonte, cor=PRETO, largura: int = LARGURA) -> int:
    """Desenha com quebra de linha e devolve o y logo abaixo."""
    for linha in _quebrar(d, texto, fonte, largura) or [""]:
        d.text((x, y), linha, font=fonte, fill=cor)
        y += int(fonte.size * 1.3)
    return y


def _colar_logo(img: Image.Image, x: int, y: int, altura: int) -> None:
    try:
        logo = Image.open(LOGO_PATH).convert("RGBA")
    except OSError:
        return
    logo = logo.resize((int(logo.width * altura / logo.height), altura), Image.Resampling.LANCZOS)
    img.paste(logo, (x, y), logo)


def _abrir_foto(conteudo: bytes) -> Image.Image | None:
    try:
        return ImageOps.exif_transpose(Image.open(io.BytesIO(conteudo))).convert("RGB")
    except Exception as e:  # foto corrompida nao pode derrubar o relatorio
        logger.warning(f"Foto do relatorio de recebimento ilegivel, ficou de fora: {e}")
        return None


def _colar_foto(img: Image.Image, foto: Image.Image, caixa: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = caixa
    escala = min((x1 - x0) / foto.width, (y1 - y0) / foto.height)
    foto = foto.resize((max(1, int(foto.width * escala)), max(1, int(foto.height * escala))), Image.Resampling.LANCZOS)
    px, py = x0 + (x1 - x0 - foto.width) // 2, y0 + (y1 - y0 - foto.height) // 2
    img.paste(foto, (px, py))
    ImageDraw.Draw(img).rectangle((px, py, px + foto.width - 1, py + foto.height - 1), outline=CINZA_LINHA, width=2)


def _nova_pagina() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", A4, BRANCO)
    return img, ImageDraw.Draw(img)


def _campo(d, x: int, y: int, rotulo: str, valor: str, largura: int) -> int:
    y = _texto(d, x, y, rotulo.upper(), _fonte(17, True), CINZA_TXT, largura)
    return _texto(d, x, y + 2, valor or "—", _fonte(26, True), PRETO, largura) + 10


def _secao(d, y: int, titulo: str) -> int:
    d.line((MARGEM, y, A4[0] - MARGEM, y), fill=CINZA_LINHA, width=2)
    return _texto(d, MARGEM, y + 14, titulo, _fonte(30, True), NAVY) + 8


def _linha_temperatura(t: dict) -> tuple[str, bool]:
    fora = bool(t.get("fora"))
    texto = (f"{ROTULO_CLASSE.get(t['classe'], t['classe'].title())}: {_graus(t['medida_c'])} "
             f"(faixa {_graus(t['faixa_min'])} a {_graus(t['faixa_max'])}) · "
             f"{'fora da faixa' if fora else 'dentro da faixa'}")
    return texto, fora


def gerar(detalhe: dict, fotos: list, emitido_em: datetime | None = None) -> bytes:
    emitido = (emitido_em or datetime.now()).strftime("%d/%m/%Y %H:%M")
    paginas = []
    img, d = _nova_pagina()
    paginas.append(img)

    _colar_logo(img, MARGEM, 70, 90)
    y = _texto(d, MARGEM, 190, "Relatório de Recebimento", _fonte(46, True), NAVY) + 20

    meio = MARGEM + LARGURA // 2
    recs = detalhe.get("recebimentos", [])
    cods = ", ".join(r["codigo"] for r in recs if r.get("codigo"))
    embs = ", ".join(dict.fromkeys(r["embarcador"] for r in recs if r.get("embarcador")))
    y = max(_campo(d, MARGEM, y, "Carga", cods, LARGURA // 2 - 20),
            _campo(d, meio, y, "Embarcador", embs, LARGURA // 2))
    y = _campo(d, MARGEM, y, "Chegada", _data(detalhe.get("chegada_em")), LARGURA)
    y0 = y
    y = _campo(d, MARGEM, y0, "Motorista", detalhe.get("motorista"), 400)
    _campo(d, MARGEM + 420, y0, "Placa", detalhe.get("placa"), 250)
    _campo(d, MARGEM + 700, y0, "Transportadora", detalhe.get("transportadora"), LARGURA - 700)
    y0 = y
    y = _campo(d, MARGEM, y0, "Volumes", detalhe.get("volumes"), 400)
    _campo(d, MARGEM + 420, y0, "Lacre", detalhe.get("lacre"), 400)

    if detalhe.get("temperaturas"):
        y = _secao(d, y + 10, "Temperatura")
        for t in detalhe["temperaturas"]:
            texto, fora = _linha_temperatura(t)
            y = _texto(d, MARGEM, y, texto, _fonte(26, True), VERMELHO if fora else VERDE)

    y = _secao(d, y + 10, "Condição da carga")
    integra = detalhe.get("integra")
    y = _texto(d, MARGEM, y, "Íntegra" if integra else "Com avaria", _fonte(28, True),
               VERDE if integra else VERMELHO)
    y = _texto(d, MARGEM, y + 4, "OBSERVAÇÃO", _fonte(17, True), CINZA_TXT)
    y = _texto(d, MARGEM, y + 2, detalhe.get("observacao") or "—", _fonte(24))
    y = _campo(d, MARGEM, y + 14, "Operador", detalhe.get("operador_nome"), LARGURA)

    via = detalhe.get("liberacao_via")
    if via:
        y = _secao(d, y + 6, "Liberação")
        quando = _data(detalhe.get("liberacao_em"))
        nome = detalhe.get("liberacao_nome") or "—"
        if via == "CLIENTE":
            frase = f"Autorizado por {nome} em {quando}, pelo link de liberação"
        else:
            frase = f"Registrado por {nome} em {quando}: {detalhe.get('liberacao_texto') or '—'}"
        y = _texto(d, MARGEM, y, frase, _fonte(24))

    abertas = [(tipo, f) for tipo, f in ((t, _abrir_foto(b)) for t, b in fotos) if f is not None]
    if abertas:
        if y + 20 + 70 + _FOTO_H + _LEGENDA_H > _LIMITE_Y:  # titulo nao fica sozinho no fim da pagina
            img, d = _nova_pagina()
            paginas.append(img)
            y = MARGEM - 20
        y = _secao(d, y + 20, "Fotos")
    larg_foto = (LARGURA - _GAP * (_COLUNAS_FOTO - 1)) // _COLUNAS_FOTO
    linha_y = y
    for i, (tipo, foto) in enumerate(abertas):
        col = i % _COLUNAS_FOTO
        if col == 0:
            if y + _FOTO_H + _LEGENDA_H > _LIMITE_Y:
                img, d = _nova_pagina()
                paginas.append(img)
                y = MARGEM
            linha_y = y
        x = MARGEM + col * (larg_foto + _GAP)
        _colar_foto(img, foto, (x, linha_y, x + larg_foto, linha_y + _FOTO_H))
        d.text((x, linha_y + _FOTO_H + 8), LEGENDA_FOTO.get(tipo, tipo.title()), font=_fonte(22, True), fill=CINZA_TXT)
        if col == _COLUNAS_FOTO - 1 or i == len(abertas) - 1:
            y = linha_y + _FOTO_H + _LEGENDA_H + _GAP

    for pagina in paginas:
        ImageDraw.Draw(pagina).text((MARGEM, A4[1] - 90), f"Emitido em {emitido} · Fresh Log",
                                    font=_fonte(20), fill=CINZA_TXT)

    buf = io.BytesIO()
    paginas[0].save(buf, format="PDF", save_all=True, append_images=paginas[1:], resolution=DPI)
    return buf.getvalue()


def do_banco(conn, chegada_id: int, pasta: Path | None = None) -> bytes:
    """Monta o detalhe e le as fotos do disco; foto ausente no disco e pulada."""
    det = wms_chegada.detalhe(conn, chegada_id)
    fotos = []
    for f in det["fotos"]:
        try:
            fotos.append((f["tipo"], wms_chegada.caminho_foto(conn, chegada_id, f["id"], pasta).read_bytes()))
        except OSError:
            logger.warning(f"Foto {f['id']} da chegada {chegada_id} sumiu do disco, ficou de fora")
    return gerar(det, fotos)
