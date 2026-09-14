# -*- coding: utf-8 -*-
"""
nucleo/assistente_motorista.py

Assistente de triagem do chat do MOTORISTA (aba Ajuda do app) -- pedido do
Hugo, 12/09/2026. Mesmo desenho do assistente do portal do cliente
(portal_cliente/assistente.py), com outro contexto: quem fala é o motorista
que está na rua, e o que o modelo sabe é a ROTA DELE de hoje, as paradas, a
tarifa e os pedágios.

Fluxo em etapas (chamado.etapa_assistente):
    area     -> motorista escolhe o assunto (chips) ou escreve livre
    parada   -> quando o assunto é entrega: qual pedido (TODAS as paradas da
                rota, pendentes primeiro, com cliente/código/bairro/caixas)
                ou "não é sobre um pedido". Sem rota: pede o código PS-.
    problema -> o que aconteceu naquele pedido (chips de ocorrência)
    livre    -> conversa com o modelo (Claude), que faz UMA pergunta por vez
                com respostas prontas pra tocar e decide se resolveu ou se
                precisa da logística

Objetivo (Hugo, 13/09): o motorista na rua escreve pouco e mal; o chat tem
que levá-lo por toque até "qual pedido + o que houve" antes de qualquer
texto livre.

O modelo NUNCA autoriza nada operacional (liberar insucesso, mandar voltar,
prometer pagamento, autorizar guincho, reagendar entrega): isso é sempre
encaminhado pra logística. Emergência (acidente, roubo, veículo parado) vai
direto pra fila humana, sem conversa.
"""
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "portal_cliente"))

import chamados as ch  # noqa: E402  (portal_cliente/chamados.py)
from nucleo import banco, financeiro  # noqa: E402
from regras import tarifa_motorista  # noqa: E402

logger = logging.getLogger("nucleo.assistente_motorista")

MODELO_PADRAO = "claude-opus-5"
ETAPA_AREA, ETAPA_PARADA, ETAPA_PROBLEMA, ETAPA_LIVRE = "area", "parada", "problema", "livre"
SEM_PARADA = "__sem_parada__"
OPCAO_ATENDENTE = {"rotulo": "Falar com a logística", "acao": "atendente", "estilo": "principal"}
OPCAO_RESOLVEU = {"rotulo": "Resolveu, obrigado", "acao": "resolvido"}
MAX_PEDIDOS_OPCOES = 40
MAX_SUGESTOES = 4
TEXTO_TOQUE_PEDIDO = "Toque no cliente na lista."

# "O que aconteceu?" depois de escolher o pedido. Espelha os motivos de
# ocorrência mais comuns (motivos_ocorrencia) em linguagem de rua.
PREFIXO_PROBLEMA = "problema:"
PROBLEMAS_ENTREGA = {
    "fechado": "Local fechado / ninguém atende",
    "recusou": "Cliente recusou ou não reconhece",
    "endereco": "Não achei o endereço",
    "produto": "Produto faltando ou avariado",
    "espera": "Demora pra receber / fora do horário",
    "documento": "Nota, boleto ou canhoto",
    "outro": "Outro",
}
SITUACAO_ROTULO = {"PENDENTE": "Pendente", "EM_DESLOCAMENTO": "A caminho", "EM_ROTA": "No local",
                   "ENTREGUE": "Entregue", "PARCIAL": "Parcial", "INSUCESSO": "Não entregue",
                   "CANCELADA": "Cancelada"}

# Assuntos que NÃO passam pelo assistente: vão direto pra fila humana.
AREAS_URGENTES = {"veiculo"}

