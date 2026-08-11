# -*- coding: utf-8 -*-
"""
matcher.py

Descobre a qual pedido (PS-XXXXX) um documento pertence -- pedido do
Hugo, 05/08 ("tenta pelo nome/assunto primeiro, cai pro conteúdo do
PDF se não achar") + spec de boletos parcelados de 11/08 (casamento
de boleto via número da NF-e da DANFE).

Hierarquia de casamento (casar_documento_com_pedido):
  1. Código do pedido (PS-XXXXX) direto no nome do arquivo, no
     assunto do e-mail ou no texto do PDF -- rápido, sem API.
  2. [Boleto] (numero_nf, cnpj_pagador) do boleto bate com uma DANFE
     já indexada (IndexadorNF) -- resolve o caso de cliente com
     VÁRIOS pedidos em aberto, onde o CNPJ sozinho é ambíguo.
  3. [Boleto] numero_nf bate com exatamente 1 pedido no índice (e o
     CNPJ não conflita com o registrado).
  4. CNPJ no VUUPT (buscar_customer_por_code) -> serviços do cliente.
     Só casa se achar exatamente 1 -- 0 ou N ficam pra revisão manual
     (melhor não adivinhar errado do que grudar documento no pedido
     errado).
  (A "Regra 4" da spec -- CNPJ + valor da duplicata -- não foi
  implementada: o serviço do VUUPT não carrega valor de NF/parcela,
  confirmado na API real em 11/08, então não há contra o que cruzar.)
"""
import logging
import re

logger = logging.getLogger(__name__)

PADRAO_CODIGO_PEDIDO = re.compile(r"PS-?\d{4,6}", re.IGNORECASE)
# Guardas (?<!\d)/(?!\d): sem elas o padrão casa 14 dígitos dentro de
# sequências longas (chave de acesso de NF-e, linha digitável de
# boleto) -- falso CNPJ visto em documento real ("NFS SP.pdf", 10/08).
PADRAO_CNPJ = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")

# Número da NF na DANFE (layout NFePHP visto em todas as DANFEs reais
# do projeto): "Nº. 000.149.747" logo abaixo de "NF-e" no cabeçalho.
PADRAO_NF_DANFE = re.compile(r"N[ºo°]\.?\s*([\d.]{6,12})")

# Seção do destinatário na DANFE. Precisa ser o cabeçalho de seção
# "DESTINATÁRIO / REMETENTE" -- a palavra "DESTINATÁRIO:" também
# aparece antes, no canhoto ("RECEBEMOS DE ..."), e o CNPJ seguinte
# àquela ocorrência é o do EMITENTE (erro visto em DANFE real).
PADRAO_SECAO_DESTINATARIO = re.compile(r"DESTINAT[ÁA]RIO\s*/\s*REMETENTE", re.IGNORECASE)


def _normalizar_codigo(bruto: str) -> str:
    """'ps12345' ou 'PS-12345' ou 'ps_12345' -> 'PS-12345' (formato
    padrão usado em todo o projeto)."""
    digitos = re.sub(r"\D", "", bruto)
    return f"PS-{digitos}"


def extrair_codigo_pedido(texto: str) -> str | None:
    match = PADRAO_CODIGO_PEDIDO.search(texto or "")
    return _normalizar_codigo(match.group(0)) if match else None


def extrair_cnpj(texto: str) -> str | None:
    """Retorna só os dígitos do primeiro CNPJ encontrado (14 dígitos),
    formato compatível com o campo 'code' dos clientes no VUUPT."""
    match = PADRAO_CNPJ.search(texto or "")
    if not match:
        return None
    digitos = re.sub(r"\D", "", match.group(0))
    return digitos if len(digitos) == 14 else None


