# -*- coding: utf-8 -*-
"""
pipeline.py

Orquestrador principal do Agente Stokki Eventos.

Fluxo:
  1. Autentica no Stokki (cookies)
  2. Lista pedidos "Aguardando Transportador" via endpoint interno
  3. Para cada pedido:
     a. Busca o detalhe completo (5 blocos de endereço + mensagens)
     b. Resolve o endereço de entrega (hierarquia: LLM → Local de Entrega
        → Redespacho → Destino)
     c. Monta o payload do VUUPT
     d. Cria ou atualiza o serviço no VUUPT (via resolver_customer_id +
        criar_ou_atualizar_servico)
  4. Reporta resultados

Modo de execução:
    py -3.11 pipeline.py                  # processa todos os pedidos pendentes
    py -3.11 pipeline.py --modo-teste     # simula sem gravar nada no VUUPT
    py -3.11 pipeline.py --pedido PS-34345  # processa um único pedido (por código)
"""
import argparse
import logging
import re
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import yaml

# Garante que imports relativos funcionem de qualquer diretório de execução
_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
(_RAIZ / "dados").mkdir(parents=True, exist_ok=True)

from regras.embarcadores import embarcador_prioritario, resolver_stkkc_id_por_nome
from regras.endereco import resolver_endereco_entrega
from regras.transportadoras import CatalogoTransportadoras
from stokki.auth import StokkiSession
from stokki import pedidos as stokki_pedidos
from stokki.estacao_impressao import listar_pedidos_em_espera
from vuupt_client import VuuptClient, VuuptAPIError, _converter_data_para_iso
from geocodificacao import geocodificar
from historico import registrar_execucao
from email_utils import notificacoes_automaticas_ativas
from telefone_origem import indexar_xmls, telefone_correto
from regras.complexidade_entrega import carregar_niveis, classificar_nivel
from regras.clientes_agendamento import carregar_clientes_agendamento, tem_agendamento
from fingerprint_status_vuupt import ja_confirmado_atribuido, marcar_atribuido
from agendamento_confirmacao import buscar_confirmacao, enviar_solicitacao
import redespacho_confirmacao
from retiradas.regras_retirada import config_retiradas, importar_retirada, montar_payload_retirada

# Seção 'retiradas' do config.yaml, preenchida em main() -- processar_pedido
# não recebe o config inteiro e tem assinatura usada por vários chamadores.
config_retiradas_global: dict = {}

# ── Configuração de logging ────────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ / "dados" / "pipeline.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("pipeline")

# ── Constantes ─────────────────────────────────────────────────────────────────
CONFIG_PATH      = _RAIZ / "config.yaml"
TRANSPORTADORAS  = _RAIZ / "dados" / "BD_TRANSPORTADORAS.xlsx"
DB_PATH          = _RAIZ / "dados" / "dados.db"
PAUSA_ENTRE_PEDIDOS = 1.0  # segundos — ajustada 06/08: 0.3s (04/08) causou 429 em quase
# TODO pedido em produção real (confirmado pelo Hugo, lote de 241 pedidos). Na época da
# redução, só buscar_customer_por_id tinha retry contra 429 -- as outras chamadas de
# vuupt_client.py (inclusive buscar_servico_por_code, a que estava falhando) não tinham,
# então cada 429 virava erro na hora. Duas correções juntas: (1) TODAS as chamadas de
# vuupt_client.py agora têm chamar_com_retry() como rede de segurança; (2) pausa subiu
# pra 1.0s -- 429 quase constante desperdiça mais tempo esperando retry (3s+ por vez) do
# que economiza pausando menos. Ainda bem mais rápido que os 2.2s originais.

# Status confirmados (case-sensitive -- valores em lowercase ou fora
# desta lista são ignorados pelo servidor e retornam tudo sem filtro).
# Lista COMPLETA dos 5 status do funil de pedidos de saída, confirmada
# em 09/08 direto no HTML do painel outbound (data-situation="..." nos
# cards do dashboard + os mesmos 5 nomes no handler JS que popula os
# contadores) -- outros valores que a API de contagem também retorna
# ("review", "processing", "waiting_approval") NÃO são status de
# pedido de saída: pertencem à fila separada de "Pedidos de
# integração" (Vendas), confirmado pelo próprio HTML (o badge desses
# contadores fica no link pra /integration/sale, não num card deste
# dashboard) -- por isso ficam de fora daqui.
STATUS_AGUARDANDO_TRANSPORTADOR = "Waiting for Carrier"  # 277 pedidos (09/08)
STATUS_ABERTO                   = "Open"                 # 14 pedidos (09/08)
STATUS_SEPARANDO                = "Separating"           # 2 pedidos (09/08)
STATUS_PACK                     = "Ready to Pack"         # 52 pedidos (09/08)
STATUS_HOLD                     = "On hold"               # 25 pedidos (09/08)
STATUSES_EM_ABERTO              = [
    STATUS_AGUARDANDO_TRANSPORTADOR,
    STATUS_ABERTO,
    STATUS_SEPARANDO,
    STATUS_PACK,
    STATUS_HOLD,
]

# Embarcadores com regra especial: importa QUALQUER pedido em aberto,
# não só "Aguardando Transportador".
# O filtro 'cliente=' da listagem usa o ID NUMÉRICO do Stokki
# (confirmado via outbound/select — nome como texto é ignorado).
# Formato: {id_stokki: nome_legivel}
EMBARCADORES_IMPORTAR_ABERTOS: dict[str, str] = {
    "98": "COMERCIO DE CEREAIS QUATRO ESTRELAS LTDA",
    "18": "LATICINIOS DOURADO - INDUSTRIA E COMERCIO LTDA",
    "79": "JERSEY VALE AGROINDUSTRIAL LTDA",
}


