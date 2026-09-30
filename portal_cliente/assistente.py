# -*- coding: utf-8 -*-
"""
portal_cliente/assistente.py

Assistente de triagem do chat do portal (pedido do Hugo, 09/09/2026):
antes de chegar num atendente, entende a ÁREA, o PEDIDO/NF e um RESUMO da
demanda -- e, quando dá, tira a dúvida sozinho com os dados que o portal
já tem (posição do pedido, motorista, previsão, canhoto).

Fluxo em etapas (chamado.etapa_assistente):
    area   -> cliente escolhe a área (chips) ou escreve livre
    pedido -> cliente informa pedido/NF (chips com os pedidos do dia) ou
              "não é sobre um pedido"
    livre  -> conversa com o modelo (Claude), que responde e decide se
              resolveu ou se precisa de atendente

O modelo NUNCA promete ação operacional (reentrega, cancelamento,
reagendamento, estorno): isso é sempre encaminhado. Usa o SDK oficial
`anthropic` com a mesma chave de config.yaml (anthropic.api_key).
"""
import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
_AQUI = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_AQUI))

import chamados as ch

logger = logging.getLogger("portal_cliente.assistente")

MODELO_PADRAO = "claude-opus-5"
ETAPA_AREA, ETAPA_PEDIDO, ETAPA_LIVRE = "area", "pedido", "livre"
SEM_PEDIDO = "__sem_pedido__"
OPCAO_ATENDENTE = {"rotulo": "Falar com atendente", "acao": "atendente", "estilo": "principal"}
OPCAO_RESOLVEU = {"rotulo": "Resolveu, obrigado", "acao": "resolvido"}
RE_PEDIDO = re.compile(r"(?:PS|OS)?[-\s#]?(\d{4,7})(?:-R\d+)?", re.IGNORECASE)

_SCHEMA_RESPOSTA = {
    "type": "object",
    "properties": {
        "resposta": {"type": "string", "description": "O que dizer ao cliente, em português do Brasil, curto (até 3 frases)."},
        "area": {"type": "string", "enum": list(ch.AREAS.keys())},
        "assunto": {"type": "string", "description": "Título do chamado, até 60 caracteres, sem ponto final."},
        "resumo": {"type": "string", "description": "Resumo da demanda pro atendente, 1 a 3 frases, com pedido/NF se houver."},
        "resolvido": {"type": "boolean", "description": "true se a dúvida do cliente foi respondida por completo com os dados disponíveis."},
        "precisa_atendente": {"type": "boolean", "description": "true se a demanda exige ação humana (falta, avaria, reentrega, reagendar, cancelar, financeiro, cadastro, reclamação) ou se faltam dados."},
    },
    "required": ["resposta", "area", "assunto", "resumo", "resolvido", "precisa_atendente"],
    "additionalProperties": False,
}


def _cliente_anthropic(config: dict):
    import anthropic
    chave = (config.get("anthropic", {}) or {}).get("api_key")
    if not chave or chave == "SUA_CHAVE_AQUI":
        raise RuntimeError("anthropic.api_key ausente no config.yaml")
    return anthropic.Anthropic(api_key=chave, timeout=45.0, max_retries=1)


def _modelo(config: dict) -> str:
    return ch.cfg_chamados(config).get("modelo_assistente") or MODELO_PADRAO


# ── Dados do cliente (pedidos do dia) ──────────────────────────────────────────

def _pedidos_do_cliente(cliente: dict, config: dict) -> tuple[list[dict], dict]:
    """Pedidos de hoje + agendados (cache de 5 min do próprio portal)."""
    try:
        import dados_cliente
        d = dados_cliente.montar_dia(cliente["sender_id"], date.today(), config)
        return (d.get("pedidos") or []) + (d.get("agendados_futuros") or []), d.get("kpis") or {}
    except Exception as e:
        logger.warning("assistente: não carregou pedidos do cliente %s: %s", cliente.get("cnpj"), e)
        return [], {}


def _resumo_pedido(p: dict) -> dict:
    campos = ("codigo", "nf", "destinatario", "endereco", "volumes", "situacao_rotulo", "motorista", "rota", "ordem",
              "total_paradas", "detalhe", "concluido_em", "motivo", "janela_atendimento", "agendado_para", "criado_em", "observacoes")
    return {k: p.get(k) for k in campos if p.get(k) not in (None, "", [])}