def extrair_nf_da_danfe(texto: str) -> tuple[str | None, str | None]:
    """(numero_nf, cnpj_destinatario) do texto de uma DANFE. O
    destinatário é quem paga o boleto dessa NF -- é o par que o
    IndexadorNF guarda. Retorna (None, None) no que não achar."""
    numero_nf = None
    m_nf = PADRAO_NF_DANFE.search(texto or "")
    if m_nf:
        digitos = re.sub(r"\D", "", m_nf.group(1)).lstrip("0")
        numero_nf = digitos or None

    cnpj_destinatario = None
    m_secao = PADRAO_SECAO_DESTINATARIO.search(texto or "")
    if m_secao:
        m_cnpj = PADRAO_CNPJ.search(texto, m_secao.end())
        if m_cnpj:
            digitos = re.sub(r"\D", "", m_cnpj.group(0))
            if len(digitos) == 14:
                cnpj_destinatario = digitos

    return numero_nf, cnpj_destinatario


class IndexadorNF:
    """
    Índice em memória { numero_nf -> pedido(s) } construído a partir
    das DANFEs já processadas (banco, via fingerprint.carregar_indice_nf)
    + das DANFEs da execução atual (registrar()). É a ponte que casa
    boleto com pedido quando o CNPJ sozinho é ambíguo.
    """

    def __init__(self):
        self._por_nf: dict[str, set[str]] = {}          # nf -> {PS-...}
        self._cnpj_por_nf_pedido: dict[tuple[str, str], str | None] = {}

    def carregar_do_banco(self):
        from fingerprint_documentos import carregar_indice_nf
        for row in carregar_indice_nf():
            self.registrar(row["numero_nf"], row["cnpj_contraparte"], row["codigo_pedido"])
        logger.info(f"Índice NF->pedido carregado: {len(self._por_nf)} NF(s) conhecida(s).")

    def registrar(self, numero_nf: str | None, cnpj_destinatario: str | None, codigo_pedido: str):
        if not numero_nf or not codigo_pedido:
            return
        self._por_nf.setdefault(numero_nf, set()).add(codigo_pedido)
        self._cnpj_por_nf_pedido[(numero_nf, codigo_pedido)] = cnpj_destinatario

    def resolver(self, numero_nf: str | None, cnpj_pagador: str | None) -> dict | None:
        """{"codigo_pedido", "metodo"} ou None se não der pra afirmar."""
        if not numero_nf or numero_nf not in self._por_nf:
            return None
        pedidos = self._por_nf[numero_nf]

        # Regra 2: NF + CNPJ batem com uma DANFE indexada
        if cnpj_pagador:
            for codigo_ps in pedidos:
                if self._cnpj_por_nf_pedido.get((numero_nf, codigo_ps)) == cnpj_pagador:
                    return {"codigo_pedido": codigo_ps, "metodo": "nf_cnpj_danfe"}

        # Regra 3: NF única no índice -- aceita desde que o CNPJ não
        # CONFLITE com o registrado (registrado None = sem conflito)
        if len(pedidos) == 1:
            codigo_ps = next(iter(pedidos))
            cnpj_registrado = self._cnpj_por_nf_pedido.get((numero_nf, codigo_ps))
            if cnpj_pagador and cnpj_registrado and cnpj_registrado != cnpj_pagador:
                return None
            return {"codigo_pedido": codigo_ps, "metodo": "nf_unica_danfe"}

        return None


def extrair_cnpj_pagador_boleto(texto: str) -> str | None:
    """Pro CNPJ que interessa num BOLETO: o do PAGADOR (cliente
    destinatário, ligado a UM pedido específico) -- não o do
    Beneficiário (o embarcador que emitiu o boleto, sempre o MESMO
    CNPJ em todos os boletos dele, então inútil pra descobrir qual
    pedido é -- lição de pedidos reais da Dourado, 10/08, onde o
    primeiro CNPJ do texto sempre batia com o próprio embarcador).
    Procura o primeiro CNPJ depois da palavra "Pagador"; cai pro
    genérico (primeiro CNPJ do documento) se não achar esse padrão."""
    if texto:
        pos = texto.lower().find("pagador")
        if pos != -1:
            match = PADRAO_CNPJ.search(texto, pos)
            if match:
                digitos = re.sub(r"\D", "", match.group(0))
                if len(digitos) == 14:
                    return digitos
    return extrair_cnpj(texto)


