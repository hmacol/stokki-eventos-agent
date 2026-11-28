"""
Etiqueta de volume do pedido, 10 x 15 cm (17/11/2026; H9 e C3/C4 da spec do galpao).

Impressa no fechamento dos volumes, sem rota: faixa preta com o PS e "n / total",
destinatario (nome, endereco, cidade), embarcador e o QR `FV|<PS>|<n>|<total>`.
Sai pela fila de impressao (tipo VOLUME), uma etiqueta por pagina.
"""
import io

import wms

LARGURA_PX, ALTURA_PX = 1181, 1772   # 10 x 15 cm a 300 dpi, em pe
MARGEM = 50


def _quebrar(draw, texto: str, fonte, largura: int, max_linhas: int) -> list[str]:
    """Quebra por palavra em ate max_linhas; o que nao couber termina em reticencias."""
    palavras, linhas, atual = str(texto or "").split(), [], ""
    for palavra in palavras:
        tentativa = f"{atual} {palavra}".strip()
        if draw.textlength(tentativa, font=fonte) <= largura or not atual:
            atual = tentativa
            continue
        linhas.append(atual)
        atual = palavra
        if len(linhas) == max_linhas:
            break
    if atual and len(linhas) < max_linhas:
        linhas.append(atual)
    elif len(linhas) == max_linhas and atual:
        ultima = linhas[-1]
        while ultima and draw.textlength(ultima + "…", font=fonte) > largura:
            ultima = ultima[:-1]
        linhas[-1] = ultima.rstrip() + "…"
    # palavra sozinha maior que a largura: corta
    saida = []
    for linha in linhas:
        while linha and draw.textlength(linha, font=fonte) > largura:
            linha = linha[:-1]
        saida.append(linha)
    return saida


def desenhar_etiqueta_volume(dados: dict):
    """Imagem PIL (modo L) da etiqueta. dados: codigo_ps, numero, total, qr, destinatario,
    endereco, cidade, uf, embarcador, nf. Campo vazio sai em branco, nunca impede imprimir."""
    from PIL import Image, ImageDraw
    W, H = LARGURA_PX, ALTURA_PX
    img = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(img)
    largura = W - 2 * MARGEM

    # faixa preta: PS em cima, "n / total" enorme embaixo
    faixa_h = 560
    draw.rectangle((0, 0, W, faixa_h), fill=0)
    ps = str(dados.get("codigo_ps") or "-")
    f_ps = wms._ajustar_fonte(draw, ps, largura, 170, negrito=True, condensada=True)
    draw.text((W / 2, 50), ps, fill=255, font=f_ps, anchor="ma")
    contagem = f"{dados.get('numero') or '?'} / {dados.get('total') or '?'}"
    f_n = wms._ajustar_fonte(draw, contagem, largura, 300, negrito=True, condensada=True)
    draw.text((W / 2, faixa_h - 40), contagem, fill=255, font=f_n, anchor="mb")
    f_vol = wms._fonte(44, negrito=True, condensada=True)
    draw.text((MARGEM, faixa_h - 60), "VOLUME", fill=255, font=f_vol)

    y = faixa_h + 45
    f_rot = wms._fonte(34, negrito=True, condensada=True)
    draw.text((MARGEM, y), "DESTINATÁRIO", fill=0, font=f_rot)
    y += 48
    f_dest = wms._fonte(78, negrito=True, condensada=True)
    for linha in _quebrar(draw, str(dados.get("destinatario") or "").upper(), f_dest, largura, 3):
        draw.text((MARGEM, y), linha, fill=0, font=f_dest)
        y += 88
    y += 14
    f_end = wms._fonte(46, condensada=True)
    for linha in _quebrar(draw, dados.get("endereco") or "", f_end, largura, 2):
        draw.text((MARGEM, y), linha, fill=0, font=f_end)
        y += 56
    cidade = " - ".join(p for p in (str(dados.get("cidade") or "").upper(), str(dados.get("uf") or "").upper()) if p)
    if cidade:
        y += 10
        f_cid = wms._ajustar_fonte(draw, cidade, largura, 84, negrito=True, condensada=True)
        draw.text((MARGEM, y), cidade, fill=0, font=f_cid)

    # rodape: QR a direita, embarcador e NF a esquerda
    lado = 420
    qr = wms._qr_imagem(str(dados.get("qr") or ""), lado)
    y_qr = H - MARGEM - lado
    img.paste(qr, (W - MARGEM - lado, y_qr))
    draw.line((MARGEM, y_qr - 30, W - MARGEM, y_qr - 30), fill=0, width=4)
    larg_esq = largura - lado - 40
    y = y_qr
    nf = str(dados.get("nf") or "").strip()
    if nf:
        draw.text((MARGEM, y), "NF", fill=0, font=f_rot)
        draw.text((MARGEM, y + 40), nf, fill=0, font=wms._ajustar_fonte(draw, nf, larg_esq, 80, negrito=True, condensada=True))
        y += 150
    draw.text((MARGEM, y), "EMBARCADOR", fill=0, font=f_rot)
    y += 46
    f_emb = wms._fonte(46, negrito=True, condensada=True)
    for linha in _quebrar(draw, str(dados.get("embarcador") or "").upper(), f_emb, larg_esq, 3):
        draw.text((MARGEM, y), linha, fill=0, font=f_emb)
        y += 54
    draw.text((MARGEM, H - MARGEM - 40), "Freshlog", fill=0, font=wms._fonte(36, negrito=True))
    return img


def gerar_pdf_volumes(conn, itens: list) -> bytes:
    """PDF com uma etiqueta 10x15 por item da fila. Cada item e o QR: `FV|PS|n|total` (piloto do
    galpao, wms_separacao) ou `FV|PS|n|total|id` (todo pedido da Stokki, expedicao_volumes)."""
    import expedicao_volumes
    import wms_separacao
    artes = []
    for conteudo in itens or []:
        if str(conteudo).count("|") == 4:
            dados = expedicao_volumes.dados_da_etiqueta(conn, conteudo)
        else:
            dados = wms_separacao.dados_do_volume(conn, conteudo)
        if not dados:
            raise wms.ErroWMS(f"Etiqueta de volume invalida: {conteudo}")
        artes.append(desenhar_etiqueta_volume(dados))
    if not artes:
        raise wms.ErroWMS("Informe ao menos um volume pra imprimir.")
    buf = io.BytesIO()
    artes[0].save(buf, format="PDF", save_all=True, append_images=artes[1:], resolution=wms._DPI)
    return buf.getvalue()
