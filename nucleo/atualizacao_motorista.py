# -*- coding: utf-8 -*-
"""
nucleo/atualizacao_motorista.py

Atualização cadastral de motorista que já existe (link por WhatsApp).
Base: "pessoa" (linhas da bd_motoristas que são o mesmo motorista), ficha
vigente (cadastro APROVADO mais recente) e pendências do que falta.

Só regra e banco; nada aqui chama Vuupt, WhatsApp ou Stokki.
"""
import hashlib
import hmac
import logging
import re
import secrets
import shutil
import sqlite3
from datetime import datetime, timedelta

from nucleo import auth_motorista as auth, banco, cadastro_motorista as cad
from regras import motoristas_banco
from regras.preferencias_motoristas import _parse_bool
from regras.tipo_veiculo import tipo_por_codigo

logger = logging.getLogger("nucleo.atualizacao_motorista")

ORIGEM_ATUALIZACAO = "atualizacao"
DOCUMENTOS_MINIMOS = ("cnh", "crlv", "selfie")


def _digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def pessoas(conn: sqlite3.Connection) -> list[dict]:
    """Agrupa as linhas da bd_motoristas: mesmo CPF = mesma pessoa. Linha sem
    CPF entra na pessoa com CPF do mesmo AGENT_ID_VUUPT quando só uma pessoa
    com CPF tem esse agente (caso "CARRO 2"); com duas ou mais (dupla no mesmo
    agente) fica sozinha, sem adivinhar. Sem pessoa com CPF no agente, as linhas
    sem CPF do mesmo agente se juntam. principal_id = menor id (pode mudar
    quando uma linha entra ou sai: quem guarda vínculo procura por qualquer id)."""
    grupos: dict[tuple, list[dict]] = {}
    linhas_tabela = motoristas_banco.listar(conn)
    cpfs_do_agente: dict = {}
    for linha in linhas_tabela:
        cpf = _digitos(linha.get("CPF_MOTORISTA"))
        if cpf:
            grupos.setdefault(("cpf", cpf), []).append(linha)
            if linha.get("AGENT_ID_VUUPT") is not None:
                cpfs_do_agente.setdefault(linha["AGENT_ID_VUUPT"], set()).add(cpf)
    for linha in linhas_tabela:
        if _digitos(linha.get("CPF_MOTORISTA")):
            continue
        agent = linha.get("AGENT_ID_VUUPT")
        cpfs = cpfs_do_agente.get(agent, set()) if agent is not None else set()
        if agent is None or len(cpfs) > 1:
            chave = ("id", linha["id"])
        elif cpfs:
            chave = ("cpf", next(iter(cpfs)))
        else:
            chave = ("agent", agent)
        grupos.setdefault(chave, []).append(linha)
    resultado = []
    for linhas in grupos.values():
        linhas.sort(key=lambda l: l["id"])
        agents = sorted({l["AGENT_ID_VUUPT"] for l in linhas if l.get("AGENT_ID_VUUPT") is not None})
        cpf = next((_digitos(l["CPF_MOTORISTA"]) for l in linhas if _digitos(l.get("CPF_MOTORISTA"))), None)
        # nome da linha com CPF ("FULANO CARRO 2" sem CPF pode ter id menor e não vira o nome da pessoa)
        com_cpf = next((l for l in linhas if _digitos(l.get("CPF_MOTORISTA"))), linhas[0])
        resultado.append({
            "principal_id": linhas[0]["id"],
            "ids": [l["id"] for l in linhas],
            "agent_ids": agents,
            "cpf": cpf,
            "nome": com_cpf.get("NOME_MOTORISTA") or "",
            "ativo": any(_parse_bool(l.get("ATIVO")) for l in linhas),
            "linhas": linhas,
        })
    resultado.sort(key=lambda p: p["principal_id"])
    return resultado