def localizar_pedido(texto: str, pedidos: list[dict]) -> dict | None:
    """Procura o pedido/NF citado no texto entre os pedidos do cliente."""
    t = (texto or "").upper()
    numeros = {m.group(1) for m in RE_PEDIDO.finditer(t)}
    numeros |= set(re.findall(r"\d{4,9}", t))
    for p in pedidos:
        cod = (p.get("codigo") or "").upper().lstrip("#")
        base = re.sub(r"-R\d+$", "", cod)
        nf = (p.get("nf") or "").strip()
        if cod and (cod in t or base in t):
            return p
        for n in numeros:
            if n and (base.endswith(n) or base == n or (nf and n in [x.strip() for x in nf.split(",")])):
                return p
    return None


# ── Mensagens do assistente ────────────────────────────────────────────────────

def _opcoes_areas() -> list[dict]:
    return [{"rotulo": r, "valor": k, "acao": "chip"} for k, r in ch.AREAS.items()]


def _opcoes_pedidos(pedidos: list[dict]) -> list[dict]:
    ativos = [p for p in pedidos if p.get("situacao") in ("em_rota", "insucesso", "programado", "aguardando_saida", "entregue")]
    ordem = {"insucesso": 0, "em_rota": 1, "programado": 2, "aguardando_saida": 3, "entregue": 4}
    ativos.sort(key=lambda p: ordem.get(p.get("situacao"), 9))
    ops = [{"rotulo": f"{p['codigo']} · {(p.get('destinatario') or '')[:22]}", "valor": p["codigo"], "acao": "chip"} for p in ativos[:6]]
    ops.append({"rotulo": "Não é sobre um pedido", "valor": SEM_PEDIDO, "acao": "chip"})
    return ops


def iniciar(conn, chamado: dict, cliente: dict) -> dict:
    nome = (cliente.get("nome") or "").strip()
    saudacao = "Bom dia" if datetime.now().hour < 12 else ("Boa tarde" if datetime.now().hour < 18 else "Boa noite")
    texto = f"{saudacao}! Sou o assistente da Fresh Log. Sobre o que você precisa hoje?"
    if nome:
        texto = f"{saudacao}, {nome.title()}! Sou o assistente da Fresh Log. Sobre o que você precisa hoje?"
    ch.atualizar_chamado(conn, chamado["id"], etapa_assistente=ETAPA_AREA)
    return ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente", texto, opcoes=_opcoes_areas())


def responder(conn, chamado: dict, cliente: dict, texto: str, chip: str | None, config: dict) -> list[dict]:
    """Processa a fala do cliente (já gravada) e devolve as mensagens novas
    do assistente. Nunca levanta -- em erro, oferece o atendente."""
    try:
        return _responder(conn, chamado, cliente, texto, chip, config)
    except Exception as e:
        logger.exception("assistente falhou no chamado %s: %s", chamado["id"], e)
        ch.atualizar_chamado(conn, chamado["id"], etapa_assistente=ETAPA_LIVRE)
        return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente",
                                      "Não consegui processar sua mensagem agora. Quer falar com um atendente?",
                                      opcoes=[OPCAO_ATENDENTE])]


def _responder(conn, chamado, cliente, texto, chip, config) -> list[dict]:
    etapa = chamado.get("etapa_assistente") or ETAPA_AREA
    pedidos, kpis = _pedidos_do_cliente(cliente, config)
    saida = []

    # ── etapa área ──
    if etapa == ETAPA_AREA:
        area = chip if chip in ch.AREAS else None
        if not area:
            # texto livre: deixa o modelo classificar e já responder
            return _conversar(conn, chamado, cliente, pedidos, kpis, config, primeira=True)
        ch.atualizar_chamado(conn, chamado["id"], area=area, etapa_assistente=ETAPA_PEDIDO)
        chamado = ch.buscar_chamado(conn, chamado["id"])
        if area in ("financeiro", "cadastro"):
            pergunta = "Entendi. Me conta em poucas palavras o que você precisa."
            ch.atualizar_chamado(conn, chamado["id"], etapa_assistente=ETAPA_LIVRE)
            return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente", pergunta)]
        pergunta = "Entendi. Qual pedido ou NF? Se preferir, escolha um da lista."
        return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente", pergunta, opcoes=_opcoes_pedidos(pedidos))]

    # ── etapa pedido ──
    if etapa == ETAPA_PEDIDO:
        if chip == SEM_PEDIDO or re.search(r"n[ãa]o\s+(é|e)\s+sobre|sem pedido|nenhum pedido", (texto or "").lower()):
            ch.atualizar_chamado(conn, chamado["id"], etapa_assistente=ETAPA_LIVRE, pedido_ref="")
            chamado = ch.buscar_chamado(conn, chamado["id"])
            return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente", "Certo. Me conta o que você precisa.")]
        p = localizar_pedido(chip or texto, pedidos)
        if p:
            ch.atualizar_chamado(conn, chamado["id"], pedido_ref=p["codigo"], pedido_dados=_resumo_pedido(p), etapa_assistente=ETAPA_LIVRE)
            chamado = ch.buscar_chamado(conn, chamado["id"])
            # se o cliente só mandou o código, pergunta o que houve; se já contou o problema, responde direto
            so_codigo = bool(chip) or len(re.sub(r"[\W\d]+", "", texto or "")) <= 6
            if so_codigo:
                cartao = _cartao_pedido(p)
                return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente",
                                              f"Achei. {cartao} Me conta o que aconteceu ou o que você precisa com esse pedido?")]
            return _conversar(conn, chamado, cliente, pedidos, kpis, config)
        ref = (chip or texto or "").strip()[:40]
        ch.atualizar_chamado(conn, chamado["id"], pedido_ref=ref, etapa_assistente=ETAPA_LIVRE)
        chamado = ch.buscar_chamado(conn, chamado["id"])
        return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente",
                                      f"Não achei \"{ref}\" entre os pedidos de hoje e os agendados, mas anotei a referência. "
                                      "Me conta o que você precisa que eu sigo daqui.")]

    # ── etapa livre ──
    return _conversar(conn, chamado, cliente, pedidos, kpis, config)


