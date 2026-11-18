# -*- coding: utf-8 -*-
"""
painel_agentes/pagamentos_pontuais.py

Pagamentos pontuais aos motoristas (Hugo, 18/11/2026): a logistica pede ao
financeiro um Adiantamento ou uma Descarga pela tela /financeiro/pagamentos
(menu Motoristas) e o pedido vai por WhatsApp pro grupo "FINANCEIRO +
LOGÍSTICA". Nao executa pagamento nenhum e nao entra no extrato do motorista
(card de melhoria "Pagamentos pontuais no extrato do motorista").

Fluxo combinado com o Hugo:
1. Lancar: tipo, motorista (ativos da bd_motoristas, um por CPF), valor > 0 e
   observacao opcional. Descarga pode ser paga ao motorista ou ao LOCAL da
   descarga (nome + PIX/dados bancarios obrigatorios; endereco e CNPJ/CPF
   opcionais). Situacao AGUARDANDO_PAGAMENTO + aviso no grupo.
2. O financeiro paga e manda o comprovante (fora do sistema). A logistica
   confirma na tela anexando o comprovante (foto ou PDF, obrigatorio).
   Adiantamento -> PAGO. Descarga -> AGUARDANDO_DOCUMENTO.
3. Descarga: a logistica anexa o documento (foto ou PDF) -> PAGO + aviso so de
   texto no grupo.
4. Cancelar so enquanto AGUARDANDO_PAGAMENTO, com aviso curto no grupo.
Sem trava de duplicado e sem limite de valor (decisao do Hugo).

Envio: notificar_whatsapp.despachar (OpenWA), origem propria com teto proprio.
Trava propria, DESLIGADA por padrao: desligada, a mensagem fica so na tela
(situacao "simulado") e no log. Grupo: pagamentos_pontuais.grupo_id, senao o
grupo achado pelo nome no gateway.

config.yaml:
    pagamentos_pontuais:
      ativo: false      # false = simulacao (nada sai); true = manda pro grupo
      grupo_id: ""      # vazio = grupo "FINANCEIRO + LOGÍSTICA" achado pelo nome
      teto_diario: 30   # avisos por dia (balde proprio em notificar_whatsapp)

Tabela pagamentos_pontuais (dados.db); arquivos em
dados/pagamentos_pontuais/<id>/ (comprovante_N.ext, documento_N.ext).
"""
import logging
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("painel.pagamentos_pontuais")

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "dados.db"
PASTA_ARQUIVOS = _RAIZ / "dados" / "pagamentos_pontuais"
MAX_ARQUIVO_BYTES = 10 * 1024 * 1024
MAX_OBSERVACAO = 500
MAX_CAMPO_LOCAL = 200
NOME_GRUPO = "FINANCEIRO + LOGÍSTICA"
ORIGEM_WHATSAPP = "pagamento_pontual"

ADIANTAMENTO, DESCARGA = "ADIANTAMENTO", "DESCARGA"
TIPOS = {ADIANTAMENTO: "Adiantamento", DESCARGA: "Descarga"}
MOTORISTA, LOCAL = "MOTORISTA", "LOCAL"

AGUARDANDO_PAGAMENTO = "AGUARDANDO_PAGAMENTO"
AGUARDANDO_DOCUMENTO = "AGUARDANDO_DOCUMENTO"
PAGO = "PAGO"
CANCELADO = "CANCELADO"
SITUACOES = {
    AGUARDANDO_PAGAMENTO: "Aguardando pagamento",
    AGUARDANDO_DOCUMENTO: "Aguardando documento",
    PAGO: "Pago",
    CANCELADO: "Cancelado",
}

# Situacao do aviso no WhatsApp
AVISO_SIMULADO = "simulado"     # trava desligada: so na tela e no log
AVISO_ENVIANDO = "enviando"


class ErroPagamento(Exception):
    """Mensagem pro usuario (vira 400 na API)."""


