# -*- coding: utf-8 -*-
"""
nucleo/canhotos_manuais.py

Canhoto enviado pela tela de baixa de motorista sem app (Hugo, 09/10):
foto/PDF vira PDF em dados/canhotos_manuais/{PS}.pdf (+ copia no GCS) e a
expedicao (expedir_pedidos.py da raiz) anexa na Stokki quando a Vuupt
nao tem canhoto. Tem que existir ANTES da conclusao na Vuupt: a Stokki
nao aceita anexo em pedido ja expedido.
"""
import io
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from nucleo import banco

logger = logging.getLogger(__name__)
LIMITE_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 40_000_000       # foto maior que isso e recusada antes de decodificar (memoria do painel)
LADO_MAXIMO = 3000            # foto e reduzida a no maximo isso no maior lado antes do PDF
_HEIC = (b"ftypheic", b"ftypheix", b"ftyphevc", b"ftypmif1", b"ftypmsf1")
_RE_CODIGO = re.compile(r"^PS-\d+(-[RC]\d+)*$")


class CanhotoInvalido(ValueError):
    pass


def normalizar(codigo) -> str:
    return str(codigo or "").strip().lstrip("#").upper()


def _pasta(db_path: Path | None = None) -> Path:
    return Path(db_path or banco.DB_PATH).parent / "canhotos_manuais"


def para_pdf(conteudo: bytes) -> bytes:
    if len(conteudo) > LIMITE_BYTES:
        raise CanhotoInvalido("Arquivo maior que 15 MB.")
    if conteudo[:5] == b"%PDF-":
        return conteudo
    if conteudo[4:12] in _HEIC:
        raise CanhotoInvalido("Foto em HEIC não é aceita: tire a foto pela própria tela ou envie JPG/PDF.")
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(conteudo))      # preguicoso: so le o cabecalho
    except Exception as e:  # noqa: BLE001
        raise CanhotoInvalido("Formato não aceito: envie foto (JPG/PNG) ou PDF.") from e
    if img.size[0] * img.size[1] > MAX_PIXELS:
        raise CanhotoInvalido("Foto grande demais: tire de novo com resolução menor.")
    try:
        img.thumbnail((LADO_MAXIMO, LADO_MAXIMO))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise CanhotoInvalido("Formato não aceito: envie foto (JPG/PNG) ou PDF.") from e
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=150)
    return buf.getvalue()