def vincular_fichas(conn: sqlite3.Connection) -> int:
    """Preenche bd_motorista_id nas fichas APROVADAS sem vínculo, apontando para
    o principal_id da pessoa: primeiro pelo CPF da ficha; pelo agent_id só se
    nenhuma linha tem aquele CPF, o agente é de uma pessoa só e essa pessoa não
    tem outro CPF (a ficha de Y nunca vai pra X). Idempotente."""
    ps = pessoas(conn)
    donos_do_agent: dict = {}
    for p in ps:
        for a in p["agent_ids"]:
            donos_do_agent.setdefault(a, []).append(p)
    por_cpf = {p["cpf"]: p["principal_id"] for p in ps if p["cpf"]}
    vinculadas = 0
    rows = conn.execute("SELECT id, agent_id, cpf FROM motoristas_cadastros "
                        "WHERE status = ? AND bd_motorista_id IS NULL", (cad.STATUS_APROVADO,)).fetchall()
    for r in rows:
        cpf = _digitos(r["cpf"])
        alvo = por_cpf.get(cpf) if cpf else None
        if alvo is None and r["agent_id"] is not None:
            donos = donos_do_agent.get(r["agent_id"], [])
            if len(donos) == 1 and not (cpf and donos[0]["cpf"]):
                alvo = donos[0]["principal_id"]
        if alvo is None:
            continue
        conn.execute("UPDATE motoristas_cadastros SET bd_motorista_id = ? WHERE id = ?", (alvo, r["id"]))
        vinculadas += 1
    conn.commit()
    return vinculadas


def _ficha_vigente_ids(conn: sqlite3.Connection, ids: list[int], cpf: str | None = None) -> dict | None:
    """Aprovada mais recente ligada a uma das linhas. Pessoa com CPF: só ficha desse CPF
    (ficha de outro CPF ligada por engano a uma linha dela é ignorada)."""
    marcas = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM motoristas_cadastros WHERE status = ? AND bd_motorista_id IN ({marcas}) "
                        "ORDER BY id DESC", [cad.STATUS_APROVADO, *ids]).fetchall()
    row = next((r for r in rows if not cpf or _digitos(r["cpf"]) == cpf), None)
    return cad._montar(row) if row else None


def ficha_vigente(conn: sqlite3.Connection, principal_id: int) -> dict | None:
    pessoa = next((p for p in pessoas(conn) if p["principal_id"] == principal_id), None)
    return _ficha_vigente_ids(conn, pessoa["ids"], pessoa["cpf"]) if pessoa else None


def _primeiro(*valores):
    return next((v for v in valores if str(v or "").strip()), None)


def _codigo_tipo(valor) -> str:
    tipo = tipo_por_codigo(valor)
    return tipo.codigo if tipo else str(valor or "").strip().upper()


def pix_vigente(conn: sqlite3.Connection, pessoa: dict, ficha: dict | None) -> str | None:
    """PIX que vale: o do login do app (motoristas.chave_pix, que o motorista
    troca em "Meus dados" sem passar pela ficha); a ficha vigente é o reserva."""
    ficha = ficha or {}
    cpf = _primeiro(pessoa["cpf"], ficha.get("cpf"))
    r = conn.execute("SELECT chave_pix FROM motoristas WHERE cpf = ?", (_digitos(cpf),)).fetchone() if cpf else None
    return _primeiro(r["chave_pix"] if r else None, ficha.get("chave_pix"))


def pendencias(conn: sqlite3.Connection, pessoa: dict) -> dict:
    """O que falta na pessoa (todas as linhas juntas + ficha vigente)."""
    ficha = _ficha_vigente_ids(conn, pessoa["ids"], pessoa["cpf"]) or {}
    linhas = pessoa["linhas"]
    documentos = [t for t in cad.DOCUMENTOS if not ficha.get(f"{t}_arquivo")]

    cpf = _primeiro(pessoa["cpf"], ficha.get("cpf"))
    pix = pix_vigente(conn, pessoa, ficha)
    campos = []
    for rotulo, valor in (
        ("CPF", cpf),
        ("telefone", _primeiro(*(l.get("TELEFONE_MOTORISTA") for l in linhas), ficha.get("telefone"))),
        ("PIX", pix),
        ("nascimento", ficha.get("data_nascimento")),
        ("placa", _primeiro(*(l.get("PLACA") for l in linhas), ficha.get("placa"))),
        ("modelo do veículo", ficha.get("veiculo_modelo")),
        ("ano do veículo", ficha.get("veiculo_ano")),
    ):
        if not str(valor or "").strip():
            campos.append(rotulo)

    tipo_linha = str(linhas[0].get("TIPO_VEICULO") or "").strip()
    tipo_ficha = str(ficha.get("tipo_veiculo") or "").strip()
    return {
        "documentos": documentos,
        "campos": campos,
        "veiculo_diferente": bool(tipo_ficha and tipo_linha and _codigo_tipo(tipo_ficha) != _codigo_tipo(tipo_linha)),
        # mesma regra do candidatos_envio: sem CPF na tabela a pessoa fica fora do lote do grupo
        "falta_cpf": not pessoa["cpf"],
    }


