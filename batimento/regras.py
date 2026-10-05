# -*- coding: utf-8 -*-
"""
batimento/regras.py

Regras PURAS do batimento (sem banco, sem rede): dado o retrato de UM
pedido-base, diz em qual das tres caixas da equacao ele cai:

  lancados = DESTINO confirmado nas duas pontas
           + EM_ANDAMENTO (aberto na Stokki; prazo e do vigia)
           + DIVERGENCIA com motivo

Decisoes do Hugo (04/10, ver DOC_EXECUCAO_CLAUDE_BATIMENTO_PEDIDOS.md):
  - destino final SEMPRE exige documento assinado de recebimento;
  - canhoto sem validacao vale como documento;
  - redespacho e retirada hoje nao tem comprovante: entram como divergencia;
  - servico com mais de um pedido nao existe mais.

O retrato (`fato`) e um dict montado por quem le as fontes (medir.py /
bater.py). Chaves usadas aqui:

  status_stokki_bruto   texto da coluna "state" da listagem da Stokki
  transportadora_tipo   ENTREGA | RETIRADA | TERCEIROS | None (regras/transportadoras)
  nucleo_status         ABERTO | EM_ROTA | ENTREGUE | INSUCESSO | CANCELADO | None (sem servico)
  nucleo_fluxo          ENTREGA | RETIRADA | None
  vuupt_sucesso         True se algum servico do pedido fechou done+success
  vuupt_insucesso       True se algum servico fechou done+failed
  servico_vivo          True se existe servico nao cancelado/nao excluido
  canhoto_fonte         None | "app" | "vuupt_foto" | "expedicao_anexou"
  canhoto_validado      bool
  comprovante_redespacho / comprovante_retirada / comprovante_lalamove  bool
  dedicado              True se esta em pedidos_dedicados (ativo)
  lalamove              True se a rota do pedido e do agente virtual LALAMOVE
  embarcador_recusou    True se respondeu "nao reenviar" ao insucesso
"""

# Caixas da equacao
DESTINO = "DESTINO"
EM_ANDAMENTO = "EM_ANDAMENTO"
DIVERGENCIA = "DIVERGENCIA"

# Destinos finais (lista fechada)
ENTREGUE = "ENTREGUE"
REDESPACHADO = "REDESPACHADO"
RETIRADO = "RETIRADO"
DEDICADO = "DEDICADO"
LALAMOVE = "LALAMOVE"
DEVOLVIDO = "DEVOLVIDO"
CANCELADO = "CANCELADO"

# Motivos de divergencia (lista fechada; cresce so com decisao)
EXPEDIDO_SEM_ENTREGA = "EXPEDIDO_SEM_ENTREGA"
EXPEDIDO_SEM_DOCUMENTO = "EXPEDIDO_SEM_DOCUMENTO"
ENTREGUE_NAO_EXPEDIDO = "ENTREGUE_NAO_EXPEDIDO"
CANCELADO_STOKKI_SERVICO_VIVO = "CANCELADO_STOKKI_SERVICO_VIVO"
CANCELADO_VUUPT_STOKKI_ABERTO = "CANCELADO_VUUPT_STOKKI_ABERTO"
REDESPACHO_SEM_COMPROVANTE = "REDESPACHO_SEM_COMPROVANTE"
RETIRADA_SEM_COMPROVANTE = "RETIRADA_SEM_COMPROVANTE"
LALAMOVE_SEM_COMPROVANTE = "LALAMOVE_SEM_COMPROVANTE"
EXPEDIDO_COM_INSUCESSO_ABERTO = "EXPEDIDO_COM_INSUCESSO_ABERTO"
STATUS_STOKKI_DESCONHECIDO = "STATUS_STOKKI_DESCONHECIDO"

# Situacao da Stokki, resumida em tres valores
STOKKI_ABERTO = "ABERTO"
STOKKI_EXPEDIDO = "EXPEDIDO"
STOKKI_CANCELADO = "CANCELADO"
STOKKI_DESCONHECIDO = "DESCONHECIDO"

# Textos que a listagem da Stokki usa (valor da URL em ingles; a coluna da
# tela pode vir em portugues). Comparacao por "contem", em minusculas.
_ABERTO = ("waiting for carrier", "aguardando transportador", "open", "aberto",
           "separating", "separando", "ready to pack", "pack", "on hold", "em espera",
           "picking", "separacao", "separação", "em conferência", "em conferencia")
_EXPEDIDO = ("sent", "enviado", "expedido", "shipped", "delivered", "entregue")
_CANCELADO = ("cancel",)


def situacao_stokki(texto) -> str:
    """Resume o texto da coluna 'state' em ABERTO / EXPEDIDO / CANCELADO.
    Cancelado e testado antes de expedido porque ha telas que escrevem
    'Envio cancelado'."""
    t = str(texto or "").strip().lower()
    if not t:
        return STOKKI_DESCONHECIDO
    if any(p in t for p in _CANCELADO):
        return STOKKI_CANCELADO
    if any(p in t for p in _EXPEDIDO):
        return STOKKI_EXPEDIDO
    if any(p in t for p in _ABERTO):
        return STOKKI_ABERTO
    return STOKKI_DESCONHECIDO


