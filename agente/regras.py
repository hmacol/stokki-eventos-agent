# -*- coding: utf-8 -*-
"""
agente/regras.py

Regras PURAS do agente analista (sem banco, sem rede): dado um FATO, que
ACOES o agente toma. Mesmo padrao de vigia/regras.py. Spec e decisoes do
Hugo (04/10) em DOC_EXECUCAO_CLAUDE_AGENTE_ANALISTA.md.

Fatos que o agente entende (dicts montados por analisar.py):

  {"fonte": "vigia", "codigo", "estado", "vencido", "desde", "vence_em",
   "motivo", "detalhe", "service_id"}
      uma linha de vigia_pedidos (pedido ABERTO)

  {"fonte": "batimento", "codigo", "motivo", "evidencias"}
      uma divergencia de batimento_pedidos (pedido FECHADO) -- a tabela
      ainda nao existe (spec do batimento); a regra ja esta pronta

Acoes:
  AVISAR  -- mensagem ao embarcador (WhatsApp pelo OpenWA + e-mail). Nao
             precisa de aprovacao: so fala, nao escreve em sistema nenhum.
  PROPOR  -- proposta de acao sobre Stokki/Vuupt. Nasce PROPOSTA e so vira
             executada depois que uma pessoa aprova na Torre.

Regras que nao mudam (Hugo, 04/10): o agente fala so com o embarcador,
nunca com o destinatario; nenhuma escrita em Stokki/Vuupt sem aprovacao;
estados do vigia que ja viram excecao na Torre (rota passada, sem servico,
rascunho com erro, pool vencido) NAO geram acao aqui -- seria alerta em
dobro.
"""
import re
from dataclasses import dataclass, field

AVISAR = "AVISAR"
PROPOR = "PROPOR"

# Templates (chave -> agente/templates.py). O nome e o que vai pra tabela.
COBRAR_DATA_AGENDAMENTO = "COBRAR_DATA_AGENDAMENTO"
AVISO_FALHA_ENTREGA = "AVISO_FALHA_ENTREGA"
PEDIR_CONFIRMACAO_DEVOLUCAO = "PEDIR_CONFIRMACAO_DEVOLUCAO"
PROPOSTA_EXPEDIR_STOKKI = "PROPOSTA_EXPEDIR_STOKKI"
PROPOSTA_COBRAR_CANHOTO = "PROPOSTA_COBRAR_CANHOTO"
PROPOSTA_COBRAR_COMPROVANTE_TRANSPORTADORA = "PROPOSTA_COBRAR_COMPROVANTE_TRANSPORTADORA"
PROPOSTA_COBRAR_COMPROVANTE_RETIRADA = "PROPOSTA_COBRAR_COMPROVANTE_RETIRADA"
PROPOSTA_CONFERIR_ENTREGA = "PROPOSTA_CONFERIR_ENTREGA"
PROPOSTA_RECRIAR_OU_CANCELAR = "PROPOSTA_RECRIAR_OU_CANCELAR"
PROPOSTA_CANCELAR_SERVICO_VUUPT = "PROPOSTA_CANCELAR_SERVICO_VUUPT"

# Motivos da lista fechada do batimento -> proposta (tabela da spec).
# STATUS_STOKKI_DESCONHECIDO fica de fora de proposito: e bug de de-para,
# nao acao de operacao.
PROPOSTA_POR_DIVERGENCIA = {
    "ENTREGUE_NAO_EXPEDIDO": PROPOSTA_EXPEDIR_STOKKI,
    "EXPEDIDO_SEM_DOCUMENTO": PROPOSTA_COBRAR_CANHOTO,
    "REDESPACHO_SEM_COMPROVANTE": PROPOSTA_COBRAR_COMPROVANTE_TRANSPORTADORA,
    "RETIRADA_SEM_COMPROVANTE": PROPOSTA_COBRAR_COMPROVANTE_RETIRADA,
    "EXPEDIDO_SEM_ENTREGA": PROPOSTA_CONFERIR_ENTREGA,
    "CANCELADO_VUUPT_STOKKI_ABERTO": PROPOSTA_RECRIAR_OU_CANCELAR,
    "CANCELADO_STOKKI_SERVICO_VIVO": PROPOSTA_CANCELAR_SERVICO_VUUPT,
}