# ── Link pessoal ──────────────────────────────────────────────────────────────
# O token só existe em claro no endereço; no banco fica o sha256. A ficha é
# uma linha de motoristas_cadastros (origem 'atualizacao') que nasce PENDENTE
# no primeiro "abrir" e vira APROVADO no "concluir" (não há aprovação).

VALIDADE_DIAS = 15
MAX_TENTATIVAS_CPF = 5
ORIGEM_REPASSE = "link_atualizacao"
_FMT = "%Y-%m-%d %H:%M:%S"
_CAMPOS_FICHA = ("cpf", "nome", "data_nascimento", "telefone", "email", "chave_pix", "placa", "tipo_veiculo",
                 "veiculo_modelo", "veiculo_ano", "refrigerado", "temp_min_c", "emite_nf", "dono_veiculo")
_ROTULOS_DOCUMENTO = {"cnh": "CNH", "crlv": "CRLV", "selfie": "selfie"}
MSG_LINK_INVALIDO = "Link inválido ou vencido. Peça um link novo à Fresh Log."
MSG_LINK_USADO = "Este link já foi usado: a ficha foi enviada."


class LinkInvalido(Exception):
    """404 não existe/cancelado, 410 vencido ou concluído, 403 CPF errado,
    409 CPF de outro motorista, 429 tentativas de CPF esgotadas."""

    def __init__(self, mensagem: str, codigo: int = 404):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.codigo = codigo


def _ts(agora: datetime | None) -> str:
    return (agora or datetime.now()).strftime(_FMT)


def _hash(token) -> str:
    return hashlib.sha256(str(token or "").encode()).hexdigest()


def _pessoa_de(conn: sqlite3.Connection, bd_motorista_id: int) -> dict | None:
    return next((p for p in pessoas(conn) if bd_motorista_id in p["ids"]), None)


def _ids_da_pessoa(conn: sqlite3.Connection, bd_motorista_id: int) -> list[int]:
    """Todos os ids da pessoa daquela linha: o link pode ter sido guardado numa
    linha que deixou de ser a principal (linha que entrou ou saiu da pessoa)."""
    pessoa = _pessoa_de(conn, bd_motorista_id)
    return pessoa["ids"] if pessoa else [bd_motorista_id]


def descartar_rascunhos(conn: sqlite3.Connection, link_ids: list[int]) -> list[int]:
    """Rascunho PENDENTE (origem atualização) dos links cancelados vira
    SUBSTITUIDO, sem commit: parado, seguraria o CPF (o auto-cadastro do app
    recusa com 409 quem tem cadastro PENDENTE). Devolve os ids pra apagar_pastas
    depois do commit."""
    if not link_ids:
        return []
    marcas = ",".join("?" * len(link_ids))
    ids = [r[0] for r in conn.execute(
        "SELECT c.id FROM motoristas_links_atualizacao l JOIN motoristas_cadastros c ON c.id = l.cadastro_id "
        f"WHERE l.id IN ({marcas}) AND c.status = ? AND c.origem = ?",
        (*link_ids, cad.STATUS_PENDENTE, ORIGEM_ATUALIZACAO))]
    for cadastro_id in ids:
        conn.execute("UPDATE motoristas_cadastros SET status = ? WHERE id = ? AND status = ?",
                     (cad.STATUS_SUBSTITUIDO, cadastro_id, cad.STATUS_PENDENTE))
    return ids


def apagar_pastas(cadastro_ids: list[int]) -> None:
    """Cópia dos documentos de rascunho descartado (só depois do commit)."""
    for cadastro_id in cadastro_ids:
        shutil.rmtree(cad.pasta_cadastros() / str(cadastro_id), ignore_errors=True)


def _link(conn: sqlite3.Connection, token: str) -> dict:
    row = conn.execute("SELECT * FROM motoristas_links_atualizacao WHERE token_hash = ?", (_hash(token),)).fetchone()
    if not row or row["cancelado_em"]:
        raise LinkInvalido(MSG_LINK_INVALIDO, 404)
    return dict(row)


