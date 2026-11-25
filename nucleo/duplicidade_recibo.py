"""Recibo repetido nas despesas da rota (revisao de seguranca, 24/11).

Com a foto da galeria no app, o motorista pode anexar recibo antigo, de outra
rota ou de outra pessoa. Dois sinais, ambos so MARCAM no painel (quem aprova
decide; nada e barrado, pra nao perder despesa legitima):

- IDENTICO: mesmo sha256 (mesmo arquivo) em outro lancamento.
- PARECIDO: hash visual (dHash 64 bits) a ate LIMITE_HAMMING bits de outro
  lancamento COM O MESMO VALOR informado. Recibos de pedagio tem layout quase
  igual, entao so a imagem parecida nao basta -- o valor igual corta o ruido.

Lancamentos CANCELADOS (o motorista desistiu) nao contam: mandar de novo o
recibo certo depois de cancelar o errado e legitimo.
"""
from __future__ import annotations

import io
import sqlite3

LIMITE_HAMMING = 6
MAX_PIXELS = 50_000_000
_heif_ok: bool | None = None


def _registrar_heif() -> None:
    global _heif_ok
    if _heif_ok is None:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
            _heif_ok = True
        except Exception:  # noqa: BLE001
            _heif_ok = False


def hash_visual(conteudo: bytes) -> str | None:
    """dHash de 64 bits em hex (16 caracteres); None se a imagem nao abrir.
    Nunca levanta: a falta do hash so tira o sinal PARECIDO."""
    try:
        from PIL import Image, ImageOps
        _registrar_heif()
        img = Image.open(io.BytesIO(conteudo), formats=["JPEG", "PNG", "WEBP", "HEIF"])
        if img.size[0] * img.size[1] > MAX_PIXELS:
            return None
        img = ImageOps.exif_transpose(img).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        px = img.tobytes()   # modo "L": 1 byte por pixel
        bits = 0
        for linha in range(8):
            for col in range(8):
                bits = (bits << 1) | (1 if px[linha * 9 + col] > px[linha * 9 + col + 1] else 0)
        return f"{bits:016x}"
    except Exception:  # noqa: BLE001
        return None


def distancia(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def marcar_duplicados(conn: sqlite3.Connection, itens: list[dict]) -> None:
    """Poe em cada item (linha de nucleo_pedagios) a chave `duplicados`: lista de
    {id, rota_id, motorista, motivo: 'IDENTICO'|'PARECIDO'} dos OUTROS lancamentos
    nao cancelados que se repetem com ele. Uma consulta so, comparada em memoria."""
    if not itens:
        return
    todos = conn.execute("""
        SELECT p.id, p.rota_id, p.sha256, p.hash_visual, p.valor_informado, r.motorista_nome
        FROM nucleo_pedagios p JOIN nucleo_rotas r ON r.id = p.rota_id
        WHERE p.status != 'CANCELADO' AND (p.sha256 IS NOT NULL OR p.hash_visual IS NOT NULL)
    """).fetchall()
    for item in itens:
        achados = []
        if item.get("status") != "CANCELADO":
            for o in todos:
                if o["id"] == item["id"]:
                    continue
                motivo = None
                if item.get("sha256") and o["sha256"] == item["sha256"]:
                    motivo = "IDENTICO"
                elif (item.get("hash_visual") and o["hash_visual"] and o["valor_informado"] == item.get("valor_informado")
                      and distancia(item["hash_visual"], o["hash_visual"]) <= LIMITE_HAMMING):
                    motivo = "PARECIDO"
                if motivo:
                    achados.append({"id": o["id"], "rota_id": o["rota_id"], "motorista": o["motorista_nome"], "motivo": motivo})
        item["duplicados"] = achados
