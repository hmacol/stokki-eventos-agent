# -*- coding: utf-8 -*-
"""
painel_agentes/compras.py

Demandas de compra (Hugo, 19/11/2026): a equipe registra o que precisa ser
comprado (item, quantidade, motivo, observacoes) pela tela /compras, a Vivian
ve a lista por prioridade e marca como comprada guardando os dados da compra.
O aviso vai por WhatsApp pro grupo "Freshlog Adm/Financeiro/RH".

Situacoes:  PENDENTE -> COMPRADA            (fluxo normal)
            AGUARDANDO_APROVACAO -> PENDENTE | RECUSADA   (so com aprovacao ligada)
            AGUARDANDO_APROVACAO | PENDENTE -> CANCELADA

Fluxo de aprovacao: PRONTO E DESLIGADO. Com compras.aprovacao_ativa = false a
demanda nasce PENDENTE; ligado, nasce AGUARDANDO_APROVACAO e so vira PENDENTE
(visivel pra comprar) quando um login de nivel total aprova.

Envio: notificar_whatsapp.despachar, trava propria DESLIGADA por padrao:
desligada, a mensagem fica so na tela (aviso "simulado") e no log.

config.yaml:
    compras:
      ativo: false            # false = simulacao (nada sai); true = manda pro grupo
      aprovacao_ativa: false  # true = demanda nova espera aprovacao do nivel total
      grupo_id: ""            # vazio = grupo "Freshlog Adm/Financeiro/RH" achado pelo nome

Cadastros (22/11): itens (nome + unidade) e areas (so o nome), listas que a tela
oferece na demanda. Item e area da demanda tem de existir e estar ativos; quem tem a
tela cadastra na hora, so o nivel total inativa/reativa. Demandas antigas (sem
area, item em texto livre) continuam valendo.

Tabelas compras_demandas, compras_itens e compras_areas (dados.db).
"""
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from pagamentos_pontuais import _so_letras

logger = logging.getLogger("painel.compras")

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"
NOME_GRUPO = "Freshlog Adm/Financeiro/RH"
ORIGEM_WHATSAPP = "compras"

AGUARDANDO_APROVACAO = "AGUARDANDO_APROVACAO"
PENDENTE = "PENDENTE"
COMPRADA = "COMPRADA"
RECUSADA = "RECUSADA"
CANCELADA = "CANCELADA"
SITUACOES = {AGUARDANDO_APROVACAO: "Aguardando aprovação", PENDENTE: "Pendente", COMPRADA: "Comprada",
             RECUSADA: "Recusada", CANCELADA: "Cancelada"}
ABERTAS = (AGUARDANDO_APROVACAO, PENDENTE)

PRIORIDADES = {"URGENTE": "Urgente", "ALTA": "Alta", "NORMAL": "Normal", "BAIXA": "Baixa"}
PRIORIDADE_PADRAO = "NORMAL"

AVISO_SIMULADO = "simulado"
AVISO_ENVIANDO = "enviando"

LIMITES = {"nome": 120, "unidade": 30, "area": 60, "item": 120, "quantidade": 40, "motivo": 300, "observacoes": 500, "fornecedor": 120,
           "compra_obs": 500, "motivo_recusa": 300, "motivo_cancelamento": 300}


class ErroCompra(ValueError):
    """Dado invalido ou transicao nao permitida (vira 400 na rota)."""