def _conferir_aberto(link: dict, ts: str) -> None:
    if link["concluido_em"]:
        raise LinkInvalido(MSG_LINK_USADO, 410)
    if link["expira_em"] <= ts:
        raise LinkInvalido(MSG_LINK_INVALIDO, 410)


def _rascunho(conn: sqlite3.Connection, link: dict, chave_envio: str):
    row = conn.execute("SELECT * FROM motoristas_cadastros WHERE id = ?",
                       (link["cadastro_id"],)).fetchone() if link["cadastro_id"] else None
    if not row or not hmac.compare_digest(str(row["chave_envio"]), str(chave_envio or "")):
        raise LinkInvalido("Ficha não encontrada. Abra o link de novo.", 404)
    return row


def exigir_link_vivo_do_rascunho(conn: sqlite3.Connection, cadastro_id: int, agora: datetime | None = None) -> None:
    """Upload de documento de um rascunho de atualização: o link dono dele
    (motoristas_links_atualizacao.cadastro_id) tem de existir e estar vivo.
    404 sem link ou cancelado; 410 vencido ou concluído."""
    row = conn.execute("SELECT * FROM motoristas_links_atualizacao WHERE cadastro_id = ?", (cadastro_id,)).fetchone()
    if not row or row["cancelado_em"]:
        raise LinkInvalido(MSG_LINK_INVALIDO, 404)
    _conferir_aberto(dict(row), _ts(agora))


def _resposta(conn: sqlite3.Connection, cadastro_id: int) -> dict:
    row = conn.execute("SELECT * FROM motoristas_cadastros WHERE id = ?", (cadastro_id,)).fetchone()
    return {"cadastro_id": row["id"], "chave_envio": row["chave_envio"],
            "dados": {c: row[c] for c in _CAMPOS_FICHA},
            "documentos": {t: bool(row[f"{t}_arquivo"]) for t in cad.DOCUMENTOS}}


def gerar_link(conn: sqlite3.Connection, principal_id: int, usuario: str, *, validade_dias: int = VALIDADE_DIAS,
               piloto: bool = False, agora: datetime | None = None) -> dict:
    """Link novo pra pessoa; cancela os anteriores dela ainda não concluídos.
    Devolve {id, token, expira_em}: o token em claro só sai aqui."""
    agora = agora or datetime.now()
    pessoa = _pessoa_de(conn, principal_id)
    if not pessoa:
        raise LinkInvalido("Motorista não encontrado.", 404)
    token = secrets.token_urlsafe(32)
    criado, expira = agora.strftime(_FMT), (agora + timedelta(days=validade_dias)).strftime(_FMT)
    # link real cancela tudo (inclusive o piloto); piloto só cancela piloto (desenho, seção 4);
    # por qualquer id da pessoa (a principal pode ter mudado depois do link anterior)
    marcas = ",".join("?" * len(pessoa["ids"]))
    onde = (f"bd_motorista_id IN ({marcas}) AND concluido_em IS NULL AND cancelado_em IS NULL"
            + (" AND piloto = 1" if piloto else ""))
    cancelados = [r[0] for r in conn.execute(f"SELECT id FROM motoristas_links_atualizacao WHERE {onde}",
                                             pessoa["ids"])]
    conn.execute(f"UPDATE motoristas_links_atualizacao SET cancelado_em = ? WHERE {onde}", (criado, *pessoa["ids"]))
    rascunhos = descartar_rascunhos(conn, cancelados)
    cur = conn.execute("INSERT INTO motoristas_links_atualizacao (bd_motorista_id, token_hash, criado_em, criado_por, "
                       "expira_em, piloto) VALUES (?, ?, ?, ?, ?, ?)",
                       (pessoa["principal_id"], _hash(token), criado, usuario, expira, int(bool(piloto))))
    conn.commit()
    apagar_pastas(rascunhos)
    return {"id": cur.lastrowid, "token": token, "expira_em": expira}


