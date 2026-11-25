# -*- coding: utf-8 -*-
"""
comprovante_entrega/gerar_comprovante.py

Monta o comprovante proprio (gerar_pdf.py) de um pedido concluido: dados do
servico na Vuupt, NF e motorista/rota do nosso banco, fotos originais do
checklist e o link publico do portal no QR.

So LE (Vuupt por GET, banco em leitura) e grava o PDF pedido em --saida.

COMO USAR:
    py -3.11 comprovante_entrega/gerar_comprovante.py --pedido PS-44149 --saida comprovante.pdf
"""
import argparse
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path

_RAIZ_PROJETO = Path(__file__).parent.parent
for _p in (str(_RAIZ_PROJETO), str(_RAIZ_PROJETO / "portal_cliente")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from comprovante_entrega import fotos_vuupt, gerar_pdf  # noqa: E402
from notificacao_entregas import regras_entrega as regras  # noqa: E402
from notificacao_entregas.montar_email_entrega import dados_do_servico  # noqa: E402

logger = logging.getLogger("comprovante_entrega")

PORTAL_URL_PADRAO = "https://app.freshhub.com.br/cliente"


def motorista_e_rota(conn: sqlite3.Connection, service_id) -> tuple[str, str]:
    """(motorista, "Rota #11 · parada 2 de 10") do nucleo; vazio se nao achar."""
    try:
        row = conn.execute(
            "SELECT r.motorista_nome, r.nome, p.ordem, r.total_paradas FROM nucleo_paradas p "
            "JOIN nucleo_rotas r ON r.id = p.rota_id WHERE p.service_id = ? ORDER BY p.id DESC LIMIT 1",
            (service_id,)).fetchone()
    except sqlite3.Error as e:
        logger.warning(f"Motorista/rota do servico {service_id} indisponiveis: {e}")
        return "", ""
    if not row:
        return "", ""
    numero = re.search(r"#\s*(\d+)\s*$", row[1] or "")
    partes = [f"Rota #{numero.group(1)}" if numero else ""]
    if row[2] and row[3]:
        partes.append(f"parada {row[2]} de {row[3]}")
    return (row[0] or "").strip(), " · ".join(p for p in partes if p)


def link_publico(config: dict, servico: dict) -> str:
    portal = config.get("portal_cliente") or {}
    segredo = portal.get("secret_key")
    if not segredo or not servico.get("id") or not servico.get("sender_id"):
        return ""
    import link_canhoto
    return link_canhoto.url(servico["id"], servico["sender_id"], segredo,
                            (portal.get("url_base") or PORTAL_URL_PADRAO).rstrip("/"))


def montar(servico: dict, *, config: dict, conn: sqlite3.Connection, embarcadores: dict, buscar_nf,
           buscar_fotos=fotos_vuupt.buscar_fotos, exigir_foto: bool = False) -> bytes | None:
    """PDF do comprovante de um servico concluido da Vuupt. Com exigir_foto,
    devolve None quando nenhuma foto baixou (quem chama decide o que fazer)."""
    token = (config.get("vuupt_api") or {}).get("token", "")
    fotos = buscar_fotos(token, servico)
    if exigir_foto and not fotos:
        return None
    dados = dados_do_servico(servico, embarcadores.get(servico.get("sender_id")) or {},
                             buscar_nf(regras.codigo_limpo(servico.get("code"))), bool(fotos))
    dados["motorista"], dados["rota"] = motorista_e_rota(conn, servico.get("id"))
    return gerar_pdf.gerar(dados, fotos, link=link_publico(config, servico))


def do_servico(servico: dict, config: dict, exigir_foto: bool = False) -> bytes | None:
    """montar() com o nosso banco (embarcador, NF, motorista/rota)."""
    from notificacao_entregas import fingerprint_notificacao_entrega as fp
    from notificacao_entregas import notificar_entrega_concluida as rotina

    conn = fp.conectar()
    try:
        return montar(servico, config=config, conn=conn, embarcadores=rotina.carregar_embarcadores(conn),
                      buscar_nf=lambda codigo: rotina.buscar_nf_no_banco(conn, codigo), exigir_foto=exigir_foto)
    finally:
        conn.close()


def salvar_com_foto(servico: dict, config: dict, pasta: Path, validade_s: int = 0) -> Path | None:
    """Comprovante do servico em <pasta>/comprovante_<codigo>_<id>.pdf; None
    quando a foto do canhoto nao baixou. E o que e-mail e portal usam no lugar
    do PDF de checklist da Vuupt (que continua de reserva em quem chama).
    validade_s: reaproveita o arquivo gerado ha menos que isso (o portal usa,
    pra abrir o mesmo link varias vezes nao refazer Vuupt + desenho a cada vez).
    O id do servico vai no nome: reentrega pode repetir o codigo do original e
    a pasta e de todos os clientes, entao so o codigo misturaria comprovantes."""
    seguro = re.sub(r"[^A-Za-z0-9_-]", "_", regras.codigo_limpo(servico.get("code")))
    servico_id = re.sub(r"[^0-9]", "", str(servico.get("id") or ""))
    if not servico_id:
        return None
    destino = pasta / f"comprovante_{seguro}_{servico_id}.pdf"
    if validade_s and destino.exists() and time.time() - destino.stat().st_mtime < validade_s:
        return destino
    pdf = do_servico(servico, config, exigir_foto=True)
    if not pdf:
        return None
    pasta.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(pdf)
    return destino


def main(pedido: str, saida: Path) -> int:
    from notificacao_entregas import notificar_entrega_concluida as rotina
    from notificacao_entregas import vuupt_entregas

    config = rotina._carregar_config()
    servicos = vuupt_entregas.buscar_por_codigo((config.get("vuupt_api") or {}).get("token", ""), pedido)
    if not servicos:
        print(f"Nenhum servico concluido com o codigo {pedido} na Vuupt.")
        return 1
    pdf = do_servico(servicos[0], config)
    saida.write_bytes(pdf)
    print(f"{pedido}: {len(pdf)} bytes em {saida}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gera o comprovante proprio (PDF) de um pedido concluido")
    parser.add_argument("--pedido", required=True, help="Codigo do pedido concluido (ex.: PS-44149)")
    parser.add_argument("--saida", required=True, type=Path, help="Arquivo PDF de saida")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    sys.exit(main(args.pedido, args.saida))
