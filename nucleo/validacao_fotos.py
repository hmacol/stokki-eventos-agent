# -*- coding: utf-8 -*-
"""
nucleo/validacao_fotos.py

Validação automática das fotos que o app de motoristas manda (Hugo,
12/09): nitidez do canhoto e do recibo de pedágio, e o canhoto precisa
OBRIGATORIAMENTE mostrar o número da NF do pedido (um canhoto por NF).

Duas camadas, na ordem:
  1. Nitidez (barata, sem modelo): variância do Laplaciano da imagem em
     escala de cinza reduzida a ~1000 px. Abaixo de `nitidez_minima`
     reprova na hora ("foto tremida") sem gastar modelo.
  2. Visão do Claude (Haiku 4.5 -- escolha do Hugo, custo): julga
     legibilidade real (reflexo, corte, escuro), diz que documento é e
     lê os números de NF visíveis; no pedágio, se é recibo e o valor.

FICA INATIVA POR PADRÃO (Hugo, 12/09: "não quero esse custo agora, mas
deixar pronto"). Liga no config.yaml:

    api_motorista:
      validacao_fotos:
        ativo: true                 # padrão false
        modelo: claude-haiku-4-5    # padrão (Hugo, 12/09); Sonnet 5 custa 2x
        nitidez_minima: 40          # variância do Laplaciano
        max_tentativas: 2           # depois disso o app libera "não consigo melhorar"
        exigir_nf: true             # canhoto tem que mostrar a NF do pedido
        exigir_assinatura: false    # só aviso; true reprova sem assinatura

CALIBRAÇÃO DO LIMIAR (12/09, 40 canhotos reais de dados/canhotos/): foto
boa mediu de 158 a 2.390 (mediana ~680); a MESMA foto com desfoque
gaussiano de raio 3 -- o "tremido" que não dá pra ler -- caiu pra 4-50.
O padrão 40 derruba só o caso perdido e deixa o modelo julgar o resto.
Refazer a medição com fotos do próprio app antes de ligar em produção
(`py -3.11 nucleo/calibrar_nitidez.py`).

Chave do modelo: `anthropic.api_key` (a mesma do assistente do portal).

Resultado por foto: APROVADO | REPROVADO | NAO_VERIFICADO (inativo,
sem chave, modelo fora, formato não suportado...). O app trava a parada
com REPROVADO até `max_tentativas`, depois deixa seguir marcado pra
revisão humana; NAO_VERIFICADO nunca trava o motorista.

A validação é chamada ANTES do envio definitivo (POST /api/fotos/validar,
com o motorista ainda no cliente) e o resultado fica guardado por sha256
em `nucleo_validacoes_foto`; quando a mesma foto chega pela fila
(comprovante/pedágio), o servidor só copia o resultado -- não paga o
modelo duas vezes. Foto que chegou sem validação prévia (motorista sem
sinal na hora) é validada na chegada, marcada `no_ato=false`.
"""
import base64
import io
import json
import logging
import re
import sqlite3
from datetime import datetime

logger = logging.getLogger("nucleo.validacao_fotos")

MODELO_PADRAO = "claude-haiku-4-5"
NITIDEZ_MINIMA_PADRAO = 40.0
MAX_TENTATIVAS_PADRAO = 2
LADO_MAX_MODELO = 1600      # px; acima disso reduz antes de mandar pro modelo (custo e limite de 5 MB)
LADO_NITIDEZ = 1000         # px; nitidez medida sempre nesse tamanho pra o limiar valer pra qualquer câmera

APROVADO = "APROVADO"
REPROVADO = "REPROVADO"
NAO_VERIFICADO = "NAO_VERIFICADO"

TIPO_CANHOTO = "CANHOTO"
TIPO_PEDAGIO = "PEDAGIO"

_MEDIA_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}