def link_em_aberto(conn: sqlite3.Connection, principal_id: int, agora: datetime | None = None) -> dict | None:
    """Link real em aberto da pessoa (procura por qualquer id dela)."""
    ids = _ids_da_pessoa(conn, principal_id)
    row = conn.execute(f"SELECT * FROM motoristas_links_atualizacao WHERE bd_motorista_id IN ({','.join('?' * len(ids))}) "
                       "AND piloto = 0 AND cancelado_em IS NULL AND concluido_em IS NULL AND expira_em > ? "
                       "ORDER BY id DESC LIMIT 1", (*ids, _ts(agora))).fetchone()
    return dict(row) if row else None


def estado_link(conn: sqlite3.Connection, principal_id: int, agora: datetime | None = None) -> str | None:
    """Estado do link mais recente da pessoa, por qualquer id dela (piloto não conta; cancelado = None)."""
    ids = _ids_da_pessoa(conn, principal_id)
    row = conn.execute(f"SELECT * FROM motoristas_links_atualizacao WHERE bd_motorista_id IN ({','.join('?' * len(ids))}) "
                       "AND piloto = 0 ORDER BY id DESC LIMIT 1", ids).fetchone()
    if not row:
        return None
    if row["concluido_em"]:
        return "concluido"
    if row["cancelado_em"]:
        return None
    if row["expira_em"] <= _ts(agora):
        return "vencido"
    return "aberto" if row["aberto_em"] else "enviado"


MSG_CPF_DE_OUTRO = "Este CPF não pode ser usado neste link. Fale com a Fresh Log."


def _cpf_conhecido(conn: sqlite3.Connection, pessoa: dict) -> str | None:
    """CPF que a pessoa já tem: o da tabela ou, sem ele, o do login do app no
    mesmo agent_id."""
    if pessoa["cpf"] or not pessoa["agent_ids"]:
        return pessoa["cpf"]
    marcas = ",".join("?" * len(pessoa["agent_ids"]))
    row = conn.execute(f"SELECT cpf FROM motoristas WHERE agent_id IN ({marcas}) ORDER BY cpf LIMIT 1",
                       pessoa["agent_ids"]).fetchone()
    return _digitos(row["cpf"]) if row else None


def _cpf_livre(conn: sqlite3.Connection, pessoa: dict, cpf: str, ts: str) -> bool:
    """CPF sem dono fora da pessoa: nenhuma linha de outra pessoa, nenhum login
    de outro agent_id, nenhum rascunho com link em aberto de outra pessoa."""
    if any(l["id"] not in pessoa["ids"] for l in motoristas_banco.por_cpf(conn, cpf)):
        return False
    login = auth.buscar_motorista(conn, cpf)
    if login and login.get("agent_id") not in pessoa["agent_ids"]:
        return False
    marcas = ",".join("?" * len(pessoa["ids"]))
    return conn.execute(
        "SELECT 1 FROM motoristas_links_atualizacao l JOIN motoristas_cadastros c ON c.id = l.cadastro_id "
        f"WHERE c.cpf = ? AND c.status = ? AND l.bd_motorista_id NOT IN ({marcas}) AND l.cancelado_em IS NULL "
        "AND l.concluido_em IS NULL AND l.expira_em > ? LIMIT 1",
        (cpf, cad.STATUS_PENDENTE, *pessoa["ids"], ts)).fetchone() is None


def _erro_cpf(conn: sqlite3.Connection, pessoa: dict, rascunho, cpf: str, ts: str) -> LinkInvalido | None:
    """CPF da tabela, do login no mesmo agent_id ou do rascunho já aberto: o
    digitado tem de bater. Sem nenhum: vale um CPF válido e livre."""
    conhecido = _cpf_conhecido(conn, pessoa) or (_digitos(rascunho["cpf"]) if rascunho else None)
    if conhecido:
        return None if hmac.compare_digest(cpf, conhecido) else LinkInvalido("CPF não confere com o cadastro.", 403)
    if not cad.cpf_valido(cpf):
        return LinkInvalido("CPF inválido.", 403)
    if not _cpf_livre(conn, pessoa, cpf, ts):
        return LinkInvalido(MSG_CPF_DE_OUTRO, 409)
    return None