def _cartao_pedido(p: dict) -> str:
    partes = [f"{p['codigo']}"]
    if p.get("nf"):
        partes.append(f"NF {p['nf']}")
    if p.get("destinatario"):
        partes.append(p["destinatario"])
    sit = p.get("situacao_rotulo") or ""
    det = p.get("detalhe") or ""
    mot = f" com {p['motorista']}" if p.get("motorista") else ""
    linha = " · ".join(partes)
    return f"{linha}: {sit.lower()}{mot}{(' · ' + det) if det else ''}."


def _system_prompt(conn, cliente: dict, chamado: dict, pedidos: list[dict], kpis: dict, config: dict) -> str:
    sit = ch.situacao_atendimento(conn, config)
    pedido = chamado.get("pedido_dados")
    resumo_dia = ", ".join(f"{k}: {v}" for k, v in kpis.items() if k in ("total", "entregues", "em_rota", "aguardando_saida", "insucessos", "programados"))
    lista = "\n".join(f"- {json.dumps(_resumo_pedido(p), ensure_ascii=False)}" for p in pedidos[:40])
    return f"""Você é o assistente de atendimento da Fresh Log, uma transportadora de alimentos refrigerados na Grande São Paulo. Você conversa, pelo chat do portal, com o cliente embarcador {cliente.get('nome') or ''} (quem manda os pedidos pra Fresh Log entregar). Responda sempre em português do Brasil, em tom cordial e direto, em no máximo 3 frases. Nunca use markdown.

Data e hora de agora: {datetime.now():%d/%m/%Y %H:%M}. Atendimento humano: {sit['horario']}. Situação agora: {sit['texto']}.

O QUE VOCÊ PODE FAZER SOZINHO (com os dados abaixo): informar situação do pedido, motorista e rota, posição na rota (parada N de M), horário de saída/entrega registrado, motivo de insucesso, se o comprovante (canhoto) já está disponível no portal, agendamentos futuros e o resumo do dia. Se o dado está na lista, responda com ele; se não está, diga que não tem essa informação.

O QUE VOCÊ NÃO FAZ: prometer reentrega, cancelamento, reagendamento, devolução, estorno, mudança de endereço ou qualquer ação operacional/financeira; confirmar faltas ou avarias; dar previsão de horário que não esteja nos dados. Nesses casos explique em uma frase que isso precisa de um atendente e marque precisa_atendente=true. Nunca invente dados.

Área escolhida pelo cliente: {chamado.get('area_rotulo') or 'não informada'}. Pedido em foco: {json.dumps(pedido, ensure_ascii=False) if pedido else 'nenhum'}.

Resumo do dia do cliente: {resumo_dia or 'sem dados'}.
Pedidos de hoje e agendados (campos: codigo, nf, destinatario, situacao_rotulo, motorista, rota, ordem/total_paradas, detalhe, motivo):
{lista or '- (nenhum pedido carregado)'}

Preencha o JSON: resposta (o que dizer), area (classifique), assunto (título curto do chamado), resumo (pro atendente: o que o cliente precisa, com pedido/NF), resolvido (true só se a dúvida foi respondida por completo e não há pedido de ação), precisa_atendente."""


