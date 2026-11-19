"""
Planilha de lotes do cliente (17/11/2026; primeiro modelo: Maria Dolores).

O cliente manda uma planilha dizendo, por nota fiscal e por produto, de qual lote quer cada
quantidade. O modelo da Maria Dolores e uma tabela dinamica na aba "Lotes":

    Nome do contato | Nota Fiscal | Descricao | Codigo (SKU) | 247 | 254 | 255 | 258 | FIFO | Total Geral
    DUFRY ...       | 42430       | MINI CX.. | MNCXDCHF300  |   6 |     |     |     |      |     6
                    |             | ...       | MNDDTP       |     |  30 |     |     |      |    30

Cada coluna depois do SKU e um lote; a coluna "FIFO" (ou sem nome) e "sem lote indicado": vale a
regra padrao (sai primeiro o que vence primeiro). Nome do contato e Nota Fiscal em branco repetem
a linha de cima.

A tabela wms_lotes_cliente mora aqui (18/11) porque o portal do cliente tambem grava nela (a
planilha solta no envio e o lote marcado na previa) e o portal nao importa o resto do WMS. Linha
com `inteiro = 1` e "este lote pro produto inteiro" (marcacao do cliente na previa, sem
quantidade). Os resumos e avisos ficam com quem chama (wms_separacao, portal_cliente/lotes_cliente).
"""
import io
import math
import re
import unicodedata

try:
    from wms import ErroWMS
except ImportError:      # importado pelo portal do cliente (painel_agentes fora do sys.path)
    from painel_agentes.wms import ErroWMS

MAX_BYTES = 8 * 1024 * 1024
MAX_LINHAS = 5000
# Tetos (revisao de seguranca 18/11): a planilha vem do cliente pelo portal, aberto na internet.
# Sem eles uma planilha pequena enchia o dados.db, travava a escrita ou esgotava a memoria.
MAX_ABAS = 5
MAX_COLUNAS = 100
MAX_INDICACOES = 2000
MAX_QTD = 1_000_000
MAX_SKU = 60
MAX_DIGITOS_NF = 9
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
    """Quantidade da celula; None se vazia ou nao for numero. ErroWMS se infinita ou absurda."""
    if valor is None or isinstance(valor, bool) or valor == "":
        return None
    if isinstance(valor, (int, float)):
        numero = float(valor)
    else:
        bruto = str(valor).strip().replace(".", "").replace(",", ".") if "," in str(valor) else str(valor).strip()
        try:
            numero = float(bruto)
        except ValueError:
            return None
    if not math.isfinite(numero) or numero > MAX_QTD:
        raise ErroWMS(f"Quantidade invalida na planilha: {str(valor)[:20]!r} (maximo {MAX_QTD:,}).".replace(",", "."))
    return numero


