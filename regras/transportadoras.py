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


@dataclass
class PontoRedespacho:
    """
    Um endereço físico de redespacho (galpão de transportadora TERCEIROS),
    agregando todas as linhas da planilha que apontam pra ele -- KANEJO e
    IMG TRANSPORTES, por exemplo, são a mesma Rua Osaka 880; CENTROSUL,
    ANDREA BELOTTO, FREZZE e GESSY LOPES são a mesma Rua Makita Brasil 300.

    Usado pela notificação de transportadoras (notificacao_transportadoras/)
    pra bater o endereço do serviço na VUUPT contra a planilha, em vez de
    confiar no bloco "Transportadora" da Stokki (pedido do Hugo, 10/09: o
    cliente às vezes esquece de informar a transportadora na Stokki, mas o
    endereço de entrega já é o do galpão).

    chave:            identidade do ponto (CEP+número, ou rua+número+cidade sem CEP)
    nome:             transportadora "dona" do ponto, pra exibição
    nome_normalizado: _normalizar(nome) -- chave estável pra fingerprint
    endereco:         endereço da planilha (primeira linha do grupo)
    nomes:            todas as grafias/transportadoras que usam esse ponto
    emails:           e-mails (coluna T) de todas as linhas do grupo, sem repetição
    """
    chave: str
    nome: str
    nome_normalizado: str
    endereco: EnderecoRedespacho
    nomes: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)


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

        self._pontos: list[PontoRedespacho] = _montar_pontos_redespacho(entradas)

    # ── Batimento por endereço ────────────────────────────────────────────

    def pontos_redespacho(self) -> list[PontoRedespacho]:
        """Endereços físicos de redespacho (TERCEIROS), 1 por galpão."""
        return list(self._pontos)

    def resolver_por_endereco(self, endereco: str) -> PontoRedespacho | None:
        """
        Dado um endereço formatado (ex.: campo 'address' do serviço na VUUPT,
        "Est. Francisco Hengles, 591, Potuvera, Itapecerica da Serra - SP,
        06885-160, Brasil"), devolve o ponto de redespacho correspondente ou
        None se o endereço não for de nenhuma transportadora TERCEIROS.

        Dois níveis, do mais pro menos confiável:
          1. CEP (8 dígitos) igual E número do imóvel presente no endereço.
          2. Rua (sem o tipo de logradouro: R./RUA/AV./ESTRADA...) contida no
             endereço E número presente E município presente -- cobre
             endereço digitado pelo cliente com CEP diferente/ausente e
             transportadora cadastrada sem CEP na planilha.

        O número é comparado como inteiro ('059' == '59') e só conta se
        aparecer como token isolado fora do CEP -- número de apartamento
        igual ao do galpão no mesmo CEP é o único falso positivo possível,
        e exige coincidência dupla.
        """
        if not endereco or not endereco.strip():
            return None
        cep_end = _extrair_cep(endereco)
        texto_sem_cep = endereco
        if cep_end:
            texto_sem_cep = re.sub(r"\d{5}-?\d{3}", " ", endereco)
        numeros = _numeros_isolados(texto_sem_cep)
        texto_norm = _normalizar_endereco(texto_sem_cep)

        candidatos_cep: list[PontoRedespacho] = []
        candidatos_rua: list[PontoRedespacho] = []
        for p in self._pontos:
            end = p.endereco
            numero = _numero_int(end.numero)
            if numero is None or numero not in numeros:
                continue
            cep_p = _somente_digitos(end.cep)
            if cep_end and len(cep_p) == 8 and cep_p == cep_end:
                candidatos_cep.append(p)
                continue
            rua = _rua_sem_tipo(end.logradouro)
            municipio = _normalizar_endereco(end.municipio)
            if (rua and f" {rua} " in f" {texto_norm} "
                    and municipio and f" {municipio} " in f" {texto_norm} "):
                candidatos_rua.append(p)

        candidatos = candidatos_cep or candidatos_rua
        if not candidatos:
            return None
        if len(candidatos) > 1:
            logger.warning(
                f"Endereço {endereco!r} bate com mais de um ponto de redespacho "
                f"({[c.nome for c in candidatos]}) -- usando o primeiro."
            )
        return candidatos[0]

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


# ── Pontos de redespacho (batimento por endereço) ─────────────────────────────

_TIPOS_LOGRADOURO = {
    "R", "RUA", "AV", "AVENIDA", "AL", "ALAMEDA", "EST", "ESTRADA", "TRAV",
    "TRAVESSA", "ROD", "RODOVIA", "PC", "PCA", "PRACA", "LGO", "LARGO", "VL", "VILA",
}