def casar_documento_com_pedido(nome_arquivo: str, assunto_email: str | None,
                               texto_pdf: str, vuupt, tipo_documento: str | None = None,
                               metadados_boleto: dict | None = None,
                               indexador_nf: "IndexadorNF | None" = None) -> dict:
    """
    Retorna {"codigo_pedido": "PS-XXXXX" ou None, "metodo": "...",
    "motivo_falha": "..." (só quando codigo_pedido é None)}.

    metadados_boleto: saída de boleto_parser.extrair_metadados_boleto()
    -- habilita as regras 2/3 (NF da DANFE) quando tipo_documento é
    "Boleto" e indexador_nf foi informado. Ver hierarquia na docstring
    do módulo.
    """
    # Regra 1: código do pedido direto no nome, assunto ou texto
    codigo = extrair_codigo_pedido(nome_arquivo)
    if codigo:
        return {"codigo_pedido": codigo, "metodo": "nome_arquivo", "motivo_falha": None}

    if assunto_email:
        codigo = extrair_codigo_pedido(assunto_email)
        if codigo:
            return {"codigo_pedido": codigo, "metodo": "assunto_email", "motivo_falha": None}

    codigo = extrair_codigo_pedido(texto_pdf)
    if codigo:
        return {"codigo_pedido": codigo, "metodo": "texto_pdf", "motivo_falha": None}

    # Regras 2 e 3: boleto casado com a DANFE pelo número da NF
    numero_nf = None
    if tipo_documento == "Boleto" and metadados_boleto:
        numero_nf = metadados_boleto.get("numero_nf")
        if indexador_nf and numero_nf:
            resolvido = indexador_nf.resolver(numero_nf, metadados_boleto.get("cnpj_pagador"))
            if resolvido:
                return {**resolvido, "motivo_falha": None}

    # Regra final: CNPJ -> cliente no VUUPT com exatamente 1 serviço
    if tipo_documento == "Boleto":
        cnpj = ((metadados_boleto or {}).get("cnpj_pagador")
                or extrair_cnpj_pagador_boleto(texto_pdf))
    else:
        cnpj = extrair_cnpj(texto_pdf)

    # Nos motivos de falha de boleto, registra a NF extraída -- é a
    # informação que a revisão manual precisa (basta processar a DANFE
    # desse pedido que a próxima execução casa sozinha via índice).
    sufixo_nf = f" NF {numero_nf} não está no índice de DANFEs." if numero_nf else ""
    if not cnpj:
        return {"codigo_pedido": None, "metodo": None,
                "motivo_falha": "Sem código de pedido no nome/assunto, e sem CNPJ reconhecível no PDF."
                                + sufixo_nf}

    try:
        cliente = vuupt.buscar_customer_por_code(cnpj)
    except Exception as e:
        return {"codigo_pedido": None, "metodo": None,
                "motivo_falha": f"CNPJ {cnpj} encontrado no PDF, mas falha ao buscar cliente no VUUPT: {e}"}

    if not cliente:
        return {"codigo_pedido": None, "metodo": None,
                "motivo_falha": f"CNPJ {cnpj} encontrado no PDF, mas nenhum cliente com esse código no VUUPT."}

    try:
        servicos = vuupt.listar_servicos(
            [{"field": "customer_id", "operator": "eq", "value": cliente["id"]}], per_page=50
        )
    except Exception as e:
        return {"codigo_pedido": None, "metodo": None,
                "motivo_falha": f"Cliente do CNPJ {cnpj} encontrado, mas falha ao buscar serviços dele: {e}"}

    if len(servicos) == 1:
        codigo_encontrado = servicos[0].get("code", "").lstrip("#")
        return {"codigo_pedido": codigo_encontrado, "metodo": "cnpj_pdf", "motivo_falha": None}
    if len(servicos) == 0:
        return {"codigo_pedido": None, "metodo": None,
                "motivo_falha": f"CNPJ {cnpj} casou com um cliente no VUUPT, mas ele não tem nenhum serviço."
                                + sufixo_nf}
    return {"codigo_pedido": None, "metodo": None,
            "motivo_falha": f"CNPJ {cnpj} casou com um cliente que tem {len(servicos)} serviços -- "
                            f"ambíguo demais pra casar automaticamente." + sufixo_nf}
