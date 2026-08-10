# -*- coding: utf-8 -*-
"""
regras/endereco.py

Resolução do endereço de entrega final de um pedido, aplicando a
hierarquia de prioridade definida:

  1. Mensagens (LLM) — se houver mensagem com instrução de endereço
     alternativo ou transportadora diferente, prevalece sobre tudo.
  2. Local de Entrega — campo formal preenchido no pedido.
  3. Redespacho por Transportadora — se a transportadora for TERCEIROS,
     o endereço é substituído pelo endereço fixo da planilha.
  4. Destino — endereço do destinatário final (fallback padrão).

Uso:
    from regras.endereco import resolver_endereco_entrega
    from regras.transportadoras import CatalogoTransportadoras

    catalogo = CatalogoTransportadoras.carregar(Path("dados/BD_TRANSPORTADORAS.xlsx"))
    resultado = resolver_endereco_entrega(detalhe_pedido, catalogo, anthropic_api_key)

    print(resultado.endereco)   # dict com campos de endereço
    print(resultado.fonte)      # de onde veio: "mensagem_llm" | "local_entrega" |
                                #               "redespacho" | "destino"
    if resultado.requer_revisao:
        # LLM extraiu algo mas com baixa confiança — revisar manualmente
        print(resultado.observacao)
"""
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import requests

from regras.transportadoras import CatalogoTransportadoras, ResultadoResolucao

logger = logging.getLogger(__name__)

FonteEndereco = Literal[
    "mensagem_llm", "local_entrega", "redespacho", "destino", "aguardando_redespacho",
]

_ROTEIRIZACAO_DIR = Path(__file__).resolve().parent.parent / "roteirizacao"

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"

UFS_VALIDAS = {
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT",
    "MS", "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO",
    "RR", "SC", "SP", "SE", "TO",
}


def _validar_endereco_plausivel(endereco: dict) -> tuple[bool, str]:
    """
    Checagem mínima de plausibilidade do endereço extraído pelo LLM das
    mensagens, ANTES de aceitá-lo como endereço final de entrega.

    Sem isso, uma extração ruim (mensagem ambígua, contexto confuso,
    conversa sobre outro assunto) pode virar endereço de entrega com
    informação essencialmente aleatória — confirmado pelo Hugo em
    produção (28/07). A "confiança" autorreportada pelo LLM não basta
    sozinha: uma extração pode vir marcada "alta" e ainda assim ser
    estruturalmente inválida.

    Critérios (propositalmente simples e determinísticos, não mais IA
    julgando IA): logradouro e cidade não vazios e com pelo menos uma
    letra (barra endereço vazio/só número/lixo tipo "-"); UF precisa
    ser uma das 27 siglas reais; CEP, se vier preenchido, precisa ter
    8 dígitos (não exige CEP — mensagem de cliente nem sempre inclui).

    Retorna (válido, motivo) — motivo preenchido só quando válido=False,
    pronto para log/observação.
    """
    logradouro = str(endereco.get("logradouro") or "").strip()
    cidade     = str(endereco.get("cidade") or "").strip()
    uf         = str(endereco.get("uf") or "").strip().upper()
    cep_bruto  = str(endereco.get("cep") or "").strip()
    cep_digitos = "".join(c for c in cep_bruto if c.isdigit())

    if len(logradouro) < 4 or not any(c.isalpha() for c in logradouro):
        return False, f"logradouro ausente/curto demais ({logradouro!r})"
    if len(cidade) < 2 or not any(c.isalpha() for c in cidade):
        return False, f"cidade ausente/inválida ({cidade!r})"
    if uf not in UFS_VALIDAS:
        return False, f"UF inválida ({uf!r})"
    # cep_bruto vazio = não informado (aceitável); não-vazio mas sem 8
    # dígitos = informado só que inválido/lixo (ex: "abc") — rejeita.
    if cep_bruto and len(cep_digitos) != 8:
        return False, f"CEP com formato inválido ({endereco.get('cep')!r})"

    return True, ""