# Tentativas: original = 1, -R1 = 2, -R2 = 3. A partir da 3a falha o
# embarcador precisa confirmar a devolucao (Hugo, 04/10).
TENTATIVAS_MAXIMAS = 3
_SUFIXO_REENTREGA = re.compile(r"-R(\d+)", re.IGNORECASE)


@dataclass
class Acao:
    tipo: str                 # AVISAR | PROPOR
    template: str             # chave em agente/templates.py
    codigo: str               # pedido (como veio do fato)
    fato_origem: str          # chave de idempotencia: mesma origem nao gera a mesma acao 2x
    precisa_aprovacao: bool
    dados: dict = field(default_factory=dict)


def tentativa(codigo: str) -> int:
    """Numero da tentativa pelo codigo: PS-1 -> 1, PS-1-R1 -> 2, PS-1-R2 -> 3.
    Codigo com sufixo repetido pelo bug antigo (PS-1-R1-R1) conta cada um."""
    total = 1
    for m in _SUFIXO_REENTREGA.finditer(str(codigo or "")):
        n = int(m.group(1))
        total += n if n > 0 else 1
    # PS-1-R1-R1: dois sufixos de 1 -> tentativa 3 (soma); PS-1-R2 -> 3.
    return total


def _origem_vigia(fato: dict) -> str:
    # `desde` muda quando o pedido troca de estado e volta: a acao pode se
    # repetir, de proposito (e um fato novo).
    return f"vigia:{fato.get('estado')}:{fato.get('codigo')}:{str(fato.get('desde') or '')[:19]}"


def _decidir_vigia(fato: dict) -> list[Acao]:
    estado = fato.get("estado")
    codigo = str(fato.get("codigo") or "")
    base = {"codigo": codigo, "motivo": fato.get("motivo"), "detalhe": fato.get("detalhe"),
            "service_id": fato.get("service_id"), "desde": fato.get("desde")}
    acoes: list[Acao] = []

    if estado == "AGUARDANDO_CLIENTE" and fato.get("vencido"):
        acoes.append(Acao(AVISAR, COBRAR_DATA_AGENDAMENTO, codigo, _origem_vigia(fato), False, base))

    elif estado == "INSUCESSO":
        # Avisa a falha assim que o vigia enxerga o insucesso (nao espera
        # vencer): o e-mail de pergunta ja sai pelo fluxo de insucesso; este
        # e o aviso curto pelo WhatsApp, com o link da central.
        acoes.append(Acao(AVISAR, AVISO_FALHA_ENTREGA, codigo, _origem_vigia(fato), False,
                          {**base, "tentativa": tentativa(codigo)}))
        if tentativa(codigo) >= TENTATIVAS_MAXIMAS:
            acoes.append(Acao(AVISAR, PEDIR_CONFIRMACAO_DEVOLUCAO, codigo,
                              f"devolucao:{codigo}", False, {**base, "tentativa": tentativa(codigo)}))

    elif estado == "RECUSADO":
        # Embarcador disse "nao reenviar": pedido segue aberto na Stokki ate
        # alguem devolver/cancelar. Pede a confirmacao formal da devolucao.
        acoes.append(Acao(AVISAR, PEDIR_CONFIRMACAO_DEVOLUCAO, codigo,
                          f"devolucao:{codigo}", False, {**base, "tentativa": tentativa(codigo)}))

    # ROTA_PASSADA, SEM_SERVICO, RASCUNHO_COM_ERRO, EM_RASCUNHO, NO_POOL,
    # AGENDADO, EM_ROTA: nada -- ou ja e excecao da Torre, ou esta no prazo.
    return acoes


def _decidir_batimento(fato: dict) -> list[Acao]:
    template = PROPOSTA_POR_DIVERGENCIA.get(str(fato.get("motivo") or ""))
    if not template:
        return []
    codigo = str(fato.get("codigo") or "")
    return [Acao(PROPOR, template, codigo, f"batimento:{fato.get('motivo')}:{codigo}", True,
                 {"codigo": codigo, "motivo": fato.get("motivo"), "evidencias": fato.get("evidencias") or {}})]


def decidir(fato: dict) -> list[Acao]:
    """Acoes para UM fato. Lista vazia = nada a fazer."""
    fonte = fato.get("fonte")
    if fonte == "vigia":
        return _decidir_vigia(fato)
    if fonte == "batimento":
        return _decidir_batimento(fato)
    return []
