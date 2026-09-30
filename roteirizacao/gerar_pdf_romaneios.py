# -*- coding: utf-8 -*-
"""
roteirizacao/gerar_pdf_romaneios.py

Gera 1 PDF por romaneio (= rota do VUUPT) com a papelada que o
motorista leva na entrega: pedido a pedido, NA ORDEM DE VISITA da
rota, todas as Notas Fiscais do pedido seguidas dos Boletos -- doc de
origem: DOC_EXECUCAO_CLAUDE_ROMANEIOS_PDF.md.

Reaproveita o que já existe: as rotas vêm do VUUPT (mesma leitura de
avisar_motoristas_rotas.py), o índice de documentos vem do SQLite
(documentos_processados, ver documentos_pedido/fingerprint_documentos.py)
e os PDFs físicos ainda estão nas pastas temporárias de
documentos_pedido/dados/ (ver documentos_pedido/localizar_arquivos.py)
-- nada é baixado do GCS.

Estrutura do PDF (ajuste do Hugo, 11/08: sem páginas separadoras entre
os documentos):
  1. CAPA com logo Freshlog, em PAISAGEM (pedido do Hugo, 13/08):
     tabela com 1 linha por pedido na ordem de visita (pedido + nº da
     NF, embarcador, cliente, endereço de entrega, volumes, peso e
     status NF/BOL com check/x). Volumes e peso bruto vêm da DANFE
     local do pedido; sem DANFE, os volumes caem pro dimension_3 do
     VUUPT dividido pelo fator_ponderado do embarcador (o dimension_3
     é o volume PONDERADO, não a contagem real) e o peso fica "—".
  2. Documentos emendados direto: NFs e depois boletos, pedido a pedido.
  3. CANHOTEIRA (só se a rota tiver entrega de Padrão Puro, Quatro
     Estrelas ou Pedramoura): tabela na ordem da rota com campos de
     recebedor/data/assinatura, identificando motorista, rota e dia.
  4. CANHOTEIRA DE TRANSPORTADORA (pedido do Hugo, 10/09): 1 folha por
     transportadora de redespacho presente na rota, listando só os
     pedidos que o motorista entrega naquele galpão (pedido, NF,
     embarcador, destinatário final, volumes) com bloco de recebimento
     (nome, documento, data/hora, assinatura e carimbo). O pedido é
     "via transportadora" quando o ENDEREÇO do serviço na VUUPT bate
     com um ponto de redespacho TERCEIROS da BD_TRANSPORTADORAS
     (regras/transportadoras.py::resolver_por_endereco -- o mesmo
     critério da notificação de transportadoras).

Pendências (sem NF, sem boleto, arquivo ilegível) aparecem como X
vermelho na capa e listadas no _resumo.txt + notificação.

Idempotente: regerar a mesma data sobrescreve tudo daquela pasta.

COMO USAR:
    py -3.11 roteirizacao/gerar_pdf_romaneios.py --modo-teste --data hoje
    py -3.11 roteirizacao/gerar_pdf_romaneios.py --data 15/08/2026 --rota 5041749
"""
import argparse
import io
import logging
import re
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

_RAIZ_LOCAL   = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_LOCAL))
# append (não insert): documentos_pedido/ entra por último pra não
# sombrear nenhum módulo de roteirizacao/ ou da raiz.
sys.path.append(str(_RAIZ_PROJETO / "documentos_pedido"))
(_RAIZ_LOCAL / "dados").mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# basicConfig ANTES de importar avisar_motoristas_rotas: o import dele
# também chama basicConfig, mas com o root logger já configurado a
# chamada de lá vira no-op e os logs ficam no arquivo daqui.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ_LOCAL / "dados" / "gerar_pdf_romaneios.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("gerar_pdf_romaneios")

import yaml
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter

from avisar_motoristas_rotas import (
    _extrair_servicos_da_rota,
    _normalizar_texto,
    _parse_data,
    buscar_rotas_do_dia,
)
from localizar_arquivos import resolver_arquivo_local
from notificar_execucao_agente import notificar_execucao
from regras.preferencias_motoristas import CatalogoMotoristas
from regras.transportadoras import CatalogoTransportadoras, PontoRedespacho

PASTA_ROMANEIOS = _RAIZ_LOCAL / "dados" / "romaneios"
DB_PATH = _RAIZ_PROJETO / "dados" / "dados.db"
LOGO_PATH = _RAIZ_PROJETO / "assets" / "logo_freshlog.png"
# Mesma planilha do pipeline e da notificação de transportadoras.
TRANSPORTADORAS_PATH = _RAIZ_PROJETO / "dados" / "BD_TRANSPORTADORAS.xlsx"

# Embarcadores cujas entregas geram folha de CANHOTEIRA no fim do
# romaneio (pedido do Hugo, 11/08). sender_id do VUUPT (tabela interno).
SENDERS_CANHOTEIRA = {
    12887364: "PADRÃO PURO",
    21785428: "QUATRO ESTRELAS",
    21911340: "PEDRAMOURA",
}

# Esses mesmos 3 embarcadores não precisam ir acompanhados de Nota
# Fiscal (pedido do Hugo, 13/08) -- a entrega deles é controlada pela
# CANHOTEIRA. Falta de NF não é pendência pra eles (a capa mostra "—"
# na coluna NF em vez do X vermelho), e o agente de documentos nem gera
# a DANFE (ver documentos_pedido/selecionar_pedidos.py::
# EMBARCADORES_SEM_NF).
SENDERS_SEM_NF = set(SENDERS_CANHOTEIRA)

# De Tommaso (pedido do Hugo, 20/08): quando ela não manda Nota Fiscal
# pra uma entrega, um "Pedido de Venda" do sistema interno dela conta
# como substituto -- documento padronizado, confirmado contra PDF real
# (040191.pdf). sender_id do VUUPT (tabela interno), mesmo padrão de
# SENDERS_CANHOTEIRA/SENDERS_SEM_NF acima.
SENDERS_PEDIDO_VENDA_SUBSTITUI_NF = {20562589}  # De Tommaso

# Pedido de reentrega ganha um código com sufixo "-R1", "-R2"... no
# VUUPT (ver insucesso_entrega/expedir_pedidos.py::duplicar_servico_
# por_insucesso) -- é o MESMO pedido original, mesma NF/boleto valem
# pra qualquer tentativa de entrega dele. documentos_processados.
# codigo_pedido é sempre gravado no código BASE (achado 12/08, testando
# o botão "Imprimir rota": o matcher normaliza o código a partir do
# regex PS-\d+ no nome do arquivo/PDF, que não pega o sufixo -- não é
# by design, mas é 100% consistente hoje: 0 de 1224 documentos no banco
# têm sufixo -R). Buscar pelo código COM sufixo nunca casava nada pra
# reentrega -- bug pré-existente, também presente no romaneio das 04h
# pra rotas reais (não é algo introduzido pelo rascunho/planejamento).
_PADRAO_CODIGO_BASE = re.compile(r"PS-?\d{4,6}", re.IGNORECASE)


def _codigo_base(codigo: str) -> str:
    """Extrai o PREFIXO 'PS-NNNNN' em vez de remover sufixo do FIM da
    string: reentrega de reentrega (insucesso de novo numa entrega já
    reentregue) empilha sufixo -- 'PS-36741-R1-R1' -- e uma regex
    ancorada em '$' só tira o ÚLTIMO '-R\\d+', devolvendo 'PS-36741-R1'
    em vez do código base (achado 20/08: NF/boleto certos no banco sob
    'PS-36741' somiam do romaneio pra esses casos). Casar pelo prefixo
    é imune a qualquer sufixo/combinação que apareça depois (-R1, -C1,
    -R1-R1, -R2-C1...). Usar SÓ pra buscar documentos -- a exibição do
    pedido na capa/canhoteira mantém o código completo (o motorista
    precisa saber que é a reentrega, não o pedido original)."""
    m = _PADRAO_CODIGO_BASE.match((codigo or "").lstrip("#").strip())
    return m.group(0) if m else (codigo or "").lstrip("#")