# Prompt de sistema para extração de dados de entrega das mensagens
_PROMPT_SISTEMA = """
Você é um assistente especializado em logística. Sua tarefa é analisar
mensagens de texto livre trocadas entre operadores e clientes de uma
empresa de logística e extrair, se houver, instruções de entrega
diferentes do padrão cadastrado.

Responda APENAS com um objeto JSON válido, sem markdown, sem explicação.
Estrutura esperada:
{
  "tem_instrucao_entrega": true | false,
  "confianca": "alta" | "media" | "baixa",
  "transportadora_alternativa": "NOME DA TRANSPORTADORA ou null",
  "endereco": {
    "logradouro": "...",
    "numero": "...",
    "complemento": "...",
    "bairro": "...",
    "cidade": "...",
    "uf": "XX",
    "cep": "..."
  } | null,
  "horario_entrega": {
    "inicio": "HH:MM",
    "fim": "HH:MM",
    "observacao": "texto original mencionado"
  } | null,
  "observacao": "explicação breve do que foi encontrado ou por que não há instrução"
}

Regras:
- tem_instrucao_entrega = true SOMENTE se houver instrução clara de
  endereço alternativo, transportadora diferente OU horário específico
  de entrega.
- Mensagens genéricas ("ok", "confirmo", "recebido") → false.
- Quando o endereço for parcial (ex: só nome do galpão), extraia o que
  houver e marque confianca = "baixa".
- Se mencionar transportadora mas não endereço, preencha
  transportadora_alternativa e deixe endereco = null.
- Para horário: extraia qualquer janela de entrega mencionada
  (ex: "das 8h às 12h", "somente após 14h", "antes das 10h",
  "entre 9 e 17h", "manhã", "tarde"). Se só houver um horário
  limite, use-o como "fim" e deixe "inicio" como "00:00".
  Se for período vago ("manhã"), use inicio="08:00" fim="12:00",
  ("tarde") use inicio="13:00" fim="18:00".
- horario_entrega = null se nenhum horário for mencionado.
""".strip()


@dataclass
class ResultadoEndereco:
    """
    Resultado completo da resolução de endereço de entrega.

    endereco:          dict com campos de endereço (mesmo formato do bloco "Destino")
    fonte:             de onde veio o endereço final
    transportadora:    dict com tipo + nome (se relevante)
    horario_entrega:   dict {"inicio": "HH:MM", "fim": "HH:MM", "observacao": str}
                       quando o LLM detectar janela de entrega nas mensagens, None caso contrário
    requer_revisao:    True quando LLM extraiu algo com confiança baixa
    observacao:        detalhes adicionais (ex: conflito de transportadora,
                       baixa confiança do LLM, transportadora desconhecida)
    """
    endereco: dict
    fonte: FonteEndereco
    transportadora: dict = field(default_factory=dict)
    horario_entrega: dict | None = None
    requer_revisao: bool = False
    observacao: str = ""


