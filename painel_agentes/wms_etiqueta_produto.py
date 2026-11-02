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


def conteudo_qr(sku: str, lote: str, validade: str | None, serial: str = "") -> str:
    """Texto que vai dentro do QR: FL|SKU|LOTE|AAAA-MM-DD, mais |SERIAL na etiqueta de caixa."""
    texto = f"{PREFIXO}{str(sku or '').strip()}|{str(lote or '').strip()}|{str(validade or '').strip()}"
    serial = str(serial or "").strip()
    return f"{texto}|{serial}" if serial else texto


def identificador_do_produto(produto: dict) -> str:
    """
    O que vai no lugar do SKU dentro do QR. Produto sem SKU (cadastro
    rapido do galpao, por exemplo) gerava 'FL||LOTE|VAL', que o
    ler_codigo() nao resolve de volta -- etiqueta impressa e colada na
    caixa que ninguem consegue bipar. O EAN serve tao bem quanto: o
    wms.buscar_por_codigo() procura por EAN, DUN, SKU e codigo interno.
    Sem SKU nem EAN usa o codigo interno (FLP000001); se ele ainda nao
    foi gerado devolve vazio e quem imprime chama _identificador_garantido.
    """
    return (str(produto.get("sku") or "").strip() or str(produto.get("ean") or "").strip()
            or str(produto.get("codigo_interno") or "").strip())


def _identificador_garantido(conn, produto: dict) -> str:
    """Identificador do QR; gera o codigo interno se o produto nao tem SKU nem EAN. Commita."""
    identificador = identificador_do_produto(produto)
    if identificador:
        return identificador
    import wms_caixas
    wms_caixas.garantir_tabelas(conn)
    return wms_caixas.garantir_codigo_interno(conn, produto["id"])


def _cortar(texto: str, limite: int) -> str:
    """
    Corta pra caber na etiqueta sem picar palavra no meio -- ela vai na
    caixa que o operador le ("GRANDE" virando "GRAND" e pior do que
    faltar a palavra inteira). Sobrou texto, marca com reticencias.
    """
    texto = " ".join(str(texto or "").split())
    if len(texto) <= limite:
        return texto
    corte = texto[:limite - 1]
    if " " in corte:
        corte = corte[:corte.rfind(" ")]
    return corte.rstrip() + "…"


def parse_qr(texto: str) -> dict | None:
    """Le o QR da nossa etiqueta. Devolve None pro que nao for nosso."""
    bruto = str(texto or "").strip()
    if not bruto.upper().startswith(PREFIXO):
        return None
    partes = bruto[len(PREFIXO):].split("|")
    if len(partes) < 3:
        return None
    return {"sku": partes[0].strip(), "lote": partes[1].strip(), "validade": partes[2].strip(),
            "serial": partes[3].strip().upper() if len(partes) > 3 else ""}


def desenhar_etiqueta_produto(dados: dict):
    """
    Imagem PIL (modo L, 1181×591 @300dpi) da etiqueta de mercadoria.
    Com dados["serial"] (caixa) entra a faixa preta 'CAIXA <serial>'.
    """
    from PIL import Image, ImageDraw
    W, H = wms._LARGURA_PX, wms._ALTURA_PX
    img = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(img)
    serial = str(dados.get("serial") or "").strip()

    # QR a esquerda
    lado = H - 70
    qr = wms._qr_imagem(conteudo_qr(dados.get("sku"), dados.get("lote"), dados.get("validade"), serial), lado)
    img.paste(qr, (30, 35))

    x0 = 30 + lado + 40
    largura = W - x0 - 30
    descricao = _cortar((dados.get("descricao") or "").upper(), 60)
    f_desc = wms._ajustar_fonte(draw, descricao, largura, 50, negrito=True, condensada=True)
    draw.text((x0, 35), descricao, fill=0, font=f_desc)
    y = 35 + f_desc.size + 14

    # SKU em destaque
    linha_sku = f"SKU {dados.get('sku') or '-'}"
    f_sku = wms._ajustar_fonte(draw, linha_sku, largura, 58, negrito=True, condensada=True)
    draw.text((x0, y), linha_sku, fill=0, font=f_sku)
    y += f_sku.size + 14

    linha_lote = f"LOTE {dados.get('lote') or '-'}"
    validade = dados.get("validade") or ""
    if validade:
        try:
            from datetime import date
            validade = date.fromisoformat(validade).strftime("%d/%m/%Y")
        except ValueError:
            pass
    linha_val = f"VAL {validade}" if validade else "SEM VALIDADE"
    # as duas linhas no mesmo tamanho: o da que for mais larga
    fontes = [wms._ajustar_fonte(draw, t, largura, 72, condensada=True) for t in (linha_lote, linha_val)]
    f_big = min(fontes, key=lambda f: f.size)
    draw.text((x0, y), linha_lote, fill=0, font=f_big)
    y += f_big.size + 8
    draw.text((x0, y), linha_val, fill=0, font=f_big)
    y += f_big.size + 16

    if serial:
        draw.rectangle((x0, y, W - 30, y + 62), fill=0)
        draw.text((x0 + 14, y + 6), f"CAIXA {serial}", fill=255, font=wms._fonte(46, negrito=True))

    rodape = f"{_cortar(dados.get('embarcador'), 34)}  ·  Freshlog"
    f_rod = wms._ajustar_fonte(draw, rodape, largura, 28, negrito=False)
    draw.text((x0, H - 60), rodape, fill=0, font=f_rod)
    return img


