# -*- coding: utf-8 -*-
"""
lalamove_integracao.py

Cola entre o /planejamento (rascunhos de rota) e a Lalamove. Pedido do
Hugo, 28/08: quando a rota é confirmada com o motorista virtual
"LALAMOVE" (config lalamove.agent_id_vuupt), além da rota na VUUPT:

  1. cota e cria o pedido na Lalamove (base = coleta, paradas = entregas,
     na ordem do rascunho) -- sempre IMEDIATO, sem o scheduleAt da
     Lalamove (Hugo, 03/09): a corrida sai na hora do clique em "Lançar
     na Lalamove" ou no horário escolhido no card (lalamove_lancar_em),
     disparado pelo timer nucleo/lancar_lalamove_programados.py
     (lancar_programados);
  2. grava orderId/status/preço/link no rascunho (colunas lalamove_*);
  3. carimba o código Lalamove no título de cada serviço na VUUPT
     ("[LALAMOVE 1234567890] <título original>");
  4. sincronizar_pedidos() (timer nucleo/sincronizar_lalamove.py) puxa o
     status da Lalamove e, quando uma parada tem comprovante (POD) ou o
     pedido fecha COMPLETED, conclui o serviço na VUUPT como se fosse o
     agente (VuuptClient.concluir_como_agente) -- assim a Torre, a
     expedição e o histórico continuam funcionando sem nada manual.

Tudo best-effort em relação à VUUPT: falha na Lalamove nunca desfaz a
rota já criada; fica em lalamove_erro e aparece no card.
"""
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "painel_agentes"))

from lalamove_client import (LalamoveAPIError, LalamoveClient, STATUS_FINAIS, resumir_pedido,
                             stops_da_rota)
from vuupt_client import VuuptClient

logger = logging.getLogger(__name__)

ENDERECO_BASE = "Rua Zilda, 288, Casa Verde Alta, São Paulo"  # mesma base de rascunhos_rota.py
POD_ENTREGUE = {"DELIVERED", "SIGNED"}
STATUS_VUUPT_FECHADO = {"done", "canceled", "cancelled"}


# ── Config ─────────────────────────────────────────────────────────────

def _carregar_config() -> dict:
    import yaml
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def cfg_lalamove(config: dict | None = None) -> dict:
    return (config or _carregar_config()).get("lalamove", {}) or {}


def cliente_de_config(cfg: dict) -> LalamoveClient:
    return LalamoveClient(
        api_key=str(cfg.get("api_key") or ""),
        api_secret=str(cfg.get("api_secret") or ""),
        sandbox=bool(cfg.get("sandbox", True)),
        market=str(cfg.get("market") or "BR"),
    )


# ── Catálogo de opcionais (special requests) por veículo ───────────────
# GET /v3/cities estruturado: cada item tem parent_type (grupo, máx. 1
# por grupo) ou é avulso. Cacheado em dados/lalamove_catalogo.json por
# 24h -- a tela do planejamento lê daqui (ver _dados_lalamove).

_ARQ_CATALOGO = _RAIZ / "dados" / "lalamove_catalogo.json"
_TTL_CATALOGO_H = 24
_LOCODE_SP = "BR SAO"

_GRUPOS_PT = {
    "How much extra time is needed?": "Espera extra",
    "How long do you need help for?": "Ajuda no carregamento",
}
_NOMES_PT = {
    "RETURN": "Ida e volta",
    "THERMAL_BAG_1": "Bolsa térmica",
    "REFRIGERATED_VEHICLE": "Refrigerado (-1°C a -15°C)",
    "INSULATED_VEHICLE": "Isotérmico",
    "FROZEN_VEHICLE": "Congelado (abaixo de -15°C)",
    "LOADING_1DRIVER1HELPER": "Porta a porta (motorista + ajudante)",
}
_GRUPO_TEMPERATURA = {"REFRIGERATED_VEHICLE", "INSULATED_VEHICLE", "FROZEN_VEHICLE"}
_DURACOES_PT = {"30 min": "até 30 min", "1 hr": "até 1h", "1.5 hr": "até 1h30",
                "2 hr": "até 2h", "3 hr": "até 3h", "4 hr": "até 4h"}