def _historico(conn, chamado: dict) -> list[dict]:
    msgs = ch.mensagens(conn, chamado["id"])
    hist = []
    for m in msgs:
        if m["origem"] == ch.ORIGEM_CLIENTE:
            txt = m["texto"] + (f" [anexou: {', '.join(a['nome'] for a in m['anexos'])}]" if m.get("anexos") else "")
            hist.append({"role": "user", "content": txt})
        elif m["origem"] == ch.ORIGEM_ASSISTENTE:
            hist.append({"role": "assistant", "content": m["texto"]})
    # a API exige alternância e começo em user
    limpo = []
    for h in hist:
        if limpo and limpo[-1]["role"] == h["role"]:
            limpo[-1]["content"] += "\n" + h["content"]
        else:
            limpo.append(h)
    while limpo and limpo[0]["role"] != "user":
        limpo.pop(0)
    if limpo and limpo[-1]["role"] == "assistant":
        limpo.append({"role": "user", "content": "(continue)"})
    return limpo[-20:]


def _conversar(conn, chamado, cliente, pedidos, kpis, config, primeira: bool = False) -> list[dict]:
    hist = _historico(conn, chamado)
    if not hist:
        return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente", "Me conta o que você precisa.")]
    # pedido citado em texto livre, sem ter passado pela etapa 'pedido'
    if not chamado.get("pedido_ref"):
        p = localizar_pedido(hist[-1]["content"], pedidos)
        if p:
            ch.atualizar_chamado(conn, chamado["id"], pedido_ref=p["codigo"], pedido_dados=_resumo_pedido(p))
            chamado = ch.buscar_chamado(conn, chamado["id"])
    client = _cliente_anthropic(config)
    resp = client.messages.create(
        model=_modelo(config),
        max_tokens=1024,
        system=_system_prompt(conn, cliente, chamado, pedidos, kpis, config),
        messages=hist,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA_RESPOSTA}},
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("modelo recusou")
    texto_json = next(b.text for b in resp.content if b.type == "text")
    dados = json.loads(texto_json)
    campos = {"etapa_assistente": ETAPA_LIVRE}
    if dados.get("area") in ch.AREAS and (primeira or not chamado.get("area")):
        campos["area"] = dados["area"]
    if dados.get("assunto"):
        campos["assunto"] = dados["assunto"].strip()[:120]
    if dados.get("resumo"):
        campos["resumo_assistente"] = dados["resumo"].strip()[:1000]
    ch.atualizar_chamado(conn, chamado["id"], **campos)
    chamado = ch.buscar_chamado(conn, chamado["id"])
    if dados.get("precisa_atendente"):
        opcoes = [OPCAO_ATENDENTE, OPCAO_RESOLVEU]
    else:
        opcoes = [OPCAO_RESOLVEU, {**OPCAO_ATENDENTE, "estilo": "linha"}]
    return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente", dados["resposta"].strip(), opcoes=opcoes)]


def garantir_resumo(conn, chamado: dict, cliente: dict, config: dict) -> dict:
    """Antes de mandar pra fila/e-mail: se o assistente ainda não gerou
    assunto/resumo, monta um a partir do que o cliente escreveu."""
    if chamado.get("assunto") and chamado.get("resumo_assistente"):
        return chamado
    msgs = [m for m in ch.mensagens(conn, chamado["id"]) if m["origem"] == ch.ORIGEM_CLIENTE]
    falas = [m["texto"] for m in msgs if m["texto"] and m["texto"] not in ch.AREAS.values()]
    texto = " ".join(falas)[:600]
    if not texto:
        texto = chamado.get("area_rotulo") or "Atendimento"
    campos = {}
    if not chamado.get("assunto"):
        primeira = falas[0] if falas else texto
        assunto = primeira.strip().split("\n")[0][:60]
        if chamado.get("pedido_ref") and chamado["pedido_ref"] not in assunto:
            assunto = f"{assunto[:44]} · {chamado['pedido_ref']}"
        campos["assunto"] = assunto or (chamado.get("area_rotulo") or "Atendimento")
    if not chamado.get("resumo_assistente"):
        campos["resumo_assistente"] = (f"Área: {chamado.get('area_rotulo') or '-'}. Pedido: {chamado.get('pedido_ref') or '-'}. "
                                       f"Cliente escreveu: {texto}")[:1000]
    ch.atualizar_chamado(conn, chamado["id"], **campos)
    return ch.buscar_chamado(conn, chamado["id"])