_DDL = """
CREATE TABLE IF NOT EXISTS nucleo_validacoes_foto (
    sha256          TEXT PRIMARY KEY,
    tipo            TEXT NOT NULL,                  -- CANHOTO | PEDAGIO
    resultado       TEXT NOT NULL,                  -- APROVADO | REPROVADO | NAO_VERIFICADO
    motivo          TEXT,
    nitidez         REAL,
    modelo          TEXT,
    tokens_entrada  INTEGER,
    tokens_saida    INTEGER,
    agent_id        INTEGER,
    referencia      TEXT,                           -- parada:<id> | rota:<id>
    no_ato          INTEGER NOT NULL DEFAULT 1,     -- 1 = validada antes do envio (motorista no cliente)
    dados_json      TEXT,
    criado_em       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


# ── config ─────────────────────────────────────────────────────────────────────

def config_validacao(config: dict | None) -> dict:
    config = config or {}
    cfg = ((config.get("api_motorista") or {}).get("validacao_fotos") or {})
    return {
        "ativo": bool(cfg.get("ativo", False)),
        "modelo": str(cfg.get("modelo") or MODELO_PADRAO),
        "nitidez_minima": float(cfg.get("nitidez_minima", NITIDEZ_MINIMA_PADRAO)),
        "max_tentativas": max(1, int(cfg.get("max_tentativas", MAX_TENTATIVAS_PADRAO))),
        "exigir_nf": bool(cfg.get("exigir_nf", True)),
        "exigir_assinatura": bool(cfg.get("exigir_assinatura", False)),
        "api_key": (config.get("anthropic") or {}).get("api_key"),
    }


def publico(cfg: dict) -> dict:
    """O que o app precisa saber (vai junto do /api/checklist)."""
    return {"ativo": cfg["ativo"], "max_tentativas": cfg["max_tentativas"], "exigir_nf": cfg["exigir_nf"]}


# ── tabela ─────────────────────────────────────────────────────────────────────

def garantir_tabela(conn: sqlite3.Connection):
    conn.executescript(_DDL)


def buscar_validacao(conn: sqlite3.Connection, sha256: str) -> dict | None:
    garantir_tabela(conn)
    row = conn.execute("SELECT * FROM nucleo_validacoes_foto WHERE sha256 = ?", (sha256,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["dados"] = json.loads(d.pop("dados_json") or "{}")
    except ValueError:
        d["dados"] = {}
    return d


def registrar_validacao(conn: sqlite3.Connection, sha256: str, tipo: str, resultado: dict, agent_id: int | None,
                        referencia: str | None, no_ato: bool = True) -> None:
    """Guarda (ou substitui) o resultado dessa foto. Chave = sha256 do
    arquivo: a mesma foto validada no ato e depois enviada pela fila
    casa aqui sem nova chamada ao modelo."""
    garantir_tabela(conn)
    dados = {k: v for k, v in resultado.items() if k not in ("resultado", "motivo", "nitidez", "modelo", "tokens")}
    tokens = resultado.get("tokens") or {}
    conn.execute("""
        INSERT INTO nucleo_validacoes_foto (sha256, tipo, resultado, motivo, nitidez, modelo, tokens_entrada, tokens_saida,
                                            agent_id, referencia, no_ato, dados_json, criado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sha256) DO UPDATE SET tipo = excluded.tipo, resultado = excluded.resultado, motivo = excluded.motivo,
            nitidez = excluded.nitidez, modelo = excluded.modelo, tokens_entrada = excluded.tokens_entrada,
            tokens_saida = excluded.tokens_saida, agent_id = excluded.agent_id, referencia = excluded.referencia,
            no_ato = excluded.no_ato, dados_json = excluded.dados_json, criado_em = excluded.criado_em
    """, (sha256, tipo, resultado["resultado"], resultado.get("motivo"), resultado.get("nitidez"), resultado.get("modelo"),
          tokens.get("entrada"), tokens.get("saida"), agent_id, referencia, 1 if no_ato else 0,
          json.dumps(dados, ensure_ascii=False, default=str), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


# ── NF do pedido ───────────────────────────────────────────────────────────────

def normalizar_nf(valor) -> str:
    """Só dígitos, sem zeros à esquerda: '000.012.345' == '12345'."""
    digitos = re.sub(r"\D", "", str(valor or ""))
    return digitos.lstrip("0")


def nfs_do_pedido(conn: sqlite3.Connection, codigo: str | None) -> list[str]:
    """Números de NF conhecidos do pedido (documentos_processados, tipo
    'Nota Fiscal', preenchido pelo processar_documentos). Lista vazia =
    pedido sem NF conhecida (placeholder da Stokki, cliente sem NF...):
    aí o canhoto passa só pela nitidez.

    FILTRO DA NF COMPARTILHADA (achado 12/09 nos dados reais): o
    extrator às vezes tira o número errado do PDF e o mesmo numero_nf
    acaba colado em vários pedidos (`245699` em 11 pedidos, `646` em 6).
    Exigir canhoto de uma NF dessas travaria o motorista pedindo uma
    nota que não existe na entrega -- então só entra a NF que pertence a
    ESTE pedido e a mais nenhum.
    """
    if not codigo:
        return []
    existe = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='documentos_processados'").fetchone()
    if not existe:
        return []
    try:
        rows = conn.execute("""
            SELECT DISTINCT d.numero_nf FROM documentos_processados d
            WHERE d.codigo_pedido = ? AND d.tipo = 'Nota Fiscal'
              AND d.numero_nf IS NOT NULL AND d.numero_nf != ''
              AND (SELECT COUNT(DISTINCT o.codigo_pedido) FROM documentos_processados o
                   WHERE o.numero_nf = d.numero_nf AND o.tipo = 'Nota Fiscal' AND o.codigo_pedido IS NOT NULL) = 1
            ORDER BY d.numero_nf
        """, (codigo,)).fetchall()
    except sqlite3.OperationalError:
        return []   # banco antigo sem a coluna numero_nf
    vistos, saida = set(), []
    for r in rows:
        n = normalizar_nf(r[0])
        if n and n not in vistos:
            vistos.add(n)
            saida.append(n)
    return saida


# ── imagem ─────────────────────────────────────────────────────────────────────

def _abrir(conteudo: bytes):
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(conteudo))
    im.load()
    return ImageOps.exif_transpose(im)


def medir_nitidez(conteudo: bytes) -> float | None:
    """Variância do Laplaciano (escala de cinza, lado maior = LADO_NITIDEZ).
    Quanto maior, mais bordas nítidas; foto tremida/desfocada fica baixa.
    None se a imagem não abrir."""
    try:
        import numpy as np
        im = _abrir(conteudo).convert("L")
        w, h = im.size
        escala = LADO_NITIDEZ / max(w, h)
        if escala < 1:
            im = im.resize((max(1, round(w * escala)), max(1, round(h * escala))))
        a = np.asarray(im, dtype=np.float32)
        if a.shape[0] < 3 or a.shape[1] < 3:
            return 0.0
        lap = 4 * a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]
        return round(float(lap.var()), 2)
    except Exception as e:
        logger.warning(f"Nitidez não medida: {e}")
        return None


def preparar_para_modelo(conteudo: bytes) -> tuple[bytes, str]:
    """JPEG com lado maior <= LADO_MAX_MODELO (menos tokens, cabe no
    limite da API). Devolve (bytes, media_type)."""
    im = _abrir(conteudo)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    w, h = im.size
    escala = LADO_MAX_MODELO / max(w, h)
    if escala < 1:
        im = im.resize((max(1, round(w * escala)), max(1, round(h * escala))))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue(), "image/jpeg"


# ── modelo ─────────────────────────────────────────────────────────────────────

_SCHEMA_CANHOTO = {
    "type": "object",
    "properties": {
        "legivel": {"type": "boolean", "description": "O texto impresso do documento dá pra ler na foto"},
        "documento": {"type": "string", "enum": ["CANHOTO_NF", "DANFE", "OUTRO_COMPROVANTE", "NAO_E_DOCUMENTO"]},
        "numeros_nf": {"type": "array", "items": {"type": "string"},
                       "description": "Números de Nota Fiscal visíveis na foto, só dígitos, como impressos (ex.: 000012345)"},
        "assinatura_ou_identificacao": {"type": "boolean"},
        "problemas": {"type": "array", "items": {"type": "string", "enum": ["DESFOCADA", "ESCURA", "REFLEXO", "CORTADA", "SEM_ASSINATURA", "DOCUMENTO_ERRADO"]}},
        "motivo": {"type": "string", "description": "Uma frase curta em português explicando o julgamento"},
    },
    "required": ["legivel", "documento", "numeros_nf", "assinatura_ou_identificacao", "problemas", "motivo"],
    "additionalProperties": False,
}

_PROMPT_CANHOTO = """Você confere fotos de canhoto de entrega tiradas pelo motorista da FreshLog no momento da entrega.

