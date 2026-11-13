# -*- coding: utf-8 -*-
"""Devolucao de estoque no cancelamento: regras puras de decisao (sem rede) e a fila no banco.

Pedido cancelado depois da separacao deixa o saldo preso no endereco Virtual.
Aqui so se decide QUANTO devolver e PARA ONDE; quem le a Stokki e move e outro trecho.
Na duvida, nao mover e avisar a operacao.
"""
import json
import logging
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

SISTEMA = ("virtual", "falta", "sobra", "ajuste", "recebimento")


def _e_sistema(endereco: str) -> bool:
    """Endereco de sistema ou vazio nunca e destino."""
    e = (endereco or "").strip().lower()
    return not e or e in SISTEMA


def _ref_pedido(referencia: str) -> str:
    """Referencia limpa (sem espaco, sem '#', minuscula) para comparar numero exato."""
    return (referencia or "").strip().lstrip("#").strip().lower()


def bloqueado_pelo_pedido(movimentos: list, id_stokki: int) -> float:
    """Soma dos Bloqueios Internos cuja referencia e exatamente #PS-<id> (PE-, outros pedidos
    e movimentos nao Internos nao contam)."""
    alvo = f"ps-{id_stokki}"
    return sum(m["quantidade"] for m in movimentos
               if m.get("tipo") == "Bloqueio" and m.get("movimento") == "Interno"
               and _ref_pedido(m.get("referencia")) == alvo)


def ja_devolvido(movimentos: list, qtd: float, cancelado_em: datetime) -> str:
    """Prova de devolucao: linhas `Desbloqueio | Interno` sem referencia a partir do minuto do cancelamento
    (na pratica, as de ler_devolucoes). 'sim' se alguma tem a mesma qtd ou se a SOMA delas e a qtd
    (devolucao em partes), tolerancia 0.001; 'duvida' se ha linhas que nao fecham, se nao ha data de
    cancelamento ou se a prova usa linha nao concluida (`concluido` False; sem a chave conta como concluida);
    'nao' se nao ha nenhuma. Linha sem data conta como posterior (lado seguro)."""
    if cancelado_em is None:
        return "duvida"
    desbloqueios = [m for m in movimentos
                    if m.get("tipo") == "Desbloqueio" and m.get("movimento") == "Interno"
                    and not _ref_pedido(m.get("referencia")) and (m.get("quando") is None or m["quando"] >= cancelado_em)]
    if not desbloqueios:
        return "nao"
    iguais = [m for m in desbloqueios if abs(m["quantidade"] - qtd) < 0.001]
    if iguais:
        usadas = iguais
    elif abs(sum(m["quantidade"] for m in desbloqueios) - qtd) < 0.001:
        usadas = desbloqueios
    else:
        return "duvida"
    if any(m.get("concluido") is False for m in usadas):
        return "duvida"
    return "sim"


def quanto_mover(bloqueado: float, oferecido: float) -> float:
    """Nunca mais que o Bloqueio do pedido nem mais que a tela oferece; nunca negativo."""
    return max(0, min(bloqueado, oferecido))


def _por_estoque(saldo: list) -> list:
    """Enderecos fisicos com estoque, do maior para o menor (empate: ordem alfabetica)."""
    fisicos = [s for s in saldo if not _e_sistema(s["endereco"]) and s["estoque"] > 0]
    return [s["endereco"] for s in sorted(fisicos, key=lambda s: (-s["estoque"], s["endereco"]))]


def escolher_destinos(qtd: float, origens: list, saldo_produto: list, saldo_embarcador: list) -> list:
    """Candidatos (endereco, qtd) em ordem de preferencia: origem da separacao, maior saldo do
    produto, maior saldo do embarcador. Sem repetir endereco e sem endereco de sistema.
    Contrato para quem executa: as partes da origem somam; se nao chegam a qtd, o resto vai como
    candidato extra. Os candidatos das regras 2 e 3 sao ALTERNATIVAS que levam o que falta: o
    executor acumula o que ja moveu e nunca passa de qtd no total. Se a soma movida ficar menor
    que qtd, o item e parcial."""
    saldos = _por_estoque(saldo_produto) + [e for e in _por_estoque(saldo_embarcador)
                                           if e not in _por_estoque(saldo_produto)]
    cands, vistos, restante = [], set(), qtd
    for endereco, parte in origens:
        chave = endereco.strip().lower()
        if _e_sistema(endereco) or chave in vistos or restante <= 0 or parte <= 0:
            continue
        parte = min(parte, restante)
        cands.append((endereco, parte))
        vistos.add(chave)
        restante -= parte
    if cands and restante > 0:
        resto_para = next((e for e in saldos if e.strip().lower() not in vistos), None)
        if resto_para:
            cands.append((resto_para, restante))
            vistos.add(resto_para.strip().lower())
    for endereco in saldos:
        if endereco.strip().lower() not in vistos:
            cands.append((endereco, qtd))
            vistos.add(endereco.strip().lower())
    return cands


