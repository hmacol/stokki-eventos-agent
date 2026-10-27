# -*- coding: utf-8 -*-
"""
avisar_cliente_sem_resposta.py

A equipe respondeu um chamado do portal e o embarcador ficou 10 minutos sem
responder: ele recebe um WhatsApp curto, no numero que ele mesmo cadastrou no
botao Notificacoes do portal, com o link do chamado (pedido do Hugo,
29/09/2026; spec em docs/superpowers/specs/2026-09-30-whatsapp-cliente-sem-resposta-design.md).

Roda por timer a cada 2 minutos (infra/stokki-avisar-cliente-sem-resposta.timer).

Regras:
- chamado de embarcador (tipo CLIENTE), nao resolvido, cuja ULTIMA mensagem
  da equipe (portal ou e-mail) tem entre `minutos` (10) e `dias_max` (3 dias)
  e nao foi seguida de mensagem do cliente; `sistema` e `assistente` nao
  contam como resposta;
- so dentro do horario de atendimento dos chamados (seg-sex 08:30-17:00,
  almoco 13-14). Fora dele a rodada nao grava nada: o mesmo chamado e
  reavaliado na primeira rodada dentro do horario (resposta as 16:55 vira
  aviso as 08:30 do proximo dia util);
- UMA tentativa por mensagem da equipe (assinatura msg:<id> em
  notificacoes_whatsapp, em qualquer situacao). Sem reenvio;
- teto diario proprio (whatsapp_notificacoes.clientes.teto_diario): quando
  acaba, a rodada para sem gravar nada, e o resto fica pra amanha;
- numero consultado no gateway antes de enviar (nono digito): sem WhatsApp
  vira `nao_enviado`; gateway sem resposta nao grava e tenta depois.

Desligado por padrao (whatsapp_notificacoes.clientes.ativo). Com
clientes.forcar_destino preenchido tudo vai pro numero do Hugo (piloto).

Tela de Notificacoes (controle_notificacoes.py, tipo "whatsapp_sem_resposta"):
vale por cima do config (que fica de fallback). NAO pela tela -> a mensagem e
consumida (msg:<id> gravada como "bloqueado"; ligar depois nao manda aviso
velho). PILOTO manda a copia so ao numero do piloto, uma por mensagem, gravada
com origem propria (nao conta como recebido pelo cliente: a mensagem segue
elegivel pro envio real). NAO por causa do config (sem linha na tela) so pula,
como sempre.

Uso:
    python avisar_cliente_sem_resposta.py [--modo-teste]
"""
import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

_RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "portal_cliente"))

import chamados as ch  # noqa: E402  (portal_cliente/chamados.py)
import controle_notificacoes  # noqa: E402
import notificar_whatsapp as nw  # noqa: E402
import preferencias_notificacao as pn  # noqa: E402
from controle_notificacoes import acao_do_config, decidir  # noqa: E402

logger = logging.getLogger("avisar_cliente_sem_resposta")

TIPO_TELA = "whatsapp_sem_resposta"   # chave na tela de Notificacoes
MINUTOS_PADRAO = 10
DIAS_MAX_PADRAO = 3

# A ultima mensagem da equipe de cada chamado aberto de embarcador, sem
# resposta do cliente depois dela e sem tentativa registrada. Datas no
# formato de chamados._agora() ("%Y-%m-%d %H:%M:%S").
_SQL = f"""
SELECT c.id AS chamado_id, c.cnpj_embarcador, m.id AS msg_id, m.criado_em
FROM portal_chamados c
JOIN portal_chamados_mensagens m ON m.id = (
    SELECT MAX(id) FROM portal_chamados_mensagens
    WHERE chamado_id = c.id AND origem = '{ch.ORIGEM_EQUIPE}')
WHERE c.tipo = '{ch.TIPO_CLIENTE}' AND c.status != '{ch.STATUS_RESOLVIDO}'
  AND m.criado_em <= :limite_min AND m.criado_em >= :limite_max
  AND NOT EXISTS (SELECT 1 FROM portal_chamados_mensagens r
                  WHERE r.chamado_id = c.id AND r.id > m.id AND r.origem = '{ch.ORIGEM_CLIENTE}')
  AND NOT EXISTS (SELECT 1 FROM notificacoes_whatsapp n
                  WHERE n.origem = '{nw.ORIGEM_CLIENTE}' AND n.assinatura = 'msg:' || m.id)
ORDER BY m.criado_em, m.id
"""


def _fmt(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _inteiro(cfg: dict, chave: str, padrao: int) -> int:
    try:
        return int(cfg.get(chave, padrao))
    except (TypeError, ValueError):
        return padrao


def selecionar(conn, config: dict, agora: datetime) -> list[dict]:
    """Chamados em silencio com telefone cadastrado e chave ligada, do mais
    antigo pro mais novo: [{chamado_id, cnpj, msg_id, telefone}]."""
    cfg = nw._cfg_clientes(config)
    minutos = _inteiro(cfg, "minutos", MINUTOS_PADRAO)
    dias = _inteiro(cfg, "dias_max", DIAS_MAX_PADRAO)
    conn.execute(nw._SCHEMA)
    rows = conn.execute(_SQL, {"limite_min": _fmt(agora - timedelta(minutes=minutos)),
                               "limite_max": _fmt(agora - timedelta(days=dias))}).fetchall()
    selecionados = []
    for r in rows:
        telefone = pn.whatsapp_do_embarcador(conn, r["cnpj_embarcador"])
        if not telefone:
            continue
        selecionados.append({"chamado_id": r["chamado_id"], "cnpj": r["cnpj_embarcador"],
                             "msg_id": r["msg_id"], "telefone": telefone})
    return selecionados


def _tela_tem_linha(db_path=None) -> bool:
    """A tela de Notificacoes manda neste tipo (linha propria ou chave geral
    desligada)? Erro de banco = nao: vale o config."""
    try:
        conn = controle_notificacoes.conectar(db_path)
        try:
            return (controle_notificacoes.estado_geral(conn) == "DESLIGADO"
                    or controle_notificacoes.estado_tipo(conn, TIPO_TELA) is not None)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"nao leu a tela de Notificacoes ({e}); vale o config")
        return False