def _pdf_das_artes(artes: list, orientacao: str, margem_mm: float, desloc_x_mm: float, desloc_y_mm: float) -> bytes:
    """PDF com uma pagina por arte, no tamanho exato da midia termica."""
    import io
    paginas = []
    for arte in artes:
        if orientacao == "retrato":
            arte = arte.rotate(90, expand=True)
        elif orientacao == "retrato-inv":
            arte = arte.rotate(270, expand=True)
        paginas.append(wms._aplicar_margem(arte, margem_mm, desloc_x_mm, desloc_y_mm).convert("L"))
    buf = io.BytesIO()
    paginas[0].save(buf, format="PDF", save_all=True, append_images=paginas[1:], resolution=wms._DPI)
    return buf.getvalue()


def gerar_etiquetas_produto_pdf(conn, produto_id: int, lote: str, validade: str | None, copias: int = 1,
                                orientacao: str = "paisagem", margem_mm: float = 0,
                                desloc_x_mm: float = 0, desloc_y_mm: float = 0) -> bytes:
    """PDF com uma etiqueta por pagina, no tamanho exato da midia termica."""
    if not produto_id:
        raise wms.ErroWMS("Informe o produto da etiqueta.")
    produto = wms.obter_produto(conn, produto_id)
    if not produto:
        raise wms.ErroWMS("Produto nao encontrado.")
    identificador = _identificador_garantido(conn, produto)
    validade = wms._validar_validade(validade)
    copias = max(1, min(int(copias or 1), 200))
    dados = {"descricao": produto["descricao"], "sku": identificador, "lote": lote,
             "validade": validade, "embarcador": produto["embarcador"]}
    arte = desenhar_etiqueta_produto(dados)
    return _pdf_das_artes([arte] * copias, orientacao, margem_mm, desloc_x_mm, desloc_y_mm)


def gerar_pdf_caixas(conn, seriais: list[str], orientacao: str = "paisagem", margem_mm: float = 0,
                     desloc_x_mm: float = 0, desloc_y_mm: float = 0) -> bytes:
    """
    PDF com uma etiqueta por serial (reimpressao incluida). Recusa serial
    inexistente ou CANCELADA: etiqueta de caixa que o sistema nao reconhece
    nao pode ir pro galpao.
    """
    import wms_caixas
    seriais = [str(s or "").strip().upper() for s in (seriais or [])]
    if not seriais:
        raise wms.ErroWMS("Informe ao menos uma caixa pra imprimir.")
    if len(seriais) > 200:
        raise wms.ErroWMS("No maximo 200 caixas por impressao.")
    wms_caixas.garantir_tabelas(conn)
    caixas = []
    for serial in seriais:
        caixa = wms_caixas.obter_caixa(conn, serial)
        if not caixa:
            raise wms.ErroWMS(f"Caixa {serial} nao existe.")
        if caixa["situacao"] == "CANCELADA":
            raise wms.ErroWMS(f"Caixa {serial} esta cancelada.")
        caixas.append(caixa)
    identificadores = {}  # produto_id -> identificador (gera codigo interno fora de transacao)
    artes = []
    for caixa in caixas:
        produto = caixa["produto"]
        if produto["id"] not in identificadores:
            identificadores[produto["id"]] = _identificador_garantido(conn, produto)
        artes.append(desenhar_etiqueta_produto({
            "descricao": produto["descricao"], "sku": identificadores[produto["id"]],
            "lote": caixa["lote"], "validade": caixa["validade"],
            "embarcador": produto["embarcador"], "serial": caixa["serial"]}))
    return _pdf_das_artes(artes, orientacao, margem_mm, desloc_x_mm, desloc_y_mm)