def cancelamento_antigo(cancelado_em, agora: datetime, ja_estava: bool) -> bool:
    """Cancelamento que ja existia na primeira leitura e tem 24 h ou mais (ou sem data): so avisar."""
    if not ja_estava:
        return False
    return cancelado_em is None or agora - cancelado_em >= timedelta(hours=24)


# --- Leituras na Stokki (so GET; a sessao vem de fora, aqui nao se loga nem se pega trava) ---
# ajuste_estoque e bs4 entram so quando uma leitura roda: a fila (registrar/ligado) nao carrega nada disso.

TOLERANCIA_ORIGEM = timedelta(minutes=2)
MESES_PARA_TRAS = 3
COLS_MOVIMENTOS = ["date", "conclusion", "reference", "marker", "type", "motion", "quantity", "quantity_stock"]
COLS_ORIGEM = ["id", "created", "concluded", "user", "origin", "destination", "quantity"]
CHAVES_MOVIMENTOS = ("date", "conclusion", "reference", "type", "motion", "quantity")
CHAVES_ORIGEM = ("id", "created", "concluded", "origin", "destination", "quantity")


def _ae():
    from stokki import ajuste_estoque
    return ajuste_estoque


def _soup(html: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


def _quando(texto):
    """'03/11/2026 12:12' (com ou sem HTML no meio) -> datetime; sem data legivel -> None."""
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})\D+(\d{1,2}):(\d{2})", _ae().limpa(texto))
    if not m:
        return None
    d, mes, a, h, mi = (int(x) for x in m.groups())
    return datetime(a, mes, d, h, mi)


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()


def _meses(inicio: datetime, fim: datetime) -> list:
    """[(ano, mes)] de `inicio` ate `fim`, inclusive (ao menos o mes de `inicio`)."""
    ano, mes, saida = inicio.year, inicio.month, []
    while (ano, mes) <= (fim.year, fim.month):
        saida.append((ano, mes))
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return saida or [(inicio.year, inicio.month)]


def _get(sess, url: str, **kw):
    r = sess.get(url, **kw)
    r.raise_for_status()
    return r


def _linhas_tabela(sess, url: str, params: dict, referer: str, chaves: tuple = ()) -> list:
    """Todas as linhas (aaData) de uma tabela DataTables. Levanta ValueError com a URL se a resposta nao
    tem aaData/iTotalDisplayRecords, se uma linha nao tem as `chaves`, se uma pagina repete linhas de
    paginas anteriores (servidor ignorou `start`) ou se o total lido nao fecha com o informado."""
    linhas, vistas, start, total = [], set(), 0, 0
    while True:
        r = _get(sess, url, params={**params, "start": start}, headers={**_ae().AJAX, "Referer": referer})
        try:
            j = r.json()
            rows = j["aaData"]
            total = int(j["iTotalDisplayRecords"])
            assert isinstance(rows, list)
        except (ValueError, KeyError, TypeError, AttributeError, AssertionError) as e:
            raise ValueError(f"resposta sem a tabela esperada (aaData/iTotalDisplayRecords): {url}") from e
        for row in rows:
            faltam = [c for c in chaves if not isinstance(row, dict) or c not in row]
            if faltam:
                raise ValueError(f"linha sem a coluna {faltam[0]}: {url}")
        pagina = {json.dumps(x, sort_keys=True) for x in rows}
        if pagina & vistas:
            raise ValueError(f"paginacao repetiu linhas (start ignorado?): {url}")
        vistas |= pagina
        linhas += rows
        start += len(rows)
        if not rows or len(linhas) >= total:
            break
    if len(linhas) != total:
        raise ValueError(f"total lido ({len(linhas)}) difere de iTotalDisplayRecords ({total}): {url}")
    return linhas


