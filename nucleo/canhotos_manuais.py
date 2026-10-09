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