def _traduzir_special_request(item: dict) -> dict:
    nome_api = str(item.get("name") or "")
    descricao = str(item.get("description") or "")
    grupo = _GRUPOS_PT.get(str(item.get("parent_type") or ""), "")
    if nome_api in _GRUPO_TEMPERATURA:
        grupo = "Tipo de baú"
    nome = _NOMES_PT.get(nome_api)
    if not nome:
        duracao = descricao.split("·")[-1].strip().lower()
        duracao_pt = next((pt for en, pt in _DURACOES_PT.items() if duracao.endswith(en.lower())), None)
        if duracao_pt:
            sufixo = ""
            if "1DRIVER1HELPER" in nome_api:
                sufixo = " (com ajudante)"
            elif nome_api.startswith("HOUSE_MOVING"):
                sufixo = " (mudança)"
            nome = duracao_pt + sufixo
        else:
            nome = descricao or nome_api
    return {"codigo": nome_api, "nome": nome, "grupo": grupo}


def catalogo_special_requests(config: dict, forcar: bool = False) -> dict:
    """{service_type: [{codigo, nome, grupo}]} da cidade de SP, com cache
    em disco (24h). Falha de rede sem cache -> {} (a tela só não mostra
    opcionais; o resto segue)."""
    import json as _json
    import time as _time
    try:
        if not forcar and _ARQ_CATALOGO.exists():
            bruto = _json.loads(_ARQ_CATALOGO.read_text(encoding="utf-8"))
            if _time.time() - float(bruto.get("_em", 0)) < _TTL_CATALOGO_H * 3600:
                return bruto.get("catalogo", {})
    except Exception:
        pass
    try:
        cli = cliente_de_config(cfg_lalamove(config))
        catalogo: dict[str, list] = {}
        for cidade in cli.info_cidades():
            if str(cidade.get("locode") or "") != _LOCODE_SP:
                continue
            for servico in cidade.get("services", []) or []:
                chave = str(servico.get("key") or "").upper()
                if chave:
                    catalogo[chave] = [_traduzir_special_request(i) for i in (servico.get("specialRequests") or [])]
        _ARQ_CATALOGO.parent.mkdir(parents=True, exist_ok=True)
        _ARQ_CATALOGO.write_text(_json.dumps({"_em": _time.time(), "catalogo": catalogo}, ensure_ascii=False),
                                 encoding="utf-8")
        return catalogo
    except Exception as e:
        logger.warning(f"Lalamove: catálogo de opcionais indisponível ({e}) -- usando cache velho se houver.")
        try:
            return _json.loads(_ARQ_CATALOGO.read_text(encoding="utf-8")).get("catalogo", {})
        except Exception:
            return {}


def special_requests_do_rascunho(rascunho: dict) -> list[str]:
    import json as _json
    bruto = rascunho.get("lalamove_special_requests") or "[]"
    try:
        lista = _json.loads(bruto) if isinstance(bruto, str) else list(bruto)
        return [str(s).upper() for s in lista if s]
    except Exception:
        return []


def resolver_veiculo(cfg: dict, codigo: str | None) -> dict:
    """codigo do seletor (lalamove_veiculo do rascunho) -> {codigo,
    service_type, special_requests}. Sem código ou código desconhecido:
    veiculo_padrao; sem lista de veículos: service_type simples do config."""
    veiculos = [v for v in (cfg.get("veiculos") or []) if v.get("codigo")]
    codigo = (codigo or "").strip().upper()
    padrao = str(cfg.get("veiculo_padrao") or "").upper()
    escolhido = next((v for v in veiculos if str(v["codigo"]).upper() == codigo), None)
    if escolhido is None and codigo:
        logger.warning(f"Lalamove: veículo '{codigo}' não está em lalamove.veiculos -- usando o padrão.")
    if escolhido is None:
        escolhido = next((v for v in veiculos if str(v["codigo"]).upper() == padrao), None) or (veiculos[0] if veiculos else None)
    if escolhido is None:
        return {"codigo": str(cfg.get("service_type") or "VAN"), "service_type": str(cfg.get("service_type") or "VAN"),
                "special_requests": []}
    return {"codigo": str(escolhido["codigo"]).upper(),
            "service_type": str(escolhido.get("service_type") or escolhido["codigo"]).upper(),
            "special_requests": [str(s) for s in (escolhido.get("special_requests") or [])]}