def _criar_rascunho(conn: sqlite3.Connection, pessoa: dict, cpf: str, ts: str) -> int:
    """Ficha PENDENTE pré-preenchida com as linhas da pessoa + ficha vigente;
    os documentos da vigente são copiados pra pasta da nova."""
    ficha = _ficha_vigente_ids(conn, pessoa["ids"], pessoa["cpf"]) or {}
    linhas = pessoa["linhas"]
    login = auth.buscar_motorista(conn, cpf) or {}
    valores = {
        "cpf": cpf,
        "nome": _primeiro(pessoa["nome"], ficha.get("nome")) or "",
        "telefone": _primeiro(*(l.get("TELEFONE_MOTORISTA") for l in linhas), ficha.get("telefone")),
        "email": _primeiro(*(l.get("EMAIL_MOTORISTA") for l in linhas), ficha.get("email")),
        # PIX vigente = o do login (trocado em "Meus dados"); a ficha é o reserva
        "chave_pix": _primeiro(login.get("chave_pix"), ficha.get("chave_pix")),
        "placa": _primeiro(*(l.get("PLACA") for l in linhas), ficha.get("placa")),
        "tipo_veiculo": _primeiro(ficha.get("tipo_veiculo"), linhas[0].get("TIPO_VEICULO")),
        **{c: ficha.get(c) for c in ("data_nascimento", "veiculo_modelo", "veiculo_ano", "refrigerado",
                                     "temp_min_c", "emite_nf")},
        "dono_veiculo": int(ficha.get("dono_veiculo", True)),
        "chave_envio": secrets.token_urlsafe(24), "status": cad.STATUS_PENDENTE, "origem": ORIGEM_ATUALIZACAO,
        "bd_motorista_id": pessoa["principal_id"], "criado_em": ts,
    }
    cur = conn.execute(f"INSERT INTO motoristas_cadastros ({', '.join(valores)}) "
                       f"VALUES ({', '.join('?' * len(valores))})", list(valores.values()))
    cadastro_id = cur.lastrowid
    for tipo in cad.DOCUMENTOS if ficha else ():
        origem = cad.caminho_documento(conn, ficha["id"], tipo)
        if origem:
            pasta = cad.pasta_cadastros() / str(cadastro_id)
            pasta.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origem, pasta / origem.name)
            conn.execute(f"UPDATE motoristas_cadastros SET {tipo}_arquivo = ? WHERE id = ?", (origem.name, cadastro_id))
    return cadastro_id


def abrir(conn: sqlite3.Connection, token: str, cpf: str, agora: datetime | None = None) -> dict:
    """Primeiro passo da página: confere o CPF e devolve a ficha (a mesma em
    toda abertura). CPF errado conta tentativa; na 5ª o link é cancelado."""
    ts = _ts(agora)
    cpf = auth.normalizar_cpf(cpf)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")   # duas aberturas ao mesmo tempo não criam dois rascunhos
    try:
        link = _link(conn, token)
        _conferir_aberto(link, ts)
        pessoa = _pessoa_de(conn, link["bd_motorista_id"])
        if not pessoa:
            raise LinkInvalido(MSG_LINK_INVALIDO, 404)
        rascunho = conn.execute("SELECT * FROM motoristas_cadastros WHERE id = ? AND status = ?",
                                (link["cadastro_id"], cad.STATUS_PENDENTE)).fetchone() if link["cadastro_id"] else None
        erro = _erro_cpf(conn, pessoa, rascunho, cpf, ts)
        if erro:
            tentativas = link["tentativas_cpf"] + 1
            esgotou = tentativas >= MAX_TENTATIVAS_CPF
            conn.execute("UPDATE motoristas_links_atualizacao SET tentativas_cpf = ?, cancelado_em = ? WHERE id = ?",
                         (tentativas, ts if esgotou else None, link["id"]))
            rascunhos = descartar_rascunhos(conn, [link["id"]]) if esgotou else []
            conn.commit()
            if esgotou:
                apagar_pastas(rascunhos)
                raise LinkInvalido("Muitas tentativas com CPF errado. Peça um link novo à Fresh Log.", 429)
            raise erro
        if rascunho and _digitos(rascunho["cpf"]) != cpf:
            # a equipe preencheu o CPF depois da 1ª abertura: o rascunho passa a levar o conferido agora
            conn.execute("UPDATE motoristas_cadastros SET cpf = ? WHERE id = ?", (cpf, rascunho["id"]))
        cadastro_id = rascunho["id"] if rascunho else _criar_rascunho(conn, pessoa, cpf, ts)
        conn.execute("UPDATE motoristas_links_atualizacao SET cadastro_id = ?, aberto_em = COALESCE(aberto_em, ?) "
                     "WHERE id = ?", (cadastro_id, ts, link["id"]))
        if not rascunho:
            # PIX pré-preenchido = referência: no concluir o login só muda se o motorista digitou outro
            pix = conn.execute("SELECT chave_pix FROM motoristas_cadastros WHERE id = ?", (cadastro_id,)).fetchone()[0]
            conn.execute("UPDATE motoristas_links_atualizacao SET pix_inicial = ? WHERE id = ?",
                         (str(pix or "").strip(), link["id"]))
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return _resposta(conn, cadastro_id)