def _ordem_das_abas(nomes: list[str]) -> list[int]:
    """Indices das abas a ler: as "Lote..." primeiro, no maximo MAX_ABAS."""
    return sorted(range(len(nomes)), key=lambda i: (not _norm(nomes[i]).startswith("lote"), i))[:MAX_ABAS]


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
            wb = xlrd.open_workbook(file_contents=conteudo, on_demand=True)   # so carrega a aba pedida
        except Exception as e:  # noqa: BLE001
            raise ErroWMS(f"Nao consegui abrir a planilha ({type(e).__name__}).")
        try:
            abas = []
            for i in _ordem_das_abas(wb.sheet_names()):
                sh = wb.sheet_by_index(i)
                abas.append((sh.name, [[sh.cell_value(r, c) for c in range(min(sh.ncols, MAX_COLUNAS))]
                                       for r in range(min(sh.nrows, MAX_LINHAS))]))
            return abas
        except ErroWMS:
            raise
        except Exception as e:  # noqa: BLE001
            raise ErroWMS(f"Nao consegui ler a planilha ({type(e).__name__}).")
        finally:
            wb.release_resources()
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise ErroWMS(f"Nao consegui abrir a planilha ({type(e).__name__}). Envie um arquivo .xlsx ou .xls.")
    try:
        abas = []
        nomes = wb.sheetnames
        for i in _ordem_das_abas(nomes):
            ws = wb[nomes[i]]
            # max_col: em read_only o openpyxl completa cada linha ate a coluna declarada na
            # <dimension>, que um arquivo de 2 KB pode dizer que e a XFD (16 mil colunas)
            linhas = [list(row) for row in ws.iter_rows(max_row=MAX_LINHAS, max_col=MAX_COLUNAS, values_only=True)]
            abas.append((ws.title or "", linhas))
        return abas
    except Exception as e:  # noqa: BLE001
        raise ErroWMS(f"Nao consegui ler a planilha ({type(e).__name__}).")
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
        colunas_lote.append((j, "" if nome_col in _SEM_LOTE else normalizar_lote(_texto(cabecalho[j]))))
    if not any(lote for _, lote in colunas_lote):
        raise ErroWMS("A tabela nao tem nenhuma coluna de lote depois do SKU.")
    saida, sem_lote = [], []
    nf_atual, cliente_atual = "", ""
    for n_linha, linha in enumerate(linhas[i_cab + 1:], start=i_cab + 2):
        linha = list(linha) + [None] * (len(cabecalho) - len(linha))
        if _norm(linha[0]) in _TOTAIS or _norm(linha[col_nf]) in _TOTAIS:
            break
        nf = re.sub(r"\D", "", _texto(linha[col_nf])).lstrip("0")
        if len(nf) > MAX_DIGITOS_NF:
            raise ErroWMS(f"Linha {n_linha}: numero de nota fiscal com mais de {MAX_DIGITOS_NF} digitos.")
        if nf:
            nf_atual = nf
        if col_cli is not None and _texto(linha[col_cli]):
            cliente_atual = _texto(linha[col_cli])[:120]
        sku = _texto(linha[col_sku]).upper()
        if not sku or not nf_atual:
            continue
        if len(sku) > MAX_SKU:
            raise ErroWMS(f"Linha {n_linha}: SKU com mais de {MAX_SKU} caracteres.")
        descricao = _texto(linha[col_sku - 1])[:120] if col_sku - 1 > col_nf else ""
        for j, lote in colunas_lote:
            qtd = _numero(linha[j])
            if not qtd or qtd <= 0:
                continue
            if lote:
                saida.append({"nf": nf_atual, "sku": sku, "lote": lote, "qtd_un": round(qtd, 3),
                              "cliente": cliente_atual, "descricao": descricao})
            else:
                sem_lote.append({"nf": nf_atual, "sku": sku, "qtd_un": round(qtd, 3)})
            if len(saida) + len(sem_lote) > MAX_INDICACOES:
                raise ErroWMS(f"Planilha com mais de {MAX_INDICACOES} indicacoes: divida em arquivos menores.")
    if not saida and not sem_lote:
        raise ErroWMS("A tabela de lotes esta vazia.")
    return {"aba": titulo, "linhas": saida, "sem_lote": sem_lote,
            "lotes": [lote for _, lote in colunas_lote if lote]}


# ---------------------------------------------------------------------------
# Gravacao (tabela wms_lotes_cliente)
# ---------------------------------------------------------------------------

DDL_LOTES_CLIENTE = """
CREATE TABLE IF NOT EXISTS wms_lotes_cliente (
    embarcador_id INTEGER NOT NULL,
    nf            TEXT NOT NULL,              -- numero da NF, como em wms_pedidos.nf
    sku           TEXT NOT NULL,
    lote          TEXT NOT NULL,
    qtd_un        REAL NOT NULL,              -- quanto desse lote o cliente quer nesse produto
    cliente       TEXT NOT NULL DEFAULT '',
    arquivo       TEXT NOT NULL DEFAULT '',
    por           TEXT NOT NULL DEFAULT '',
    em            TEXT NOT NULL,
    inteiro       INTEGER NOT NULL DEFAULT 0, -- 1 = este lote pro produto inteiro (qtd_un nao vale)
    PRIMARY KEY (embarcador_id, nf, sku, lote)
)
"""
_RE_LOTE = re.compile(r"^[A-Z0-9][A-Z0-9 ./_-]{0,39}$")