def ler_pedido(sess, id_stokki: int) -> dict:
    """Pedido de saida: cliente, quando foi cancelado, quando comecou a separar (a mais antiga entre
    'Em separacao' e 'Em Conferencia') e os itens (uma entrada por produto, mesmo repetido no pedido)."""
    ae = _ae()
    url = f"{ae.ADM}/inventory/outbound/show/{int(id_stokki)}"
    soup = _soup(_get(sess, url, headers=ae.HTML).text)
    th = next((t for t in soup.find_all("th") if _sem_acento(ae.limpa(t.get_text())).startswith("cliente")), None)
    m = re.search(r"#stkkc-(\d+)", th.find_next("td").get_text()) if th is not None else None
    tabela = soup.select_one("div#item table")
    if not m or tabela is None:
        raise ValueError(f"pagina do pedido sem cliente ou sem itens: {url}")
    itens, vistos = [], set()
    for tr in tabela.select("tbody tr"):
        tds = [ae.limpa(td.get_text()) for td in tr.find_all("td")]
        mi = re.search(r"#ITM-(\d+)", tds[1]) if len(tds) >= 4 else None
        if not mi:
            raise ValueError(f"linha de item sem #ITM-<id>: {url}")
        if int(mi.group(1)) in vistos:   # o Bloqueio e por produto: duas linhas = uma so entrada
            continue
        vistos.add(int(mi.group(1)))
        itens.append({"product_id": int(mi.group(1)), "sku": tds[2], "nome": tds[3]})
    if not itens:
        raise ValueError(f"pedido sem itens: {url}")
    cancelado_em, separado_em, dia = None, None, ""
    for el in soup.select("div#history .time-label span, div#history .timeline-item"):
        if "timeline-item" not in (el.get("class") or []):
            dia = ae.limpa(el.get_text())
            continue
        hora, cab = el.select_one("span.time"), el.select_one(".timeline-header")
        qd = _quando(f"{dia} {ae.limpa(hora.get_text()) if hora is not None else ''}")
        sit = _sem_acento(ae.limpa(cab.get_text())) if cab is not None else ""
        if qd is None:
            continue
        if sit == "cancelado":
            cancelado_em = qd if cancelado_em is None else max(cancelado_em, qd)
        elif sit.startswith("em separa") or sit.startswith("em confer"):
            separado_em = qd if separado_em is None else min(separado_em, qd)
    return {"cliente_id": int(m.group(1)), "cancelado_em": cancelado_em, "separado_em": separado_em, "itens": itens}


def ler_movimentos(sess, product_id: int, ano: int, mes: int) -> list:
    """Movimentacao do produto no mes (v2/moviment/table), normalizada: tipo, movimento, referencia,
    quantidade, quando (conclusao da linha, ou a data da linha se vier vazia). Fonte SO do Bloqueio do pedido."""
    ae = _ae()
    url = f"{ae.ADM}/inventory/v2/moviment/table"
    extra = {"moviment": "all", "type": "all", "warehouse_id": 1, "year": ano, "month": mes, "product_id": int(product_id)}
    rows = _linhas_tabela(sess, url, ae._params_tabela(extra, COLS_MOVIMENTOS), f"{ae.ADM}/inventory/v2/moviment",
                          CHAVES_MOVIMENTOS)
    saida = []
    for r in rows:
        ref = re.search(r"#\s*[A-Za-z]+-\d+", ae.limpa(r["reference"]))
        saida.append({"tipo": ae.limpa(r["type"]), "movimento": ae.limpa(r["motion"]),
                      "referencia": re.sub(r"\s+", "", ref.group(0)) if ref else "",
                      "quantidade": ae.numero(r["quantity"]),
                      "quando": _quando(r["conclusion"]) or _quando(r["date"])})
    return saida


def ler_saldo_enderecos(sess, product_id: int) -> list:
    """Saldo do produto por endereco (Codigo | Estoque | Bloqueado | Disponivel), Virtual incluido."""
    ae = _ae()
    url = f"{ae.ADM}/inventory/moviment/addressing/{int(product_id)}/1"
    tabela = _soup(_get(sess, url, headers=ae.HTML).text).select_one("table#example1")
    if tabela is None or "localiza" not in _sem_acento(tabela.get_text()):
        raise ValueError(f"pagina sem a tabela de enderecos: {url}")
    saida = []
    for tr in tabela.select("tbody tr"):
        tds = [ae.limpa(td.get_text()) for td in tr.find_all("td")]
        if len(tds) >= 4 and tds[0]:
            saida.append({"endereco": tds[0], "estoque": ae.numero(tds[1]), "bloqueado": ae.numero(tds[2]),
                          "disponivel": ae.numero(tds[3])})
    return saida


