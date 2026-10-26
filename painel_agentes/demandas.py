# -*- coding: utf-8 -*-
"""
painel_agentes/demandas.py

Tela Demandas (Hugo, 25/10/2026): tarefas pro time, dirigidas a uma
EQUIPE e, se quiser, a uma pessoa. Substitui as Demandas do Fresh Hub
antigo (Lovable). Plano: ~/.claude/plans/vamos-desenvolver-juntos-a-calm-sedgewick.md

Regras combinadas com o Hugo:
- equipes = a lista fixa acesso_telas.EQUIPES (Hugo, 25/10: a mesma do campo
  Equipe de cada login nominal na /acessos); membro = login nominal ativo com
  aquela equipe. Na aba Equipes (so o total) marca lideres, niveis de login
  fixo vinculados (ex.: Estoque <- galpao), grupo do aviso e se esta ativa;
- cria demanda: total e lideres (pra qualquer equipe ativa);
- cada um ve o que e dele, das equipes dele e o que criou; total ve tudo;
  login fixo ve as equipes vinculadas ao nivel e, pra agir, informa o nome;
- fluxo ABERTA -> EM_ANDAMENTO -> CONCLUIDA (+ CANCELADA; reabre);
- todo evento (criacao, comentario, cada campo mudado) fica em
  demandas_eventos: e a linha do tempo e o ponteiro do aviso sonoro;
- aviso no grupo interno do /atendimento so com numero e equipe (os
  grupos sao abertos a todo o Atendimento; o titulo fica na tela).

Tabelas em dados/dados.db. Rotas por `registrar(app, ...)`, chamado no
fim de painel_agentes.py.
"""
import json
import logging
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from flask import Response, abort, g, jsonify, render_template, request, session

import acesso_telas
import grupos_equipe

logger = logging.getLogger("painel.demandas")

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"

STATUS = {"ABERTA": "Aberta", "EM_ANDAMENTO": "Em andamento", "CONCLUIDA": "Concluída", "CANCELADA": "Cancelada"}
ABERTOS = ("ABERTA", "EM_ANDAMENTO")
PRIORIDADES = {"BAIXA": "Baixa", "NORMAL": "Normal", "ALTA": "Alta", "URGENTE": "Urgente"}
ORDEM_PRIORIDADE = {"URGENTE": 0, "ALTA": 1, "NORMAL": 2, "BAIXA": 3}
# Niveis de login fixo que uma equipe pode vincular (total ja ve tudo;
# logistica so existe em login nominal).
NIVEIS_FIXOS = ("operador", "leitura", "expedicao", "galpao", "atendimento")
NIVEIS_TELA = acesso_telas.NIVEIS
NOME_GRUPO = "Demandas"
AUTOR_AVISO = "Demandas"
TITULO_MIN, TITULO_MAX = 3, 120
TEXTO_MAX = 4000
_PREFIXO_PS = re.compile(r"^PS\s*[.\-_ ]*\s*(\d+)$")


class ErroDemanda(Exception):
    """Mensagem pro usuario (vira 400 na API)."""


class SemPermissao(ErroDemanda):
    """Vira 403."""


class PrecisaNome(ErroDemanda):
    """Login fixo sem nome informado: a tela pede o nome."""


