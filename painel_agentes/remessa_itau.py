# -*- coding: utf-8 -*-
"""
Remessa Itau (Sispag CNAB 240, pagamento por Pix): modulo PURO, sem banco e sem Flask.

Layout: docs/referencias/itau-sispag-cnab240-2020.txt (manual oficial, outubro/2020).
Cada campo cita a posicao do manual (inicio-fim, base 1).
"""
import re
from dataclasses import dataclass


def _digitos(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


# ── Chave Pix ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChavePix:
    tipo: str            # segmento B pos. 15-16: 01 telefone, 02 e-mail, 03 CPF/CNPJ, 04 aleatoria
    valor: str           # segmento B pos. 128-227
    documento: str       # CPF/CNPJ do favorecido (so digitos): segmento A 204-217 / B 19-32
    tipo_inscricao: int  # segmento B pos. 18: 1 CPF, 2 CNPJ


def _dv(base: list[int]) -> int:
    soma = sum(d * p for d, p in zip(base, range(len(base) + 1, 1, -1)))
    return (soma * 10 % 11) % 10


def _cpf_valido(d: str) -> bool:
    if len(d) != 11 or d == d[0] * 11:
        return False
    nums = [int(c) for c in d]
    return _dv(nums[:9]) == nums[9] and _dv(nums[:10]) == nums[10]


def _cnpj_valido(d: str) -> bool:
    if len(d) != 14 or d == d[0] * 14:
        return False
    nums = [int(c) for c in d]

    def dv(base, pesos):
        r = sum(n * p for n, p in zip(base, pesos)) % 11
        return 0 if r < 2 else 11 - r

    return (dv(nums[:12], [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]) == nums[12]
            and dv(nums[:13], [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]) == nums[13])


def _celular_valido(d: str) -> bool:
    """DDD (11-99) + 9 + 8 digitos = 11 digitos."""
    return len(d) == 11 and "11" <= d[:2] <= "99" and d[2] == "9"


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def classificar_chave(chave, cpf_motorista) -> "ChavePix | str":
    """ChavePix pronta pro arquivo, ou a MENSAGEM do motivo de nao poder entrar (vai pra tela).
    Nunca chuta: chave de 11 digitos que serve como CPF e como celular e ambigua."""
    bruta = str(chave or "").strip()
    if not bruta:
        return "sem chave Pix cadastrada"
    cpf_m = _digitos(cpf_motorista)
    cpf_m_ok = _cpf_valido(cpf_m)
    v = bruta.lower()

    def com_doc_do_motorista(tipo, valor):
        if not cpf_m_ok:
            return "motorista sem CPF válido no cadastro"
        return ChavePix(tipo, valor, cpf_m, 1)

    if "@" in v:
        if not _EMAIL.match(v) or len(v) > 77:
            return "formato inválido (e-mail)"
        return com_doc_do_motorista("02", v)
    if _UUID.match(v):
        return com_doc_do_motorista("04", v)

    if not re.fullmatch(r"\+?[\d.\-/\s()]+", bruta):
        return "formato inválido (chave Pix)"
    d = _digitos(bruta)
    if bruta.startswith("+") or (len(d) == 13 and d.startswith("55")):
        if len(d) == 13 and d.startswith("55") and _celular_valido(d[2:]):
            return com_doc_do_motorista("01", "+" + d)
        return "formato inválido (telefone)"
    if len(d) == 14:
        return ChavePix("03", d, d, 2) if _cnpj_valido(d) else "formato inválido (CNPJ)"
    if len(d) == 11:
        if cpf_m_ok and d == cpf_m:
            return ChavePix("03", d, d, 1)
        cpf_ok, cel_ok = _cpf_valido(d), _celular_valido(d)
        if cpf_ok and cel_ok:
            return "chave ambígua (serve como CPF e como celular): confirmar o tipo da chave com o motorista"
        if cpf_ok:
            return "chave CPF de outro titular (diferente do CPF do motorista)"
        if cel_ok:
            return com_doc_do_motorista("01", "+55" + d)
    return "formato inválido (chave Pix)"


# ── Arquivo de remessa (CNAB 240) ───────────────────────────────────────────

import unicodedata
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

CODIGO_BANCO = "341"
FORMA_PIX_TRANSFERENCIA = "45"          # nota 5 do manual


@dataclass
class ContaPagadora:
    cnpj: str
    agencia: str
    conta: str
    dac: str
    nome_empresa: str
    tipo_pagamento: str = "20"           # nota 4: 20 Fornecedores


@dataclass
class ItemRemessa:
    pagamento_id: int
    nome: str
    valor: Decimal
    chave: ChavePix


def limpar_nome(nome, n: int) -> str:
    """Maiusculo, ASCII, so letras/numeros/espaco, espacos colapsados, truncado em n."""
    t = unicodedata.normalize("NFKD", str(nome or "")).encode("ascii", "ignore").decode("ascii").upper()
    t = re.sub(r"[^A-Z0-9]+", " ", t).strip()
    return t[:n].rstrip()


def para_centavos(valor) -> int:
    """Inteiro de centavos, sem erro de ponto flutuante (0.1 + 0.2 -> 30)."""
    return int((Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100).to_integral_value())


def _reg(campos: list) -> str:
    """Registro de 240 posicoes: campos = [(ini, fim, texto)] com posicoes do manual (base 1).
    O texto ja vem no tamanho exato do campo; o que nao e citado fica em branco."""
    linha = [" "] * 240
    for ini, fim, texto in campos:
        if len(texto) != fim - ini + 1:
            raise AssertionError(f"campo {ini}-{fim} com {len(texto)} posicoes: {texto!r}")
        linha[ini - 1:fim] = list(texto)
    out = "".join(linha)
    if len(out) != 240:
        raise AssertionError("registro fora de 240 posicoes")
    return out


def _num(v, n: int) -> str:
    s = str(int(v))
    if len(s) > n:
        raise ValueError(f"numero {s} nao cabe em {n} posicoes")
    return s.rjust(n, "0")


def _alfa(v, n: int) -> str:
    return str(v or "")[:n].ljust(n)


def _validar_conta(c: ContaPagadora) -> None:
    if not (_digitos(c.cnpj) and len(_digitos(c.cnpj)) == 14 and _digitos(c.agencia) and len(_digitos(c.agencia)) <= 5
            and _digitos(c.conta) and len(_digitos(c.conta)) <= 12 and len(_digitos(c.dac)) == 1
            and limpar_nome(c.nome_empresa, 30) and _digitos(c.tipo_pagamento)):
        raise ValueError("dados da conta pagadora incompletos ou invalidos (itau_remessa no config)")


def _conta_campos(c: ContaPagadora) -> list:
    return [(18, 18, "2"), (19, 32, _num(_digitos(c.cnpj), 14)), (53, 57, _num(_digitos(c.agencia), 5)),
            (59, 70, _num(_digitos(c.conta), 12)), (72, 72, _digitos(c.dac)),
            (73, 102, _alfa(limpar_nome(c.nome_empresa, 30), 30))]


def montar_arquivo(conta: ContaPagadora, itens: list, data_pagamento: date, agora: datetime) -> str:
    """Arquivo de remessa Pix: header de arquivo (0), header de lote (1), por pagamento A + B (3),
    trailer de lote (5), trailer de arquivo (9). Registros separados por CRLF, terminando em CRLF."""
    if not itens:
        raise ValueError("remessa sem pagamentos")
    if data_pagamento < agora.date():
        raise ValueError("data de pagamento anterior a hoje")
    _validar_conta(conta)
    for it in itens:
        if para_centavos(it.valor) <= 0:
            raise ValueError(f"valor invalido no pagamento {it.pagamento_id}")

    linhas = [_reg([  # header de arquivo (manual p. 12)
        (1, 3, CODIGO_BANCO), (4, 7, "0000"), (8, 8, "0"), (15, 17, "080"), *_conta_campos(conta),
        (103, 132, _alfa("BANCO ITAU SA", 30)), (143, 143, "1"),
        (144, 151, agora.strftime("%d%m%Y")), (152, 157, agora.strftime("%H%M%S")),
        (158, 166, "0" * 9), (167, 171, "0" * 5)])]
    linhas.append(_reg([  # header de lote (p. 13): C, tipo de pagamento, forma 45, layout de lote 040
        (1, 3, CODIGO_BANCO), (4, 7, "0001"), (8, 8, "1"), (9, 9, "C"),
        (10, 11, _num(_digitos(conta.tipo_pagamento), 2)), (12, 13, FORMA_PIX_TRANSFERENCIA), (14, 16, "040"),
        *_conta_campos(conta), (173, 177, "0" * 5), (213, 220, "0" * 8)]))
    total = 0
    for seq, it in enumerate(itens, 1):
        cent = para_centavos(it.valor)
        total += cent
        doc = _num(_digitos(it.chave.documento), 14)
        linhas.append(_reg([  # segmento A (p. 15)
            (1, 3, CODIGO_BANCO), (4, 7, "0001"), (8, 8, "3"), (9, 13, _num(seq, 5)), (14, 14, "A"),
            (15, 17, "000"), (18, 20, "009"), (21, 23, "000"),
            (44, 73, _alfa(limpar_nome(it.nome, 30), 30)), (74, 93, _alfa(str(it.pagamento_id), 20)),
            (94, 101, data_pagamento.strftime("%d%m%Y")), (102, 104, "REA"),
            (113, 114, "04"), (115, 119, "0" * 5), (120, 134, _num(cent, 15)),
            (155, 162, "0" * 8), (163, 177, "0" * 15), (198, 203, "0" * 6), (204, 217, doc), (230, 230, "0")]))
        linhas.append(_reg([  # segmento B para Pix (p. 18): tipo e valor da chave
            (1, 3, CODIGO_BANCO), (4, 7, "0001"), (8, 8, "3"), (9, 13, _num(seq, 5)), (14, 14, "B"),
            (15, 16, it.chave.tipo), (18, 18, str(it.chave.tipo_inscricao)), (19, 32, doc),
            (128, 227, _alfa(it.chave.valor, 100))]))
    linhas.append(_reg([  # trailer de lote (p. 24): registros do lote = header + detalhes + trailer
        (1, 3, CODIGO_BANCO), (4, 7, "0001"), (8, 8, "5"), (18, 23, _num(len(itens) * 2 + 2, 6)),
        (24, 41, _num(total, 18)), (42, 59, "0" * 18)]))
    linhas.append(_reg([  # trailer de arquivo (p. 43): 1 lote; registros = todos, ate este
        (1, 3, CODIGO_BANCO), (4, 7, "9999"), (8, 8, "9"), (18, 23, _num(1, 6)),
        (24, 29, _num(len(linhas) + 1, 6))]))
    return "".join(l + chr(13) + chr(10) for l in linhas)
# Tabela de ocorrencias do retorno (nota 8 do manual). Texto copiado do manual; ver docs/referencias.
OCORRENCIAS = {
    "00": "PAGAMENTO EFETUADO",
    "AE": "DATA DE PAGAMENTO ALTERADA",
    "AG": "NÚMERO DO LOTE INVÁLIDO",
    "AH": "NÚMERO SEQUENCIAL DO REGISTRO NO LOTE INVÁLIDO",
    "AI": "PRODUTO DEMONSTRATIVO DE PAGAMENTO NÃO CONTRATADO",
    "AJ": "TIPO DE MOVIMENTO INVÁLIDO",
    "AL": "CÓDIGO DO BANCO FAVORECIDO INVÁLIDO",
    "AM": "AGÊNCIA DO FAVORECIDO INVÁLIDA",
    "AN": "CONTA CORRENTE DO FAVORECIDO INVÁLIDA",
    "AO": "NOME DO FAVORECIDO INVÁLIDO",
    "AP": "DATA DE PAGAMENTO / DATA DE VALIDADE / HORA DE LANÇAMENTO / ARRECADAÇÃO / APURAÇÃO INVÁLIDA",
    "AQ": "QUANTIDADE DE REGISTROS MAIOR QUE 999999",
    "AR": "VALOR ARRECADADO / LANÇAMENTO INVÁLIDO",
    "BC": "NOSSO NÚMERO INVÁLIDO",
    "BD": "PAGAMENTO AGENDADO",
    "BE": "PAGAMENTO AGENDADO COM FORMA ALTERADA PARA OP",
    "BI": "CNPJ / CPF DO FAVORECIDO NO SEGMENTO J-52 ou B INVÁLIDO / DOCUMENTO FAVORECIDO INVÁLIDO PIX",
    "BL": "VALOR DA PARCELA INVÁLIDO",
    "CD": "CNPJ / CPF INFORMADO DIVERGENTE DO CADASTRADO",
    "CE": "PAGAMENTO CANCELADO",
    "CF": "VALOR DO DOCUMENTO INVÁLIDO / VALOR DIVERGENTE DO QR CODE",
    "CG": "VALOR DO ABATIMENTO INVÁLIDO",
    "CH": "VALOR DO DESCONTO INVÁLIDO",
    "CI": "CNPJ / CPF / IDENTIFICADOR / INSCRIÇÃO ESTADUAL / INSCRIÇÃO NO CAD / ICMS INVÁLIDO",
    "CJ": "VALOR DA MULTA INVÁLIDO",
    "CK": "TIPO DE INSCRIÇÃO INVÁLIDA",
    "CL": "VALOR DO INSS INVÁLIDO",
    "CM": "VALOR DO COFINS INVÁLIDO",
    "CN": "CONTA NÃO CADASTRADA",
    "CO": "VALOR DE OUTRAS ENTIDADES INVÁLIDO",
    "CP": "CONFIRMAÇÃO DE OP CUMPRIDA",
    "CQ": "SOMA DAS FATURAS DIFERE DO PAGAMENTO",
    "CR": "VALOR DO CSLL INVÁLIDO",
    "CS": "DATA DE VENCIMENTO DA FATURA INVÁLIDA",
    "D0": "FINALIDADE DO HOLERITE INVÁLIDA",
    "D1": "MÊS DE COMPETENCIA DO HOLERITE INVÁLIDA",
    "D2": "DIA DA COMPETENCIA DO HOLETITE INVÁLIDA",
    "D3": "CENTRO DE CUSTO INVÁLIDO",
    "D4": "CAMPO NUMÉRICO DA FUNCIONAL INVÁLIDO",
    "D5": "DATA INÍCIO DE FÉRIAS NÃO NUMÉRICA",
    "D6": "DATA INÍCIO DE FÉRIAS INCONSISTENTE",
    "D7": "DATA FIM DE FÉRIAS NÃO NUMÉRICO",
    "D8": "DATA FIM DE FÉRIAS INCONSISTENTE",
    "D9": "NÚMERO DE DEPENDENTES IR INVÁLIDO",
    "DA": "NÚMERO DE DEPEND. SALÁRIO FAMILIA INVALIDO",
    "DB": "NÚMERO DE HORAS SEMANAIS INVÁLIDO",
    "DC": "SALÁRIO DE CONTRIBUIÇÃO INSS INVÁLIDO",
    "DD": "SALÁRIO DE CONTRIBUIÇÃO FGTS INVÁLIDO",
    "DE": "VALOR TOTAL DOS PROVENTOS INVÁLIDO",
    "DF": "VALOR TOTAL DOS DESCONTOS INVÁLIDO",
    "DG": "VALOR LÍQUIDO NÃO NUMÉRICO",
    "DH": "VALOR LIQ. INFORMADO DIFERE DO CALCULADO",
    "DI": "VALOR DO SALÁRIO-BASE INVÁLIDO",
    "DJ": "BASE DE CÁLCULO IRRF INVÁLIDA",
    "DK": "BASE DE CÁLCULO FGTS INVÁLIDA",
    "DL": "FORMA DE PAGAMENTO INCOMPATÍVEL COM HOLERITE",
    "DM": "E-MAIL DO FAVORECIDO INVÁLIDO",
    "DV": "DOC / TED DEVOLVIDO PELO BANCO FAVORECIDO",
    "E0": "TIPO DE MOVIMENTO HOLERITE INVÁLIDO",
    "E1": "VALOR 01 DO HOLERITE / INFORME INVÁLIDO",
    "E2": "VALOR 02 DO HOLERITE / INFORME INVÁLIDO",
    "E3": "VALOR 03 DO HOLERITE / INFORME INVÁLIDO",
    "E4": "VALOR 04 DO HOLERITE / INFORME INVÁLIDO",
    "EM": "CONFIRMAÇÃO DE OP EMITIDA",
    "EX": "DEVOLUÇÃO DE OP NÃO SACADA PELO FAVORECIDO",
    "FC": "PAGAMENTO EFETUADO ATRAVÉS DE FINANCIAMENTO COMPROR",
    "FD": "PAGAMENTO EFETUADO ATRAVÉS DE FINANCIAMENTO DESCOMPROR",
    "HA": "HÁ ERRO NO LOTE",
    "HM": "ERRO NO REGISTRO HEADER DE ARQUIVO",
    "IB": "VALOR DO DOCUMENTO INVÁLIDO",
    "IC": "VALOR DO ABATIMENTO INVÁLIDO",
    "ID": "VALOR DO DESCONTO INVÁLIDO",
    "IE": "VALOR DA MORA INVÁLIDO",
    "IF": "VALOR DA MULTA INVÁLIDO",
    "IG": "VALOR DA DEDUÇÃO INVÁLIDO",
    "IH": "VALOR DO ACRÉSCIMO INVÁLIDO",
    "II": "DATA DE VENCIMENTO INVÁLIDA / QR CODE EXPIRADO",
    "IJ": "COMPETÊNCIA / PERÍODO REFERÊNCIA / PARCELA INVÁLIDA",
    "IK": "TRIBUTO NÃO LIQUIDÁVEL VIA SISPAG OU NÃO CONVENIADO COM ITAÚ",
    "IL": "CÓDIGO DE PAGAMENTO / EMPRESA /RECEITA INVÁLIDO",
    "IM": "TIPO X FORMA NÃO COMPATÍVEL",
    "IN": "BANCO/AGÊNCIA NÃO CADASTRADOS",
    "IO": "DAC / VALOR / COMPETÊNCIA / IDENTIFICADOR DO LACRE INVÁLIDO / IDENTIFICAÇÃO DO QR CODE INVÁLIDO",
    "IP": "DAC DO CÓDIGO DE BARRAS INVÁLIDO / ERRO NA VALIDAÇÃO DO QR CODE",
    "IQ": "DÍVIDA ATIVA OU NÚMERO DE ETIQUETA INVÁLIDO",
    "IR": "PAGAMENTO ALTERADO",
    "IS": "CONCESSIONÁRIA NÃO CONVENIADA COM ITAÚ",
    "IT": "VALOR DO TRIBUTO INVÁLIDO",
    "IU": "VALOR DA RECEITA BRUTA ACUMULADA INVÁLIDO",
    "IV": "NÚMERO DO DOCUMENTO ORIGEM / REFERÊNCIA INVÁLIDO",
    "IX": "CÓDIGO DO PRODUTO INVÁLIDO",
    "LA": "DATA DE PAGAMENTO DE UM LOTE ALTERADA",
    "LC": "LOTE DE PAGAMENTOS CANCELADO",
    "NA": "PAGAMENTO CANCELADO POR FALTA DE AUTORIZAÇÃO",
    "NB": "IDENTIFICAÇÃO DO TRIBUTO INVÁLIDA",
    "NC": "EXERCÍCIO (ANO BASE) INVÁLIDO",
    "ND": "CÓDIGO RENAVAM NÃO ENCONTRADO/INVÁLIDO",
    "NE": "UF INVÁLIDA",
    "NF": "CÓDIGO DO MUNICÍPIO INVÁLIDO",
    "NG": "PLACA INVÁLIDA",
    "NH": "OPÇÃO/PARCELA DE PAGAMENTO INVÁLIDA",
    "NI": "TRIBUTO JÁ FOI PAGO OU ESTÁ VENCIDO",
    "NR": "OPERAÇÃO NÃO REALIZADA",
    "PD": "AQUISIÇÃO CONFIRMADA (EQUIVALE A OCORRÊNCIA 02 NO LAYOUT DE RISCO SACADO)",
    "RJ": "REGISTRO REJEITADO",
    "RS": "PAGAMENTO DISPONÍVEL PARA ANTECIPAÇÃO NO RISCO SACADO – MODALIDADE RISCO SACADO PÓS AUTORIZADO",
    "SS": "PAGAMENTO CANCELADO POR INSUFICIÊNCIA DE SALDO / LIMITE DIÁRIO DE PAGTO EXCEDIDO",
    "TA": "LOTE NÃO ACEITO - TOTAIS DO LOTE COM DIFERENÇA",
    "TI": "TITULARIDADE INVÁLIDA",
    "X1": "FORMA INCOMPATÍVEL COM LAYOUT 010",
    "X2": "NÚMERO DA NOTA FISCAL INVÁLIDO",
    "X3": "IDENTIFICADOR DE NF/CNPJ INVÁLIDO",
    "X4": "FORMA 32 INVÁLIDA",
}


# Ocorrencias que NAO sao falha nem pagamento efetuado: o pagamento segue em andamento no banco.
INFORMATIVAS = {"BD", "AE", "LA", "IR", "BE", "CP", "EM", "PD", "RS", "FC", "FD"}


@dataclass
class RegistroRetorno:
    pagamento_id: "int | None"      # segmento A pos. 74-93 (Seu Numero = id do pagamento)
    ocorrencias: list               # pos. 231-240, ate 5 codigos de 2 posicoes
    data_efetiva: "date | None"     # pos. 155-162
    valor_efetivo: "Decimal | None"  # pos. 163-177 (centavos)
    valor_previsto: Decimal         # pos. 120-134 (centavos)


def _linhas240(texto: str) -> list:
    linhas = []
    for n, linha in enumerate(str(texto or "").splitlines(), 1):
        if not linha.strip():
            continue
        if len(linha) != 240:
            raise ValueError(f"linha {n} com {len(linha)} posições (esperado 240)")
        linhas.append(linha)
    return linhas


def _codigos(campo: str) -> list:
    return [campo[i:i + 2] for i in range(0, len(campo), 2) if campo[i:i + 2].strip()]


def _data_ddmmaaaa(txt: str):
    if not txt.isdigit() or int(txt) == 0:
        return None
    try:
        return date(int(txt[4:]), int(txt[2:4]), int(txt[:2]))
    except ValueError:
        return None


def _reais(txt: str):
    return Decimal(int(txt)) / 100 if txt.isdigit() else None


def ler_retorno(texto: str) -> list:
    """Segmentos A do arquivo de retorno (registro tipo 3, segmento A), na ordem do arquivo."""
    regs = []
    for linha in _linhas240(texto):
        if linha[7] == "3" and linha[13] == "A":
            seu = linha[73:93].strip()
            regs.append(RegistroRetorno(
                pagamento_id=int(seu) if seu.isdigit() else None,
                ocorrencias=_codigos(linha[230:240]),
                data_efetiva=_data_ddmmaaaa(linha[154:162]),
                valor_efetivo=_reais(linha[162:177]),
                valor_previsto=_reais(linha[119:134]) or Decimal("0")))
    return regs


def ocorrencias_de_lote(texto: str) -> list:
    """Ocorrencias nos headers/trailers de lote (ex.: TA lote nao aceito, HA erro no lote)."""
    codigos = []
    for linha in _linhas240(texto):
        if linha[7] in ("1", "5"):
            codigos += _codigos(linha[230:240])
    return codigos
