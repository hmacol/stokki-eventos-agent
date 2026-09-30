# -*- coding: utf-8 -*-
"""
telefone_origem.py

Busca o telefone COMPLETO e correto do destinatário direto no XML
original da NF-e (a fonte da verdade), em vez de confiar no campo
"Telefone" da tela de detalhe da Stokki — que, confirmado com XMLs
reais em 27/07 (PS-35089 e PS-35088), vem com o ÚLTIMO DÍGITO CORTADO
para números de celular.

O problema de desambiguação: um mesmo destinatário (CNPJ/CPF) pode ter
várias notas fiscais (compras em datas diferentes). Pegar a primeira
que bater o documento é arriscado — foi exatamente esse erro que gerou
um falso positivo no teste anterior (pegou a nota errada, número
totalmente diferente). Este módulo desempata por CEP e, na falta dele,
por similaridade do endereço — e se não conseguir desempatar com
confiança, NÃO adivinha: reporta ambíguo.

Uso no pipeline:

    indice = indexar_xmls(pasta_xmls)  # uma vez, no início da execução
    resultado = telefone_correto(doc, endereco_stokki, cep_stokki, indice)
    if resultado.encontrado:
        telefone_e164 = _normalizar_telefone_e164(resultado.fone)
"""
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

logger = logging.getLogger(__name__)


def _so_digitos(s: str) -> str:
    return "".join(c for c in str(s or "") if c.isdigit())


