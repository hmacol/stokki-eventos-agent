# -*- coding: utf-8 -*-
"""
preferencias_notificacao.py

Preferencias de notificacao por embarcador (pedido do Hugo, 17/09): no
portal do cliente, o botao Notificacoes deixa o proprio embarcador ligar e
desligar cada tipo de e-mail e informar o e-mail em que quer recebe-los.
Spec: DOC_EXECUCAO_CLAUDE_RESUMO_DIARIO_EMBARCADOR.md.

Este modulo e a fonte unica das rotinas que mandam e-mail ao embarcador:
carregar_embarcadores(tipo) devolve o mesmo formato que cada rotina ja
montava na mao ({chave: {"nome", "emails"}}) mais "desligado".

  - E-mail: o campo de notificacoes (tabela preferencias_notificacao); vazio
    cai no interno.email do cadastro. O interno.email continua sendo o do
    cadastro (link de PIN, Cc de transportadora) e so o Hugo altera.
  - Sem linha na tabela = DEFAULTS (tudo ligado, menos o resumo diario).
  - interno.notificar_email = 0 NAO e mais veto (Hugo, 17/09, 2a decisao):
    pros tipos novos de 17/09 (TIPOS_OPT_IN_SE_NOTIFICAR_EMAIL_0) so muda o
    PADRAO de quem nunca salvou nada -- nascem desmarcados, e o embarcador
    liga sozinho no portal (ou o Hugo pelo /equipe). Linha gravada sempre
    vence. Os tipos antigos nunca olharam essa coluna e continuam sem olhar;
    pipeline.py e a triagem de pedidos parados seguem lendo a coluna como
    antes (la ela continua bloqueando).

A tabela fica fora da `interno` de proposito: nao depende de quem reescreve
aquela tabela.
"""
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "dados" / "dados.db"

# Ordem = ordem das chaves na tela do portal. grupo: "acompanhamento" (so informa)
# ou "acao" (o e-mail pede uma decisao do embarcador).
TIPOS = {
    "nfs_em_rota": {
        "grupo": "acompanhamento",
        "rotulo": "Notas em rota",
        "descricao": "Toda manhã, a lista das notas que saem para entrega no dia.",
        "default": True,
    },
    "entrega_concluida": {
        "grupo": "acompanhamento",
        "rotulo": "Entrega concluída",
        "descricao": "Um e-mail por pedido assim que a entrega é concluída, com o canhoto ou o motivo da falha.",
        "default": True,
    },
    "resumo_diario": {
        "grupo": "acompanhamento",
        "rotulo": "Resumo diário",
        "descricao": "No fim do dia, o status de todas as notas: entregues, com falha e não concluídas.",
        "default": False,
    },
    "insucesso": {
        "grupo": "acao",
        "rotulo": "Insucesso aguardando retorno",
        "descricao": "Aviso de falha na entrega pedindo a sua decisão (reenviar, devolver...). "
                     "Desligado, a pendência continua aparecendo aqui no portal.",
        "default": True,
    },
    "pedidos_em_espera": {
        "grupo": "acao",
        "rotulo": "Pedidos em espera",
        "descricao": "Cobrança do XML da nota fiscal dos pedidos parados aguardando faturamento.",
        "default": True,
    },
    "agendamento": {
        "grupo": "acao",
        "rotulo": "Agendamento",
        "descricao": "Pedidos que precisam de data e horário de agendamento, ou que foram agendados "
                     "para o dia fixo da região.",
        "default": True,
    },
}
TIPOS_OPT_IN_SE_NOTIFICAR_EMAIL_0 = ("nfs_em_rota", "entrega_concluida", "resumo_diario")
CHAVES = ("sender_id", "stkkc_id")
MAX_EMAILS = 5

_RE_EMAIL = re.compile(r"[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+")


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _so_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def _emails_do_campo(raw) -> list[str]:
    """Mesmo split do interno.email usado no resto do projeto."""
    return [e.strip() for e in re.split(r"[,;\t]+", str(raw or "")) if e.strip() and "@" in e]


def _garantir_tabela(conn: sqlite3.Connection) -> None:
    colunas = ", ".join(f"{tipo} INTEGER NOT NULL DEFAULT {int(info['default'])}" for tipo, info in TIPOS.items())
    conn.execute(
        "CREATE TABLE IF NOT EXISTS preferencias_notificacao ("
        "cnpj_embarcador TEXT PRIMARY KEY, emails TEXT NOT NULL DEFAULT '', "
        f"{colunas}, atualizado_em TEXT, atualizado_por TEXT)"
    )


def _tabela_existe(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='preferencias_notificacao'"
    ).fetchone() is not None


def _validar_emails(emails) -> list[str]:
    limpos = []
    for bruto in emails or []:
        email = str(bruto or "").strip().lower()
        if not email:
            continue
        if len(email) > 254 or not _RE_EMAIL.fullmatch(email):
            raise ValueError(f"E-mail inválido: {str(bruto).strip()[:80]!r}. Informe um endereço por linha.")
        if email not in limpos:
            limpos.append(email)
    if len(limpos) > MAX_EMAILS:
        raise ValueError(f"Informe no máximo {MAX_EMAILS} e-mails.")
    return limpos