def rascunho_e_lalamove(rascunho: dict, cfg: dict | None = None) -> bool:
    """Rota cujo motorista é o agente virtual LALAMOVE (por agent_id do
    config; fallback: nome do motorista começando com 'LALAMOVE')."""
    cfg = cfg if cfg is not None else cfg_lalamove()
    agent_cfg = int(cfg.get("agent_id_vuupt") or 0)
    if agent_cfg and int(rascunho.get("agent_id") or 0) == agent_cfg:
        return True
    return (rascunho.get("motorista_nome") or "").strip().upper().startswith("LALAMOVE")


# ── Montagem do pedido ─────────────────────────────────────────────────

def _coords_base(config: dict) -> tuple[float, float]:
    from geocodificacao import geocodificar
    coords = geocodificar(ENDERECO_BASE, config.get("google_maps", {}).get("api_key", ""))
    if not coords:
        raise RuntimeError("Lalamove: não consegui geocodificar a base (Rua Zilda).")
    return coords


def _telefone_servico(servico: dict | None) -> str:
    if not servico:
        return ""
    tel = servico.get("phone_number") or (servico.get("customer") or {}).get("phone_number") or ""
    tel = re.sub(r"[^\d+]", "", str(tel))
    if tel and not tel.startswith("+"):
        tel = "+55" + tel.lstrip("0") if len(tel) <= 11 else "+" + tel
    return tel if re.fullmatch(r"\+[1-9]\d{1,14}", tel) else ""


def _titulo_com_codigo(titulo: str, order_id: str, prefixo: str) -> str:
    marca = f"[{prefixo} {order_id}]"
    titulo = (titulo or "").strip()
    if marca in titulo:
        return titulo
    titulo = re.sub(rf"^\[{re.escape(prefixo)} [^\]]*\]\s*", "", titulo)  # troca código antigo
    return f"{marca} {titulo}".strip()


