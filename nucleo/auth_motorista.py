# -*- coding: utf-8 -*-
"""
nucleo/auth_motorista.py

Login do motorista no app: CPF + PIN de 6 dígitos (mesmo padrão do Fresh
Hub, sistema interno da Freshlog) sobre a tabela `motoristas` (desenho de
junho/2026, reaproveitada -- ver nucleo/banco.py).

- PIN nunca em texto puro: PBKDF2-HMAC-SHA256, 200k iterações, salt por
  motorista (colunas pin_hash / pin_salt já existentes).
- 5 erros seguidos bloqueiam por 15 min (mesma trava de
  confirmacao_motoristas/app.py, _MAX_TENTATIVAS).
- Tokens assinados com itsdangerous (já é dependência do projeto): acesso
  de 12h e refresh de 30 dias. O token carrega um prefixo do pin_hash --
  trocar o PIN invalida TODOS os tokens do motorista sem coluna extra.
"""
import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timedelta

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from nucleo import banco

PBKDF2_ITERACOES = 200_000
MAX_TENTATIVAS = 5
BLOQUEIO_MINUTOS = 15
ACESSO_SEGUNDOS = 12 * 3600
REFRESH_SEGUNDOS = 30 * 24 * 3600
_SALT_ACESSO = "motorista-acesso"
_SALT_REFRESH = "motorista-refresh"

PERFIL_MOTORISTA = "MOTORISTA"
PERFIL_TESTE = "TESTE"


class AutenticacaoInvalida(Exception):
    def __init__(self, mensagem: str, codigo: int = 401):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.codigo = codigo


def normalizar_cpf(valor: str | None) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def _hash_pin(pin: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ITERACOES).hex()


def validar_pin_formato(pin: str) -> str:
    pin = str(pin or "").strip()
    if not re.fullmatch(r"\d{6}", pin):
        raise ValueError("PIN precisa ter exatamente 6 dígitos.")
    return pin


def validar_pin_forte(pin: str) -> str:
    """PIN escolhido pelo motorista (revisão 08/10): além do formato, recusa
    os óbvios -- todos iguais (111111), sequência crescente ou decrescente
    (123456, 654321, 012345) e o provisório 123456. O PIN da operação
    (criar/resetar) não passa por aqui de propósito."""
    pin = validar_pin_formato(pin)
    digitos = [int(c) for c in pin]
    iguais = len(set(digitos)) == 1
    crescente = all(b - a == 1 for a, b in zip(digitos, digitos[1:]))
    decrescente = all(a - b == 1 for a, b in zip(digitos, digitos[1:]))
    if iguais or crescente or decrescente or pin == "123456":
        raise ValueError("PIN fácil demais: evite sequências (123456) e números repetidos (111111).")
    return pin


def criar_ou_atualizar_motorista(conn: sqlite3.Connection, cpf: str, nome: str, pin: str | None = None,
                                 agent_id: int | None = None, vehicle_id: int | None = None,
                                 telefone: str | None = None, email: str | None = None,
                                 tipo_veiculo: str | None = None, perfil: str = PERFIL_MOTORISTA,
                                 ativo: bool = True, trocar_pin: bool = False) -> dict:
    """Upsert por CPF. `pin` None em atualização mantém o PIN atual (e a
    flag trocar_pin); em criação é obrigatório. `trocar_pin=True` marca que
    o PIN gravado é provisório e o app exige a troca no próximo login."""
    cpf = normalizar_cpf(cpf)
    if len(cpf) != 11:
        raise ValueError("CPF precisa ter 11 dígitos.")
    existente = conn.execute("SELECT * FROM motoristas WHERE cpf = ?", (cpf,)).fetchone()
    if pin is not None:
        pin = validar_pin_formato(pin)
        salt = secrets.token_hex(16)
        pin_hash = _hash_pin(pin, salt)
        flag_trocar = int(bool(trocar_pin))
    elif existente:
        salt, pin_hash = existente["pin_salt"], existente["pin_hash"]
        flag_trocar = int(existente["trocar_pin"] or 0)
    else:
        raise ValueError("PIN é obrigatório pra criar o motorista.")

    agora = banco.agora()
    conn.execute("""
        INSERT INTO motoristas (cpf, nome, pin_hash, pin_salt, telefone, ativo, agent_id, vehicle_id, email,
                                tipo_veiculo, perfil, trocar_pin, tentativas_pin, bloqueado_ate, criado_em, atualizado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
        ON CONFLICT(cpf) DO UPDATE SET
            nome = excluded.nome, pin_hash = excluded.pin_hash, pin_salt = excluded.pin_salt,
            telefone = COALESCE(excluded.telefone, motoristas.telefone), ativo = excluded.ativo,
            agent_id = COALESCE(excluded.agent_id, motoristas.agent_id),
            vehicle_id = COALESCE(excluded.vehicle_id, motoristas.vehicle_id),
            email = COALESCE(excluded.email, motoristas.email),
            tipo_veiculo = COALESCE(excluded.tipo_veiculo, motoristas.tipo_veiculo),
            perfil = excluded.perfil, trocar_pin = excluded.trocar_pin,
            tentativas_pin = 0, bloqueado_ate = NULL, atualizado_em = excluded.atualizado_em
    """, (cpf, nome, pin_hash, salt, telefone, int(ativo), agent_id, vehicle_id, email, tipo_veiculo, perfil,
          flag_trocar, agora, agora))
    conn.commit()
    return dict(conn.execute("SELECT * FROM motoristas WHERE cpf = ?", (cpf,)).fetchone())