_SCHEMA_RESPOSTA = {
    "type": "object",
    "properties": {
        "resposta": {"type": "string", "description": "O que dizer ao motorista, em português do Brasil, curto (até 3 frases), linguagem simples."},
        "area": {"type": "string", "enum": list(ch.AREAS_MOTORISTA.keys())},
        "assunto": {"type": "string", "description": "Título do chamado, até 60 caracteres, sem ponto final."},
        "resumo": {"type": "string", "description": "Resumo pra logística: o que o motorista precisa, com rota/parada/pedido se houver."},
        "resolvido": {"type": "boolean", "description": "true se a dúvida foi respondida por completo com os dados disponíveis."},
        "precisa_atendente": {"type": "boolean", "description": "true se precisa de decisão humana (autorizar, liberar, pagar, mandar voltar, avisar cliente) ou se faltam dados."},
        "sugestoes": {"type": "array", "items": {"type": "string"},
                      "description": "Até 4 respostas curtas (até 30 caracteres cada) que o motorista pode TOCAR pra responder a sua pergunta. Vazio se você não fez pergunta."},
        "pedir_pedido": {"type": "boolean", "description": "true quando o assunto é um pedido/entrega específico e a parada em foco é 'nenhuma': o app mostra a lista de pedidos da rota pra ele tocar."},
    },
    "required": ["resposta", "area", "assunto", "resumo", "resolvido", "precisa_atendente", "sugestoes", "pedir_pedido"],
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


# ── Contexto do motorista (rota de hoje, paradas, dinheiro) ────────────────────

def _resumo_parada(p: dict) -> dict:
    campos = ("ordem", "codigo", "destinatario_nome", "endereco", "situacao", "volume_caixas",
              "janela_inicio", "janela_fim", "motivo_texto", "reagendado_para", "tentativas")
    return {k: p.get(k) for k in campos if p.get(k) not in (None, "", [])}


def contexto_motorista(motorista: dict, config: dict) -> dict:
    """Rota de hoje (com paradas), extrato curto e tarifa -- o que o modelo
    pode responder sozinho. Nunca levanta: sem dado, devolve vazio."""
    agent_id = motorista.get("agent_id")
    out = {"rota": None, "paradas": [], "extrato": None, "tarifa": None}
    if agent_id is None:
        return out
    conn = None
    try:
        conn = banco.conectar()
        hoje = date.today()
        rows = conn.execute("""
            SELECT * FROM nucleo_rotas
            WHERE agent_id = ? AND data_rota BETWEEN ? AND ? AND status != ?
            ORDER BY (data_rota = ?) DESC, data_rota DESC LIMIT 1
        """, (agent_id, (hoje - timedelta(days=1)).isoformat(), hoje.isoformat(), banco.ROTA_CANCELADA,
              hoje.isoformat())).fetchone()
        if rows:
            rota = dict(rows)
            rota.pop("dados_json", None)
            out["rota"] = {k: rota.get(k) for k in ("id", "data_rota", "nome", "status", "provedor", "start_at",
                                                    "km_estimado", "km_real", "total_paradas", "entregues", "insucessos")}
            paradas = conn.execute("SELECT * FROM nucleo_paradas WHERE rota_id = ? ORDER BY ordem", (rota["id"],)).fetchall()
            out["paradas"] = [dict(p) for p in paradas]
        tipo = motorista.get("tipo_veiculo")
        t = tarifa_motorista.calcular_valor_rota(tipo, 0, tarifa_motorista.carregar_tarifas(conn))
        out["tarifa"] = t.como_dict() if t else None
        ini = (hoje - timedelta(days=13)).isoformat()
        ex = financeiro.extrato_motorista(agent_id, ini, hoje.isoformat(), tipo, conn=conn)
        out["extrato"] = {"periodo": "últimos 14 dias", "rotas": len(ex["linhas"]), "total": ex["total"],
                          "total_pedagio": ex.get("total_pedagio"), "pedagio_pendente": ex.get("pedagio_pendente"),
                          "valores_provisorios": ex.get("valores_provisorios")}
    except Exception as e:
        logger.warning("assistente do motorista: contexto de %s falhou: %s", agent_id, e)
    finally:
        if conn is not None:
            conn.close()
    return out


def paradas_pendentes(ctx: dict) -> list[dict]:
    finais = {"ENTREGUE", "PARCIAL", "INSUCESSO", "CANCELADA"}
    return [p for p in ctx.get("paradas") or [] if (p.get("situacao") or "") not in finais]


def paradas_para_escolha(ctx: dict) -> list[dict]:
    """Ordem da lista "qual pedido?": onde ele está agora, depois o que falta
    (na ordem da rota), depois o que já foi feito (mais recente primeiro) --
    problema de canhoto ou avaria aparece DEPOIS da entrega."""
    em_curso = {banco.PARADA_EM_ROTA: 0, banco.PARADA_EM_DESLOCAMENTO: 1, banco.PARADA_PENDENTE: 2}
    todas = [p for p in ctx.get("paradas") or [] if p.get("situacao") != banco.PARADA_CANCELADA]
    abertas = sorted((p for p in todas if p.get("situacao") in em_curso),
                     key=lambda p: (em_curso[p["situacao"]], p.get("ordem") or 0))
    feitas = sorted((p for p in todas if p.get("situacao") not in em_curso),
                    key=lambda p: -(p.get("ordem") or 0))
    return abertas + feitas


def codigo_base(codigo: str | None) -> str:
    """'#PS-38905-R2' -> 'PS-38905' (o código da parada vem com # e sufixo de
    reentrega; o motorista digita só o número ou PS-número)."""
    m = re.search(r"PS-?\s*(\d{3,})", (codigo or "").upper())
    return f"PS-{m.group(1)}" if m else (codigo or "").lstrip("#").strip()


def _bairro(endereco: str | None) -> str:
    partes = [x.strip() for x in (endereco or "").split(",")]
    return partes[1] if len(partes) >= 3 and not partes[1][:1].isdigit() else ""


def localizar_parada(texto: str, paradas: list[dict]) -> dict | None:
    """Acha a parada citada pelo código do pedido (com ou sem '#', 'PS-' ou
    sufixo de reentrega), pelo nome do destinatário ou pelo número da ordem."""
    t = (texto or "").upper().strip()
    if not t or not paradas:
        return None
    for p in paradas:
        cod = (p.get("codigo") or "").upper()
        if cod and cod in t:
            return p
    numeros = set(re.findall(r"\d{3,}", t))       # ordem tem até 2 dígitos; 3+ é código
    for p in paradas:
        m = re.search(r"(\d{3,})", codigo_base(p.get("codigo")))
        if m and m.group(1) in numeros:
            return p
    for p in paradas:
        nome = (p.get("destinatario_nome") or "").upper()
        if len(nome) >= 4 and nome[:18] in t:
            return p
    m = re.fullmatch(r"(?:PARADA\s*)?(\d{1,2})", t)
    if m:
        ordem = int(m.group(1))
        for p in paradas:
            if p.get("ordem") == ordem:
                return p
    return None


# ── Mensagens do assistente ────────────────────────────────────────────────────

def _opcoes_areas() -> list[dict]:
    return [{"rotulo": r, "valor": k, "acao": "chip"} for k, r in ch.AREAS_MOTORISTA.items()]


def _opcoes_paradas(paradas: list[dict]) -> list[dict]:
    """Um chip por pedido. `pedido` leva o que o app mostra no cartão; app
    antigo ignora e usa só o rótulo."""
    ops = []
    for p in paradas[:MAX_PEDIDOS_OPCOES]:
        nome = (p.get("destinatario_nome") or codigo_base(p.get("codigo")) or "").strip()
        ops.append({
            "rotulo": f"{p.get('ordem')} · {nome[:26]}",
            "valor": p.get("codigo") or str(p.get("ordem")),
            "acao": "chip",
            "pedido": {
                "ordem": p.get("ordem"), "nome": nome, "codigo": codigo_base(p.get("codigo")),
                "bairro": _bairro(p.get("endereco")), "caixas": p.get("volume_caixas"),
                "situacao": p.get("situacao"), "situacao_rotulo": SITUACAO_ROTULO.get(p.get("situacao") or "", ""),
                "janela": f"{p['janela_inicio']}-{p['janela_fim']}" if p.get("janela_inicio") and p.get("janela_fim") else "",
            },
        })
    ops.append({"rotulo": "Não é sobre um pedido", "valor": SEM_PARADA, "acao": "chip"})
    return ops


def _opcoes_problemas() -> list[dict]:
    return [{"rotulo": r, "valor": PREFIXO_PROBLEMA + k, "acao": "chip"} for k, r in PROBLEMAS_ENTREGA.items()]


def _perguntar_pedido(conn, chamado: dict, ctx: dict, texto: str | None = None) -> list[dict]:
    """Mostra a lista de pedidos da rota (ou pede o código, sem rota)."""
    ch.atualizar_chamado(conn, chamado["id"], etapa_assistente=ETAPA_PARADA)
    chamado = ch.buscar_chamado(conn, chamado["id"])
    lista = paradas_para_escolha(ctx)
    if lista:
        return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente",
                                      texto or f"Qual pedido? {TEXTO_TOQUE_PEDIDO}", opcoes=_opcoes_paradas(lista))]
    return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente",
                                  "Não achei rota sua hoje. Qual o número do pedido? (ex.: PS-38905)",
                                  opcoes=[{"rotulo": "Não é sobre um pedido", "valor": SEM_PARADA, "acao": "chip"}])]


