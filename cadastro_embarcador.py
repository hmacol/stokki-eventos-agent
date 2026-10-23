# -*- coding: utf-8 -*-
"""
cadastro_embarcador.py

Andamento do pre-cadastro automatico de embarcador novo da Stokki (pedido do
Hugo, 07/10/2026; spec em
docs/superpowers/specs/2026-10-08-embarcadores-novos-stokki-design.md).

Uma linha por CNPJ. Quem escreve:
- embarcadores_stokki.py (rotina de hora em hora): cria e convida;
- portal (tela /completar-cadastro): o cliente informa o tipo de carga;
- painel /inicio e gerenciar_clientes.py cadastro: aprovacao.

interno.habilidade so muda em aprovar(): o tipo informado pelo cliente nao
vale no roteirizador antes disso. interno.email nao espera: nasce com o da
Stokki e vira o do cliente em salvar_preenchimento() (Hugo, 17/10).
"""
import re
from datetime import datetime

INATIVO = "INATIVO"          # sem pedido nos ultimos 30 dias: sem convite
PENDENTE = "PENDENTE"        # ativo, ainda sem sender ou sem convite
CONVIDADO = "CONVIDADO"
REENVIADO = "REENVIADO"
SEM_CONTATO = "SEM_CONTATO"  # nem WhatsApp valido nem e-mail na Stokki
PREENCHIDO = "PREENCHIDO"    # cliente informou, falta a aprovacao
APROVADO = "APROVADO"

ABERTOS_PRO_CLIENTE = (PENDENTE, CONVIDADO, REENVIADO, SEM_CONTATO)
HABILIDADES = ("Seco", "Refrigerado", "Congelado")
DESCRICOES_HABILIDADE = {
    "Seco": "Temperatura ambiente: bebidas, mercearia, embalagens.",
    "Refrigerado": "Precisa de geladeira: laticínios, frios, hortifrúti.",
    "Congelado": "Precisa ficar congelado: sorvetes, carnes e massas congeladas.",
}

_DDL = """
CREATE TABLE IF NOT EXISTS cadastro_embarcador (
    cnpj_embarcador      TEXT PRIMARY KEY,
    stkkc_id             INTEGER NOT NULL,
    celular_stokki       TEXT NOT NULL DEFAULT '',
    email_stokki         TEXT NOT NULL DEFAULT '',
    situacao             TEXT NOT NULL,
    canal_convite        TEXT,
    convite_enviado_em   TEXT,
    reenvio_em           TEXT,
    habilidade_informada TEXT,
    preenchido_em        TEXT,
    aprovado_em          TEXT,
    aprovado_por         TEXT,
    criado_em            TEXT NOT NULL,
    atualizado_em        TEXT NOT NULL
)"""

_EDITAVEIS = ("stkkc_id", "celular_stokki", "email_stokki", "situacao", "canal_convite", "convite_enviado_em",
              "reenvio_em", "habilidade_informada", "preenchido_em", "aprovado_em", "aprovado_por")


class CadastroInvalido(ValueError):
    def __init__(self, mensagem: str, codigo: int = 400):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.codigo = codigo


