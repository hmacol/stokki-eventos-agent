"""
Acompanha as retiradas no galpão (serviços avulsos "[RETIRADA] ..."
atribuídos ao agente fixo) contra a Stokki:

  - Stokki "Enviado"   -> fecha o serviço na VUUPT como entregue com
                          sucesso (accept/start/check-in/check-out por API)
  - Stokki "Cancelado" -> cancela o serviço na VUUPT (PUT /cancel)
  - qualquer outro     -> segue aguardando

Uso:
  python retiradas/acompanhar_retiradas.py [--modo-teste]

Agendamento: timer próprio a cada 30 min em horário comercial
(infra/stokki-acompanhar-retiradas.timer) + etapa 4 do executar_tudo.py
(18h/22h). Trava de sessão Stokki: NÃO roda enquanto houver agente do
painel RODANDO (mesma regra da Torre/pedidos parados -- um login
concorrente derruba a sessão do agente em execução).
"""
import argparse
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path

import yaml

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from stokki import pedidos as stokki_pedidos  # noqa: E402
from stokki.auth import StokkiSession  # noqa: E402
from vuupt_client import VuuptClient, VuuptAPIError  # noqa: E402
from retiradas.regras_retirada import (  # noqa: E402
    STATUSES_ABERTOS, config_retiradas, eh_servico_retirada, id_stokki_do_code,
)

logger = logging.getLogger("acompanhar_retiradas")

DB_PATH = _RAIZ / "dados" / "dados.db"
PAUSA_ENTRE_CONSULTAS_SEG = 0.3

# Mesmo regex de painel_agentes/pedidos_parados_triagem.py: o status é o
# PRIMEIRO badge-status depois de "Situação:" (os seguintes são marcadores
# tipo "Remessa Expressa").
_RE_SITUACAO = re.compile(
    r"Situa[çc][ãa]o:\s*</th>\s*<td>\s*<span[^>]*badge-status[^>]*>(.*?)</span>", re.S | re.IGNORECASE,
)


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _painel_tem_execucao_rodando() -> bool:
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT 1 FROM painel_execucoes WHERE status='RODANDO' LIMIT 1").fetchone()
        conn.close()
        return bool(row)
    except Exception:
        return False


def status_stokki(sessao: StokkiSession, id_stokki: int) -> str | None:
    """Texto do badge de situação do pedido ("Enviado", "Cancelado",
    "Aguardando Transportador"...) ou None se o pedido não existe (404/500)."""
    resp = sessao.get(f"{stokki_pedidos.BASE_URL}/pt-br/administrator/inventory/outbound/show/{id_stokki}")
    if resp.status_code in (404, 500):
        return None
    resp.raise_for_status()
    m = _RE_SITUACAO.search(resp.text)
    if not m:
        return None
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()


def decidir(status: str | None) -> str:
    """'concluir' | 'cancelar' | 'aguardar' | 'desconhecido'."""
    if status is None:
        return "desconhecido"
    s = status.lower()
    if "cancel" in s:
        return "cancelar"
    if "enviad" in s or s == "sent":
        return "concluir"
    return "aguardar"