def resolver_endereco_entrega(
    detalhe: dict,
    catalogo: CatalogoTransportadoras,
    anthropic_api_key: str = "",
    google_maps_api_key: str = "",
) -> ResultadoEndereco:
    """
    Resolve o endereço de entrega final aplicando a hierarquia de prioridade.

    detalhe: dict retornado por stokki.pedidos.obter_detalhe()
    catalogo: CatalogoTransportadoras carregado de BD_TRANSPORTADORAS.xlsx
    anthropic_api_key: chave da API Anthropic (deixar vazio para pular LLM)
    google_maps_api_key: chave do Google Maps (deixar vazio para pular a
        checagem de área atendida do Local de Entrega — nesse caso o
        Local de Entrega é sempre usado como antes, sem checar redespacho)
    """
    mensagens = detalhe.get("mensagens", [])
    local_entrega = detalhe.get("local_entrega")
    destino = detalhe.get("destino") or {}
    transportadora_bloco = detalhe.get("transportadora")

    nome_transportadora = (transportadora_bloco or {}).get("nome", "") if transportadora_bloco else ""
    cnpj_transportadora = (transportadora_bloco or {}).get("documento", "") if transportadora_bloco else ""
    # Remove pontuação do CNPJ para usar como chave
    cnpj_transportadora = "".join(c for c in cnpj_transportadora if c.isdigit())
    resultado_transp = catalogo.resolver(nome_transportadora, cnpj=cnpj_transportadora) if nome_transportadora else None

    # ── Prioridade 1: Mensagens interpretadas pelo LLM ────────────────────────
    if mensagens and anthropic_api_key:
        resultado_llm = _interpretar_mensagens_llm(mensagens, anthropic_api_key)
        if resultado_llm and resultado_llm.get("tem_instrucao_entrega"):
            confianca = resultado_llm.get("confianca", "baixa")
            transp_alternativa = resultado_llm.get("transportadora_alternativa")
            end_llm = resultado_llm.get("endereco")

            # Resolve a transportadora alternativa (se houver) ANTES de
            # validar o endereço -- se ela for TERCEIROS com endereço de
            # redespacho conhecido no catálogo, esse endereço confiável
            # substitui o texto livre extraído pelo LLM. Sem essa ordem,
            # uma transportadora alternativa válida ("manda pela Kanejo,
            # perto do metrô") era descartada inteira só porque o texto
            # livre do endereço ("perto do metrô") não passava na
            # validação -- mesmo o catálogo tendo o endereço correto.
            transp_resolvida = resultado_transp
            transp_alt_resolvida = None
            if transp_alternativa:
                transp_alt_resolvida = catalogo.resolver(transp_alternativa)
                if transp_alt_resolvida.tipo == "TERCEIROS" and transp_alt_resolvida.endereco_redespacho:
                    end_llm = _redespacho_para_dict(transp_alt_resolvida.endereco_redespacho)
                # Só substitui a transportadora formal pela alternativa se
                # ela foi resolvida com um tipo conhecido -- uma alternativa
                # desconhecida na planilha (ex.: mensagem citando um serviço
                # avulso tipo "LaLaMove") não pode apagar o tipo real da
                # transportadora formal (ex.: RETIRADA), senão o pedido
                # escapa do filtro que impede pedidos RETIRADA de ir pro
                # VUUPT (confirmado com o Hugo, pedido #PS-36209, 09/08).
                if transp_alt_resolvida.tipo is not None:
                    transp_resolvida = transp_alt_resolvida

            # Valida estruturalmente ANTES de aceitar (ver
            # _validar_endereco_plausivel) — sem isso, mensagem confusa
            # podia virar endereço de entrega com dado essencialmente
            # aleatório (confirmado pelo Hugo em produção, 28/07). A
            # "confiança" que o próprio LLM reporta não é suficiente
            # sozinha — já vimos "alta" em cima de extração inválida.
            # end_llm=None (mensagem só menciona transportadora/horário,
            # sem endereço) não precisa validar — segue normal.
            endereco_invalido = False
            if end_llm is not None:
                valido, motivo_invalido = _validar_endereco_plausivel(end_llm)
                if not valido:
                    endereco_invalido = True
                    logger.warning(
                        f"Mensagem indicou instrução de entrega, mas o endereço "
                        f"extraído não passou na validação ({motivo_invalido}) — "
                        f"IGNORADO. Endereço final virá da próxima prioridade "
                        f"(Local de Entrega/Destino). Revisar mensagens manualmente."
                    )

            if not endereco_invalido:
                endereco_final = end_llm or _bloco_para_dict(destino)
                horario_entrega = resultado_llm.get("horario_entrega")
                observacao = resultado_llm.get("observacao", "")
                if transp_resolvida and transp_resolvida.conflito:
                    observacao += f" | CONFLITO na planilha: {transp_resolvida.motivo}"
                if transp_alt_resolvida and transp_alt_resolvida.desconhecida:
                    observacao += f" | Transportadora desconhecida: {transp_alternativa}"

                logger.info(
                    f"Endereço resolvido via mensagem LLM (confiança={confianca}"
                    f"{', horário: ' + str(horario_entrega) if horario_entrega else ''})."
                )
                return ResultadoEndereco(
                    endereco=endereco_final,
                    fonte="mensagem_llm",
                    transportadora=_transp_para_dict(transp_resolvida),
                    horario_entrega=horario_entrega,
                    requer_revisao=(confianca == "baixa"),
                    observacao=observacao,
                )

    # ── Prioridade 2: Local de Entrega (campo formal) ─────────────────────────
    # Exceção (pedido do Hugo, 10/08, caso #PS-36198): se o Local de
    # Entrega está fora da área atendida pela Freshlog (mesma checagem
    # de roteirizacao/notificar_area_nao_atendida.py), ele deixa de ter
    # prioridade automática -- primeiro verifica se a transportadora tem
    # redespacho conhecido (prioridade 3, abaixo). Só cai de volta pro
    # Local de Entrega se a checagem de área não puder ser feita (sem
    # chave do Google Maps).
    local_entrega_fora_area = False
    if local_entrega:
        local_entrega_fora_area = _bloco_fora_area_atendida(local_entrega, google_maps_api_key)
        if not local_entrega_fora_area:
            logger.info("Endereço resolvido via Local de Entrega.")
            return ResultadoEndereco(
                endereco=_bloco_para_dict(local_entrega),
                fonte="local_entrega",
                transportadora=_transp_para_dict(resultado_transp),
            )
        logger.info(
            f"Local de Entrega fora da área atendida "
            f"({local_entrega.get('cidade','')}-{local_entrega.get('uf','')}) -- "
            f"verificando redespacho da transportadora antes de usar."
        )

    # ── Prioridade 3: Redespacho por transportadora (TERCEIROS) ──────────────
    if resultado_transp and resultado_transp.tipo == "TERCEIROS":
        if resultado_transp.endereco_redespacho:
            logger.info(
                f"Endereço resolvido por redespacho: {nome_transportadora!r} → "
                f"{resultado_transp.endereco_redespacho}"
            )
            observacao = ""
            if resultado_transp.conflito:
                observacao = f"CONFLITO na planilha: {resultado_transp.motivo}"
            return ResultadoEndereco(
                endereco=_redespacho_para_dict(resultado_transp.endereco_redespacho),
                fonte="redespacho",
                transportadora=_transp_para_dict(resultado_transp),
                requer_revisao=resultado_transp.conflito,
                observacao=observacao,
            )

    # Local de Entrega fora da área atendida e SEM redespacho conhecido pra
    # transportadora (desconhecida, conflito, ou tipo diferente de
    # TERCEIROS): não dá pra decidir sozinho -- sinaliza pro pipeline
    # segurar o pedido (não importar no VUUPT ainda) e notificar o
    # embarcador pedindo nome da transportadora + endereço de redespacho.
    if local_entrega_fora_area:
        motivo_transp = resultado_transp.motivo if resultado_transp else ""
        observacao = (
            f"Local de Entrega ({local_entrega.get('cidade','')}-{local_entrega.get('uf','')}) "
            f"fora da área atendida e sem redespacho conhecido para a transportadora "
            f"{nome_transportadora!r}."
            + (f" {motivo_transp}" if motivo_transp else "")
        )
        logger.warning(observacao)
        return ResultadoEndereco(
            endereco={},
            fonte="aguardando_redespacho",
            transportadora=_transp_para_dict(resultado_transp),
            requer_revisao=True,
            observacao=observacao,
        )

    # Transportadora desconhecida: usa o destino mas avisa
    if resultado_transp and resultado_transp.desconhecida and nome_transportadora:
        logger.warning(f"Transportadora desconhecida: {nome_transportadora!r}")

    # ── Prioridade 4: Destino (fallback padrão) ───────────────────────────────
    logger.info("Endereço resolvido via Destino (padrão).")
    observacao = ""
    if resultado_transp and resultado_transp.desconhecida and nome_transportadora:
        observacao = resultado_transp.motivo
    if resultado_transp and resultado_transp.conflito:
        observacao = resultado_transp.motivo

    return ResultadoEndereco(
        endereco=_bloco_para_dict(destino),
        fonte="destino",
        transportadora=_transp_para_dict(resultado_transp),
        requer_revisao=bool(observacao),
        observacao=observacao,
    )


