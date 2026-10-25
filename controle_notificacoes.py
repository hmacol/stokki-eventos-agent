# -*- coding: utf-8 -*-
"""
controle_notificacoes.py

Controle central dos envios a destinatarios EXTERNOS (embarcador,
transportadora, futuro cliente). Spec:
docs/superpowers/specs/2026-10-24-tela-notificacoes-design.md.

Toda rotina que manda e-mail/WhatsApp pra fora pergunta a decidir(...) se
envia de verdade (ENVIAR), manda so pro piloto (PILOTO, o Hugo) ou nao envia
(NAO). Ordem da decisao:

  1. Geral desligado                -> NAO
  2. Tipo sem linha na tela         -> vale o config (fallback da rotina)
  3. Excecao do destinatario BLOQUEADO -> NAO
  4. Estado do tipo: DESLIGADO -> NAO; LIGADO -> ENVIAR;
     PILOTO -> ENVIAR se a excecao for LIBERADO, senao PILOTO
     (NAO se o piloto nao tem destino no canal).
  Erro de banco -> vale o config e loga aviso (nunca derruba a rotina).

A preferencia do cliente no portal (preferencias_notificacao) continua sendo
checada pela rotina: a tela e uma trava POR CIMA, nunca a forca.

E-mails a equipe interna, ao motorista e ao financeiro nao passam por aqui.
"""
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "dados" / "dados.db"

ACOES = ("ENVIAR", "PILOTO", "NAO")
ESTADOS = ("DESLIGADO", "PILOTO", "LIGADO")
REGRAS = ("BLOQUEADO", "LIBERADO")
CANAIS = ("email", "whatsapp")

_GERAL = "_geral"
_PILOTO = "_piloto"
_PILOTO_EMAIL_PADRAO = "hugo@freshlogbr.com"

# Ordem = ordem das linhas na tela. destino: quem recebe (define como a chave
# da excecao e normalizada). quando: texto livre exibido na tela.
TIPOS = {
    "nfs_em_rota": {"rotulo": "Notas em rota", "destino": "embarcador",
                    "canais": ("email",), "quando": "timer 07h"},
    "entrega_concluida": {"rotulo": "Entrega concluída", "destino": "embarcador",
                          "canais": ("email",), "quando": "timer frequente"},
    "resumo_diario": {"rotulo": "Resumo diário", "destino": "embarcador",
                      "canais": ("email",), "quando": "timer 20h"},
    "insucesso": {"rotulo": "Insucesso aguardando retorno", "destino": "embarcador",
                  "canais": ("email",), "quando": "dentro da expedição"},
    "pedidos_em_espera": {"rotulo": "Pedidos em espera", "destino": "embarcador",
                          "canais": ("email",), "quando": "timer 08:20 e 15:20"},
    "agendamento": {"rotulo": "Agendamento", "destino": "embarcador",
                    "canais": ("email",), "quando": "dentro das sequências"},
    "fora_dia_fixo": {"rotulo": "Fora do dia fixo", "destino": "embarcador",
                      "canais": ("email", "whatsapp"), "quando": "dentro das sequências"},
    "faltas_recebimento": {"rotulo": "Faltas no recebimento", "destino": "embarcador",
                           "canais": ("email",), "quando": "ao encerrar o recebimento"},
    "whatsapp_sem_resposta": {"rotulo": "Chamado sem resposta (WhatsApp)", "destino": "embarcador",
                              "canais": ("whatsapp",), "quando": "timer de 2 min"},
    "chamados": {"rotulo": "Chamados", "destino": "embarcador",
                 "canais": ("email",), "quando": "na hora do evento"},
    "area_nao_atendida": {"rotulo": "Área não atendida", "destino": "embarcador",
                          "canais": ("email",), "quando": "na hora do envio do XML"},
    "transportadoras": {"rotulo": "Transportadoras", "destino": "transportadora",
                        "canais": ("email",), "quando": "timer 04h10"},
    "convite_embarcador": {"rotulo": "Convite ao embarcador novo", "destino": "embarcador",
                           "canais": ("email", "whatsapp"), "quando": "timer 07h10"},
    "redespacho": {"rotulo": "Redespacho", "destino": "embarcador",
                   "canais": ("email",), "quando": "dentro do pipeline"},
    "consulta_transportadora": {"rotulo": "Consulta à transportadora", "destino": "embarcador",
                                "canais": ("email", "whatsapp"), "quando": "dentro do pipeline e lembrete"},
}


@dataclass(frozen=True)
class Decisao:
    acao: str            # ENVIAR | PILOTO | NAO
    destino_piloto: str  # e-mail/numero do piloto (so quando acao == PILOTO)
    origem: str          # "tela" | "config"
    motivo: str


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _so_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def _normalizar_destino(tipo: str, destino) -> str:
    """Embarcador = so digitos (CNPJ); transportadora = nome em minusculas."""
    if TIPOS[tipo]["destino"] == "embarcador":
        return _so_digitos(destino)
    return str(destino or "").strip().lower()


