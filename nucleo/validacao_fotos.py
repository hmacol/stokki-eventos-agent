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
  2. Visão do Claude (Haiku 5.5 desde 20/11; era 4.5): julga
     legibilidade real (reflexo, corte, escuro), diz que documento é,
     lê os números de NF visíveis (ou as linhas PS da canhoteira) e para
     que lado está o texto; no pedágio, se é recibo e o valor.

Regras do Hugo (20/11):
  - Canhoteira da FreshLog vale como canhoto, conferida pela linha do PS
    da parada (a folha junta várias entregas); NF não é cobrada nela.
  - Foto que não é documento (fachada, portão, carga) é gravada como
    comprovante FACHADA, não vale como canhoto e o app pede outra foto
    sem liberar o "não consigo melhorar".
  - A foto gravada sai girada com o texto na posição de leitura
    (`corrigir_orientacao`); o sha256 continua o do original.

FICA INATIVA POR PADRÃO (Hugo, 12/09: "não quero esse custo agora, mas
deixar pronto"). Liga no config.yaml:

    api_motorista:
      validacao_fotos:
        ativo: true                 # padrão false
        modelo: claude-haiku-5-5    # padrão desde 20/11 (~US$ 0,0006/foto); haiku-4-5 errava NF
        esforco: low                # vazio = não manda effort; o haiku-4-5 nunca recebe
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

MODELO_PADRAO = "claude-haiku-5-5"   # 20/11: no teste com 66 canhotos reais o 4.5 reprovou 23 bons (leu NF errada)
ESFORCO_PADRAO = "low"               # haiku-5-5 pensa por padrão; low = mesmo veredito, menos tokens
NITIDEZ_MINIMA_PADRAO = 40.0
MAX_TENTATIVAS_PADRAO = 2
LADO_MAX_MODELO = 1600      # px; acima disso reduz antes de mandar pro modelo (custo e limite de 5 MB)
LADO_NITIDEZ = 1000         # px; nitidez medida sempre nesse tamanho pra o limiar valer pra qualquer câmera

APROVADO = "APROVADO"
REPROVADO = "REPROVADO"
NAO_VERIFICADO = "NAO_VERIFICADO"

TIPO_CANHOTO = "CANHOTO"
TIPO_PEDAGIO = "PEDAGIO"
TIPO_FACHADA = "FACHADA"   # foto mandada como canhoto que não é documento: fica guardada, não vale (Hugo, 20/11)

# Em que sentido se lê o texto na foto -> transposição do PIL que o deixa
# na posição de leitura (ROTATE_90 = 90° anti-horário). Texto que se lê de
# cima para baixo tem o topo das letras virado pra direita: gira anti-horário.
# Perguntar o sentido de leitura (e não "pra onde aponta o topo") errou
# menos no teste de 20/11 -- ver DOC_EXECUCAO_CLAUDE_APP_MOTORISTAS.md 4.2.
_GIRO_DO_SENTIDO = {"DE_CIMA_PARA_BAIXO": "ROTATE_90", "DE_CABECA_PARA_BAIXO": "ROTATE_180",
                    "DE_BAIXO_PARA_CIMA": "ROTATE_270"}
# Mais meia volta: quando a conferência do giro acha o texto invertido.
_GIRO_MAIS_180 = {"ROTATE_90": "ROTATE_270", "ROTATE_270": "ROTATE_90", "ROTATE_180": None}

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
        "esforco": str(cfg.get("esforco", ESFORCO_PADRAO) or ""),
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

_PADRAO_CODIGO_BASE = re.compile(r"PS-?\d{4,6}", re.IGNORECASE)


def codigos_base(codigo: str | None) -> list[str]:
    """'#PS-36623-R2' -> ['PS-36623'].

    O `codigo` da parada vem da VUUPT com '#' na frente e sufixo de
    reentrega/recoleta ('-R1', '-C1', e empilhado '-R1-R1'), enquanto
    `documentos_processados.codigo_pedido` guarda sempre o código BASE
    sem '#'. Sem isso a NF nunca casa: medido na VPS em 12/09, só 3 de
    1.017 paradas dos últimos 15 dias achavam a nota.

    Extrai o PREFIXO em vez de tirar sufixo do fim (achado 20/08 do
    fluxo irmão: regex ancorada em '$' só tira o ÚLTIMO sufixo e
    devolve 'PS-36741-R1'), e quebra por vírgula porque o code da VUUPT
    pode agrupar pedidos combinados. Mesma normalização de
    painel_agentes/planejamento_rotas.py::_codigo_base."""
    saida = []
    for pedaco in (codigo or "").split(","):
        m = _PADRAO_CODIGO_BASE.match(pedaco.strip().lstrip("#"))
        base = m.group(0).upper() if m else pedaco.strip().lstrip("#").upper()
        if base and base not in saida:
            saida.append(base)
    return saida


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
    bases = codigos_base(codigo)
    if not bases:
        return []
    existe = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='documentos_processados'").fetchone()
    if not existe:
        return []
    marcadores = ",".join("?" for _ in bases)
    try:
        rows = conn.execute(f"""
            SELECT DISTINCT d.numero_nf FROM documentos_processados d
            WHERE UPPER(d.codigo_pedido) IN ({marcadores}) AND d.tipo = 'Nota Fiscal' AND d.status = 'ENVIADO'
              AND d.numero_nf IS NOT NULL AND d.numero_nf != ''
              AND (SELECT COUNT(DISTINCT o.codigo_pedido) FROM documentos_processados o
                   WHERE o.numero_nf = d.numero_nf AND o.tipo = 'Nota Fiscal' AND o.status = 'ENVIADO' AND o.codigo_pedido IS NOT NULL) = 1
            ORDER BY d.numero_nf
        """, bases).fetchall()
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


def giro_para_leitura(resultado: dict | None) -> str | None:
    """Nome da transposição que põe o texto em pé, ou None se já está.
    Usa o giro já conferido (`giro`) quando existe; senão o da leitura."""
    resultado = resultado or {}
    if "giro" in resultado:
        return resultado["giro"]
    leitura = resultado.get("leitura") or {}
    return _GIRO_DO_SENTIDO.get(str(leitura.get("sentido_do_texto") or "").upper())


def _girar(imagem: bytes, giro: str) -> bytes:
    from PIL import Image
    im = _abrir(imagem).transpose(getattr(Image.Transpose, giro))
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90, optimize=True)
    return buf.getvalue()


