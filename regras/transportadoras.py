# -*- coding: utf-8 -*-
"""
regras/transportadoras.py

Resolução de transportadoras: dado o nome de uma transportadora vindo
do Stokki, determina seu Tipo (ENTREGA / RETIRADA / TERCEIROS) e, se
for TERCEIROS, retorna o endereço fixo de redespacho.

Fonte de verdade: BD_TRANSPORTADORAS.xlsx (colunas A-I, S, T).
  A: TRANSPORTADORA (nome, pode ter variações de grafia)
  B: Tipo (ENTREGA | RETIRADA | TERCEIROS)
  C-I: Destinatário - UF, Municipio, Bairro, Endereço, Numero, Complemento, CEP
  S: CNPJ (posição dinâmica, ver varredura em carregar())
  T: E-mail (pedido do Hugo, 13/08 -- notificação de transportadoras com
     XML da NF-e, ver notificacao_transportadoras/)

Enquanto isso, o matching é feito por nome normalizado (sem acentos,
sem maiúsculas, sem sufixos jurídicos) quando o CNPJ não está preenchido.
Quando o mesmo nome normalizado aparecer com tipos diferentes (conflito
real, ex: TRANSFRIOS e LOGGI), o método resolve() retorna um
TipoDesconhecido com detalhe do conflito — não escolhe silenciosamente
um dos dois.

Uso:
    from regras.transportadoras import CatalogoTransportadoras

    catalogo = CatalogoTransportadoras.carregar(Path("dados/BD_TRANSPORTADORAS.xlsx"))
    resultado = catalogo.resolver("TRANSFRIOS TRANSPORTES LTDA")
    if resultado.tipo == "TERCEIROS":
        endereco = resultado.endereco_redespacho
    elif resultado.tipo is None:
        # transportadora desconhecida — notificar
        print(resultado.motivo)
"""
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import openpyxl

logger = logging.getLogger(__name__)

TipoTransportadora = Literal["ENTREGA", "RETIRADA", "TERCEIROS"]


@dataclass
class EnderecoRedespacho:
    uf: str
    municipio: str
    bairro: str
    logradouro: str
    numero: str
    complemento: str
    cep: str

    def __str__(self) -> str:
        partes = [f"{self.logradouro}, {self.numero}".strip(", ")]
        if self.complemento:
            partes.append(self.complemento)
        partes.append(self.bairro)
        partes.append(f"{self.municipio} - {self.uf}")
        if self.cep:
            partes.append(self.cep)
        return ", ".join(p for p in partes if p)


@dataclass
class ResultadoResolucao:
    """
    Resultado de resolver() para uma transportadora.

    tipo:                ENTREGA | RETIRADA | TERCEIROS | None
    nome_normalizado:    nome após normalização (para debug)
    endereco_redespacho: preenchido só se tipo == "TERCEIROS"
    email:               e-mail de contato da transportadora (planilha, coluna T) -- "" se não cadastrado
    desconhecida:        True quando a transportadora não foi encontrada
    conflito:            True quando há ambiguidade irresolvível no catálogo
    motivo:              descrição do problema (quando desconhecida ou conflito)
    """
    tipo: TipoTransportadora | None
    nome_original: str
    nome_normalizado: str
    endereco_redespacho: EnderecoRedespacho | None = None
    email: str = ""
    desconhecida: bool = False
    conflito: bool = False
    motivo: str = ""


@dataclass
class _Entrada:
    """Linha interna do catálogo, já normalizada."""
    nome_original: str
    nome_normalizado: str
    cnpj: str
    tipo: str
    endereco: EnderecoRedespacho | None
    email: str = ""


