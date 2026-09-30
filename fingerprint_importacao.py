"""
fingerprint_importacao.py
Calcula e compara um "fingerprint" (hash) do conteúdo de cada payload de
pedido enviado ao VUUPT, para detectar se algo realmente mudou desde a
última importação — evitando reenviar (PUT) pedidos idênticos só porque
ainda estão com status='not_assigned'.

A latitude/longitude geocodificada (ver steps/geocodificacao.py) FAZ
PARTE do hash principal — ou seja, se a geocodificação vier diferente
da salva (mesmo que o endereço em texto não tenha mudado, por exemplo
o Google atualizar a precisão), isso por si só conta como alteração e
dispara o reenvio do serviço com a lat/long nova.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "dados" / "dados.db"

# Campos do payload que, se mudarem, justificam reenvio. Deliberadamente
# exclui campos que mudam por si só sem alteração real do pedido (ex:
# nenhum no momento, mas estrutura preparada para isso no futuro).
CAMPOS_IGNORADOS_NO_HASH = set()


def _calcular_hash(payload: dict) -> str:
    """
    Calcula um hash estável do conteúdo do payload — ordena as chaves
    para garantir que a mesma informação sempre produza o mesmo hash,
    independente da ordem de inserção no dict. Inclui latitude/longitude
    (se presentes em customer/sender) como parte do conteúdo relevante —
    uma mudança de coordenada, mesmo sem mudança no texto do endereço,
    conta como alteração real e dispara reenvio.
    """
    payload_relevante = {
        k: v for k, v in payload.items() if k not in CAMPOS_IGNORADOS_NO_HASH
    }
    payload_serializado = json.dumps(payload_relevante, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload_serializado.encode("utf-8")).hexdigest()


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fingerprint_importacao_vuupt (
            codigo         TEXT PRIMARY KEY,
            hash_conteudo  TEXT,
            hash_endereco  TEXT,
            customer_id    INTEGER,
            sender_id      INTEGER,
            atualizado_em  TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    return conn


def houve_alteracao(codigo: str, payload: dict) -> bool:
    """
    Compara o hash do payload atual com o último hash salvo para esse
    código. Retorna True se for a primeira vez (sem histórico) ou se o
    conteúdo mudou — incluindo mudança só de latitude/longitude, já que
    elas fazem parte do hash; False se for idêntico ao que já foi enviado.
    """
    hash_atual = _calcular_hash(payload)

    conn = _db()
    row = conn.execute(
        "SELECT hash_conteudo FROM fingerprint_importacao_vuupt WHERE codigo = ?",
        (codigo,)
    ).fetchone()
    conn.close()

    if row is None:
        return True  # nunca importado antes — sempre considera alteração

    return row["hash_conteudo"] != hash_atual


def salvar_fingerprint(codigo: str, payload: dict, customer_id: int | None = None, sender_id: int | None = None):
    """
    Salva/atualiza o hash do payload enviado, para comparação na próxima
    execução. Também salva o hash específico de endereço (campos
    'customer.address' e 'sender.address', se presentes) e, quando
    disponíveis, os IDs de contato retornados pela API — usados para
    decidir e executar a forma "leve" de atualização (por ID, sem
    reenviar endereço) nas próximas importações.
    """
    hash_atual = _calcular_hash(payload)
    hash_endereco_atual = _calcular_hash_endereco(payload)

    conn = _db()
    conn.execute("""
        INSERT INTO fingerprint_importacao_vuupt
            (codigo, hash_conteudo, hash_endereco, customer_id, sender_id, atualizado_em)
        VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))
        ON CONFLICT(codigo) DO UPDATE SET
            hash_conteudo = excluded.hash_conteudo,
            hash_endereco = excluded.hash_endereco,
            customer_id   = COALESCE(excluded.customer_id, fingerprint_importacao_vuupt.customer_id),
            sender_id     = COALESCE(excluded.sender_id, fingerprint_importacao_vuupt.sender_id),
            atualizado_em = datetime('now','localtime')
    """, (codigo, hash_atual, hash_endereco_atual, customer_id, sender_id))
    conn.commit()
    conn.close()


def _calcular_hash_endereco(payload: dict) -> str:
    """
    Calcula um hash dos campos de endereço (customer.address,
    customer.latitude, customer.longitude, sender.address) — separado
    do hash geral do pedido. Usado para decidir se o ENDEREÇO TEXTUAL
    realmente mudou (ver endereco_mudou), o que determina se a forma
    "leve" (por customer_id, sem reenviar nenhum dado de endereço) pode
    ser usada. Latitude/longitude entram aqui também: se a geocodificação
    mudou, o hash de endereço muda, e a forma leve deixa de ser aplicada
    — forçando o envio do endereço completo (com a lat/long nova) na
    próxima atualização.
    """
    customer = payload.get("customer") or {}
    enderecos = {
        "customer_address": customer.get("address", ""),
        "customer_latitude": customer.get("latitude", ""),
        "customer_longitude": customer.get("longitude", ""),
        "sender_address": (payload.get("sender") or {}).get("address", ""),
    }
    enderecos_serializado = json.dumps(enderecos, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(enderecos_serializado.encode("utf-8")).hexdigest()


def endereco_mudou(codigo: str, payload: dict) -> bool:
    """
    Compara o hash de endereço atual com o último salvo (que agora
    inclui latitude/longitude — ver _calcular_hash_endereco). Retorna
    True se for a primeira vez (sem histórico), se o endereço em texto
    mudou, OU se a geocodificação (lat/long) mudou mesmo com o mesmo
    texto de endereço. Em qualquer um desses casos, é necessário enviar
    o objeto completo customer/sender (com a lat/long atual) em vez da
    forma leve por ID.
    """
    hash_endereco_atual = _calcular_hash_endereco(payload)

    conn = _db()
    row = conn.execute(
        "SELECT hash_endereco FROM fingerprint_importacao_vuupt WHERE codigo = ?",
        (codigo,)
    ).fetchone()
    conn.close()

    if row is None or row["hash_endereco"] is None:
        return True  # sem histórico de endereço — trata como mudança, por segurança

    return row["hash_endereco"] != hash_endereco_atual


def buscar_ids_salvos(codigo: str) -> tuple[int | None, int | None]:
    """
    Retorna (customer_id, sender_id) salvos da última importação deste
    código, ou (None, None) se não houver registro — usado para montar o
    payload "leve" (por ID) quando o endereço/geocodificação não mudou.
    """
    conn = _db()
    row = conn.execute(
        "SELECT customer_id, sender_id FROM fingerprint_importacao_vuupt WHERE codigo = ?",
        (codigo,)
    ).fetchone()
    conn.close()

    if row is None:
        return None, None
    return row["customer_id"], row["sender_id"]