def garantir_tabela(conn) -> None:
    """Cria a tabela (ou a coluna `inteiro`, que entrou em 18/11). Nao faz commit."""
    conn.execute(DDL_LOTES_CLIENTE)
    if "inteiro" not in {r[1] for r in conn.execute("PRAGMA table_info(wms_lotes_cliente)")}:
        conn.execute("ALTER TABLE wms_lotes_cliente ADD COLUMN inteiro INTEGER NOT NULL DEFAULT 0")


def normalizar_nf(valor) -> str:
    return re.sub(r"\D", "", str(valor or "")).lstrip("0")


def normalizar_lote(valor) -> str:
    """Lote digitado pelo cliente: maiusculo, espacos simples. '' = sem lote. ErroWMS se estranho."""
    lote = " ".join(str(valor or "").split()).upper()
    if lote and not _RE_LOTE.match(lote):
        raise ErroWMS(f"Lote invalido: {lote[:40]!r}. Use letras, numeros, espaco, ponto, barra, hifen ou "
                      f"sublinhado (ate 40).")
    return lote


def gravar_planilha(conn, embarcador_id: int, lido: dict, arquivo: str, por: str, em: str) -> None:
    """
    Grava o que ler_lotes leu. Cada (NF, SKU) da planilha SUBSTITUI o que havia dele (reenviar
    corrige); linha "sem lote" apaga a indicacao antiga daquele produto. Sem commit: quem chama
    abre e fecha a transacao.
    """
    embarcador_id = int(embarcador_id)
    vistos = set()
    for l in list(lido.get("linhas") or []) + list(lido.get("sem_lote") or []):
        chave = (l["nf"], l["sku"])
        if chave not in vistos:
            vistos.add(chave)
            conn.execute("DELETE FROM wms_lotes_cliente WHERE embarcador_id = ? AND nf = ? AND sku = ?",
                         (embarcador_id, l["nf"], l["sku"]))
    for l in lido.get("linhas") or []:
        conn.execute(
            "INSERT INTO wms_lotes_cliente (embarcador_id, nf, sku, lote, qtd_un, cliente, arquivo, por, em) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(embarcador_id, nf, sku, lote) DO UPDATE SET "
            "qtd_un = wms_lotes_cliente.qtd_un + excluded.qtd_un",
            (embarcador_id, l["nf"], l["sku"], l["lote"], round(float(l["qtd_un"] or 0), 3),
             str(l.get("cliente") or "")[:120], str(arquivo or "")[:120], str(por or "")[:80], em))


def gravar_lote_inteiro(conn, embarcador_id: int, nf: str, sku: str, lote: str, arquivo: str, por: str,
                        em: str) -> None:
    """O produto inteiro desta NF sai do `lote` (marcacao do cliente na previa do envio). Substitui
    o que havia do (NF, SKU), inclusive o que veio da planilha. Sem commit."""
    nf, sku, lote = normalizar_nf(nf), str(sku or "").strip().upper(), normalizar_lote(lote)
    if not (nf and sku and lote):
        raise ErroWMS("Indicacao de lote sem NF, produto ou lote.")
    conn.execute("DELETE FROM wms_lotes_cliente WHERE embarcador_id = ? AND nf = ? AND sku = ?",
                 (int(embarcador_id), nf, sku))
    conn.execute(
        "INSERT INTO wms_lotes_cliente (embarcador_id, nf, sku, lote, qtd_un, arquivo, por, em, inteiro) "
        "VALUES (?,?,?,?,0,?,?,?,1)", (int(embarcador_id), nf, sku, lote, str(arquivo or "")[:120], str(por or "")[:80], em))
