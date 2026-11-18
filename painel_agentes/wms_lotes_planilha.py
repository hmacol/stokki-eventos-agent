"""
Planilha de lotes do cliente (17/11/2026; primeiro modelo: Maria Dolores).

O cliente manda uma planilha dizendo, por nota fiscal e por produto, de qual lote quer cada
quantidade. O modelo da Maria Dolores e uma tabela dinamica na aba "Lotes":

    Nome do contato | Nota Fiscal | Descricao | Codigo (SKU) | 247 | 254 | 255 | 258 | FIFO | Total Geral
    DUFRY ...       | 42430       | MINI CX.. | MNCXDCHF300  |   6 |     |     |     |      |     6
                    |             | ...       | MNDDTP       |     |  30 |     |     |      |    30

Cada coluna depois do SKU e um lote; a coluna "FIFO" (ou sem nome) e "sem lote indicado": vale a
regra padrao (sai primeiro o que vence primeiro). Nome do contato e Nota Fiscal em branco repetem
a linha de cima. Este modulo so LE a planilha; quem grava e wms_separacao.importar_lotes_cliente.
"""
import io
import re
import unicodedata

from wms import ErroWMS

MAX_BYTES = 8 * 1024 * 1024
MAX_LINHAS = 5000
_SEM_LOTE = {"", "fifo", "fefo", "sem lote", "(vazio)", "vazio", "padrao", "(em branco)", "em branco"}
_TOTAIS = {"total geral", "total", "soma", "grand total"}
_CAB_NF = {"nota fiscal", "nf", "n nf", "numero nf", "numero da nf", "nota", "nfe", "nf e"}
_CAB_SKU = {"codigo (sku)", "codigo sku", "sku", "codigo", "codigo do produto", "cod produto", "cod"}
_CAB_CLIENTE = {"nome do contato", "cliente", "destinatario", "nome", "razao social"}


def _norm(valor) -> str:
    texto = unicodedata.normalize("NFKD", str(valor if valor is not None else "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", texto).strip().lower()


def _texto(valor) -> str:
    """Celula como texto: numero inteiro sem '.0' (lote 247 vem como 247.0 do Excel)."""
    if valor is None:
        return ""
    if isinstance(valor, bool):
        return ""
    if isinstance(valor, (int, float)):
        return str(int(valor)) if float(valor) == int(valor) else str(valor)
    return " ".join(str(valor).split())


def _numero(valor) -> float | None:
    if valor is None or isinstance(valor, bool) or valor == "":
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    bruto = str(valor).strip().replace(".", "").replace(",", ".") if "," in str(valor) else str(valor).strip()
    try:
        return float(bruto)
    except ValueError:
        return None


def abrir(conteudo: bytes, nome: str) -> list[tuple[str, list[list]]]:
    """[(nome da aba, linhas)] de um .xlsx (openpyxl) ou .xls (xlrd, se instalado)."""
    if not conteudo:
        raise ErroWMS("Arquivo vazio.")
    if len(conteudo) > MAX_BYTES:
        raise ErroWMS("Planilha grande demais (limite de 8 MB).")
    if conteudo[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or str(nome or "").lower().endswith(".xls"):
        try:
            import xlrd
        except ImportError:
            raise ErroWMS("Este servidor ainda nao le o formato .xls (Excel antigo): salve a planilha como .xlsx "
                          "e envie de novo.")
        try:
            wb = xlrd.open_workbook(file_contents=conteudo)
        except Exception as e:  # noqa: BLE001
            raise ErroWMS(f"Nao consegui abrir a planilha ({type(e).__name__}).")
        return [(sh.name, [[sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(min(sh.nrows, MAX_LINHAS))])
                for sh in wb.sheets()]
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise ErroWMS(f"Nao consegui abrir a planilha ({type(e).__name__}). Envie um arquivo .xlsx ou .xls.")
    try:
        abas = []
        for ws in wb.worksheets:
            linhas = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= MAX_LINHAS:
                    break
                linhas.append(list(row))
            abas.append((ws.title or "", linhas))
        return abas
    finally:
        wb.close()


def _achar_cabecalho(linhas: list[list]) -> tuple[int, int, int, int | None] | None:
    """(linha do cabecalho, coluna da NF, coluna do SKU, coluna do cliente) ou None."""
    for i, linha in enumerate(linhas[:40]):
        nomes = [_norm(c) for c in linha]
        col_nf = next((j for j, n in enumerate(nomes) if n in _CAB_NF), None)
        col_sku = next((j for j, n in enumerate(nomes) if n in _CAB_SKU), None)
        if col_nf is not None and col_sku is not None and col_sku > col_nf:
            col_cli = next((j for j, n in enumerate(nomes) if n in _CAB_CLIENTE), None)
            return i, col_nf, col_sku, col_cli
    return None


def ler_lotes(conteudo: bytes, nome: str) -> dict:
    """
    {"aba", "linhas": [{nf, sku, lote, qtd_un, cliente, descricao}], "sem_lote": [{nf, sku, qtd_un}],
     "lotes": [nomes das colunas de lote]}. `linhas` so traz o que tem lote indicado.
    """
    abas = abrir(conteudo, nome)
    # a aba "Lotes" primeiro; senao a primeira que tiver o cabecalho
    abas.sort(key=lambda a: not _norm(a[0]).startswith("lote"))
    for titulo, linhas in abas:
        achado = _achar_cabecalho(linhas)
        if achado:
            break
    else:
        raise ErroWMS("Nao achei na planilha uma tabela com as colunas 'Nota Fiscal' e 'Codigo (SKU)' "
                      "seguidas das colunas de lote.")
    i_cab, col_nf, col_sku, col_cli = achado
    cabecalho = linhas[i_cab]
    colunas_lote = []   # (indice, lote ou '' pra "sem lote")
    for j in range(col_sku + 1, len(cabecalho)):
        nome_col = _norm(_texto(cabecalho[j]))
        if nome_col in _TOTAIS:
            break
        colunas_lote.append((j, "" if nome_col in _SEM_LOTE else _texto(cabecalho[j]).upper()))
    if not any(lote for _, lote in colunas_lote):
        raise ErroWMS("A tabela nao tem nenhuma coluna de lote depois do SKU.")
    saida, sem_lote = [], []
    nf_atual, cliente_atual = "", ""
    for linha in linhas[i_cab + 1:]:
        linha = list(linha) + [None] * (len(cabecalho) - len(linha))
        if _norm(linha[0]) in _TOTAIS or _norm(linha[col_nf]) in _TOTAIS:
            break
        nf = re.sub(r"\D", "", _texto(linha[col_nf])).lstrip("0")
        if nf:
            nf_atual = nf
        if col_cli is not None and _texto(linha[col_cli]):
            cliente_atual = _texto(linha[col_cli])
        sku = _texto(linha[col_sku]).upper()
        if not sku or not nf_atual:
            continue
        descricao = _texto(linha[col_sku - 1]) if col_sku - 1 > col_nf else ""
        for j, lote in colunas_lote:
            qtd = _numero(linha[j])
            if not qtd or qtd <= 0:
                continue
            if lote:
                saida.append({"nf": nf_atual, "sku": sku, "lote": lote, "qtd_un": round(qtd, 3),
                              "cliente": cliente_atual, "descricao": descricao})
            else:
                sem_lote.append({"nf": nf_atual, "sku": sku, "qtd_un": round(qtd, 3)})
    if not saida and not sem_lote:
        raise ErroWMS("A tabela de lotes esta vazia.")
    return {"aba": titulo, "linhas": saida, "sem_lote": sem_lote,
            "lotes": [lote for _, lote in colunas_lote if lote]}