def acao_do_config(ativo: bool, forcar: str) -> str:
    """Traduz o par antigo do config (ativo, forcar_destino) em acao."""
    if not ativo:
        return "NAO"
    return "PILOTO" if str(forcar or "").strip() else "ENVIAR"


def _garantir_tabelas(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notificacoes_controle ("
        "tipo TEXT PRIMARY KEY, estado TEXT NOT NULL, atualizado_em TEXT, atualizado_por TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notificacoes_controle_excecao ("
        "tipo TEXT, destino TEXT, regra TEXT NOT NULL, atualizado_em TEXT, atualizado_por TEXT, "
        "PRIMARY KEY(tipo, destino))")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notificacoes_controle_param ("
        "chave TEXT PRIMARY KEY, valor TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notificacoes_controle_historico ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, quando TEXT, por TEXT, alvo TEXT, destino TEXT, "
        "de TEXT, para TEXT)")


def conectar(db_path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path) if db_path else DB_PATH, timeout=10)
    try:
        _garantir_tabelas(conn)
        conn.commit()
    except sqlite3.Error:
        conn.close()
        raise
    return conn


# ----------------------------------------------------------------- leitura

def estado_geral(conn: sqlite3.Connection) -> str | None:
    return estado_tipo(conn, _GERAL)


def estado_tipo(conn: sqlite3.Connection, tipo: str) -> str | None:
    row = conn.execute("SELECT estado FROM notificacoes_controle WHERE tipo = ?", (tipo,)).fetchone()
    return row[0] if row else None


def excecoes(conn: sqlite3.Connection, tipo: str) -> dict:
    return {d: r for d, r in conn.execute(
        "SELECT destino, regra FROM notificacoes_controle_excecao WHERE tipo = ? ORDER BY destino", (tipo,))}


def piloto(conn: sqlite3.Connection) -> dict:
    params = dict(conn.execute(
        "SELECT chave, valor FROM notificacoes_controle_param WHERE chave IN ('piloto_email', 'piloto_whatsapp')"))
    return {"email": params.get("piloto_email", _PILOTO_EMAIL_PADRAO),
            "whatsapp": params.get("piloto_whatsapp", "")}


def historico(conn: sqlite3.Connection, limite: int = 50) -> list[dict]:
    """Mais recente primeiro."""
    rows = conn.execute(
        "SELECT id, quando, por, alvo, destino, de, para FROM notificacoes_controle_historico "
        "ORDER BY id DESC LIMIT ?", (int(limite),)).fetchall()
    return [dict(zip(("id", "quando", "por", "alvo", "destino", "de", "para"), r)) for r in rows]


# ---------------------------------------------------------------- mutacoes

def _registrar(conn, por, alvo, destino, de, para) -> None:
    conn.execute(
        "INSERT INTO notificacoes_controle_historico (quando, por, alvo, destino, de, para) "
        "VALUES (?, ?, ?, ?, ?, ?)", (_agora(), str(por or "")[:40], alvo, destino, de, para))


def _gravar_estado(conn, alvo: str, estado: str | None, por: str) -> None:
    antes = estado_tipo(conn, alvo)
    if antes == estado:
        return
    if estado is None:
        conn.execute("DELETE FROM notificacoes_controle WHERE tipo = ?", (alvo,))
    else:
        conn.execute(
            "INSERT INTO notificacoes_controle (tipo, estado, atualizado_em, atualizado_por) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tipo) DO UPDATE SET estado = excluded.estado, "
            "atualizado_em = excluded.atualizado_em, atualizado_por = excluded.atualizado_por",
            (alvo, estado, _agora(), str(por or "")[:40]))
    _registrar(conn, por, alvo, None, antes, estado)
    conn.commit()


def _gravar_excecao(conn, tipo: str, destino: str, regra: str | None, por: str) -> None:
    antes = excecoes(conn, tipo).get(destino)
    if antes == regra:
        return
    if regra is None:
        conn.execute("DELETE FROM notificacoes_controle_excecao WHERE tipo = ? AND destino = ?", (tipo, destino))
    else:
        conn.execute(
            "INSERT INTO notificacoes_controle_excecao (tipo, destino, regra, atualizado_em, atualizado_por) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(tipo, destino) DO UPDATE SET regra = excluded.regra, "
            "atualizado_em = excluded.atualizado_em, atualizado_por = excluded.atualizado_por",
            (tipo, destino, regra, _agora(), str(por or "")[:40]))
    _registrar(conn, por, tipo, destino, antes, regra)
    conn.commit()


def _gravar_param(conn, campo: str, valor: str | None, por: str) -> None:
    """campo: 'email' ou 'whatsapp'. valor None apaga (volta ao padrao)."""
    chave = f"piloto_{campo}"
    row = conn.execute("SELECT valor FROM notificacoes_controle_param WHERE chave = ?", (chave,)).fetchone()
    antes = row[0] if row else None
    if antes == valor:
        return
    if valor is None:
        conn.execute("DELETE FROM notificacoes_controle_param WHERE chave = ?", (chave,))
    else:
        conn.execute("INSERT INTO notificacoes_controle_param (chave, valor) VALUES (?, ?) "
                     "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor", (chave, valor))
    _registrar(conn, por, _PILOTO, campo, antes, valor)
    conn.commit()