def _normalizar_texto(s: str) -> str:
    """Maiúsculas, sem acento, espaços colapsados — mesmo padrão usado
    em regras/transportadoras.py e geocodificacao.py."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.upper().split())


def _tag_local(el) -> str:
    return el.tag.split("}")[-1]


@dataclass
class CandidatoXML:
    arquivo: str
    nome: str
    fone: str          # dígitos, sem +55 (ex: "11998499111")
    endereco_norm: str
    cep: str            # dígitos
    nnf: str = ""


@dataclass
class ResultadoBusca:
    encontrado: bool
    fone: str = ""              # dígitos completos, sem +55
    status: str = "nao_encontrado"   # unica | confirmada_cep | confirmada_endereco | ambigua | nao_encontrado
    candidatos: list = field(default_factory=list)  # para status ambigua/log
    detalhe: str = ""


def indexar_xmls(pasta: Path | str) -> dict[str, list[CandidatoXML]]:
    """
    Varre um diretório (recursivo) de XMLs de NF-e e monta um índice
    {documento_destinatario: [CandidatoXML, ...]}.

    Namespace-agnóstico (compara pelo nome local da tag, não pela URI
    completa) — funciona tanto com nfeProc quanto NFe "solta".
    """
    pasta = Path(pasta)
    indice: dict[str, list[CandidatoXML]] = {}
    if not pasta.exists():
        logger.warning(f"Pasta de XMLs não encontrada: {pasta} — busca de telefone por XML desativada.")
        return indice

    arquivos = list(pasta.rglob("*.xml"))
    lidos = 0
    for arq in arquivos:
        try:
            tree = ET.parse(arq)
        except ET.ParseError:
            continue

        nnf = ""
        for el in tree.iter():
            if _tag_local(el) == "nNF":
                nnf = (el.text or "").strip()
                break

        for el in tree.iter():
            if _tag_local(el) != "dest":
                continue
            doc, nome, fone = "", "", ""
            partes_end = {"lgr": "", "nro": "", "bairro": "", "mun": "", "uf": "", "cep": ""}
            for filho in el.iter():
                t = _tag_local(filho)
                txt = (filho.text or "").strip()
                if t in ("CNPJ", "CPF") and not doc:
                    doc = _so_digitos(txt)
                elif t == "xNome" and not nome:
                    nome = txt
                elif t == "fone" and not fone:
                    fone = _so_digitos(txt)
                elif t == "xLgr" and not partes_end["lgr"]:
                    partes_end["lgr"] = txt
                elif t == "nro" and not partes_end["nro"]:
                    partes_end["nro"] = txt
                elif t == "xBairro" and not partes_end["bairro"]:
                    partes_end["bairro"] = txt
                elif t == "xMun" and not partes_end["mun"]:
                    partes_end["mun"] = txt
                elif t == "UF" and not partes_end["uf"]:
                    partes_end["uf"] = txt
                elif t == "CEP" and not partes_end["cep"]:
                    partes_end["cep"] = _so_digitos(txt)

            if doc and fone:
                endereco_norm = _normalizar_texto(
                    f"{partes_end['lgr']} {partes_end['nro']} {partes_end['bairro']} {partes_end['mun']} {partes_end['uf']}"
                )
                indice.setdefault(doc, []).append(CandidatoXML(
                    arquivo=arq.name, nome=nome, fone=fone,
                    endereco_norm=endereco_norm, cep=partes_end["cep"], nnf=nnf,
                ))
                lidos += 1
            break  # só o primeiro <dest> por arquivo

    logger.info(f"Índice de XMLs: {lidos} destinatário(s) em {len(arquivos)} arquivo(s), "
                f"{len(indice)} documento(s) único(s).")
    return indice


def telefone_correto(doc: str, endereco_stokki: str, cep_stokki: str,
                     indice: dict[str, list[CandidatoXML]]) -> ResultadoBusca:
    """
    Busca o telefone correto para um destinatário, desempatando entre
    múltiplas notas do mesmo documento quando necessário.

    doc: CNPJ/CPF do destinatário (com ou sem máscara — normaliza aqui)
    endereco_stokki: endereço de entrega já resolvido pelo pipeline
                     (para desempate por similaridade)
    cep_stokki: CEP do destino, se disponível (desempate mais forte)
    """
    doc_norm = _so_digitos(doc)
    if not doc_norm:
        return ResultadoBusca(encontrado=False, status="nao_encontrado",
                              detalhe="documento do destinatário vazio")

    candidatos = indice.get(doc_norm, [])
    if not candidatos:
        return ResultadoBusca(encontrado=False, status="nao_encontrado",
                              detalhe=f"nenhum XML para o documento {doc_norm}")

    if len(candidatos) == 1:
        c = candidatos[0]
        return ResultadoBusca(encontrado=True, fone=c.fone, status="unica",
                              candidatos=candidatos,
                              detalhe=f"única nota encontrada ({c.arquivo})")

    # Múltiplos candidatos — tenta desempatar por CEP exato primeiro
    # (sinal mais forte: mesmo CEP praticamente garante mesmo endereço)
    cep_norm = _so_digitos(cep_stokki)
    if cep_norm:
        bater_cep = [c for c in candidatos if c.cep == cep_norm]
        if len(bater_cep) == 1:
            c = bater_cep[0]
            return ResultadoBusca(encontrado=True, fone=c.fone, status="confirmada_cep",
                                  candidatos=candidatos,
                                  detalhe=f"desempatado por CEP ({c.arquivo})")

    # Desempate por similaridade de endereço
    end_stokki_norm = _normalizar_texto(endereco_stokki)
    if end_stokki_norm:
        pontuados = [
            (SequenceMatcher(None, end_stokki_norm, c.endereco_norm).ratio(), c)
            for c in candidatos
        ]
        pontuados.sort(key=lambda x: x[0], reverse=True)
        melhor_score, melhor = pontuados[0]
        segundo_score = pontuados[1][0] if len(pontuados) > 1 else 0.0
        # Exige score alto E margem clara sobre o segundo colocado —
        # senão é melhor não adivinhar.
        if melhor_score >= 0.6 and (melhor_score - segundo_score) >= 0.15:
            return ResultadoBusca(
                encontrado=True, fone=melhor.fone, status="confirmada_endereco",
                candidatos=candidatos,
                detalhe=f"desempatado por endereço ({melhor.arquivo}, score={melhor_score:.2f})"
            )

    return ResultadoBusca(
        encontrado=False, status="ambigua", candidatos=candidatos,
        detalhe=f"{len(candidatos)} notas para o documento {doc_norm}, "
                f"não foi possível desempatar com confiança"
    )