# ── Banco ─────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pagamentos_pontuais (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo                TEXT NOT NULL,
    situacao            TEXT NOT NULL,
    valor               REAL NOT NULL,
    observacao          TEXT,
    motorista_id        INTEGER,
    motorista_nome      TEXT NOT NULL,
    motorista_cpf       TEXT,
    motorista_telefone  TEXT,
    motorista_placa     TEXT,
    motorista_veiculo   TEXT,
    motorista_pix       TEXT,
    favorecido          TEXT NOT NULL DEFAULT 'MOTORISTA',
    local_nome          TEXT,
    local_endereco      TEXT,
    local_documento     TEXT,
    local_pagamento     TEXT,
    lancado_por         TEXT,
    lancado_em          TEXT NOT NULL,
    comprovante         TEXT,
    confirmado_por      TEXT,
    confirmado_em       TEXT,
    documento           TEXT,
    documento_por       TEXT,
    documento_em        TEXT,
    cancelado_por       TEXT,
    cancelado_em        TEXT,
    motivo_cancelamento TEXT,
    aviso_evento        TEXT,
    aviso_texto         TEXT,
    aviso_situacao      TEXT,
    aviso_motivo        TEXT,
    aviso_em            TEXT
);
CREATE INDEX IF NOT EXISTS idx_pagamentos_pontuais_situacao ON pagamentos_pontuais(situacao);
"""


def conectar(caminho=None) -> sqlite3.Connection:
    conn = sqlite3.connect(caminho or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def config_pagamentos(config: dict | None) -> dict:
    cfg = (config or {}).get("pagamentos_pontuais") or {}
    return {"ativo": bool(cfg.get("ativo")), "grupo_id": str(cfg.get("grupo_id") or "").strip()}


# ── Motoristas (drop down) ────────────────────────────────────────────────────

def _so_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def _texto(valor) -> str:
    if valor is None:
        return ""
    texto = str(valor).strip()
    return "" if texto.lower() in ("nan", "none") else texto


def _ativo(valor) -> bool:
    from regras.preferencias_motoristas import _parse_bool
    return _parse_bool(valor)


def _pix_por_cpf(conn) -> dict[str, str]:
    """Chave PIX que o motorista cadastrou no app (tabela motoristas)."""
    try:
        return {_so_digitos(r[0]): _texto(r[1]) for r in
                conn.execute("SELECT cpf, chave_pix FROM motoristas WHERE chave_pix IS NOT NULL AND chave_pix != ''")}
    except sqlite3.OperationalError:     # tabela do app ainda nao existe neste banco
        return {}


def listar_motoristas(conn) -> list[dict]:
    """Motoristas ATIVOS da bd_motoristas, uma entrada por pessoa (CPF; sem
    CPF, a propria linha), em ordem alfabetica. `id` = menor id da pessoa."""
    try:
        linhas = conn.execute("SELECT * FROM bd_motoristas ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return []
    pix = _pix_por_cpf(conn)
    pessoas: dict[str, dict] = {}
    for r in linhas:
        if not _ativo(r["ATIVO"]) or not _texto(r["NOME_MOTORISTA"]):
            continue
        cpf = _so_digitos(r["CPF_MOTORISTA"])
        chave = cpf or f"id:{r['id']}"
        p = pessoas.get(chave)
        if p is None:
            p = pessoas[chave] = {"id": r["id"], "nome": _texto(r["NOME_MOTORISTA"]), "cpf": cpf,
                                  "telefone": "", "placas": [], "veiculos": [], "pix": pix.get(cpf, "") if cpf else ""}
        if not p["telefone"]:
            p["telefone"] = _so_digitos(r["TELEFONE_MOTORISTA"])
        for campo, lista in (("PLACA", "placas"), ("TIPO_VEICULO", "veiculos")):
            v = _texto(r[campo]).upper() if campo == "PLACA" else _texto(r[campo])
            if v and v not in p[lista]:
                p[lista].append(v)
    saida = []
    for p in pessoas.values():
        saida.append({"id": p["id"], "nome": p["nome"], "cpf": p["cpf"], "telefone": p["telefone"],
                      "placa": " / ".join(p["placas"]), "veiculo": " / ".join(p["veiculos"]), "pix": p["pix"]})
    return sorted(saida, key=lambda m: _sem_acento(m["nome"]))


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().upper()


# ── Validacao ─────────────────────────────────────────────────────────────────

def ler_valor(bruto) -> float:
    """'150', '150.5', '150,50', '1.234,56', 'R$ 80' -> float > 0 com no
    maximo 2 casas. Qualquer outra coisa -> ErroPagamento."""
    texto = str(bruto if bruto is not None else "").replace("R$", "").replace(" ", "").strip()
    if not texto:
        raise ErroPagamento("Informe o valor.")
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    if not re.fullmatch(r"-?\d+(\.\d{1,2})?", texto):
        raise ErroPagamento("Valor inválido: use só números, com até 2 casas decimais.")
    valor = round(float(texto), 2)
    if valor <= 0:
        raise ErroPagamento("O valor precisa ser maior que zero.")
    return valor


def _campo(dados: dict, chave: str, limite: int) -> str:
    return " ".join(str(dados.get(chave) or "").split())[:limite]


def validar(dados: dict, motoristas: list[dict]) -> dict:
    """Corpo do formulario -> campos prontos pra gravar. Levanta ErroPagamento."""
    tipo = str(dados.get("tipo") or "").strip().upper()
    if tipo not in TIPOS:
        raise ErroPagamento("Escolha o tipo de pagamento (Adiantamento ou Descarga).")
    try:
        motorista_id = int(dados.get("motorista_id") or 0)
    except (TypeError, ValueError):
        motorista_id = 0
    if not motorista_id:
        raise ErroPagamento("Escolha o motorista.")
    motorista = next((m for m in motoristas if m["id"] == motorista_id), None)
    if motorista is None:
        raise ErroPagamento("Motorista não encontrado ou inativo. Recarregue a tela.")
    valor = ler_valor(dados.get("valor"))
    favorecido = str(dados.get("favorecido") or MOTORISTA).strip().upper()
    if tipo == ADIANTAMENTO or favorecido not in (MOTORISTA, LOCAL):
        favorecido = MOTORISTA
    local = {"local_nome": "", "local_endereco": "", "local_documento": "", "local_pagamento": ""}
    if favorecido == LOCAL:
        local = {k: _campo(dados, k, MAX_CAMPO_LOCAL) for k in local}
        if not local["local_nome"]:
            raise ErroPagamento("Informe o nome do local da descarga.")
        if not local["local_pagamento"]:
            raise ErroPagamento("Informe a chave PIX ou os dados bancários do local da descarga.")
    observacao = str(dados.get("observacao") or "").strip()[:MAX_OBSERVACAO]
    return {"tipo": tipo, "valor": valor, "observacao": observacao, "favorecido": favorecido,
            "motorista": motorista, **local}


# ── Fluxo ─────────────────────────────────────────────────────────────────────

def lancar(conn, dados: dict, usuario: str) -> int:
    campos = validar(dados, listar_motoristas(conn))
    m = campos["motorista"]
    cur = conn.execute(
        "INSERT INTO pagamentos_pontuais (tipo, situacao, valor, observacao, motorista_id, motorista_nome, "
        "motorista_cpf, motorista_telefone, motorista_placa, motorista_veiculo, motorista_pix, favorecido, "
        "local_nome, local_endereco, local_documento, local_pagamento, lancado_por, lancado_em) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (campos["tipo"], AGUARDANDO_PAGAMENTO, campos["valor"], campos["observacao"] or None, m["id"], m["nome"],
         m["cpf"] or None, m["telefone"] or None, m["placa"] or None, m["veiculo"] or None, m["pix"] or None,
         campos["favorecido"], campos["local_nome"] or None, campos["local_endereco"] or None,
         campos["local_documento"] or None, campos["local_pagamento"] or None, usuario or None, _agora()))
    conn.commit()
    return cur.lastrowid


def obter(conn, pagamento_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM pagamentos_pontuais WHERE id = ?", (pagamento_id,)).fetchone()
    return dict(r) if r else None


def _exigir(conn, pagamento_id: int, situacao: str, erro: str) -> dict:
    p = obter(conn, pagamento_id)
    if p is None:
        raise ErroPagamento("Lançamento não encontrado.")
    if p["situacao"] != situacao:
        raise ErroPagamento(erro)
    return p


def confirmar(conn, pagamento_id: int, conteudo: bytes, usuario: str) -> str:
    """Pagamento feito: grava o comprovante (obrigatorio). Devolve a situacao nova."""
    p = _exigir(conn, pagamento_id, AGUARDANDO_PAGAMENTO, "Só dá pra confirmar um lançamento aguardando pagamento.")
    if not conteudo:
        raise ErroPagamento("Anexe o comprovante do pagamento (foto ou PDF).")
    nome = salvar_arquivo(pagamento_id, "comprovante", conteudo)
    nova = AGUARDANDO_DOCUMENTO if p["tipo"] == DESCARGA else PAGO
    conn.execute("UPDATE pagamentos_pontuais SET situacao = ?, comprovante = ?, confirmado_por = ?, confirmado_em = ? "
                 "WHERE id = ? AND situacao = ?",
                 (nova, nome, usuario or None, _agora(), pagamento_id, AGUARDANDO_PAGAMENTO))
    conn.commit()
    return nova


def incluir_documento(conn, pagamento_id: int, conteudo: bytes, usuario: str) -> None:
    """Descarga ja paga: grava o documento e fecha como PAGO."""
    _exigir(conn, pagamento_id, AGUARDANDO_DOCUMENTO,
            "O documento só entra numa descarga com o pagamento já confirmado.")
    if not conteudo:
        raise ErroPagamento("Anexe o documento da descarga (foto ou PDF).")
    nome = salvar_arquivo(pagamento_id, "documento", conteudo)
    conn.execute("UPDATE pagamentos_pontuais SET situacao = ?, documento = ?, documento_por = ?, documento_em = ? "
                 "WHERE id = ? AND situacao = ?",
                 (PAGO, nome, usuario or None, _agora(), pagamento_id, AGUARDANDO_DOCUMENTO))
    conn.commit()


def cancelar(conn, pagamento_id: int, motivo: str, usuario: str) -> None:
    _exigir(conn, pagamento_id, AGUARDANDO_PAGAMENTO, "Só dá pra cancelar um lançamento aguardando pagamento.")
    motivo = " ".join(str(motivo or "").split())[:MAX_OBSERVACAO]
    if not motivo:
        raise ErroPagamento("Informe o motivo do cancelamento.")
    conn.execute("UPDATE pagamentos_pontuais SET situacao = ?, motivo_cancelamento = ?, cancelado_por = ?, "
                 "cancelado_em = ? WHERE id = ? AND situacao = ?",
                 (CANCELADO, motivo, usuario or None, _agora(), pagamento_id, AGUARDANDO_PAGAMENTO))
    conn.commit()


def listar(conn, situacao: str | None = None, limite: int = 200) -> list[dict]:
    if situacao:
        rows = conn.execute("SELECT * FROM pagamentos_pontuais WHERE situacao = ? ORDER BY id DESC LIMIT ?",
                            (situacao, limite))
    else:
        rows = conn.execute("SELECT * FROM pagamentos_pontuais ORDER BY id DESC LIMIT ?", (limite,))
    return [dict(r) for r in rows]


def contagem(conn) -> dict:
    c = {s: 0 for s in SITUACOES}
    for s, n in conn.execute("SELECT situacao, COUNT(*) FROM pagamentos_pontuais GROUP BY situacao"):
        c[s] = n
    c["TODOS"] = sum(c.values())
    return c


# ── Arquivos ──────────────────────────────────────────────────────────────────

def _extensao(conteudo: bytes) -> str | None:
    if conteudo.startswith(b"%PDF"):
        return "pdf"
    if conteudo.startswith(b"\xff\xd8"):
        return "jpg"
    if conteudo.startswith(b"\x89PNG"):
        return "png"
    if conteudo[:4] == b"RIFF" and conteudo[8:12] == b"WEBP":
        return "webp"
    return None


def salvar_arquivo(pagamento_id: int, prefixo: str, conteudo: bytes) -> str:
    if len(conteudo) > MAX_ARQUIVO_BYTES:
        raise ErroPagamento("Arquivo muito grande (máximo 10 MB).")
    ext = _extensao(conteudo)
    if not ext:
        raise ErroPagamento("O arquivo precisa ser uma foto (JPG, PNG, WEBP) ou um PDF.")
    pasta = PASTA_ARQUIVOS / str(pagamento_id)
    pasta.mkdir(parents=True, exist_ok=True)
    n = sum(1 for x in pasta.iterdir() if x.name.startswith(prefixo + "_")) + 1
    nome = f"{prefixo}_{n}.{ext}"
    (pasta / nome).write_bytes(conteudo)
    return nome


def caminho_arquivo(pagamento_id: int, nome: str) -> Path | None:
    """Caminho do arquivo do lancamento, ou None se nao existir / sair da pasta."""
    pasta = (PASTA_ARQUIVOS / str(pagamento_id)).resolve()
    alvo = (pasta / nome).resolve()
    if alvo.parent != pasta or not alvo.is_file():
        return None
    return alvo


# ── Mensagens ─────────────────────────────────────────────────────────────────

def _moeda(valor) -> str:
    inteiro, centavos = f"{float(valor):.2f}".split(".")
    return f"R$ {int(inteiro):,}".replace(",", ".") + f",{centavos}"


def _cpf(cpf: str | None) -> str:
    d = _so_digitos(cpf)
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}" if len(d) == 11 else d


def _telefone(tel: str | None) -> str:
    d = _so_digitos(tel)
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return d


def _quando(iso: str | None) -> str:
    try:
        return datetime.strptime(iso or "", "%Y-%m-%d %H:%M:%S").strftime("%d/%m %H:%M")
    except ValueError:
        return iso or ""


def _linhas_motorista(p: dict) -> list[str]:
    linhas = [f"Motorista: {p['motorista_nome']}"]
    ident = " · ".join(x for x in (f"CPF {_cpf(p['motorista_cpf'])}" if p.get("motorista_cpf") else "",
                                   f"Tel {_telefone(p['motorista_telefone'])}" if p.get("motorista_telefone") else "") if x)
    if ident:
        linhas.append(ident)
    carro = " · ".join(x for x in (p.get("motorista_veiculo") or "",
                                   f"Placa {p['motorista_placa']}" if p.get("motorista_placa") else "") if x)
    if carro:
        linhas.append(f"Veículo: {carro}")
    return linhas


def texto_lancamento(p: dict) -> str:
    linhas = [f"💰 *Solicitação de pagamento #{p['id']} · {TIPOS[p['tipo']]}*",
              f"Valor: *{_moeda(p['valor'])}*"]
    if p["favorecido"] == LOCAL:
        linhas.append(f"Pagar ao LOCAL da descarga: *{p['local_nome']}*")
        if p.get("local_endereco"):
            linhas.append(f"Endereço: {p['local_endereco']}")
        if p.get("local_documento"):
            linhas.append(f"CNPJ/CPF: {p['local_documento']}")
        linhas.append(f"PIX / dados bancários: {p['local_pagamento']}")
        linhas.append("")
        linhas += _linhas_motorista(p)
    else:
        linhas.append("Pagar ao motorista")
        linhas += _linhas_motorista(p)
        linhas.append(f"PIX: {p['motorista_pix']}" if p.get("motorista_pix") else "PIX: não cadastrado no app")
    if p.get("observacao"):
        linhas.append(f"Obs: {p['observacao']}")
    linhas.append(f"Lançado por {p.get('lancado_por') or 'equipe'} em {_quando(p['lancado_em'])}")
    linhas.append("Depois de pagar, mandem o comprovante aqui: a logística confirma no painel.")
    return "\n".join(linhas)


def texto_documento(p: dict) -> str:
    return (f"📎 Documento da descarga #{p['id']} incluído ({p['motorista_nome']} · {_moeda(p['valor'])}) "
            f"por {p.get('documento_por') or 'equipe'}.")


def texto_cancelamento(p: dict) -> str:
    return (f"❌ Solicitação de pagamento #{p['id']} cancelada ({TIPOS[p['tipo']]} · {p['motorista_nome']} · "
            f"{_moeda(p['valor'])}) por {p.get('cancelado_por') or 'equipe'}.\nMotivo: {p['motivo_cancelamento']}")


TEXTOS = {"lancamento": texto_lancamento, "documento": texto_documento, "cancelamento": texto_cancelamento}


# ── Aviso no WhatsApp ─────────────────────────────────────────────────────────

def _so_letras(nome) -> str:
    return "".join(c for c in _sem_acento(str(nome or "")) if c.isalnum())


def grupo_destino(config: dict, cfg: dict, listar=None) -> str | None:
    """grupo_id do config, senao o grupo FINANCEIRO + LOGÍSTICA achado pelo
    nome no gateway. None = gateway fora ou o numero nao esta no grupo."""
    if cfg["grupo_id"]:
        return cfg["grupo_id"]
    if listar is None:
        from integracao_openwa import listar_grupos as listar
    alvo = _so_letras(NOME_GRUPO)
    for g in listar((config or {}).get("whatsapp_notificacoes") or {}) or []:
        if isinstance(g, dict) and g.get("id") and _so_letras(g.get("name")) == alvo:
            return g["id"]
    return None


def preparar_aviso(conn, pagamento_id: int, evento: str, config: dict) -> dict:
    """Grava o texto do aviso no lancamento. Trava desligada = 'simulado' na
    hora (nada sai); ligada = 'enviando' e quem chama dispara enviar_aviso."""
    p = obter(conn, pagamento_id)
    texto = TEXTOS[evento](p)
    situacao = AVISO_ENVIANDO if config_pagamentos(config)["ativo"] else AVISO_SIMULADO
    motivo = None if situacao == AVISO_ENVIANDO else "envio desligado (pagamentos_pontuais.ativo)"
    conn.execute("UPDATE pagamentos_pontuais SET aviso_evento = ?, aviso_texto = ?, aviso_situacao = ?, "
                 "aviso_motivo = ?, aviso_em = ? WHERE id = ?",
                 (evento, texto, situacao, motivo, _agora(), pagamento_id))
    conn.commit()
    if situacao == AVISO_SIMULADO:
        logger.info("[SIMULACAO] Pagamento #%s (%s) nao enviado ao grupo %s. Texto:\n%s",
                    pagamento_id, evento, NOME_GRUPO, texto)
    return {"situacao": situacao, "texto": texto}


def enviar_aviso(config: dict, pagamento_id: int, caminho_db=None, despachar=None, listar=None) -> str:
    """Manda o aviso preparado (situacao 'enviando') e grava o resultado.
    Nunca levanta excecao: roda numa thread depois da resposta da tela."""
    try:
        if despachar is None:
            from notificar_whatsapp import despachar
        conn = conectar(caminho_db)
        try:
            p = obter(conn, pagamento_id)
            if not p or p["aviso_situacao"] != AVISO_ENVIANDO:
                return "ignorado"
            grupo = grupo_destino(config, config_pagamentos(config), listar)
            if grupo is None:
                situacao, motivo = "nao_enviado", f"grupo {NOME_GRUPO} não encontrado no WhatsApp"
            else:
                situacao = despachar(config, ORIGEM_WHATSAPP, p["aviso_evento"], p["aviso_texto"],
                                     assinatura=f"pagamento-{pagamento_id}-{p['aviso_evento']}", grupo_id=grupo)
                motivo = {"desligado": "WhatsApp das notificações desligado ou sem gateway",
                          "nao_enviado": "teto do dia, canal em pausa ou repetida",
                          "falhou": "gateway fora do ar ou envio recusado"}.get(situacao)
            conn.execute("UPDATE pagamentos_pontuais SET aviso_situacao = ?, aviso_motivo = ?, aviso_em = ? "
                         "WHERE id = ?", (situacao, motivo, _agora(), pagamento_id))
            conn.commit()
            return situacao
        finally:
            conn.close()
    except Exception:
        logger.exception("Pagamento #%s: falha ao enviar o aviso", pagamento_id)
        return "falhou"


def disparar_aviso(conn, pagamento_id: int, evento: str, config: dict, iniciar=None) -> dict:
    """Prepara e, com a trava ligada, envia em segundo plano (o despachar
    pode esperar o intervalo minimo entre mensagens)."""
    aviso = preparar_aviso(conn, pagamento_id, evento, config)
    if aviso["situacao"] == AVISO_ENVIANDO:
        iniciar = iniciar or (lambda alvo, args: threading.Thread(target=alvo, args=args, daemon=True).start())
        iniciar(enviar_aviso, (config, pagamento_id))
    return aviso


def pode_reenviar(p: dict) -> bool:
    return bool(p.get("aviso_evento")) and p.get("aviso_situacao") in ("nao_enviado", "falhou", "desligado")