class CatalogoTransportadoras:
    """
    Catálogo carregado da planilha BD_TRANSPORTADORAS.xlsx.
    Imutável após carregamento.
    """

    def __init__(self, entradas: list[_Entrada]):
        self._entradas = entradas
        # Índice por nome normalizado → lista de _Entrada
        self._indice: dict[str, list[_Entrada]] = {}
        for e in entradas:
            self._indice.setdefault(e.nome_normalizado, []).append(e)
        # Índice por CNPJ → lista de _Entrada (mesmo padrão do índice por
        # nome -- permite detectar conflito em vez de pegar a primeira
        # entrada em silêncio, contradizendo o proprio design do modulo).
        self._indice_cnpj: dict[str, list[_Entrada]] = {}
        for e in entradas:
            if e.cnpj:
                self._indice_cnpj.setdefault(e.cnpj, []).append(e)

        conflitos = {k: v for k, v in self._indice.items() if len({e.tipo for e in v}) > 1}
        if conflitos:
            logger.warning(
                f"BD_TRANSPORTADORAS tem {len(conflitos)} nome(s) com tipos conflitantes — "
                f"esses precisarão de resolução manual: "
                f"{list(conflitos.keys())}"
            )

    @classmethod
    def carregar(cls, caminho: Path) -> "CatalogoTransportadoras":
        """Carrega o catálogo da planilha Excel."""
        if not caminho.exists():
            raise FileNotFoundError(f"Planilha não encontrada: {caminho}")

        wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
        ws = wb.active
        entradas = []

        for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            nome_raw = row[0]
            tipo_raw = row[1]

            if not nome_raw or not tipo_raw:
                continue

            nome_original = str(nome_raw).strip()
            tipo = str(tipo_raw).strip().upper()

            if tipo not in ("ENTREGA", "RETIRADA", "TERCEIROS"):
                logger.debug(f"Linha {i}: tipo desconhecido {tipo!r} para {nome_original!r} — ignorando.")
                continue

            # CNPJ — coluna após as colunas de endereço (posição dinâmica)
            cnpj = ""
            for idx in range(9, min(len(row), 20)):
                val = row[idx]
                if not val:
                    continue
                val_str = str(val).strip()
                if not val_str.isdigit():
                    continue
                if len(val_str) == 13:
                    # Excel guarda a celula como numero -> perde o zero a
                    # esquerda de CNPJs que comecam com "0". CNPJ tem
                    # sempre 14 digitos, entao 13 so pode significar isso
                    # -- sem o zfill, esses CNPJs nunca entravam no indice
                    # e caiam pra correspondencia por nome (mais fraca).
                    val_str = val_str.zfill(14)
                if len(val_str) >= 14:
                    cnpj = val_str
                    break

            # E-mail -- coluna T (índice 19), logo após o CNPJ. Posição fixa
            # (diferente do CNPJ, que varre um intervalo): não colide com a
            # varredura de CNPJ acima porque e-mail nunca é só-dígitos.
            email = ""
            if len(row) > 19 and row[19]:
                email = str(row[19]).strip()

            endereco = None
            if tipo == "TERCEIROS":
                uf  = str(row[2] or "").strip()
                mun = str(row[3] or "").strip().rstrip("\xa0")
                bai = str(row[4] or "").strip()
                log = str(row[5] or "").strip()
                num = str(row[6] or "").strip() if row[6] is not None else ""
                com = str(row[7] or "").strip()
                cep = str(row[8] or "").strip() if row[8] is not None else ""
                endereco = EnderecoRedespacho(
                    uf=uf, municipio=mun, bairro=bai,
                    logradouro=log, numero=num, complemento=com, cep=cep,
                )

            entradas.append(_Entrada(
                nome_original=nome_original,
                nome_normalizado=_normalizar(nome_original),
                cnpj=cnpj,
                tipo=tipo,
                endereco=endereco,
                email=email,
            ))

        wb.close()
        logger.info(f"Catálogo carregado: {len(entradas)} entradas de {caminho.name}.")
        return cls(entradas)

    def resolver(self, nome_transportadora: str, cnpj: str = "") -> ResultadoResolucao:
        """
        Resolve o tipo e endereço de redespacho para o nome/CNPJ dado.

        Prioridade de matching:
          0. Nome que já resolve (sozinho, sem ambiguidade) para RETIRADA
             — prevalece sobre CNPJ. Achado 27/08 (caso #PS-37190): pedidos
             "Cliente Retira" no Stokki vêm com o CNPJ da própria Freshlog
             anexado no bloco Transportadora (é o galpão de origem, não um
             transportador real), e esse mesmo CNPJ está cadastrado na
             planilha sob várias grafias de "Freshlog" como tipo ENTREGA —
             sem esta prioridade, o match por CNPJ mascarava silenciosamente
             um nome inequívoco de retirada, deixando o pedido ir pro VUUPT.
          1. CNPJ (quando fornecido) — chave primária pros demais casos,
             sem ambiguidade
          2. Nome normalizado — fallback quando CNPJ não está na planilha

        Casos possíveis:
          - Encontrou → retorna tipo + endereço
          - Conflito de tipos → retorna resultado com conflito=True
          - Não encontrado → retorna resultado com desconhecida=True
        """
        nome_norm = _normalizar(nome_transportadora)
        candidatos_nome = self._indice.get(nome_norm, [])

        # Prioridade 0: nome inequívoco de RETIRADA vence CNPJ
        if candidatos_nome:
            tipos_nome = {e.tipo for e in candidatos_nome}
            if tipos_nome == {"RETIRADA"}:
                entrada = candidatos_nome[0]
                return ResultadoResolucao(
                    tipo="RETIRADA",
                    nome_original=nome_transportadora,
                    nome_normalizado=nome_norm,
                    email=entrada.email,
                )

        # Prioridade 1: CNPJ
        if cnpj and cnpj in self._indice_cnpj:
            candidatos_cnpj = self._indice_cnpj[cnpj]
            tipos_cnpj = {e.tipo for e in candidatos_cnpj}
            if len(tipos_cnpj) > 1:
                entradas_detalhes = "; ".join(
                    f"{e.nome_original!r}→{e.tipo}" for e in candidatos_cnpj
                )
                return ResultadoResolucao(
                    tipo=None,
                    nome_original=nome_transportadora,
                    nome_normalizado=_normalizar(nome_transportadora),
                    conflito=True,
                    motivo=(
                        f"CNPJ {cnpj!r} tem tipos conflitantes em BD_TRANSPORTADORAS.xlsx: "
                        f"[{entradas_detalhes}]. Remover as entradas duplicadas/erradas da planilha."
                    ),
                )
            entrada = candidatos_cnpj[0]
            return ResultadoResolucao(
                tipo=entrada.tipo,
                nome_original=nome_transportadora or entrada.nome_original,
                nome_normalizado=entrada.nome_normalizado,
                endereco_redespacho=entrada.endereco if entrada.tipo == "TERCEIROS" else None,
                email=entrada.email,
            )

        # Prioridade 2: nome normalizado (já calculado acima, na prioridade 0)
        candidatos = candidatos_nome

        if not candidatos:
            return ResultadoResolucao(
                tipo=None,
                nome_original=nome_transportadora,
                nome_normalizado=nome_norm,
                desconhecida=True,
                motivo=(
                    f"Transportadora {nome_transportadora!r} não encontrada em "
                    f"BD_TRANSPORTADORAS.xlsx. Pode ser uma entrada nova — "
                    f"adicionar à planilha com o tipo correto."
                ),
            )

        tipos_distintos = {e.tipo for e in candidatos}

        if len(tipos_distintos) > 1:
            # Conflito real: mesma transportadora (nome normalizado igual)
            # com tipos diferentes na planilha.
            entradas_detalhes = "; ".join(
                f"{e.nome_original!r}→{e.tipo}" for e in candidatos
            )
            return ResultadoResolucao(
                tipo=None,
                nome_original=nome_transportadora,
                nome_normalizado=nome_norm,
                conflito=True,
                motivo=(
                    f"Transportadora {nome_transportadora!r} tem tipos conflitantes "
                    f"em BD_TRANSPORTADORAS.xlsx: [{entradas_detalhes}]. "
                    f"Remover as entradas duplicadas/erradas da planilha."
                ),
            )

        # Sem conflito: todos os candidatos têm o mesmo tipo
        tipo = candidatos[0].tipo

        # Para TERCEIROS, pega o endereço da primeira entrada com endereço preenchido
        endereco = next((e.endereco for e in candidatos if e.endereco), None)
        email = next((e.email for e in candidatos if e.email), "")

        return ResultadoResolucao(
            tipo=tipo,
            nome_original=nome_transportadora,
            nome_normalizado=nome_norm,
            endereco_redespacho=endereco if tipo == "TERCEIROS" else None,
            email=email,
        )

    def listar_conflitos(self) -> list[str]:
        """Retorna lista de nomes normalizados que têm conflito de tipos."""
        return [
            k for k, v in self._indice.items()
            if len({e.tipo for e in v}) > 1
        ]

    def listar_desconhecidas(self, nomes: list[str]) -> list[str]:
        """
        Dado um conjunto de nomes de transportadoras (ex: vindos do Stokki),
        retorna os que NÃO estão no catálogo.
        Útil para detecção proativa de transportadoras novas.
        """
        return [n for n in nomes if _normalizar(n) not in self._indice]