def iniciar(conn, chamado: dict, motorista: dict) -> dict:
    nome = (motorista.get("nome") or "").strip().split(" ")[0]
    hora = datetime.now().hour
    saudacao = "Bom dia" if hora < 12 else ("Boa tarde" if hora < 18 else "Boa noite")
    texto = f"{saudacao}{', ' + nome.title() if nome else ''}! Sou o assistente da Fresh Log. Como posso ajudar?"
    ch.atualizar_chamado(conn, chamado["id"], etapa_assistente=ETAPA_AREA)
    return ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente", texto, opcoes=_opcoes_areas())


def responder(conn, chamado: dict, motorista: dict, texto: str, chip: str | None, config: dict) -> list[dict]:
    """Processa a fala do motorista (já gravada) e devolve as mensagens novas
    do assistente. Nunca levanta -- em erro, oferece a logística."""
    try:
        return _responder(conn, chamado, motorista, texto, chip, config)
    except Exception as e:
        logger.exception("assistente do motorista falhou no chamado %s: %s", chamado["id"], e)
        ch.atualizar_chamado(conn, chamado["id"], etapa_assistente=ETAPA_LIVRE)
        return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente",
                                      "Não consegui processar sua mensagem agora. Quer falar com a logística?",
                                      opcoes=[OPCAO_ATENDENTE])]