# ── LLM ───────────────────────────────────────────────────────────────────────

def _interpretar_mensagens_llm(mensagens: list, api_key: str) -> dict | None:
    """
    Chama a API Anthropic para interpretar as mensagens e extrair,
    se houver, instrução de endereço alternativo.
    Retorna o dict parseado do JSON, ou None em caso de falha.
    """
    texto_mensagens = "\n".join(
        f"[{m.get('data', '')}] {m.get('usuario', '')} ({m.get('tipo_usuario', '')}): "
        f"{m.get('texto', '')}"
        for m in mensagens
        if m.get("texto")
    )

    if not texto_mensagens.strip():
        return None

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 1000,
                "system": _PROMPT_SISTEMA,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Analise as mensagens abaixo e extraia instruções de entrega "
                            "se houver:\n\n" + texto_mensagens
                        ),
                    }
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
        conteudo = resp.json()["content"][0]["text"].strip()
        if not conteudo:
            return None
        # Remove possíveis markdown fences antes do JSON
        conteudo = re.sub(r"^```(?:json)?\s*", "", conteudo)
        conteudo = re.sub(r"\s*```$", "", conteudo).strip()
        return json.loads(conteudo)
    except Exception as e:
        logger.warning(f"Falha ao interpretar mensagens via LLM: {e}")
        return None