def _conferir_giro(cfg: dict, imagem: bytes, giro: str, tokens: dict) -> str | None:
    """O modelo erra o lado em ~4% das fotos (direita x esquerda; teste de
    20/11 com 66 canhotos) e a foto sai de cabeça pra baixo. Aqui a foto
    JÁ GIRADA volta pro modelo com uma pergunta só; se vier invertida,
    soma meia volta. Falha na conferência mantém o giro da leitura."""
    try:
        r, t = _chamar_modelo(cfg, _girar(imagem, giro), "image/jpeg", _PROMPT_GIRO, _SCHEMA_GIRO)
    except Exception as e:
        logger.warning(f"Giro não conferido ({type(e).__name__}: {e}).")
        return giro
    for k in ("entrada", "saida"):
        tokens[k] = (tokens.get(k) or 0) + (t.get(k) or 0)
    if r and r.get("texto") == "DE_CABECA_PARA_BAIXO":
        return _GIRO_MAIS_180[giro]
    return giro


def corrigir_orientacao(conteudo: bytes, resultado: dict | None) -> bytes:
    """Foto com o texto na posição de leitura (Hugo, 20/11): o modelo diz
    em que sentido o texto corre (e confere o giro) e aqui a imagem gira.
    Sem giro (ou se não abrir) devolve o original intacto, sem reencodar."""
    giro = giro_para_leitura(resultado)
    if not giro:
        return conteudo
    try:
        return _girar(conteudo, giro)   # _abrir já aplica o EXIF: o giro vale sobre a imagem como ela aparece
    except Exception as e:
        logger.warning(f"Foto não girada ({giro}): {e}")
        return conteudo