def _buscar_dados_embarcador_banco(stkkc_id: int) -> dict:
    """
    Busca dados do embarcador na tabela 'interno' pelo stkkc_id: sender_id,
    apelido, habilidade (tipo de carga: Congelado/Seco/Refrigerado —
    definido pelo embarcador), cnpj_embarcador, email e notificar_email
    (usados para a solicitação de confirmação de agendamento).
    """
    padrao = {"sender_id": None, "apelido": "", "habilidade": "",
             "cnpj_embarcador": "", "email": "", "notificar_email": True,
             "fator_ponderado": 1.0}
    if not DB_PATH.exists():
        return padrao
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT sender_id, apelido, nome_remetente, habilidade, "
            "cnpj_embarcador, email, notificar_email, fator_ponderado "
            "FROM interno WHERE stkkc_id = ?",
            (stkkc_id,)
        ).fetchone()
        conn.close()
        if row:
            return {
                "sender_id":       row["sender_id"],
                "apelido":         row["nome_remetente"] or row["apelido"] or "",
                "habilidade":      (row["habilidade"] or "").strip(),
                "cnpj_embarcador": row["cnpj_embarcador"] or "",
                "email":           row["email"] or "",
                "notificar_email": bool(row["notificar_email"]) if row["notificar_email"] is not None else True,
                "fator_ponderado": float(row["fator_ponderado"]) if row["fator_ponderado"] else 1.0,
            }
    except Exception as e:
        logger.debug(f"Erro ao buscar embarcador stkkc_id={stkkc_id}: {e}")
    return padrao


def _embarcador_prioritario(linha) -> bool:
    """True se a linha pertence a um embarcador prioritario (todos os status).
    Aceita tanto o dict da linha do aaData quanto o nome do cliente ja
    extraido como string. Ver regras/embarcadores.py -- compartilhado
    com inventario.py."""
    return embarcador_prioritario(linha, EMBARCADORES_IMPORTAR_ABERTOS)


def _carregar_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ── Conversão de dados Stokki → payload VUUPT ─────────────────────────────────

# Janela padrão de entrega quando o pedido não tem horário especial
HORA_ENTREGA_PADRAO_INICIO = "08:00"
HORA_ENTREGA_PADRAO_FIM    = "16:00"

# Tipo de carga (skill do VUUPT): definido pelo EMBARCADOR, via a coluna
# 'habilidade' da tabela 'interno' (já preenchida no banco — confirmado
# com o Hugo em 28/07, não precisa de planilha nova pra isso). Aplicado
# quando o embarcador não tem 'habilidade' cadastrada (ex: embarcador
# novo, ainda não migrado pro banco) ou o valor não é um dos 3 válidos.
TIPOS_CARGA_VALIDOS = {"Congelado", "Seco", "Refrigerado"}
TIPO_CARGA_PADRAO   = "Seco"

def _proximo_dia_util(data):
    """Rola a data para frente até cair em dia útil (seg-sex)."""
    from datetime import timedelta
    while data.weekday() >= 5:  # 5=sábado, 6=domingo
        data += timedelta(days=1)
    return data


def calcular_data_entrega(data_saida_str: str):
    """
    Determina a data de agendamento da entrega a partir da data de saída
    (expedition_date da listagem Stokki, formato DD/MM/YYYY).

    Regras:
      - Data de entrega = data de saída.
      - Se a data de saída for menor que amanhã (hoje, passado, vazia ou
        inválida), usa o próximo dia útil a partir de amanhã.
      - Se a data final cair em fim de semana, rola para o próximo dia útil.

    Retorna date.
    """
    from datetime import datetime, timedelta, timezone as _tz

    # "Hoje" no fuso de Brasília (offset fixo -03:00, sem horário de verão)
    tz_brasilia = _tz(timedelta(hours=-3))
    hoje    = datetime.now(tz_brasilia).date()
    amanha  = hoje + timedelta(days=1)

    data_saida = None
    if data_saida_str:
        for formato in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                data_saida = datetime.strptime(data_saida_str.strip(), formato).date()
                break
            except ValueError:
                continue

    if data_saida is None or data_saida < amanha:
        base = amanha
    else:
        base = data_saida

    return _proximo_dia_util(base)


def montar_agendamento(data_saida_str: str, horario_entrega: dict | None):
    """
    Monta (scheduled_start, scheduled_end) em ISO8601 com offset -03:00,
    combinando a data de entrega calculada com a janela de horário:
      - horário especial do pedido (mensagens/LLM), se houver;
      - senão, janela padrão 08:00-16:00.
    """
    data_entrega = calcular_data_entrega(data_saida_str)
    data_fmt     = data_entrega.strftime("%d/%m/%Y")

    hora_inicio = HORA_ENTREGA_PADRAO_INICIO
    hora_fim    = HORA_ENTREGA_PADRAO_FIM
    if horario_entrega:
        hora_inicio = (horario_entrega.get("inicio") or "").strip() or hora_inicio
        hora_fim    = (horario_entrega.get("fim") or "").strip() or hora_fim

    return (
        _converter_data_para_iso(f"{data_fmt} {hora_inicio}"),
        _converter_data_para_iso(f"{data_fmt} {hora_fim}"),
    )