def _movimentos_entre_enderecos(sess, product_id: int, de: datetime, ate: datetime) -> list:
    """Linhas de 'movimentos entre enderecamentos' dos meses de `de` a `ate`, normalizadas:
    [{id (do #A-<n>), quando (criacao), concluido (bool), origem, destino, quantidade}]. Base de ler_origem e ler_devolucoes."""
    ae = _ae()
    url = f"{ae.ADM}/inventory/moviment/addressing/table/moviment/table"
    referer = f"{ae.ADM}/inventory/moviment/addressing/moviment/{int(product_id)}/1"
    saida = []
    for ano, mes in _meses(de, ate):
        params = {"draw": 1, "length": 100, "search[value]": "", "search[regex]": "false",
                  "product_id": int(product_id), "provider_id": 1, "month": f"{mes:02d}", "year": ano,
                  "origin_id": "", "destination_id": ""}
        for i, nome in enumerate(COLS_ORIGEM):
            params.update({f"columns[{i}][data]": nome, f"columns[{i}][name]": "",
                           f"columns[{i}][searchable]": "true", f"columns[{i}][orderable]": "true"})
        for r in _linhas_tabela(sess, url, params, referer, CHAVES_ORIGEM):
            quando = _quando(r["created"])
            num = re.search(r"-(\d+)", ae.limpa(r["id"]))
            if quando is None or num is None:   # leitura que nao fecha nunca vira plano
                raise ValueError(f"movimento sem data de criacao ou sem id legivel: {url}")
            saida.append({"id": int(num.group(1)), "quando": quando, "concluido": _quando(r["concluded"]) is not None,
                          "origem": ae.limpa(r["origin"]), "destino": ae.limpa(r["destination"]),
                          "quantidade": ae.numero(r["quantity"])})
    return saida


def ler_origem(sess, product_id: int, inicio, fim, qtd: float) -> list:
    """Origem da separacao: linhas `<endereco fisico> -> Virtual` do produto entre o inicio da separacao e o
    cancelamento (+-2 min). So vale se a soma delas for exatamente `qtd`; qualquer outra situacao (nada,
    soma diferente, janela desconhecida) devolve [] = origem desconhecida. Nao adivinha subconjuntos."""
    if inicio is None or fim is None:
        return []
    de, ate = inicio - TOLERANCIA_ORIGEM, fim + TOLERANCIA_ORIGEM
    partes = {}
    for r in _movimentos_entre_enderecos(sess, product_id, de, ate):
        if (r["quando"] and de <= r["quando"] <= ate and r["destino"].lower() == "virtual"
                and not _e_sistema(r["origem"])):
            partes[r["origem"]] = partes.get(r["origem"], 0) + r["quantidade"]
    if partes and abs(sum(partes.values()) - qtd) < 0.001:
        return list(partes.items())
    return []


def ler_devolucoes(sess, product_id: int, desde: datetime, ate: datetime, ignorar=frozenset()) -> list:
    """Prova de devolucao ja feita: linhas `Virtual -> <endereco fisico>` do produto criadas de `desde` (inclusive,
    mesmo minuto conta) ate `ate`, no formato que ja_devolvido entende (mais o `id` do movimento). E o que a
    devolucao manual gera. Linhas cujo id esta em `ignorar` (devolucoes do proprio robo, outros pedidos) ficam fora."""
    return [{"tipo": "Desbloqueio", "movimento": "Interno", "referencia": "", "quantidade": r["quantidade"],
             "quando": r["quando"], "concluido": r["concluido"], "id": r["id"]}
            for r in _movimentos_entre_enderecos(sess, product_id, desde, ate)
            if (r["quando"] and desde <= r["quando"] <= ate and r["origem"].lower() == "virtual"
                and not _e_sistema(r["destino"]) and r["id"] not in ignorar)]


# --- Plano: leituras + regras (nada de escrita aqui) ---

def _inicio_da_janela(agora: datetime) -> datetime:
    """Primeiro dia do mes que fica MESES_PARA_TRAS meses antes do de `agora`."""
    limite = agora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for _ in range(MESES_PARA_TRAS):
        limite = (limite - timedelta(days=1)).replace(day=1)
    return limite