O canhoto é a tira destacável do DANFE (Nota Fiscal eletrônica), assinada por quem recebeu. Formato estreito é normal. Também pode ser o DANFE inteiro ou outro comprovante de entrega assinado.

Julgue APENAS o que está na foto:
1. legivel: o texto impresso dá pra ler? Foto desfocada, escura, com reflexo forte ou cortada no que importa = não legível.
2. documento: o que aparece (CANHOTO_NF, DANFE, OUTRO_COMPROVANTE, ou NAO_E_DOCUMENTO se for caixa, fachada, tela, chão...).
3. numeros_nf: TODOS os números de Nota Fiscal legíveis na foto, só dígitos, exatamente como impressos (o canhoto traz "NF-e Nº 000.012.345" ou "Nº 12345"; o DANFE traz "Nº 000.012.345" e a chave de acesso de 44 dígitos -- NÃO inclua a chave de acesso, só o número da nota). Lista vazia se nenhum número for legível. Nunca invente dígitos: se um número estiver parcialmente ilegível, não o inclua.
4. assinatura_ou_identificacao: há assinatura, nome ou documento de quem recebeu?
5. problemas: liste os que se aplicam.
6. motivo: uma frase curta em português (vai ser mostrada ao motorista)."""

_SCHEMA_PEDAGIO = {
    "type": "object",
    "properties": {
        "legivel": {"type": "boolean"},
        "eh_recibo_pedagio": {"type": "boolean", "description": "É um recibo/cupom de pedágio (praça, concessionária, tarifa)"},
        "valor_total": {"type": ["number", "null"], "description": "Valor pago em reais, se legível"},
        "praca_ou_concessionaria": {"type": ["string", "null"]},
        "data": {"type": ["string", "null"], "description": "Data impressa no recibo, se legível, no formato YYYY-MM-DD"},
        "problemas": {"type": "array", "items": {"type": "string", "enum": ["DESFOCADA", "ESCURA", "REFLEXO", "CORTADA", "DOCUMENTO_ERRADO"]}},
        "motivo": {"type": "string"},
    },
    "required": ["legivel", "eh_recibo_pedagio", "valor_total", "praca_ou_concessionaria", "data", "problemas", "motivo"],
    "additionalProperties": False,
}

_PROMPT_PEDAGIO = """Você confere fotos de recibo de pedágio tiradas pelo motorista da FreshLog para reembolso.