def montar_payload_vuupt(
    id_stokki: int,
    codigo_ps: str,
    referencia: str,
    detalhe: dict,
    endereco_resolvido: dict,
    skill_ids: list[int],
    horario_entrega: dict | None = None,
    sender_id: int | None = None,
    apelido_embarcador: str = "",
    data_saida: str = "",
    indice_xmls: dict | None = None,
    scheduled_start_final: str | None = None,
    scheduled_end_final: str | None = None,
    fator_ponderado: float = 1.0,
) -> dict:
    """
    Constrói o payload para criar/atualizar um servico no VUUPT.

    Titulo: PS-XXXXX - Referencia / Apelido do Embarcador - Nome do Destinatario
    """
    destino    = detalhe.get("destino") or {}
    embarcador = detalhe.get("cliente") or {}

    nome_dest  = destino.get("nome", "")
    cnpj_dest  = destino.get("documento", "")
    telefone   = destino.get("telefone", "")
    email      = destino.get("email", "")

    logradouro  = endereco_resolvido.get("logradouro", "")
    bairro      = endereco_resolvido.get("bairro", "")
    cidade      = endereco_resolvido.get("cidade", "")
    uf          = endereco_resolvido.get("uf", "")
    cep         = endereco_resolvido.get("cep", "")
    complemento = endereco_resolvido.get("complemento", "")

    partes_end = [logradouro]
    if complemento:
        partes_end.append(complemento)
    if bairro:
        partes_end.append(bairro)
    if cidade and uf:
        partes_end.append(f"{cidade} - {uf}")
    elif cidade:
        partes_end.append(cidade)
    if cep:
        partes_end.append(cep)
    partes_end.append("Brasil")
    endereco_str = ", ".join(p for p in partes_end if p)

    from vuupt_client import _normalizar_telefone_e164

    # Telefone: prioriza a fonte da verdade (XML da NF-e original) sobre
    # o campo da tela de detalhe da Stokki — confirmado com XMLs reais em
    # 27/07 que este último vem com o ÚLTIMO DÍGITO CORTADO em celulares.
    # Só usa o XML quando dá pra identificar a nota certa com confiança
    # (documento único, ou desempatado por CEP/endereço — nunca "chuta"
    # entre notas ambíguas do mesmo CNPJ/CPF). Sem confiança, cai no
    # comportamento seguro de sempre: usa o da Stokki, e se esse tiver
    # cara de celular cortado (10 díg, 6-9), vai sem telefone.
    fonte_telefone = "stokki"
    endereco_para_lookup = logradouro
    if bairro:
        endereco_para_lookup += f" {bairro}"
    if cidade:
        endereco_para_lookup += f" {cidade}"
    if uf:
        endereco_para_lookup += f" {uf}"

    if indice_xmls:
        resultado_xml = telefone_correto(cnpj_dest, endereco_para_lookup, cep, indice_xmls)
        if resultado_xml.encontrado:
            telefone = resultado_xml.fone
            fonte_telefone = resultado_xml.status  # unica | confirmada_cep | confirmada_endereco
        elif resultado_xml.status == "ambigua":
            fonte_telefone = "ambigua_xml_usou_stokki"

    telefone_e164 = _normalizar_telefone_e164(telefone) if telefone else ""

    # Titulo: #PS-XXXXX - Referencia / Apelido - Destinatario
    partes_titulo = [f"#{codigo_ps}"]
    if referencia:
        partes_titulo[0] = f"#{codigo_ps} - {referencia}"
    nome_emb = apelido_embarcador or embarcador.get("nome", "")
    if nome_emb:
        partes_titulo.append(nome_emb)
    if nome_dest:
        partes_titulo.append(nome_dest)
    titulo = " / ".join(partes_titulo) if len(partes_titulo) > 1 else partes_titulo[0]

    # Volume normalizado (dimension_3): quantidade de volumes do pedido
    # (aba "Detalhes do transporte" da Stokki — conferência do galpão,
    # com fallback pro valor da NF-e) multiplicada pelo fator_ponderado
    # do embarcador (interno.fator_ponderado, cadastrado no banco).
    # Sem quantidade informada, assume 1 volume.
    qtd_volumes_bruta = detalhe.get("quantidade_volumes") or 1
    volume_final = max(1, round(qtd_volumes_bruta * fator_ponderado))

    payload = {
        "title": titulo,
        "code":  f"#{codigo_ps}",
        "type":  "delivery",
        "dimension_3": volume_final,
        "customer": {
            "name":    nome_dest,
            "code":    cnpj_dest,
            "address": endereco_str,
        },
    }

    # Sender: usa sender_id do banco (forma leve, sem recriar o remetente)
    # Fallback: passa nome/codigo para o VUUPT resolver ou criar
    if sender_id:
        payload["sender_id"] = sender_id
    else:
        payload["sender"] = {
            "name": embarcador.get("nome", ""),
            "code": embarcador.get("documento", ""),
        }

    if telefone_e164:
        payload["phone_number"] = telefone_e164
        payload["customer"]["phone_number"] = telefone_e164

    # Marca interna (nunca vai pro VUUPT — removida em processar_pedido)
    # só pra alimentar a auditoria de onde veio o telefone.
    payload["_fonte_telefone"] = fonte_telefone

    if email:
        payload["customer"]["email"] = email

    if horario_entrega:
        hora_inicio = horario_entrega.get("inicio", "")
        hora_fim    = horario_entrega.get("fim", "")
        if hora_inicio:
            payload["customer"]["operating_hour_start"] = hora_inicio
        if hora_fim:
            payload["customer"]["operating_hour_end"] = hora_fim

    # Agendamento da entrega — SÓ preenchido quando o destinatário está
    # no cadastro de clientes com agendamento real (pedido do Hugo,
    # 29/07: estávamos agendando TODO pedido com um padrão 08:00-16:00,
    # mesmo pra quem não tem exigência de agendamento nenhuma). A
    # DECISÃO (exige agendamento? tem horário das mensagens? já foi
    # confirmado por e-mail? precisa disparar solicitação?) é feita em
    # processar_pedido — aqui só aplicamos o resultado já pronto.
    if scheduled_start_final:
        payload["scheduled_start"] = scheduled_start_final
    if scheduled_end_final:
        payload["scheduled_end"] = scheduled_end_final

    if skill_ids:
        payload["skills"] = [{"id": sid} for sid in skill_ids]

    return payload


# ── Processamento de um pedido ─────────────────────────────────────────────────