def criar_pedido_para_rascunho(rascunho_id: int, token: str, config: dict | None = None) -> dict:
    """Cota e cria o pedido Lalamove pra um rascunho já ENVIADO à VUUPT.
    Lança exceção em falha (quem chama grava lalamove_erro)."""
    import rascunhos_rota

    config = config or _carregar_config()
    cfg = cfg_lalamove(config)
    rascunho = rascunhos_rota.buscar_rascunho(rascunho_id)
    if not rascunho:
        raise ValueError(f"Rascunho {rascunho_id} não encontrado.")
    if rascunho.get("lalamove_order_id"):
        return {"ok": True, "order_id": rascunho["lalamove_order_id"], "ja_existia": True}
    if not rascunho["paradas"]:
        raise ValueError("Rascunho sem paradas.")

    lala = cliente_de_config(cfg)
    vuupt = VuuptClient(token)
    tel_base = str(cfg.get("remetente_telefone") or "")
    nome_base = str(cfg.get("remetente_nome") or "Fresh Log")
    if not re.fullmatch(r"\+[1-9]\d{1,14}", tel_base):
        raise ValueError("lalamove.remetente_telefone precisa estar em E.164 (+5511...) no config.yaml.")

    # Telefone do destinatário não fica no rascunho -- busca o serviço na VUUPT.
    entregas, contatos = [], []
    for p in rascunho["paradas"]:
        servico = vuupt.buscar_servico_por_id(p["service_id"])
        tel = _telefone_servico(servico)
        if not tel:
            logger.warning(f"Lalamove: pedido {p['codigo']} sem telefone válido -- usando o da base.")
            tel = tel_base
        entregas.append({"code": p["codigo"], "latitude": p["latitude"], "longitude": p["longitude"],
                         "endereco": p["endereco"]})
        contatos.append({"name": (p.get("destinatario_nome") or p["codigo"])[:100], "phone": tel,
                         "remarks": f"Pedido {p['codigo']}"
                                    + (f" · atende {p['horario_atendimento_inicio']}-{p['horario_atendimento_fim']}"
                                       if p.get("horario_atendimento_inicio") else "")})

    lat_b, lng_b = _coords_base(config)
    stops = stops_da_rota({"latitude": lat_b, "longitude": lng_b, "endereco": ENDERECO_BASE}, entregas)

    veiculo = resolver_veiculo(cfg, rascunho.get("lalamove_veiculo"))
    # Opcionais escolhidos na tela + os fixos do veículo do config, sem
    # duplicar; o que não existe pro service_type (catálogo) é descartado
    # com aviso em vez de derrubar a cotação inteira.
    special = list(dict.fromkeys(veiculo["special_requests"] + special_requests_do_rascunho(rascunho)))
    catalogo = catalogo_special_requests(config)
    validos = {i["codigo"] for i in catalogo.get(veiculo["service_type"], [])}
    if validos:
        descartados = [s for s in special if s not in validos]
        if descartados:
            logger.warning(f"Lalamove: opcionais fora do catálogo de {veiculo['service_type']} descartados: {descartados}")
        special = [s for s in special if s in validos]
    logger.info(f"Lalamove: veículo {veiculo['codigo']} -> serviceType {veiculo['service_type']} "
                f"specialRequests {special}")
    # Sem scheduleAt: corrida imediata. O "quando" é decidido do nosso
    # lado -- clique no botão ou horário programado no card (Hugo, 03/09
    # não quer o agendamento da Lalamove).
    cotacao = lala.cotar(stops, veiculo["service_type"], schedule_at=None,
                         is_route_optimized=False, special_requests=special)
    if not rascunho.get("lalamove_veiculo"):
        rascunhos_rota.gravar_lalamove(rascunho_id, veiculo=veiculo["codigo"])
    stop_ids = [s.get("stopId") for s in cotacao.get("stops", [])]
    if len(stop_ids) != len(stops):
        raise LalamoveAPIError(f"Cotação devolveu {len(stop_ids)} stopIds pra {len(stops)} paradas.")

    pedido = lala.criar_pedido(
        cotacao["quotationId"],
        sender={"stopId": stop_ids[0], "name": nome_base, "phone": tel_base},
        recipients=[{"stopId": sid, **c} for sid, c in zip(stop_ids[1:], contatos)],
        is_pod_enabled=True,
        metadata={"rascunho_id": rascunho_id, "vuupt_route_id": rascunho.get("vuupt_route_id") or "",
                  "rota": rascunho.get("nome") or "", "data": rascunho.get("data_alvo") or ""},
    )
    resumo = resumir_pedido(pedido)
    preco = (cotacao.get("priceBreakdown") or {}).get("total") or resumo.get("total")
    rascunhos_rota.gravar_lalamove(
        rascunho_id, order_id=resumo["orderId"], quotation_id=cotacao["quotationId"],
        status=resumo["status"] or "ASSIGNING_DRIVER", share_link=resumo.get("shareLink"),
        preco=preco, erro=None,
    )
    logger.info(f"Lalamove: pedido {resumo['orderId']} criado pra rota '{rascunho.get('nome')}' "
                f"({len(entregas)} entregas, {preco} {resumo.get('currency') or ''}).")

    erros_titulo = _carimbar_titulos(vuupt, rascunho["paradas"], resumo["orderId"], cfg)
    if erros_titulo:
        rascunhos_rota.gravar_lalamove(rascunho_id, erro="Título não atualizado: " + "; ".join(erros_titulo))
    return {"ok": True, "order_id": resumo["orderId"], "preco": preco, "share_link": resumo.get("shareLink"),
            "erros_titulo": erros_titulo}


def _carimbar_titulos(vuupt: VuuptClient, paradas: list[dict], order_id: str, cfg: dict) -> list[str]:
    prefixo = str(cfg.get("prefixo_titulo") or "LALAMOVE")
    erros = []
    for p in paradas:
        try:
            servico = vuupt.buscar_servico_por_id(p["service_id"]) or {}
            titulo_atual = servico.get("title") or p.get("titulo") or p["codigo"]
            novo = _titulo_com_codigo(titulo_atual, order_id, prefixo)
            if novo != titulo_atual:
                # payload parcial no nível raiz (mesmo padrão de _gravar_endereco_pedido)
                vuupt.atualizar_servico(p["service_id"], {"title": novo})
        except Exception as e:
            erros.append(f"{p['codigo']}: {e}")
    return erros


# ── Sincronização de status ────────────────────────────────────────────

