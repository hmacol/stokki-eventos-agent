# -*- coding: utf-8 -*-
"""
nucleo/cadastro_motorista.py

Cadastro do motorista pelo app (Hugo, 08/10/2026):

- "Meus dados": telefone, e-mail, chave PIX e placa valem na hora
  (decisão do Hugo), com histórico em `motoristas_alteracoes`. Telefone,
  e-mail e placa são espelhados na BD_MOTORISTAS.xlsx pela API (a planilha
  continua mandando na roteirização).
- Auto-cadastro (`motoristas_cadastros`): o motorista novo manda dados,
  foto da CNH e do CRLV; o Hugo aprova no painel escolhendo o agente
  Vuupt (criado à mão, como na tela /motoristas), o que grava a linha na
  planilha e cria o login com PIN provisório (trocar_pin=1).

Só regra e banco: a API (nucleo/api_motorista.py) e o painel chamam daqui.
"""
import hmac
import logging
import re
import secrets
import sqlite3
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ / "roteirizacao") not in sys.path:
    sys.path.append(str(_RAIZ / "roteirizacao"))

from nucleo import auth_motorista as auth, banco  # noqa: E402
from regras.tipo_veiculo import tipo_por_codigo  # noqa: E402
from zonas_sp import (  # noqa: E402
    ABCD, CENTRO, COTIA_EMBU_TABOAO, GUARULHOS, OSASCO_BARUERI_ALPHAVILLE,
    ZONA_LESTE, ZONA_NORTE, ZONA_OESTE, ZONA_SUL,
)

logger = logging.getLogger("nucleo.cadastro_motorista")

STATUS_PENDENTE = "PENDENTE"
STATUS_APROVADO = "APROVADO"
STATUS_RECUSADO = "RECUSADO"

ZONAS = [ZONA_NORTE, ZONA_SUL, ZONA_LESTE, ZONA_OESTE, CENTRO, GUARULHOS, ABCD,
         OSASCO_BARUERI_ALPHAVILLE, COTIA_EMBU_TABOAO]
DIAS = ["SEGUNDA", "TERCA", "QUARTA", "QUINTA", "SEXTA", "SABADO", "DOMINGO"]
DOCUMENTOS = ("cnh", "crlv")
EXTENSOES_FOTO = {"jpg", "jpeg", "png", "webp"}
MAX_FOTO_BYTES = 10 * 1024 * 1024
CAMPOS_EDITAVEIS = ("telefone", "email", "chave_pix", "placa")
# PIN provisório da aprovação: sorteado por motorista (o painel mostra uma
# vez e o Hugo manda no WhatsApp); o app obriga a troca no 1º login.
# O lote de 08/10 usou 123456 por decisão do Hugo; aqui não há motivo pra
# repetir um PIN conhecido.
def sortear_pin() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(6))
ROTULOS = {"telefone": "Telefone", "email": "E-mail", "chave_pix": "Chave PIX", "placa": "Placa"}


class CadastroInvalido(Exception):
    def __init__(self, mensagem: str, codigo: int = 400):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.codigo = codigo


def pasta_cadastros() -> Path:
    return Path(banco.DB_PATH).parent / "cadastros_motoristas"


# ── Validação dos campos ───────────────────────────────────────────────────────

def validar_telefone(valor) -> str | None:
    d = re.sub(r"\D", "", str(valor or ""))
    if not d:
        return None
    if len(d) in (12, 13) and d.startswith("55"):
        d = d[2:]
    if len(d) not in (10, 11):
        raise CadastroInvalido("Telefone precisa ter DDD + número (10 ou 11 dígitos).")
    return d


def validar_email(valor) -> str | None:
    v = str(valor or "").strip().lower()
    if not v:
        return None
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v) or len(v) > 120:
        raise CadastroInvalido("E-mail inválido.")
    return v