def _codigos_base_lista(codigo: str) -> list[str]:
    """Um 'service' da VUUPT pode agrupar mais de um pedido no mesmo
    endereço num único 'code' combinado por vírgula (achado 20/08, rota
    #19: '#PS-37189, PS-37176, PS-37175', mesma entrega MARCHEF/GOURMAR)
    -- _codigo_base sozinho tentava casar a string INTEIRA como chave e
    nunca batia com nenhum codigo_pedido do banco, então a NF/boleto de
    cada pedido (que estavam corretamente casados) sumiam do romaneio
    inteiro. Aqui, quebra em códigos individuais antes de normalizar."""
    return [_codigo_base(c.strip()) for c in (codigo or "").split(",") if c.strip()]

# Página A4 em pixels a 150 dpi -- o resolution=150.0 no Image.save é
# o que faz 1240px virarem 595pt (A4 de verdade) no PDF final.
# Capa em PAISAGEM (pedido do Hugo, 13/08) pra caber endereço, volumes
# e peso; canhoteira segue em RETRATO.
A4_RETRATO  = (1240, 1754)
A4_PAISAGEM = (1754, 1240)
DPI = 150.0
MARGEM = 100

# Identidade visual (cores tiradas do logo Freshlog: folha verde-água
# em degradê + texto azul-marinho).
NAVY        = (23, 29, 51)
TEAL        = (34, 220, 160)
CINZA_ZEBRA = (243, 246, 249)
CINZA_LINHA = (215, 221, 229)
CINZA_TXT   = (108, 115, 130)
VERDE_OK    = (16, 150, 95)
VERMELHO    = (204, 62, 62)

DIAS_SEMANA_PT = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira",
                  "Sexta-feira", "Sábado", "Domingo"]


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Seleção de documentos (SQLite)
# ---------------------------------------------------------------------------

def carregar_documentos_por_pedido(codigos: set[str]) -> tuple[dict[str, list[dict]], int]:
    """
    Uma ida só ao banco: todos os documentos ENVIADOS ('Nota Fiscal' e
    'Boleto') dos códigos informados, agrupados por codigo_pedido.
    Também conta quantos documentos desses pedidos estão parados em
    REVISAO_MANUAL (não entram no PDF, mas o resumo avisa).
    """
    docs_por_pedido: dict[str, list[dict]] = {}
    em_revisao = 0
    if not codigos:
        return docs_por_pedido, em_revisao

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        lista = sorted(codigos)
        # SQLite limita em 999 variáveis por statement -- lotes de 900.
        for i in range(0, len(lista), 900):
            lote = lista[i:i + 900]
            marcadores = ",".join("?" * len(lote))
            for row in con.execute(
                f"SELECT * FROM documentos_processados "
                f"WHERE status='ENVIADO' AND tipo IN ('Nota Fiscal','Boleto','Pedido de Venda') "
                f"AND codigo_pedido IN ({marcadores})", lote):
                docs_por_pedido.setdefault(row["codigo_pedido"], []).append(dict(row))
            em_revisao += con.execute(
                f"SELECT COUNT(*) FROM documentos_processados "
                f"WHERE status='REVISAO_MANUAL' AND codigo_pedido IN ({marcadores})",
                lote).fetchone()[0]
    finally:
        con.close()
    return docs_por_pedido, em_revisao


def carregar_embarcadores() -> tuple[dict[int, str], dict[int, float]]:
    """(sender_id -> nome curto, sender_id -> fator_ponderado), da
    tabela interno. Na interno, o nome_remetente é o nome de fantasia
    curto ('COGUMELADO', 'PADRÃO PURO') e o apelido costuma ser a razão
    social comprida -- pra capa, o curto é o que cabe na coluna. O
    fator_ponderado serve pra DESFAZER a ponderação do dimension_3 do
    VUUPT (= qtd real x fator, ver pipeline.py) quando a DANFE não dá
    a quantidade real de volumes."""
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            nomes, fatores = {}, {}
            for r in con.execute(
                    "SELECT sender_id, apelido, nome_remetente, fator_ponderado "
                    "FROM interno WHERE sender_id IS NOT NULL"):
                sid = int(r["sender_id"])
                nomes[sid] = (r["nome_remetente"] or r["apelido"] or "").strip()
                fatores[sid] = float(r["fator_ponderado"]) if r["fator_ponderado"] else 1.0
            return nomes, fatores
        finally:
            con.close()
    except Exception as e:
        logger.warning(f"Falha ao carregar embarcadores da tabela interno: {e}")
        return {}, {}


def carregar_catalogo_transportadoras() -> CatalogoTransportadoras | None:
    """Catálogo da BD_TRANSPORTADORAS pra identificar pedido "via
    transportadora" pelo endereço (canhoteira de transportadora). None
    quando a planilha não existe/não abre -- o romaneio sai sem essa
    folha, nunca deixa de sair por causa dela."""
    try:
        return CatalogoTransportadoras.carregar(TRANSPORTADORAS_PATH)
    except Exception as e:
        logger.warning(f"BD_TRANSPORTADORAS indisponível ({e}) -- romaneio sai sem canhoteira "
                       f"de transportadora.")
        return None


_CONECTIVOS_FINAIS = {"E", "DE", "DA", "DO", "DAS", "DOS"}


def nome_curto_transportadora(ponto: PontoRedespacho) -> str:
    """'DAFRAN TRANSPORTES E SERVICOS LTDA EPP' -> 'DAFRAN' (o normalizador
    da planilha já tira TRANSPORTES/SERVICOS/LTDA/EPP, mas deixa a
    conjunção solta no fim: 'DAFRAN E'). Pra capa e resumo; a folha em si
    leva o nome completo."""
    tokens = (ponto.nome_normalizado or "").split()
    while tokens and tokens[-1] in _CONECTIVOS_FINAIS:
        tokens.pop()
    return " ".join(tokens) or (ponto.nome or "").strip().upper() or "TRANSPORTADORA"


def agrupar_por_transportadora(itens: list[dict],
                               catalogo: CatalogoTransportadoras | None) -> list[tuple[PontoRedespacho, list[dict]]]:
    """Pedidos da rota entregues em galpão de transportadora (TERCEIROS),
    agrupados por ponto de redespacho na ordem em que o galpão aparece na
    rota. Critério = ENDEREÇO do serviço na VUUPT (item['endereco_vuupt'])
    batido contra a planilha, o mesmo da notificação de transportadoras:
    pega tanto o redespacho automático do pipeline quanto o endereço do
    galpão digitado à mão pelo cliente. Nunca levanta -- falha vira
    warning e a rota sai sem a folha."""
    if catalogo is None:
        return []
    grupos: dict[str, tuple[PontoRedespacho, list[dict]]] = {}
    for item in itens:
        try:
            ponto = catalogo.resolver_por_endereco(item.get("endereco_vuupt") or "")
        except Exception as e:
            logger.warning(f"  {item.get('codigo')}: falha no batimento de transportadora ({e}).")
            continue
        if ponto is None:
            continue
        grupos.setdefault(ponto.chave, (ponto, []))[1].append(item)
    return list(grupos.values())


def _nf_norm(numero_nf) -> str | None:
    s = str(numero_nf or "").strip().lstrip("0")
    return s or None


def selecionar_nfs(docs: list[dict]) -> list[dict]:
    """
    Notas Fiscais do pedido, deduplicadas por número de NF: pode haver
    mais de um registro pra mesma nota (DANFE gerada na Stokki + nota
    anexada manualmente, hashes diferentes). Preferência: origem
    'stokki' (DANFE oficial gerada do XML); empate -> mais recente.
    NF sem numero_nf extraído não tem como dedupar -- entra sempre.
    """
    grupos: dict[str, list[dict]] = {}
    for d in docs:
        if d.get("tipo") != "Nota Fiscal":
            continue
        chave = _nf_norm(d.get("numero_nf")) or f"__sem_nf__{d['hash_conteudo']}"
        grupos.setdefault(chave, []).append(d)

    escolhidas = []
    for grupo in grupos.values():
        escolhidas.append(max(grupo, key=lambda d: (d.get("origem") == "stokki",
                                                    d.get("processado_em") or "")))

    def _ordem(d):
        nf = _nf_norm(d.get("numero_nf"))
        if nf and nf.isdigit():
            return (0, int(nf), d.get("processado_em") or "")
        return (1, 0, d.get("processado_em") or "")
    escolhidas.sort(key=_ordem)
    return escolhidas