def _meses_dos_movimentos(cancelado_em: datetime, agora: datetime) -> list:
    """Do mes do cancelamento ate o mes de `agora`, no maximo 3 meses para tras de `agora`."""
    inicio = max(cancelado_em.replace(day=1), _inicio_da_janela(agora))
    return _meses(inicio, max(agora, inicio))


def _saldo_do_embarcador(sess, cliente_id: int) -> list:
    por_endereco = {}
    for linha in _ae().saldo_por_endereco(sess, cliente_id):
        por_endereco[linha["endereco"]] = por_endereco.get(linha["endereco"], 0) + linha["estoque"]
    return [{"endereco": e, "estoque": q} for e, q in por_endereco.items()]


def _plano_do_item(sess, ped: dict, item: dict, agora: datetime, ignorar=frozenset()) -> tuple:
    """(item do plano, motivo ou ''). Erro de leitura sobe como excecao: nunca vira 'nada a devolver'."""
    saida = {**item, "qtd": 0, "destinos": [], "situacao": "nada"}
    movimentos = []
    for ano, mes in _meses_dos_movimentos(ped["cancelado_em"], agora):
        movimentos += ler_movimentos(sess, item["product_id"], ano, mes)
    bloqueado = bloqueado_pelo_pedido(movimentos, ped["id_stokki"])
    if bloqueado <= 0:
        return saida, ""
    saida["qtd"] = bloqueado
    devolucoes = ler_devolucoes(sess, item["product_id"], ped["cancelado_em"], max(agora, ped["cancelado_em"]), ignorar)
    ja = ja_devolvido(devolucoes, bloqueado, ped["cancelado_em"])
    if ja == "sim":
        saida["situacao"] = "ja_devolvido"
        return saida, ""
    if ja == "duvida":
        saida["situacao"] = "duvida"
        if any(d.get("concluido") is False for d in devolucoes):
            return saida, f"devolucao do Virtual ainda em andamento (produto {item['sku']})"
        return saida, ("ha movimento do Virtual para endereco fisico depois do cancelamento que nao fecha com a "
                       f"quantidade do pedido (produto {item['sku']})")
    origem = ler_origem(sess, item["product_id"], ped["separado_em"], ped["cancelado_em"], bloqueado)
    saldo_produto = ler_saldo_enderecos(sess, item["product_id"])
    saldo_embarcador = []
    if not any(not _e_sistema(e) for e, _ in origem) and not _por_estoque(saldo_produto):
        saldo_embarcador = _saldo_do_embarcador(sess, ped["cliente_id"])   # regra 3, so se precisar
    saida["destinos"] = escolher_destinos(bloqueado, origem, saldo_produto, saldo_embarcador)
    if saida["destinos"]:
        saida["situacao"] = "mover"
        return saida, ""
    saida["situacao"] = "sem_destino"
    return saida, f"nenhum endereco fisico para devolver o produto {item['sku']}"


def montar_plano(sess, id_stokki: int, agora: datetime, ja_estava: bool = False, ignorar_movimentos=frozenset()) -> dict:
    """Le o pedido e a movimentacao e decide: MOVER / NADA / JA_DEVOLVIDO / ANTIGO / DUVIDA.
    Na duvida nada se move (o plano inteiro vira DUVIDA). Falha de leitura levanta excecao.
    `ignorar_movimentos`: ids (#A-<n>) de devolucoes do proprio robo para outros pedidos, que nao provam nada."""
    ped = {**ler_pedido(sess, id_stokki), "id_stokki": int(id_stokki)}
    vazios = [{**i, "qtd": 0, "destinos": [], "situacao": "nada"} for i in ped["itens"]]

    def plano(status, itens, motivo=""):
        return {"status": status, "cliente_id": ped["cliente_id"], "itens": itens, "motivo": motivo}

    if cancelamento_antigo(ped["cancelado_em"], agora, ja_estava):
        return plano("ANTIGO", vazios, "cancelamento de 24 h ou mais (ou sem data) que ja existia: avisar a operacao")
    if ped["cancelado_em"] is None:
        return plano("DUVIDA", vazios, "nao consegui ler a data do cancelamento no historico do pedido")
    if ped["cancelado_em"].replace(day=1, hour=0, minute=0) < _inicio_da_janela(agora):
        return plano("DUVIDA", vazios, "cancelamento antigo demais para conferir as movimentacoes")
    itens, motivos = [], []
    for item in ped["itens"]:
        it, motivo = _plano_do_item(sess, ped, item, agora, ignorar_movimentos)
        itens.append(it)
        if motivo:
            motivos.append(motivo)
    situacoes = [i["situacao"] for i in itens]
    motivo = "; ".join(motivos)
    if "duvida" in situacoes:
        return plano("DUVIDA", itens, motivo)
    if all(s == "nada" for s in situacoes):
        return plano("NADA", itens)
    if all(s in ("nada", "ja_devolvido") for s in situacoes):
        return plano("JA_DEVOLVIDO", itens)
    if "mover" in situacoes:
        return plano("MOVER", itens, motivo)
    return plano("DUVIDA", itens, motivo)