def validar_placa(valor) -> str | None:
    v = re.sub(r"[\s\-]", "", str(valor or "")).upper()
    if not v:
        return None
    if not re.fullmatch(r"[A-Z]{3}\d[A-Z0-9]\d{2}", v):
        raise CadastroInvalido("Placa inválida: use o formato ABC1234 ou ABC1D23.")
    return v


def validar_pix(valor) -> str | None:
    v = str(valor or "").strip()
    if not v:
        return None
    if len(v) > 120:
        raise CadastroInvalido("Chave PIX longa demais.")
    return v


VALIDADORES = {"telefone": validar_telefone, "email": validar_email, "chave_pix": validar_pix, "placa": validar_placa}


def _lista(valor, permitidos: list[str], rotulo: str) -> list[str]:
    if isinstance(valor, str):
        valor = [p for p in valor.split(",")]
    itens = []
    for v in valor or []:
        v = str(v or "").strip().upper()
        if not v:
            continue
        if v not in permitidos:
            raise CadastroInvalido(f"{rotulo} não reconhecido: {v}.")
        if v not in itens:
            itens.append(v)
    return itens


# ── Meus dados (motorista já com login) ───────────────────────────────────────

def atualizar_meus_dados(conn: sqlite3.Connection, cpf: str, dados: dict) -> tuple[dict, list[dict]]:
    """Aplica só os campos presentes em `dados`. Devolve (motorista, mudanças);
    cada mudança = {campo, rotulo, de, para}. Nada muda = lista vazia."""
    m = auth.buscar_motorista(conn, cpf)
    if not m:
        raise CadastroInvalido("Motorista não encontrado.", 404)
    mudancas = []
    for campo in CAMPOS_EDITAVEIS:
        if campo not in (dados or {}):
            continue
        novo = VALIDADORES[campo](dados[campo])
        if novo != (m.get(campo) or None):
            mudancas.append({"campo": campo, "rotulo": ROTULOS[campo], "de": m.get(campo) or None, "para": novo})
    if not mudancas:
        return m, []
    agora = banco.agora()
    for mud in mudancas:
        conn.execute(f"UPDATE motoristas SET {mud['campo']} = ?, atualizado_em = ? WHERE cpf = ?",
                     (mud["para"], agora, m["cpf"]))
        conn.execute("INSERT INTO motoristas_alteracoes (cpf, campo, valor_antigo, valor_novo, alterado_em) VALUES (?, ?, ?, ?, ?)",
                     (m["cpf"], mud["campo"], mud["de"], mud["para"], agora))
    conn.commit()
    logger.info("motorista %s alterou %s", m["cpf"], ", ".join(x["campo"] for x in mudancas))
    return auth.buscar_motorista(conn, cpf), mudancas


def historico(conn: sqlite3.Connection, cpf: str, limite: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM motoristas_alteracoes WHERE cpf = ? ORDER BY id DESC LIMIT ?",
                        (auth.normalizar_cpf(cpf), limite)).fetchall()
    return [dict(r) for r in rows]


# ── Auto-cadastro ─────────────────────────────────────────────────────────────

def _montar(row) -> dict:
    c = dict(row)
    c["zonas"] = [z for z in (c.get("zonas") or "").split(",") if z]
    c["dias"] = [d for d in (c.get("dias") or "").split(",") if d]
    c["aceita_viagens"] = bool(c.get("aceita_viagens"))
    c["dono_veiculo"] = bool(c.get("dono_veiculo", 1))
    c["tem_cnh"] = bool(c.get("cnh_arquivo"))
    c["tem_crlv"] = bool(c.get("crlv_arquivo"))
    c["completo"] = c["tem_cnh"] and c["tem_crlv"]
    c.pop("chave_envio", None)
    return c