# ── Normalização ──────────────────────────────────────────────────────────────

# Sufixos jurídicos e palavras de transporte que variam muito entre grafias
_SUFIXOS_REMOVER = re.compile(
    r"\b(?:LTDA|EIRELI|EIRELLI|S\.?A|ME|EPP|LTD|"
    r"TRANSPORTES|TRANSPORTE|LOGISTICA|LOGISTICA|LOGISTICS|"
    r"INDUSTRIA|COMERCIO|ARMAZENAMENTO|ARMAZENAGEM|SERVICOS|SERVICO|"
    r"SOLUCOES|EMPREENDEDORAS|TECNOLOGIA|BRASIL)\b\.?",
    re.IGNORECASE,
)


def _normalizar(nome: str) -> str:
    """
    Normaliza um nome de transportadora para matching tolerante a variações:
    - Maiúsculas
    - Remove acentos
    - Remove sufixos jurídicos e palavras de transporte/logística
    - Remove pontuação e espaços extras

    Exemplos que resultam no mesmo valor normalizado:
      "KANEJO LOGISTICA LTDA" → "KANEJO"
      "Kanejo" → "KANEJO"
      "KANEJO" → "KANEJO"

      "TRANSFRIOS TRANSPORTES LTDA" → "TRANSFRIOS"
      "Transfrios" → "TRANSFRIOS"
    """
    # 1. Maiúsculas e normalização Unicode
    s = nome.upper().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))

    # 2. Remove sufixos jurídicos e palavras de transporte
    s = _SUFIXOS_REMOVER.sub(" ", s)

    # 3. Remove pontuação (exceto espaços)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)

    # 4. Colapsa espaços
    s = re.sub(r"\s+", " ", s).strip()

    return s