def processar_pedido(
    id_stokki: int,
    codigo_ps: str,
    sess_stokki: StokkiSession,
    catalogo: CatalogoTransportadoras,
    vuupt: VuuptClient,
    anthropic_api_key: str,
    modo_teste: bool,
    referencia: str = "",
    stkkc_id_emb: int | None = None,
    data_saida: str = "",
    google_maps_api_key: str = "",
    indice_xmls: dict | None = None,
    mapa_niveis: dict | None = None,
    conjunto_agendamento: set | None = None,
    config_email: dict | None = None,
) -> dict:
    """
    Processa um único pedido: detalhe → endereço → VUUPT.
    Retorna dict com resultado: acao, fonte_endereco, requer_revisao, erro.
    """
    resultado = {
        "codigo_ps":      codigo_ps,
        "id_stokki":      id_stokki,
        "acao":           None,
        "fonte_endereco": None,
        "fonte_telefone": None,
        "fonte_tipo_carga": None,
        "fonte_nivel_complexidade": None,
        "tem_agendamento": None,
        "fonte_agendamento": None,
        "skill_aplicada": None,
        "volume_dimension_3": None,
        "requer_revisao": False,
        "observacao":     "",
        "referencia":     referencia,
        "erro":           None,
    }

    try:
        # 0. Checagem ANTECIPADA de status no VUUPT — pedido do Hugo,
        # 31/07: evita geocodificar/resolver endereço/telefone/skill/
        # agendamento à toa pra pedidos que já saíram de 'not_assigned'
        # (atribuídos, em andamento, concluídos ou cancelados). Essa
        # checagem já existia, mas só rodava no FINAL (criar_ou_
        # atualizar_servico), depois de todo o trabalho caro já feito.
        #
        # 1ª camada: fingerprint local (instantâneo, sem rede) — uma
        # vez confirmado que saiu de not_assigned, o VUUPT nunca mais
        # volta pra esse status, então fica marcado pra sempre.
        if ja_confirmado_atribuido(codigo_ps):
            resultado["acao"] = "pulado_ja_atribuido"
            resultado["observacao"] = "Já confirmado fora de not_assigned em execução anterior (fingerprint)."
            return resultado

        # 2ª camada: se ainda não está no fingerprint local, confere
        # rápido direto no VUUPT (1 consulta, sem geocodificar nada) —
        # se já saiu de not_assigned, marca no fingerprint (pra nunca
        # mais precisar consultar de novo) e pula sem gastar mais nada.
        servico_existente = vuupt.buscar_servico_por_code(codigo_ps)
        if servico_existente and servico_existente.get("status") != vuupt.STATUS_ATUALIZAVEL:
            marcar_atribuido(codigo_ps, status=servico_existente.get("status", ""))
            resultado["acao"] = "pulado_ja_atribuido"
            resultado["observacao"] = f"Status no VUUPT: {servico_existente.get('status')} (não é mais not_assigned)."
            return resultado

        # 1. Detalhe completo do pedido
        detalhe = stokki_pedidos.obter_detalhe(sess_stokki, id_stokki)

        # 2. Resolução do endereço
        res_end = resolver_endereco_entrega(
            detalhe, catalogo, anthropic_api_key, google_maps_api_key)

        # Pedidos com transportadora RETIRADA (cliente retira / terceira
        # coleta no galpão): SERVIÇO AVULSO na VUUPT, título "[RETIRADA]",
        # atribuído ao agente fixo de retiradas -- nunca entra em rota
        # (pedido do Hugo, 28/08; ver retiradas/regras_retirada.py). Sem
        # retiradas.agent_id no config, mantém o comportamento antigo
        # (ignora). Quem fecha/cancela depois é acompanhar_retiradas.py.
        if res_end.transportadora.get("tipo") == "RETIRADA":
            cfg_ret = config_retiradas(config_retiradas_global or {})
            if not cfg_ret["ativo"]:
                logger.info(
                    f"  {codigo_ps}: RETIRADA ({res_end.transportadora.get('nome')}) "
                    f"— ignorado, nao importa no VUUPT (retiradas desativadas no config)."
                )
                resultado["acao"] = "ignorado_retirada"
                return resultado
            stkkc_id_final = stkkc_id_emb or detalhe.get("stkkc_id")
            dados_banco = _buscar_dados_embarcador_banco(stkkc_id_final) if stkkc_id_final else {}
            transp_bloco = detalhe.get("transportadora") or {}
            transp_info = {"nome": res_end.transportadora.get("nome") or transp_bloco.get("nome", ""),
                           "documento": transp_bloco.get("documento", "")}
            payload_ret = montar_payload_retirada(
                codigo_ps, referencia or detalhe.get("referencia", ""), detalhe,
                transp_info, dados_banco, cfg_ret, data_saida=data_saida)
            acao_ret, _ = importar_retirada(vuupt, payload_ret, cfg_ret, servico_existente, modo_teste)
            resultado["acao"] = acao_ret
            resultado["fonte_endereco"] = "retirada_galpao"
            resultado["observacao"] = f"Retirada no galpão via {transp_info['nome'] or 'cliente'} -- serviço avulso, sem rota."
            return resultado

        # Local de Entrega fora da área atendida e sem redespacho conhecido
        # pra transportadora (pedido do Hugo, 10/08, caso #PS-36198): segura
        # o pedido (não importa no VUUPT) e notifica o embarcador pedindo
        # nome da transportadora + endereço de redespacho.
        if res_end.fonte == "aguardando_redespacho":
            resultado["fonte_endereco"] = res_end.fonte
            resultado["requer_revisao"] = True
            resultado["observacao"]     = res_end.observacao
            stkkc_id_final = stkkc_id_emb or detalhe.get("stkkc_id")
            dados_banco = _buscar_dados_embarcador_banco(stkkc_id_final) if stkkc_id_final else {}
            local_entrega = detalhe.get("local_entrega") or {}
            if modo_teste:
                logger.info(
                    f"  [TESTE] {codigo_ps}: exigiria solicitação de confirmação de "
                    f"redespacho (e-mail NÃO enviado em modo teste) — {res_end.observacao}"
                )
            elif dados_banco.get("notificar_email", True) and dados_banco.get("email"):
                enviado, motivo_email = redespacho_confirmacao.enviar_solicitacao(
                    pedido=codigo_ps,
                    nome_dest=detalhe.get("destino", {}).get("nome", ""),
                    cidade=local_entrega.get("cidade", ""),
                    uf=local_entrega.get("uf", ""),
                    nome_transportadora=res_end.transportadora.get("nome", ""),
                    email_emb=dados_banco.get("email", ""),
                    config_email=config_email or {},
                )
                if enviado:
                    logger.info(f"  {codigo_ps}: solicitação de confirmação de redespacho enviada.")
                elif motivo_email:
                    logger.debug(
                        f"  {codigo_ps}: solicitação de redespacho não enviada ({motivo_email})."
                    )
            resultado["acao"] = "aguardando_redespacho"
            return resultado

        resultado["fonte_endereco"] = res_end.fonte
        resultado["requer_revisao"] = res_end.requer_revisao
        resultado["observacao"]     = res_end.observacao

        if res_end.requer_revisao:
            logger.warning(
                f"  {codigo_ps}: requer revisão manual — {res_end.observacao}"
            )
        if res_end.horario_entrega:
            logger.info(
                f"  {codigo_ps}: horário de entrega detectado via mensagem — "
                f"{res_end.horario_entrega.get('inicio')} às {res_end.horario_entrega.get('fim')}"
                f" ({res_end.horario_entrega.get('observacao', '')})"
            )

        # 3. Monta payload do VUUPT
        # Referencia: vem da linha do aaData (modo lote) ou do detalhe (modo --pedido)
        ref_final = referencia or detalhe.get("referencia", "")
        stkkc_id_final = stkkc_id_emb or detalhe.get("stkkc_id")
        dados_banco = _buscar_dados_embarcador_banco(stkkc_id_final) if stkkc_id_final else {}

        # Skill do serviço: "{TipoCarga}-{Nível}". Tipo de carga
        # (Congelado/Seco/Refrigerado) é definido pelo EMBARCADOR — vem
        # direto de interno.habilidade (já confiável no banco, confirmado
        # com o Hugo, sem precisar de planilha). Nível de complexidade
        # (1-4) é do DESTINATÁRIO, via planilha (regras/complexidade_entrega).
        # Substitui o placeholder anterior (todas as skills da conta em
        # todo serviço). Em modo_teste, mantém o comportamento já
        # existente de não consultar a API (skill_ids vazio) — só a
        # classificação em si é resolvida, pra aparecer no log/auditoria.
        habilidade_bruta  = (dados_banco.get("habilidade", "") or "").strip().capitalize()
        tipo_carga        = habilidade_bruta if habilidade_bruta in TIPOS_CARGA_VALIDOS else TIPO_CARGA_PADRAO
        tipo_carga_encontrado = habilidade_bruta in TIPOS_CARGA_VALIDOS

        cnpj_destino = detalhe.get("destino", {}).get("documento", "")
        nivel, nivel_encontrado, nivel_requer_revisao = classificar_nivel(
            cnpj_destino, mapa_niveis or {})

        nome_skill = f"{tipo_carga}-{nivel}"
        skill_ids  = vuupt.skill_ids_por_nome(nome_skill) if not modo_teste else []

        resultado["skill_aplicada"]             = nome_skill
        resultado["fonte_tipo_carga"]           = "embarcador" if tipo_carga_encontrado else "padrao"
        resultado["fonte_nivel_complexidade"]   = "planilha" if nivel_encontrado else "padrao"

        if not tipo_carga_encontrado:
            logger.info(
                f"  {codigo_ps}: embarcador (stkkc_id={stkkc_id_final}) sem 'habilidade' "
                f"válida cadastrada em interno — aplicando tipo de carga padrão ({TIPO_CARGA_PADRAO})."
            )
        if nivel_requer_revisao:
            # CNPJ sem classificação na planilha: segue com nível padrão
            # (2) provisório, mas marcado para revisão manual (pedido do
            # Hugo, 10/08) — diferente de CPF, que aplica o padrão (1)
            # direto, sem exigir revisão.
            resultado["requer_revisao"] = True
            obs_nivel = (
                f"CNPJ do destinatário sem classificação de nível na planilha "
                f"de complexidade — aplicando nível padrão ({nivel}) provisoriamente, "
                f"requer classificação manual."
            )
            resultado["observacao"] = (
                f"{resultado['observacao']} {obs_nivel}".strip()
                if resultado["observacao"] else obs_nivel
            )
            logger.info(f"  {codigo_ps}: {obs_nivel}")
        elif not nivel_encontrado:
            logger.info(
                f"  {codigo_ps}: CPF do destinatário não encontrado na planilha "
                f"de complexidade — aplicando nível padrão ({nivel})."
            )

        # Agendamento — só entra no payload quando o destinatário exige
        # (cadastro clientes_agendamento) e, quando exige, só com um
        # horário de VERDADE: das mensagens da Stokki (prioridade,
        # já veio confiável de res_end); senão, de uma confirmação já
        # recebida por e-mail; senão, dispara a solicitação de
        # confirmação e o pedido fica SEM agendamento por enquanto
        # (pedido do Hugo, 29/07).
        scheduled_start_final = None
        scheduled_end_final = None
        fonte_agendamento = "nao_exigido"
        cnpj_destino_bruto = detalhe.get("destino", {}).get("documento", "")

        if tem_agendamento(cnpj_destino_bruto, conjunto_agendamento or set()):
            if res_end.horario_entrega:
                scheduled_start_final, scheduled_end_final = montar_agendamento(
                    data_saida, res_end.horario_entrega)
                fonte_agendamento = "mensagem_stokki"
            else:
                confirmacao = buscar_confirmacao(codigo_ps)
                if confirmacao:
                    scheduled_start_final = _converter_data_para_iso(
                        f"{confirmacao['data']} {confirmacao['inicio']}")
                    scheduled_end_final = _converter_data_para_iso(
                        f"{confirmacao['data']} {confirmacao['fim']}")
                    fonte_agendamento = "confirmado_email"
                else:
                    fonte_agendamento = "aguardando_confirmacao"
                    resultado["requer_revisao"] = True
                    if modo_teste:
                        logger.info(
                            f"  [TESTE] {codigo_ps}: exigiria solicitação de confirmação "
                            f"de agendamento (e-mail NÃO enviado em modo teste)."
                        )
                    elif dados_banco.get("notificar_email", True) and dados_banco.get("email"):
                        enviado, motivo_email = enviar_solicitacao(
                            pedido=codigo_ps,
                            nome_dest=detalhe.get("destino", {}).get("nome", ""),
                            numero_nf=ref_final,
                            cnpj_dest=cnpj_destino_bruto,
                            cnpj_emb=dados_banco.get("cnpj_embarcador", ""),
                            email_emb=dados_banco.get("email", ""),
                            config_email=config_email or {},
                        )
                        if enviado:
                            logger.info(f"  {codigo_ps}: solicitação de confirmação de agendamento enviada.")
                        elif motivo_email:
                            logger.debug(
                                f"  {codigo_ps}: solicitação de agendamento não enviada ({motivo_email})."
                            )
                    else:
                        logger.debug(
                            f"  {codigo_ps}: exige agendamento mas embarcador sem e-mail/"
                            f"notificação desativada — aguardando confirmação manual."
                        )

        resultado["tem_agendamento"] = fonte_agendamento != "nao_exigido"
        resultado["fonte_agendamento"] = fonte_agendamento

        payload = montar_payload_vuupt(
            id_stokki=id_stokki,
            codigo_ps=codigo_ps,
            referencia=ref_final,
            detalhe=detalhe,
            endereco_resolvido=res_end.endereco,
            horario_entrega=res_end.horario_entrega,
            skill_ids=skill_ids,
            sender_id=dados_banco.get("sender_id"),
            apelido_embarcador=dados_banco.get("apelido", ""),
            data_saida=data_saida,
            indice_xmls=indice_xmls,
            scheduled_start_final=scheduled_start_final,
            scheduled_end_final=scheduled_end_final,
            fator_ponderado=dados_banco.get("fator_ponderado", 1.0),
        )
        resultado["fonte_telefone"] = payload.pop("_fonte_telefone", "stokki")
        resultado["volume_dimension_3"] = payload.get("dimension_3")

        # Regras de dia fixo de entrega (regioes_dia_fixo.py) valem também
        # pra data que já chega PRONTA na criação -- horário das mensagens
        # da Stokki ou confirmação por e-mail (pedido do Hugo, 13/08:
        # antes essas datas entravam às cegas; um pedido de Sorocaba
        # confirmado pra sexta ficava pra sexta, e Sorocaba só recebe às
        # terças). Se a data cai num dia sem entrega na região/galpão,
        # empurra pra próxima válida; o main() avisa o remetente no mesmo
        # e-mail do agendamento por dia fixo. Só notifica quando o VUUPT
        # ainda não tem essa data (servico_existente) -- o pipeline
        # reprocessa pedidos not_assigned toda rodada, e sem essa checagem
        # o remetente receberia o mesmo aviso em toda execução.
        if payload.get("scheduled_start"):
            try:
                from roteirizacao.regioes_dia_fixo import ajustar_data_por_dia_fixo, nomes_dias
                data_original = date.fromisoformat(payload["scheduled_start"][:10])
                data_final, regra = ajustar_data_por_dia_fixo(
                    {"address": payload.get("customer", {}).get("address", "")}, data_original)
                if regra:
                    payload["scheduled_start"] = data_final.isoformat() + payload["scheduled_start"][10:]
                    if payload.get("scheduled_end"):
                        payload["scheduled_end"] = data_final.isoformat() + payload["scheduled_end"][10:]
                    logger.info(
                        f"  {codigo_ps}: agendamento {data_original.strftime('%d/%m')} cai fora dos dias de "
                        f"'{regra['nome']}' ({nomes_dias(regra['dias'])}) — ajustado pra "
                        f"{data_final.strftime('%d/%m/%Y')}."
                    )
                    data_ja_no_vuupt = ((servico_existente or {}).get("scheduled_start") or "")[:10]
                    if data_ja_no_vuupt != data_final.isoformat():
                        resultado["ajuste_dia_fixo"] = {
                            "code":  codigo_ps,
                            "title": payload.get("title", ""),
                            "sender_id": payload.get("sender_id"),
                            "regiao": regra["nome"],
                            "dias":  regra["dias"],
                            "data":  data_final.isoformat(),
                            "data_original": data_original.isoformat(),
                        }
            except Exception as e:
                logger.warning(f"  {codigo_ps}: falha ao validar dia fixo do agendamento "
                               f"(segue com a data original): {e}")

        # 3b. Geocodificação do endereço de entrega — lat/long entram em
        # DOIS níveis do payload (confirmado via debug/testar_coords_servico):
        #   - no serviço (topo): o pino do mapa usa a coordenada própria do
        #     serviço (campos latitude/longitude, que ficavam null);
        #   - no customer: alimenta o upsert do contato via
        #     resolver_customer_id, o fingerprint e a forma leve/completa.
        endereco_payload = payload.get("customer", {}).get("address", "")
        coords = geocodificar(endereco_payload, google_maps_api_key)
        if coords:
            payload["latitude"]  = coords[0]
            payload["longitude"] = coords[1]
            payload["customer"]["latitude"]  = coords[0]
            payload["customer"]["longitude"] = coords[1]
            logger.info(
                f"  {codigo_ps}: geocodificado ({coords[0]:.6f}, {coords[1]:.6f})"
            )
        else:
            logger.info(
                f"  {codigo_ps}: sem coordenadas — VUUPT geocodificará por conta própria."
            )

        # 4. Cria ou atualiza no VUUPT
        if modo_teste:
            logger.info(f"  {codigo_ps}: [TESTE] payload montado, fonte={res_end.fonte}")
            resultado["acao"] = "simulado"
        else:
            resposta_vuupt, acao = vuupt.criar_ou_atualizar_servico(payload)
            resultado["acao"] = acao
            logger.info(
                f"  {codigo_ps}: {acao} no VUUPT (endereço via {res_end.fonte})"
            )
            # Dual-write no núcleo próprio (Fase A do app de motoristas,
            # DOC_EXECUCAO_CLAUDE_APP_MOTORISTAS.md) -- best-effort: a VUUPT
            # já foi atualizada, falha aqui só vira aviso.
            try:
                from nucleo.pedidos import registrar_importacao
                registrar_importacao(payload, resposta_vuupt, acao)
            except Exception as e_nucleo:
                logger.warning(f"  {codigo_ps}: não espelhado no núcleo próprio ({e_nucleo}) -- VUUPT já atualizada, segue.")

    except Exception as e:
        resultado["erro"] = str(e)
        logger.error(f"  {codigo_ps}: erro — {e}")

    return resultado