# ── modelo ─────────────────────────────────────────────────────────────────────

_SCHEMA_CANHOTO = {
    "type": "object",
    "properties": {
        "legivel": {"type": "boolean", "description": "O texto impresso do documento dá pra ler na foto"},
        "documento": {"type": "string", "enum": ["CANHOTO_NF", "DANFE", "CANHOTEIRA_FRESHLOG", "OUTRO_COMPROVANTE", "NAO_E_DOCUMENTO"]},
        "numeros_nf": {"type": "array", "items": {"type": "string"},
                       "description": "Números de Nota Fiscal visíveis na foto, só dígitos, como impressos (ex.: 000012345)"},
        "linhas_canhoteira": {
            "type": "array",
            "description": "Só na CANHOTEIRA_FRESHLOG: uma entrada por linha de entrega visível",
            "items": {
                "type": "object",
                "properties": {
                    "codigo_pedido": {"type": "string", "description": "Código impresso na linha, ex.: PS-44598"},
                    "preenchida": {"type": "boolean", "description": "A linha tem recebedor ou assinatura escritos à mão"},
                },
                "required": ["codigo_pedido", "preenchida"],
                "additionalProperties": False,
            },
        },
        "assinatura_ou_identificacao": {"type": "boolean"},
        "sentido_do_texto": {"type": "string",
                             "enum": ["NORMAL", "DE_CIMA_PARA_BAIXO", "DE_BAIXO_PARA_CIMA", "DE_CABECA_PARA_BAIXO", "SEM_TEXTO"],
                             "description": "Em que sentido se lê o texto impresso, olhando a imagem como ela está"},
        "problemas": {"type": "array", "items": {"type": "string", "enum": ["DESFOCADA", "ESCURA", "REFLEXO", "CORTADA", "SEM_ASSINATURA", "DOCUMENTO_ERRADO"]}},
        "motivo": {"type": "string", "description": "Uma frase curta em português explicando o julgamento"},
    },
    "required": ["legivel", "documento", "numeros_nf", "linhas_canhoteira", "assinatura_ou_identificacao",
                 "sentido_do_texto", "problemas", "motivo"],
    "additionalProperties": False,
}

_PROMPT_CANHOTO = """Você confere fotos de canhoto de entrega tiradas pelo motorista da FreshLog no momento da entrega.

O canhoto é a tira destacável do DANFE (Nota Fiscal eletrônica), assinada por quem recebeu. Formato estreito é normal. Também pode ser o DANFE inteiro ou outro comprovante de entrega assinado.

Existe ainda a CANHOTEIRA da FreshLog: uma folha com o logo FRESHLOG e o título "CANHOTEIRA", com uma linha por entrega. Cada linha traz o código do pedido (ex.: "PS-44598 · NF —"), o embarcador, o cliente e os campos Recebedor, Assinatura e Data. Uma mesma folha junta várias entregas. A foto pode mostrar só um pedaço da folha, sem o cabeçalho: se aparecem linhas com "PS-número · NF —" e os campos Recebedor/Assinatura, é a CANHOTEIRA_FRESHLOG.

Julgue APENAS o que está na foto:
1. legivel: o texto impresso dá pra ler? Foto desfocada, escura, com reflexo forte ou cortada no que importa = não legível.
2. documento: o que aparece (CANHOTO_NF, DANFE, CANHOTEIRA_FRESHLOG, OUTRO_COMPROVANTE, ou NAO_E_DOCUMENTO se for fachada, portão, carga, caixa, tela, chão...).
3. numeros_nf: TODOS os números de Nota Fiscal legíveis na foto, só dígitos, exatamente como impressos (o canhoto traz "NF-e Nº 000.012.345" ou "Nº 12345"; o DANFE traz "Nº 000.012.345" e a chave de acesso de 44 dígitos -- NÃO inclua a chave de acesso, só o número da nota). Na canhoteira, os códigos PS NÃO são número de NF. Lista vazia se nenhum número for legível. Nunca invente dígitos: se um número estiver parcialmente ilegível, não o inclua.
4. linhas_canhoteira: só na canhoteira, uma entrada por linha visível, com o código PS exatamente como impresso e se a linha tem recebedor ou assinatura escritos à mão. Lista vazia nos outros documentos.
5. assinatura_ou_identificacao: há assinatura, nome ou documento de quem recebeu?
6. sentido_do_texto: olhando a imagem exatamente como ela está, sem girar, em que sentido se lê o texto impresso do documento? NORMAL (linhas na horizontal, lidas da esquerda para a direita), DE_CIMA_PARA_BAIXO (linhas na vertical, a leitura desce), DE_BAIXO_PARA_CIMA (linhas na vertical, a leitura sobe) ou DE_CABECA_PARA_BAIXO (linhas na horizontal, mas invertidas). SEM_TEXTO se não houver texto.
7. problemas: liste os que se aplicam.
8. motivo: uma frase curta em português (vai ser mostrada ao motorista)."""