def _tem_canhoto(f: dict) -> bool:
    return bool(f.get("canhoto_fonte"))


def classificar(f: dict) -> tuple[str, str, list[str]]:
    """Devolve (caixa, destino_ou_motivo, evidencias).

    `evidencias` e a lista, em texto curto, do que sustentou a decisao --
    vai pra planilha/tabela pra alguem conferir sem reabrir as fontes."""
    ev: list[str] = []
    sit = situacao_stokki(f.get("status_stokki_bruto"))
    ev.append(f"stokki={sit}")

    if sit == STOKKI_DESCONHECIDO:
        return DIVERGENCIA, STATUS_STOKKI_DESCONHECIDO, ev + [f"state={f.get('status_stokki_bruto')!r}"]

    servico_vivo = bool(f.get("servico_vivo"))
    nucleo_status = f.get("nucleo_status")

    # --- Aberto na Stokki: e do vigia, salvo quando a Vuupt ja fechou ----
    if sit == STOKKI_ABERTO:
        if nucleo_status == "CANCELADO" or (nucleo_status is None and f.get("servico_ja_existiu")):
            return DIVERGENCIA, CANCELADO_VUUPT_STOKKI_ABERTO, ev + [f"nucleo={nucleo_status}"]
        if f.get("vuupt_sucesso") and not servico_vivo_pendente(f):
            return DIVERGENCIA, ENTREGUE_NAO_EXPEDIDO, ev + ["vuupt=done/success"]
        return EM_ANDAMENTO, f.get("vigia_estado") or "SEM_ESTADO_VIGIA", ev + [f"nucleo={nucleo_status}"]

    # --- Cancelado na Stokki -------------------------------------------
    if sit == STOKKI_CANCELADO:
        if servico_vivo:
            return DIVERGENCIA, CANCELADO_STOKKI_SERVICO_VIVO, ev + [f"nucleo={nucleo_status}"]
        if f.get("embarcador_recusou"):
            return DESTINO, DEVOLVIDO, ev + ["embarcador=nao reenviar"]
        return DESTINO, CANCELADO, ev + ["sem servico vivo"]

    # --- Expedido na Stokki: precisa de ponta operacional + documento -----
    tipo = f.get("transportadora_tipo")
    fluxo = f.get("nucleo_fluxo")

    if tipo == "TERCEIROS":
        ev.append("transportadora=TERCEIROS")
        if f.get("comprovante_redespacho"):
            return DESTINO, REDESPACHADO, ev + ["comprovante=transportadora"]
        return DIVERGENCIA, REDESPACHO_SEM_COMPROVANTE, ev

    if tipo == "RETIRADA" or fluxo == "RETIRADA":
        ev.append("retirada")
        if f.get("comprovante_retirada"):
            return DESTINO, RETIRADO, ev + ["comprovante=retirada"]
        return DIVERGENCIA, RETIRADA_SEM_COMPROVANTE, ev

    if f.get("lalamove"):
        ev.append("rota=LALAMOVE")
        if not f.get("vuupt_sucesso") and nucleo_status != "ENTREGUE":
            return DIVERGENCIA, EXPEDIDO_SEM_ENTREGA, ev
        if f.get("comprovante_lalamove"):
            return DESTINO, LALAMOVE, ev + ["comprovante=pod"]
        return DIVERGENCIA, LALAMOVE_SEM_COMPROVANTE, ev

    entregue = bool(f.get("vuupt_sucesso")) or nucleo_status == "ENTREGUE"
    if not entregue:
        if f.get("vuupt_insucesso") or nucleo_status == "INSUCESSO":
            return DIVERGENCIA, EXPEDIDO_COM_INSUCESSO_ABERTO, ev + [f"nucleo={nucleo_status}"]
        return DIVERGENCIA, EXPEDIDO_SEM_ENTREGA, ev + [f"nucleo={nucleo_status}"]

    ev.append("entrega=" + ("vuupt done/success" if f.get("vuupt_sucesso") else "nucleo ENTREGUE"))
    if not _tem_canhoto(f):
        return DIVERGENCIA, EXPEDIDO_SEM_DOCUMENTO, ev
    ev.append(f"canhoto={f.get('canhoto_fonte')}" + (" validado" if f.get("canhoto_validado") else " sem validar"))
    if f.get("dedicado"):
        return DESTINO, DEDICADO, ev + ["pedidos_dedicados"]
    return DESTINO, ENTREGUE, ev


def servico_vivo_pendente(f: dict) -> bool:
    """Aberto na Stokki com entrega feita so e divergencia se nao ha uma
    reentrega/servico ainda rodando (ex.: -R1 em rota depois de um sucesso
    parcial). Servico vivo nao fechado = ainda em andamento."""
    return bool(f.get("servico_vivo")) and f.get("nucleo_status") in ("ABERTO", "EM_ROTA")