def conectar(caminho=None) -> sqlite3.Connection:
    conn = sqlite3.connect(caminho or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS demandas_equipes (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            nome           TEXT NOT NULL,
            descricao      TEXT NOT NULL DEFAULT '',
            ativo          INTEGER NOT NULL DEFAULT 1,
            grupo_id       INTEGER,
            criado_por     TEXT,
            criado_em      TEXT NOT NULL,
            atualizado_por TEXT,
            atualizado_em  TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_demandas_equipes_nome ON demandas_equipes (lower(nome));
        CREATE TABLE IF NOT EXISTS demandas_equipe_lideres (
            equipe_id   INTEGER NOT NULL,
            usuario     TEXT NOT NULL,
            marcado_por TEXT,
            marcado_em  TEXT NOT NULL,
            PRIMARY KEY (equipe_id, usuario)
        );
        CREATE TABLE IF NOT EXISTS demandas_equipe_niveis (
            equipe_id INTEGER NOT NULL,
            nivel     TEXT NOT NULL,
            PRIMARY KEY (equipe_id, nivel)
        );
        CREATE INDEX IF NOT EXISTS idx_demandas_niveis_nivel ON demandas_equipe_niveis (nivel);
        CREATE TABLE IF NOT EXISTS demandas (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo          TEXT NOT NULL,
            descricao       TEXT NOT NULL DEFAULT '',
            equipe_id       INTEGER NOT NULL,
            responsavel     TEXT,
            status          TEXT NOT NULL DEFAULT 'ABERTA',
            prioridade      TEXT NOT NULL DEFAULT 'NORMAL',
            prazo           TEXT,
            pedido_codigo   TEXT,
            embarcador_cnpj TEXT,
            embarcador_nome TEXT,
            origem          TEXT NOT NULL DEFAULT 'manual',
            origem_ref      TEXT,
            criado_por      TEXT NOT NULL,
            criado_por_nome TEXT,
            criado_em       TEXT NOT NULL,
            atualizado_em   TEXT NOT NULL,
            concluido_em    TEXT,
            concluido_por   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_demandas_equipe ON demandas (equipe_id, status);
        CREATE INDEX IF NOT EXISTS idx_demandas_responsavel ON demandas (responsavel, status);
        CREATE INDEX IF NOT EXISTS idx_demandas_prazo ON demandas (status, prazo);
        CREATE INDEX IF NOT EXISTS idx_demandas_pedido ON demandas (pedido_codigo);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_demandas_origem ON demandas (origem, origem_ref)
            WHERE origem_ref IS NOT NULL;
        CREATE TABLE IF NOT EXISTS demandas_eventos (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            demanda_id INTEGER NOT NULL,
            tipo       TEXT NOT NULL,
            campo      TEXT,
            de         TEXT,
            para       TEXT,
            texto      TEXT,
            usuario    TEXT NOT NULL,
            autor      TEXT NOT NULL,
            criado_em  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_demandas_eventos ON demandas_eventos (demanda_id, id);
    """)
    # Equipes = lista fixa do login (acesso_telas.EQUIPES): cria a que faltar.
    # painel_usuarios mora no mesmo banco e e de onde saem os membros.
    acesso_telas.conectar(caminho or DB_PATH).close()
    for nome in acesso_telas.EQUIPES:
        if not conn.execute("SELECT 1 FROM demandas_equipes WHERE lower(nome) = lower(?)", (nome,)).fetchone():
            conn.execute("INSERT INTO demandas_equipes (nome, criado_por, criado_em) VALUES (?, 'sistema', ?)",
                         (nome, _agora()))
    conn.commit()
    return conn


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _quando(valor: str | None) -> str:
    """'14:32' se hoje, '18/10 14:32' se outro dia."""
    if not valor:
        return ""
    try:
        d = datetime.strptime(valor, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return valor
    return d.strftime("%H:%M") if d.date() == datetime.now().date() else d.strftime("%d/%m %H:%M")


def _texto(valor, nome: str, minimo: int, maximo: int, uma_linha: bool = False) -> str:
    texto = " ".join(str(valor or "").split()) if uma_linha else str(valor or "").strip()
    if not minimo <= len(texto) <= maximo:
        raise ErroDemanda(f"{nome} precisa ter de {minimo} a {maximo} letras." if minimo
                          else f"{nome} muito longo (máximo {maximo} letras).")
    return texto


def normalizar_pedido(valor) -> str | None:
    """'#ps 36327' / 'PS.36327' / '36327' -> 'PS-36327'; o resto
    (PS-1-R2, NF do cliente com letras) fica como veio, em maiusculas e
    sem '#'."""
    from nucleo.normalizacao import normalizar_codigo
    texto = normalizar_codigo(valor)
    if not texto:
        return None
    m = _PREFIXO_PS.match(texto)
    if m:
        return f"PS-{m.group(1)}"
    return f"PS-{texto}" if texto.isdigit() else texto


def _prazo(valor) -> str | None:
    texto = str(valor or "").strip()
    if not texto:
        return None
    try:
        return date.fromisoformat(texto).isoformat()
    except ValueError:
        raise ErroDemanda("Prazo inválido.") from None


# ── Identidade e permissao ────────────────────────────────────────────────────

def identidade(conn, usuario: str, nivel: str, telas, nome_informado: str | None = None) -> dict:
    """Quem esta usando a tela. `nivel` e o da sessao (o real: o acesso
    personalizado vira "total" so dentro das telas liberadas, por isso
    admin exige telas is None)."""
    conta = acesso_telas.conta(usuario) if usuario else None
    nominal = conta is not None
    admin = nivel == "total" and telas is None
    membro_de = set()
    if nominal and conta.get("ativo") and conta.get("equipe"):
        membro_de = {r["id"] for r in conn.execute("SELECT id FROM demandas_equipes WHERE nome = ?", (conta["equipe"],))}
    lider_de = {r["equipe_id"] for r in conn.execute(
        "SELECT equipe_id FROM demandas_equipe_lideres WHERE usuario = ?", (usuario,))} & membro_de
    vinculadas = set() if nominal else {r["equipe_id"] for r in conn.execute(
        "SELECT equipe_id FROM demandas_equipe_niveis WHERE nivel = ?", (nivel,))}
    if nominal:
        autor = conta["nome"] or usuario
    else:
        autor = (nome_informado or "").strip() or (usuario if admin else None)
    return {"usuario": usuario, "nominal": nominal, "admin": admin, "nivel": nivel,
            "membro_de": membro_de, "lider_de": lider_de, "vinculadas": vinculadas,
            "equipes": membro_de | vinculadas, "autor": autor, "pode_criar": admin or bool(lider_de)}


def _filtro_visivel(ident: dict) -> tuple[str, list]:
    if ident["admin"]:
        return "1 = 1", []
    equipes = sorted(ident["equipes"])
    marcas = ",".join("?" * len(equipes)) or "NULL"
    return (f"(d.responsavel = ? OR d.criado_por = ? OR d.equipe_id IN ({marcas}))",
            [ident["usuario"], ident["usuario"], *equipes])


def _pode_ver(ident: dict, d) -> bool:
    return (ident["admin"] or d["responsavel"] == ident["usuario"] or d["criado_por"] == ident["usuario"]
            or d["equipe_id"] in ident["equipes"])


def acoes(ident: dict, d) -> list[str]:
    """Botoes que essa pessoa pode usar nessa demanda (a tela desenha so
    esses; o servidor confere de novo em cada acao)."""
    if not _pode_ver(ident, d):
        return []
    status = d["status"]
    gestor = ident["admin"] or d["criado_por"] == ident["usuario"] or d["equipe_id"] in ident["lider_de"]
    executor = ident["admin"] or d["equipe_id"] in ident["equipes"] or d["responsavel"] == ident["usuario"]
    lista = ["comentar"]
    if status in ABERTOS and gestor:
        lista += ["editar", "cancelar"]
    if status == "ABERTA" and executor:
        lista.append("iniciar")
    if status == "EM_ANDAMENTO" and executor:
        lista.append("concluir")
    if status not in ABERTOS and gestor:
        lista.append("reabrir")
    if (status in ABERTOS and ident["nominal"] and d["equipe_id"] in ident["membro_de"]
            and d["responsavel"] != ident["usuario"]):
        lista.append("assumir")
    return lista


def _autor(ident: dict) -> str:
    if not ident["autor"]:
        raise PrecisaNome("Informe seu nome antes de mexer nas demandas.")
    return ident["autor"]


# ── Equipes ───────────────────────────────────────────────────────────────────

_SQL_MEMBROS = ("SELECT u.usuario, COALESCE(NULLIF(u.nome, ''), u.usuario) AS nome FROM painel_usuarios u "
                "JOIN demandas_equipes q ON q.nome = u.equipe WHERE q.id = ? AND u.ativo = 1 "
                "ORDER BY lower(COALESCE(NULLIF(u.nome, ''), u.usuario))")


def _eh_membro(conn, equipe_id: int, usuario: str) -> bool:
    return any(r["usuario"] == usuario for r in conn.execute(_SQL_MEMBROS, (equipe_id,)))


def listar_equipes(conn, so_ativas: bool = False) -> list[dict]:
    ordem = {nome: i for i, nome in enumerate(acesso_telas.EQUIPES)}
    equipes = []
    for e in conn.execute("SELECT * FROM demandas_equipes" + (" WHERE ativo = 1" if so_ativas else "")):
        lideres = {r["usuario"] for r in conn.execute(
            "SELECT usuario FROM demandas_equipe_lideres WHERE equipe_id = ?", (e["id"],))}
        membros = [{"usuario": m["usuario"], "nome": m["nome"], "lider": m["usuario"] in lideres}
                   for m in conn.execute(_SQL_MEMBROS, (e["id"],))]
        niveis = [r["nivel"] for r in conn.execute("SELECT nivel FROM demandas_equipe_niveis WHERE equipe_id = ? "
                                                    "ORDER BY nivel", (e["id"],))]
        equipes.append({"id": e["id"], "nome": e["nome"], "descricao": e["descricao"], "ativo": bool(e["ativo"]),
                        "grupo_id": e["grupo_id"], "membros": membros, "niveis": niveis})
    # Ordem da lista do login; equipe que saiu da lista vai pro fim.
    equipes.sort(key=lambda x: (not x["ativo"], ordem.get(x["nome"], 99), x["nome"].lower()))
    return equipes


def salvar_equipe(conn, ident: dict, equipe_id: int, dados: dict) -> dict:
    """Regrava o que a aba Equipes controla: descricao, ativa, grupo do
    aviso, lideres (precisam ser membros) e niveis de login fixo. Nome e
    membros vem da /acessos (campo Equipe de cada login)."""
    if not ident["admin"]:
        raise SemPermissao("Só o acesso total mexe nas equipes.")
    e = conn.execute("SELECT * FROM demandas_equipes WHERE id = ?", (equipe_id,)).fetchone()
    if not e:
        raise ErroDemanda("Equipe não encontrada.")
    descricao = _texto(dados.get("descricao"), "A descrição", 0, 500)
    ativo = 1 if dados.get("ativo", True) else 0
    grupo_id = int(dados["grupo_id"]) if dados.get("grupo_id") else None
    if grupo_id and not grupos_equipe.buscar_grupo(conn, grupo_id):
        raise ErroDemanda("Grupo do /atendimento não encontrado.")
    membros = {r["usuario"] for r in conn.execute(_SQL_MEMBROS, (equipe_id,))}
    lideres = sorted({str(u or "").strip() for u in (dados.get("lideres") or [])} - {""})
    fora = [u for u in lideres if u not in membros]
    if fora:
        raise ErroDemanda(f"{', '.join(fora)} não é da equipe {e['nome']} (a equipe de cada login é a da tela Acessos).")
    niveis = sorted(set(dados.get("niveis") or []))
    if any(n not in NIVEIS_FIXOS for n in niveis):
        raise ErroDemanda("Nível de login fixo inválido.")
    agora, por = _agora(), ident["usuario"]
    conn.execute("UPDATE demandas_equipes SET descricao = ?, ativo = ?, grupo_id = ?, atualizado_por = ?, "
                 "atualizado_em = ? WHERE id = ?", (descricao, ativo, grupo_id, por, agora, equipe_id))
    conn.execute("DELETE FROM demandas_equipe_lideres WHERE equipe_id = ?", (equipe_id,))
    conn.executemany("INSERT INTO demandas_equipe_lideres (equipe_id, usuario, marcado_por, marcado_em) "
                     "VALUES (?, ?, ?, ?)", [(equipe_id, u, por, agora) for u in lideres])
    conn.execute("DELETE FROM demandas_equipe_niveis WHERE equipe_id = ?", (equipe_id,))
    conn.executemany("INSERT INTO demandas_equipe_niveis (equipe_id, nivel) VALUES (?, ?)",
                     [(equipe_id, n) for n in niveis])
    conn.commit()
    logger.info("equipe %s '%s' salva por %s (lideres: %s)", equipe_id, e["nome"], por, ", ".join(lideres) or "-")
    return next(x for x in listar_equipes(conn) if x["id"] == equipe_id)


def _equipe(conn, equipe_id, exigir_ativa: bool = True):
    e = conn.execute("SELECT * FROM demandas_equipes WHERE id = ?", (equipe_id,)).fetchone() if equipe_id else None
    if not e:
        raise ErroDemanda("Escolha a equipe.")
    if exigir_ativa and not e["ativo"]:
        raise ErroDemanda(f"A equipe {e['nome']} está desativada e não recebe demandas novas.")
    return e


def _validar_responsavel(conn, equipe_id: int, responsavel) -> str | None:
    responsavel = str(responsavel or "").strip() or None
    if responsavel and not _eh_membro(conn, equipe_id, responsavel):
        raise ErroDemanda("O responsável precisa ser membro da equipe.")
    return responsavel


# ── Demandas ──────────────────────────────────────────────────────────────────

def _evento(conn, demanda_id: int, tipo: str, ident: dict, campo=None, de=None, para=None, texto=None) -> int:
    cur = conn.execute("INSERT INTO demandas_eventos (demanda_id, tipo, campo, de, para, texto, usuario, autor, "
                       "criado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (demanda_id, tipo, campo, None if de is None else str(de), None if para is None else str(para),
                        texto, ident["usuario"], _autor(ident), _agora()))
    return cur.lastrowid


def _embarcador_nome(conn, cnpj: str | None) -> str | None:
    if not cnpj:
        return None
    try:
        r = conn.execute("SELECT COALESCE(apelido, nome_remetente) FROM interno WHERE cnpj_embarcador = ?",
                         (cnpj,)).fetchone()
    except sqlite3.OperationalError:   # banco sem a tabela interno (testes)
        return None
    return r[0] if r else None


def buscar_embarcadores(conn, termo: str, limite: int = 20) -> list[dict]:
    termo = f"%{(termo or '').strip()}%"
    try:
        rows = conn.execute("SELECT cnpj_embarcador, COALESCE(apelido, nome_remetente) AS nome FROM interno "
                            "WHERE cnpj_embarcador IS NOT NULL AND (apelido LIKE ? OR nome_remetente LIKE ? "
                            "OR cnpj_embarcador LIKE ?) ORDER BY nome LIMIT ?",
                            (termo, termo, termo, limite)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"cnpj": r["cnpj_embarcador"], "nome": r["nome"] or r["cnpj_embarcador"]} for r in rows]


def _linha(conn, demanda_id: int):
    return conn.execute("SELECT d.*, q.nome AS equipe_nome FROM demandas d "
                        "JOIN demandas_equipes q ON q.id = d.equipe_id WHERE d.id = ?", (demanda_id,)).fetchone()


def _formatar(d, ident: dict, nomes: dict) -> dict:
    hoje = date.today().isoformat()
    return {"id": d["id"], "titulo": d["titulo"], "descricao": d["descricao"], "equipe_id": d["equipe_id"],
            "equipe": d["equipe_nome"], "responsavel": d["responsavel"],
            "responsavel_nome": nomes.get(d["responsavel"], d["responsavel"]) if d["responsavel"] else None,
            "status": d["status"], "status_rotulo": STATUS.get(d["status"], d["status"]),
            "prioridade": d["prioridade"], "prioridade_rotulo": PRIORIDADES.get(d["prioridade"], d["prioridade"]),
            "prazo": d["prazo"], "atrasada": bool(d["prazo"] and d["prazo"] < hoje and d["status"] in ABERTOS),
            "pedido_codigo": d["pedido_codigo"], "embarcador_cnpj": d["embarcador_cnpj"],
            "embarcador_nome": d["embarcador_nome"], "criado_por": d["criado_por"],
            "criado_por_nome": d["criado_por_nome"], "criado_quando": _quando(d["criado_em"]),
            "atualizado_quando": _quando(d["atualizado_em"]), "origem": d["origem"], "acoes": acoes(ident, d)}


def _nomes() -> dict:
    return {u["usuario"]: (u["nome"] or u["usuario"]) for u in acesso_telas.listar(acesso_telas.conectar())}


def criar(conn, ident: dict, dados: dict) -> dict:
    if not ident["pode_criar"]:
        raise SemPermissao("Só o acesso total e os líderes de equipe criam demandas.")
    autor = _autor(ident)
    titulo = _texto(dados.get("titulo"), "O título", TITULO_MIN, TITULO_MAX, uma_linha=True)
    descricao = _texto(dados.get("descricao"), "A descrição", 0, TEXTO_MAX)
    equipe = _equipe(conn, dados.get("equipe_id"))
    responsavel = _validar_responsavel(conn, equipe["id"], dados.get("responsavel"))
    prioridade = str(dados.get("prioridade") or "NORMAL").upper()
    if prioridade not in PRIORIDADES:
        raise ErroDemanda("Prioridade inválida.")
    cnpj = str(dados.get("embarcador_cnpj") or "").strip() or None
    agora = _agora()
    cur = conn.execute(
        "INSERT INTO demandas (titulo, descricao, equipe_id, responsavel, prioridade, prazo, pedido_codigo, "
        "embarcador_cnpj, embarcador_nome, origem, origem_ref, criado_por, criado_por_nome, criado_em, atualizado_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (titulo, descricao, equipe["id"], responsavel, prioridade, _prazo(dados.get("prazo")),
         normalizar_pedido(dados.get("pedido_codigo")), cnpj,
         _embarcador_nome(conn, cnpj) or (str(dados.get("embarcador_nome") or "").strip() or None) if cnpj else None,
         dados.get("origem") or "manual", dados.get("origem_ref"), ident["usuario"], autor, agora, agora))
    demanda_id = cur.lastrowid
    _evento(conn, demanda_id, "criada", ident, para=equipe["id"])
    if responsavel:
        _evento(conn, demanda_id, "responsavel", ident, campo="responsavel", para=responsavel)
    conn.commit()
    logger.info("demanda %s criada por %s pra equipe %s", demanda_id, autor, equipe["nome"])
    d = _linha(conn, demanda_id)
    _avisar_grupo(conn, d, "criada")
    return _formatar(d, ident, _nomes())


def _carregar(conn, ident: dict, demanda_id: int, acao: str | None = None):
    d = _linha(conn, demanda_id)
    if not d or not _pode_ver(ident, d):
        raise ErroDemanda("Demanda não encontrada.")
    if acao and acao not in acoes(ident, d):
        raise SemPermissao("Você não pode fazer isso nessa demanda.")
    return d


def editar(conn, ident: dict, demanda_id: int, dados: dict) -> dict:
    d = _carregar(conn, ident, demanda_id, "editar")
    _autor(ident)
    novo = {}
    if "titulo" in dados:
        novo["titulo"] = _texto(dados.get("titulo"), "O título", TITULO_MIN, TITULO_MAX, uma_linha=True)
    if "descricao" in dados:
        novo["descricao"] = _texto(dados.get("descricao"), "A descrição", 0, TEXTO_MAX)
    if "prioridade" in dados:
        novo["prioridade"] = str(dados.get("prioridade") or "").upper()
        if novo["prioridade"] not in PRIORIDADES:
            raise ErroDemanda("Prioridade inválida.")
    if "prazo" in dados:
        novo["prazo"] = _prazo(dados.get("prazo"))
    if "pedido_codigo" in dados:
        novo["pedido_codigo"] = normalizar_pedido(dados.get("pedido_codigo"))
    if "embarcador_cnpj" in dados:
        cnpj = str(dados.get("embarcador_cnpj") or "").strip() or None
        novo["embarcador_cnpj"] = cnpj
        novo["embarcador_nome"] = (_embarcador_nome(conn, cnpj)
                                   or (str(dados.get("embarcador_nome") or "").strip() or None)) if cnpj else None
    equipe_id = d["equipe_id"]
    if "equipe_id" in dados and int(dados.get("equipe_id") or 0) != d["equipe_id"]:
        equipe_id = novo["equipe_id"] = _equipe(conn, dados.get("equipe_id"))["id"]
    responsavel = d["responsavel"]
    if "responsavel" in dados:
        responsavel = _validar_responsavel(conn, equipe_id, dados.get("responsavel"))
    elif responsavel and equipe_id != d["equipe_id"] and not _eh_membro(conn, equipe_id, responsavel):
        responsavel = None   # trocou de equipe: responsavel que nao e membro da nova sai
    if responsavel != d["responsavel"]:
        novo["responsavel"] = responsavel
    mudou = {k: v for k, v in novo.items() if v != d[k]}
    if not mudou:
        return _formatar(d, ident, _nomes())
    sets = ", ".join(f"{k} = ?" for k in mudou)
    conn.execute(f"UPDATE demandas SET {sets}, atualizado_em = ? WHERE id = ?", (*mudou.values(), _agora(), demanda_id))
    for campo, valor in mudou.items():
        if campo == "embarcador_nome":
            continue
        tipo = {"equipe_id": "equipe", "responsavel": "responsavel", "prioridade": "prioridade",
                "prazo": "prazo"}.get(campo, "edicao")
        _evento(conn, demanda_id, tipo, ident, campo=campo, de=d[campo], para=valor)
    conn.commit()
    atual = _linha(conn, demanda_id)
    if "equipe_id" in mudou:
        _avisar_grupo(conn, atual, "equipe")
    elif "responsavel" in mudou and responsavel:
        _avisar_grupo(conn, atual, "responsavel")
    if mudou.get("prioridade") == "URGENTE":
        _avisar_grupo(conn, atual, "urgente")
    return _formatar(atual, ident, _nomes())


_TRANSICOES = {("ABERTA", "EM_ANDAMENTO"): "iniciar", ("EM_ANDAMENTO", "CONCLUIDA"): "concluir",
               ("ABERTA", "CANCELADA"): "cancelar", ("EM_ANDAMENTO", "CANCELADA"): "cancelar",
               ("CONCLUIDA", "ABERTA"): "reabrir", ("CANCELADA", "ABERTA"): "reabrir"}


def mudar_status(conn, ident: dict, demanda_id: int, novo: str, comentario: str = "") -> dict:
    d = _carregar(conn, ident, demanda_id)
    novo = str(novo or "").upper()
    acao = _TRANSICOES.get((d["status"], novo))
    if not acao:
        raise ErroDemanda(f"Não dá pra passar de {STATUS.get(d['status'], d['status'])} "
                          f"pra {STATUS.get(novo, novo or '?')}.")
    if acao not in acoes(ident, d):
        raise SemPermissao("Você não pode fazer isso nessa demanda.")
    autor = _autor(ident)
    comentario = _texto(comentario, "O comentário", 0, TEXTO_MAX)
    if acao == "cancelar" and not comentario:
        raise ErroDemanda("Diga por que a demanda foi cancelada.")
    agora = _agora()
    concluido = (agora, autor) if novo in ("CONCLUIDA", "CANCELADA") else (None, None)
    conn.execute("UPDATE demandas SET status = ?, atualizado_em = ?, concluido_em = ?, concluido_por = ? WHERE id = ?",
                 (novo, agora, *concluido, demanda_id))
    _evento(conn, demanda_id, "reaberta" if acao == "reabrir" else "status", ident, campo="status",
            de=d["status"], para=novo, texto=comentario or None)
    if acao == "iniciar" and not d["responsavel"] and ident["nominal"] and d["equipe_id"] in ident["membro_de"]:
        conn.execute("UPDATE demandas SET responsavel = ? WHERE id = ?", (ident["usuario"], demanda_id))
        _evento(conn, demanda_id, "responsavel", ident, campo="responsavel", para=ident["usuario"])
    conn.commit()
    logger.info("demanda %s: %s -> %s por %s", demanda_id, d["status"], novo, autor)
    return _formatar(_linha(conn, demanda_id), ident, _nomes())


def assumir(conn, ident: dict, demanda_id: int) -> dict:
    d = _carregar(conn, ident, demanda_id, "assumir")
    conn.execute("UPDATE demandas SET responsavel = ?, atualizado_em = ? WHERE id = ?",
                 (ident["usuario"], _agora(), demanda_id))
    _evento(conn, demanda_id, "responsavel", ident, campo="responsavel", de=d["responsavel"], para=ident["usuario"])
    conn.commit()
    return _formatar(_linha(conn, demanda_id), ident, _nomes())


def comentar(conn, ident: dict, demanda_id: int, texto: str) -> dict:
    _carregar(conn, ident, demanda_id, "comentar")
    texto = _texto(texto, "O comentário", 1, TEXTO_MAX)
    _evento(conn, demanda_id, "comentario", ident, texto=texto)
    conn.execute("UPDATE demandas SET atualizado_em = ? WHERE id = ?", (_agora(), demanda_id))
    conn.commit()
    return detalhar(conn, ident, demanda_id)


_ROTULO_EVENTO = {"criada": "criou a demanda", "comentario": "comentou", "reaberta": "reabriu",
                  "responsavel": "mudou o responsável", "equipe": "mudou a equipe", "prioridade": "mudou a prioridade",
                  "prazo": "mudou o prazo", "edicao": "editou", "status": "mudou o status"}
_ROTULO_CAMPO = {"titulo": "título", "descricao": "descrição", "pedido_codigo": "pedido", "embarcador_cnpj": "embarcador"}


def detalhar(conn, ident: dict, demanda_id: int) -> dict:
    d = _carregar(conn, ident, demanda_id)
    nomes = _nomes()
    equipes = {r["id"]: r["nome"] for r in conn.execute("SELECT id, nome FROM demandas_equipes")}

    def _valor(campo, v):
        if v is None or v == "":
            return "—"
        if campo == "responsavel":
            return nomes.get(v, v)
        if campo == "equipe_id":
            return equipes.get(int(v), v)
        if campo == "status":
            return STATUS.get(v, v)
        if campo == "prioridade":
            return PRIORIDADES.get(v, v)
        return v

    linha_do_tempo = []
    for e in conn.execute("SELECT * FROM demandas_eventos WHERE demanda_id = ? ORDER BY id", (demanda_id,)):
        resumo = _ROTULO_EVENTO.get(e["tipo"], e["tipo"])
        if e["tipo"] == "status":
            resumo = f"passou pra {_valor('status', e['para'])}"
        elif e["tipo"] == "responsavel":
            resumo = f"responsável: {_valor('responsavel', e['para'])}"
        elif e["tipo"] == "equipe":
            resumo = f"equipe: {_valor('equipe_id', e['para'])}"
        elif e["tipo"] in ("prioridade", "prazo"):
            resumo = f"{e['tipo']}: {_valor(e['campo'], e['de'])} → {_valor(e['campo'], e['para'])}"
        elif e["tipo"] == "edicao":
            resumo = f"editou {_ROTULO_CAMPO.get(e['campo'], e['campo'])}"
        linha_do_tempo.append({"id": e["id"], "tipo": e["tipo"], "autor": e["autor"], "resumo": resumo,
                               "texto": e["texto"], "quando": _quando(e["criado_em"])})
    return {**_formatar(d, ident, nomes), "eventos": linha_do_tempo}


def listar(conn, ident: dict, aba: str = "equipe", filtros: dict | None = None) -> list[dict]:
    filtros = filtros or {}
    visivel, params = _filtro_visivel(ident)
    where = [visivel]
    if aba == "minhas":
        where.append("d.responsavel = ?")
        params.append(ident["usuario"])
    elif aba == "criadas":
        where.append("d.criado_por = ?")
        params.append(ident["usuario"])
    elif aba == "todas":
        if not ident["admin"]:
            raise SemPermissao("Só o acesso total vê todas as demandas.")
    else:   # equipe
        equipes = sorted(ident["equipes"])
        where.append(f"d.equipe_id IN ({','.join('?' * len(equipes)) or 'NULL'})")
        params += equipes
    status = [s for s in str(filtros.get("status") or "ABERTA,EM_ANDAMENTO").upper().split(",") if s in STATUS]
    if status:
        where.append(f"d.status IN ({','.join('?' * len(status))})")
        params += status
    if filtros.get("equipe_id"):
        where.append("d.equipe_id = ?")
        params.append(int(filtros["equipe_id"]))
    if str(filtros.get("prioridade") or "").upper() in PRIORIDADES:
        where.append("d.prioridade = ?")
        params.append(str(filtros["prioridade"]).upper())
    if filtros.get("atrasadas"):
        where.append("d.prazo IS NOT NULL AND d.prazo < ?")
        params.append(date.today().isoformat())
    busca = str(filtros.get("busca") or "").strip()
    if busca:
        where.append("(d.titulo LIKE ? OR d.pedido_codigo LIKE ? OR d.embarcador_nome LIKE ? OR CAST(d.id AS TEXT) = ?)")
        params += [f"%{busca}%", f"%{busca}%", f"%{busca}%", busca.lstrip("#")]
    rows = conn.execute(
        "SELECT d.*, q.nome AS equipe_nome, (SELECT MAX(id) FROM demandas_eventos e WHERE e.demanda_id = d.id) "
        "AS ultimo_evento FROM demandas d JOIN demandas_equipes q ON q.id = d.equipe_id "
        f"WHERE {' AND '.join(where)} ORDER BY d.id DESC LIMIT 500", params).fetchall()
    nomes = _nomes()
    itens = [{**_formatar(r, ident, nomes), "ultimo_evento": r["ultimo_evento"] or 0} for r in rows]
    itens.sort(key=lambda x: (x["status"] not in ABERTOS, ORDEM_PRIORIDADE.get(x["prioridade"], 9),
                              x["prazo"] or "9999-12-31", -x["id"]))
    return itens


def opcoes(conn, ident: dict) -> dict:
    return {"equipes": [{"id": e["id"], "nome": e["nome"],
                         "membros": [{"usuario": m["usuario"], "nome": m["nome"]} for m in e["membros"]]}
                        for e in listar_equipes(conn, so_ativas=True)],
            "prioridades": [{"valor": k, "rotulo": v} for k, v in PRIORIDADES.items()],
            "status": [{"valor": k, "rotulo": v} for k, v in STATUS.items()],
            "pode_criar": ident["pode_criar"], "admin": ident["admin"], "nominal": ident["nominal"],
            "autor": ident["autor"], "minhas_equipes": sorted(ident["equipes"])}


# ── Avisos (badge + som em qualquer tela) ─────────────────────────────────────

def _escopo_proprio(ident: dict) -> tuple[str, list]:
    """Minhas e das minhas equipes -- sem o "ve tudo" do admin e sem o
    "criei": o aviso e o badge sao do que pede acao de quem esta logado."""
    equipes = sorted(ident["equipes"])
    return (f"(d.responsavel = ? OR d.equipe_id IN ({','.join('?' * len(equipes)) or 'NULL'}))",
            [ident["usuario"], *equipes])


def contar_badge(conn, ident: dict) -> dict:
    escopo, params = _escopo_proprio(ident)
    r = conn.execute(f"SELECT COUNT(*) AS qtd, COALESCE(SUM(d.prioridade = 'URGENTE' OR (d.prazo IS NOT NULL AND "
                     f"d.prazo < ?)), 0) AS criticas FROM demandas d WHERE d.status IN ('ABERTA', 'EM_ANDAMENTO') "
                     f"AND {escopo}", [date.today().isoformat(), *params]).fetchone()
    return {"qtd": r["qtd"], "criticas": r["criticas"]}


def avisos(conn, ident: dict, desde: int | None) -> dict:
    """Novidades pra quem esta logado depois do evento `desde`: demanda
    nova ou reaberta na minha equipe/comigo, transferida pra minha equipe,
    ou eu virei o responsavel. Sem `desde` (primeira vez neste aparelho)
    devolve so o ponteiro, sem itens -- senao tocaria o historico todo."""
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM demandas_eventos").fetchone()[0]
    resultado = {"max_id": max_id, "itens": [], "badge": contar_badge(conn, ident)}
    if desde is None or desde >= max_id:
        return resultado
    escopo, params = _escopo_proprio(ident)
    rows = conn.execute(
        "SELECT e.id AS evento_id, e.tipo, e.autor, d.id, d.titulo, d.prioridade, q.nome AS equipe "
        "FROM demandas_eventos e JOIN demandas d ON d.id = e.demanda_id JOIN demandas_equipes q ON q.id = d.equipe_id "
        "WHERE e.id > ? AND e.usuario <> ? AND d.status IN ('ABERTA', 'EM_ANDAMENTO') AND ("
        "  (e.tipo = 'responsavel' AND e.para = ?) "
        f"  OR (e.tipo IN ('criada', 'equipe', 'reaberta') AND {escopo})"
        ") ORDER BY e.id LIMIT 20", [desde, ident["usuario"], ident["usuario"], *params]).fetchall()
    vistos = set()
    for r in rows:
        if r["id"] in vistos:
            continue
        vistos.add(r["id"])
        motivo = {"responsavel": "ficou com você", "equipe": "chegou pra sua equipe",
                  "reaberta": "foi reaberta"}.get(r["tipo"], "nova pra sua equipe")
        resultado["itens"].append({"evento_id": r["evento_id"], "demanda_id": r["id"], "titulo": r["titulo"],
                                   "prioridade": r["prioridade"], "equipe": r["equipe"], "motivo": motivo,
                                   "autor": r["autor"]})
    return resultado


# ── Aviso no grupo interno ────────────────────────────────────────────────────

def texto_aviso(d, tipo: str) -> str:
    """So numero, prioridade e equipe: os grupos do /atendimento sao abertos
    a todo o Atendimento, e o titulo fica na tela pra quem pode ver."""
    marca = f" [{PRIORIDADES.get(d['prioridade'], d['prioridade']).upper()}]" if d["prioridade"] in ("ALTA", "URGENTE") else ""
    frase = {"criada": f"Nova demanda #{d['id']}{marca} para {d['equipe_nome']}",
             "equipe": f"Demanda #{d['id']}{marca} transferida para {d['equipe_nome']}",
             "responsavel": f"Demanda #{d['id']}{marca} de {d['equipe_nome']} tem responsável novo",
             "urgente": f"Demanda #{d['id']} de {d['equipe_nome']} virou URGENTE"}[tipo]
    return frase + " — abrir em Demandas"


def _avisar_grupo(conn, d, tipo: str) -> None:
    """Depois do commit e sem nunca derrubar a acao: grupo da equipe se ela
    tiver um escolhido, senao o grupo "Demandas" (criado se faltar)."""
    try:
        conn_g = grupos_equipe.conectar(DB_PATH)
        try:
            grupo_id = conn.execute("SELECT grupo_id FROM demandas_equipes WHERE id = ?",
                                    (d["equipe_id"],)).fetchone()["grupo_id"]
            if not grupo_id or not grupos_equipe.buscar_grupo(conn_g, grupo_id):
                row = conn_g.execute("SELECT id FROM equipe_grupos WHERE lower(nome) = lower(?)", (NOME_GRUPO,)).fetchone()
                grupo_id = row["id"] if row else grupos_equipe.criar_grupo(conn_g, NOME_GRUPO, "sistema", AUTOR_AVISO)["id"]
            grupos_equipe.postar(conn_g, grupo_id, "sistema", AUTOR_AVISO, texto_aviso(d, tipo))
        finally:
            conn_g.close()
    except Exception as e:
        logger.warning("demanda %s: aviso no grupo falhou (%s): %s", d["id"], tipo, e)


# ── Rotas ─────────────────────────────────────────────────────────────────────

def registrar(app, *, requer_auth, exige_mesma_origem):
    niveis = NIVEIS_TELA

    def _ident(conn) -> dict:
        nome = session.get("demandas_nome") or session.get("atendente_nome")
        return identidade(conn, session.get("usuario") or g.nivel_acesso, session.get("nivel_acesso"),
                          g.get("telas_liberadas"), nome)

    def _corpo() -> dict:
        corpo = request.get_json(force=True, silent=True)
        if not isinstance(corpo, dict):
            abort(Response(json.dumps({"erro": "Corpo da requisição inválido."}), 400, mimetype="application/json"))
        return corpo

    def _executar(func):
        # Toda resposta diz quem o servidor ve logado: aba aberta como um
        # usuario e login de outro no mesmo navegador (cookie compartilhado)
        # faz a tela recarregar em vez de mostrar botao que vai dar 403.
        conn = conectar()
        try:
            resp, status = jsonify(func(conn, _ident(conn))), 200
        except PrecisaNome as e:
            resp, status = jsonify({"erro": str(e), "precisa_nome": True}), 400
        except SemPermissao as e:
            resp, status = jsonify({"erro": str(e)}), 403
        except (ErroDemanda, ValueError) as e:
            resp, status = jsonify({"erro": str(e)}), 400
        finally:
            conn.close()
        resp.status_code = status
        resp.headers["X-Demandas-Usuario"] = session.get("usuario") or g.nivel_acesso
        return resp

    @app.route("/demandas")
    @requer_auth(niveis=niveis)
    def demandas():
        conn = conectar()
        try:
            ident = _ident(conn)
        finally:
            conn.close()
        return render_template("demandas.html", ident=ident, abrir=request.args.get("abrir", type=int))

    @app.route("/api/demandas")
    @requer_auth(niveis=niveis)
    def api_demandas_listar():
        filtros = {k: request.args.get(k) for k in ("status", "equipe_id", "prioridade", "busca", "atrasadas")}
        return _executar(lambda conn, ident: {"demandas": listar(conn, ident, request.args.get("aba", "equipe"),
                                                                 filtros)})

    @app.route("/api/demandas", methods=["POST"])
    @requer_auth(niveis=niveis)
    @exige_mesma_origem
    def api_demandas_criar():
        corpo = _corpo()
        corpo.pop("origem", None)
        corpo.pop("origem_ref", None)
        return _executar(lambda conn, ident: {"demanda": criar(conn, ident, corpo)})

    @app.route("/api/demandas/<int:demanda_id>")
    @requer_auth(niveis=niveis)
    def api_demanda(demanda_id):
        return _executar(lambda conn, ident: {"demanda": detalhar(conn, ident, demanda_id)})

    @app.route("/api/demandas/<int:demanda_id>", methods=["POST"])
    @requer_auth(niveis=niveis)
    @exige_mesma_origem
    def api_demanda_editar(demanda_id):
        corpo = _corpo()
        return _executar(lambda conn, ident: {"demanda": editar(conn, ident, demanda_id, corpo)})

    @app.route("/api/demandas/<int:demanda_id>/status", methods=["POST"])
    @requer_auth(niveis=niveis)
    @exige_mesma_origem
    def api_demanda_status(demanda_id):
        corpo = _corpo()
        return _executar(lambda conn, ident: {"demanda": mudar_status(conn, ident, demanda_id, corpo.get("status"),
                                                                      corpo.get("comentario") or "")})

    @app.route("/api/demandas/<int:demanda_id>/assumir", methods=["POST"])
    @requer_auth(niveis=niveis)
    @exige_mesma_origem
    def api_demanda_assumir(demanda_id):
        return _executar(lambda conn, ident: {"demanda": assumir(conn, ident, demanda_id)})

    @app.route("/api/demandas/<int:demanda_id>/comentarios", methods=["POST"])
    @requer_auth(niveis=niveis)
    @exige_mesma_origem
    def api_demanda_comentar(demanda_id):
        corpo = _corpo()
        return _executar(lambda conn, ident: {"demanda": comentar(conn, ident, demanda_id, corpo.get("texto"))})

    @app.route("/api/demandas/nome", methods=["POST"])
    @requer_auth(niveis=niveis)
    @exige_mesma_origem
    def api_demandas_nome():
        nome = " ".join(str(_corpo().get("nome") or "").split())
        if not 2 <= len(nome) <= 60:
            return jsonify({"erro": "Informe seu nome (de 2 a 60 letras)."}), 400
        session["demandas_nome"] = nome
        return jsonify({"ok": True, "nome": nome})

    @app.route("/api/demandas/opcoes")
    @requer_auth(niveis=niveis)
    def api_demandas_opcoes():
        return _executar(opcoes)

    @app.route("/api/demandas/embarcadores")
    @requer_auth(niveis=niveis)
    def api_demandas_embarcadores():
        return _executar(lambda conn, ident: {"embarcadores": buscar_embarcadores(conn, request.args.get("q", ""))})

    @app.route("/api/demandas/avisos")
    @requer_auth(niveis=niveis)
    def api_demandas_avisos():
        desde = request.args.get("desde", type=int)
        return _executar(lambda conn, ident: avisos(conn, ident, desde))

    def _so_admin(func):
        def interno(conn, ident):
            if not ident["admin"]:
                raise SemPermissao("Só o acesso total cadastra equipes.")
            return func(conn, ident)
        return interno

    @app.route("/api/demandas/equipes")
    @requer_auth(niveis=niveis)
    def api_demandas_equipes():
        def _dados(conn, ident):
            conn_g = grupos_equipe.conectar(DB_PATH)
            try:
                grupos = [{"id": r["id"], "nome": r["nome"]}
                          for r in conn_g.execute("SELECT id, nome FROM equipe_grupos ORDER BY lower(nome)")]
            finally:
                conn_g.close()
            sem_equipe = [{"usuario": u["usuario"], "nome": u["nome"] or u["usuario"]}
                          for u in acesso_telas.listar(acesso_telas.conectar()) if u["ativo"] and not u.get("equipe")]
            return {"equipes": listar_equipes(conn), "grupos": grupos, "niveis_fixos": list(NIVEIS_FIXOS),
                    "sem_equipe": sem_equipe}
        return _executar(_so_admin(_dados))

    @app.route("/api/demandas/equipes/<int:equipe_id>", methods=["POST"])
    @requer_auth(niveis=niveis)
    @exige_mesma_origem
    def api_demandas_equipe_salvar(equipe_id):
        corpo = _corpo()
        return _executar(lambda conn, ident: {"equipe": salvar_equipe(conn, ident, equipe_id, corpo)})