def _responder(conn, chamado, motorista, texto, chip, config) -> list[dict]:
    etapa = chamado.get("etapa_assistente") or ETAPA_AREA
    ctx = contexto_motorista(motorista, config)

    # ── etapa assunto ──
    if etapa == ETAPA_AREA:
        area = chip if chip in ch.AREAS_MOTORISTA else None
        if not area:
            return _conversar(conn, chamado, motorista, ctx, config, primeira=True)
        campos = {"area": area, "etapa_assistente": ETAPA_PARADA}
        if ctx.get("rota"):
            campos["rota_id"] = ctx["rota"]["id"]
        ch.atualizar_chamado(conn, chamado["id"], **campos)
        chamado = ch.buscar_chamado(conn, chamado["id"])

        if area in AREAS_URGENTES:
            # Veículo/acidente/atraso não passa por triagem: a logística
            # precisa saber AGORA (decisão do Hugo, 12/09).
            ch.atualizar_chamado(conn, chamado["id"], etapa_assistente=ETAPA_LIVRE,
                                 assunto="Veículo / acidente / atraso")
            chamado = ch.buscar_chamado(conn, chamado["id"])
            return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente",
                                          "Entendi, isso é urgente. Me conta em uma frase o que aconteceu e onde você está "
                                          "-- já vou chamar a logística.", opcoes=[OPCAO_ATENDENTE])]
        if area == "entrega_problema":
            return _perguntar_pedido(conn, chamado, ctx)
        ch.atualizar_chamado(conn, chamado["id"], etapa_assistente=ETAPA_LIVRE)
        chamado = ch.buscar_chamado(conn, chamado["id"])
        return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente", "Certo. Me conta o que houve.")]

    # ── etapa parada (qual pedido?) ──
    if etapa == ETAPA_PARADA:
        if chip == SEM_PARADA:
            ch.atualizar_chamado(conn, chamado["id"], etapa_assistente=ETAPA_LIVRE, pedido_ref="")
            chamado = ch.buscar_chamado(conn, chamado["id"])
            return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente", "Certo. Me conta o que você precisa.")]
        p = localizar_parada(chip or texto, ctx.get("paradas") or [])
        if p:
            ch.atualizar_chamado(conn, chamado["id"], pedido_ref=codigo_base(p.get("codigo"))[:40], parada_id=p.get("id"),
                                 pedido_dados=_resumo_parada(p))
            chamado = ch.buscar_chamado(conn, chamado["id"])
            # Já contou o problema antes de dizer o pedido (texto livre -> lista):
            # não pergunta "o que aconteceu?" de novo.
            relatou = _tem_relato(texto, p) if not chip else _ja_relatou(conn, chamado)
            if not relatou:
                return _perguntar_problema(conn, chamado, _cartao_parada(p))
            ch.atualizar_chamado(conn, chamado["id"], etapa_assistente=ETAPA_LIVRE)
            return _conversar(conn, ch.buscar_chamado(conn, chamado["id"]), motorista, ctx, config)
        # Sem rota carregada: vale o número do pedido digitado.
        numero = re.search(r"\d{4,}", texto or "")
        if not ctx.get("paradas") and numero:
            ref = f"PS-{numero.group(0)}"
            ch.atualizar_chamado(conn, chamado["id"], pedido_ref=ref[:40])
            chamado = ch.buscar_chamado(conn, chamado["id"])
            if not _tem_relato(texto, None):
                return _perguntar_problema(conn, chamado, f"Pedido {ref}.")
        # Não casou com nenhuma parada: o motorista respondeu contando o
        # problema em vez de escolher. NÃO grava esse texto como referência
        # (achado no teste em produção de 12/09: "O cliente está fechado..."
        # virou a "parada" do chamado na tela da equipe). Segue a conversa.
        ch.atualizar_chamado(conn, chamado["id"], etapa_assistente=ETAPA_LIVRE)
        chamado = ch.buscar_chamado(conn, chamado["id"])
        return _conversar(conn, chamado, motorista, ctx, config)

    # ── etapa problema (o que aconteceu?) ──
    if etapa == ETAPA_PROBLEMA:
        chave = chip[len(PREFIXO_PROBLEMA):] if chip and chip.startswith(PREFIXO_PROBLEMA) else None
        ch.atualizar_chamado(conn, chamado["id"], etapa_assistente=ETAPA_LIVRE)
        chamado = ch.buscar_chamado(conn, chamado["id"])
        if chave == "outro":
            return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente",
                                          "Me conta em uma frase o que houve.")]
        if chave in PROBLEMAS_ENTREGA:
            # Título provisório (fica se o modelo falhar); o modelo refina.
            ref = codigo_base(chamado.get("pedido_ref"))
            ch.atualizar_chamado(conn, chamado["id"], assunto=f"{PROBLEMAS_ENTREGA[chave]} · {ref}"[:120] if ref else PROBLEMAS_ENTREGA[chave])
            chamado = ch.buscar_chamado(conn, chamado["id"])
        return _conversar(conn, chamado, motorista, ctx, config)

    # ── etapa livre ──
    return _conversar(conn, chamado, motorista, ctx, config)