def selecionar_boletos(docs: list[dict]) -> list[dict]:
    """Boletos do pedido, sem dedup (hash é PK; parcelas não colidem).
    Ordem: nº da NF, nº da parcela, data de processamento."""
    boletos = [d for d in docs if d.get("tipo") == "Boleto"]
    boletos.sort(key=lambda d: (_nf_norm(d.get("numero_nf")) or "~",
                                d.get("numero_parcela") or 0,
                                d.get("processado_em") or ""))
    return boletos


def selecionar_pedidos_de_venda(docs: list[dict]) -> list[dict]:
    """'Pedido de Venda' do pedido, dedup por número -- mesmo critério
    de selecionar_nfs(). Só é chamado como substituto de NF ausente
    (ver SENDERS_PEDIDO_VENDA_SUBSTITUI_NF), hoje exclusivo da De
    Tommaso."""
    grupos: dict[str, list[dict]] = {}
    for d in docs:
        if d.get("tipo") != "Pedido de Venda":
            continue
        chave = _nf_norm(d.get("numero_nf")) or f"__sem_num__{d['hash_conteudo']}"
        grupos.setdefault(chave, []).append(d)

    escolhidas = [max(g, key=lambda d: d.get("processado_em") or "") for g in grupos.values()]
    escolhidas.sort(key=lambda d: _nf_norm(d.get("numero_nf")) or "")
    return escolhidas


# ---------------------------------------------------------------------------
# Volumes, peso e endereço de entrega (pedido do Hugo, 13/08)
# ---------------------------------------------------------------------------

# Bloco "TRANSPORTADOR / VOLUMES TRANSPORTADOS" da DANFE, no texto
# extraído por pypdf: o valor vem ENTRE os rótulos ('QUANTIDADE\n6\n
# ESPÉCIE', 'PESO BRUTO\n53,640\nPESO LÍQUIDO'). Campo vazio na DANFE
# deixa os rótulos colados ('PESO BRUTO\nPESO LÍQUIDO') -- não casa, e
# é isso que se quer (validado em 30 DANFEs reais, 13/08).
_RE_DANFE_QTD  = re.compile(r"QUANTIDADE\s+(\d[\d.]*)\s+ESP[EÉ]CIE", re.IGNORECASE)
_RE_DANFE_PESO = re.compile(r"PESO\s+BRUTO\s+([\d.,]+)\s+PESO\s+L[IÍ]QUIDO", re.IGNORECASE)
# Layout alternativo (algumas DANFEs de terceiros): os 3 valores vêm
# ANTES da linha de rótulos ('1 5,500 5,000\nQUANTIDADE ESPÉCIE MARCA
# NUMERAÇÃO PESO BRUTO PESO LÍQUIDO').
_RE_DANFE_TRIO = re.compile(
    r"(\d[\d.]*)\s+([\d.,]+)\s+([\d.,]+)\s+"
    r"QUANTIDADE\s+ESP[EÉ]CIE\s+MARCA\s+NUMERA[CÇ][AÃ]O\s+PESO\s+BRUTO",
    re.IGNORECASE)
# "Pedido de Venda" da De Tommaso (ver SENDERS_PEDIDO_VENDA_SUBSTITUI_NF):
# não tem peso, só "Volume1: N" -- validado contra PDF real (040191.pdf,
# 20/08). Sem peso, a capa mostra "—" nessa coluna (mesmo comportamento
# de qualquer NF sem peso legível).
_RE_PV_VOLUME = re.compile(r"Volume1:\s*(\d+)", re.IGNORECASE)