Julgue APENAS o que está na foto:
1. legivel: o texto do recibo dá pra ler?
2. eh_recibo_pedagio: é mesmo um cupom/recibo de pedágio (praça, concessionária, tarifa, categoria do veículo)? Extrato de tag/Sem Parar também vale. Foto de outra coisa = false.
3. valor_total: o valor pago em reais (número, ex.: 12.5), ou null se não der pra ler.
4. praca_ou_concessionaria e data (YYYY-MM-DD), se legíveis.
5. problemas: os que se aplicam.
6. motivo: uma frase curta em português (vai ser mostrada ao motorista)."""


def _chamar_modelo(cfg: dict, imagem: bytes, media_type: str, prompt: str, schema: dict) -> tuple[dict | None, dict]:
    """Uma chamada de visão com saída estruturada. Devolve (dict da
    resposta ou None se recusou, tokens). Levanta exceção em falha de
    rede/API -- quem chama transforma em NAO_VERIFICADO."""
    import anthropic
    client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=40.0, max_retries=1)
    resp = client.messages.create(
        model=cfg["modelo"],
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.standard_b64encode(imagem).decode("ascii")}},
                {"type": "text", "text": prompt},
            ],
        }],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    tokens = {"entrada": getattr(resp.usage, "input_tokens", None), "saida": getattr(resp.usage, "output_tokens", None)}
    if resp.stop_reason == "refusal":
        return None, tokens
    texto = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(texto), tokens


# ── decisão ────────────────────────────────────────────────────────────────────

def _nao_verificado(motivo: str, nitidez=None) -> dict:
    return {"resultado": NAO_VERIFICADO, "motivo": motivo, "nitidez": nitidez, "modelo": None}


def validar_foto(cfg: dict, tipo: str, conteudo: bytes, *, nfs_esperadas: list[str] | None = None,
                 nf_alvo: str | None = None, valor_informado: float | None = None) -> dict:
    """Decide APROVADO / REPROVADO / NAO_VERIFICADO pra uma foto.

    tipo CANHOTO: `nf_alvo` é a NF que ESTA foto deve mostrar (um canhoto
    por NF -- Hugo, 12/09); `nfs_esperadas` são todas as do pedido (usado
    quando o app não diz qual). Sem nenhuma NF conhecida, não cobra NF.
    tipo PEDAGIO: `valor_informado` só gera aviso VALOR_DIVERGE (o Hugo
    bate no painel); não reprova.
    """
    tipo = (tipo or "").upper()
    if tipo not in (TIPO_CANHOTO, TIPO_PEDAGIO):
        return _nao_verificado(f"Tipo de foto sem validação: {tipo}.")
    if not cfg.get("ativo"):
        return _nao_verificado("Validação automática desligada.")

    nitidez = medir_nitidez(conteudo)
    if nitidez is None:
        return _nao_verificado("Não foi possível abrir a imagem.")
    base = {"nitidez": nitidez, "modelo": None, "avisos": []}
    if nitidez < cfg["nitidez_minima"]:
        return {**base, "resultado": REPROVADO, "motivo": "Foto tremida ou desfocada. Segure firme, aproxime e tire de novo.",
                "problemas": ["DESFOCADA"]}

    if not cfg.get("api_key"):
        return {**base, "resultado": NAO_VERIFICADO, "motivo": "Sem chave do modelo (anthropic.api_key)."}
    try:
        imagem, media_type = preparar_para_modelo(conteudo)
        prompt, schema = (_PROMPT_CANHOTO, _SCHEMA_CANHOTO) if tipo == TIPO_CANHOTO else (_PROMPT_PEDAGIO, _SCHEMA_PEDAGIO)
        leitura, tokens = _chamar_modelo(cfg, imagem, media_type, prompt, schema)
    except Exception as e:
        logger.warning(f"Validação de {tipo} sem modelo ({type(e).__name__}: {e}) -- NAO_VERIFICADO.")
        return {**base, "resultado": NAO_VERIFICADO, "motivo": "Conferência automática indisponível agora."}
    base["modelo"] = cfg["modelo"]
    base["tokens"] = tokens
    if leitura is None:
        return {**base, "resultado": NAO_VERIFICADO, "motivo": "O modelo não analisou esta foto."}
    base["leitura"] = leitura
    base["problemas"] = list(leitura.get("problemas") or [])

    if tipo == TIPO_CANHOTO:
        return _decidir_canhoto(cfg, base, leitura, nfs_esperadas or [], nf_alvo)
    return _decidir_pedagio(base, leitura, valor_informado)


def _decidir_canhoto(cfg: dict, base: dict, leitura: dict, nfs_esperadas: list[str], nf_alvo: str | None) -> dict:
    motivo_modelo = (leitura.get("motivo") or "").strip()
    lidas = []
    for n in leitura.get("numeros_nf") or []:
        nn = normalizar_nf(n)
        if nn and nn not in lidas:
            lidas.append(nn)
    base["nf_lidas"] = lidas
    if leitura.get("documento") == "NAO_E_DOCUMENTO":
        return {**base, "resultado": REPROVADO, "motivo": motivo_modelo or "A foto não mostra o canhoto ou a nota fiscal."}
    if not leitura.get("legivel"):
        return {**base, "resultado": REPROVADO, "motivo": motivo_modelo or "Não dá pra ler o documento na foto. Tire de novo com mais luz e sem reflexo."}

    alvo = normalizar_nf(nf_alvo) if nf_alvo else None
    esperadas = [normalizar_nf(n) for n in nfs_esperadas if normalizar_nf(n)]
    if cfg.get("exigir_nf") and (alvo or esperadas):
        # O número impresso pode ter mais dígitos que o esperado (série
        # junto, ex.: "1-12345") -- aceita quando termina com o esperado.
        def confere(esp: str) -> bool:
            return any(l == esp or l.endswith(esp) for l in lidas)
        if alvo:
            base["nf_alvo"] = alvo
            base["nf_confere"] = confere(alvo)
            if not base["nf_confere"]:
                if not lidas:
                    return {**base, "resultado": REPROVADO, "motivo": f"Não deu pra ler o número da NF {alvo} na foto. Enquadre o número da nota."}
                return {**base, "resultado": REPROVADO, "motivo": f"Esta foto mostra a NF {', '.join(lidas)}, não a NF {alvo}. Fotografe o canhoto da NF {alvo}."}
        else:
            batidas = [e for e in esperadas if confere(e)]
            base["nf_confere"] = bool(batidas)
            base["nf_batidas"] = batidas
            if not batidas:
                if not lidas:
                    return {**base, "resultado": REPROVADO, "motivo": "Não deu pra ler o número da NF na foto. Enquadre o número da nota."}
                return {**base, "resultado": REPROVADO,
                        "motivo": f"A NF da foto ({', '.join(lidas)}) não é deste pedido (NF {', '.join(esperadas)})."}
    else:
        base["nf_confere"] = None

    if not leitura.get("assinatura_ou_identificacao"):
        if cfg.get("exigir_assinatura"):
            return {**base, "resultado": REPROVADO, "motivo": "Canhoto sem assinatura ou identificação de quem recebeu."}
        base["avisos"].append("SEM_ASSINATURA")
    return {**base, "resultado": APROVADO, "motivo": motivo_modelo or "Canhoto legível e NF conferida."}


def aplicar_tentativas(cfg: dict, resultado: dict, tentativa: int) -> dict:
    """Regra do Hugo (12/09): foto reprovada TRAVA a conclusão da parada,
    mas depois de `max_tentativas` o app libera um "não consigo melhorar"
    e a foto segue marcada pra revisão humana -- não dá pra prender o
    motorista no cliente. NAO_VERIFICADO nunca trava."""
    tentativa = max(1, int(tentativa or 1))
    reprovado = resultado.get("resultado") == REPROVADO
    restantes = max(0, cfg["max_tentativas"] - tentativa)
    return {
        **resultado,
        "tentativa": tentativa,
        "tentativas_restantes": restantes,
        "pode_seguir": (not reprovado) or restantes <= 0,
        "revisao_humana": reprovado and restantes <= 0,
    }


def _decidir_pedagio(base: dict, leitura: dict, valor_informado: float | None) -> dict:
    motivo_modelo = (leitura.get("motivo") or "").strip()
    base["valor_lido"] = leitura.get("valor_total")
    base["praca"] = leitura.get("praca_ou_concessionaria")
    base["data_recibo"] = leitura.get("data")
    if not leitura.get("eh_recibo_pedagio"):
        return {**base, "resultado": REPROVADO, "motivo": motivo_modelo or "A foto não parece ser um recibo de pedágio."}
    if not leitura.get("legivel"):
        return {**base, "resultado": REPROVADO, "motivo": motivo_modelo or "Não dá pra ler o recibo na foto. Tire de novo com mais luz."}
    try:
        lido = float(leitura.get("valor_total")) if leitura.get("valor_total") is not None else None
        informado = float(valor_informado) if valor_informado is not None else None
    except (TypeError, ValueError):
        lido, informado = None, None
    if lido is not None and informado is not None and abs(lido - informado) > 0.011:
        base["avisos"].append("VALOR_DIVERGE")
    return {**base, "resultado": APROVADO, "motivo": motivo_modelo or "Recibo legível."}