def gravar(conn: sqlite3.Connection, token: str, chave_envio: str, dados: dict) -> dict:
    """Dados da ficha (sem zonas, dias, posição e celular: são da operação)."""
    link = _link(conn, token)
    _conferir_aberto(link, _ts(None))
    row = _rascunho(conn, link, chave_envio)
    valores = cad._validar_dados_pessoa_veiculo(dados)
    valores["email"] = cad.validar_email((dados or {}).get("email"))
    cur = conn.execute(f"UPDATE motoristas_cadastros SET {', '.join(f'{c} = ?' for c in valores)} "
                       "WHERE id = ? AND status = ?", (*valores.values(), row["id"], cad.STATUS_PENDENTE))
    conn.commit()
    if cur.rowcount != 1:
        raise LinkInvalido(MSG_LINK_USADO, 410)
    return _resposta(conn, row["id"])


def _resultado(ficha) -> dict:
    return {"ok": True, "pendentes": [t for t in cad.DOCUMENTOS if not ficha[f"{t}_arquivo"]]}


def _repassar(conn: sqlite3.Connection, pessoa: dict, ficha: dict, *, repassar_pix: bool = False) -> None:
    """`ficha` = valores já normalizados pelo validador. CPF (só onde está
    vazio), telefone e e-mail em todas as linhas da pessoa; placa só se ela
    tem uma linha (com 2+ carros não dá pra saber qual). Tipo de veículo,
    zonas, dias, posição e carga seca nunca. O que mudou vai ao login do app
    como na edição pelo painel; o PIX também, se o login é desta pessoa e
    `repassar_pix` (o motorista digitou outro PIX no formulário).
    Linha que falhar fica no log e não impede as outras."""
    from regras.cadastro_motoristas import _espelhar, _repassar_ao_login
    cpf = _digitos(ficha["cpf"])
    comuns = {coluna: ficha[campo] for coluna, campo in (("TELEFONE_MOTORISTA", "telefone"),
                                                          ("EMAIL_MOTORISTA", "email")) if ficha[campo]}
    mudou = False
    for linha in pessoa["linhas"]:
        campos = dict(comuns)
        if not linha.get("CPF_MOTORISTA"):   # mesmo critério do motoristas_banco.atualizar
            campos["CPF_MOTORISTA"] = cpf
        if len(pessoa["linhas"]) == 1 and ficha["placa"]:
            campos["PLACA"] = ficha["placa"]
        try:
            mudancas = motoristas_banco.atualizar(conn, linha["id"], campos, ORIGEM_REPASSE)
        except Exception:
            logger.exception("Falha ao repassar a atualizacao na linha id=%s da bd_motoristas", linha["id"])
            if conn.in_transaction:
                conn.rollback()   # o que essa linha escreveu não pode sair no commit da próxima
            continue
        if mudancas:
            mudou = True
            atual = dict(conn.execute("SELECT * FROM bd_motoristas WHERE id = ?", (linha["id"],)).fetchone())
            try:
                _repassar_ao_login(conn, atual, mudancas)
            except Exception:
                logger.exception("Falha ao repassar a atualizacao da linha id=%s ao login do app", linha["id"])
                if conn.in_transaction:
                    conn.rollback()
    login = auth.buscar_motorista(conn, cpf)
    dono = login and (login.get("agent_id") in pessoa["agent_ids"]
                      or any(l["id"] in pessoa["ids"] for l in motoristas_banco.por_cpf(conn, cpf)))
    if dono and repassar_pix and ficha["chave_pix"] and ficha["chave_pix"] != login.get("chave_pix"):
        agora = banco.agora()
        conn.execute("UPDATE motoristas SET chave_pix = ?, atualizado_em = ? WHERE cpf = ?",
                     (ficha["chave_pix"], agora, cpf))
        conn.execute("INSERT INTO motoristas_alteracoes (cpf, campo, valor_antigo, valor_novo, alterado_em) "
                     "VALUES (?, 'chave_pix', ?, ?, ?)", (cpf, login.get("chave_pix"), ficha["chave_pix"], agora))
        conn.commit()
    if mudou:
        _espelhar(conn)


