# -*- coding: utf-8 -*-
"""
conferir_pool.py

Batimento do pool ANTES do rascunho (Hugo, 05/10): pedido que já saiu não
pode virar parada de novo. Roda na sequência da tarde, logo antes de
criar_rotas_diarias.py --gerar-rascunho.

Para cada serviço not_assigned do pool, lê a situação na Stokki (/show):
  - Stokki "Enviado" E (outro serviço do mesmo pedido concluído com
    sucesso na Vuupt OU rota Lalamove COMPLETED com o pedido)
      -> cancela o serviço na Vuupt (sai do pool) e tira a parada dos
         rascunhos ainda não enviados. Casos reais de 04/10: reentrega R1
         que sobrou depois da original entregue; código antigo recriado.
  - Só a Stokki diz Enviado, ou Stokki diz Cancelado -> só avisa.
  - Mesmo pedido duas vezes no pool (original + reentrega) -> só avisa.
Regra do Hugo (05/10): cancela sozinho só quando Vuupt e Stokki concordam.

NÃO mexe na Stokki. Aviso curto no grupo ALERTAS FRESH (WhatsApp); a lista
completa fica no log (dados/conferir_pool.log).

COMO USAR:
    python roteirizacao/conferir_pool.py
    python roteirizacao/conferir_pool.py --modo-teste   # lê tudo, não cancela nem avisa
"""
import argparse
import logging
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logger = logging.getLogger("conferir_pool")

CANCELAR = "cancelar"
AVISAR = "avisar"
DONO_TRAVA_STOKKI = "conferir_pool"
TTL_TRAVA_STOKKI_S = 900
ESPERA_TRAVA_STOKKI_S = 600

_RE_CODIGO = re.compile(r"(PS-\d{4,6})", re.IGNORECASE)


# ── Regras puras ──────────────────────────────────────────────────────────────

def codigo_base(code) -> str | None:
    """'#PS-40426-R1' -> 'PS-40426' (pedido da Stokki, sem reentrega)."""
    m = _RE_CODIGO.search(str(code or ""))
    return m.group(1).upper() if m else None


def entregue_em_outro_servico(irmaos: list[dict], proprio_id: int) -> bool:
    """Outro serviço do mesmo pedido concluído com SUCESSO (mesmo critério
    de pedidos_parados_triagem._servico_com_sucesso)."""
    return any(s.get("id") != proprio_id and s.get("status") == "done"
               and s.get("completed_at") and not s.get("failed_reason_id")
               for s in irmaos)


def decidir(status_stokki: str, vuupt_entregue: bool, lalamove_entregue: bool) -> tuple[str | None, str | None]:
    """(ação, motivo do aviso)."""
    status = status_stokki or ""
    if re.search(r"cancel", status, re.IGNORECASE):
        return AVISAR, "Cancelado na Stokki"
    if re.search(r"enviado", status, re.IGNORECASE):
        if vuupt_entregue or lalamove_entregue:
            return CANCELAR, None
        return AVISAR, "Enviado na Stokki sem entrega na Vuupt"
    return None, None


def duplicados_no_pool(pool: list[dict]) -> dict[str, list[str]]:
    por_base: dict[str, list[str]] = {}
    for s in pool:
        base = codigo_base(s.get("code"))
        if base:
            por_base.setdefault(base, []).append(s.get("code"))
    return {b: sorted(cs) for b, cs in por_base.items() if len(cs) > 1}


def texto_whatsapp(cancelados: list[dict], avisos: list[dict], duplicados: dict) -> str:
    from notificar_whatsapp import MAX_MENSAGEM, _plural
    linhas = ["🔎 *Conferência do pool*"]
    if cancelados:
        linhas.append("• " + _plural(len(cancelados), "já entregue, tirado do pool", "já entregues, tirados do pool"))
    if duplicados:
        linhas.append("• " + _plural(len(duplicados), "pedido duplicado no pool", "pedidos duplicados no pool"))
    if avisos:
        linhas.append("• " + _plural(len(avisos), "pedido a conferir", "pedidos a conferir") + ":")
        # Códigos enquanto couber (nunca nome de cliente/NF); o resto no log.
        for a in avisos:
            if len("\n".join(linhas)) + len(a["codigo"]) + 3 > MAX_MENSAGEM:
                linhas[-1] += " …"
                break
            linhas[-1] += " " + a["codigo"].lstrip("#")
    return "\n".join(linhas)