def _tem_relato(texto: str | None, p: dict | None) -> bool:
    """O motorista já contou o problema junto com o pedido ("PS-38905 tá
    fechado")? Tira código, número e nome do cliente e vê se sobra frase."""
    t = (texto or "").upper()
    if p:
        for x in (p.get("codigo"), codigo_base(p.get("codigo")), (p.get("destinatario_nome") or "")[:18]):
            if x:
                t = t.replace(x.upper(), " ")
    t = re.sub(r"#?PS-?\s*\d+(-R\d+)?|\d+|PEDIDO|PARADA|[^\wÀ-Ú ]", " ", t)
    return len([w for w in t.split() if len(w) >= 3]) >= 2


def _ja_relatou(conn, chamado: dict) -> bool:
    """Alguma fala do motorista antes da escolha do pedido já descreve o
    problema (não conta o toque no assunto nem no pedido)."""
    falas = [m["texto"] for m in ch.mensagens(conn, chamado["id"]) if m["origem"] == ch.ORIGEM_CLIENTE]
    return any(_tem_relato(t, None) for t in falas[:-1] if t not in ch.AREAS_MOTORISTA.values())


def _perguntar_problema(conn, chamado: dict, cartao: str) -> list[dict]:
    ch.atualizar_chamado(conn, chamado["id"], etapa_assistente=ETAPA_PROBLEMA)
    chamado = ch.buscar_chamado(conn, chamado["id"])
    return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente",
                                  f"{cartao} O que aconteceu?", opcoes=_opcoes_problemas())]