def executar(config: dict, conn=None, agora: datetime | None = None, modo_teste: bool = False,
             db_path=None, **kw) -> dict:
    """Uma rodada. `agora` e `kw` (ex.: dormir) so nos testes: em producao o
    despacho usa o relogio real (o intervalo minimo entre envios depende disso).
    `db_path`: banco do controle da tela de Notificacoes (so nos testes)."""
    momento = agora or datetime.now()
    resumo = {"fora_horario": False, "avaliados": 0, "enviados": 0, "sem_whatsapp": 0, "pulados": 0,
              "bloqueados": 0, "copias_piloto": 0, "situacoes": []}
    # o que o config decidiria hoje (a tela vale por cima)
    fallback = acao_do_config(nw.clientes_ligado(config), nw._cfg_clientes(config).get("forcar_destino") or "")
    if not ch.situacao_horario(config, momento, ch.PERFIL_CLIENTE)["dentro"]:
        resumo["fora_horario"] = True
        logger.info("Fora do horario de atendimento: nada a fazer.")
        return resumo
    fechar = conn is None
    if fechar:
        conn = ch.conectar()
    try:
        candidatos = selecionar(conn, config, momento)
        resumo["avaliados"] = len(candidatos)
        saldo = nw.saldo_clientes(conn, config, momento)
        saldo_piloto = nw.saldo_piloto(conn, config, momento)
        for c in candidatos:
            destino_forcado = None   # vale o whatsapp_notificacoes.clientes.forcar_destino do config
            piloto = False
            if not modo_teste:
                d = decidir(TIPO_TELA, c["cnpj"], fallback_acao=fallback, canal="whatsapp", db_path=db_path)
                if d.acao == "NAO" and d.origem == "tela" and not d.consome:
                    # piloto sem WhatsApp de destino: nada sai e a mensagem fica pendente
                    # (sai pro cliente quando a tela ligar)
                    resumo["pulados"] += 1
                    logger.info(f"Chamado #{c['chamado_id']} (msg {c['msg_id']}): nao enviado pela tela "
                                f"de Notificacoes ({d.motivo}); fica pendente.")
                    continue
                if d.acao == "NAO" and d.origem == "tela":
                    nw.registrar_bloqueado_sem_resposta(conn, momento, c["msg_id"],
                                                        f"tela de Notificacoes: {d.motivo}")
                    resumo["bloqueados"] += 1
                    logger.info(f"Chamado #{c['chamado_id']} (msg {c['msg_id']}): barrado pela tela "
                                f"de Notificacoes ({d.motivo}); mensagem consumida.")
                    continue
                # NAO do config (sem linha na tela) segue: nw devolve "desligado", como sempre
                if d.origem == "tela":
                    piloto = d.acao == "PILOTO"
                    destino_forcado = d.destino_piloto if piloto else ""
                    if piloto and nw.copia_piloto_sem_resposta_enviada(conn, c["msg_id"]):
                        continue   # a copia desta mensagem ja saiu
            if (saldo_piloto if piloto else saldo) <= 0:
                resumo["pulados"] += 1
                logger.info(f"Chamado #{c['chamado_id']}: teto diario "
                            f"{'das copias de piloto' if piloto else 'dos clientes'} atingido, fica pra depois.")
                continue
            link = f"{ch.url_base(config)}/?chamado={c['chamado_id']}"
            situacao = nw.avisar_cliente_sem_resposta(c["chamado_id"], c["msg_id"], c["telefone"], link, config,
                                                      modo_teste=modo_teste, conn=conn, agora=agora,
                                                      destino_forcado=destino_forcado, **kw)
            resumo["situacoes"].append((c["chamado_id"], situacao))
            if situacao == "enviado" and piloto:
                resumo["copias_piloto"] += 1   # nao conta como aviso ao cliente nem no teto dele
                saldo_piloto -= 1
            elif situacao == "enviado":
                resumo["enviados"] += 1
                saldo -= 1
            elif situacao == "numero_sem_whatsapp":
                resumo["sem_whatsapp"] += 1
            elif situacao != "modo_teste":
                resumo["pulados"] += 1
            logger.info(f"Chamado #{c['chamado_id']} (msg {c['msg_id']}): {situacao}.")
    finally:
        if fechar:
            conn.close()
    return resumo


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="WhatsApp pro embarcador que nao respondeu a equipe.")
    parser.add_argument("--modo-teste", action="store_true", help="so mostra o que enviaria")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    if not args.modo_teste:
        if not nw.gateway_clientes_ok(config):
            logger.info("WhatsApp desligado ou gateway OpenWA desconfigurado: nada a fazer.")
            return 0
        if not nw.clientes_ligado(config) and not _tela_tem_linha():
            logger.info("whatsapp_notificacoes.clientes.ativo desligado e sem linha na tela de "
                        "Notificacoes: nada a fazer.")
            return 0
    resumo = executar(config, modo_teste=args.modo_teste)
    logger.info(f"Resumo: {resumo}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
