# -*- coding: utf-8 -*-
"""
painel_agentes/wms_etiqueta_produto.py

Etiqueta de mercadoria (10 × 5 cm) com QR de SKU, lote e validade.
Decisao do Hugo (21/09/2026): sem serial -- a etiqueta vale pra qualquer
caixa daquele lote, entao da pra imprimir um bolo e colar em tudo.

O layout reusa as medidas e as fontes da etiqueta de posicao, que ja esta
calibrada na Elgin L42 Pro Full (midia deitada, config wms.etiqueta_* da
VPS). Nao mude a calibracao aqui -- os ajustes finos (orientacao, margem,
deslocamento) vem do config.yaml da VPS.
"""
import wms

PREFIXO = "FL|"


def conteudo_qr(sku: str, lote: str, validade: str | None) -> str:
    """Texto que vai dentro do QR: FL|SKU|LOTE|AAAA-MM-DD."""
    return f"{PREFIXO}{str(sku or '').strip()}|{str(lote or '').strip()}|{str(validade or '').strip()}"


def parse_qr(texto: str) -> dict | None:
    """Le o QR da nossa etiqueta. Devolve None pro que nao for nosso."""
    bruto = str(texto or "").strip()
    if not bruto.upper().startswith(PREFIXO):
        return None
    partes = bruto[len(PREFIXO):].split("|")
    if len(partes) < 3:
        return None
    return {"sku": partes[0].strip(), "lote": partes[1].strip(), "validade": partes[2].strip()}


def desenhar_etiqueta_produto(dados: dict):
    """Imagem PIL (modo L, 1181×591 @300dpi) da etiqueta de mercadoria."""
    from PIL import Image, ImageDraw
    W, H = wms._LARGURA_PX, wms._ALTURA_PX
    img = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(img)

    # QR a esquerda
    lado = H - 70
    qr = wms._qr_imagem(conteudo_qr(dados.get("sku"), dados.get("lote"), dados.get("validade")), lado)
    img.paste(qr, (30, 35))

    x0 = 30 + lado + 40
    largura = W - x0 - 30
    descricao = (dados.get("descricao") or "").upper()[:60]
    f_desc = wms._ajustar_fonte(draw, descricao, largura, 54, negrito=True, condensada=True)
    draw.text((x0, 40), descricao, fill=0, font=f_desc)

    linha_lote = f"LOTE {dados.get('lote') or '-'}"
    f_lote = wms._ajustar_fonte(draw, linha_lote, largura, 92, condensada=True)
    draw.text((x0, 40 + f_desc.size + 25), linha_lote, fill=0, font=f_lote)

    validade = dados.get("validade") or ""
    if validade:
        try:
            from datetime import date
            validade = date.fromisoformat(validade).strftime("%d/%m/%Y")
        except ValueError:
            pass
    linha_val = f"VAL {validade}" if validade else "SEM VALIDADE"
    f_val = wms._ajustar_fonte(draw, linha_val, largura, 92, condensada=True)
    draw.text((x0, 40 + f_desc.size + 25 + f_lote.size + 15), linha_val, fill=0, font=f_val)

    rodape = f"{dados.get('sku') or '-'}  ·  {(dados.get('embarcador') or '')[:28]}  ·  Freshlog"
    f_rod = wms._ajustar_fonte(draw, rodape, largura, 30, negrito=False)
    draw.text((x0, H - 75), rodape, fill=0, font=f_rod)
    return img


def gerar_etiquetas_produto_pdf(conn, produto_id: int, lote: str, validade: str | None, copias: int = 1,
                                orientacao: str = "paisagem", margem_mm: float = 0,
                                desloc_x_mm: float = 0, desloc_y_mm: float = 0) -> bytes:
    """PDF com uma etiqueta por pagina, no tamanho exato da midia termica."""
    import io

    produto = wms.obter_produto(conn, produto_id)
    if not produto:
        raise wms.ErroWMS("Produto nao encontrado.")
    validade = wms._validar_validade(validade)
    copias = max(1, min(int(copias or 1), 200))
    dados = {"descricao": produto["descricao"], "sku": produto["sku"], "lote": lote,
             "validade": validade, "embarcador": produto["embarcador"]}
    arte = desenhar_etiqueta_produto(dados)
    if orientacao == "retrato":
        arte = arte.rotate(90, expand=True)
    elif orientacao == "retrato-inv":
        arte = arte.rotate(270, expand=True)
    pagina = wms._aplicar_margem(arte, margem_mm, desloc_x_mm, desloc_y_mm)
    paginas = [pagina.convert("L")] * copias
    buf = io.BytesIO()
    paginas[0].save(buf, format="PDF", save_all=True, append_images=paginas[1:], resolution=wms._DPI)
    return buf.getvalue()