# ── Área atendida ────────────────────────────────────────────────────────────

def _bloco_fora_area_atendida(bloco: dict, google_maps_api_key: str) -> bool:
    """
    Reaproveita a MESMA lógica de área atendida usada em
    roteirizacao/notificar_area_nao_atendida.py (Grande SP + regiões com
    dia fixo de roteirizacao/regioes_dia_fixo.py) pra decidir se um bloco
    de endereço (aqui, o Local de Entrega) está fora da área atendida
    pela Freshlog.

    Sem chave do Google Maps, não dá pra checar o raio da Grande SP com
    segurança -- retorna False (mantém o comportamento anterior: Local
    de Entrega sempre vale) em vez de arriscar um falso positivo.
    """
    if not bloco or not google_maps_api_key:
        return False

    if str(_ROTEIRIZACAO_DIR) not in sys.path:
        sys.path.insert(0, str(_ROTEIRIZACAO_DIR))

    from geocodificacao import geocodificar
    from notificar_area_nao_atendida import classificar_pedido
    from regioes_dia_fixo import (
        ENDERECO_REFERENCIA_SP, RAIO_GRANDE_SP_KM,
        dia_fixo_da_cidade, extrair_cidade, extrair_uf,
    )
    from roteirizacao_dados import _distancia_km, obter_coordenadas

    endereco_completo = (
        f"{bloco.get('endereco', '')}, {bloco.get('cidade', '')} - "
        f"{bloco.get('uf', '')}, {bloco.get('cep', '')}"
    )
    servico_sintetico = {"address": endereco_completo}
    coords_sp = geocodificar(ENDERECO_REFERENCIA_SP, google_maps_api_key)

    tipo = classificar_pedido(
        servico_sintetico, google_maps_api_key, coords_sp,
        extrair_cidade, extrair_uf, dia_fixo_da_cidade,
        obter_coordenadas, _distancia_km, RAIO_GRANDE_SP_KM,
    )
    return tipo is not None


# ── Helpers de conversão ──────────────────────────────────────────────────────

def _bloco_para_dict(bloco: dict | None) -> dict:
    """Converte um bloco de endereço (do parsear_pagina_detalhe) para dict padronizado."""
    if not bloco:
        return {}
    return {
        "nome":        bloco.get("nome", ""),
        "documento":   bloco.get("documento", ""),
        "logradouro":  bloco.get("endereco", ""),
        "bairro":      "",  # o parser atual combina tudo em "endereco"
        "cidade":      bloco.get("cidade", ""),
        "uf":          bloco.get("uf", ""),
        "cep":         bloco.get("cep", ""),
        "telefone":    bloco.get("telefone", ""),
        "email":       bloco.get("email", ""),
    }


def _redespacho_para_dict(end) -> dict:
    """Converte EnderecoRedespacho para dict padronizado."""
    return {
        "nome":       end.complemento,  # o complemento costuma ter o nome do galpão
        "documento":  "",
        "logradouro": f"{end.logradouro}, {end.numero}".strip(", "),
        "bairro":     end.bairro,
        "cidade":     end.municipio,
        "uf":         end.uf,
        "cep":        end.cep,
        "telefone":   "",
        "email":      "",
    }


def _transp_para_dict(resultado: ResultadoResolucao | None) -> dict:
    """Converte ResultadoResolucao para dict resumido."""
    if not resultado:
        return {}
    return {
        "nome":          resultado.nome_original,
        "tipo":          resultado.tipo,
        "desconhecida":  resultado.desconhecida,
        "conflito":      resultado.conflito,
    }