def _cnpj(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def _fmt(agora: datetime | None) -> str:
    return (agora or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _como_dicts(cur) -> list[dict]:
    colunas = [d[0] for d in cur.description]
    return [dict(zip(colunas, tuple(r))) for r in cur.fetchall()]


def garantir_tabela(conn) -> None:
    conn.execute(_DDL)


def buscar(conn, cnpj) -> dict | None:
    garantir_tabela(conn)
    linhas = _como_dicts(conn.execute("SELECT * FROM cadastro_embarcador WHERE cnpj_embarcador = ?", (_cnpj(cnpj),)))
    return linhas[0] if linhas else None


def registrar(conn, cnpj, stkkc_id: int, situacao: str, celular: str = "", email: str = "",
              agora: datetime | None = None) -> dict:
    """Cria a linha. Ja existindo, nao mexe (a rotina pode passar duas vezes)."""
    garantir_tabela(conn)
    momento = _fmt(agora)
    conn.execute(
        "INSERT OR IGNORE INTO cadastro_embarcador (cnpj_embarcador, stkkc_id, celular_stokki, email_stokki, situacao, "
        "criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_cnpj(cnpj), int(stkkc_id), str(celular or ""), str(email or ""), situacao, momento, momento))
    conn.commit()
    return buscar(conn, cnpj)


def atualizar(conn, cnpj, agora: datetime | None = None, **campos) -> dict:
    desconhecidos = [c for c in campos if c not in _EDITAVEIS]
    if desconhecidos:
        raise ValueError(f"Campo desconhecido em cadastro_embarcador: {', '.join(desconhecidos)}")
    garantir_tabela(conn)
    if campos:
        atribuicoes = ", ".join(f"{c} = ?" for c in campos)
        conn.execute(f"UPDATE cadastro_embarcador SET {atribuicoes}, atualizado_em = ? WHERE cnpj_embarcador = ?",
                     (*campos.values(), _fmt(agora), _cnpj(cnpj)))
        conn.commit()
    return buscar(conn, cnpj)


def normalizar_habilidade(valor) -> str:
    tipo = str(valor or "").strip().capitalize()
    if tipo not in HABILIDADES:
        raise CadastroInvalido("Escolha o tipo de carga: Seco, Refrigerado ou Congelado.")
    return tipo


def precisa_completar(conn, cnpj) -> bool:
    c = buscar(conn, cnpj)
    return bool(c and c["situacao"] in ABERTOS_PRO_CLIENTE)


def salvar_preenchimento(conn, cnpj, habilidade, agora: datetime | None = None) -> dict:
    tipo = normalizar_habilidade(habilidade)
    c = buscar(conn, cnpj)
    if not c:
        raise CadastroInvalido("Cadastro não encontrado.", 404)
    if c["situacao"] == APROVADO:
        raise CadastroInvalido("Cadastro já aprovado.", 409)
    # E-mails que o cliente informou valem ja, sem esperar a aprovacao (Hugo,
    # 17/10); so o tipo de carga espera. Quem chama salvou as preferencias antes.
    import preferencias_notificacao as pn
    emails = pn.ler(conn, c["cnpj_embarcador"])["emails"]
    if emails:
        conn.execute("UPDATE interno SET email = ?, atualizado_em = ? WHERE cnpj_embarcador = ?",
                     (", ".join(emails), _fmt(agora), c["cnpj_embarcador"]))
    return atualizar(conn, cnpj, agora, habilidade_informada=tipo, preenchido_em=_fmt(agora), situacao=PREENCHIDO)


def aprovar(conn, cnpj, por: str, habilidade: str | None = None, agora: datetime | None = None) -> dict:
    """Grava interno.habilidade. Sem `habilidade`, usa o que o cliente
    informou (exige PREENCHIDO); com ela, a equipe corrige o tipo."""
    c = buscar(conn, cnpj)
    if not c:
        raise CadastroInvalido("Cadastro não encontrado.", 404)
    if habilidade is None:
        if c["situacao"] != PREENCHIDO:
            raise CadastroInvalido("O cliente ainda não informou o tipo de carga.", 409)
        tipo = c["habilidade_informada"]
    else:
        tipo = normalizar_habilidade(habilidade)
    cur = conn.execute("UPDATE interno SET habilidade = ?, atualizado_em = ? WHERE cnpj_embarcador = ?",
                       (tipo, _fmt(agora), c["cnpj_embarcador"]))
    if cur.rowcount == 0:
        conn.rollback()
        raise CadastroInvalido("Embarcador não está em interno.", 404)
    if not (conn.execute("SELECT email FROM interno WHERE cnpj_embarcador = ?",
                         (c["cnpj_embarcador"],)).fetchone()[0] or "").strip():
        import preferencias_notificacao as pn
        emails = pn.ler(conn, c["cnpj_embarcador"])["emails"] or ([c["email_stokki"]] if c["email_stokki"] else [])
        if emails:
            conn.execute("UPDATE interno SET email = ? WHERE cnpj_embarcador = ?",
                         (", ".join(emails), c["cnpj_embarcador"]))
    return atualizar(conn, cnpj, agora, situacao=APROVADO, aprovado_em=_fmt(agora),
                     aprovado_por=str(por or "")[:40], habilidade_informada=tipo)


def listar_abertos(conn) -> list[dict]:
    """Tudo que nao esta APROVADO nem INATIVO, PREENCHIDO primeiro."""
    garantir_tabela(conn)
    return _como_dicts(conn.execute("""
        SELECT c.*, COALESCE(i.apelido, i.nome_remetente, c.cnpj_embarcador) AS nome
        FROM cadastro_embarcador c LEFT JOIN interno i ON i.cnpj_embarcador = c.cnpj_embarcador
        WHERE c.situacao NOT IN ('APROVADO', 'INATIVO')
        ORDER BY CASE c.situacao WHEN 'PREENCHIDO' THEN 0 ELSE 1 END, c.criado_em, c.cnpj_embarcador
    """))