_SCHEMA_GIRO = {
    "type": "object",
    "properties": {"texto": {"type": "string", "enum": ["EM_PE", "DE_CABECA_PARA_BAIXO", "DE_LADO", "SEM_TEXTO"]}},
    "required": ["texto"],
    "additionalProperties": False,
}

_PROMPT_GIRO = """Olhe o texto impresso do documento nesta foto, exatamente como a imagem está, sem girar.
O texto está EM_PE (dá pra ler normalmente), DE_CABECA_PARA_BAIXO, DE_LADO, ou é SEM_TEXTO?"""

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
    output_config = {"format": {"type": "json_schema", "schema": schema}}
    # haiku-4-5 não aceita effort (400); os modelos novos pensam e aceitam.
    if cfg.get("esforco") and not str(cfg["modelo"]).startswith("claude-haiku-4"):
        output_config["effort"] = cfg["esforco"]
    resp = client.messages.create(
        model=cfg["modelo"],
        max_tokens=2048,   # o raciocínio do haiku-5-5 conta aqui (~370 tokens por foto no teste de 20/11)
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.standard_b64encode(imagem).decode("ascii")}},
                {"type": "text", "text": prompt},
            ],
        }],
        output_config=output_config,
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
                 nf_alvo: str | None = None, valor_informado: float | None = None,
                 codigos_pedido: list[str] | None = None) -> dict:
    """Decide APROVADO / REPROVADO / NAO_VERIFICADO pra uma foto.

    tipo CANHOTO: `nf_alvo` é a NF que ESTA foto deve mostrar (um canhoto
    por NF -- Hugo, 12/09); `nfs_esperadas` são todas as do pedido (usado
    quando o app não diz qual). Sem nenhuma NF conhecida, não cobra NF.
    `codigos_pedido` (PS-xxx da parada) é o que se procura na canhoteira.
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
        giro = giro_para_leitura(base)
        if giro and leitura.get("documento") != "NAO_E_DOCUMENTO":
            giro = _conferir_giro(cfg, imagem, giro, base["tokens"])
        base["giro"] = giro
        return _decidir_canhoto(cfg, base, leitura, nfs_esperadas or [], nf_alvo, codigos_pedido or [])
    return _decidir_pedagio(base, leitura, valor_informado)


def redecidir_canhoto(cfg: dict, salvo: dict, *, nfs_esperadas: list[str] | None = None,
                      nf_alvo: str | None = None, codigos_pedido: list[str] | None = None) -> dict | None:
    """Refaz a decisão de um canhoto a partir da leitura guardada, SEM
    chamar o modelo. A mesma foto pode servir a várias paradas (a
    canhoteira junta várias entregas; o mesmo canhoto pode ter 2 NFs), e o
    veredito depende da parada. None se não há leitura guardada (foto
    reprovada na nitidez, validação antiga...)."""
    leitura = salvo.get("leitura")
    if not isinstance(leitura, dict):
        return None
    base = {"nitidez": salvo.get("nitidez"), "modelo": salvo.get("modelo"), "avisos": [],
            "leitura": leitura, "problemas": list(leitura.get("problemas") or [])}
    if "giro" in salvo:
        base["giro"] = salvo["giro"]
    return _decidir_canhoto(cfg, base, leitura, nfs_esperadas or [], nf_alvo, codigos_pedido or [])


def _decidir_canhoteira(base: dict, leitura: dict, codigos_pedido: list[str]) -> dict:
    """Canhoteira da FreshLog (Hugo, 20/11): vale como canhoto, mas a folha
    junta várias entregas -- então só aprova se a linha DESTE pedido
    aparece e está preenchida (recebedor ou assinatura). NF não é cobrada:
    a folha sai com "NF —"."""
    linhas = []
    for l in leitura.get("linhas_canhoteira") or []:
        for cod in codigos_base(str(l.get("codigo_pedido") or "")):
            linhas.append((cod, bool(l.get("preenchida"))))
    base["canhoteira_codigos"] = [c for c, _ in linhas]
    alvo = ", ".join(codigos_pedido)
    minhas = [preenchida for cod, preenchida in linhas if cod in codigos_pedido]
    if not minhas:
        return {**base, "resultado": REPROVADO,
                "motivo": f"A foto da canhoteira não mostra a linha do {alvo}. Fotografe a parte da folha com essa entrega."}
    if not any(minhas):
        return {**base, "resultado": REPROVADO,
                "motivo": f"Na canhoteira, a linha do {alvo} está sem recebedor e sem assinatura. Peça pra preencher e fotografe de novo."}
    return {**base, "resultado": APROVADO, "nf_confere": None, "motivo": f"Canhoteira com a linha do {alvo} preenchida."}


def _decidir_canhoto(cfg: dict, base: dict, leitura: dict, nfs_esperadas: list[str], nf_alvo: str | None,
                     codigos_pedido: list[str] | None = None) -> dict:
    motivo_modelo = (leitura.get("motivo") or "").strip()
    lidas = []
    for n in leitura.get("numeros_nf") or []:
        nn = normalizar_nf(n)
        if nn and nn not in lidas:
            lidas.append(nn)
    base["nf_lidas"] = lidas
    if leitura.get("documento") == "NAO_E_DOCUMENTO":
        # Fachada, portão, carga... não vale como canhoto (Hugo, 20/11): a
        # foto fica guardada como FACHADA e o app pede outra, sem o
        # "não consigo melhorar" -- ver aplicar_tentativas.
        return {**base, "resultado": REPROVADO, "exige_outra_foto": True, "guardar_como": TIPO_FACHADA,
                "motivo": "Esta foto não é o canhoto. Ela ficou guardada, mas fotografe o canhoto assinado."}
    if not leitura.get("legivel"):
        return {**base, "resultado": REPROVADO, "motivo": motivo_modelo or "Não dá pra ler o documento na foto. Tire de novo com mais luz e sem reflexo."}
    # Linha "PS-xxx" lida = canhoteira, mesmo que o modelo tenha chamado a
    # folha cortada de outra coisa (teste de 20/11: oscila entre rodadas).
    if codigos_pedido and (leitura.get("documento") == "CANHOTEIRA_FRESHLOG" or leitura.get("linhas_canhoteira")):
        return _decidir_canhoteira(base, leitura, codigos_pedido)

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
    # Foto que não é documento (fachada...) nunca libera: tentar de novo
    # não vai virar canhoto (Hugo, 20/11).
    libera = restantes <= 0 and not resultado.get("exige_outra_foto")
    return {
        **resultado,
        "tentativa": tentativa,
        "tentativas_restantes": restantes,
        "pode_seguir": (not reprovado) or libera,
        "revisao_humana": reprovado and libera,
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
