"""
Indicação de lote pelo cliente no portal (18/11/2026; primeiro cliente: Maria Dolores).

As duas formas ficam no envio de pedidos:
- a planilha de lotes do cliente (modelo Maria Dolores, lida por painel_agentes/wms_lotes_planilha),
  solta junto com os XMLs ou sozinha: gravada na hora da leitura e casada com o pedido pelo número
  da NF quando ele chega ao WMS;
- o lote marcado na prévia, produto a produto (um lote pro produto inteiro da nota): gravado na
  confirmação do envio. Pra dividir um produto entre lotes, o cliente usa a planilha.

Só vale pra embarcador piloto do WMS (config wms.pilotos) e pra nota XML (pedido de planilha não
tem o número da NF que casa com o WMS). Sem indicação, a separação segue a regra padrão (sai
primeiro o que vence primeiro). Quem usa as indicações é painel_agentes/wms_separacao.lotes_indicados.
"""
import sqlite3
from datetime import date, datetime
from pathlib import Path

from painel_agentes import wms_lotes_planilha as planilha
from painel_agentes import wms_pilotos

ErroLote = planilha.ErroWMS
normalizar_lote = planilha.normalizar_lote
MAX_LOTES_POR_NOTA = 300
ARQUIVO_PREVIA = "portal: prévia do envio"
# Colunas depois do SKU com estes nomes são de planilha de PEDIDOS, não de lotes
_NAO_SAO_LOTE = {"quantidade", "qtd", "qtde", "quant", "valor", "valor unitario", "preco", "preco unitario",
                 "unidade", "un", "peso", "descricao", "produto", "data", "observacao", "observacoes"}
_SAIU_DA_SEPARACAO = ("SEPARADO", "CONFERINDO", "AGUARDANDO_DECISAO", "CONFERIDO", "EMBALADO")
_NO_ESTOQUE = ("EM_ESTOQUE", "ABERTA", "DEVOLVIDA")


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _tem_tabela(conn, nome: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (nome,)).fetchone() is not None


def embarcador_wms(conn, cnpj: str, config: dict | None) -> int | None:
    """Id do embarcador no WMS (stkkc_id da tabela interno) se ele é piloto; senão None."""
    cnpj = "".join(c for c in str(cnpj or "") if c.isdigit())
    if not cnpj:
        return None
    try:
        row = conn.execute("SELECT stkkc_id FROM interno WHERE cnpj_embarcador = ?", (cnpj,)).fetchone()
    except sqlite3.Error:
        return None
    try:
        emb = int(row[0]) if row and row[0] not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return emb if emb in {p["id"] for p in wms_pilotos.pilotos(config or {})} else None


def ler_planilha_de_lotes(nome: str, conteudo: bytes) -> dict | None:
    """
    O arquivo é a planilha de lotes? Devolve o que ela diz (wms_lotes_planilha.ler_lotes) ou None
    quando é outra planilha (segue como planilha de pedidos). Reconhece pelo nome do arquivo ou da
    aba ("lote"). Levanta ErroLote quando o nome diz que é de lotes mas a tabela não fecha.
    """
    nome_de_lote = "lote" in planilha._norm(Path(str(nome or "")).stem)
    try:
        lido = planilha.ler_lotes(conteudo, nome)
    except ErroLote:
        if nome_de_lote:
            raise
        return None
    if not (nome_de_lote or planilha._norm(lido.get("aba")).startswith("lote")):
        return None
    if any(planilha._norm(l) in _NAO_SAO_LOTE for l in lido["lotes"]):
        if nome_de_lote:
            raise ErroLote("A planilha de lotes precisa ter, depois do SKU, uma coluna por lote (o nome da coluna é "
                           "o lote). Achei colunas de quantidade ou valor no lugar.")
        return None
    for l in lido["lotes"]:
        normalizar_lote(l)
    return lido


def lotes_em_estoque(conn, embarcador_id: int, skus) -> dict[str, list[dict]]:
    """{SKU: [{lote, validade}]} das caixas no estoque (não vencidas), primeiro o que vence antes."""
    skus = sorted({str(s or "").strip().upper() for s in skus if s})
    if not skus or not (_tem_tabela(conn, "wms_caixas") and _tem_tabela(conn, "wms_produtos")):
        return {}
    hoje = date.today().isoformat()
    saida: dict[str, list[dict]] = {}
    for i in range(0, len(skus), 400):
        parte = skus[i:i + 400]
        rows = conn.execute(
            "SELECT UPPER(p.sku) AS sku, c.lote, COALESCE(c.validade, '') AS validade FROM wms_caixas c "
            "JOIN wms_produtos p ON p.id = c.produto_id "
            f"WHERE p.embarcador_id = ? AND p.ativo = 1 AND c.situacao IN ({','.join('?' * len(_NO_ESTOQUE))}) "
            "AND COALESCE(c.lote, '') <> '' AND (COALESCE(c.validade, '') = '' OR c.validade >= ?) "
            f"AND UPPER(p.sku) IN ({','.join('?' * len(parte))}) "
            "GROUP BY 1, 2, 3 ORDER BY 1, COALESCE(c.validade, '') = '', 3, 2",
            (int(embarcador_id), *_NO_ESTOQUE, hoje, *parte)).fetchall()
        for r in rows:
            saida.setdefault(r["sku"], []).append({"lote": r["lote"], "validade": r["validade"]})
    return saida