def acompanhar(config: dict, modo_teste: bool = False, ignorar_trava_painel: bool = False) -> dict:
    cfg = config_retiradas(config)
    resumo = {"concluidos": [], "cancelados": [], "aguardando": [], "desconhecidos": [], "erros": []}
    if not cfg["ativo"]:
        logger.info("Retiradas desativadas (config retiradas.agent_id/customer_id vazios) -- nada a fazer.")
        return resumo
    if not ignorar_trava_painel and _painel_tem_execucao_rodando():
        raise RuntimeError("Há agente do painel RODANDO -- consulta à Stokki adiada pra não derrubar a sessão dele.")

    token = config.get("vuupt_api", {}).get("token", "")
    vuupt = VuuptClient(token)
    servicos = [s for s in vuupt.listar_servicos_do_agente(cfg["agent_id"], STATUSES_ABERTOS)
                if eh_servico_retirada(s)]
    logger.info(f"{len(servicos)} retirada(s) em aberto na VUUPT (agente {cfg['agent_id']}).")
    if not servicos:
        return resumo

    sessao = StokkiSession(config)
    for s in servicos:
        code = s.get("code", "")
        id_stokki = id_stokki_do_code(code)
        if not id_stokki:
            resumo["desconhecidos"].append(f"{code} (sem id)")
            continue
        try:
            status = status_stokki(sessao, id_stokki)
            acao = decidir(status)
            rotulo = f"{code} (serviço {s['id']}, Stokki='{status}')"
            if acao == "concluir":
                if modo_teste:
                    logger.info(f"[TESTE] {rotulo}: fecharia como entregue com sucesso.")
                else:
                    vuupt.concluir_como_agente(s["id"], sucesso=True, status_atual=s.get("status", ""))
                    logger.info(f"{rotulo}: fechado como entregue com sucesso.")
                resumo["concluidos"].append(code)
            elif acao == "cancelar":
                if modo_teste:
                    logger.info(f"[TESTE] {rotulo}: cancelaria na VUUPT.")
                else:
                    vuupt.cancelar_servico_oficial(s["id"])
                    logger.info(f"{rotulo}: cancelado na VUUPT.")
                resumo["cancelados"].append(code)
            elif acao == "aguardar":
                resumo["aguardando"].append(f"{code} ({status})")
            else:
                logger.warning(f"{rotulo}: pedido não encontrado na Stokki -- mantido em aberto.")
                resumo["desconhecidos"].append(code)
        except (VuuptAPIError, Exception) as e:
            logger.error(f"{code}: erro ao acompanhar -- {e}")
            resumo["erros"].append(f"{code}: {e}")
        time.sleep(PAUSA_ENTRE_CONSULTAS_SEG)
    return resumo


def main(modo_teste: bool = False, ignorar_trava_painel: bool = False) -> dict:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(_RAIZ / "dados" / "acompanhar_retiradas.log", encoding="utf-8")],
    )
    inicio = time.time()
    config = _carregar_config()
    prefixo = "[MODO TESTE] " if modo_teste else ""
    logger.info(f"{prefixo}Acompanhamento de retiradas iniciado.")
    resumo_etapas = {}
    try:
        resumo = acompanhar(config, modo_teste, ignorar_trava_painel)
        detalhe = (f"Concluídos: {len(resumo['concluidos'])} {resumo['concluidos'] or ''} | "
                   f"Cancelados: {len(resumo['cancelados'])} {resumo['cancelados'] or ''} | "
                   f"Aguardando: {len(resumo['aguardando'])} | "
                   f"Não encontrados: {len(resumo['desconhecidos'])} | Erros: {len(resumo['erros'])}")
        resumo_etapas["Acompanhar retiradas"] = {"status": "erro" if resumo["erros"] else "ok", "detalhe": detalhe}
        logger.info(detalhe)
    except RuntimeError as e:
        logger.warning(str(e))
        resumo_etapas["Acompanhar retiradas"] = {"status": "ok", "detalhe": f"Adiado: {e}"}
        resumo = {}
    except Exception as e:
        logger.exception(f"Erro no acompanhamento de retiradas: {e}")
        resumo_etapas["Acompanhar retiradas"] = {"status": "erro", "detalhe": str(e)}
        resumo = {}
    duracao = time.time() - inicio
    logger.info(f"{prefixo}Acompanhamento finalizado em {duracao:.1f}s.")
    # E-mail de execução só quando algo aconteceu (fechou/cancelou/erro):
    # o job roda a cada 30 min e a maioria das rodadas é "nada a fazer".
    houve_acao = bool(resumo.get("concluidos") or resumo.get("cancelados") or resumo.get("erros")) \
        or any(v.get("status") == "erro" for v in resumo_etapas.values())
    if __name__ == "__main__" and houve_acao:
        try:
            from notificar_execucao_agente import notificar_execucao
            notificar_execucao(resumo_etapas, duracao, modo_teste, config)
        except Exception as e:
            logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")
    return resumo


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fecha/cancela retiradas na VUUPT conforme a Stokki")
    parser.add_argument("--modo-teste", action="store_true", help="Só mostra o que faria")
    parser.add_argument("--ignorar-trava-painel", action="store_true",
                        help="Roda mesmo com agente do painel RODANDO (só quando chamado de dentro de uma sequência)")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste, ignorar_trava_painel=args.ignorar_trava_painel)