def concluir(conn: sqlite3.Connection, config: dict, token: str, chave_envio: str,
             agora: datetime | None = None) -> dict:
    """Botão "Enviar": exige dados válidos + CNH, CRLV e selfie; a ficha vira
    APROVADO e os repasses rodam uma vez só (concluir de novo devolve o
    mesmo resultado sem repetir). ANTT e residência voltam em `pendentes`."""
    ts = _ts(agora)
    link = _link(conn, token)
    if not link["concluido_em"]:
        _conferir_aberto(link, ts)
    ficha = _rascunho(conn, link, chave_envio)
    if not link["concluido_em"] and ficha["status"] != cad.STATUS_PENDENTE:
        link = _link(conn, token)   # outro "Enviar" pode ter concluído entre as duas leituras
        if not link["concluido_em"]:
            # rascunho encerrado sem este link concluir (ex.: SUBSTITUIDO pelo cadastro novo do mesmo CPF)
            raise LinkInvalido(MSG_LINK_INVALIDO, 410)
    if link["concluido_em"]:
        return _resultado(ficha)
    faltam = [t for t in DOCUMENTOS_MINIMOS if not ficha[f"{t}_arquivo"]]
    if faltam:
        raise cad.CadastroInvalido("Faltam documentos: " + ", ".join(_ROTULOS_DOCUMENTO[t] for t in faltam) + ".")
    valores = cad._validar_dados_pessoa_veiculo(dict(ficha))
    valores["email"] = cad.validar_email(ficha["email"])
    cpf = _digitos(ficha["cpf"])
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")   # conferência do CPF e aprovação juntas
    try:
        pessoa = _pessoa_de(conn, link["bd_motorista_id"])
        if not pessoa:
            raise LinkInvalido(MSG_LINK_INVALIDO, 404)
        # o CPF foi conferido no abrir, até 15 dias atrás: confere de novo contra o estado de agora
        conhecido = _cpf_conhecido(conn, pessoa)
        if (conhecido and conhecido != cpf) or (not conhecido and not _cpf_livre(conn, pessoa, cpf, ts)):
            raise LinkInvalido(MSG_CPF_DE_OUTRO, 409)
        # PIX só vai ao login se o motorista digitou outro no formulário (comparado com o que veio
        # pré-preenchido; sem referência gravada, não mexe). Sem mudança, a ficha fica com o PIX
        # atual do login (ele pode ter trocado no app durante o link): ficha e login não divergem.
        repassar_pix = link.get("pix_inicial") is not None and (valores["chave_pix"] or "") != link["pix_inicial"]
        login = auth.buscar_motorista(conn, cpf)
        if not repassar_pix and login and str(login.get("chave_pix") or "").strip():
            valores["chave_pix"] = login["chave_pix"]
        # condicional: dois "Enviar" ao mesmo tempo, só um grava e repassa
        cur = conn.execute(f"UPDATE motoristas_cadastros SET {', '.join(f'{c} = ?' for c in valores)}, status = ?, "
                           "enviado_em = ?, concluido_em = ?, revisado_em = ?, revisado_por = ? WHERE id = ? AND status = ?",
                           (*valores.values(), cad.STATUS_APROVADO, ts, ts, ts, ORIGEM_REPASSE, ficha["id"],
                            cad.STATUS_PENDENTE))
        ganhou = cur.rowcount == 1
        if ganhou:
            conn.execute("UPDATE motoristas_links_atualizacao SET concluido_em = ? WHERE id = ?", (ts, link["id"]))
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    if ganhou:
        _repassar(conn, pessoa, {**valores, "cpf": cpf}, repassar_pix=repassar_pix)
        logger.info("ficha de atualizacao #%s concluida (bd_motoristas id=%s)", ficha["id"], pessoa["principal_id"])
    return _resultado(ficha)
