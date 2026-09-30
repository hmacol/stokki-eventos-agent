# -*- coding: utf-8 -*-
"""
roteirizacao/sincronizar_respostas_confirmacao.py

Puxa da VPS pública (confirmacao_motoristas/app.py) as respostas que os
motoristas já deram (confirmado/recusado) e aplica em
regras/confirmacao_rotas.py (local) -- pedido do Hugo, 16/08. Roda
solto, sem depender do horário de geração dos avisos
(avisar_motoristas_rotas.py --gerar-confirmacoes), pra refletir a
resposta do motorista em /planejamento assim que possível.

COMO USAR (agendado a cada 30 min no Agendador de Tarefas do Windows,
mesmo padrão dos demais agentes -- usar caminho completo do py.exe):
    py -3.11 roteirizacao/sincronizar_respostas_confirmacao.py
"""
import logging
import sys
from pathlib import Path

_RAIZ_LOCAL   = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_LOCAL))
sys.path.insert(0, str(_RAIZ_PROJETO / "painel_agentes"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ_LOCAL / "dados" / "sincronizar_respostas_confirmacao.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("sincronizar_respostas_confirmacao")

import requests
import yaml

from regras import confirmacao_rotas, ofertas_rota
from rascunhos_rota import aplicar_escolha_motorista, trocar_motorista, buscar_rascunho


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _sincronizar_confirmacoes(url_base: str, sync_secret: str):
    try:
        resp = requests.get(
            f"{url_base}/api/sync/respostas",
            headers={"X-Sync-Secret": sync_secret},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(f"Falha ao consultar respostas na VPS: {exc}")
        return

    respostas = resp.json().get("respostas", [])
    aplicadas = nao_encontradas = 0
    for resposta in respostas:
        ok = confirmacao_rotas.aplicar_resposta_remota(
            token=resposta["token"], status=resposta["status"],
            motivo_recusa=resposta.get("motivo_recusa"), respondido_em=resposta["respondido_em"],
        )
        if ok:
            aplicadas += 1
        else:
            nao_encontradas += 1

    logger.info(
        f"{len(respostas)} resposta(s) de confirmação recebida(s) da VPS -- {aplicadas} aplicada(s) localmente"
        + (f", {nao_encontradas} sem correspondência local (dessincronia)" if nao_encontradas else "") + "."
    )


def _sincronizar_ofertas_escolhidas(url_base: str, sync_secret: str):
    """Marketplace de rotas (Hugo, 22/08): puxa da VPS as ofertas já
    ESCOLHIDA e aplica em rascunhos_rota (motorista gravado, rascunho
    volta pra RASCUNHO pronto pro 'Confirmar e Enviar' normal). Full
    pull a cada rodada, mesmo padrão de _sincronizar_confirmacoes --
    ofertas_rota.marcar_aplicada garante que reaplicar a mesma escolha
    de novo (rodada seguinte, antes do rascunho sair de OFERTADA por
    algum outro motivo) não tem efeito colateral."""
    try:
        resp = requests.get(
            f"{url_base}/api/sync/ofertas/escolhidas",
            headers={"X-Sync-Secret": sync_secret},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(f"Falha ao consultar ofertas escolhidas na VPS: {exc}")
        return

    escolhidas = resp.json().get("ofertas", [])
    for oferta in escolhidas:
        ofertas_rota.aplicar_resposta_remota(
            rascunho_id=oferta["rascunho_id"], agent_id=oferta["escolhido_por"],
            escolhido_em=oferta["escolhido_em"],
        )

    pendentes = ofertas_rota.listar_escolhidas_nao_aplicadas()
    if pendentes:
        # A VPS só sabe o agent_id de quem escolheu -- nome/vehicle_id
        # pra gravar no rascunho vêm do MESMO catálogo local que
        # planejamento_rotas.publicar_oferta_rascunho já usa.
        from regras.preferencias_motoristas import CatalogoMotoristas
        cfg_motoristas = _carregar_config().get("motoristas", {})
        catalogo = CatalogoMotoristas.carregar(cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""))
        motoristas_por_id = {m.agent_id: m for m in catalogo.motoristas}

    for pendente in pendentes:
        motorista = motoristas_por_id.get(pendente["escolhido_por"])
        aplicar_escolha_motorista(
            pendente["rascunho_id"], pendente["escolhido_por"],
            vehicle_id=motorista.vehicle_id if motorista else None,
            motorista_nome=motorista.nome if motorista else f"Motorista {pendente['escolhido_por']}",
        )
        ofertas_rota.marcar_aplicada(pendente["rascunho_id"])

    logger.info(
        f"{len(escolhidas)} oferta(s) escolhida(s) recebida(s) da VPS -- {len(pendentes)} aplicada(s) localmente."
    )


def _reconciliar_escolhas_revertidas(url_base: str, sync_secret: str):
    """Marketplace de rotas -- achado real 23/08 testando com o Hugo:
    motorista escolhe, o pull acima já aplica (rascunho ganha o agent_id
    dele), e DEPOIS o motorista desiste pela própria tela pública
    (/escolher, botão "Cancelar escolha") -- a VPS volta a oferta pra
    ABERTA, mas nada nunca reconsulta essa oferta específica de novo
    (_sincronizar_ofertas_escolhidas só pergunta "quem ESTÁ escolhida
    agora", nunca "essa que eu já apliquei ainda está?"). Resultado: o
    rascunho local continua mostrando o motorista que desistiu, E a
    rota reaparece disponível pra outro motorista escolher na VPS --
    dois motoristas podiam achar que a rota é deles, sem o painel saber
    de nenhum dos dois.

    Consulta pontualmente (GET /api/sync/ofertas/status) só as ofertas
    que o lado local marcou como aplicadas -- conjunto pequeno, não o
    pull completo de sempre."""
    aplicadas = ofertas_rota.listar_aplicadas()
    if not aplicadas:
        return

    ids = [str(a["rascunho_id"]) for a in aplicadas]
    try:
        resp = requests.get(
            f"{url_base}/api/sync/ofertas/status",
            params={"ids": ",".join(ids)},
            headers={"X-Sync-Secret": sync_secret},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(f"Falha ao consultar status atual das ofertas aplicadas na VPS: {exc}")
        return

    status_atual = {linha["rascunho_id"]: linha for linha in resp.json().get("ofertas", [])}
    revertidas = 0
    for aplicada in aplicadas:
        rascunho_id = aplicada["rascunho_id"]
        atual = status_atual.get(rascunho_id)
        # status != 'ESCOLHIDA' (motorista cancelou, ou o rascunho nem
        # existe mais lá) conta como revertida; se ainda ESCOLHIDA mas
        # por um agent_id DIFERENTE do que aplicamos, também -- alguém
        # cancelou e outro motorista já pegou no lugar dele.
        if atual and atual["status"] == "ESCOLHIDA" and atual["escolhido_por"] == aplicada["escolhido_por"]:
            continue

        rascunho = buscar_rascunho(rascunho_id)
        if rascunho and rascunho.get("agent_id") == aplicada["escolhido_por"]:
            trocar_motorista(rascunho_id, None, None, None)
            logger.warning(
                f"Rascunho {rascunho_id} ({rascunho['nome']}): motorista {aplicada['escolhido_por']} "
                f"desistiu da escolha depois de já aplicada -- desalocado, precisa de nova decisão."
            )
        ofertas_rota.marcar_revertida(rascunho_id)
        revertidas += 1

    if revertidas:
        logger.info(f"{revertidas} escolha(s) revertida(s) pelo motorista reconciliada(s).")
        # empurra o CANCELADA de volta pra VPS -- sem isso a oferta
        # ficaria "disponível" na página pública indefinidamente, mesmo
        # já desalocada aqui (mesmo achado do Hugo: "continua aparecendo
        # pro usuário de teste"). Guarda de sempre (status != 'ESCOLHIDA'
        # do lado da VPS) protege sozinha o caso raro de outro motorista
        # ter escolhido bem nesse meio-tempo -- o push simplesmente não
        # tem efeito nesse caso, sem sobrescrever a escolha dele.
        from avisar_motoristas_rotas import push_ofertas_vps
        push_ofertas_vps({"url_base": url_base, "sync_secret": sync_secret})


def main():
    config = _carregar_config().get("confirmacao_rotas", {})
    url_base = (config.get("url_base") or "").rstrip("/")
    sync_secret = config.get("sync_secret")
    if not url_base or not sync_secret:
        logger.warning("confirmacao_rotas.url_base/sync_secret não configurados em config.yaml -- nada a sincronizar.")
        return

    _sincronizar_confirmacoes(url_base, sync_secret)
    _sincronizar_ofertas_escolhidas(url_base, sync_secret)
    _reconciliar_escolhas_revertidas(url_base, sync_secret)
    _avisar_ondas_liberadas()


def _avisar_ondas_liberadas():
    """Ondas de prioridade do marketplace (Hugo, 03/09, ver
    regras/prioridade_ofertas.py): avisa os motoristas cuja onda abriu
    desde a última rodada. A VPS já mostra a oferta pra eles sozinha
    (compara visivel_a_partir_de na hora da consulta); este passo só
    cuida do AVISO (WhatsApp/e-mail/texto). Roda por último: se uma
    oferta acabou de ser escolhida/revertida acima, a lista de ABERTA
    já está atualizada."""
    try:
        from avisar_motoristas_rotas import avisar_ondas_liberadas
        resultado = avisar_ondas_liberadas(_carregar_config())
    except Exception as exc:  # aviso nunca pode derrubar a sincronização em si
        logger.warning(f"Falha ao avisar ondas liberadas do marketplace: {exc}")
        return
    if resultado:
        logger.info(f"Ondas do marketplace avisadas: {resultado}")


if __name__ == "__main__":
    main()