def _num_br(valor: str) -> float | None:
    """'53,640' / '1.234,5' -> float. None se não parsear."""
    try:
        return float(str(valor).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _qtd_peso_da_danfe(reader: PdfReader) -> tuple[int | None, float | None]:
    """(quantidade de volumes, peso bruto em kg) do bloco de transporte
    da DANFE (ou do 'Volume1:' do Pedido de Venda da De Tommaso, ver
    _RE_PV_VOLUME -- mesma função, o reader pode ser qualquer um dos
    dois), ou None no que não der pra extrair. O bloco fica na 1ª
    página, mas varre até 3 (DANFE de terceiro com página extra)."""
    for pagina in reader.pages[:3]:
        try:
            texto = pagina.extract_text() or ""
        except Exception:
            continue
        qtd = peso = None
        m = _RE_DANFE_QTD.search(texto)
        if m:
            valor = _num_br(m.group(1))
            if valor and 0 < valor < 10000:
                qtd = int(round(valor))
        m = _RE_DANFE_PESO.search(texto)
        if m:
            valor = _num_br(m.group(1))
            if valor and 0 < valor < 100000:
                peso = valor
        if qtd is None and peso is None:
            m = _RE_DANFE_TRIO.search(texto)
            if m:
                v_qtd, v_peso = _num_br(m.group(1)), _num_br(m.group(2))
                if v_qtd and 0 < v_qtd < 10000:
                    qtd = int(round(v_qtd))
                if v_peso and 0 < v_peso < 100000:
                    peso = v_peso
        if qtd is None and peso is None:
            m = _RE_PV_VOLUME.search(texto)
            if m:
                valor = _num_br(m.group(1))
                if valor and 0 < valor < 10000:
                    qtd = int(round(valor))
        if qtd is not None or peso is not None:
            return qtd, peso
    return None, None


def _qtd_peso_do_pedido(nfs_abertas: list[tuple[dict, PdfReader]]) -> tuple[int | None, float | None]:
    """Soma volumes e peso bruto de todas as NFs do pedido. None quando
    nenhuma NF informa aquele campo."""
    qtd_total = peso_total = None
    for _row, reader in nfs_abertas:
        qtd, peso = _qtd_peso_da_danfe(reader)
        if qtd is not None:
            qtd_total = (qtd_total or 0) + qtd
        if peso is not None:
            peso_total = (peso_total or 0.0) + peso
    return qtd_total, peso_total


def calcular_peso_paradas(paradas: list[dict]) -> tuple[float | None, int, int]:
    """
    Peso bruto conhecido (kg) de uma lista de paradas (formato
    rascunhos_parada -- chaves 'codigo' e 'sender_id'), somando só o que
    já tem NF processada. Reaproveita o MESMO motor de extração da capa
    do romaneio (DANFE local via carregar_documentos_por_pedido/
    selecionar_nfs/_abrir_documentos/_qtd_peso_do_pedido) em vez de
    duplicar a lógica -- pedido do Hugo, 22/08: mostrar peso aproximado
    no resumo da oferta de rota pro motorista, só quando já é dado real
    (nunca estimativa).

    Retorna (peso_total_kg ou None se nada conhecido, paradas com peso
    conhecido, total de paradas) -- quem chama decide como exibir
    parcial (ex.: "340 kg (8 de 12 pedidos)").
    """
    codigos_base = {_codigo_base(p["codigo"]) for p in paradas if p.get("codigo")}
    docs_por_pedido, _em_revisao = carregar_documentos_por_pedido(codigos_base)

    peso_total = None
    paradas_com_peso = 0
    for p in paradas:
        codigo = p.get("codigo")
        if not codigo:
            continue
        sender_id = p.get("sender_id")
        if sender_id in SENDERS_SEM_NF:
            continue  # controlado por canhoteira, não por NF -- nunca tem peso de DANFE.
        docs = [d for sub in _codigos_base_lista(codigo) for d in docs_por_pedido.get(sub, [])]
        nfs, _problemas = _abrir_documentos(selecionar_nfs(docs))
        if not nfs and sender_id in SENDERS_PEDIDO_VENDA_SUBSTITUI_NF:
            nfs, _problemas = _abrir_documentos(selecionar_pedidos_de_venda(docs))
        _qtd, peso = _qtd_peso_do_pedido(nfs)
        if peso is not None:
            peso_total = (peso_total or 0.0) + peso
            paradas_com_peso += 1

    return peso_total, paradas_com_peso, len(paradas)


def _volumes_fallback(servico: dict, fator: float) -> int | None:
    """Sem DANFE legível, estima a quantidade real de volumes desfazendo
    a ponderação do dimension_3 (= max(1, round(qtd x fator)), ver
    pipeline.py). Exata pra fator 1.0 (maioria dos embarcadores)."""
    try:
        vol = int(float(servico.get("dimension_3")))
    except (TypeError, ValueError):
        return None
    if vol <= 0:
        return None
    return max(1, round(vol / (fator or 1.0)))


def _endereco_entrega(servico: dict) -> str:
    """Endereço do serviço VUUPT sem o rabo ', CEP, Brasil' -- na capa
    o que importa é rua/número/bairro/cidade, e a coluna é disputada."""
    endereco = str(servico.get("address") or "").strip()
    # Alguns remetentes mandam endereço com quebra de linha embutida
    # (achado real 21/08: rota da Cícero, pedido #PS-37000, Ceagesp) --
    # draw.textlength() do Pillow (usado em _truncar) recusa medir texto
    # multilinha ("can't measure length of multiline text"), então isso
    # precisa virar uma linha só antes de qualquer coisa.
    endereco = re.sub(r"\s+", " ", endereco)
    endereco = re.sub(r",?\s*Brasil\s*$", "", endereco, flags=re.IGNORECASE)
    endereco = re.sub(r",?\s*\d{5}-?\d{3}\s*$", "", endereco)
    return endereco


def _fmt_peso(peso: float | None) -> str:
    if peso is None:
        return "—"
    return f"{peso:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ---------------------------------------------------------------------------
# Desenho (Pillow): identidade visual, capa e canhoteira
# ---------------------------------------------------------------------------

def _fonte(tamanho_px: int, negrito: bool = False) -> ImageFont.FreeTypeFont:
    # Windows primeiro (máquina local); Liberation Sans depois (VPS/Linux
    # -- métrica compatível com Arial, `apt install fonts-liberation`) --
    # sem nenhuma das duas, cai pro bitmap padrão do PIL, que não tem os
    # acentos do português (pt-BR) direito.
    candidatos = (["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"] if negrito
                  else ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf",
                        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"])
    for caminho in candidatos:
        try:
            return ImageFont.truetype(caminho, tamanho_px)
        except OSError:
            continue
    return ImageFont.load_default()


_LOGO_CACHE: list = []


def _logo() -> Image.Image | None:
    if not _LOGO_CACHE:
        try:
            _LOGO_CACHE.append(Image.open(LOGO_PATH).convert("RGBA"))
        except Exception as e:
            logger.warning(f"Logo não carregado ({LOGO_PATH}): {e}")
            _LOGO_CACHE.append(None)
    return _LOGO_CACHE[0]


def _pagina_branca(tamanho: tuple[int, int] = A4_RETRATO) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", tamanho, "white")
    return img, ImageDraw.Draw(img)


def _truncar(draw: ImageDraw.ImageDraw, texto: str, fonte, largura_max: int) -> str:
    """Trunca pelo comprimento RENDERIZADO (textlength), não por nº de
    caracteres -- é o que evita texto estourando a margem direita."""
    texto = str(texto or "").strip()
    if draw.textlength(texto, font=fonte) <= largura_max:
        return texto
    while texto and draw.textlength(texto + "…", font=fonte) > largura_max:
        texto = texto[:-1]
    return texto.rstrip() + "…"


def _limpar_titulo(codigo: str, titulo: str) -> str:
    """O title do serviço no VUUPT começa repetindo o código do pedido
    ('#PS-36008 - 024746 / CLIENTE ...') -- tira esse prefixo pra não
    duplicar com o código que já aparece na mesma linha."""
    titulo = str(titulo or "").strip()
    return re.sub(rf"^#?{re.escape(codigo)}\s*-\s*", "", titulo) if codigo else titulo


def _nome_cliente(titulo_limpo: str) -> str:
    """O title (sem o prefixo do código) vem como
    'ref / EMBARCADOR / CLIENTE' -- o cliente é o último segmento."""
    partes = [p.strip() for p in titulo_limpo.split("/") if p.strip()]
    return partes[-1] if partes else titulo_limpo


def _cabecalho(img: Image.Image, draw: ImageDraw.ImageDraw,
               titulo: str, subtitulo: str) -> int:
    """Cabeçalho padrão das páginas geradas: logo à esquerda, título e
    subtítulo à direita, barra verde-água. Dimensiona pela largura da
    própria página (capa em paisagem, canhoteira em retrato). Retorna o
    y onde o conteúdo pode começar."""
    logo = _logo()
    if logo is not None:
        img.paste(logo, (MARGEM, 78), logo)
    draw.text((img.width - MARGEM, 105), titulo, font=_fonte(48, True),
              fill=NAVY, anchor="rm")
    if subtitulo:
        draw.text((img.width - MARGEM, 165), subtitulo, font=_fonte(26),
                  fill=CINZA_TXT, anchor="rm")
    draw.rectangle([(MARGEM, 208), (img.width - MARGEM, 215)], fill=TEAL)
    return 250


def _rodape(img: Image.Image, draw: ImageDraw.ImageDraw):
    draw.text((img.width // 2, img.height - 58),
              f"Freshlog · romaneio gerado automaticamente em "
              f"{datetime.now().strftime('%d/%m/%Y %H:%M')}",
              font=_fonte(18), fill=CINZA_TXT, anchor="mm")


def _marca(draw: ImageDraw.ImageDraw, cx: int, cy: int, ok: bool):
    """Check verde / X vermelho desenhados na mão (Arial não tem esses
    glifos de forma confiável)."""
    if ok:
        draw.line([(cx - 11, cy + 1), (cx - 3, cy + 9), (cx + 12, cy - 9)],
                  fill=VERDE_OK, width=5, joint="curve")
    else:
        draw.line([(cx - 9, cy - 9), (cx + 9, cy + 9)], fill=VERMELHO, width=5)
        draw.line([(cx - 9, cy + 9), (cx + 9, cy - 9)], fill=VERMELHO, width=5)


# Colunas da tabela da capa em PAISAGEM (x em px, página de 1754). A
# coluna PEDIDO é larga porque a célula mostra código + nº da NF
# ('PS-36008 · NF 24746').
_COL_N, _COL_PEDIDO, _COL_EMB, _COL_CLI, _COL_END = 118, 160, 450, 630, 880
_COL_VOL_CX, _COL_PESO_CX = 1375, 1485
_COL_NF_CX, _COL_BOL_CX = 1575, 1630
_LARG_EMB, _LARG_CLI, _LARG_END = 165, 235, 450


def _tabela_header_capa(draw: ImageDraw.ImageDraw, y: int) -> int:
    draw.rectangle([(MARGEM, y), (A4_PAISAGEM[0] - MARGEM, y + 48)], fill=NAVY)
    f = _fonte(22, True)
    meio = y + 24
    draw.text((_COL_N, meio), "#", font=f, fill="white", anchor="lm")
    draw.text((_COL_PEDIDO, meio), "PEDIDO", font=f, fill="white", anchor="lm")
    draw.text((_COL_EMB, meio), "EMBARCADOR", font=f, fill="white", anchor="lm")
    draw.text((_COL_CLI, meio), "CLIENTE", font=f, fill="white", anchor="lm")
    draw.text((_COL_END, meio), "ENDEREÇO DE ENTREGA", font=f, fill="white", anchor="lm")
    draw.text((_COL_VOL_CX, meio), "VOL", font=f, fill="white", anchor="mm")
    draw.text((_COL_PESO_CX, meio), "PESO (KG)", font=f, fill="white", anchor="mm")
    draw.text((_COL_NF_CX, meio), "NF", font=f, fill="white", anchor="mm")
    draw.text((_COL_BOL_CX, meio), "BOL", font=f, fill="white", anchor="mm")
    return y + 48


def gerar_capa(rota: dict, itens: list[dict], nome_motorista: str,
               data_alvo: date, tem_canhoteira: bool,
               transportadoras: list[str] | None = None) -> list[Image.Image]:
    """Capa em PAISAGEM: identidade Freshlog + resumo da rota + tabela
    com 1 linha por pedido (ordem de visita) mostrando embarcador,
    cliente, endereço de entrega, volumes, peso e status de NF/boleto."""
    data_br = data_alvo.strftime("%d/%m/%Y")
    dia_semana = DIAS_SEMANA_PT[data_alvo.weekday()]
    nome_rota = rota.get("name") or f"Rota {rota.get('id')}"

    img, draw = _pagina_branca(A4_PAISAGEM)
    y = _cabecalho(img, draw, "ROMANEIO DE ENTREGAS", nome_rota)

    # Bloco de informações da rota
    def _info(x, rotulo, valor, anchor="ls"):
        draw.text((x, y + 18), rotulo, font=_fonte(20), fill=CINZA_TXT, anchor=anchor)
        draw.text((x, y + 56), valor, font=_fonte(30, True), fill=NAVY, anchor=anchor)
    _info(MARGEM, "DATA", f"{dia_semana}, {data_br}")
    _info(760, "MOTORISTA", _truncar(draw, nome_motorista, _fonte(30, True), 540))
    _info(A4_PAISAGEM[0] - MARGEM, "PEDIDOS", str(len(itens)), anchor="rs")
    avisos = []
    if tem_canhoteira:
        avisos.append("Inclui CANHOTEIRA nas páginas finais")
    if transportadoras:
        avisos.append("Inclui CANHOTEIRA DE TRANSPORTADORA: " + " · ".join(transportadoras))
    if avisos:
        fonte_aviso = _fonte(22, True)
        draw.text((MARGEM, y + 100),
                  _truncar(draw, "   |   ".join(avisos), fonte_aviso, A4_PAISAGEM[0] - 2 * MARGEM),
                  font=fonte_aviso, fill=VERDE_OK, anchor="ls")
    y += 130

    paginas = [img]
    y = _tabela_header_capa(draw, y)
    fonte_ped, fonte_txt, fonte_nf = _fonte(23, True), _fonte(22), _fonte(20)
    fonte_end = _fonte(20)
    altura_linha, limite_y = 46, A4_PAISAGEM[1] - 130

    for item in itens:
        if y + altura_linha > limite_y:            # nova página de continuação
            _rodape(img, draw)
            img, draw = _pagina_branca(A4_PAISAGEM)
            y = _cabecalho(img, draw, "ROMANEIO (continuação)", nome_rota)
            y = _tabela_header_capa(draw, y + 10)
            paginas.append(img)
        if item["posicao"] % 2 == 0:
            draw.rectangle([(MARGEM, y), (A4_PAISAGEM[0] - MARGEM, y + altura_linha)],
                           fill=CINZA_ZEBRA)
        meio = y + altura_linha // 2
        draw.text((_COL_N, meio), str(item["posicao"]), font=fonte_txt,
                  fill=CINZA_TXT, anchor="lm")
        draw.text((_COL_PEDIDO, meio), item["codigo"], font=fonte_ped,
                  fill=NAVY, anchor="lm")
        if item["nfs"]:
            x_nf = _COL_PEDIDO + draw.textlength(item["codigo"], font=fonte_ped) + 14
            larg_nf = _COL_EMB - 18 - x_nf
            if larg_nf > 40:
                rotulo_nf = "PV" if item.get("veio_de_pedido_venda") else "NF"
                draw.text((x_nf, meio), _truncar(draw, f"{rotulo_nf} {item['nfs']}", fonte_nf, larg_nf),
                          font=fonte_nf, fill=CINZA_TXT, anchor="lm")
        draw.text((_COL_EMB, meio), _truncar(draw, item["embarcador"], fonte_txt, _LARG_EMB),
                  font=fonte_txt, fill=NAVY, anchor="lm")
        draw.text((_COL_CLI, meio), _truncar(draw, item["cliente"], fonte_txt, _LARG_CLI),
                  font=fonte_txt, fill=NAVY, anchor="lm")
        draw.text((_COL_END, meio), _truncar(draw, item["endereco"], fonte_end, _LARG_END),
                  font=fonte_end, fill=NAVY, anchor="lm")
        draw.text((_COL_VOL_CX, meio), str(item["volumes"]) if item["volumes"] else "—",
                  font=fonte_txt, fill=NAVY, anchor="mm")
        draw.text((_COL_PESO_CX, meio), _fmt_peso(item["peso"]),
                  font=fonte_txt, fill=NAVY, anchor="mm")
        if item.get("nf_dispensada"):
            draw.text((_COL_NF_CX, meio), "—", font=fonte_txt, fill=CINZA_TXT, anchor="mm")
        else:
            _marca(draw, _COL_NF_CX, meio, item["tem_nf"])
        _marca(draw, _COL_BOL_CX, meio, item["tem_boleto"])
        y += altura_linha

    _rodape(img, draw)
    return paginas


def gerar_canhoteira(rota: dict, entregas: list[dict], nome_motorista: str,
                     data_alvo: date) -> list[Image.Image]:
    """Folha(s) de canhoteira: tabela na ordem da rota com as entregas
    dos embarcadores monitorados e campos de recebedor/data/assinatura.
    Identifica motorista, rota e dia no cabeçalho (pedido do Hugo)."""
    data_br = data_alvo.strftime("%d/%m/%Y")
    nome_rota = rota.get("name") or f"Rota {rota.get('id')}"
    embarcadores_presentes = " · ".join(sorted({e["embarcador"] for e in entregas}))

    def _nova_pagina(continuacao: bool = False):
        img, draw = _pagina_branca()
        titulo = "CANHOTEIRA" + (" (continuação)" if continuacao else "")
        y = _cabecalho(img, draw, titulo, nome_rota)
        draw.text((MARGEM, y + 20), f"Motorista: {nome_motorista}   ·   {nome_rota}   ·   {data_br}",
                  font=_fonte(26, True), fill=NAVY, anchor="ls")
        draw.text((MARGEM, y + 58), f"Recolher canhoto assinado de cada entrega abaixo — "
                                    f"{embarcadores_presentes}",
                  font=_fonte(21), fill=CINZA_TXT, anchor="ls")
        y += 90
        # header da tabela
        draw.rectangle([(MARGEM, y), (A4_RETRATO[0] - MARGEM, y + 48)], fill=NAVY)
        f = _fonte(22, True)
        draw.text((118, y + 24), "PARADA", font=f, fill="white", anchor="lm")
        draw.text((250, y + 24), "ENTREGA", font=f, fill="white", anchor="lm")
        draw.text((700, y + 24), "RECEBEDOR / DATA / ASSINATURA", font=f,
                  fill="white", anchor="lm")
        return img, draw, y + 48

    paginas = []
    img, draw, y = _nova_pagina()
    paginas.append(img)
    altura_linha, limite_y = 150, A4_RETRATO[1] - 130
    fonte_rotulo = _fonte(20)

    for entrega in entregas:
        if y + altura_linha > limite_y:
            _rodape(img, draw)
            img, draw, y = _nova_pagina(continuacao=True)
            paginas.append(img)

        # coluna parada
        draw.text((140, y + altura_linha // 2), str(entrega["posicao"]),
                  font=_fonte(34, True), fill=NAVY, anchor="mm")
        # coluna entrega (3 linhas)
        nfs = entrega["nfs"] or "—"
        draw.text((250, y + 40), f"{entrega['codigo']}  ·  NF {nfs}",
                  font=_fonte(24, True), fill=NAVY, anchor="ls")
        draw.text((250, y + 78), entrega["embarcador"], font=_fonte(22),
                  fill=VERDE_OK, anchor="ls")
        draw.text((250, y + 114), _truncar(draw, entrega["cliente"], _fonte(22), 400),
                  font=_fonte(22), fill=CINZA_TXT, anchor="ls")
        # coluna assinatura
        draw.text((700, y + 52), "Recebedor:", font=fonte_rotulo, fill=CINZA_TXT, anchor="ls")
        draw.line([(830, y + 56), (1130, y + 56)], fill=CINZA_LINHA, width=2)
        draw.text((700, y + 116), "Assinatura:", font=fonte_rotulo, fill=CINZA_TXT, anchor="ls")
        draw.line([(830, y + 120), (1000, y + 120)], fill=CINZA_LINHA, width=2)
        draw.text((1020, y + 116), "Data:", font=fonte_rotulo, fill=CINZA_TXT, anchor="ls")
        draw.line([(1080, y + 120), (1130, y + 120)], fill=CINZA_LINHA, width=2)

        draw.line([(MARGEM, y + altura_linha), (A4_RETRATO[0] - MARGEM, y + altura_linha)],
                  fill=CINZA_LINHA, width=2)
        y += altura_linha

    _rodape(img, draw)
    return paginas


# Colunas da canhoteira de transportadora (retrato, página de 1240).
_CT_COL_N, _CT_COL_PED, _CT_COL_EMB, _CT_COL_DEST = 118, 170, 470, 690
_CT_COL_VOL_CX, _CT_COL_OK_CX = 1010, 1100
_CT_LARG_PED, _CT_LARG_EMB, _CT_LARG_DEST = 285, 205, 280
_CT_ALTURA_LINHA = 72
_CT_ALTURA_RECEBIMENTO = 300


def _somar_volumes(entregas: list[dict]) -> str:
    """'12' quando todos os pedidos têm volumes; '12 (parcial)' quando
    algum não tem (a transportadora confere no físico)."""
    conhecidos = [e["volumes"] for e in entregas if e.get("volumes")]
    if not conhecidos:
        return "—"
    total = str(sum(conhecidos))
    return total if len(conhecidos) == len(entregas) else f"{total} (parcial)"


def gerar_canhoteira_transportadora(rota: dict, ponto: PontoRedespacho, entregas: list[dict],
                                    nome_motorista: str, data_alvo: date) -> list[Image.Image]:
    """Folha(s) de canhoteira de UMA transportadora de redespacho: só os
    pedidos da rota que o motorista entrega naquele galpão, na ordem da
    rota, com pedido/NF, embarcador, destinatário final e volumes, e um
    bloco de recebimento (nome, documento, data/hora, assinatura e
    carimbo) no fim -- a transportadora assina o lote inteiro, e a
    coluna OK serve pra ela conferir pedido a pedido (pedido do Hugo,
    10/09: 1 canhoteira por transportadora)."""
    data_br = data_alvo.strftime("%d/%m/%Y")
    nome_rota = rota.get("name") or f"Rota {rota.get('id')}"
    nome_transp = (ponto.nome or "").strip().upper() or "TRANSPORTADORA"
    endereco_galpao = str(ponto.endereco)
    total_volumes = _somar_volumes(entregas)

    def _nova_pagina(continuacao: bool = False):
        img, draw = _pagina_branca()
        titulo = "CANHOTEIRA · TRANSPORTADORA" + (" (cont.)" if continuacao else "")
        y = _cabecalho(img, draw, titulo, nome_rota)
        largura = A4_RETRATO[0] - 2 * MARGEM
        draw.text((MARGEM, y + 24), _truncar(draw, nome_transp, _fonte(36, True), largura),
                  font=_fonte(36, True), fill=NAVY, anchor="ls")
        draw.text((MARGEM, y + 60), _truncar(draw, endereco_galpao, _fonte(21), largura),
                  font=_fonte(21), fill=CINZA_TXT, anchor="ls")
        # a rota já está no subtítulo do cabeçalho -- aqui só motorista e dia
        draw.text((MARGEM, y + 104),
                  _truncar(draw, f"Motorista: {nome_motorista}   ·   {data_br}",
                           _fonte(24, True), largura),
                  font=_fonte(24, True), fill=NAVY, anchor="ls")
        draw.text((MARGEM, y + 140),
                  f"Conferir e assinar o recebimento dos pedidos abaixo — "
                  f"{len(entregas)} pedido(s), {total_volumes} volume(s)",
                  font=_fonte(21), fill=CINZA_TXT, anchor="ls")
        y += 170
        draw.rectangle([(MARGEM, y), (A4_RETRATO[0] - MARGEM, y + 48)], fill=NAVY)
        f = _fonte(22, True)
        meio = y + 24
        draw.text((_CT_COL_N, meio), "#", font=f, fill="white", anchor="lm")
        draw.text((_CT_COL_PED, meio), "PEDIDO / NF", font=f, fill="white", anchor="lm")
        draw.text((_CT_COL_EMB, meio), "EMBARCADOR", font=f, fill="white", anchor="lm")
        draw.text((_CT_COL_DEST, meio), "DESTINATÁRIO", font=f, fill="white", anchor="lm")
        draw.text((_CT_COL_VOL_CX, meio), "VOL", font=f, fill="white", anchor="mm")
        draw.text((_CT_COL_OK_CX, meio), "OK", font=f, fill="white", anchor="mm")
        return img, draw, y + 48

    paginas = []
    img, draw, y = _nova_pagina()
    paginas.append(img)
    limite_y = A4_RETRATO[1] - 130
    fonte_ped, fonte_nf, fonte_txt = _fonte(23, True), _fonte(19), _fonte(21)

    for n, entrega in enumerate(entregas):
        if y + _CT_ALTURA_LINHA > limite_y:
            _rodape(img, draw)
            img, draw, y = _nova_pagina(continuacao=True)
            paginas.append(img)
        if n % 2 == 0:
            draw.rectangle([(MARGEM, y), (A4_RETRATO[0] - MARGEM, y + _CT_ALTURA_LINHA)],
                           fill=CINZA_ZEBRA)
        meio = y + _CT_ALTURA_LINHA // 2
        draw.text((_CT_COL_N, meio), str(entrega["posicao"]), font=fonte_txt,
                  fill=CINZA_TXT, anchor="lm")
        draw.text((_CT_COL_PED, y + 30), _truncar(draw, entrega["codigo"], fonte_ped, _CT_LARG_PED),
                  font=fonte_ped, fill=NAVY, anchor="ls")
        rotulo_nf = "PV" if entrega.get("veio_de_pedido_venda") else "NF"
        draw.text((_CT_COL_PED, y + 56),
                  _truncar(draw, f"{rotulo_nf} {entrega['nfs']}" if entrega["nfs"] else "sem NF",
                           fonte_nf, _CT_LARG_PED),
                  font=fonte_nf, fill=CINZA_TXT, anchor="ls")
        draw.text((_CT_COL_EMB, meio), _truncar(draw, entrega["embarcador"], fonte_txt, _CT_LARG_EMB),
                  font=fonte_txt, fill=NAVY, anchor="lm")
        draw.text((_CT_COL_DEST, meio), _truncar(draw, entrega["cliente"], fonte_txt, _CT_LARG_DEST),
                  font=fonte_txt, fill=NAVY, anchor="lm")
        draw.text((_CT_COL_VOL_CX, meio), str(entrega["volumes"]) if entrega["volumes"] else "—",
                  font=fonte_txt, fill=NAVY, anchor="mm")
        draw.rectangle([(_CT_COL_OK_CX - 15, meio - 15), (_CT_COL_OK_CX + 15, meio + 15)],
                       outline=CINZA_TXT, width=2)
        draw.line([(MARGEM, y + _CT_ALTURA_LINHA), (A4_RETRATO[0] - MARGEM, y + _CT_ALTURA_LINHA)],
                  fill=CINZA_LINHA, width=1)
        y += _CT_ALTURA_LINHA

    # Bloco de recebimento -- sempre na última folha, inteiro.
    if y + _CT_ALTURA_RECEBIMENTO > limite_y:
        _rodape(img, draw)
        img, draw, y = _nova_pagina(continuacao=True)
        paginas.append(img)
    y += 40
    fonte_rotulo = _fonte(20)
    x_dir = A4_RETRATO[0] - MARGEM
    draw.text((MARGEM, y), "RECEBIMENTO PELA TRANSPORTADORA", font=_fonte(22, True),
              fill=NAVY, anchor="ls")
    draw.text((x_dir, y), f"Total: {len(entregas)} pedido(s) · {total_volumes} volume(s)",
              font=_fonte(20, True), fill=NAVY, anchor="rs")
    draw.line([(MARGEM, y + 12), (x_dir, y + 12)], fill=TEAL, width=3)
    y += 70
    draw.text((MARGEM, y), "Recebido por (nome legível):", font=fonte_rotulo, fill=CINZA_TXT, anchor="ls")
    draw.line([(MARGEM + 270, y + 4), (MARGEM + 620, y + 4)], fill=CINZA_LINHA, width=2)
    draw.text((MARGEM + 650, y), "Documento:", font=fonte_rotulo, fill=CINZA_TXT, anchor="ls")
    draw.line([(MARGEM + 760, y + 4), (x_dir, y + 4)], fill=CINZA_LINHA, width=2)
    y += 70
    draw.text((MARGEM, y), "Data:  ____ / ____ / ________      Hora:  ____ : ____",
              font=fonte_rotulo, fill=CINZA_TXT, anchor="ls")
    draw.text((MARGEM + 650, y), "Assinatura e carimbo:", font=fonte_rotulo, fill=CINZA_TXT, anchor="ls")
    draw.rectangle([(MARGEM + 650, y + 20), (x_dir, y + 130)], outline=CINZA_LINHA, width=2)
    y += 70
    draw.text((MARGEM, y), "Ocorrências (avaria, falta, recusa):", font=fonte_rotulo,
              fill=CINZA_TXT, anchor="ls")
    draw.line([(MARGEM, y + 44), (MARGEM + 600, y + 44)], fill=CINZA_LINHA, width=2)
    draw.line([(MARGEM, y + 84), (MARGEM + 600, y + 84)], fill=CINZA_LINHA, width=2)

    _rodape(img, draw)
    return paginas


def _paginas_pillow(imagens: list[Image.Image], buffers_vivos: list) -> list:
    """Converte páginas Pillow em páginas pypdf. O BytesIO precisa
    continuar vivo até o writer.write() (pypdf lê o stream de forma
    lazy) -- por isso ele é acumulado em buffers_vivos pelo chamador."""
    buf = io.BytesIO()
    imagens[0].save(buf, format="PDF", save_all=True,
                    append_images=imagens[1:], resolution=DPI)
    buf.seek(0)
    buffers_vivos.append(buf)
    return list(PdfReader(buf).pages)


# ---------------------------------------------------------------------------
# Montagem do PDF da rota
# ---------------------------------------------------------------------------

def _abrir_documentos(rows: list[dict]) -> tuple[list[tuple[dict, PdfReader]], list[str]]:
    """
    Resolve o arquivo local e valida que o PDF abre. Devolve (lista de
    (registro, reader) utilizáveis, lista de problemas legíveis pra
    pendência). Arquivo ausente ou PDF corrompido não derruba a rota.
    """
    abertos, problemas = [], []
    for row in rows:
        nome = row.get("nome_arquivo") or "?"
        caminho = resolver_arquivo_local(nome)
        if caminho is None:
            problemas.append(f"arquivo não localizado: {nome}")
            continue
        try:
            reader = PdfReader(str(caminho))
            if reader.is_encrypted:
                reader.decrypt("")
            _ = len(reader.pages)               # força o parse -- pega PDF corrompido aqui
            abertos.append((row, reader))
        except Exception as e:
            problemas.append(f"PDF ilegível: {nome}")
            logger.warning(f"  PDF ilegível ({nome}): {e}")
    return abertos, problemas


def montar_pdf_rota(rota: dict, servicos: list[dict], docs_por_pedido: dict,
                    embarcadores: dict[int, str], fatores: dict[int, float],
                    nome_motorista: str, data_alvo: date, caminho_saida: Path,
                    catalogo_transportadoras: CatalogoTransportadoras | None = None) -> dict:
    """catalogo_transportadoras: BD_TRANSPORTADORAS já carregada (job das
    04h carrega uma vez pra todas as rotas). Omitido -> carrega aqui
    (botão "Imprimir rota", expedição, documentação automática)."""
    if catalogo_transportadoras is None:
        catalogo_transportadoras = carregar_catalogo_transportadoras()
    writer = PdfWriter()
    buffers_vivos: list = []        # BytesIO das páginas Pillow -- vivos até o write()
    readers_vivos: list = []        # PdfReaders dos arquivos -- idem
    pendencias: list[str] = []
    total_nfs = total_boletos = 0

    # Passada 1: resolve e valida os documentos de todos os pedidos --
    # a capa precisa do status de tudo antes de qualquer página.
    itens: list[dict] = []
    total = len(servicos)
    for posicao, s in enumerate(servicos, start=1):
        codigo = (s.get("code") or "").lstrip("#")
        titulo_limpo = _limpar_titulo(codigo, s.get("title"))
        sender_id = s.get("sender_id")
        embarcador = embarcadores.get(sender_id) or SENDERS_CANHOTEIRA.get(sender_id) or ""
        docs = [d for sub in _codigos_base_lista(codigo) for d in docs_por_pedido.get(sub, [])]

        nf_dispensada = sender_id in SENDERS_SEM_NF
        if nf_dispensada:
            # Esses embarcadores são controlados pela CANHOTEIRA, não
            # pela NF -- mesmo que uma NF antiga esteja no banco
            # (achado real, 20/08: NF de Padrão Puro indo emendada no
            # romaneio de entrega), ela nunca é aberta, emendada ou
            # contada pra eles.
            nfs, problemas_nf = [], []
        else:
            nfs, problemas_nf = _abrir_documentos(selecionar_nfs(docs))
            # Sem NF, mas De Tommaso costuma mandar um "Pedido de Venda"
            # padronizado no lugar dela (pedido do Hugo, 20/08) -- conta
            # como se fosse a própria NF daqui pra baixo (capa, volumes/
            # peso, páginas emendadas), só o número exibido leva "PV ".
            if not nfs and sender_id in SENDERS_PEDIDO_VENDA_SUBSTITUI_NF:
                nfs, problemas_nf = _abrir_documentos(selecionar_pedidos_de_venda(docs))
        boletos, problemas_bol = _abrir_documentos(selecionar_boletos(docs))

        faltas = []
        if not nfs and not nf_dispensada:
            faltas.append("sem nota fiscal")
        if not boletos:
            faltas.append("sem boleto")
        faltas.extend(problemas_nf + problemas_bol)
        pendencias.extend(f"{codigo}: {f}" for f in faltas)

        numeros_nf = [_nf_norm(row.get("numero_nf")) for row, _r in nfs]
        veio_de_pedido_venda = bool(nfs) and all(
            row.get("tipo") == "Pedido de Venda" for row, _r in nfs)

        # Volumes e peso bruto: DANFE primeiro (contagem real); sem NF
        # legível, volumes caem pro dimension_3 despoderado e o peso
        # fica sem valor ("—" na capa).
        volumes, peso = _qtd_peso_do_pedido(nfs)
        if volumes is None:
            volumes = _volumes_fallback(s, fatores.get(sender_id, 1.0))

        itens.append({
            "posicao": posicao, "codigo": codigo,
            "embarcador": embarcador, "cliente": _nome_cliente(titulo_limpo),
            "sender_id": sender_id,
            "endereco": _endereco_entrega(s),
            # bruto (com CEP): é o que o batimento de transportadora usa
            "endereco_vuupt": str(s.get("address") or ""),
            "volumes": volumes, "peso": peso,
            "nfs": ", ".join(n for n in numeros_nf if n),
            "veio_de_pedido_venda": veio_de_pedido_venda,
            "tem_nf": bool(nfs), "tem_boleto": bool(boletos),
            # NF dispensada -> capa mostra "—" no lugar da marca (nfs
            # já vem sempre vazio pra esses embarcadores, ver acima).
            "nf_dispensada": nf_dispensada,
            "_abertos_nf": nfs, "_abertos_bol": boletos,
        })

    entregas_canhoteira = [i for i in itens if i["sender_id"] in SENDERS_CANHOTEIRA]
    for i in entregas_canhoteira:               # nome padronizado na canhoteira
        i["embarcador"] = SENDERS_CANHOTEIRA[i["sender_id"]]

    # Pedidos entregues em galpão de transportadora (1 folha por galpão)
    grupos_transportadora = agrupar_por_transportadora(itens, catalogo_transportadoras)
    # Nome curto (sem LTDA/TRANSPORTES...) pra capa e resumo -- a folha em
    # si leva o nome completo da planilha. Mesma chave do fingerprint da
    # notificação de transportadoras.
    nomes_transportadoras = [nome_curto_transportadora(p) for p, _e in grupos_transportadora]

    # Capa
    for pagina in _paginas_pillow(
            gerar_capa(rota, itens, nome_motorista, data_alvo,
                       tem_canhoteira=bool(entregas_canhoteira),
                       transportadoras=nomes_transportadoras),
            buffers_vivos):
        writer.add_page(pagina)

    # Documentos emendados: NFs e depois boletos, pedido a pedido.
    for item in itens:
        for _row, reader in item["_abertos_nf"]:
            readers_vivos.append(reader)
            for pagina in reader.pages:
                writer.add_page(pagina)
            total_nfs += 1
        for _row, reader in item["_abertos_bol"]:
            readers_vivos.append(reader)
            for pagina in reader.pages:
                writer.add_page(pagina)
            total_boletos += 1

    # Canhoteira (só quando a rota tem entrega dos embarcadores monitorados)
    if entregas_canhoteira:
        for pagina in _paginas_pillow(
                gerar_canhoteira(rota, entregas_canhoteira, nome_motorista, data_alvo),
                buffers_vivos):
            writer.add_page(pagina)

    # Canhoteira de transportadora: 1 folha (ou mais) por galpão da rota
    for ponto, entregas in grupos_transportadora:
        for pagina in _paginas_pillow(
                gerar_canhoteira_transportadora(rota, ponto, entregas, nome_motorista, data_alvo),
                buffers_vivos):
            writer.add_page(pagina)

    caminho_saida.parent.mkdir(parents=True, exist_ok=True)
    with open(caminho_saida, "wb") as f:
        writer.write(f)

    return {"pedidos": total, "paginas": len(writer.pages),
            "nfs": total_nfs, "boletos": total_boletos,
            "canhoteira": len(entregas_canhoteira), "pendencias": pendencias,
            # [(nome da transportadora, nº de pedidos)] na ordem da rota
            "canhoteira_transportadoras": [(nome, len(e)) for nome, (_p, e)
                                           in zip(nomes_transportadoras, grupos_transportadora)]}


def nome_arquivo_saida(rota: dict, nome_motorista: str, data_alvo: date) -> str:
    m = re.search(r"#(\d+)", rota.get("name") or "")
    numero = m.group(1) if m else "X"
    slug = re.sub(r"[^A-Z0-9]+", "_", _normalizar_texto(nome_motorista)).strip("_") \
        or "SEM_MOTORISTA"
    return f"romaneio_{data_alvo.isoformat()}_rota{numero}_id{rota.get('id')}_{slug}.pdf"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(modo_teste: bool, data_str: str, rota_id: int | None) -> int:
    inicio = time.time()
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")

    cfg_motoristas = config.get("motoristas", {})
    catalogo = CatalogoMotoristas.carregar(
        cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""),
    )
    nome_por_agent_id = {m.agent_id: m.nome for m in catalogo.motoristas}
    embarcadores, fatores = carregar_embarcadores()
    catalogo_transp = carregar_catalogo_transportadoras()

    data_alvo = _parse_data(data_str)
    data_br = data_alvo.strftime("%d/%m/%Y")
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Gerando PDFs de romaneio para {data_br}.")

    rotas = buscar_rotas_do_dia(token, data_alvo)
    if rota_id is not None:
        rotas = [r for r in rotas if r.get("id") == rota_id]
        if not rotas:
            logger.error(f"Rota {rota_id} não encontrada entre as rotas de {data_br} "
                         f"(ou está cancelada).")
            return 1
    logger.info(f"{len(rotas)} rota(s) para {data_br}.")

    pasta = PASTA_ROMANEIOS / ("teste" if modo_teste else "") / data_alvo.isoformat()
    pasta.mkdir(parents=True, exist_ok=True)

    # Idempotência: a pasta é 100% regenerável. Execução completa limpa
    # tudo da data (remove órfãos de rota renomeada/motorista trocado);
    # com --rota, limpa só os PDFs daquela rota.
    padrao = f"*_id{rota_id}_*.pdf" if rota_id is not None else "romaneio_*.pdf"
    for antigo in pasta.glob(padrao):
        antigo.unlink()

    codigos: set[str] = set()
    rotas_com_servicos = []
    for r in rotas:
        servicos = _extrair_servicos_da_rota(r)
        if not servicos:
            logger.warning(f"Rota {r.get('name')} (id {r.get('id')}) sem serviços -- pulada.")
            continue
        rotas_com_servicos.append((r, servicos))
        for s in servicos:
            codigos.update(_codigos_base_lista(s.get("code") or ""))

    docs_por_pedido, em_revisao = carregar_documentos_por_pedido(codigos)
    logger.info(f"{sum(len(v) for v in docs_por_pedido.values())} documento(s) ENVIADO(s) "
                f"encontrados para {len(codigos)} pedido(s); {em_revisao} em REVISAO_MANUAL.")

    resumo_etapas: dict = {}
    linhas_resumo: list[str] = [f"Romaneios de {data_br} -- gerados em "
                                f"{time.strftime('%d/%m/%Y %H:%M:%S')}", ""]
    todas_pendencias: list[str] = []
    gerados = com_erro = 0

    for rota, servicos in rotas_com_servicos:
        agent_id = rota.get("agent_id")
        nome_motorista = nome_por_agent_id.get(agent_id, "(sem motorista)")
        etiqueta = f"{rota.get('name')} — {nome_motorista}"
        caminho_saida = pasta / nome_arquivo_saida(rota, nome_motorista, data_alvo)
        try:
            stats = montar_pdf_rota(rota, servicos, docs_por_pedido, embarcadores,
                                    fatores, nome_motorista, data_alvo, caminho_saida,
                                    catalogo_transportadoras=catalogo_transp)
            gerados += 1
            transp = stats.get("canhoteira_transportadoras") or []
            detalhe = (f"{stats['pedidos']} pedido(s), {stats['nfs']} NF(s), "
                       f"{stats['boletos']} boleto(s), {stats['paginas']} página(s), "
                       f"{len(stats['pendencias'])} pendência(s)"
                       + (f", canhoteira com {stats['canhoteira']} entrega(s)"
                          if stats["canhoteira"] else "")
                       + (", canhoteira de transportadora: "
                          + ", ".join(f"{nome} ({qtd})" for nome, qtd in transp)
                          if transp else ""))
            resumo_etapas[etiqueta] = {"status": "ok", "detalhe": detalhe}
            linhas_resumo.append(f"[OK] {etiqueta}: {detalhe} -> {caminho_saida.name}")
            todas_pendencias.extend(stats["pendencias"])
            logger.info(f"  {caminho_saida.name}: {detalhe}")
        except Exception as e:
            com_erro += 1
            resumo_etapas[etiqueta] = {"status": "erro", "detalhe": str(e)}
            linhas_resumo.append(f"[ERRO] {etiqueta}: {e}")
            logger.exception(f"Erro ao montar PDF da rota {rota.get('id')}: {e}")

    if todas_pendencias:
        linhas_resumo += ["", "PENDÊNCIAS:"] + [f"  - {p}" for p in todas_pendencias]
    if em_revisao:
        linhas_resumo += ["", f"{em_revisao} documento(s) desses pedidos em REVISAO_MANUAL "
                              f"(fora do PDF -- resolver no fluxo de documentos)."]

    # Com --rota o resumo cobriria só aquela rota -- não sobrescrever o
    # _resumo.txt da execução completa da data.
    if rota_id is None:
        (pasta / "_resumo.txt").write_text("\n".join(linhas_resumo) + "\n", encoding="utf-8")

    resumo_etapas["Resumo geral"] = {
        "status": "erro" if com_erro else "ok",
        "detalhe": f"{gerados} PDF(s) gerado(s), {com_erro} erro(s), "
                  f"{len(todas_pendencias)} pendência(s), {em_revisao} doc(s) em revisão manual",
    }

    duracao = time.time() - inicio
    logger.info(f"Concluído em {duracao:.1f}s: {resumo_etapas['Resumo geral']['detalhe']}. "
                f"Saída: {pasta}")

    if modo_teste:
        logger.info("[MODO TESTE] Notificação de execução não enviada.")
    else:
        try:
            notificar_execucao(resumo_etapas, duracao, modo_teste, config)
        except Exception as e:
            logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")

    return 1 if com_erro else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Gera 1 PDF por rota do dia com NFs e boletos na ordem de visita")
    parser.add_argument("--modo-teste", action="store_true",
                        help="Gera os PDFs em dados/romaneios/teste/ e não envia notificação")
    parser.add_argument("--data", default="hoje",
                        help="Data alvo: 'hoje' (padrão), 'amanhã' ou DD/MM/AAAA")
    parser.add_argument("--rota", type=int, default=None,
                        help="route_id do VUUPT: regenera só o PDF dessa rota")
    args = parser.parse_args()
    sys.exit(main(modo_teste=args.modo_teste, data_str=args.data, rota_id=args.rota))