# ── Orquestrador principal ─────────────────────────────────────────────────────

def main(modo_teste: bool = False, filtro_pedido: str = "", filtro_embarcador: str = "",
         filtro_statuses: list[str] | None = None):
    inicio_execucao = time.monotonic()
    logger.info(
        f"{'[MODO TESTE] ' if modo_teste else ''}Pipeline iniciado."
    )

    config           = _carregar_config()
    global config_retiradas_global
    config_retiradas_global = config
    anthropic_key    = config.get("anthropic", {}).get("api_key", "")
    vuupt_token      = config.get("vuupt_api", {}).get("token", "")
    gmaps_key        = config.get("google_maps", {}).get("api_key", "")

    # Índice de XMLs de NF-e (fonte da verdade do telefone do destinatário
    # — a Stokki corta o último dígito de celulares). Caminho configurável
    # em config.yaml: importacao_email.pasta_xmls_processados (aponta pra
    # pasta onde o outro projeto, agente_importacao_stokki, arquiva os XMLs
    # já importados). Se ausente/pasta não existe, índice fica vazio e o
    # pipeline cai no comportamento seguro de sempre (telefone da Stokki,
    # ou vazio se tiver cara de cortado).
    pasta_xmls = config.get("importacao_email", {}).get("pasta_xmls_processados", "")
    indice_xmls = indexar_xmls(pasta_xmls) if pasta_xmls else {}

    # Nível de complexidade de entrega (destinatário) — o tipo de carga
    # (Congelado/Seco/Refrigerado) vem do embarcador, direto do banco
    # (interno.habilidade, já confiável — ver _buscar_dados_embarcador_banco),
    # não de planilha. Caminho configurável em config.yaml:
    # complexidade_entrega.planilha. Ausente/não encontrada: {} — todo
    # destinatário cai no nível padrão (1), sem travar o pipeline.
    caminho_niveis = config.get("complexidade_entrega", {}).get("planilha", "")
    mapa_niveis = carregar_niveis(caminho_niveis)

    # Cadastro de clientes (destinatário, por CNPJ/CPF) que exigem
    # agendamento real de entrega. Caminho configurável em config.yaml:
    # clientes_agendamento.planilha. Ausente/não encontrada: set() —
    # nenhum pedido recebe agendamento (comportamento seguro por
    # padrão — não assume agendamento pra ninguém sem o cadastro).
    caminho_agendamento = config.get("clientes_agendamento", {}).get("planilha", "")
    conjunto_agendamento = carregar_clientes_agendamento(caminho_agendamento)

    if not vuupt_token:
        raise SystemExit("Token do VUUPT não configurado em config.yaml (vuupt_api.token).")

    # Carrega catálogo de transportadoras
    catalogo = CatalogoTransportadoras.carregar(TRANSPORTADORAS)
    conflitos = catalogo.listar_conflitos()
    if conflitos:
        logger.warning(
            f"BD_TRANSPORTADORAS tem {len(conflitos)} conflito(s) a resolver: {conflitos}"
        )

    # Resolve filtro de embarcador (stkkc_id ou parte do nome)
    filtro_stkkc_id: int | None = None
    filtro_cliente_api: str = ""  # ID para o parametro client= da listagem
    if filtro_embarcador:
        if filtro_embarcador.isdigit():
            filtro_stkkc_id = int(filtro_embarcador)
            filtro_cliente_api = filtro_embarcador
            logger.info(f"Filtro de embarcador: stkkc_id={filtro_stkkc_id}")
        else:
            # Busca por nome no banco (regras/embarcadores.py -- compartilhado
            # com inventario.py)
            stkkc_id_encontrado = resolver_stkkc_id_por_nome(filtro_embarcador, DB_PATH)
            if stkkc_id_encontrado is not None:
                filtro_stkkc_id = stkkc_id_encontrado
                filtro_cliente_api = str(stkkc_id_encontrado)
                logger.info(f"Filtro de embarcador: '{filtro_embarcador}' -> stkkc_id={filtro_stkkc_id}")
            else:
                raise SystemExit(f"Embarcador '{filtro_embarcador}' não encontrado no banco.")

    # Inicia sessoes
    sess_stokki = StokkiSession(config)
    vuupt = VuuptClient(vuupt_token)

    # Lista pedidos
    # ── Coleta pedidos das fontes relevantes ──────────────────────────────────
    if filtro_pedido:
        # Modo pedido único: extrai o ID numérico do código e vai direto
        # pro show/{id} — a busca por texto no endpoint de listagem não
        # funciona por código de pedido (confirmado via debug_busca.py).
        import re as _re
        codigo = filtro_pedido.lstrip("#").upper()
        # Primeira sequência de dígitos (não a última): 'PS-36327-R1'
        # com r'(\d+)$' viraria id 1 -- pedido errado na Stokki.
        m = _re.search(r"(\d+)", codigo)
        if not m:
            raise SystemExit(f"Não foi possível extrair ID numérico de: {filtro_pedido!r}")
        id_stokki = int(m.group(1))
        logger.info(f"Pedido específico: {codigo} (id={id_stokki})")
        linhas = [{"id": f'<a href="/show/{id_stokki}">{id_stokki}</a>',
                   "destination": "", "carrier": "", "client": "",
                   "_codigo_ps": codigo}]
    elif filtro_embarcador:
        # Modo embarcador específico: busca os status abertos para esse embarcador
        # (todos os 5 por padrão, ou só os informados via filtro_statuses)
        statuses_busca = filtro_statuses or STATUSES_EM_ABERTO
        linhas_ids_vistos: set[int] = set()
        linhas: list = []
        for status in statuses_busca:
            try:
                for linha in stokki_pedidos.iterar_todos_pedidos(
                    sess_stokki, status=status, cliente=filtro_cliente_api, pausa_entre_paginas=0.3
                ):
                    id_ = stokki_pedidos.extrair_id_da_linha(linha)
                    if id_ and id_ not in linhas_ids_vistos:
                        linhas_ids_vistos.add(id_)
                        linhas.append(linha)
            except Exception as e:
                logger.warning(f"Erro ao buscar embarcador status={status!r}: {e}")
        logger.info(
            f"Embarcador stkkc_id={filtro_stkkc_id}: {len(linhas)} pedido(s) em aberto."
        )
    else:
        linhas_ids_vistos: set[int] = set()
        linhas: list = []

        # Fonte 1: embarcadores prioritários — todos os pedidos abertos
        for id_emb, nome_emb in EMBARCADORES_IMPORTAR_ABERTOS.items():
            for status in STATUSES_EM_ABERTO:
                try:
                    for linha in stokki_pedidos.iterar_todos_pedidos(
                        sess_stokki, status=status, cliente=id_emb, pausa_entre_paginas=0.3
                    ):
                        id_ = stokki_pedidos.extrair_id_da_linha(linha)
                        if id_ and id_ not in linhas_ids_vistos:
                            linhas_ids_vistos.add(id_)
                            if isinstance(linha, dict):
                                linha["_fonte"] = "prioritario"
                            linhas.append(linha)
                except Exception as e:
                    logger.warning(f"Erro ao buscar {nome_emb!r} status={status!r}: {e}")
        logger.info(f"Embarcadores prioritários: {len(linhas)} pedido(s) coletado(s).")

        # Fonte 2: todos os outros — apenas Aguardando Transportador
        n_antes = len(linhas)
        for linha in stokki_pedidos.iterar_todos_pedidos(
            sess_stokki, status=STATUS_AGUARDANDO_TRANSPORTADOR
        ):
            id_ = stokki_pedidos.extrair_id_da_linha(linha)
            nome_cli = re.sub(r"<[^>]+>", "", str(
                linha.get("client", "") if isinstance(linha, dict) else ""
            )).strip()
            if id_ and id_ not in linhas_ids_vistos:
                if not _embarcador_prioritario(nome_cli):
                    linhas_ids_vistos.add(id_)
                    if isinstance(linha, dict):
                        linha["_fonte"] = "aguardando_transportador"
                    linhas.append(linha)
        logger.info(f"Aguardando Transportador (outros embarcadores): "
                    f"{len(linhas) - n_antes} pedido(s) adicionados.")

        # Fonte 3: Estação de Impressão — pedidos em espera
        n_antes2 = len(linhas)
        try:
            em_espera = listar_pedidos_em_espera(config)
            for p in em_espera:
                id_ = p["id_stokki"]
                if id_ and id_ not in linhas_ids_vistos:
                    linhas_ids_vistos.add(id_)
                    # Converte para o formato de linha esperado pelo pipeline
                    linhas.append({
                        "id": f'<a href="/show/{id_}">{id_}</a>',
                        "client": p["cliente"],
                        "carrier": "",
                        "destination": "",
                        "_codigo_ps": p["codigo_ps"].lstrip("#"),
                        "_fonte": "estacao_impressao",
                    })
        except Exception as e:
            logger.warning(f"Erro ao buscar pedidos da Estação de Impressão: {e}")
        logger.info(f"Estação de Impressão: {len(linhas) - n_antes2} pedido(s) adicionados.")

        logger.info(f"Total: {len(linhas)} pedido(s) a processar.")

    if not linhas:
        logger.info("Nenhum pedido encontrado.")
        registrar_execucao(
            modo_teste=modo_teste,
            filtro=filtro_pedido or filtro_embarcador or "",
            resultados=[],
            duracao_seg=time.monotonic() - inicio_execucao,
        )
        return

    # Processa
    resultados = []
    for i, linha in enumerate(linhas, 1):
        id_stokki  = stokki_pedidos.extrair_id_da_linha(linha)
        codigo_ps  = stokki_pedidos.extrair_codigo_ps_da_linha(linha)
        referencia = stokki_pedidos.extrair_referencia_da_linha(linha)

        # Data de saída (expedition_date) — vem limpa (DD/MM/YYYY) na
        # listagem; ausente nas linhas sintéticas (--pedido, Estação de
        # Impressão), caso em que o agendamento cai no próximo dia útil.
        data_saida = ""
        if isinstance(linha, dict):
            data_saida = re.sub(r"<[^>]+>", "", str(linha.get("expedition_date", ""))).strip()

        # stkkc_id do embarcador para lookup no banco
        stkkc_id_emb = None
        if isinstance(linha, dict):
            client_html = str(linha.get("client", ""))
            m = re.search(r"#stkkc-(\d+)", client_html)
            if m:
                stkkc_id_emb = int(m.group(1))

        if not id_stokki:
            logger.warning(f"  Linha {i}: não foi possível extrair ID — pulando.")
            continue

        logger.info(f"[{i}/{len(linhas)}] {codigo_ps} (id={id_stokki})"
                    + (f" ref={referencia}" if referencia else ""))

        res = processar_pedido(
            id_stokki=id_stokki,
            codigo_ps=codigo_ps,
            sess_stokki=sess_stokki,
            catalogo=catalogo,
            vuupt=vuupt,
            anthropic_api_key=anthropic_key,
            modo_teste=modo_teste,
            referencia=referencia,
            stkkc_id_emb=stkkc_id_emb,
            data_saida=data_saida,
            google_maps_api_key=gmaps_key,
            indice_xmls=indice_xmls,
            mapa_niveis=mapa_niveis,
            conjunto_agendamento=conjunto_agendamento,
            config_email=config.get("email", {}),
        )
        res["fonte_coleta"] = (
            linha.get("_fonte", "manual") if isinstance(linha, dict) else "manual"
        )
        resultados.append(res)

        if i < len(linhas):
            time.sleep(PAUSA_ENTRE_PEDIDOS)

    # Resumo
    ok      = [r for r in resultados if not r["erro"]]
    erros   = [r for r in resultados if r["erro"]]
    revisao = [r for r in ok if r["requer_revisao"]]

    logger.info("=" * 60)
    logger.info(f"RESUMO: {len(resultados)} processado(s)")
    logger.info(f"  OK: {len(ok)} | Erros: {len(erros)} | Revisão manual: {len(revisao)}")

    fontes = {}
    for r in ok:
        f = r["fonte_endereco"] or "desconhecida"
        fontes[f] = fontes.get(f, 0) + 1
    for fonte, qtd in sorted(fontes.items()):
        logger.info(f"  Fonte '{fonte}': {qtd}")

    if erros:
        logger.warning(f"Pedidos com erro:")
        for r in erros:
            logger.warning(f"  {r['codigo_ps']}: {r['erro']}")
    if revisao:
        logger.warning(f"Pedidos que requerem revisão manual:")
        for r in revisao:
            logger.warning(f"  {r['codigo_ps']}: {r['observacao'][:80]}")

    # Datas movidas pelo dia fixo na criação (pedido do Hugo, 13/08):
    # avisa os remetentes no mesmo e-mail do agendamento por dia fixo
    # (1 e-mail por remetente, com data original e data ajustada).
    ajustes_dia_fixo = [r["ajuste_dia_fixo"] for r in resultados if r.get("ajuste_dia_fixo")]
    if ajustes_dia_fixo and notificacoes_automaticas_ativas(config):
        try:
            from roteirizacao.notificar_agendamento_dia_fixo import notificar_agendamentos_dia_fixo
            itens = [{
                "servico": {"code": a["code"], "title": a["title"], "sender_id": a["sender_id"]},
                "regiao": a["regiao"],
                "dias":   a["dias"],
                "data":   date.fromisoformat(a["data"]),
                "data_original": date.fromisoformat(a["data_original"]),
            } for a in ajustes_dia_fixo]
            resultado_aviso = notificar_agendamentos_dia_fixo(
                itens, config.get("email", {}), modo_teste=modo_teste)
            logger.info(f"Notificação de ajuste por dia fixo: {resultado_aviso}")
        except Exception as e:
            logger.warning(f"Falha ao notificar ajustes de dia fixo (não afeta a importação): {e}")

    # Auditoria da execução: JSONL + CSV por pedido + STATUS_IMPORTACAO.md
    registrar_execucao(
        modo_teste=modo_teste,
        filtro=filtro_pedido or filtro_embarcador or "",
        resultados=resultados,
        duracao_seg=time.monotonic() - inicio_execucao,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agente Stokki Eventos — Pipeline principal")
    parser.add_argument("--modo-teste", action="store_true",
                        help="Simula sem gravar nada no VUUPT")
    parser.add_argument("--pedido", metavar="PS-XXXXX",
                        help="Processa somente um pedido específico")
    parser.add_argument("--embarcador", metavar="ID_OU_NOME",
                        help="Processa somente pedidos de um embarcador (stkkc_id ou parte do nome)")
    parser.add_argument("--status", action="append", metavar="STATUS",
                        choices=STATUSES_EM_ABERTO,
                        help="Restringe a busca (só com --embarcador) a status específico(s) do "
                             "Stokki. Repita a flag para vários. Padrão: todos os 5 status abertos. "
                             f"Valores válidos: {', '.join(STATUSES_EM_ABERTO)}")
    args = parser.parse_args()

    main(modo_teste=args.modo_teste, filtro_pedido=args.pedido or "",
         filtro_embarcador=args.embarcador or "", filtro_statuses=args.status)
