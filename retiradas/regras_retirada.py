"""Regras das retiradas no galpão: identificação do serviço, payload da
VUUPT e importação como serviço avulso atribuído ao agente fixo.

Fluxo (Hugo, 28/08/2026):
  - pipeline.py detecta transportadora RETIRADA (regras/transportadoras.py)
    e, em vez de ignorar, chama importar_retirada();
  - o serviço nasce com título "[RETIRADA] #PS-xxxxx - ref / Embarcador /
    Destinatário / via Transportadora", customer = o próprio galpão
    (retiradas.customer_id) e é ATRIBUÍDO ao agente retiradas.agent_id
    (status 'assigned', sem rota). Como só 'not_assigned' entra no pool
    do /planejamento e nos roteirizadores (criar_rotas_diarias,
    incrementar_rotas), ele nunca entra em rota;
  - acompanhar_retiradas.py fecha (check-out sucesso) quando a Stokki
    marca "Enviado" e cancela na VUUPT quando a Stokki cancela.
"""
import logging
import re

logger = logging.getLogger(__name__)

PREFIXO_TITULO = "[RETIRADA]"
STATUSES_ABERTOS = ("assigned", "accepted", "on_route", "arrived")

# Previsão de expedição da listagem Stokki (expedition_date, DD/MM/AAAA),
# carimbada na NOTA do serviço na importação -- a VUUPT não tem campo
# próprio pra isso num serviço sem agendamento, e scheduled_start faria a
# retirada entrar no resumo de agendados do /planejamento como se fosse
# entrega. O card "A retirar no galpão" lê de volta com este regex.
_REGEX_PREVISAO_NOTA = re.compile(r"Previs[ãa]o de expedi[çc][ãa]o \(Stokki\):\s*(\d{2}/\d{2}/\d{4})")


def data_prevista_do_servico(servico: dict) -> str:
    """'DD/MM/AAAA' carimbado na nota pela importação; '' se não houver
    (serviço criado antes desta versão, ou listagem sem a data)."""
    m = _REGEX_PREVISAO_NOTA.search(servico.get("note") or "")
    return m.group(1) if m else ""


def config_retiradas(config: dict) -> dict:
    """{'ativo', 'agent_id', 'customer_id'} -- ativo só quando agent_id e
    customer_id estão preenchidos (sem eles o pipeline mantém o
    comportamento antigo de ignorar retiradas)."""
    cfg = config.get("retiradas") or {}
    agent_id = cfg.get("agent_id")
    customer_id = cfg.get("customer_id")
    ativo = bool(cfg.get("ativo", True)) and bool(agent_id) and bool(customer_id)
    return {"ativo": ativo, "agent_id": int(agent_id) if agent_id else None,
            "customer_id": int(customer_id) if customer_id else None}


def eh_servico_retirada(servico: dict) -> bool:
    """Identificação pelo TÍTULO (pedido do Hugo): prefixo [RETIRADA]."""
    return (servico.get("title") or "").lstrip().upper().startswith(PREFIXO_TITULO)


def id_stokki_do_code(code: str) -> int | None:
    """'#PS-37190' / 'PS-37190-R1' -> 37190 (primeira sequência de dígitos,
    mesma regra de expedir_pedidos._extrair_id)."""
    m = re.search(r"(\d+)", code or "")
    return int(m.group(1)) if m else None


def montar_titulo(codigo_ps: str, referencia: str, nome_embarcador: str,
                  nome_destinatario: str, nome_transportadora: str) -> str:
    cabeca = f"{PREFIXO_TITULO} #{codigo_ps}"
    if referencia:
        cabeca += f" - {referencia}"
    partes = [cabeca]
    if nome_embarcador:
        partes.append(nome_embarcador)
    if nome_destinatario:
        partes.append(nome_destinatario)
    if nome_transportadora:
        partes.append(f"via {nome_transportadora}")
    return " / ".join(partes)


def montar_payload_retirada(codigo_ps: str, referencia: str, detalhe: dict,
                            transportadora: dict, dados_banco: dict, cfg: dict,
                            data_saida: str = "") -> dict:
    destino = detalhe.get("destino") or {}
    nome_dest = (destino.get("nome") or "").strip()
    doc_dest = (destino.get("documento") or "").strip()
    nome_transp = (transportadora.get("nome") or "").strip()
    cnpj_transp = "".join(c for c in (transportadora.get("documento") or "") if c.isdigit())
    nome_emb = dados_banco.get("apelido") or (detalhe.get("cliente") or {}).get("nome", "")
    qtd_volumes = detalhe.get("quantidade_volumes") or 1
    volume_final = max(1, round(qtd_volumes * (dados_banco.get("fator_ponderado") or 1.0)))

    nota = (
        f"RETIRADA NO GALPÃO -- não roteirizar. Quem retira: {nome_transp or 'cliente'}"
        f"{f' (CNPJ {cnpj_transp})' if cnpj_transp else ''}. "
        f"Destinatário final: {nome_dest}{f' ({doc_dest})' if doc_dest else ''}. "
        f"Fechado automaticamente quando a Stokki marcar 'Enviado'."
    )
    if (data_saida or "").strip():
        nota += f" Previsão de expedição (Stokki): {data_saida.strip()}."
    payload = {
        "title": montar_titulo(codigo_ps, referencia, nome_emb, nome_dest, nome_transp),
        "code": f"#{codigo_ps}",
        "type": "delivery",
        "customer_id": cfg["customer_id"],
        "dimension_3": volume_final,
        "note": nota,
    }
    if dados_banco.get("sender_id"):
        payload["sender_id"] = dados_banco["sender_id"]
    return payload


def importar_retirada(vuupt, payload: dict, cfg: dict, servico_existente: dict | None,
                      modo_teste: bool = False) -> tuple[str, dict | None]:
    """Cria (ou atualiza, se ainda not_assigned) o serviço avulso e o
    atribui ao agente de retiradas. Retorna (acao, servico):
    'retirada_criada' | 'retirada_atualizada' | 'simulado'.

    Não passa por criar_ou_atualizar_servico: aquele caminho faz upsert do
    contato (destinatário) e fingerprint -- aqui o contato é fixo (o galpão)
    e, uma vez atribuído, o pipeline pula o pedido pra sempre (checagem
    antecipada de status em processar_pedido)."""
    code = payload["code"]
    if modo_teste:
        logger.info(f"  {code}: [TESTE] retirada -- criaria serviço avulso '{payload['title']}' "
                    f"e atribuiria ao agente {cfg['agent_id']}.")
        return "simulado", None

    if servico_existente and servico_existente.get("status") == "not_assigned":
        service_id = servico_existente["id"]
        vuupt.atualizar_servico(service_id, payload)
        acao = "retirada_atualizada"
    else:
        resposta = vuupt.criar_servico(payload)
        servico = resposta.get("service", resposta)
        service_id = servico["id"]
        acao = "retirada_criada"

    vuupt.atribuir_agente(service_id, cfg["agent_id"])
    logger.info(f"  {code}: {acao} -- serviço avulso {service_id} atribuído ao agente de retiradas "
                f"({cfg['agent_id']}), sem rota.")
    servico_final = vuupt.buscar_servico_por_id(service_id) or {}
    return acao, (servico_final.get("service") or servico_final)