def _cartao_parada(p: dict) -> str:
    partes = [f"Parada {p.get('ordem')}"]
    if p.get("destinatario_nome"):
        partes.append(p["destinatario_nome"])
    if p.get("codigo"):
        partes.append(codigo_base(p["codigo"]))
    return " · ".join(partes) + "."


def _system_prompt(conn, motorista: dict, chamado: dict, ctx: dict, config: dict) -> str:
    sit = ch.situacao_atendimento(conn, config, perfil=ch.PERFIL_LOGISTICA)
    rota = ctx.get("rota")
    paradas = "\n".join(f"- {json.dumps(_resumo_parada(p), ensure_ascii=False)}" for p in (ctx.get("paradas") or [])[:40])
    parada_foco = chamado.get("pedido_dados")
    tarifa = ctx.get("tarifa") or {}
    return f"""Você é o assistente de apoio ao MOTORISTA da Fresh Log, uma transportadora de alimentos refrigerados na Grande São Paulo. Você conversa pelo chat do aplicativo com {motorista.get('nome') or 'o motorista'}, que está na rua fazendo entregas AGORA. Responda sempre em português do Brasil, direto e simples, no máximo 3 frases, sem markdown. Trate por você. Ele está dirigindo: nada de texto longo.

Data e hora de agora: {datetime.now():%d/%m/%Y %H:%M}. Logística humana: {sit['horario']}. Situação agora: {sit['texto']}.

O QUE VOCÊ PODE RESOLVER SOZINHO (com os dados abaixo):
- Rota e paradas: qual a próxima parada, endereço, janela de horário, quantas faltam, o que já foi entregue, quantas caixas.
- Como usar o app: iniciar deslocamento, marcar chegada, finalizar entrega, escolher o motivo da não entrega, reagendar a parada, tirar foto do canhoto, lançar pedágio (valor + foto do recibo), ver o extrato, marcar disponibilidade, aceitar rota/oferta.
- Regra de pagamento: valor da saída R$ {tarifa.get('valor_base', '-')}, com {tarifa.get('km_franquia', '-')} km inclusos e R$ {tarifa.get('valor_km_adicional', '-')} por km a mais. A volta ao galpão só entra no km quando a rota teve insucesso, entrega parcial ou parada fora da Grande SP. Pedágio é reembolsado à parte: o motorista lança valor + foto do recibo no app e a Fresh Log aprova antes de pagar.

O QUE VOCÊ NÃO FAZ (marque precisa_atendente=true e diga em uma frase que vai chamar a logística): autorizar insucesso ou recusa, mandar voltar ou pular parada, falar com o cliente/destinatário, liberar reentrega ou reagendamento com o cliente, prometer pagamento, valor ou prazo, autorizar guincho, socorro, combustível ou qualquer gasto, resolver acidente, roubo, avaria ou falta de produto, mexer em rota, e qualquer coisa que dependa de decisão da empresa. Nunca invente dado que não está aqui.

COMO CONVERSAR (o motorista está na rua e escreve pouco; seu trabalho é deixar o caso OBJETIVO pra logística):
- Faça UMA pergunta por vez, a que mais falta pra logística decidir, e ofereça em sugestoes de 2 a 4 respostas curtas pra ele tocar (ex.: "Há quanto tempo está esperando?" -> "Menos de 10 min", "10 a 30 min", "Mais de 30 min"). Nunca pergunte o que já está nos dados abaixo ou no que ele já disse.
- O que a logística precisa saber, conforme o caso: local fechado -> há quanto tempo espera e se ligou/tocou; recusa -> o motivo que o cliente deu e se recusou tudo ou parte; endereço -> se o número/local existe; produto faltando/avariado -> quais itens e quantas caixas, e peça foto (ícone da câmera); demora -> quanto tempo e se o cliente deu previsão; nota/boleto/canhoto -> o que falta.
- Quando já tiver o essencial (pedido + o que houve + o detalhe), não pergunte mais: diga que vai chamar a logística, sugestoes vazio, precisa_atendente=true.
- Se ele fala de um pedido/entrega e a parada em foco é "nenhuma", marque pedir_pedido=true e peça pra tocar no pedido na lista (não peça pra digitar código).

Assunto escolhido: {chamado.get('area_rotulo') or 'não informado'}. Pedido citado: {chamado.get('pedido_ref') or 'nenhum'}. Parada em foco: {json.dumps(parada_foco, ensure_ascii=False) if parada_foco else 'nenhuma'}.

Rota de hoje: {json.dumps(rota, ensure_ascii=False) if rota else 'nenhuma rota encontrada pra hoje'}.
Paradas (campos: ordem, codigo, destinatario_nome, endereco, situacao, volume_caixas, janela_inicio/fim, motivo_texto, reagendado_para):
{paradas or '- (nenhuma parada carregada)'}
Extrato do motorista: {json.dumps(ctx.get('extrato'), ensure_ascii=False) if ctx.get('extrato') else 'sem dados'}.

Preencha o JSON: resposta (o que dizer), area (classifique o assunto), assunto (título curto do chamado), resumo (pra logística: pedido, o que houve e os detalhes que ele deu, em uma linha objetiva), resolvido (true só se a dúvida foi respondida por completo e não há nada pra empresa decidir), precisa_atendente, sugestoes, pedir_pedido."""