def trocar_pin(conn: sqlite3.Connection, cpf: str, pin_atual: str, pin_novo: str) -> dict:
    """Troca feita pelo próprio motorista no app. Confere o PIN atual pela
    mesma trava de tentativas do login; o novo precisa ter 6 dígitos e ser
    diferente. Limpa trocar_pin e, como o hash muda, derruba as sessões
    antigas (o app guarda os tokens novos que a API devolve)."""
    m = autenticar(conn, cpf, pin_atual)
    try:
        pin_novo = validar_pin_forte(pin_novo)
    except ValueError as e:
        raise AutenticacaoInvalida(str(e), 400)
    if pin_novo == str(pin_atual or "").strip():
        raise AutenticacaoInvalida("O PIN novo precisa ser diferente do atual.", 400)
    salt = secrets.token_hex(16)
    conn.execute("UPDATE motoristas SET pin_hash = ?, pin_salt = ?, trocar_pin = 0, atualizado_em = ? WHERE cpf = ?",
                 (_hash_pin(pin_novo, salt), salt, banco.agora(), m["cpf"]))
    conn.commit()
    return dict(conn.execute("SELECT * FROM motoristas WHERE cpf = ?", (m["cpf"],)).fetchone())


def buscar_motorista(conn: sqlite3.Connection, cpf: str) -> dict | None:
    row = conn.execute("SELECT * FROM motoristas WHERE cpf = ?", (normalizar_cpf(cpf),)).fetchone()
    return dict(row) if row else None


def autenticar(conn: sqlite3.Connection, cpf: str, pin: str) -> dict:
    """Valida CPF + PIN com trava de tentativas. Levanta AutenticacaoInvalida
    com mensagem segura (nunca diz se o CPF existe)."""
    generico = "CPF ou PIN incorretos."
    m = buscar_motorista(conn, cpf)
    if not m or not m.get("ativo"):
        raise AutenticacaoInvalida(generico)

    agora = datetime.now()
    bloqueado_ate = m.get("bloqueado_ate")
    if bloqueado_ate and datetime.strptime(bloqueado_ate, "%Y-%m-%d %H:%M:%S") > agora:
        raise AutenticacaoInvalida("Muitas tentativas. Tente de novo em alguns minutos.", 429)

    esperado = m["pin_hash"]
    if not hmac.compare_digest(_hash_pin(str(pin or ""), m["pin_salt"]), esperado):
        tentativas = int(m.get("tentativas_pin") or 0) + 1
        bloqueio = None
        if tentativas >= MAX_TENTATIVAS:
            bloqueio = (agora + timedelta(minutes=BLOQUEIO_MINUTOS)).strftime("%Y-%m-%d %H:%M:%S")
            tentativas = 0
        conn.execute("UPDATE motoristas SET tentativas_pin = ?, bloqueado_ate = ? WHERE cpf = ?",
                     (tentativas, bloqueio, m["cpf"]))
        conn.commit()
        raise AutenticacaoInvalida(generico)

    conn.execute("UPDATE motoristas SET tentativas_pin = 0, bloqueado_ate = NULL, ultimo_login_em = ? WHERE cpf = ?",
                 (banco.agora(), m["cpf"]))
    conn.commit()
    return m


# ── Tokens ─────────────────────────────────────────────────────────────────────

def _serializador(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret)


def _versao(m: dict) -> str:
    return (m.get("pin_hash") or "")[:12]


def emitir_tokens(secret: str, m: dict) -> dict:
    s = _serializador(secret)
    payload = {"cpf": m["cpf"], "v": _versao(m)}
    return {
        "acesso": s.dumps(payload, salt=_SALT_ACESSO),
        "refresh": s.dumps(payload, salt=_SALT_REFRESH),
        "expira_em_segundos": ACESSO_SEGUNDOS,
    }


def _validar_token(secret: str, token: str, salt: str, max_age: int, conn: sqlite3.Connection) -> dict:
    try:
        payload = _serializador(secret).loads(token, salt=salt, max_age=max_age)
    except SignatureExpired:
        raise AutenticacaoInvalida("Sessão expirada. Entre de novo.")
    except BadSignature:
        raise AutenticacaoInvalida("Token inválido.")
    m = buscar_motorista(conn, payload.get("cpf", ""))
    if not m or not m.get("ativo") or payload.get("v") != _versao(m):
        raise AutenticacaoInvalida("Sessão inválida. Entre de novo.")
    return m


def motorista_do_token_acesso(secret: str, token: str, conn: sqlite3.Connection) -> dict:
    return _validar_token(secret, token, _SALT_ACESSO, ACESSO_SEGUNDOS, conn)


def renovar(secret: str, refresh: str, conn: sqlite3.Connection) -> dict:
    m = _validar_token(secret, refresh, _SALT_REFRESH, REFRESH_SEGUNDOS, conn)
    return emitir_tokens(secret, m)


def publico(m: dict) -> dict:
    """Campos do motorista que o app pode ver (nunca hash/salt)."""
    return {
        "cpf": m["cpf"], "nome": m["nome"], "agent_id": m.get("agent_id"), "vehicle_id": m.get("vehicle_id"),
        "telefone": m.get("telefone"), "email": m.get("email"), "tipo_veiculo": m.get("tipo_veiculo"),
        "perfil": m.get("perfil") or PERFIL_MOTORISTA,
        "trocar_pin": bool(m.get("trocar_pin")),
        "chave_pix": m.get("chave_pix"), "placa": m.get("placa"),
    }