def _default_do_tipo(tipo: str, notificar_email) -> bool:
    """Padrao de quem nunca salvou preferencia. notificar_email = 0 na
    `interno` faz os tipos novos nascerem desmarcados (NULL conta como 1,
    que e o default da coluna)."""
    if tipo in TIPOS_OPT_IN_SE_NOTIFICAR_EMAIL_0 and notificar_email is not None and not notificar_email:
        return False
    return TIPOS[tipo]["default"]


def _linha_interno(conn: sqlite3.Connection, cnpj) -> tuple:
    row = conn.execute(
        "SELECT cnpj_embarcador, email, notificar_email FROM interno WHERE cnpj_embarcador = ?",
        (_so_digitos(cnpj),)
    ).fetchone()
    if not row:
        raise ValueError("Embarcador não encontrado no cadastro.")
    return row[0], row[1], row[2]


def ler(conn: sqlite3.Connection, cnpj) -> dict:
    """Preferencias do embarcador pra tela do portal. Sem linha gravada,
    devolve os defaults."""
    cnpj_cadastro, email_cadastro, notificar_email = _linha_interno(conn, cnpj)
    _garantir_tabela(conn)
    colunas = ", ".join(TIPOS)
    row = conn.execute(
        f"SELECT emails, atualizado_em, atualizado_por, {colunas} FROM preferencias_notificacao "
        "WHERE cnpj_embarcador = ?", (cnpj_cadastro,)
    ).fetchone()
    tipos = {tipo: _default_do_tipo(tipo, notificar_email) for tipo in TIPOS}
    if row:
        tipos = {tipo: bool(row[3 + i]) for i, tipo in enumerate(TIPOS)}
    return {
        "cnpj": cnpj_cadastro,
        "emails": _emails_do_campo(row[0]) if row else [],
        "emails_cadastro": _emails_do_campo(email_cadastro),
        "tipos": tipos,
        "atualizado_em": row[1] if row else None,
        "atualizado_por": row[2] if row else None,
    }


def salvar(conn: sqlite3.Connection, cnpj, emails, flags: dict, por: str) -> dict:
    """Valida e grava (upsert). `flags` pode ser parcial: tipo nao informado
    mantem o que ja estava. Levanta ValueError com mensagem pronta pra tela;
    com erro, nada e gravado."""
    desconhecidos = [t for t in (flags or {}) if t not in TIPOS]
    if desconhecidos:
        raise ValueError(f"Tipo de notificação desconhecido: {', '.join(map(str, desconhecidos))}.")
    limpos = _validar_emails(emails)
    atuais = ler(conn, cnpj)

    tipos = dict(atuais["tipos"])
    tipos.update({t: bool(v) for t, v in (flags or {}).items()})

    colunas = ", ".join(TIPOS)
    marcadores = ", ".join("?" for _ in TIPOS)
    atualizacoes = ", ".join(f"{t} = excluded.{t}" for t in TIPOS)
    conn.execute(
        f"INSERT INTO preferencias_notificacao (cnpj_embarcador, emails, {colunas}, atualizado_em, atualizado_por) "
        f"VALUES (?, ?, {marcadores}, ?, ?) "
        f"ON CONFLICT(cnpj_embarcador) DO UPDATE SET emails = excluded.emails, {atualizacoes}, "
        "atualizado_em = excluded.atualizado_em, atualizado_por = excluded.atualizado_por",
        (atuais["cnpj"], ", ".join(limpos), *(int(tipos[t]) for t in TIPOS), _agora(), str(por or "")[:40]),
    )
    conn.commit()
    return ler(conn, cnpj)


def carregar_embarcadores(tipo: str, chave: str = "sender_id", db_path=None) -> dict:
    """{chave: {"nome", "emails", "cnpj", "desligado"}} pra rotina do `tipo`.
    So leitura: banco que ainda nao tem a tabela de preferencias (ninguem
    salvou nada) vale os defaults."""
    if tipo not in TIPOS:
        raise ValueError(f"Tipo de notificacao desconhecido: {tipo}")
    if chave not in CHAVES:
        raise ValueError(f"Chave desconhecida: {chave}")
    caminho = Path(db_path) if db_path else DB_PATH
    if not caminho.exists():
        raise FileNotFoundError(f"Banco nao encontrado: {caminho}")

    conn = sqlite3.connect(caminho)
    conn.row_factory = sqlite3.Row
    try:
        if _tabela_existe(conn):
            juncao = "LEFT JOIN preferencias_notificacao p ON p.cnpj_embarcador = i.cnpj_embarcador"
            campos = f"p.emails AS emails_notificacao, p.{tipo} AS ligado"
        else:
            juncao, campos = "", "NULL AS emails_notificacao, NULL AS ligado"
        rows = conn.execute(
            f"SELECT i.cnpj_embarcador, i.{chave} AS chave, i.nome_remetente, i.apelido, i.email, "
            f"i.notificar_email, {campos} FROM interno i {juncao} WHERE i.{chave} IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    embs = {}
    for r in rows:
        ligado = _default_do_tipo(tipo, r["notificar_email"]) if r["ligado"] is None else bool(r["ligado"])
        embs[r["chave"]] = {
            "nome": r["apelido"] or r["nome_remetente"] or "",
            "emails": _emails_do_campo(r["emails_notificacao"]) or _emails_do_campo(r["email"]),
            "cnpj": _so_digitos(r["cnpj_embarcador"]),
            "desligado": not ligado,
        }
    return embs