# ── Banco ─────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS compras_demandas (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    item                TEXT NOT NULL,
    quantidade          TEXT NOT NULL,
    motivo              TEXT NOT NULL,
    observacoes         TEXT,
    prioridade          TEXT NOT NULL DEFAULT 'NORMAL',
    situacao            TEXT NOT NULL,
    criado_por          TEXT,
    criado_em           TEXT NOT NULL,
    aprovado_por        TEXT,
    aprovado_em         TEXT,
    recusado_por        TEXT,
    recusado_em         TEXT,
    motivo_recusa       TEXT,
    comprado_por        TEXT,
    comprado_em         TEXT,
    data_compra         TEXT,
    fornecedor          TEXT,
    valor               REAL,
    compra_obs          TEXT,
    cancelado_por       TEXT,
    cancelado_em        TEXT,
    motivo_cancelamento TEXT,
    aviso_evento        TEXT,
    aviso_texto         TEXT,
    aviso_situacao      TEXT,
    aviso_motivo        TEXT,
    aviso_em            TEXT
);
CREATE INDEX IF NOT EXISTS idx_compras_demandas_situacao ON compras_demandas(situacao);
CREATE TABLE IF NOT EXISTS compras_itens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    nome       TEXT NOT NULL,
    unidade    TEXT,
    ativo      INTEGER NOT NULL DEFAULT 1,
    criado_por TEXT,
    criado_em  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS compras_areas (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    nome       TEXT NOT NULL,
    ativo      INTEGER NOT NULL DEFAULT 1,
    criado_por TEXT,
    criado_em  TEXT NOT NULL
);
"""


def conectar(caminho=None) -> sqlite3.Connection:
    conn = sqlite3.connect(caminho or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    if "area" not in {r[1] for r in conn.execute("PRAGMA table_info(compras_demandas)")}:
        conn.execute("ALTER TABLE compras_demandas ADD COLUMN area TEXT")     # criada antes dos cadastros
    return conn


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def config_compras(config: dict | None) -> dict:
    cfg = (config or {}).get("compras") or {}
    return {"ativo": bool(cfg.get("ativo")), "aprovacao_ativa": bool(cfg.get("aprovacao_ativa")),
            "grupo_id": str(cfg.get("grupo_id") or "").strip()}


# ── Validacao ─────────────────────────────────────────────────────────────────

def _campo(dados: dict, chave: str, rotulo: str, obrigatorio: bool) -> str:
    valor = " ".join(str(dados.get(chave) or "").split()) if chave != "observacoes" else str(dados.get(chave) or "").strip()
    if obrigatorio and not valor:
        raise ErroCompra(f"Informe {rotulo}.")
    if len(valor) > LIMITES[chave]:
        raise ErroCompra(f"{rotulo[0].upper() + rotulo[1:]} passa de {LIMITES[chave]} caracteres.")
    return valor


def _valor(bruto) -> float | None:
    texto = str(bruto or "").strip().replace("R$", "").replace(" ", "")
    if not texto:
        return None
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        numero = float(texto)
    except ValueError:
        raise ErroCompra("Valor inválido.")
    if not (0 < numero < 1e9) or round(numero, 2) != numero:
        raise ErroCompra("O valor deve ser maior que zero, com até 2 casas decimais.")
    return numero


def _data(bruto) -> str:
    texto = str(bruto or "").strip()
    try:
        return datetime.strptime(texto, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ErroCompra("Informe a data da compra.")


# ── Operacoes ─────────────────────────────────────────────────────────────────

def obter(conn, demanda_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM compras_demandas WHERE id = ?", (demanda_id,)).fetchone()
    return dict(r) if r else None


def _exigir(conn, demanda_id: int, permitidas: tuple, erro: str) -> dict:
    d = obter(conn, demanda_id)
    if not d:
        raise ErroCompra("Demanda não encontrada.")
    if d["situacao"] not in permitidas:
        raise ErroCompra(erro)
    return d


# ── Cadastros de itens e areas ────────────────────────────────────────────────

_TABELAS = {"itens": "compras_itens", "areas": "compras_areas"}


def _chave(nome) -> str:
    return _so_letras(nome)        # sem acento, maiuscula nem pontuacao: "Fita  Larga" = "fita larga"


def _existente(conn, tabela: str, nome: str) -> dict | None:
    alvo = _chave(nome)
    return next((dict(r) for r in conn.execute(f"SELECT * FROM {tabela}") if _chave(r["nome"]) == alvo), None)


def _cadastrar(conn, tabela: str, nome: str, unidade, usuario: str, rotulo: str) -> int:
    if _existente(conn, tabela, nome):
        ja = _existente(conn, tabela, nome)
        raise ErroCompra(f"{rotulo.capitalize()} já cadastrado" + ("" if ja["ativo"] else " (está inativo: reative)") + ".")
    if tabela == "compras_itens":
        cur = conn.execute("INSERT INTO compras_itens (nome, unidade, criado_por, criado_em) VALUES (?,?,?,?)",
                           (nome, unidade or None, usuario, _agora()))
    else:
        cur = conn.execute("INSERT INTO compras_areas (nome, criado_por, criado_em) VALUES (?,?,?)",
                           (nome, usuario, _agora()))
    conn.commit()
    return cur.lastrowid


def cadastrar_item(conn, dados: dict, usuario: str) -> int:
    nome = _campo({"nome": dados.get("nome")}, "nome", "o nome do item", True)
    unidade = _campo({"unidade": dados.get("unidade")}, "unidade", "a unidade", False)
    return _cadastrar(conn, "compras_itens", nome, unidade, usuario, "item")


def cadastrar_area(conn, dados: dict, usuario: str) -> int:
    nome = _campo({"area": dados.get("nome")}, "area", "o nome da área", True)
    return _cadastrar(conn, "compras_areas", nome, None, usuario, "área")


def definir_ativo(conn, cadastro: str, registro_id: int, ativo: bool) -> None:
    tabela = _TABELAS[cadastro]
    if not conn.execute(f"SELECT 1 FROM {tabela} WHERE id = ?", (registro_id,)).fetchone():
        raise ErroCompra("Cadastro não encontrado.")
    conn.execute(f"UPDATE {tabela} SET ativo = ? WHERE id = ?", (1 if ativo else 0, registro_id))
    conn.commit()


def listar_cadastro(conn, cadastro: str, so_ativos: bool = False) -> list[dict]:
    filtro = "WHERE ativo = 1" if so_ativos else ""
    rows = conn.execute(f"SELECT * FROM {_TABELAS[cadastro]} {filtro} ORDER BY ativo DESC, nome COLLATE NOCASE")
    return [dict(r) for r in rows]


def _do_cadastro(conn, tabela: str, nome, rotulo: str) -> dict:
    nome = " ".join(str(nome or "").split())
    if not nome:
        raise ErroCompra(f"Escolha {'o' if rotulo == 'item' else 'a'} {rotulo}.")
    achado = _existente(conn, tabela, nome)
    if not achado or not achado["ativo"]:
        raise ErroCompra(f"{rotulo.capitalize()} \"{nome}\" não está na lista: escolha um cadastrado ou cadastre.")
    return achado


def criar(conn, dados: dict, usuario: str, config: dict | None = None) -> int:
    prioridade = str(dados.get("prioridade") or PRIORIDADE_PADRAO).upper()
    if prioridade not in PRIORIDADES:
        raise ErroCompra("Prioridade inválida.")
    area = _do_cadastro(conn, "compras_areas", dados.get("area"), "área")["nome"]
    item = _do_cadastro(conn, "compras_itens", dados.get("item"), "item")["nome"]
    quantidade = _campo(dados, "quantidade", "a quantidade", True)
    motivo = _campo(dados, "motivo", "o motivo", True)
    observacoes = _campo(dados, "observacoes", "as observações", False)
    situacao = AGUARDANDO_APROVACAO if config_compras(config)["aprovacao_ativa"] else PENDENTE
    cur = conn.execute("INSERT INTO compras_demandas (item, area, quantidade, motivo, observacoes, prioridade, situacao, "
                       "criado_por, criado_em) VALUES (?,?,?,?,?,?,?,?,?)",
                       (item, area, quantidade, motivo, observacoes or None, prioridade, situacao, usuario, _agora()))
    conn.commit()
    return cur.lastrowid


def aprovar(conn, demanda_id: int, usuario: str) -> None:
    _exigir(conn, demanda_id, (AGUARDANDO_APROVACAO,), "Só dá para aprovar demanda aguardando aprovação.")
    conn.execute("UPDATE compras_demandas SET situacao = ?, aprovado_por = ?, aprovado_em = ? WHERE id = ?",
                 (PENDENTE, usuario, _agora(), demanda_id))
    conn.commit()


def recusar(conn, demanda_id: int, motivo, usuario: str) -> None:
    _exigir(conn, demanda_id, (AGUARDANDO_APROVACAO,), "Só dá para recusar demanda aguardando aprovação.")
    motivo = _campo({"motivo_recusa": motivo}, "motivo_recusa", "o motivo da recusa", True)
    conn.execute("UPDATE compras_demandas SET situacao = ?, recusado_por = ?, recusado_em = ?, motivo_recusa = ? "
                 "WHERE id = ?", (RECUSADA, usuario, _agora(), motivo, demanda_id))
    conn.commit()


def marcar_comprada(conn, demanda_id: int, dados: dict, usuario: str) -> None:
    _exigir(conn, demanda_id, (PENDENTE,), "Só dá para marcar como comprada uma demanda pendente.")
    data = _data(dados.get("data_compra"))
    fornecedor = _campo(dados, "fornecedor", "o fornecedor", False)
    compra_obs = _campo(dados, "compra_obs", "a observação da compra", False)
    conn.execute("UPDATE compras_demandas SET situacao = ?, comprado_por = ?, comprado_em = ?, data_compra = ?, "
                 "fornecedor = ?, valor = ?, compra_obs = ? WHERE id = ?",
                 (COMPRADA, usuario, _agora(), data, fornecedor or None, _valor(dados.get("valor")),
                  compra_obs or None, demanda_id))
    conn.commit()


def cancelar(conn, demanda_id: int, motivo, usuario: str) -> None:
    _exigir(conn, demanda_id, ABERTAS, "Só dá para cancelar demanda que ainda não foi comprada.")
    motivo = _campo({"motivo_cancelamento": motivo}, "motivo_cancelamento", "o motivo do cancelamento", True)
    conn.execute("UPDATE compras_demandas SET situacao = ?, cancelado_por = ?, cancelado_em = ?, "
                 "motivo_cancelamento = ? WHERE id = ?", (CANCELADA, usuario, _agora(), motivo, demanda_id))
    conn.commit()


def listar(conn, situacao: str | None = None, limite: int = 300) -> list[dict]:
    """Por prioridade (urgente primeiro) e, dentro dela, a mais antiga primeiro."""
    filtro, args = ("WHERE situacao = ?", [situacao]) if situacao else ("", [])
    prioridade = "CASE prioridade WHEN 'URGENTE' THEN 0 WHEN 'ALTA' THEN 1 WHEN 'NORMAL' THEN 2 ELSE 3 END"
    if situacao in ABERTAS:
        ordem = f"{prioridade}, criado_em"
    elif situacao:                      # encerradas: as mais recentes primeiro
        ordem = "criado_em DESC"
    else:                               # todas: abertas primeiro (por prioridade), depois as encerradas
        ordem = (f"CASE WHEN situacao IN ('PENDENTE','AGUARDANDO_APROVACAO') THEN 0 ELSE 1 END, "
                 f"CASE WHEN situacao IN ('PENDENTE','AGUARDANDO_APROVACAO') THEN {prioridade} END, criado_em DESC")
    rows = conn.execute(f"SELECT * FROM compras_demandas {filtro} ORDER BY {ordem} LIMIT ?", (*args, limite))
    return [dict(r) for r in rows]


def contagem(conn) -> dict:
    cont = {s: 0 for s in SITUACOES}
    for r in conn.execute("SELECT situacao, COUNT(*) n FROM compras_demandas GROUP BY situacao"):
        cont[r["situacao"]] = r["n"]
    return cont


# ── Texto do aviso ────────────────────────────────────────────────────────────

def _moeda(valor) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _data_br(iso: str | None) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y") if iso else ""


def _pedido(d: dict) -> list[str]:
    linhas = [f"Item: {d['item']}", f"Quantidade: {d['quantidade']}", f"Motivo: {d['motivo']}",
              f"Prioridade: {PRIORIDADES[d['prioridade']]}"]
    if d.get("area"):
        linhas.insert(0, f"Área: {d['area']}")
    if d.get("observacoes"):
        linhas.append(f"Obs.: {d['observacoes']}")
    return linhas


def texto_criacao(d: dict) -> str:
    if d["situacao"] == AGUARDANDO_APROVACAO:
        topo = f"🛒 Compra #{d['id']} aguardando aprovação (pedida por {d['criado_por']})"
    else:
        topo = f"🛒 Nova demanda de compra #{d['id']} (pedida por {d['criado_por']})"
    return "\n".join([topo, *_pedido(d)])


def texto_aprovacao(d: dict) -> str:
    return "\n".join([f"✅ Compra #{d['id']} aprovada por {d['aprovado_por']} — pode comprar", *_pedido(d)])


def texto_recusa(d: dict) -> str:
    return f"❌ Compra #{d['id']} ({d['item']}) recusada por {d['recusado_por']}: {d['motivo_recusa']}"


def texto_compra(d: dict) -> str:
    linhas = [f"📦 Compra #{d['id']} concluída por {d['comprado_por']} em {_data_br(d['data_compra'])}",
              f"Item: {d['item']} ({d['quantidade']})"]
    if d.get("fornecedor"):
        linhas.append(f"Fornecedor: {d['fornecedor']}")
    if d.get("valor") is not None:
        linhas.append(f"Valor: {_moeda(d['valor'])}")
    if d.get("compra_obs"):
        linhas.append(f"Obs.: {d['compra_obs']}")
    return "\n".join(linhas)


def texto_cancelamento(d: dict) -> str:
    return f"🚫 Compra #{d['id']} ({d['item']}) cancelada por {d['cancelado_por']}: {d['motivo_cancelamento']}"


TEXTOS = {"criacao": texto_criacao, "aprovacao": texto_aprovacao, "recusa": texto_recusa,
          "compra": texto_compra, "cancelamento": texto_cancelamento}


# ── Aviso no WhatsApp ─────────────────────────────────────────────────────────

def grupo_destino(config: dict, cfg: dict, listar_grupos=None) -> str | None:
    """grupo_id do config, senao o grupo achado pelo nome no gateway (ignora
    acento, maiuscula e pontuacao). None = nao achou ou ha mais de um."""
    if cfg["grupo_id"]:
        return cfg["grupo_id"]
    if listar_grupos is None:
        from integracao_openwa import listar_grupos
    alvo = _so_letras(NOME_GRUPO)
    achados = {g["id"] for g in listar_grupos((config or {}).get("whatsapp_notificacoes") or {}) or []
               if isinstance(g, dict) and g.get("id") and _so_letras(g.get("name")) == alvo}
    if len(achados) > 1:
        logger.warning("Mais de um grupo %r no WhatsApp: aviso nao sai; definir compras.grupo_id", NOME_GRUPO)
    return achados.pop() if len(achados) == 1 else None


def preparar_aviso(conn, demanda_id: int, evento: str, config: dict) -> dict:
    """Grava o texto do aviso. Trava desligada = 'simulado' (nada sai)."""
    d = obter(conn, demanda_id)
    texto = TEXTOS[evento](d)
    situacao = AVISO_ENVIANDO if config_compras(config)["ativo"] else AVISO_SIMULADO
    motivo = None if situacao == AVISO_ENVIANDO else "envio desligado (compras.ativo)"
    conn.execute("UPDATE compras_demandas SET aviso_evento = ?, aviso_texto = ?, aviso_situacao = ?, "
                 "aviso_motivo = ?, aviso_em = ? WHERE id = ?", (evento, texto, situacao, motivo, _agora(), demanda_id))
    conn.commit()
    if situacao == AVISO_SIMULADO:
        logger.info("[SIMULACAO] Compra #%s (%s) nao enviada ao grupo %s.", demanda_id, evento, NOME_GRUPO)
    return {"situacao": situacao, "texto": texto}


def enviar_aviso(config: dict, demanda_id: int, caminho_db=None, despachar=None, listar_grupos=None) -> str:
    """Manda o aviso preparado e grava o resultado. Nunca levanta excecao."""
    try:
        if despachar is None:
            from notificar_whatsapp import despachar
        conn = conectar(caminho_db)
        try:
            d = obter(conn, demanda_id)
            if not d or d["aviso_situacao"] != AVISO_ENVIANDO:
                return "ignorado"
            grupo = grupo_destino(config, config_compras(config), listar_grupos)
            if grupo is None:
                situacao, motivo = "nao_enviado", f"grupo {NOME_GRUPO} não encontrado (ou repetido) no WhatsApp"
            else:
                situacao = despachar(config, ORIGEM_WHATSAPP, d["aviso_evento"], d["aviso_texto"],
                                     assinatura=f"compra-{demanda_id}-{d['aviso_evento']}", grupo_id=grupo)
                motivo = {"desligado": "WhatsApp das notificações desligado ou sem gateway",
                          "nao_enviado": "teto do dia, canal em pausa ou repetida",
                          "falhou": "gateway fora do ar ou envio recusado"}.get(situacao)
            conn.execute("UPDATE compras_demandas SET aviso_situacao = ?, aviso_motivo = ?, aviso_em = ? WHERE id = ?",
                         (situacao, motivo, _agora(), demanda_id))
            conn.commit()
            return situacao
        finally:
            conn.close()
    except Exception:
        logger.exception("Compra #%s: falha ao enviar o aviso", demanda_id)
        return "falhou"


def disparar_aviso(conn, demanda_id: int, evento: str, config: dict, iniciar=None) -> dict:
    aviso = preparar_aviso(conn, demanda_id, evento, config)
    if aviso["situacao"] == AVISO_ENVIANDO:
        iniciar = iniciar or (lambda alvo, args: threading.Thread(target=alvo, args=args, daemon=True).start())
        iniciar(enviar_aviso, (config, demanda_id))
    return aviso


def pode_reenviar(d: dict) -> bool:
    return bool(d.get("aviso_evento")) and d.get("aviso_situacao") in ("nao_enviado", "falhou", "desligado")