def garantir_tabela(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS canhotos_manuais (
            codigo      TEXT PRIMARY KEY,
            service_id  INTEGER,
            caminho     TEXT NOT NULL,
            caminho_gcs TEXT,
            enviado_por TEXT,
            enviado_em  TEXT NOT NULL,
            origem      TEXT NOT NULL DEFAULT 'BAIXA_SEM_APP'
        )""")
    conn.commit()


def codigos_validos(codigo: str) -> list[str]:
    """'PS-1, PS-2' -> ['PS-1', 'PS-2'] (servico com mais de um pedido; a
    expedicao separa e procura cada um). Codigo fora do padrao PS-n
    levanta: o nome vira caminho de arquivo."""
    cods = [normalizar(c) for c in str(codigo or "").split(",") if c.strip()]
    if not cods or any(not _RE_CODIGO.match(c) for c in cods):
        raise CanhotoInvalido(f"Código de pedido inválido: {codigo!r}.")
    return cods


def salvar(conn: sqlite3.Connection, codigo: str, service_id: int | None, pdf: bytes, por: str,
           config: dict | None = None) -> dict:
    cods = codigos_validos(codigo)
    for cod in cods[1:]:
        _salvar_um(conn, cod, service_id, pdf, por, config)
    return _salvar_um(conn, cods[0], service_id, pdf, por, config)


def _salvar_um(conn: sqlite3.Connection, cod: str, service_id: int | None, pdf: bytes, por: str,
               config: dict | None) -> dict:
    pasta = _pasta()
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / f"{cod}.pdf"
    caminho.write_bytes(pdf)
    caminho_gcs = None
    if config and config.get("gcs"):
        try:
            from documentos_pedido.storage_gcs import enviar_documento
            caminho_gcs = enviar_documento(config, caminho, cod, "Canhoto")
        except Exception as e:  # noqa: BLE001 -- disco basta pra expedicao
            logger.warning(f"Canhoto de {cod} salvo em disco, mas nao subiu pro GCS: {e}")
    garantir_tabela(conn)
    conn.execute("""
        INSERT INTO canhotos_manuais (codigo, service_id, caminho, caminho_gcs, enviado_por, enviado_em)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(codigo) DO UPDATE SET service_id = excluded.service_id, caminho = excluded.caminho,
            caminho_gcs = excluded.caminho_gcs, enviado_por = excluded.enviado_por, enviado_em = excluded.enviado_em
    """, (cod, service_id, str(caminho), caminho_gcs, por, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    return {"codigo": cod, "caminho": str(caminho), "caminho_gcs": caminho_gcs}


def caminho_canhoto_manual(codigo: str, db_path: Path | None = None) -> Path | None:
    caminho = _pasta(db_path) / f"{normalizar(codigo)}.pdf"
    return caminho if caminho.exists() else None


# ── Lista de canhotos pendentes (Hugo, 09/10) ─────────────────────────────────

def pendentes(conn: sqlite3.Connection, desde: str, embarcador: str = "", motorista: str = "", busca: str = "",
              pagina: int = 1, por_pagina: int = 50) -> dict:
    """Pedidos ENTREGUES (rota com data >= `desde`) sem nenhum comprovante:
    checklist da Vuupt com 0 fotos (NULL = sem informacao, fica fora pra nao
    listar quem talvez tenha foto), sem canhoto/assinatura do app e sem
    canhoto ja enviado pelo painel. Mais recentes primeiro.
    Pedido entregue SEM nenhuma parada no nucleo (rota fora do espelho)
    entra tambem: data = atualizado_em do pedido (`data_aproximada`) e sem
    motorista (Hugo, 09/10)."""
    garantir_tabela(conn)
    filtros, params = [], [desde]
    if embarcador.strip():
        filtros.append("UPPER(ped.remetente_nome) LIKE ?")
        params.append(f"%{embarcador.strip().upper()}%")
    if motorista.strip():
        filtros.append("UPPER(r.motorista_nome) LIKE ?")
        params.append(f"%{motorista.strip().upper()}%")
    if busca.strip():
        filtros.append("ped.codigo LIKE ?")
        params.append(f"%{normalizar(busca)}%")
    extra = "".join(f" AND {f}" for f in filtros)
    data = "COALESCE(r.data_rota, SUBSTR(ped.atualizado_em, 1, 10))"
    base = f"""
        FROM nucleo_pedidos ped
        LEFT JOIN nucleo_paradas p ON REPLACE(p.codigo, '#', '') = ped.codigo AND p.situacao IN ('ENTREGUE', 'PARCIAL')
        LEFT JOIN nucleo_rotas r ON r.id = p.rota_id
        WHERE ped.status = 'ENTREGUE' AND ped.qtd_checklists = 0 AND {data} >= ?
          AND (p.id IS NOT NULL OR NOT EXISTS (SELECT 1 FROM nucleo_paradas p2
                                               WHERE REPLACE(p2.codigo, '#', '') = ped.codigo))
          AND ped.codigo NOT IN (SELECT codigo FROM canhotos_manuais)
          AND NOT EXISTS (SELECT 1 FROM nucleo_comprovantes c
                          WHERE c.parada_id = p.id AND c.tipo IN ('CANHOTO', 'ASSINATURA'))
          {extra}"""
    total = conn.execute(f"SELECT COUNT(DISTINCT ped.codigo) {base}", params).fetchone()[0]
    pagina = max(1, int(pagina or 1))
    linhas = []
    for row in conn.execute(f"""
        SELECT ped.codigo, ped.vuupt_service_id, MAX({data}), MAX(r.motorista_nome),
               MAX(ped.remetente_nome), MAX(ped.destinatario_nome), MAX(r.id) IS NULL
        {base}
        GROUP BY ped.codigo ORDER BY MAX({data}) DESC, ped.codigo DESC LIMIT ? OFFSET ?""",
                            (*params, por_pagina, (pagina - 1) * por_pagina)):
        l = dict(zip(("codigo", "service_id", "data_rota", "motorista", "embarcador", "destinatario"), row[:6]))
        l["data_aproximada"] = bool(row[6])
        linhas.append(l)
    return {"linhas": linhas, "total": total, "pagina": pagina, "por_pagina": por_pagina}