def _pedidos_por_nf(conn, embarcador_id: int, nfs) -> dict[str, dict]:
    nfs = sorted({n for n in nfs if n})
    if not nfs or not _tem_tabela(conn, "wms_pedidos"):
        return {}
    try:
        rows = conn.execute(
            "SELECT codigo_ps, nf, COALESCE(execucao, '') AS execucao, estado_reserva FROM wms_pedidos "
            f"WHERE embarcador_id = ? AND nf IN ({','.join('?' * len(nfs))})", (str(int(embarcador_id)), *nfs)).fetchall()
    except sqlite3.OperationalError:   # WMS sem as colunas da separação ainda
        return {}
    return {str(r["nf"]): dict(r) for r in rows}


def _ja_separado(pedido: dict | None) -> bool:
    return bool(pedido) and (pedido["execucao"] in _SAIU_DA_SEPARACAO or pedido.get("estado_reserva") == "BAIXADO")


def _skus_do_cadastro(conn, embarcador_id: int) -> set[str] | None:
    if not _tem_tabela(conn, "wms_produtos"):
        return None
    return {r[0] for r in conn.execute(
        "SELECT UPPER(sku) FROM wms_produtos WHERE embarcador_id = ? AND ativo = 1 AND COALESCE(sku, '') <> ''",
        (int(embarcador_id),)).fetchall()}


def _aviso_estoque(estoque: dict, nf: str, sku: str, lote: str) -> str | None:
    """Só avisa quando o produto TEM caixa no estoque e nenhuma é do lote pedido."""
    lotes = {x["lote"] for x in estoque.get(sku, [])}
    if lotes and lote not in lotes:
        return f"NF {nf}: não há caixa do lote {lote} de {sku} no estoque da Fresh Log agora."
    return None


def _escrever(conn, gravar) -> None:
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        planilha.garantir_tabela(conn)
        gravar()
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def importar_planilha(conn, embarcador_id: int, lido: dict, arquivo: str, por: str) -> dict:
    """Grava a planilha de lotes (cada NF+SKU substitui o que havia) e devolve o resumo pra tela."""
    em = _agora()
    _escrever(conn, lambda: planilha.gravar_planilha(conn, embarcador_id, lido, f"portal: {arquivo}", por, em))
    linhas = lido.get("linhas") or []
    cadastro = _skus_do_cadastro(conn, embarcador_id)
    pedidos = _pedidos_por_nf(conn, embarcador_id, [l["nf"] for l in linhas])
    estoque = lotes_em_estoque(conn, embarcador_id, [l["sku"] for l in linhas])
    avisos, resumo = [], []
    for l in linhas:
        p = pedidos.get(l["nf"])
        situacao = "ok" if p else "aguardando_pedido"
        if cadastro is not None and l["sku"] not in cadastro:
            situacao = "produto_desconhecido"
            avisos.append(f"NF {l['nf']}: o produto {l['sku']} não está no cadastro da Fresh Log. Essa indicação não vai valer.")
        elif _ja_separado(p):
            situacao = "chegou_tarde"
            avisos.append(f"NF {l['nf']} ({p['codigo_ps']}): o pedido já foi separado. O lote {l['lote']} chegou tarde para ele.")
        else:
            aviso = _aviso_estoque(estoque, l["nf"], l["sku"], l["lote"])
            if aviso:
                avisos.append(aviso)
        resumo.append({"nf": l["nf"], "sku": l["sku"], "lote": l["lote"], "qtd_un": l["qtd_un"],
                       "codigo_ps": p["codigo_ps"] if p else "", "situacao": situacao})
    return {"arquivo": arquivo, "gravadas": len(resumo), "sem_lote": len(lido.get("sem_lote") or []),
            "linhas": resumo, "avisos": avisos}


def gravar_lotes_da_nota(conn, embarcador_id: int, nf: str, lotes: dict, produtos: list[dict], por: str) -> dict:
    """
    Lotes marcados na prévia ({SKU: lote}, um lote pro produto inteiro). `produtos` = os da nota
    (relidos do XML gravado, nunca o que o navegador mandou). Lote em branco = sem indicação (não
    mexe no que veio da planilha). Devolve {gravados, avisos}.
    """
    if not isinstance(lotes, dict) or not lotes:
        return {"gravados": 0, "avisos": []}
    if len(lotes) > MAX_LOTES_POR_NOTA:
        raise ErroLote("Lotes demais para uma nota.")
    nf = planilha.normalizar_nf(nf)
    if not nf:
        raise ErroLote("A nota não tem número: não dá para indicar lote.")
    da_nota = {str(p.get("sku") or "").strip().upper() for p in produtos or []}
    marcados = {}
    for sku, lote in lotes.items():
        sku = str(sku or "").strip().upper()
        if sku not in da_nota:
            raise ErroLote(f"NF {nf}: o produto {sku[:60]} não está nesta nota.")
        lote = normalizar_lote(lote)
        if lote:
            marcados[sku] = lote
    if not marcados:
        return {"gravados": 0, "avisos": []}
    p = _pedidos_por_nf(conn, embarcador_id, [nf]).get(nf)
    if _ja_separado(p):
        raise ErroLote(f"NF {nf} ({p['codigo_ps']}): o pedido já foi separado. Não dá mais para indicar lote.")
    em = _agora()

    def gravar():
        for sku, lote in marcados.items():
            planilha.gravar_lote_inteiro(conn, embarcador_id, nf, sku, lote, ARQUIVO_PREVIA, por, em)
    _escrever(conn, gravar)
    estoque = lotes_em_estoque(conn, embarcador_id, marcados)
    avisos = [a for a in (_aviso_estoque(estoque, nf, sku, lote) for sku, lote in marcados.items()) if a]
    return {"gravados": len(marcados), "avisos": avisos}