def _liberar_rota_agendada(vuupt: VuuptClient, route_id: int) -> None:
    """PUT /routes/{id} com start_at = agora (UTC): rota 'scheduled' vira
    'assigned' e os serviços passam a not_assigned -> assigned (28/08)."""
    from vuupt_client import API_BASE_URL
    from http_retry import chamar_com_retry
    agora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")
    resp = chamar_com_retry(vuupt.session.put, f"{API_BASE_URL}/routes/{route_id}",
                            json={"start_at": agora}, timeout=20)
    if not resp.ok:
        raise RuntimeError(f"PUT /routes/{route_id} start_at={agora} -> {resp.status_code}: {resp.text[:200]}")
    logger.info(f"Rota VUUPT {route_id} agendada liberada (start_at={agora}) pra concluir serviços da Lalamove.")


def _concluir_na_vuupt(vuupt: VuuptClient, service_id: int, codigo: str, modo_teste: bool,
                       agent_id: int = 0) -> bool:
    servico = vuupt.buscar_servico_por_id(service_id) or {}
    status = str(servico.get("status") or "")
    if status in STATUS_VUUPT_FECHADO:
        return False
    if not hasattr(vuupt, "concluir_como_agente"):
        logger.warning("VuuptClient.concluir_como_agente indisponível -- serviço não concluído.")
        return False
    if modo_teste:
        logger.info(f"[TESTE] concluiria serviço {service_id} ({codigo}) na VUUPT (status atual {status}).")
        return True
    # Rota agendada (start_at futuro) deixa o serviço not_assigned até a
    # hora de saída, e nesse estado nem assign-agent (409) nem accept
    # (410) funcionam (visto 28/08). O que libera é adiantar o start_at
    # da rota pra agora (PUT /routes/{id}) -- a rota vira 'assigned' e os
    # serviços recebem o agente virtual.
    if status in ("", "not_assigned"):
        route_id = servico.get("route_id")
        if route_id:
            _liberar_rota_agendada(vuupt, int(route_id))
            status = str((vuupt.buscar_servico_por_id(service_id) or {}).get("status") or "")
        elif agent_id:
            vuupt.atribuir_agente(service_id, agent_id)
            status = "assigned"
    vuupt.concluir_como_agente(service_id, sucesso=True, status_atual=status)
    logger.info(f"Lalamove -> VUUPT: serviço {service_id} ({codigo}) concluído.")
    return True


def sincronizar_pedidos(token: str, config: dict | None = None, modo_teste: bool = False) -> dict:
    """Atualiza status dos pedidos Lalamove abertos e fecha na VUUPT os
    serviços já entregues. Retorna contadores."""
    import rascunhos_rota

    config = config or _carregar_config()
    cfg = cfg_lalamove(config)
    stats = {"pedidos": 0, "atualizados": 0, "servicos_concluidos": 0, "erros": 0}
    abertos = rascunhos_rota.listar_rascunhos_lalamove_abertos()
    if not abertos:
        return stats
    lala = cliente_de_config(cfg)
    vuupt = VuuptClient(token)

    for r in abertos:
        stats["pedidos"] += 1
        try:
            resumo = resumir_pedido(lala.consultar_pedido(r["lalamove_order_id"]))
        except Exception as e:
            stats["erros"] += 1
            logger.warning(f"Lalamove: falha ao consultar pedido {r['lalamove_order_id']}: {e}")
            continue

        status = resumo["status"] or r.get("lalamove_status")
        if not modo_teste and (status != r.get("lalamove_status") or resumo.get("shareLink") != r.get("lalamove_share_link")):
            rascunhos_rota.gravar_lalamove(r["id"], status=status, share_link=resumo.get("shareLink"),
                                           preco=resumo.get("total") or r.get("lalamove_preco"))
            stats["atualizados"] += 1
        logger.info(f"Lalamove pedido {r['lalamove_order_id']} (rota '{r.get('nome')}'): {status}")

        # Paradas da Lalamove = [coleta] + entregas na ordem do rascunho.
        pods = resumo["paradas"][1:] if len(resumo["paradas"]) > 1 else []
        for i, p in enumerate(r["paradas"]):
            pod_status = pods[i]["pod_status"] if i < len(pods) else None
            entregue = pod_status in POD_ENTREGUE or status == "COMPLETED"
            if not entregue:
                continue
            try:
                if _concluir_na_vuupt(vuupt, p["service_id"], p["codigo"], modo_teste,
                                      agent_id=int(cfg.get("agent_id_vuupt") or 0)):
                    stats["servicos_concluidos"] += 1
            except Exception as e:
                stats["erros"] += 1
                logger.warning(f"Lalamove -> VUUPT: falha ao concluir serviço {p['service_id']} ({p['codigo']}): {e}")

        if status in STATUS_FINAIS and status != "COMPLETED":
            logger.warning(f"Lalamove pedido {r['lalamove_order_id']} terminou como {status} -- "
                           f"rota '{r.get('nome')}' (VUUPT #{r.get('vuupt_route_id')}) precisa de outro motorista.")
    return stats