# ── Leituras ──────────────────────────────────────────────────────────────────

def _bases_lalamove_entregues(db_path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        linhas = conn.execute("""
            SELECT p.codigo FROM rascunhos_rota r JOIN rascunhos_parada p ON p.rascunho_id = r.id
            WHERE r.lalamove_order_id IS NOT NULL AND r.lalamove_status = 'COMPLETED'""").fetchall()
    finally:
        conn.close()
    return {b for (c,) in linhas if (b := codigo_base(c))}


def _irmaos_vuupt(vuupt, base: str) -> list[dict]:
    """Todos os serviços do pedido: código base e reentregas (-R1, -R2),
    com e sem '#'. Reentrega tem code próprio, então busca as 3 primeiras."""
    vistos: dict[int, dict] = {}
    for sufixo in ("", "-R1", "-R2", "-R3"):
        for variante in (f"{base}{sufixo}", f"#{base}{sufixo}"):
            for s in vuupt.listar_servicos([{"field": "code", "operator": "eq", "value": variante}], per_page=20):
                vistos[s["id"]] = s
    return list(vistos.values())


def _tirar_dos_rascunhos(service_id: int) -> list[int]:
    """Remove a parada de todo rascunho ainda não enviado, de hoje em diante."""
    import rascunhos_rota
    conn = sqlite3.connect(rascunhos_rota.DB_PATH)
    try:
        ids = [r for (r,) in conn.execute("""
            SELECT DISTINCT r.id FROM rascunhos_rota r JOIN rascunhos_parada p ON p.rascunho_id = r.id
            WHERE p.service_id = ? AND r.data_alvo >= ? AND r.status IN (?, ?)""",
            (service_id, date.today().isoformat(), rascunhos_rota.STATUS_RASCUNHO, rascunhos_rota.STATUS_ERRO_ENVIO))]
    finally:
        conn.close()
    for rid in ids:
        rascunhos_rota.remover_parada(rid, service_id)
    return ids


def cancelar_ja_entregue(vuupt, s: dict) -> list[int] | None:
    """Tira do pool o pedido ja entregue: cancela o servico na Vuupt e tira dos rascunhos nao
    enviados. Pedido sem servico na Vuupt (id negativo, fonte stokki) fecha LOCAL no nucleo
    (CANCELADO): nunca chama a Vuupt com id sintetico e, fora do pool e dos rascunhos, nunca
    e materializado no envio. Devolve os rascunhos mexidos, ou None se o cancelamento falhou."""
    from nucleo import banco, pool
    if pool.eh_sem_servico(s["id"]):
        from nucleo.pedidos import cancelar_sem_servico
        if not cancelar_sem_servico(s.get("code"), "ja entregue (conferir_pool)", banco.ORIGEM_PIPELINE):
            logger.warning(f"{s.get('code')}: pedido sem servico ja nao estava ABERTO no nucleo")
    else:
        from vuupt_client import VuuptAPIError
        try:
            vuupt.cancelar_servico_oficial(s["id"])
        except VuuptAPIError as e:
            logger.warning(f"{s.get('code')}: cancelamento na Vuupt falhou ({e})")
            return None
    return _tirar_dos_rascunhos(s["id"])


# ── Rotina ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tira do pool pedido que já foi entregue.")
    parser.add_argument("--modo-teste", action="store_true", help="lê tudo, não cancela nem avisa")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(_RAIZ / "dados" / "conferir_pool.log", encoding="utf-8")],
    )

    import yaml
    sys.path.insert(0, str(_RAIZ / "painel_agentes"))
    import pedidos_parados_triagem as triagem
    import notificar_whatsapp
    from nucleo.pool import listar_pool_not_assigned
    from nucleo.sincronizar_servicos_vuupt import ressincronizar_ids
    from stokki import sessao_uso
    from stokki.auth import StokkiSession
    from vuupt_client import VuuptClient

    config = yaml.safe_load((_RAIZ / "config.yaml").read_text(encoding="utf-8"))
    vuupt = VuuptClient(config.get("vuupt_api", {}).get("token", ""))
    pool = listar_pool_not_assigned(config, vuupt)
    logger.info(f"Pool: {len(pool)} serviço(s) not_assigned.")

    ocupante = sessao_uso.aguardar_vez_para_login(ESPERA_TRAVA_STOKKI_S)
    if ocupante:
        logger.warning(f"Stokki em uso por '{ocupante}' -- conferência do pool não feita.")
        return 0
    tem_trava = sessao_uso.adquirir(DONO_TRAVA_STOKKI, ttl_segundos=TTL_TRAVA_STOKKI_S)
    situacao: dict[str, dict | None] = {}
    try:
        sessao = StokkiSession(config)
        for n, base in enumerate(sorted({b for s in pool if (b := codigo_base(s.get("code")))}), 1):
            try:
                situacao[base] = triagem._status_e_transportadora_stokki(sessao, base.split("-")[1])
            except Exception as e:  # pedido que falha não para os outros
                logger.warning(f"{base}: Stokki não conferida ({e})")
            if tem_trava and n % 50 == 0:
                sessao_uso.renovar(DONO_TRAVA_STOKKI, TTL_TRAVA_STOKKI_S)
    finally:
        if tem_trava:
            sessao_uso.liberar(DONO_TRAVA_STOKKI)

    lalamove = _bases_lalamove_entregues(_RAIZ / "dados" / "dados.db")
    cancelados, avisos = [], []
    for s in pool:
        base = codigo_base(s.get("code"))
        visto = situacao.get(base)
        if not visto:
            continue
        status = visto["status"]
        vuupt_entregue = False
        if re.search(r"enviado", status, re.IGNORECASE):
            vuupt_entregue = entregue_em_outro_servico(_irmaos_vuupt(vuupt, base), s["id"])
        acao, motivo = decidir(status, vuupt_entregue, base in lalamove)
        item = {"codigo": s.get("code"), "service_id": s["id"], "stokki": status}
        if acao == AVISAR:
            avisos.append(item)
            logger.info(f"AVISO {item['codigo']} (serviço {s['id']}): {motivo}")
        elif acao == CANCELAR:
            if args.modo_teste:
                logger.info(f"[MODO TESTE] cancelaria {item['codigo']} (serviço {s['id']}): já entregue")
                cancelados.append(item)
                continue
            rascunhos = cancelar_ja_entregue(vuupt, s)
            if rascunhos is None:
                continue
            cancelados.append(item)
            logger.info(f"CANCELADO {item['codigo']} (serviço {s['id']}): já entregue; "
                        f"tirado dos rascunhos {rascunhos or '-'}")
    if cancelados and not args.modo_teste:
        try:
            ressincronizar_ids(vuupt, [c["service_id"] for c in cancelados])
        except Exception as e:
            logger.warning(f"Ressincronização do núcleo falhou ({e}); o timer de 15 min alcança.")

    duplicados = duplicados_no_pool(pool)
    for base, codigos in duplicados.items():
        logger.info(f"DUPLICADO {base}: {', '.join(codigos)}")

    nao_conferidos = sum(1 for s in pool if codigo_base(s.get("code")) and not situacao.get(codigo_base(s.get("code"))))
    logger.info(f"Concluído: {len(cancelados)} cancelado(s), {len(avisos)} aviso(s), "
                f"{len(duplicados)} duplicado(s), {nao_conferidos} sem conferência na Stokki.")
    if cancelados or avisos or duplicados:
        notificar_whatsapp.despachar(config, "conferir_pool", "conferir_pool",
                                     texto_whatsapp(cancelados, avisos, duplicados),
                                     modo_teste=args.modo_teste)
    return 0


if __name__ == "__main__":
    sys.exit(main())