def buscar_cadastro(conn: sqlite3.Connection, cadastro_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM motoristas_cadastros WHERE id = ?", (cadastro_id,)).fetchone()
    return _montar(row) if row else None


def listar_pendentes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM motoristas_cadastros WHERE status = ? ORDER BY id", (STATUS_PENDENTE,)).fetchall()
    return [_montar(r) for r in rows]


def contar_pendentes(conn: sqlite3.Connection, so_completos: bool = True) -> int:
    sql = "SELECT COUNT(*) FROM motoristas_cadastros WHERE status = ?"
    if so_completos:
        sql += " AND cnh_arquivo IS NOT NULL AND crlv_arquivo IS NOT NULL"
    return conn.execute(sql, (STATUS_PENDENTE,)).fetchone()[0]


def criar_cadastro(conn: sqlite3.Connection, dados: dict) -> dict:
    """Pedido de cadastro vindo do app (sem login). Devolve {id, chave_envio};
    a chave autoriza só o envio dos documentos desse cadastro."""
    dados = dados or {}
    cpf = auth.normalizar_cpf(dados.get("cpf"))
    if len(cpf) != 11:
        raise CadastroInvalido("CPF precisa ter 11 dígitos.")
    nome = re.sub(r"\s+", " ", str(dados.get("nome") or "")).strip()
    if len(nome) < 5 or " " not in nome:
        raise CadastroInvalido("Informe o nome completo.")
    telefone = validar_telefone(dados.get("telefone"))
    if not telefone:
        raise CadastroInvalido("Telefone é obrigatório (é por ele que a Fresh Log manda o PIN).")
    email = validar_email(dados.get("email"))
    chave_pix = validar_pix(dados.get("chave_pix"))
    if not chave_pix:
        raise CadastroInvalido("Chave PIX é obrigatória.")
    placa = validar_placa(dados.get("placa"))
    if not placa:
        raise CadastroInvalido("Placa é obrigatória.")
    tipo_veiculo = str(dados.get("tipo_veiculo") or "").strip().upper()
    if not tipo_veiculo or not tipo_por_codigo(tipo_veiculo):
        raise CadastroInvalido("Escolha o tipo de veículo.")
    zonas = _lista(dados.get("zonas"), ZONAS, "Zona")
    if not zonas:
        raise CadastroInvalido("Marque pelo menos uma zona que você atende.")
    dias = _lista(dados.get("dias"), DIAS, "Dia")
    if not dias:
        raise CadastroInvalido("Marque pelo menos um dia da semana.")
    aceita_viagens = bool(dados.get("aceita_viagens"))
    dono_veiculo = bool(dados.get("dono_veiculo", True))

    if auth.buscar_motorista(conn, cpf):
        raise CadastroInvalido("Este CPF já tem cadastro. Entre com o seu PIN ou fale com a Fresh Log.", 409)
    if conn.execute("SELECT 1 FROM motoristas_cadastros WHERE cpf = ? AND status = ?", (cpf, STATUS_PENDENTE)).fetchone():
        raise CadastroInvalido("Já existe um cadastro em análise para este CPF. Aguarde o retorno da Fresh Log.", 409)

    chave = secrets.token_urlsafe(24)
    cur = conn.execute("""
        INSERT INTO motoristas_cadastros (cpf, nome, telefone, email, chave_pix, placa, tipo_veiculo, zonas, dias,
                                          aceita_viagens, dono_veiculo, chave_envio, status, criado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (cpf, nome, telefone, email, chave_pix, placa, tipo_veiculo, ",".join(zonas), ",".join(dias),
          int(aceita_viagens), int(dono_veiculo), chave, STATUS_PENDENTE, banco.agora()))
    conn.commit()
    logger.info("cadastro #%s criado pra %s (%s)", cur.lastrowid, nome, tipo_veiculo)
    return {"id": cur.lastrowid, "chave_envio": chave}


def guardar_documento(conn: sqlite3.Connection, cadastro_id: int, chave_envio: str, tipo: str,
                      nome_arquivo: str, conteudo: bytes) -> dict:
    """Foto da CNH ou do CRLV. A chave_envio é a prova de que é o mesmo
    aparelho que criou o cadastro. Troca a foto se mandar de novo."""
    row = conn.execute("SELECT * FROM motoristas_cadastros WHERE id = ?", (cadastro_id,)).fetchone()
    if not row or not hmac.compare_digest(str(row["chave_envio"]), str(chave_envio or "")):
        raise CadastroInvalido("Cadastro não encontrado.", 404)
    if row["status"] != STATUS_PENDENTE:
        raise CadastroInvalido("Este cadastro já foi avaliado.", 409)
    tipo = str(tipo or "").lower()
    if tipo not in DOCUMENTOS:
        raise CadastroInvalido("Documento precisa ser 'cnh' ou 'crlv'.")
    ext = (Path(nome_arquivo or "").suffix.lstrip(".") or "jpg").lower()
    if ext not in EXTENSOES_FOTO:
        raise CadastroInvalido("Mande a foto em JPG ou PNG.")
    if not conteudo:
        raise CadastroInvalido("Arquivo vazio.")
    if len(conteudo) > MAX_FOTO_BYTES:
        raise CadastroInvalido("Foto grande demais (máx. 10 MB).", 413)
    pasta = pasta_cadastros() / str(cadastro_id)
    pasta.mkdir(parents=True, exist_ok=True)
    nome = f"{tipo}.{ext}"
    (pasta / nome).write_bytes(conteudo)
    conn.execute(f"UPDATE motoristas_cadastros SET {tipo}_arquivo = ? WHERE id = ?", (nome, cadastro_id))
    conn.commit()
    return buscar_cadastro(conn, cadastro_id)


def caminho_documento(conn: sqlite3.Connection, cadastro_id: int, tipo: str) -> Path | None:
    if tipo not in DOCUMENTOS:
        return None
    c = buscar_cadastro(conn, cadastro_id)
    nome = c and c.get(f"{tipo}_arquivo")
    if not nome or "/" in nome or "\\" in nome:
        return None
    p = pasta_cadastros() / str(cadastro_id) / nome
    return p if p.is_file() else None


def marcar_avisado(conn: sqlite3.Connection, cadastro_id: int) -> None:
    conn.execute("UPDATE motoristas_cadastros SET avisado_em = ? WHERE id = ? AND avisado_em IS NULL",
                 (banco.agora(), cadastro_id))
    conn.commit()


def aprovar(conn: sqlite3.Connection, cadastro_id: int, config: dict, *, agent_id, zonas, dias, tipo_veiculo,
            aceita_viagens: bool, revisado_por: str, pin_inicial: str | None = None,
            gravar_planilha=None, dono_veiculo: bool | None = None) -> dict:
    """Aprovação pelo painel: linha na BD_MOTORISTAS (regras/cadastro_motoristas)
    + login no app com PIN provisório sorteado (devolvido UMA vez em `pin`).
    `gravar_planilha` e `pin_inicial` só existem pros testes."""
    pin_inicial = pin_inicial or sortear_pin()
    c = buscar_cadastro(conn, cadastro_id)
    if not c:
        raise CadastroInvalido("Cadastro não encontrado.", 404)
    if c["status"] != STATUS_PENDENTE:
        raise CadastroInvalido("Este cadastro já foi avaliado.", 409)
    if not c["completo"]:
        raise CadastroInvalido("Faltam documentos (CNH e CRLV) -- peça ao motorista pra reenviar.")
    try:
        agent_id = int(agent_id)
    except (TypeError, ValueError):
        raise CadastroInvalido("Escolha o agente da Vuupt.")
    zonas = _lista(zonas if zonas is not None else c["zonas"], ZONAS, "Zona")
    dias = _lista(dias if dias is not None else c["dias"], DIAS, "Dia")
    tipo_veiculo = str(tipo_veiculo or c["tipo_veiculo"] or "").upper()
    if not zonas or not dias or not tipo_por_codigo(tipo_veiculo):
        raise CadastroInvalido("Zonas, dias e tipo de veículo são obrigatórios.")
    if auth.buscar_motorista(conn, c["cpf"]):
        raise CadastroInvalido("Este CPF já tem login no app.", 409)

    if gravar_planilha is None:
        from regras.cadastro_motoristas import cadastrar_motorista
        gravar_planilha = cadastrar_motorista
    gravar_planilha(config, {
        "agent_id": agent_id, "nome": c["nome"], "aceita_viagens": bool(aceita_viagens), "ativo": True,
        "max_rotas_dia": 1, "dias_disponiveis": dias, "zonas_preferidas": zonas, "tipo_veiculo": tipo_veiculo,
        "telefone": c["telefone"], "email": c["email"], "placa": c["placa"], "cpf": c["cpf"],
    })
    m = auth.criar_ou_atualizar_motorista(conn, c["cpf"], c["nome"], pin_inicial, agent_id=agent_id,
                                          telefone=c["telefone"], email=c["email"], tipo_veiculo=tipo_veiculo,
                                          trocar_pin=True)
    agora = banco.agora()
    dono = c["dono_veiculo"] if dono_veiculo is None else bool(dono_veiculo)
    conn.execute("UPDATE motoristas SET chave_pix = ?, placa = ?, ve_financeiro = ?, atualizado_em = ? WHERE cpf = ?",
                 (c["chave_pix"], c["placa"], int(dono), agora, c["cpf"]))
    conn.execute("""UPDATE motoristas_cadastros SET status = ?, agent_id = ?, zonas = ?, dias = ?, tipo_veiculo = ?,
                    aceita_viagens = ?, dono_veiculo = ?, revisado_em = ?, revisado_por = ? WHERE id = ?""",
                 (STATUS_APROVADO, agent_id, ",".join(zonas), ",".join(dias), tipo_veiculo, int(bool(aceita_viagens)),
                  int(dono), agora, revisado_por, cadastro_id))
    conn.commit()
    logger.info("cadastro #%s aprovado por %s: %s -> agent %s", cadastro_id, revisado_por, c["nome"], agent_id)
    m = auth.buscar_motorista(conn, c["cpf"])
    return {"cadastro": buscar_cadastro(conn, cadastro_id), "motorista": auth.publico(m), "pin": pin_inicial}


def definir_ve_financeiro(conn: sqlite3.Connection, cpf: str, ve: bool) -> dict:
    """Chave "Vê financeiro" da tela /motoristas (Hugo, 08/10): motorista que
    dirige carro de outro não vê o extrato no app; pedágio continua."""
    m = auth.buscar_motorista(conn, cpf)
    if not m:
        raise CadastroInvalido("Motorista sem login no app.", 404)
    conn.execute("UPDATE motoristas SET ve_financeiro = ?, atualizado_em = ? WHERE cpf = ?",
                 (int(bool(ve)), banco.agora(), m["cpf"]))
    conn.commit()
    return auth.buscar_motorista(conn, cpf)


def logins_por_agent_id(conn: sqlite3.Connection) -> dict[int, dict]:
    """Pra tela /motoristas cruzar a planilha com quem tem login no app."""
    rows = conn.execute("SELECT cpf, agent_id, ve_financeiro, ultimo_login_em, trocar_pin FROM motoristas "
                        "WHERE agent_id IS NOT NULL AND ativo = 1").fetchall()
    return {int(r["agent_id"]): dict(r) for r in rows}


def recusar(conn: sqlite3.Connection, cadastro_id: int, motivo: str, revisado_por: str) -> dict:
    c = buscar_cadastro(conn, cadastro_id)
    if not c:
        raise CadastroInvalido("Cadastro não encontrado.", 404)
    if c["status"] != STATUS_PENDENTE:
        raise CadastroInvalido("Este cadastro já foi avaliado.", 409)
    conn.execute("UPDATE motoristas_cadastros SET status = ?, motivo = ?, revisado_em = ?, revisado_por = ? WHERE id = ?",
                 (STATUS_RECUSADO, (motivo or "").strip() or None, banco.agora(), revisado_por, cadastro_id))
    conn.commit()
    return buscar_cadastro(conn, cadastro_id)