def _historico(conn, chamado: dict) -> list[dict]:
    msgs = ch.mensagens(conn, chamado["id"])
    hist = []
    for m in msgs:
        if m["origem"] == ch.ORIGEM_CLIENTE:
            txt = m["texto"] + (f" [anexou: {', '.join(a['nome'] for a in m['anexos'])}]" if m.get("anexos") else "")
            hist.append({"role": "user", "content": txt})
        elif m["origem"] == ch.ORIGEM_ASSISTENTE:
            hist.append({"role": "assistant", "content": m["texto"]})
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


def _conversar(conn, chamado, motorista, ctx, config, primeira: bool = False) -> list[dict]:
    hist = _historico(conn, chamado)
    if not hist:
        return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente", "Me conta o que você precisa.")]
    # parada citada em texto livre, sem ter passado pela etapa 'parada'
    if not chamado.get("pedido_ref"):
        p = localizar_parada(hist[-1]["content"], ctx.get("paradas") or [])
        if p:
            ch.atualizar_chamado(conn, chamado["id"], pedido_ref=codigo_base(p.get("codigo"))[:40], parada_id=p.get("id"),
                                 pedido_dados=_resumo_parada(p))
            chamado = ch.buscar_chamado(conn, chamado["id"])
    client = _cliente_anthropic(config)
    resp = client.messages.create(
        model=_modelo(config),
        max_tokens=1024,
        system=_system_prompt(conn, motorista, chamado, ctx, config),
        messages=hist,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA_RESPOSTA}},
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("modelo recusou")
    dados = json.loads(next(b.text for b in resp.content if b.type == "text"))
    campos = {"etapa_assistente": ETAPA_LIVRE}
    if dados.get("area") in ch.AREAS_MOTORISTA and (primeira or not chamado.get("area")):
        campos["area"] = dados["area"]
    if dados.get("assunto"):
        campos["assunto"] = dados["assunto"].strip()[:120]
    if dados.get("resumo"):
        campos["resumo_assistente"] = dados["resumo"].strip()[:1000]
    if ctx.get("rota") and not chamado.get("rota_id"):
        campos["rota_id"] = ctx["rota"]["id"]
    ch.atualizar_chamado(conn, chamado["id"], **campos)
    chamado = ch.buscar_chamado(conn, chamado["id"])
    resposta = dados["resposta"].strip()
    # Falou de entrega sem dizer qual: a lista de pedidos no lugar de digitar.
    # Uma vez só por conversa e só com rota: se ele já viu a lista e seguiu
    # escrevendo, insistir vira um laço.
    if (dados.get("pedir_pedido") and not chamado.get("pedido_ref") and paradas_para_escolha(ctx)
            and not any(TEXTO_TOQUE_PEDIDO in m["texto"] for m in ch.mensagens(conn, chamado["id"])
                        if m["origem"] == ch.ORIGEM_ASSISTENTE)):
        # Texto fixo: o do modelo repetia a instrução ("toca no pedido...").
        return _perguntar_pedido(conn, chamado, ctx, f"Entendi. Qual pedido? {TEXTO_TOQUE_PEDIDO}")
    sugestoes = []
    for s in dados.get("sugestoes") or []:
        s = (s or "").strip()[:40]
        if s and s not in sugestoes:
            sugestoes.append(s)
    chips = [{"rotulo": s, "valor": s, "acao": "chip"} for s in sugestoes[:MAX_SUGESTOES]]
    if chips:
        # Perguntou algo: as respostas vêm primeiro; a logística fica à mão.
        opcoes = chips + [{**OPCAO_ATENDENTE, "estilo": "linha"}]
    elif dados.get("precisa_atendente"):
        opcoes = [OPCAO_ATENDENTE, OPCAO_RESOLVEU]
    else:
        opcoes = [OPCAO_RESOLVEU, {**OPCAO_ATENDENTE, "estilo": "linha"}]
    return [ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_ASSISTENTE, "Assistente", resposta, opcoes=opcoes)]