# ── Lançamento no horário programado ───────────────────────────────────

MAX_TENTATIVAS_PROGRAMADO = 3


def lancar_programados(token: str, config: dict | None = None, modo_teste: bool = False,
                       agora: datetime | None = None) -> dict:
    """Timer nucleo/lancar_lalamove_programados.py (Hugo, 03/09): lança a
    corrida IMEDIATA das rotas LALAMOVE já enviadas à VUUPT cujo horário
    de lançamento escolhido no card (lalamove_lancar_em) chegou. É o
    substituto do agendamento da própria Lalamove (scheduleAt), que o
    Hugo não quer usar: o horário é nosso, a corrida nasce na hora.

    Falha conta uma tentativa (lalamove_lancar_tentativas); depois de
    MAX_TENTATIVAS_PROGRAMADO desiste e deixa o erro no card -- o botão
    "Lançar na Lalamove" continua valendo. Rota que trocou de motorista
    ou é de dia passado perde a programação (não lanço corrida atrasada
    de outro dia). Retorna contadores."""
    import rascunhos_rota

    config = config or _carregar_config()
    cfg = cfg_lalamove(config)
    agora = agora or datetime.now()
    hoje = agora.date().isoformat()
    stats = {"pendentes": 0, "lancados": 0, "erros": 0, "desistidos": 0, "ignorados": 0}

    for r in rascunhos_rota.listar_rascunhos_lalamove_programados(agora.strftime("%Y-%m-%d %H:%M"),
                                                                  max_tentativas=MAX_TENTATIVAS_PROGRAMADO):
        stats["pendentes"] += 1
        rotulo = (f"rota '{r.get('nome')}' (rascunho {r['id']}, VUUPT #{r.get('vuupt_route_id')}, "
                  f"programada pra {r.get('lalamove_lancar_em')})")
        motivo_ignorar = None
        if not rascunho_e_lalamove(r, cfg):
            motivo_ignorar = "não está mais com o motorista virtual LALAMOVE"
        elif str(r.get("data_alvo") or "") < hoje:
            motivo_ignorar = "é de dia passado -- não lanço corrida atrasada de outro dia"
        if motivo_ignorar:
            stats["ignorados"] += 1
            logger.warning(f"Lalamove: {rotulo} {motivo_ignorar}; programação removida.")
            if not modo_teste:
                rascunhos_rota.gravar_lalamove(r["id"], lancar_em=None)
            continue

        if modo_teste:
            logger.info(f"[TESTE] lançaria agora a corrida da {rotulo}.")
            stats["lancados"] += 1
            continue

        resultado = rascunhos_rota.lancar_lalamove(r["id"], token)  # grava lalamove_erro em falha
        if resultado.get("ok"):
            stats["lancados"] += 1
            preco = f" · R$ {resultado['preco']}" if resultado.get("preco") else ""
            logger.info(f"Lalamove: corrida #{resultado.get('order_id')} lançada no horário programado{preco} -- "
                        f"{rotulo}{' (já existia)' if resultado.get('ja_existia') else ''}.")
            continue

        stats["erros"] += 1
        tentativas = int(r.get("lalamove_lancar_tentativas") or 0) + 1
        rascunhos_rota.gravar_lalamove(r["id"], lancar_tentativas=tentativas)
        if tentativas >= MAX_TENTATIVAS_PROGRAMADO:
            stats["desistidos"] += 1
            logger.error(f"Lalamove: lançamento programado da {rotulo} falhou {tentativas}x -- desisti; lançar "
                         f"pelo botão do card em /planejamento. Último erro: {resultado.get('erro')}")
        else:
            logger.warning(f"Lalamove: lançamento programado da {rotulo} falhou (tentativa {tentativas}/"
                           f"{MAX_TENTATIVAS_PROGRAMADO}): {resultado.get('erro')} -- tento de novo na próxima rodada.")
    return stats