# --- Fila no banco (dados.db): o gancho do cancelamento registra, o robo processa ---

STATUS = ("PENDENTE", "FEITA", "PARCIAL", "NADA", "JA_DEVOLVIDO", "ANTIGO", "DUVIDA", "FALHA")


def ligado(config: dict) -> bool:
    """Chave stokki.devolucao_cancelamento.ativo; ausente = desligada."""
    try:
        return bool(((config or {}).get("stokki") or {}).get("devolucao_cancelamento", {}).get("ativo"))
    except AttributeError:
        return False


def garantir_tabela(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stokki_devolucoes ("
        "id_stokki INTEGER PRIMARY KEY, origem TEXT, ja_estava INTEGER, status TEXT, "
        "tentativas INTEGER DEFAULT 0, itens_json TEXT, erro TEXT, criado_em TEXT, atualizado_em TEXT)")


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def registrar(id_stokki: int, origem: str, config: dict, ja_estava: bool = False, conn=None) -> bool:
    """Poe o pedido cancelado na fila (PENDENTE), uma vez so (INSERT OR IGNORE). Desligado: False sem
    tocar o banco. Nunca levanta: falha vira log e False, o cancelamento segue."""
    if not ligado(config):
        return False
    propria = conn is None
    try:
        if propria:
            from stokki.sessao_uso import DB_PATH
            conn = sqlite3.connect(DB_PATH, timeout=30)
        garantir_tabela(conn)
        agora = _agora()
        cur = conn.execute(
            "INSERT OR IGNORE INTO stokki_devolucoes (id_stokki, origem, ja_estava, status, tentativas, criado_em, atualizado_em) "
            "VALUES (?, ?, ?, 'PENDENTE', 0, ?, ?)", (int(id_stokki), origem, int(bool(ja_estava)), agora, agora))
        conn.commit()
        return cur.rowcount == 1
    except Exception as e:  # noqa: BLE001 -- a fila nunca pode derrubar o cancelamento
        logger.warning(f"devolucao_estoque: nao registrou PS-{id_stokki} na fila: {e}")
        return False
    finally:
        if propria and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def pendentes(conn) -> list:
    """Linhas PENDENTE (mais antigas primeiro) como dict, com `itens` ja desserializado."""
    garantir_tabela(conn)
    cur = conn.execute("SELECT * FROM stokki_devolucoes WHERE status = 'PENDENTE' ORDER BY criado_em, id_stokki")
    nomes = [c[0] for c in cur.description]
    saida = []
    for linha in cur.fetchall():
        d = dict(zip(nomes, tuple(linha)))
        d["itens"] = json.loads(d.get("itens_json") or "[]")
        saida.append(d)
    return saida


def gravar(conn, id_stokki: int, *, status=None, itens=None, erro=None, mais_uma_tentativa=False) -> None:
    """Atualiza so o que foi informado; sempre carimba atualizado_em."""
    garantir_tabela(conn)
    campos, valores = ["atualizado_em = ?"], [_agora()]
    if status is not None:
        campos.append("status = ?")
        valores.append(status)
    if itens is not None:
        campos.append("itens_json = ?")
        valores.append(json.dumps(itens, ensure_ascii=False))
    if erro is not None:
        campos.append("erro = ?")
        valores.append(erro)
    if mais_uma_tentativa:
        campos.append("tentativas = COALESCE(tentativas, 0) + 1")
    conn.execute(f"UPDATE stokki_devolucoes SET {', '.join(campos)} WHERE id_stokki = ?", (*valores, int(id_stokki)))
    conn.commit()