def garantir_resumo(conn, chamado: dict, motorista: dict, config: dict) -> dict:
    """Antes de mandar pra fila/e-mail: se o assistente ainda não gerou
    assunto/resumo, monta um a partir do que o motorista escreveu."""
    if chamado.get("assunto") and chamado.get("resumo_assistente"):
        return chamado
    msgs = [m for m in ch.mensagens(conn, chamado["id"]) if m["origem"] == ch.ORIGEM_CLIENTE]
    falas = [m["texto"] for m in msgs if m["texto"] and m["texto"] not in ch.AREAS_MOTORISTA.values()]
    texto = " ".join(falas)[:600] or (chamado.get("area_rotulo") or "Apoio ao motorista")
    campos = {}
    if not chamado.get("assunto"):
        primeira = (falas[0] if falas else texto).strip().split("\n")[0][:60]
        if chamado.get("pedido_ref") and chamado["pedido_ref"] not in primeira:
            primeira = f"{primeira[:44]} · {chamado['pedido_ref']}"
        campos["assunto"] = primeira or (chamado.get("area_rotulo") or "Apoio ao motorista")
    if not chamado.get("resumo_assistente"):
        campos["resumo_assistente"] = (f"Assunto: {chamado.get('area_rotulo') or '-'}. Parada/pedido: {chamado.get('pedido_ref') or '-'}. "
                                       f"Motorista escreveu: {texto}")[:1000]
    ch.atualizar_chamado(conn, chamado["id"], **campos)
    return ch.buscar_chamado(conn, chamado["id"])