def _validar_tipo(tipo: str) -> None:
    if tipo not in TIPOS:
        raise ValueError(f"Tipo de notificação desconhecido: {tipo}")


def _validar_estado(estado: str) -> None:
    if estado not in ESTADOS:
        raise ValueError(f"Estado inválido: {estado}. Use {', '.join(ESTADOS)}.")


def _validar_regra(tipo: str, regra: str) -> None:
    if regra not in REGRAS:
        raise ValueError(f"Regra inválida: {regra}. Use {', '.join(REGRAS)}.")
    if regra == "BLOQUEADO" and TIPOS[tipo]["destino"] == "transportadora":
        # O bloqueio de transportadora e so o "NÃO ENVIAR" do cadastro dela.
        raise ValueError("Transportadora não aceita bloqueio aqui: use o NÃO ENVIAR do cadastro.")


def definir_geral(conn: sqlite3.Connection, ligado: bool, por: str) -> None:
    _gravar_estado(conn, _GERAL, "LIGADO" if ligado else "DESLIGADO", por)


def definir_estado(conn: sqlite3.Connection, tipo: str, estado: str, por: str) -> None:
    _validar_tipo(tipo)
    _validar_estado(estado)
    _gravar_estado(conn, tipo, estado, por)


def definir_excecao(conn: sqlite3.Connection, tipo: str, destino: str, regra: str | None, por: str) -> None:
    """regra None remove a excecao."""
    _validar_tipo(tipo)
    if regra is not None:
        _validar_regra(tipo, regra)
    chave = _normalizar_destino(tipo, destino)
    if not chave:
        raise ValueError("Destinatário vazio.")
    _gravar_excecao(conn, tipo, chave, regra, por)


def definir_piloto(conn: sqlite3.Connection, email: str, whatsapp: str, por: str) -> None:
    _gravar_param(conn, "email", str(email or "").strip() or None, por)
    _gravar_param(conn, "whatsapp", _so_digitos(whatsapp) or None, por)


def desfazer(conn: sqlite3.Connection, historico_id: int, por: str) -> None:
    """Restaura o valor anterior (`de`) do item do historico e grava uma nova
    linha de historico com a reversao."""
    row = conn.execute(
        "SELECT alvo, destino, de FROM notificacoes_controle_historico WHERE id = ?", (historico_id,)).fetchone()
    if not row:
        raise ValueError(f"Item de histórico inexistente: {historico_id}")
    alvo, destino, de = row
    if alvo == _PILOTO:
        _gravar_param(conn, destino, de, por)
    elif destino is not None:
        _gravar_excecao(conn, alvo, destino, de, por)
    else:
        _gravar_estado(conn, alvo, de, por)


# ------------------------------------------------------------------ decisao

def decidir(tipo: str, destino: str | None, *, fallback_acao: str, fallback_piloto: str = "",
            canal: str = "email", db_path=None) -> Decisao:
    """Ver o docstring do modulo. `fallback_acao`/`fallback_piloto` e o que o
    config da rotina decidiria hoje (use acao_do_config)."""
    _validar_tipo(tipo)
    if canal not in CANAIS:
        raise ValueError(f"Canal desconhecido: {canal}")

    def do_config(motivo: str) -> Decisao:
        return Decisao(fallback_acao, fallback_piloto if fallback_acao == "PILOTO" else "", "config", motivo)

    try:
        conn = conectar(db_path)
        try:
            if estado_geral(conn) == "DESLIGADO":
                return Decisao("NAO", "", "tela", "geral desligado")
            estado = estado_tipo(conn, tipo)
            if estado is None:
                return do_config("sem linha na tela")
            regra = None
            if destino is not None:
                regra = excecoes(conn, tipo).get(_normalizar_destino(tipo, destino))
            if regra == "BLOQUEADO":
                return Decisao("NAO", "", "tela", "destinatário bloqueado")
            if estado == "DESLIGADO":
                return Decisao("NAO", "", "tela", "tipo desligado")
            if estado == "LIGADO":
                return Decisao("ENVIAR", "", "tela", "tipo ligado")
            if regra == "LIBERADO":
                return Decisao("ENVIAR", "", "tela", "destinatário liberado no piloto")
            alvo_piloto = piloto(conn)[canal]
            if not alvo_piloto:
                return Decisao("NAO", "", "tela", f"piloto sem destino de {canal}")
            return Decisao("PILOTO", alvo_piloto, "tela", "tipo em piloto")
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning("controle_notificacoes: erro ao ler o controle (%s); vale o config", e)
        return do_config(f"erro ao ler o controle: {e}")
