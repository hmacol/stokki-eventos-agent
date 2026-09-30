# -*- coding: utf-8 -*-
"""
portal_cliente/auth_cliente.py

Login do embarcador no portal de acompanhamento (app.freshhub.com.br/cliente),
pedido do Hugo em 08/09: CNPJ + PIN de 6 dígitos, com primeiro acesso e
"esqueci o PIN" por link assinado enviado ao e-mail já cadastrado na
tabela `interno` (o mesmo e-mail usado pelas notificações de insucesso).

Mesmo desenho de nucleo/auth_motorista.py (PBKDF2-HMAC-SHA256 200k
iterações + salt por conta, 5 erros -> 15 min de bloqueio, mensagem
genérica que nunca revela se o CNPJ existe), sobre a tabela nova
`clientes_portal` em dados/dados.db. Quem PODE ter conta é quem está em
`interno` com sender_id preenchido -- é o sender_id da VUUPT que liga o
embarcador aos pedidos dele.

O link de definição de PIN (itsdangerous, 24 h) carrega um prefixo do
pin_hash atual ("novo" quando ainda não há PIN): usar o link uma vez
muda o hash e invalida o próprio link, sem coluna extra. A sessão do
navegador (cookie assinado do Flask) carrega o mesmo prefixo -- trocar o
PIN derruba as outras sessões abertas.
"""
import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "dados.db"

PBKDF2_ITERACOES = 200_000
MAX_TENTATIVAS = 5
BLOQUEIO_MINUTOS = 15
LINK_PIN_SEGUNDOS = 24 * 3600
_SALT_LINK_PIN = "portal-cliente-definir-pin"


class AutenticacaoInvalida(Exception):
    def __init__(self, mensagem: str, codigo: int = 401):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.codigo = codigo


# ── Banco ──────────────────────────────────────────────────────────────────────

def conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clientes_portal (
            cnpj            TEXT PRIMARY KEY,
            sender_id       INTEGER,
            nome            TEXT,
            pin_hash        TEXT,
            pin_salt        TEXT,
            tentativas_pin  INTEGER NOT NULL DEFAULT 0,
            bloqueado_ate   TEXT,
            ativo           INTEGER NOT NULL DEFAULT 1,
            criado_em       TEXT NOT NULL,
            atualizado_em   TEXT NOT NULL,
            ultimo_login_em TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portal_tentativas (
            escopo    TEXT NOT NULL,
            chave     TEXT NOT NULL,
            criado_em TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_portal_tentativas ON portal_tentativas (escopo, chave, criado_em)")
    # Grupo economico (17/09): o CNPJ que loga (cnpj_login) enxerga tambem os
    # pedidos dos membros. Membro continua podendo ter a conta propria dele.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portal_grupos (
            cnpj_login  TEXT NOT NULL,
            cnpj_membro TEXT NOT NULL,
            PRIMARY KEY (cnpj_login, cnpj_membro)
        )
    """)
    conn.commit()
    return conn


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── Trava de tentativas por escopo/chave ───────────────────────────────────────
# O login do cliente tem a trava dele em clientes_portal (por conta). Isto
# cobre o que nao tem conta pra pendurar o contador: pedidos de link de PIN
# (por CNPJ e por IP) e o login da equipe em /equipe (por IP).

def registrar_tentativa(conn: sqlite3.Connection, escopo: str, chave: str) -> None:
    limite = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM portal_tentativas WHERE criado_em < ?", (limite,))
    conn.execute("INSERT INTO portal_tentativas (escopo, chave, criado_em) VALUES (?, ?, ?)",
                 (escopo, str(chave or ""), _agora()))
    conn.commit()


def contar_tentativas(conn: sqlite3.Connection, escopo: str, chave: str, minutos: int) -> int:
    desde = (datetime.now() - timedelta(minutes=minutos)).strftime("%Y-%m-%d %H:%M:%S")
    return conn.execute("SELECT count(*) FROM portal_tentativas WHERE escopo = ? AND chave = ? AND criado_em >= ?",
                        (escopo, str(chave or ""), desde)).fetchone()[0]


def limpar_tentativas(conn: sqlite3.Connection, escopo: str, chave: str) -> None:
    conn.execute("DELETE FROM portal_tentativas WHERE escopo = ? AND chave = ?", (escopo, str(chave or "")))
    conn.commit()


# ── CNPJ / PIN ─────────────────────────────────────────────────────────────────

def normalizar_cnpj(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def formatar_cnpj(digitos: str) -> str:
    d = normalizar_cnpj(digitos)
    if len(d) != 14:
        return d
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


def validar_pin_formato(pin) -> str:
    pin = str(pin or "").strip()
    if not re.fullmatch(r"\d{6}", pin):
        raise ValueError("O PIN precisa ter exatamente 6 dígitos.")
    return pin


def _hash_pin(pin: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ITERACOES).hex()


# ── Embarcador (tabela interno) ────────────────────────────────────────────────

def emails_do_campo(raw) -> list[str]:
    """`interno.email` é multivalorado (vírgula, ponto-e-vírgula ou tab) --
    mesmo split de notificar_pedidos_em_espera.py e
    aplicar_resposta_insucesso.py."""
    return [e.strip() for e in re.split(r"[,;\t]+", str(raw or "")) if e.strip() and "@" in e]


def buscar_embarcador(conn: sqlite3.Connection, cnpj: str) -> dict | None:
    """Quem pode ter conta: linha de `interno` com sender_id. Devolve
    {cnpj, sender_id, nome, emails} ou None."""
    row = conn.execute(
        "SELECT cnpj_embarcador, sender_id, nome_remetente, apelido, email "
        "FROM interno WHERE cnpj_embarcador = ?", (normalizar_cnpj(cnpj),)
    ).fetchone()
    if not row or not row["sender_id"]:
        return None
    return {
        "cnpj": row["cnpj_embarcador"],
        "sender_id": int(row["sender_id"]),
        "nome": row["apelido"] or row["nome_remetente"] or f"Remetente {row['sender_id']}",
        "emails": emails_do_campo(row["email"]),
    }


def listar_embarcadores(conn: sqlite3.Connection) -> list[dict]:
    """Todos os embarcadores elegíveis, com o estado da conta no portal
    (pra CLI de gestão)."""
    rows = conn.execute("""
        SELECT i.cnpj_embarcador AS cnpj, i.sender_id, i.apelido, i.nome_remetente, i.email,
               c.pin_hash, c.ativo, c.ultimo_login_em
        FROM interno i LEFT JOIN clientes_portal c ON c.cnpj = i.cnpj_embarcador
        WHERE i.sender_id IS NOT NULL
        ORDER BY COALESCE(i.apelido, i.nome_remetente)
    """).fetchall()
    return [{
        "cnpj": r["cnpj"], "sender_id": r["sender_id"],
        "nome": r["apelido"] or r["nome_remetente"] or "",
        "emails": emails_do_campo(r["email"]),
        "tem_pin": bool(r["pin_hash"]),
        "ativo": r["ativo"] if r["ativo"] is not None else None,
        "ultimo_login_em": r["ultimo_login_em"],
    } for r in rows]


# ── Grupo econômico (um login, várias empresas) ────────────────────────────────

def empresas_do_login(conn: sqlite3.Connection, cnpj: str) -> list[dict]:
    """Embarcadores que o login enxerga: o próprio CNPJ primeiro e depois os
    membros do grupo (por nome). Sem grupo, só ele. Membro que saiu de
    `interno` (ou ficou sem sender_id) some da lista sozinho."""
    principal = buscar_embarcador(conn, cnpj)
    if not principal:
        return []
    membros = []
    for r in conn.execute("SELECT cnpj_membro FROM portal_grupos WHERE cnpj_login = ?", (principal["cnpj"],)):
        emb = buscar_embarcador(conn, r["cnpj_membro"])
        if emb and emb["cnpj"] != principal["cnpj"]:
            membros.append(emb)
    return [principal] + sorted(membros, key=lambda e: e["nome"])


def definir_grupo(conn: sqlite3.Connection, cnpj_login: str, membros: list[str]) -> list[dict]:
    """Substitui a lista de membros do grupo (lista vazia desfaz o grupo).
    Confere TODOS antes de gravar -- CNPJ fora de `interno` levanta ValueError
    e nada muda. `interno.cnpj_embarcador` tem linha com 13 dígitos (2º
    remetente do Grupo Trigo), por isso o membro entra como está lá."""
    principal = buscar_embarcador(conn, cnpj_login)
    if not principal:
        raise ValueError(f"CNPJ do login {cnpj_login} não está em `interno` com sender_id.")
    validos = []
    for m in membros:
        emb = buscar_embarcador(conn, m)
        if not emb:
            raise ValueError(f"Membro {m} não está em `interno` com sender_id.")
        if emb["cnpj"] != principal["cnpj"] and emb["cnpj"] not in validos:
            validos.append(emb["cnpj"])
    conn.execute("DELETE FROM portal_grupos WHERE cnpj_login = ?", (principal["cnpj"],))
    conn.executemany("INSERT INTO portal_grupos (cnpj_login, cnpj_membro) VALUES (?, ?)",
                     [(principal["cnpj"], m) for m in validos])
    conn.commit()
    return empresas_do_login(conn, principal["cnpj"])


def listar_grupos(conn: sqlite3.Connection) -> dict[str, list[str]]:
    grupos: dict[str, list[str]] = {}
    for r in conn.execute("SELECT cnpj_login, cnpj_membro FROM portal_grupos ORDER BY cnpj_login, cnpj_membro"):
        grupos.setdefault(r["cnpj_login"], []).append(r["cnpj_membro"])
    return grupos


def montar_cliente(conn: sqlite3.Connection, cnpj: str, versao: str) -> dict | None:
    """O `g.cliente` do portal: a empresa do login + as do grupo. `sender_id`
    e `cnpj` são sempre os do login; `sender_ids`/`empresas` cobrem o grupo."""
    empresas = empresas_do_login(conn, cnpj)
    if not empresas:
        return None
    emb = empresas[0]
    return {"cnpj": emb["cnpj"], "cnpj_formatado": formatar_cnpj(emb["cnpj"]),
            "sender_id": emb["sender_id"], "nome": emb["nome"], "versao": versao,
            "sender_ids": [e["sender_id"] for e in empresas],
            "empresas": [{"cnpj": e["cnpj"], "cnpj_formatado": formatar_cnpj(e["cnpj"]),
                          "sender_id": e["sender_id"], "nome": e["nome"]} for e in empresas]}


# ── Conta do portal ────────────────────────────────────────────────────────────

def buscar_conta(conn: sqlite3.Connection, cnpj: str) -> dict | None:
    row = conn.execute("SELECT * FROM clientes_portal WHERE cnpj = ?", (normalizar_cnpj(cnpj),)).fetchone()
    return dict(row) if row else None


def definir_pin(conn: sqlite3.Connection, cnpj: str, pin: str) -> dict:
    """Cria a conta (se não existir) ou troca o PIN. Só pra CNPJ que está
    em `interno` com sender_id -- levanta ValueError caso contrário."""
    emb = buscar_embarcador(conn, cnpj)
    if not emb:
        raise ValueError("CNPJ não cadastrado como embarcador (tabela interno sem sender_id).")
    pin = validar_pin_formato(pin)
    salt = secrets.token_hex(16)
    pin_hash = _hash_pin(pin, salt)
    agora = _agora()
    conn.execute("""
        INSERT INTO clientes_portal (cnpj, sender_id, nome, pin_hash, pin_salt, tentativas_pin, bloqueado_ate,
                                     ativo, criado_em, atualizado_em)
        VALUES (?, ?, ?, ?, ?, 0, NULL, 1, ?, ?)
        ON CONFLICT(cnpj) DO UPDATE SET
            sender_id = excluded.sender_id, nome = excluded.nome,
            pin_hash = excluded.pin_hash, pin_salt = excluded.pin_salt,
            tentativas_pin = 0, bloqueado_ate = NULL, atualizado_em = excluded.atualizado_em
    """, (emb["cnpj"], emb["sender_id"], emb["nome"], pin_hash, salt, agora, agora))
    conn.commit()
    return buscar_conta(conn, emb["cnpj"])


def definir_ativo(conn: sqlite3.Connection, cnpj: str, ativo: bool) -> bool:
    cur = conn.execute("UPDATE clientes_portal SET ativo = ?, atualizado_em = ? WHERE cnpj = ?",
                       (int(ativo), _agora(), normalizar_cnpj(cnpj)))
    conn.commit()
    return cur.rowcount > 0


def autenticar(conn: sqlite3.Connection, cnpj: str, pin: str) -> dict:
    """CNPJ + PIN com trava de tentativas. Mensagem sempre genérica."""
    generico = "CNPJ ou PIN incorretos."
    conta = buscar_conta(conn, cnpj)
    if not conta or not conta.get("ativo") or not conta.get("pin_hash"):
        raise AutenticacaoInvalida(generico)

    agora = datetime.now()
    bloqueado_ate = conta.get("bloqueado_ate")
    if bloqueado_ate and datetime.strptime(bloqueado_ate, "%Y-%m-%d %H:%M:%S") > agora:
        raise AutenticacaoInvalida("Muitas tentativas. Tente de novo em alguns minutos.", 429)

    if not hmac.compare_digest(_hash_pin(str(pin or ""), conta["pin_salt"]), conta["pin_hash"]):
        tentativas = int(conta.get("tentativas_pin") or 0) + 1
        bloqueio = None
        if tentativas >= MAX_TENTATIVAS:
            bloqueio = (agora + timedelta(minutes=BLOQUEIO_MINUTOS)).strftime("%Y-%m-%d %H:%M:%S")
            tentativas = 0
        conn.execute("UPDATE clientes_portal SET tentativas_pin = ?, bloqueado_ate = ? WHERE cnpj = ?",
                     (tentativas, bloqueio, conta["cnpj"]))
        conn.commit()
        raise AutenticacaoInvalida(generico)

    conn.execute("UPDATE clientes_portal SET tentativas_pin = 0, bloqueado_ate = NULL, ultimo_login_em = ? "
                 "WHERE cnpj = ?", (_agora(), conta["cnpj"]))
    conn.commit()
    # Nome/sender_id sempre da `interno` (fonte da verdade, pode ter mudado).
    emb = buscar_embarcador(conn, conta["cnpj"]) or {}
    return {**conta, "nome": emb.get("nome") or conta.get("nome"),
            "sender_id": emb.get("sender_id") or conta.get("sender_id")}


def versao_conta(conta: dict | None) -> str:
    """Prefixo do pin_hash -- muda a cada troca de PIN; 'novo' sem PIN."""
    return ((conta or {}).get("pin_hash") or "novo")[:12]


def sessao_valida(conn: sqlite3.Connection, cnpj: str, versao: str) -> dict | None:
    """Confere o cookie de sessão contra o banco a cada request: conta
    ativa e PIN não trocado desde o login. Devolve o cliente pra `g`."""
    conta = buscar_conta(conn, cnpj)
    if not conta or not conta.get("ativo") or versao_conta(conta) != versao:
        return None
    return montar_cliente(conn, cnpj, versao)


# ── Link de definição de PIN (primeiro acesso / esqueci o PIN) ─────────────────

def gerar_token_definir_pin(secret: str, conn: sqlite3.Connection, cnpj: str) -> str:
    conta = buscar_conta(conn, cnpj)
    payload = {"cnpj": normalizar_cnpj(cnpj), "v": versao_conta(conta)}
    return URLSafeTimedSerializer(secret).dumps(payload, salt=_SALT_LINK_PIN)


def validar_token_definir_pin(secret: str, conn: sqlite3.Connection, token: str) -> dict:
    """Devolve {estado: ok|expirado|invalido|usado, cnpj, embarcador}."""
    try:
        payload = URLSafeTimedSerializer(secret).loads(token, salt=_SALT_LINK_PIN, max_age=LINK_PIN_SEGUNDOS)
    except SignatureExpired:
        return {"estado": "expirado"}
    except BadSignature:
        return {"estado": "invalido"}
    cnpj = normalizar_cnpj(payload.get("cnpj"))
    emb = buscar_embarcador(conn, cnpj)
    if not emb:
        return {"estado": "invalido"}
    if payload.get("v") != versao_conta(buscar_conta(conn, cnpj)):
        return {"estado": "usado"}
    return {"estado": "ok", "cnpj": cnpj, "embarcador": emb}
