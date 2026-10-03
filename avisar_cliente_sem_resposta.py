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
import notificar_whatsapp as nw  # noqa: E402
import preferencias_notificacao as pn  # noqa: E402

logger = logging.getLogger("avisar_cliente_sem_resposta")

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


def executar(config: dict, conn=None, agora: datetime | None = None, modo_teste: bool = False, **kw) -> dict:
    """Uma rodada. `agora` e `kw` (ex.: dormir) so nos testes: em producao o
    despacho usa o relogio real (o intervalo minimo entre envios depende disso)."""
    momento = agora or datetime.now()
    resumo = {"fora_horario": False, "avaliados": 0, "enviados": 0, "sem_whatsapp": 0, "pulados": 0,
              "situacoes": []}
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
        for c in candidatos:
            if saldo <= 0:
                resumo["pulados"] += 1
                logger.info(f"Chamado #{c['chamado_id']}: teto diario dos clientes atingido, fica pra depois.")
                continue
            link = f"{ch.url_base(config)}/?chamado={c['chamado_id']}"
            situacao = nw.avisar_cliente_sem_resposta(c["chamado_id"], c["msg_id"], c["telefone"], link, config,
                                                      modo_teste=modo_teste, conn=conn, agora=agora, **kw)
            resumo["situacoes"].append((c["chamado_id"], situacao))
            if situacao == "enviado":
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
    if not nw.clientes_ligado(config) and not args.modo_teste:
        logger.info("whatsapp_notificacoes.clientes.ativo desligado: nada a fazer.")
        return 0
    resumo = executar(config, modo_teste=args.modo_teste)
    logger.info(f"Resumo: {resumo}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