def _somente_digitos(s: str | None) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _numero_int(numero: str | None) -> int | None:
    """'591' -> 591, '341\xa0' -> 341, 'S/N' -> None. Usa a primeira
    sequência de dígitos (a planilha às vezes traz '300 ' ou '1505')."""
    m = re.search(r"\d+", str(numero or ""))
    return int(m.group()) if m else None


def _extrair_cep(texto: str) -> str:
    """CEP de 8 dígitos dentro de um endereço formatado -- mesmos 2 formatos
    de roteirizacao_dados.extrair_cep ('12345-678' em qualquer posição ou
    '12345678' no fim)."""
    m = re.search(r"(\d{5})-(\d{3})", texto or "")
    if m:
        return m.group(1) + m.group(2)
    m = re.search(r"(\d{8})\s*$", (texto or "").strip())
    return m.group(1) if m else ""


def _numeros_isolados(texto: str) -> set[int]:
    """Todos os números que aparecem como token isolado no texto (número do
    imóvel, apartamento, loja...). 'RUA SIMAO ALVARES 059' -> {59}."""
    return {int(n) for n in re.findall(r"(?<!\d)(\d{1,6})(?!\d)", texto or "")}


def _normalizar_endereco(texto: str) -> str:
    """Maiúsculas, sem acento, pontuação vira espaço, espaços colapsados."""
    s = unicodedata.normalize("NFKD", str(texto or "").upper())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _rua_sem_tipo(logradouro: str) -> str:
    """'Est. Francisco Hengles' -> 'FRANCISCO HENGLES'; 'RUA CONS CANDIDO DE
    OLIVEIRA, 341' -> 'CONS CANDIDO DE OLIVEIRA' (número no fim também sai,
    a planilha às vezes repete o número dentro do logradouro)."""
    tokens = _normalizar_endereco(logradouro).split()
    while tokens and tokens[0] in _TIPOS_LOGRADOURO:
        tokens = tokens[1:]
    while tokens and tokens[-1].isdigit():
        tokens = tokens[:-1]
    return " ".join(tokens)


def _separar_emails(raw: str) -> list[str]:
    """Coluna T pode trazer vários e-mails separados por vírgula, ponto e
    vírgula, quebra de linha ou tab."""
    return [e.strip() for e in re.split(r"[,;\s]+", raw or "") if e.strip() and "@" in e]


def _chave_ponto(end: EnderecoRedespacho) -> str | None:
    numero = _numero_int(end.numero)
    if numero is None:
        return None
    cep = _somente_digitos(end.cep)
    if len(cep) == 8:
        return f"{cep}-{numero}"
    rua = _rua_sem_tipo(end.logradouro)
    municipio = _normalizar_endereco(end.municipio)
    if not rua or not municipio:
        return None
    return f"{rua}|{numero}|{municipio}"


def _escolher_nome_do_ponto(entradas: list[_Entrada], end: EnderecoRedespacho) -> _Entrada:
    """Transportadora 'dona' do galpão: a que aparece no complemento da
    planilha (que costuma trazer o nome do galpão -- 'Rua Osaka 880 KANEJO'),
    senão a primeira com e-mail, senão a primeira na ordem da planilha."""
    complemento = _normalizar_endereco(end.complemento)
    for e in entradas:
        if e.nome_normalizado and f" {e.nome_normalizado} " in f" {complemento} ":
            return e
    for e in entradas:
        if e.email:
            return e
    return entradas[0]


def _montar_pontos_redespacho(entradas: list[_Entrada]) -> list[PontoRedespacho]:
    grupos: dict[str, list[_Entrada]] = {}
    for e in entradas:
        if e.tipo != "TERCEIROS" or not e.endereco:
            continue
        chave = _chave_ponto(e.endereco)
        if not chave:
            logger.debug(f"Transportadora {e.nome_original!r} sem número/CEP/rua utilizável -- "
                         f"fora do batimento por endereço.")
            continue
        grupos.setdefault(chave, []).append(e)

    pontos = []
    for chave, grupo in grupos.items():
        end = grupo[0].endereco
        dona = _escolher_nome_do_ponto(grupo, end)
        emails: list[str] = []
        for e in grupo:
            for em in _separar_emails(e.email):
                if em.lower() not in {x.lower() for x in emails}:
                    emails.append(em)
        nomes: list[str] = []
        for e in grupo:
            if e.nome_original not in nomes:
                nomes.append(e.nome_original)
        pontos.append(PontoRedespacho(
            chave=chave,
            nome=dona.nome_original,
            nome_normalizado=dona.nome_normalizado,
            endereco=end,
            nomes=nomes,
            emails=emails,
        ))
    return pontos


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
